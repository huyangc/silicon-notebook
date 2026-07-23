from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.migration.sqlite_to_postgres import (
    SqliteToPostgresMigrationError,
    _CommutativeRowDigest,
    _PostgresColumn,
    _TransformAudit,
    _sqlite_business_tables,
    _transform_sqlite_value,
    create_consistent_snapshot,
    inspect_source,
    target_url_from_environment,
    validate_existing_snapshot,
)


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
    ) == datetime(2026, 7, 23, 10, 11, 12, tzinfo=timezone.utc)
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
