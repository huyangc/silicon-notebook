"""Non-serving CLIs are schema guests: PostgreSQL twin of
``tests/test_cli_schema_guest.py``.

PostgreSQL is where the original damage happened: ``bundle._initialize``
re-hashes ``settings.admin_password`` on every seeding composition, and a
migrating composition applies DDL to a database a service may be serving. The
same CLI table runs here against a real database:

* each CLI composes through the shared guest seam and leaves the admin row, the
  migration ledger and the catalog untouched;
* each refuses a ledger one version short and a database with no ledger (exit
  3, shared wording, no DDL: the ledger table is not even created);
* the tools that always named ``SQLiteRepository`` keep refusing PostgreSQL
  before touching it;
* ``database migrate`` migrates behind -> current under the offline
  maintenance lock, is idempotent, never seeds, needs the confirmation and
  reports a held maintenance lock instead of waiting on it.
"""
from __future__ import annotations

import psycopg
import pytest

from app.core.config import Settings, get_settings
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.maintenance import _OFFLINE_MAINTENANCE_LOCK_KEY
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.repository import PostgresRepository
from app.repositories.schema_currency import (
    MIGRATE_COMMAND,
    inspect_schema_currency,
    packaged_postgres_migrations,
)
from tests.cli_schema_guest_support import (
    CASES,
    CliContext,
    Opened,
    ROOT,
    guest_seam_spy,
    load_script,
    run_case,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_offline_maintenance"),
]

OWNER_PASSWORD = "owner-secret-at-first-start"
SHELL_PASSWORD = "a-different-operator-shell-secret"
REQUIRED = len(packaged_postgres_migrations())
CONFIRM = ("--confirm-service-stopped",)


def _env(monkeypatch, tmp_path, url: str, password: str) -> None:
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", password)
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "2")
    get_settings.cache_clear()


@pytest.fixture
def guest_pg(postgres_scope, tmp_path, monkeypatch) -> str:
    """A database the service created (migrate + seed with OWNER_PASSWORD);
    afterwards the shell's password differs, so any seed would show."""
    _env(monkeypatch, tmp_path, postgres_scope.url, OWNER_PASSWORD)
    PostgresRepository(Settings()).close()
    _env(monkeypatch, tmp_path, postgres_scope.url, SHELL_PASSWORD)
    yield postgres_scope.url
    get_settings.cache_clear()


@pytest.fixture
def behind_pg(postgres_scope, tmp_path, monkeypatch) -> str:
    """A ledger one version short of this build, with the built-in admin row
    the service would have seeded at that version."""
    _env(monkeypatch, tmp_path, postgres_scope.url, SHELL_PASSWORD)
    database = PostgresDatabase(Settings(), ROOT)
    try:
        PostgresMigrator(database).migrate(target_version=REQUIRED - 1)
    finally:
        database.close()
    with psycopg.connect(postgres_scope.url, autocommit=True) as connection:
        connection.execute(
            "INSERT INTO users (id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) VALUES "
            "('user-local','local@example.com','Local','admin','active',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',"
            "'admin','owner-hash','owner-salt',1)"
        )
    yield postgres_scope.url
    get_settings.cache_clear()


def _admin(url: str) -> tuple:
    with psycopg.connect(url, autocommit=True) as connection:
        return connection.execute(
            "SELECT password_hash, password_salt, password_iterations, updated_at "
            "FROM users WHERE id='user-local'"
        ).fetchone()


def _schema(url: str) -> tuple:
    """Ledger rows plus a digest of every relation and column in the scope."""
    with psycopg.connect(url, autocommit=True) as connection:
        present = connection.execute(
            "SELECT to_regclass('silicon_schema_migrations') IS NOT NULL"
        ).fetchone()[0]
        ledger = (
            tuple(connection.execute(
                "SELECT version, checksum FROM silicon_schema_migrations ORDER BY version"
            ).fetchall())
            if present
            else None
        )
        relations = connection.execute(
            "SELECT count(*), md5(coalesce(string_agg(c.relname::text || ':' || c.relkind::text, ',' "
            "ORDER BY c.relname), '')) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
        ).fetchone()
        columns = connection.execute(
            "SELECT md5(coalesce(string_agg(table_name || '.' || column_name || ':' || "
            "data_type, ',' ORDER BY table_name, column_name), '')) "
            "FROM information_schema.columns WHERE table_schema = current_schema()"
        ).fetchone()
    return ledger, relations, columns


def _ctx(tmp_path) -> CliContext:
    return CliContext(tmp=tmp_path, confirm=CONFIRM)


