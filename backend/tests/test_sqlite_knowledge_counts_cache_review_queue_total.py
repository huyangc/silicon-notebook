"""SQLite ``knowledge_counts_cache.review_queue_total`` 的 seq-gating 单测(R3
T-A3)。纯 Python + fake db,无需真实 sqlite3 连接——镜像
``test_postgres_knowledge_counts_cache.py`` 的 fake-db 模式。

只覆盖新增的 ``review_queue_total`` memo:既有五个 sqlite 侧 memo
(type_status_counts/pending_source_count/visible_pending_source_count/
chunk_count/warm_all)的既有覆盖不变。这里要证的是——

1. 同 seq 命中、seq 变重查、``invalidate`` 安全阀后重查(基本 memo 行为);
2. epoch 保护形态对齐 ``pending_source_count`` / ``visible_pending_source_count``
   (有 epoch 校验),不是 ``type_status_counts`` / ``chunk_count``(无 epoch 校验)——
   sqlite 侧的 epoch 是全局单值(``_INVALIDATION_EPOCH``),没有 per-notebook 隔离,
   所以「别的 notebook 被 invalidate」也会拒绝本次写回,这是已知、接受的从简行为,
   不是 bug(见模块头注释);
3. 不进 warm_all(懒算)。
"""
from __future__ import annotations

from app.repositories.sqlite import knowledge_counts_cache as kcc


class _Cursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeDB:
    """按 SQL 关键字分流:seq 读不计入任何一类;``FROM chunks`` → chunk_calls;
    ``FROM knowledge_relations`` → review_queue_total_calls;``GROUP BY
    object_type, status`` → type_status_calls;``FROM notebooks WHERE status``
    (warm_all 的 notebook 枚举)按固定列表回应。"""

    def __init__(
        self,
        seq: int,
        *,
        chunk_count: int = 42,
        review_queue_total: int = 17,
        notebook_ids=("nb-ok",),
    ):
        self.seq = seq
        self.type_status_calls = 0
        self.chunk_calls = 0
        self.review_queue_total_calls = 0
        self._chunk_count = chunk_count
        self._review_queue_total = review_queue_total
        self._notebook_ids = notebook_ids

    def execute(self, sql, params=()):
        if "kg_mutation_seq" in sql:
            return _Cursor(row={"kg_mutation_seq": self.seq})
        if "FROM notebooks WHERE status" in sql:
            return _Cursor(rows=[{"id": nb} for nb in self._notebook_ids])
        if "GROUP BY object_type, status" in sql:
            self.type_status_calls += 1
            return _Cursor(rows=[{"object_type": "concept", "status": "approved", "c": 42}])
        if "FROM chunks" in sql:
            self.chunk_calls += 1
            return _Cursor(row={"c": self._chunk_count})
        if "FROM knowledge_relations" in sql:
            self.review_queue_total_calls += 1
            return _Cursor(row={"c": self._review_queue_total})
        # pending-source 相关子查询(不需要在这里精确建模,给个稳定值即可)——
        # ``_pending_source_count_query`` 读 ``row[0]``(位置访问),既有 fake db 用的
        # dict 行不支持整数下标,所以这里同时塞一个整数键 0。
        return _Cursor(row={"c": 0, 0: 0}, rows=[])


def test_review_queue_total_is_seq_gated_memo():
    kcc.invalidate()
    db = _FakeDB(seq=1, review_queue_total=17)
    assert kcc.review_queue_total(db, "nb-r") == 17
    assert kcc.review_queue_total(db, "nb-r") == 17
    assert db.review_queue_total_calls == 1  # 同 kg_mutation_seq → 只跑一次冷 COUNT

    db.seq = 2  # kg_mutation_seq bump(审核动作落库)
    assert kcc.review_queue_total(db, "nb-r") == 17
    assert db.review_queue_total_calls == 2  # seq 变 → 重查

    kcc.invalidate("nb-r")  # 安全阀
    assert kcc.review_queue_total(db, "nb-r") == 17
    assert db.review_queue_total_calls == 3  # invalidate 后重查


