"""PostgreSQL 开路计数缓存的 seq-gating 单测(codex 第4轮 P2 + 大库打开卡死修复移植)。

纯 Python + fake db,无需 postgres 服务器——故放在主测试根、不进 backend/tests/postgres/
(那目录整体在无服务器时 skip)。验证 checkup H6 的 visible 计数、以及移植自 sqlite 的
type_status_counts/type_counts/active_object_count/object_type_total/chunk_count 都按
kg_mutation_seq memo:同 seq 不重查、seq 变重查、invalidate 后重查(安全阀),且各 memo
互相独立(一个的 miss/hit 不影响另一个的调用计数)。
"""
from __future__ import annotations

from app.repositories.postgres import knowledge_counts_cache as kcc


class _Cursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeDB:
    """按 SQL 关键字分流到四类计数器:seq 读不计入任何一类;GROUP BY object_type,
    status → type_status_calls;``FROM chunks`` → chunk_calls;其余(pending 相关
    子查询)→ query_calls(向后兼容既有用例的属性名)。"""

    def __init__(
        self,
        seq: int,
        *,
        type_rows=None,
        chunk_count: int = 42,
    ):
        self.seq = seq
        self.query_calls = 0
        self.pending_sql: list[str] = []
        self.type_status_calls = 0
        self.chunk_calls = 0
        self._type_rows = (
            [{"object_type": "concept", "status": "approved", "c": 42}]
            if type_rows is None
            else type_rows
        )
        self._chunk_count = chunk_count

    def execute(self, sql, params=()):
        if "kg_mutation_seq" in sql:
            return _Cursor(row={"kg_mutation_seq": self.seq})
        if "GROUP BY object_type, status" in sql:
            self.type_status_calls += 1
            return _Cursor(rows=list(self._type_rows))
        if "FROM chunks" in sql:
            self.chunk_calls += 1
            return _Cursor(row={"c": self._chunk_count})
        self.query_calls += 1  # pending 相关子查询
        self.pending_sql.append(sql)
        return _Cursor(row={"c": 42})


def test_postgres_h6_visible_count_is_seq_gated_memo():
    kcc.invalidate()  # 清进程缓存,免跨测试污染
    db = _FakeDB(seq=5)
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 1  # 同 kg_mutation_seq → 只跑一次冷查询(seq-gated)

    db.seq = 6  # kg_mutation_seq bump(数据变)
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 2  # seq 变 → 重查

    kcc.invalidate("nb-x")  # 安全阀
    assert kcc.visible_pending_source_count(db, "nb-x") == 42
    assert db.query_calls == 3  # invalidate 后重查


def test_postgres_pending_and_visible_are_independent_memos():
    """全集 pending 与 visible 各自独立 memo(共享 seq gate),互不串缓存。"""
    kcc.invalidate()
    db = _FakeDB(seq=1)
    kcc.pending_source_count(db, "nb-y")
    kcc.visible_pending_source_count(db, "nb-y")
    assert db.query_calls == 2  # 两个独立计数各查一次
    kcc.pending_source_count(db, "nb-y")
    kcc.visible_pending_source_count(db, "nb-y")
    assert db.query_calls == 2  # 同 seq → 都命中缓存


def test_pending_query_is_source_driven_and_reads_latest_run_once():
    """Regression: never evaluate latest-run state once per knowledge object."""
    db = _FakeDB(seq=1)

    assert kcc._pending_query(db, "nb-shape", visible_only=True) == 42

    sql = db.pending_sql[-1]
    assert sql.count("LATERAL") == 3
    assert sql.count("FROM extraction_runs er") == 1
    assert sql.count("FROM knowledge_objects k") == 1
    assert "ORDER BY er.created_at DESC,er.ordinal DESC LIMIT 1" in sql
    assert "source_kg.found IS NULL" in sql
    assert "s.source_type NOT IN ('memory','knowhow')" in sql
    assert "NOT EXISTS(SELECT 1 FROM knowledge_objects" not in sql


def test_physical_pending_query_keeps_hidden_sources():
    db = _FakeDB(seq=1)

    assert kcc._pending_query(db, "nb-shape", visible_only=False) == 42

    assert "s.source_type NOT IN" not in db.pending_sql[-1]


def test_type_status_counts_is_seq_gated_memo():
    kcc.invalidate()
    db = _FakeDB(seq=1)
    expect = {("concept", "approved"): 42}
    assert kcc.type_status_counts(db, "nb-t") == expect
    assert kcc.type_status_counts(db, "nb-t") == expect
    assert db.type_status_calls == 1  # 同 kg_mutation_seq → 只跑一次冷 GROUP BY

    db.seq = 2  # kg_mutation_seq bump(数据变)
    assert kcc.type_status_counts(db, "nb-t") == expect
    assert db.type_status_calls == 2  # seq 变 → 重查

    kcc.invalidate("nb-t")  # 安全阀
    assert kcc.type_status_counts(db, "nb-t") == expect
    assert db.type_status_calls == 3  # invalidate 后重查


