"""The reasoning agent's three paper tools (spec 2026-10-28 §7.1): pure parts.

``find_sources`` ranks the run's documents about a topic (``SourceFinder``),
``get_paper_cards`` reads/lazily generates their cached key-point cards
(``PaperCardService``) and ``extract_from_sources`` extracts ad-hoc fields per
document (``PaperExtractionService``).  ``answer_layout`` (``prose`` /
``paper_table``) is the reflect top-level field that asks synthesis for the
deterministic paper table.

This module holds what has no I/O: the action ids and enums, the run-local
handle book (``p1``, ``p2`` ... issued by ``find_sources``), argument parsing
off the reflect payload, source-token resolution against the run ceiling, the
per-run outcome records handed to synthesis on ``ReasoningResult``, and the
ledger text fed back to the next reflect turn.  The executors live on
``ReasoningRetriever`` (they read and write the run state).

Every argument is a string, a string enum or a list of strings -- never a
boolean (``reasoning_actions`` discipline).  A source token is a handle this
run issued or a raw ``source_id``; either is checked against the run's source
ceiling (the ``enumerate_sources`` source set) before any service sees it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.core.model_values import as_text, as_text_list

FIND_SOURCES_ACTION = "find_sources"
GET_PAPER_CARDS_ACTION = "get_paper_cards"
EXTRACT_FROM_SOURCES_ACTION = "extract_from_sources"
PAPER_TOOL_ACTIONS = (
    FIND_SOURCES_ACTION, GET_PAPER_CARDS_ACTION, EXTRACT_FROM_SOURCES_ACTION,
)

ANSWER_LAYOUT_PROSE = "prose"
ANSWER_LAYOUT_PAPER_TABLE = "paper_table"
ANSWER_LAYOUTS = (ANSWER_LAYOUT_PROSE, ANSWER_LAYOUT_PAPER_TABLE)
#: ``ReasoningResult.answer_layout_fallback`` when ``paper_table`` was asked
#: for but the run holds no card and no extracted value.
ANSWER_LAYOUT_NO_RESULTS = "no_paper_results"

FIND_SOURCES_KINDS = ("papers", "all")
FIND_SOURCES_KIND_DEFAULT = "papers"
FIND_SOURCES_SCOPE_DEFAULT = "all"

#: Raw list entries read off one payload before any bound applies (a model
#: pasting hundreds of ids must not cost a quadratic walk); the per-call caps
#: are enforced by the executors and reported in the ledger.
_LIST_READ_MAX = 64
#: How many items of one ``find_sources`` page the ledger names.
LEDGER_FIND_ITEMS = 10
#: How many ``find_sources`` calls the ledger replays (the most recent ones).
LEDGER_FIND_CALLS = 3
#: Executor notes kept for the ledger (the most recent ones).
LEDGER_LOG_LINES = 6
_TITLE_CHARS = 60
_TOKEN_ECHO_CHARS = 64

_HANDLE_RE = re.compile(r"^p(\d{1,4})$")


@dataclass(frozen=True)
class ReasoningPaperTools:
    """The three paper services a run may use (``None`` = that tool absent).

    Seated only on the Ask path (``AskService._build_reasoning_retriever``);
    report sections and knowhow completion build their retriever without it,
    so their runs are byte-identical to before the tools existed."""

    finder: Any = None
    cards: Any = None
    extraction: Any = None


@dataclass(frozen=True)
class HandleEntry:
    handle: str
    notebook_id: str
    source_id: str
    title: str


class PaperHandles:
    """Run-local handles ``p1``, ``p2`` ... one per source, stable for the run:
    a source found again by a later ``find_sources`` call keeps its handle."""

    def __init__(self) -> None:
        self._by_handle: Dict[str, HandleEntry] = {}
        self._by_source: Dict[str, HandleEntry] = {}

    def issue(self, notebook_id: str, source_id: str, title: str) -> Tuple[str, bool]:
        """``(handle, newly_issued)``."""
        held = self._by_source.get(source_id)
        if held is not None:
            return held.handle, False
        entry = HandleEntry(f"p{len(self._by_handle) + 1}", notebook_id, source_id, title)
        self._by_handle[entry.handle] = entry
        self._by_source[source_id] = entry
        return entry.handle, True

    def entry(self, handle: str) -> Optional[HandleEntry]:
        return self._by_handle.get(handle)

    def for_source(self, source_id: str) -> Optional[HandleEntry]:
        return self._by_source.get(source_id)

    def __len__(self) -> int:
        return len(self._by_handle)


@dataclass(frozen=True)
class ResolvedSource:
    token: str
    handle: str
    notebook_id: str
    source_id: str
    title: str

    @property
    def key(self) -> Tuple[str, str]:
        return (self.notebook_id, self.source_id)

    @property
    def label(self) -> str:
        return self.handle or self.source_id


@dataclass(frozen=True)
class RejectedSource:
    token: str
    #: ``unknown_handle`` / ``out_of_scope``.
    reason: str


def resolve_source_tokens(
    tokens: Iterable[str],
    handles: PaperHandles,
    allowed: "frozenset[Tuple[str, str]]",
) -> Tuple[List[ResolvedSource], List[RejectedSource]]:
    """Map each token (a handle this run issued, or a raw ``source_id``) to its
    ``(owning notebook, source)`` pair, ONLY within ``allowed`` (the run's
    source ceiling).  A handle is re-checked too: it was issued inside the set,
    and the check costs nothing.  Duplicates (two tokens naming one source)
    collapse to the first."""
    by_source: Dict[str, str] = {}
    for notebook_id, source_id in allowed:
        by_source.setdefault(source_id, notebook_id)
    resolved: List[ResolvedSource] = []
    rejected: List[RejectedSource] = []
    seen: set = set()
    for raw in tokens:
        token = str(raw or "").strip()
        if not token:
            continue
        entry = handles.entry(token.lower()) if _HANDLE_RE.match(token.lower()) else None
        if entry is not None:
            pair: Optional[Tuple[str, str]] = (entry.notebook_id, entry.source_id)
        elif token in by_source:
            pair = (by_source[token], token)
        else:
            # A handle-shaped token this run never issued is told apart from
            # an id outside the ceiling: the two call for different fixes.
            reason = "unknown_handle" if _HANDLE_RE.match(token.lower()) else "out_of_scope"
            rejected.append(RejectedSource(token[:_TOKEN_ECHO_CHARS], reason))
            continue
        if pair not in allowed:
            rejected.append(RejectedSource(token[:_TOKEN_ECHO_CHARS], "out_of_scope"))
            continue
        if pair in seen:
            continue
        seen.add(pair)
        known = handles.for_source(pair[1])
        resolved.append(ResolvedSource(
            token=token, handle=known.handle if known is not None else "",
            notebook_id=pair[0], source_id=pair[1],
            title=known.title if known is not None else "",
        ))
    return resolved, rejected


# ----------------------------------------------------------- run outcomes
@dataclass(frozen=True)
class FoundSourcesOutcome:
    """One ``find_sources`` call: the service request and result, and the
    handle each item of the page was given (same order as ``result.items``)."""

    request: Any
    result: Any
    handles: Tuple[str, ...]


@dataclass(frozen=True)
class PaperCardOutcome:
    """The run's latest card answer for one source (``result`` is the service's
    ``CardResult``; ``card`` non-empty only for ``ready`` / ``generated``)."""

    handle: str
    title: str
    result: Any


@dataclass(frozen=True)
class PaperExtractionItem:
    handle: str
    title: str
    result: Any


@dataclass(frozen=True)
class PaperExtractionOutcome:
    """One ``extract_from_sources`` call: the normalized fields and each
    source's ``ExtractionResult`` in request order."""

    fields: Tuple[str, ...]
    items: Tuple[PaperExtractionItem, ...]


