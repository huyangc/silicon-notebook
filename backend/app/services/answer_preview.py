"""Deterministic plain-text preview of a Markdown answer (MCP answer links).

Pure function, no model call. Tables and display-math blocks are replaced by a
one-line placeholder pointing at the link; everything else is reduced to text.
"""
from __future__ import annotations

import bisect
import re

from app.services.citation_markers import MARKER_RE

TABLE_PLACEHOLDER = "（表格，见链接）"
MATH_PLACEHOLDER = "（公式，见链接）"
_ELLIPSIS = "…"
_SENTENCE_END = "。！？；.!?"

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d{1,9}[.)])\s+(.*)$")
_BLOCKQUOTE_RE = re.compile(r"^\s{0,3}>\s?")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
# Character classes exclude the opening bracket so a run of "[" cannot backtrack
# quadratically; lines are additionally clipped to _LINE_CAP.
_IMAGE_RE = re.compile(r"!\[[^\[\]]*\]\([^()]*\)")
_LINK_RE = re.compile(r"\[([^\[\]]*)\]\([^()]*\)")
_HTML_RE = re.compile(r"</?[A-Za-z][^<>]*>|<!--.*?-->", re.DOTALL)
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC_STAR_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w])_(?=\S)([^_]+?)(?<=\S)_(?![\w])")
_INLINE_MATH_RE = re.compile(r"(?<![\\$])\$(?=\S)([^$\n]+?)(?<=\S)\$(?!\d)")
_PLACEHOLDER_RE = re.compile("\ue000(\\d+)\ue001")
_LINE_CAP = 2000


def _is_table_row(line: str) -> bool:
    return "|" in line and bool(line.strip())


