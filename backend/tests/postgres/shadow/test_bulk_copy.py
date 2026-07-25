from __future__ import annotations

import inspect
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import sql as pg_sql
from psycopg.types.json import Jsonb
import pytest

from app.core.config import Settings
from app.migration.shadow import bulk_copy
from app.migration.shadow import control
from app.migration.shadow import postgres_catalog
from app.migration.shadow.bulk_copy import copy_snapshot
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.control import (
    BackupEvidence,
    DiskEvidence,
    ShadowPhase,
    commit_guard_report,
    committed_sqlite_guard_report,
    create_or_reuse_run,
    enable_sqlite_capture_after_guard_reports,
    get_run,
    install_postgres_control_schema,
    preflight,
    prepare_postgres_replication_key_guards,
    update_forward_state,
)
from app.migration.shadow.manifest import MANIFEST, replication_guard_specs
from app.migration.shadow.snapshot import create_snapshot
from app.migration.shadow.snapshot import SnapshotInfo
from app.migration.shadow.transform import PostgresColumn
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


REPO_ROOT = Path(__file__).resolve().parents[4]
RUN_ID = "bulk-copy-integration-1"
SECRET = b"bulk-copy-confirmation-secret-32bytes"
_POSTGRES_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def test_copy_contract_is_bounded_non_destructive_and_uses_postgres_copy():
    source = inspect.getsource(bulk_copy)
    assert ".fetchmany(batch_rows)" in source
    assert "COPY " in source and " FROM STDIN" in source
    assert 'EXECUTE("TRUNCATE' not in source.upper()
    assert 'EXECUTE("DELETE FROM' not in source.upper()
    assert "array(" not in source.lower()
    assert len(POSTGRES_ROWID_ORDINAL_TABLES) == 7
    assert set(POSTGRES_ROWID_ORDINAL_TABLES) <= set(MANIFEST.replicated_names)
    finalizer = inspect.getsource(bulk_copy._finalize_copy)
    assert "_final_write_with_live_fence" in finalizer
    assert "retain_live_fence" in finalizer and "_begin_live_source_fence" in finalizer
    assert finalizer.index("_lock_copy_catalog_tables") < finalizer.index(
        "_lock_identity_sequences"
    )
    assert finalizer.index("_lock_identity_sequences") < finalizer.index(
        "_validate_full_copy_target_catalog"
    )
    assert finalizer.index("_validate_full_copy_target_catalog") < finalizer.index(
        "_begin_live_source_fence"
    )
    assert finalizer.index("_reseed_and_verify_identity_sequences") < finalizer.index(
        "_begin_live_source_fence"
    )
    copier = inspect.getsource(bulk_copy.copy_snapshot)
    live_fence = inspect.getsource(bulk_copy._begin_live_source_fence)
    assert copier.count("_sqlite_schema_contract(") == 1
    assert "_open_snapshot" not in live_fence
    assert live_fence.count("_sqlite_schema_contract(") == 1
    assert "_sqlite_schema_contract(" not in finalizer


def test_sequence_reseed_uses_catalog_dependency_and_bound_oid_not_identifier():
    source = inspect.getsource(bulk_copy._reseed_ordinal)
    assert "pg_depend" in source
    assert "sequence_oid" in source
    assert "%s::oid::regclass" in source
    assert "setval" in source
    assert "nextval" not in source


def test_fk_trigger_validator_rejects_a_disabled_internal_trigger():
    rows: list[dict[str, object]] = []
    expected = {
        name: contract
        for name, contract in postgres_catalog.EXPECTED_CONSTRAINTS.items()
        if contract.kind == "f"
    }
    for constraint_oid, (name, contract) in enumerate(expected.items(), start=1):
        assert contract.referenced_table is not None
        for trigger_table in (
            contract.table,
            contract.table,
            contract.referenced_table,
            contract.referenced_table,
        ):
            rows.append(
                {
                    "trigger_oid": len(rows) + 1,
                    "tgenabled": "O",
                    "tgisinternal": True,
                    "constraint_oid": constraint_oid,
                    "conname": name,
                    "constraint_table": contract.table,
                    "constraint_schema": "business",
                    "referenced_table": contract.referenced_table,
                    "referenced_schema": "business",
                    "trigger_table": trigger_table,
                }
            )
    rows[0]["tgenabled"] = "D"
    conn = SimpleNamespace(
        execute=lambda *_args, **_kwargs: SimpleNamespace(fetchall=lambda: rows)
    )

    with pytest.raises(ValueError, match="foreign-key trigger contract drifted"):
        postgres_catalog._validate_fk_constraint_triggers(
            conn,
            business_schema="business",
            business_tables=MANIFEST.replicated_names,
        )


def test_qualified_accepts_run_bound_quoted_schema_but_not_untrusted_table():
    assert bulk_copy._qualified("Team-DB", "app_settings").as_string() == (
        '"Team-DB"."app_settings"'
    )
    assert bulk_copy._qualified("MixedCase", "app_settings").as_string() == (
        '"MixedCase"."app_settings"'
    )
    with pytest.raises(ValueError, match="unsafe schema"):
        bulk_copy._qualified("Team-DB", "app-settings")


def test_expression_contract_preserves_semantic_operators_and_pg_deparse():
    canonical = postgres_catalog._canonical_expression
    expected_check = canonical("status IN ('active','revoked')", context="check")
    pg_check = canonical(
        "CHECK ((status = ANY (ARRAY['active'::text, 'revoked'::text])))",
        context="check",
    )
    assert expected_check == pg_check
    assert expected_check != canonical(
        "status NOT IN ('active','revoked')", context="check"
    )
    assert canonical("source_answer_id IS NOT NULL", context="predicate") != canonical(
        "source_answer_id IS NULL", context="predicate"
    )
    assert canonical("left_value || right_value", context="index_key") != canonical(
        "left_value = right_value", context="index_key"
    )
    assert canonical("dimension > 0", context="check") != canonical(
        "dimension::smallint > 0", context="check"
    )
    assert canonical("dimension1 > 0", context="check") != canonical(
        "dimension1::smallint > 0", context="check"
    )
    assert canonical("revoked_at", context="index_key") != canonical(
        "revoked_at::date", context="index_key"
    )
    assert canonical("status = 'Active'", context="check") != canonical(
        "status = 'active'", context="check"
    )
    assert canonical('"Status" = 1', context="check") != canonical(
        '"status" = 1', context="check"
    )
    assert canonical("public.btrim(content_md)", context="index_key") != canonical(
        "btrim(content_md)", context="index_key"
    )


def test_jsonb_proof_canonicalizes_json_numbers_without_merging_sql_types():
    json_column = (PostgresColumn("payload", "jsonb", False),)
    source = Jsonb(
        {
            "large": 1e20,
            "small": 1.25e-7,
            "negative_zero": -0.0,
            "huge_integer": 123456789012345678901234567890123456789,
            "nested": [True, {"value": 10.5000}],
        }
    )
    target = {
        "large": Decimal("100000000000000000000"),
        "small": Decimal("0.000000125"),
        "negative_zero": Decimal("0"),
        "huge_integer": Decimal("123456789012345678901234567890123456789"),
        "nested": [True, {"value": Decimal("10.5")}],
    }
    assert bulk_copy._canonical_bytes((source,), json_column) == (
        bulk_copy._canonical_bytes((target,), json_column)
    )
    drifted = dict(target, small=Decimal("0.000000126"))
    assert bulk_copy._canonical_bytes((source,), json_column) != (
        bulk_copy._canonical_bytes((drifted,), json_column)
    )
    assert bulk_copy._canonical_bytes((Jsonb(Decimal("1")),), json_column) != (
        bulk_copy._canonical_bytes(
            (Jsonb({"$json_number": "1"}),),
            json_column,
        )
    )
    assert bulk_copy._canonical_bytes((None,), json_column) != (
        bulk_copy._canonical_bytes((Jsonb(None),), json_column)
    )
    assert (
        len(
            bulk_copy._canonical_bytes(
                (Jsonb(Decimal("1e999999999")),),
                json_column,
            )
        )
        < 100
    )
    assert bulk_copy._canonical_bytes(
        (1,), (PostgresColumn("value", "bigint", False),)
    ) != bulk_copy._canonical_bytes(
        (1.0,), (PostgresColumn("value", "double precision", False),)
    )


