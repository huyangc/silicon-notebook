"""Per-notebook knowledge-object count cache, gated on ``kg_mutation_seq``.

The per-type / active COUNT(*) GROUP BY over ``knowledge_objects`` is reached on
every notebook open, board (analytics), KG-overview render and status poll
(``notebook_catalog.from_row``, ``notebook_analytics``, ``type_counts``,
``count_active_objects``, ``effective_object_count``). At scale (a base library
reaches ~2M objects) that is a per-notebook covering-index scan of every leaf
entry — recomputed every request even though the counts only change on a KG
write.

Counts are a pure function of the object rows, and EVERY write that can change
them bumps the monotonic ``unified_kg_state.kg_mutation_seq`` (the single choke
point — ``_mark_unified_kg_dirty`` on store_kg / update_knowledge (status/type
flip) / merge / promotion / conflict-apply / relink / chunk rebuild; verified).
So we memoize the raw ``{(object_type, status): count}`` breakdown keyed on
``(notebook_id, kg_mutation_seq)`` and let each caller derive its own
status-filtered view. One GROUP BY per (notebook, seq) instead of per request;
O(1) single-row seq read on the hot path. Same seq-gate pattern as the trusted
``scale_artifact_runtime.version_memo`` (index staleness) and the rebuild skip
gate.

Correctness note: a monotonic counter — never a timestamp — because a same-second
in-place status/type edit must invalidate (1s-resolution ``updated_at`` would
miss it). Rebuild deliberately keeps ``kg_mutation_seq`` stable, which is correct
here: a rebuild re-clusters but does not add/remove/re-status objects, so counts
are unchanged across it.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from typing import Dict, Optional, Tuple

# non-deprecated is the "active" set most call sites want; USABLE_STATUSES is the
# narrower reviewed set from knowledge_contracts (imported lazily to avoid a
# repository->service import edge).
_DEPRECATED = "deprecated"

_MEMO: "OrderedDict[str, Tuple[int, Dict[Tuple[str, str], int]]]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_NOTEBOOKS = 512  # bounded LRU; counts dict per notebook is tiny (types×statuses)


def _mutation_seq(db: sqlite3.Connection, notebook_id: str) -> int:
    row = db.execute(
        "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
        (notebook_id,),
    ).fetchone()
    if row is None or row["kg_mutation_seq"] is None:
        return 0
    return int(row["kg_mutation_seq"])


def type_status_counts(
    db: sqlite3.Connection, notebook_id: str
) -> Dict[Tuple[str, str], int]:
    """``{(object_type, status): count}`` for the notebook, memoized on
    ``kg_mutation_seq``. Returns a shared read-only dict — callers must not
    mutate it."""
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = _MEMO.get(notebook_id)
        if hit is not None and hit[0] == seq:
            _MEMO.move_to_end(notebook_id)
            return hit[1]

    rows = db.execute(
        "SELECT object_type, status, COUNT(*) AS c FROM knowledge_objects "
        "WHERE notebook_id=? GROUP BY object_type, status",
        (notebook_id,),
    ).fetchall()
    counts: Dict[Tuple[str, str], int] = {
        (r["object_type"], r["status"]): int(r["c"]) for r in rows
    }

    with _LOCK:
        _MEMO[notebook_id] = (seq, counts)
        _MEMO.move_to_end(notebook_id)
        while len(_MEMO) > _MAX_NOTEBOOKS:
            _MEMO.popitem(last=False)
    return counts


def type_counts(
    db: sqlite3.Connection,
    notebook_id: str,
    statuses: "Optional[Tuple[str, ...]]" = None,
) -> Dict[str, int]:
    """``{object_type: count}`` filtered to ``statuses`` (a whitelist), or to
    all-but-``deprecated`` when ``statuses is None``."""
    raw = type_status_counts(db, notebook_id)
    allow = set(statuses) if statuses is not None else None
    out: Dict[str, int] = {}
    for (object_type, status), c in raw.items():
        if allow is None:
            if status == _DEPRECATED:
                continue
        elif status not in allow:
            continue
        out[object_type] = out.get(object_type, 0) + c
    return out


def active_object_count(db: sqlite3.Connection, notebook_id: str) -> int:
    """Total non-deprecated object count."""
    raw = type_status_counts(db, notebook_id)
    return sum(c for (_ot, status), c in raw.items() if status != _DEPRECATED)


def invalidate(notebook_id: "Optional[str]" = None) -> None:
    """Drop cached counts (a whole notebook, or everything). Not required for
    correctness — the seq gate self-invalidates — but a cheap safety valve for
    tests and for any future write path that lands before its seq bump commits."""
    with _LOCK:
        if notebook_id is None:
            _MEMO.clear()
        else:
            _MEMO.pop(notebook_id, None)
