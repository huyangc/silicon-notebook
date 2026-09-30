"""Share-disclosure contract shared by the service and both repository backends (M4).

``services/share_disclosure.py`` computes the count and decides who may
publish; the report store re-takes the count inside the transaction that sets
the share token.  Both need the same typed refusal and the same arithmetic, and
repositories may not import services, so they live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

SHARE_DISCLOSURE_REQUIRED = "share_disclosure_required"

# Where a report records the author's Memory whose content entered a prompt
# while it was produced (M4).  Both keys are written only when non-empty, so a
# report that used no Memory keeps its stored bytes.
#
# * ``understanding_json[REPORT_PLANNING_MEMORY_KEY]``: Memory the planning
#   prompts may have carried (the corpus map's Memory lines, and every Memory
#   source retrieval handed the planner), joined before completion by the
#   generation run's own (every Memory source retrieval handed the run: the
#   deep-dive agent's observations, the synthesis payload).  Planning happens
#   before generation, possibly days before, and ``understanding_json`` is the
#   only report column that survives from planning to the finished report.
#   The key is owned by
#   the report store: every later understanding write that does not carry it
#   keeps the stored value (like ``_generation_started_at``), and ``row_to_dict``
#   takes it out of ``understanding``.  A new intent claim starts a new plan and
#   drops it with the rest of the old understanding.
# * ``sections_json[i][SECTION_MEMORY_KEY]``: Memory that entered section i's
#   drafting prompt, written atomically with the finished report.
#
# ``row_to_dict`` removes both from what it returns and hands the union over as
# ``report["memory_used"]``; the report detail API and the public page never
# carry it (neither names the field).
REPORT_PLANNING_MEMORY_KEY = "_memory_used"
SECTION_MEMORY_KEY = "memory_used"
REPORT_MEMORY_USED_FIELD = "memory_used"


# The per-citation record the report engine writes (``_record_memory_use``):
# internal handles, never shown on any surface — the public projection is an
# allowlist, and the admin audit detail strips them (``without_memory_record``).
CITATION_MEMORY_RECORD_FIELDS = ("memory_id", "memory_owner_id")


def without_memory_record(reference: object) -> object:
    """A stored citation without the engine's Memory record fields."""
    if not isinstance(reference, Mapping):
        return reference
    return {
        key: value for key, value in reference.items()
        if key not in CITATION_MEMORY_RECORD_FIELDS
    }


def _memory_ids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def split_report_memory_use(
    understanding: dict, sections: list
) -> tuple[dict, list, list[str]]:
    """Take the recorded Memory use out of a stored report's understanding and
    sections; return both without it plus the sorted distinct Memory ids."""
    used = set(_memory_ids(understanding.pop(REPORT_PLANNING_MEMORY_KEY, None)))
    visible: list = []
    for section in sections:
        if isinstance(section, dict) and SECTION_MEMORY_KEY in section:
            section = dict(section)
            used.update(_memory_ids(section.pop(SECTION_MEMORY_KEY)))
        visible.append(section)
    return understanding, visible, sorted(used)


@runtime_checkable
class MemorySourceReader(Protocol):
    """The report engine's own seat for recording which Memory a report
    carries (M4).  Wired from the repository runtime's Memory store; checked
    at wiring time (``isinstance``), so a store or test double without these
    reads fails when the engine is built, never in the middle of a report."""

    def memory_sources_for_source_ids(
        self, source_ids: Sequence[str], owner_id: str
    ) -> Mapping[str, str]: ...

    def foreign_memory_sources_for_source_ids(
        self, source_ids: Sequence[str], member_id: str
    ) -> Mapping[str, tuple[str, str]]: ...


class ShareDisclosureRequired(Exception):
    """The page may carry more of the author's Memory than was acknowledged."""

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
    """Raise when ``count`` is above what was acknowledged (nothing counts as 0).

    A count at or below the acknowledgement is accepted: the author agreed to
    at least that much.  It falls when a cited Memory can no longer be
    recognised (in a report generated before recording existed, its Memory
    source was removed), while the page still carries the stored excerpt the
    author saw counted — so refusing would only ask the author to acknowledge
    a smaller number for the same page.  Only a HIGHER count asks again; that
    is the direction the count taken inside the share transaction protects.

    ``new_memory_count`` is how many more entries the page may carry than were
    acknowledged (all of them when nothing was); always at least 1.
    """
    if count > (acknowledged or 0):
        raise ShareDisclosureRequired(count, count - (acknowledged or 0))


@dataclass(frozen=True)
class ShareMemoryGuard:
    """What the report store re-counts inside the share transaction.

    ``known_memory_ids`` are facts stored on the report itself (citations that
    are a Memory, citations recorded at generation time as coming from the
    author's Memory, and the Memory recorded as used while the report was
    produced); they cannot change.  ``live_source_ids`` are the cited
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
