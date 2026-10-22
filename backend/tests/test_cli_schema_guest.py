"""Non-serving CLIs are schema guests: SQLite half (PG twin:
``tests/postgres/test_cli_schema_guest_pg.py``).

* every CLI composes through the shared guest seam, and that composition leaves
  the admin credential and the schema byte-for-byte as the service left them --
  even though the operator shell's ``SILICON_NOTEBOOK_ADMIN_PASSWORD`` differs
  (a seed would re-hash it);
* every CLI refuses a database one version behind, a missing database (without
  creating the file) and a newer database, with exit status 3 and the shared
  reason-plus-remedy wording, running no DDL;
* ``database migrate`` is the explicit owner: migrates behind -> current, is
  idempotent, never touches the admin row, refuses without
  ``--confirm-service-stopped`` and refuses what migration cannot fix.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.repositories.schema_currency import (
    MIGRATE_COMMAND,
    MIGRATE_MODULE_COMMAND,
    SchemaCurrency,
    inspect_schema_currency,
    schema_refusal_message,
)
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services.sqlite_repository import SQLiteRepository
from tests.cli_schema_guest_support import (
    CASES,
    CliContext,
    Opened,
    guest_seam_spy,
    load_script,
    run_case,
)

OWNER_PASSWORD = "owner-secret-at-first-start"
SHELL_PASSWORD = "a-different-operator-shell-secret"


@pytest.fixture
def guest_db(tmp_path, monkeypatch) -> Path:
    """A database the service created (migrate + seed with OWNER_PASSWORD);
    afterwards the shell's password differs, so any seed would show."""
    db = tmp_path / "guest.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", OWNER_PASSWORD)
    get_settings.cache_clear()
    SQLiteRepository(Settings()).close()
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", SHELL_PASSWORD)
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def _admin(db: Path) -> tuple:
    with sqlite3.connect(db) as connection:
        return connection.execute(
            "SELECT password_hash, password_salt, password_iterations, updated_at "
            "FROM users WHERE id='user-local'"
        ).fetchone()


def _schema(db: Path) -> tuple:
    with sqlite3.connect(db) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
    return version, tuple(objects)


def _stamp(db: Path, version: int) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute(f"PRAGMA user_version = {int(version)}")


def _ctx(tmp_path) -> CliContext:
    return CliContext(tmp=tmp_path, confirm=())


COMPOSING = tuple(case for case in CASES if case.composes)


def _side_files(db: Path) -> set[str]:
    return {p.name for p in db.parent.iterdir() if p.name.startswith(db.name)}


@pytest.mark.parametrize("case", COMPOSING, ids=lambda case: case.name)
def test_every_cli_composes_as_a_guest_and_leaves_admin_and_schema_untouched(
    guest_db, tmp_path, monkeypatch, case
):
    admin, schema = _admin(guest_db), _schema(guest_db)
    with guest_seam_spy(monkeypatch) as spy:
        with pytest.raises(Opened):
            run_case(case, _ctx(tmp_path), monkeypatch, spy)
        assert spy["reached"], "the CLI must compose through a shared guest seam"
    assert _admin(guest_db) == admin, "a schema guest must never re-seed the admin row"
    assert _schema(guest_db) == schema, "a schema guest must never run migration DDL"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_every_cli_refuses_a_database_one_version_behind(
    guest_db, tmp_path, monkeypatch, capsys, case
):
    _stamp(guest_db, SCHEMA_VERSION - 1)
    admin, schema = _admin(guest_db), _schema(guest_db)
    files = _side_files(guest_db)

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 3

    output = capsys.readouterr()
    text = output.out + output.err
    assert f"SQLite v{SCHEMA_VERSION - 1}" in text
    assert MIGRATE_COMMAND in text
    assert MIGRATE_MODULE_COMMAND in text
    assert _schema(guest_db) == schema
    assert _admin(guest_db) == admin
    assert _side_files(guest_db) == files, "the refusal must leave no -wal/-shm behind"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_every_cli_refuses_a_missing_database_without_creating_it(
    tmp_path, monkeypatch, case
):
    db = tmp_path / "never-created.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 3
    assert not db.exists()


def test_the_seam_refuses_a_newer_database_with_the_shared_wording(guest_db):
    from app.services.maintenance_cli import (
        MaintenanceCliSchemaError,
        create_schema_guest_repository,
    )

    _stamp(guest_db, SCHEMA_VERSION + 1)
    schema = _schema(guest_db)
    with pytest.raises(MaintenanceCliSchemaError) as caught:
        create_schema_guest_repository(Settings())
    assert caught.value.exit_code == 3
    assert f"SQLite v{SCHEMA_VERSION + 1}" in str(caught.value)
    assert "更新" in str(caught.value)
    assert _schema(guest_db) == schema


