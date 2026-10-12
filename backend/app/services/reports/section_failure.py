"""How a failed Deep Report section reads in the report body.

A section that produced no body is still written into ``content_md`` as a
short note, so the gap is visible instead of the section silently vanishing.
That body is what the anonymous share page and the export serve, so the note
is always one of the fixed neutral sentences below.  The raw exception text
behind a failure never enters it: it stays in the section's own
``sections[].error``, which only the report's author can read (the report
detail route answers its creator alone), and the stage that failed is recorded
by the content-free ``report_stage_timing`` event (``stage`` + ``failed``).

``neutralize_failure_notes`` applies the same rule to bodies stored before
this boundary existed, at read time and only on the anonymous projection: it
rewrites the exact literal ``_assemble`` wrote for each failed section's stored
error, then that paragraph shape as a fallback, so a report with no failed
section is returned byte for byte.

Raw text stays readable to exactly two parties: the author, in the notebook
report detail's ``sections[].error``, and administrators, in the activity
detail's ``section_errors`` (``failed_section_errors`` below).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence

from app.domain.report_failure_note import (
    NOTE_PREFIX as _NOTE_PREFIX,
    NOTE_SUFFIX as _NOTE_SUFFIX,
    SECTION_FAILURE_COPY,
    carries_raw_failure_note,
)

_NEUTRAL_NOTES = frozenset(SECTION_FAILURE_COPY.values())
# ``_assemble`` writes a failed section as the three lines
# ``## <title>`` / blank / ``（本节生成失败:<error>）`` followed by a blank line
# (or the end of the body), with ``<error>`` cut to 300 characters by the
# retrieval/draft handlers. Only that exact shape is rewritten; the lazy,
# bounded inner match stops at the first closing bracket that ends the
# paragraph, so it can never run into the next section.
_STORED_NOTE_RE = re.compile(
    r"(?m)^(## [^\n]*\n\n)（本节生成失败:([\s\S]{0,300}?)）(?=\n\n|\n\Z|\Z)"
)


def section_failure_note(error: object) -> str:
    """The neutral sentence a failed section shows; never the raw error."""
    text = error if isinstance(error, str) else ""
    return text if text in _NEUTRAL_NOTES else SECTION_FAILURE_COPY["internal"]


def section_failure_paragraph(error: object) -> str:
    """The body paragraph ``_assemble`` writes for one failed section."""
    return f"{_NOTE_PREFIX}{section_failure_note(error)}{_NOTE_SUFFIX}"


def neutralize_failure_notes(
    content_md: str, failed_errors: Sequence[str] | None = None,
) -> str:
    """Rewrite stored failure notes that still carry raw error text.

    ``failed_errors`` are the stored ``error`` values of the report's failed
    sections. Each one names its paragraph exactly -- ``_assemble`` wrote the
    literal ``（本节生成失败:<error>）`` -- so it is replaced as a literal first,
    whatever the error contains (a closing bracket before a blank line) and
    whatever the heading above it looks like (a title with a line break).
    Longest first, so an error that is a prefix of another cannot cut the
    longer note short. The paragraph pattern then runs as the fallback for a
    body whose sections carry no usable error; on an already-neutral note it
    is a no-op, so a body whose notes are all neutral is returned untouched
    without either pass (``carries_raw_failure_note``, the stores' gate too).
    """
    if not carries_raw_failure_note(content_md):
        return content_md
    for error in sorted(set(failed_errors or ()), key=len, reverse=True):
        content_md = content_md.replace(
            f"{_NOTE_PREFIX}{error}{_NOTE_SUFFIX}", section_failure_paragraph(error)
        )
    return _STORED_NOTE_RE.sub(
        lambda match: match.group(1) + section_failure_paragraph(match.group(2)),
        content_md,
    )


def failed_section_errors(value: object) -> list[dict[str, Any]]:
    """Normalise the failed-section rows a store read selected in SQL.

    Both adapters select ``[{index, title, error}, ...]`` for the sections
    whose ``failed`` is JSON ``true`` (PostgreSQL as jsonb, SQLite as JSON
    text); this gives the two dialects one Python shape.
    """
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value or "[]")
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        title, error = item.get("title"), item.get("error")
        rows.append({
            "index": index,
            "title": title if isinstance(title, str) else "",
            "error": error if isinstance(error, str) else "",
        })
    return rows


__all__ = [
    "SECTION_FAILURE_COPY",
    "carries_raw_failure_note",
    "failed_section_errors",
    "neutralize_failure_notes",
    "section_failure_note",
    "section_failure_paragraph",
]
