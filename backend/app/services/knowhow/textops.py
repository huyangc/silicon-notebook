"""Pure, zero-LLM text transforms for knowhow-table cell markdown (Task 5:
knowhow-tables PR-1, extended by PR-2+3 Task 2). Every function here is a
total, side-effect-free string -> string/list transform — no IO, no model
calls — so KnowhowProjector (see ``projection.py``) can call them
synchronously and deterministically from inside a DB write transaction.

Design doc §④ (docs/superpowers/specs/2026-07-15-knowhow-tables-design.md):
images are for humans only — machine consumption (KG/embedding/FTS/Agent API)
never sees raw ``asset://`` markdown, only the ``strip_images`` placeholder.
``parse_steps``/``split_tools`` turn a cell's markdown list structure into
structured output with ZERO model calls (deterministic regex parsing only).

PR-2+3 Task 2 adds three more transforms the cell-level node-model projector
needs: ``node_name`` (a knowledge object's display name) and ``value_key`` (its
cross-row merge identity — design doc §④ "同列同值跨行归并") plus
``compose_row_title`` (the synthesized row label used for section_path/
evidence labels on a table with no anchor column, or a row whose anchor cell
is itself blank — design doc §① "行标题自动合成").
"""
from __future__ import annotations

import hashlib
import re

# ``![alt](anything-without-a-closing-paren)`` — non-greedy on the URL body so
# multiple images on one line each match independently. Title syntax
# (``![alt](url "title")``) is intentionally not special-cased: the app never
# emits it (images are inserted solely via the asset-upload flow's
# ``asset://<id>`` references), mirroring the frontend's own image-rewrite
# scope (Task 7).
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")

# List-item markers. Both anchor on optional leading whitespace (markdown
# nesting/indentation) then require at least one space after the marker.
_ORDERED_ITEM_RE = re.compile(r"^\s*\d+[.\)]\s+(.*)$")
_UNORDERED_ITEM_RE = re.compile(r"^\s*[-*+]\s+(.*)$")


def strip_images(md: "str | None") -> str:
    """Replace every ``![alt](url)`` with a machine-safe placeholder:
    ``（图示：alt）`` when alt text is present, else the bare ``（图示）``.
    Everything else in the string (list markers, other text) is left
    untouched — callers that also need ``parse_steps``/``split_tools`` should
    run this FIRST and parse the result, so list structure survives."""

    def _replace(match: "re.Match[str]") -> str:
        alt = match.group(1).strip()
        return f"（图示：{alt}）" if alt else "（图示）"

    return _IMAGE_RE.sub(_replace, md or "")


def _list_item_text(line: str) -> "str | None":
    """Return the item's text with its marker stripped, or None if `line`
    does not start a new ordered/unordered list item."""
    match = _ORDERED_ITEM_RE.match(line) or _UNORDERED_ITEM_RE.match(line)
    return match.group(1).strip() if match else None


def parse_steps(md: "str | None") -> list[str]:
    """Deterministically parse a cell's ordered/unordered markdown list into
    ``steps[]`` (marker stripped). A non-marker line occurring AFTER at least
    one step has started is treated as a continuation of that step (merged
    with a space — handles a wrapped/indented follow-on line for the same
    step); blank lines are skipped without breaking the list; leading prose
    before the first marker is dropped (not merged into anything, since no
    step exists yet to attach it to). Text with no list marker at all
    returns ``[]`` — the caller keeps the whole cell as plain prose instead."""
    steps: list[str] = []
    for line in (md or "").splitlines():
        if not line.strip():
            continue
        item = _list_item_text(line)
        if item is not None:
            steps.append(item)
        elif steps:
            steps[-1] = f"{steps[-1]} {line.strip()}".strip()
        # else: prose before any marker has appeared yet — dropped.
    return steps


