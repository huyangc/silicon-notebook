"""Share-disclosure contract shared by the service and both repository backends (M4).

``services/share_disclosure.py`` computes the count and decides who may
publish; the report store re-takes the count inside the transaction that sets
the share token.  Both need the same typed refusal and the same arithmetic, and
repositories may not import services, so they live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

SHARE_DISCLOSURE_REQUIRED = "share_disclosure_required"


class ShareDisclosureRequired(Exception):
    """The acknowledgement is missing or no longer matches the current count."""

    def __init__(self, memory_count: int, new_memory_count: int) -> None:
        super().__init__(SHARE_DISCLOSURE_REQUIRED)
        self.memory_count = memory_count
        self.new_memory_count = new_memory_count

    def detail(self) -> dict[str, Any]:
        return {
            "code": SHARE_DISCLOSURE_REQUIRED,
            "memory_count": self.memory_count,
            "new_memory_count": self.new_memory_count,
        }


def require_acknowledged(count: int, acknowledged: int | None) -> None:
    """Raise unless nothing is disclosed or ``acknowledged`` equals ``count``.

    ``new_memory_count`` is how many more entries the page now carries than
    were acknowledged (all of them when nothing was); never negative.
    """
    if count and acknowledged != count:
        raise ShareDisclosureRequired(count, max(0, count - (acknowledged or 0)))


@dataclass(frozen=True)
class ShareMemoryGuard:
    """What the report store re-counts inside the share transaction.

    ``known_memory_ids`` are facts stored on the report itself (citations that
    are a Memory, and citations recorded at generation time as coming from the
    author's Memory); they cannot change.  ``live_source_ids`` are the cited
    sources without such a record, looked up as the author's Memory sources
    on the store's own connection, in the same transaction as the flag flip.
    """

    author_id: str
    known_memory_ids: frozenset[str]
    live_source_ids: tuple[str, ...]
    acknowledged: int | None

    def check(self, live_memory_ids: Iterable[str]) -> None:
        count = len(self.known_memory_ids | set(live_memory_ids))
        require_acknowledged(count, self.acknowledged)
