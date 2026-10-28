"""论文卡片的懒生成服务 ``PaperCardService``(规格 §4.5)。

卡片是 ``is_paper=1`` 的来源由原文确定性派生的缓存:读的时候判有效
(``status='ready'`` ∧ ``basis`` = 当前卡片见证 ∧ ``card_version`` = 代码常量 ∧ 来源仍是论文),
缺失或过期的在本次调用的生成上限内懒生成,其余返回 ``deferred``。

调用方负责权限:``allowed`` 是调用方算好的 ``(所属库 id, source_id)`` 集合(问答是本次运行
的来源天花板,MCP 是凭证主人可读的可见 ∪ 自己的隐藏来源);集合外的条目一律 ``not_found``,
服务只在检索层这一个入口收窄(retrieval-owns-permissions)。

并发:元数据与卡片的模型调用在有界线程池里跑(``contextvars`` 传播调用方的
``model_work_scope`` 与检索运行状态),数据库读写包在 ``retrieval_fanout_slot`` 里;
同一来源的并发请求进程内单飞,后来者在池外等待、轮询取消。队列满/等待超时只降级为
``deferred``,不记失败。
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.core.llm import cap_kwargs
from app.domain.cancellation import CancelEvent, CoreCancellation, raise_if_cancelled
from app.domain.promotion_source import PROMOTION_SOURCE_TYPE
from app.repositories.ports import RepositoryDatabasePort, SourceStorePort
from app.services.document_source_overview import PaperCardSample, sample_paper_card_input
from app.services.kg.json_utils import safe_json
from app.services.model_work import ModelQueueFull, ModelQueueTimeout, model_artifact_scope
from app.services.paper_cards import (
    PAPER_CARD_SCHEMA_HINT,
    PAPER_CARD_VERSION,
    card_is_empty,
    dropped_count,
    paper_card_prompt,
    verify_paper_card,
)
from app.services.paper_meta import paper_meta_doc_type_eligible
from app.services.retrieval_run import retrieval_fanout_slot

CARD_STATUSES = (
    "ready", "generated", "not_paper", "meta_pending", "failed", "deferred", "not_found",
)
# 连续失败 ≥3 次后的固定冷却(规格 §4.4 / §9 第 3 条)。
LONG_COOLDOWN_SECONDS = 24 * 3600
LONG_COOLDOWN_ATTEMPTS = 3
_SYNTHETIC_SOURCE_TYPES = ("memory", "knowhow", PROMOTION_SOURCE_TYPE)
_WAIT_POLL_SECONDS = 0.1
_WORKLOAD = "paper_metadata"


@dataclass(frozen=True)
class CardResult:
    """一篇的结果。``card`` 只在 ``ready`` / ``generated`` 时非空;``reason`` 是机器可读的
    原因码(如 ``out_of_scope``、``cooldown``、``generate_limit``、``model_queue``)。"""

    notebook_id: str
    source_id: str
    status: str
    card: dict = field(default_factory=dict)
    reason: str = ""
    model: str = ""
    dropped: int = 0


@dataclass
class _Plan:
    notebook_id: str
    source_id: str
    needs_meta: bool
    calls: int = 0


@dataclass(frozen=True)
class _Snapshot:
    listing: dict[str, Mapping[str, Any]]
    cards: dict[str, dict]
    bases: dict[str, str]
    mirrored: bool


class _Flight:
    __slots__ = ("done",)

    def __init__(self) -> None:
        self.done = threading.Event()


class _FlightTable:
    """进程内单飞:同一 source_id 同时只有一个生成者,其余等它结束后重读缓存。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

    def join(self, key: str) -> tuple[_Flight, bool]:
        with self._lock:
            current = self._flights.get(key)
            if current is not None:
                return current, False
            flight = _Flight()
            self._flights[key] = flight
            return flight, True

    def finish(self, key: str, flight: _Flight) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
        flight.done.set()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperCardService:
    def __init__(
        self,
        *,
        settings: Any,
        database: RepositoryDatabasePort,
        sources: SourceStorePort,
        ingestion: Any,
        model_clients: Any,
        sync_origin: Callable[[str], str],
        event_log: Any,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._settings = settings
        self._database = database
        self._sources = sources
        # SourceIngestionService:可取消的元数据抽取变体与 doc_type 归一化。
        self._ingestion = ingestion
        self._models = model_clients
        self._sync_origin = sync_origin
        self._event_log = event_log
        self._now = now
        self._flights = _FlightTable()

    # ------------------------------------------------------------ entry
    def get_cards(
        self,
        items: Iterable[tuple[str, str]],
        *,
        allowed: Iterable[tuple[str, str]],
        generate_limit: int,
        cancel_event: CancelEvent = None,
        actor_id: str = "",
    ) -> list[CardResult]:
        """按输入顺序(去重后)返回每篇的 ``CardResult``。

        ``items`` 与 ``allowed`` 都是 ``(所属库 id, source_id)``;``generate_limit`` 是本次
        元数据 + 卡片模型调用的合计上限(需要先抽元数据的一篇按 2 次预留)。取消时抛
        ``AskCancelled``。
        """
        raise_if_cancelled(cancel_event)
        started = time.monotonic()
        ordered = list(dict.fromkeys((str(nb), str(sid)) for nb, sid in items))
        permitted = {(str(nb), str(sid)) for nb, sid in allowed}
        results: dict[tuple[str, str], CardResult] = {}
        by_notebook: dict[str, list[str]] = {}
        for key in ordered:
            if key in permitted:
                by_notebook.setdefault(key[0], []).append(key[1])
            else:
                results[key] = CardResult(*key, "not_found", reason="out_of_scope")
        plans: dict[tuple[str, str], _Plan] = {}
        for notebook_id, source_ids in by_notebook.items():
            raise_if_cancelled(cancel_event)
            snapshot = self._snapshot(notebook_id, source_ids)
            for source_id in source_ids:
                outcome = self._classify(notebook_id, source_id, snapshot)
                if isinstance(outcome, _Plan):
                    plans[(notebook_id, source_id)] = outcome
                else:
                    results[(notebook_id, source_id)] = outcome
        jobs = self._admit([plans[key] for key in ordered if key in plans], generate_limit, results)
        leaders, waiters = [], []
        for plan in jobs:
            flight, leader = self._flights.join(plan.source_id)
            (leaders if leader else waiters).append((plan, flight))
        results.update(self._run_leaders(leaders, cancel_event))
        results.update(self._await_waiters(waiters, cancel_event))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ordered_results = [results[key] for key in ordered]
        for result in ordered_results:
            self._emit(result, elapsed_ms, actor_id)
        return ordered_results

    # ------------------------------------------------------------ read side
    def _snapshot(self, notebook_id: str, source_ids: Sequence[str]) -> _Snapshot:
        """一个所属库的批量读:来源行(含 is_paper)、卡片行、卡片见证,每样一条 SQL。"""
        with retrieval_fanout_slot():
            with self._database.connect() as db:
                listing = {
                    row["id"]: row
                    for row in self._sources.source_listing_rows(db, source_ids)
                    if row["notebook_id"] == notebook_id
                }
                owned = [sid for sid in source_ids if sid in listing]
                cards = self._sources.paper_cards_for_sources(db, owned, notebook_id=notebook_id) if owned else {}
                bases = self._sources.source_card_bases(db, owned) if owned else {}
            mirrored = bool(owned) and bool(self._sync_origin(notebook_id))
        return _Snapshot(listing, cards, bases, mirrored)

    def _valid(self, row: Mapping[str, Any] | None, basis: str) -> bool:
        return (
            row is not None and row["status"] == "ready" and row["basis"] == basis
            and int(row["card_version"]) == PAPER_CARD_VERSION
        )

    def _cooling(self, row: Mapping[str, Any] | None, basis: str) -> bool:
        """同 basis 同版本的失败行仍在冷却期内(换了内容或版本的旧失败不压新尝试)。"""
        if (
            row is None or row["status"] != "failed" or row["basis"] != basis
            or int(row["card_version"]) != PAPER_CARD_VERSION
        ):
            return False
        window = (
            LONG_COOLDOWN_SECONDS if int(row["attempts"]) >= LONG_COOLDOWN_ATTEMPTS
            else int(self._settings.paper_card_retry_seconds)
        )
        try:
            updated = datetime.fromisoformat(str(row["updated_at"]))
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.astimezone(timezone.utc)
        return (self._now() - updated).total_seconds() < window

    def _classify(self, notebook_id: str, source_id: str, snapshot: _Snapshot) -> CardResult | _Plan:
        listing = snapshot.listing.get(source_id)
        if listing is None:
            return CardResult(notebook_id, source_id, "not_found", reason="missing")
        is_paper = listing["is_paper"]
        if is_paper is not None and not is_paper:
            return CardResult(notebook_id, source_id, "not_paper")
        row = snapshot.cards.get(source_id)
        basis = snapshot.bases.get(source_id, "")
        if is_paper is not None:
            if self._valid(row, basis):
                return CardResult(notebook_id, source_id, "ready", card=row["card"], model=row["model"])
            if self._cooling(row, basis):
                return CardResult(notebook_id, source_id, "failed", reason="cooldown", model=row["model"])
            if snapshot.mirrored:
                return CardResult(notebook_id, source_id, "meta_pending", reason="mirrored")
            if not basis:
                return CardResult(notebook_id, source_id, "meta_pending", reason="no_text")
            return _Plan(notebook_id, source_id, needs_meta=False)
        # 还没有元数据行(懒生成、不回填):不合格的类型直接不是论文,合格的先抽元数据。
        doc_type = self._ingestion.normalize_doc_type(str(listing["doc_type"] or ""))
        if listing["source_type"] in _SYNTHETIC_SOURCE_TYPES or not paper_meta_doc_type_eligible(doc_type):
            return CardResult(notebook_id, source_id, "not_paper")
        if snapshot.mirrored:
            return CardResult(notebook_id, source_id, "meta_pending", reason="mirrored")
        return _Plan(notebook_id, source_id, needs_meta=True)

    def _admit(self, plans: list[_Plan], generate_limit: int,
               results: dict[tuple[str, str], CardResult]) -> list[_Plan]:
        """按输入顺序预留模型调用:卡片 1 次,需先抽元数据的 2 次(预算只剩 1 时只做元数据,
        是论文的话卡片留到下次)。预留不退回,所以哪几篇被推迟只取决于输入顺序。"""
        enabled = bool(getattr(self._settings, "paper_card_enabled", True))
        remaining = max(0, int(generate_limit))
        admitted = []
        for plan in plans:
            key = (plan.notebook_id, plan.source_id)
            if not enabled:
                results[key] = CardResult(*key, "failed", reason="disabled")
                continue
            if remaining <= 0:
                results[key] = CardResult(*key, "deferred", reason="generate_limit")
                continue
            plan.calls = min(2 if plan.needs_meta else 1, remaining)
            remaining -= plan.calls
            admitted.append(plan)
        return admitted

    # ------------------------------------------------------------ generation
    def _run_leaders(self, leaders: list[tuple[_Plan, _Flight]],
                     cancel_event: CancelEvent) -> dict[tuple[str, str], CardResult]:
        out: dict[tuple[str, str], CardResult] = {}
        if not leaders:
            return out
        workers = max(1, min(
            len(leaders),
            int(self._models.parallelism(_WORKLOAD)),
            int(self._settings.paper_card_max_workers),
        ))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paper-card")
        try:
            futures = [
                pool.submit(contextvars.copy_context().run, self._lead, plan, flight, cancel_event)
                for plan, flight in leaders
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                out[(result.notebook_id, result.source_id)] = result
                raise_if_cancelled(cancel_event)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            # 被取消、从未开跑的任务也要放掉自己的单飞位。
            for plan, flight in leaders:
                self._flights.finish(plan.source_id, flight)
        return out

    def _lead(self, plan: _Plan, flight: _Flight, cancel_event: CancelEvent) -> CardResult:
        try:
            return self._run_plan(plan, cancel_event)
        finally:
            self._flights.finish(plan.source_id, flight)

    def _run_plan(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult:
        key = (plan.notebook_id, plan.source_id)
        raise_if_cancelled(cancel_event)
        try:
            calls = plan.calls
            if plan.needs_meta:
                outcome = self._ensure_meta(plan, cancel_event)
                calls -= 1
                if outcome is not None:
                    return outcome
                if calls <= 0:
                    return CardResult(*key, "deferred", reason="generate_limit")
            return self._generate(plan, cancel_event)
        except (ModelQueueFull, ModelQueueTimeout):
            # 交互队列满或等待超时:这一篇稍后再试,不写失败行、不进冷却。
            return CardResult(*key, "deferred", reason="model_queue")
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 - one source never fails the batch
            self._event_log.logger.exception("paper card generation failed for %s", plan.source_id)
            return CardResult(*key, "failed", reason=f"error:{type(exc).__name__}")

    def _ensure_meta(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult | None:
        """先抽元数据。返回 ``None`` 表示确认是论文、可以继续生成卡片;否则是这一篇的结果。
        元数据抽取的每种返回值都显式映射(规格 §4.5 第 1 条)。"""
        key = (plan.notebook_id, plan.source_id)
        try:
            with retrieval_fanout_slot():
                source = self._sources.get_source(plan.source_id)
        except KeyError:  # 来源在两步之间被删
            return CardResult(*key, "not_found", reason="missing")
        status = self._ingestion.extract_paper_metadata_cancellable(
            source, cancel_event=cancel_event, client=self._models.chat(_WORKLOAD),
        )
        if status == "skipped":
            # gate 已在 _classify 判过,这里的 skipped 是「别人刚抽完」:按落库结果判。
            with retrieval_fanout_slot():
                meta = self._sources.get_paper_meta(plan.source_id, plan.notebook_id)
            if meta is None:
                return CardResult(*key, "meta_pending", reason="metadata_skipped")
            status = "stored" if meta.get("is_paper") else "not_paper"
        if status == "stored":
            return None
        if status == "not_paper":
            return CardResult(*key, "not_paper")
        if status in ("disabled", "no_llm", "no_text"):
            return CardResult(*key, "meta_pending", reason=f"metadata_{status}")
        return CardResult(*key, "failed", reason="metadata_failed")

    def _generate(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult:
        key = (plan.notebook_id, plan.source_id)
        client = self._models.chat(_WORKLOAD)
        if not getattr(client, "configured", False):
            return CardResult(*key, "meta_pending", reason="no_llm")
        model = str(getattr(client, "model", "") or "")
        with retrieval_fanout_slot():
            sample = sample_paper_card_input(
                self._sources, self._database, plan.source_id,
                int(self._settings.paper_card_input_chars),
                generation_reader=self._sources.source_card_basis,
                cancel_event=cancel_event,
            )
        if sample.status == "empty":
            return CardResult(*key, "meta_pending", reason="no_text")
        if sample.status == "changed":
            return CardResult(*key, "deferred", reason="source_changed")
        raise_if_cancelled(cancel_event)
        try:
            with model_artifact_scope(notebook_id=plan.notebook_id, parent_id=plan.source_id):
                raw = client.chat_json(
                    [{"role": "user", "content": paper_card_prompt(sample.input_block)}],
                    PAPER_CARD_SCHEMA_HINT,
                    temperature=0.0,
                    cancel_event=cancel_event,
                    **cap_kwargs(client, "openai_compat_max_tokens"),
                )
        except (CoreCancellation, ModelQueueFull, ModelQueueTimeout):
            raise
        except Exception as exc:  # noqa: BLE001 - recorded as a failed attempt
            return self._write(plan, sample, model, failure=f"model_error:{type(exc).__name__}")
        parsed = _parse_object(raw)
        if parsed is None:
            return self._write(plan, sample, model, failure="malformed")
        card, dropped = verify_paper_card(parsed, sample.elements)
        if card_is_empty(card):
            return self._write(plan, sample, model, failure="empty_card", parsed=parsed, dropped=dropped)
        return self._write(plan, sample, model, card=card, parsed=parsed, dropped=dropped)

    def _write(self, plan: _Plan, sample: PaperCardSample, model: str, *, card: dict | None = None,
               failure: str = "", parsed: Any = None, dropped: Mapping[str, Any] | None = None) -> CardResult:
        """条件写一行;没落地(被同版本 ready 保护、或 basis 已过期)就重读,不假定写成。"""
        key = (plan.notebook_id, plan.source_id)
        status = "failed" if failure else "ready"
        raw_json: dict[str, Any] = {
            "llm": parsed if parsed is not None else {},
            "dropped": dict(dropped or {}),
            "input": {
                "elements": len(sample.elements),
                "chars": sample.input_chars,
                "total_elements": sample.total_elements,
            },
        }
        if failure:
            raw_json["reason"] = failure
        n_dropped = dropped_count(dropped or {})
        with retrieval_fanout_slot():
            try:
                written = self._sources.upsert_paper_card(
                    plan.source_id, plan.notebook_id, basis=sample.basis,
                    card_version=PAPER_CARD_VERSION, status=status,
                    card=card or {}, raw_json=raw_json, model=model,
                )
            except KeyError:  # 加锁时来源恰好被删(或已不属于这个库)
                return CardResult(*key, "not_found", reason="missing")
            if written:
                if failure:
                    return CardResult(*key, "failed", reason=failure, model=model, dropped=n_dropped)
                return CardResult(*key, "generated", card=card or {}, model=model, dropped=n_dropped)
            snapshot = self._reread(plan.notebook_id, plan.source_id)
        row = snapshot.cards.get(plan.source_id)
        if self._valid(row, snapshot.bases.get(plan.source_id, "")):
            return CardResult(*key, "ready", card=row["card"], model=row["model"])
        return CardResult(*key, "deferred", reason="source_changed", model=model)

    def _reread(self, notebook_id: str, source_id: str) -> _Snapshot:
        with self._database.connect() as db:
            cards = self._sources.paper_cards_for_sources(db, [source_id], notebook_id=notebook_id)
            bases = self._sources.source_card_bases(db, [source_id])
        return _Snapshot({}, cards, bases, False)

    # ------------------------------------------------------------ single flight
    def _await_waiters(self, waiters: list[tuple[_Plan, _Flight]],
                       cancel_event: CancelEvent) -> dict[tuple[str, str], CardResult]:
        """同一篇正被别的请求生成:在池外等它结束(轮询取消),然后按缓存重判。"""
        out: dict[tuple[str, str], CardResult] = {}
        if not waiters:
            return out
        for _plan, flight in waiters:
            while not flight.done.wait(_WAIT_POLL_SECONDS):
                raise_if_cancelled(cancel_event)
        raise_if_cancelled(cancel_event)
        by_notebook: dict[str, list[str]] = {}
        for plan, _flight in waiters:
            by_notebook.setdefault(plan.notebook_id, []).append(plan.source_id)
        for notebook_id, source_ids in by_notebook.items():
            snapshot = self._snapshot(notebook_id, source_ids)
            for source_id in source_ids:
                outcome = self._classify(notebook_id, source_id, snapshot)
                if isinstance(outcome, _Plan):
                    outcome = CardResult(notebook_id, source_id, "deferred", reason="in_flight")
                out[(notebook_id, source_id)] = outcome
        return out

    # ------------------------------------------------------------ events
    def _emit(self, result: CardResult, elapsed_ms: int, actor_id: str) -> None:
        """每篇一条 ``paper_card`` 事件:只记 id、状态、原因码(含异常类名)与计数,不记正文。"""
        event: dict[str, Any] = {
            "kind": "paper_card",
            # 集合外的条目是调用方(可能是模型)写的任意字符串:截断,不让它把正文带进日志。
            "source_id": result.source_id[:128],
            "notebook_id": result.notebook_id[:128],
            "status": result.status,
            "reason": result.reason,
            "dropped": result.dropped,
            "elapsed_ms": elapsed_ms,
        }
        if actor_id:
            event["actor_id"] = actor_id
        self._event_log.emit(event)


def _parse_object(raw: Any) -> dict | None:
    """与论文元数据同一口径:先直解析,失败再走 ``safe_json`` 的围栏/think 剥离;
    单对象数组拆包,其它非对象形状算畸形。"""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = safe_json(raw)
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    return parsed if isinstance(parsed, dict) and parsed else None
