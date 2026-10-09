"""Agent-token-tiers migration, SQLite side (``_migration_89``; PostgreSQL twin
``postgres/test_agent_token_tiers_migration_pg.py`` seeds the same world from
``agent_token_tiers_migration_cases.py`` and asserts the same rows).

Pinned here: the upgrade (tiers from main capabilities only, revoked rows
included, empty tiers for secondary-only tokens, non-array / non-string values
grant nothing, canonical order); ``token_plain`` added and NULL on old rows;
idempotent re-run; fresh database at v89 and a v88 database migrating [89];
the frozen rule agrees with the live capability table; the PostgreSQL file
spells the same grants.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain import agent_tools
from app.repositories.sqlite import migrations as sqlite_migrations
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.services.sqlite_repository import SQLiteRepository
from tests import agent_token_tiers_migration_cases as cases

PG_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0069_agent_token_tiers.sql"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tt.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def _seed_at_v88(database) -> None:
    with database.write() as db:
        db.execute("ALTER TABLE agent_access_tokens DROP COLUMN token_plain")
        cases.seed(db, postgres=False)
        db.execute("PRAGMA user_version = 88")


def _snapshot(database) -> dict:
    with database.connect() as db:
        return cases.snapshot(db)


def test_upgrade_rewrites_every_token_to_tiers(repo):
    database = repo._runtime.database
    _seed_at_v88(database)
    assert SqliteMigrator(database, repo.settings).migrate() == [89, 90]
    cases.assert_migrated(_snapshot(database))


def test_rerun_changes_nothing(repo):
    database = repo._runtime.database
    _seed_at_v88(database)
    migrator = SqliteMigrator(database, repo.settings)
    migrator.migrate()
    after = _snapshot(database)
    migrator._migration_89()
    assert _snapshot(database) == after


def test_migration_logs_counts_only(repo, caplog):
    database = repo._runtime.database
    _seed_at_v88(database)
    with caplog.at_level(logging.INFO, logger="silicon_notebook.sqlite.maintenance"):
        SqliteMigrator(database, repo.settings).migrate()
    lines = [r.getMessage() for r in caplog.records
             if "agent-token-tiers migration" in r.getMessage()]
    # every seeded row changes (tk-empty's "[]" is already canonical);
    # emptied counts the live rows that end with no tier
    assert lines == [
        f"agent-token-tiers migration: tokens={len(cases.TOKENS)} "
        f"rewritten={len(cases.TOKENS) - 1} emptied={cases.EMPTIED_LIVE}"
    ]


def test_fresh_database_is_at_v89_with_a_nullable_plaintext_column(repo):
    database = repo._runtime.database
    assert SCHEMA_VERSION >= 89
    with database.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        column = {
            row[1]: (row[2], row[3], row[4])
            for row in db.execute("PRAGMA table_info(agent_access_tokens)")
        }["token_plain"]
    assert column == ("TEXT", 0, None)


def test_the_frozen_rule_matches_the_live_tier_table():
    """v89 runs a frozen copy; while it is the rule this migration means, its
    tiers are the live tiers in display order and every main capability it
    names grants that same tier in the live table."""
    frozen = sqlite_migrations._V89_TIER_RULES
    assert tuple(tier for tier, _ in frozen) == agent_tools.AGENT_TIERS
    for tier, granting in frozen:
        assert granting[0] == tier
        for capability in granting[1:]:
            assert agent_tools.AGENT_CAPABILITY_TIER[capability] == tier
    for scopes_json, _revoked, tiers in cases.TOKENS.values():
        assert sqlite_migrations._v89_tiers(scopes_json) == tiers
    assert sqlite_migrations._v89_tiers(None) == []
    assert sqlite_migrations._v89_tiers("not json") == []


def test_the_postgresql_file_spells_the_same_grants():
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    rows = re.findall(r"\((\d), '(\w+)', ARRAY\[([^\]]*)\]\)", sql)
    parsed = tuple(
        (tier, tuple(item.strip().strip("'") for item in granting.split(",")))
        for _ord, tier, granting in rows
    )
    assert parsed == sqlite_migrations._V89_TIER_RULES
    assert [int(ordinal) for ordinal, _, _ in rows] == [1, 2, 3, 4, 5]
    assert 'ADD COLUMN IF NOT EXISTS token_plain text COLLATE "C"' in sql
