"""Bounded document-order evidence for a source already resolved in scope."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable

from app.domain.citation_origin import foreign_notebook_id
from app.domain.source_positions import MAX_POSITIONS_PER_READ
from app.models.ask import Citation
from app.repositories.ports import RepositoryDatabasePort, SourceStorePort
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.collection_enumeration import SourceItem
from app.services.context_spans import entry_lines
from app.services.evidence_attestation import attest_read
from app.services.paper_cards import (
    CardInputElement,
    cap_number_safe,
    card_element_cost,
    render_card_input,
)
from app.services.source_scope import citation_active_id


@dataclass(frozen=True)
class SourceOverview:
    context_block: str
    id_map: dict[str, dict]
    citations: list[Citation]
    coverage_note: str


def _safe_text(text: str) -> str:
    # Source content cannot manufacture handles belonging to this context.
    return re.sub(r"\[\s*k\d+\s*\]", lambda m: "［" + m[0][1:-1] + "］", text)


def _attest_cited(elements: list, citations: list[Citation]) -> None:
    """PR-D D3: register every element this overview cites, from the FULL text.

    ``element.text`` is the stored body the page read handed back; the citation
    only quotes a budget-sized prefix of it, and the terminal check re-hashes
    the whole stored row, so the snapshot is taken from the body, never the
    quote. Zero extra reads; a no-op outside a global run.
    """
    cited = {citation.element_id for citation in citations}
    attest_read("document_overview", {
        element.id: (element.source_id, element.text)
        for element in elements if element.id in cited
    })


def _position_batches(positions: list[int]) -> list[list[int]]:
    """Split the positions into reads of at most ``MAX_POSITIONS_PER_READ``.

    ``document_overview_max_elements`` has no upper bound, so a long list is
    split rather than truncated. The offset sum needs no split of its own:
    the PostgreSQL adapter answers a read with ONE walk up to its largest
    position (``span``) when the offsets (``sum(p + 1)``) reach
    ``max(2 * span, 2048)``, and with per-offset walks when they stay below
    it -- so a read walks at most ``max(2 * span, 2048)`` index rows. The
    read that holds the last position walks the document however the list
    is cut, as the single-offset read of it did; more reads would only
    re-walk prefixes.
    """
    return [
        positions[start:start + MAX_POSITIONS_PER_READ]
        for start in range(0, len(positions), MAX_POSITIONS_PER_READ)
    ]


def _read_positions(
    sources: SourceStorePort,
    source_id: str,
    positions: list[int],
    total: int,
    cancel_event: CancelEvent,
) -> tuple[dict[int, list], bool]:
    """``{position: [element]}`` for every position read, and whether every
    read saw the first page's ``total``.

    Each batch is one adapter call whose count and elements share a snapshot;
    the total is compared batch by batch, so a change between the first page
    and any batch -- or between two batches -- still voids the overview the way
    a change between two single-offset reads did. A source deleted in between
    raises ``KeyError`` from the adapter, as before.
    """
    read_at: dict[int, list] = {}
    stable_count = True
    for batch in _position_batches(positions):
        raise_if_cancelled(cancel_event)
        page = sources.source_elements_page(source_id, positions=batch)
        stable_count = stable_count and page.total_count == total
        answered = [position for position in batch if position < page.total_count]
        read_at.update((position, [element]) for position, element in zip(answered, page.items))
    return read_at, stable_count


def supplemental_excerpt_header(key: str, title: str) -> str:
    """Build the wrapper header for a directory row's supplemental original excerpts.

    ``key`` is the handle the document's own directory row already occupies in
    the prompt (``k5001`` etc.), so "the row you listed" and "the text I sampled
    out of it" are visibly the same document. An EMPTY ``key`` is the honest
    degradation for a sample whose row never reached the preview (its budget
    squeezed it out, or the enumeration block was dropped wholesale): the same
    sentence, minus the claim that some listed row is this document. It never
    invents a handle — a ``kN`` the evidence map does not carry would bind to
    nothing and teach the model to cite a key that cannot be resolved.

    Both shapes escape the title identically, which is why they live in one
    function: the escaping is the security-relevant half (document text must not
    be able to manufacture a handle belonging to this context), and a second
    hand-copied renderer elsewhere is exactly how one of the two loses it.
    """
    subject = f"document {key}" if key else "the document named here"
    return (
        f"\n\n[Supplemental original excerpts for {subject}; bounded sampling, "
        "not a full reading] "
        + json.dumps(title, ensure_ascii=False).replace("[", "［").replace("]", "］")
        + "\n"
    )


def prepare_source_overview(
    sources: SourceStorePort,
    source_item: SourceItem,
    budget_chars: int,
    max_elements: int,
    cancel_event: CancelEvent = None,
    *,
    active_notebook_id: str,
    generation_reader: Callable[[str], str] | None = None,
    key_offset: int = 0,
    coverage: str = "spread",
) -> SourceOverview:
    """Read evenly spaced elements, including the last source-detail position.

    The caller owns source authorization and title resolution. Every read is a
    single bounded page; there is no full-source hydration. Without a generation
    witness this function never certifies an unchanged complete document.

    ``active_notebook_id`` is required (no default) on purpose: the overview
    source may live in a mounted library, and ``Citation.notebook_id`` /
    the id_map ``notebook_id`` must be non-empty only for such cross-notebook
    evidence.  A defaulted argument would let a caller forget it and echo the
    active notebook's own id back, which is exactly the badge bug the A1 guard
    (``tests/test_citation_notebook_id_guard.py``) exists to prevent.  In PEER
    mode (``source_scope.citation_active_id``) there is no current library at
    all, so that id resolves to ``""`` and every enumerated source keeps its
    real owner, the nominal active's included.  Normalised once, on entry:
    citation origin is this parameter's ONLY use here -- authorization and
    title resolution belong to the caller, as stated above.

    ``coverage`` selects the sampling shape: ``"spread"`` (the default) takes
    evenly spaced positions that always include the last element, since a
    document's conclusion often carries its verdict. ``"opening"`` instead
    reads only the first ``max_elements`` elements -- the cheapest useful
    reading for "what is this document about" when the document has no
    stored summary at all. Any other string is treated as ``"spread"``
    (permissive, never raises). Both existing callers omit this argument, so
    their output is byte-for-byte unchanged.
    """
    raise_if_cancelled(cancel_event)
    active_notebook_id = citation_active_id(active_notebook_id)
    if budget_chars <= 0 or max_elements <= 0:
        return SourceOverview("", {}, [], "本次没有可用的原文概述预算，请增加预算后重试。")
    source_id = source_item.source_id
    before = generation_reader(source_id) if generation_reader else None
    first = sources.source_elements_page(source_id, offset=0, limit=1)
    total = first.total_count
    count = min(total, max_elements)
    if coverage == "opening":
        offsets = list(range(count))
    elif count > 1:
        offsets = [index * (total - 1) // (count - 1) for index in range(count)]
    elif count == 1:
        # 单元素的 spread 读**末**元素,不是首元素(codex #724 R3):spread 的承诺是
        # 「必含文档最后一个位置」,而 overview 档的按篇取样份额常常只够读一个——
        # 读首元素等于把 spread 变成 opening,模型选 spread 想要的结论一段拿不到。
        # 单元素文档两者相同。
        offsets = [total - 1]
    else:
        offsets = []
    elements = []
    seen = set()
    read_at, stable_count = _read_positions(
        sources, source_id, [offset for offset in offsets if offset], total, cancel_event,
    )
    read_at[0] = first.items
    for offset in offsets:
        for element in read_at.get(offset, ()):
            if element.source_id == source_id and element.id not in seen:
                elements.append(element)
                seen.add(element.id)
    raise_if_cancelled(cancel_event)
    if generation_reader and before != generation_reader(source_id):
        return SourceOverview("", {}, [], "读取期间文档重新解析，无法确认原文一致性，请重试。")
    if not stable_count:
        return SourceOverview("", {}, [], "读取期间文档内容发生变化，请重试。")

    lines: list[str] = []
    id_map: dict[str, dict] = {}
    citations: list[Citation] = []
    used = 0
    complete_text = True
    bodies = [
        _safe_text(str(element.metadata.get("section_path") or element.location_label) + "\n" + element.text)
        for element in elements
    ]
    whole_fits = sum(
        len(f"k{key_offset + index + 1}: " + json.dumps(body, ensure_ascii=False)) + 1
        for index, body in enumerate(bodies)
    ) <= budget_chars
    for index, element in enumerate(elements):
        raise_if_cancelled(cancel_event)
        # Reserve a fair share for every sampled position; the opening cannot
        # exhaust the budget before a late-document conclusion reaches synthesis.
        slot = (budget_chars - used) if whole_fits else (budget_chars - used) // (len(elements) - index)
        key = f"k{key_offset + len(lines) + 1}"
        section = str(element.metadata.get("section_path") or element.location_label)
        original = element.text
        body = bodies[index]
        prefix = key + ": "
        low, high = 0, len(body)
        while low < high:
            middle = (low + high + 1) // 2
            if len(prefix + json.dumps(body[:middle], ensure_ascii=False)) + 1 <= slot:
                low = middle
            else:
                high = middle - 1
        if low <= len(_safe_text(section + "\n")):
            complete_text = False
            continue
        projected = body[:low]
        complete_text = complete_text and low == len(body)
        line = prefix + json.dumps(projected, ensure_ascii=False)
        lines.append(line)
        used += len(line) + 1
        quote = original[:len(projected) - len(_safe_text(section + "\n"))]
        id_map[key] = {
            "object_id": element.id, "object_type": "element",
            "name": section or source_item.source_title, "definition": None,
            "snippet": quote, "source_title": source_item.source_title,
            "source_id": source_id, "element_id": element.id,
            "location_label": element.location_label,
            "tier": source_item.tier,
            "notebook_id": foreign_notebook_id(source_item.notebook_id, active_notebook_id),
            "relevance": 1.0,
        }
        citations.append(Citation(
            label=source_item.source_title, source_id=source_id,
            element_id=element.id, location_label=element.location_label,
            quoted_span=quote, tier=source_item.tier,
            notebook_id=foreign_notebook_id(source_item.notebook_id, active_notebook_id),
        ))
    if not total:
        note = "这篇文档没有可读取的原文，请先完成文档解析。"
    elif not lines:
        # 文档有原文元素，但**一条都没落地**：每个元素分到的字符份额连它自己的
        # 章节面包屑都装不下（`low <= len(_safe_text(section + "\n"))` 那道闸把它
        # 整条丢掉）。极长的 section_path 配上极小的预算就是这个形状。
        #
        # 这一条必须与下面那条有界摘录分开说：那句话以「请仅依据这些原文介绍」
        # 结尾，而这里**一个字的原文都没有**——把它交给模型，就是在请模型依据
        # 一份空证据去介绍这篇文档，而那正是无依据编造的入口。两条读取通道
        # （目录补摘要与按篇取样）共享这一份措辞。
        note = (
            f"本次字符预算放不下这篇文档的任何原文元素（{total} 个元素），未能取样；"
            "请如实说明这一篇暂无依据。"
        )
    elif len(lines) == total and complete_text and generation_reader and before:
        note = f"已读取全部 {total} 个原文元素，读取期间文档解析版本未变化。"
    elif coverage == "opening":
        # opening 只读了开头(codex #724 R3):合成侧拿到的只有这一行披露,不带
        # `coverage` 字段;沿用「分布取样」的措辞会让模型把只读了开头的证据当成
        # 分布在全文的样本。要说清后面的章节**没有**取样。
        note = (
            f"本次只读取了文档开头的 {len(lines)}/{total} 个原文元素，后面的章节未取样；"
            "不代表覆盖所有章节或全文，请仅依据这些原文介绍，并明确概述范围有限。"
        )
    else:
        note = (
            f"本次提供 {len(lines)}/{total} 个原文元素的有界摘录，按来源详情顺序分布取样；"
            "不代表覆盖所有章节或全文，请仅依据这些原文介绍，并明确概述范围有限。"
        )
    _attest_cited(elements, citations)
    return SourceOverview(entry_lines(lines, empty=""), id_map, citations, note)


# ------------------------------------------------------------ paper card input
#
# 论文卡片的输入采样(规格 docs/superpowers/specs/2026-10-28-paper-cards-find-sources-design_zh.md
# §4.3):开头 40% → 表格(按文档顺序,单表截断 1500 字,最多 6 张)→ 余下按 spread
# 均匀取样(含最后一个元素)。与 ``prepare_source_overview`` 同一套分页读取与生成见证,
# 不另起读取路径;确定性、可复现。

PAPER_CARD_OPENING_SHARE = 0.4
PAPER_CARD_TABLE_MAX_CHARS = 1500
PAPER_CARD_MAX_TABLES = 6
_CARD_OPENING_PAGE = 24
_CARD_TABLE_PAGE = 12
# spread 段按「每个元素大约这么多字」决定取几个位置;实际份额按剩余预算均分。
_CARD_SPREAD_ELEMENT_CHARS = 600
_CARD_SPREAD_MAX_ELEMENTS = 32
# 截断后正文短于这个数就不放(只剩头行的元素对模型没有用)。
_CARD_MIN_TEXT_CHARS = 120


@dataclass(frozen=True)
class PaperCardSample:
    """``status``:``ok`` 有输入;``empty`` 没有可读原文;``changed`` 读取期间内容变了。
    ``basis`` 是守护这次采样的生成见证值(``generation_reader`` 在读取前给出、读取后
    复核相等),写卡必须用它,不能另读一次。"""

    status: str
    basis: str
    elements: tuple[CardInputElement, ...]
    input_block: str
    total_elements: int

    @property
    def input_chars(self) -> int:
        return len(self.input_block)


class _CardBudget:
    def __init__(self, total: int) -> None:
        self.total = total
        self.used = 0
        self.elements: list[CardInputElement] = []
        self.ids: set[str] = set()

    @property
    def remaining(self) -> int:
        return self.total - self.used

    def fit(self, element_id: str, element_type: str, location_label: str,
            text: str, *, limit: int, cap: int | None = None) -> bool:
        """把一个元素按 ``limit``(本段还能用的字数)截断后放进来;放不下返回 False。"""
        if element_id in self.ids:
            return True
        text = (text or "").strip()
        if not text:
            return True
        # 两处截断都不许切进数字:模型只看到截断后的文本、接地也按它判,``85.23`` 截成
        # ``85.2`` 会让一个原文里没有的数通过接地并被缓存成「已核实」。
        if cap is not None:
            text = cap_number_safe(text, cap)
        handle = f"e{len(self.elements) + 1}"
        room = min(limit, self.remaining) - card_element_cost(handle, element_type, location_label, "")
        if room < min(len(text), _CARD_MIN_TEXT_CHARS):
            return False
        text = cap_number_safe(text, room)
        if not text:
            return False
        self.elements.append(CardInputElement(handle, element_id, element_type, location_label, text))
        self.ids.add(element_id)
        self.used += card_element_cost(handle, element_type, location_label, text)
        return True


def _card_opening(sources: SourceStorePort, source_id: str, budget: _CardBudget,
                  limit: int, cancel_event: CancelEvent) -> tuple[int, int, bool]:
    """开头段:从位置 0 顺序分页读,直到本段预算用完。返回 (total, 下一个未读位置, 计数稳定)。"""
    spent_before = budget.used
    offset = 0
    total = None
    stable = True
    while True:
        raise_if_cancelled(cancel_event)
        page = sources.source_elements_page(source_id, offset=offset, limit=_CARD_OPENING_PAGE)
        if total is None:
            total = page.total_count
        stable = stable and page.total_count == total
        if not page.items:
            return total, offset, stable
        for element in page.items:
            if element.source_id != source_id:
                offset += 1
                continue
            share = limit - (budget.used - spent_before)
            cap = PAPER_CARD_TABLE_MAX_CHARS if element.element_type == "table" else None
            if not budget.fit(element.id, element.element_type, element.location_label,
                              element.text, limit=share, cap=cap):
                return total, offset, stable
            offset += 1
        if offset >= total:
            return total, offset, stable


def _card_tables(database: RepositoryDatabasePort, sources: SourceStorePort,
                 source_id: str, budget: _CardBudget, cancel_event: CancelEvent) -> None:
    """表格段:按文档顺序取表格元素(长表的续段不算新表),跳过开头段已放的。"""
    taken = 0
    after = None
    with database.connect() as db:
        while taken < PAPER_CARD_MAX_TABLES:
            raise_if_cancelled(cancel_event)
            rows = sources.element_page_rows(db, source_id, "table", after, _CARD_TABLE_PAGE)
            for row in rows:
                if row["id"] in budget.ids:
                    continue
                if not budget.fit(row["id"], "table", row["location_label"] or "", row["text"] or "",
                                  limit=budget.remaining, cap=PAPER_CARD_TABLE_MAX_CHARS):
                    return
                taken += 1
                if taken >= PAPER_CARD_MAX_TABLES:
                    return
            if len(rows) < _CARD_TABLE_PAGE:
                return
            after = (rows[-1]["created_at"], rows[-1]["id"])


def _card_spread(sources: SourceStorePort, source_id: str, budget: _CardBudget,
                 start: int, total: int, cancel_event: CancelEvent) -> bool:
    """spread 段:在开头段之后的位置里均匀取样(必含最后一个元素),份额按剩余预算均分。"""
    candidates = list(range(start, total))
    if not candidates or budget.remaining < _CARD_MIN_TEXT_CHARS:
        return True
    count = min(len(candidates), _CARD_SPREAD_MAX_ELEMENTS,
                max(1, budget.remaining // _CARD_SPREAD_ELEMENT_CHARS))
    if count == 1:
        positions = [candidates[-1]]
    else:
        positions = sorted({candidates[i * (len(candidates) - 1) // (count - 1)] for i in range(count)})
    read_at, stable = _read_positions(sources, source_id, positions, total, cancel_event)
    picked = [element for position in positions for element in read_at.get(position, ())
              if element.source_id == source_id and element.id not in budget.ids
              and (element.text or "").strip()]
    for index, element in enumerate(picked):
        share = budget.remaining // (len(picked) - index)
        cap = PAPER_CARD_TABLE_MAX_CHARS if element.element_type == "table" else None
        budget.fit(element.id, element.element_type, element.location_label, element.text,
                   limit=share, cap=cap)
    return stable


def sample_paper_card_input(
    sources: SourceStorePort,
    database: RepositoryDatabasePort,
    source_id: str,
    budget_chars: int,
    *,
    generation_reader: Callable[[str], str],
    cancel_event: CancelEvent = None,
) -> PaperCardSample:
    """论文卡片的确定性输入采样(规格 §4.3),输入块不超过 ``budget_chars`` 字。

    ``generation_reader`` 必须是论文卡片的 basis 读取函数
    (``SourceStorePort.source_card_basis``):它在读取前给出见证值、读取后复核,
    两次不等(或分页期间元素数变化)返回 ``changed``;返回的 ``basis`` 就是那个守护值。
    调用方负责来源授权。
    """
    raise_if_cancelled(cancel_event)
    before = generation_reader(source_id)
    budget = _CardBudget(max(0, int(budget_chars)))
    opening_limit = int(budget.total * PAPER_CARD_OPENING_SHARE)
    total, opening_end, stable = _card_opening(sources, source_id, budget, opening_limit, cancel_event)
    if not total:
        return PaperCardSample("empty", before, (), "", 0)
    _card_tables(database, sources, source_id, budget, cancel_event)
    stable = _card_spread(sources, source_id, budget, opening_end, total, cancel_event) and stable
    raise_if_cancelled(cancel_event)
    if not stable or generation_reader(source_id) != before:
        return PaperCardSample("changed", before, (), "", total)
    if not budget.elements:
        return PaperCardSample("empty", before, (), "", total)
    elements = tuple(budget.elements)
    return PaperCardSample("ok", before, elements, render_card_input(elements), total)
