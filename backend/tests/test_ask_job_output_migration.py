"""Ask job output kind, SQLite side (``_migration_90``; PostgreSQL twin
``postgres/test_migrations.py`` pins ``0070_ask_job_output.sql``).

Pinned here: the column lands on ask_jobs and retained_user_activity as
``TEXT NOT NULL DEFAULT 'answer'``; a v89 database with existing rows keeps
every row as an answer (the default is the whole backfill); the migration is
re-runnable; the PostgreSQL file spells the same two columns.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.services.sqlite_repository import SQLiteRepository

PG_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0070_ask_job_output.sql"
)
NOW = "2026-10-09T00:00:00+00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'o.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def _output_column(db, table):
    return {
        row[1]: (row[2], row[3], row[4]) for row in db.execute(f"PRAGMA table_info({table})")
    }.get("output")


def test_fresh_database_has_the_output_column_on_both_tables(repo):
    assert SCHEMA_VERSION >= 90
    with repo._runtime.database.connect() as db:
        for table in ("ask_jobs", "retained_user_activity"):
            assert _output_column(db, table) == ("TEXT", 1, "'answer'")


def test_v89_rows_stay_answers_and_rerun_changes_nothing(repo):
    database = repo._runtime.database
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
            "VALUES('u1','u1@x','u1','user','active',?,?)", (NOW, NOW))
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES('nb1','nb1','u1','ready',?,?)", (NOW, NOW))
        db.execute("ALTER TABLE ask_jobs DROP COLUMN output")
        db.execute("ALTER TABLE retained_user_activity DROP COLUMN output")
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,created_at,updated_at) VALUES('j1','nb1','','u1','chunk','q','done',?,?)",
            (NOW, NOW))
        db.execute("PRAGMA user_version = 89")

    assert SqliteMigrator(database, repo.settings).migrate() == [90]
    with database.connect() as db:
        assert db.execute("SELECT output FROM ask_jobs WHERE id='j1'").fetchone()[0] == "answer"
        assert _output_column(db, "retained_user_activity") == ("TEXT", 1, "'answer'")
    SqliteMigrator(database, repo.settings)._migration_90()
    with database.connect() as db:
        assert db.execute("SELECT output FROM ask_jobs WHERE id='j1'").fetchone()[0] == "answer"


def test_postgres_migration_spells_the_same_two_columns():
    sql = PG_MIGRATION.read_text(encoding="utf-8")
    assert "ALTER TABLE ask_jobs ADD COLUMN IF NOT EXISTS output text" in sql
    assert "ALTER TABLE retained_user_activity ADD COLUMN IF NOT EXISTS output text" in sql
    statements = [
        line for line in sql.splitlines() if line.startswith("ALTER TABLE")
    ]
    assert len(statements) == 2
    assert all(line.endswith("NOT NULL DEFAULT 'answer';") for line in statements)
