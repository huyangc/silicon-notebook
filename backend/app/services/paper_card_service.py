"""论文卡片的懒生成服务 ``PaperCardService``(规格 §4.5)。

卡片是 ``is_paper=1`` 的来源由原文确定性派生的缓存:读的时候判有效
(``status='ready'`` ∧ ``basis`` = 当前卡片见证 ∧ ``card_version`` = 代码常量 ∧ 来源仍是论文),
缺失或过期的在本次调用的生成上限内懒生成,其余返回 ``deferred``。

调用方负责权限:``allowed`` 是调用方算好的 ``(所属库 id, source_id)`` 集合(问答是本次运行
的来源天花板,MCP 是凭证主人可读的可见 ∪ 自己的隐藏来源);集合外的条目一律 ``not_found``,
服务只在检索层这一个入口收窄(retrieval-owns-permissions)。

并发:元数据与卡片的模型调用在有界线程池里跑(``contextvars`` 传播调用方的
``model_work_scope`` 与检索运行状态),数据库读写包在 ``retrieval_fanout_slot`` 里;
同一来源的并发请求进程内单飞,后来者在池外等待、轮询取消,不占生成上限。

失败分三类(按调度层包装后的 ``.code`` 判定,原始调度异常同样接受):
- **暂时不可用**(``is_transient_model_error``:队列满/等待超时/熔断/429/5xx/超时)→
  ``deferred``,不写失败行、不累计 ``attempts``;
- **内容失败与对这篇文档确定性的失败**(模型回了但不是合法对象、``malformed_response``、
  校验后全空、上下文超长、服务商拒绝该内容等非鉴权 4xx——``INPUT_DETERMINISTIC_MODEL_ERROR_CODES``
  ——以及元数据抽取的同类失败)→ 写 ``failed`` 行,按同一 basis + 版本冷却 1h(连续 3 次后 24h);
- 鉴权/配置这类全局故障与未知异常 → ``failed``,不写行、不冷却(修好配置后应立即可用)。
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import dataclasses
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
from app.services.document_source_overview import sample_paper_card_input
from app.services.kg.json_utils import safe_json
from app.services.model_work import (
    is_transient_model_error,
    model_artifact_scope,
    model_error_code,
    model_failure_reason,
)
from app.services.paper_cards import (
    PAPER_CARD_SCHEMA_HINT,
    PAPER_CARD_VERSION,
    card_is_empty,
    dropped_count,
    paper_card_prompt,
    verify_paper_card,
)
from app.services.paper_meta import paper_meta_doc_type_eligible
from app.services.retrieval_run import (
    holding_fanout_slot,
    note_paper_meta_written,
    retrieval_fanout_slot,
)

CARD_STATUSES = (
    "ready", "generated", "not_paper", "meta_pending", "failed", "deferred", "not_found",
)
# 连续失败 ≥3 次后的固定冷却(规格 §4.4 / §9 第 3 条)。
LONG_COOLDOWN_SECONDS = 24 * 3600
LONG_COOLDOWN_ATTEMPTS = 3
# 单飞的等待者:领头者以这些终态结束时,等待者直接沿用它的结论。
_TERMINAL_WITHOUT_CARD = ("meta_pending", "not_paper", "failed")
_SYNTHETIC_SOURCE_TYPES = ("memory", "knowhow", PROMOTION_SOURCE_TYPE)
_WAIT_POLL_SECONDS = 0.1
_WORKLOAD = "paper_metadata"


@dataclass(frozen=True)
class CardResult:
    """一篇的结果。``card`` 只在 ``ready`` / ``generated`` 时非空;``reason`` 是机器可读的
    原因码(如 ``out_of_scope``、``cooldown``、``generate_limit``、``model_unavailable``)。
    ``elapsed_ms`` 是这一篇自己花的时间(读缓存、生成或等待)。``calls`` 是这一篇从本次
    ``generate_limit`` 里预留的模型调用数(领头者的预留,不退回;命中缓存、单飞等待者与
    被推迟的篇为 0)——调用方据它累计跨调用的生成预算。"""

    notebook_id: str
    source_id: str
    status: str
    card: dict = field(default_factory=dict)
    reason: str = ""
    model: str = ""
    dropped: int = 0
    elapsed_ms: int = 0
    calls: int = 0


@dataclass
class _Plan:
    notebook_id: str
    source_id: str
    needs_meta: bool
    calls: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.notebook_id, self.source_id)


@dataclass(frozen=True)
class _Snapshot:
    listing: dict[str, Mapping[str, Any]]
    meta: dict[str, dict]
    cards: dict[str, dict]
    bases: dict[str, str]
    mirrored: bool


class _Flight:
    __slots__ = ("done", "result", "waiters")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: CardResult | None = None
        self.waiters = 0


class _FlightTable:
    """进程内单飞:同一 source_id 同时只有一个生成者,其余等它结束。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: dict[str, _Flight] = {}

    def join(self, key: str) -> tuple[_Flight, bool]:
        with self._lock:
            current = self._flights.get(key)
            if current is not None:
                current.waiters += 1
                return current, False
            flight = _Flight()
            self._flights[key] = flight
            return flight, True

    def finish(self, key: str, flight: _Flight, result: CardResult | None = None) -> None:
        with self._lock:
            if result is not None and flight.result is None:
                flight.result = result
            if self._flights.get(key) is flight:
                del self._flights[key]
        flight.done.set()

    def get(self, key: str) -> _Flight | None:
        with self._lock:
            return self._flights.get(key)


