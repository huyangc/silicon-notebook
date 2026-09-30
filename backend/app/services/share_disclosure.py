"""Report share disclosure: how much of the author's own Memory a public report
may carry, and whether it may be published at all (M4).

Ruling M4: publishing a report that cites the author's own Memory asks the
author first, with the count.  This module is the single server-side
definition of that count for reports and of the publish rule built on it; the
report page shows the number it is given and never counts Memory itself.
(Conversations are not covered here yet; their count is a later task.)

The count
---------
The number of DISTINCT Memory entries of the author whose content the public
page may carry — cited or merely used — the union of:

* citations that ARE a Memory (``object_type == "memory"``, id in
  ``object_id``) — the author's confirmed Memory injected into the run;
* citations the report engine recorded at generation time as coming from the
  author's Memory projection (``memory_id`` + ``memory_owner_id`` equal to the
  author on the stored citation) — counted from the record, so deleting the
  Memory or its projection later does not hide them;
* the author's Memory recorded as USED while the report was produced
  (``report["memory_used"]``): the planner's Memory lines, each section's
  confirmed-Memory block, and the Memory behind every source retrieval handed
  the planning and generation runs (corpus map and probes, the section
  deep-dive agent's observations, the synthesis payload, the section
  contexts — cited or not; ``app.services.report_memory_use``).  The body can
  restate any of it without a citation marker, so the count covers it;
* for citations without a record (reports generated before recording
  existed): cited ``source_id``s that are, right now, the author's Memory
  sources, resolved by ``memory_store.memory_ids_for_source_ids(source_ids,
  author_id)`` on ``memory_sql.memory_source_readable``.

What a report generated before recording cannot tell: which Memory its planner
and drafting prompts used (only its citations can be recognised), and — once a
cited Memory source is gone — that the citation came from Memory at all.

Stored facts count as stored: the page publishes the stored excerpt whatever
has happened to the Memory since.

Another member's Memory
-----------------------
A report that cites another member's Memory source (recorded with
``memory_owner_id`` not the author, or — without a record — a cited source
that is right now a Memory source of another member,
``memory_store.foreign_memory_sources_for_source_ids``) is never published:
the other member was never asked — and a link issued before this rule stops
serving it: the anonymous page asks ``report_foreign_memory_ids`` on every
open.  ``foreign_memory_ids`` carries those
Memory ids; the author already reads the excerpts inside their own report, so
refusing reveals nothing new.  Body text restating another member's Memory
without a citation cannot be recognised in a report generated before the
retrieval fixes that stop such Memory from reaching a report at all.

The publish rule
----------------
* another member's Memory cited: refused (403 at the route), whatever the count.
* count 0: nothing to disclose; publishing is exactly what it was before M4.
* count > 0 and the requester is not the author: refused (403 at the route).
* count above the acknowledgement (an absent one counts as 0):
  ``ShareDisclosureRequired`` carrying the current count (409 at the route).
  A count at or below it is accepted: the author agreed to at least that much,
  and the count of a report generated before recording can only FALL (a cited
  Memory source removed), while its page still carries the excerpt the author
  saw counted.
* already public: re-sharing publishes nothing new; the existing link is
  returned without asking (another member's Memory is still refused).

The route checks this on the publishing request and then hands
``share_memory_guard`` to ``share_report``, which re-counts the live part in
the same transaction that sets the share token and refuses with the same
typed error if it ROSE in between — that upward direction is what the lock
and the in-transaction count protect.  The foreign check is not repeated there:
a cited source cannot become another member's Memory source (Memory source ids
are minted fresh and a Memory's creator never changes), it can only stop being
one, which makes publishing less restricted, never more.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from app.domain.share_disclosure import (
    REPORT_MEMORY_USED_FIELD,
    SHARE_DISCLOSURE_REQUIRED,
    ShareDisclosureRequired,
    ShareMemoryGuard,
    require_acknowledged,
)

__all__ = [
    "SHARE_DISCLOSURE_REQUIRED",
    "ForeignMemoryShareRefused",
    "MemoryIdReader",
    "NonAuthorShareRefused",
    "ShareDisclosure",
    "ShareDisclosureRequired",
    "ShareMemoryGuard",
    "report_foreign_memory_ids",
    "report_share_disclosure",
    "require_publishable",
    "share_memory_guard",
]

MEMORY_OBJECT_TYPE = "memory"


class MemoryIdReader(Protocol):
    def memory_ids_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> list[str]: ...

    def foreign_memory_sources_for_source_ids(
        self, source_ids: Sequence[str], member_id: str
    ) -> Mapping[str, tuple[str, str]]: ...


@dataclass(frozen=True)
class ShareDisclosure:
    """The author, the distinct Memory ids of the author a public page may
    carry, the parts the count was built from (stored facts vs. live-resolved
    sources), and the Memory ids of other members the report cites."""

    author_id: str
    memory_ids: tuple[str, ...]
    known_memory_ids: frozenset[str] = frozenset()
    live_source_ids: tuple[str, ...] = ()
    foreign_memory_ids: tuple[str, ...] = ()

    @property
    def memory_count(self) -> int:
        return len(self.memory_ids)

    @property
    def foreign_memory_count(self) -> int:
        return len(self.foreign_memory_ids)


class NonAuthorShareRefused(Exception):
    """Someone other than the author tried to publish the author's Memory.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


