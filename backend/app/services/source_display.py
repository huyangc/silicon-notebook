"""Frozen source display-title contract.

One rule with one definition point: a grounded paper title outranks the name
the file was uploaded under; every other source keeps its ordinary source
title, then its file name.

Three call sites name sources for the user, and they name the *same* sources in
the same session — the answer's ``[k]`` citation cards
(``evidence_context.EvidenceContextService.citation_titles``), the element
candidates the reasoning retriever scores and renders as evidence
(``retrieval_candidates``), and the typed-collection list
(``collection_enumeration``).  Each arrived separately and each wrote the rule
out again; the failure that costs is not the duplication but its drift, which
is visible to the user in one screen: the same paper listed as "1706.03762.pdf"
on a list card and as its parsed title on the citation card next to it.

A leaf on purpose.  It imports nothing from the application, so the citation
path, the retrieval path and the enumeration path each depend on it without
depending on one another.

``test_source_display_title.py`` holds the source-level guard that keeps this
the only copy.
"""
from __future__ import annotations

from typing import Any


def _columns(row: Any, *keys: str) -> dict[str, Any]:
    """Read the named columns off a source row, whatever shape it arrives in.

    Subscript rather than ``.get``: ``source_metadata`` returns plain dicts but
    ``source_display_rows`` hands back driver rows on the caller's connection,
    and ``sqlite3.Row`` has no ``.get``.  A column the row does not carry reads
    as ``None`` instead of raising — the citation path passes ``{}`` for a
    source whose metadata row was not found, and a source with no name is a
    display detail, not an error.
    """
    values: dict[str, Any] = {}
    for key in keys:
        try:
            values[key] = row[key]
        except (KeyError, IndexError, TypeError):
            values[key] = None
    return values


def source_display_title(row: Any) -> str:
    """The user-facing title of one source row, or ``""`` if it has no name.

    The parsed paper title wins only when the row is marked as a paper *and*
    that title is nonblank: ``is_paper`` alone can sit on a row whose title
    extraction was rejected by the grounding check, and a whitespace-only title
    is not a title.  Callers that must not show an unnamed source test the
    empty string; nothing here invents a placeholder.

    The ordinary branch picks before it strips, so a whitespace-only ``title``
    shadows ``file_name`` and yields ``""``.  That is what all three call sites
    did before they shared this function and it is pinned by
    ``test_whitespace_only_source_title_still_beats_the_file_name``: changing it
    is a user-visible behaviour change and belongs to its own change, not to
    the consolidation.
    """
    values = _columns(row, "is_paper", "paper_title", "title", "file_name")
    paper_title = str(values["paper_title"] or "").strip()
    if values["is_paper"] and paper_title:
        return paper_title
    return str(values["title"] or values["file_name"] or "").strip()
