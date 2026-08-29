"""PostgreSQL 侧 orphan-cluster keyset 分批清扫的纯 Python 单测(Z6,P0 行为恢复)。

无需活 PostgreSQL(纯 fake db,与 ``test_postgres_knowledge_counts_cache.py`` /
``test_kg_empty_extraction_marker.py`` 的 pending-SQL 守卫同一体裁)——真实语义等价
（新两步版 vs 旧 NOT IN 单条版逐字对账、分批推进）由
``backend/tests/test_incremental_fuse_bounded.py`` 在 SQLite 上跑真库验证；这里只钉
PG 侧那两条 SQL 字符串本身的几件事：① 第一条是**纯 keyset 页读**(有 LIMIT、无孤儿
过滤)——批大小必须界住**扫描行数**而不是被删行数,② 第二条 DELETE 由页读回来的
**主键**驱动(`c.id = ANY(%s)`)而不是键区间,输入以页为界且计划不可能退化成切片扫描,
③ 用的是 NOT EXISTS 而不是回退成 NOT IN,④ keyset 游标 + limit 按位置传给占位符,
⑤ 没有 psycopg 会在运行时炸掉的裸字面 `%`
（历史踩坑,见 ``test_postgres_pending_sql_has_no_literal_percent``）。
"""
from __future__ import annotations

from app.repositories.postgres.governance_store import GovernanceStore


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDB:
    """记录收到的每条 SQL 与参数;按调用次序吐出 canned 结果。

    第一条 execute 是页读,第二条(若有)是页内删除 —— 两者的 canned 结果分开给,
    正是为了能断言「页读返回什么」与「删了几行」是两件独立的事。"""

    def __init__(self, page_rows=(), deleted_rows=()):
        self._queued = [list(page_rows), list(deleted_rows)]
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        rows = self._queued.pop(0) if self._queued else []
        return _Cursor(rows)


_PAGE = [
    {"object_type": "claim", "member_object_id": "ko-a", "id": "cc-1"},
    {"object_type": "concept", "member_object_id": "ko-z", "id": "cc-2"},
]


def _swept(page_rows=_PAGE, deleted_rows=(), **kwargs):
    db = _FakeDB(page_rows=page_rows, deleted_rows=deleted_rows)
    result = GovernanceStore.sweep_orphan_clusters_page(
        db,
        kwargs.get("notebook_id", "nb-1"),
        kwargs.get("after_object_type", ""),
        kwargs.get("after_member_object_id", ""),
        kwargs.get("limit", 5000),
    )
    return db, result


def test_page_read_is_a_pure_keyset_page_not_an_orphan_filtered_one():
    """P1 的核心回归:第一条语句必须是**不带孤儿过滤**的 keyset 页读 + LIMIT。

    被打回的上一版把 LIMIT 放在已经 `NOT EXISTS` 过滤过的子查询上 —— 于是 LIMIT
    界住的是「被删的行」,扫描要一直走到攒够 n 条孤儿为止;零孤儿(常态)时每批
    都扫完整个 notebook 切片,30s statement_timeout 照撞。页读里出现任何孤儿谓词
    都意味着回到了那个形状。"""
    db, _result = _swept()

    sql, _params = db.calls[0]
    assert sql.startswith("SELECT object_type, member_object_id, id FROM concept_clusters")
    assert "LIMIT %s" in sql
    assert "ORDER BY" in sql
    assert "NOT EXISTS" not in sql        # 页读不带孤儿过滤
    assert "knowledge_objects" not in sql
    assert "DELETE" not in sql


def test_page_read_threads_the_keyset_cursor_and_limit_by_position():
    """notebook_id / after_object_type / after_member_object_id / limit 必须按
    这个顺序绑定给占位符 —— 顺序错了游标就推进不到位,分批会死循环或漏批。"""
    db, _result = _swept(
        after_object_type="claim", after_member_object_id="ko-cursor", limit=4096
    )

    _sql, params = db.calls[0]
    assert params == ("nb-1", "claim", "ko-cursor", 4096)