def card_anchor_ids(card: Mapping[str, Any]) -> List[str]:
    """Every element id a verified card cites, deduplicated in field order
    (``tldr`` → ``problem`` → ``method`` → contributions → metrics → datasets)."""
    ids: List[str] = []

    def take(values: Any) -> None:
        for value in values if isinstance(values, list) else [values]:
            text = str(value or "")
            if text and text not in ids:
                ids.append(text)

    for key in ("tldr", "problem", "method"):
        entry = card.get(key)
        if isinstance(entry, Mapping):
            take(entry.get("anchors"))
    for key, anchor in (("contributions", "anchors"), ("metrics", "anchor"), ("datasets", "anchors")):
        for entry in card.get(key) or []:
            if isinstance(entry, Mapping):
                take(entry.get(anchor))
    return ids


def card_has_content(outcome: PaperCardOutcome) -> bool:
    return outcome.result.status in ("ready", "generated") and bool(outcome.result.card)


def extraction_value_count(outcome: PaperExtractionOutcome) -> int:
    return sum(
        1 for item in outcome.items for value in item.result.fields if value.value
    )


def resolve_answer_layout(requested: str, has_results: bool) -> Tuple[str, str]:
    """``(layout, fallback_reason)``: ``paper_table`` only when the run holds a
    card or an extracted value; anything else is ``prose``."""
    if requested != ANSWER_LAYOUT_PAPER_TABLE:
        return ANSWER_LAYOUT_PROSE, ""
    if has_results:
        return ANSWER_LAYOUT_PAPER_TABLE, ""
    return ANSWER_LAYOUT_PROSE, ANSWER_LAYOUT_NO_RESULTS


