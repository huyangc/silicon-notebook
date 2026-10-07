# backend/tests/test_memory_chunk_write_sqlite_plans.py
"""E4-1b: SQLite query plans of the Memory probes the chunk write paths run.

Production SQLite never runs ``ANALYZE`` on these tables, so the planner decides
from the schema alone; these pins run the production statements (the module
constants and the import's own builder) on a freshly migrated database WITHOUT
``ANALYZE`` and require an index SEARCH, never a SCAN of ``sources``:

* ``ChunkStore`` probe, Knowhow transfer probe, sync import probe: primary-key
  lookups of ``sources`` (``sqlite_autoindex_sources_1``), the import one for a
  full per-batch ``IN`` list;
* KG build publish: the notebook's Memory sources by
  ``idx_sources_nb_hidden_type (notebook_id=? AND source_type=?)``, so the probe
  is bounded by the notebook, not by the Memory sources of the whole database.

The PostgreSQL twins are ``tests/postgres/test_memory_chunk_write_explain_pins.py``.
"""
from __future__ import annotations

import re

import pytest

from app.core.config import Settings
from app.migration.sync.import_ import _ROW_BATCH, _source_rows_statement
from app.repositories.sqlite import chunk_store, kg_build_job_store, knowhow_transfer_store
from app.services.sqlite_repository import SQLiteRepository

_PK = re.compile(
    r"^SEARCH sources USING (?:COVERING )?INDEX sqlite_autoindex_sources_1 \(id=\?\)$"
    r"|^SEARCH sources USING INTEGER PRIMARY KEY"
)
_NOTEBOOK_MEMORY = re.compile(
    r"^SEARCH sources USING (?:COVERING )?INDEX idx_sources_nb_hidden_type "
    r"\(notebook_id=\? AND source_type=\?\)$"
)


@pytest.fixture
def connection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'plans.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        with repo._runtime.chunk_store.database.connect() as db:
            yield db
    finally:
        repo.close()


def _plan(db, sql: str, params: tuple) -> list[str]:
    return [str(row[3]) for row in db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()]


@pytest.mark.parametrize(
    ("name", "sql", "params", "expected"),
    [
        ("chunk_store", chunk_store.MEMORY_PROBE_SQL, ("src-1",), _PK),
        ("transfer", knowhow_transfer_store.MEMORY_PROBE_SQL, ("src-1",), _PK),
        (
            # a full production batch: the import probes ``_ROW_BATCH`` ids per statement
            "import",
            _source_rows_statement(_ROW_BATCH),
            tuple(f"src-{i}" for i in range(_ROW_BATCH)),
            _PK,
        ),
        ("publish", kg_build_job_store.NOTEBOOK_MEMORY_SOURCES_SQL, ("nb",), _NOTEBOOK_MEMORY),
    ],
)
def test_memory_probe_searches_an_index_without_analyze(connection, name, sql, params, expected):
    assert connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='sqlite_stat1'"
    ).fetchone()[0] == 0 or not connection.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone()
    plan = _plan(connection, sql, params)
    assert not any(line.startswith("SCAN sources") for line in plan), (name, plan)
    assert any(expected.search(line) for line in plan), (name, plan)
