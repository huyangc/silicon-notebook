"""PostgreSQL ``SourceStore.find_sources_metadata_rows`` (the canonical
definition). SQLite twin: ``tests/test_find_sources_store.py``; shared cases:
``tests/find_sources_store_cases.py``."""
from __future__ import annotations

import pytest

from app.repositories.postgres.source_store import SourceStore
from tests import find_sources_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def env(request) -> cases.Env:
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    PostgresMigrator(database).migrate()

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    return cases.Env(
        store=SourceStore(database, now=lambda: cases.NOW),
        run=run,
        connect=database.connect,
    )


@pytest.mark.parametrize("case", cases.ALL_CASES, ids=lambda case: case.__name__)
def test_find_sources_metadata_rows(env, case):
    case(env)