def test_column_contract_requires_builtin_collation_and_no_domain_or_generation():
    expected = postgres_catalog.ColumnContract(
        "text",
        False,
        "NO",
        "C",
        (),
        identity_cycle="NO",
        collation_schema="pg_catalog",
    )
    assert expected != postgres_catalog.ColumnContract(
        "text",
        False,
        "NO",
        "C",
        (),
        identity_cycle="NO",
        collation_schema="public",
    )
    assert expected != postgres_catalog.ColumnContract(
        "text",
        False,
        "NO",
        "C",
        (),
        identity_cycle="NO",
        domain_schema="public",
        domain_name="shadow_text",
        collation_schema="pg_catalog",
    )


def test_identity_contract_is_exact_for_seven_ordinals_and_ordinary_columns():
    identities = {
        table
        for table, columns in postgres_catalog.EXPECTED_COLUMNS.items()
        if any(column.is_identity == "YES" for column in columns.values())
    }
    assert identities == set(POSTGRES_ROWID_ORDINAL_TABLES)
    for table in identities:
        ordinal = postgres_catalog.EXPECTED_COLUMNS[table]["ordinal"]
        assert ordinal.data_type == "bigint"
        assert (
            ordinal.is_identity,
            ordinal.identity_generation,
            ordinal.identity_start,
            ordinal.identity_increment,
            ordinal.identity_minimum,
            ordinal.identity_maximum,
            ordinal.identity_cycle,
        ) == (
            "YES",
            "BY DEFAULT",
            "1",
            "1",
            "1",
            "9223372036854775807",
            "NO",
        )
    ordinary = postgres_catalog.EXPECTED_COLUMNS["ask_trace_steps"]["seq"]
    assert ordinary.is_identity == "NO"
    assert ordinary.identity_generation is None
    assert ordinary.identity_start is None
    assert ordinary.identity_increment is None
    assert ordinary.identity_minimum is None
    assert ordinary.identity_maximum is None
    assert ordinary.identity_cycle == "NO"


def test_exact_catalog_flag_helpers_reject_table_and_index_semantic_drift():
    table = {
        "relkind": "r",
        "relpersistence": "p",
        "owner": "migration_role",
        "expected_owner": "migration_role",
        "relacl": None,
        "relrowsecurity": False,
        "relforcerowsecurity": False,
        "relispartition": False,
    }
    assert postgres_catalog._table_semantics_are_exact(table)
    for field, value in (
        ("relkind", "p"),
        ("relpersistence", "u"),
        ("owner", "other"),
        ("relacl", ["=r/migration_role"]),
        ("relrowsecurity", True),
        ("relforcerowsecurity", True),
        ("relispartition", True),
    ):
        drifted = table | {field: value}
        assert not postgres_catalog._table_semantics_are_exact(drifted)
    assert postgres_catalog._index_flags_are_live(
        {"indisvalid": True, "indisready": True, "indislive": True}
    )
    for field in ("indisvalid", "indisready", "indislive"):
        flags = {"indisvalid": True, "indisready": True, "indislive": True}
        flags[field] = False
        assert not postgres_catalog._index_flags_are_live(flags)


def test_expected_index_contract_covers_constraints_guards_and_null_semantics():
    indexes = postgres_catalog._expected_all_indexes(MANIFEST)
    assert indexes["pk_answers"] == postgres_catalog.IndexContract(
        "answers", True, "btree", (("id", "collate", '"C"'),), ()
    )
    guard = next(iter(replication_guard_specs(MANIFEST)))
    assert indexes[guard.name] == postgres_catalog.IndexContract(
        guard.table,
        True,
        "btree",
        postgres_catalog._plain_index_keys(guard.table, guard.columns),
        (),
    )
    assert not indexes["pk_answers"].nulls_not_distinct
    assert indexes["pk_answers"] != postgres_catalog.IndexContract(
        "answers",
        True,
        "btree",
        (("id", "collate", '"C"'),),
        (),
        nulls_not_distinct=True,
    )


def test_snapshot_metadata_and_copy_progress_reject_bool_counters(tmp_path: Path):
    snapshot = SnapshotInfo(
        path=(tmp_path / "snapshot.db").resolve(),
        run_id=RUN_ID,
        source_identity_hash="a" * 64,
        schema_epoch=True,
        baseline_seq=0,
        size_bytes=1,
        sha256="b" * 64,
    )
    with pytest.raises(ValueError, match="epoch"):
        bulk_copy._validate_snapshot_info(snapshot, MANIFEST)
    with pytest.raises(ValueError, match="counters"):
        bulk_copy._decode_progress(
            {
                "last_key_json": None,
                "rows_copied": True,
                "bytes_copied": 0,
                "rolling_hash": None,
                "complete": False,
            }
        )


def test_live_source_fence_rejects_disabled_capture(source_database):
    database, _source_url, _storage = source_database
    conn = database.connect()
    install_sqlite_capture(conn, run_id=RUN_ID, schema_epoch=1, manifest=MANIFEST)
    conn.execute("UPDATE shadow_capture_control SET enabled=0 WHERE singleton=1")
    conn.commit()
    identity = bulk_copy.fingerprint_sqlite(database.settings.database_url, conn)
    snapshot = SimpleNamespace(
        source_identity_hash=identity.identity_hash,
        run_id=RUN_ID,
        schema_epoch=1,
        path=database.db_path,
    )
    with pytest.raises(ValueError, match="not enabled"):
        bulk_copy._validate_live_source(
            database,
            snapshot,
            MANIFEST,
            expected_schema_contract=bulk_copy._sqlite_schema_contract(conn, MANIFEST),
        )


def test_live_source_fence_closes_fresh_connection_when_begin_fails(monkeypatch):
    class BeginFailureConnection:
        in_transaction = False
        closed = False

        def execute(self, statement, _parameters=()):
            assert statement == "BEGIN IMMEDIATE"
            raise sqlite3.OperationalError("injected begin failure")

        def close(self):
            self.closed = True

    connection = BeginFailureConnection()
    monkeypatch.setattr(
        bulk_copy,
        "open_fresh_live_sqlite",
        lambda _source: (connection, SimpleNamespace()),
    )
    source = SimpleNamespace(
        settings=SimpleNamespace(database_url="sqlite:////tmp/not-opened.db")
    )
    with pytest.raises(sqlite3.OperationalError, match="begin failure"):
        bulk_copy._begin_live_source_fence(
            source,
            SimpleNamespace(),
            MANIFEST,
            expected_schema_contract=(),
        )
    assert connection.closed


