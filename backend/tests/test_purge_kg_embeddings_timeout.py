"""T-W4-4 维护站点普查修补:``purge_kg_embeddings`` 的事务内 statement_timeout
放宽必须先于两条整表 DELETE 执行——这是它唯一的可观测契约,用一个记录 SQL
执行顺序的假连接钉住,不需要真实 PostgreSQL。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any


class _SpyDb:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: tuple = ()) -> "_SpyDb":
        self.calls.append((sql, tuple(params)))
        return self


class _FakeDatabase:
    def __init__(self, db: _SpyDb) -> None:
        self._db = db

    @contextmanager
    def write(self):
        yield self._db

    @contextmanager
    def connect(self):
        yield self._db


class _FakeRuntime:
    def __init__(self, db: _SpyDb) -> None:
        self.database = _FakeDatabase(db)


def test_purge_kg_embeddings_raises_statement_timeout_before_deleting() -> None:
    from app.repositories.postgres.maintenance import (
        PostgresMaintenanceAdapter,
        _PURGE_KG_EMBEDDINGS_TIMEOUT_MS,
    )

    db = _SpyDb()
    adapter = PostgresMaintenanceAdapter(_FakeRuntime(db))

    adapter.purge_kg_embeddings("nb-1")

    assert len(db.calls) == 3, db.calls
    set_config_sql, set_config_params = db.calls[0]
    assert "set_config" in set_config_sql
    assert "statement_timeout" in set_config_sql
    # Third arg literal `true` in the SQL text = transaction-local (SET LOCAL
    # equivalent) — the same precedent as notebook_store.py:437/migrator.py:191.
    assert ", true)" in set_config_sql
    assert set_config_params == (f"{_PURGE_KG_EMBEDDINGS_TIMEOUT_MS}ms",)

    delete_kg_sql, delete_kg_params = db.calls[1]
    assert "DELETE FROM knowledge_embeddings" in delete_kg_sql
    assert delete_kg_params == ("nb-1",)

    delete_rel_sql, delete_rel_params = db.calls[2]
    assert "DELETE FROM relation_embeddings" in delete_rel_sql
    assert delete_rel_params == ("nb-1",)
