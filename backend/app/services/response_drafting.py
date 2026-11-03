"""Reasoning response drafting, context rendering and citation assembly.

AskService owns admission, authority, cancellation boundaries and persistence;
this module owns the implementation behind the response-draft stage. Evidence-only
Ask shares these assemblers without invoking the synthesis implementation.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Sequence

from app.core.ask_context import _ASK_MODEL_ERRORS
from app.core.llm import cap_kwargs
from app.core.model_values import as_text_list
from app.domain.report_followup import (
    RESTORED_KIND_CHUNK,
    RESTORED_KIND_ELEMENT,
    RESTORED_KIND_KG,
    RESTORED_KIND_MEMORY,
    ReportFollowup,
    RestoredReportEvidence,
    SectionSelection,
)
from app.domain.retrieval import KnowledgeContextOutcome
from app.domain.retrieval_control import RetrievalControlError
from app.models.ask import TRACE_ANCHOR_EVIDENCE_IDS_MAX, AnswerAnchor, AskResponse, Citation, TraceStep
from app.models.knowledge import KnowledgeFieldValue, KnowledgeRecord
from app.services.cancellation import AskCancelled, CancelEvent, raise_if_cancelled
from app.services.chunk_federation import reasoning_order_for
from app.services.citation_markers import LOOSE_MARKER_RE, MARKER_RE, marker_keys
from app.services.context_spans import Glue, clip, concat
from app.services.document_read_answer import document_read_block_reserve
from app.services.evidence_context import CitationLookups, anchor_image_targets
from app.services.model_work import MalformedModelResponse
from app.services.paper_card_answer import (
    PaperCardEvidence,
    compose_paper_answer,
    coverage_line_rendered,
    paper_digest_block,
    paper_digest_reserve,
    roster_omitted_for,
    sectioned_paper_prefix,
)
from app.services.paper_card_answer import registered_keys as paper_registered_keys
from app.services.paper_card_answer import with_registration as with_paper_registration
from app.services.prompts import ANSWER_SCHEMA_HINT, answer_prompt
from app.services.report_evidence import (
    ReportCards,
    ReportEvidencePartition,
    ReportTurn,
    assemble_report_evidence,
    citation_identity,
    report_readable,
)
from app.services.report_followup import select_report_sections
from app.services.retrieval import RetrievedKnowledge, classify_evidence, passage_floor
from app.services.source_element_selection import rank_source_elements
from app.services.source_scope import subjectless_run_active


def _answer_text(data: dict) -> str:
    """The synthesis reply's ``answer`` as prose, or "" when it is not a string.

    The shape boundary delivers off-type fields (reported, not rejected), so
    a list/dict/number here must read as "no answer" — a bare ``str()`` would
    turn ``[]`` into the non-empty text "[]", count as a successful synthesis,
    skip the empty-content retry and publish container syntax as the answer
    (codex #720 R2). Empty is what ``_answer_with_retry`` already handles.
    """
    value = data.get("answer") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""

_MARKER_GROUP_RE = MARKER_RE

_LOOSE_MARKER_GROUP_RE = LOOSE_MARKER_RE

_NO_RETRIEVAL_EVIDENCE_MESSAGE = (
    "当前检索没有找到足以支撑回答的来源证据。资料可能已经导入，"
    "但本次没有命中；请尝试补充文章标题、关键词或原文中的术语后重试。"
)

@dataclass(frozen=True)
class _ReasoningContext:
    """``AskService._assemble_reasoning_context``'s result: the reasoning
    synthesis context and everything ``_answer_reasoning`` reports about it."""

    context_block: str
    id_map: dict
    counts: dict
    # The baseline-manifest capture (``baseline_sink``'s payload).
    baseline: dict
    has_external: bool
    admitted_external: set

def _merged_section_baseline(rows: Sequence[Mapping[str, Any]]) -> dict:
    """One baseline capture for a sectioned synthesis: the per-section
    captures (``_ReasoningContext.baseline``) concatenated in section order --
    blocks joined by a newline, id maps and handles chained, budgets and
    capture errors summed -- plus their ``CitationLookups`` folded into one,
    so the passage-card leg reuses every section's reads."""
    lookups = CitationLookups()
    for row in rows:
        section_lookups = row.get("citation_lookups")
        if isinstance(section_lookups, CitationLookups):
            lookups.merge(section_lookups)
    return {
        "context_block": "\n".join(
            str(row.get("context_block") or "") for row in rows
        ),
        "id_map": {
            key: value
            for row in rows
            for key, value in dict(row.get("id_map") or {}).items()
        },
        "ordered_handles": tuple(
            handle
            for row in rows
            for handle in tuple(row.get("ordered_handles") or ())
        ),
        "budget_chars": sum(int(row.get("budget_chars") or 0) for row in rows),
        "capture_error_count": sum(
            int(row.get("capture_error_count") or 0) for row in rows
        ),
        "citation_lookups": lookups,
    }

_EXTERNAL_CONTEXT_HEADING = "External evidence"

_EXTERNAL_CONTEXT_PREFIX_CHARS = len(f"\n\n[{_EXTERNAL_CONTEXT_HEADING}]\n")

@dataclass
class _SectionedSynthesis:
    """按节合成成功后的整篇产物(设计文档 §3.1)。

    ``counts`` 是各节装配计数之和 —— 同一条证据被两节绑上就计两次,那是诚实的:
    它确实进了两次 prompt。
    """

    answer: str
    llm_grounded: bool
    anchors: list
    sections: int
    counts: dict
    section_grounding: list
    baseline_assemblies: list

def _keep_only_section_markers(answer: str, section_keys: set) -> str:
    """按节合成:把一节的正文里**不属于本节号段**的 `[k…]` 标记清掉。

    为什么必须在拼接**之前**做(codex PR#407 R3 P1):按节解析已经正确地不给跨节标记
    发锚点了,但正文里那个 `[k1]` 还原样留着 —— 而**前端是按合并后的引用表解析正文
    标记的**(`buildAnswerReferences` 拿 `answer.matchAll` 去查 `anchorsByKey`)。第二节
    写出的 `[k1]` 于是照样绑到第一节那条毫不相干的证据上:按节解析防住的误绑,从
    渲染这道后门原样回来了。号段偏移买到的隔离,必须在**送到读者眼前的那份文本**上
    也成立。

本函数的两处设计取舍:

    * **混合组保留合法子集**而不是整组丢弃。号段互不相交是**服务端**造的事实,
      本节模型手里从来就没有过 `k1` 这个号,它只可能是幻觉,而同组里的
      `k10001` 是服务端确确实实发给这一节、它也确确实实看见了的证据。连着好的一半
      一起丢,是拿一次幻觉惩罚一条真引用。
    * **空白收敛带 `(?<=\\S)` 前置断言**:标记按 prompt 规则 1 挂在句末,删掉只会留下
      「词__词」这种**跟在非空白字符后面**的空格串。不加断言的话,分节答案里的嵌套
      列表(`  - 项`)与四空格代码块会被一起压平 —— 那对长文比漏一个标记严重得多。

    合法标记逐字保留(顺带把 `[ k10001]` 这类畸形写法规范化成可绑定的形态)。整组
    全非法时连同方括号一起移除,所以不会留下空括号或多余逗号。
    """
    def _sub(match: re.Match) -> str:
        keys = marker_keys(match.group(0))
        kept = [key for key in keys if key in section_keys]
        return ("[" + ", ".join(kept) + "]") if kept else ""

    cleaned = _LOOSE_MARKER_GROUP_RE.sub(_sub, answer or "")
    return re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned).strip()

def knowledge_record(object_type: str, obj: dict, schema) -> KnowledgeRecord:
    """KG 对象 → KnowledgeRecord 展示投影(canonical body;facade 的
    _knowledge_record 与 ask_reasoning 的 related_knowledge 共用)。"""
    from app.domain.knowledge_contracts import SCHEMA_FIELDS_PAYLOAD_KEY
    from app.services.knowledge_governance import knowledge_headline

    payload = obj.get("payload") or {}
    common_payload = payload
    declared = payload.get(SCHEMA_FIELDS_PAYLOAD_KEY)
    if isinstance(declared, dict):
        payload = {**payload, **declared}
    visible_payload_keys = [
        key for key in payload if not str(key).startswith("_")
    ]
    # A definition controls preferred field order; it must not hide persisted
    # user data when an owner later removes a field from the definition. Keep
    # any additional payload keys after the configured fields so existing
    # objects remain completely browsable and can be governed/migrated.
    keys = (
        [*schema.fields, *(
            key for key in visible_payload_keys if key not in schema.fields
        )]
        if schema
        else visible_payload_keys
    )
    fields: List[KnowledgeFieldValue] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (list, tuple)):
            text = ", ".join(str(v) for v in value if str(v).strip())
        elif value is None:
            text = ""
        else:
            text = str(value)
        if text.strip():
            fields.append(KnowledgeFieldValue(key=key, value=text.strip()))
    return KnowledgeRecord(
        id=obj["id"],
        object_type=object_type,
        headline=knowledge_headline(object_type, common_payload),
        fields=fields,
        status=obj.get("status", "approved"),
        owner=obj.get("owner", ""),
        last_reviewed=obj.get("last_reviewed", ""),
        evidence=obj.get("evidence", []),
    )

def _synthesis_step_detail(
    *,
    citations: int,
    anchors: Sequence[Any],
    evidence_level: str,
    counts: Mapping[str, Any],
    enumerated_collections: int,
    enumeration_block_dropped: bool,
    document_read_block_dropped: bool,
    outline_planned: bool,
    sectioned: Any,
    outline_fallback: bool,
    outline_skipped: Sequence[str],
    outline_bypassed: str = "",
) -> dict:
    """The terminal ``synthesis`` trace step's ``detail`` — a pure builder.

    Lifted out of ``_draft_reasoning_response`` verbatim (same keys, same
    order, same values) so the run-terminal facts below could be added without
    spending that hot function's zero-slack length ceiling.  It reads nothing
    but its arguments and writes nothing: every value here is already decided
    by the time the step is written.
    """

    detail = {
        "citations": citations,
        "anchors": len(anchors),
        "evidence_level": evidence_level,
        # Actual counts that entered the synthesis prompt (post
        # per-partition budget truncation), distinct from the
        # earlier "answer" step's pre-truncation candidate pool.
        "included_kg": counts.get("included_kg", 0),
        "included_chunks": counts.get("included_chunks", 0),
        "included_elements": counts.get("included_elements", 0),
        "included_collections": counts.get("included_collections", 0),
        # 外部证据(reflect 插件动作带回的库外材料)的装入/丢弃披露。丢弃有两
        # 个理由——外部段自己的字符预算装不下、以及没有可打开的 http(s) URL 被
        # 渲染契约拒收——都算在这一个数里:对读者而言「检索器带回来了、模型没
        # 看到」是同一件事,而两个理由的区分属于插件侧的 plugin_action 轨迹步。
        #
        # **稀疏**:``counts`` 只在这一轮真的有外部证据时才带这两个键
        # (``_append_external_context``),所以没有插件动作的答案的 detail 与
        # 接入这个特性之前逐键相等 —— 恒零地写进去会改掉每一条既有答案的持久化
        # payload(golden oracle 正是这样红的)。
        **{
            key: counts[key]
            for key in ("external_included", "external_dropped")
            if key in counts
        },
        # 按篇原文取样(``read_document``)真正进合成 prompt 的原文元素数。
        # 同样**稀疏**、同样的理由:没有按篇取样的一轮的 detail 与接入这个特性
        # 之前逐键相等。关联报告复原证据分区自己进 prompt 的条目数
        # (``included_report_evidence``)同一条纪律:没读报告的一轮没有这个键。
        **{
            key: counts[key]
            for key in (
                "included_document_reads", "included_spreadsheets",
                "included_report_evidence", "included_paper_cards",
            )
            if key in counts
        },
        # 本轮产生的类型化集合清单数(诊断字段,不上屏)。清单本身
        # 进合成 prompt / 结果卡由 T5 接管;在这里露一个数,是为了
        # 让「工具跑了但答案没体现」这种情况在轨迹里可查。
        "enumerated_collections": enumerated_collections,
        # 枚举块因预算太紧(连块头都放不下)被整体挤出合成证据;
        # 结果卡不受影响(typed_collection_result_sets 仍然完整),
        # 只是模型这一轮没看到清单预览。trace 已闭合,这是唯一
        # 挂点。
        "enumeration_block_dropped": enumeration_block_dropped,
        # 同上,给按篇取样块的那一份:整块因子预算(structured 段不得超过
        # ``chunk_context_chars`` 的一半)被挡在合成 prompt 之外。取样仍然发生过
        # (它在检索轨迹的 ``read_document`` 步里,``result_ids`` 齐全),只是模型
        # 这一轮没看到那几段原文。
        #
        # **稀疏**,与上面两组外部证据键同一条纪律、同一个理由:恒为 False 地写
        # 进去会改掉每一条既有答案的持久化 payload(golden oracle 会红),而「没有
        # 被挤掉」本来就是缺席该表达的事实。
        **({"document_read_block_dropped": True}
           if document_read_block_dropped else {}),
        # Agentic Memory P4 (T1): the answer's actually-bound
        # [k] anchors, by object_id — the raw material for
        # step→anchor attribution (see TRACE_ANCHOR_EVIDENCE_
        # IDS_MAX's docstring in app.models.ask for the
        # disclosure argument and the cap's derivation).
        # Written unconditionally, including the empty-list
        # zero-anchor case — a synthesis step with no anchors
        # is itself a real signal, not an absent one.
        "anchor_evidence_ids": [
            anchor.object_id for anchor in anchors
        ][:TRACE_ANCHOR_EVIDENCE_IDS_MAX],
    }
    if len(anchors) > TRACE_ANCHOR_EVIDENCE_IDS_MAX:
        # Sparse marker (mirrors the "neighbor_truncated" pattern
        # in reasoning_retrieval.py's expand step): the cap is a
        # protocol ceiling that should never actually bind in
        # practice under the existing per-tier retrieval budgets,
        # so this key only appears on the (unexpected) day it does.
        detail["anchor_evidence_ids_truncated"] = True
    if outline_planned:
        # 这组键在大纲**规划跑过**时就出现,而不只在按节合成真的被尝试
        # 过时(codex r6):大纲只装配出 1 个有证据节时按节合成被绕过,
        # 但另一节「问到了没找到」的披露不能跟着消失——否则单节答案看
        # 起来是完整的。没有大纲、低档位与关闭态下规划不会跑,synthesis
        # 步的 detail 仍逐键不变(冻结基线口径)。
        detail.update({
            # 实际合成的节数;回退时为 0(分节产物已全部丢弃)。
            "outline_sections": (
                sectioned.sections if sectioned is not None else 0
            ),
            "outline_fallback": outline_fallback,
            # 被跳过的空节标题:它们是「问到了但没找到」的诚实记录,
            # 只在轨迹里露面,答案里不留空壳标题。
            "outline_skipped": outline_skipped,
            # 只落 trace detail,不扩 AskResponse。每节结果已经过该节
            # 自己的 classify_evidence,不是模型裸自报。
            "section_grounded": (
                sectioned.section_grounding if sectioned is not None else []
            ),
            "ungrounded_sections": (
                [
                    item["title"]
                    for item in sectioned.section_grounding
                    if not item["grounded"]
                ]
                if sectioned is not None else []
            ),
            # 为什么有 ≥2 节却没按节写(``_outline_bypass_reason``,闭合取值,
            # 目前只有 ``linked_report``,D8)。**稀疏**:只在这种绕过时出现,
            # 与「大纲不足两节」(同样 ``outline_sections == 0``)区分开。
            **({"outline_bypassed": outline_bypassed} if outline_bypassed else {}),
        })
    return detail

OUTLINE_BYPASSED_LINKED_REPORT = "linked_report"

def _outline_bypass_reason(report: "ReportTurn | None", outline_slices: Sequence[Any]) -> str:
    """Why a run whose outline had two or more sections with evidence was
    still written in one synthesis, for the synthesis step's sparse
    ``outline_bypassed`` key: ``linked_report`` when the turn read an attached
    report (plan D8 -- the report block and its evidence are whole-turn
    context, cut into sections they would either be repeated k times or kept
    from a section); "" otherwise, the key then absent."""
    if report is not None and len(outline_slices) >= 2:
        return OUTLINE_BYPASSED_LINKED_REPORT
    return ""

_REASONING_CHUNK_HEADING = "Retrieved chunks"

SPREADSHEET_OMITTED_NOTICE = (
    "[Spreadsheet analysis results exist for this question but were left out "
    "of this context for space; do not state that they are absent.]"
)

DOCUMENT_READ_OMITTED_NOTICE = (
    "[Sampled document excerpts exist for this question but were left out "
    "of this context for space; do not state that they are absent.]"
)

# Shared with the core retrieval run through ask_service's legacy re-export.
# A produced None is a value; this sentinel alone means not yet produced.
_UNSET: Any = object()

@dataclass(eq=False)
class _ReasoningDraftRun:
    """Cross-phase state of one ``DefaultResponseDraftStage.draft_response`` call,
    handed from phase helper to phase helper by identity.

    The fields above ``related_knowledge`` are the draft input rebound by the
    prologue (``_begin_reasoning_draft``) under the names the body has always
    read -- list snapshots of the input's evidence tuples, the attached report
    the turn reads, the trace sink, the cancellation and the request's
    model-error sink. ``trace`` is the one list the section progress steps and
    the synthesis step append to; ``citations`` is the one list the card phase
    extends (until a checklist answer with no bound marker replaces it); the
    counts / baseline dicts and the admitted-external set are the sinks the
    synthesis fills in place. The run holds no callable pointing back at it
    (``record_section_step`` is bound on call), so it is freed by refcount when
    the call returns, as the single method's locals were.

    The fields from ``related_knowledge`` on are set by the phase that
    produces them and start as ``_UNSET``; a phase reads them through
    ``ready`` so that running the phases out of order fails loudly instead of
    reading an empty default (several produced values are legitimately
    ``None`` or empty: no sectioned synthesis, no baseline manifest)."""

    stage: Any
    prepared: Any
    notebook_id: Any
    conversation_id: Any
    reasoning_history: Any
    style_block: Any
    research_question: Any
    limits: Any
    retrieval_effort: Any
    intent_contract: Any
    memory_hits: list
    structured_batch: Any
    completeness_unavailable: Any
    no_usable_kg: Any
    top_hits: list
    elements: list
    trace: list
    chunks: list
    chains: list
    enumerations: list
    spreadsheet_results: list
    collection_map_text: Any
    reasoning_outline: list
    reasoning_outline_evidence: list
    historical_reasoning_chunks: list
    report: Any
    on_trace: Any
    cancel_event: Any
    err_sink: list
    # ``_draft_related_knowledge``
    related_knowledge: Any = _UNSET
    citations: Any = _UNSET
    # ``_draft_synthesis_inputs`` (the answer's starting values,
    # which the synthesis phase replaces)
    answer: Any = _UNSET
    llm_grounded: Any = _UNSET
    anchors: Any = _UNSET
    synth_failed: Any = _UNSET
    synthesis_ran: Any = _UNSET
    synthesis_started: Any = _UNSET
    reasoning_counts: Any = _UNSET
    reasoning_baseline: Any = _UNSET
    run_lookups: Any = _UNSET
    admitted_external: Any = _UNSET
    answer_client: Any = _UNSET
    collection_map_prompt_block: Any = _UNSET
    structured_block: Any = _UNSET
    structured_map: Any = _UNSET
    typed_collection_result_sets: Any = _UNSET
    enumeration_block_dropped: Any = _UNSET
    collection_item_citations: Any = _UNSET
    document_read_block_dropped: Any = _UNSET
    paper_evidence: Any = _UNSET
    # ``_draft_sectioned_attempt``
    outline_slices: Any = _UNSET
    outline_skipped: Any = _UNSET
    outline_attempted: Any = _UNSET
    outline_planned: Any = _UNSET
    sectioned: Any = _UNSET
    outline_err_mark: Any = _UNSET
    # ``_draft_synthesis``
    outline_fallback: Any = _UNSET
    # ``_draft_baseline_manifest``
    final_baseline_manifest: Any = _UNSET
    # ``_draft_citation_cards``
    chain_evidence: Any = _UNSET
    element_evidence: Any = _UNSET
    report_cards: Any = _UNSET
    collection_citation_ids: Any = _UNSET
    # ``_draft_evidence_level``
    evidence_level: Any = _UNSET
    top_relevance: Any = _UNSET
    grounded: Any = _UNSET
    # ``_draft_conclusion``
    conclusion: Any = _UNSET
    llm_mode: Any = _UNSET
    warning: Any = _UNSET

    def ready(self, name: str) -> Any:
        """A phase-produced field, refusing one its phase has not set yet."""
        value = getattr(self, name)
        if value is _UNSET:
            raise RuntimeError(
                f"_draft_reasoning_response phase read run.{name} before the phase that sets it")
        return value

    def record_section_step(self, step: TraceStep) -> None:
        """分节进度步:实时推给客户端,同时留在本轮轨迹里。"""
        raise_if_cancelled(self.cancel_event)
        self.trace.append(step)
        if self.on_trace:
            self.on_trace(step)