def test_type_counts_filters_purely_in_python_without_extra_query():
    """type_counts 是 type_status_counts 之上的纯 Python 过滤,不应触发第二次 GROUP BY。"""
    kcc.invalidate()
    db = _FakeDB(
        seq=1,
        type_rows=[
            {"object_type": "concept", "status": "approved", "c": 3},
            {"object_type": "concept", "status": "deprecated", "c": 5},
            {"object_type": "claim", "status": "reviewed", "c": 2},
        ],
    )
    # statuses=None → 排除 deprecated(sqlite 侧同一默认语义)
    assert kcc.type_counts(db, "nb-tc") == {"concept": 3, "claim": 2}
    # 显式白名单只留 deprecated
    assert kcc.type_counts(db, "nb-tc", ("deprecated",)) == {"concept": 5}
    # 空白名单 → 空字典(knowledge_type_count_rows 的边界,不是 None)
    assert kcc.type_counts(db, "nb-tc", ()) == {}
    assert db.type_status_calls == 1  # 三次调用共享同一份底层 memo


def test_active_object_count_sums_non_deprecated_types():
    kcc.invalidate()
    db = _FakeDB(
        seq=1,
        type_rows=[
            {"object_type": "concept", "status": "approved", "c": 3},
            {"object_type": "claim", "status": "reviewed", "c": 4},
            {"object_type": "concept", "status": "deprecated", "c": 5},
        ],
    )
    assert kcc.active_object_count(db, "nb-a") == 7
    assert db.type_status_calls == 1


def test_object_type_total_falsy_status_counts_all_including_deprecated():
    kcc.invalidate()
    db = _FakeDB(
        seq=1,
        type_rows=[
            {"object_type": "concept", "status": "approved", "c": 3},
            {"object_type": "concept", "status": "deprecated", "c": 5},
            {"object_type": "claim", "status": "approved", "c": 9},
        ],
    )
    assert kcc.object_type_total(db, "nb-o", "concept") == 8  # falsy status: 含 deprecated
    assert kcc.object_type_total(db, "nb-o", "concept", "approved") == 3
    assert kcc.object_type_total(db, "nb-o", "concept", "deprecated") == 5
    assert kcc.object_type_total(db, "nb-o", "missing-type", "approved") == 0
    assert db.type_status_calls == 1  # 四次调用共享同一份底层 memo


def test_chunk_count_is_seq_gated_memo_independent_from_type_memo():
    kcc.invalidate()
    db = _FakeDB(seq=1, chunk_count=7)
    assert kcc.chunk_count(db, "nb-c") == 7
    assert kcc.chunk_count(db, "nb-c") == 7
    assert db.chunk_calls == 1  # 同 seq → 只跑一次冷 COUNT

    db.seq = 2
    assert kcc.chunk_count(db, "nb-c") == 7
    assert db.chunk_calls == 2  # seq 变 → 重查

    kcc.invalidate("nb-c")
    assert kcc.chunk_count(db, "nb-c") == 7
    assert db.chunk_calls == 3  # invalidate 后重查

    # chunk memo 与 type/status memo 是独立键空间:同一个 notebook_id 在两边各自
    # 第一次访问都必须真的冷查一次,互不借对方的命中。
    assert kcc.type_status_counts(db, "nb-c") == {("concept", "approved"): 42}
    assert db.type_status_calls == 1


def test_invalidate_none_clears_every_memo():
    """P2-1/S3:全局失效必须清空全部 4 个 memo,而不仅仅是其中几个——把「新增 memo
    必须在 invalidate() 里登记」这条约定配上守卫。

    (T-A3 v4:这里曾经是 5 个 memo——``review_queue_total`` 已随 codex #638 R1
    整体撤除,总量现在存在 ``app.services.review_queue_memo.ReviewQueueMemo``
    里,不受这个模块的 invalidate() 管辖,见该模块 module docstring。)

    变异复现:删掉 ``invalidate()`` 里任意一个 ``.clear()`` 调用,这条用例必须变红。"""
    kcc.invalidate()
    db = _FakeDB(seq=1, chunk_count=7)
    kcc.type_status_counts(db, "nb-all")
    kcc.pending_source_count(db, "nb-all")
    kcc.visible_pending_source_count(db, "nb-all")
    kcc.chunk_count(db, "nb-all")
    # 四个 memo 全部非空(populate 成功,后面的清空断言才有意义)。
    assert kcc._MEMO and kcc._PENDING and kcc._VISIBLE_PENDING
    assert kcc._CHUNKS

    kcc.invalidate()  # 全局清空形态(notebook_id=None)

    assert kcc._MEMO == {}
    assert kcc._PENDING == {}
    assert kcc._VISIBLE_PENDING == {}
    assert kcc._CHUNKS == {}


