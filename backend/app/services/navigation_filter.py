"""Read side of the navigation-page marker: retrieval producers drop
``chunks.navigation`` rows.

The flag is computed once, at chunk build and by the background backfill
(``app.services.navigation_chunks`` / ``navigation_backfill``); every retrieval
producer that turns stored rows into candidates removes flagged ones here.
Dropping, not sinking: MMR, the quota fusers, rerank, the federation merge and
``reasoning_passage_order`` all re-sort by relevance and would lift a
down-weighted page back up.

The read-side switch is ``Settings.retrieval_navigation_filter_enabled``
(``RETRIEVAL_NAVIGATION_FILTER_ENABLED``). Every helper takes it as an
explicit ``enabled`` argument resolved by the caller from its own settings --
there is no module-level state -- so a pure scorer stays pure and the switch
off restores the unflagged result at every producer at once.

A row without the column (an older test double, a projection that does not
read it) counts as not navigation: the marker only ever removes rows the
build positively flagged.
"""
from __future__ import annotations

from typing import Any, Iterable, List, TypeVar

T = TypeVar("T")


def navigation_filter_enabled(settings: Any) -> bool:
    """The read-side switch, from the caller's own settings object."""
    return bool(settings.retrieval_navigation_filter_enabled)


def row_navigation(row: Any) -> bool:
    """``row["navigation"]`` as a bool; a row shape without it reads False.

    Accepts a ``dict`` (PostgreSQL adapters, rebuilt score rows) and a
    ``sqlite3.Row`` (which raises ``IndexError`` for an unknown column)."""
    try:
        value = row["navigation"]
    except (KeyError, IndexError, TypeError):
        return False
    return bool(value)


def chunk_navigation(chunk: Any) -> bool:
    """``chunk.navigation`` (``RetrievedChunk``, ``SourceSubgraphChunk``) as a
    bool; an object without the attribute reads False."""
    return bool(getattr(chunk, "navigation", False))


def drop_navigation_rows(rows: Iterable[T], *, enabled: bool) -> List[T]:
    """``rows`` without the navigation-flagged ones (all of them when the
    switch is off). Order is preserved."""
    values = list(rows)
    if not enabled:
        return values
    return [row for row in values if not row_navigation(row)]


def drop_navigation_chunks(chunks: Iterable[T], *, enabled: bool) -> List[T]:
    """``RetrievedChunk``-like objects without the ones whose ``navigation``
    attribute is set (all of them when the switch is off). Order is
    preserved."""
    values = list(chunks)
    if not enabled:
        return values
    return [chunk for chunk in values if not chunk_navigation(chunk)]
