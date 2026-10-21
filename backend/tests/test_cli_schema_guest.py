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


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
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

    assert run_case(case, _ctx(tmp_path), monkeypatch) == 3

    output = capsys.readouterr()
    text = output.out + output.err
    assert f"SQLite v{SCHEMA_VERSION - 1}" in text
    assert MIGRATE_COMMAND in text
    assert _schema(guest_db) == schema
    assert _admin(guest_db) == admin


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
    return load_script("migrate_database").main(argv)


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
    assert command.script == "migrate_database.py"
    assert command.deployment_env is True
