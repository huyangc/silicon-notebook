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

import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite import migrations as migrations_module
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


def _v77_database(tmp_path, monkeypatch):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'auth-v77.db'}",
        storage_dir=str(tmp_path / "storage"),
        _env_file=None,
        event_log_enabled=False,
        llm_log_enabled=False,
    )
    database = SqliteDatabase(settings, tmp_path)
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 77)
    assert SqliteMigrator(database, settings).migrate()
    with database.write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,password_hash,"
            "password_salt,password_iterations,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "user-existing",
                "existing@example.invalid",
                "Existing User",
                "user",
                "active",
                "legacy-login",
                "preserved-hash",
                "preserved-salt",
                12345,
                "before",
                "before",
            ),
        )
        db.execute(
            "INSERT INTO auth_sessions "
            "(token,user_id,created_at,expires_at,last_seen_at) VALUES (?,?,?,?,?)",
            ("preserved-session", "user-existing", "before", "later", "before"),
        )
    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 78)
    return settings, database


def test_v78_recovers_committed_schema_with_v77_stamp_without_data_loss(
    tmp_path, monkeypatch
):
    settings, database = _v77_database(tmp_path, monkeypatch)
    migrator = SqliteMigrator(database, settings)
    assert migrator.migrate() == [78]

    # Reproduce the old failure window exactly: all v78 DDL committed, but the
    # separate version-stamp transaction did not.  The alias represents a
    # value already chosen after backfill and must never be overwritten.
    with database.write() as db:
        db.execute(
            "UPDATE users SET local_login_name='preserved-alias' "
            "WHERE id='user-existing'"
        )
        db.execute("PRAGMA user_version = 77")
    with database.connect() as db:
        user_before = dict(
            db.execute("SELECT * FROM users WHERE id='user-existing'").fetchone()
        )
        session_before = dict(
            db.execute(
                "SELECT * FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    database.close_local()

    reopened = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(reopened, settings).migrate() == [78]
    with reopened.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        user_after = dict(
            db.execute("SELECT * FROM users WHERE id='user-existing'").fetchone()
        )
        session_after = dict(
            db.execute(
                "SELECT * FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    assert user_after == user_before
    assert user_after["local_login_name"] == "preserved-alias"
    assert session_after == session_before
    reopened.close_local()


def test_v78_schema_and_version_stamp_roll_back_together_on_interruption(
    tmp_path, monkeypatch
):
    settings, database = _v77_database(tmp_path, monkeypatch)
    original_new_connection = database._new_connection
    interrupt = {"armed": True}

    def interrupted_connection(mode="read", operation="sqlite.read"):
        connection = original_new_connection(mode, operation)
        if connection._diag_mode == "write" and interrupt["armed"]:

            def deny_auth_policy(action, name, _column, _database, _source):
                if action == sqlite3.SQLITE_CREATE_TABLE and name == "auth_policy":
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            connection.set_authorizer(deny_auth_policy)
        return connection

    monkeypatch.setattr(database, "_new_connection", interrupted_connection)
    migrator = SqliteMigrator(database, settings)
    with pytest.raises(sqlite3.DatabaseError):
        migrator.migrate()

    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 77
        user_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
        }
        session_columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(auth_sessions)").fetchall()
        }
        assert "local_login_name" not in user_columns
        assert "auth_source" not in session_columns
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_policy'"
        ).fetchone() is None
        password = tuple(
            db.execute(
                "SELECT password_hash,password_salt,password_iterations "
                "FROM users WHERE id='user-existing'"
            ).fetchone()
        )
        session = tuple(
            db.execute(
                "SELECT token,user_id,created_at,expires_at,last_seen_at "
                "FROM auth_sessions WHERE token='preserved-session'"
            ).fetchone()
        )
    assert password == ("preserved-hash", "preserved-salt", 12345)
    assert session == (
        "preserved-session",
        "user-existing",
        "before",
        "later",
        "before",
    )

    interrupt["armed"] = False
    assert migrator.migrate() == [78]
    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 78
        assert db.execute(
            "SELECT local_login_name FROM users WHERE id='user-existing'"
        ).fetchone()[0] == "legacy-login"
    database.close_local()
