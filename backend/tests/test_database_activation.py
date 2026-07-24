from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import dotenv_values

from app.migration import database_activation
from app.migration.database_activation import (
    _env_quote,
    activate_postgres_from_receipt,
)
from app.migration.sqlite_to_postgres import (
    MigrationVerification,
    SqliteToPostgresMigrationError,
)


def _verification(source: Path) -> MigrationVerification:
    return MigrationVerification(
        source_path=str(source),
        snapshot_path=str(source.with_suffix(".snapshot.db")),
        target_redacted_url="postgresql://127.0.0.1:5432/notebook",
        target_schema="public",
        total_rows=7,
        tables=(),
    )


def test_verified_activation_atomically_swaps_active_and_shadow_urls(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "silicon_notebook.db"
    source.touch()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "KEEP=value\nDATABASE_URL=sqlite:///silicon_notebook.db\n",
        encoding="utf-8",
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )
    target = "postgresql://user:secret@127.0.0.1:5432/notebook?sslmode=disable"

    result = activate_postgres_from_receipt(
        source_path=source,
        target_url=target,
        receipt_path=tmp_path / "receipt.json",
        work_dir=tmp_path / "work",
        env_path=env_path,
        root_dir=tmp_path,
    )

    values = dotenv_values(env_path, interpolate=False)
    assert values["DATABASE_URL"] == target
    assert values["SHADOW_DATABASE_URL"] == "sqlite:///silicon_notebook.db"
    assert values["KEEP"] == "value"
    assert result.changed is True
    assert result.backup_path is not None
    backup = Path(result.backup_path)
    assert backup.name.startswith(".env.pre-postgres-")
    assert backup.read_text(encoding="utf-8") == (
        "KEEP=value\nDATABASE_URL=sqlite:///silicon_notebook.db\n"
    )
    assert os.stat(env_path).st_mode & 0o777 == 0o600
    assert os.stat(backup).st_mode & 0o777 == 0o600
    assert "secret" not in result.active_database
    assert result.env_sha256_before != result.env_sha256_after

    second = activate_postgres_from_receipt(
        source_path=source,
        target_url=target,
        receipt_path=tmp_path / "receipt.json",
        work_dir=tmp_path / "work",
        env_path=env_path,
        root_dir=tmp_path,
    )
    assert second.changed is False
    assert second.backup_path is None
    assert second.env_sha256_before == second.env_sha256_after


def test_activation_fails_closed_before_replacing_mismatched_env(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "expected.db"
    source.touch()
    env_path = tmp_path / ".env"
    original = "DATABASE_URL=sqlite:///different.db\n"
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )

    with pytest.raises(SqliteToPostgresMigrationError, match="migrated SQLite source"):
        activate_postgres_from_receipt(
            source_path=source,
            target_url="postgresql://127.0.0.1:5432/notebook",
            receipt_path=tmp_path / "receipt.json",
            work_dir=tmp_path / "work",
            env_path=env_path,
            root_dir=tmp_path,
        )

    assert env_path.read_text(encoding="utf-8") == original
    assert not list(tmp_path.glob(".env.pre-postgres-*.bak"))


def test_activation_rejects_ambiguous_or_symlinked_env(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.db"
    source.touch()
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )
    duplicate = tmp_path / "duplicate.env"
    duplicate.write_text(
        "DATABASE_URL=sqlite:///source.db\nDATABASE_URL=sqlite:///source.db\n",
        encoding="utf-8",
    )
    link = tmp_path / "linked.env"
    link.symlink_to(duplicate)

    for env_path, message in (
        (duplicate, "exactly one DATABASE_URL"),
        (link, "must not be a symlink"),
    ):
        with pytest.raises(SqliteToPostgresMigrationError, match=message):
            activate_postgres_from_receipt(
                source_path=source,
                target_url="postgresql://127.0.0.1:5432/notebook",
                receipt_path=tmp_path / "receipt.json",
                work_dir=tmp_path / "work",
                env_path=env_path,
                root_dir=tmp_path,
            )


def test_activation_does_not_overwrite_a_concurrent_env_edit(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.db"
    source.touch()
    env_path = tmp_path / ".env"
    env_path.write_text("DATABASE_URL=sqlite:///source.db\n", encoding="utf-8")
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )
    original_write = database_activation._write_exclusive

    def write_then_race(path: Path, data: bytes, *, mode: int) -> None:
        original_write(path, data, mode=mode)
        if ".postgres-" in path.name:
            env_path.write_text(
                "DATABASE_URL=sqlite:///source.db\nCONCURRENT=yes\n",
                encoding="utf-8",
            )

    monkeypatch.setattr(database_activation, "_write_exclusive", write_then_race)

    with pytest.raises(SqliteToPostgresMigrationError, match="changed during"):
        activate_postgres_from_receipt(
            source_path=source,
            target_url="postgresql://127.0.0.1:5432/notebook",
            receipt_path=tmp_path / "receipt.json",
            work_dir=tmp_path / "work",
            env_path=env_path,
            root_dir=tmp_path,
        )

    assert "CONCURRENT=yes" in env_path.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".env.pre-postgres-*.bak"))
    assert not list(tmp_path.glob(".env.postgres-*.tmp"))