def _inline(text: str) -> str:
    # Inline code and inline math are lifted out first so their "<", ">" and
    # emphasis characters survive the HTML/emphasis passes; restored at the end.
    text = text.replace("\ue000", "").replace("\ue001", "")
    saved: list[str] = []

    def stash(m: "re.Match[str]") -> str:
        saved.append(m.group(m.lastindex))
        return f"\ue000{len(saved) - 1}\ue001"

    # A marker takes the blanks before it along ("exists [k1]." -> "exists."),
    # by splitting rather than a `[ \t]*` prefix that would backtrack on runs of blanks.
    pieces = MARKER_RE.sub("", text).split("")
    text = "".join(p.rstrip(" \t") for p in pieces[:-1]) + pieces[-1]
    text = _IMAGE_RE.sub("", text)
    text = _CODE_SPAN_RE.sub(stash, text)
    text = _INLINE_MATH_RE.sub(stash, text)
    text = _HTML_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _BOLD_RE.sub(r"\2", text)
    text = _STRIKE_RE.sub(r"\1", text)
    text = _ITALIC_STAR_RE.sub(r"\1", text)
    text = _ITALIC_UNDER_RE.sub(r"\1", text)
    text = _PLACEHOLDER_RE.sub(lambda m: saved[int(m.group(1))], text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _heading_text(rest: str) -> str:
    rest = rest.rstrip()
    core = rest.rstrip("#")
    if len(core) < len(rest) and (not core or core[-1].isspace()):
        rest = core.rstrip()
    return rest


def _next_index(indices: list[int], after: int) -> int | None:
    pos = bisect.bisect_right(indices, after)
    return indices[pos] if pos < len(indices) else None


def _to_blocks(markdown: str, stop_after: int) -> tuple[list[str], bool]:
    """Plain-text lines (empty string = paragraph break) and whether input was clipped or left unread.

    Stops once more than ``stop_after`` characters of text exist: the caller
    truncates anyway, so reading further input only costs time.
    """
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    clipped = False
    for k, ln in enumerate(lines):
        if len(ln) > _LINE_CAP:
            lines[k] = ln[:_LINE_CAP]
            clipped = True
    # Closer indices cover only lines outside fenced code (same open/close rule as
    # the main loop), so a "$$" inside a code block never closes prose math.
    dollar_close: list[int] = []
    bracket_close: list[int] = []
    scan_fence: str | None = None
    for k, ln in enumerate(lines):
        fm = _FENCE_RE.match(ln)
        if scan_fence is not None:
            if (
                fm
                and fm.group(1)[0] == scan_fence[0]
                and len(fm.group(1)) >= len(scan_fence)
                and not ln.strip().strip(scan_fence[0])
            ):
                scan_fence = None
        elif fm:
            scan_fence = fm.group(1)
        else:
            if "$$" in ln:
                dollar_close.append(k)
            if "\\]" in ln:
                bracket_close.append(k)
    out: list[str] = []
    size = 0
    i, n = 0, len(lines)
    fence: str | None = None
    while i < n and size <= stop_after:
        line = lines[i]
        if fence is not None:
            m = _FENCE_RE.match(line)
            if (
                m
                and m.group(1)[0] == fence[0]
                and len(m.group(1)) >= len(fence)
                and not line.strip().strip(fence[0])
            ):
                fence = None
            else:
                out.append(line.rstrip())
                size += len(line.rstrip()) + 1
            i += 1
            continue
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)
            i += 1
            continue
        stripped = line.strip()
        consumed = 0
        for open_, close, closers in (("$$", "$$", dollar_close), ("\\[", "\\]", bracket_close)):
            if not stripped.startswith(open_):
                continue
            if close in stripped[len(open_):]:
                consumed = 1
            else:
                end = _next_index(closers, i)
                # an unclosed opener is ordinary text, never a swallow-the-rest block
                consumed = (end - i + 1) if end is not None else 0
            break
        if consumed:
            out.append(MATH_PLACEHOLDER)
            size += len(MATH_PLACEHOLDER) + 1
            i += consumed
            continue
        if (
            _is_table_row(line)
            and i + 1 < n
            and "|" in lines[i + 1]
            and "-" in lines[i + 1]
            and _TABLE_SEP_RE.match(lines[i + 1])
        ):
            i += 2
            while i < n and _is_table_row(lines[i]):
                i += 1
            out.append(TABLE_PLACEHOLDER)
            size += len(TABLE_PLACEHOLDER) + 1
            continue
        if not stripped or _HR_RE.match(line):
            out.append("")
        else:
            h = _HEADING_RE.match(line)
            if h:
                text = _inline(_heading_text(h.group(1)))
            else:
                line = _BLOCKQUOTE_RE.sub("", line)
                b = _BULLET_RE.match(line)
                text = ("• " + _inline(b.group(2))) if b else _inline(line)
            out.append(text)
            size += len(text) + 1
        i += 1
    # Input left unread (we stopped early) counts as clipped so the caller reports
    # truncation; bare fence markers and blank lines are not content.
    if not clipped:
        clipped = any(
            ln.strip() and not (_FENCE_RE.match(ln) and not ln.strip().strip("`~"))
            for ln in lines[i:]
        )
    return out, clipped


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


def _is_sentence_end(text: str, idx: int) -> bool:
    ch = text[idx]
    if ch in "。！？；":
        return True
    if ch in ".!?":
        return idx + 1 >= len(text) or text[idx + 1].isspace()
    return False


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    if max_chars <= 0:
        return "", True
    budget = max_chars - len(_ELLIPSIS)
    cut = 0
    # A boundary may sit exactly at `budget` (the next char is a newline).
    for idx in range(min(budget, len(text) - 1), -1, -1):
        if idx < budget and _is_sentence_end(text, idx):
            cut = idx + 1
            break
        if text[idx] == "\n" and idx <= budget:
            cut = idx
            break
    # Never keep less than half the budget just to land on a boundary.
    if cut * 2 < budget:
        cut = budget
    return text[:cut].rstrip() + _ELLIPSIS, True


def answer_preview(markdown: str, max_chars: int) -> tuple[str, bool]:
    """(plain_text, truncated); length counted in Unicode code points."""
    if not markdown or not markdown.strip():
        return "", False
    lines, clipped = _to_blocks(markdown, max(max_chars, 0))
    text, truncated = _truncate(_collapse(lines).strip(), max_chars)
    if clipped and not truncated and text:
        # An over-long line was clipped; the text is incomplete even if short.
        if len(text) + len(_ELLIPSIS) <= max_chars:
            return text + _ELLIPSIS, True
        return _truncate(text + _ELLIPSIS, max_chars - 1)
    return text, truncated
