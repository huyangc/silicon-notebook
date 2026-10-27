"""Owner-id sets for the per-member overlay deletes (both backends).

The agent-profile blocks, the overlay job rows and the agent observations are
each cleared for a SET of owners in one statement per table (kick-all review
2026-10-23): removing N link members at once must cost a fixed number of write
transactions, not N per table. Removing one member passes a one-element set,
so the single and the notebook-wide removal run the same delete.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator

# SQLite binds one variable per id; a fixed bound keeps every statement well
# under SQLITE_MAX_VARIABLE_NUMBER whatever the build's limit is.
SQLITE_OWNER_IN_CHUNK = 500


def owner_id_list(owner_ids: Iterable[str]) -> list[str]:
    """The distinct owner ids, in first-seen order. A bare string is refused:
    iterating it would delete per character, never the intended owner."""
    if isinstance(owner_ids, (str, bytes)):
        raise TypeError("owner_ids must be a collection of owner ids, not a string")
    return list(dict.fromkeys(str(owner_id) for owner_id in owner_ids))


def sqlite_owner_chunks(owner_ids: list[str]) -> Iterator[list[str]]:
    for offset in range(0, len(owner_ids), SQLITE_OWNER_IN_CHUNK):
        yield owner_ids[offset:offset + SQLITE_OWNER_IN_CHUNK]


def sqlite_in_placeholders(count: int) -> str:
    return ",".join("?" * count)
