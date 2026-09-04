"""T-W4-4 维护站点普查修补:整表 DELETE 站点的事务内 statement_timeout
放宽必须(a)先于 DELETE、(b)与 DELETE 在**同一个** ``write()`` 事务块里
——``true``(事务本地)只有同事务才有意义,拆成两个块时放宽随第一个块
提交即丢(T4 质量评 P2-2:假连接必须表达事务边界,否则「移动」变异钉不
住)。用记录 enter/exit 标记的假连接钉住,不需要真实 PostgreSQL;GREATEST
取底语义(不压运维值、0 保持)由 tests/postgres/test_batch_maintenance.py
的真库用例钉。"""
from __future__ import annotations

from contextlib import contextmanager

import pytest


class _SpyDb:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def execute(self, sql: str, params: tuple = ()) -> "_SpyDb":
        self.calls.append(("sql", sql, tuple(params)))
        return self

    def fetchone(self) -> dict:
        return {"c": 0}


class _FakeDatabase:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    @contextmanager
    def write(self):
        self.calls.append(("enter", "write", ()))
        yield _SpyDb(self.calls)
        self.calls.append(("exit", "write", ()))

    @contextmanager
    def connect(self):
        self.calls.append(("enter", "connect", ()))
        yield _SpyDb(self.calls)
        self.calls.append(("exit", "connect", ()))


class _FakeSeams:
    def now(self) -> str:
        return "2026-09-04T00:00:00Z"


class _FakeRuntime:
    def __init__(self, calls: list) -> None:
        self.database = _FakeDatabase(calls)
        self.seams = _FakeSeams()


def _sql_calls_inside_single_write_block(calls: list) -> list[tuple[str, tuple]]:
    """断言全部语句都落在恰好一对 write enter/exit 标记之间,并返回它们。"""
    assert calls[0] == ("enter", "write", ()), calls
    assert calls[-1] == ("exit", "write", ()), calls
    body = calls[1:-1]
    assert all(kind == "sql" for kind, _sql, _params in body), calls
    return [(sql, params) for _kind, sql, params in body]


def _assert_floor_raise(sql: str, params: tuple) -> None:
    assert "set_config" in sql
    assert "statement_timeout" in sql
    # Transaction-local (`true`) — the same precedent as
    # notebook_store.py:437 / migrator.py:191.
    assert ", true)" in sql
    # Floor semantics, not a fixed value: never lowers an operator-raised
    # deployment timeout, and keeps 0 (= timeout disabled) as-is.
    assert "GREATEST" in sql
    assert "WHEN cur = 0" in sql
    # Independent literal on purpose (NOT derived from the module constant):
    # a fat-fingered constant (e.g. 600 → 0.6s, tighter than the 30s
    # default) must turn this red.
    assert params == (600_000,)


@pytest.mark.parametrize(
    ("method", "expected_deletes"),
    [
        (
            "purge_kg_embeddings",
            ["DELETE FROM knowledge_embeddings", "DELETE FROM relation_embeddings"],
        ),
        ("clear_source_index", ["DELETE FROM knowledge_object_sources"]),
        ("clear_chunk_element_index", ["DELETE FROM chunk_elements"]),
    ],
)
def test_whole_notebook_delete_sites_raise_the_timeout_floor_in_the_same_tx(
    method: str, expected_deletes: list[str]
) -> None:
    from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter

    calls: list = []
    adapter = PostgresMaintenanceAdapter(_FakeRuntime(calls))

    getattr(adapter, method)("nb-1")

    sql_calls = _sql_calls_inside_single_write_block(calls)
    _assert_floor_raise(*sql_calls[0])
    delete_sqls = [sql for sql, _params in sql_calls[1:]]
    for index, expected in enumerate(expected_deletes):
        assert expected in delete_sqls[index], sql_calls
        assert sql_calls[1 + index][1][0] == "nb-1"
