"""逐篇临时字段抽取 ``PaperExtractionService``(规格 §6,``extract_from_sources``)。

调用方给几篇来源和至多 4 个字段(如「在 COCO 上的 mAP」),每篇:

1. **选片段**:全篇 chunk id 取自 ``RetrievalPort.section_outline``(上限
   ``REASONING_SECTION_OUTLINE_MAX_ROWS``,默认 4000 行;超出的长文档只在前这么多行里
   选,结果里 ``outline_truncated`` 如实标出)。``section_chunk_similarities`` 对多条
   查询取的是**最大值**,所以每个字段单独调用一次、各取前 4 个片段,去重后最多 12 个;
   该篇没有可用向量(返回 ``{}``)的字段回落到这一篇的 FTS
   (``chunk_fts_search(..., allowed_source_ids=[该篇])``)。再加开头 2 个元素作上下文。
2. **一次** ``paper_metadata`` 模型调用,schema ``EXTRACTION_SCHEMA_HINT``。
3. **校验**(零 LLM):``anchor`` 必须是本次输入里的句柄或 id,片段映射到它的首个元素;
   值里的数字按卡片同一套归一化规则接地(``ground_prose_numbers``),不过就丢弃并记审计。

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
    INPUT_DETERMINISTIC_MODEL_ERROR_CODES,
    is_transient_model_error,
    model_artifact_scope,
    model_error_code,
)
from app.services.paper_card_service import parse_model_object
from app.services.paper_cards import card_element_header, ground_prose_numbers
from app.services.retrieval_run import holding_fanout_slot, retrieval_fanout_slot

#: 单次调用的来源与字段上限(规格 §6)。超出的请求整体拒绝,不截断。
EXTRACT_MAX_SOURCES = 8
EXTRACT_MAX_FIELDS = 4
EXTRACT_FIELD_MAX_CHARS = 80
#: 每个字段取的片段数与每篇的片段总数(去重后)。
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
    (片段先映射到首个元素),``chunk_id`` 是引用片段时的片段 id;没抽到时 ``missing``
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
    """送进模型的一段原文。``element_id`` 是这段对应的元素(片段为它的首个元素,可能为空)。"""

    handle: str
    kind: str  # "element" | "chunk"
    element_id: str
    chunk_id: str
    label: str
    text: str


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


def _resolver(excerpts: Iterable[Excerpt]) -> dict[str, Excerpt]:
    table: dict[str, Excerpt] = {}
    for excerpt in excerpts:
        table.setdefault(excerpt.handle.lower(), excerpt)
        for ident in (excerpt.chunk_id, excerpt.element_id):
            if ident:
                table.setdefault(ident, excerpt)
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


def verify_extraction(
    parsed: Mapping[str, Any], fields: Sequence[str], excerpts: Sequence[Excerpt],
) -> tuple[tuple[FieldValue, ...], dict]:
    """零 LLM 校验,返回每个字段的 ``FieldValue``(按字段顺序)与审计 ``dropped``。

    - ``field_index`` 越界或不是整数的条目丢弃(``bad_field_index``);
    - ``anchor`` 必须是本次输入里的句柄或 id(``invalid_anchor``),片段必须能映射到元素
      (``unmapped_anchor``);
    - 值里的数字按 ``ground_prose_numbers`` 在**送给模型的那段文字**里接地;
    - 同一字段给了多条时取第一条通过校验的;值超长截断。
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
        value = " ".join(_scalar_text(raw.get("value")).split())
        if index is None or not 0 <= index < len(fields):
            dropped.setdefault("bad_field_index", []).append({"value": value[:120]})
            continue
        if not value or index in chosen:
            continue
        anchor = as_text(raw.get("anchor"))
        excerpt = table.get(anchor) or table.get(anchor.strip("[]【】 ").lower())
        reason = ""
        if excerpt is None:
            reason = "invalid_anchor"
        elif not excerpt.element_id:
            reason = "unmapped_anchor"
        else:
            reason = ground_prose_numbers(value, excerpt.text)
        if reason:
            dropped.setdefault(str(index), []).append({"reason": reason, "value": value[:120]})
            failures.setdefault(index, reason)
            continue
        chosen[index] = FieldValue(
            index, fields[index], value=value[:EXTRACT_VALUE_MAX_CHARS].rstrip(),
            element_id=excerpt.element_id,
            chunk_id=excerpt.chunk_id if excerpt.kind == "chunk" else "",
        )
    out = tuple(
        chosen.get(index) or FieldValue(index, text, missing=failures.get(index, "not_stated"))
        for index, text in enumerate(fields)
    )
    return out, dropped


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
                self._event_log.logger.exception("paper extraction failed for %s", key[1])
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
            code = model_error_code(exc)
            if code in INPUT_DETERMINISTIC_MODEL_ERROR_CODES:
                reason = code if code == "malformed_response" else f"model_error:{code}"
            else:
                reason = f"model_error:{code or type(exc).__name__}"
            return ExtractionResult(*key, "failed", reason=reason, model=model, **common)
        parsed = parse_model_object(raw)
        if parsed is None:
            return ExtractionResult(*key, "failed", reason="malformed", model=model, **common)
        values, dropped = verify_extraction(parsed, fields, selection.excerpts)
        return ExtractionResult(
            *key, "extracted", fields=values, model=model, dropped=_dropped_count(dropped), **common,
        )

    def _select(self, key, fields, cancel_event) -> _Selection:
        """开头 2 个元素 + 每字段前 4 个片段(去重 ≤12,文档顺序)。"""
        notebook_id, source_id = key
        retrieval = self._retrieval()
        selection = _Selection()
        with retrieval_fanout_slot():
            page = self._sources.source_elements_page(source_id, offset=0, limit=EXTRACT_OPENING_ELEMENTS)
            outline, selection.truncated = retrieval.section_outline(notebook_id, source_id)
        for element in page.items:
            text = (element.text or "").strip()[:EXTRACT_OPENING_MAX_CHARS]
            if element.source_id == source_id and text:
                selection.excerpts.append(Excerpt(
                    f"e{len(selection.excerpts) + 1}", "element", element.id, "",
                    element.location_label or "", text,
                ))
        eligible = [row[0] for row in outline if not row[2]]
        if not eligible:
            return selection
        member = set(eligible)
        picked: list[str] = []
        lanes: set[str] = set()
        for text in fields:
            raise_if_cancelled(cancel_event)
            with retrieval_fanout_slot():
                sims = retrieval.section_chunk_similarities(notebook_id, source_id, eligible, [text])
                if sims:
                    lanes.add("vector")
                    top = [cid for cid, _s in sorted(sims.items(), key=lambda kv: (-kv[1], kv[0]))]
                else:
                    lanes.add("fts")
                    with self._database.connect() as db:
                        hits = self._knowledge.chunk_fts_search(
                            db, notebook_id, text, _FTS_PROBE, allowed_source_ids=[source_id],
                        )
                    top = [str(hit["chunk_id"]) for hit in hits if str(hit["chunk_id"]) in member]
            for chunk_id in top[:EXTRACT_CHUNKS_PER_FIELD]:
                if chunk_id not in picked and len(picked) < EXTRACT_MAX_CHUNKS:
                    picked.append(chunk_id)
        selection.lane = lanes.pop() if len(lanes) == 1 else "mixed"
        if not picked:
            return selection
        with retrieval_fanout_slot():
            chunks = retrieval.section_chunks(notebook_id, source_id, picked, len(picked), "")
        for index, chunk in enumerate(chunks, 1):
            text = (chunk.text or "").strip()[:EXTRACT_PASSAGE_MAX_CHARS]
            if not text:
                continue
            element_ids = [str(e) for e in (chunk.element_ids or ()) if str(e or "")]
            selection.excerpts.append(Excerpt(
                f"c{index}", "chunk", element_ids[0] if element_ids else "", chunk.chunk_id,
                chunk.section_path or "", text,
            ))
        return selection

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