class _InterruptingDB(_FakeDB):
    """在冷查询 SQL(``match`` 子串)执行期间(``execute`` 内部,即锁外阶段)触发一次
    ``kcc.invalidate(interrupt_notebook)``,用于验证 epoch 写回守卫:同一 notebook 的
    并发 invalidate 必须让本次结果不留 memo,别的 notebook 的 invalidate 必须不受
    影响(per-notebook 隔离,F1)。只触发一次,避免递归/重复失效。"""

    def __init__(self, seq: int, *, interrupt_notebook: str, match: str, **kwargs):
        super().__init__(seq, **kwargs)
        self._interrupt_notebook = interrupt_notebook
        self._match = match
        self._fired = False

    def execute(self, sql, params=()):
        cursor = super().execute(sql, params)
        if not self._fired and self._match in sql:
            self._fired = True
            kcc.invalidate(self._interrupt_notebook)
        return cursor


def test_epoch_guard_rejects_writeback_when_same_notebook_invalidated_mid_query():
    """F2:冷查询(GROUP BY)执行期间,同一 notebook 被 invalidate → 本次返回值仍正确,
    但不留 memo,下一次读会重查。

    变异复现:把 ``_seq_gated`` 写回守卫 ``if epoch == _epoch_of(notebook_id):`` 改成
    ``if True:`` 会让这条用例失败——memo 会被错误写入,下一次读不再重查。"""
    kcc.invalidate()
    db = _InterruptingDB(
        seq=1, interrupt_notebook="nb-race", match="GROUP BY object_type, status"
    )
    result = kcc.type_status_counts(db, "nb-race")
    assert result == {("concept", "approved"): 42}  # 这次返回值仍正确
    assert "nb-race" not in kcc._MEMO  # 但没有留下 memo(写回被拒)
    assert kcc.type_status_counts(db, "nb-race") == {("concept", "approved"): 42}
    assert db.type_status_calls == 2  # 下一次读确实重查了


def test_epoch_guard_is_per_notebook_other_notebook_invalidate_does_not_block():
    """F1:冷查询(chunk COUNT)期间,invalidate 的是**另一个** notebook → 写回不受影响,
    memo 正常命中。

    变异复现:把 per-notebook 判据退化回全局单值 epoch(即任何 notebook 的
    invalidate 都会拒绝所有在途写回)会让这条用例失败——nb-mine 的写回会被 nb-other
    的 invalidate 误伤,memo 不会命中,第二次读会多一次冷查。"""
    kcc.invalidate()
    db = _InterruptingDB(
        seq=1, interrupt_notebook="nb-other", match="FROM chunks", chunk_count=7
    )
    result = kcc.chunk_count(db, "nb-mine")
    assert result == 7
    assert "nb-mine" in kcc._CHUNKS  # 写回成功
    assert kcc.chunk_count(db, "nb-mine") == 7
    assert db.chunk_calls == 1  # 没有被误伤,不需要重查


def test_lru_eviction_evicts_the_oldest_notebook_first(monkeypatch):
    """F3:memo 填满(``_MAX_NOTEBOOKS`` 个)后再访问一个新 notebook,应淘汰最早访问的
    那个,最近访问的仍然命中。

    变异复现:把淘汰逻辑的 ``memo.popitem(last=False)``(淘汰最老)误改成
    ``memo.popitem(last=True)``(淘汰最新)会让这条用例失败——届时被淘汰的会是
    nb-3(最新),再读 nb-1(本应已淘汰)反而命中缓存,而再读 nb-3(本应仍命中)反而
    触发重查,与断言相反。"""
    kcc.invalidate()
    monkeypatch.setattr(kcc, "_MAX_NOTEBOOKS", 2)
    db = _FakeDB(seq=1, chunk_count=7)

    kcc.chunk_count(db, "nb-1")
    kcc.chunk_count(db, "nb-2")
    kcc.chunk_count(db, "nb-3")  # 超过上限(2),触发一次淘汰
    assert db.chunk_calls == 3

    kcc.chunk_count(db, "nb-1")  # 最早访问 → 应已被淘汰 → 重查
    assert db.chunk_calls == 4

    kcc.chunk_count(db, "nb-3")  # 最近访问 → 应仍命中 → 不重查
    assert db.chunk_calls == 4