@pytest.mark.parametrize(
    ("state", "reason", "remedy"),
    [
        (SchemaCurrency("sqlite", False, 0, 98, sqlite_file_missing=True), "库文件不存在", "启动一次服务"),
        (SchemaCurrency("sqlite", False, 0, 98), "schema 版本为 0", "启动一次服务"),
        (SchemaCurrency("postgresql", False, 0, 78), "没有迁移账本", "启动一次服务"),
        (SchemaCurrency("sqlite", True, 97, 98), "SQLite v97，本构建需要 SQLite v98", MIGRATE_COMMAND),
        (
            SchemaCurrency("postgresql", True, 77, 78, recorded_versions=tuple(range(1, 78))),
            "PostgreSQL 0077，本构建需要 PostgreSQL 0078",
            MIGRATE_COMMAND,
        ),
        (SchemaCurrency("sqlite", True, 99, 98), "更新", "相同版本的代码"),
        (
            SchemaCurrency("postgresql", True, 78, 78, recorded_versions=(1, 3)),
            "不是连续的",
            "完全相同版本的代码",
        ),
        (
            SchemaCurrency(
                "postgresql", True, 2, 78, recorded_versions=(1, 2), checksum_mismatch_at=2
            ),
            "版本 0002 的 checksum",
            "完全相同版本的代码",
        ),
    ],
)
def test_the_one_refusal_wording_names_reason_and_remedy(state, reason, remedy):
    assert not state.is_current
    message = schema_refusal_message(state)
    assert reason in message
    assert remedy in message


# ─────────────────────────────────────────────── database migrate ──


def _migrate(argv):
    from app.scripts import migrate_database

    get_settings.cache_clear()
    return migrate_database.main(argv)


def test_database_migrate_brings_a_behind_database_current_without_seeding(
    guest_db, capsys
):
    _stamp(guest_db, SCHEMA_VERSION - 1)
    admin = _admin(guest_db)

    assert _migrate(["--confirm-service-stopped"]) == 0

    assert _schema(guest_db)[0] == SCHEMA_VERSION
    assert inspect_schema_currency(Settings()).is_current
    assert _admin(guest_db) == admin, "the migrate command must never seed"
    assert f"SQLite v{SCHEMA_VERSION - 1}" in capsys.readouterr().out


def test_database_migrate_is_idempotent(guest_db, capsys):
    schema, admin = _schema(guest_db), _admin(guest_db)

    assert _migrate(["--confirm-service-stopped"]) == 0
    assert _migrate(["--confirm-service-stopped"]) == 0

    assert _schema(guest_db) == schema
    assert _admin(guest_db) == admin
    assert "无需迁移" in capsys.readouterr().out


def test_database_migrate_refuses_without_the_stopped_service_confirmation(
    guest_db, capsys
):
    _stamp(guest_db, SCHEMA_VERSION - 1)
    schema = _schema(guest_db)

    assert _migrate([]) == 2

    assert _schema(guest_db) == schema
    assert "--confirm-service-stopped" in capsys.readouterr().err


def test_database_migrate_refuses_to_create_a_database(tmp_path, monkeypatch, capsys):
    db = tmp_path / "never-created.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))

    assert _migrate(["--confirm-service-stopped"]) == 3

    assert not db.exists()
    assert "启动一次服务" in capsys.readouterr().err


def test_database_migrate_refuses_a_newer_database(guest_db):
    _stamp(guest_db, SCHEMA_VERSION + 1)
    schema = _schema(guest_db)

    assert _migrate(["--confirm-service-stopped"]) == 3
    assert _schema(guest_db) == schema


def test_database_migrate_is_in_the_unified_catalog():
    cli = load_script("cli")

    command = cli.COMMANDS[("database", "migrate")]
    # A backend module, so the offline package (which ships backend/ only) has it.
    assert command.script == "app.scripts.migrate_database"
    assert command.module is True
    assert command.deployment_env is True
    # --env-file is the authoritative settings source, as for selected-source.
    assert ("database", "migrate") in cli.SELECTED_ENV_COMMANDS


