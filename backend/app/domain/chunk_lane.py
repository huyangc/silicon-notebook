"""Which participants the federated chunk lane searches -- one pure rule.

Two consumers need the same answer and must never drift apart:

* ``chunk_federation._bounded_participants`` -- the fan-out itself, which
  sends one recall leg per kept participant;
* ``NotebookSummaryQuery._chunk_lane_base_ids`` -- the reference-library
  passage half of ``NotebookSummary.ask_available``, which may only count a
  mounted library the lane will actually search.

The rule is the ``CHUNK_FEDERATION_ENABLED`` switch plus the
``CHUNK_FEDERATION_MAX_PARTICIPANTS`` cap over a participant list whose head
is the active notebook and whose tail is in ``MOUNT_ORDER``.  It lives in the
dependency-free domain layer so the catalog can read it without depending on
the retrieval services, and it issues no query: callers hand over the list
they already hold.
"""
from __future__ import annotations

from typing import Any, Sequence, TypeVar

T = TypeVar("T")


def chunk_lane_reads_mounts(settings: Any) -> bool:
    """Does the lane search anything beyond the active notebook?

    False under the rollback switch: the participant set is then the active
    notebook alone, so a caller may skip reading the mounts at all."""
    return bool(settings.chunk_federation_enabled)


def chunk_lane_bound(
    settings: Any, participants: Sequence[T],
) -> tuple[tuple[T, ...], int]:
    """``(kept, mounted_total)`` for a list headed by the active notebook.

    ``kept`` is the prefix the lane searches: the head alone with the switch
    off, else the first ``CHUNK_FEDERATION_MAX_PARTICIPANTS`` entries.
    ``mounted_total`` is ``len(participants)`` when the CAP cut something and
    ``0`` otherwise -- the switch being off is not a truncation, so it never
    announces one."""
    if not chunk_lane_reads_mounts(settings):
        return tuple(participants[:1]), 0
    maximum = int(settings.chunk_federation_max_participants)
    if len(participants) <= maximum:
        return tuple(participants), 0
    return tuple(participants[:maximum]), len(participants)
