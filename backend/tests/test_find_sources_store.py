"""SQLite ``SourceStore.find_sources_metadata_rows``. PostgreSQL twin:
``tests/postgres/test_find_sources_store_pg.py``; shared cases:
``tests/find_sources_store_cases.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.source_store import SourceStore
from tests import find_sources_store_cases as cases


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    return cases.Env(
        store=SourceStore(database, now=lambda: cases.NOW),
        run=run,
        connect=database.connect,
    )


@pytest.mark.parametrize("case", cases.ALL_CASES, ids=lambda case: case.__name__)
def test_find_sources_metadata_rows(env, case):
    case(env)