def test_live_source_fence_does_not_scan_business_key_rows(source_database):
    database, _source_url, _storage = source_database
    conn = database.connect()
    install_sqlite_capture(
        conn,
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    conn.execute("UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1")
    conn.commit()
    identity = bulk_copy.fingerprint_sqlite(database.settings.database_url, conn)
    snapshot = SimpleNamespace(
        source_identity_hash=identity.identity_hash,
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        path=database.db_path,
    )
    expected_schema_contract = bulk_copy._sqlite_schema_contract(conn, MANIFEST)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        bulk_copy._validate_live_source(
            database,
            snapshot,
            MANIFEST,
            expected_schema_contract=expected_schema_contract,
        )
    finally:
        conn.set_trace_callback(None)

    normalized = [" ".join(statement.split()).upper() for statement in statements]
    business_tables = {name.upper() for name in MANIFEST.replicated_names}
    assert not any(" GROUP BY " in statement for statement in normalized)
    assert not any(
        statement.startswith("SELECT ")
        and any(
            f' FROM "{table}"' in statement or f" FROM {table}" in statement
            for table in business_tables
        )
        for statement in normalized
    )


def test_live_source_fence_rejects_post_snapshot_column_contract_drift(
    source_database,
    tmp_path: Path,
):
    database, _source_url, _storage = source_database
    install_sqlite_capture(
        database.connect(),
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    database.connect().execute(
        "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1"
    )
    database.connect().commit()
    snapshot = create_snapshot(
        database,
        tmp_path / "schema-contract" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    snapshot_conn = sqlite3.connect(snapshot.path)
    try:
        snapshot_schema_contract = bulk_copy._sqlite_schema_contract(
            snapshot_conn, MANIFEST
        )
    finally:
        snapshot_conn.close()
    database.connect().execute(
        "ALTER TABLE answers ADD COLUMN future_payload TEXT DEFAULT 'future'"
    )
    database.connect().commit()
    with pytest.raises(ValueError, match="column contract drifted"):
        bulk_copy._validate_live_source(
            database,
            snapshot,
            MANIFEST,
            expected_schema_contract=snapshot_schema_contract,
        )


def test_live_source_fence_uses_current_file_after_cached_connection_replace(
    source_database,
    tmp_path: Path,
):
    database, _source_url, _storage = source_database
    install_sqlite_capture(
        database.connect(),
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    database.connect().execute(
        "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1"
    )
    database.connect().commit()
    snapshot = create_snapshot(
        database,
        tmp_path / "replace-fence" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    snapshot_conn = sqlite3.connect(snapshot.path)
    try:
        snapshot_contract = bulk_copy._sqlite_schema_contract(snapshot_conn, MANIFEST)
    finally:
        snapshot_conn.close()
    cached = _atomically_replace_live_database(
        database,
        tmp_path / "live-replacement.db",
        run_id="replacement-run",
    )
    assert (
        cached.execute(
            "SELECT run_id FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()[0]
        == RUN_ID
    )
    with pytest.raises(ValueError, match="not enabled and safe"):
        bulk_copy._validate_live_source(
            database,
            snapshot,
            MANIFEST,
            expected_schema_contract=snapshot_contract,
        )


def test_copy_post_integrity_key_scan_cancel_clears_handler_without_h0(
    source_database,
    tmp_path: Path,
    monkeypatch,
):
    database, _source_url, _storage = source_database
    install_sqlite_capture(
        database.connect(),
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    database.connect().execute(
        "UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1"
    )
    database.connect().executemany(
        "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
        [
            (f"copy-cancel-{index}", "value", "2026-07-24 00:00:00")
            for index in range(2_500)
        ],
    )
    database.connect().commit()
    snapshot = create_snapshot(
        database,
        tmp_path / "integrity-cancel" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
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

    uri = snapshot.path.resolve(strict=True).as_uri() + "?mode=ro"
    tracked = sqlite3.connect(uri, uri=True, factory=TrackingConnection)
    tracked.row_factory = sqlite3.Row
    monkeypatch.setattr(bulk_copy, "_open_snapshot", lambda *_args: tracked)

    class Target:
        writes = 0

        @contextmanager
        def write(self):
            self.writes += 1
            raise AssertionError("H0 target transaction must not start")
            yield

    target = Target()
    with pytest.raises(bulk_copy.CopyCancelled):
        copy_snapshot(
            snapshot,
            database,
            target,
            MANIFEST,
            cancel_requested=lambda: (
                TrackingConnection.integrity_finished
                and TrackingConnection.replication_scan
            ),
        )
    assert target.writes == 0
    assert TrackingConnection.calls[0] == (True, 1_000)
    assert TrackingConnection.calls[-1] == (False, 0)
    assert TrackingConnection.integrity_finished
    assert TrackingConnection.replication_scan


@pytest.mark.parametrize("fail_pg_commit", [False, True])
def test_final_live_fence_spans_pg_commit_and_releases_on_failure(
    source_database,
    fail_pg_commit: bool,
):
    database, _source_url, _storage = source_database
    source_conn = database.connect()
    install_sqlite_capture(
        source_conn,
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    source_conn.execute("UPDATE shadow_capture_control SET enabled=1 WHERE singleton=1")
    source_conn.commit()
    identity = bulk_copy.fingerprint_sqlite(database.settings.database_url, source_conn)
    snapshot = SimpleNamespace(
        source_identity_hash=identity.identity_hash,
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
    )

    def try_disable() -> bool:
        contender = sqlite3.connect(database.db_path, timeout=0)
        try:
            contender.execute(
                "UPDATE shadow_capture_control SET enabled=0 WHERE singleton=1"
            )
            contender.commit()
            return True
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            contender.rollback()
            return False
        finally:
            contender.close()

    class PgConnection:
        h0_pending = False

    class Target:
        h0_committed = False
        blocked_at_commit = False

        @contextmanager
        def write(self):
            connection = PgConnection()
            yield connection
            self.blocked_at_commit = not try_disable()
            if fail_pg_commit:
                raise RuntimeError("injected PG commit failure")
            self.h0_committed = connection.h0_pending

    target = Target()

    def operation() -> None:
        with bulk_copy._final_write_with_live_fence(target) as (
            pg_conn,
            retain_live_fence,
        ):
            retain_live_fence(
                bulk_copy._begin_live_source_fence(
                    database,
                    snapshot,
                    MANIFEST,
                    expected_schema_contract=bulk_copy._sqlite_schema_contract(
                        source_conn, MANIFEST
                    ),
                )
            )
            assert not try_disable()
            pg_conn.h0_pending = True

    if fail_pg_commit:
        with pytest.raises(RuntimeError, match="commit failure"):
            operation()
    else:
        operation()
    assert target.blocked_at_commit
    assert target.h0_committed is not fail_pg_commit
    # Both PG success and failure release the SQLite lease after the PG
    # context has committed or rolled back.
    assert try_disable()


def test_final_live_path_recheck_runs_before_pg_commit(monkeypatch):
    class SqliteConnection:
        in_transaction = True
        rolled_back = False
        closed = False

        def rollback(self):
            self.rolled_back = True
            self.in_transaction = False

        def commit(self):  # pragma: no cover - forbidden by assertion failure
            raise AssertionError("SQLite fence must not commit")

        def close(self):
            self.closed = True

    class Target:
        committed = False

        @contextmanager
        def write(self):
            yield object()
            self.committed = True

    sqlite_conn = SqliteConnection()
    fence = bulk_copy.LiveSourceFence(
        sqlite_conn,
        SimpleNamespace(),
        SimpleNamespace(),
    )
    checks = 0

    def reject_replaced_path(_source, _binding):
        nonlocal checks
        checks += 1
        raise ValueError("live SQLite source path changed")

    monkeypatch.setattr(bulk_copy, "assert_live_sqlite_path", reject_replaced_path)
    target = Target()
    with pytest.raises(ValueError, match="path changed"):
        with bulk_copy._final_write_with_live_fence(target) as (
            _pg_conn,
            retain_live_fence,
        ):
            retain_live_fence(fence)
    assert checks == 1
    assert not target.committed
    assert sqlite_conn.rolled_back and sqlite_conn.closed


class _TrackingCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.offset = 0
        self.requests: list[int] = []
        self.closed = False

    def fetchmany(self, size):
        self.requests.append(size)
        result = self.rows[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    def fetchone(self):
        rows = self.fetchmany(1)
        return rows[0] if rows else None

    def execute(self, _statement):
        return self

    def close(self):
        self.closed = True


class _PrefixTarget:
    def __init__(self, rows):
        self.rows = rows
        self.target_cursor = _TrackingCursor(rows)

    def execute(self, sql, _params=()):
        return _TrackingCursor([{"count": len(self.rows)}])

    def cursor(self, *, name):
        assert name.startswith("shadow_copy_")
        return self.target_cursor


def test_resume_proof_streams_target_and_snapshot_within_batch_bound():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.execute(
        "CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
    )
    source.executemany(
        "INSERT INTO app_settings VALUES (?,?,?)",
        [(f"k-{index}", f"v-{index}", "2026-07-24 00:00:00") for index in range(7)],
    )
    spec = next(item for item in MANIFEST.replicated if item.name == "app_settings")
    columns = (
        PostgresColumn("key", "text", False),
        PostgresColumn("value", "text", False),
        PostgresColumn("updated_at", "timestamp with time zone", False),
    )
    batches = []
    last_key = None
    rolling = "0" * 64
    copied_bytes = 0
    while len(batches) < 7:
        rows = bulk_copy._snapshot_batch(
            source,
            spec,
            ("key", "value", "updated_at"),
            columns,
            last_key=last_key,
            batch_rows=3,
        )
        if not rows:
            break
        batches.extend(rows)
        last_key = list(rows[-1][0])
        for _key, values in rows:
            rolling, size = bulk_copy._advance_hash(rolling, values)
            copied_bytes += size
    target_rows = [
        {column.name: values[position] for position, column in enumerate(columns)}
        for _key, values in batches
    ]
    target = _PrefixTarget(target_rows)
    bulk_copy._validate_target_prefix(
        target,
        source,
        spec,
        ("key", "value", "updated_at"),
        columns,
        (last_key, 7, copied_bytes, rolling, False),
        business_schema="public",
        batch_rows=3,
    )
    assert max(target.target_cursor.requests) <= 3
    assert target.target_cursor.itersize == 3
    assert target.target_cursor.arraysize == 3
    assert target.target_cursor.closed


def test_resume_proof_closes_server_cursor_on_cancellation():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.execute(
        "CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT)"
    )
    source.execute("INSERT INTO app_settings VALUES ('k','v','2026-07-24 00:00:00')")
    spec = next(item for item in MANIFEST.replicated if item.name == "app_settings")
    columns = (
        PostgresColumn("key", "text", False),
        PostgresColumn("value", "text", False),
        PostgresColumn("updated_at", "timestamp with time zone", False),
    )
    values = bulk_copy._snapshot_batch(
        source,
        spec,
        ("key", "value", "updated_at"),
        columns,
        last_key=None,
        batch_rows=1,
    )[0][1]
    rolling, copied_bytes = bulk_copy._advance_hash("0" * 64, values)
    target = _PrefixTarget(
        [{column.name: values[index] for index, column in enumerate(columns)}]
    )
    polls = 0

    def cancel() -> bool:
        nonlocal polls
        polls += 1
        return polls > 1

    with pytest.raises(bulk_copy.CopyCancelled):
        bulk_copy._validate_target_prefix(
            target,
            source,
            spec,
            ("key", "value", "updated_at"),
            columns,
            (["k"], 1, copied_bytes, rolling, False),
            business_schema="public",
            batch_rows=1,
            cancel_requested=cancel,
        )
    assert target.target_cursor.closed


def test_all_ordinal_snapshot_rows_copy_the_explicit_sqlite_rowid():
    for table in POSTGRES_ROWID_ORDINAL_TABLES:
        source = sqlite3.connect(":memory:")
        source.row_factory = sqlite3.Row
        source.execute(f'CREATE TABLE "{table}"(id TEXT PRIMARY KEY)')
        source.execute(
            f'INSERT INTO "{table}"(rowid,id) VALUES (41,?)', (f"{table}-id",)
        )
        spec = next(item for item in MANIFEST.replicated if item.name == table)
        rows = bulk_copy._snapshot_batch(
            source,
            spec,
            ("id",),
            (
                PostgresColumn("id", "text", False),
                PostgresColumn("ordinal", "bigint", False),
            ),
            last_key=None,
            batch_rows=1,
        )
        assert rows == [((f"{table}-id",), (f"{table}-id", 41))]
        source.close()


def test_reseed_failure_cannot_mark_table_complete(monkeypatch):
    spec = next(item for item in MANIFEST.replicated if item.name == "answers")
    checkpoint = (["answer-1"], 1, 10, "a" * 64, False)
    executed: list[str] = []

    class Connection:
        def execute(self, sql, _params=()):
            executed.append(sql)
            return object()

    class Target:
        @contextmanager
        def write(self):
            yield Connection()

    monkeypatch.setattr(
        bulk_copy,
        "_validate_copy_target",
        lambda *_a, **_k: SimpleNamespace(target_business_schema="public"),
    )
    monkeypatch.setattr(bulk_copy, "_set_statement_timeout", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_validate_live_source", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_bind_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_validate_target_prefix", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_snapshot_batch", lambda *_a, **_k: [])
    monkeypatch.setattr(bulk_copy, "_progress_row", lambda *_a, **_k: object())
    monkeypatch.setattr(bulk_copy, "_decode_progress", lambda _row: checkpoint)
    monkeypatch.setattr(
        bulk_copy,
        "_reseed_ordinal",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("reseed failed")),
    )
    with pytest.raises(RuntimeError, match="reseed failed"):
        bulk_copy._finish_table(
            Target(),
            SimpleNamespace(run_id=RUN_ID),
            MANIFEST,
            spec,
            checkpoint,
            sqlite_conn=object(),
            sqlite_columns=("id",),
            postgres_columns=(PostgresColumn("id", "text", False),),
            batch_rows=2,
            source=object(),
            snapshot_schema_contract=(),
            cancel_requested=None,
            statement_timeout_seconds=1,
        )
    assert len(executed) == 1


def test_completion_revalidates_prefix_inside_the_completion_transaction(monkeypatch):
    spec = next(item for item in MANIFEST.replicated if item.name == "app_settings")
    checkpoint = (["key"], 1, 10, "b" * 64, False)
    completed: list[str] = []

    class Connection:
        def execute(self, sql, _params=()):
            completed.append(sql)
            return object()

    class Target:
        @contextmanager
        def write(self):
            yield Connection()

    monkeypatch.setattr(
        bulk_copy,
        "_validate_copy_target",
        lambda *_a, **_k: SimpleNamespace(target_business_schema="public"),
    )
    monkeypatch.setattr(bulk_copy, "_set_statement_timeout", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_validate_live_source", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_bind_snapshot", lambda *_a, **_k: None)
    monkeypatch.setattr(bulk_copy, "_progress_row", lambda *_a, **_k: object())
    monkeypatch.setattr(bulk_copy, "_decode_progress", lambda _row: checkpoint)
    monkeypatch.setattr(
        bulk_copy,
        "_validate_target_prefix",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("target row drifted")),
    )
    with pytest.raises(ValueError, match="drifted"):
        bulk_copy._finish_table(
            Target(),
            SimpleNamespace(run_id=RUN_ID),
            MANIFEST,
            spec,
            checkpoint,
            sqlite_conn=object(),
            sqlite_columns=("key", "value", "updated_at"),
            postgres_columns=(
                PostgresColumn("key", "text", False),
                PostgresColumn("value", "text", False),
                PostgresColumn("updated_at", "timestamp with time zone", False),
            ),
            batch_rows=2,
            source=object(),
            snapshot_schema_contract=(),
            cancel_requested=None,
            statement_timeout_seconds=1,
        )
    assert len(completed) == 1


@pytest.fixture
def source_database(tmp_path: Path):
    path = tmp_path / "copy-source.db"
    storage = tmp_path / "storage"
    storage.mkdir()
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        yield database, settings.database_url, storage
    finally:
        database.close_local()


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
def clean_control_schema(postgres_scope):
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")
    try:
        yield
    finally:
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA IF EXISTS silicon_shadow CASCADE")


def _prepare_forward_run(source_database, postgres_database, *, poison_temp=False):
    database, source_url, storage = source_database
    source_conn = database.connect()
    report = preflight(
        run_id=RUN_ID,
        source_url=source_url,
        source_conn=source_conn,
        storage_root=storage,
        target=postgres_database,
        disk_evidence=DiskEvidence("copy-disk", 10_000_000_000, True),
        backup_evidence=BackupEvidence("copy-backup", True, True),
        confirmation_secret=SECRET,
        now=1_000,
    )
    assert PostgresMigrator(postgres_database).migrate() == 10
    install_postgres_control_schema(postgres_database)
    create_or_reuse_run(
        postgres_database,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_000,
    )
    if poison_temp:
        with postgres_database.write() as conn:
            conn.execute("CREATE TEMP TABLE community_members(level bigint)")
            conn.execute("CREATE TEMP TABLE kg_canonical_scratch(seed text)")
    install_sqlite_capture(
        source_conn,
        run_id=RUN_ID,
        schema_epoch=MANIFEST.schema_pair.epoch,
        manifest=MANIFEST,
    )
    sqlite_report = committed_sqlite_guard_report(
        source_conn,
        source_url=source_url,
        run_id=RUN_ID,
        manifest=MANIFEST,
    )
    postgres_report = prepare_postgres_replication_key_guards(
        postgres_database,
        manifest=MANIFEST,
        run_id=RUN_ID,
    )
    state = get_run(postgres_database, RUN_ID)
    state = commit_guard_report(
        postgres_database,
        report=sqlite_report,
        expected_revision=state.revision,
    )
    state = commit_guard_report(
        postgres_database,
        report=postgres_report,
        expected_revision=state.revision,
    )
    enable_sqlite_capture_after_guard_reports(
        source_conn,
        postgres_database,
        source_url=source_url,
        sqlite_report=sqlite_report,
        postgres_report=postgres_report,
        manifest=MANIFEST,
    )
    state = get_run(postgres_database, RUN_ID)
    update_forward_state(
        postgres_database,
        run_id=RUN_ID,
        expected_phase=ShadowPhase.OFF,
        expected_revision=state.revision,
        new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
        progress={"stage": "capture-enabled"},
        source_url=source_url,
        source_conn=source_conn,
    )
    return database


@pytest.mark.postgres_integration
def test_final_catalog_locks_block_concurrent_ledger_ddl_and_sequence_drift(
    postgres_database,
    postgres_scope,
):
    assert PostgresMigrator(postgres_database).migrate() == 10
    with postgres_database.write() as lock_conn:
        schema = str(
            lock_conn.execute("SELECT current_schema() AS name").fetchone()["name"]
        )
        ordered = bulk_copy._lock_copy_catalog_tables(
            lock_conn,
            business_schema=schema,
            manifest=MANIFEST,
        )
        sequences = bulk_copy._lock_identity_sequences(
            lock_conn,
            business_schema=schema,
            ordered_specs=ordered,
        )
        assert tuple(spec.copy_rank for spec in ordered) == tuple(
            sorted(spec.copy_rank for spec in MANIFEST.replicated)
        )
        assert len(sequences) == len(POSTGRES_ROWID_ORDINAL_TABLES)
        blocked_statements = (
            pg_sql.SQL(
                "UPDATE {}.silicon_schema_migrations SET checksum=checksum "
                "WHERE version=1"
            ).format(pg_sql.Identifier(schema)),
            pg_sql.SQL("ALTER TABLE {}.answers ADD COLUMN shadow_probe text").format(
                pg_sql.Identifier(schema)
            ),
            pg_sql.SQL("ALTER SEQUENCE {}.{} INCREMENT BY 2").format(
                pg_sql.Identifier(sequences[0][1]),
                pg_sql.Identifier(sequences[0][2]),
            ),
            pg_sql.SQL("SELECT setval({}::regclass,999,true)").format(
                pg_sql.Literal(f'"{sequences[0][1]}"."{sequences[0][2]}"')
            ),
        )
        for statement in blocked_statements:
            with psycopg.connect(postgres_scope.base_url) as contender:
                contender.execute("SET lock_timeout='100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    contender.execute(statement)
                contender.rollback()
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name='answers' "
                "AND column_name='shadow_probe'",
                (schema,),
            ).fetchone()
            is None
        )


@pytest.mark.postgres_integration
def test_final_reseed_repairs_state_and_rollback_cannot_restore_worse_drift(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    monkeypatch,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / "sequence-repair" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    original_lock = bulk_copy._lock_identity_sequences
    original_reseed = bulk_copy._reseed_and_verify_identity_sequences
    drifted = False
    failed_after_reseed = False

    def inject_drift(conn, *, business_schema, ordered_specs):
        nonlocal drifted
        if not drifted:
            conn.execute(
                pg_sql.SQL("SELECT setval({}::regclass,999,true)").format(
                    pg_sql.Literal(f'"{business_schema}"."answers_ordinal_seq"')
                )
            )
            drifted = True
        return original_lock(
            conn,
            business_schema=business_schema,
            ordered_specs=ordered_specs,
        )

    def fail_once_after_reseed(conn, *, business_schema, sequences):
        nonlocal failed_after_reseed
        original_reseed(
            conn,
            business_schema=business_schema,
            sequences=sequences,
        )
        if not failed_after_reseed:
            failed_after_reseed = True
            raise RuntimeError("injected after final reseed")

    monkeypatch.setattr(bulk_copy, "_lock_identity_sequences", inject_drift)
    monkeypatch.setattr(
        bulk_copy,
        "_reseed_and_verify_identity_sequences",
        fail_once_after_reseed,
    )
    with pytest.raises(RuntimeError, match="after final reseed"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    assert drifted and failed_after_reseed
    with postgres_database.connect() as conn:
        schema = str(conn.execute("SELECT current_schema() AS name").fetchone()["name"])
        state = conn.execute(
            pg_sql.SQL(
                "SELECT last_value,is_called FROM {}.answers_ordinal_seq"
            ).format(pg_sql.Identifier(schema))
        ).fetchone()
        assert (
            conn.execute(
                "SELECT 1 FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (RUN_ID,),
            ).fetchone()
            is None
        )
    # The transactional same-value ALTER rolls back to the exact pre-finalize
    # state. The failed attempt therefore leaves the drift unchanged, not
    # worse, and publishes no H0.
    assert (int(state["last_value"]), bool(state["is_called"])) == (999, True)

    report = copy_snapshot(
        snapshot, database, postgres_database, MANIFEST, batch_rows=2
    )
    assert report.baseline_seq == snapshot.baseline_seq
    with postgres_database.connect() as conn:
        state = conn.execute(
            pg_sql.SQL(
                "SELECT last_value,is_called FROM {}.answers_ordinal_seq"
            ).format(pg_sql.Identifier(schema))
        ).fetchone()
        h0 = conn.execute(
            "SELECT last_seq FROM silicon_shadow.checkpoints "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (RUN_ID,),
        ).fetchone()
        maximum = conn.execute(
            pg_sql.SQL("SELECT MAX(ordinal) AS maximum FROM {}.answers").format(
                pg_sql.Identifier(schema)
            )
        ).fetchone()["maximum"]
    expected_state = (1, False) if maximum is None else (int(maximum), True)
    assert (int(state["last_value"]), bool(state["is_called"])) == expected_state
    assert int(h0["last_seq"]) == snapshot.baseline_seq


@pytest.mark.postgres_integration
def test_final_live_fence_rejects_post_snapshot_schema_drift_without_h0(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    monkeypatch,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / "final-schema-drift" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    original_reseed = bulk_copy._reseed_and_verify_identity_sequences
    drifted = False

    def drift_after_final_proof(conn, *, business_schema, sequences):
        nonlocal drifted
        original_reseed(
            conn,
            business_schema=business_schema,
            sequences=sequences,
        )
        if not drifted:
            live = database.connect()
            live.execute("ALTER TABLE answers ADD COLUMN future_payload TEXT")
            live.commit()
            drifted = True

    monkeypatch.setattr(
        bulk_copy,
        "_reseed_and_verify_identity_sequences",
        drift_after_final_proof,
    )
    with pytest.raises(ValueError, match="column contract drifted"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    assert drifted
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (RUN_ID,),
            ).fetchone()
            is None
        )


@pytest.mark.postgres_integration
def test_final_live_fence_rejects_atomic_source_replace_without_h0(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    monkeypatch,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / "final-source-replace" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    original_reseed = bulk_copy._reseed_and_verify_identity_sequences
    replaced = False

    def replace_after_final_proof(conn, *, business_schema, sequences):
        nonlocal replaced
        original_reseed(
            conn,
            business_schema=business_schema,
            sequences=sequences,
        )
        if not replaced:
            _atomically_replace_live_database(
                database,
                tmp_path / "current-source-replacement.db",
                run_id="replacement-run",
            )
            replaced = True

    monkeypatch.setattr(
        bulk_copy,
        "_reseed_and_verify_identity_sequences",
        replace_after_final_proof,
    )
    with pytest.raises(ValueError, match="not enabled and safe"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    assert replaced
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (RUN_ID,),
            ).fetchone()
            is None
        )


@pytest.mark.postgres_integration
def test_snapshot_hash_open_atomic_replace_fails_without_h0(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    monkeypatch,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / "snapshot-open-race" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    replacement = tmp_path / "snapshot-open-replacement.db"
    shutil.copy2(snapshot.path, replacement)
    replacement_conn = sqlite3.connect(replacement)
    try:
        replacement_conn.execute("PRAGMA journal_mode=DELETE")
        replacement_conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
            ("unverified-replacement", "must-not-copy", "2026-07-24 00:00:00"),
        )
        replacement_conn.commit()
    finally:
        replacement_conn.close()
    original_hash = bulk_copy._sha256_file
    replaced = False

    def replace_after_hash(path, cancel_requested=None):
        nonlocal replaced
        digest = original_hash(path, cancel_requested)
        if Path(path) == snapshot.path and not replaced:
            os.replace(replacement, snapshot.path)
            replaced = True
        return digest

    monkeypatch.setattr(bulk_copy, "_sha256_file", replace_after_hash)
    with pytest.raises(ValueError, match="changed while opening"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    assert replaced
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT 1 FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (RUN_ID,),
            ).fetchone()
            is None
        )


@pytest.mark.postgres_integration
def test_snapshot_copy_end_to_end_resumes_and_reseeds_all_ordinals(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    monkeypatch,
):
    database = _prepare_forward_run(source_database, postgres_database)
    with database.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (?,?,?)",
            ("copy-non-ascii", "中文 English", "2026-07-24 00:00:00"),
        )
        allocated_h0 = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='shadow_change_log'"
        ).fetchone()[0]
        conn.execute("DELETE FROM shadow_change_log")
    snapshot = create_snapshot(
        database,
        tmp_path / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
        pages=1,
    )
    assert snapshot.baseline_seq == allocated_h0
    original_sequence_lock = bulk_copy._lock_identity_sequences
    state_drift_injected = False

    def inject_state_drift(conn, *, business_schema, ordered_specs):
        nonlocal state_drift_injected
        conn.execute(
            pg_sql.SQL("SELECT setval({}::regclass,999999,true)").format(
                pg_sql.Literal(f'"{business_schema}"."answers_ordinal_seq"')
            )
        )
        state_drift_injected = True
        return original_sequence_lock(
            conn,
            business_schema=business_schema,
            ordered_specs=ordered_specs,
        )

    monkeypatch.setattr(bulk_copy, "_lock_identity_sequences", inject_state_drift)
    report = copy_snapshot(
        snapshot, database, postgres_database, MANIFEST, batch_rows=2
    )
    assert state_drift_injected
    assert len(report.tables) == 60
    assert report.baseline_seq == snapshot.baseline_seq
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS count FROM pg_cursors WHERE name LIKE 'shadow_copy_%'"
            ).fetchone()["count"]
            == 0
        )
        before_repeat = conn.execute(
            "SELECT r.revision AS run_revision,c.revision AS checkpoint_revision "
            "FROM silicon_shadow.runs r JOIN silicon_shadow.checkpoints c ON c.run_id=r.run_id "
            "WHERE r.run_id=%s AND c.direction='sqlite_to_postgres'",
            (RUN_ID,),
        ).fetchone()
    # A complete rerun is an exact checkpoint verification, not a truncate.
    assert (
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=3)
        == report
    )
    with postgres_database.connect() as conn:
        after_repeat = conn.execute(
            "SELECT r.revision AS run_revision,c.revision AS checkpoint_revision "
            "FROM silicon_shadow.runs r JOIN silicon_shadow.checkpoints c ON c.run_id=r.run_id "
            "WHERE r.run_id=%s AND c.direction='sqlite_to_postgres'",
            (RUN_ID,),
        ).fetchone()
        assert after_repeat == before_repeat
        assert (
            conn.execute(
                "SELECT value FROM app_settings WHERE key=%s", ("copy-non-ascii",)
            ).fetchone()["value"]
            == "中文 English"
        )
        checkpoint = conn.execute(
            "SELECT last_seq FROM silicon_shadow.checkpoints "
            "WHERE run_id=%s AND direction='sqlite_to_postgres'",
            (RUN_ID,),
        ).fetchone()
        assert checkpoint["last_seq"] == snapshot.baseline_seq
        for table in POSTGRES_ROWID_ORDINAL_TABLES:
            sequence = conn.execute(
                "SELECT seq.oid::bigint AS oid FROM pg_class tbl "
                "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
                "JOIN pg_attribute att ON att.attrelid=tbl.oid AND att.attname='ordinal' "
                "JOIN pg_depend dep ON dep.refobjid=tbl.oid AND dep.refobjsubid=att.attnum "
                "AND dep.classid='pg_class'::regclass AND dep.deptype IN ('a','i') "
                "JOIN pg_class seq ON seq.oid=dep.objid AND seq.relkind='S' "
                "WHERE ns.nspname=current_schema() AND tbl.relname=%s",
                (table,),
            ).fetchone()["oid"]
            maximum = conn.execute(
                f'SELECT MAX(ordinal)::bigint AS maximum FROM "{table}"'
            ).fetchone()["maximum"]
            allocated = conn.execute(
                "SELECT nextval(%s::oid::regclass) AS value", (sequence,)
            ).fetchone()["value"]
            assert allocated == (1 if maximum is None else maximum + 1)


@pytest.mark.postgres_integration
def test_jsonb_numeric_roundtrip_reproof_and_drift_detection(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
):
    database = _prepare_forward_run(source_database, postgres_database)
    raw_payload = (
        '[{"escaped":"quote \\" slash \\\\ newline\\n 中文",'
        '"huge":1e400,"huge_integer":123456789012345678901234567890123456789,'
        '"negative_zero":-0,"nested":[true,null,{"value":10.5000}],'
        '"precise":0.12345678901234567890123456789}]'
    )
    with database.write() as conn:
        conn.execute(
            "INSERT INTO notebooks"
            "(id,name,created_at,updated_at,expected_questions) "
            "VALUES (?,?,?,?,?)",
            (
                "json-number-notebook",
                "JSON number proof",
                "2026-07-24 00:00:00",
                "2026-07-24 00:00:00",
                raw_payload,
            ),
        )
        conn.execute(
            "INSERT INTO kg_conflict_candidates"
            "(id,notebook_id,kind,left_ref,right_ref,resolved_payload,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "json-null-conflict",
                "json-number-notebook",
                "claim",
                "left",
                "right",
                "null",
                "2026-07-24 00:00:00",
                "2026-07-24 00:00:00",
            ),
        )
    snapshot = create_snapshot(
        database,
        tmp_path / "json-number-proof" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    report = copy_snapshot(
        snapshot,
        database,
        postgres_database,
        MANIFEST,
        batch_rows=2,
    )
    with postgres_database.connect() as conn:
        stored = conn.execute(
            "SELECT (expected_questions->0->>'precise')::numeric AS precise,"
            "(expected_questions->0->>'huge')::numeric AS huge,"
            "(expected_questions->0->>'negative_zero')::numeric AS negative_zero,"
            "expected_questions->0->>'huge_integer' AS huge_integer,"
            "expected_questions->0->>'escaped' AS escaped,"
            "expected_questions::text AS rendered "
            "FROM notebooks WHERE id=%s",
            ("json-number-notebook",),
        ).fetchone()
    assert stored["precise"] == Decimal("0.12345678901234567890123456789")
    assert stored["huge"] == Decimal("1e400")
    assert stored["negative_zero"] == Decimal("0")
    assert stored["huge_integer"] == "123456789012345678901234567890123456789"
    assert stored["escaped"] == 'quote " slash \\ newline\n 中文'
    assert "0.12345678901234567890123456789" in stored["rendered"]
    assert (
        copy_snapshot(
            snapshot,
            database,
            postgres_database,
            MANIFEST,
            batch_rows=3,
        )
        == report
    )
    collision = raw_payload.replace(
        '"precise":0.12345678901234567890123456789',
        '"precise":{"$json_number":"0.12345678901234567890123456789"}',
    )
    with postgres_database.write() as conn:
        conn.execute(
            "UPDATE notebooks SET expected_questions=%s::jsonb WHERE id=%s",
            (collision, "json-number-notebook"),
        )
    with pytest.raises(ValueError, match="row drifted"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    with postgres_database.write() as conn:
        conn.execute(
            "UPDATE notebooks SET expected_questions=%s::jsonb WHERE id=%s",
            (raw_payload, "json-number-notebook"),
        )
        conn.execute(
            "UPDATE kg_conflict_candidates SET resolved_payload=NULL WHERE id=%s",
            ("json-null-conflict",),
        )
    with pytest.raises(ValueError, match="row drifted"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    with postgres_database.write() as conn:
        conn.execute(
            "UPDATE kg_conflict_candidates SET resolved_payload='null'::jsonb "
            "WHERE id=%s",
            ("json-null-conflict",),
        )
    drifted = raw_payload.replace(
        "0.12345678901234567890123456789",
        "0.12345678901234567890123456788",
    )
    with postgres_database.write() as conn:
        conn.execute(
            "UPDATE notebooks SET expected_questions=%s::jsonb WHERE id=%s",
            (
                drifted,
                "json-number-notebook",
            ),
        )
    with pytest.raises(ValueError, match="row drifted"):
        copy_snapshot(
            snapshot,
            database,
            postgres_database,
            MANIFEST,
            batch_rows=2,
        )


@pytest.mark.postgres_integration
def test_target_drift_fails_closed_without_deleting_unrelated_rows(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO app_settings(key,value,updated_at) VALUES (%s,%s,%s)",
            ("unrelated", "must-survive", "2026-07-24 00:00:00+00"),
        )
    with pytest.raises(ValueError, match="checkpoint"):
        copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    with postgres_database.connect() as conn:
        assert (
            conn.execute(
                "SELECT value FROM app_settings WHERE key=%s", ("unrelated",)
            ).fetchone()["value"]
            == "must-survive"
        )


@pytest.mark.postgres_integration
def test_frozen_v9_upgrade_capture_snapshot_copies_full_catalog_and_ordinals(
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
):
    source_path = tmp_path / "frozen-v9.db"
    shutil.copy2(
        REPO_ROOT / "backend/tests/fixtures/repository_v9/baseline.db",
        source_path,
    )
    settings = Settings(database_url=f"sqlite:///{source_path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    storage = tmp_path / "frozen-storage"
    storage.mkdir()
    fixture_file = storage / "notebooks" / "nb-fixture" / "fixture-source.md"
    fixture_file.parent.mkdir(parents=True)
    fixture_file.write_text("# frozen fixture\n", encoding="utf-8")
    with database.write() as conn:
        now = "2026-07-24 00:00:00"
        conn.execute(
            "UPDATE sources SET file_path=?,file_size=? WHERE id='src-fixture'",
            (str(fixture_file), fixture_file.stat().st_size),
        )
        conn.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,score,created_at,updated_at) "
            "VALUES ('ordinal-merge','nb-fixture','a','b',0.5,?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO extraction_runs(id,notebook_id,source_id,run_type,status,created_at,updated_at) "
            "VALUES ('ordinal-extract','nb-fixture','src-fixture','kg','done',?,?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO kg_build_jobs(id,notebook_id,mode,status,stage,created_at,updated_at) "
            "VALUES ('ordinal-job','nb-fixture','full','done','done',?,?)",
            (now, now),
        )
    source_fixture = (database, settings.database_url, storage)
    try:
        _prepare_forward_run(source_fixture, postgres_database, poison_temp=True)
        with postgres_database.write() as conn:
            # Same-name temp relations (implicitly first for relation lookup)
            # must not redirect any baseline COPY business SQL.
            conn.execute("CREATE TEMP TABLE app_settings(key text)")
            conn.execute("CREATE TEMP TABLE answers(id text)")
        snapshot = create_snapshot(
            database,
            tmp_path / "frozen-private" / f"{RUN_ID}.sqlite3",
            run_id=RUN_ID,
        )
        report = copy_snapshot(
            snapshot,
            database,
            postgres_database,
            MANIFEST,
            batch_rows=2,
        )
        assert len(report.tables) == 60
        source_conn = sqlite3.connect(snapshot.path)
        try:
            source_counts = {
                table: source_conn.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                for table in MANIFEST.replicated_names
            }
        finally:
            source_conn.close()
        with postgres_database.connect() as conn:
            schema = conn.execute("SELECT current_schema() AS schema").fetchone()[
                "schema"
            ]
            for table, count in source_counts.items():
                actual = conn.execute(
                    pg_sql.SQL("SELECT COUNT(*) AS count FROM {}.{}").format(
                        pg_sql.Identifier(schema), pg_sql.Identifier(table)
                    )
                ).fetchone()["count"]
                assert actual == count
            assert (
                conn.execute(
                    pg_sql.SQL(
                        "SELECT pg_typeof(expected_questions)::text AS kind FROM {}.notebooks LIMIT 1"
                    ).format(pg_sql.Identifier(schema))
                ).fetchone()["kind"]
                == "jsonb"
            )
            assert (
                conn.execute(
                    pg_sql.SQL(
                        "SELECT pg_typeof(vector)::text AS kind FROM {}.element_embeddings LIMIT 1"
                    ).format(pg_sql.Identifier(schema))
                ).fetchone()["kind"]
                == "bytea"
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS count FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
                    "JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname=%s AND c.contype='f' AND c.convalidated",
                    (schema,),
                ).fetchone()["count"]
                > 0
            )
            for table in POSTGRES_ROWID_ORDINAL_TABLES:
                maximum = conn.execute(
                    pg_sql.SQL("SELECT MAX(ordinal) AS maximum FROM {}.{}").format(
                        pg_sql.Identifier(schema), pg_sql.Identifier(table)
                    )
                ).fetchone()["maximum"]
                assert maximum is not None
            assert (
                conn.execute(
                    "SELECT last_seq FROM silicon_shadow.checkpoints WHERE run_id=%s AND direction='sqlite_to_postgres'",
                    (RUN_ID,),
                ).fetchone()["last_seq"]
                == snapshot.baseline_seq
            )
    finally:
        database.close_local()


@pytest.mark.postgres_integration
@pytest.mark.parametrize(
    "drift",
    [
        "acl",
        "check_column_cast",
        "check_literal_case",
        "check_not_in",
        "column_domain",
        "constraint_flags",
        "cross_schema_foreign_key",
        "default_literal_case",
        "extra_internal_trigger",
        "foreign_key",
        "inheritance",
        "identity_nonidentity_always",
        "identity_ordinal_always",
        "ledger_acl",
        "ledger_column",
        "ledger_pk",
        "ledger_rls",
        "ledger_trigger",
        "ledger_unlogged",
        "operational_index",
        "operational_index_cast",
        "partial_predicate",
        "policy",
        "rls",
        "sequence_acl",
        "sequence_cache",
        "sequence_owner",
        "sequence_unlogged",
        "trigger_disabled",
        "unlogged",
    ],
)
def test_exact_v9_catalog_rejects_same_name_semantic_drift(
    source_database,
    postgres_database,
    clean_control_schema,
    tmp_path: Path,
    drift: str,
):
    database = _prepare_forward_run(source_database, postgres_database)
    snapshot = create_snapshot(
        database,
        tmp_path / "catalog" / f"{RUN_ID}.sqlite3",
        run_id=RUN_ID,
    )
    decoy_schema: str | None = None
    decoy_role: str | None = None
    decoy_role_schema: str | None = None
    decoy_role_is_preprovisioned = False
    with postgres_database.write() as conn:
        if drift == "foreign_key":
            conn.execute(
                "ALTER TABLE answers DROP CONSTRAINT fk_answers_notebook_id__notebooks"
            )
            conn.execute(
                "ALTER TABLE answers ADD CONSTRAINT fk_answers_notebook_id__notebooks "
                "FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE RESTRICT"
            )
        elif drift == "operational_index":
            conn.execute("DROP INDEX idx_answers_nb")
            conn.execute("CREATE INDEX idx_answers_nb ON answers(conversation_id)")
        elif drift == "operational_index_cast":
            conn.execute("DROP INDEX idx_agent_profiles_owner_status")
            conn.execute(
                "CREATE INDEX idx_agent_profiles_owner_status "
                "ON agent_profiles(owner_id,status,"
                "((updated_at AT TIME ZONE 'UTC')::date) DESC)"
            )
        elif drift == "unlogged":
            conn.execute("ALTER TABLE app_settings SET UNLOGGED")
        elif drift == "acl":
            conn.execute("GRANT SELECT ON answers TO PUBLIC")
        elif drift == "rls":
            conn.execute("ALTER TABLE answers ENABLE ROW LEVEL SECURITY")
        elif drift == "policy":
            conn.execute("CREATE POLICY shadow_drift ON answers USING (true)")
        elif drift == "trigger_disabled":
            # PostgreSQL reserves internal constraint-trigger state changes for
            # superusers. The pure validator test above preserves CI coverage;
            # privileged local lanes still exercise the catalog end to end.
            is_superuser = bool(
                conn.execute(
                    "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
                ).fetchone()["rolsuper"]
            )
            if not is_superuser:
                pytest.skip(
                    "disabling PostgreSQL internal constraint triggers requires superuser"
                )
            conn.execute("ALTER TABLE answers DISABLE TRIGGER ALL")
        elif drift == "sequence_acl":
            conn.execute("GRANT SELECT ON SEQUENCE answers_ordinal_seq TO PUBLIC")
        elif drift == "sequence_cache":
            conn.execute("ALTER SEQUENCE answers_ordinal_seq CACHE 7")
        elif drift == "sequence_owner":
            schema = str(
                conn.execute("SELECT current_schema() AS name").fetchone()["name"]
            )
            decoy_role = os.environ.get("TEST_POSTGRES_DECOY_OWNER_ROLE")
            if decoy_role is not None:
                if _POSTGRES_ROLE_NAME.fullmatch(decoy_role) is None:
                    raise RuntimeError(
                        "TEST_POSTGRES_DECOY_OWNER_ROLE must be a simple PostgreSQL role name"
                    )
                decoy_role_is_preprovisioned = True
                decoy_role_schema = schema
                conn.execute(
                    pg_sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
                        pg_sql.Identifier(schema), pg_sql.Identifier(decoy_role)
                    )
                )
            else:
                decoy_role = f"seq_owner_{schema.rsplit('_', 1)[-1]}"
                conn.execute(
                    pg_sql.SQL("CREATE ROLE {}").format(pg_sql.Identifier(decoy_role))
                )
            conn.execute(
                pg_sql.SQL("ALTER TABLE answers OWNER TO {}").format(
                    pg_sql.Identifier(decoy_role)
                )
            )
        elif drift == "sequence_unlogged":
            # A generated identity sequence inherits table persistence. Use an
            # identity table with no incoming logged FK so PostgreSQL permits
            # this real catalog drift without direct system-catalog writes.
            conn.execute("ALTER TABLE concept_merge_candidates SET UNLOGGED")
        elif drift == "constraint_flags":
            conn.execute(
                "ALTER TABLE answers DROP CONSTRAINT fk_answers_notebook_id__notebooks"
            )
            conn.execute(
                "ALTER TABLE answers ADD CONSTRAINT fk_answers_notebook_id__notebooks "
                "FOREIGN KEY (notebook_id) REFERENCES notebooks(id) MATCH FULL "
                "ON UPDATE NO ACTION ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED"
            )
        elif drift == "cross_schema_foreign_key":
            decoy_schema = f"{conn.execute('SELECT current_schema() AS name').fetchone()['name']}_decoy"
            conn.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(decoy_schema))
            )
            conn.execute(
                pg_sql.SQL("CREATE TABLE {}.notebooks(id text PRIMARY KEY)").format(
                    pg_sql.Identifier(decoy_schema)
                )
            )
            conn.execute(
                "ALTER TABLE answers DROP CONSTRAINT fk_answers_notebook_id__notebooks"
            )
            conn.execute(
                pg_sql.SQL(
                    "ALTER TABLE answers ADD CONSTRAINT fk_answers_notebook_id__notebooks "
                    "FOREIGN KEY (notebook_id) REFERENCES {}.notebooks(id) "
                    "ON UPDATE NO ACTION ON DELETE CASCADE"
                ).format(pg_sql.Identifier(decoy_schema))
            )
        elif drift == "inheritance":
            decoy_schema = f"{conn.execute('SELECT current_schema() AS name').fetchone()['name']}_decoy"
            conn.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(decoy_schema))
            )
            conn.execute(
                pg_sql.SQL(
                    "CREATE TABLE {}.shadow_child () INHERITS (app_settings)"
                ).format(pg_sql.Identifier(decoy_schema))
            )
        elif drift == "extra_internal_trigger":
            business_schema = str(
                conn.execute("SELECT current_schema() AS name").fetchone()["name"]
            )
            decoy_schema = f"{business_schema}_decoy"
            conn.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(decoy_schema))
            )
            conn.execute(
                pg_sql.SQL(
                    "CREATE TABLE {}.shadow_child "
                    "(key text REFERENCES {}.app_settings(key))"
                ).format(
                    pg_sql.Identifier(decoy_schema),
                    pg_sql.Identifier(business_schema),
                )
            )
        elif drift == "check_not_in":
            conn.execute(
                "ALTER TABLE agent_profiles DROP CONSTRAINT ck_agent_profiles_status"
            )
            conn.execute(
                "ALTER TABLE agent_profiles ADD CONSTRAINT ck_agent_profiles_status "
                "CHECK (status NOT IN ('active','revoked'))"
            )
        elif drift == "check_column_cast":
            conn.execute(
                "ALTER TABLE memory_embeddings "
                "DROP CONSTRAINT ck_memory_embeddings_dimension"
            )
            conn.execute(
                "ALTER TABLE memory_embeddings "
                "ADD CONSTRAINT ck_memory_embeddings_dimension "
                "CHECK (dimension::smallint > 0)"
            )
        elif drift == "check_literal_case":
            conn.execute(
                "ALTER TABLE agent_profiles DROP CONSTRAINT ck_agent_profiles_status"
            )
            conn.execute(
                "ALTER TABLE agent_profiles ADD CONSTRAINT ck_agent_profiles_status "
                "CHECK (status IN ('Active','revoked'))"
            )
        elif drift == "default_literal_case":
            conn.execute(
                "ALTER TABLE knowhow_changes ALTER COLUMN origin SET DEFAULT 'USER'"
            )
        elif drift == "column_domain":
            conn.execute('CREATE DOMAIN shadow_text AS text COLLATE "C"')
            conn.execute("ALTER TABLE app_settings ALTER COLUMN value TYPE shadow_text")
        elif drift == "identity_nonidentity_always":
            conn.execute(
                "ALTER TABLE ask_trace_steps ALTER COLUMN seq "
                "ADD GENERATED ALWAYS AS IDENTITY"
            )
        elif drift == "identity_ordinal_always":
            conn.execute(
                "ALTER TABLE answers ALTER COLUMN ordinal SET GENERATED ALWAYS"
            )
        elif drift == "ledger_acl":
            conn.execute("GRANT SELECT ON silicon_schema_migrations TO PUBLIC")
        elif drift == "ledger_column":
            conn.execute(
                "ALTER TABLE silicon_schema_migrations "
                "ALTER COLUMN applied_at DROP DEFAULT"
            )
        elif drift == "ledger_pk":
            conn.execute(
                "ALTER TABLE silicon_schema_migrations "
                "DROP CONSTRAINT silicon_schema_migrations_pkey"
            )
        elif drift == "ledger_rls":
            conn.execute(
                "ALTER TABLE silicon_schema_migrations ENABLE ROW LEVEL SECURITY"
            )
        elif drift == "ledger_trigger":
            conn.execute(
                "CREATE FUNCTION shadow_ledger_trigger() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$"
            )
            conn.execute(
                "CREATE TRIGGER shadow_ledger_trigger BEFORE UPDATE "
                "ON silicon_schema_migrations FOR EACH ROW "
                "EXECUTE FUNCTION shadow_ledger_trigger()"
            )
        elif drift == "ledger_unlogged":
            conn.execute("ALTER TABLE silicon_schema_migrations SET UNLOGGED")
        elif drift == "partial_predicate":
            conn.execute("DROP INDEX idx_memory_answer_once")
            conn.execute(
                "CREATE UNIQUE INDEX idx_memory_answer_once "
                "ON memory_items(created_by,source_answer_id) "
                "WHERE source_answer_id IS NULL"
            )
    try:
        with pytest.raises(ValueError, match="drifted|not valid|behavior"):
            copy_snapshot(snapshot, database, postgres_database, MANIFEST, batch_rows=2)
    finally:
        if decoy_schema is not None:
            with postgres_database.write() as conn:
                conn.execute(
                    pg_sql.SQL("DROP SCHEMA {} CASCADE").format(
                        pg_sql.Identifier(decoy_schema)
                    )
                )
        if decoy_role is not None:
            with postgres_database.write() as conn:
                if decoy_role_is_preprovisioned:
                    conn.execute("ALTER TABLE answers OWNER TO CURRENT_USER")
                    assert decoy_role_schema is not None
                    conn.execute(
                        pg_sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
                            pg_sql.Identifier(decoy_role_schema),
                            pg_sql.Identifier(decoy_role),
                        )
                    )
                else:
                    conn.execute(
                        pg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(
                            pg_sql.Identifier(decoy_role)
                        )
                    )
                    conn.execute(
                        pg_sql.SQL("DROP OWNED BY {}").format(
                            pg_sql.Identifier(decoy_role)
                        )
                    )
                    conn.execute(
                        pg_sql.SQL("DROP ROLE {}").format(pg_sql.Identifier(decoy_role))
                    )


