# backend/tests/test_sqlite_migrations.py
"""SQLite migration-ledger contracts that don't belong to any single
`_migration_N` step: the upper-bound guard on `SqliteMigrator.migrate()`
(A7) and the equality short-circuit it must not break.

PostgreSQL already fails closed on a future ledger version
(`PostgresMigrator._validate_ledger`, see `tests/postgres/test_migrations.py::
test_unknown_future_ledger_version_fails_closed`). SQLite's `migrate()` used to
tolerate `current > SCHEMA_VERSION` silently (`if current >= SCHEMA_VERSION:
return []`), which meant a database written by a newer build, opened by an
older/rolled-back build, would silently run against a schema it doesn't
understand instead of failing fast at startup. This module pins the SQLite
side of that guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


def _fresh_migrated_database(tmp_path: Path) -> tuple[SqliteDatabase, Settings]:
    db_path = tmp_path / "migrations.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, root_dir=tmp_path)
    SqliteMigrator(database, settings).migrate()
    return database, settings


def _set_user_version(database: SqliteDatabase, version: int) -> None:
    with database.connect() as db:
        db.execute(f"PRAGMA user_version = {int(version)}")


def test_migrate_returns_empty_list_when_database_is_exactly_current(tmp_path):
    """Equality must keep short-circuiting: no migrations re-run, no raise."""
    database, settings = _fresh_migrated_database(tmp_path)
    try:
        with database.connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert SqliteMigrator(database, settings).migrate() == []
    finally:
        database.close_local()


def test_migrate_raises_when_database_is_stamped_newer_than_schema_version(
    tmp_path,
):
    """A7: a database from a newer build must be refused at startup, not
    silently tolerated. The error names both the database's stamped version
    and the running build's SCHEMA_VERSION so an operator can act on it."""
    database, settings = _fresh_migrated_database(tmp_path)
    try:
        future_version = SCHEMA_VERSION + 1
        _set_user_version(database, future_version)

        with pytest.raises(RuntimeError) as excinfo:
            SqliteMigrator(database, settings).migrate()

        message = str(excinfo.value)
        assert str(future_version) in message
        assert str(SCHEMA_VERSION) in message
    finally:
        database.close_local()

