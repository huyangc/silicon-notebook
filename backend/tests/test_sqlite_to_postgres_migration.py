from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.migration import sqlite_to_postgres as migration_module
from app.migration.sqlite_to_postgres import (
    SqliteToPostgresMigrationError,
    _CommutativeRowDigest,
    _PostgresColumn,
    _TransformAudit,
    _sqlite_business_tables,
    _transform_sqlite_value,
    create_consistent_snapshot,
    inspect_source,
    prepare_upgraded_snapshot,
    target_url_from_environment,
    validate_existing_snapshot,
)
from app.repositories.postgres._store_utils import normalize_timestamp


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE users(id TEXT PRIMARY KEY, display_name TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO users VALUES ('u1','测试')")
        connection.execute("PRAGMA user_version=7")


def test_consistent_snapshot_is_read_only_and_published_atomically(tmp_path: Path):
    source = tmp_path / "source.db"
    _source(source)
    before_hash = _sha256(source)
    before_stat = source.stat()

    messages: list[str] = []
    snapshot, digest = create_consistent_snapshot(
        source_path=source,
        work_dir=tmp_path / "work",
        progress=messages.append,
    )

    assert _sha256(source) == before_hash
    assert source.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert _sha256(snapshot) == digest
    with sqlite3.connect(snapshot) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT display_name FROM users").fetchone()[0] == "测试"
    assert messages
    assert not list((tmp_path / "work").glob("*.tmp"))
    assert validate_existing_snapshot(
        snapshot_path=snapshot, work_dir=tmp_path / "work"
    ) == (snapshot, digest)

    snapshot.with_name(snapshot.name + "-wal").touch()
    with pytest.raises(SqliteToPostgresMigrationError, match="sidecar"):
        validate_existing_snapshot(
            snapshot_path=snapshot, work_dir=tmp_path / "work"
        )


def test_reused_snapshot_is_bound_to_its_source(tmp_path: Path):
    source = tmp_path / "source.db"
    _source(source)
    work = tmp_path / "work"

    snapshot, _digest = create_consistent_snapshot(
        source_path=source, work_dir=work, progress=lambda _message: None
    )
    sidecar = migration_module._snapshot_origin_sidecar(snapshot, source.resolve())
    assert sidecar.exists()

    # Reuse selecting the original source is accepted.
    reused, _ = validate_existing_snapshot(
        snapshot_path=snapshot, work_dir=work, expected_source=source.resolve()
    )
    assert reused == snapshot

    # Reuse selecting a different source fails closed (the codex round-7 P2 gap):
    # that source has no matching origin record for this snapshot.
    other = tmp_path / "other.db"
    other.touch()
    with pytest.raises(SqliteToPostgresMigrationError, match="no matching origin"):
        validate_existing_snapshot(
            snapshot_path=snapshot, work_dir=work, expected_source=other.resolve()
        )

    # Removing this source's origin record also blocks reuse for it.
    sidecar.unlink()
    with pytest.raises(SqliteToPostgresMigrationError, match="no matching origin"):
        validate_existing_snapshot(
            snapshot_path=snapshot, work_dir=work, expected_source=source.resolve()
        )


def test_snapshot_origin_retains_all_identical_source_bindings(tmp_path: Path):
    # Snapshot names are content-addressed, so a byte-identical database at
    # another path seals to the same file. Binding the second source must not
    # invalidate the first source's receipt/reuse (codex PR#355 round-1 P2).
    import shutil

    source_a = tmp_path / "a.db"
    _source(source_a)
    work = tmp_path / "work"
    snapshot, _digest = create_consistent_snapshot(
        source_path=source_a, work_dir=work, progress=lambda _message: None
    )

    source_b = tmp_path / "b.db"
    shutil.copy(source_a, source_b)
    # Simulate the collision-reuse path: create_consistent_snapshot would seal to
    # the same content-addressed file and record the second source's origin.
    migration_module._write_snapshot_origin(snapshot, source_b.resolve())

    for source in (source_a, source_b):
        reused, _ = validate_existing_snapshot(
            snapshot_path=snapshot, work_dir=work, expected_source=source.resolve()
        )
        assert reused == snapshot


def test_inspect_source_rejects_symlink(tmp_path: Path):
    source = tmp_path / "source.db"
    _source(source)
    link = tmp_path / "source-link.db"
    link.symlink_to(source)

    with pytest.raises(SqliteToPostgresMigrationError, match="symlink"):
        inspect_source(link)


def test_transform_typed_values_and_normalize_legacy_sentinels():
    json_column = _PostgresColumn("trace_json", "jsonb", "jsonb", False, False)
    time_column = _PostgresColumn(
        "finished_at", "timestamp with time zone", "timestamptz", True, False
    )
    bytea_column = _PostgresColumn("vector", "bytea", "bytea", False, False)

    assert _transform_sqlite_value(
        table="ask_jobs", column=json_column, value=""
    ) == []
    assert _transform_sqlite_value(
        table="kg_build_jobs", column=time_column, value=""
    ) is None
    assert _transform_sqlite_value(
        table="kg_build_jobs", column=time_column, value="2026-07-23 10:11:12"
    ) == normalize_timestamp("2026-07-23 10:11:12")
    assert _transform_sqlite_value(
        table="chunk_embeddings",
        column=bytea_column,
        value=memoryview(b"\x00\x00\x80?"),
    ) == b"\x00\x00\x80?"
    assert _transform_sqlite_value(
        table="chunk_embeddings", column=bytea_column, value="[1.0, 2.0]"
    ) == b"\x00\x00\x80?\x00\x00\x00@"

    audit = _TransformAudit()
    assert _transform_sqlite_value(
        table="knowledge_objects",
        column=_PostgresColumn("payload", "jsonb", "jsonb", False, False),
        value='{"text":"before\\u0000after"}',
        audit=audit,
    ) == {"text": "before\\u0000after"}
    assert audit.nul_escapes == {"knowledge_objects.payload": 1}


def test_naive_timestamps_are_interpreted_as_local_wall_time():
    # On a non-UTC host, a naive SQLite timestamp is local wall time; migrating
    # it must shift to UTC by the host offset, not relabel it as UTC.
    time_column = _PostgresColumn(
        "finished_at", "timestamp with time zone", "timestamptz", True, False
    )
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"  # UTC+8, no DST
    time.tzset()
    try:
        assert _transform_sqlite_value(
            table="kg_build_jobs", column=time_column, value="2026-07-23 10:11:12"
        ) == datetime(2026, 7, 23, 2, 11, 12, tzinfo=timezone.utc)
        # An aware value keeps its instant regardless of host zone.
        assert _transform_sqlite_value(
            table="kg_build_jobs",
            column=time_column,
            value="2026-07-23T10:11:12+00:00",
        ) == datetime(2026, 7, 23, 10, 11, 12, tzinfo=timezone.utc)
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def test_explicit_source_timezone_interprets_naive_timestamps():
    # An operator running the importer on a different-timezone host than the
    # SQLite source passes --source-timezone; naive timestamps are then read in
    # that explicit zone, deterministically, regardless of the importer host.
    from zoneinfo import ZoneInfo

    time_column = _PostgresColumn(
        "finished_at", "timestamp with time zone", "timestamptz", True, False
    )
    assert _transform_sqlite_value(
        table="kg_build_jobs",
        column=time_column,
        value="2026-07-23 10:11:12",
        source_timezone=ZoneInfo("Asia/Shanghai"),
    ) == datetime(2026, 7, 23, 2, 11, 12, tzinfo=timezone.utc)
    assert _transform_sqlite_value(
        table="kg_build_jobs",
        column=time_column,
        value="2026-07-23 10:11:12",
        source_timezone=ZoneInfo("UTC"),
    ) == datetime(2026, 7, 23, 10, 11, 12, tzinfo=timezone.utc)


def test_failed_snapshot_upgrade_deletes_the_working_copy(
    tmp_path: Path, monkeypatch
):
    # A pre-v29 source is copied to a private working DB before upgrade; if the
    # upgrade fails, that (potentially multi-GB) copy and its sidecars must be
    # removed so retries cannot exhaust the migration volume.
    source = tmp_path / "old.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        connection.execute("PRAGMA user_version=1")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    def boom(self, *args, **kwargs):
        raise RuntimeError("simulated upgrade failure")

    monkeypatch.setattr(migration_module.SqliteMigrator, "migrate", boom)

    with pytest.raises(SqliteToPostgresMigrationError):
        prepare_upgraded_snapshot(
            snapshot_path=source, work_dir=work_dir, root_dir=REPO_ROOT
        )
    assert list(work_dir.glob(".sqlite-upgrade-*")) == []


def test_row_digest_is_order_independent_but_duplicate_sensitive():
    columns = (_PostgresColumn("id", "text", "text", False, False),)
    first = _CommutativeRowDigest()
    second = _CommutativeRowDigest()
    duplicate = _CommutativeRowDigest()
    for value in ("a", "b", "c"):
        first.add(columns, (value,))
    for value in ("c", "a", "b"):
        second.add(columns, (value,))
    for value in ("a", "b", "b"):
        duplicate.add(columns, (value,))

    assert first.hexdigest() == second.hexdigest()
    assert first.hexdigest() != duplicate.hexdigest()


def test_target_url_is_read_from_a_valid_named_environment(monkeypatch):
    monkeypatch.setenv("POSTGRES_MIGRATION_URL", "postgresql://hidden@example/db")
    assert target_url_from_environment("POSTGRES_MIGRATION_URL").startswith(
        "postgresql://hidden"
    )
    with pytest.raises(SqliteToPostgresMigrationError, match="invalid"):
        target_url_from_environment("bad-name")
    monkeypatch.delenv("POSTGRES_MIGRATION_URL")
    with pytest.raises(SqliteToPostgresMigrationError, match="not set"):
        target_url_from_environment("POSTGRES_MIGRATION_URL")


def test_retired_sqlite_tables_must_be_empty(tmp_path: Path):
    source = tmp_path / "legacy.db"
    with sqlite3.connect(source) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE users(id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE articles(id TEXT PRIMARY KEY)")
        assert _sqlite_business_tables(connection) == {"users"}
        connection.execute("INSERT INTO articles VALUES ('legacy')")
        with pytest.raises(SqliteToPostgresMigrationError, match="refusing to discard"):
            _sqlite_business_tables(connection)
