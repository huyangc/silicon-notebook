"""Share disclosure: how many of the author's own Memory entries a public page carries (M4).

Ruling M4: publishing a report or conversation that cites the author's own
Memory asks the author first, with the count.  This module is the single
server-side definition of that count and of the publish rule built on it; the
frontend shows the number it is given and never counts Memory itself.

The count
---------
The number of DISTINCT Memory entries of the author that the public page would
carry, the union of:

* citations that ARE a Memory (``object_type == "memory"``, id in
  ``object_id``) — the author's confirmed Memory injected into the run;
* citations the report engine recorded at generation time as coming from the
  author's Memory projection (``memory_id`` + ``memory_owner_id`` on the stored
  citation, see ``ReportEngine._record_memory_citations``) — counted from the
  record, so deleting the Memory or its projection later does not hide them;
* for citations without that record (reports generated before it existed):
  cited ``source_id``s that are, right now, the author's Memory sources,
  resolved by ``memory_store.memory_ids_for_source_ids(source_ids, author_id)``
  on ``memory_sql.memory_source_readable``.  A pre-record report whose
  Memory source is already gone cannot be recognised any more.

Stored facts count as stored: the page publishes the stored excerpt whatever
has happened to the Memory since.  Every lookup resolves only to Memory created
by the author, so the number cannot reveal anything about another member.

The publish rule
----------------
* count 0: nothing to disclose; publishing is exactly what it was before M4.
* count > 0 and the requester is not the author: refused (403 at the route).
* count > 0 and the acknowledgement is absent or differs: ``ShareDisclosureRequired``
  carrying the current count (409 at the route).

The route checks this on the publishing request and then hands
``share_memory_guard`` to ``share_report``, which re-counts the live part in
the same transaction that sets the share token and refuses with the same
typed error if it changed in between.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from app.domain.share_disclosure import (
    SHARE_DISCLOSURE_REQUIRED,
    ShareDisclosureRequired,
    ShareMemoryGuard,
    require_acknowledged,
)

__all__ = [
    "SHARE_DISCLOSURE_REQUIRED",
    "MemoryIdReader",
    "NonAuthorShareRefused",
    "ShareDisclosure",
    "ShareDisclosureRequired",
    "ShareMemoryGuard",
    "report_share_disclosure",
    "require_publishable",
    "share_memory_guard",
]

MEMORY_OBJECT_TYPE = "memory"


class MemoryIdReader(Protocol):
    def memory_ids_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> list[str]: ...


@dataclass(frozen=True)
class ShareDisclosure:
    """The author, the distinct Memory ids a public page would carry, and the
    parts the count was built from (stored facts vs. live-resolved sources)."""

    author_id: str
    memory_ids: tuple[str, ...]
    known_memory_ids: frozenset[str] = frozenset()
    live_source_ids: tuple[str, ...] = ()

    @property
    def memory_count(self) -> int:
        return len(self.memory_ids)


class NonAuthorShareRefused(Exception):
    """Someone other than the author tried to publish the author's Memory.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


def report_share_disclosure(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> ShareDisclosure:
    """Count the author's Memory entries cited by a stored report."""
    author_id = str(report.get("created_by") or "")
    known: set[str] = set()
    live_sources: list[str] = []
    for reference in report.get("references") or ():
        if not isinstance(reference, Mapping):
            continue
        object_id = str(reference.get("object_id") or "")
        if object_id and str(reference.get("object_type") or "") == MEMORY_OBJECT_TYPE:
            known.add(object_id)
        recorded = str(reference.get("memory_id") or "")
        if recorded:
            if author_id and str(reference.get("memory_owner_id") or "") == author_id:
                known.add(recorded)
            continue
        source_id = str(reference.get("source_id") or "")
        if source_id:
            live_sources.append(source_id)
    live_sources = list(dict.fromkeys(live_sources))
    memory_ids = set(known)
    if live_sources and author_id:
        memory_ids.update(memory_reader.memory_ids_for_source_ids(live_sources, author_id))
    return ShareDisclosure(
        author_id=author_id,
        memory_ids=tuple(sorted(memory_ids)),
        known_memory_ids=frozenset(known),
        live_source_ids=tuple(live_sources),
    )


def require_publishable(
    disclosure: ShareDisclosure, *, requester_id: str, acknowledged: int | None
) -> None:
    """Raise unless ``requester_id`` may publish now with ``acknowledged``."""
    if disclosure.memory_count == 0:
        return
    if not requester_id or requester_id != disclosure.author_id:
        raise NonAuthorShareRefused()
    require_acknowledged(disclosure.memory_count, acknowledged)


def share_memory_guard(
    disclosure: ShareDisclosure, acknowledged: int | None
) -> ShareMemoryGuard:
    """What ``share_report`` re-checks inside its transaction."""
    return ShareMemoryGuard(
        author_id=disclosure.author_id,
        known_memory_ids=disclosure.known_memory_ids,
        live_source_ids=disclosure.live_source_ids,
        acknowledged=acknowledged,
    )
