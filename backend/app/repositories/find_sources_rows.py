"""Backend-neutral assembly of ``find_sources_metadata_rows`` (both adapters).

Each adapter issues its own statements -- the counts, the filtered-scope read
and the per-field term legs -- and hands the raw rows here, so "which field
matched, and on a word boundary?", the row shape and the "bind the term legs to
the scope?" rule are written once (``app.domain.source_find_terms`` holds the
matching rule).  Adapters never import each other; this module imports neither.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from app.domain.promotion_source import presented_source_title
from app.domain.source_find_terms import field_match, merge_match, really_narrowed

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


def bind_term_legs(scope_size: int, visible_total: int) -> bool:
    """Should the term legs carry ``member_of(scope ids)``?  Only for a scope
    the filters / allow list REALLY narrowed (``really_narrowed``: binding a
    large share costs more than the hits it drops)."""
    return really_narrowed(scope_size, visible_total)


def scope_rows_dicts(
    scope_rows: Iterable[Mapping[str, Any]], notebook_id: str, *, with_titles: bool,
) -> dict[str, dict]:
    """``{source_id: row}`` of the light scope rows (``id, pub_year,
    pending`` -- plus ``title, source_type, paper_title`` with titles)."""
    rows: dict[str, dict] = {}
    for row in scope_rows:
        source_id = str(row["id"])
        entry = {
            "source_id": source_id,
            "notebook_id": notebook_id,
            "pub_year": _year(row["pub_year"]),
            "pending": bool(row["pending"]),
            "matches": {},
        }
        if with_titles:
            entry["sort_title"] = str(row["paper_title"] or "") or presented_source_title(
                row["title"], source_type=row["source_type"], source_id=source_id,
            )
        rows[source_id] = entry
    return rows


def assemble_scope(
    rows: dict[str, dict],
    hit_rows: Iterable[Mapping[str, Any]],
    folded_terms: Sequence[str],
    *,
    scope_total: int,
    visible_total: int,
    meta_pending_excluded: int,
) -> dict:
    """The method's result: ``rows`` (one light dict per in-scope source, in
    id order, ``matches`` filled from the term legs) and the counts.

    ``hit_rows``: ``source_id, field, v1, v2`` (``v2`` is the file name on the
    title leg, ``''`` elsewhere; on the keywords leg ``v1`` is ONE keyword).  A
    hit for a source outside the scope (hidden projection, filtered out,
    outside the allowed set) is dropped here."""
    for hit in hit_rows if folded_terms else ():
        target = rows.get(str(hit["source_id"]))
        if target is None:
            continue
        field = str(hit["field"])
        values = [hit["v1"], hit["v2"]] if field == "title" else [hit["v1"]]
        merge_match(target["matches"], field, field_match(folded_terms, values))
    ordered = [rows[key] for key in sorted(rows)]
    return {
        "rows": ordered,
        # The unfiltered scope (visible ∧ allowed) and the library's visible
        # sources: a caller tells "the filters narrowed this library" from
        # ``len(rows) < scope_total``.
        "scope_total": int(scope_total),
        "visible_total": int(visible_total),
        "meta_pending_excluded": int(meta_pending_excluded),
    }
