"""``NotebookCopyService.copy_notebook`` characterization on SQLite.

The world, the scenarios, the observation and both assertion layers live in
``tests/notebook_copy_characterization_cases.py`` (its docstring explains the two
layers); PostgreSQL runs the same scenarios in
``tests/postgres/test_notebook_copy_characterization_pg.py``. This module only says how
SQLite stores a row, which tables it has and how its statements are traced.

Re-record the split oracle (never needed for a zero-behaviour-change refactor)::

    REGEN_NOTEBOOK_COPY_CHARACTERIZATION=1 PYTHONPATH=backend \\
      python -m pytest backend/tests/test_notebook_copy_characterization.py -n0
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.repositories.sqlite.database import SqliteDatabase
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_copy_characterization_cases as cases

ORACLE_DIR = Path(__file__).parent / "fixtures" / "notebook_copy_characterization" / "sqlite"
REGEN = os.environ.get("REGEN_NOTEBOOK_COPY_CHARACTERIZATION") == "1"

_FTS_SHADOW = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [json.dumps(values[c], ensure_ascii=False) if c in cases.JSON_COLUMNS else values[c]
         for c in columns],
    )


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return [n for n in names if not n.startswith("sqlite_") and not n.endswith(_FTS_SHADOW)]


def _application_statement(statement: str) -> bool:
    """Statements the application itself issued: FTS5 runs nested statements of its own
    (``-- `` comments, quoted ``'main'.`` shadow tables) that change with the SQLite
    version and are not the copy's."""
    return not statement.startswith("--") and "'main'." not in statement


@contextlib.contextmanager
def _record_sql(repo, recorder: cases.Recorder, monkeypatch):
    def trace(statement: str) -> None:
        if recorder.on_thread() and _application_statement(statement):
            recorder.timeline.append({"sql": recorder.canon.text(" ".join(statement.split()))})

    original = SqliteDatabase._new_connection

    def new_connection(self, *args, **kwargs):
        connection = original(self, *args, **kwargs)
        connection.set_trace_callback(trace)
        return connection

    monkeypatch.setattr(SqliteDatabase, "_new_connection", new_connection)
    local = getattr(repo._runtime.database._local, "conn", None)
    if local is not None:
        local.set_trace_callback(trace)
    try:
        yield
    finally:
        if local is not None:
            local.set_trace_callback(None)


BACKEND = cases.Backend(
    name="sqlite",
    insert=_insert,
    tables=_tables,
    record_sql=_record_sql,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_copy_notebook_characterization(scenario, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'copy.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(storage))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteRepository(Settings())
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)
    cases.assert_matches_oracle(observation, ORACLE_DIR / f"{scenario.name}.json", regen=REGEN)


def test_split_oracle_covers_every_scenario():
    """One oracle file per scenario, no stray files."""
    if REGEN:
        return
    assert sorted(p.stem for p in ORACLE_DIR.glob("*.json")) == sorted(
        s.name for s in cases.SCENARIOS)