@pytest.mark.postgres_integration
def test_postgres_schema_version_supports_quoted_mixed_case_schema(
    postgres_database,
):
    with postgres_database.write() as conn:
        base = str(conn.execute("SELECT current_schema() AS name").fetchone()["name"])
        quoted_schema = f"MixedCase-Team-DB-{base.rsplit('_', 1)[-1]}"
        conn.execute(
            pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(quoted_schema))
        )
        conn.execute(
            pg_sql.SQL(
                "CREATE TABLE {}.silicon_schema_migrations "
                "(version integer PRIMARY KEY, checksum text NOT NULL)"
            ).format(pg_sql.Identifier(quoted_schema))
        )
        for migration in control._POSTGRES_MIGRATIONS:
            conn.execute(
                pg_sql.SQL(
                    "INSERT INTO {}.silicon_schema_migrations(version,checksum) "
                    "VALUES (%s,%s)"
                ).format(pg_sql.Identifier(quoted_schema)),
                (migration.version, migration.checksum),
            )
        assert control._postgres_schema_version(conn, quoted_schema) == len(
            control._POSTGRES_MIGRATIONS
        )
        conn.execute(
            pg_sql.SQL("DROP SCHEMA {} CASCADE").format(
                pg_sql.Identifier(quoted_schema)
            )
        )
