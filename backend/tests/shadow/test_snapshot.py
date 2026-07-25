from __future__ import annotations

import json
import os
import shutil
import sqlite3
import struct
import threading
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow import snapshot as snapshot_module
from app.migration.shadow.manifest import MANIFEST
from app.migration.shadow.snapshot import SnapshotCancelled, create_snapshot
from app.migration.shadow.transform import (
    PostgresColumn,
    canonical_json_value,
    exact_json_dumps,
    transform_sqlite_row,
)
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "snapshot-run-1"


def _atomically_replace_live_database(
    database: SqliteDatabase,
    replacement: Path,
    *,
    run_id: str,
) -> sqlite3.Connection:
    cached = database.connect()
    cached.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    cached.execute("PRAGMA journal_mode=DELETE")
    cached.commit()
    fresh = sqlite3.connect(replacement)
    try:
        cached.backup(fresh)
        fresh.execute(
            "UPDATE shadow_capture_control SET run_id=? WHERE singleton=1",
            (run_id,),
        )
        fresh.commit()
        fresh.execute("PRAGMA journal_mode=DELETE")
    finally:
        fresh.close()
    os.replace(replacement, database.db_path)
    return cached


@pytest.fixture
def source_database(tmp_path: Path):
    path = tmp_path / "source with # marker.db"
    settings = Settings(database_url=f"sqlite:///{quote(str(path))}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    conn = database.connect()
    install_sqlite_capture(conn, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    conn.execute(
        "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1 AND run_id=?",
        (RUN_ID,),
    )
    # Enough pages for the backup progress hook to interleave a real writer.
    body = "中文 Markdown $v=1$\n" * 8_000
    for index in range(12):
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
            (f"large-{index}", body, "2026-07-24 00:00:00"),
        )
    conn.commit()
    try:
        yield database
    finally:
        database.close_local()


def test_snapshot_is_consistent_under_active_writes_and_reads_h0_from_itself(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    first_progress = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def writer() -> None:
        try:
            assert first_progress.wait(5)
            conn = sqlite3.connect(source_database.db_path, timeout=5)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES ('pair-a','x','t')"
                )
                conn.execute(
                    "INSERT INTO app_settings(key,value,updated_at) VALUES ('pair-b','x','t')"
                )
                conn.commit()
            finally:
                conn.close()
        except BaseException as exc:  # pragma: no cover - asserted below
            writer_errors.append(exc)
        finally:
            writer_done.set()

    thread = threading.Thread(target=writer)
    thread.start()
    snapshot = create_snapshot(
        source_database,
        tmp_path / "work" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
        pages=1,
        progress=lambda *_args: first_progress.set(),
    )
    thread.join(5)
    assert writer_done.is_set()
    assert writer_errors == []
    assert snapshot.path.exists()
    assert snapshot.path.stat().st_mode & 0o077 == 0
    copied = sqlite3.connect(snapshot.path)
    try:
        pair_count = copied.execute(
            "SELECT COUNT(*) FROM app_settings WHERE key IN ('pair-a','pair-b')"
        ).fetchone()[0]
        assert pair_count in {0, 2}
        assert (
            snapshot.baseline_seq
            == copied.execute(
                "SELECT MAX(COALESCE((SELECT MAX(seq) FROM shadow_change_log),0),"
                "COALESCE((SELECT seq FROM sqlite_sequence "
                "WHERE name='shadow_change_log'),0))"
            ).fetchone()[0]
        )
    finally:
        copied.close()
    # The live source remained writable after backup publication.
    with source_database.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES ('after','ok','t')"
        )


