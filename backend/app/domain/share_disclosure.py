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

# Where a report records the author's Memory whose content entered a prompt
# while it was produced (M4).  Both keys are written only when non-empty, so a
# report that used no Memory keeps its stored bytes.
#
# * ``understanding_json[REPORT_PLANNING_MEMORY_KEY]``: Memory shown to the
#   outline planner (the corpus map).  Planning happens before generation,
#   possibly days before, and ``understanding_json`` is the only report column
#   that survives from planning to the finished report.  The key is owned by
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