# ------------------------------------------------------------- parsing
def _text_list(value: Any) -> Tuple[str, ...]:
    """A list of strings off the payload (a lone string reads as one item;
    non-string entries are dropped), whitespace-trimmed, empties dropped,
    order kept, bounded at ``_LIST_READ_MAX`` raw entries."""
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = as_text_list(value[:_LIST_READ_MAX])
    else:
        items = []
    return tuple(text for text in (str(item).strip() for item in items) if text)


def _year_text(value: Any) -> str:
    """A year as typed: the schema shows a string, but a model writing the
    number ``2020`` means the same thing (an acceptable deviation)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return as_text(value)


def parse_paper_tool_arguments(
    decision: Any,
    data: Mapping[str, Any],
    *,
    find_sources: bool,
    paper_cards: bool,
    extract: bool,
    enumerate_scope: bool,
    answer_layout: bool = False,
) -> None:
    """Fill the paper-tool fields of a ``ReflectDecision`` from the payload.

    A closed tool's branch is not even read (the action is not in the
    whitelist either).  Enum values outside their set fall back to the
    default, never raise; free text is NOT cut here -- the fail-closed
    2000-character gate must see it, and the services refuse over-long input
    themselves (``FindSourcesError`` / ``ExtractionRequestError``)."""
    request = data.get(FIND_SOURCES_ACTION)
    if find_sources and isinstance(request, Mapping):
        decision.find_sources_query = as_text(request.get("query"))
        decision.find_sources_terms = _text_list(request.get("terms"))
        decision.find_sources_year_from = _year_text(request.get("year_from"))
        decision.find_sources_year_to = _year_text(request.get("year_to"))
        decision.find_sources_venue = as_text(request.get("venue"))
        decision.find_sources_author = as_text(request.get("author"))
        kind = as_text(request.get("kind")).lower()
        decision.find_sources_kind = (
            kind if kind in FIND_SOURCES_KINDS else FIND_SOURCES_KIND_DEFAULT
        )
        # Same scope knob as the document roster: a PEER run has no current
        # notebook, so the field is not offered there and not read either.
        scope = as_text(request.get("scope")).lower() if enumerate_scope else ""
        decision.find_sources_scope = (
            scope if scope in ("all", "current_notebook") else FIND_SOURCES_SCOPE_DEFAULT
        )
        decision.find_sources_cursor = as_text(request.get("cursor"))
    request = data.get(GET_PAPER_CARDS_ACTION)
    if paper_cards and isinstance(request, Mapping):
        decision.paper_card_sources = _text_list(request.get("sources"))
    request = data.get(EXTRACT_FROM_SOURCES_ACTION)
    if extract and isinstance(request, Mapping):
        decision.extract_sources = _text_list(request.get("sources"))
        decision.extract_fields = _text_list(request.get("fields"))
    if answer_layout or paper_cards or extract:
        decision.answer_layout = answer_layout_value(data)


def answer_layout_value(data: Mapping[str, Any]) -> str:
    """The layout this payload writes, ``""`` when it writes none (or an
    unknown value): the run keeps the last value actually written."""
    layout = as_text(data.get("answer_layout")).lower()
    return layout if layout in ANSWER_LAYOUTS else ""


def cap_find_terms(terms: Sequence[str], limit: int) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(kept, dropped)``: the first ``limit`` distinct terms and the rest.

    The finder refuses more than its term cap outright (it is also the MCP
    tool's validation); the Agent's call is an acceptable deviation, so the
    executor keeps the first ``limit`` terms -- the model lists the most
    literal ones first -- and names the rest in the ledger instead of
    discarding the whole call."""
    distinct = tuple(dict.fromkeys(terms))
    return distinct[:limit], distinct[limit:]


