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

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FORMULA_BLOCK = re.compile(r"^\s*\$\$")
_TABLE_HTML = re.compile(r"^\s*<(table|details)\b", re.IGNORECASE)
_FIGURE = re.compile(r"^\s*(Figure\s+\d+|!\[\]\()", re.IGNORECASE)
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")


def line_offsets(text: str) -> Dict[int, int]:
    """1-based line number -> absolute char offset where that line begins."""
    offsets: Dict[int, int] = {}
    off = 0
    for i, line in enumerate(text.split("\n"), start=1):
        offsets[i] = off
        off += len(line) + 1  # +1 for the '\n'
    return offsets


def _classify_line(line: str) -> str:
    if _HEADING.match(line):
        return "heading"
    if _FORMULA_BLOCK.match(line):
        return "formula"
    if _TABLE_HTML.match(line):
        return "table"
    if _FIGURE.match(line):
        return "figure_caption"
    if _LIST.match(line):
        return "list_item"
    return "paragraph"


def parse_elements(
    text: str, source_file: str, line_range: Optional[List[int]] = None
) -> List[SourceElementQ]:
    lines = text.split("\n")
    offs = line_offsets(text)
    lo, hi = (line_range or [1, len(lines)])
    elements: List[SourceElementQ] = []
    counter = 0

    def emit(kind: str, l_start: int, l_end: int) -> None:
        emit_span(kind, offs[l_start], offs[l_end] + len(lines[l_end - 1]),
                  l_start, l_end)

    def emit_span(kind: str, char_start: int, char_end: int,
                  l_start: int, l_end: int) -> None:
        nonlocal counter
        raw = text[char_start:char_end]
        if not raw.strip():
            return
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{l_start}-{counter}", type=kind, file=source_file,
            line_start=l_start, line_end=l_end,
            char_start=char_start, char_end=char_end, text=raw,
        ))

    def emit_formula(i: int) -> int:
        """Display formula. MinerU emits `$$` / latex / `$$` (3 lines); the
        formula element text is the INNER latex (delimiters excluded). Returns
        the next line index to process."""
        if lines[i - 1].strip() == "$$":
            k = i + 1
            while k <= hi and lines[k - 1].strip() != "$$":
                k += 1
            if k - 1 >= i + 1:  # has inner content
                emit("formula", i + 1, k - 1)  # inner latex line(s)
            return k + 1  # skip the closing $$
        line = lines[i - 1]
        inner = line.strip().strip("$").strip()
        if inner:
            local = line.find(inner)
            if local >= 0:
                emit_span("formula", offs[i] + local, offs[i] + local + len(inner), i, i)
        return i + 1

    i = lo
    while i <= hi:
        line = lines[i - 1]
        if not line.strip():
            i += 1
            continue
        kind = _classify_line(line)
        if kind == "heading":
            emit("heading", i, i)
            i += 1
        elif kind == "formula":
            i = emit_formula(i)
        elif kind in ("table", "figure_caption", "list_item"):
            emit(kind, i, i)
            i += 1
        else:
            j = i
            while (j < hi and lines[j].strip()
                   and _classify_line(lines[j]) == "paragraph"):
                j += 1
            emit("paragraph", i, j)
            i = j + 1
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