def test_activation_preserves_export_prefix_on_database_url(
    tmp_path: Path, monkeypatch
):
    # A sourced env file that exports DATABASE_URL must keep exporting it after
    # cutover; dropping `export` would demote it to a plain shell variable that
    # child processes never inherit (they could then fall back to SQLite).
    source = tmp_path / "silicon_notebook.db"
    source.touch()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "export DATABASE_URL=sqlite:///silicon_notebook.db\n", encoding="utf-8"
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )
    target = "postgresql://user:secret@127.0.0.1:5432/notebook"

    result = activate_postgres_from_receipt(
        source_path=source,
        target_url=target,
        receipt_path=tmp_path / "receipt.json",
        work_dir=tmp_path / "work",
        env_path=env_path,
        root_dir=tmp_path,
    )

    assert result.changed is True
    text = env_path.read_text(encoding="utf-8")
    assert any(line.startswith("export DATABASE_URL=") for line in text.splitlines())
    values = dotenv_values(env_path, interpolate=False)
    assert values["DATABASE_URL"] == target
    assert values["SHADOW_DATABASE_URL"] == "sqlite:///silicon_notebook.db"


def test_activation_resolves_relative_sqlite_url_against_app_root(
    tmp_path: Path, monkeypatch
):
    # The env file lives outside the app root (e.g. /etc) and holds the supported
    # relative default; activation must resolve it against the app root_dir (as
    # the running app does), not the env file's own parent directory.
    root_dir = tmp_path / "app"
    (root_dir / ".local").mkdir(parents=True)
    source = root_dir / ".local" / "silicon_notebook.db"
    source.touch()
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    env_path = env_dir / "silicon.env"
    env_path.write_text(
        "DATABASE_URL=sqlite:///.local/silicon_notebook.db\n", encoding="utf-8"
    )
    env_path.chmod(0o600)
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )

    result = activate_postgres_from_receipt(
        source_path=source,
        target_url="postgresql://127.0.0.1:5432/notebook",
        receipt_path=tmp_path / "receipt.json",
        work_dir=tmp_path / "work",
        env_path=env_path,
        root_dir=root_dir,
    )

    assert result.changed is True
    values = dotenv_values(env_path, interpolate=False)
    assert values["DATABASE_URL"] == "postgresql://127.0.0.1:5432/notebook"
    assert values["SHADOW_DATABASE_URL"] == "sqlite:///.local/silicon_notebook.db"


def test_reactivation_with_a_different_active_url_fails_closed(
    tmp_path: Path, monkeypatch
):
    # DATABASE_URL is already PostgreSQL with the same host/port/db but a
    # different password; DatabaseIdentity alone would treat this as "already
    # active, unchanged", so a full-URL mismatch must fail closed instead of
    # letting the backend restart on stale credentials.
    source = tmp_path / "silicon_notebook.db"
    source.touch()
    env_path = tmp_path / ".env"
    original = (
        "DATABASE_URL=postgresql://user:OLDPASS@127.0.0.1:5432/notebook\n"
        "SHADOW_DATABASE_URL=sqlite:///silicon_notebook.db\n"
    )
    env_path.write_text(original, encoding="utf-8")
    env_path.chmod(0o600)
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )

    with pytest.raises(
        SqliteToPostgresMigrationError, match="does not match this migration"
    ):
        activate_postgres_from_receipt(
            source_path=source,
            target_url="postgresql://user:NEWPASS@127.0.0.1:5432/notebook",
            receipt_path=tmp_path / "receipt.json",
            work_dir=tmp_path / "work",
            env_path=env_path,
            root_dir=tmp_path,
        )
    assert env_path.read_text(encoding="utf-8") == original


def test_activation_restricts_credential_env_to_owner_only(
    tmp_path: Path, monkeypatch
):
    # A permissive (0644) source env file must not propagate its group/world
    # read bits to the replacement that now holds the PostgreSQL password.
    source = tmp_path / "silicon_notebook.db"
    source.touch()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "DATABASE_URL=sqlite:///silicon_notebook.db\n", encoding="utf-8"
    )
    env_path.chmod(0o644)
    monkeypatch.setattr(
        database_activation,
        "verify_migration_receipt",
        lambda **_kwargs: _verification(source),
    )

    result = activate_postgres_from_receipt(
        source_path=source,
        target_url="postgresql://user:secret@127.0.0.1:5432/notebook",
        receipt_path=tmp_path / "receipt.json",
        work_dir=tmp_path / "work",
        env_path=env_path,
        root_dir=tmp_path,
    )

    assert result.changed is True
    assert os.stat(env_path).st_mode & 0o777 == 0o600
    assert Path(result.backup_path).stat().st_mode & 0o777 == 0o600


def test_env_quote_is_safe_and_fails_closed_on_apostrophe_or_interpolation():
    # A safe value is single-quote wrapped (literal for shell `source`).
    assert _env_quote("postgresql://u:p@h:5432/db?sslmode=disable") == (
        "'postgresql://u:p@h:5432/db?sslmode=disable'"
    )
    # An apostrophe (unquotable for both loaders) and a `$` (interpolated by the
    # runtime dotenv loader even inside single quotes) both fail closed rather
    # than be silently altered on restart.
    for unsafe in (
        "postgresql://u:pa'ss@h:5432/db",
        "postgresql://u:pa${X}ss@h:5432/db",
        "postgresql://u:pa$X@h:5432/db",
    ):
        with pytest.raises(SqliteToPostgresMigrationError):
            _env_quote(unsafe)
