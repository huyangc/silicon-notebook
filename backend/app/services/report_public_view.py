"""The projection a shared report exposes to anonymous readers.

This module is the disclosure boundary for public share links.  It is written
as an explicit allowlist rather than a redaction pass: anything a future change
adds to the stored report stays private until it is named here.

What a public reader gets, and why:

* the question, the body, and the timing — that is the artifact being shared;
* per citation: label, display title, location, and the stored excerpt, so the
  ``[k]`` markers in the body can actually be checked against something;
* on a citation of the author's own personal memory, the boolean ``is_memory``
  (absent otherwise) and the title without the ``Memory · `` label prefix
  (M4: the author agreed to publish it; the page says whose memory it is).

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

* the question is served whole up to the create rail and, past it (only
  reachable for pre-rail rows), bounded with ``question_truncated`` — see
  ``_question_text``;
* a reference title / original filename / excerpt stays bounded (it is evidence
  metadata, not the user's own artifact) but an over-length value sets
  ``title_truncated`` / ``file_name_truncated`` / ``snippet_truncated`` so the
  page can say so instead of dropping the tail;
* ``key`` / ``location`` / the timestamps stay silently capped on purpose: they
  are server-derived labels (``kN``, ``PDF p.3``, an ISO instant), not user text,
  so there is no user-authored tail for a cap to eat.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.models.reports import REPORT_QUESTION_MAX_CHARS
# M4: which citation is the author's personal memory, and how its title reads
# in public -- one definition shared with the conversation projection.
from app.services.share_disclosure import (
    public_memory_title,
    reference_memory_id,
    unresolved_source_ids,
)

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


def _question_text(value: Any) -> tuple[str, bool]:
    """The research question plus whether a legacy row forced a bound.

    The value is ``reports.question``, the create-time question: confirmation
    writes its edited ``resolved_question`` into ``understanding`` (which this
    projection deliberately never exposes) and never rewrites the column.  It is
    the user's own artifact, and this module used to cap it at 2,000 chars —
    silently dropping the tail of the very text that produced the report, which
    is exactly what AGENTS.md 用户编辑的数据不得静默截断 forbids.

    So it is served **whole up to the create rail** and, past it, bounded *with
    disclosure* rather than either silently clipped or unbounded:

    * for anything creatable today the flag can never fire — the create API
      refuses a longer question (``REPORT_QUESTION_MAX_CHARS``), so this is the
      identical "served whole" behavior, byte for byte;
    * a report created *before* that rail existed can still hold a longer
      question, and its already-issued share link would otherwise return an
      arbitrarily large client-controlled string on every anonymous request
      (codex #525 R2 P2).  Bounding it here makes the projection self-bounded
      whatever the column holds, and ``question_truncated`` keeps the user's
      data from disappearing quietly.

    Deliberately no migration: rewriting a stored question would modify the
    user's own data to fix a projection problem.

    Note the bound's justification is NOT "the body is bigger anyway":
    ``content_md`` is model-generated and bounded by the generation budget,
    whereas the question is raw client input.
    """
    return _text_flag(value, REPORT_QUESTION_MAX_CHARS)


def report_memory_lookup_source_ids(references: Sequence[Any]) -> list[str]:
    """Distinct ``source_id``s of the citations the page will show whose Memory
    identity is not stored on the report (a Memory citation, or one the engine
    recorded as the author's Memory): the ids the route resolves against the
    author's Memory sources in ONE batch per page."""
    return unresolved_source_ids(list(references)[:MAX_REFERENCES])


def public_reference(
    reference: Any, memory_sources: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """One citation as an anonymous reader sees it: nothing addressable.

    ``title``/``file_name``/``snippet`` stay bounded — they are evidence
    metadata, not the user's own artifact the way the question is — but an
    over-length value sets the matching ``*_truncated`` flag so the page can
    DISCLOSE the clip rather than drop the tail silently.

    M4: a citation of the author's own personal memory (``object_type ==
    "memory"``, a recorded ``memory_id``, or a source in ``memory_sources``,
    the author's Memory sources the route read once for the page) carries
    ``is_memory: True`` and its title without the ``Memory · `` label prefix;
    any other citation carries neither (the key is absent, not false)."""
    row = reference if isinstance(reference, dict) else {}
    is_memory = bool(reference_memory_id(row, memory_sources or {}))
    raw_title = str(
        row.get("source_title") or row.get("label") or row.get("name") or ""
    ).strip()
    title, title_truncated = _text_flag(
        public_memory_title(raw_title) if is_memory else raw_title,
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
        **({"is_memory": True} if is_memory else {}),
    }


def public_report_payload(
    row: dict[str, Any],
    references: Sequence[Any],
    memory_sources: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the anonymous view from a token-resolved report row."""
    visible = [
        public_reference(reference, memory_sources)
        for reference in list(references)[:MAX_REFERENCES]
    ]
    question, question_truncated = _question_text(row.get("question"))
    return {
        "question": question,
        # Only ever True for a report created before the create rail existed.
        "question_truncated": question_truncated,
        "content_md": str(row.get("content_md") or ""),
        "created_at": _text(row.get("created_at"), 64),
        "updated_at": _text(row.get("updated_at"), 64),
        "references": [item for item in visible if item["title"] or item["snippet"]],
        "reference_count": len(visible),
        "truncated_references": len(list(references)) > MAX_REFERENCES,
    }
