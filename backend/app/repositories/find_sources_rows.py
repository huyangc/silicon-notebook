"""Backend-neutral assembly of ``find_sources_metadata_rows`` (both adapters).

Each adapter issues its own two statements -- the filtered-scope read and the
per-field term legs -- and hands the raw rows here, so "which field matched,
and on a word boundary?" and the row shape are written once
(``app.domain.source_find_terms`` holds the matching rule).  Adapters never
import each other; this module imports neither.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from app.domain.promotion_source import presented_source_title
from app.domain.source_find_terms import (
    field_match,
    keyword_values,
    merge_match,
)

#: The kinds ``find_sources_metadata_rows`` accepts.
FIND_SOURCES_KINDS = ("papers", "all")


def check_kind(kind: str) -> str:
    value = str(kind or "")
    if value not in FIND_SOURCES_KINDS:
        raise ValueError(f"unknown find_sources kind: {kind!r}")
    return value


def _year(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def assemble_rows(
    scope_rows: Iterable[Mapping[str, Any]],
    hit_rows: Iterable[Mapping[str, Any]],
    folded_terms: Sequence[str],
) -> list[dict]:
    """One dict per in-scope source, ``matches`` filled from the term legs.

    ``scope_rows``: ``id, notebook_id, title, source_type, doc_type,
    parse_status, meta_id, is_paper, paper_title, venue, pub_year``.
    ``hit_rows``: ``source_id, field, v1, v2`` (``v2`` is the file name on the
    title leg, ``''`` elsewhere).  A hit for a source outside the scope (hidden
    projection, filtered out, outside the allowed set) is dropped here."""
    rows: dict[str, dict] = {}
    for row in scope_rows:
        source_id = str(row["id"])
        has_meta = row["meta_id"] is not None
        rows[source_id] = {
            "source_id": source_id,
            "notebook_id": str(row["notebook_id"]),
            "title": presented_source_title(
                row["title"], source_type=row["source_type"], source_id=source_id,
            ),
            "source_type": str(row["source_type"] or ""),
            "doc_type": str(row["doc_type"] or ""),
            "parse_status": str(row["parse_status"] or ""),
            "has_meta": has_meta,
            "is_paper": bool(row["is_paper"]) if has_meta else None,
            "paper_title": str(row["paper_title"] or "") if has_meta else "",
            "venue": str(row["venue"] or "") if has_meta else "",
            "pub_year": _year(row["pub_year"]) if has_meta else None,
            "matches": {},
        }
    if not folded_terms:
        return [rows[key] for key in sorted(rows)]
    for hit in hit_rows:
        target = rows.get(str(hit["source_id"]))
        if target is None:
            continue
        field = str(hit["field"])
        if field == "keywords":
            values: list[object] = keyword_values(hit["v1"])
        elif field == "title":
            values = [hit["v1"], hit["v2"], target["title"]]
        else:
            values = [hit["v1"]]
        merge_match(target["matches"], field, field_match(folded_terms, values))
    return [rows[key] for key in sorted(rows)]
