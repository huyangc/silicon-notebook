"""PostgreSQL 的开路计数进程缓存,gated on ``kg_mutation_seq``——镜像 sqlite 的
``knowledge_counts_cache`` 的 pending-source 两个变体(checkup H6 与开路 readiness 用)。

为什么要缓存(codex 第4轮 P2):``_pending_source_count`` 冷查询即使按 source 做
有界索引探测,仍然与来源数成正比;前端进入 notebook 会自动拉 checkup,大库
不应每次都付冷查询成本。sqlite 侧早已 seq-gated memo,postgres 之前直读——
本模块补齐,兑现「体检廉价」契约。

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
    #
    # 不要把 latest KG run 的标量子查询放进 knowledge_objects 的 EXISTS 里。
    # PostgreSQL 可能把它下沉到 KO 侧，使每条 KO 都重复读 status/error：
    # 9.1M KO 的实库因此执行了约 27M 次 extraction_runs 索引探测。
    # 三个 LATERAL ... LIMIT 1 刻意以当前 notebook 的 sources 为驱动：
    # 每个 source 最多探测一个 element、一个最新 run 和一个 KO。
    visible_clause = (
        "AND s.source_type NOT IN ('memory','knowhow') " if visible_only else ""
    )
    row = db.execute(
        "SELECT COUNT(*) AS c FROM sources s "
        "JOIN LATERAL (SELECT 1 AS found FROM source_elements e "
        "WHERE e.source_id=s.id LIMIT 1) parsed ON TRUE "
        "LEFT JOIN LATERAL (SELECT er.status,er.error_message "
        "FROM extraction_runs er WHERE er.source_id=s.id AND er.run_type='kg' "
        "ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1) latest_kg ON TRUE "
        "LEFT JOIN LATERAL (SELECT 1 AS found FROM knowledge_objects k "
        "WHERE k.source_id=s.id AND k.source_id!='' LIMIT 1) source_kg ON TRUE "
        "WHERE s.notebook_id=%s "
        + visible_clause
        + "AND (source_kg.found IS NULL "
        "OR COALESCE(latest_kg.status,'completed')!='completed' "
        "OR COALESCE(latest_kg.error_message,'') "
        "~ 'windows_failed=[1-9][0-9]*/[0-9]+' "
        "OR strpos(COALESCE(latest_kg.error_message,''),'retry_incomplete=1')>0)",
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