class ForeignMemoryShareRefused(Exception):
    """The report cites another member's Memory; it is never published.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


def report_foreign_memory_ids(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> tuple[str, ...]:
    """Other members' Memory a stored report cites: recorded citations whose
    owner is not the author, and — for unrecorded citations — cited sources
    that are right now another member's Memory sources.

    The anonymous report page asks this on every open, so a link issued before
    the rule stops serving such a report (the same 404 as a revoked link)."""
    author_id, _known, foreign, live_sources = _classified_references(report)
    if live_sources and author_id:
        foreign.update(
            memory_id for memory_id, _owner in memory_reader
            .foreign_memory_sources_for_source_ids(live_sources, author_id).values()
        )
    return tuple(sorted(foreign))


def _classified_references(
    report: Mapping[str, Any],
) -> tuple[str, set[str], set[str], list[str]]:
    """(author, the author's known Memory ids, other members' recorded Memory
    ids, cited sources without a record) of a stored report."""
    author_id = str(report.get("created_by") or "")
    known: set[str] = {
        str(item) for item in report.get(REPORT_MEMORY_USED_FIELD) or ()
        if isinstance(item, str) and item
    }
    foreign: set[str] = set()
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
            else:
                foreign.add(recorded)
            continue
        source_id = str(reference.get("source_id") or "")
        if source_id:
            live_sources.append(source_id)
    return author_id, known, foreign, list(dict.fromkeys(live_sources))


def report_share_disclosure(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> ShareDisclosure:
    """Count the author's Memory a stored report carries, and find any other
    member's Memory it cites."""
    author_id, known, foreign, live_sources = _classified_references(report)
    memory_ids = set(known)
    if live_sources and author_id:
        memory_ids.update(memory_reader.memory_ids_for_source_ids(live_sources, author_id))
        foreign.update(
            memory_id for memory_id, _owner in memory_reader
            .foreign_memory_sources_for_source_ids(live_sources, author_id).values()
        )
    return ShareDisclosure(
        author_id=author_id,
        memory_ids=tuple(sorted(memory_ids)),
        known_memory_ids=frozenset(known),
        live_source_ids=tuple(live_sources),
        foreign_memory_ids=tuple(sorted(foreign)),
    )


def require_publishable(
    disclosure: ShareDisclosure,
    *,
    requester_id: str,
    acknowledged: int | None,
    already_shared: bool = False,
) -> None:
    """Raise unless ``requester_id`` may publish now with ``acknowledged``.

    ``already_shared``: the report is public already, so re-sharing publishes
    nothing new and the acknowledgement is not checked here; the store hands
    back the existing link, and if the link was revoked in the meantime it
    checks the acknowledgement itself (``share_memory_guard``).  Another
    member's Memory is refused either way.
    """
    if disclosure.foreign_memory_ids:
        raise ForeignMemoryShareRefused()
    if disclosure.memory_count == 0:
        return
    if not requester_id or requester_id != disclosure.author_id:
        raise NonAuthorShareRefused()
    if not already_shared:
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
