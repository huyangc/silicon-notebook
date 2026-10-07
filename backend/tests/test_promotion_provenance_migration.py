"""PR-E8 promotion-provenance migration, SQLite side (``_migration_88``;
PostgreSQL twin ``postgres/test_promotion_provenance_migration_pg.py`` seeds the
same world from ``promotion_provenance_migration_cases.py`` and asserts the same
rows -- the cross-backend equality the plan asks for).

Pinned here:

* upgrade: a v87 database's public-library entries naming another notebook's
  source come out pointing at the library's own promotion sources and
  elements (live element text, else the stored quote, else dropped; a Memory
  element never read); the reverse index follows; personal notebooks and own
  entries are untouched;
* the literal expectation equals what the runtime planner computes;
* idempotent: re-running changes nothing;
* fresh database: user_version 88; a v86 database migrates [87, 88];
* the PostgreSQL file names the same rule (prefixes, separator, bound).
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain import promotion_provenance
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.services.sqlite_repository import SQLiteRepository
from tests import promotion_provenance_migration_cases as cases

PG_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0068_promotion_provenance.sql"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pp.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def _snapshot(database) -> dict:
    with database.connect() as db:
        return cases.snapshot(db)


def _seed_at_v87(database) -> None:
    with database.write() as db:
        cases.seed(db, postgres=False)
        db.execute("PRAGMA user_version = 87")


def test_upgrade_rewrites_foreign_entries_to_the_librarys_own_provenance(repo):
    database = repo._runtime.database
    _seed_at_v87(database)
    assert SqliteMigrator(database, repo.settings).migrate() == [88]
    cases.assert_migrated(_snapshot(database))


def test_the_expectation_is_the_runtime_planners(repo):
    assert cases.expected_from_planner() == cases.EXPECTED_EVIDENCE


def test_rerun_changes_nothing(repo):
    database = repo._runtime.database
    _seed_at_v87(database)
    migrator = SqliteMigrator(database, repo.settings)
    migrator.migrate()
    after = _snapshot(database)
    migrator._migration_88()
    assert _snapshot(database) == after


def test_migration_logs_counts_only(repo, caplog):
    database = repo._runtime.database
    _seed_at_v87(database)
    with caplog.at_level(logging.INFO, logger="silicon_notebook.sqlite.maintenance"):
        SqliteMigrator(database, repo.settings).migrate()
    lines = [r.getMessage() for r in caplog.records
             if "promotion-provenance migration" in r.getMessage()]
    assert lines == [
        "promotion-provenance migration: libraries=2 objects_rewritten=6 "
        "entries_rewritten=5 entries_dropped=1 objects_without_evidence=1"
    ]


def test_fresh_database_is_at_v88_and_v86_migrates_both(repo):
    database = repo._runtime.database
    assert SCHEMA_VERSION == 88
    with database.connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 88
    with database.write() as db:
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN memory_isolation_version")
        db.execute("PRAGMA user_version = 86")
    assert SqliteMigrator(database, repo.settings).migrate() == [87, 88]


def test_the_postgresql_file_spells_the_same_rule():
    """The ids, titles and excerpt bound are written into 0068 as literals; they
    must be the domain module's."""
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    assert "'src-promo-' || md5(f.base_id || '|' || f.origin_source_id)" in sql
    assert ("'el-promo-' || md5(x.promo_source_id || '|' || x.origin_element_id "
            "|| '|' || x.body)") in sql
    assert f"'{promotion_provenance.PROMOTION_TITLE_PREFIX}' || d.origin_title" in sql
    assert f"left(r.body, {promotion_provenance.EXCERPT_CHARS})" in sql
    assert f"ELSE '{promotion_provenance.DEFAULT_ELEMENT_TYPE}' END" in sql
    assert f"'{promotion_provenance.PROMOTION_SOURCE_TYPE}'," in sql
    assert promotion_provenance.promotion_source_id("b", "s").startswith("src-promo-")
    assert promotion_provenance._KEY_SEPARATOR == "|"
