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
"""
from __future__ import annotations

from typing import Any, Sequence

MAX_REFERENCES = 500
MAX_SNIPPET_CHARS = 1200


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def public_reference(reference: Any) -> dict[str, str]:
    """One citation as an anonymous reader sees it: nothing addressable."""
    row = reference if isinstance(reference, dict) else {}
    return {
        "key": _text(row.get("key"), 24),
        "title": _text(
            row.get("source_title") or row.get("label") or row.get("name"), 400
        ),
        "file_name": _text(row.get("source_file_name"), 400),
        "location": _text(row.get("location_label"), 200),
        "snippet": _text(row.get("snippet"), MAX_SNIPPET_CHARS),
    }


def public_report_payload(row: dict[str, Any], references: Sequence[Any]) -> dict[str, Any]:
    """Assemble the anonymous view from a token-resolved report row."""
    visible = [
        public_reference(reference) for reference in list(references)[:MAX_REFERENCES]
    ]
    return {
        "question": _text(row.get("question"), 2000),
        "content_md": str(row.get("content_md") or ""),
        "created_at": _text(row.get("created_at"), 64),
        "updated_at": _text(row.get("updated_at"), 64),
        "references": [item for item in visible if item["title"] or item["snippet"]],
        "reference_count": len(visible),
        "truncated_references": len(list(references)) > MAX_REFERENCES,
    }
