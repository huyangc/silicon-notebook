"""Share disclosure: how many of the author's own Memory entries a public page carries (M4).

Ruling M4: publishing a report or conversation that cites the author's own
Memory asks the author first, with the count.  This module is the single
server-side definition of that count and of the publish rule built on it; the
frontend shows the number it is given and never counts Memory itself.

The count
---------
The number of DISTINCT Memory entries of the author that the public page would
carry, the union of:

* citations that ARE a Memory (``object_type == "memory"``, the id in
  ``object_id``) — the author's confirmed Memory injected into the run;
* citations whose ``source_id`` is one of the author's Memory sources (the
  Memory projection's elements / knowledge objects), mapped to Memory ids by
  ``memory_store.memory_ids_for_source_ids(source_ids, author_id)`` in one
  batched read built on ``memory_sql.memory_source_readable``.

Memory citations are counted as stored: the page publishes the stored excerpt
whatever has happened to the Memory since, so the count follows the excerpt.
The source mapping only ever resolves to Memory created by the author, so the
number cannot reveal whether anything cited belongs to another member.

The publish rule
----------------
* count 0: nothing to disclose; publishing is exactly what it was before M4
  (no acknowledgement asked for, none required).
* count > 0 and the requester is not the author: refused.  The Memory is the
  author's, so only the author decides (403 at the route).
* count > 0 and the acknowledgement is absent or differs from the count taken
  on the publishing request itself: ``ShareDisclosureRequired`` carrying the
  current count (409 at the route, ``share_disclosure_required``).

The caller takes the count on the publishing request, right before it flips
the share flag, and never trusts a number from an earlier request: what the
author confirmed is compared with what is about to be published.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

SHARE_DISCLOSURE_REQUIRED = "share_disclosure_required"
MEMORY_OBJECT_TYPE = "memory"


class MemoryIdReader(Protocol):
    def memory_ids_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> list[str]: ...


@dataclass(frozen=True)
class ShareDisclosure:
    """The author and the distinct Memory ids a public page would carry."""

    author_id: str
    memory_ids: tuple[str, ...]

    @property
    def memory_count(self) -> int:
        return len(self.memory_ids)


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


class NonAuthorShareRefused(Exception):
    """Someone other than the author tried to publish the author's Memory.

    The user-facing sentence belongs to the route (``user_error`` literal)."""


def report_share_disclosure(
    memory_reader: MemoryIdReader, report: Mapping[str, Any]
) -> ShareDisclosure:
    """Count the author's Memory entries cited by a stored report."""
    author_id = str(report.get("created_by") or "")
    memory_ids: set[str] = set()
    source_ids: list[str] = []
    for reference in report.get("references") or ():
        if not isinstance(reference, Mapping):
            continue
        object_id = str(reference.get("object_id") or "")
        if object_id and str(reference.get("object_type") or "") == MEMORY_OBJECT_TYPE:
            memory_ids.add(object_id)
        source_id = str(reference.get("source_id") or "")
        if source_id:
            source_ids.append(source_id)
    if source_ids and author_id:
        memory_ids.update(memory_reader.memory_ids_for_source_ids(source_ids, author_id))
    return ShareDisclosure(author_id=author_id, memory_ids=tuple(sorted(memory_ids)))


def require_publishable(
    disclosure: ShareDisclosure, *, requester_id: str, acknowledged: int | None
) -> None:
    """Raise unless ``requester_id`` may publish now with ``acknowledged``.

    ``new_memory_count`` in the refusal is how many more entries the page now
    carries than the requester acknowledged (all of them when nothing was
    acknowledged); it is never negative.
    """
    count = disclosure.memory_count
    if count == 0:
        return
    if not requester_id or requester_id != disclosure.author_id:
        raise NonAuthorShareRefused()
    if acknowledged != count:
        raise ShareDisclosureRequired(count, max(0, count - (acknowledged or 0)))