def test_review_queue_total_is_independent_key_space_from_other_memos():
    kcc.invalidate()
    db = _FakeDB(seq=1, chunk_count=7, review_queue_total=9)
    assert kcc.review_queue_total(db, "nb-x") == 9
    assert kcc.chunk_count(db, "nb-x") == 7
    assert kcc.type_status_counts(db, "nb-x") == {("concept", "approved"): 42}
    # 每个 memo 各自第一次访问都必须真的冷查一次,互不借对方的命中。
    assert db.review_queue_total_calls == 1
    assert db.chunk_calls == 1
    assert db.type_status_calls == 1

    # 再访问一次 review_queue_total(同 seq)不应牵连其它 memo 的调用计数。
    assert kcc.review_queue_total(db, "nb-x") == 9
    assert db.review_queue_total_calls == 1
    assert db.chunk_calls == 1
    assert db.type_status_calls == 1


class _InterruptingDB(_FakeDB):
    """在冷查询 SQL(``match`` 子串)执行期间(``execute`` 内部,即锁外阶段)触发一次
    ``kcc.invalidate(interrupt_notebook)``。sqlite 侧是**全局单值** epoch
    (``_INVALIDATION_EPOCH``),没有 per-notebook 隔离——所以无论 interrupt_notebook
    是不是本次读取的 notebook,写回都会被拒绝(与 pending_source_count 既有行为
    一致,不是本次新增的缺陷)。"""

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


def test_review_queue_total_epoch_guard_rejects_writeback_when_invalidated_mid_query():
    """冷 COUNT 执行期间,同一 notebook 被 invalidate → 本次返回值仍正确,但不留
    memo,下一次读会重查。

    变异复现:把写回守卫 ``if invalidation_epoch == _INVALIDATION_EPOCH:`` 改成
    ``if True:`` 会让这条用例失败——memo 会被错误写入,下一次读不再重查。"""
    kcc.invalidate()
    db = _InterruptingDB(
        seq=1, interrupt_notebook="nb-race", match="FROM knowledge_relations",
        review_queue_total=17,
    )
    result = kcc.review_queue_total(db, "nb-race")
    assert result == 17  # 这次返回值仍正确
    assert "nb-race" not in kcc._REVIEW_QUEUE_TOTAL  # 但没有留下 memo(写回被拒)
    assert kcc.review_queue_total(db, "nb-race") == 17
    assert db.review_queue_total_calls == 2  # 下一次读确实重查了


def test_review_queue_total_epoch_guard_is_global_not_per_notebook():
    """已知、接受的从简行为(与 sqlite 侧 pending_source_count 一致):
    invalidate **别的** notebook 也会让本次写回被拒——sqlite 的 epoch 是全局单值,
    不像 PG 侧那样按 notebook 隔离。这不是本次要修的 bug,只是把既有行为钉在
    review_queue_total 上,避免以后有人误以为它是 per-notebook 隔离的。"""
    kcc.invalidate()
    db = _InterruptingDB(
        seq=1, interrupt_notebook="nb-other", match="FROM knowledge_relations",
        review_queue_total=17,
    )
    result = kcc.review_queue_total(db, "nb-mine")
    assert result == 17  # 返回值仍正确
    assert "nb-mine" not in kcc._REVIEW_QUEUE_TOTAL  # 全局 epoch → 写回也被拒
    assert kcc.review_queue_total(db, "nb-mine") == 17
    assert db.review_queue_total_calls == 2  # 因为没留 memo,下一次读又重查了一次


def test_review_queue_total_reads_seq_before_running_count():
    """P2-2(硬读序契约):冷路径必须先点读 ``kg_mutation_seq``,再跑 COUNT——内容
    永远不旧于标签,反序会让 carry 把陈旧内容续成新版本。断言 fake db 收到的头两条
    SQL 顺序:第一条是 seq 点读,第二条才是 COUNT。

    变异复现:把 ``review_queue_total`` 内部顺序改成「先跑冷 COUNT 再读 seq」会让
    这条用例失败——第一条 SQL 会变成 ``FROM knowledge_relations``。"""
    kcc.invalidate()
    order: list[str] = []

    class _OrderedDB(_FakeDB):
        def execute(self, sql, params=()):
            if "kg_mutation_seq" in sql:
                order.append("seq")
            elif "FROM knowledge_relations" in sql:
                order.append("count")
            return super().execute(sql, params)

    db = _OrderedDB(seq=1, review_queue_total=17)
    assert kcc.review_queue_total(db, "nb-order") == 17
    assert order[:2] == ["seq", "count"]


