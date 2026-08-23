"""Markdown source element parsing and section-tree construction for KG windowing.

Extracted from the former qiefen pipeline as standalone utilities so the KG
windowing module has no dependency on qiefen.

Sunk to app.domain in B3 (zero app.services/app.repositories dependency —
its one internal dependency, structural Markdown block parsing, is
app.domain.structural_markdown, itself sunk in the same change) so
app.repositories.sqlite.maintenance can call parse_elements directly.
``app.services.kg.parsing`` re-exports every name here unchanged for
existing importers (kg/windowing.py, kg/filters.py, kg/extract.py, and the
KG test suite).
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
    # image 元素例外：text 是结构化 caption，跨度被收缩到 caption 长度
    # (see parse_elements below)，因此不再等于 source_file 里那段原文
    # 切片——它只用于 make_windows() 的打包定位，不是可还原的原文。
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
    # `> **图片描述**` 引用块：parsers 把它折进图片元素，KG 侧仍按普通段落走
    # verbatim 切片那条路（`else` 分支），与折叠前的 blockquote-as-paragraph
    # 逐字同义——描述正文照常参与 KG 抽取。
    "image_description": "paragraph",
}


def parse_elements(
    text: str, source_file: str, line_range: Optional[List[int]] = None
) -> List[SourceElementQ]:
    from app.domain.structural_markdown import (
        contains_data_uri_image_literal,
        parse_blocks,
    )

    blocks = parse_blocks(text)
    lo, hi = (line_range or [1, len(text.split("\n"))])
    elements: List[SourceElementQ] = []
    counter = 0
    for b in blocks:
        if not (lo <= b.line_start <= hi):
            continue
        char_start = b.char_start
        char_end = b.char_end
        if b.type == "image":
            # Structured caption only — never the raw markdown slice. For a
            # `data:image/...;base64,...` src the raw slice is the entire
            # base64 payload, which would otherwise get sliced into dozens of
            # KG extraction windows for a single image (修1). Blocks without
            # a caption produce no KG parse element at all.
            raw = b.text
            # Shrink the element's span to just the caption. The raw markdown
            # span (b.char_start..b.char_end) still covers the full
            # `![caption](data:...)` literal — for a large embedded image
            # that's hundreds of KB, and make_windows() packs purely by char
            # span, so an unshrunk span would be sliced into dozens of windows
            # that each re-run kg_extract on the same short caption. The
            # shrink turns that O(payload/n) into O(1): at most one extra
            # window boundary at the gap it leaves behind (caption shorter
            # than the pack overlap may land in two adjacent windows — an
            # observed, accepted trade-off).
            #
            # Evidence spans must stay truthful (`_ev()` persists
            # char_start/char_end as the grounded source range), so locate
            # the caption's REAL position inside the literal. When the
            # caption does not appear verbatim in the source (markdown-it
            # normalized it: `\]` unescaped, NUL → U+FFFD, ...), there is no
            # truthful span to record — skip the KG element entirely rather
            # than manufacture a prefix span whose slice would ground
            # evidence to `![fo`-style noise (codex R3 P2). The caption still
            # reaches retrieval through the parsers.py element/chunk path,
            # which does not carry char offsets.
            idx = text.find(raw, b.char_start, b.char_end) if raw else -1
            if idx < 0:
                continue
            char_start = idx
            char_end = idx + len(raw)
        else:
            raw = text[b.char_start:b.char_end]
            # codex R4 P1: 容器块（段落/列表/表格/标题/代码块）的 verbatim 切片
            # 若带着 data URI 图片字面量（混排、嵌套、被拒 mime 都会留在原文
            # 里），载荷会整段进 KG 窗口；而换成 parse_blocks 的已消毒文本又
            # 会破坏「证据跨度切原文即元素文本」契约（消毒文本不是连续切片、
            # 无法定位）。两条契约不可兼得时跳过该块——其正文仍经 parsers.py
            # 的 element/chunk 通道（消毒后、不带偏移）参与检索，只是不参与
            # KG 抽取。已登记的取舍。
            if contains_data_uri_image_literal(raw):
                continue
        if not raw.strip():
            continue
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{b.line_start}-{counter}",
            type=_QTYPE_MAP.get(b.type, "paragraph"),
            file=source_file,
            line_start=b.line_start, line_end=b.line_end,
            char_start=char_start, char_end=char_end,
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
