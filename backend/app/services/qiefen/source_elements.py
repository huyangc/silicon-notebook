"""S1: raw MinerU markdown -> SourceElementQ with absolute char spans.

Unlike the legacy parsers.py, raw text is NEVER whitespace-collapsed: each
element's [char_start, char_end] is a verbatim slice of the source file, so the
downstream atomizer can compute spans that satisfy source[span]==raw_text.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from app.services.qiefen.models import SourceElementQ

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FORMULA_BLOCK = re.compile(r"^\s*\$\$")  # $$ ... $$ display formula
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
        nonlocal counter
        char_start = offs[l_start]
        # char_end = end of l_end's text (no trailing newline).
        char_end = offs[l_end] + len(lines[l_end - 1])
        raw = text[char_start:char_end]
        if not raw.strip():
            return
        counter += 1
        elements.append(SourceElementQ(
            id=f"SE-{l_start}-{counter}", type=kind, file=source_file,
            line_start=l_start, line_end=l_end,
            char_start=char_start, char_end=char_end, text=raw,
        ))

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
        elif kind in ("formula", "table", "figure_caption", "list_item"):
            # single-line structural element (MinerU emits these on one line)
            emit(kind, i, i)
            i += 1
        else:
            # paragraph: consume consecutive non-blank, non-structural lines
            j = i
            while (j < hi and lines[j].strip()
                   and _classify_line(lines[j]) == "paragraph"):
                j += 1
            emit("paragraph", i, j)
            i = j + 1
    return elements