def find_sources_has_criteria(decision: Any) -> bool:
    return bool(
        decision.find_sources_query or decision.find_sources_terms
        or decision.find_sources_venue or decision.find_sources_author
        or decision.find_sources_year_from or decision.find_sources_year_to
        or decision.find_sources_cursor
    )


def missing_paper_tool_argument(decision: Any, action: str) -> str:
    """The required argument a paper-tool call lacks (``""`` = complete)."""
    if action == FIND_SOURCES_ACTION and not find_sources_has_criteria(decision):
        return "query"
    if action == GET_PAPER_CARDS_ACTION and not decision.paper_card_sources:
        return "sources"
    if action == EXTRACT_FROM_SOURCES_ACTION:
        if not decision.extract_sources:
            return "sources"
        if not decision.extract_fields:
            return "fields"
    return ""


def paper_tool_text_fields(decision: Any) -> Tuple[str, ...]:
    """Every free-text value of the paper tools, for the fail-closed
    2000-character gate."""
    return (
        decision.find_sources_query,
        decision.find_sources_year_from,
        decision.find_sources_year_to,
        decision.find_sources_venue,
        decision.find_sources_author,
        decision.find_sources_cursor,
        *decision.find_sources_terms,
        *decision.paper_card_sources,
        *decision.extract_sources,
        *decision.extract_fields,
    )


def year_value(raw: str) -> Any:
    """``""`` → ``None``; anything else is handed to the service as typed (it
    refuses a non-integer with ``FindSourcesError``)."""
    return raw if raw else None