class _EvictingInterruptDB(_FakeDB):
    """在冷查询 SQL(``match`` 子串)执行期间(锁外阶段)先 invalidate 目标 notebook,
    再 invalidate 足够多的其它 notebook,把目标 notebook 刚推进的 ``_EPOCHS`` 条目从
    有界 LRU 里挤出去——验证 C2:被挤出去之后,目标 notebook 的写回必须仍然被拒绝
    (fail closed),不能因为 ``_epoch_of`` 退回默认值 0 而被误判成「没被 invalidate」。
    只触发一次,避免递归/重复失效。"""

    def __init__(self, seq: int, *, target_notebook: str, match: str, filler_count: int, **kwargs):
        super().__init__(seq, **kwargs)
        self._target_notebook = target_notebook
        self._match = match
        self._filler_count = filler_count
        self._fired = False

    def execute(self, sql, params=()):
        cursor = super().execute(sql, params)
        if not self._fired and self._match in sql:
            self._fired = True
            kcc.invalidate(self._target_notebook)
            for i in range(self._filler_count):
                kcc.invalidate(f"nb-filler-{i}")
        return cursor


def test_epoch_lru_eviction_fails_closed_for_evicted_notebook(monkeypatch):
    """C2(codex #621 R1 P2):目标 notebook 的 epoch 条目一旦被有界 LRU 淘汰出
    ``_EPOCHS``,``_epoch_of`` 会退回默认值 0——如果淘汰不推进全局代次,这次在途写回
    会被误判成「没被 invalidate」,把 invalidate 之前的陈旧快照写进 memo。

    变异复现:把 ``invalidate()`` 淘汰分支里新加的 ``_GLOBAL_EPOCH += 1`` 去掉,这条
    用例必须变红——被淘汰后 `_epoch_of` 与采样时的 epoch 重新相等,写回被误放行,
    ``nb-target`` 会留下 memo。"""
    kcc.invalidate()
    monkeypatch.setattr(kcc, "_MAX_NOTEBOOKS", 2)
    db = _EvictingInterruptDB(
        seq=1,
        target_notebook="nb-target",
        match="GROUP BY object_type, status",
        filler_count=3,  # 超过 _MAX_NOTEBOOKS(2),确保 nb-target 被挤出 _EPOCHS
    )
    result = kcc.type_status_counts(db, "nb-target")
    assert result == {("concept", "approved"): 42}  # 这次返回值仍正确
    assert "nb-target" not in kcc._EPOCHS  # 佐证:目标 notebook 的 epoch 条目确实被淘汰
    assert "nb-target" not in kcc._MEMO  # 写回被拒:目标库没有留下 memo 条目
    assert kcc.type_status_counts(db, "nb-target") == {("concept", "approved"): 42}
    assert db.type_status_calls == 2  # 下一次读确实重查了(没有被陈旧地钉在 memo 里)


class _WarmDB(_FakeDB):
    """warm_all 的 fake db:notebooks 列表固定,某个 notebook 的 GROUP BY 故意抛
    ``psycopg.Error``,断言 warm_all 吞掉它、rollback、并继续处理下一个 notebook
    (PG 的事务语义要求出错后必须 rollback 连接才能继续 execute——与 sqlite 版
    「一次失败不影响后续查询」不同,这是本次移植新增的行为,专门测)。"""

    def __init__(self, *, fail_notebook: str):
        super().__init__(seq=1)
        self._fail_notebook = fail_notebook
        self._current_notebook = None
        self.rollback_calls = 0

    def execute(self, sql, params=()):
        if "FROM notebooks WHERE status" in sql:
            return _Cursor(rows=[{"id": "nb-ok"}, {"id": self._fail_notebook}])
        if params:
            self._current_notebook = params[0]
        if (
            self._current_notebook == self._fail_notebook
            and "GROUP BY object_type, status" in sql
        ):
            from psycopg import Error
            raise Error("boom")
        return super().execute(sql, params)

    def rollback(self):
        self.rollback_calls += 1


def test_warm_all_rolls_back_and_continues_past_a_failing_notebook():
    kcc.invalidate()
    db = _WarmDB(fail_notebook="nb-broken")
    progress_calls: list[tuple[int, int]] = []

    total = kcc.warm_all(db, progress=lambda done, total: progress_calls.append((done, total)))

    assert total == 2
    assert db.rollback_calls == 1  # 出错的那个 notebook 触发一次 rollback
    assert progress_calls == [(1, 2), (2, 2)]  # 两个 notebook 都推进了 progress,顺序不乱
    # 失败 notebook 的 memo 没有写入(下一次访问仍会冷查),健康 notebook 的 memo 已经暖好。
    assert "nb-ok" in kcc._MEMO
    assert "nb-broken" not in kcc._MEMO
