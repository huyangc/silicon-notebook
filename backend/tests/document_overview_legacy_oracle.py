"""Frozen oracle for the batched positional read (debt fix F3).

``legacy_prepare_source_overview`` is ``prepare_source_overview`` as it was
before the per-offset loop became batched ``source_elements_page(positions=)``
reads (base 60fac3d5): one ``offset``/``limit=1`` read per sampled position,
totals compared call by call. Only the docstring is shortened; every statement
is the original. The byte-equality tests run it and the current function over
the same store and compare all four ``SourceOverview`` fields. The rendering
helpers are imported from the live module on purpose: they did not change, and
the oracle exists to pin the READ path, not to fork the renderer.
"""
from __future__ import annotations

import json
from typing import Callable

from app.domain.citation_origin import foreign_notebook_id
from app.models.ask import Citation
from app.repositories.ports import SourceStorePort
from app.services.cancellation import CancelEvent, raise_if_cancelled
from app.services.collection_enumeration import SourceItem
from app.services.context_spans import entry_lines
from app.services.document_source_overview import (
    SourceOverview,
    _attest_cited,
    _safe_text,
)
from app.services.source_scope import citation_active_id


def legacy_prepare_source_overview(
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
    """Pre-batching ``prepare_source_overview``, verbatim below this line."""
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
