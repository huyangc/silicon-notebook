"""Owner-id sets for the per-member overlay deletes (both backends).

The agent-profile blocks, the overlay job rows and the agent observations are
each cleared for a SET of owners in one statement per table (kick-all review
2026-10-23): removing N link members at once must cost a fixed number of write
transactions, not N per table. Removing one member passes a one-element set,
so the single and the notebook-wide removal run the same delete. The set is as
large as the member list being removed, so each store binds it through its
backend's ``id_binding`` module.
"""
from __future__ import annotations

from collections.abc import Iterable


def owner_id_list(owner_ids: Iterable[str]) -> list[str]:
    """The distinct owner ids, in first-seen order. A bare string is refused:
    iterating it would delete per character, never the intended owner."""
    if isinstance(owner_ids, (str, bytes)):
        raise TypeError("owner_ids must be a collection of owner ids, not a string")
    return list(dict.fromkeys(str(owner_id) for owner_id in owner_ids))
