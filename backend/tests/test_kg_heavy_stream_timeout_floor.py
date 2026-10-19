"""KG 重建的两条流式整表读(PG 侧)必须先把 statement_timeout 抬到维护底线。

`cursor().stream()` 下服务端语句要等客户端读完最后一行才算结束,`statement_timeout`
因此覆盖**整个消费期**(调用方 Python 循环的时间也算进去)。所以 `community_graph_rows`
与 `source_canonical_rows` 都在**函数一开头**调 `raise_statement_timeout_floor(db)` ——
与离线维护的整表 DELETE 同一个函数、同一个 600 s 底线。

这里用记录调用顺序的假连接钉三件事,不需要真实 PostgreSQL(真库行为 —— 紧超时 + 慢消费者
读得完、事务结束后复原 —— 由 `tests/postgres/test_kg_heavy_streams_statement_timeout_pg.py`
钉):
  (a) 放宽是这个函数的**第一条语句**,先于任何读(`names` 查询、`stream()`);
  (b) 放宽语句的形状与离线维护那条逐字一致:事务本地(`, true)`)、取底(`GREATEST`)、
      0 保持(`WHEN cur = 0`)、底线 600_000 ms —— 独立字面量,不从模块常量推;
  (c) 流确实是在放宽**之后**才建的(同一个连接对象上的 `cursor().stream()`)。
"""
from __future__ import annotations

from app.repositories.postgres.unified_kg_store import UnifiedKgStore


class _SpyDb:
    """按调用顺序记录 execute / cursor().stream();execute 返回空可迭代对象。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()):
        self.calls.append(("execute", sql, tuple(params or ())))
        return iter(())

    def cursor(self) -> "_SpyDb":
        return self

    def stream(self, sql: str, params: tuple = ()):
        self.calls.append(("stream", sql, tuple(params or ())))
        return iter(())


def _assert_floor_raise(kind: str, sql: str, params: tuple) -> None:
    assert kind == "execute"
    assert "set_config" in sql
    assert "statement_timeout" in sql
    assert ", true)" in sql          # 事务本地,不泄漏给池里的下一个借用者
    assert "GREATEST" in sql         # 取底:不压低运维调得更高的值
    assert "WHEN cur = 0" in sql     # 0 = 已禁用超时,保持
    assert params == (600_000,)      # 独立字面量:常量被改歪(如 0.6 s)必须变红


def test_community_graph_rows_raises_the_floor_before_anything_else():
    db = _SpyDb()

    UnifiedKgStore.community_graph_rows(db, "nb-1")

    kinds = [kind for kind, _sql, _params in db.calls]
    assert kinds == ["execute", "execute", "stream"], db.calls
    _assert_floor_raise(*db.calls[0])
    assert "DISTINCT canonical_id" in db.calls[1][1]       # names 小查询在放宽之后
    assert "FROM knowledge_relations kr" in db.calls[2][1]  # 流在最后


def test_source_canonical_rows_raises_the_floor_before_the_stream():
    db = _SpyDb()

    UnifiedKgStore.source_canonical_rows(db, "nb-1")

    kinds = [kind for kind, _sql, _params in db.calls]
    assert kinds == ["execute", "stream"], db.calls
    _assert_floor_raise(*db.calls[0])
    assert "FROM knowledge_objects o" in db.calls[1][1]