def test_database_migrate_refusing_a_fresh_database_does_not_say_it_never_migrates(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.db'}")

    assert _migrate(["--confirm-service-stopped"]) == 3

    err = capsys.readouterr().err
    assert "本命令不创建、不迁移" not in err
    assert "`database migrate` 只升级已由服务初始化过的库" in err


def test_database_migrate_env_file_wins_over_the_shell(guest_db, tmp_path, monkeypatch):
    """With --env-file the file names the database; the exported DATABASE_URL
    (another, also-behind database) is ignored and left exactly as it was."""
    shell_db = tmp_path / "shell-target.db"
    import shutil

    shutil.copy(guest_db, shell_db)
    _stamp(shell_db, SCHEMA_VERSION - 1)
    _stamp(guest_db, SCHEMA_VERSION - 1)
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        f"DATABASE_URL=sqlite:///{guest_db}\n"
        f"SILICON_NOTEBOOK_STORAGE_DIR={tmp_path / 'storage'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{shell_db}")
    shell_schema = _schema(shell_db)

    assert _migrate(["--env-file", str(env_file), "--confirm-service-stopped"]) == 0

    assert _schema(guest_db)[0] == SCHEMA_VERSION
    assert _schema(shell_db) == shell_schema


def test_database_migrate_rejects_a_missing_env_file(tmp_path, capsys):
    assert _migrate(["--env-file", str(tmp_path / "nope.env"), "--confirm-service-stopped"]) == 2
    assert "--env-file" in capsys.readouterr().err


def test_the_maintenance_opener_rechecks_the_schema_under_the_lock(guest_db, monkeypatch):
    """A migration from a newer checkout landing between the composition gate
    and the maintenance lock is seen under the lock; the body never runs."""
    from app.services import maintenance_cli

    real = maintenance_cli.create_repository

    def compose_then_race(settings):
        repository = real(settings)
        _stamp(guest_db, SCHEMA_VERSION + 1)  # the concurrent `database migrate`
        return repository

    monkeypatch.setattr(maintenance_cli, "create_repository", compose_then_race)
    entered = []
    with pytest.raises(maintenance_cli.MaintenanceCliSchemaError) as caught:
        with maintenance_cli.open_maintenance_cli_repository(
            Settings(), confirm_service_stopped=False
        ):
            entered.append(True)
    assert entered == []
    assert f"SQLite v{SCHEMA_VERSION + 1}" in str(caught.value)


def test_the_sqlite_probe_creates_nothing_beside_a_closed_wal_database(tmp_path):
    from app.repositories.schema_currency import inspect_sqlite_schema

    db = tmp_path / "wal.db"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (a)")
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    connection.close()  # the last close checkpoints and removes -wal/-shm
    assert _side_files(db) == {"wal.db"}

    assert inspect_sqlite_schema(db).current == 7
    assert _side_files(db) == {"wal.db"}


def test_the_sqlite_probe_reads_a_version_still_in_the_wal(tmp_path):
    """While a connection holds the database open, the newest user_version may
    sit in the WAL; the probe must read through it, not the stale main file."""
    from app.repositories.schema_currency import inspect_sqlite_schema

    db = tmp_path / "live.db"
    holder = sqlite3.connect(db)
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("PRAGMA wal_autocheckpoint=0")
        holder.execute("CREATE TABLE t (a)")
        holder.execute("PRAGMA user_version = 11")
        holder.commit()
        assert inspect_sqlite_schema(db).current == 11
    finally:
        holder.close()


def test_promotion_targets_refuses_a_database_one_version_behind(guest_db, capsys):
    module = load_script("backfill_promotion_targets")
    _stamp(guest_db, SCHEMA_VERSION - 1)
    schema = _schema(guest_db)

    assert module.main(["--db", str(guest_db), "list"]) == 3

    err = capsys.readouterr().err
    assert f"SQLite v{SCHEMA_VERSION - 1}" in err and MIGRATE_COMMAND in err
    # The remedy names the --db file, not whatever DATABASE_URL points at.
    assert f"DATABASE_URL=sqlite:///{guest_db.resolve()}" in err
    assert "以该库启动一次升级后的服务" in err
    assert _schema(guest_db) == schema


def test_promotion_targets_remedy_for_an_uninitialized_file_names_that_file(tmp_path, capsys):
    module = load_script("backfill_promotion_targets")
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()  # exists, user_version 0

    assert module.main(["--db", str(db), "list"]) == 3

    err = capsys.readouterr().err
    assert "SQLite 库的 schema 版本为 0" in err and "以该库启动一次服务" in err