def test_snapshot_h0_is_global_and_survives_retention_without_current_run_rows(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    with source_database.write() as conn:
        conn.execute("UPDATE shadow_change_log SET run_id='older-run'")
        expected = conn.execute("SELECT MAX(seq) FROM shadow_change_log").fetchone()[0]
    first = create_snapshot(
        source_database,
        tmp_path / "old-run" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    assert first.baseline_seq == expected

    with source_database.write() as conn:
        conn.execute("DELETE FROM shadow_change_log")
        allocated = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='shadow_change_log'"
        ).fetchone()[0]
    second = create_snapshot(
        source_database,
        tmp_path / "retained" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    assert second.baseline_seq == allocated == expected


def test_snapshot_uses_current_path_not_cached_connection_after_atomic_replace(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    cached = _atomically_replace_live_database(
        source_database,
        tmp_path / "replacement.db",
        run_id="replacement-run",
    )
    assert (
        cached.execute(
            "SELECT run_id FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()[0]
        == RUN_ID
    )
    with pytest.raises(ValueError, match="capture run"):
        create_snapshot(
            source_database,
            tmp_path / "must-not-publish.db",
            run_id=RUN_ID,
        )
    assert not (tmp_path / "must-not-publish.db").exists()


def test_fresh_live_open_is_independent_and_configured(source_database: SqliteDatabase):
    cached = source_database.connect()
    fresh, binding = snapshot_module.open_fresh_live_sqlite(source_database)
    try:
        assert fresh is not cached
        assert fresh.row_factory is sqlite3.Row
        assert fresh.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert fresh.execute("PRAGMA busy_timeout").fetchone()[0] == (
            source_database.settings.db_busy_timeout_ms
        )
        snapshot_module.assert_live_sqlite_path(source_database, binding)
    finally:
        fresh.close()


def test_fresh_live_open_rejects_path_stat_change_during_open(
    source_database: SqliteDatabase,
    monkeypatch,
):
    actual = snapshot_module._live_sqlite_path_binding(source_database)
    changed = snapshot_module.LiveSqlitePathBinding(
        configured_path=actual.configured_path,
        resolved_path=actual.resolved_path,
        device=actual.device,
        inode=actual.inode + 1,
    )
    bindings = iter((actual, changed))
    monkeypatch.setattr(
        snapshot_module,
        "_live_sqlite_path_binding",
        lambda _source: next(bindings),
    )
    real_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def close(self):
            type(self).closed = True
            return super().close()

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", tracking_connect)
    with pytest.raises(ValueError, match="changed while opening"):
        snapshot_module.open_fresh_live_sqlite(source_database)
    assert TrackingConnection.closed


def test_fresh_live_open_never_recreates_path_deleted_during_open(
    source_database: SqliteDatabase,
    monkeypatch,
):
    path = Path(source_database.db_path)
    real_connect = sqlite3.connect
    removed = False

    def remove_then_connect(*args, **kwargs):
        nonlocal removed
        if not removed:
            path.unlink()
            removed = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", remove_then_connect)
    with pytest.raises(sqlite3.OperationalError):
        snapshot_module.open_fresh_live_sqlite(source_database)
    assert removed and not path.exists()


def test_cancelled_snapshot_never_publishes_or_leaves_a_partial(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    destination = tmp_path / "cancelled.sqlite3"
    with pytest.raises(SnapshotCancelled):
        create_snapshot(
            source_database,
            destination,
            run_id=RUN_ID,
            pages=1,
            cancel_requested=lambda: True,
        )
    assert not destination.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_post_integrity_key_scan_cancellation_clears_handler_and_never_publishes(
    source_database: SqliteDatabase,
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "integrity-cancelled.sqlite3"
    real_connect = sqlite3.connect
    with source_database.write() as conn:
        conn.executemany(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
            [
                (f"cancel-key-{index}", "value", "2026-07-24 00:00:00")
                for index in range(2_500)
            ],
        )

    class TrackingConnection(sqlite3.Connection):
        active = False
        integrity_finished = False
        replication_scan = False
        calls: list[tuple[bool, int]] = []

        def set_progress_handler(self, handler, instructions):
            type(self).active = handler is not None
            type(self).calls.append((handler is not None, instructions))
            return super().set_progress_handler(handler, instructions)

        def execute(self, statement, parameters=()):
            normalized = " ".join(str(statement).upper().split())
            if normalized.startswith('SELECT 1 FROM "APP_SETTINGS"'):
                type(self).replication_scan = True
            cursor = super().execute(statement, parameters)
            if normalized == "PRAGMA INTEGRITY_CHECK":
                type(self).integrity_finished = True
            return cursor

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", tracking_connect)
    with pytest.raises(SnapshotCancelled):
        create_snapshot(
            source_database,
            destination,
            run_id=RUN_ID,
            pages=1,
            cancel_requested=lambda: (
                TrackingConnection.integrity_finished
                and TrackingConnection.replication_scan
            ),
        )
    assert not destination.exists()
    assert list(tmp_path.glob(".*.tmp")) == []
    assert TrackingConnection.calls[0] == (True, 1_000)
    assert TrackingConnection.calls[-1] == (False, 0)
    assert TrackingConnection.integrity_finished
    assert TrackingConnection.replication_scan


def test_snapshot_refuses_to_overwrite_any_published_path(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    destination = tmp_path / "published.sqlite3"
    destination.write_bytes(b"another-run")
    with pytest.raises(FileExistsError):
        create_snapshot(source_database, destination, run_id=RUN_ID)
    assert destination.read_bytes() == b"another-run"


def test_atomic_publication_loses_race_without_clobbering_winner(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    destination = tmp_path / "raced.sqlite3"
    raced = False

    def publish_competitor(*_args) -> None:
        nonlocal raced
        if not raced:
            destination.write_bytes(b"race-winner")
            raced = True

    with pytest.raises(FileExistsError):
        create_snapshot(
            source_database,
            destination,
            run_id=RUN_ID,
            pages=1,
            progress=publish_competitor,
        )
    assert destination.read_bytes() == b"race-winner"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_snapshot_rejects_insecure_or_symlink_directory(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o700)
    insecure.chmod(0o750)
    with pytest.raises(ValueError, match="owner-only"):
        create_snapshot(source_database, insecure / "snapshot.db", run_id=RUN_ID)
    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(secure, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        create_snapshot(source_database, link / "snapshot.db", run_id=RUN_ID)


def test_snapshot_temp_and_final_ignore_permissive_umask(
    source_database: SqliteDatabase,
    tmp_path: Path,
):
    previous = os.umask(0)
    try:
        snapshot = create_snapshot(
            source_database,
            tmp_path / "private" / "snapshot.db",
            run_id=RUN_ID,
        )
    finally:
        os.umask(previous)
    assert snapshot.path.stat().st_mode & 0o777 == 0o600
    assert snapshot.path.parent.stat().st_mode & 0o777 == 0o700


def test_post_link_directory_fsync_failure_rolls_back_publication(
    source_database: SqliteDatabase,
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "durability" / "snapshot.db"
    real_fsync = os.fsync
    calls = 0

    def fail_directory_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_once)
    with pytest.raises(OSError, match="injected"):
        create_snapshot(source_database, destination, run_id=RUN_ID)
    assert not destination.exists()
    assert list(destination.parent.glob(".*.tmp")) == []


def test_directory_fsync_and_publish_rollback_failure_never_reports_success(
    source_database: SqliteDatabase,
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "double-failure" / "snapshot.db"
    real_fsync = os.fsync
    real_unlink = Path.unlink
    fsync_calls = 0

    def fail_both_directory_fsyncs(fd: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls in {2, 3}:
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    def fail_final_unlink(path: Path, *args, **kwargs):
        if path == destination:
            raise OSError("injected final unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fail_both_directory_fsyncs)
    monkeypatch.setattr(Path, "unlink", fail_final_unlink)
    with pytest.raises(OSError, match="directory fsync"):
        create_snapshot(source_database, destination, run_id=RUN_ID)
    assert destination.exists()
    assert fsync_calls >= 3


def test_post_link_temp_cleanup_failure_returns_valid_snapshot(
    source_database: SqliteDatabase,
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "cleanup" / "snapshot.db"
    real_unlink = Path.unlink
    failed = False

    def fail_once(path: Path, *args, **kwargs):
        nonlocal failed
        if path.name.endswith(".tmp") and not failed:
            failed = True
            raise OSError("injected cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    snapshot = create_snapshot(source_database, destination, run_id=RUN_ID)
    assert snapshot.path == destination and destination.exists()
    assert list(destination.parent.glob(".*.tmp")) == []


def test_strict_transform_covers_json_boolean_timestamp_blob_text_and_path():
    spec = next(item for item in MANIFEST.replicated if item.name == "sources")
    columns = (
        PostgresColumn("json_value", "jsonb", False),
        PostgresColumn("flag", "boolean", False),
        PostgresColumn("created_at", "timestamp with time zone", False),
        PostgresColumn("blob", "bytea", False),
        PostgresColumn("title", "text", False),
        PostgresColumn("file_path", "text", False),
    )
    row = {
        "json_value": '{"z":null,"a":[true,2]}',
        "flag": 1,
        "created_at": "2026-07-24 09:30:00",
        "blob": memoryview(b"\x00\xff"),
        "title": "混合 English\n" + "Markdown" * 10_000,
        "file_path": "nested/来源.md",
    }
    transformed = transform_sqlite_row(spec, columns, row)
    assert isinstance(transformed[0], Jsonb)
    assert transformed[0].obj == {"a": [True, Decimal("2")], "z": None}
    assert transformed[1] is True
    assert transformed[2] == datetime(2026, 7, 24, 9, 30, tzinfo=timezone.utc)
    assert transformed[3] == b"\x00\xff"
    assert transformed[4] == row["title"]
    assert transformed[5] == row["file_path"]
    assert canonical_json_value("null") is None
    with pytest.raises(ValueError, match="invalid"):
        canonical_json_value(json.dumps(float("nan")))
    numeric = transform_sqlite_row(
        spec,
        (
            PostgresColumn("as_float", "double precision", False),
            PostgresColumn("as_numeric", "numeric", False),
        ),
        {"as_float": 0, "as_numeric": 0},
    )
    assert numeric == (0.0, Decimal("0"))


def test_exact_json_parser_and_local_dumps_preserve_numeric_tokens_and_escaping():
    raw = (
        '{"escaped":"quote \\" slash \\\\ newline\\n 中文",'
        '"huge":1e400,"negative_zero":-0,'
        '"nested":[true,null,{"precise":0.12345678901234567890123456789}]}'
    )
    parsed = canonical_json_value(raw)
    assert parsed["huge"] == Decimal("1e400")
    assert parsed["nested"][2]["precise"] == Decimal("0.12345678901234567890123456789")
    rendered = exact_json_dumps(parsed)
    assert "1E+400" in rendered
    assert '"precise":0.12345678901234567890123456789' in rendered
    assert '"precise":"0.12345678901234567890123456789"' not in rendered
    assert (
        json.loads(
            rendered,
            parse_float=Decimal,
            parse_int=Decimal,
        )
        == parsed
    )
    with pytest.raises(ValueError, match="non-finite"):
        exact_json_dumps({"bad": Decimal("NaN")})
    with pytest.raises(ValueError, match="unsupported"):
        exact_json_dumps({"bad": 1.5})
    with pytest.raises(ValueError, match="SQLite JSON value is invalid"):
        canonical_json_value("1e999999999999999999999999999999999999999")

    spec = next(item for item in MANIFEST.replicated if item.name == "notebooks")
    wrapped = transform_sqlite_row(
        spec,
        (PostgresColumn("expected_questions", "jsonb", False),),
        {"expected_questions": raw},
    )[0]
    assert isinstance(wrapped, Jsonb)
    assert wrapped.dumps is exact_json_dumps
    assert '"huge":"' not in wrapped.dumps(wrapped.obj)


def test_empty_time_sentinel_becomes_sql_null_but_json_null_stays_json():
    ask_spec = next(item for item in MANIFEST.replicated if item.name == "ask_jobs")
    values = transform_sqlite_row(
        ask_spec,
        (
            PostgresColumn("trace_json", "jsonb", False),
            PostgresColumn("created_at", "timestamp with time zone", True),
        ),
        {"trace_json": "null", "created_at": ""},
    )
    assert isinstance(values[0], Jsonb) and values[0].obj is None
    assert values[1] is None


def test_frozen_v9_fixture_upgrades_snapshots_and_converts_only_legacy_text_vectors(
    tmp_path: Path,
):
    fixture = REPO_ROOT / "backend/tests/fixtures/repository_v9/baseline.db"
    copied = tmp_path / "historical.db"
    shutil.copy2(fixture, copied)
    settings = Settings(database_url=f"sqlite:///{copied}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    conn = database.connect()
    install_sqlite_capture(conn, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    conn.execute("UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1")
    conn.commit()
    try:
        snapshot = create_snapshot(
            database,
            tmp_path / "historical-snapshot.db",
            run_id=RUN_ID,
        )
    finally:
        database.close_local()
    historical = sqlite3.connect(snapshot.path)
    historical.row_factory = sqlite3.Row
    try:
        contract = json.loads(
            (
                REPO_ROOT / "backend/tests/fixtures/postgres_schema_contract.json"
            ).read_text(encoding="utf-8")
        )
        assert set(contract["tables"]) == set(MANIFEST.replicated_names)
        transformed_rows = 0
        for table, facts in contract["tables"].items():
            spec = next(item for item in MANIFEST.replicated if item.name == table)
            columns = tuple(
                PostgresColumn(
                    str(column["name"]),
                    str(column["postgres_type"]),
                    bool(column["nullable"]),
                )
                for column in facts["columns"]
            )
            selected = ",".join(f'"{column.name}"' for column in columns)
            if table in set(contract["ordinal_tables"]):
                selected += ",rowid AS ordinal"
                columns += (PostgresColumn("ordinal", "bigint", False),)
            rows = historical.execute(f'SELECT {selected} FROM "{table}"').fetchall()
            for source_row in rows:
                converted = transform_sqlite_row(spec, columns, source_row)
                assert len(converted) == len(columns)
                transformed_rows += 1
        assert transformed_rows > 0

        row = historical.execute(
            "SELECT * FROM element_embeddings WHERE typeof(vector)='text' LIMIT 1"
        ).fetchone()
        assert row is not None
        spec = next(
            item for item in MANIFEST.replicated if item.name == "element_embeddings"
        )
        value = transform_sqlite_row(
            spec,
            (PostgresColumn("vector", "bytea", False),),
            {"vector": row["vector"]},
        )[0]
        assert value == struct.pack("<4f", 1.0, 0.0, 0.5, 0.25)
        blob = historical.execute(
            "SELECT vector FROM element_embeddings WHERE typeof(vector)='blob' LIMIT 1"
        ).fetchone()[0]
        assert (
            transform_sqlite_row(
                spec,
                (PostgresColumn("vector", "bytea", False),),
                {"vector": blob},
            )[0]
            is blob
        )
    finally:
        historical.close()