def split_tools(md: "str | None") -> list[str]:
    """Split a tool cell into individual tool names: by list item when the
    cell uses a markdown list, otherwise by newline. Dedupes on a casefolded
    key (keeping the first-seen spelling/casing) and drops empty entries."""
    seen: set[str] = set()
    out: list[str] = []
    for line in (md or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        item = _list_item_text(line)
        if item is None:
            item = stripped
        if not item:
            continue
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


#: node_name's first-line display cap (design doc §④ "名=格值首行截断" — the
#: brief's exact figure, "净文本首行≤40 字+…").
_NODE_NAME_LIMIT = 40

#: value_key's normalized-text/hash-gate threshold (brief: "≤80 字...；>80：
#: sha256 前 32 hex").
_VALUE_KEY_LIMIT = 80

#: compose_row_title's segment count/per-segment cap (brief: "前≤3 个非空格
#: 首行、每段≤16 字").
_ROW_TITLE_MAX_SEGMENTS = 3
_ROW_TITLE_SEGMENT_LIMIT = 16

# Whitespace-or-punctuation RUNS, deleted (not collapsed to a separator) by
# value_key's normalization — "示波器"/"示波器。"/"示波器 " must all normalize
# to the exact same key. `\W` is unicode-aware or `\s`/no re.ASCII flag was
# ever passed here (Python 3 `str` patterns default to unicode semantics), so
# CJK ideographs — Unicode category Lo, "alphanumeric" per the unicode
# database — are correctly treated as WORD characters and never stripped;
# only actual whitespace/punctuation/symbols are.
_VALUE_KEY_NORMALIZE_RE = re.compile(r"[\s\W]+")


def node_name(text: "str | None") -> str:
    """A knowledge object's display name (design doc §④ "名=node_name"):
    the cell's net text, first line only, trimmed to ``_NODE_NAME_LIMIT``
    characters with a trailing "…" when truncated. Applied uniformly to every
    per-cell KO (including the row-title/anchor cell's own KO — its name also
    doubles as the row's synthesized title when the anchor cell itself has
    content, see ``projection.py``'s row-title resolution). Whitespace-only
    or empty input returns ``""`` (no phantom "…")."""
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0].strip()
    if len(first_line) > _NODE_NAME_LIMIT:
        return f"{first_line[:_NODE_NAME_LIMIT]}…"
    return first_line


def value_key(text: "str | None") -> str:
    """Cross-row merge identity for a cell's value (design doc §④ "同列同值
    跨行归并"): casefold, then strip every whitespace/punctuation RUN entirely
    (not just collapse it) so trivial formatting differences ("示波器" vs
    "示波器。" vs "示波器 ") normalize to the identical key — this is a
    deliberate, coarser-than-display normalization purely for merge identity;
    ``node_name``/the cell's stored payload text are what preserve the
    original readable form. A short (``<= _VALUE_KEY_LIMIT`` chars, post-
    normalization) result IS the key verbatim (stays human-legible for direct
    DB inspection); a longer one hashes to a 32-hex-char SHA-256 prefix
    instead (bounds the KO id's hash INPUT to something reasonable rather
    than embedding an arbitrarily long cell's full text in it — the id
    itself is already a fixed-length hash either way, see ``_cell_ko_id``).
    Empty/whitespace-only input normalizes to ``""``."""
    normalized = _VALUE_KEY_NORMALIZE_RE.sub("", (text or "").casefold())
    if len(normalized) <= _VALUE_KEY_LIMIT:
        return normalized
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def compose_row_title(cells_by_position: "list[str]") -> str:
    """Synthesize a display-only row label from a row's cells IN COLUMN-
    POSITION ORDER (design doc §① "行标题自动合成"): the first line of each
    of the first ``_ROW_TITLE_MAX_SEGMENTS`` NON-EMPTY cells, each truncated
    to ``_ROW_TITLE_SEGMENT_LIMIT`` characters (plain slice — deliberately NO
    "…" marker, unlike ``node_name``: joining several already-truncated
    segments with "…" on each would read as noisier clutter, not clearer
    truncation, for a composite label built from multiple short fragments).
    Segments are joined with " · ". Returns ``""`` when every cell is empty —
    the "行N" positional fallback for that case is the CALLER's job
    (``projection.py``, which has the row's position; this function does
    not)."""
    segments: "list[str]" = []
    for text in cells_by_position:
        stripped = (text or "").strip()
        if not stripped:
            continue
        first_line = stripped.splitlines()[0].strip()
        if not first_line:
            continue
        segments.append(first_line[:_ROW_TITLE_SEGMENT_LIMIT])
        if len(segments) >= _ROW_TITLE_MAX_SEGMENTS:
            break
    return " · ".join(segments)


__all__ = [
    "strip_images", "parse_steps", "split_tools",
    "node_name", "value_key", "compose_row_title",
]
