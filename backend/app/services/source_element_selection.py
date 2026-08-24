"""Shared source-element cleanup and ranked-selection rules.

Raw parsers preserve physical page structure, while answer synthesis needs
content diversity.  This module keeps those concerns compatible: ingestion
removes only repeated page-boundary artifacts, and ranked retrieval also
deduplicates normalized text defensively for historical/external-parser data.
"""

from __future__ import annotations

import unicodedata
from typing import AbstractSet, Sequence


# Structural parser invariants, not deployment quality/cost knobs.  Requiring a
# repeated boundary on at least half of the represented pages keeps ordinary
# repeated prose, formulas, table rows, and same-page repetitions untouched.
_REPEATED_BOUNDARY_MIN_PAGES = 3
_REPEATED_BOUNDARY_COVERAGE_NUMERATOR = 1
_REPEATED_BOUNDARY_COVERAGE_DENOMINATOR = 2
_PAGE_BOUNDARY_TEXT_TYPES = frozenset({"heading", "paragraph", "page_text"})


def normalized_source_element_text(element: object) -> str:
    """Normalize layout-only differences without fuzzy semantic rewriting."""
    text = unicodedata.normalize(
        "NFKC", str(getattr(element, "text", "") or "")
    )
    return " ".join(text.split()).casefold()


def source_element_content_key(element: object) -> tuple[str, str]:
    """Stable within-source identity for ranked raw-element diversity.

    Empty-text rows fall back to their element id so malformed/structural rows
    are never all merged into one anonymous item.  The source id remains part of
    the key because identical statements in different documents are independent
    provenance and must remain eligible as corroborating evidence.
    """
    source_id = str(getattr(element, "source_id", "") or "")
    normalized = normalized_source_element_text(element)
    if normalized:
        return source_id, normalized
    element_id = str(getattr(element, "element_id", "") or "")
    return source_id, f"\x00{element_id}"


def source_chunk_content_key(chunk: object) -> tuple[str, str]:
    """Within-source normalized content identity for chunk candidates."""
    source_id = str(getattr(chunk, "source_id", "") or "")
    text = unicodedata.normalize(
        "NFKC", str(getattr(chunk, "text", "") or "")
    )
    normalized = " ".join(text.split()).casefold()
    if normalized:
        return source_id, normalized
    chunk_id = str(getattr(chunk, "chunk_id", "") or "")
    return source_id, f"\x00{chunk_id}"


def rank_source_elements(
    elements: Sequence[object],
    keep: int,
    *,
    excluded_content_keys: AbstractSet[tuple[str, str]] = frozenset(),
    priority_element_ids: AbstractSet[str] = frozenset(),
) -> list:
    """Rank source elements while giving duplicate text one bounded slot.

    The best-scoring representative wins within one source; ``element_id`` is
    the deterministic tie-break.  Approved outline bindings may receive
    priority, but still occupy the caller's closed cap.
    """
    cap = max(0, int(keep))
    if cap == 0:
        return []
    best_by_content: dict[tuple[str, str], object] = {}

    def rank_key(element: object) -> tuple[float, str]:
        return (
            -float(getattr(element, "score", 0.0) or 0.0),
            str(getattr(element, "element_id", "") or ""),
        )

    def is_priority(element: object) -> bool:
        return str(getattr(element, "element_id", "") or "") in priority_element_ids

    for element in elements:
        content_key = source_element_content_key(element)
        if content_key in excluded_content_keys:
            continue
        current = best_by_content.get(content_key)
        if (
            current is None
            or (is_priority(element) and not is_priority(current))
            or (
                is_priority(element) == is_priority(current)
                and rank_key(element) < rank_key(current)
            )
        ):
            best_by_content[content_key] = element

    representatives = list(best_by_content.values())
    priority = sorted(
        (element for element in representatives if is_priority(element)),
        key=rank_key,
    )
    others = sorted(
        (element for element in representatives if not is_priority(element)),
        key=rank_key,
    )
    return (priority + others)[:cap]


def rank_source_chunks(chunks: Sequence[object], keep: int) -> list:
    """Rank chunks after collapsing normalized duplicates within one source."""
    cap = max(0, int(keep))
    if cap == 0:
        return []
    best_by_content: dict[tuple[str, str], object] = {}

    def rank_key(chunk: object) -> tuple[float, str]:
        return (
            -float(
                getattr(chunk, "relevance", 0.0)
                or getattr(chunk, "score", 0.0)
                or 0.0
            ),
            str(getattr(chunk, "chunk_id", "") or ""),
        )

    for chunk in chunks:
        content_key = source_chunk_content_key(chunk)
        current = best_by_content.get(content_key)
        if current is None or rank_key(chunk) < rank_key(current):
            best_by_content[content_key] = chunk
    return sorted(best_by_content.values(), key=rank_key)[:cap]


def deduplicate_source_chunks_in_order(chunks: Sequence[object]) -> list:
    """Keep the first same-source text representative without reordering.

    Exact section lookup deliberately preserves document order for equal-score
    chunks.  This variant is therefore the final context-assembly backstop:
    duplicate rows cannot spend the character budget, while distinct rows keep
    the retrieval channel's intentional ordering.
    """
    seen: set[tuple[str, str]] = set()
    distinct: list = []
    for chunk in chunks:
        content_key = source_chunk_content_key(chunk)
        if content_key in seen:
            continue
        seen.add(content_key)
        distinct.append(chunk)
    return distinct


def _page_number(element: object) -> int | None:
    metadata = getattr(element, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    try:
        page = int(metadata.get("page_number"))
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def deduplicate_repeated_page_boundaries(
    elements: Sequence[object],
) -> tuple[list, int]:
    """Remove repeated running headers/footers while preserving one locator.

    Only normalized text that appears at a textual page boundary on at least
    half of three or more represented pages is eligible.  Non-boundary copies,
    repeated tables/formulas/images/code, and repetitions confined to one page
    are preserved.  The first boundary occurrence remains available for source
    browsing and provenance; downstream chunks, embeddings, and KG extraction
    therefore receive one representative instead of one copy per page.
    """
    materialized = list(elements)
    textual_by_page: dict[int, list[int]] = {}
    for index, element in enumerate(materialized):
        page = _page_number(element)
        if page is None:
            continue
        if str(getattr(element, "element_type", "") or "") not in _PAGE_BOUNDARY_TEXT_TYPES:
            continue
        if not normalized_source_element_text(element):
            continue
        textual_by_page.setdefault(page, []).append(index)

    represented_pages = len(textual_by_page)
    if represented_pages < _REPEATED_BOUNDARY_MIN_PAGES:
        return materialized, 0

    boundary_occurrences: dict[str, list[tuple[int, int]]] = {}
    for page, indices in textual_by_page.items():
        boundary_indices = {indices[0], indices[-1]}
        for index in boundary_indices:
            normalized = normalized_source_element_text(materialized[index])
            boundary_occurrences.setdefault(normalized, []).append((page, index))

    suppressed: set[int] = set()
    for occurrences in boundary_occurrences.values():
        pages = {page for page, _index in occurrences}
        if len(pages) < _REPEATED_BOUNDARY_MIN_PAGES:
            continue
        if (
            len(pages) * _REPEATED_BOUNDARY_COVERAGE_DENOMINATOR
            < represented_pages * _REPEATED_BOUNDARY_COVERAGE_NUMERATOR
        ):
            continue
        ordered = sorted(occurrences, key=lambda item: item[1])
        suppressed.update(index for _page, index in ordered[1:])

    if not suppressed:
        return materialized, 0
    return [
        element for index, element in enumerate(materialized)
        if index not in suppressed
    ], len(suppressed)
