"""Markdown source element parsing and section-tree construction for KG windowing.

Extracted from the former qiefen pipeline as standalone utilities so the KG
windowing module has no dependency on qiefen.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Data models (minimal subset required by windowing.py)
# ---------------------------------------------------------------------------

class SourceElementQ(BaseModel):
    id: str
    type: str  # heading | paragraph | formula | table | figure_caption | list_item
    file: str
    line_start: int
    line_end: int
    char_start: int
    char_end: int
    text: str  # verbatim slice of source_file[char_start:char_end]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SectionNode(BaseModel):
    id: str
    path: str
    title: str
    parent: Optional[str] = None
    kind: Optional[str] = None


# ---------------------------------------------------------------------------
# S1: raw markdown -> SourceElementQ with absolute char spans
# ---------------------------------------------------------------------------

# (旧的行级正则分类器/line_offsets 已移除：parse_elements 现委托
#  structural_markdown.parse_blocks，不再逐行分类。)


# 结构化块类型 -> SourceElementQ.type。code_block 映射成非 _PROSE_TYPES 的
# "code_block"，故不进 KG 抽取窗口(代码不被抽成实体);仍存为元素供检索/引用。
_QTYPE_MAP = {
    "heading": "heading",
    "paragraph": "paragraph",
    "list_item": "list_item",
    "code_block": "code_block",   # 不进 KG 抽取窗口(仍存为元素供检索/引用)
    "table": "table",
    "image": "figure_caption",
    "blockquote": "paragraph",
}


def parse_elements(
    text: str, source_file: str, line_range: Optional[List[int]] = None
) -> List[SourceElementQ]:
    from app.services.structural_markdown import parse_blocks

    blocks = parse_blocks(text)
    lo, hi = (line_range or [1, len(text.split("\n"))])
    elements: List[SourceElementQ] = []
    counter = 0
    for b in blocks:
        if not (lo <= b.line_start <= hi):
            continue
        raw = text[b.char_start:b.char_end]
        if not raw.strip():
            continue
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{b.line_start}-{counter}",
            type=_QTYPE_MAP.get(b.type, "paragraph"),
            file=source_file,
            line_start=b.line_start, line_end=b.line_end,
            char_start=b.char_start, char_end=b.char_end,
            text=raw,
        ))
    return elements


# ---------------------------------------------------------------------------
# S2: heading elements -> SectionNode list with breadcrumb `path`
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_NUM = re.compile(r"^(?:chapter|section)?\s*(\d+(?:\.\d+)*)\b", re.IGNORECASE)


def _heading_title(el: SourceElementQ) -> str:
    m = _HEADING_RE.match(el.text)
    return m.group(2).strip() if m else el.text.strip()


def _numeric_label(title: str) -> Optional[str]:
    m = _NUM.match(title)
    return m.group(1) if m else None


def build_section_tree(elements: List[SourceElementQ]) -> List[SectionNode]:
    nodes: List[SectionNode] = []
    counter = 0
    cur_chain: List[str] = []
    for el in elements:
        if el.type != "heading":
            continue
        title = _heading_title(el)
        num = _numeric_label(title)
        if num:
            parts = num.split(".")
            chain = [".".join(parts[: i + 1]) for i in range(len(parts))]
            path = " > ".join(chain)
            cur_chain = chain
        elif cur_chain:
            path = " > ".join(cur_chain + [title])
        else:
            path = title
        counter += 1
        nodes.append(SectionNode(id=f"SEC-{counter}", path=path, title=title))
    return nodes
