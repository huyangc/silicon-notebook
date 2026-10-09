"""PostgreSQL twin of ``tests/test_ask_intent_handle_store.py`` (0070)."""
from __future__ import annotations

import pytest

from app.repositories.postgres.ask_intent_handle_store import AskIntentHandleStore
from tests import ask_intent_handle_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 71
    return database


def test_round_trip_is_owner_bound_and_non_consuming(database):
    cases.assert_round_trip_is_owner_bound_and_non_consuming(
        AskIntentHandleStore(database, now=cases.Clock())
    )


def test_expiry_hides_then_purges(database):
    clock = cases.Clock()

    def count_rows() -> int:
        with database.connect() as db:
            return int(db.execute(
                "SELECT COUNT(*) AS n FROM ask_intent_handles"
            ).fetchone()["n"])

    cases.assert_expiry_hides_then_purges(
        AskIntentHandleStore(database, now=clock), clock, count_rows
    )