class DefaultResponseDraftStage:
    """Own reasoning response synthesis and its shared evidence assembly.

    Receives only rendering, model and configuration dependencies. The frozen
    application input supplies the run's evidence; no dependency grants answer
    persistence, job control, or a reference to the Ask orchestrator.
    """

    def __init__(self, *, settings, evidence_context, model_clients,
                 note_model_error, memory_context, effective_schemas, logger,
                 paper_display_reader=None):
        self.settings = settings
        self.evidence_context = evidence_context
        self.model_clients = model_clients
        self.note_model_error = note_model_error
        self.memory_context = memory_context
        self.effective_schemas = effective_schemas
        self.logger = logger
        self.paper_display_reader = (
            paper_display_reader if callable(paper_display_reader) else None)

    _MIX_KG_KEY_BASE = 1000

    _MEMORY_KEY_BASE = 3000

    _ELEMENT_KEY_BASE = 4000

    _COLLECTION_KEY_BASE = 5000

    _EXTERNAL_KEY_BASE = 6000

    _DOCUMENT_READ_KEY_BASE = 7000

    _PAPER_CARD_KEY_BASE = 9000

    _REPORT_KEY_BANDS = {
        RESTORED_KIND_CHUNK: (1, _MIX_KG_KEY_BASE - 1),
        RESTORED_KIND_KG: (_MIX_KG_KEY_BASE + 1, 1999),
        RESTORED_KIND_MEMORY: (_MEMORY_KEY_BASE + 1, _ELEMENT_KEY_BASE - 1),
        RESTORED_KIND_ELEMENT: (_ELEMENT_KEY_BASE + 1, _COLLECTION_KEY_BASE - 1),
    }


    def _chunk_answer_context(self, chunks, budget_chars: "int | None" = None,
                              notebook_id: str = "", id_offset: int = 0,
                              lookup_sink: "CitationLookups | None" = None) -> tuple:
        return self.evidence_context.chunk_context(
            chunks, notebook_id=notebook_id, id_offset=id_offset,
            budget_chars=budget_chars, lookup_sink=lookup_sink)

    def _answer_context(self, notebook_id: str, top_hits: List[RetrievedKnowledge],
                        id_offset: int = 0, budget_chars: int | None = None) -> tuple:
        return self.evidence_context.knowledge_context(
            notebook_id, top_hits, id_offset=id_offset,
            budget_chars=budget_chars)

    def _parse_answer_anchors(self, answer: str, id_map: dict) -> list:
        return self.evidence_context.parse_anchors(answer, id_map)

    @staticmethod
    def _bounded_context_append(
        context_block: str,
        id_map: dict,
        block: str,
        block_map: dict,
        *,
        budget_chars: int,
        heading: str = "",
        admission_sink: dict | None = None,
    ) -> tuple[str, dict]:
        """Append one evidence block without crossing a partition hard limit."""
        if not block or block == "(none)":
            return context_block, id_map
        prefix = ("\n\n" if context_block else "")
        if heading:
            prefix += f"[{heading}]\n"
        remaining = max(0, int(budget_chars) - len(context_block))
        if remaining <= len(prefix):
            return context_block, id_map
        rendered = clip(concat(Glue(prefix), block), remaining)
        if admission_sink is not None and len(prefix) + len(block) > remaining:
            admission_sink["ambiguous_truncations"] = int(
                admission_sink.get("ambiguous_truncations") or 0
            ) + 1
        merged = dict(id_map)
        for key, value in block_map.items():
            if re.search(rf"(?m)^{re.escape(str(key))}:", rendered):
                merged[key] = value
        return concat(context_block, rendered), merged

    def _append_external_context(
        self, context_block: str, id_map: dict, items, *, key_offset: int
    ) -> tuple[str, dict, set, dict]:
        """外部证据段:在库内各段**之后**拼接,自带独立预算。

        它是唯一在总预算截断(``context_block[:total_context_budget]``)与证据
        精炼之后才拼的段,两条理由都硬:①精炼是一次模型调用,把外部材料交给它
        重写等于让模型转述一段带出处的引文,而引用卡承诺显示的是插件原文;
        ②外部证据是本轮唯一「库里查不到」的材料,把它排在库内证据后面按同一
        个总预算竞争,等于让最容易被挤掉的那一段恰好是不可替代的那一段。
        于是它拿 ``EXTERNAL_EVIDENCE_CONTEXT_CHARS`` 这一份独立的、有上限的
        额外预算(``external_context`` 自己按条丢弃,绝不切半条)。

        零外部证据时返回**原对象**:``external_context`` 给出 ``"(none)"``,
        ``_bounded_context_append`` 对它是恒等,所以 context_block / id_map /
        进而 prompt 逐字节不变。

        返回 ``(context_block, id_map, admitted_keys, counts)``。
        ``admitted_keys`` 是**真的进了这次 prompt** 的那批 ``ExternalEvidence
        .key``,它是引用侧唯一的准绳:引用是「答案可能引自这条材料」的声明,
        被预算丢掉的那条模型从没见过,把它列进引用就是给一份没看过它的答案挂
        来源(设计文档 §6.3)。``counts`` 是给轨迹披露的
        ``external_included``/``external_dropped``,由 ``external_context`` 的
        两个理由(预算丢弃 / 无可打开 URL 被拒)相加而来。
        """
        sink: dict = {}
        block, block_map = self.evidence_context.external_context(
            items, id_offset=self._external_key_offset(id_map, key_offset),
            truncation_sink=sink,
        )
        context_block, merged = self._bounded_context_append(
            context_block, id_map, block, block_map,
            # 预算按「已用 + 本段前缀 + 本段」给足:真正的上限已经由
            # ``external_context`` 自己花掉了,这里再夹一次只会把最后一条切成
            # 半句(见那里为什么外部摘录不许切半条)。
            budget_chars=(
                len(context_block) + _EXTERNAL_CONTEXT_PREFIX_CHARS + len(block)
            ),
            heading=_EXTERNAL_CONTEXT_HEADING,
        )
        # 按**合并后**的 id_map 反查,而不是照抄 block_map:
        # ``_bounded_context_append`` 会把没能留在渲染文本里的 key 过滤掉,
        # 照抄等于宣称一条其实没进 prompt 的证据进了。
        admitted = {
            str(value.get("object_id") or "")
            for key, value in block_map.items() if key in merged
        }
        # **稀疏**:没有外部证据的一轮一个键都不写。这两个数是给「检索器带回了
        # N 条、模型只看到 M 条」这件事做披露的,而一次没调过插件动作的问答里
        # 那句话没有内容;恒零地写进去会让每一条既有答案的持久化 payload 与
        # synthesis 轨迹 detail 都多两个键(golden oracle 正是这样红的),
        # 而「无外部证据时逐键不变」是这个特性自己许下的承诺。
        counts = {} if not items else {
            "external_included": len(admitted),
            "external_dropped": (
                int(sink.get("truncated") or 0) + int(sink.get("rejected") or 0)
            ),
        }
        return context_block, merged, admitted, counts

    @classmethod
    def _external_key_offset(cls, id_map: dict, key_offset: int) -> int:
        """Where this synthesis' external keys start numbering from.

        External evidence shares the 6000 band with the workbook results
        (``spreadsheet_prompt_block`` writes ``k6001`` + plan position;
        ``ask_evidence.kind_for_key`` tells the two apart by ``definition``),
        and the workbook block is already in ``id_map`` when this runs.  So
        the external keys continue after the highest workbook key instead of
        restarting at ``k6001``: two lines under one key would both reach the
        prompt, and the id_map merge would silently replace the workbook
        receipt with the external one.  Without workbook keys the offset is
        the band base, exactly as before.
        """
        base = key_offset + cls._EXTERNAL_KEY_BASE
        taken = [
            int(match.group(1)) for key in id_map
            if (match := re.fullmatch(r"k(\d+)", str(key))) is not None
            and base < int(match.group(1)) < key_offset + cls._DOCUMENT_READ_KEY_BASE
        ]
        return max(taken, default=base)

    @staticmethod
    def _external_exact_keys(anchors) -> set:
        """The ``[k]`` keys of the external anchors, for ``classify_evidence``.

        External evidence reaches grounding through the *deterministic,
        non-ranked* door (``exact_evidence_keys`` — the one a source-backed
        exact enumeration row already uses) rather than through
        ``evidence_pool``.  The alternative — appending a
        ``SimpleNamespace(object_id=…, relevance=1.0)`` per item, the way
        chains and elements do — would work for the grounding decision and lie
        about everything else: ``classify_evidence`` also returns
        ``top_relevance`` as ``max(h.relevance for h in top_hits)``, so a run
        that found NOTHING in the library and answered purely from a plugin
        would report a perfect retrieval score.  There is no retrieval score
        for external material; inventing one to move a different decision is
        how a metric stops meaning anything.

        Read off the anchors rather than off ``external_evidence``, and that
        is the load-bearing part: an ``object_type == "external"`` anchor can
        only have been minted by ``external_context`` AND bound by
        ``parse_anchors``, so it already carries both facts this needs — the
        item really entered the prompt, and the answer really cited it.
        """

        return {
            anchor.key for anchor in anchors
            if anchor.object_type == "external"
        }

    def _append_external_citations(self, citations, items, admitted) -> None:
        """Append fallback citations for the external items that were USED.

        ``admitted`` is the set of ``ExternalEvidence.key`` values that
        actually entered a synthesis prompt (filled by ``_answer_reasoning``'s
        ``external_sink``; in sectioned synthesis it is the union over every
        section).  Everything else the retriever brought back — items the
        block's own budget dropped, items refused for having no openable URL —
        is silently absent from the answer the model saw, and a citation is a
        claim that the answer could have been written from that material.
        Listing all of them would put a source under an answer that never had
        it, which is precisely the failure the citation list exists to make
        impossible.

        The append lands at the TAIL, after the spreadsheet receipts and after
        the enumeration branch may have cleared the list, because that is the
        design's stated position (§6.3) and because an enumeration answer with
        no bound marker has no verifiable attribution for library evidence
        *or* for external evidence.
        """

        citations.extend(self.evidence_context.external_citations([
            item for item in items
            if str(getattr(item, "key", "") or "") in admitted
        ]))

    @staticmethod
    def _memory_citations(anchors, hits) -> list[Citation]:
        by_id = {hit.memory_id: hit for hit in hits}
        out: list[Citation] = []
        for anchor in anchors:
            hit = by_id.get(anchor.object_id) if anchor.object_type == "memory" else None
            if hit is None:
                continue
            out.append(Citation(
                label=f"Memory · {hit.title}",
                source_id="",
                element_id="",
                location_label="Memory",
                quoted_span=hit.text[:200],
                tier="personal",
                memory_id=hit.memory_id,
                provenance=dict(hit.provenance),
            ))
        return out

    def _report_prompt_parts(
        self, followup: "ReportFollowup | None", query: str,
    ) -> "ReportTurn | None":
        """The attached report as this turn's synthesis will use it, or None
        when the turn does not read it (no report, a verdict that keeps it
        out, or a followup without the parsed body -- ``report_readable``).

        Selects the sections for ``query`` within
        ``ask_report_context_max_chars`` (``select_report_sections``, D12),
        ranking them by relevance to it -- the same ranking orders the
        evidence partition: the chunk engine passes its rewritten retrieval
        query, the reasoning engine its research question. The block itself
        is rendered only once the evidence partition has mapped the report's
        keys (``ReportTurn.enter_prompt``).

        ``ask_chunk`` (a capped hot function) grew by three statements for
        the report, 156 -> 159 lines in ``architecture_boundary_baseline``:
        this call, ``_report_citations_and_keys`` and extending its cards; the
        rest of the wiring rides on lines it already had."""
        if not report_readable(followup):
            return None
        return ReportTurn(followup, select_report_sections(
            followup.digest, query, self.settings.ask_report_context_max_chars,
        ))

    def _render_report_kind(
        self, kind: str, items: Sequence[Any], *, notebook_id: str,
        id_offset: int, budget_chars: int,
        outcome: "KnowledgeContextOutcome | None" = None,
        lookups: "CitationLookups | None" = None,
    ) -> tuple[str, dict]:
        """One kind of restored item through its native renderer -- the same
        lines, id_map entries and ``object_type`` the turn's own retrieval of
        that object would get (D1), keys from ``id_offset + 1``, within
        ``budget_chars``. Passages share ``lookups`` (one partition's batch
        reads, reused by its citation cards); knowledge objects report what
        they folded or withheld into ``outcome``. Memory's renderer has its
        own per-entry and total caps but no budget: the longest prefix of
        ``items`` that fits."""
        if kind == RESTORED_KIND_CHUNK:
            return self._chunk_answer_context(
                list(items), budget_chars=budget_chars, notebook_id=notebook_id,
                id_offset=id_offset, lookup_sink=lookups,
            )
        if kind == RESTORED_KIND_ELEMENT:
            return self.evidence_context.element_context(
                list(items), notebook_id=notebook_id, id_offset=id_offset,
                budget_chars=budget_chars,
            )
        if kind == RESTORED_KIND_KG:
            return self.evidence_context.knowledge_context(
                notebook_id, list(items), id_offset=id_offset,
                budget_chars=budget_chars, outcome=outcome,
            )
        for count in range(len(items), 0, -1):
            block, block_map = self.memory_context(
                list(items[:count]), id_offset=id_offset,
            )
            if not block_map or len(block) <= budget_chars:
                return block, block_map
        return "(none)", {}

    def _append_report_evidence(
        self,
        context_block: str,
        id_map: dict,
        followup: ReportFollowup,
        selection: SectionSelection,
        *,
        notebook_id: str,
        key_offset: int = 0,
    ) -> ReportEvidencePartition:
        """Append the attached report's evidence partition (plan D1, §2.7) --
        ``report_evidence.assemble_report_evidence`` (the rules: relevance
        order, reuse, native bands, own budget, not-loaded keys) with this
        service's seats: the native renderers (``_render_report_kind``), the
        partition budget ``ask_report_evidence_budget_chars``, the key bands
        (``_REPORT_KEY_BANDS``) and ``_bounded_context_append``.

        Shared by the chunk engine (``_chunk_synthesis_context``) and the
        reasoning engine's assembly (pass its section ``key_offset``)."""

        def render(kind: str, items: Sequence[Any], **kwargs: Any) -> tuple[str, dict]:
            return self._render_report_kind(kind, items, notebook_id=notebook_id, **kwargs)

        return assemble_report_evidence(
            context_block, id_map, followup, selection,
            budget_chars=self.settings.ask_report_evidence_budget_chars,
            bands=self._REPORT_KEY_BANDS, render=render,
            append=self._bounded_context_append, key_offset=key_offset,
        )

    def _report_citations_and_keys(
        self,
        report: "ReportTurn | None",
        anchors: Sequence[AnswerAnchor],
        notebook_id: str,
        *,
        existing: Iterable[Citation] = (),
    ) -> ReportCards:
        """The answer's cards and grounding keys for the report evidence it
        bound (plan §2.7).

        Only anchors on a key the partition admitted count. Their restored
        objects get cards from the native builders -- passages
        (``chunk_citations``, anchor order, reusing the partition's batch
        reads), elements (``element_citations``), knowledge objects
        (``citations_from`` over the bound objects only), Memory
        (``_memory_citations``) -- minus any card the turn already has
        (``existing``, by ``citation_identity``). The bound keys go to
        ``classify_evidence(exact_evidence_keys=...)``: a restored item has no
        retrieval score of its own, and the report's citation is
        deterministic evidence the same way an external item is
        (``_external_exact_keys``). The bound passages are returned for the
        anchor image candidates (``anchor_image_targets``). Reads nothing
        when no restored item is bound."""
        if report is None or not report.admitted_keys:
            return ReportCards()
        bound = [anchor for anchor in anchors if anchor.key in report.admitted_keys]
        if not bound:
            return ReportCards()
        restored = report.followup.restored or RestoredReportEvidence()

        def bound_ids(*types: str, other: bool = False) -> set:
            return {
                anchor.object_id for anchor in bound
                if (anchor.object_type not in types) == other
            }

        chunk_ids = bound_ids("chunk")
        element_ids = bound_ids("element")
        memory_ids = bound_ids("memory")
        kg_ids = bound_ids("chunk", "element", "memory", "external", other=True)
        chunks = [item for item in restored.chunks if item.chunk_id in chunk_ids]
        elements = [item for item in restored.elements if item.element_id in element_ids]
        knowledge = [item for item in restored.knowledge if item.object_id in kg_ids]
        memory = [item for item in restored.memory if item.memory_id in memory_ids]
        cards: list[Citation] = []
        if chunks:
            cards.extend(card for card, _ids in self.evidence_context.chunk_citations(
                chunks, notebook_id=notebook_id, anchors=bound,
                lookups=report.partition.lookups,
            ))
        if elements:
            cards.extend(self.evidence_context.element_citations(
                elements, bound, notebook_id=notebook_id,
            ))
        if knowledge:
            cards.extend(self.evidence_context.citations_from(
                knowledge,
                {ev.element_id for hit in knowledge for ev in hit.evidence if ev.element_id},
                "KG evidence", notebook_id=notebook_id,
            ))
        if memory:
            cards.extend(self._memory_citations(bound, memory))
        seen = {citation_identity(card) for card in existing}
        fresh: list[Citation] = []
        for card in cards:
            identity = citation_identity(card)
            if identity not in seen:
                seen.add(identity)
                fresh.append(card)
        return ReportCards(
            citations=tuple(fresh), chunks=tuple(chunks),
            exact_keys=frozenset(anchor.key for anchor in bound),
        )

    def _extend_report_cards(
        self,
        citations: List[Citation],
        report: "ReportTurn | None",
        anchors: Sequence[AnswerAnchor],
        notebook_id: str,
    ) -> ReportCards:
        """``_report_citations_and_keys`` for the reasoning draft: the fresh
        cards of the restored items the answer bound are appended to
        ``citations`` in place (deduplicated against what it already holds),
        and the ``ReportCards`` are returned for the grounding keys, the anchor
        image candidates and the card-image exclusion. One call, so the card
        phase of the draft (``_draft_citation_cards``) spends one line on it."""
        cards = self._report_citations_and_keys(
            report, anchors, notebook_id, existing=citations,
        )
        citations.extend(cards.citations)
        return cards

    def _reasoning_report_turn(self, prepared) -> "ReportTurn | None":
        """The attached report as a reasoning turn's synthesis uses it, or
        None when the turn does not read it: the envelope's followup
        (``PreparedReasoningAsk.report_followup``, D11) with its sections
        selected for the run's research question (``_report_prompt_parts``,
        D12) -- the question the understanding step resolved "第三节" or
        "这份报告" into.

        Deterministic for the envelope, so the trace's ``report`` step
        (``_reasoning_report_step``), the answer draft and the retrieval-only
        draft each build it and select the same sections (a lexical score over
        at most ``REPORT_MAX_SECTIONS`` sections) instead of a mutable turn
        crossing the stage seams; each draft records its own partition on its
        own turn.

        ``_draft_reasoning_response`` (then a capped hot function, since split
        into phase helpers) grew by two statements for the report, 564 -> 566 lines in
        ``architecture_boundary_baseline``: this call and
        ``_extend_report_cards``. The rest of the wiring rides on lines it
        already had: the synthesis call takes the turn; the sectioned gate
        adds ``report is None`` (D8: a turn that reads the report keeps one
        synthesis -- the report block and its evidence are whole-turn
        context, and cutting them into sections would either repeat the block
        k times or hide the report from a section -- and the synthesis step
        says so, ``outline_bypassed``); the synthesis trigger
        adds the turn (the chunk engine's rule); the restored keys join the
        exact keys, the restored passages the anchor image candidates, their
        cards the card-image exclusion (as on the chunk engine, the anchors
        bring their images); and the response carries the receipt."""
        return self._report_prompt_parts(
            getattr(prepared, "report_followup", None), prepared.research_question,
        )

    def _append_reasoning_report_evidence(
        self,
        context_block: str,
        id_map: dict,
        report: "ReportTurn | None",
        *,
        notebook_id: str,
        key_offset: int,
    ) -> tuple[str, dict, int, dict]:
        """``_assemble_reasoning_context``'s report partition:
        ``(context_block, id_map, chars it took, counts)``. Without a report
        the inputs come back as they are, 0 and ``{}``; otherwise the shared
        ``_append_report_evidence`` appends the partition, the turn records it
        (``ReportTurn.admit``) and ``counts`` is
        ``{"included_report_evidence": n}`` -- the partition's own entries in
        the prompt (items reused from the turn's retrieval are counted where
        they already were)."""
        if report is None:
            return context_block, id_map, 0, {}
        partition = self._append_report_evidence(
            context_block, id_map, report.followup, report.selection,
            notebook_id=notebook_id, key_offset=key_offset,
        )
        report.admit(partition)
        return (
            partition.context_block, partition.id_map,
            len(partition.context_block) - len(context_block),
            {"included_report_evidence": len(partition.id_map) - len(id_map)},
        )

    def _refine_context(
        self,
        question: str,
        context_block: str,
        client,
        cancel_event: CancelEvent = None,
        budget_chars: int | None = None,
    ) -> str:
        """问题感知证据精炼:把 context_block 喂给 evidence_refine LLM,抽"相关要点"
        前置成聚焦上下文(参考性,不产生 [k] 锚点)。默认开(kg_query_refine_enabled);
        client 未配/失败/无内容 → 原样返回。所有调用方都必须传入已按
        ``evidence_refine`` workload 绑定的 client。"""
        if not (self.settings.kg_query_refine_enabled
                and getattr(client, "configured", False)
                and context_block.strip() and context_block.strip() != "(none)"):
            return context_block
        raise_if_cancelled(cancel_event)
        from app.services.prompts import EVIDENCE_REFINE_SCHEMA_HINT, evidence_refine_prompt
        ev_block = context_block[: self.settings.query_refine_max_chars]
        try:
            raw = client.chat_json(
                [{"role": "user", "content": evidence_refine_prompt(question, ev_block)}],
                EVIDENCE_REFINE_SCHEMA_HINT,
                timeout=self.settings.reasoning_timeout_seconds,
                max_retries=self.settings.reasoning_max_retries,
                cancel_event=cancel_event)
            rel = as_text_list(json.loads(raw).get("relevant"))
        except AskCancelled:
            raise
        except Exception:
            rel = []
        if rel:
            focused = ("Focused relevant evidence (for this question):\n"
                       + "\n".join(
                           f"- {x}" for x in
                           rel[: self.settings.query_refine_max_items]
                       )
                       + "\n\n")
            candidate = focused + context_block
            # Refinement is optional metadata.  Never evict already-budgeted,
            # citable evidence merely to prepend a model-generated summary.
            if budget_chars is None or len(candidate) <= budget_chars:
                context_block = candidate
        return context_block

    def _answer_with_retry(
        self, synth, model_label, service="llm"
    ):
        """答案合成有界重试(治思考型模型偶发空 content)。synth() 返回
        (answer, grounded, anchors);answer 空(思考型模型偶把输出预算耗在
        reasoning_content 上→content 空→chat_json 兜底 "{}"→空 answer,不抛异常、
        status=ok)或抛错 → 重试一次("{}" 不入 LLM 缓存,故重试是真·重掷);两次皆空/
        抛错 → emit 一条 model_error(空 content 本身静默不可见,补此条让"检索到却答不出"
        可追踪:前端横幅 + events.jsonl)。返回 (answer, grounded, anchors, ok)。

        **重试成功不留假报警**:第一次尝试失败、第二次拿到完整答案时,把本次调用
        期间新增的那几条响应内 model_error 摘掉。理由与按节合成回退那处同源——
        那次故障已经被同一轮吸收,用户拿到的是一份完整答案,再挂一条红色横幅
        「本次回答可能不完整」是在报一个并没有影响到结果的错误(真机:第一次合成
        耗时 4 分 25 秒返回空 content,重试后答案 78 锚点 / 5829 字,横幅照挂)。
        摘除**只**针对 ``mark`` 之后本次尝试自己记的那几条:同一 run 里更早的其它
        workload 报错(evidence_refine、embedding/rerank 等)一条都不许动。
        两次都失败时全部保留,包括最后那条 empty-content 的 RuntimeError ——
        「检索到却答不出」必须可见。``events.jsonl`` 始终记全:``note_model_error``
        的两个副作用里,只有响应里的这一份是给用户看的。"""
        # sink 是本轮 Ask 的响应内报警列表(ContextVar,请求局部)。直调/离线路径
        # 下它是 None:那时 note_model_error 只写事件日志、不写响应,没有可摘的
        # 东西,保持现状即可。
        sink = _ASK_MODEL_ERRORS.get()
        mark = len(sink) if sink is not None else None
        answer, grounded, anchors = "", False, []
        last_raised = False
        for _ in range(2):
            last_raised = False
            try:
                answer, grounded, anchors = synth()
            except (AskCancelled, RetrievalControlError):
                # ``synth()`` 把 KG 证据装配(``_answer_context`` ->
                # ``evidence_context.knowledge_context``)算在合成里,而那一步的
                # canonical 折叠范围读参与集座位。身份复核失败被下面那条宽
                # handler 吞掉,就会重试一次、记一条 answer 报警、返回空答案——
                # 一次越权上下文泄漏表现成「检索到却答不出」。登记在
                # ``test_participant_override_guard._SEAT_FAILSOFT_SITES``。
                raise
            except Exception as exc:
                self.note_model_error(
                    "answer", exc, workload_id="ask_answer"
                )
                answer, grounded, anchors = "", False, []
                last_raised = True
            if answer:
                if mark is not None:
                    # 只摘**本次调用自己**记下的那条 answer 报警,不是「mark 之后
                    # 的一切」。今天 synth() 内部没有别的 note_model_error 调用
                    # (`_refine_context` 静默吞异常),所以两种写法等价——但那是
                    # 一个会被下一次改动打破的巧合:给证据精炼补一条
                    # note_model_error 是很自然的一步,那之后位置式摘除会在
                    # 「首次即成功 + 精炼失败」时把别人的报警一起删掉,正好违反
                    # 「其它 workload 一条都不许动」。按身份过滤让这条前提不必
                    # 靠注释维持。
                    sink[mark:] = [
                        item for item in sink[mark:]
                        if item.get("workload_id") != "ask_answer"
                    ]
                return answer, grounded, anchors, True
        # 终态报警。最后一次尝试**没抛异常却答空**时带 reason="empty_answer":JSON
        # 本身合规、只是 answer 为空,与「返回格式异常」是两种现象,前端按 detail
        # 分别措辞;code 是 malformed_response(模型行为,不动熔断器)。服务身份由
        # note_model_error 按 workload 从注册表补齐,横幅因此能说出是哪个模型两次
        # 都答空。最后一次是抛异常收场时,现象已经由上面那条按异常记下的报警说清,
        # 终态这条只保留「检索到却答不出」的事实,不把上游故障冒充成答空。
        terminal: Exception = (
            RuntimeError(
                "answer synthesis failed after retry (both attempts raised)"
            )
            if last_raised
            else MalformedModelResponse(
                "answer synthesis produced empty content after retry "
                "(reasoning model likely spent output budget on discarded chain-of-thought)",
                reason="empty_answer",
            )
        )
        self.note_model_error(
            "answer", terminal, workload_id="ask_answer"
        )
        return answer, grounded, anchors, False

    def _assemble_reasoning_context(
        self,
        notebook_id,
        question,
        top_hits,
        elements,
        *,
        cancel_event: CancelEvent = None,
        chunks=None,
        chains=None,
        memory_hits=None,
        kg_context_chars: int | None = None,
        chunk_context_chars: int | None = None,
        element_items: int = 6,
        structured_block: str = "",
        structured_map: dict | None = None,
        collection_map_block: str = "",
        key_offset: int = 0,
        external_evidence=(),
        trailing_chunks=(),
        refine: bool = True,
        citation_lookups: "CitationLookups | None" = None,
        report: "ReportTurn | None" = None,
    ) -> "_ReasoningContext":
        """The reasoning synthesis context, assembled exactly as
        ``_answer_reasoning`` sends it (its parameters, same meaning; see that
        docstring): partitions, budgets, keys, the external segment and the
        per-partition counts, plus the baseline-manifest capture.

        ``report`` (the turn reads an attached report,
        ``_reasoning_report_turn``): its evidence partition
        (``_append_report_evidence``, own budget, native key bands after this
        section's keys) goes after the total-budget clip and the refinement
        and before the external segment, and is recorded on it
        (``ReportTurn.admit``). ``baseline["budget_chars"]`` adds what the
        partition actually took, the way it adds the external segment's, and
        ``counts`` gains ``included_report_evidence`` (the partition's own
        entries in the prompt; sparse: absent without a report). None: as
        before, byte for byte.

        ``refine`` runs the optional ``evidence_refine`` call that prepends a
        model-written summary.  ``_answer_reasoning`` refines outside the
        sectioned mode; the retrieval-only output (``refine=False``) does not,
        because that summary is not evidence -- it carries no ``[k]`` -- and it
        would be one more model call on a path that promises none.

        ``citation_lookups`` is the run's shared read memo (``CitationLookups``):
        the chunk blocks read through it -- only keys it lacks -- and it is
        handed back as ``baseline["citation_lookups"]`` (``None`` when not
        given) for the passage citation cards minted from this run.  Only
        ``_answer_reasoning`` passes one; the retrieval-only output has no card
        leg and keeps the memo-less reads.
        """
        raise_if_cancelled(cancel_event)
        chunks = chunks or []
        chains = chains or []
        chunk_budget = max(0, int(
            self.settings.chunk_answer_budget_chars
            if chunk_context_chars is None else chunk_context_chars
        ))
        kg_budget = max(0, int(
            self.settings.answer_context_budget_chars
            if kg_context_chars is None else kg_context_chars
        ))
        source_context = ""
        source_map: dict = {}
        baseline_admission: dict = {}
        # The run's shared read memo for the chunk blocks (see the docstring).
        lookup_sink = citation_lookups
        # The collection MAP goes in FIRST, ahead of every other block. It is
        # the smallest thing in the prompt (a fixed header plus one hard-capped
        # counts line) and the only one that cannot be recovered from anywhere
        # else: the reflect model is told to answer a too-large collection with
        # its count instead of paging it, and this is the only path that number
        # takes to the model writing the answer. Appended last it would be the
        # first casualty of a full chunk budget — precisely in the runs that
        # need it.
        if collection_map_block:
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, collection_map_block, {},
                budget_chars=chunk_budget, admission_sink=baseline_admission,
            )
        if structured_block:
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, structured_block, structured_map or {},
                budget_chars=chunk_budget, admission_sink=baseline_admission,
            )
        if chunks:
            # 装配顺序单点在 `chunk_federation.reasoning_order_for`(深度报告撰写的未绑定
            # 段走同一个函数):相关度降序(稳定,同分保留插入序 = 检索序 / 节内文档
            # 序,不按随机 chunk_id 洗牌)→ 按来源两道交错(概念漫游段是 run 内
            # min-max 归一分、播种/检索段是绝对融合分,不可比;精确段锚定在自己的
            # 相关度位置不参与交错)→ 前 reserve 个精确段提前(优先级最高)→ 紧随
            # 其后是当前库保底前缀(挂了参考库时;对等模式为逐库前缀,判据与席位
            # 数同 mix 切分,`retrieval.library_floor_rules`)。relevance 数值不
            # 改写,它还参与接地阈值。
            # 精确前缀席位救的两条风险:①同分 1.0——PPR / 词法通道也会给出 1.0 的
            # 块,稳定排序下按插入序排在精确块之前,一次 PPR 丰收就能把整节命令表挤
            # 出预算;②切点落在精确小节中间,主描述进了 prompt、`Arguments` 参数表
            # 没进。残余是刻意的:一节 12 块时只有前 reserve 块拿到席位,其余靠
            # 1.0 相关度自然靠前(交错锚定精确段正是为了不破坏这一点)——与 chunk
            # 模式 `exact_section_reserve` 的 4/12 同义。structured_block 先于原文
            # 段消费同一份预算,整表够大时曾让原文段拿到 0 字符、席位作废;现在
            # 上游只在照旧渲染会把保底前缀挤出去时,才以「分区 − 原文段下限」为
            # 上限重新渲染结构化侧(`retrieval.passage_floor`,见
            # `_assemble_structured_evidence`)。按节合成注入的未绑定精确块**不**走
            # 这里:它们不在 `chunks` 里,而是经 `trailing_chunks` 单独成段装在本节
            # 全部绑定证据之后。
            # 号段:chunk 段 k1..N、KG 段 k1001+,合并 id_map,两段都可 [k] 引用。
            # 只有**渲染进预算**的段才拿到号,所以撞号的界是「字符预算能装下几行」
            # 而不是证据池大小:池子本身是 PPR ≤ ppr_top_chunks×(1 seed + 3 次动作)、
            # 精确 ≤144、首轮播种 ≤ 首轮子查询数×每查询纳入数 + 词法臂一份、补种与
            # add_subquery 每步至多一份 chunk_mmr_k、search_chunks ≤3 次——最深档
            # 可以上千,但 120k 字符的最大分区按本地语料 p10 段长(约 210 字)也只
            # 装得下约 550 行,< 1000。只有大量极短段的库才可能逼近(已登记)。
            ordered = reasoning_order_for(self.settings, chunks, notebook_id).passages
            chunk_block, chunk_id_map = self._chunk_answer_context(
                ordered, notebook_id=notebook_id, id_offset=key_offset,
                budget_chars=max(0, chunk_budget - len(source_context)),
                lookup_sink=lookup_sink)
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, chunk_block, chunk_id_map,
                budget_chars=chunk_budget, heading=_REASONING_CHUNK_HEADING,
                admission_sink=baseline_admission,
            )

        # Direct source elements share the Chunk/source partition instead of
        # bypassing its advertised hard ceiling. Admit the top `element_items`
        # by retrieval relevance descending (tie-break element_id) rather than
        # whatever order the retriever happened to collect them in — an
        # insertion-order slice silently drops the most relevant elements
        # whenever the retriever collects more than the cap.
        if elements and len(source_context) < chunk_budget:
            ranked_elements = rank_source_elements(elements, element_items)
            element_block, element_id_map = self.evidence_context.element_context(
                ranked_elements, notebook_id=notebook_id,
                id_offset=key_offset + self._ELEMENT_KEY_BASE,
                budget_chars=max(0, chunk_budget - len(source_context)),
            )
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, element_block, element_id_map,
                budget_chars=chunk_budget, heading="Direct source elements",
                admission_sink=baseline_admission,
            )

        # 兜底原文段(未被本节绑定的精确命中)。装在绑定 chunk 段与 element 段
        # **之后**、只吃它们剩下的预算:本节绑定的证据是模型自己的判断,注入的
        # 材料不许跟它竞争,更不许按相关度排到它前面(理由见 docstring 的
        # ``trailing_chunks`` 一段)。不排序——注入序就是 `outline_synthesis` 给
        # 的顺序;不走 `promote_bounded_prefix`——前缀席位是另一条路径的机制,
        # 这一段整体已经在最后了。号段接在绑定 chunk 段之后(见 id_offset),
        # 两段合计 ≤144+reserve ≪ `_MIX_KG_KEY_BASE`。
        if trailing_chunks and len(source_context) < chunk_budget:
            trailing_block, trailing_id_map = self._chunk_answer_context(
                list(trailing_chunks), notebook_id=notebook_id,
                id_offset=key_offset + len(chunks or ()),
                budget_chars=max(0, chunk_budget - len(source_context)),
                lookup_sink=lookup_sink,
            )
            source_context, source_map = self._bounded_context_append(
                source_context, source_map, trailing_block, trailing_id_map,
                budget_chars=chunk_budget, heading="Exact-lookup passages",
                admission_sink=baseline_admission,
            )

        # Reserve the inter-partition separator inside the KG budget so the
        # final evidence block never exceeds kg_context_chars+chunk_context_chars.
        effective_kg_budget = max(0, kg_budget - (2 if source_context else 0))
        kg_context, kg_map = self._answer_context(
            notebook_id, top_hits,
            # `trailing_chunks` 同样占用低号段,所以它单独出现时 KG 也必须让到
            # 1001+:只看 `chunks` 的话,一个没有绑定 chunk、只有注入块的节会让
            # KG 段从 k1 重新开始,两段撞号、锚点误绑。
            id_offset=key_offset + (
                self._MIX_KG_KEY_BASE if (chunks or trailing_chunks) else 0
            ),
            budget_chars=effective_kg_budget,
        )
        if kg_context == "(none)":
            kg_context, kg_map = "", {}
        # Captured before Memory/derived-chain blocks merge into kg_map so
        # "included_kg" reports KG objects/relations only, not those other
        # evidence categories.
        included_kg = len(kg_map)
        if memory_hits and len(kg_context) < effective_kg_budget:
            memory_block, memory_map = self.memory_context(
                memory_hits, id_offset=key_offset + self._MEMORY_KEY_BASE
            )
            kg_context, kg_map = self._bounded_context_append(
                kg_context, kg_map, memory_block, memory_map,
                budget_chars=effective_kg_budget, heading="Confirmed Memory",
                admission_sink=baseline_admission,
            )
        if chains:
            from app.services.kg.follow_chain import render_follow_chain_context
            chain_block, chain_id_map = render_follow_chain_context(
                chains, id_offset=key_offset + 2000,
                active_notebook_id=notebook_id)
            kg_context, kg_map = self._bounded_context_append(
                kg_context, kg_map, chain_block, chain_id_map,
                budget_chars=effective_kg_budget, heading="Derived chains",
                admission_sink=baseline_admission,
            )
        context_block = source_context
        if kg_context:
            context_block = (
                concat(context_block, Glue("\n\n"), kg_context) if context_block else kg_context
            )
        id_map = {**source_map, **kg_map}
        total_context_budget = chunk_budget + kg_budget
        if refine:
            refine_client = self.model_clients.chat("evidence_refine")
            context_block = self._refine_context(
                question, context_block, refine_client, cancel_event,
                budget_chars=total_context_budget,
            )
        context_block = clip(context_block, total_context_budget)
        # 关联报告的复原证据分区:总预算截断与精炼之后、外部证据之前,自带预算
        # (见 docstring 的 ``report`` 一段)。
        context_block, id_map, report_chars, report_counts = (
            self._append_reasoning_report_evidence(
                context_block, id_map, report,
                notebook_id=notebook_id, key_offset=key_offset,
            )
        )
        # 外部证据段拼在最后,自带独立预算——理由见 ``_append_external_context``。
        # ``has_external`` 是 answer_prompt 那条规则唯一的闸:块真的进了上下文
        # 才追加规则句(设计文档 §九 不变量 9 的另一半在 reflect 侧)。
        pre_external_block = context_block
        context_block, id_map, admitted_external, external_counts = (
            self._append_external_context(
                context_block, id_map, external_evidence, key_offset=key_offset,
            )
        )
        has_external = context_block != pre_external_block
        # Partition the merged *source* map (structured preview + chunks +
        # elements) by numeric key range rather than trusting chunk_id_map /
        # element_id_map's sizes from before the append: _bounded_context_append
        # can drop an entire block (remaining budget <= prefix) or filter it
        # down to only the ids whose "k<n>:" line actually survived truncation
        # — counting before that step over-reports what really entered the
        # prompt. Elements live in the isolated k4001+ (_ELEMENT_KEY_BASE)
        # namespace, collection-preview rows in k5001+, workbook results in
        # k6001+ (``spreadsheet_prompt_block``, one key per executed analysis,
        # carried in through ``structured_map``) and sampled document reads in
        # k7001+; everything below the element band is a chunk. The
        # collection-map block is appended with an empty id map and
        # contributes no keys.
        # 分区判定按**去掉本节偏移之后**的号段来做:节偏移是 10000 的倍数,不减
        # 掉的话第 2 节的 chunk(k10001)会被算成"集合清单"(>=5000),整份 synthesis
        # 计数从第二节起全错。
        included_elements = 0
        included_collections = 0
        included_spreadsheets = 0
        included_document_reads = 0
        included_paper_cards = 0
        for key in source_map:
            try:
                key_num = int(key[1:]) - key_offset
            except (TypeError, ValueError):
                continue
            # 7000+ 在 5000+ 之前判:按篇原文取样的号段在集合清单之上,反过来写
            # 的话每一段原文摘录都会被算成一行清单。6000 段在这里只有表格结果
            # (经 structured_map 进来),单独计数,不再算成集合清单行;外部证据
            # 也在 6000 段,但它是在 source_map/kg_map 合并之后才拼进 id_map 的。
            # 9000+(论文卡片摘要里的键)最先判,同一条理由。
            if key_num >= self._PAPER_CARD_KEY_BASE:
                included_paper_cards += 1
            elif key_num >= self._DOCUMENT_READ_KEY_BASE:
                included_document_reads += 1
            elif key_num >= self._EXTERNAL_KEY_BASE:
                included_spreadsheets += 1
            elif key_num >= self._COLLECTION_KEY_BASE:
                included_collections += 1
            elif key_num >= self._ELEMENT_KEY_BASE:
                included_elements += 1
        included_chunks = (len(source_map) - included_elements
                           - included_collections - included_spreadsheets
                           - included_document_reads - included_paper_cards)
        counts = {
            "included_kg": included_kg,
            "included_chunks": included_chunks,
            "included_elements": included_elements,
            "included_collections": included_collections,
            # 按篇取样真正进 prompt 的原文元素数。**稀疏**,与下面的外部证据同
            # 一条口径:没有按篇取样的一轮这里什么都不加,counts 与接入这个特性
            # 之前逐键相等(恒零地写进去会改掉每一条既有答案的持久化 payload)。
            **({"included_document_reads": included_document_reads}
               if included_document_reads else {}),
            # 论文卡片摘要真正进 prompt 的键数,同样**稀疏**。登记进 id_map 但
            # 不进 prompt 的那些键(表格用)不算在这里。
            **({"included_paper_cards": included_paper_cards}
               if included_paper_cards else {}),
            # 进 prompt 的表格分析结果数,同样**稀疏**:没有表格结果的一轮
            # counts 逐键不变。
            **({"included_spreadsheets": included_spreadsheets}
               if included_spreadsheets else {}),
            # 外部证据的装入/丢弃披露,**稀疏**:没有外部证据的一轮这里什么都不
            # 加,counts 与接入这个特性之前逐键相等(理由见
            # ``_append_external_context``)。
            **external_counts,
            # 关联报告复原证据分区自己进 prompt 的条目数,同样**稀疏**:没挂报告
            # (或本轮不读报告)时不出现。
            **report_counts,
        }
        baseline = {
            "context_block": context_block,
            "id_map": dict(id_map),
            "ordered_handles": tuple(id_map),
            # 上界含外部段:``budget_chars`` 的合同是「这段上下文不会超过它」
            # (``test_answer_quality_contract`` 直接断言
            # ``len(context_block) <= budget_chars``),而外部段是在总预算
            # 截断之后另加的一份独立预算。记成总预算本身会让这条不变量在
            # 有外部证据的一轮上假红;记成「总预算 + 本次外部段实际占用」
            # 才是真实上界——而且是**实际**占用而不是名义上限
            # ``EXTERNAL_EVIDENCE_CONTEXT_CHARS``,否则零外部证据的一轮会
            # 报出一个从来没打算花的额度。
            # 关联报告的复原证据分区同理:另加的独立预算,按实际占用计入。
            "budget_chars": (
                total_context_budget + report_chars
                + len(context_block) - len(pre_external_block)
            ),
            "capture_error_count": int(
                baseline_admission.get("ambiguous_truncations") or 0
            ),
            # Not part of the manifest (``build_retrieval_baseline_manifest``
            # reads the keys above by name): the reads the passage-card leg of
            # the reasoning draft (``_draft_citation_cards``) reuses
            # (``_reasoning_chunk_cards``).
            "citation_lookups": lookup_sink,
        }
        return _ReasoningContext(
            context_block=context_block,
            id_map=id_map,
            counts=counts,
            baseline=baseline,
            has_external=has_external,
            admitted_external=set(admitted_external),
        )

    def _answer_reasoning(
        self,
        notebook_id,
        question,
        top_hits,
        elements,
        history="",
        cancel_event: CancelEvent = None,
        chunks=None,
        chains=None,
        memory_hits=None,
        answer_client=None,
        kg_context_chars: int | None = None,
        chunk_context_chars: int | None = None,
        element_items: int = 6,
        structured_block: str = "",
        structured_map: dict | None = None,
        collection_map_block: str = "",
        counts_sink: dict | None = None,
        baseline_sink: dict | None = None,
        sectioned: bool = False,
        key_offset: int = 0,
        section_title: str = "",
        section_index: int = 0,
        section_total: int = 0,
        style_block: str = "",
        external_evidence=(),
        external_sink: set | None = None,
        *,
        trailing_chunks=(),
        citation_lookups: "CitationLookups | None" = None,
        report: "ReportTurn | None" = None,
        paper: "PaperCardEvidence | None" = None,
    ):
        """Synthesise the reasoning-mode answer. When PPR chunks are present they
        become first-class [k]-citable evidence: chunk segment k1..N + KG reasoning
        chain segment k1001+ (mirrors _answer_mix's keying), with final synthesis
        still handled by ``ask_answer``. Otherwise KG-only (legacy).
        Direct ``SourceElement`` passages use the isolated k4001+ namespace and
        are first-class citations rather than unbound prompt decoration; at most
        ``element_items`` are admitted, chosen by retrieval relevance descending
        (tie-break ``element_id``) rather than insertion order.
        ``collection_map_block`` is the run's deterministic collection counts,
        placed at the head of the source partition and carrying no ``[k]`` id
        (see ``collection_enumeration_answer.collection_map_block``). Context refinement
        uses ``evidence_refine`` while final synthesis uses ``ask_answer``. Returns
        (answer, llm_grounded, anchors, counts) where ``counts`` reports how many
        KG/chunk/element entries actually entered the prompt (``included_kg``/
        ``included_chunks``/``included_elements``).

        按节合成(设计文档 §3.1)复用这同一个装配器。``sectioned`` 是**唯一**的模式
        判定(不看标题真值,见 ``prompts._answer_section_directive``):它开着时
        ``key_offset`` 把本节的每一段 ``[k]`` 号整体平移(见
        ``outline_synthesis.OUTLINE_SECTION_KEY_STRIDE``),prompt 多一段节级指令,
        证据精炼跳过。缺省为「不在按节合成里」,单次合成路径逐字节不变。节模式下
        调用方只传该节绑定的证据,Memory / 推导链 / 集合地图 / 结构化预览一律不传
        ——它们不可被大纲绑定,留在回退路径上(v1 刻意的边界)。``style_block``:
        见 ``_answer_chunks`` 同名参数,单次合成与按节合成共用调用方传入的同一份。

        ``external_evidence``: 插件 reflect 动作带回的库外材料
        (``domain.reflect_action.ExternalEvidence``,设计文档
        ``2026-09-13-reflect-plugin-action-design_zh.md`` §6.2)。缺省空元组,
        此时上下文块、id_map 与 prompt 与接入这个特性之前逐字节相等。与 Memory /
        推导链**不**同的一条边界:按节合成每一节都装同一份外部块(见
        ``_answer_reasoning_sections``)——外部材料不属于任何一节的大纲绑定,
        但它恰恰是本轮唯一库里查不到的东西,只喂给某一节等于让别的节在同一篇
        答案里对着一个自己看不见的 ``[k]`` 号写作。号段由 ``key_offset``
        隔开,各节的外部键互不相交。

        ``external_sink``:调用方传入的集合,本次**真的装进了 prompt** 的那批
        ``ExternalEvidence.key`` 会被 update 进去。引用侧据此过滤——被预算丢掉
        的条目模型从没见过,不给它发引用(设计文档 §6.3)。

        ``trailing_chunks``:**不属于本节绑定证据**的兜底原文段(按节合成里由
        ``outline_synthesis`` 注入的未绑定精确命中,见
        ``OutlineSectionSlice.exact_chunks``)。它们单独成一段
        「Exact-lookup passages」,装在本节绑定的原文段与元素**之后**,只用这两
        段吃完后剩余的那点预算;不排序(保持注入序)、不走前缀席位。缺省空元组,
        此时上下文块、id_map 与 counts 逐字节等于接这个参数之前。**为什么必须
        排在后面**:``chunks`` 段按相关度排序,而精确命中的相关度常为 1.0、本节
        绑定块可能更低——同一段里混装等于让模型没绑进本节的原文按构造吃掉整份
        预算,绑到 source element 的节也一样(chunk 段先于 element 段装配)。

        ``citation_lookups``:本 run 共享的读取备忘(``CitationLookups``)。按节合成
        的每一节、``_answer_with_retry`` 的重试与回退的单次合成都传同一份,所以
        同一个键在一个 run 里至多读一次;缺省则本次装配新建一份。

        ``report``:本轮读入的关联报告(``_reasoning_report_turn``)——复原证据分区
        进上下文(见 ``_assemble_reasoning_context``)、报告块进
        ``answer_prompt(report_block=...)``;只出现在单次合成里(D8:读入报告的
        轮次不按节合成)。None = 接入前逐字行为。

        ``paper``:本 run 论文工具的证据(``paper_card_answer.PaperCardEvidence``)。
        摘要块已经由调用方经 ``structured_block`` 送进 prompt;这里做三件事:登记
        的全部键在模型正文**之后**并入 ``id_map``(不占结构化侧预算,与外部证据
        同理),``paper_table`` 版式时 prompt 加「表格由系统生成」规则、正文拼成
        「覆盖说明 + 模型综述 + 表格」(节模式不拼:表格由
        ``_answer_reasoning_sections`` 在第一节之前放一次),最后才解析锚点。
        None = 接入前逐字行为。

        """
        context = self._assemble_reasoning_context(
            notebook_id, question, top_hits, elements,
            cancel_event=cancel_event, chunks=chunks, chains=chains,
            memory_hits=memory_hits, kg_context_chars=kg_context_chars,
            chunk_context_chars=chunk_context_chars, element_items=element_items,
            structured_block=structured_block, structured_map=structured_map,
            collection_map_block=collection_map_block, key_offset=key_offset,
            external_evidence=external_evidence, trailing_chunks=trailing_chunks,
            # 按节合成不做证据精炼。两条理由都硬:①精炼是**每次装配一次**模型调用,
            # 节模式下会把 k 次合成变成 2k 次调用——成本合同只承诺了合成那一半;
            # ②精炼是给「上下文太大、要先挑重点」准备的,而一节的切片至多 8 条绑定
            # 证据,本来就没有中段可丢。
            refine=not sectioned,
            # 本 run 共享的读取备忘:别的节 / 重试 / 回退的装配不重读已读过的键,
            # 调用方(草稿阶段的 ``_draft_citation_cards``)随后给同一批原文段发引用卡时
            # 也复用它(``_reasoning_chunk_cards``)。没传就是本次装配自己一份。
            citation_lookups=(
                citation_lookups if citation_lookups is not None
                else CitationLookups()
            ),
            report=report,
        )
        context_block, id_map, has_external = (
            context.context_block, context.id_map, context.has_external)
        answer_client = answer_client or self.model_clients.chat("ask_answer")
        if external_sink is not None:
            external_sink.update(context.admitted_external)
        # Fill the sink BEFORE the model call: when the answer client raises
        # or returns malformed JSON, the synthesis trace must still report the
        # evidence that really was assembled and sent, not zeros
        # (codex PR#391 round-2).
        if counts_sink is not None:
            counts_sink.clear()
            counts_sink.update(context.counts)
        if baseline_sink is not None:
            baseline_sink.clear()
            baseline_sink.update(context.baseline)
        raw = answer_client.chat_json(
            [{"role": "user", "content": answer_prompt(
                question, context_block, history,
                sectioned=sectioned,
                section_title=section_title,
                section_index=section_index,
                section_total=section_total,
                style_block=style_block,
                external_rules=has_external,
                peer_notebooks=subjectless_run_active(),
                report_block=report.enter_prompt() if report is not None else "",
                paper_table=paper is not None and paper.paper_table,
            )}],
            ANSWER_SCHEMA_HINT,
            timeout=self.settings.reasoning_timeout_seconds,
            max_retries=self.settings.reasoning_max_retries,
            cancel_event=cancel_event,
            **cap_kwargs(answer_client, "answer_max_tokens"),
        )
        raise_if_cancelled(cancel_event)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = _answer_text(data)
        llm_grounded = data.get("grounded", False) is True
        # 节模式:先按本节号段清洗正文,再解析锚点。
        #
        # 顺序是有讲究的。**清洗必须在解析之前**,因为 `parse_anchors` 对混合组
        # (`[k10001, k1]`)是整组失效的 —— 先解析的话,那一组里合法的 k10001 也拿不到
        # 锚点,而清洗后正文却写着 `[k10001]`,变成一个永远绑不上的标记。先洗再解,
        # 两边说的是同一件事。
        #
        # **清洗本身是必需的**(codex PR#407 R3 P1):按节解析只决定谁能拿到锚点,
        # 而前端是拿正文里的标记去查**合并后**的引用表 —— 不清洗,第二节那个 `[k1]`
        # 会绑到第一节的无关证据上,按节解析防住的误绑从渲染这道后门回来。
        if sectioned:
            answer = _keep_only_section_markers(answer, set(id_map))
        # 锚点按**本节自己的** id_map 解析(节模式下 id_map 只含本节证据)。合并
        # 之后再统一解析拼好的全文是错的:一节写出别节号段的 `[k]`(它根本没见过
        # 那个号)只可能是幻觉,合并解析会把它一本正经地绑到别节的证据上,而按节
        # 解析直接丢弃 —— 这正是号段偏移要买到的东西。合法标记两种口径结果相同
        # (号段互不相交),差别只在幻觉这一种情况上。
        anchors = self._parse_answer_anchors(answer, id_map)
        if not sectioned:  # 论文表:模型正文按 prompt 号段、表格按登记表各解析一次
            answer, anchors = self._with_paper_table(answer, anchors, paper, set(id_map))
        return answer, llm_grounded, anchors, context.counts

    def _answer_reasoning_sections(
        self,
        notebook_id: str,
        question: str,
        slices,
        *,
        history: str,
        limits,
        answer_client,
        cancel_event: CancelEvent = None,
        on_section=None,
        style_block: str = "",
        external_evidence=(),
        external_sink: set | None = None,
        citation_lookups: "CitationLookups | None" = None,
        paper: "PaperCardEvidence | None" = None,
    ):
        """按节合成(设计文档 §3.1):每节一次合成调用,只喂该节绑定的证据。

        ``paper``(论文工具的证据,规格 2026-10-28 §7.3):卡片证据只给**第 0 节**
        (偏移 0,登记键 k9001+ 落在它自己的号段里)——摘要块经 ``structured_block``
        送进它的 prompt、跨论文对比只由它写;第 1 节起不给摘要(那些键不属于后面
        各节的号段,``_keep_only_section_markers`` 也会删掉)。表格只出现一次,放在
        第一节之前,它的标记按登记表解析成锚点。

        返回 ``_SectionedSynthesis``;**任何一节失败就返回 None**,由调用方整体
        回退到单次合成。绝不半篇拼接:少写一节的答案与「这一节没有内容」在屏幕上
        长得一模一样,而实际发生的是一次模型故障 —— 宁可多付一次单次合成,也不
        交付一份看不出缺口的残篇。

        每节沿用 ``_answer_with_retry`` 的重试语义(空 content 重掷一次、两次皆空
        才记 model_error),所以「思考型模型偶发空 content」不会把整轮拖进回退。
        取消异常照常穿透 —— 用户按了中断不是合成失败。

        ``on_section(step)`` 在每节写完后立刻收到一条轻量进度步(不带 ``duration_ms``,
        理由见发送点)。分节合成是整轮里最长的一段(k 次模型调用),不发进度的话实时
        轨迹会在「合成」上静止几分钟。**进度步实时发出、不因后续回退而回收**:那一节
        确实写完了、那笔钱确实付了,事后抹掉等于让轨迹少报整轮做过的工作;回退由收尾
        那条 synthesis 步的 ``outline_fallback`` 说清楚。

        ``style_block``:见 ``_answer_chunks`` 同名参数,原样转发给每一节的
        ``_answer_reasoning`` 调用——同一份风格提示对全篇一致,不按节重新点读。

        ``external_evidence``:**每一节都装**同一份外部块(与只属于回退路径的
        Memory / 推导链 / 集合地图刻意相反)。理由:外部材料不由大纲绑定,却是
        本轮唯一「库里查不到」的东西;只给某一节,别的节在同一篇答案里就会对着
        一个自己没见过的 ``[k]`` 号写作,而合并后的引用表却认得它——正是号段
        隔离要防的那种误绑。各节的外部键落在自己 ``key_offset`` 的号段里
        (``_EXTERNAL_KEY_BASE`` + 至多 ``EXTERNAL_EVIDENCE_MAX_PER_RUN`` 条
        ≪ ``OUTLINE_SECTION_KEY_STRIDE``),互不相交。``external_sink`` 收各节
        装入键的**并集**——同一条材料进了三节仍然只发一条引用。

        ``OutlineSectionSlice.exact_chunks``(未被任何节绑定的精确命中)同样是
        「每一节都装同一份」,但它经 ``trailing_chunks`` 单独成段,装在本节绑定的
        原文段与元素**之后**、只吃剩余预算:它不是本节的证据,而是兜底材料,按
        相关度与绑定块同段竞争的话,精确命中那条常为 1.0 的相关度会直接吃掉整份
        chunk 预算,把模型亲自绑上的证据挤出 prompt。

        ``citation_lookups``:本 run 共享的读取备忘,原样交给每一节的装配(各节
        顺序执行、重试同步),所以每节都装的精确块与几节共用的段落只读一次;调用方
        把同一份交给回退的单次合成。缺省则本函数内新建一份,各节仍共享。

        """
        from app.services.outline_synthesis import outline_answer_text

        if citation_lookups is None:
            citation_lookups = CitationLookups()
        rendered: list = []
        merged_anchors: list = []
        seen_anchor_keys: set = set()
        merged_counts = {
            "included_kg": 0, "included_chunks": 0,
            "included_elements": 0, "included_collections": 0,
        }
        # 外部装入是**并集**,不是逐节求和:同一条材料进了三节还是一条材料,
        # 求和会在轨迹上报出「装入 6 条」而 run 里总共只有 2 条。丢弃数由并集
        # 反算,同一条口径(见循环之后)。
        admitted_external: set = set()
        grounded = False
        section_grounding_detail: list = []
        baseline_assemblies: list[dict] = []
        total = len(slices)
        model_label = getattr(answer_client, "model", "")
        for item in slices:
            section_counts: dict = {}
            section_baseline: dict = {}
            section_paper = paper if item.index == 0 else None
            digest, digest_map = paper_digest_block(
                section_paper, limits.chunk_context_chars // 2)

            def _synth_section(
                item=item,
                section_counts=section_counts,
                section_baseline=section_baseline,
                section_paper=section_paper, digest=digest, digest_map=digest_map,
            ):
                text_, grounded_, anchors_, _counts = self._answer_reasoning(
                    notebook_id, question, item.hits, item.elements, history,
                    cancel_event=cancel_event, chunks=item.chunks,
                    answer_client=answer_client,
                    kg_context_chars=limits.kg_context_chars,
                    chunk_context_chars=limits.chunk_context_chars,
                    element_items=limits.answer_element_items,
                    counts_sink=section_counts,
                    baseline_sink=section_baseline,
                    sectioned=True,
                    key_offset=item.key_offset,
                    section_title=item.section.title,
                    section_index=item.index + 1,
                    section_total=total,
                    style_block=style_block,
                    external_evidence=external_evidence,
                    external_sink=admitted_external,
                    trailing_chunks=item.exact_chunks,
                    citation_lookups=citation_lookups,
                    structured_block=digest, structured_map=digest_map,
                    paper=section_paper,
                )
                return text_, grounded_, anchors_

            text, section_llm_grounded, section_anchors, ok = self._answer_with_retry(
                _synth_section, model_label, service="ask_answer",
            )
            if not ok:
                return None
            baseline_assemblies.append(dict(section_baseline))
            # 分节判定必须在本节自己的证据池与锚点上完成。只留模型自报会绕过
            # classify_evidence 这道真实门;拿合并后的锚点回算又会把另一节的高分
            # 证据借给本节。这个逐节结果驱动全局确定性降级。
            # 注入的精确块(``exact_chunks``)也要进这个池子:它们真的进了本节的
            # prompt、真的拿到了本节号段里的 ``[k]``,漏掉的话模型引用它们时
            # ``classify_evidence`` 会判成「引了不存在的对象」而把这一节判成
            # ungrounded,再由节级封顶把整篇答案压到 overview。
            from types import SimpleNamespace
            section_evidence = list(item.hits) + list(item.chunks) + [
                SimpleNamespace(
                    object_id=element.element_id,
                    relevance=float(element.score or 0.0),
                )
                for element in item.elements
            ] + list(item.exact_chunks)
            # 外部证据走的是**非排序**那道门(`exact_evidence_keys`),库内命中
            # 走 `evidence_pool`——两道门缺一道,一个只引站外材料的节就会被判成
            # 「引了不存在的东西」而 ungrounded,再由下面的节级封顶把整篇答案压到
            # overview(codex PR#714 R1 P2)。键从**本节自己**的锚点上读,与
            # `section_evidence` 同口径:另一节引到的外部条目不借给本节。
            section_level, _ = classify_evidence(
                section_evidence,
                section_anchors,
                section_llm_grounded,
                self.settings.evidence_tau_low,
                self.settings.evidence_tau_high,
                exact_evidence_keys=(self._external_exact_keys(section_anchors)
                                     | paper_registered_keys(section_paper)),
            )
            section_is_grounded = section_level == "grounded"
            section_grounding_detail.append({
                "id": item.section.id,
                "title": item.section.title[:60],
                "grounded": section_is_grounded,
                "evidence_level": section_level,
            })
            if on_section is not None:
                # 复用 synthesis 这个 step_type:前端 reasoning-trace.ts 的
                # synthesis 分支只在 `detail.anchors` 是数字时给出细节文案,进度步
                # 不带 anchors,于是它落到「第 i/共 N 节」那条进度分支上。
                #
                # **刻意不带 duration_ms**(codex PR#407 R1 P2):前端的轨迹总耗时
                # 是**所有**步的 duration_ms 求和,而末尾那条总 synthesis 步已经
                # 独家记了从 `synthesis_started` 起的整段合成时间——进度步再各记
                # 一份,分节答案的合成耗时就报成约两倍(回退 run 还会把已完成节的
                # 那几次尝试也算进去)。进度步是**进度标记**,不是独立的耗时区间;
                # 「合成耗时必须进轨迹总耗时」那条红线由总步承担,恰好一次。
                on_section(TraceStep(
                    step_type="synthesis",
                    # 措辞与前端 reasoning-trace.ts 的进度细节分支同款
                    # (「第 i/共 N 节」),summary 与 detail 读起来才是一句话。
                    summary=f"已写完第 {item.index + 1}/共 {total} 节",
                    detail={
                        "section_index": item.index + 1,
                        "section_total": total,
                        "section_title": item.section.title[:60],
                    },
                ))
            for key in merged_counts:
                merged_counts[key] += int(section_counts.get(key, 0) or 0)
            # 外部计数不参与上面那圈求和(``merged_counts`` 的键里没有它们);
            # 见循环之后的并集口径。
            rendered.append((item, text))
            grounded = grounded or section_llm_grounded
            # 号段互不相交,所以跨节不可能撞 key;去重只防同一节内重复标记(
            # parse_anchors 自己已经去过一次,这一层是拼接侧的常数级防御)。
            for anchor in section_anchors:
                if anchor.key in seen_anchor_keys:
                    continue
                seen_anchor_keys.add(anchor.key)
                merged_anchors.append(anchor)
        if external_sink is not None:
            external_sink.update(admitted_external)
        if external_evidence:  # 稀疏,同单次合成路径
            merged_counts["external_included"] = len(admitted_external)
            merged_counts["external_dropped"] = max(
                0, len(external_evidence) - len(admitted_external)
            )
        answer_text, merged_anchors = self._with_sectioned_paper_table(
            outline_answer_text(rendered), merged_anchors, paper)
        return _SectionedSynthesis(
            answer=answer_text,
            # 这里仅汇总模型自报供全局 classify_evidence 使用;真正的逐节判定已经
            # 独立落在 section_grounding,后续整体档位再按其全/部分/零支撑确定性封顶。
            llm_grounded=grounded,
            anchors=merged_anchors,
            sections=len(rendered),
            counts=merged_counts,
            section_grounding=section_grounding_detail,
            baseline_assemblies=baseline_assemblies,
        )

    def _paper_table_anchors(self, text: str, paper) -> list:
        """The anchors of the system-written table text, bound against the
        registration ONLY: the table cites registered keys and nothing else."""
        return self._parse_answer_anchors(text, with_paper_registration({}, paper))

    def _with_paper_table(self, answer: str, anchors: list, paper, prompt_keys: set) -> tuple:
        """A single synthesis's answer laid out as the paper table: the model's
        text keeps only markers of keys its prompt carried (a ``k9xxx`` it made
        up would otherwise render bound to the table's anchor of that key), then
        coverage line + text + table; the table's anchors follow the text's.
        No table, or an empty model answer (the retry reads it): unchanged."""
        if paper is None or not paper.paper_table or not answer:
            return answer, anchors
        answer = compose_paper_answer(_keep_only_section_markers(answer, prompt_keys), paper)
        keys = {anchor.key for anchor in anchors}
        return answer, anchors + [
            a for a in self._paper_table_anchors(sectioned_paper_prefix(paper), paper)
            if a.key not in keys]

    def _with_sectioned_paper_table(self, answer: str, anchors: list, paper) -> tuple:
        """The sectioned answer led by the paper table (once, before the first
        section) and its anchors -- the table's markers bound against the
        registration, first, then the sections' (a key both carry kept once).
        Also the synthesis-failure fallback (``answer`` = the honest-degradation
        text): the table is deterministic and its citations registered, so it
        still follows.  Without a table: ``(answer, anchors)`` unchanged."""
        prefix = sectioned_paper_prefix(paper)
        if not prefix:
            return answer, anchors
        table_anchors = self._paper_table_anchors(prefix, paper)
        keys = {anchor.key for anchor in table_anchors}
        return (f"{prefix}\n\n{answer}" if answer else prefix,
                table_anchors + [a for a in anchors if a.key not in keys])

    def _paper_card_evidence(self, stage, *, for_answer: bool = True) -> "PaperCardEvidence | None":
        """The run's paper-tool evidence for synthesis (``paper_card_answer``):
        every card / extracted-value element registered under k9001+ through
        ``collection_item_citations`` (one bounded hydration, participant and
        ceiling checks, read attestation) and, for ``paper_table``, the table.
        ``None`` without paper tools -- and when building it fails: then no key,
        no digest and no table exist, never a table whose markers bind to
        nothing (logged by class name, the answer proceeds as prose).
        ``for_answer=False`` (retrieval-only output): digest keys only, no table."""
        tools = getattr(stage, "paper_tools", None)
        if tools is None:
            return None
        from app.services.paper_card_answer import build_paper_card_evidence

        notebook_id = stage.prepared.notebook_id
        try:
            return build_paper_card_evidence(
                tools,
                read_citations=lambda items: self.evidence_context.collection_item_citations(
                    items, active_notebook_id=notebook_id),
                tier_of=self.evidence_context.tier_map,
                table_rows=stage.prepared.limits.paper_table_rows,
                for_answer=for_answer,
                read_display=self.paper_display_reader,
            )
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - the answer survives as prose
            self.logger.warning(
                "paper card evidence failed (%s)", type(exc).__name__)
            return None

    def _add_paper_digest(self, structured_block: str, structured_map: dict, paper,
                          answer_client, limits, knowhow_block_len: int, room,
                          read_reserve: int) -> str:
        """Append the paper digest after the enumeration preview and before the
        document reads (spec §7.2 order), within the structured side's caps --
        the same shared / partition caps ``_assemble_document_read_block``
        applies, and ``room`` -- less what the document-read block reserved.
        Whole papers only (``paper_digest_block``); nothing that fits, no block."""
        if paper is None or not answer_client.configured:
            return structured_block
        from app.services.collection_enumeration_answer import (
            COLLECTION_MAP_BLOCK_MAX_CHARS,
        )
        chunk_chars = int(limits.chunk_context_chars)
        limit = min(int(knowhow_block_len) + (2 if knowhow_block_len else 0) + chunk_chars // 2,
                    chunk_chars - COLLECTION_MAP_BLOCK_MAX_CHARS - 4)
        if room is not None:
            limit = min(limit, int(room))
        joiner = 2 if structured_block else 0
        digest, digest_map = paper_digest_block(
            paper, limit - len(structured_block) - joiner - int(read_reserve))
        if not digest:
            return structured_block
        structured_map.update(digest_map)
        return concat(structured_block, Glue("\n\n"), digest) if structured_block else digest

    def _assemble_document_read_block(
        self,
        document_reads,
        structured_block: str,
        structured_map: dict,
        collection_item_citations: dict,
        answer_client,
        chunk_context_chars: int,
        knowhow_block_len: int = 0,
        *,
        room: int | None = None,
    ) -> tuple[str, bool]:
        """把本轮按篇读到的有界原文摘录缝进合成证据块(PR-A T5)。

        ``room``:原文段下限留给整个结构化侧的字符总量(见
        ``_assemble_structured_evidence``);``None`` = 没有保底段落,逐字节同旧。

        ``knowhow_block_len`` 是花名册拼上来**之前** ``structured_block`` 的长度
        (knowhow 整表块)。整块不装的判据与枚举侧的子预算同口径:knowhow 块本身
        不受半预算约束(既有设计,PR-B 登记的风险 a),受约束的是「花名册 + 取样」
        这一对——所以上限是 ``knowhow_block_len + 拼接符 + chunk_context_chars // 2``,
        不是裸的一半(codex #724 R4:按裸的一半判,4000 字 knowhow + 13000 字
        花名册 + 2000 字取样就会把预留过席位的取样块照样整块挡下)。

        装配位在**枚举预览之后**:先是「库里有这几篇」,紧接着才是「其中这几篇
        我真的翻开读了几段」,读者(模型)看到的顺序与它自己的动作顺序一致。整体
        仍然只经 ``structured_block`` 这一个参数进 ``_answer_reasoning``。

        返回 ``(structured_block, dropped)``。``dropped`` 只在**子预算**把这一块
        整体挡下时为真(见下),渲染失败与空产物都不是它——那两种情况下本来就没有
        块可装,而 ``dropped`` 要回答的是「装得下的块被预算挤掉了吗」。

        ## 子预算(与 ``enumeration_sub_budget`` 的第 3 层同形、同理由)

        取样块与枚举预览同住 ``structured_block``,而这个参数整体与 chunks /
        elements 争同一份 ``chunk_context_chars``。枚举那一侧已经把自己夹在了
        一半以内;取样块**拼在它后面**,不夹的话两块加起来可以把另一半问题的证据
        预算整个挤空——检索到的原文段落一条都进不了 prompt,而这个动作恰恰常与
        `search_chunks` 同轮发生。

        夹法是**整块不装**而不是截断:取样块的内部结构(每篇一个包头 + 它自己的
        coverage 披露行 + 带 `[kN]` 的正文)在字符级截断下会碎成「有包头没正文」
        或「有正文但披露行被切掉」——后者正是「模型以为自己读全了」的那条路。
        执行体侧的份额切分已经把常态挡在预算之内,这里是**配置错误的兜底**,与
        ``enumeration_block_dropped`` 一样只在轨迹上披露一次。

        ## 全成功再写回

        三件事同时发生(块拼进 prompt、反向绑定进 ``structured_map`` 让 ``[kN]``
        解析得回 anchor、``Citation`` 进 ``collection_item_citations`` 让 anchor
        出得了引用卡)。它们必须**同生共死**:留下「块进去了但引用解析不了」这种
        半状态,会让模型引的号在响应侧变成悬空引用。所以三份产物先各自算好,
        两次 ``update`` 放在**渲染全部成功之后**一起做——把 update 夹在渲染中间
        的话,后一步抛异常就正好留下前一半,而 ``except`` 分支已经没法把它撤回了
        (两个字典都是调用方的,不是本函数造的)。

        ``collection_item_citations`` 的键语义是「这一行的查找身份」
        (``collection_item_citations.get(anchor.object_id)``)。取样产物的 id_map
        条目里 ``object_id == element_id == element.id``,所以这里按
        ``citation.element_id`` 入键,与 anchor 对得上。

        空产物(见证失败)在 ``document_read_answer`` 侧就被滤掉:它只属于回喂
        账目。``answer_client`` 未配置时整段不做——那一轮压根不会有合成 prompt。
        """
        if not document_reads or not answer_client.configured:
            return structured_block, False
        try:
            from app.services.document_read_answer import (
                document_read_prompt_block,
            )
            preview = document_read_prompt_block(
                document_reads, roster_map=structured_map)
            if not preview.text:
                return structured_block, False
            combined = (concat(structured_block, Glue("\n\n"), preview.text)
                        if structured_block else preview.text)
            from app.services.collection_enumeration_answer import (
                COLLECTION_MAP_BLOCK_MAX_CHARS,
            )
            shared_cap = (int(knowhow_block_len)
                          + (2 if knowhow_block_len else 0)
                          + int(chunk_context_chars) // 2)
            # 再夹一层**分区总容量**(codex #724 R6):knowhow 块本身不受半预算约束,
            # 它接近填满 chunk_context_chars 时共享上限会远大于分区,取样块虽过了
            # 上限却装不进 `_answer_reasoning` 的分区——被 `_bounded_context_append`
            # 按字符截掉、披露行却留着,还报 dropped=False。集合地图块与分隔符先于
            # structured_block 占用同一分区,按它的硬上界预留。整块要么装进、要么
            # 显式不装,不允许静默截断。
            partition_cap = (int(chunk_context_chars)
                             - COLLECTION_MAP_BLOCK_MAX_CHARS - 4)
            # `room`(原文段下限留给整个结构化侧的总量,已扣实际地图块)只是
            # 额外一道上限:两条历史上限照旧按整份分区算,不因下限而缩小。
            limit = min(shared_cap, partition_cap)
            if len(combined) > limit:
                return structured_block, True
            if room is not None and len(combined) > int(room):
                # 只因原文段下限才放不下:整块不装,但给模型留一句固定说明
                # (放得下时才写),免得它以为本轮没读过文档。
                noted = (concat(structured_block, Glue("\n\n"), DOCUMENT_READ_OMITTED_NOTICE)
                         if structured_block else DOCUMENT_READ_OMITTED_NOTICE)
                return (noted if len(noted) <= int(room) else structured_block), True
            citation_updates = {
                citation.element_id: citation for citation in preview.citations}
            structured_map.update(preview.evidence_by_id)
            collection_item_citations.update(citation_updates)
            return combined, False
        except Exception as exc:  # noqa: BLE001 - cards survive prompt degradation
            self.logger.warning(
                "document read prompt rendering failed (%s)", type(exc).__name__
            )
            return structured_block, False

    def _add_sheet_prompt(
        self,
        structured_block: str,
        structured_map: dict,
        spreadsheet_results: list,
        answer_client,
        *,
        max_chars: int | None = None,
    ) -> str:
        """Append a bounded workbook preview without endangering result cards.

        ``max_chars``: the characters the passage floor leaves this block in
        the reasoning source partition (``_assemble_structured_evidence``);
        whole lines that do not fit are left out rather than cut afterwards.
        ``None`` keeps only the byte cap -- the historical behaviour.  When
        that space cannot hold even the first line, the model still learns
        that table results exist: ``SPREADSHEET_OMITTED_NOTICE`` is written
        instead, if it fits (its length is inside ``max_chars``).
        """
        if not spreadsheet_results or not answer_client.configured:
            return structured_block
        try:
            from app.services.spreadsheet_analysis import spreadsheet_prompt_block

            spreadsheet_block, spreadsheet_map = spreadsheet_prompt_block(
                spreadsheet_results,
                preview_rows=self.settings.spreadsheet_analysis_prompt_rows,
                max_bytes=self.settings.spreadsheet_analysis_prompt_bytes,
                max_chars=max_chars,
            )
            if not spreadsheet_block and max_chars is not None and len(
                    SPREADSHEET_OMITTED_NOTICE) <= max_chars:
                spreadsheet_block, spreadsheet_map = SPREADSHEET_OMITTED_NOTICE, {}
            if spreadsheet_block:
                structured_map.update(spreadsheet_map)
                return (
                    concat(structured_block, Glue("\n\n"), spreadsheet_block)
                    if structured_block else spreadsheet_block
                )
        except Exception as exc:  # noqa: BLE001 - cards survive prompt degradation
            self.logger.warning(
                "spreadsheet prompt rendering failed (%s)", type(exc).__name__
            )
        return structured_block

    @staticmethod
    def _append_spreadsheet_citations(citations: list, results: list) -> None:
        """Attach at most one stable row locator per workbook result.

        The identity includes the sheet range: a multi-table plan may analyse
        two sheets of one workbook, and two source-level receipts (no row
        element) of the same workbook are still two different tables.
        """
        seen: set[tuple[str, str, str]] = set()
        for result_set in results:
            citation = next(
                (row.citation for row in result_set.rows if row.citation), None
            )
            if citation is None:
                continue
            identity = (
                citation.source_id, citation.element_id, citation.location_label,
            )
            if identity in seen:
                continue
            seen.add(identity)
            citations.append(citation)

    @staticmethod
    def _reasoning_result_sets(structured_batch, collections: list, sheets: list) -> list:
        """Keep the response union ordering explicit outside the draft monolith."""
        return (
            (structured_batch.result_sets if structured_batch else [])
            + collections
            + sheets
        )

    def _assemble_structured_evidence(
        self, stage, answer_client, collection_map_prompt_block: str,
        paper: "PaperCardEvidence | None" = None,
    ) -> tuple:
        """The structured side of the reasoning source partition, kept out of
        the reserved passages' way.

        Five blocks share ``chunk_context_chars`` with the passage segment and
        are assembled before it: the Knowhow preview
        (``structured_prompt_block``), the enumeration preview (its
        ``enumeration_sub_budget``, less the reserves of the two blocks after
        it), the paper digest (``_add_paper_digest``: ``paper``, the run's
        ``PaperCardEvidence``; whole papers only), sampled document reads
        (``_assemble_document_read_block``'s shared / partition caps) and the
        spreadsheet block (``_add_sheet_prompt``'s ``max_chars``).  Rendered
        against the whole partition they could leave the reserved passages
        zero characters and void every reserved seat.

        So the floor -- ``retrieval.passage_floor``: the REAL rendered cost of
        the reserved-seat prefix of the very order ``_answer_reasoning`` will
        render (``reasoning_order_for``: exact prefix, then the section-read
        seats of passages a ``read_section`` read back whole, then the active /
        per-library prefix), plus the passage segment's heading, capped at half the
        partition -- is computed once and becomes ``room``: what the
        structured side may take in total, ``partition - floor`` less the
        collection map block that sits ahead of it.

        The structured side is first rendered exactly as it always has been.
        Only when that historical rendering is longer than ``room`` -- i.e.
        only when it really would push part of the reserved prefix out of the
        prompt (or, past the half-partition cap, keep more than half of it) --
        is it rendered a second time with ``room`` as an extra ceiling; there
        each block's own coverage disclosure describes the smaller rendering
        exactly.  Every other run -- no chunk, no reserved seat, or a
        historical structured side that leaves the prefix whole -- returns
        the historical rendering byte for byte.

        Returns ``(structured_block, structured_map,
        typed_collection_result_sets, enumeration_block_dropped,
        collection_item_citations, document_read_block_dropped)``.
        """
        limits = stage.prepared.limits
        notebook_id = stage.prepared.notebook_id
        order = (reasoning_order_for(self.settings, list(stage.chunks), notebook_id)
                 if stage.chunks else None)
        floor = (passage_floor(
            order.passages[:order.prefix], limits.chunk_context_chars,
            heading_chars=len(f"\n\n[{_REASONING_CHUNK_HEADING}]\n"))
            if order else 0)
        room = (limits.chunk_context_chars - floor - (
            len(collection_map_prompt_block) + 2
            if collection_map_prompt_block else 0)) if floor else None
        memo: dict = {}
        historical = self._render_structured_side(stage, answer_client, None, memo, paper)
        if room is None or len(historical[0]) <= room:
            return historical
        return self._render_structured_side(stage, answer_client, room, memo, paper)

    def _render_structured_side(self, stage, answer_client, room, memo, paper=None) -> tuple:
        """One rendering of the structured side (see
        ``_assemble_structured_evidence``).  ``room=None`` is the historical
        inline segment of ``_draft_reasoning_response``, moved verbatim;
        with ``room`` each block that would cross what is left is rendered
        again against it (the half-partition caps still read the whole
        partition), and a document-read or spreadsheet block left out only
        for ``room`` leaves its one-line notice.  ``memo`` carries the
        enumeration mapping to the second rendering so it is read once.
        """
        limits = stage.prepared.limits
        notebook_id = stage.prepared.notebook_id
        structured_batch = stage.structured_batch
        enumerations = list(stage.enumerations)
        spreadsheet_results = list(stage.spreadsheet_results)
        structured_block = ""
        if structured_batch is not None and answer_client.configured:
            from app.services.structured_retrieval import structured_prompt_block

            def _knowhow(budget):
                return structured_prompt_block(
                    structured_batch,
                    inline_rows=limits.inline_answer_rows,
                    cell_excerpt_chars=limits.cell_excerpt_chars,
                    budget_chars=budget,
                )
            structured_block = _knowhow(limits.chunk_context_chars)
            if room is not None and len(structured_block) > room:
                structured_block = _knowhow(max(0, room))
        # 类型化集合清单(T4 产出的 enumerations)映射成 AskResponse.result_sets
        # 行 + 合成证据块(T5)。映射与 prompt 块拼接放在**同一个** try 里:两者
        # 失败都只应让清单这一整份"锦上添花"的产出消失,不该出现"卡片有了
        # 但块裸抛穿整轮 Ask"或"块建起来了但卡片已经在别的异常里被清空"的
        # 一半状态(codex 评审实测复现过后一种)。`enumerations` 在上面的 broad
        # except 分支里可能已经被清空过一次,这里的 try 只防映射/渲染本身再
        # 出岔子。
        typed_collection_result_sets: list = []
        enumeration_block_dropped = False
        collection_item_citations: dict = {}
        structured_map: dict = {}
        knowhow_block_len = len(structured_block)   # 花名册拼上来之前的长度
        if enumerations:
            try:
                from app.services.collection_enumeration_answer import (
                    apply_synthesis_preview_counts,
                    delivered_outcomes,
                    enumeration_prompt_block,
                    typed_collection_results,
                )
                # 载荷闸在这里按**真实 wire 形状**收口:执行器的池量的是
                # 紧凑 dataclass,而联合体两臂的默认字段 + 结果元数据会让
                # 下发/持久化的 JSON 明显更宽(见该函数 docstring)。
                cached = memo.get("mapping")
                if cached is None:
                    collection_items = [
                        item for outcome in enumerations for item in outcome.items
                    ]
                    collection_item_citations = (
                        self.evidence_context.collection_item_citations(
                            collection_items,
                            active_notebook_id=notebook_id,
                        )
                    )
                    typed_collection_result_sets = typed_collection_results(
                        enumerations,
                        payload_chars=limits.structured_payload_chars,
                        citations_by_item_id=collection_item_citations,
                    )
                    # 第二次(挤占时)渲染复用映射,不再读一次库;字典留副本,
                    # 取样块会往本次渲染的那份里写。
                    memo["mapping"] = (dict(collection_item_citations),
                                       typed_collection_result_sets)
                else:
                    collection_item_citations = dict(cached[0])
                    typed_collection_result_sets = cached[1]
                if answer_client.configured:
                    from app.services.collection_enumeration_answer import (
                        COLLECTION_MAP_BLOCK_MAX_CHARS,
                        enumeration_sub_budget,
                    )
                    # 枚举块在后:与既有 knowhow structured_block 拼接,整体
                    # 仍经 structured_block 这一个参数进 _answer_reasoning,
                    # 装配位保持在 source 分区最前(语义不变,见该函数 843
                    # 行起)。子预算三层夹(见 enumeration_sub_budget 的
                    # docstring):减去 knowhow 已占字符 + 两者之间 "\n\n"
                    # 拼接符本身的 2 个字符,再夹到 chunk_context_chars 的
                    # 一半,防止模型"顺便列出所有表格"把另一半问题
                    # (chunks/elements)的证据预算整个挤空。
                    # 取样块的席位**先于**枚举预览预留(codex #724 R2):80 篇的
                    # 花名册能把共享的那一半预算吃到只剩几十字,随后拼上来的
                    # 取样块整块被挡——白读。按取样块真实渲染长度先扣。
                    # 预留要从**半预算上限**里扣,不是从整份预算里扣:整份预算
                    # 减掉两千字仍远大于一半,夹到一半后花名册照样拿满 15000。
                    read_reserve = document_read_block_reserve(
                        stage.document_reads) + paper_digest_reserve(
                            paper, limits.chunk_context_chars)
                    enum_budget_chars = max(0, enumeration_sub_budget(
                        chunk_context_chars=limits.chunk_context_chars,
                        structured_block_len=len(structured_block),
                    ) - read_reserve)
                    # 预览渲染的是**结果卡真正拿到的那份**:wire 闸裁过的
                    # 集合若照原 outcome 渲染,prompt 里会出现卡片没有的行,
                    # 头部还写着「complete」——prompt 与卡片对同一份清单说
                    # 两套话,正是 coverage 合同要防的东西。
                    delivered = delivered_outcomes(
                        enumerations, typed_collection_result_sets)

                    # 论文表由 find_sources 定了覆盖说明时,花名册预览不进合成:它的
                    # 「previewed N of M」会让模型另写一句与覆盖说明矛盾的范围声明。
                    omit = roster_omitted_for(paper)

                    def _preview(budget):
                        return enumeration_prompt_block(
                            delivered,
                            inline_rows=limits.inline_answer_rows,
                            budget_chars=budget,
                            citations_by_item_id=collection_item_citations,
                            omit_collections=omit,
                        )
                    preview = _preview(enum_budget_chars)
                    # 挤占时(room 非空)才收紧,半分区上限仍按整份分区算。只为
                    # 真能装进去的取样块预留:它在 room 与自身历史上限下都放得
                    # 下才扣它的份额,否则花名册白白让出一块最终没人用的空间。
                    joiner = 2 if structured_block else 0
                    read_limit = (None if room is None else min(
                        room, limits.chunk_context_chars
                        - COLLECTION_MAP_BLOCK_MAX_CHARS - 4))
                    read_fits = bool(read_reserve) and read_limit is not None and (
                        read_reserve - 2 <= limits.chunk_context_chars // 2
                        and len(structured_block) + joiner + read_reserve - 2
                        <= read_limit)
                    fit = (None if room is None else max(
                        0, (read_limit if read_fits else room)
                        - len(structured_block) - joiner
                        - (read_reserve if read_fits else 0)))
                    if fit is not None and len(preview.text) > fit:
                        preview = _preview(min(enum_budget_chars, fit))
                    structured_map = preview.evidence_by_id
                    apply_synthesis_preview_counts(
                        typed_collection_result_sets, preview.shown_rows
                    )
                    if preview.text:
                        structured_block = (
                            concat(structured_block, Glue("\n\n"), preview.text)
                            if structured_block else preview.text
                        )
                    else:
                        # 预算把连块头都挤没了(极端场景,如 knowhow 已经吃满
                        # chunk_context_chars 的绝大部分):清单结果卡仍然
                        # 存在(上面 typed_collection_result_sets 不受影响),
                        # 只是这一轮没能把它塞进合成证据——这条轨迹detail是
                        # 唯一挂点(trace 已闭合,不能另起一条独立 trace 步)。
                        # 整份按 ``omit`` 刻意省略的不算被挤掉。
                        enumeration_block_dropped = any(
                            o.collection not in omit for o in delivered)
            except (AskCancelled, RetrievalControlError):
                raise
            except Exception:
                typed_collection_result_sets = []
                enumeration_block_dropped = False
        structured_block = self._add_paper_digest(
            structured_block, structured_map, paper, answer_client, limits,
            knowhow_block_len, room, document_read_block_reserve(stage.document_reads))
        structured_block, document_read_block_dropped = (
            self._assemble_document_read_block(
                stage.document_reads, structured_block, structured_map,
                collection_item_citations, answer_client,
                limits.chunk_context_chars, knowhow_block_len, room=room))
        structured_block = self._add_sheet_prompt(
            structured_block, structured_map, spreadsheet_results, answer_client,
            max_chars=(None if room is None else max(
                0, room - len(structured_block) - (2 if structured_block else 0))))
        return (structured_block, structured_map, typed_collection_result_sets,
                enumeration_block_dropped, collection_item_citations,
                document_read_block_dropped)

    def _reasoning_chunk_cards(self, chunks, baseline: Mapping[str, Any],
                               notebook_id: str) -> list[Citation]:
        """The passage (chunk) citation cards of ``_draft_reasoning_response``.

        The candidates are the passages that really entered the synthesis
        prompt: ``baseline["id_map"]`` (filled before the model call, so a
        round whose model raised still has it) filters this run's
        ``stage.chunks``, keeping stage order.  ``chunks`` in full is
        deliberately not used -- a passage the ``chunk_context`` character
        budget squeezed out never reached the model, and carding it would list
        as "the answer's basis" something the answer's author never read.  A
        sectioned synthesis has already merged every section's id map into one
        baseline (``_merged_section_baseline``), so no key-range split is
        needed here.

        The three batch reads behind each card were made once already, when
        the same passages were assembled into the prompt; ``baseline
        ["citation_lookups"]`` carries them, and ``chunk_citations`` reads only
        what that memo lacks (see ``CitationLookups``).  No passage in the
        prompt (a KG-only run, or no synthesis at all) -> no call and no read.
        """
        prompt_chunk_ids = {
            str(entry.get("object_id") or "")
            for entry in dict(baseline.get("id_map") or {}).values()
            if entry.get("object_type") == "chunk"
        }
        prompt_chunks = [item for item in chunks if item.chunk_id in prompt_chunk_ids]
        if not prompt_chunks:
            return []
        lookups = baseline.get("citation_lookups")
        return [c for c, _ids in self.evidence_context.chunk_citations(
            prompt_chunks, notebook_id=notebook_id,
            lookups=lookups if isinstance(lookups, CitationLookups) else None)]

    def draft_response(self, stage, runtime):
        """The shipped ``ResponseDraftStage`` body: evidence -> response draft.

        Only the order of the phases lives here, after the two type gates; each
        phase is one private helper over a ``_ReasoningDraftRun``: the rebinding
        prologue (``_begin_reasoning_draft``), the related knowledge and the KG
        evidence cards, the synthesis inputs (the answer client, the collection
        map block and the structured side), the sectioned synthesis attempt,
        its adoption or the single synthesis, the retrieval baseline manifest,
        the citation cards, the evidence grading, the enumeration clearing and
        the evidence images, the synthesis trace step, the conclusion and its
        disclosures, and the ``AskResponse``. This module owns the synthesis,
        evidence assembly and citation binding behind the application seam;
        each phase preserves the existing order and shared run-state identity.

        The response graph is *finished* here, including ``mode`` -- so the
        only mutations left after the frozen envelope are ``model_errors``
        (filled by the orchestrator right after this stage returns, from the
        same request-local sink, so every stage gets the same treatment
        without needing to import it -- see ``_reasoning_draft_phase``) and
        ``answer_id`` (which the commit boundary owns).
        """
        from app.application.ask_reasoning import (
            ReasoningResponseDraft,
            ReasoningRetrievalRuntime,
            ResponseDraftInput,
            StageBoundaryError,
        )

        # Defense-in-depth only: the full authority sweep for this point now
        # runs once in the orchestrator (``_reasoning_draft_phase``), right
        # before it enters this seam, so every stage implementation -- shipped
        # or injected -- is admitted under the same already-verified runtime.
        # These two ``type(...)`` checks stay here because this method is
        # itself directly callable (it is the shipped stage's own body), and
        # a malformed envelope should still fail loudly even when invoked
        # outside the orchestrator's protection.
        if type(stage) is not ResponseDraftInput:
            raise StageBoundaryError("invalid reasoning response draft input")
        if type(runtime) is not ReasoningRetrievalRuntime:
            raise StageBoundaryError("invalid reasoning response draft runtime")
        run = self._begin_reasoning_draft(stage, runtime)
        self._draft_related_knowledge(run)
        self._draft_synthesis_inputs(run)
        self._draft_sectioned_attempt(run)
        self._draft_synthesis(run)
        self._draft_baseline_manifest(run)
        self._draft_citation_cards(run)
        self._draft_evidence_level(run)
        self._draft_attachments(run)
        self._draft_synthesis_step(run)
        self._draft_conclusion(run)
        response = self._draft_ask_response(run)
        prepared = run.prepared
        notebook_id = run.notebook_id
        conversation_id = run.conversation_id
        final_baseline_manifest = run.ready("final_baseline_manifest")
        # ``model_errors`` is deliberately *not* set here any more: the
        # orchestrator (``_reasoning_draft_phase``) fills
        # ``draft.response.model_errors`` from the same request-local
        # ``_err_sink`` right after this stage returns, so every stage --
        # shipped or injected -- gets the same treatment and an injected
        # implementation is never required to import the private
        # ``_ASK_MODEL_ERRORS`` ContextVar just to keep retrieval-side
        # warnings visible (codex #571 R2 P2).  Setting it here too would
        # only assign it twice with the same value.
        return ReasoningResponseDraft(
            notebook_id=notebook_id,
            question=prepared.question,
            response=response,
            conversation_id=conversation_id,
            user_id=prepared.user_id,
            job_id=prepared.job_id,
            asked_at=prepared.asked_at,
            baseline_manifest=final_baseline_manifest,
        )

    def _begin_reasoning_draft(self, stage, runtime) -> _ReasoningDraftRun:
        """The run state: the draft input rebound to the names the body reads
        (list snapshots of its evidence tuples), the attached report this turn
        reads, the trace sink, the cancellation and the request's model-error
        sink."""
        prepared = stage.prepared
        notebook_id = prepared.notebook_id
        conversation_id = prepared.conversation_id
        reasoning_history = prepared.history
        style_block = prepared.style_block
        research_question = prepared.research_question
        limits = prepared.limits
        retrieval_effort = prepared.retrieval_effort
        intent_contract = stage.intent_contract
        memory_hits = list(stage.memory_hits)
        structured_batch = stage.structured_batch
        completeness_unavailable = stage.completeness_unavailable
        no_usable_kg = stage.kg_required
        top_hits = list(stage.top_hits)
        elements = list(stage.elements)
        trace = list(stage.trace)
        chunks = list(stage.chunks)
        chains = list(stage.chains)
        enumerations = list(stage.enumerations)
        spreadsheet_results = list(stage.spreadsheet_results)
        collection_map_text = stage.collection_map_text
        reasoning_outline = list(stage.outline)
        reasoning_outline_evidence = list(stage.outline_evidence)
        historical_reasoning_chunks = list(stage.historical_chunks)
        report = self._reasoning_report_turn(prepared)  # 关联报告:见其 docstring
        on_trace = runtime.trace_sink
        cancel_event = runtime.cancellation
        # 与 ``_answer_with_retry`` 同一条惯例:响应内报警表是请求局部
        # ContextVar,由 ``_run_reasoning_stage`` 设置、在它的 ``finally`` 里
        # 复位。直调/离线路径下它是 None —— 那时 ``note_model_error`` 只写事件
        # 日志、不写响应,没有可摘也没有可报的东西。
        _err_sink = _ASK_MODEL_ERRORS.get()
        if _err_sink is None:
            _err_sink = []
        return _ReasoningDraftRun(
            stage=stage, prepared=prepared, notebook_id=notebook_id,
            conversation_id=conversation_id, reasoning_history=reasoning_history,
            style_block=style_block, research_question=research_question,
            limits=limits, retrieval_effort=retrieval_effort,
            intent_contract=intent_contract, memory_hits=memory_hits,
            structured_batch=structured_batch,
            completeness_unavailable=completeness_unavailable,
            no_usable_kg=no_usable_kg, top_hits=top_hits, elements=elements,
            trace=trace, chunks=chunks, chains=chains, enumerations=enumerations,
            spreadsheet_results=spreadsheet_results,
            collection_map_text=collection_map_text,
            reasoning_outline=reasoning_outline,
            reasoning_outline_evidence=reasoning_outline_evidence,
            historical_reasoning_chunks=historical_reasoning_chunks,
            report=report, on_trace=on_trace, cancel_event=cancel_event,
            err_sink=_err_sink,
        )

    def _draft_related_knowledge(self, run: _ReasoningDraftRun) -> None:
        """The related knowledge the response lists (each hit rendered against
        its owning notebook's schema overlay, deduplicated, capped) and the KG
        evidence cards the citations start from."""
        notebook_id, top_hits = run.notebook_id, run.top_hits
        cancel_event = run.cancel_event
        # Federated hits carry their owning notebook. Render each payload
        # against THAT notebook's schema overlay: the active notebook may
        # customize the same object_type, but its field order/primary label
        # must never be projected onto an object from a mounted base.
        schema_registries = {
            owner_id: self.effective_schemas(owner_id)
            for owner_id in {
                item.notebook_id or notebook_id for item in top_hits
            }
        }
        seen_ids: set = set()
        related_knowledge: List[KnowledgeRecord] = []
        raise_if_cancelled(cancel_event)
        for item in top_hits:
            if item.object_id in seen_ids:
                continue
            seen_ids.add(item.object_id)
            related_knowledge.append(knowledge_record(
                item.object_type,
                {"id": item.object_id, "payload": item.payload, "status": item.status,
                 "owner": getattr(item, "owner", ""),
                 "last_reviewed": getattr(item, "last_reviewed", ""),
                 "evidence": item.evidence},
                schema_registries[item.notebook_id or notebook_id].get(
                    item.object_type
                )))
        related_knowledge = related_knowledge[
            : self.settings.ask_related_knowledge_limit
        ]

        cited_element_ids = {ev.element_id for item in top_hits
                             for ev in item.evidence if ev.element_id}
        citations = self.evidence_context.citations_from(
            top_hits, cited_element_ids, "KG evidence", notebook_id=notebook_id)
        run.related_knowledge, run.citations = related_knowledge, citations

    def _draft_synthesis_inputs(self, run: _ReasoningDraftRun) -> None:
        """What every synthesis below reads: the answer's starting values, the
        synthesis clock, the counts / baseline sinks, the run's citation
        lookups and admitted external keys (behind a cancellation check), the
        answer client, the collection map block and the structured side."""
        stage, collection_map_text = run.stage, run.collection_map_text
        cancel_event = run.cancel_event
        answer, llm_grounded, anchors = "", False, []
        synth_failed = False
        synthesis_ran = False
        synthesis_started = time.perf_counter()
        # _answer_reasoning returns a 4th `counts` element (included_kg/
        # chunks/elements); _answer_with_retry's synth() contract is shared
        # with other answer paths and stays a 3-tuple, so this closure
        # captures counts as a side effect instead of widening that contract.
        reasoning_counts: dict = {}
        reasoning_baseline: dict = {}
        run_lookups = CitationLookups()  # 本 run 各次装配与引用卡共用(见其 docstring)
        admitted_external: set = set()
        raise_if_cancelled(cancel_event)
        answer_client = self.model_clients.chat("ask_answer")
        # 地图块:确定性服务端小块,不依赖 enumerations 是否为空——「集合太大
        # 就别枚举、直接报数」这条路径下恰恰一条清单都没有,而那个数正是答案
        # 本身。渲染纯字符串拼接,不可能抛,故不需要 try 包(与下方清单块不同,
        # 那里有映射/预算计算)。
        from app.services.collection_enumeration_answer import (
            collection_map_block as _collection_map_block,
        )
        collection_map_prompt_block = (
            _collection_map_block(collection_map_text)
            if answer_client.configured else ""
        )
        paper = self._paper_card_evidence(stage)  # 论文工具的登记与表格(见其 docstring)
        (structured_block, structured_map, typed_collection_result_sets,
         enumeration_block_dropped, collection_item_citations,
         document_read_block_dropped) = self._assemble_structured_evidence(
            stage, answer_client, collection_map_prompt_block, paper)
        if paper is not None:  # 登记的引用卡按锚点的 object_id(元素 id)发;
            for element_id, citation in paper.citations.items():  # 清单行的卡不被顶掉
                collection_item_citations.setdefault(element_id, citation)
        run.paper_evidence = paper
        run.answer, run.llm_grounded, run.anchors = answer, llm_grounded, anchors
        run.synth_failed, run.synthesis_ran = synth_failed, synthesis_ran
        run.synthesis_started = synthesis_started
        run.reasoning_counts, run.reasoning_baseline = reasoning_counts, reasoning_baseline
        run.run_lookups, run.admitted_external = run_lookups, admitted_external
        run.answer_client = answer_client
        run.collection_map_prompt_block = collection_map_prompt_block
        run.structured_block, run.structured_map = structured_block, structured_map
        run.typed_collection_result_sets = typed_collection_result_sets
        run.enumeration_block_dropped = enumeration_block_dropped
        run.collection_item_citations = collection_item_citations
        run.document_read_block_dropped = document_read_block_dropped

    def _draft_sectioned_attempt(self, run: _ReasoningDraftRun) -> None:
        """The outline plan and, when it has two or more sections with evidence
        and no checklist, Knowhow batch or attached report holds the run to one
        synthesis, the section-by-section synthesis attempt."""
        stage, limits, report = run.stage, run.limits, run.report
        notebook_id, research_question = run.notebook_id, run.research_question
        reasoning_history, style_block = run.reasoning_history, run.style_block
        top_hits, elements, chunks = run.top_hits, run.elements, run.chunks
        enumerations, structured_batch = run.enumerations, run.structured_batch
        reasoning_outline = run.reasoning_outline
        reasoning_outline_evidence = run.reasoning_outline_evidence
        cancel_event, _err_sink = run.cancel_event, run.err_sink
        answer_client = run.ready("answer_client")
        admitted_external = run.ready("admitted_external")
        run_lookups = run.ready("run_lookups")
        record_section_step = run.record_section_step
        # ---------------------------------------------- 按节合成(设计文档 §3.1)
        # 终态大纲有 ≥2 个能装配出证据的节时,逐节合成再拼接:每节只看见自己
        # 绑上的那几条证据(DualGraph 的产出侧借鉴,避免 lost-in-the-middle)。
        # 闸与 O1 同一个 —— 只在穷尽档提供,k 次合成调用的成本由用户显式选择
        # 「穷尽」来承担;门关着或大纲够不到两节时,下面那条单次合成路径逐字
        # 节不变。
        outline_slices: list = []
        outline_skipped: list[str] = []
        outline_attempted = False
        outline_planned = False
        sectioned = None
        # 分节阶段可能记下的 model_error 起点。回退**成功**后要把这一段摘掉:
        # 那次故障已经被同一轮的重试路径吸收,用户拿到了完整答案,再挂一条红色
        # 横幅是假报警。事件日志(events.jsonl)不受影响 —— note_model_error 的
        # 两个副作用里,只有响应里的这一份是给用户看的。
        outline_err_mark = len(_err_sink)

        if answer_client.configured and reasoning_outline:
            from app.services.outline_synthesis import plan_outline_sections
            from app.services.reasoning_retrieval import outline_wiring_active
            if outline_wiring_active(self.settings, limits):
                # top_hits 的重排分数优先:同一个对象两处都有时,以最终选集
                # 那一份为准(补集只补它没有的)。
                outline_kg_by_id = {
                    hit.object_id: hit for hit in reasoning_outline_evidence
                }
                outline_kg_by_id.update({hit.object_id: hit for hit in top_hits})
                outline_slices, outline_skipped = plan_outline_sections(
                    reasoning_outline,
                    kg_by_id=outline_kg_by_id,
                    element_by_id={item.element_id: item for item in elements},
                    chunk_by_id={item.chunk_id: item for item in chunks},
                    exact_reserve=self.settings.reasoning_exact_reserve,
                )
                outline_planned = True
                # 产出过集合清单/结构化整表枚举的 run 保持单次合成(codex r7):
                # 节切片刻意只装该节绑定证据,清单块与结构化行不进切片——
                # 「按来源列出全部公式」这类请求若被节化,合成会拿 ranked 样本
                # 写散文,把手上已有的完整清单丢在回退路径里,甚至自称不完整。
                # 单次合成路径的清单预览/覆盖披露机制是成熟的,清单类问题本来
                # 就该走它;清单进节切片是 v2 的设计题,不在绕过里偷做。
                if (len(outline_slices) >= 2 and not enumerations
                        and structured_batch is None and report is None):
                    outline_attempted = True
                    sectioned = self._answer_reasoning_sections(
                        notebook_id, research_question, outline_slices,
                        history=reasoning_history, limits=limits,
                        answer_client=answer_client, cancel_event=cancel_event,
                        on_section=record_section_step,
                        style_block=style_block,
                        external_evidence=stage.external_evidence,
                        external_sink=admitted_external,
                        citation_lookups=run_lookups,
                        paper=run.ready("paper_evidence"),
                    )
        run.outline_slices, run.outline_skipped = outline_slices, outline_skipped
        run.outline_attempted, run.outline_planned = outline_attempted, outline_planned
        run.sectioned, run.outline_err_mark = sectioned, outline_err_mark

    def _draft_synthesis(self, run: _ReasoningDraftRun) -> None:
        """The sectioned synthesis's answer when it was written, else the single
        synthesis (when there is anything to answer from) -- and, when that one
        absorbs a sectioned attempt's failure, the attempt's alarms dropped."""
        stage, limits, report = run.stage, run.limits, run.report
        notebook_id, research_question = run.notebook_id, run.research_question
        reasoning_history, style_block = run.reasoning_history, run.style_block
        top_hits, elements, chunks = run.top_hits, run.elements, run.chunks
        chains, memory_hits = run.chains, run.memory_hits
        enumerations, structured_batch = run.enumerations, run.structured_batch
        cancel_event, _err_sink = run.cancel_event, run.err_sink
        answer = run.ready("answer")
        llm_grounded = run.ready("llm_grounded")
        anchors = run.ready("anchors")
        synth_failed = run.ready("synth_failed")
        synthesis_ran = run.ready("synthesis_ran")
        reasoning_counts = run.ready("reasoning_counts")
        reasoning_baseline = run.ready("reasoning_baseline")
        run_lookups = run.ready("run_lookups")
        admitted_external = run.ready("admitted_external")
        answer_client = run.ready("answer_client")
        collection_map_prompt_block = run.ready("collection_map_prompt_block")
        structured_block = run.ready("structured_block")
        structured_map = run.ready("structured_map")
        outline_attempted = run.ready("outline_attempted")
        sectioned = run.ready("sectioned")
        outline_err_mark = run.ready("outline_err_mark")
        paper = run.ready("paper_evidence")
        def _synth_reasoning():
            # counts_sink 在模型调用前就被 _answer_reasoning 填充:合成模型
            # 抛错/吐畸形 JSON 时,synthesis 步仍能报出真实装配计数而非全零。
            ans, llm_grounded_, anchors_, _counts = self._answer_reasoning(
                notebook_id, research_question, top_hits, elements,
                reasoning_history,
                cancel_event=cancel_event, chunks=chunks, chains=chains,
                memory_hits=memory_hits, answer_client=answer_client,
                kg_context_chars=limits.kg_context_chars,
                chunk_context_chars=limits.chunk_context_chars,
                element_items=limits.answer_element_items,
                structured_block=structured_block,
                structured_map=structured_map,
                collection_map_block=collection_map_prompt_block,
                counts_sink=reasoning_counts,
                baseline_sink=reasoning_baseline,
                external_evidence=stage.external_evidence,
                external_sink=admitted_external,
                style_block=style_block, citation_lookups=run_lookups, report=report,
                paper=paper)
            return ans, llm_grounded_, anchors_

        # 按节合成失败(某一节两次都吐不出内容)→ 整体回退单次合成,已经产出
        # 的分节文本全部丢弃。多付一次合成调用是 fail-open 的价钱。
        outline_fallback = outline_attempted and sectioned is None
        if sectioned is not None:
            answer = sectioned.answer
            llm_grounded = sectioned.llm_grounded
            anchors = sectioned.anchors
            reasoning_counts.clear()
            reasoning_counts.update(sectioned.counts)
            reasoning_baseline.clear()
            reasoning_baseline.update(
                _merged_section_baseline(sectioned.baseline_assemblies))
            synthesis_ran = True
        # 地图也是合成的触发条件之一:大集合场景里模型 reflect 直接 answer、
        # 一条证据都没检索到,而正确答案就是那个计数——没有这一项,合成压根
        # 不跑,用户拿到的是空答案(codex 第 4 轮 P2)。地图非空意味着枚举
        # 工具接线成功且作用域里有可数的东西(空库在更早的早退里就返回了),
        # 所以这不是给空库额外加一次模型调用。外部证据同理且更强:模型正是在
        # 库内反复空手之后才去调插件动作的,漏掉它就是让唯一有材料的那轮没合成。
        # `stage.document_reads` 不另列:总闸含 `enumeration_active()`、执行体无
        # 花名册只会 skip,故有取样必有 `enumerations`(拆那把闸要同改这里)。
        elif answer_client.configured and (
                top_hits or elements or chunks or chains or memory_hits
                or structured_batch is not None or enumerations
                or collection_map_prompt_block or stage.external_evidence or report
                or paper is not None):
            # 空 content 有界重试 + 诚实降级 + 可观测,统一走 _answer_with_retry(见其 docstring)。
            fallback_err_mark = len(_err_sink)
            answer, llm_grounded, anchors, _ok = self._answer_with_retry(
                _synth_reasoning,
                getattr(answer_client, "model", ""),
                service="ask_answer",
            )
            synth_failed = not _ok
            if synth_failed:  # 论文表是确定性的、引用已登记:合成失败也照样给出
                answer, anchors = self._with_sectioned_paper_table(answer, anchors, paper)
            synthesis_ran = True
            if outline_fallback and _ok:
                # 只摘分节那一段(mark..fallback_mark)。单次合成自己那几条
                # 已经由 `_answer_with_retry` 按同一条规则处理过了(重试成功
                # 自摘、两次都失败全留),所以这里刻意仍用**闭区间上界**
                # `fallback_err_mark` 而不是 `del _err_sink[outline_err_mark:]`:
                # 上界是这两段的边界,写成开区间就等于宣称「回退阶段的报警一律
                # 由这一行负责」,哪天单次合成路径多记一条本该保留的错误就会被
                # 这一行悄悄吞掉。
                del _err_sink[outline_err_mark:fallback_err_mark]
        run.outline_fallback = outline_fallback
        run.answer, run.llm_grounded, run.anchors = answer, llm_grounded, anchors
        run.synth_failed, run.synthesis_ran = synth_failed, synthesis_ran

    def _draft_baseline_manifest(self, run: _ReasoningDraftRun) -> None:
        """The retrieval baseline manifest of what the synthesis was given."""
        stage, notebook_id = run.stage, run.notebook_id
        research_question = run.research_question
        top_hits, elements, chunks = run.top_hits, run.elements, run.chunks
        historical_reasoning_chunks = run.historical_reasoning_chunks
        trace = run.trace
        reasoning_baseline = run.ready("reasoning_baseline")
        from app.services.retrieval_baseline import (
            build_retrieval_baseline_manifest,
        )
        candidate_manifest = stage.candidate_manifest
        final_baseline_manifest = build_retrieval_baseline_manifest(
            notebook_id=notebook_id,
            query=research_question,
            mode="reasoning",
            settings=self.settings,
            candidate_knowledge=(
                candidate_manifest.candidate_knowledge
                if candidate_manifest is not None else top_hits
            ),
            candidate_chunks=(
                candidate_manifest.candidate_chunks
                if candidate_manifest is not None else chunks
            ),
            candidate_elements=(
                candidate_manifest.candidate_elements
                if candidate_manifest is not None else elements
            ),
            selected_knowledge=top_hits,
            selected_chunks=historical_reasoning_chunks,
            selected_elements=elements,
            final_context_block=str(reasoning_baseline.get("context_block") or ""),
            final_id_map=dict(reasoning_baseline.get("id_map") or {}),
            final_ordered_handles=tuple(
                reasoning_baseline.get("ordered_handles") or ()
            ),
            final_budget_chars=int(reasoning_baseline.get("budget_chars") or 0),
            prior_manifest=candidate_manifest,
            capture_error_count=int(
                reasoning_baseline.get("capture_error_count") or 0
            ),
            baseline_step_usage=len(trace),
        )
        run.final_baseline_manifest = final_baseline_manifest

    def _draft_citation_cards(self, run: _ReasoningDraftRun) -> None:
        """The citation cards the bound anchors add (Memory, elements, passages,
        the attached report's items, typed collection rows), and the relevance
        entries of the relations and elements the evidence pool needs."""
        notebook_id, report = run.notebook_id, run.report
        elements, chunks = run.elements, run.chunks
        chains, memory_hits = run.chains, run.memory_hits
        citations = run.ready("citations")
        anchors = run.ready("anchors")
        reasoning_baseline = run.ready("reasoning_baseline")
        collection_item_citations = run.ready("collection_item_citations")
        # chunks 直接进证据池:RetrievedChunk.object_id 属性=chunk_id,与 chunk 锚的
        # object_id 对齐,classify_evidence 即可正确计 anchored_rel(守 tau)。
        # Relation anchors need classifier entries, but chain trust is NOT
        # query relevance.  Each chain carries only the relevance of the
        # candidate that authorized that action; unrelated high-scoring hits
        # elsewhere in the answer cannot elevate its anchors over tau.
        from types import SimpleNamespace

        from app.services.kg.follow_chain import chain_anchor_relevances
        relation_relevances = chain_anchor_relevances(chains)
        chain_evidence = [SimpleNamespace(
            object_id=relation_id, relevance=relevance,
        ) for relation_id, relevance in relation_relevances.items()]
        citations.extend(self._memory_citations(anchors, memory_hits))
        # 卡片段(k9001+)的锚点不走元素腿:那条腿逐锚点建卡,同一元素被 k4001 与
        # k9xxx 各引一次会得到两张卡;卡片段自己的卡在下面的清单循环里按元素补发。
        paper = run.ready("paper_evidence")
        paper_keys = paper_registered_keys(paper)
        citations.extend(self.evidence_context.element_citations(
            elements, [a for a in anchors if a.key not in paper_keys], notebook_id=notebook_id))
        # 原文段腿:候选集口径与读取复用见 `_reasoning_chunk_cards`;清空闸照常适用,
        # 这些卡不参与附图(见下面 attach_citation_images 处按身份排除的理由)。
        chunk_cards = self._reasoning_chunk_cards(chunks, reasoning_baseline, notebook_id)
        citations.extend(chunk_cards)
        report_cards = self._extend_report_cards(citations, report, anchors, notebook_id)
        # Typed collection rows have their own deterministic k5001+
        # bindings.  Mirror the cited rows into the fallback Citation
        # contract as well, while keeping the anchor path authoritative.
        seen_collection_citations: set[tuple[str, str]] = set()
        # 这些 Citation **实例**同时躺在 `typed_collection_result_sets` 的行
        # 里(`typed_collection_results` 把同一个对象挂进 `TypedCollectionItem
        # .citation`),所以下面那次 `attach_citation_images` 必须按身份把它们
        # 排除——理由写在那处。
        collection_citation_ids: set[int] = {id(c) for c in (*chunk_cards, *report_cards.citations)}
        # 论文卡片段(k9001+)的卡只在同一元素还没有卡时才发:同一个元素被 k4001 与
        # k9xxx 各引一次时只留一张。只作用于卡片段——清单行(k5001+)的既有行为不动。
        paper_card_ids = {id(c) for c in paper.citations.values()} if paper is not None else set()
        for anchor in anchors:
            citation = collection_item_citations.get(anchor.object_id)
            if citation is None:
                continue
            identity = (citation.source_id, citation.element_id)
            if identity in seen_collection_citations or (id(citation) in paper_card_ids and any(
                    c.element_id == citation.element_id for c in citations)):
                continue
            seen_collection_citations.add(identity)
            citations.append(citation)
            collection_citation_ids.add(id(citation))
        element_evidence = [SimpleNamespace(
            object_id=item.element_id, relevance=float(item.score or 0.0),
        ) for item in elements]
        run.chain_evidence, run.element_evidence = chain_evidence, element_evidence
        run.report_cards = report_cards
        run.collection_citation_ids = collection_citation_ids

    def _draft_evidence_level(self, run: _ReasoningDraftRun) -> None:
        """The answer's evidence level: ``classify_evidence`` over the pool (the
        outline bindings the selection lost included when the sectioned
        synthesis was written), capped by the per-section grounding."""
        top_hits, chunks, memory_hits = run.top_hits, run.chunks, run.memory_hits
        anchors = run.ready("anchors")
        llm_grounded = run.ready("llm_grounded")
        sectioned = run.ready("sectioned")
        outline_slices = run.ready("outline_slices")
        structured_map = run.ready("structured_map")
        chain_evidence = run.ready("chain_evidence")
        element_evidence = run.ready("element_evidence")
        report_cards = run.ready("report_cards")
        # 按节合成真正写进 prompt 的知识对象里,有一部分被 top_n 截断挤出了
        # top_hits(见 outline_truncated_kg_evidence)。它们进不了这个池子的话,
        # 引用了它们的句子在 classify_evidence 眼里就是"引了个不存在的东西",
        # 整篇答案被降级成 inferred —— 而模型引的恰恰是服务端指名喂给它的证据。
        # 只在按节合成真的产出答案时并入:回退路径与关闭态的池子逐字不变。
        outline_pool_extra: list = []
        if sectioned is not None:
            pooled_ids = {hit.object_id for hit in top_hits}
            for item in outline_slices:
                for hit in item.hits:
                    if hit.object_id in pooled_ids:
                        continue
                    pooled_ids.add(hit.object_id)
                    outline_pool_extra.append(hit)
        evidence_pool = (
            list(top_hits) + outline_pool_extra + list(chunks) + chain_evidence
            + element_evidence + list(memory_hits)
        )
        collection_exact_keys = {
            key
            for key, context in structured_map.items()
            if context.get("source_id") and context.get("element_id")
        } | self._external_exact_keys(anchors) | report_cards.exact_keys | (
            paper_registered_keys(run.ready("paper_evidence")))
        evidence_level, top_relevance = classify_evidence(
            evidence_pool, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high,
            exact_evidence_keys=collection_exact_keys,
        )
        if sectioned is not None:
            grounded_sections = sum(
                1 for item in sectioned.section_grounding
                if item["grounded"]
            )
            if grounded_sections < sectioned.sections:
                # 部分或零节通过逐节 grounded 门时,整体最多 overview。这里是
                # 封顶而非强制设置:全局分类若已是 inferred 不会被反向抬高;
                # 零节各自为 overview 时也不会被误写成「未命中笔记本依据」。
                if evidence_level == "grounded":
                    evidence_level = "overview"
        grounded = evidence_level == "grounded"
        run.evidence_level, run.top_relevance = evidence_level, top_relevance
        run.grounded = grounded

    def _draft_attachments(self, run: _ReasoningDraftRun) -> None:
        """The checklist answer's citation clearing, the workbook and external
        receipts, then the images of the final anchors and citations."""
        stage, chunks = run.stage, run.chunks
        enumerations, spreadsheet_results = run.enumerations, run.spreadsheet_results
        citations = run.ready("citations")
        anchors = run.ready("anchors")
        synthesis_ran = run.ready("synthesis_ran")
        admitted_external = run.ready("admitted_external")
        report_cards = run.ready("report_cards")
        collection_citation_ids = run.ready("collection_citation_ids")
        # An enumeration answer with no bound [k] marker has no verifiable
        # attribution.  Do not let unrelated ranked-retrieval citations
        # masquerade as sources for a checklist the model may have copied.
        if enumerations and synthesis_ran and not anchors:
            citations = []

        self._append_spreadsheet_citations(citations, spreadsheet_results)
        self._append_external_citations(
            citations, stage.external_evidence, admitted_external)

        # 本段附图: 统一挂在**锚点最终确定之后**——按节合成(sectioned)会整体
        # 换掉 anchors,在它之前富化等于富化一批被丢弃的对象;citations 也要等
        # 到上面那条枚举清零判完,否则会给一批马上要被扔掉的引用白发一次读取。
        # 这里的引用(KG / element / 原文段)各自都带 element_id,所以只有 chunk
        # 锚点需要额外候选。锚点与引用同一次调用,共享每答案预算。
        #
        # **集合枚举行的引用按身份排除**(collection_citation_ids):它们是嵌在
        # `typed_collection_result_sets` 里的**同一个** Citation 实例,就地填
        # `.images` 会绕过 `typed_collection_results` 已经按 wire 形状收完的
        # 载荷计费(每张图约 +77 字节未计费),把「响应不会大于它声明的」这条
        # 保证拆掉;而清单卡本来就自带 `TypedCollectionItem.asset_id`,附图对它
        # 纯属冗余。k5001 锚点那条腿不受影响——锚点是另一批对象。
        # 按篇取样(k7001+)的卡**也**被排除,但理由是另一条:它们是独立实例、不涉
        # 共享计费,排除是为与「零锚点回退不带配图」这条已登记取舍一致(不重复计费)。
        # 原文段腿的卡(chunk_cards)同样按身份排除:其 element_id 是该段首元素,模型
        # 引了同一段时锚点候选已含它——不排除就是同一张图在每答案预算里扣两次。
        self.evidence_context.attach_citation_images(
            anchor_image_targets(
                anchors, {item.chunk_id: item.element_ids for item in (*chunks, *report_cards.chunks)}
            ) + [
                (citation, ()) for citation in citations
                if id(citation) not in collection_citation_ids
            ]
        )
        run.citations = citations

    def _draft_synthesis_step(self, run: _ReasoningDraftRun) -> None:
        """The synthesis trace step, when a synthesis ran."""
        trace, on_trace = run.trace, run.on_trace
        enumerations, report = run.enumerations, run.report
        answer = run.ready("answer")
        anchors = run.ready("anchors")
        citations = run.ready("citations")
        synthesis_ran = run.ready("synthesis_ran")
        synthesis_started = run.ready("synthesis_started")
        evidence_level = run.ready("evidence_level")
        reasoning_counts = run.ready("reasoning_counts")
        enumeration_block_dropped = run.ready("enumeration_block_dropped")
        document_read_block_dropped = run.ready("document_read_block_dropped")
        outline_planned = run.ready("outline_planned")
        sectioned = run.ready("sectioned")
        outline_fallback = run.ready("outline_fallback")
        outline_skipped = run.ready("outline_skipped")
        outline_slices = run.ready("outline_slices")
        if synthesis_ran:
            # The retriever's own last step reports which evidence it ADOPTED;
            # writing the answer (and assembling its citations) happens out
            # here and used to be invisible.  Without this step the trace
            # stalls on "合成" for the whole generation call and the trace
            # total silently omits it — usually the largest slice of a run.
            synthesis_step = TraceStep(
                step_type="synthesis",
                summary=(
                    # anchors 才是模型真正绑上的 [k];citations 是「每条检索到
                    # 的证据一张卡」,模型一个锚点都没吐出来时它还会被当兜底
                    # 列表展示(见 evidence_context.citations_from 的注释)。
                    # 拿它当引用数,会在零绑定的回答上写出「引用 10 处证据」。
                    f"已生成答案，引用 {len(anchors)} 处证据"
                    if answer else "答案合成未产出内容"
                ),
                detail=_synthesis_step_detail(
                    citations=len(citations), anchors=anchors,
                    evidence_level=evidence_level, counts=reasoning_counts,
                    enumerated_collections=len(enumerations),
                    enumeration_block_dropped=enumeration_block_dropped,
                    document_read_block_dropped=document_read_block_dropped,
                    outline_planned=outline_planned, sectioned=sectioned,
                    outline_fallback=outline_fallback,
                    outline_skipped=outline_skipped, outline_bypassed=_outline_bypass_reason(report, outline_slices),
                ),
                duration_ms=round((time.perf_counter() - synthesis_started) * 1000),
            )
            trace.append(synthesis_step)
            if on_trace:
                on_trace(synthesis_step)

    def _draft_conclusion(self, run: _ReasoningDraftRun) -> None:
        """The conclusion and ``llm_mode``, then the disclosure that leads them:
        the completeness warning (unless a complete checklist answers it) or the
        Knowhow coverage line."""
        top_hits, intent_contract = run.top_hits, run.intent_contract
        completeness_unavailable = run.completeness_unavailable
        structured_batch = run.structured_batch
        answer = run.ready("answer")
        grounded = run.ready("grounded")
        synth_failed = run.ready("synth_failed")
        typed_collection_result_sets = run.ready("typed_collection_result_sets")
        if answer and not synth_failed:
            conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
            llm_mode = "grounded" if grounded else "ungrounded"
        elif synth_failed:
            # 诚实降级:检索成功但答案合成未产出内容 —— 绝不冒充成 "Found N objects"
            # (那读起来像"成功但偷懒")。如实说明并保留下方证据(related_knowledge/citations)。
            llm_mode = "synthesis_failed"
            conclusion = (
                f"已检索到 {len(top_hits)} 条相关证据,但本次答案合成未产出内容。"
                "请重试该问题;下方为已检索到的证据。"
                if top_hits else
                "本次答案合成未产出内容,请重试该问题。")
            if answer:  # 只有论文表(确定性、引用已登记)会在合成失败时留下正文
                answer = f"{conclusion}\n\n{answer}"
                conclusion = _MARKER_GROUP_RE.sub("", answer).strip()
        else:
            llm_mode = "deterministic"
            conclusion = _NO_RETRIEVAL_EVIDENCE_MESSAGE

        # 抑制免责声明须是确定性规则,不是"有任何一张卡就消音"。四个条件
        # 全部成立才抑制,任一不成立就保留警告——方向是**宁可多警告**:
        # 多一句免责最多显得啰嗦,少一句就是把「相关性抽样」说成了「全部」。
        #
        # ①result_scope=="aggregate"(如"库里有多少种公式"这类去重/种类计数)
        # 从不抑制——枚举工具只会精确统计"表里的物理条目数",证明不了模型
        # 自己在归并去重后的种类数,红线要求这类问题必须回退到相关性检索并
        # 保留警告,哪怕模型顺手枚举出了一张卡。
        # ②意图合同里带**谓词**(约束/排除项/前提)时同样不抑制。清单卡的
        # coverage 只证明「某个物理集合被完整走了一遍」,证明不了那个集合就是
        # 用户要的那个子集——模型完全可能枚举了无关的 kind、或者把带条件的
        # 请求做成了不过滤的全集(codex 第 1 轮 P1-2)。这里刻意**不做**语义
        # 匹配(「这张卡是否覆盖了这条约束」没有确定性判据,做出来的只会是
        # 又一个说不清对错的启发式),而是按合同字段是否为空一刀切。
        #   assumptions(前提)也算在内:一条「只统计 2023 年之后的」与一条
        #   「假定用户指当前笔记本」在字段层面长得一模一样,区分它们需要的
        #   正是上面刚否掉的语义匹配。宁可在有前提时多留一句免责。
        # ③卡有但 returned_total==0(例如枚举了一个空集合)不算「已产出清单
        # 结果」——0 条清单不能替这道题的「全部结果」背书。
        # ④而且必须至少有一张 complete=True 的卡:部分清单自己的 partial
        # 徽章只说明「这张卡没列完」,承担不了「你要的那种请求本产品还不
        # 支持完整枚举」这句披露。
        #
        # 四条都过时,每张卡自己的 coverage 徽章已经承担「完整/部分」的披露,
        # 再前置一句免责反而会让「明明列出了公式清单」的回答开头像在道歉。
        #
        # 另一条独立的抑制:回答带着论文表与它的覆盖说明时,覆盖说明就是这份
        # 回答唯一的完整性声明(有缺口时它自己写「可能还有更多」,没做文档级
        # 检索时写「不代表已列全」),不再在表后追加一句口径不同的免责(T7 复跑 B)。
        has_complete_collection_result = any(
            row.coverage.complete and row.coverage.returned_total > 0
            for row in typed_collection_result_sets
        )
        has_scoping_predicate = bool(
            intent_contract.constraints
            or intent_contract.excluded_topics
            or intent_contract.assumptions
        )
        suppress_completeness_warning = (
            intent_contract.result_scope != "aggregate"
            and not has_scoping_predicate
            and has_complete_collection_result
        ) or coverage_line_rendered(answer, run.ready("paper_evidence"))
        warning = ""
        if completeness_unavailable and not suppress_completeness_warning:
            warning = "本次回答未验证完整性，不能视为全部结果。"
            conclusion = f"{conclusion}\n\n{warning}"
            answer = f"{answer}\n\n> {warning}" if answer else warning
        elif structured_batch is not None:
            enumeration_line = (
                f"Knowhow 枚举：{structured_batch.returned_rows}/"
                f"{structured_batch.known_total_rows} 行，"
                f"{'完整' if structured_batch.complete else '部分'}。"
            )
            analysis_line = (
                "分析未运行。"
                if structured_batch.synthesis_complete is None else
                f"分析覆盖：{structured_batch.synthesis_rows}/"
                f"{structured_batch.known_total_rows} 行，"
                f"{'完整' if structured_batch.synthesis_complete else '部分'}。"
            )
            coverage_line = f"{enumeration_line} {analysis_line}"
            conclusion = f"{coverage_line}\n\n{conclusion}"
            answer = f"> {coverage_line}\n\n{answer}" if answer else coverage_line
        run.answer, run.conclusion = answer, conclusion
        run.llm_mode, run.warning = llm_mode, warning

    def _draft_ask_response(self, run: _ReasoningDraftRun) -> AskResponse:
        """The finished ``AskResponse`` (``mode`` included)."""
        conversation_id, research_question = run.conversation_id, run.research_question
        trace, intent_contract = run.trace, run.intent_contract
        retrieval_effort, report = run.retrieval_effort, run.report
        structured_batch, spreadsheet_results = run.structured_batch, run.spreadsheet_results
        no_usable_kg = run.no_usable_kg
        conclusion = run.ready("conclusion")
        answer = run.ready("answer")
        warning = run.ready("warning")
        grounded = run.ready("grounded")
        evidence_level = run.ready("evidence_level")
        anchors = run.ready("anchors")
        related_knowledge = run.ready("related_knowledge")
        citations = run.ready("citations")
        llm_mode = run.ready("llm_mode")
        top_relevance = run.ready("top_relevance")
        typed_collection_result_sets = run.ready("typed_collection_result_sets")
        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer,
            completeness_notice=warning, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors,
            related_knowledge=related_knowledge, citations=citations,
            llm_mode=llm_mode, conversation_id=conversation_id,
            retrieval_query=research_question, top_relevance=top_relevance,
            reasoning_trace=trace or None,
            intent=intent_contract,
            retrieval_effort=retrieval_effort,
            result_sets=self._reasoning_result_sets(
                structured_batch, typed_collection_result_sets, spreadsheet_results
            ),
            result_coverage=structured_batch.coverage() if structured_batch else None,
            # 走到这里说明这一轮真的跑了检索与作答,但「本笔记本还没有知识
            # 图谱」这件事没有因此变成假的:旗标继续如实上报,前端的建图提示
            # 与答案并存。它只是不再是一道闸。
            kg_required=no_usable_kg, linked_report=report.receipt() if report else None,
        )

        response.mode = "reasoning"
        return response
