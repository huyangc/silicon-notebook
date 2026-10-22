"""Backend-neutral rule for ``sources.navigation_version`` (SQLite v101 /
PostgreSQL 0082).

The counter goes up by one, in the same transaction, whenever a write changes
the set of a source's chunks flagged ``chunks.navigation``: the build, the
indexing-pipeline publish, the background pass and ``--clear``. The
selected-source snapshot cache keys on it (read with the source row its
generation signature already reads), so a flag change re-keys the cache
without a scan of chunks. Over-counting is harmless (one extra cache miss);
missing a change would serve stale flags, so a write that cannot tell bumps.

Lives here, next to ``chunk_elements``, because adapters must never import
each other.
"""
from __future__ import annotations

from typing import Any, Iterable


def navigation_bump(before_rows: Iterable[Any], after_flagged_ids: Iterable[Any]) -> int:
    """1 when the flagged set changes, else 0.

    ``before_rows``: the source's currently flagged chunk rows (anything
    indexable by ``"id"``); ``after_flagged_ids``: the ids flagged once the
    write lands."""
    before = {str(row["id"]) for row in before_rows}
    after = {str(value) for value in after_flagged_ids}
    return int(before != after)
