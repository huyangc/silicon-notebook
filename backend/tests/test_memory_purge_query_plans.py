"""SQLite query-plan pins for the Memory purge (no ANALYZE, as in production).

* ``derived_memory_sources`` repeats ``idx_sources_memory_id``'s partial-index
  predicate term for term, so SQLite searches that index (quality review
  P2-1: without it every call scanned ``sources``, 9.6 ms vs 0.32 ms at
  100k rows).
* Every statement a real purge page issues — captured from an actual exit
  of the shared scenario world — searches an index on the tables that grow
  with the data: no full ``SCAN`` of such a table.
"""
from __future__ import annotations

import re
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.memory_purge_cases import EMBED_DIM, build_world, self_exit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'purge-plans.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    return SQLiteRepository(Settings())


class _Recording:
    def __init__(self, inner, statements):
        self._inner = inner
        self._statements = statements

    def execute(self, sql, params=(), *args, **kwargs):
        self._statements.append((str(sql), params))
        return self._inner.execute(sql, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextmanager
def _recorded(repo, monkeypatch):
    database = repo._runtime.database
    statements: list[tuple[str, object]] = []
    for name in ("connect", "write"):
        original = getattr(database, name)

        def recording(*args, _original=original, **kwargs):
            @contextmanager
            def manager():
                with _original(*args, **kwargs) as db:
                    yield _Recording(db, statements)

            return manager()

        monkeypatch.setattr(database, name, recording)
    try:
        yield statements
    finally:
        monkeypatch.undo()


def _plan(connection, sql: str, params) -> list[str]:
    return [
        str(row["detail"])
        for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    ]


def test_derived_memory_sources_searches_the_partial_index(repo, monkeypatch):
    world = build_world(repo, postgres=False)
    refs = [(projection.memory_id, projection.notebook_id)
            for projection in world.projections.values()]
    with _recorded(repo, monkeypatch) as statements:
        found = repo._runtime.memory_service.store.derived_memory_sources(refs)
    assert len(found) == len(refs)
    [(sql, params)] = statements
    with repo._runtime.database.connect() as connection:
        plan = _plan(connection, sql, params)
    assert any("USING INDEX idx_sources_memory_id" in line for line in plan), plan
    assert not any(line.startswith("SCAN sources") for line in plan), plan


# Tables that do not grow with the corpus (one row per notebook / user), and
# the CTE the conflict-candidate delete reads.
_SMALL = {"unified_kg_state", "notebooks", "notebook_members", "users", "refs"}
# ``kg_objects_fts`` is an FTS5 table whose ``object_id`` / ``notebook_id``
# columns are UNINDEXED: no delete by object id can search it (the KG build's
# own delete scans it the same way). The purge deletes those rows once per
# PAGE, never per Memory — pinned by the statement count, registered here.
_FTS_SCAN_PER_PAGE = "SCAN kg_objects_fts VIRTUAL TABLE"


def test_every_statement_of_a_purge_page_searches_an_index(repo, monkeypatch):
    world = build_world(repo, postgres=False)
    with _recorded(repo, monkeypatch) as statements:
        assert self_exit(world, world.alice, world.shared, 1) == 1
    planned = [
        (sql, params) for sql, params in statements
        if sql.lstrip().upper().startswith(("SELECT", "DELETE", "UPDATE", "WITH"))
    ]
    assert len(planned) >= 20, planned
    with repo._runtime.database.connect() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        offenders = []
        for sql, params in planned:
            for line in _plan(connection, sql, params):
                match = re.match(r"SCAN (\w+)", line)
                if line.startswith(_FTS_SCAN_PER_PAGE):
                    continue
                if match and match.group(1) in tables - _SMALL:
                    offenders.append((sql, line))
    assert offenders == [], offenders
