"""Backend-neutral result shape of the guarded Knowhow cell write
(``update_knowhow_cells_guarded_atomic``, share-edit P1 decision D7, plan PD-1).

Both stores build their answer through these two functions so the shape cannot
drift between SQLite and PostgreSQL:

* a refusal says WHY (``reason``): ``content_changed`` -- a target cell's
  stored content no longer equals the caller's ``expected_before``; ``current``
  then carries every target's stored content read in the same write
  transaction, so the client can show "their version" and overwrite with it as
  the new baseline without a second read (which could already be stale);
  ``group_changed`` -- the target set itself moved (a row or the table is gone,
  the anchor designation or a group's membership changed, an anchor value
  moved): overwriting cannot be offered, the client reloads.
* a success carries ``stored``: each target's content and projection status as
  this transaction left them. The write stores ``content_md`` verbatim and
  flags the row ``pending``, so the values are known without a re-read -- and a
  re-read after commit could return a LATER writer's value, which the client
  would then take as its baseline and never conflict against.

Each value also carries the owning table's persistent ``mutation_seq`` from
this same transaction. It orders delayed responses and browser broadcasts;
timestamps and prunable history sequence numbers cannot provide that order.

The legacy keys (``written``, ``conflict``) are unchanged.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

CONTENT_CHANGED = "content_changed"
GROUP_CHANGED = "group_changed"
GUARDED_CONFLICT_REASONS = (CONTENT_CHANGED, GROUP_CHANGED)


def guarded_conflict(reason: str, current: Iterable[dict] = ()) -> dict[str, object]:
    """A refusal: nothing written. ``current`` only with ``content_changed``."""
    if reason not in GUARDED_CONFLICT_REASONS:
        raise ValueError(f"unknown guarded conflict reason: {reason!r}")
    return {"written": [], "conflict": True, "reason": reason, "current": list(current)}


def current_cell(row_id: str, column_id: str, stored: object, mutation_seq: int) -> dict[str, object]:
    """One target's stored content; a never-written cell reads ``""``, exactly
    as ``get_knowhow_table`` presents it (and as the compare accepts it)."""
    return {"row_id": row_id, "column_id": column_id, "content_md": str(stored or ""),
            "mutation_seq": mutation_seq}


def guarded_success(
    written: list[tuple[str, str]],
    updates: Sequence[tuple[str, str, str, object, str]],
    mutation_seqs: Mapping[str, int],
) -> dict[str, object]:
    """A committed write: ``stored`` parallels ``updates``."""
    return {
        "written": written,
        "conflict": False,
        "stored": [
            {
                "row_id": row_id,
                "column_id": column_id,
                "content_md": content_md,
                "projection_status": "pending",
                "mutation_seq": mutation_seqs[table_id],
            }
            for table_id, row_id, column_id, _expected, content_md in updates
        ],
    }
