"""KG 重建的两条流式整表读(PG 侧)必须在开始取流**之前**先把 statement_timeout 抬到维护底线。

`cursor().stream()` 下服务端语句要等客户端读完最后一行才算结束,`statement_timeout`
因此覆盖**整个消费期**(调用方 Python 循环的时间也算进去)。所以 `community_graph_rows`
与 `source_canonical_rows` 都调 `raise_statement_timeout_floor(db)` —— 与离线维护的整表
DELETE 同一个函数、同一个 600 s 底线。

真正的要求只有两条,这里只钉这两条(不要求它是函数的第一条语句、也不逐段比 SQL 文本),
用记录调用顺序的假连接,不需要真实 PostgreSQL:
  (a) 放宽语句在**同一个连接**上、**先于** `cursor().stream()` 发出;
  (b) 放宽语句是事务本地的、底线是 600_000 ms(最小形状断言;完整的取底 / 0 保持 / 复原语义
      由 `tests/postgres/test_batch_maintenance.py` 与
      `tests/postgres/test_kg_heavy_streams_statement_timeout_pg.py` 在真库上钉)。
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


def _assert_floor_raised_before_the_stream(calls: list) -> None:
    raises = [
        index for index, (kind, sql, _params) in enumerate(calls)
        if kind == "execute" and "set_config" in sql and "statement_timeout" in sql
    ]
    streams = [index for index, (kind, _sql, _params) in enumerate(calls) if kind == "stream"]
    assert len(raises) == 1 and len(streams) == 1, calls
    assert raises[0] < streams[0], calls
    _kind, sql, params = calls[raises[0]]
    assert ", true)" in sql          # 事务本地,不泄漏给池里的下一个借用者
    assert params == (600_000,)      # 独立字面量:常量被改歪(如 0.6 s)必须变红


def test_community_graph_rows_raises_the_floor_before_the_stream():
    db = _SpyDb()

    UnifiedKgStore.community_graph_rows(db, "nb-1")

    _assert_floor_raised_before_the_stream(db.calls)


def test_source_canonical_rows_raises_the_floor_before_the_stream():
    db = _SpyDb()

    UnifiedKgStore.source_canonical_rows(db, "nb-1")

    _assert_floor_raised_before_the_stream(db.calls)