def test_the_sqlite_probe_never_reads_immutable_beside_a_rollback_journal(
    tmp_path, monkeypatch
):
    """A ``-journal`` (hot, or a DELETE-mode writer mid-transaction) means the
    main file alone cannot be trusted: the probe must take the ordinary
    locked ``mode=ro`` path, never ``immutable=1``."""
    import sqlite3 as sqlite3_module

    from app.repositories.schema_currency import inspect_sqlite_schema

    db = tmp_path / "rollback.db"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA user_version = 9")
    connection.commit()
    connection.close()
    (tmp_path / "rollback.db-journal").write_bytes(b"")
    opened: list[str] = []
    real_connect = sqlite3_module.connect

    def recording_connect(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(sqlite3_module, "connect", recording_connect)

    assert inspect_sqlite_schema(db).current == 9
    assert opened and all("immutable" not in uri for uri in opened)
    assert all("mode=ro" in uri for uri in opened)


def test_a_connected_but_failed_ledger_read_has_its_own_credential_free_wording(
    tmp_path, monkeypatch, capfd
):
    """Connected, then the read fails (timeout, lock wait, privilege): the
    operator sees the second fixed wording, never the server's text."""
    from app.repositories.postgres import database as postgres_database

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            raise RuntimeError(
                'permission denied for table silicon_schema_migrations '
                '(user "opsuser" at db-secret-host/secretdb, password s3cret)'
            )

    monkeypatch.setattr(
        postgres_database._SafeDiagnosticConnection,
        "connect",
        classmethod(lambda cls, *args, **kwargs: _Session()),
    )
    monkeypatch.setenv("DATABASE_URL", "postgresql://opsuser:s3cret@db-secret-host:5432/secretdb")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    get_settings.cache_clear()

    assert _migrate(["--confirm-service-stopped"]) == 2

    output = capfd.readouterr()
    text = output.out + output.err
    assert "已连上 PostgreSQL，但读取 schema 迁移账本失败" in text
    assert "无法连接 PostgreSQL" not in text
    for token in SECRET_TOKENS + ("permission denied",):
        assert token not in text, f"{token!r} leaked: {text}"


def test_promotion_targets_runs_on_a_current_database(guest_db):
    module = load_script("backfill_promotion_targets")

    assert module.main(["--db", str(guest_db), "list"]) == 0


# ────────────────────────────── connection failures never leak credentials ──

SECRET_TOKENS = ("s3cr", "ZZet", "opsuser", "db-secret-host", "10.255.255.1", "secretdb")
UNREACHABLE_URLS = {
    # an unescaped "%" in the password: psycopg's parse error quotes the token
    "malformed": "postgresql://opsuser:s3cr%ZZet@db-secret-host.invalid:1/secretdb",
    # unroutable address: bounded by connect_timeout, not the OS TCP timeout
    "unreachable": "postgresql://opsuser:s3cret@10.255.255.1:5432/secretdb",
}
LEAK_PROBES = (
    ("database migrate", lambda ctx, mp: _migrate(["--confirm-service-stopped"])),
    *(
        (case.name, (lambda case: lambda ctx, mp: run_case(case, ctx, mp))(case))
        for case in CASES
        if not case.sqlite_only and case.name in {
            "batch_ingest embed", "build_chunks", "kg build_kg",
            "backfill_global_ask_attribution", "maintain delete-leftovers",
            "backfill_navigation_chunks --dry-run", "backfill_navigation_chunks",
            "prepare_selected_source_graph", "reflect_shadow_rig search",
        }
    ),
)


@pytest.mark.parametrize("url_kind", sorted(UNREACHABLE_URLS))
@pytest.mark.parametrize("probe", LEAK_PROBES, ids=lambda probe: probe[0])
def test_a_failed_schema_check_prints_no_connection_details(
    tmp_path, monkeypatch, capfd, url_kind, probe
):
    import time

    monkeypatch.setenv("DATABASE_URL", UNREACHABLE_URLS[url_kind])
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()
    started = time.monotonic()

    code = probe[1](CliContext(tmp=tmp_path, confirm=("--confirm-service-stopped",)), monkeypatch)

    elapsed = time.monotonic() - started
    output = capfd.readouterr()
    text = output.out + output.err
    if probe[0] == "prepare_selected_source_graph":
        assert code == 1 and "open_repository_failed" in text  # content-free code
    else:
        assert code == 2, text
        assert "无法连接 PostgreSQL 数据库来检查 schema 版本" in text
    for token in SECRET_TOKENS:
        assert token not in text, f"{token!r} leaked: {text}"
    assert elapsed < 15, f"the check must fail within its connect timeout ({elapsed:.1f}s)"
