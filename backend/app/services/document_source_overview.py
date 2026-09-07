"""Bounded document-order evidence for a source already resolved in scope."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Callable

from app.models.ask import Citation
from app.repositories.ports import SourceStorePort
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.collection_enumeration import SourceItem


@dataclass(frozen=True)
class SourceOverview:
    context_block: str
    id_map: dict[str, dict]
    citations: list[Citation]
    coverage_note: str


def _safe_text(text: str) -> str:
    # Source content cannot manufacture handles belonging to this context.
    return re.sub(r"\[\s*k\d+\s*\]", lambda m: "［" + m[0][1:-1] + "］", text)


def prepare_source_overview(
    sources: SourceStorePort,
    source_item: SourceItem,
    budget_chars: int,
    max_elements: int,
    cancel_event: CancelEvent = None,
    *,
    generation_reader: Callable[[str], str] | None = None,
) -> SourceOverview:
    """Read evenly spaced elements, including the last source-detail position.

    The caller owns source authorization and title resolution. Every read is a
    single bounded page; there is no full-source hydration. Without a generation
    witness this function never certifies an unchanged complete document.
    """
    raise_if_cancelled(cancel_event)
    if budget_chars <= 0 or max_elements <= 0:
        return SourceOverview("", {}, [], "本次没有可用的原文概述预算，请增加预算后重试。")
    source_id = source_item.source_id
    before = generation_reader(source_id) if generation_reader else None
    first = sources.source_elements_page(source_id, offset=0, limit=1)
    total = first.total_count
    count = min(total, max_elements)
    offsets = (
        [index * (total - 1) // (count - 1) for index in range(count)]
        if count > 1 else ([0] if count else [])
    )
    elements = []
    stable_count = True
    seen = set()
    for offset in offsets:
        raise_if_cancelled(cancel_event)
        page = first if offset == 0 else sources.source_elements_page(
            source_id, offset=offset, limit=1,
        )
        stable_count = stable_count and page.total_count == total
        for element in page.items:
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
        len(f"k{index + 1}: " + json.dumps(body, ensure_ascii=False)) + 1
        for index, body in enumerate(bodies)
    ) <= budget_chars
    for index, element in enumerate(elements):
        raise_if_cancelled(cancel_event)
        # Reserve a fair share for every sampled position; the opening cannot
        # exhaust the budget before a late-document conclusion reaches synthesis.
        slot = (budget_chars - used) if whole_fits else (budget_chars - used) // (len(elements) - index)
        key = f"k{len(lines) + 1}"
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
            "tier": source_item.tier, "notebook_id": source_item.notebook_id,
            "relevance": 1.0,
        }
        citations.append(Citation(
            label=source_item.source_title, source_id=source_id,
            element_id=element.id, location_label=element.location_label,
            quoted_span=quote, tier=source_item.tier,
            notebook_id=source_item.notebook_id,
        ))
    if not total:
        note = "这篇文档没有可读取的原文，请先完成文档解析。"
    elif len(lines) == total and complete_text and generation_reader and before:
        note = f"已读取全部 {total} 个原文元素，读取期间文档解析版本未变化。"
    else:
        note = (
            f"本次提供 {len(lines)}/{total} 个原文元素的有界摘录，按来源详情顺序分布取样；"
            "不代表覆盖所有章节或全文，请仅依据这些原文介绍，并明确概述范围有限。"
        )
    return SourceOverview("\n".join(lines), id_map, citations, note)