def test_delete_is_driven_by_the_pages_primary_keys():
    """第二条语句的输入是**页读回来的主键**,而不是任何键区间。

    实测(本地 PG16,100 万行 notebook,页 5000):`c.id = ANY(%s)` 走
    Index Scan + Nested Loop Anti Join,5000 次主键探针、5.3ms;而把同一页写成
    键区间 `> 游标 AND <= 页尾` —— 没有 LIMIT 撑着,规划器把行比较当**过滤条件**,
    选 Seq Scan 扫完整个 notebook 切片再 Hash 掉全部 knowledge_objects:删 5000 行
    要碰 1,000,000 行、201ms,而且随 N 线性增长,P1 等于没修。主键 `= ANY` 不可能
    退化成切片扫描,键区间可以,而且是静默的 —— 所以这条守卫钉的是 id 列表形态。"""
    db, _result = _swept()

    sql, params = db.calls[1]
    assert sql.startswith("DELETE FROM concept_clusters AS c")
    assert "c.id = ANY(%s)" in sql
    # 回归:不得改回键区间形态。
    assert "member_object_id COLLATE \"C\") <=" not in sql
    assert params == ("nb-1", ["cc-1", "cc-2"])   # 恰好是这一页的主键,按页序


def test_sweep_uses_not_exists_not_the_legacy_not_in():
    """回归:清扫 SQL 不得退回旧的单条全库 NOT IN 反连接。"""
    db, _result = _swept()

    all_sql = " ".join(sql for sql, _params in db.calls)
    assert "NOT EXISTS" in all_sql
    assert "NOT IN" not in all_sql


def test_sweep_keeps_the_original_notebook_scoped_orphan_semantics():
    """语义登记(逐字等价的另一半证据,配合 test_incremental_fuse_bounded 的真库
    对账):NOT EXISTS 子查询必须带 ``k.notebook_id = c.notebook_id`` —— 否则一个
    在**别的** notebook 下真实存在的对象 id,会被误判为「不是孤儿」,而旧的
    ``NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=%s)`` 是按
    notebook 过滤过的,同样的 id 在旧语义下仍然算孤儿。丢了这个条件是可以从这段
    SQL 文本上直接看出的静态回归,不需要起真库也能钉住。"""
    db, _result = _swept()

    sql, _params = db.calls[1]
    assert "k.notebook_id = c.notebook_id" in sql


def test_sweep_returns_the_page_rows_and_the_deleted_count():
    """返回契约:``(页行, 删除数)``。调用方靠**页**推进游标/判终止,靠**删除数**
    决定要不要 bump cluster_mutation_seq —— 两者不能混成一个数字。"""
    deleted_rows = [{"id": "cc-1"}]
    _db, (page, deleted) = _swept(deleted_rows=deleted_rows)

    assert page == _PAGE
    assert deleted == 1


def test_empty_page_skips_the_delete_entirely():
    """扫到尽头(页为空)时不发第二条语句,直接回 ``([], 0)`` 让循环终止。"""
    db, (page, deleted) = _swept(page_rows=[])

    assert (page, deleted) == ([], 0)
    assert len(db.calls) == 1


def test_sweep_sql_has_no_literal_percent():
    """PG 侧两条 SQL 里都不得出现非占位符的裸 `%`(历史踩坑,见
    ``test_kg_empty_extraction_marker.test_postgres_pending_sql_has_no_literal_percent``
    的完整背景——psycopg 把查询里的 `%` 当占位符起头,裸 `%` 会在**运行时**才炸,
    本地标准门跑不到活 PostgreSQL 那条集成 lane,只能靠这种字符串级守卫本地钉住)。"""
    db, _result = _swept()

    for sql, _params in db.calls:
        offending = [
            sql[index : index + 4]
            for index, char in enumerate(sql)
            if char == "%" and sql[index + 1 : index + 2] not in {"s", "b", "t"}
        ]
        assert not offending, offending