PORTABLE = tuple(case for case in CASES if not case.sqlite_only)
SQLITE_ONLY = tuple(case for case in CASES if case.sqlite_only)


@pytest.mark.parametrize("case", PORTABLE, ids=lambda case: case.name)
def test_every_cli_composes_as_a_guest_and_leaves_admin_and_schema_untouched(
    guest_pg, tmp_path, monkeypatch, case
):
    admin, schema = _admin(guest_pg), _schema(guest_pg)
    with guest_seam_spy(monkeypatch) as spy:
        with pytest.raises(Opened):
            run_case(case, _ctx(tmp_path), monkeypatch, spy)
        assert spy["reached"], "the CLI must compose through a shared guest seam"
    assert _admin(guest_pg) == admin, "a schema guest must never re-seed the admin row"
    assert _schema(guest_pg) == schema, "a schema guest must never run migration DDL"


@pytest.mark.parametrize("case", PORTABLE, ids=lambda case: case.name)
def test_every_cli_refuses_a_ledger_one_version_short(
    behind_pg, tmp_path, monkeypatch, capsys, case
):
    admin, schema = _admin(behind_pg), _schema(behind_pg)

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 3

    output = capsys.readouterr()
    text = output.out + output.err
    assert f"PostgreSQL {REQUIRED - 1:04d}" in text
    assert MIGRATE_COMMAND in text
    assert _schema(behind_pg) == schema
    assert _admin(behind_pg) == admin


@pytest.mark.parametrize("case", PORTABLE, ids=lambda case: case.name)
def test_every_cli_refuses_a_database_with_no_ledger_without_creating_one(
    postgres_scope, tmp_path, monkeypatch, case
):
    _env(monkeypatch, tmp_path, postgres_scope.url, SHELL_PASSWORD)

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 3
    assert _schema(postgres_scope.url)[0] is None, "no ledger table may be created"


@pytest.mark.parametrize("case", SQLITE_ONLY, ids=lambda case: case.name)
def test_sqlite_only_tools_keep_refusing_postgresql_untouched(
    guest_pg, tmp_path, monkeypatch, case
):
    admin, schema = _admin(guest_pg), _schema(guest_pg)

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 2
    assert _admin(guest_pg) == admin
    assert _schema(guest_pg) == schema


# ─────────────────────────────────────────────── database migrate ──


def _migrate(argv):
    from app.scripts import migrate_database

    get_settings.cache_clear()
    return migrate_database.main(argv)


def test_database_migrate_brings_a_short_ledger_current_without_seeding(
    behind_pg, capsys
):
    admin = _admin(behind_pg)

    assert _migrate(list(CONFIRM)) == 0

    assert inspect_schema_currency(Settings()).is_current
    assert _admin(behind_pg) == admin, "the migrate command must never seed"
    assert f"PostgreSQL {REQUIRED:04d}" in capsys.readouterr().out


def test_database_migrate_is_idempotent(guest_pg, capsys):
    schema, admin = _schema(guest_pg), _admin(guest_pg)

    assert _migrate(list(CONFIRM)) == 0
    assert _migrate(list(CONFIRM)) == 0

    assert _schema(guest_pg) == schema
    assert _admin(guest_pg) == admin
    assert "无需迁移" in capsys.readouterr().out


def test_database_migrate_refuses_without_the_stopped_service_confirmation(
    behind_pg, capsys
):
    schema = _schema(behind_pg)

    assert _migrate([]) == 2

    assert _schema(behind_pg) == schema
    assert "--confirm-service-stopped" in capsys.readouterr().err


def test_database_migrate_refuses_to_initialize_an_empty_database(
    postgres_scope, tmp_path, monkeypatch, capsys
):
    _env(monkeypatch, tmp_path, postgres_scope.url, SHELL_PASSWORD)

    assert _migrate(list(CONFIRM)) == 3

    assert _schema(postgres_scope.url)[0] is None
    assert "启动一次服务" in capsys.readouterr().err


def test_database_migrate_reports_a_held_maintenance_lock_instead_of_waiting(
    behind_pg, capsys
):
    schema = _schema(behind_pg)
    with psycopg.connect(behind_pg, autocommit=True) as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (_OFFLINE_MAINTENANCE_LOCK_KEY,))
        try:
            assert _migrate(list(CONFIRM)) == 2
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (_OFFLINE_MAINTENANCE_LOCK_KEY,))

    assert _schema(behind_pg) == schema
    assert "already running" in capsys.readouterr().err
