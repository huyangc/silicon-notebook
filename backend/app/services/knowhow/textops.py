"""Pure, zero-LLM text transforms for knowhow-table cell markdown (Task 5:
knowhow-tables PR-1). Every function here is a total, side-effect-free string
-> string/list transform — no IO, no model calls — so KnowhowProjector (see
``projection.py``) can call them synchronously and deterministically from
inside a DB write transaction.

Design doc §④ (docs/superpowers/specs/2026-07-15-knowhow-tables-design.md):
images are for humans only — machine consumption (KG/embedding/FTS/Agent API)
never sees raw ``asset://`` markdown, only the ``strip_images`` placeholder.
``parse_steps``/``split_tools`` turn a cell's markdown list structure into
structured output with ZERO model calls (deterministic regex parsing only).
"""
from __future__ import annotations

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


__all__ = ["strip_images", "parse_steps", "split_tools"]
