"""逐篇临时字段抽取 ``PaperExtractionService``(规格 §6,``extract_from_sources``)。

调用方给几篇来源和至多 4 个字段(如「在 COCO 上的 mAP」),每篇:

1. **选片段**:全篇 chunk id 取自 ``RetrievalPort.section_outline``(上限
   ``REASONING_SECTION_OUTLINE_MAX_ROWS``,默认 4000 行;超出的长文档只在前这么多行里
   选,结果里 ``outline_truncated`` 如实标出)。``section_chunk_similarities`` 对多条
   查询取的是**最大值**,所以每个字段单独调用一次;片段名额按排名在字段间**轮流**分配,
   去重后最多 12 个,每个字段至少 ``12 // 字段数`` 个。第一个字段就拿不到相似度
   (返回 ``{}``:这一篇没有可用向量)时,其余字段不再算相似度,直接走这一篇的 FTS
   (``chunk_fts_search(..., allowed_source_ids=[该篇])``,只留大纲内的非导航片段)。
   再加开头 2 个元素作上下文。片段按 ``REASONING_SECTION_READ_MAX_CHUNKS`` 分批按 id
   读回,不会被单次读取上限按文档顺序截掉。
2. **一次** ``paper_metadata`` 模型调用,schema ``EXTRACTION_SCHEMA_HINT``。
3. **校验**(零 LLM):``anchor`` 必须是本次输入里的句柄或 id(同一个元素 id 既是开头元素
   又在某个片段里时,两段原文都可以作证);值先截到 300 字(不切断数字),里面的数字按
   「数字组」接地(``ground_prose_numbers``),不过就丢弃并记审计。引用挂在片段里
   **真正包含该值的那个元素**上,找不到才退回片段首元素。

调用方不在检索运行里(MCP)时,本方法自己开一个轻量运行(``retrieval_run``):字段的查询
向量、每篇的大纲与向量在这次调用内各只算一次,无向量事件每篇只发一次。

与论文卡片的区别:**不缓存、不落库**(临时字段与问题相关,每次重算),也**不检查
``is_paper``**(非论文同样可用)。并发、优先级、取消、瞬时错误降级与卡片服务同一口径:
有界线程池 + ``contextvars``,数据库/检索读取包在 ``retrieval_fanout_slot`` 里,
瞬时模型错误 → ``deferred``,对这篇确定性的失败与内容失败 → ``failed``(不写任何行)。

调用方负责权限:``allowed`` 是 ``(所属库 id, source_id)`` 集合(问答为运行天花板,MCP 为
凭证主人可读的可见 ∪ 自己的隐藏来源);集合外的条目一律 ``not_found``。
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.core.llm import cap_kwargs
from app.core.model_values import as_text
from app.domain.cancellation import CancelEvent, CoreCancellation, raise_if_cancelled
from app.repositories.ports import RepositoryDatabasePort, SourceStorePort
from app.services.model_work import (
    is_transient_model_error,
    model_artifact_scope,
    model_error_code,
    model_failure_reason,
)
from app.services.paper_card_service import parse_model_object
from app.services.paper_cards import (
    cap_number_safe,
    card_element_header,
    ground_prose_numbers,
    number_tokens,
)
from app.services.retrieval_run import (
    current_retrieval_run,
    holding_fanout_slot,
    retrieval_fanout_slot,
    retrieval_run,
)

#: 单次调用的来源与字段上限(规格 §6)。超出的请求整体拒绝,不截断。
EXTRACT_MAX_SOURCES = 8
EXTRACT_MAX_FIELDS = 4
EXTRACT_FIELD_MAX_CHARS = 80
#: 每个字段至少排这么多个候选片段(字段少时按 ``EXTRACT_MAX_CHUNKS // 字段数`` 放宽),
#: 每篇的片段总数(去重后)上限。
EXTRACT_CHUNKS_PER_FIELD = 4
EXTRACT_MAX_CHUNKS = 12
#: 开头作上下文的元素数(标题 / 摘要)。
EXTRACT_OPENING_ELEMENTS = 2
#: 送给模型的每段文字上限;数值接地只在这段截断后的文字上判定。
EXTRACT_PASSAGE_MAX_CHARS = 1500
EXTRACT_OPENING_MAX_CHARS = 1000
EXTRACT_VALUE_MAX_CHARS = 300
#: FTS 回落时多取一些,再只保留属于这一篇(有界)大纲、且不是导航页的片段。
_FTS_PROBE = 16

# 示例里不写 null、没有空列表(json-hint-strict-alignment):严格 schema 投影可用。
EXTRACTION_SCHEMA_HINT = '{"values":[{"field_index":0,"value":"","anchor":"c1"}]}'

EXTRACTION_STATUSES = ("extracted", "no_text", "failed", "deferred", "not_found")
_WORKLOAD = "paper_metadata"
_EVENT_KIND = "paper_extraction"


class ExtractionRequestError(ValueError):
    """请求本身不合法(字段为空/过多/过长,来源过多);调用方映射为拒绝。"""


@dataclass(frozen=True)
class FieldValue:
    """一个字段在一篇里的结果。抽到时 ``value`` 非空、``element_id`` 是它引用的元素
    (片段里真正包含该值的那个元素,找不到时是片段首元素),``chunk_id`` 是引用片段时的片段 id;没抽到时 ``missing``
    是原因码:``not_stated``(模型没给)或校验丢弃的原因(``invalid_anchor``、
    ``value_not_in_anchor``、``ambiguous_value``、``unmapped_anchor``)。"""

    field_index: int
    field: str
    value: str = ""
    element_id: str = ""
    chunk_id: str = ""
    missing: str = ""


@dataclass(frozen=True)
class ExtractionResult:
    """一篇的结果。``status``:``extracted``(模型调用并校验完成,个别字段可能缺)、
    ``no_text``(选不出任何原文)、``failed``、``deferred``、``not_found``;``reason`` 是
    机器可读原因码。``lane`` 是片段选择的方式(``vector`` / ``fts`` / ``mixed`` / ``none``)。"""

    notebook_id: str
    source_id: str
    status: str
    fields: tuple[FieldValue, ...] = ()
    reason: str = ""
    model: str = ""
    dropped: int = 0
    outline_truncated: bool = False
    lane: str = ""
    elapsed_ms: int = 0


@dataclass(frozen=True)
class Excerpt:
    """送进模型的一段原文。``element_id`` 是这段对应的元素(片段为它的首个元素,可能为空);
    片段的 ``elements`` 是它各元素的 ``(id, 全文)``(按片段顺序,只在片段有多个元素时读),
    用来把引用挂到真正包含该值的元素上。"""

    handle: str
    kind: str  # "element" | "chunk"
    element_id: str
    chunk_id: str
    label: str
    text: str
    elements: tuple[tuple[str, str], ...] = ()

    def ids(self) -> tuple[str, ...]:
        """这段原文可以被引用的 id:片段 id、它的各元素 id,或元素自身。"""
        own = (self.chunk_id,) if self.chunk_id else ()
        members = tuple(eid for eid, _text in self.elements) or ((self.element_id,) if self.element_id else ())
        return own + members


@dataclass
class _Selection:
    excerpts: list[Excerpt] = field(default_factory=list)
    truncated: bool = False
    lane: str = "none"


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def normalize_fields(fields: Iterable[Any]) -> tuple[str, ...]:
    """去空白、去重;空、超过 4 个或单个超过 80 字时抛 ``ExtractionRequestError``。"""
    cleaned: list[str] = []
    for raw in fields:
        text = " ".join(str(raw or "").split())
        if not text:
            continue
        if len(text) > EXTRACT_FIELD_MAX_CHARS:
            raise ExtractionRequestError(f"a field is longer than {EXTRACT_FIELD_MAX_CHARS} characters")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise ExtractionRequestError("extract_from_sources needs at least one field")
    if len(cleaned) > EXTRACT_MAX_FIELDS:
        raise ExtractionRequestError(f"at most {EXTRACT_MAX_FIELDS} fields")
    return tuple(cleaned)


def render_excerpts(excerpts: Iterable[Excerpt]) -> str:
    return "\n\n".join(
        card_element_header(e.handle, "passage" if e.kind == "chunk" else "element", e.label) + e.text
        for e in excerpts
    )


def extraction_prompt(fields: Sequence[str], excerpts: Sequence[Excerpt]) -> str:
    field_lines = "\n".join(f"[{index}] {text}" for index, text in enumerate(fields))
    return (
        "Extract the requested fields from ONE document, using ONLY the excerpts "
        "below.\n"
        f"Fields:\n{field_lines}\n\n"
        "Each excerpt starts with a handle in brackets: [e1], [e2] are the "
        "document's opening; [c1], [c2], ... are passages.\n"
        "Rules:\n"
        "- For every field the excerpts state, return one item: \"field_index\" "
        "(the number in brackets above), \"value\" (concise, at most "
        f"{EXTRACT_VALUE_MAX_CHARS} characters, in the document's language; "
        "numbers EXACTLY as printed, with their unit and setting) and \"anchor\" "
        "(the handle, e.g. \"c2\", of the ONE excerpt that states it).\n"
        "- Leave a field out when the excerpts do not state it. Do NOT answer "
        "from memory and do NOT guess.\n"
        "- Return JSON only.\n\n"
        f"Excerpts:\n{render_excerpts(excerpts)}"
    )


def _resolver(excerpts: Iterable[Excerpt]) -> dict[str, list[Excerpt]]:
    """句柄或 id → 可以作证的原文段。句柄唯一;一个元素 id 可能既是开头元素又在某个
    片段里,两段都列上(片段在前:它是模型在片段里看到的那段文字)。"""
    ordered = sorted(excerpts, key=lambda e: e.kind != "chunk")
    table: dict[str, list[Excerpt]] = {}
    for excerpt in ordered:
        table.setdefault(excerpt.handle.lower(), []).append(excerpt)
    for excerpt in ordered:
        for ident in excerpt.ids():
            bucket = table.setdefault(ident, [])
            if excerpt not in bucket:
                bucket.append(excerpt)
    return table


def _scalar_text(value: Any) -> str:
    """字符串取 strip;JSON 模式下模型可能把数值写成 number,转成字符串;布尔不算值。"""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return as_text(value)


def _field_index(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = as_text(raw).strip("[] ")
    return int(text) if text.isdigit() else None


def cap_value(value: str, limit: int = EXTRACT_VALUE_MAX_CHARS) -> str:
    """``paper_cards.cap_number_safe`` at the extraction value limit: never
    half a number."""
    return cap_number_safe(value, limit)


def _citing_element(excerpt: Excerpt, value: str) -> str:
    """片段里真正包含该值的那个元素:含数字的值按 ``ground_prose_numbers`` 判,其余按
    不区分大小写的原文包含判;都找不到时退回片段首元素。"""
    if excerpt.kind != "chunk" or len(excerpt.elements) < 2:
        return excerpt.element_id
    numeric = bool(number_tokens(value))
    folded = value.casefold()
    for element_id, text in excerpt.elements:
        if numeric:
            if ground_prose_numbers(value, text) == "":
                return element_id
        elif folded in text.casefold():
            return element_id
    return excerpt.element_id


def verify_extraction(
    parsed: Mapping[str, Any], fields: Sequence[str], excerpts: Sequence[Excerpt],
) -> tuple[tuple[FieldValue, ...], dict]:
    """零 LLM 校验,返回每个字段的 ``FieldValue``(按字段顺序)与审计 ``dropped``。

    - ``field_index`` 越界或不是整数的条目丢弃(``bad_field_index``);
    - 值先截到 300 字(不切断数字)再校验;
    - ``anchor`` 必须是本次输入里的句柄或 id(``invalid_anchor``),片段必须能映射到元素
      (``unmapped_anchor``);一个 id 对应多段原文时任一段作证即可;
    - 值里的数字按 ``ground_prose_numbers`` 在**送给模型的那段文字**里接地;
    - 引用挂在片段里真正包含该值的元素上(``_citing_element``);
    - 同一字段给了多条时取第一条通过校验的。
    """
    table = _resolver(excerpts)
    dropped: dict = {}
    chosen: dict[int, FieldValue] = {}
    failures: dict[int, str] = {}
    items = parsed.get("values") if isinstance(parsed, Mapping) else None
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, Mapping):
            continue
        index = _field_index(raw.get("field_index"))
        value = cap_value(" ".join(_scalar_text(raw.get("value")).split()))
        if index is None or not 0 <= index < len(fields):
            dropped.setdefault("bad_field_index", []).append({"value": value[:120]})
            continue
        if not value or index in chosen:
            continue
        anchor = as_text(raw.get("anchor"))
        candidates = table.get(anchor) or table.get(anchor.strip("[]【】 ").lower()) or []
        reason = "invalid_anchor" if not candidates else ""
        accepted: Excerpt | None = None
        for excerpt in candidates:
            verdict = "unmapped_anchor" if not excerpt.element_id else ground_prose_numbers(value, excerpt.text)
            if not verdict:
                accepted = excerpt
                break
            reason = reason or verdict
        if accepted is None:
            dropped.setdefault(str(index), []).append({"reason": reason, "value": value[:120]})
            failures.setdefault(index, reason)
            continue
        chosen[index] = FieldValue(
            index, fields[index], value=value,
            element_id=_citing_element(accepted, value),
            chunk_id=accepted.chunk_id if accepted.kind == "chunk" else "",
        )
    out = tuple(
        chosen.get(index) or FieldValue(index, text, missing=failures.get(index, "not_stated"))
        for index, text in enumerate(fields)
    )
    return out, dropped


def _round_robin(ranked: Sequence[Sequence[str]], limit: int) -> list[str]:
    """按排名在字段间轮流取:第 1 轮各字段的第 1 名,第 2 轮各自的第 2 名……,去重,
    到 ``limit`` 为止。每个字段因此至少有 ``limit // 字段数`` 个自己的前几名在里面
    (一个片段同时是两个字段的候选时,两个字段都算有它)。"""
    picked: list[str] = []
    depth = max((len(top) for top in ranked), default=0)
    for rank in range(depth):
        for top in ranked:
            if rank < len(top) and top[rank] not in picked and len(picked) < limit:
                picked.append(top[rank])
    return picked


def _dropped_count(dropped: Mapping[str, Any]) -> int:
    return sum(len(value) for value in dropped.values() if isinstance(value, list))


class PaperExtractionService:
    def __init__(
        self,
        *,
        settings: Any,
        database: RepositoryDatabasePort,
        sources: SourceStorePort,
        knowledge: Any,
        retrieval: Callable[[], Any],
        model_clients: Any,
        event_log: Any,
    ) -> None:
        self._settings = settings
        self._database = database
        self._sources = sources
        # RetrievalKnowledgeStorePort:无向量时按单篇 FTS 回落。
        self._knowledge = knowledge
        # RetrievalPort 的延迟取用:检索服务在运行时装配顺序里比本服务晚建。
        self._retrieval = retrieval
        self._models = model_clients
        self._event_log = event_log

    # ------------------------------------------------------------ entry
    def extract(
        self,
        items: Iterable[tuple[str, str]],
        fields: Iterable[Any],
        *,
        allowed: Iterable[tuple[str, str]],
        limit: int,
        cancel_event: CancelEvent = None,
        actor_id: str = "",
    ) -> list[ExtractionResult]:
        """按输入顺序(去重后)返回每篇的 ``ExtractionResult``。

        ``items`` 与 ``allowed`` 都是 ``(所属库 id, source_id)``;``limit`` 是本次最多发起的
        模型调用数(每篇一次),按输入顺序放行,其余 ``deferred/limit``。字段或来源数不合法时
        抛 ``ExtractionRequestError``。取消时抛 ``AskCancelled``,抛出前为已有结果的篇写事件。

        调用方**不得**在持有 ``retrieval_fanout_slot`` 时调用(槽位不可重入,工作线程自己会取)。
        """
        if holding_fanout_slot():
            raise RuntimeError("PaperExtractionService.extract must not run inside retrieval_fanout_slot")
        wanted = normalize_fields(fields)
        ordered = list(dict.fromkeys((str(nb), str(sid)) for nb, sid in items))
        if len(ordered) > EXTRACT_MAX_SOURCES:
            raise ExtractionRequestError(f"at most {EXTRACT_MAX_SOURCES} sources")
        raise_if_cancelled(cancel_event)
        if current_retrieval_run() is None:
            # MCP 等不在检索运行里的调用方:开一个轻量运行,让字段的查询向量、每篇的大纲与
            # 向量、无向量事件在这次调用内各只算 / 发一次(没有 fan-out 上限,槽位是空操作)。
            with retrieval_run(run_kind="paper_extraction", actor_id=actor_id, cancel_event=cancel_event):
                return self._extract(ordered, wanted, allowed, limit, cancel_event, actor_id)
        return self._extract(ordered, wanted, allowed, limit, cancel_event, actor_id)

    def _extract(self, ordered, wanted, allowed, limit, cancel_event, actor_id) -> list[ExtractionResult]:
        permitted = {(str(nb), str(sid)) for nb, sid in allowed}
        results: dict[tuple[str, str], ExtractionResult] = {}
        try:
            admitted = self._admit(ordered, permitted, int(limit), results, cancel_event)
            self._run(admitted, wanted, cancel_event, results)
        except CoreCancellation:
            self._emit_all([results[key] for key in ordered if key in results], len(wanted), actor_id)
            raise
        ordered_results = [results[key] for key in ordered]
        self._emit_all(ordered_results, len(wanted), actor_id)
        return ordered_results

    def _admit(self, ordered, permitted, limit, results, cancel_event) -> list[tuple[str, str]]:
        """入口校验:集合外 → ``not_found/out_of_scope``;不存在或不属于该库 →
        ``not_found/missing``;超过 ``limit`` 的 → ``deferred/limit``。"""
        by_notebook: dict[str, list[str]] = {}
        for key in ordered:
            if key in permitted:
                by_notebook.setdefault(key[0], []).append(key[1])
            else:
                results[key] = ExtractionResult(*key, "not_found", reason="out_of_scope")
        existing: set[tuple[str, str]] = set()
        for notebook_id, source_ids in by_notebook.items():
            raise_if_cancelled(cancel_event)
            with retrieval_fanout_slot():
                with self._database.connect() as db:
                    rows = self._sources.source_listing_rows(db, source_ids)
            existing.update((notebook_id, row["id"]) for row in rows if row["notebook_id"] == notebook_id)
        admitted: list[tuple[str, str]] = []
        budget = max(0, limit)
        for key in ordered:
            if key in results:
                continue
            if key not in existing:
                results[key] = ExtractionResult(*key, "not_found", reason="missing")
            elif len(admitted) >= budget:
                results[key] = ExtractionResult(*key, "deferred", reason="limit")
            else:
                admitted.append(key)
        return admitted

    def _run(self, admitted, fields, cancel_event, results) -> None:
        if not admitted:
            return
        workers = max(1, min(
            len(admitted),
            int(self._models.parallelism(_WORKLOAD)),
            int(self._settings.paper_card_max_workers),
        ))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="paper-extract")
        try:
            futures = [
                pool.submit(contextvars.copy_context().run, self._one, key, fields, cancel_event)
                for key in admitted
            ]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                results[(result.notebook_id, result.source_id)] = result
                raise_if_cancelled(cancel_event)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    # ------------------------------------------------------------ one source
    def _one(self, key: tuple[str, str], fields: tuple[str, ...], cancel_event: CancelEvent) -> ExtractionResult:
        started = time.monotonic()
        try:
            result = self._extract_one(key, fields, cancel_event)
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 - one source never fails the batch
            if is_transient_model_error(exc):
                result = ExtractionResult(*key, "deferred", reason="model_unavailable")
            else:
                # 只记类名与稳定错误码,不带异常链(同 paper_card_service)。
                self._event_log.logger.warning(
                    "paper extraction failed for %s: %s (%s)",
                    key[1], type(exc).__name__, model_error_code(exc) or "-",
                )
                result = ExtractionResult(*key, "failed", reason=f"error:{type(exc).__name__}")
        return dataclasses.replace(result, elapsed_ms=_ms(started))

    def _extract_one(self, key, fields, cancel_event) -> ExtractionResult:
        raise_if_cancelled(cancel_event)
        client = self._models.chat(_WORKLOAD)
        if not getattr(client, "configured", False):
            return ExtractionResult(*key, "failed", reason="no_llm")
        model = str(getattr(client, "model", "") or "")
        selection = self._select(key, fields, cancel_event)
        common = dict(outline_truncated=selection.truncated, lane=selection.lane)
        if not selection.excerpts:
            return ExtractionResult(*key, "no_text", reason="no_text", **common)
        raise_if_cancelled(cancel_event)
        try:
            with model_artifact_scope(notebook_id=key[0], parent_id=key[1]):
                raw = client.chat_json(
                    [{"role": "user", "content": extraction_prompt(fields, selection.excerpts)}],
                    EXTRACTION_SCHEMA_HINT,
                    temperature=0.0,
                    cancel_event=cancel_event,
                    **cap_kwargs(client, "openai_compat_max_tokens"),
                )
        except CoreCancellation:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            if is_transient_model_error(exc):
                raise
            reason, _deterministic = model_failure_reason(exc)
            # 不缓存,所以确定性与全局故障一样只记 failed,不写任何行。
            return ExtractionResult(*key, "failed", reason=reason, model=model, **common)
        parsed = parse_model_object(raw)
        if parsed is None:
            return ExtractionResult(*key, "failed", reason="malformed", model=model, **common)
        values, dropped = verify_extraction(parsed, fields, selection.excerpts)
        return ExtractionResult(
            *key, "extracted", fields=values, model=model, dropped=_dropped_count(dropped), **common,
        )

    def _select(self, key, fields, cancel_event) -> _Selection:
        """开头 2 个元素 + 按字段轮流分配名额的片段(去重 ≤12,文档顺序)。"""
        notebook_id, source_id = key
        retrieval = self._retrieval()
        selection = _Selection()
        with retrieval_fanout_slot():
            page = self._sources.source_elements_page(source_id, offset=0, limit=EXTRACT_OPENING_ELEMENTS)
            outline, selection.truncated = retrieval.section_outline(notebook_id, source_id)
        for element in page.items:
            # 模型看到的、接地判据用的都是截断后的文本:绝不切进数字(同卡片输入)。
            text = cap_number_safe((element.text or "").strip(), EXTRACT_OPENING_MAX_CHARS)
            if element.source_id == source_id and text:
                selection.excerpts.append(Excerpt(
                    f"e{len(selection.excerpts) + 1}", "element", element.id, "",
                    element.location_label or "", text,
                ))
        eligible = [row[0] for row in outline if not row[2]]
        if not eligible:
            return selection
        ranked, lanes = self._rank_per_field(key, fields, eligible, cancel_event)
        selection.lane = lanes.pop() if len(lanes) == 1 else "mixed"
        picked = _round_robin(ranked, EXTRACT_MAX_CHUNKS)
        if picked:
            position = {chunk_id: index for index, chunk_id in enumerate(eligible)}
            selection.excerpts.extend(self._read_passages(key, sorted(picked, key=position.__getitem__)))
        return selection

    def _rank_per_field(self, key, fields, eligible, cancel_event) -> tuple[list[list[str]], set[str]]:
        """每个字段自己的候选排名(各 ``max(4, 12 // 字段数)`` 个)。第一个字段的相似度为空
        (这一篇没有可用向量)就不再给其余字段算相似度,一律走单篇 FTS。"""
        notebook_id, source_id = key
        retrieval = self._retrieval()
        member = set(eligible)
        per_field = max(EXTRACT_CHUNKS_PER_FIELD, EXTRACT_MAX_CHUNKS // len(fields))
        ranked: list[list[str]] = []
        lanes: set[str] = set()
        vectors = True
        for index, text in enumerate(fields):
            raise_if_cancelled(cancel_event)
            sims: dict[str, float] = {}
            with retrieval_fanout_slot():
                if vectors:
                    sims = retrieval.section_chunk_similarities(notebook_id, source_id, eligible, [text])
                    vectors = bool(sims) or index > 0
                if sims:
                    lanes.add("vector")
                    top = [cid for cid, _s in sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))]
                else:
                    lanes.add("fts")
                    with self._database.connect() as db:
                        hits = self._knowledge.chunk_fts_search(
                            db, notebook_id, text, _FTS_PROBE, allowed_source_ids=[source_id],
                        )
                    # 只留这一篇(有界)大纲里的非导航片段。
                    top = [str(hit["chunk_id"]) for hit in hits if str(hit["chunk_id"]) in member]
            ranked.append(top[:per_field])
        return ranked, lanes

    def _read_passages(self, key, picked: list[str]) -> list[Excerpt]:
        """按 id 分批读回(每批不超过 ``REASONING_SECTION_READ_MAX_CHUNKS``,单批的上限因此
        截不掉任何一个已选片段),再批量读多元素片段的各元素全文。"""
        notebook_id, source_id = key
        retrieval = self._retrieval()
        batch = max(1, int(self._settings.reasoning_section_read_max_chunks))
        chunks = []
        for start in range(0, len(picked), batch):
            ids = picked[start:start + batch]
            with retrieval_fanout_slot():
                chunks.extend(retrieval.section_chunks(notebook_id, source_id, ids, len(ids), ""))
        order = {chunk_id: index for index, chunk_id in enumerate(picked)}
        chunks.sort(key=lambda chunk: order.get(chunk.chunk_id, len(order)))
        multi = [str(e) for chunk in chunks if len(chunk.element_ids or ()) > 1 for e in chunk.element_ids]
        texts: dict[str, str] = {}
        if multi:
            with retrieval_fanout_slot():
                rows = self._sources.evidence_elements(multi)
            texts = {
                element_id: str(row.get("text") or "")
                for element_id, row in rows.items() if str(row.get("source_id") or "") == source_id
            }
        excerpts = []
        for index, chunk in enumerate(chunks, 1):
            text = cap_number_safe((chunk.text or "").strip(), EXTRACT_PASSAGE_MAX_CHARS)
            if not text:
                continue
            element_ids = [str(e) for e in (chunk.element_ids or ()) if str(e or "")]
            members = tuple((eid, texts[eid]) for eid in element_ids if eid in texts) if len(element_ids) > 1 else ()
            excerpts.append(Excerpt(
                f"c{index}", "chunk", element_ids[0] if element_ids else "", chunk.chunk_id,
                chunk.section_path or "", text, members,
            ))
        return excerpts

    # ------------------------------------------------------------ events
    def _emit_all(self, results: Iterable[ExtractionResult], n_fields: int, actor_id: str) -> None:
        for result in results:
            event: dict[str, Any] = {
                "kind": _EVENT_KIND,
                # 集合外的条目可能是模型写的任意字符串:截断,不让它把正文带进日志。
                "source_id": result.source_id[:128],
                "notebook_id": result.notebook_id[:128],
                "status": result.status,
                "reason": result.reason,
                "fields": n_fields,
                "extracted": sum(1 for value in result.fields if value.value),
                "dropped": result.dropped,
                "lane": result.lane,
                "outline_truncated": result.outline_truncated,
                "elapsed_ms": result.elapsed_ms,
            }
            if actor_id:
                event["actor_id"] = actor_id
            self._event_log.emit(event)


__all__ = [
    "EXTRACTION_SCHEMA_HINT",
    "EXTRACTION_STATUSES",
    "EXTRACT_MAX_FIELDS",
    "EXTRACT_MAX_SOURCES",
    "Excerpt",
    "ExtractionRequestError",
    "ExtractionResult",
    "FieldValue",
    "PaperExtractionService",
    "extraction_prompt",
    "normalize_fields",
    "verify_extraction",
]
