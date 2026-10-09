"""SQLite ``ask_intent_handles`` store (v90). PostgreSQL twin:
``tests/postgres/test_ask_intent_handle_store_pg.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.ask_intent_handle_store import AskIntentHandleStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests import ask_intent_handle_store_cases as cases


@pytest.fixture
def database(tmp_path: Path) -> SqliteDatabase:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    db = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(db, settings).migrate()
    return db


def test_round_trip_is_owner_bound_and_non_consuming(database):
    cases.assert_round_trip_is_owner_bound_and_non_consuming(
        AskIntentHandleStore(database, now=cases.Clock())
    )


def test_expiry_hides_then_purges(database):
    clock = cases.Clock()

    def count_rows() -> int:
        with database.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM ask_intent_handles").fetchone()[0])

    cases.assert_expiry_hides_then_purges(
        AskIntentHandleStore(database, now=clock), clock, count_rows
    )
