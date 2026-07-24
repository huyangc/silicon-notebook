"""PostgreSQL 的开路计数进程缓存,gated on ``kg_mutation_seq``——镜像 sqlite 的
``knowledge_counts_cache`` 的 pending-source 两个变体(checkup H6 与开路 readiness 用)。

为什么要缓存(codex 第4轮 P2):``_pending_source_count`` 是一次相关子查询全扫,前端进入
notebook 会自动拉 checkup,大 postgres 库每次都付这个扫描太贵。sqlite 侧早已 seq-gated
memo,postgres 之前直读——本模块补齐,兑现「体检廉价」契约。

seq gate 自失效即正确(seq 变→缓存 miss);``invalidate()`` 不是正确性必需,只是安全阀,
由 ``queries.invalidate_knowledge_counts`` 在「写已落、但其 seq bump 尚未提交」的边缘调用
(与 sqlite 版同款语义)。epoch 防止一个在途查询把 pre-invalidation 快照重新写回。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Optional, Tuple

_LOCK = threading.Lock()
_MAX_NOTEBOOKS = 512  # bounded LRU;每 notebook 只存一个 int,极小
_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_VISIBLE_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_INVALIDATION_EPOCH = 0


def _mutation_seq(db: Any, notebook_id: str) -> int:
    row = db.execute(
        "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=%s",
        (notebook_id,),
    ).fetchone()
    if row is None or row["kg_mutation_seq"] is None:
        return 0
    return int(row["kg_mutation_seq"])


def _pending_query(db: Any, notebook_id: str, *, visible_only: bool) -> int:
    # 判据与 postgres QueryStore 原 _pending_source_count 逐字一致(用 ordinal 排序:
    # postgres 无 rowid)。visible_only 排除 memory/knowhow 隐藏合成源。
    visible_clause = (
        "AND s.source_type NOT IN ('memory','knowhow') " if visible_only else ""
    )
    row = db.execute(
        "SELECT COUNT(*) AS c FROM sources s WHERE s.notebook_id=%s "
        + visible_clause
        + "AND EXISTS(SELECT 1 FROM source_elements e WHERE e.source_id=s.id) "
        "AND NOT EXISTS(SELECT 1 FROM knowledge_objects k "
        "WHERE k.source_id=s.id AND k.source_id!='' AND COALESCE(("
        "SELECT er.status FROM extraction_runs er "
        "WHERE er.source_id=s.id AND er.run_type='kg' "
        "ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1"
        "),'completed')='completed')",
        (notebook_id,),
    ).fetchone()
    return int(row["c"])


def _cached(memo, db: Any, notebook_id: str, *, visible_only: bool) -> int:
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = memo.get(notebook_id)
        if hit is not None and hit[0] == seq:
            memo.move_to_end(notebook_id)
            return hit[1]
        epoch = _INVALIDATION_EPOCH
    count = _pending_query(db, notebook_id, visible_only=visible_only)
    with _LOCK:
        if epoch == _INVALIDATION_EPOCH:  # 期间没被 invalidate 才写回
            memo[notebook_id] = (seq, count)
            memo.move_to_end(notebook_id)
            while len(memo) > _MAX_NOTEBOOKS:
                memo.popitem(last=False)
    return count


def pending_source_count(db: Any, notebook_id: str) -> int:
    """物理「已解析、无完整 KG」源数(全集),memoized on ``(notebook_id, kg_mutation_seq)``。"""
    return _cached(_PENDING, db, notebook_id, visible_only=False)


def visible_pending_source_count(db: Any, notebook_id: str) -> int:
    """用户可见的「已解析、无完整 KG」源数(排除 memory/knowhow 合成源),checkup H6 用。
    memoized on ``(notebook_id, kg_mutation_seq)``。"""
    return _cached(_VISIBLE_PENDING, db, notebook_id, visible_only=True)


def invalidate(notebook_id: Optional[str] = None) -> None:
    """清缓存(单 notebook 或全部)。非正确性必需(seq gate 已自失效),安全阀而已。"""
    global _INVALIDATION_EPOCH
    with _LOCK:
        _INVALIDATION_EPOCH += 1
        if notebook_id is None:
            _PENDING.clear()
            _VISIBLE_PENDING.clear()
        else:
            _PENDING.pop(notebook_id, None)
            _VISIBLE_PENDING.pop(notebook_id, None)


__all__ = [
    "pending_source_count",
    "visible_pending_source_count",
    "invalidate",
]
