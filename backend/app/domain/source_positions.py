"""The ``source_elements_page(positions=...)`` contract, in one place.

A batched positional read hands back, for a list of zero-based positions in
source-detail order (``created_at, id``), the element that sits at each
position -- in the order the positions were given, one element per position
that exists in the same snapshot as ``total_count``. A position at or past
``total_count`` contributes nothing, so a caller recovers which positions were
answered as ``[p for p in positions if p < page.total_count]`` zipped with
``page.items``.

Both repository adapters build their result with :func:`positions_page`, and
the in-memory test doubles answer through :func:`page_at_positions` over the
element list they hold; the adapter contract tests compare each real adapter
against :func:`page_at_positions` over that adapter's own ``source_elements``.
That is what keeps a test double from becoming a second implementation.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from app.models.sources import PaginatedSourceElements, SourceElement


# The same ceiling the windowed read clamps ``limit`` to. A positional read
# never truncates silently -- dropping positions would drop evidence -- so a
# longer list is a caller error; ``document_source_overview`` splits its own.
MAX_POSITIONS_PER_READ = 100


def normalize_positions(positions: Iterable[int]) -> list[int]:
    """Coerce like ``offset`` does (``max(0, int(p))``) and refuse > 100."""
    values = [max(0, int(position)) for position in positions]
    if len(values) > MAX_POSITIONS_PER_READ:
        raise ValueError(
            f"at most {MAX_POSITIONS_PER_READ} positions per read, got {len(values)}"
        )
    return values


def positions_page(
    items: Sequence[SourceElement], total: int, positions: Sequence[int]
) -> PaginatedSourceElements:
    """The result shape of a positional read.

    ``offset`` is 0 and ``limit`` is the number of positions asked for: no
    window was resolved, and the source-detail view never issues this read.
    """
    return PaginatedSourceElements(
        items=list(items), total_count=int(total), offset=0, limit=len(positions),
    )


def page_at_positions(
    ordered: Sequence[SourceElement], positions: Iterable[int]
) -> PaginatedSourceElements:
    """Answer a positional read from a document's full element list (in
    source-detail order). The reference the adapters are tested against."""
    wanted = normalize_positions(positions)
    total = len(ordered)
    return positions_page(
        [ordered[position] for position in wanted if position < total],
        total, wanted,
    )