def test_carry_review_queue_total_retags_seq_label_only():
    """P1-2:seq 匹配时只改标签(seq→new_seq),缓存的 VALUE 原样不动——验证「verified
    翻转后 total 不冷算」的计数器断言:carry 之后再读一次,不触发新的冷 COUNT。"""
    kcc.invalidate()
    db = _FakeDB(seq=1, review_queue_total=17)
    assert kcc.review_queue_total(db, "nb-carry") == 17
    assert db.review_queue_total_calls == 1

    kcc.carry_review_queue_total("nb-carry", expected_seq=1, new_seq=2)
    assert kcc._REVIEW_QUEUE_TOTAL["nb-carry"] == (2, 17)  # 标签前移,值不变

    db.seq = 2  # 模拟 set_edge_review 的 kg_mutation_seq 已经真的 bump 到 2
    assert kcc.review_queue_total(db, "nb-carry") == 17
    assert db.review_queue_total_calls == 1  # 没有新的冷 COUNT——carry 命中


def test_carry_review_queue_total_drops_entry_on_seq_mismatch():
    """seq 不符时 carry 必须丢弃该条目(fail-closed),不能猜测/硬套新标签——下一次
    读会重新冷算而不是返回一个可能挂错版本的旧值。"""
    kcc.invalidate()
    db = _FakeDB(seq=1, review_queue_total=17)
    assert kcc.review_queue_total(db, "nb-mismatch") == 17
    assert db.review_queue_total_calls == 1

    kcc.carry_review_queue_total("nb-mismatch", expected_seq=5, new_seq=6)
    assert "nb-mismatch" not in kcc._REVIEW_QUEUE_TOTAL  # 条目被丢弃

    db.seq = 6
    assert kcc.review_queue_total(db, "nb-mismatch") == 17
    assert db.review_queue_total_calls == 2  # 丢弃后确实重新冷算了


def test_carry_review_queue_total_is_a_noop_when_memo_is_cold():
    """从未暖过的 notebook 调用 carry 必须是安全的 no-op,不能凭空造出一条 memo。"""
    kcc.invalidate()
    kcc.carry_review_queue_total("nb-never-warm", expected_seq=1, new_seq=2)
    assert "nb-never-warm" not in kcc._REVIEW_QUEUE_TOTAL


def test_invalidate_none_clears_every_memo():
    """P2-1/S3:全局失效必须清空全部 5 个 memo,而不仅仅是其中几个——把「新增 memo
    必须在 invalidate() 里登记」这条约定配上守卫。

    变异复现:删掉 ``invalidate()`` 里任意一个 ``.clear()`` 调用,这条用例必须变红。"""
    kcc.invalidate()
    db = _FakeDB(seq=1, chunk_count=7, review_queue_total=9)
    kcc.type_status_counts(db, "nb-all")
    kcc.pending_source_count(db, "nb-all")
    kcc.visible_pending_source_count(db, "nb-all")
    kcc.chunk_count(db, "nb-all")
    kcc.review_queue_total(db, "nb-all")
    assert kcc._MEMO and kcc._PENDING and kcc._VISIBLE_PENDING
    assert kcc._CHUNKS and kcc._REVIEW_QUEUE_TOTAL

    kcc.invalidate()  # 全局清空形态(notebook_id=None)

    assert kcc._MEMO == {}
    assert kcc._PENDING == {}
    assert kcc._VISIBLE_PENDING == {}
    assert kcc._CHUNKS == {}
    assert kcc._REVIEW_QUEUE_TOTAL == {}


def test_review_queue_total_not_included_in_warm_all():
    """T-A3 明确不进 warm_all(大库冷 COUNT ~1.1s,懒算)——warm_all 跑完之后,
    review_queue_total 这个 notebook 仍应是冷(memo 里没有它的条目)。"""
    kcc.invalidate()
    db = _FakeDB(seq=1, review_queue_total=17, notebook_ids=("nb-ok",))
    kcc.warm_all(db)
    assert "nb-ok" not in kcc._REVIEW_QUEUE_TOTAL
    assert db.review_queue_total_calls == 0  # warm_all 从未触碰这个 memo
    # 但按需调用时仍然可用、仍然走 seq-gated memo。
    assert kcc.review_queue_total(db, "nb-ok") == 17
    assert "nb-ok" in kcc._REVIEW_QUEUE_TOTAL
