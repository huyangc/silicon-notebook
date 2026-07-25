from __future__ import annotations

import copy
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.migration.shadow import control
from app.migration.shadow.capture import install_sqlite_capture
from app.migration.shadow.control import (
    GuardReport,
    PreflightReport,
    ShadowPhase,
    commit_guard_report,
    committed_sqlite_guard_report,
    create_or_reuse_run,
    enable_sqlite_capture_after_guard_reports,
    install_postgres_control_schema,
    prepare_postgres_replication_key_guards,
    update_forward_state,
)
from app.migration.shadow.identity import (
    ConfirmationBinding,
    DatabaseFingerprint,
    issue_confirmation_token,
)
from app.migration.shadow.manifest import MANIFEST, replication_guard_specs
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.postgres.unified_kg_store import (
    UnifiedKgStore as PostgresUnifiedKgStore,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET = b"offline-control-confirmation-secret-32bytes"
TARGET_FINGERPRINT = DatabaseFingerprint(
    "postgresql", "postgresql://db:5432/test?schema=business", "b" * 64
)

# Reviewed test fixture: deliberately independent of control.py's catalog constants.
_EXPECTED_CONTROL_COLUMNS = {
    "runs": (
        ("run_id", "text", True, None, "C"),
        ("source_identity", "text", True, None, "C"),
        ("source_identity_hash", "character(64)", True, None, "C"),
        ("target_identity", "text", True, None, "C"),
        ("target_identity_hash", "character(64)", True, None, "C"),
        ("target_business_schema", "text", True, None, "C"),
        ("sqlite_version", "integer", True, None, None),
        ("postgres_version", "integer", True, None, None),
        ("schema_epoch", "integer", True, None, None),
        ("phase", "text", True, None, "C"),
        ("active_backend", "text", True, None, "C"),
        ("revision", "bigint", True, "0", None),
        ("progress", "jsonb", True, "'{}'::jsonb", None),
        ("sqlite_guard_report_hash", "character(64)", False, None, "C"),
        ("postgres_guard_report_hash", "character(64)", False, None, "C"),
        ("capture_enabled", "boolean", True, "false", None),
        ("created_at", "timestamp with time zone", True, "clock_timestamp()", None),
        ("updated_at", "timestamp with time zone", True, "clock_timestamp()", None),
        ("terminal_reason", "text", False, None, "C"),
    ),
    "checkpoints": (
        ("run_id", "text", True, None, "C"),
        ("direction", "text", True, None, "C"),
        ("last_seq", "bigint", True, "0", None),
        ("revision", "bigint", True, "0", None),
        ("updated_at", "timestamp with time zone", True, "clock_timestamp()", None),
    ),
    "workers": (
        ("run_id", "text", True, None, "C"),
        ("direction", "text", True, None, "C"),
        ("worker_id", "text", True, None, "C"),
        ("lease_expires_at", "timestamp with time zone", True, None, None),
        ("heartbeat_at", "timestamp with time zone", True, "clock_timestamp()", None),
    ),
    "poison_events": (
        ("run_id", "text", True, None, "C"),
        ("direction", "text", True, None, "C"),
        ("source_seq", "bigint", True, None, None),
        ("category", "text", True, None, "C"),
        ("safe_summary", "text", True, None, "C"),
        ("created_at", "timestamp with time zone", True, "clock_timestamp()", None),
    ),
    "copy_progress": (
        ("run_id", "text", True, None, "C"),
        ("table_name", "text", True, None, "C"),
        ("last_key_json", "jsonb", False, None, None),
        ("rows_copied", "bigint", True, "0", None),
        ("bytes_copied", "bigint", True, "0", None),
        ("rolling_hash", "character(64)", False, None, "C"),
        ("complete", "boolean", True, "false", None),
        ("updated_at", "timestamp with time zone", True, "clock_timestamp()", None),
    ),
    "verification_runs": (
        ("verification_id", "text", True, None, "C"),
        ("run_id", "text", True, None, "C"),
        ("level", "text", True, None, "C"),
        ("status", "text", True, None, "C"),
        ("source_barrier", "bigint", False, None, None),
        ("target_checkpoint", "bigint", False, None, None),
        ("started_at", "timestamp with time zone", True, "clock_timestamp()", None),
        ("completed_at", "timestamp with time zone", False, None, None),
    ),
    "verification_differences": (
        ("verification_id", "text", True, None, "C"),
        ("difference_no", "bigint", True, None, None),
        ("table_name", "text", True, None, "C"),
        ("key_hash", "character(64)", False, None, "C"),
        ("category", "text", True, None, "C"),
        ("safe_summary", "text", True, None, "C"),
    ),
    "verifier_barriers": (
        ("verification_id", "text", True, None, "C"),
        ("run_id", "text", True, None, "C"),
        ("source_barrier", "bigint", True, None, None),
        ("expires_at", "timestamp with time zone", True, None, None),
    ),
    "retention_checkpoints": (
        ("run_id", "text", True, None, "C"),
        ("checkpoint_kind", "text", True, None, "C"),
        ("source_seq", "bigint", True, None, None),
        ("updated_at", "timestamp with time zone", True, "clock_timestamp()", None),
    ),
}

_EXPECTED_CONTROL_CONSTRAINTS = {
    "pk_shadow_runs": ("runs", "p", "PRIMARY KEY (run_id)"),
    "ck_shadow_runs_phase": (
        "runs",
        "c",
        "CHECK (phase IN ('off', 'sqlite_to_postgres', 'cutover_readonly', 'postgres_to_sqlite', 'retired'))",
    ),
    "ck_shadow_runs_active_backend": (
        "runs",
        "c",
        "CHECK (active_backend IN ('sqlite', 'postgresql'))",
    ),
    "ck_shadow_runs_revision": ("runs", "c", "CHECK (revision >= 0)"),
    "pk_shadow_checkpoints": ("checkpoints", "p", "PRIMARY KEY (run_id, direction)"),
    "fk_shadow_checkpoints_run": (
        "checkpoints",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "ck_shadow_checkpoints_direction": (
        "checkpoints",
        "c",
        "CHECK (direction IN ('sqlite_to_postgres', 'postgres_to_sqlite'))",
    ),
    "ck_shadow_checkpoints_last_seq": ("checkpoints", "c", "CHECK (last_seq >= 0)"),
    "ck_shadow_checkpoints_revision": ("checkpoints", "c", "CHECK (revision >= 0)"),
    "pk_shadow_workers": ("workers", "p", "PRIMARY KEY (run_id, direction)"),
    "fk_shadow_workers_run": (
        "workers",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "pk_shadow_poison_events": (
        "poison_events",
        "p",
        "PRIMARY KEY (run_id, direction, source_seq)",
    ),
    "fk_shadow_poison_events_run": (
        "poison_events",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "pk_shadow_copy_progress": (
        "copy_progress",
        "p",
        "PRIMARY KEY (run_id, table_name)",
    ),
    "fk_shadow_copy_progress_run": (
        "copy_progress",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "ck_shadow_copy_progress_rows": ("copy_progress", "c", "CHECK (rows_copied >= 0)"),
    "ck_shadow_copy_progress_bytes": (
        "copy_progress",
        "c",
        "CHECK (bytes_copied >= 0)",
    ),
    "pk_shadow_verification_runs": (
        "verification_runs",
        "p",
        "PRIMARY KEY (verification_id)",
    ),
    "fk_shadow_verification_runs_run": (
        "verification_runs",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "pk_shadow_verification_differences": (
        "verification_differences",
        "p",
        "PRIMARY KEY (verification_id, difference_no)",
    ),
    "fk_shadow_verification_differences_run": (
        "verification_differences",
        "f",
        "FOREIGN KEY (verification_id) REFERENCES silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE",
    ),
    "pk_shadow_verifier_barriers": (
        "verifier_barriers",
        "p",
        "PRIMARY KEY (verification_id)",
    ),
    "fk_shadow_verifier_barriers_verification": (
        "verifier_barriers",
        "f",
        "FOREIGN KEY (verification_id) REFERENCES silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE",
    ),
    "fk_shadow_verifier_barriers_run": (
        "verifier_barriers",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
    "pk_shadow_retention_checkpoints": (
        "retention_checkpoints",
        "p",
        "PRIMARY KEY (run_id, checkpoint_kind)",
    ),
    "fk_shadow_retention_checkpoints_run": (
        "retention_checkpoints",
        "f",
        "FOREIGN KEY (run_id) REFERENCES silicon_shadow.runs(run_id) ON DELETE CASCADE",
    ),
}


class _Rows:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


@pytest.mark.parametrize("business_schema", [None, "MixedCase-Team-DB"])
def test_missing_postgres_migration_ledger_returns_zero(business_schema):
    calls: list[tuple[str, tuple[object, ...]]] = []

    class MissingLedger:
        def execute(self, statement, parameters=()):
            calls.append((str(statement), tuple(parameters)))
            return _Rows()

    assert control._postgres_schema_version(MissingLedger(), business_schema) == 0
    assert calls
    if business_schema is None:
        assert calls[0][1] == ("silicon_schema_migrations",)
    else:
        assert calls[0][1] == (
            "MixedCase-Team-DB",
            "silicon_schema_migrations",
        )


def _run_row(*, source_hash="a" * 64, target_hash="b" * 64):
    return {
        "run_id": "run-1",
        "source_identity": "sqlite:///<redacted>",
        "source_identity_hash": source_hash,
        "target_identity": TARGET_FINGERPRINT.redacted_identity,
        "target_identity_hash": target_hash,
        "target_business_schema": "business",
        "sqlite_version": 31,
        "postgres_version": 9,
        "schema_epoch": 1,
        "phase": "off",
        "active_backend": "sqlite",
        "progress": {},
        "revision": 0,
        "sqlite_guard_report_hash": None,
        "postgres_guard_report_hash": None,
        "capture_enabled": False,
    }


class _ScriptedConnection:
    def __init__(self, target):
        self.target = target

    def execute(self, statement: str, parameters=()):
        if not isinstance(statement, str):
            statement = statement.as_string()
        sql = " ".join(statement.split())
        self.target.statements.append(sql)
        self.target.events.append(f"pg:{sql}")
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            return _Rows([{}])
        if sql == control._CONTROL_SQL_PATH.read_text(encoding="utf-8").strip().replace(
            "\n", " "
        ):
            self.target.schema_exists = True
            return _Rows()
        if statement == control._CONTROL_SQL_PATH.read_text(encoding="utf-8"):
            self.target.schema_exists = True
            return _Rows()
        if sql.startswith("COMMENT ON SCHEMA silicon_shadow"):
            self.target.schema_comment = control._control_schema_comment()
            return _Rows()
        if "FROM pg_namespace WHERE nspname='silicon_shadow'" in sql:
            if not self.target.schema_exists:
                return _Rows()
            if "nspowner" in sql:
                return _Rows(
                    [
                        {
                            "owner": "owner",
                            "current_user": "owner",
                            "comment": self.target.schema_comment,
                            "owner_only_acl": True,
                        }
                    ]
                )
            return _Rows([{"exists": 1}])
        if "c.relpersistence" in sql and "n.nspname='silicon_shadow'" in sql:
            rows = [
                {
                    "table_name": table,
                    "owned": True,
                    "relpersistence": "p",
                    "relrowsecurity": False,
                    "relforcerowsecurity": False,
                    "owner_only_acl": True,
                }
                for table in sorted(_EXPECTED_CONTROL_COLUMNS)
            ]
            if self.target.catalog_drift == "table-acl":
                rows[0]["owner_only_acl"] = False
            return _Rows(rows)
        if "format_type(a.atttypid,a.atttypmod)" in sql:
            rows = [
                {
                    "table_name": table,
                    "column_name": column,
                    "formatted_type": formatted_type,
                    "attnotnull": not_null,
                    "default_expression": default,
                    "collation": collation,
                    "attidentity": "",
                    "attgenerated": "",
                }
                for table, columns in _EXPECTED_CONTROL_COLUMNS.items()
                for column, formatted_type, not_null, default, collation in columns
            ]
            if self.target.catalog_drift == "column-default":
                next(row for row in rows if row["column_name"] == "revision")[
                    "default_expression"
                ] = "1"
            return _Rows(rows)
        if "FROM pg_constraint k" in sql:
            rows = [
                {
                    "table_name": table,
                    "conname": name,
                    "contype": kind,
                    "definition": definition,
                    "condeferrable": False,
                    "condeferred": False,
                    "convalidated": True,
                }
                for name, (
                    table,
                    kind,
                    definition,
                ) in _EXPECTED_CONTROL_CONSTRAINTS.items()
            ]
            if self.target.catalog_drift == "constraint":
                rows[0]["definition"] = "PRIMARY KEY (run_id, phase)"
            return _Rows(rows)
        if (
            "n.nspname='silicon_shadow'" in sql
            and ("LIMIT 1" in sql or "unknown" not in sql)
            and any(
                token in sql
                for token in (
                    "c.relkind IN",
                    "x.relkind='i'",
                    "FROM pg_proc",
                    "FROM pg_trigger",
                    "FROM pg_type",
                    "FROM pg_policy",
                )
            )
        ):
            return _Rows([{}] if self.target.extra_control_object else [])
        if "current_database() AS database" in sql:
            return _Rows(
                [
                    {
                        "database": "test",
                        "schema": self.target.current_schema,
                        "database_oid": "7",
                    }
                ]
            )
        if "SELECT c.oid AS relation FROM pg_class c" in sql and "c.relname=%s" in sql:
            return _Rows([{"relation": "silicon_schema_migrations"}])
        if (
            sql.startswith("SELECT version,checksum FROM")
            and "silicon_schema_migrations" in sql
        ):
            return _Rows(
                [
                    {"version": migration.version, "checksum": migration.checksum}
                    for migration in control._POSTGRES_MIGRATIONS
                ]
            )
        if sql.startswith(control._RUN_SELECT):
            return _Rows([copy.deepcopy(self.target.run)] if self.target.run else [])
        if sql.startswith("INSERT INTO silicon_shadow.runs"):
            (
                run_id,
                source_identity,
                source_hash,
                target_identity,
                target_hash,
                schema,
                sqlite_version,
                postgres_version,
                epoch,
            ) = parameters
            self.target.run = _run_row(source_hash=source_hash, target_hash=target_hash)
            self.target.run.update(
                run_id=run_id,
                source_identity=source_identity,
                target_identity=target_identity,
                target_business_schema=schema,
                sqlite_version=sqlite_version,
                postgres_version=postgres_version,
                schema_epoch=epoch,
            )
            return _Rows()
        if sql.startswith(
            "UPDATE silicon_shadow.runs SET sqlite_guard_report_hash"
        ) or sql.startswith(
            "UPDATE silicon_shadow.runs SET postgres_guard_report_hash"
        ):
            column = (
                "sqlite_guard_report_hash"
                if "sqlite_guard" in sql
                else "postgres_guard_report_hash"
            )
            report_hash, _run_id, revision = parameters
            if (
                self.target.run["revision"] != revision
                or self.target.run[column] is not None
            ):
                return _Rows()
            self.target.run[column] = report_hash
            self.target.run["revision"] += 1
            return _Rows([{"run_id": "run-1"}])
        if sql.startswith("UPDATE silicon_shadow.runs SET capture_enabled=true"):
            _run_id, revision, sqlite_hash, postgres_hash = parameters
            if (
                self.target.fail_capture_ack_once
                or self.target.run["revision"] != revision
                or self.target.run["sqlite_guard_report_hash"] != sqlite_hash
                or self.target.run["postgres_guard_report_hash"] != postgres_hash
            ):
                self.target.fail_capture_ack_once = False
                return _Rows()
            self.target.run["capture_enabled"] = True
            self.target.run["revision"] += 1
            return _Rows([{"run_id": "run-1"}])
        if sql.startswith("UPDATE silicon_shadow.runs SET phase="):
            new_phase, progress, _run_id, expected_phase, revision, gate_phase = (
                parameters
            )
            if (
                self.target.run["phase"] != expected_phase
                or self.target.run["revision"] != revision
                or (
                    gate_phase == "sqlite_to_postgres"
                    and not (
                        self.target.run["capture_enabled"]
                        and self.target.run["sqlite_guard_report_hash"]
                        and self.target.run["postgres_guard_report_hash"]
                    )
                )
            ):
                return _Rows()
            import json

            self.target.run["phase"] = new_phase
            self.target.run["progress"] = json.loads(progress)
            self.target.run["revision"] += 1
            return _Rows([{"run_id": "run-1"}])
        if "x.relname LIKE %s" in sql and parameters[-1] == "shadow_uq_%":
            return _Rows([{"index_name": name} for name in sorted(self.target.indexes)])
        if "x.relname=%s AND x.relkind='i'" in sql:
            return _Rows([{}] if parameters[-1] in self.target.indexes else [])
        if "i.indisunique" in sql:
            guard = next(
                item
                for item in replication_guard_specs(MANIFEST)
                if item.name == parameters[-1]
            )
            columns = guard.columns
            if self.target.malformed_guard == guard.name:
                columns = tuple(reversed(columns))
            return _Rows(
                [
                    {
                        "table_name": guard.table,
                        "indisunique": True,
                        "indisvalid": True,
                        "indisready": True,
                        "indislive": True,
                        "nulls_distinct": True,
                        "no_predicate": True,
                        "no_expression": True,
                        "columns": columns,
                        "no_include": True,
                        "access_method": "btree",
                        "default_opclasses": True,
                        "default_order": True,
                        "reviewed_collation": True,
                    }
                ]
            )
        if sql.startswith("CREATE UNIQUE INDEX"):
            name = re.search(r'CREATE UNIQUE INDEX "([^"]+)"', sql).group(1)
            self.target.indexes.add(name)
            self.target.created_guard_count += 1
            if self.target.fail_guard_after == self.target.created_guard_count:
                raise RuntimeError("injected guard failure")
            return _Rows()
        if sql.startswith("SELECT 1 FROM") and (
            "IS NULL" in sql or "HAVING COUNT" in sql
        ):
            return _Rows()
        if sql.startswith("SELECT COUNT(*)::bigint AS count"):
            return _Rows([{"count": 0}])
        raise AssertionError(f"unexpected scripted SQL: {sql}")


class _ScriptedTarget:
    def __init__(self, *, run=None):
        self.settings = SimpleNamespace(
            database_url="postgresql://user:secret@db:5432/test",
            postgres_pool_max_size=4,
        )
        self.run = copy.deepcopy(run)
        self.schema_exists = False
        self.schema_comment = None
        self.extra_control_object = False
        self.catalog_drift = None
        self.indexes: set[str] = set()
        self.malformed_guard = None
        self.fail_guard_after = None
        self.created_guard_count = 0
        self.fail_capture_ack_once = False
        self.current_schema = "business"
        self.statements: list[str] = []
        self.events: list[str] = []

    @contextmanager
    def connect(self):
        yield _ScriptedConnection(self)

    @contextmanager
    def write(self):
        snapshot = copy.deepcopy(
            (
                self.run,
                self.schema_exists,
                self.schema_comment,
                self.indexes,
                self.created_guard_count,
            )
        )
        try:
            yield _ScriptedConnection(self)
        except BaseException:
            (
                self.run,
                self.schema_exists,
                self.schema_comment,
                self.indexes,
                self.created_guard_count,
            ) = snapshot
            raise


@pytest.fixture
def sqlite_capture(tmp_path: Path):
    path = tmp_path / "source.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        with database.connect() as conn:
            install_sqlite_capture(
                conn, run_id="run-1", schema_epoch=1, manifest=MANIFEST
            )
            yield settings.database_url, conn
    finally:
        database.close_local()


def _report() -> PreflightReport:
    source = DatabaseFingerprint("sqlite", "sqlite:///<redacted>", "a" * 64)
    binding = ConfirmationBinding(
        "run-1", source.identity_hash, TARGET_FINGERPRINT.identity_hash, 31, 9, 1
    )
    return PreflightReport(
        "run-1",
        source,
        TARGET_FINGERPRINT,
        MANIFEST.schema_pair,
        0,
        100,
        0,
        0,
        0,
        130,
        1_000,
        4,
        1,
        False,
        "disk",
        "backup",
        issue_confirmation_token(binding, secret=SECRET, now=1_000),
    )


def _ready_reports(sqlite_capture, monkeypatch, target):
    source_url, conn = sqlite_capture
    sqlite_report = committed_sqlite_guard_report(
        conn, source_url=source_url, manifest=MANIFEST, run_id="run-1"
    )
    target.run["source_identity_hash"] = sqlite_report.database_identity_hash
    monkeypatch.setattr(
        control, "fingerprint_postgres", lambda *_a, **_k: TARGET_FINGERPRINT
    )
    monkeypatch.setattr(control, "validate_postgres_connection", lambda *_a, **_k: None)
    postgres_report = prepare_postgres_replication_key_guards(
        target, manifest=MANIFEST, run_id="run-1"
    )
    return sqlite_report, postgres_report


def test_offline_control_schema_install_is_exact_and_unknown_schema_refuses():
    sql_text = control._CONTROL_SQL_PATH.read_text(encoding="utf-8")
    assert set(re.findall(r"CREATE TABLE silicon_shadow\.([a-z_]+)", sql_text)) == {
        "runs",
        "checkpoints",
        "workers",
        "poison_events",
        "copy_progress",
        "verification_runs",
        "verification_differences",
        "verifier_barriers",
        "retention_checkpoints",
    }
    copy_table = sql_text.split("CREATE TABLE silicon_shadow.copy_progress", 1)[
        1
    ].split("CREATE TABLE silicon_shadow.verification_runs", 1)[0]
    assert "CHECK (rows_copied >= 0)" in copy_table
    assert "CHECK (bytes_copied >= 0)" in copy_table
    assert "postgresql://" not in sql_text
    assert "password" not in sql_text.lower()
    target = _ScriptedTarget()
    checksum = install_postgres_control_schema(target)
    assert len(checksum) == 64
    install_postgres_control_schema(target)
    target.extra_control_object = True
    with pytest.raises(ValueError, match="unknown"):
        install_postgres_control_schema(target)

    unknown = _ScriptedTarget()
    unknown.schema_exists = True
    unknown.schema_comment = "someone-else"
    with pytest.raises(ValueError, match="checksum"):
        install_postgres_control_schema(unknown)


@pytest.mark.parametrize(
    ("drift", "message"),
    (
        ("table-acl", "ACL"),
        ("column-default", "column contract"),
        ("constraint", "constraint"),
    ),
)
def test_offline_control_schema_rejects_exact_catalog_drift(drift, message):
    target = _ScriptedTarget()
    install_postgres_control_schema(target)
    target.catalog_drift = drift
    with pytest.raises(ValueError, match=message):
        install_postgres_control_schema(target)


def test_postgres_check_definition_normalization_is_version_stable():
    catalog_form = (
        "CHECK ((phase = ANY (ARRAY['off'::text, 'sqlite_to_postgres'::text])))"
    )
    ddl_form = "CHECK (phase IN ('off', 'sqlite_to_postgres'))"
    assert control._normalize_constraint_definition(catalog_form) == (
        control._normalize_constraint_definition(ddl_form)
    )


def test_postgres_scratch_readers_use_preserved_knowledge_ordinal():
    statements = []

    class RecordingConnection:
        def execute(self, statement, parameters):
            statements.append((" ".join(statement.split()), parameters))
            return _Rows()

    connection = RecordingConnection()
    PostgresUnifiedKgStore.scratch_vector_rows(connection, "nb", "run")
    PostgresUnifiedKgStore.stream_scratch_rows(connection, "nb", "run")
    assert len(statements) == 2
    for statement, parameters in statements:
        assert "JOIN knowledge_objects k ON k.id=s.object_id" in statement
        assert "ORDER BY k.ordinal" in statement
        assert "ORDER BY s.object_id" not in statement
        assert parameters == ("run", "nb") or parameters == ("nb", "run")


def test_offline_run_identity_reuse_and_mismatch(monkeypatch):
    target = _ScriptedTarget()
    install_postgres_control_schema(target)
    monkeypatch.setattr(
        control, "fingerprint_postgres", lambda *_a, **_k: TARGET_FINGERPRINT
    )
    report = _report()
    first = create_or_reuse_run(
        target,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )
    assert first.revision == 0
    assert (
        create_or_reuse_run(
            target,
            report,
            confirmation_token=report.confirmation_token,
            confirmation_secret=SECRET,
            now=1_001,
        )
        == first
    )
    target.run["source_identity_hash"] = "c" * 64
    with pytest.raises(ValueError, match="different identities"):
        create_or_reuse_run(
            target,
            report,
            confirmation_token=report.confirmation_token,
            confirmation_secret=SECRET,
            now=1_001,
        )


def test_run_schema_binding_uses_structured_current_schema(monkeypatch):
    target = _ScriptedTarget()
    target.current_schema = "Team?schema=DB"
    install_postgres_control_schema(target)
    monkeypatch.setattr(
        control, "fingerprint_postgres", lambda *_a, **_k: TARGET_FINGERPRINT
    )
    report = _report()
    state = create_or_reuse_run(
        target,
        report,
        confirmation_token=report.confirmation_token,
        confirmation_secret=SECRET,
        now=1_001,
    )
    assert state.target_business_schema == "Team?schema=DB"


def test_offline_guard_atomic_rollback_exact_set_and_malformed_refusal(monkeypatch):
    target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(target)
    monkeypatch.setattr(
        control, "fingerprint_postgres", lambda *_a, **_k: TARGET_FINGERPRINT
    )
    monkeypatch.setattr(control, "validate_postgres_connection", lambda *_a, **_k: None)
    target.fail_guard_after = 2
    with pytest.raises(RuntimeError, match="injected"):
        prepare_postgres_replication_key_guards(
            target, manifest=MANIFEST, run_id="run-1"
        )
    assert target.indexes == set()
    target.fail_guard_after = None
    report = prepare_postgres_replication_key_guards(
        target, manifest=MANIFEST, run_id="run-1"
    )
    assert len(report.definitions) == 4
    target.indexes.add("shadow_uq_unknown_replication_key")
    with pytest.raises(ValueError, match="set"):
        prepare_postgres_replication_key_guards(
            target, manifest=MANIFEST, run_id="run-1"
        )
    target.indexes.remove("shadow_uq_unknown_replication_key")
    target.malformed_guard = report.definitions[0].name
    with pytest.raises(ValueError, match="definition mismatch"):
        prepare_postgres_replication_key_guards(
            target, manifest=MANIFEST, run_id="run-1"
        )


def test_offline_report_cas_rowcount_idempotency_capture_ack_retry_and_phase_gate(
    sqlite_capture, monkeypatch
):
    target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(target)
    sqlite_report, postgres_report = _ready_reports(sqlite_capture, monkeypatch, target)
    changed_counts = GuardReport(
        sqlite_report.side,
        sqlite_report.run_id,
        sqlite_report.schema_epoch,
        sqlite_report.database_identity_hash,
        sqlite_report.definitions,
        tuple((table, count + 99) for table, count in sqlite_report.row_counts),
    )
    assert changed_counts.report_hash == sqlite_report.report_hash
    commit_guard_report(target, report=sqlite_report, expected_revision=0)
    commit_guard_report(target, report=changed_counts, expected_revision=999)
    commit_guard_report(target, report=postgres_report, expected_revision=1)

    with pytest.raises(ValueError, match="gate|CAS"):
        update_forward_state(
            target,
            run_id="run-1",
            expected_phase=ShadowPhase.OFF,
            expected_revision=2,
            new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            progress={},
            source_url=sqlite_capture[0],
            source_conn=sqlite_capture[1],
        )
    target.fail_capture_ack_once = True
    with pytest.raises(ValueError, match="acknowledgement"):
        enable_sqlite_capture_after_guard_reports(
            sqlite_capture[1],
            target,
            sqlite_report=sqlite_report,
            source_url=sqlite_capture[0],
            postgres_report=postgres_report,
            manifest=MANIFEST,
        )
    assert (
        sqlite_capture[1]
        .execute("SELECT enabled FROM shadow_capture_control")
        .fetchone()[0]
        == 1
    )
    assert target.run["capture_enabled"] is False
    enable_sqlite_capture_after_guard_reports(
        sqlite_capture[1],
        target,
        sqlite_report=sqlite_report,
        source_url=sqlite_capture[0],
        postgres_report=postgres_report,
        manifest=MANIFEST,
    )
    assert target.run["capture_enabled"] is True
    target.events.clear()
    sqlite_capture[1].set_trace_callback(
        lambda statement: target.events.append(f"sqlite:{statement}")
    )
    target.catalog_drift = "table-acl"
    with pytest.raises(ValueError, match="ACL"):
        update_forward_state(
            target,
            run_id="run-1",
            expected_phase=ShadowPhase.OFF,
            expected_revision=3,
            new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            progress={"stage": "ready"},
            source_url=sqlite_capture[0],
            source_conn=sqlite_capture[1],
        )
    assert not any(event == "sqlite:BEGIN IMMEDIATE" for event in target.events)
    target.catalog_drift = None
    target.events.clear()
    state = update_forward_state(
        target,
        run_id="run-1",
        expected_phase=ShadowPhase.OFF,
        expected_revision=3,
        new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
        progress={"stage": "ready"},
        source_url=sqlite_capture[0],
        source_conn=sqlite_capture[1],
    )
    sqlite_capture[1].set_trace_callback(None)
    begin_offset = target.events.index("sqlite:BEGIN IMMEDIATE")
    before_sqlite = target.events[:begin_offset]
    assert (
        sum("pg:SELECT pg_advisory_xact_lock" in event for event in before_sqlite) == 2
    )
    assert any(
        "FROM pg_namespace WHERE nspname='silicon_shadow'" in event
        for event in before_sqlite
    )
    assert any("FOR UPDATE" in event for event in before_sqlite)
    assert not any(
        "pg_advisory_xact_lock" in event for event in target.events[begin_offset:]
    )
    assert state.phase is ShadowPhase.SQLITE_TO_POSTGRES
    sqlite_capture[1].execute("UPDATE shadow_capture_control SET enabled=0")
    sqlite_capture[1].commit()
    with pytest.raises(ValueError, match="gate"):
        update_forward_state(
            target,
            run_id="run-1",
            expected_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            expected_revision=4,
            new_phase=ShadowPhase.SQLITE_TO_POSTGRES,
            progress={"stage": "forward"},
            source_url=sqlite_capture[0],
            source_conn=sqlite_capture[1],
        )


def test_offline_enable_revalidates_live_guard_drift(sqlite_capture, monkeypatch):
    target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(target)
    sqlite_report, postgres_report = _ready_reports(sqlite_capture, monkeypatch, target)
    commit_guard_report(target, report=sqlite_report, expected_revision=0)
    commit_guard_report(target, report=postgres_report, expected_revision=1)
    target.malformed_guard = postgres_report.definitions[0].name
    with pytest.raises(ValueError, match="definition mismatch"):
        enable_sqlite_capture_after_guard_reports(
            sqlite_capture[1],
            target,
            sqlite_report=sqlite_report,
            source_url=sqlite_capture[0],
            postgres_report=postgres_report,
            manifest=MANIFEST,
        )
    assert (
        sqlite_capture[1]
        .execute("SELECT enabled FROM shadow_capture_control")
        .fetchone()[0]
        == 0
    )


def test_every_control_mutation_rejects_catalog_drift(sqlite_capture, monkeypatch):
    update_target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(update_target)
    update_target.catalog_drift = "column-default"
    with pytest.raises(ValueError, match="column contract"):
        update_forward_state(
            update_target,
            run_id="run-1",
            expected_phase=ShadowPhase.OFF,
            expected_revision=0,
            new_phase=ShadowPhase.OFF,
            progress={"stage": "preflight-confirmed"},
        )

    commit_target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(commit_target)
    sqlite_report, postgres_report = _ready_reports(
        sqlite_capture, monkeypatch, commit_target
    )
    commit_target.catalog_drift = "table-acl"
    with pytest.raises(ValueError, match="ACL"):
        commit_guard_report(commit_target, report=sqlite_report, expected_revision=0)

    commit_target.catalog_drift = None
    commit_guard_report(commit_target, report=sqlite_report, expected_revision=0)
    commit_guard_report(commit_target, report=postgres_report, expected_revision=1)
    original_validate = control._validate_control_schema
    validations = 0

    def drift_on_final_ack(conn):
        nonlocal validations
        validations += 1
        if validations == 2:
            raise ValueError("silicon_shadow final-ack catalog drift")
        original_validate(conn)

    monkeypatch.setattr(control, "_validate_control_schema", drift_on_final_ack)
    with pytest.raises(ValueError, match="final-ack catalog drift"):
        enable_sqlite_capture_after_guard_reports(
            sqlite_capture[1],
            commit_target,
            source_url=sqlite_capture[0],
            sqlite_report=sqlite_report,
            postgres_report=postgres_report,
            manifest=MANIFEST,
        )
    assert validations == 2
    assert commit_target.run["capture_enabled"] is False


@pytest.mark.parametrize(
    "progress",
    [
        {"database_url": "redacted"},
        {"stage": "postgresql://user:pw@host/db"},
        {"unknown": {"safe": {"token": "x"}}},
        {"stage": ["ready", {"cookie": "x"}]},
        {"rows_copied": -1},
        {"snapshot_sha256": "not-a-hash"},
    ],
)
def test_progress_payload_is_a_closed_non_secret_contract(progress):
    with pytest.raises(ValueError, match="progress|forbidden|snapshot"):
        control._validated_progress(progress)


def test_sqlite_report_from_source_a_cannot_enable_prepared_source_b(
    sqlite_capture, monkeypatch, tmp_path
):
    target = _ScriptedTarget(run=_run_row())
    install_postgres_control_schema(target)
    sqlite_report, postgres_report = _ready_reports(sqlite_capture, monkeypatch, target)
    commit_guard_report(target, report=sqlite_report, expected_revision=0)
    commit_guard_report(target, report=postgres_report, expected_revision=1)

    path = tmp_path / "source-b.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    database = SqliteDatabase(settings, REPO_ROOT)
    SqliteMigrator(database, settings).migrate()
    try:
        with database.connect() as source_b:
            install_sqlite_capture(
                source_b, run_id="run-1", schema_epoch=1, manifest=MANIFEST
            )
            with pytest.raises(ValueError, match="identity"):
                enable_sqlite_capture_after_guard_reports(
                    source_b,
                    target,
                    source_url=settings.database_url,
                    sqlite_report=sqlite_report,
                    postgres_report=postgres_report,
                    manifest=MANIFEST,
                )
            assert (
                source_b.execute(
                    "SELECT enabled FROM shadow_capture_control"
                ).fetchone()[0]
                == 0
            )
    finally:
        database.close_local()
