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
    MIGRATE_MODULE_COMMAND,
    inspect_schema_currency,
    packaged_postgres_migrations,
)
from tests.cli_schema_guest_support import (
    CASES,
    ROOT,
    CliContext,
    Opened,
    guest_seam_spy,
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
COMPOSING = tuple(case for case in PORTABLE if case.composes)
SQLITE_ONLY = tuple(case for case in CASES if case.sqlite_only)


@pytest.mark.parametrize("case", COMPOSING, ids=lambda case: case.name)
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
    assert MIGRATE_MODULE_COMMAND in text
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


def test_database_migrate_env_file_wins_over_the_shell(
    postgres_scope, behind_pg, tmp_path, monkeypatch
):
    """With --env-file the file names the database; the exported DATABASE_URL
    (a second, also-short ledger) is ignored and left exactly as it was."""
    from tests.postgres.conftest import _isolated_postgres_scope

    with _isolated_postgres_scope(postgres_scope.base_url) as other:
        database = PostgresDatabase(Settings(database_url=other.url), ROOT)
        try:
            PostgresMigrator(database).migrate(target_version=REQUIRED - 1)
        finally:
            database.close()
        env_file = tmp_path / "deployment.env"
        env_file.write_text(
            f"DATABASE_URL={behind_pg}\n"
            f"SILICON_NOTEBOOK_STORAGE_DIR={tmp_path / 'storage'}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("DATABASE_URL", other.url)
        other_schema = _schema(other.url)

        assert _migrate(["--env-file", str(env_file), *CONFIRM]) == 0

        assert inspect_schema_currency(Settings(database_url=behind_pg)).is_current
        assert _schema(other.url) == other_schema


def test_the_maintenance_opener_rechecks_the_ledger_under_the_lock(guest_pg, monkeypatch):
    """A newer checkout's migration landing between the composition gate and
    the maintenance lock is seen under the lock; the body never runs."""
    from app.services import maintenance_cli

    real = maintenance_cli.create_repository

    def compose_then_race(settings):
        repository = real(settings)
        with psycopg.connect(guest_pg, autocommit=True) as connection:
            connection.execute(
                "INSERT INTO silicon_schema_migrations(version, checksum) VALUES (%s, %s)",
                (REQUIRED + 1, "from-a-newer-checkout"),
            )
        return repository

    monkeypatch.setattr(maintenance_cli, "create_repository", compose_then_race)
    entered = []
    with pytest.raises(maintenance_cli.MaintenanceCliSchemaError) as caught:
        with maintenance_cli.open_maintenance_cli_repository(
            Settings(), confirm_service_stopped=True
        ):
            entered.append(True)
    assert entered == []
    assert f"PostgreSQL {REQUIRED + 1:04d}" in str(caught.value)


def test_a_ledger_read_that_times_out_gets_the_query_wording(
    guest_pg, tmp_path, monkeypatch, capfd
):
    """Connected, but the ledger is held under an exclusive lock: the read hits
    the configured statement timeout and the operator sees the second fixed
    wording -- bounded in time, no connection details, no server text."""
    import time
    from urllib.parse import urlsplit

    monkeypatch.setenv("POSTGRES_STATEMENT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS", "0.5")  # must not exceed it
    get_settings.cache_clear()
    case = next(case for case in CASES if case.name == "batch_ingest embed")
    with psycopg.connect(guest_pg) as holder:
        holder.execute("LOCK TABLE silicon_schema_migrations IN ACCESS EXCLUSIVE MODE")
        started = time.monotonic()
        code = run_case(case, _ctx(tmp_path), monkeypatch)
        elapsed = time.monotonic() - started
        holder.rollback()

    output = capfd.readouterr()
    text = output.out + output.err
    assert code == 2, text
    assert "已连上 PostgreSQL，但读取 schema 迁移账本失败" in text
    assert elapsed < 15
    parts = urlsplit(guest_pg)
    for token in (str(parts.hostname), parts.path.lstrip("/"), "canceling statement"):
        assert token not in text, f"{token!r} leaked: {text}"


@pytest.mark.parametrize(
    "probe",
    ["database migrate", "batch_ingest embed", "build_chunks", "scale ledger preflight"],
)
def test_wrong_credentials_never_reach_the_terminal(postgres_scope, tmp_path, monkeypatch, capfd, probe):
    """A role that does not exist makes the server's error name it; the
    operator sees the fixed wording only -- no user, password, host or db."""
    from urllib.parse import urlsplit, urlunsplit

    from app.services import scale_build_cli

    parts = urlsplit(postgres_scope.url)
    netloc = f"cli_guest_no_such_role:not-the-pw@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    _env(monkeypatch, tmp_path, url, SHELL_PASSWORD)

    if probe == "database migrate":
        code = _migrate(list(CONFIRM))
    elif probe == "scale ledger preflight":
        with pytest.raises(scale_build_cli.ScaleBuildCliError) as caught:
            scale_build_cli.verify_migration_ledger(url)
        print(str(caught.value))
        code = 2
    else:
        case = next(case for case in CASES if case.name == probe)
        code = run_case(case, _ctx(tmp_path), monkeypatch)

    output = capfd.readouterr()
    text = output.out + output.err
    assert code == 2, text
    assert "无法连接 PostgreSQL 数据库来检查 schema 版本" in text
    database = parts.path.lstrip("/")
    for token in ("cli_guest_no_such_role", "not-the-pw", str(parts.hostname), database):
        assert token not in text, f"{token!r} leaked: {text}"
