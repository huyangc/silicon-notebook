"""The projection a shared report exposes to anonymous readers.

This module is the disclosure boundary for public share links.  It is written
as an explicit allowlist rather than a redaction pass: anything a future change
adds to the stored report stays private until it is named here.

What a public reader gets, and why:

* the question, the body, and the timing — that is the artifact being shared;
* per citation: label, display title, location, and the stored excerpt, so the
  ``[k]`` markers in the body can actually be checked against something.

What never crosses, and why:

* ``source_id`` / ``element_id`` / ``object_id`` / ``notebook_id`` — internal
  handles.  Publishing them would let a reader probe the authenticated API for
  material the link was never meant to include, and they buy the reader nothing
  because the public page deliberately cannot open full sources.
* the whole ``understanding`` contract — it carries the intent, the frozen
  source scope (a list of source ids), and credibility internals.  The parts a
  reader benefits from (the corpus basis) are already inside ``content_md``,
  frozen there when the report was generated.

Truncation on this surface is DISCLOSED, never silent (AGENTS.md 用户编辑的数据
不得静默截断).  The sibling ``conversation_public_view`` was brought to that rule
by codex #522 R1-R4; this module carries the same three fixes:

* the question is served WHOLE (``_question_text``) — see its docstring;
* a reference title / original filename / excerpt stays bounded (it is evidence
  metadata, not the user's own artifact) but an over-length value sets
  ``title_truncated`` / ``file_name_truncated`` / ``snippet_truncated`` so the
  page can say so instead of dropping the tail;
* ``key`` / ``location`` / the timestamps stay silently capped on purpose: they
  are server-derived labels (``kN``, ``PDF p.3``, an ISO instant), not user text,
  so there is no user-authored tail for a cap to eat.
"""
from __future__ import annotations

from typing import Any, Sequence

MAX_REFERENCES = 500
MAX_SNIPPET_CHARS = 1200
# Per-reference title / original-file-name cap.  Named (it used to be an inline
# ``400`` in two places) because the exact value is registered in
# ``docs/product-and-api*.md`` and the truncation flags below refer to it.
MAX_REFERENCE_TITLE_CHARS = 400


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _text_flag(value: Any, limit: int) -> tuple[str, bool]:
    """Trimmed value capped at ``limit``, plus whether the cap actually bit.

    The public projection must DISCLOSE truncation of an evidence field, never
    drop the tail silently (AGENTS.md 用户编辑的数据不得静默截断).  The bool lets
    the page mark a reference whose title/filename/excerpt was clipped."""
    text = str(value or "").strip()
    return text[:limit], len(text) > limit


def _question_text(value: Any) -> str:
    """The research question, served WHOLE — never truncated.

    The value is ``reports.question``, the create-time question: confirmation
    writes its edited ``resolved_question`` into ``understanding`` (which this
    projection deliberately never exposes) and never rewrites the column.  It is
    the user's own artifact either way, and this module used to cap it at 2,000
    chars — silently dropping the tail of the very text that produced the
    report, which is exactly what AGENTS.md 用户编辑的数据不得静默截断 forbids.

    Serving it whole is only sound because the *create* API bounds it first
    (``models/reports.py::REPORT_QUESTION_MAX_CHARS``).  That is the other half
    of the same red line — 前端显示同一护栏, API 超限明确拒绝 — and without it
    this line would return an arbitrarily large client-controlled string on
    every anonymous request (codex #525 R1 P2).  Note the argument is NOT "the
    body is bigger anyway": ``content_md`` is model-generated and bounded by the
    generation budget, whereas the question is raw client input.

    Same call ``conversation_public_view._question_text`` makes (codex #522 R1).
    """
    return str(value or "").strip()


def public_reference(reference: Any) -> dict[str, Any]:
    """One citation as an anonymous reader sees it: nothing addressable.

    ``title``/``file_name``/``snippet`` stay bounded — they are evidence
    metadata, not the user's own artifact the way the question is — but an
    over-length value sets the matching ``*_truncated`` flag so the page can
    DISCLOSE the clip rather than drop the tail silently."""
    row = reference if isinstance(reference, dict) else {}
    title, title_truncated = _text_flag(
        row.get("source_title") or row.get("label") or row.get("name"),
        MAX_REFERENCE_TITLE_CHARS,
    )
    snippet, snippet_truncated = _text_flag(row.get("snippet"), MAX_SNIPPET_CHARS)
    # The original uploaded filename is client-supplied user data too, so it gets
    # the same disclosure as the title/excerpt.
    file_name, file_name_truncated = _text_flag(
        row.get("source_file_name"), MAX_REFERENCE_TITLE_CHARS
    )
    return {
        "key": _text(row.get("key"), 24),
        "title": title,
        "file_name": file_name,
        "location": _text(row.get("location_label"), 200),
        "snippet": snippet,
        "title_truncated": title_truncated,
        "snippet_truncated": snippet_truncated,
        "file_name_truncated": file_name_truncated,
    }


def public_report_payload(row: dict[str, Any], references: Sequence[Any]) -> dict[str, Any]:
    """Assemble the anonymous view from a token-resolved report row."""
    visible = [
        public_reference(reference) for reference in list(references)[:MAX_REFERENCES]
    ]
    return {
        "question": _question_text(row.get("question")),
        "content_md": str(row.get("content_md") or ""),
        "created_at": _text(row.get("created_at"), 64),
        "updated_at": _text(row.get("updated_at"), 64),
        "references": [item for item in visible if item["title"] or item["snippet"]],
        "reference_count": len(visible),
        "truncated_references": len(list(references)) > MAX_REFERENCES,
    }
