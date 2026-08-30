"""Per-notebook open-path count caches, gated on ``kg_mutation_seq``.

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

Three sibling open-path scans share the SAME seq gate and ``invalidate`` (so the
existing invalidate hooks cover them for free):
  * ``chunk_count`` — ``COUNT(*) FROM chunks`` (``/scale-index/status`` open path).
    Sound on ``kg_mutation_seq`` because the sole chunk writer
    (``build_chunks_for_source``) and ``delete_source`` (FK cascade) both bump it.
  * ``pending_source_count`` / ``visible_pending_source_count`` — sibling
    physical/user-visible "N sources pending KG" correlated counts
    (``from_row`` on every open, ~2s cold at 48k sources). Sound because a bare
    source add is element-less (never pending) and every real transition (parse,
    extract, source/KG delete) bumps the seq or hits ``invalidate``.
R3 T-A3 v4 note: the edge-review-queue's ``total`` field used to be a 5th
memo here (``review_queue_total`` + a ``carry_review_queue_total`` retag,
epoch-guarded the same way the two pending-source views are). codex #638 R1
found that shape produced items/total from two independent seq-gated reads
that could straddle a KG mutation and disagree about which version they
described. v4 moved ``total`` into
``app.services.review_queue_memo.ReviewQueueMemo`` instead, alongside the
ranking items it is counted with (same lock, same seq, same cold scan) — this
module no longer carries anything review-queue-shaped.

Sources COUNT / ``source_ids`` are deliberately NOT cached here: ``create_source``
inserts a row without bumping the seq, so a seq-keyed memo would drift — and they
stay cheap without a memo. The user-facing source count
(``QueryStore.visible_source_count``,
``AND source_type NOT IN ('memory', 'knowhow')``) and the /analytics
parse_status GROUP BY are both covering-only scans via
``idx_sources_nb_parse_status_type`` (notebook_id, parse_status, source_type;
migration 15), so neither回表s to the sources base table nor tops the cold-open
budget.
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
# Sibling int memos sharing _LOCK / _MAX_NOTEBOOKS / the kg_mutation_seq gate.
_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_VISIBLE_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_CHUNKS: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_LOCK = threading.Lock()
_INVALIDATION_EPOCH = 0
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


def object_type_total(
    db: sqlite3.Connection,
    notebook_id: str,
    object_type: str,
    status: "Optional[str]" = None,
) -> int:
    """The ``/knowledge`` list-pagination total for one ``object_type`` — served
    as a slice of the seq-gated ``type_status_counts`` memo instead of a fresh
    per-request ``COUNT(*)``. A falsy ``status`` counts ALL statuses (including
    deprecated), identical to the bare ``WHERE notebook_id=? AND object_type=?``
    count it replaces; a truthy status is one dict lookup."""
    raw = type_status_counts(db, notebook_id)
    if status:
        return raw.get((object_type, status), 0)
    return sum(c for (ot, _st), c in raw.items() if ot == object_type)


# 「已分析、但这篇里没有可整理的知识」不是待分析——把它排除在 pending 之外。
#
# 判据的**权威表述**是 ``app.models.sources.kg_analyzed_without_objects``;这里是它的
# SQLite 方言镜像,因为这个位置必须是一条 COUNT(逐行拉回 Python 判就是 N+1)。两者
# 由 ``backend/tests/test_kg_empty_extraction_marker.py`` 逐用例对账,PG 侧同款镜像在
# ``postgres/knowledge_counts_cache.py``。
#
# 只有两条 GLOB,没有第三条 ``retry_incomplete`` 排除:partial 重试写的消息一律以
# "partial KG retry incomplete;" 起头,与这里要求的 ``kg objects=0 `` 前缀互斥,再查
# 一次是白付一次索引探测。Python 侧那份保留了显式的 retry_incomplete 判断——它是
# 判据的表述,不受这条只对本 SQL 成立的短路影响。
#
# 代价:每个来源多两次 extraction_runs 的有界探测(各一次 LIMIT 1 索引 seek)。这条
# 查询本来就与来源数成正比且 memoized 在 kg_mutation_seq 上,常数因子可接受;换来的
# 是零对象来源不再永远占着「待分析」并在每次「分析新增」里重付一遍模型钱。
_ANALYZED_WITHOUT_OBJECTS_EXCLUSION = (
    "AND NOT (COALESCE((SELECT er.status FROM extraction_runs er "
    " WHERE er.source_id=s.id AND er.run_type='kg' "
    " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'')='completed' "
    "AND COALESCE((SELECT er.error_message FROM extraction_runs er "
    " WHERE er.source_id=s.id AND er.run_type='kg' "
    " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
    " GLOB 'kg objects=0 *' "
    "AND COALESCE((SELECT er.error_message FROM extraction_runs er "
    " WHERE er.source_id=s.id AND er.run_type='kg' "
    " ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
    " NOT GLOB '*windows_failed=[1-9]*/*')"
)


def _pending_source_count_query(
    db: sqlite3.Connection, notebook_id: str, *, visible_only: bool
) -> int:
    visible_clause = (
        "AND s.source_type NOT IN ('memory','knowhow') " if visible_only else ""
    )
    row = db.execute(
        "SELECT COUNT(*) FROM sources s WHERE s.notebook_id = ? "
        + visible_clause
        + "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id) "
        "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
        "WHERE k.source_id = s.id AND k.source_id != '' "
        "AND COALESCE(("
        "  SELECT er.status FROM extraction_runs er "
        "  WHERE er.source_id=s.id AND er.run_type='kg' "
        "  ORDER BY er.created_at DESC, er.rowid DESC LIMIT 1"
        "), 'completed')='completed' AND NOT ("
        "COALESCE((SELECT er.error_message FROM extraction_runs er "
        "WHERE er.source_id=s.id AND er.run_type='kg' "
        "ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),'') "
        "GLOB '*windows_failed=[1-9]*/*' OR instr(COALESCE(("
        "SELECT er.error_message FROM extraction_runs er "
        "WHERE er.source_id=s.id AND er.run_type='kg' "
        "ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1),''),"
        "'retry_incomplete=1')>0)) "
        + _ANALYZED_WITHOUT_OBJECTS_EXCLUSION,
        (notebook_id,),
    ).fetchone()
    return int(row[0])


def pending_source_count(db: sqlite3.Connection, notebook_id: str) -> int:
    """Physical parsed-content count without a complete KG graph.

    A direct/governance graph without extraction history remains complete, but
    rows left by a failed latest KG run stay pending. Internal scheduling and
    readiness accounting use this physical count. Memoized on
    ``(notebook_id, kg_mutation_seq)``."""
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = _PENDING.get(notebook_id)
        if hit is not None and hit[0] == seq:
            _PENDING.move_to_end(notebook_id)
            return hit[1]
        invalidation_epoch = _INVALIDATION_EPOCH

    count = _pending_source_count_query(db, notebook_id, visible_only=False)

    with _LOCK:
        if invalidation_epoch == _INVALIDATION_EPOCH:
            _PENDING[notebook_id] = (seq, count)
            _PENDING.move_to_end(notebook_id)
            while len(_PENDING) > _MAX_NOTEBOOKS:
                _PENDING.popitem(last=False)
    return count


def visible_pending_source_count(
    db: sqlite3.Connection, notebook_id: str
) -> int:
    """User-visible parsed source count without a complete KG graph.

    Excludes Memory-derived and Knowhow-table synthetic sources.
    """
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = _VISIBLE_PENDING.get(notebook_id)
        if hit is not None and hit[0] == seq:
            _VISIBLE_PENDING.move_to_end(notebook_id)
            return hit[1]
        invalidation_epoch = _INVALIDATION_EPOCH
    count = _pending_source_count_query(db, notebook_id, visible_only=True)
    with _LOCK:
        if invalidation_epoch == _INVALIDATION_EPOCH:
            _VISIBLE_PENDING[notebook_id] = (seq, count)
            _VISIBLE_PENDING.move_to_end(notebook_id)
            while len(_VISIBLE_PENDING) > _MAX_NOTEBOOKS:
                _VISIBLE_PENDING.popitem(last=False)
    return count


def chunk_count(db: sqlite3.Connection, notebook_id: str) -> int:
    """``COUNT(*)`` of the notebook's chunks (``/scale-index/status`` open path),
    memoized on ``(notebook_id, kg_mutation_seq)``. Cold it is a full covering
    scan over millions of chunk leaf entries; warm it is one PK seq read."""
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = _CHUNKS.get(notebook_id)
        if hit is not None and hit[0] == seq:
            _CHUNKS.move_to_end(notebook_id)
            return hit[1]

    row = db.execute(
        "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?",
        (notebook_id,),
    ).fetchone()
    count = int(row["c"])

    with _LOCK:
        _CHUNKS[notebook_id] = (seq, count)
        _CHUNKS.move_to_end(notebook_id)
        while len(_CHUNKS) > _MAX_NOTEBOOKS:
            _CHUNKS.popitem(last=False)
    return count


def warm_all(db: sqlite3.Connection, progress=None) -> int:
    """Prime the FOUR open-path memos every notebook open/board/status-poll
    reaches (``type_status_counts`` / both pending-source views /
    ``chunk_count``) for every live notebook, so the first request after a
    fresh process start is served warm instead of paying the cold GROUP BY +
    correlated scans (see module docstring).

    The edge-review-queue's true total is NOT a memo in this module — R3 T-A3
    v4 moved it into ``app.services.review_queue_memo.ReviewQueueMemo``,
    alongside the ranking items it is counted with in the same cold scan (see
    that module's docstring; this file's module docstring records why the
    module-global 5th-memo shape it used to have here was removed).

    Called once at startup behind the readiness gate by the facade
    ``warm_open_path_caches``. The notebook-id query lives HERE (store-owned SQL)
    so the application-service facade stays SQL-free. Best-effort: a per-notebook
    ``sqlite3.Error`` is swallowed (a warm miss only costs a later lazy recompute,
    never correctness). ``progress`` — when given — is invoked as
    ``progress(done, total)`` after EACH notebook (1-based ``done``) so the
    readiness screen can render "N/M 笔记本". Returns the notebook total.

    'copying' notebooks are skipped: a half-materialized deep-copy has no stable
    counts to warm and is not yet openable.
    """
    ids = [
        r["id"]
        for r in db.execute(
            "SELECT id FROM notebooks WHERE status != 'copying'"
        ).fetchall()
    ]
    total = len(ids)
    for i, notebook_id in enumerate(ids, start=1):
        try:
            type_status_counts(db, notebook_id)
            pending_source_count(db, notebook_id)
            visible_pending_source_count(db, notebook_id)
            chunk_count(db, notebook_id)
        except sqlite3.Error:
            continue
        finally:
            if progress is not None:
                progress(i, total)
    return total


def invalidate(notebook_id: "Optional[str]" = None) -> None:
    """Drop cached counts (a whole notebook, or everything) across ALL
    memos. Not required for correctness — the seq gate self-invalidates — but a
    cheap safety valve for tests and for any write path that lands before its seq
    bump commits (extraction begin, notebook-KG delete, test inserts). The epoch
    prevents an in-flight pending-source query from reinserting its
    pre-invalidation snapshot."""
    global _INVALIDATION_EPOCH
    with _LOCK:
        _INVALIDATION_EPOCH += 1
        if notebook_id is None:
            _MEMO.clear()
            _PENDING.clear()
            _VISIBLE_PENDING.clear()
            _CHUNKS.clear()
        else:
            _MEMO.pop(notebook_id, None)
            _PENDING.pop(notebook_id, None)
            _VISIBLE_PENDING.pop(notebook_id, None)
            _CHUNKS.pop(notebook_id, None)