class _Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = max(0, int(limit))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


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
        元数据 + 卡片模型调用的合计上限(需要先抽元数据的一篇按 2 次预留;单飞等待者
        不占)。取消时抛 ``AskCancelled``,抛出前为已经有结果的篇写事件。

        调用方**不得**在持有 ``retrieval_fanout_slot`` 时调用:槽位信号量不可重入,本方法
        自己与它的工作线程都会取槽,fan-out 上限为 1 时会死锁。违反时直接抛 ``RuntimeError``。
        """
        if holding_fanout_slot():
            raise RuntimeError("PaperCardService.get_cards must not run inside retrieval_fanout_slot")
        raise_if_cancelled(cancel_event)
        ordered = list(dict.fromkeys((str(nb), str(sid)) for nb, sid in items))
        permitted = {(str(nb), str(sid)) for nb, sid in allowed}
        results: dict[tuple[str, str], CardResult] = {}
        try:
            plans = self._classify_all(ordered, permitted, results, cancel_event)
            budget = _Budget(generate_limit)
            pending = [plans[key] for key in ordered if key in plans]
            deadline = time.monotonic() + self._wait_bound_seconds()
            for retry in (True, False):
                if not pending:
                    break
                leaders, waiters = self._partition(pending)
                try:
                    admitted = self._admit(leaders, budget, results)
                    self._run_leaders(admitted, cancel_event, results)
                finally:
                    # 无论这一轮怎么结束(包括 parallelism()/建池本身抛错),本轮占到的
                    # 单飞位都要交还,否则后来的请求会一直等一个不存在的领头者。
                    for plan, flight in leaders:
                        self._flights.finish(plan.source_id, flight)
                pending = self._await_waiters(waiters, cancel_event, results, retry=retry, deadline=deadline)
        except CoreCancellation:
            self._emit_all([results[key] for key in ordered if key in results], actor_id)
            raise
        ordered_results = [results[key] for key in ordered]
        self._emit_all(ordered_results, actor_id)
        return ordered_results

    def _classify_all(self, ordered, permitted, results, cancel_event) -> dict[tuple[str, str], _Plan]:
        by_notebook: dict[str, list[str]] = {}
        for key in ordered:
            if key in permitted:
                by_notebook.setdefault(key[0], []).append(key[1])
            else:
                results[key] = CardResult(*key, "not_found", reason="out_of_scope")
        plans: dict[tuple[str, str], _Plan] = {}
        for notebook_id, source_ids in by_notebook.items():
            raise_if_cancelled(cancel_event)
            started = time.monotonic()
            snapshot = self._snapshot(notebook_id, source_ids)
            for source_id in source_ids:
                outcome = self._classify(notebook_id, source_id, snapshot)
                if isinstance(outcome, _Plan):
                    plans[outcome.key] = outcome
                else:
                    results[(notebook_id, source_id)] = dataclasses.replace(outcome, elapsed_ms=_ms(started))
        return plans

    # ------------------------------------------------------------ read side
    def _snapshot(self, notebook_id: str, source_ids: Sequence[str]) -> _Snapshot:
        """一个所属库的批量读:来源行、按库收窄的论文元数据、卡片行、卡片见证。"""
        with retrieval_fanout_slot():
            with self._database.connect() as db:
                listing = {
                    row["id"]: row
                    for row in self._sources.source_listing_rows(db, source_ids)
                    if row["notebook_id"] == notebook_id
                }
                owned = [sid for sid in source_ids if sid in listing]
                if owned:
                    # is_paper 只认子表 notebook_id 与来源一致的行(畸形遗留行不算)。
                    meta = self._sources.paper_meta_for_sources(db, owned, notebook_id=notebook_id)
                    cards = self._sources.paper_cards_for_sources(db, owned, notebook_id=notebook_id)
                    bases = self._sources.source_card_bases(db, owned)
                else:
                    meta, cards, bases = {}, {}, {}
            mirrored = bool(owned) and bool(self._sync_origin(notebook_id))
        return _Snapshot(listing, meta, cards, bases, mirrored)

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
        key = (notebook_id, source_id)
        listing = snapshot.listing.get(source_id)
        if listing is None:
            return CardResult(*key, "not_found", reason="missing")
        meta = snapshot.meta.get(source_id)
        if meta is not None and not meta.get("is_paper"):
            return CardResult(*key, "not_paper")
        row = snapshot.cards.get(source_id)
        basis = snapshot.bases.get(source_id, "")
        if meta is not None and self._valid(row, basis):
            return CardResult(*key, "ready", card=row["card"], model=row["model"])
        if meta is None:
            # 还没有元数据行(懒生成、不回填):不合格的类型直接不是论文。
            doc_type = self._ingestion.normalize_doc_type(str(listing["doc_type"] or ""))
            if listing["source_type"] in _SYNTHETIC_SOURCE_TYPES or not paper_meta_doc_type_eligible(doc_type):
                return CardResult(*key, "not_paper")
        if self._cooling(row, basis):
            return CardResult(*key, "failed", reason="cooldown", model=row["model"])
        if snapshot.mirrored:
            return CardResult(*key, "meta_pending", reason="mirrored")
        if meta is not None and not basis:
            return CardResult(*key, "meta_pending", reason="no_text")
        return _Plan(notebook_id, source_id, needs_meta=meta is None)

    # ------------------------------------------------------------ admission
    def _partition(self, plans: list[_Plan]) -> tuple[list[tuple[_Plan, _Flight]], list[tuple[_Plan, _Flight]]]:
        """先占单飞位再算预算:只有领头者消耗生成上限,等待者不占。"""
        leaders, waiters = [], []
        for plan in plans:
            flight, leader = self._flights.join(plan.source_id)
            (leaders if leader else waiters).append((plan, flight))
        return leaders, waiters

    def _admit(self, leaders: list[tuple[_Plan, _Flight]], budget: _Budget,
               results: dict[tuple[str, str], CardResult]) -> list[tuple[_Plan, _Flight]]:
        """按输入顺序给本轮的领头者预留模型调用:卡片 1 次,需先抽元数据的 2 次(预算只剩 1
        时只做元数据,是论文的话卡片留到下次)。预留不退回。哪几篇被推迟取决于输入顺序,
        也取决于并发时序:同一篇正被别的请求生成时它是等待者、不占预算,领头者没留下卡片
        时它在第二轮才按剩余预算重新排队。没被放行的领头者立刻交还单飞位(带上结论)。"""
        enabled = bool(getattr(self._settings, "paper_card_enabled", True))
        admitted = []
        for plan, flight in leaders:
            if not enabled:
                outcome = CardResult(*plan.key, "failed", reason="disabled")
            elif budget.remaining <= 0:
                outcome = CardResult(*plan.key, "deferred", reason="generate_limit")
            else:
                plan.calls = min(2 if plan.needs_meta else 1, budget.remaining)
                budget.remaining -= plan.calls
                admitted.append((plan, flight))
                continue
            results[plan.key] = outcome
            self._flights.finish(plan.source_id, flight, outcome)
        return admitted

    # ------------------------------------------------------------ generation
    def _run_leaders(self, leaders: list[tuple[_Plan, _Flight]], cancel_event: CancelEvent,
                     results: dict[tuple[str, str], CardResult]) -> None:
        if not leaders:
            return
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
                results[(result.notebook_id, result.source_id)] = result
                raise_if_cancelled(cancel_event)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            # 被取消、从未开跑的任务也要放掉自己的单飞位(无结论 = 等待者自己再判)。
            for plan, flight in leaders:
                self._flights.finish(plan.source_id, flight)

    def _lead(self, plan: _Plan, flight: _Flight, cancel_event: CancelEvent) -> CardResult:
        started = time.monotonic()
        result: CardResult | None = None
        try:
            result = dataclasses.replace(
                self._run_plan(plan, cancel_event), elapsed_ms=_ms(started), calls=plan.calls,
            )
            return result
        finally:
            self._flights.finish(plan.source_id, flight, result)

    def _run_plan(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult:
        raise_if_cancelled(cancel_event)
        try:
            calls = plan.calls
            if plan.needs_meta:
                outcome = self._ensure_meta(plan, cancel_event)
                calls -= 1
                if outcome is not None:
                    return outcome
                if calls <= 0:
                    return CardResult(*plan.key, "deferred", reason="generate_limit")
            return self._generate(plan, cancel_event)
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 - one source never fails the batch
            if is_transient_model_error(exc):
                # 暂时不可用:这一篇稍后再试,不写失败行、不进冷却。
                return CardResult(*plan.key, "deferred", reason="model_unavailable")
            # 只记类名与稳定错误码:异常链(``raise ... from``)可能带服务商原文或含凭证的 URL。
            self._event_log.logger.warning(
                "paper card generation failed for %s: %s (%s)",
                plan.source_id, type(exc).__name__, model_error_code(exc) or "-",
            )
            return CardResult(*plan.key, "failed", reason=f"error:{type(exc).__name__}")

    def _ensure_meta(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult | None:
        """先抽元数据。返回 ``None`` 表示确认是论文、可以继续生成卡片;否则是这一篇的结果。
        元数据抽取的每种返回值都显式映射(规格 §4.5 第 1 条);暂时性失败由变体上抛。"""
        key = plan.key
        try:
            with retrieval_fanout_slot():
                source = self._sources.get_source(plan.source_id)
        except KeyError:  # 来源在两步之间被删
            return CardResult(*key, "not_found", reason="missing")
        status = self._ingestion.extract_paper_metadata_cancellable(
            source, cancel_event=cancel_event, client=self._models.chat(_WORKLOAD),
        )
        if status in ("stored", "not_paper"):
            note_paper_meta_written()  # a run's find_sources memo re-ranks
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
        if status == "failed":
            # 内容失败:记一行 failed 卡片(当前 basis),同一套冷却规则管住元数据重试。
            with retrieval_fanout_slot():
                basis = self._sources.source_card_basis(plan.source_id)
            return self._write(plan, basis, "", failure="metadata_failed")
        return CardResult(*key, "failed", reason="metadata_error")

    def _generate(self, plan: _Plan, cancel_event: CancelEvent) -> CardResult:
        key = plan.key
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
        audit = {"elements": len(sample.elements), "chars": sample.input_chars,
                 "total_elements": sample.total_elements}
        try:
            with model_artifact_scope(notebook_id=plan.notebook_id, parent_id=plan.source_id):
                raw = client.chat_json(
                    [{"role": "user", "content": paper_card_prompt(sample.input_block)}],
                    PAPER_CARD_SCHEMA_HINT,
                    temperature=0.0,
                    cancel_event=cancel_event,
                    **cap_kwargs(client, "openai_compat_max_tokens"),
                )
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            if is_transient_model_error(exc):
                raise
            failure, deterministic = model_failure_reason(exc)
            if deterministic:
                # 对这篇输入确定性的失败:写行冷却,别每次提问都再烧一次。
                return self._write(plan, sample.basis, model, failure=failure, audit=audit)
            return CardResult(*key, "failed", reason=failure, model=model)
        parsed = parse_model_object(raw)
        if parsed is None:
            return self._write(plan, sample.basis, model, failure="malformed", audit=audit)
        card, dropped = verify_paper_card(parsed, sample.elements)
        if card_is_empty(card):
            return self._write(plan, sample.basis, model, failure="empty_card",
                               parsed=parsed, dropped=dropped, audit=audit)
        return self._write(plan, sample.basis, model, card=card, parsed=parsed, dropped=dropped, audit=audit)

    def _write(self, plan: _Plan, basis: str, model: str, *, card: dict | None = None,
               failure: str = "", parsed: Any = None, dropped: Mapping[str, Any] | None = None,
               audit: Mapping[str, Any] | None = None) -> CardResult:
        """条件写一行;没落地(被同版本 ready 保护、或 basis 已过期)就重读,不假定写成。"""
        key = plan.key
        status = "failed" if failure else "ready"
        raw_json: dict[str, Any] = {
            "llm": parsed if parsed is not None else {},
            "dropped": dict(dropped or {}),
            "input": dict(audit or {}),
        }
        if failure:
            raw_json["reason"] = failure
        n_dropped = dropped_count(dropped or {})
        with retrieval_fanout_slot():
            try:
                written = self._sources.upsert_paper_card(
                    plan.source_id, plan.notebook_id, basis=basis,
                    card_version=PAPER_CARD_VERSION, status=status,
                    card=card or {}, raw_json=raw_json, model=model,
                )
            except KeyError:  # 加锁时来源恰好被删(或已不属于这个库)
                return CardResult(*key, "not_found", reason="missing")
            if written:
                if failure:
                    return CardResult(*key, "failed", reason=failure, model=model, dropped=n_dropped)
                return CardResult(*key, "generated", card=card or {}, model=model, dropped=n_dropped)
            cards, bases = self._reread(plan.notebook_id, plan.source_id)
        row = cards.get(plan.source_id)
        if self._valid(row, bases.get(plan.source_id, "")):
            return CardResult(*key, "ready", card=row["card"], model=row["model"])
        return CardResult(*key, "deferred", reason="source_changed", model=model)

    def _reread(self, notebook_id: str, source_id: str) -> tuple[dict, dict]:
        with self._database.connect() as db:
            cards = self._sources.paper_cards_for_sources(db, [source_id], notebook_id=notebook_id)
            bases = self._sources.source_card_bases(db, [source_id])
        return cards, bases

    # ------------------------------------------------------------ single flight
    def _wait_bound_seconds(self) -> float:
        """等待别的请求生成同一篇的总上限:领头者最多两次模型调用(元数据 + 卡片),
        每次最多「交互队列等待 30 秒 + 单次超时 × (重试 + 1)」。"""
        per_call = float(getattr(self._settings, "openai_compat_timeout_seconds", 60)) * (
            1 + int(getattr(self._settings, "openai_compat_max_retries", 0) or 0)
        ) + 30.0
        return 2 * per_call

    def _await_waiters(self, waiters: list[tuple[_Plan, _Flight]], cancel_event: CancelEvent,
                       results: dict[tuple[str, str], CardResult], *, retry: bool,
                       deadline: float) -> list[_Plan]:
        """同一篇正被别的请求生成:在池外等它结束(轮询取消,整次调用最多等到 ``deadline``,
        超过的记 ``deferred/in_flight``)。领头者以无卡终态结束时沿用它的结论;生成了卡片的
        按缓存重判;领头者被取消或推迟、没留下有效卡片时,返回需要自己再生成的计划
        (``retry``),否则记 ``deferred/in_flight``。"""
        again: list[_Plan] = []
        if not waiters:
            return again
        started = {plan.key: time.monotonic() for plan, _flight in waiters}
        timed_out: set[tuple[str, str]] = set()
        for plan, flight in waiters:
            while not flight.done.wait(_WAIT_POLL_SECONDS):
                raise_if_cancelled(cancel_event)
                if time.monotonic() >= deadline:
                    timed_out.add(plan.key)
                    break
        raise_if_cancelled(cancel_event)
        recheck: dict[str, list[_Plan]] = {}
        for plan, flight in waiters:
            if plan.key in timed_out:
                results[plan.key] = CardResult(*plan.key, "deferred", reason="in_flight",
                                               elapsed_ms=_ms(started[plan.key]))
                continue
            outcome = flight.result
            if outcome is not None and outcome.status in _TERMINAL_WITHOUT_CARD:
                # The leader's reservation is the leader's: this waiter made no
                # call, so its run budget is not charged (``calls=0``).
                results[plan.key] = dataclasses.replace(
                    outcome, notebook_id=plan.notebook_id, source_id=plan.source_id,
                    elapsed_ms=_ms(started[plan.key]), calls=0,
                )
            else:
                recheck.setdefault(plan.notebook_id, []).append(plan)
        for notebook_id, plans in recheck.items():
            snapshot = self._snapshot(notebook_id, [plan.source_id for plan in plans])
            for plan in plans:
                outcome = self._classify(notebook_id, plan.source_id, snapshot)
                if isinstance(outcome, _Plan):
                    if retry:
                        again.append(outcome)
                        continue
                    outcome = CardResult(*plan.key, "deferred", reason="in_flight")
                results[plan.key] = dataclasses.replace(outcome, elapsed_ms=_ms(started[plan.key]))
        return again

    # ------------------------------------------------------------ events
    def _emit_all(self, results: Iterable[CardResult], actor_id: str) -> None:
        for result in results:
            self._emit(result, actor_id)

    def _emit(self, result: CardResult, actor_id: str) -> None:
        """每篇一条 ``paper_card`` 事件:只记 id、状态、原因码(含异常类名)与计数,不记正文。"""
        event: dict[str, Any] = {
            "kind": "paper_card",
            # 集合外的条目是调用方(可能是模型)写的任意字符串:截断,不让它把正文带进日志。
            "source_id": result.source_id[:128],
            "notebook_id": result.notebook_id[:128],
            "status": result.status,
            "reason": result.reason,
            "dropped": result.dropped,
            "elapsed_ms": result.elapsed_ms,
        }
        if actor_id:
            event["actor_id"] = actor_id
        self._event_log.emit(event)


def parse_model_object(raw: Any) -> dict | None:
    """与论文元数据同一口径:先直解析,失败再走 ``safe_json`` 的围栏/think 剥离;
    单对象数组拆包,其它非对象形状算畸形。"""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        parsed = safe_json(raw)
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        parsed = parsed[0]
    return parsed if isinstance(parsed, dict) and parsed else None
