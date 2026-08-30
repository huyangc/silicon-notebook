"""PostgreSQL 的开路计数进程缓存,gated on ``kg_mutation_seq``——镜像 sqlite 的
``knowledge_counts_cache``(见该模块头注释的完整背景与代价分析)。

移植范围(大库打开卡死修复,见 docs/large-notebook-latency-analysis.md):这里原来
只有 pending-source 两个变体(checkup H6 与开路 readiness 用,codex 第4轮 P2)。
现在补齐 sqlite 侧的 ``type_status_counts`` / ``type_counts`` / ``active_object_count`` /
``object_type_total`` / ``chunk_count`` / ``warm_all``——笔记本打开、列表分页、看板、
scale-index 状态在 9M 对象的生产库上都是这几条裸 GROUP BY/COUNT 的受害者。

seq gate 自失效即正确(seq 变→缓存 miss);``invalidate()`` 不是正确性必需,只是安全阀,
由 ``queries.invalidate_knowledge_counts`` 在「写已落、但其 seq bump 尚未提交」的边缘调用
(与 sqlite 版同款语义)。epoch 防止一个在途查询把 pre-invalidation 快照重新写回——
**按 notebook 分开**:每个 notebook 有自己的失效代次,跨库互不误伤。这一点是本次
移植踩过的真实缺陷修复:早期版本用单个全局 epoch,大库(9M 对象)的冷 GROUP BY 要跑
几秒,期间任何**别的** notebook 走一次 ingestion 都会让全局 epoch +1、连带拒绝本库
本该成功的写回——在持续上传的生产环境里,大库因此可能永远暖不起来,恰好废掉这次
移植要解决的场景。现在 per-notebook epoch(``_EPOCHS``)只在该 notebook 自己被
invalidate 时推进,外部无关 notebook 的 ingestion 不再牵连它;仍保留一个全局 epoch
(``_GLOBAL_EPOCH``,只被 ``invalidate(None)`` 推进)用于「清空全部缓存」这一路径。

刻意差异(比 sqlite 版严格一点):sqlite 的 ``type_status_counts`` 写回不做 epoch 检查
(它只有一个 ``_MEMO`` 消费者形态历史更早,当时判断该窗口足够窄可以不设防;且 sqlite
侧 pending memo 用的是全局单值 epoch,没有 per-notebook 隔离)。PG 这里从
``type_status_counts`` 到 ``chunk_count`` 到两个 pending 视图,四个 memo 统一套上
per-notebook epoch 守卫——原因是 PG 面对的大库冷查询窗口(几秒级)比 sqlite 场景长得多,
全局 epoch 在这个窗口内被无关库的高频 ingestion 持续作废的概率显著更高,窄窗口下可以
不设防的假设在这里不成立,所以选择更精细的隔离而不是复用 sqlite 的从简版本。

(R3 T-A3 曾在这里加过第 5 个 memo ``review_queue_total`` 及其 ``carry_review_
queue_total`` retag——codex #638 R1 指出 module-global 键跨 runtime 混串、且
端点两次独立读会产生 items/total 跨版本不一致的响应;v4 把队列总量挪进
``app.services.review_queue_memo.ReviewQueueMemo``,与排名 items 同一个条目、
同一把锁、同一次冷算,runtime-owned、无跨进程/跨 runtime 混串面。这里的第 5
memo 已随之整体撤除,包括这个 docstring 曾经的措辞——见该模块的 module docstring
获取现在的权威口径。)
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Tuple

from psycopg import Error

# non-deprecated 是大多数调用点想要的「活跃」集合;更窄的 USABLE_STATUSES 由
# knowledge_contracts 定义,调用方按需自己过滤——与 sqlite 版同一分工。
_DEPRECATED = "deprecated"

_LOCK = threading.Lock()
_MAX_NOTEBOOKS = 512  # bounded LRU;每 notebook 的值都很小(int,或 types×statuses 的小字典)
_MEMO: "OrderedDict[str, Tuple[int, Dict[Tuple[str, str], int]]]" = OrderedDict()
_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_VISIBLE_PENDING: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()
_CHUNKS: "OrderedDict[str, Tuple[int, int]]" = OrderedDict()

# invalidation epoch:写回前重新核对「读之后到写之前,这个 notebook(或全局)没被
# invalidate 过」。_GLOBAL_EPOCH 只被 invalidate(None) 推进(清空全部缓存那条路径);
# _EPOCHS 是 per-notebook 代次,只被该 notebook 自己的 invalidate(nb) 推进——这样
# nb-A 的一次几秒级冷查询不会被 nb-B 的 ingestion 误伤。_EPOCHS 本身是有界 LRU
# (上限沿用 _MAX_NOTEBOOKS):它是 best-effort 安全阀,不是正确性权威(权威始终是
# seq gate)——但淘汰必须 fail closed,不能让被淘汰的 notebook 代次静默退回默认值 0。
# 反例(codex #621 R1 P2):冷查询采样到默认 epoch 0 → 该 notebook 被 invalidate
# (epoch→1)→ 期间 ≥_MAX_NOTEBOOKS 个别的 notebook 也被 invalidate,把这个 notebook
# 的 epoch 条目从 _EPOCHS 挤出去 → `_epoch_of` 又退回默认值 0,与采样时相同 → 写回
# 守卫误判「没被 invalidate」,把 invalidate 之前的陈旧快照写进 memo,且在没有后续
# seq bump 兜底的边缘上可能无限期陈旧。所以 `invalidate()` 每淘汰一个 _EPOCHS 条目
# 就推进一次 _GLOBAL_EPOCH:被淘汰的 notebook 在途写回从「误放行」翻成「被拒绝」,
# 方向保守——代价是多付一次冷查,绝不钉陈旧值。
_GLOBAL_EPOCH = 0
_EPOCHS: "OrderedDict[str, int]" = OrderedDict()


def _mutation_seq(db: Any, notebook_id: str) -> int:
    row = db.execute(
        "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=%s",
        (notebook_id,),
    ).fetchone()
    if row is None or row["kg_mutation_seq"] is None:
        return 0
    return int(row["kg_mutation_seq"])


def _epoch_of(notebook_id: str) -> Tuple[int, int]:
    """采样 ``(全局 epoch, 该 notebook 的 epoch)`` 二元组。调用方必须已持有 ``_LOCK``。"""
    return (_GLOBAL_EPOCH, _EPOCHS.get(notebook_id, 0))


def _seq_gated(
    memo: "OrderedDict[str, Tuple[int, Any]]",
    db: Any,
    notebook_id: str,
    compute: "Callable[[], Any]",
) -> Any:
    """四个 memo(``_MEMO`` / ``_CHUNKS`` / ``_PENDING`` / ``_VISIBLE_PENDING``)
    共享的 seq-gated 读写骨架:命中同 ``kg_mutation_seq`` 直接返回;miss 时锁外跑
    ``compute()``,写回前重新核对 ``(全局 epoch, 该 notebook 的 epoch)`` 二元组没有
    在计算期间变化——只有这个 notebook 自己被 invalidate,或全局被清空,才会拒绝写回;
    别的 notebook 的 invalidate 不影响。"""
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = memo.get(notebook_id)
        if hit is not None and hit[0] == seq:
            memo.move_to_end(notebook_id)
            return hit[1]
        epoch = _epoch_of(notebook_id)

    value = compute()

    with _LOCK:
        if epoch == _epoch_of(notebook_id):  # 期间没被 invalidate 才写回
            memo[notebook_id] = (seq, value)
            memo.move_to_end(notebook_id)
            while len(memo) > _MAX_NOTEBOOKS:
                memo.popitem(last=False)
    return value


def type_status_counts(db: Any, notebook_id: str) -> "Dict[Tuple[str, str], int]":
    """``{(object_type, status): count}`` for the notebook, memoized on
    ``kg_mutation_seq``. Returns a shared read-only dict — callers must not
    mutate it。"""

    def compute() -> Dict[Tuple[str, str], int]:
        rows = db.execute(
            "SELECT object_type, status, COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=%s GROUP BY object_type, status",
            (notebook_id,),
        ).fetchall()
        return {(r["object_type"], r["status"]): int(r["c"]) for r in rows}

    return _seq_gated(_MEMO, db, notebook_id, compute)


def type_counts(
    db: Any,
    notebook_id: str,
    statuses: "Optional[Tuple[str, ...]]" = None,
) -> "Dict[str, int]":
    """``{object_type: count}`` filtered to ``statuses`` (a whitelist), or to
    all-but-``deprecated`` when ``statuses is None``。"""
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


def active_object_count(db: Any, notebook_id: str) -> int:
    """Total non-deprecated object count。"""
    raw = type_status_counts(db, notebook_id)
    return sum(c for (_ot, status), c in raw.items() if status != _DEPRECATED)


def object_type_total(
    db: Any,
    notebook_id: str,
    object_type: str,
    status: "Optional[str]" = None,
) -> int:
    """The ``/knowledge`` list-pagination total for one ``object_type`` — served
    as a slice of the seq-gated ``type_status_counts`` memo instead of a fresh
    per-request ``COUNT(*)``. A falsy ``status`` counts ALL statuses (including
    deprecated), identical to the bare ``WHERE notebook_id=%s AND object_type=%s``
    count it replaces; a truthy status is one dict lookup。"""
    raw = type_status_counts(db, notebook_id)
    if status:
        return raw.get((object_type, status), 0)
    return sum(c for (ot, _st), c in raw.items() if ot == object_type)


def chunk_count(db: Any, notebook_id: str) -> int:
    """``COUNT(*)`` of the notebook's chunks (``/scale-index/status`` open path),
    memoized on ``(notebook_id, kg_mutation_seq)``. Cold it is a full covering
    scan over millions of chunk leaf entries; warm it is one PK seq read。"""

    def compute() -> int:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id=%s",
            (notebook_id,),
        ).fetchone()
        return int(row["c"])

    return _seq_gated(_CHUNKS, db, notebook_id, compute)


def warm_all(db: Any, progress=None) -> int:
    """Prime the FOUR open-path memos every notebook open/board/status-poll
    reaches (``type_status_counts`` / both pending-source views /
    ``chunk_count``) for every live notebook, so the first request after a
    fresh process start is served warm instead of paying the cold GROUP BY +
    correlated scans (see module docstring; sqlite 镜像见
    ``sqlite/knowledge_counts_cache.warm_all``)。

    The edge-review-queue's true total is NOT one of these four — it lives in
    ``app.services.review_queue_memo.ReviewQueueMemo`` (runtime-owned, next to
    the ranking it is counted alongside; see that module's docstring), not in
    this module at all (R3 T-A3 v4 removed the module-global ``review_queue_
    total`` memo that used to live here).

    Best-effort: 每个 notebook 一个独立事务尝试,``psycopg.Error`` 被吞掉——但 PG
    的事务语义要求出错后必须 ``rollback()`` 才能让连接在下一次 ``execute`` 继续可用
    (不像 sqlite,一次失败不会污染后续查询)。``progress`` — when given — is invoked
    as ``progress(done, total)`` after EACH notebook (1-based ``done``)。

    'copying' notebooks are skipped: a half-materialized deep-copy has no
    stable counts to warm and is not yet openable。
    """
    ids = [
        row["id"]
        for row in db.execute(
            "SELECT id FROM notebooks WHERE status != 'copying' ORDER BY id"
        ).fetchall()
    ]
    total = len(ids)
    for i, notebook_id in enumerate(ids, start=1):
        try:
            type_status_counts(db, notebook_id)
            pending_source_count(db, notebook_id)
            visible_pending_source_count(db, notebook_id)
            chunk_count(db, notebook_id)
        except Error:
            db.rollback()
            continue
        finally:
            if progress is not None:
                progress(i, total)
    return total


def _pending_sql(*, visible_only: bool) -> str:
    """构造 pending 计数的整条 SQL。

    抽成函数是为了让它能被**不需要活 PostgreSQL** 的守卫扫一遍:psycopg 把查询里的
    ``%`` 当占位符起头,SQL 里任何一个字面 ``%`` 都会在**运行时**炸成
    ``ProgrammingError``,而本仓库的 PG 集成门是独立的一条 lane——本地标准门跑不到它。
    真发生过一次(``LIKE 'kg objects=0 %'``),守卫见
    ``backend/tests/test_kg_empty_extraction_marker.py``。
    """
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
    return (
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
        "OR strpos(COALESCE(latest_kg.error_message,''),'retry_incomplete=1')>0) "
        # 「已分析、但这篇里没有可整理的知识」不是待分析。判据的权威表述是
        # ``app.models.sources.kg_analyzed_without_objects``,这里是它的 PG 方言镜像
        # (本位置必须是一条 COUNT);两者由
        # ``backend/tests/test_kg_empty_extraction_marker.py`` 逐用例对账,SQLite 侧
        # 同款镜像见 ``sqlite/knowledge_counts_cache.py``。
        #
        # 这里**零额外探测**:latest_kg 已是上面那个 LATERAL ... LIMIT 1 的结果,状态与
        # 消息都在手,新条件只是对同一行再加两个判断。没有第三条 retry_incomplete 排除,
        # 理由同 SQLite 侧:partial 重试的消息以 "partial KG retry incomplete;" 起头,与
        # ``kg objects=0 `` 前缀互斥。
        "AND NOT (COALESCE(latest_kg.status,'')='completed' "
        # 用 starts_with() 而不是 LIKE 'kg objects=0 %':psycopg 把 SQL 里的 `%`
        # 当占位符起头,字面 `%` 必须写成 `%%`,而那是个一挪就再犯的坑(已经犯过一次,
        # 见 _pending_sql 的 docstring)。starts_with 无通配符、无需转义,而且与
        # Python 判据里的 str.startswith 是逐字同一个操作。
        "AND starts_with(COALESCE(latest_kg.error_message,''),'kg objects=0 ') "
        "AND COALESCE(latest_kg.error_message,'') "
        "!~ 'windows_failed=[1-9][0-9]*/[0-9]+')"
    )


def _pending_query(db: Any, notebook_id: str, *, visible_only: bool) -> int:
    row = db.execute(
        _pending_sql(visible_only=visible_only), (notebook_id,)
    ).fetchone()
    return int(row["c"])


def _cached(memo, db: Any, notebook_id: str, *, visible_only: bool) -> int:
    return _seq_gated(
        memo, db, notebook_id,
        lambda: _pending_query(db, notebook_id, visible_only=visible_only),
    )


def pending_source_count(db: Any, notebook_id: str) -> int:
    """物理「已解析、无完整 KG」源数(全集),memoized on ``(notebook_id, kg_mutation_seq)``。"""
    return _cached(_PENDING, db, notebook_id, visible_only=False)


def visible_pending_source_count(db: Any, notebook_id: str) -> int:
    """用户可见的「已解析、无完整 KG」源数(排除 memory/knowhow 合成源),checkup H6 用。
    memoized on ``(notebook_id, kg_mutation_seq)``。"""
    return _cached(_VISIBLE_PENDING, db, notebook_id, visible_only=True)


def invalidate(notebook_id: Optional[str] = None) -> None:
    """清缓存(单 notebook 或全部)。非正确性必需(seq gate 已自失效),安全阀而已。

    ``notebook_id`` 给定时只推进该 notebook 自己的 epoch(``_EPOCHS[notebook_id]``)——
    不影响任何别的 notebook 在途的冷查询写回,**除非**这次 invalidate 恰好把 ``_EPOCHS``
    的有界 LRU 挤过上限:被淘汰出去的那个 notebook 的代次不能静默退回默认值 0(那会让
    它自己在途的写回被误判成「没被 invalidate」),所以每淘汰一条就推进一次
    ``_GLOBAL_EPOCH``,fail closed——代价是被牵连的 notebook 多付一次冷查,方向保守。
    ``notebook_id is None`` 时推进 ``_GLOBAL_EPOCH`` 并清空全部 memo;``_EPOCHS`` 也
    一并清空——全局代次已经变了,残留的 per-notebook 代次不再有意义(下次读取时
    ``_epoch_of`` 会用默认值 0 重新起算,不影响正确性)。

    本函数按名字枚举每个 memo 字典,新增一个 memo 字典必须在这里显式登记才会被安全阀
    覆盖,否则「写已落但 seq bump 未提交」那个边缘窗口对新 memo 依旧成立(seq gate
    本身仍是正确性权威,只是这一处 best-effort 安全阀会漏了它)。

    (``_REVIEW_QUEUE_TOTAL`` 曾经是这四个之外的第 5 个 memo,R3 T-A3 v4 整体撤除——
    队列总量现在随排名一起存在 ``ReviewQueueMemo`` 里,那个 memo 有自己独立的
    ``invalidate()``,不受这里管辖。)"""
    global _GLOBAL_EPOCH
    with _LOCK:
        if notebook_id is None:
            _GLOBAL_EPOCH += 1
            _MEMO.clear()
            _PENDING.clear()
            _VISIBLE_PENDING.clear()
            _CHUNKS.clear()
            _EPOCHS.clear()
        else:
            _EPOCHS[notebook_id] = _EPOCHS.get(notebook_id, 0) + 1
            _EPOCHS.move_to_end(notebook_id)
            while len(_EPOCHS) > _MAX_NOTEBOOKS:
                _EPOCHS.popitem(last=False)
                # fail closed:被淘汰的 notebook 的下一次 `_epoch_of` 采样必须与它
                # 淘汰前的任何采样都不同,否则在途写回会把它误判成「没被 invalidate」
                # (见模块 docstring 与 `_EPOCHS` 声明处的完整场景)。
                _GLOBAL_EPOCH += 1
            _MEMO.pop(notebook_id, None)
            _PENDING.pop(notebook_id, None)
            _VISIBLE_PENDING.pop(notebook_id, None)
            _CHUNKS.pop(notebook_id, None)


__all__ = [
    "type_status_counts",
    "type_counts",
    "active_object_count",
    "object_type_total",
    "chunk_count",
    "warm_all",
    "pending_source_count",
    "visible_pending_source_count",
    "invalidate",
]
