"""Deterministic plain-text preview of a Markdown answer (MCP answer links).

Pure function, no model call. Tables and display-math blocks are replaced by a
one-line placeholder pointing at the link; everything else is reduced to text.
"""
from __future__ import annotations

import re

from app.services.citation_markers import MARKER_RE

TABLE_PLACEHOLDER = "（表格，见链接）"
MATH_PLACEHOLDER = "（公式，见链接）"
_ELLIPSIS = "…"
_SENTENCE_END = "。！？；.!?"

_FENCE_RE = re.compile(r"^\s{0,3}(```+|~~~+)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d{1,9}[.)])\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_RE = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_(?=\S)([^_]+?)(?<=\S)_(?![\w])")
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)")


def _is_table_row(line: str) -> bool:
    return "|" in line and bool(line.strip())


def _inline(text: str) -> str:
    text = MARKER_RE.sub("", text)
    text = _IMAGE_RE.sub("", text)
    text = _HTML_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _CODE_SPAN_RE.sub(r"\2", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = _INLINE_MATH_RE.sub(r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _to_blocks(markdown: str) -> list[str]:
    """Return plain-text lines; an empty string is a paragraph break."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    fence: str | None = None
    while i < n:
        line = lines[i]
        if fence is not None:
            if _FENCE_RE.match(line) and line.strip().startswith(fence[0] * 3):
                fence = None
            else:
                out.append(line.rstrip())
            i += 1
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            i += 1
            continue
        stripped = line.strip()
        # display math: $$ ... $$ or \[ ... \] (single- or multi-line)
        for open_, close in (("$$", "$$"), ("\\[", "\\]")):
            if stripped.startswith(open_):
                rest = stripped[len(open_):]
                if close in rest:
                    i += 1
                else:
                    i += 1
                    while i < n and close not in lines[i]:
                        i += 1
                    i += 1
                out.append(MATH_PLACEHOLDER)
                break
        else:
            # GFM table: header row followed by a separator row
            if _is_table_row(line) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]) and "-" in lines[i + 1] and "|" in (line + lines[i + 1]):
                i += 2
                while i < n and _is_table_row(lines[i]):
                    i += 1
                out.append(TABLE_PLACEHOLDER)
                continue
            if not stripped:
                out.append("")
            elif _HR_RE.match(line):
                out.append("")
            else:
                h = _HEADING_RE.match(line)
                if h:
                    out.append(_inline(h.group(1)))
                else:
                    line = _BLOCKQUOTE_RE.sub("", line)
                    b = _BULLET_RE.match(line)
                    out.append(("• " + _inline(b.group(2))) if b else _inline(line))
            i += 1
    return out


def _collapse(lines: list[str]) -> str:
    paras: list[str] = []
    cur: list[str] = []
    for ln in lines:
        if ln == "":
            if cur:
                paras.append("\n".join(cur))
                cur = []
        else:
            cur.append(ln)
    if cur:
        paras.append("\n".join(cur))
    return "\n".join(paras)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    budget = max_chars - len(_ELLIPSIS)
    head = text[:budget]
    cut = -1
    # Prefer a paragraph/line boundary, then a sentence end; the char at `budget`
    # being a newline also means `head` already ends on a boundary.
    nl = head.rfind("\n")
    if text[budget : budget + 1] == "\n":
        nl = budget
    sentence = max((head.rfind(c) for c in _SENTENCE_END), default=-1)
    if nl > 0:
        cut = nl
    if sentence >= 0 and sentence + 1 > cut:
        cut = sentence + 1
    if cut > 0:
        head = head[:cut]
    head = head.rstrip()
    return head + _ELLIPSIS, True


def answer_preview(markdown: str, max_chars: int) -> tuple[str, bool]:
    """(plain_text, truncated); length counted in Unicode code points."""
    if not markdown or not markdown.strip():
        return "", False
    return _truncate(_collapse(_to_blocks(markdown)).strip(), max_chars)
