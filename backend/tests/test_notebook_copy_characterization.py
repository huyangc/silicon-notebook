"""``NotebookCopyService.copy_notebook`` behaviour scenarios on SQLite.

The world, the scenarios, the observation and the assertions live in
``tests/notebook_copy_characterization_cases.py``; PostgreSQL runs the same scenarios in
``tests/postgres/test_notebook_copy_characterization_pg.py``. This module only says how
SQLite stores a row and which tables it has.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_copy_characterization_cases as cases

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


BACKEND = cases.Backend(
    name="sqlite",
    insert=_insert,
    tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_copy_notebook_behaviour(scenario, tmp_path, monkeypatch):
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
