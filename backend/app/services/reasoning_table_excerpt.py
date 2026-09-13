"""Bounded, row-preserving views of tables already in the evidence pool."""

from __future__ import annotations

import re
from typing import Callable, Sequence


_RULE_CELL = re.compile(r"^:?-{3,}:?$")
_NUMBER_CELL = re.compile(r"^[+−-]?\d[\d.,%/±+−eE -]*$")


def _cells(line: str) -> tuple[str, ...]:
    # Escaped pipes need a Markdown parser; do not guess their column positions.
    if "\\|" in line or "|" not in line:
        return ()
    return tuple(cell.strip() for cell in line.strip().strip("|").split("|"))


def _tables(text: str):
    """Yield only tables whose rows retain an unambiguous column count.

    Markdown has a separator row. Reconstructed source tables use semicolons
    between rows and pipes between cells; require a textual header and numeric
    data in that less explicit form. Ragged rows end a table, never get padded.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        header = _cells(line)
        if len(header) < 2 or index + 1 >= len(lines):
            continue
        if index and len(_cells(lines[index - 1])) == len(header):
            # Multiple header levels require their parent labels too; defer
            # rather than attaching numeric rows to an incomplete hierarchy.
            continue
        rule = _cells(lines[index + 1])
        if len(rule) != len(header) or not all(_RULE_CELL.fullmatch(c) for c in rule):
            continue
        rows = []
        for body in lines[index + 2:]:
            row = _cells(body)
            if len(row) != len(header) or all(_RULE_CELL.fullmatch(c) for c in row):
                break
            rows.append(row)
        if rows:
            before = index - 1
            caption = []
            while before >= 0 and lines[before].strip() and "|" not in lines[before]:
                caption.append(lines[before])
                before -= 1
            yield "\n".join(reversed(caption)), header, rows
    for line in lines:
        pieces = re.split(r"\s+;\s+", line)
        if len(pieces) < 3:
            continue
        header = _cells(pieces[0])
        if len(header) < 2 or any(_NUMBER_CELL.fullmatch(c) for c in header):
            continue
        # Reconstructed tables often fold the caption into the first cell.
        # Separate it for readability, but retain all units/evaluation settings.
        boundaries = list(re.finditer(r"[.!?。！？]\s+", header[0]))
        end = boundaries[-1].end() if boundaries else 0
        caption = header[0][:end].strip()
        header = (header[0][end:], *header[1:])
        rows = []
        for piece in pieces[1:]:
            row = _cells(piece)
            if len(row) != len(header) or not any(
                    _NUMBER_CELL.fullmatch(c) for c in row[1:]):
                break
            rows.append(row)
        if len(rows) >= 2:
            yield caption, header, rows


def select_table_excerpt(
    text: str, terms: Sequence[str], limit: int,
    *, clean: Callable[[object], str],
) -> str | None:
    """Select whole rows with their header, or let the old excerpt take over.

    Cover distinct query terms in row labels first, then other matching rows.
    Remaining space samples the tail before earlier unmentioned rows, so a long
    comparison table does not systematically hide its last participant. This is
    a structural sample, not a claim about which participant is the main one.
    Rows are emitted in source order; no column is removed or synthesized.
    """
    if limit <= 0:
        return None
    needles = {term.casefold() for term in terms if term}
    choices = []
    for caption, header, rows in _tables(str(text or "")):
        labels = [{t for t in needles if t in row[0].casefold()} for row in rows]
        body_hits = [{t for t in needles if t in " ".join(row).casefold()}
                     for row in rows]
        score = len(set().union(*labels)) * 2 + len(set().union(*body_hits))
        choices.append((score, caption, header, rows, labels, body_hits))
    if not choices:
        return None
    _, caption, header, rows, labels, body_hits = max(choices, key=lambda item: item[0])
    heading = "表头: " + " │ ".join(clean(cell) for cell in header)
    if caption:
        heading = "表格说明: " + clean(caption) + "\n  " + heading
    rendered = ["行: " + " │ ".join(clean(cell) for cell in row) for row in rows]
    chosen: list[int] = []
    covered: set[str] = set()
    remaining = set(range(len(rows)))
    while remaining:
        omitted = len(rows) - len(chosen) - 1
        current = "\n  ".join([heading, *(rendered[i] for i in chosen)])
        note = f"（本表另有 {omitted} 行未展开；只比较已展示行）" if omitted else ""
        overhead = len(current) + len("\n  ") + (len(note) + len("\n  ") if note else 0)
        fitting = [i for i in remaining if overhead + len(rendered[i]) <= limit]
        if not fitting:
            break
        index = max(fitting, key=lambda i: (
            len(labels[i] - covered), len(labels[i]), len(body_hits[i]),
            -i if body_hits[i] else i))
        remaining.remove(index)
        chosen = sorted([*chosen, index])
        covered.update(labels[index])
    if not chosen:
        return None
    omitted = len(rows) - len(chosen)
    lines = [heading, *(rendered[i] for i in chosen)]
    if omitted:
        lines.append(f"（本表另有 {omitted} 行未展开；只比较已展示行）")
    return "\n  ".join(lines)