# -------------------------------------------------------------- ledger
def clip(text: Any, limit: int = _TITLE_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


_TIER_WORDS = {"strong": "强相关", "related": "相关"}
_CARD_STATUS_WORDS = {
    "ready": "卡片已有", "missing": "卡片未生成", "not_paper": "非论文",
    "failed": "卡片曾失败", "meta_pending": "元数据未抽取",
}
_SKIP_REASON_WORDS = {
    "timeout": "超时", "saturated": "繁忙", "queue_deadline": "排队超时",
    "unavailable": "不可用",
}


def _skip_reason_word(reason: str) -> str:
    """``timeout`` → 读取超时; ``semantic:timeout`` → 语义检索超时 (the
    finder's prefix for a peer whose metadata leg ran but semantic leg did not)."""
    semantic = reason.startswith("semantic:")
    word = _SKIP_REASON_WORDS.get(reason.removeprefix("semantic:"), "不可用")
    return ("语义检索" if semantic else "读取") + word


def _find_item_line(handle: str, item: Any) -> str:
    facts = [str(item.pub_year)] if item.pub_year else []
    if item.venue:
        facts.append(clip(item.venue, 24))
    facts.append(_TIER_WORDS.get(item.tier, item.tier))
    facts.append(_CARD_STATUS_WORDS.get(item.card_status, item.card_status))
    return f"{handle}《{clip(item.title)}》({'·'.join(facts)})"


def find_coverage_gaps(result: Any) -> List[str]:
    """Why this result cannot prove the list complete (empty = it can)."""
    gaps: List[str] = []
    if result.semantic_recall_capped:
        gaps.append("语义召回已触顶,可能还有更多相关文档没有被召回")
    if result.meta_pending_excluded:
        gaps.append(
            f"有 {result.meta_pending_excluded} 篇论文的元数据尚未抽取,无法判断是否满足"
            "年份/会议/作者过滤,已被排除")
    if result.meta_pending_unchecked:
        gaps.append(
            f"有 {result.meta_pending_unchecked} 篇论文的元数据尚未抽取,只按标题/摘要检查了"
            "字面词(论文标题/关键词/作者没查到)")
    if result.skipped_libraries:
        reasons = "、".join(sorted({
            _skip_reason_word(reason) for reason in result.skipped_libraries.values()
        }))
        gaps.append(f"{len(result.skipped_libraries)} 个参考库本次没有读全({reasons})")
    if not gaps and not result.lexical_complete:
        gaps.append("只做了语义检索、没有给 terms 做字面匹配")
    return gaps


def _find_lines(finds: Sequence[FoundSourcesOutcome], total_calls: int) -> List[str]:
    lines = []
    first = total_calls - len(finds) + 1
    for number, outcome in enumerate(finds, first):
        result = outcome.result
        page = "(翻页)" if outcome.request.cursor else ""
        shown = "、".join(
            _find_item_line(handle, item)
            for handle, item in list(zip(outcome.handles, result.items))[:LEDGER_FIND_ITEMS]
        )
        rest = len(result.items) - LEDGER_FIND_ITEMS
        line = (
            f"find_sources 第 {number} 次{page}:强相关 {result.strong} 篇、相关 "
            f"{result.related} 篇(过滤后的范围共 {result.filtered_scope_total} 篇)"
            + (f";本页:{shown}" if shown else ";本页没有命中")
            + (f";本页另有 {rest} 篇({outcome.handles[LEDGER_FIND_ITEMS]}"
               f"–{outcome.handles[-1]})" if rest > 0 else "")
        )
        if result.next_cursor:
            line += (
                f";还有下一页:把 find_sources.cursor 设为「{result.next_cursor}」并保持其余参数"
                "不变再调一次(翻页也计入次数)"
            )
        gaps = find_coverage_gaps(result)
        if gaps:
            line += (
                ";覆盖不完整:" + ";".join(gaps)
                + "——回答时必须如实说明可能有遗漏,不得声称已列全"
            )
        lines.append(line)
    return lines


_CARD_RESULT_WORDS = {
    "ready": "已有卡片", "generated": "新生成卡片",
    "not_paper": "不是论文(要点改用 extract_from_sources)",
    "meta_pending": "元数据未就绪,暂无卡片",
}
_CARD_REASON_WORDS = {
    "cooldown": "近期生成失败,冷却中",
    "generate_limit": "推迟:本次生成额度已用完,可再调一次",
    "model_unavailable": "推迟:模型暂不可用,可稍后再调",
    "in_flight": "推迟:正被别的请求生成",
    "source_changed": "推迟:文档刚重新解析",
    "out_of_scope": "不在本次检索范围内",
    "missing": "来源不存在",
    "disabled": "卡片生成已关闭",
    "mirrored": "只读同步副本,不生成卡片",
}


def _card_word(result: Any) -> str:
    if result.reason in _CARD_REASON_WORDS:
        return _CARD_REASON_WORDS[result.reason]
    if result.status in _CARD_RESULT_WORDS:
        return _CARD_RESULT_WORDS[result.status]
    if result.status == "deferred":
        return "推迟,可再调一次"
    if result.status == "not_found":
        return "来源不存在"
    return "生成失败"


def card_fields_note(card: Mapping[str, Any]) -> str:
    """Which parts a card holds, e.g. ``tldr、指标 3 条、贡献 2 条``."""
    parts = [name for name, key in (("tldr", "tldr"), ("问题", "problem"), ("方法", "method"))
             if isinstance(card.get(key), Mapping) and card[key].get("text")]
    for name, key in (("指标", "metrics"), ("贡献", "contributions"), ("数据集", "datasets")):
        count = len(card.get(key) or [])
        if count:
            parts.append(f"{name} {count} 条")
    return "、".join(parts)


def _card_lines(cards: Sequence[PaperCardOutcome]) -> List[str]:
    entries = []
    for outcome in cards:
        word = _card_word(outcome.result)
        if card_has_content(outcome):
            held = card_fields_note(outcome.result.card)
            word += f"({held})" if held else ""
        label = outcome.handle or outcome.result.source_id
        entries.append(f"{label}" + (f"《{clip(outcome.title, 30)}》" if outcome.title else "") + f":{word}")
    return ["get_paper_cards 结果:" + ";".join(entries)] if entries else []


_MISSING_WORDS = {
    "not_stated": "原文未写", "invalid_anchor": "引用无效已丢弃",
    "value_not_in_anchor": "数值未在原文出现已丢弃", "ambiguous_value": "数值有歧义已丢弃",
    "unmapped_anchor": "引用无法定位已丢弃",
}
_EXTRACT_STATUS_WORDS = {
    "no_text": "没有可读原文", "failed": "抽取失败", "not_found": "不在范围内或不存在",
}


def _extraction_lines(extractions: Sequence[PaperExtractionOutcome]) -> List[str]:
    lines = []
    for outcome in extractions:
        fields = "".join(f"[{index}]{clip(text, 30)}" for index, text in enumerate(outcome.fields))
        entries = []
        for item in outcome.items:
            result = item.result
            label = item.handle or result.source_id
            if result.status == "extracted":
                cells = " ".join(
                    f"[{value.field_index}]" + (
                        "有值" if value.value else _MISSING_WORDS.get(value.missing, "未找到"))
                    for value in result.fields
                )
            elif result.status == "deferred":
                cells = "推迟(额度已用完)" if result.reason == "limit" else "推迟(模型暂不可用)"
            else:
                cells = _EXTRACT_STATUS_WORDS.get(result.status, result.status)
            entries.append(f"{label} {cells}")
        lines.append(f"extract_from_sources 字段 {fields}:" + ";".join(entries))
    return lines


def unexpanded_strong(
    finds: Sequence[FoundSourcesOutcome], cards: Mapping[Tuple[str, str], PaperCardOutcome],
) -> List[str]:
    """Handles of ``strong`` papers found this run whose card is not held yet."""
    out: List[str] = []
    for outcome in finds:
        for handle, item in zip(outcome.handles, outcome.result.items):
            held = cards.get((item.notebook_id, item.source_id))
            if (item.tier == "strong" and item.card_status != "not_paper"
                    and (held is None or not card_has_content(held))
                    and handle not in out):
                out.append(handle)
    return out


def paper_tools_note(
    *,
    finds: Sequence[FoundSourcesOutcome],
    find_calls: int,
    cards: Mapping[Tuple[str, str], PaperCardOutcome],
    extractions: Sequence[PaperExtractionOutcome],
    log: Sequence[str],
    remaining: Mapping[str, int],
    layout_requested: str,
    has_results: bool,
) -> str:
    """The paper-tools ledger appended to the reflect candidates summary.

    Empty (no byte) until a paper tool ran, was refused, or ``answer_layout``
    was written -- so a run that never touches them renders exactly what it
    did before the tools existed."""
    lines = _find_lines(list(finds)[-LEDGER_FIND_CALLS:], find_calls)
    lines += _card_lines(list(cards.values()))
    lines += _extraction_lines(extractions)
    lines += [f"未执行:{entry}" for entry in list(log)[-LEDGER_LOG_LINES:]]
    pending = unexpanded_strong(finds, cards)
    if pending:
        shown = "、".join(pending[:LEDGER_FIND_ITEMS])
        lines.append(
            f"已找到但还没取卡片的强相关文档:{shown}"
            + (f" 等 {len(pending)} 篇" if len(pending) > LEDGER_FIND_ITEMS else "")
            + ";没有展开的论文在回答里只能列标题,不得声称已逐篇介绍")
    if layout_requested == ANSWER_LAYOUT_PAPER_TABLE:
        lines.append(
            "已选 answer_layout=paper_table:最终回答里的论文表由系统按卡片与抽取结果生成"
            if has_results else
            "answer_layout=paper_table 目前不会生效:本次运行还没有拿到论文卡片或抽取结果,"
            "请先 get_paper_cards / extract_from_sources,否则按正文(prose)作答")
    if not lines:
        return ""
    budget = "、".join(f"{name} {count}" for name, count in remaining.items())
    if budget:
        lines.append(f"本次运行剩余额度:{budget}")
    return "\n\n（论文工具记录:\n- " + "\n- ".join(lines) + "）"


__all__ = [
    "ANSWER_LAYOUTS",
    "ANSWER_LAYOUT_NO_RESULTS",
    "ANSWER_LAYOUT_PAPER_TABLE",
    "ANSWER_LAYOUT_PROSE",
    "EXTRACT_FROM_SOURCES_ACTION",
    "FIND_SOURCES_ACTION",
    "FIND_SOURCES_KINDS",
    "FoundSourcesOutcome",
    "GET_PAPER_CARDS_ACTION",
    "PAPER_TOOL_ACTIONS",
    "PaperCardOutcome",
    "PaperExtractionItem",
    "PaperExtractionOutcome",
    "PaperHandles",
    "ReasoningPaperTools",
    "RejectedSource",
    "ResolvedSource",
    "answer_layout_value",
    "cap_find_terms",
    "card_anchor_ids",
    "card_has_content",
    "clip",
    "extraction_value_count",
    "find_coverage_gaps",
    "missing_paper_tool_argument",
    "paper_tool_text_fields",
    "paper_tools_note",
    "parse_paper_tool_arguments",
    "resolve_answer_layout",
    "resolve_source_tokens",
]
