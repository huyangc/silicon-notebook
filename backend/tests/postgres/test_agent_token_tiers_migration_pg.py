"""Agent-token-tiers migration ``0069_agent_token_tiers.sql`` on real
PostgreSQL (SQLite twin: ``tests/test_agent_token_tiers_migration.py``; both
seed ``agent_token_tiers_migration_cases.py`` and assert the same rows, which
is the cross-backend equality).

Pinned: the upgrade; idempotent re-execution of the frozen SQL (a second run
updates no row); ``token_plain`` is a nullable "C"-collated text column.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from tests import agent_token_tiers_migration_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_agent_token_tiers_migration"),
]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0069_agent_token_tiers.sql"
)


def _seed_at_68(database) -> None:
    assert PostgresMigrator(database).migrate(target_version=68) == 68
    with database.write() as db:
        cases.seed(db, postgres=True)


def _snapshot(database) -> dict:
    with database.connect() as db:
        return cases.snapshot(db)


def test_pg_upgrade_rewrites_every_token_to_tiers(postgres_database):
    _seed_at_68(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 69
    cases.assert_migrated(_snapshot(postgres_database))


def test_pg_reexecuting_the_frozen_sql_changes_nothing(postgres_database):
    _seed_at_68(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 69
    after = _snapshot(postgres_database)
    with postgres_database.write() as db:
        body = MIGRATION.read_text(encoding="utf-8")
        update = body[body.index("UPDATE agent_access_tokens"):body.index("DO $tt$")]
        assert db.execute(update, prepare=False).rowcount == 0
        db.execute(body, prepare=False)
    assert _snapshot(postgres_database) == after


def test_pg_plaintext_column_is_nullable_c_collated_text(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 69
    with postgres_database.connect() as db:
        column = db.execute(
            "SELECT data_type, is_nullable, column_default, collation_name "
            "FROM information_schema.columns WHERE table_name = 'agent_access_tokens' "
            "AND column_name = 'token_plain'"
        ).fetchone()
    assert (column["data_type"], column["is_nullable"], column["column_default"],
            column["collation_name"]) == ("text", "YES", None, "C")


def test_pg_operator_check_counts_the_live_tokens_left_without_a_tier(
    postgres_database,
):
    """The check query docs/operations.md gives operators (and the count the
    migration's RAISE LOG summary reports) finds exactly the live tokens that
    ended with no tier -- the SQLite twin logs the same number."""
    _seed_at_68(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 69
    with postgres_database.connect() as db:
        count = db.execute(
            "SELECT count(*) AS n FROM agent_access_tokens "
            "WHERE scopes_json = '[]'::jsonb AND revoked_at IS NULL"
        ).fetchone()["n"]
    assert count == cases.EMPTIED_LIVE
