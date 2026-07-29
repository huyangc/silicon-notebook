"""Forward-shadow preflight and PostgreSQL control-plane primitives.

The functions in this module are deliberately not wired into application
startup.  Task 7's explicit operator command will compose them in this order:
read-only preflight, formal PostgreSQL migration, owned-control installation,
two independent guard reports, capture enable, then snapshot/COPY.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from psycopg import sql

from app.migration.shadow.capture import validate_sqlite_capture
from app.migration.shadow.identity import (
    ConfirmationBinding,
    DatabaseFingerprint,
    fingerprint_postgres,
    fingerprint_sqlite,
    issue_confirmation_token,
    reject_same_database,
    verify_confirmation_token,
)
from app.migration.shadow.manifest import (
    MANIFEST,
    replication_guard_specs,
    validate_manifest,
    validate_postgres_connection,
    validate_sqlite_schema,
)
from app.migration.shadow.types import Manifest, ReplicationGuardSpec, SchemaPair
from app.repositories.postgres.migrator import (
    load_migrations,
    migration_advisory_lock_key,
)


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_CONTROL_SCHEMA = "silicon_shadow"
_CONTROL_OBJECTS = frozenset(
    {
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
)
_CONTROL_COLUMN_DEFINITIONS = {
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
_CONTROL_CONSTRAINT_DEFINITIONS = {
    "pk_shadow_runs": ("runs", "p", "PRIMARY KEY (run_id)"),
    "ck_shadow_runs_phase": (
        "runs",
        "c",
        "CHECK (phase IN ('off', 'sqlite_to_postgres', 'cutover_readonly', "
        "'postgres_to_sqlite', 'retired'))",
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
    "ck_shadow_checkpoints_last_seq": (
        "checkpoints",
        "c",
        "CHECK (last_seq >= 0)",
    ),
    "ck_shadow_checkpoints_revision": (
        "checkpoints",
        "c",
        "CHECK (revision >= 0)",
    ),
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
    "ck_shadow_copy_progress_rows": (
        "copy_progress",
        "c",
        "CHECK (rows_copied >= 0)",
    ),
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
        "FOREIGN KEY (verification_id) REFERENCES "
        "silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE",
    ),
    "pk_shadow_verifier_barriers": (
        "verifier_barriers",
        "p",
        "PRIMARY KEY (verification_id)",
    ),
    "fk_shadow_verifier_barriers_verification": (
        "verifier_barriers",
        "f",
        "FOREIGN KEY (verification_id) REFERENCES "
        "silicon_shadow.verification_runs(verification_id) ON DELETE CASCADE",
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
_CONTROL_SQL_PATH = Path(__file__).with_name("sql") / "postgres_control.sql"
# The ``:v1`` here is the control-protocol namespace, not a schema version: it
# is hashed into the advisory-lock key, so bumping it with a schema bump would
# silently hand out a different lock and break mutual exclusion against an
# already-running worker.  It must not track ``RUNNING_SCHEMA_PAIR``.
_CONTROL_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"silicon-notebook:shadow-control:v1").digest()[:8],
    byteorder="big",
    signed=True,
)
_POSTGRES_MIGRATIONS = load_migrations(
    Path(__file__).parents[2] / "repositories" / "postgres" / "migrations"
)
_PROGRESS_INTEGER_FIELDS = {
    "baseline_seq": None,
    "applied_seq": None,
    "tables_complete": 63,
    "rows_copied": None,
    "bytes_copied": None,
    "migration_version": MANIFEST.schema_pair.postgres_version,
}
_PROGRESS_STAGES = {
    "preflight-confirmed",
    "guards-committed",
    "capture-enabled",
    "snapshot",
    "copy",
    "forward",
    "ready",
}
_SENSITIVE_PROGRESS_KEYS = re.compile(
    r"(?:database_?url|url|dsn|conninfo|password|secret|token|credential|api_?key|auth|cookie)",
    re.IGNORECASE,
)
_SENSITIVE_PROGRESS_VALUES = re.compile(
    r"(?:postgres(?:ql)?://|sqlite:///|\bbearer\s|private[-_ ]?(?:key|payload))",
    re.IGNORECASE,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("ascii")).hexdigest()


def _row_value(row: Any, name: str, position: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[position]


def _target_url(target: Any) -> str:
    try:
        value = target.settings.database_url
    except AttributeError as exc:
        raise ValueError("PostgreSQL target does not expose a database URL") from exc
    if not isinstance(value, str):
        raise ValueError("PostgreSQL target database URL is invalid")
    return value


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe shadow manifest identifier: {identifier!r}")
    return f'"{identifier}"'


def _lock_schema_and_control(conn: Any) -> None:
    """Use one global lock order for every PostgreSQL schema/control action."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (migration_advisory_lock_key(),))
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_CONTROL_LOCK_KEY,))


def _reject_sensitive_progress(value: Any, *, key: str = "") -> None:
    if key and _SENSITIVE_PROGRESS_KEYS.search(key):
        raise ValueError("shadow progress contains a forbidden field")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if not isinstance(child_key, str):
                raise ValueError("shadow progress keys must be strings")
            _reject_sensitive_progress(child, key=child_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_sensitive_progress(child)
    elif isinstance(value, str) and _SENSITIVE_PROGRESS_VALUES.search(value):
        raise ValueError("shadow progress contains forbidden text")


def _validated_progress(progress: Mapping[str, Any]) -> str:
    if not isinstance(progress, Mapping):
        raise ValueError("shadow progress must be a mapping")
    _reject_sensitive_progress(progress)
    allowed = {"stage", "snapshot_sha256", *_PROGRESS_INTEGER_FIELDS}
    unknown = set(progress) - allowed
    if unknown:
        raise ValueError("shadow progress contains an unknown field")
    if "stage" in progress and progress["stage"] not in _PROGRESS_STAGES:
        raise ValueError("shadow progress stage is not recognized")
    if "snapshot_sha256" in progress and (
        not isinstance(progress["snapshot_sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", progress["snapshot_sha256"]) is None
    ):
        raise ValueError("shadow snapshot hash is invalid")
    for field, maximum in _PROGRESS_INTEGER_FIELDS.items():
        if field not in progress:
            continue
        value = progress[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (maximum is not None and value > maximum)
        ):
            raise ValueError(f"shadow progress field is invalid: {field}")
    return _canonical_json(dict(progress))


class ShadowPhase(StrEnum):
    OFF = "off"
    SQLITE_TO_POSTGRES = "sqlite_to_postgres"
    CUTOVER_READONLY = "cutover_readonly"
    POSTGRES_TO_SQLITE = "postgres_to_sqlite"
    RETIRED = "retired"


@dataclass(frozen=True)
class DiskEvidence:
    evidence_id: str
    available_bytes: int
    verified: bool


@dataclass(frozen=True)
class BackupEvidence:
    evidence_id: str
    source_backup_verified: bool
    target_restore_prerequisites_verified: bool


@dataclass(frozen=True)
class PreflightReport:
    run_id: str
    source: DatabaseFingerprint
    target: DatabaseFingerprint
    schema_pair: SchemaPair
    target_initial_schema_version: int
    source_database_bytes: int
    source_storage_bytes: int
    source_storage_references: int
    target_database_bytes: int
    required_target_bytes: int
    available_target_bytes: int
    target_pool_max_connections: int
    target_pool_current_connections: int
    pg_trgm_installed: bool
    disk_evidence_id: str
    backup_evidence_id: str
    confirmation_token: str

    @property
    def binding(self) -> ConfirmationBinding:
        return ConfirmationBinding(
            run_id=self.run_id,
            source_identity_hash=self.source.identity_hash,
            target_identity_hash=self.target.identity_hash,
            sqlite_version=self.schema_pair.sqlite_version,
            postgres_version=self.schema_pair.postgres_version,
            schema_epoch=self.schema_pair.epoch,
        )


@dataclass(frozen=True)
class GuardReport:
    side: str
    run_id: str
    schema_epoch: int
    database_identity_hash: str
    definitions: tuple[ReplicationGuardSpec, ...]
    row_counts: tuple[tuple[str, int], ...]
    committed: bool = True

    @property
    def report_hash(self) -> str:
        return _sha256(
            {
                "side": self.side,
                "run_id": self.run_id,
                "schema_epoch": self.schema_epoch,
                "database_identity_hash": self.database_identity_hash,
                "definitions": [
                    {"name": item.name, "table": item.table, "columns": item.columns}
                    for item in self.definitions
                ],
                # Row counts are useful committed audit evidence but are not
                # part of installation identity: normal writes after capture
                # enable must not turn a same-definition retry into a new CAS.
                "committed": self.committed,
            }
        )


@dataclass(frozen=True)
class RunState:
    run_id: str
    source_identity: str
    source_identity_hash: str
    target_identity: str
    target_identity_hash: str
    target_business_schema: str
    schema_pair: SchemaPair
    phase: ShadowPhase
    active_backend: str
    revision: int
    progress: Mapping[str, Any]
    sqlite_guard_report_hash: str | None
    postgres_guard_report_hash: str | None
    capture_enabled: bool


def _validate_evidence(
    disk: DiskEvidence,
    backup: BackupEvidence,
    *,
    required_target_bytes: int,
) -> None:
    if (
        not disk.verified
        or not disk.evidence_id.strip()
        or isinstance(disk.available_bytes, bool)
        or disk.available_bytes < required_target_bytes
    ):
        raise ValueError("verified target disk evidence is insufficient")
    if (
        not backup.evidence_id.strip()
        or not backup.source_backup_verified
        or not backup.target_restore_prerequisites_verified
    ):
        raise ValueError(
            "verified source backup and target restore evidence is required"
        )


def _storage_evidence(
    source_conn: sqlite3.Connection,
    storage_root: Path,
    manifest: Manifest,
) -> tuple[int, int]:
    try:
        root = storage_root.expanduser().resolve(strict=True)
    except OSError:
        raise ValueError("source storage root is unavailable") from None
    if not root.is_dir():
        raise ValueError("source storage root must be a directory")
    references = 0
    total_bytes = 0
    seen: set[Path] = set()
    declared_path_columns = [
        (spec.name, column)
        for spec in manifest.replicated
        for column in spec.path_columns
    ]
    for table, column in declared_path_columns:
        for row in source_conn.execute(
            f"SELECT {_quote(column)} FROM {_quote(table)} "
            f"WHERE {_quote(column)} IS NOT NULL AND {_quote(column)} <> ''"
        ):
            raw = str(_row_value(row, column, 0))
            candidate = Path(raw)
            try:
                resolved = (
                    candidate.resolve(strict=True)
                    if candidate.is_absolute()
                    else (root / candidate).resolve(strict=True)
                )
            except OSError:
                raise ValueError("source file reference is unavailable") from None
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    "source file reference escapes the configured storage root"
                ) from exc
            if not resolved.is_file():
                raise ValueError("source file reference is not a regular file")
            references += 1
            if resolved not in seen:
                seen.add(resolved)
                try:
                    total_bytes += resolved.stat().st_size
                except OSError:
                    raise ValueError(
                        "source file reference metadata is unavailable"
                    ) from None
    return references, total_bytes


def _sqlite_database_bytes(source_path: Path) -> int:
    total = 0
    try:
        for candidate in (
            source_path,
            Path(f"{source_path}-wal"),
            Path(f"{source_path}-shm"),
        ):
            if candidate.is_file():
                total += candidate.stat().st_size
    except OSError:
        raise ValueError("SQLite source size is unavailable") from None
    return total


def preflight(
    *,
    run_id: str,
    source_url: str,
    source_conn: sqlite3.Connection,
    storage_root: str | Path,
    target: Any,
    disk_evidence: DiskEvidence,
    backup_evidence: BackupEvidence,
    confirmation_secret: bytes,
    manifest: Manifest = MANIFEST,
    now: int | None = None,
) -> PreflightReport:
    """Perform a strictly read-only initial-target preflight.

    The first statement issued by this function to the target checks
    ``server_encoding``.  All target statements in this function are SELECTs;
    schema migration, control installation, guards, capture, and snapshots are
    intentionally separate later operations.
    """
    validate_manifest(manifest)
    if not run_id or run_id.strip() != run_id:
        raise ValueError("run_id must be a non-empty canonical string")
    source = fingerprint_sqlite(source_url, source_conn)
    validate_sqlite_schema(
        source_conn,
        manifest,
        expected_pair=manifest.schema_pair,
    )
    storage_path = Path(storage_root)
    storage_references, storage_bytes = _storage_evidence(
        source_conn, storage_path, manifest
    )
    try:
        source_path = Path(
            next(
                str(_row_value(row, "file", 2))
                for row in source_conn.execute("PRAGMA database_list")
                if str(_row_value(row, "name", 1)) == "main"
            )
        ).resolve(strict=True)
    except (OSError, StopIteration):
        raise ValueError("SQLite source path is unavailable") from None
    source_bytes = _sqlite_database_bytes(source_path)
    # Shared source files are verified in place and are never copied into PG.
    # Target relational capacity is therefore based on the SQLite database,
    # while storage bytes remain separately visible in the report.
    required_target_bytes = max(source_bytes, 1)
    required_target_bytes = (required_target_bytes * 13 + 9) // 10
    _validate_evidence(
        disk_evidence,
        backup_evidence,
        required_target_bytes=required_target_bytes,
    )

    with target.connect() as conn:
        # This must remain the first target compatibility check.  In
        # particular, do not move identity/catalog convenience reads above it.
        encoding_row = conn.execute(
            "SELECT current_setting('server_encoding') AS server_encoding"
        ).fetchone()
        if str(_row_value(encoding_row, "server_encoding", 0)) != "UTF8":
            raise ValueError("PostgreSQL server_encoding must be UTF8")

        identity_row = conn.execute(
            "SELECT current_database() AS database, current_schema() AS schema, "
            "(SELECT oid::text FROM pg_database WHERE datname=current_database()) AS database_oid"
        ).fetchone()
        target_identity = fingerprint_postgres(
            _target_url(target), conn, identity_row=identity_row
        )
        reject_same_database(source, target_identity)
        schema = str(_row_value(identity_row, "schema", 1))
        if not _IDENTIFIER.fullmatch(schema):
            raise ValueError("PostgreSQL target business schema name is unsupported")

        shadow_row = conn.execute(
            "SELECT nspowner::regrole::text AS owner, obj_description(oid, 'pg_namespace') AS comment "
            "FROM pg_namespace WHERE nspname='silicon_shadow'"
        ).fetchone()
        if shadow_row is not None:
            raise ValueError("PostgreSQL target already has a silicon_shadow schema")

        schema_row = conn.execute(
            "SELECT n.nspowner::regrole::text AS owner, current_user AS current_user, "
            "pg_has_role(current_user,n.nspowner,'USAGE') AS owner_authorized, "
            "has_schema_privilege(current_user, n.oid, 'USAGE') AS can_use, "
            "has_schema_privilege(current_user, n.oid, 'CREATE') AS can_create "
            "FROM pg_namespace n WHERE n.nspname=current_schema()"
        ).fetchone()
        if schema_row is None:
            raise ValueError("PostgreSQL target business schema does not exist")
        if not bool(_row_value(schema_row, "owner_authorized", 2)):
            raise ValueError(
                "PostgreSQL target business schema owner role is unavailable"
            )
        if not bool(_row_value(schema_row, "can_use", 3)) or not bool(
            _row_value(schema_row, "can_create", 4)
        ):
            raise ValueError(
                "PostgreSQL target business schema privileges are insufficient"
            )

        database_privileges = conn.execute(
            "SELECT has_database_privilege(current_user, current_database(), 'CONNECT') AS can_connect, "
            "has_database_privilege(current_user, current_database(), 'CREATE') AS can_create, "
            "has_database_privilege(current_user, current_database(), 'TEMP') AS can_temp"
        ).fetchone()
        if not all(
            bool(_row_value(database_privileges, name, position))
            for position, name in enumerate(("can_connect", "can_create", "can_temp"))
        ):
            raise ValueError("PostgreSQL target database privileges are insufficient")

        objects = conn.execute(
            "WITH objects(object_kind,object_name,class_id,object_id) AS ("
            "SELECT 'relation'::text,c.relname::text,'pg_class'::regclass::oid,c.oid "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'routine',p.proname,'pg_proc'::regclass::oid,p.oid "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'type',t.typname,'pg_type'::regclass::oid,t.oid "
            "FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'collation',c.collname,'pg_collation'::regclass::oid,c.oid "
            "FROM pg_collation c JOIN pg_namespace n ON n.oid=c.collnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'operator',o.oprname,'pg_operator'::regclass::oid,o.oid "
            "FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'opclass',o.opcname,'pg_opclass'::regclass::oid,o.oid "
            "FROM pg_opclass o JOIN pg_namespace n ON n.oid=o.opcnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'opfamily',o.opfname,'pg_opfamily'::regclass::oid,o.oid "
            "FROM pg_opfamily o JOIN pg_namespace n ON n.oid=o.opfnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'conversion',c.conname,'pg_conversion'::regclass::oid,c.oid "
            "FROM pg_conversion c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'ts_config',c.cfgname,'pg_ts_config'::regclass::oid,c.oid "
            "FROM pg_ts_config c JOIN pg_namespace n ON n.oid=c.cfgnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'ts_dict',d.dictname,'pg_ts_dict'::regclass::oid,d.oid "
            "FROM pg_ts_dict d JOIN pg_namespace n ON n.oid=d.dictnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'ts_parser',p.prsname,'pg_ts_parser'::regclass::oid,p.oid "
            "FROM pg_ts_parser p JOIN pg_namespace n ON n.oid=p.prsnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'ts_template',t.tmplname,'pg_ts_template'::regclass::oid,t.oid "
            "FROM pg_ts_template t JOIN pg_namespace n ON n.oid=t.tmplnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'statistics',s.stxname,'pg_statistic_ext'::regclass::oid,s.oid "
            "FROM pg_statistic_ext s JOIN pg_namespace n ON n.oid=s.stxnamespace WHERE n.nspname=current_schema() "
            "UNION ALL SELECT 'extension',e.extname,'pg_extension'::regclass::oid,e.oid "
            "FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE n.nspname=current_schema()"
            ") SELECT object_kind,object_name FROM objects WHERE NOT ("
            "current_schema()='public' AND ((object_kind='extension' AND object_name='pg_trgm') OR "
            "EXISTS (SELECT 1 FROM pg_depend d JOIN pg_extension e "
            "ON d.refclassid='pg_extension'::regclass AND d.refobjid=e.oid "
            "JOIN pg_namespace n ON n.oid=e.extnamespace "
            "WHERE d.classid=objects.class_id AND d.objid=objects.object_id "
            "AND d.deptype='e' AND e.extname='pg_trgm' AND n.nspname='public'))) "
            "ORDER BY object_kind,object_name"
        ).fetchall()
        if objects:
            raise ValueError("PostgreSQL target business schema must be empty")
        target_initial_schema_version = 0

        extension = conn.execute(
            "SELECT a.installed_version,a.default_version,n.nspname AS extension_schema, "
            "public.oid IS NOT NULL AS public_exists, "
            "CASE WHEN public.oid IS NULL THEN false ELSE has_schema_privilege(current_user,public.oid,'USAGE') END AS public_usage, "
            "CASE WHEN public.oid IS NULL THEN false ELSE has_schema_privilege(current_user,public.oid,'CREATE') END AS public_create "
            "FROM pg_available_extensions a LEFT JOIN pg_extension e ON e.extname=a.name "
            "LEFT JOIN pg_namespace n ON n.oid=e.extnamespace "
            "LEFT JOIN pg_namespace public ON public.nspname='public' WHERE a.name='pg_trgm'"
        ).fetchone()
        if extension is None or _row_value(extension, "default_version", 1) is None:
            raise ValueError("PostgreSQL pg_trgm extension is unavailable")
        pg_trgm_installed = _row_value(extension, "installed_version", 0) is not None
        extension_schema = _row_value(extension, "extension_schema", 2)
        if pg_trgm_installed and extension_schema != "public":
            raise ValueError("PostgreSQL pg_trgm must be installed in public")
        public_exists = bool(_row_value(extension, "public_exists", 3))
        public_usage = bool(_row_value(extension, "public_usage", 4))
        public_create = bool(_row_value(extension, "public_create", 5))
        if (
            not public_exists
            or not public_usage
            or (not pg_trgm_installed and not public_create)
        ):
            raise ValueError(
                "PostgreSQL public schema privileges are insufficient for pg_trgm"
            )

        capacity = conn.execute(
            "SELECT current_setting('max_connections')::integer AS max_connections, "
            "current_setting('superuser_reserved_connections')::integer AS reserved_connections, "
            "(SELECT count(*)::integer FROM pg_stat_activity WHERE datname=current_database()) AS current_connections"
        ).fetchone()
        max_connections = int(_row_value(capacity, "max_connections", 0))
        reserved_connections = int(_row_value(capacity, "reserved_connections", 1))
        current_connections = int(_row_value(capacity, "current_connections", 2))
        configured_pool_max = int(target.settings.postgres_pool_max_size)
        if (
            configured_pool_max <= 0
            or configured_pool_max > max_connections - reserved_connections
        ):
            raise ValueError(
                "configured PostgreSQL pool exceeds usable server connections"
            )

        size_row = conn.execute(
            "SELECT pg_database_size(current_database())::bigint AS database_bytes"
        ).fetchone()
        target_database_bytes = int(_row_value(size_row, "database_bytes", 0))

    binding = ConfirmationBinding(
        run_id=run_id,
        source_identity_hash=source.identity_hash,
        target_identity_hash=target_identity.identity_hash,
        sqlite_version=manifest.schema_pair.sqlite_version,
        postgres_version=manifest.schema_pair.postgres_version,
        schema_epoch=manifest.schema_pair.epoch,
    )
    token = issue_confirmation_token(
        binding,
        secret=confirmation_secret,
        now=now,
    )
    return PreflightReport(
        run_id=run_id,
        source=source,
        target=target_identity,
        schema_pair=manifest.schema_pair,
        target_initial_schema_version=target_initial_schema_version,
        source_database_bytes=source_bytes,
        source_storage_bytes=storage_bytes,
        source_storage_references=storage_references,
        target_database_bytes=target_database_bytes,
        required_target_bytes=required_target_bytes,
        available_target_bytes=disk_evidence.available_bytes,
        target_pool_max_connections=configured_pool_max,
        target_pool_current_connections=current_connections,
        pg_trgm_installed=pg_trgm_installed,
        disk_evidence_id=disk_evidence.evidence_id,
        backup_evidence_id=backup_evidence.evidence_id,
        confirmation_token=token,
    )


def control_sql_checksum() -> str:
    return hashlib.sha256(_CONTROL_SQL_PATH.read_bytes()).hexdigest()


def _control_schema_comment() -> str:
    # Same deliberate ``v1``: the control-schema protocol generation, stamped on
    # the deployed schema and compared verbatim.  Not a business schema version.
    return f"silicon-notebook shadow control v1 sha256:{control_sql_checksum()}"


def _normalize_constraint_definition(value: Any) -> str:
    definition = re.sub(r"\s+", " ", str(value).strip()).replace('"', "")
    definition = re.sub(r"::(?:text|bpchar|character varying)", "", definition)
    definition = re.sub(
        r"([a-z_][a-z0-9_]*)\s*=\s*ANY\s*\(\s*ARRAY\[(.*?)\]\s*\)",
        r"\1 IN (\2)",
        definition,
        flags=re.IGNORECASE,
    )
    if definition.upper().startswith("CHECK "):
        expression = definition[6:].strip()
        while expression.startswith("(") and expression.endswith(")"):
            depth = 0
            wraps_whole_expression = True
            for offset, character in enumerate(expression):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0 and offset != len(expression) - 1:
                        wraps_whole_expression = False
                        break
            if not wraps_whole_expression or depth != 0:
                break
            expression = expression[1:-1].strip()
        definition = f"CHECK ({expression})"
    definition = re.sub(r"\s*,\s*", ", ", definition)
    return definition


def _normalize_column_default(formatted_type: str, value: Any) -> str | None:
    if value is None:
        return None
    default = re.sub(r"\s+", " ", str(value).strip())
    if formatted_type == "bigint" and re.fullmatch(r"(?:'0'|0)(?:::bigint)?", default):
        return "0"
    return default


def _validate_control_schema(conn: Any) -> None:
    schema = conn.execute(
        "SELECT nspowner::regrole::text AS owner, current_user AS current_user, "
        "obj_description(oid, 'pg_namespace') AS comment, "
        "NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(nspacl, "
        "acldefault('n', nspowner))) acl WHERE acl.grantee<>nspowner) AS owner_only_acl "
        "FROM pg_namespace WHERE nspname='silicon_shadow'"
    ).fetchone()
    if schema is None:
        raise ValueError("silicon_shadow control schema is missing")
    if str(_row_value(schema, "owner", 0)) != str(
        _row_value(schema, "current_user", 1)
    ):
        raise ValueError("silicon_shadow control schema has an unexpected owner")
    if str(_row_value(schema, "comment", 2)) != _control_schema_comment():
        raise ValueError("silicon_shadow control schema checksum is unrecognized")
    if not bool(_row_value(schema, "owner_only_acl", 3)):
        raise ValueError("silicon_shadow control schema ACL is unrecognized")
    table_rows = conn.execute(
        "SELECT c.relname AS table_name, c.relowner::regrole::text=current_user AS owned, "
        "c.relpersistence, c.relrowsecurity, c.relforcerowsecurity, "
        "NOT EXISTS (SELECT 1 FROM aclexplode(COALESCE(c.relacl, "
        "acldefault('r', c.relowner))) acl WHERE acl.grantee<>c.relowner) AS owner_only_acl "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='silicon_shadow' AND c.relkind='r' ORDER BY c.relname"
    ).fetchall()
    objects = {str(_row_value(row, "table_name", 0)) for row in table_rows}
    if objects != _CONTROL_OBJECTS:
        raise ValueError("silicon_shadow control schema object set is unrecognized")
    for row in table_rows:
        if not bool(_row_value(row, "owned", 1)):
            raise ValueError("silicon_shadow control table owner is unrecognized")
        if str(_row_value(row, "relpersistence", 2)) != "p":
            raise ValueError("silicon_shadow control table persistence is unrecognized")
        if bool(_row_value(row, "relrowsecurity", 3)) or bool(
            _row_value(row, "relforcerowsecurity", 4)
        ):
            raise ValueError("silicon_shadow control table RLS is unrecognized")
        if not bool(_row_value(row, "owner_only_acl", 5)):
            raise ValueError("silicon_shadow control table ACL is unrecognized")
    column_rows = conn.execute(
        "SELECT c.relname AS table_name, a.attname AS column_name, "
        "format_type(a.atttypid,a.atttypmod) AS formatted_type, a.attnotnull, "
        "pg_get_expr(d.adbin,d.adrelid) AS default_expression, coll.collname AS collation, "
        "a.attidentity, a.attgenerated "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped "
        "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
        "LEFT JOIN pg_collation coll ON coll.oid=a.attcollation "
        "WHERE n.nspname='silicon_shadow' AND c.relkind='r' "
        "ORDER BY c.relname,a.attnum"
    ).fetchall()
    actual_columns: dict[str, list[tuple[str, str, bool, str | None, str | None]]] = {
        table: [] for table in _CONTROL_OBJECTS
    }
    for row in column_rows:
        table = str(_row_value(row, "table_name", 0))
        if table not in actual_columns:
            raise ValueError("silicon_shadow control schema column owner is unknown")
        identity = str(_row_value(row, "attidentity", 6) or "")
        generated = str(_row_value(row, "attgenerated", 7) or "")
        if identity or generated:
            raise ValueError("silicon_shadow generated column contract is unrecognized")
        default = _row_value(row, "default_expression", 4)
        collation = _row_value(row, "collation", 5)
        actual_columns[table].append(
            (
                str(_row_value(row, "column_name", 1)),
                str(_row_value(row, "formatted_type", 2)),
                bool(_row_value(row, "attnotnull", 3)),
                _normalize_column_default(
                    str(_row_value(row, "formatted_type", 2)), default
                ),
                None if collation is None else str(collation),
            )
        )
    expected_columns = {
        table: list(columns) for table, columns in _CONTROL_COLUMN_DEFINITIONS.items()
    }
    if actual_columns != expected_columns:
        raise ValueError("silicon_shadow control column contract is unrecognized")
    constraint_rows = conn.execute(
        "SELECT c.relname AS table_name, k.conname, k.contype, "
        "pg_get_constraintdef(k.oid, false) AS definition, k.condeferrable, "
        "k.condeferred, k.convalidated "
        "FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='silicon_shadow' ORDER BY c.relname,k.conname"
    ).fetchall()
    actual_constraints: dict[str, tuple[str, str, str]] = {}
    for row in constraint_rows:
        table = str(_row_value(row, "table_name", 0))
        if table not in _CONTROL_OBJECTS:
            raise ValueError("silicon_shadow constraint belongs to an unknown table")
        if (
            bool(_row_value(row, "condeferrable", 4))
            or bool(_row_value(row, "condeferred", 5))
            or not bool(_row_value(row, "convalidated", 6))
        ):
            raise ValueError("silicon_shadow constraint flags are unrecognized")
        actual_constraints[str(_row_value(row, "conname", 1))] = (
            table,
            str(_row_value(row, "contype", 2)),
            _normalize_constraint_definition(_row_value(row, "definition", 3)),
        )
    expected_constraints = {
        name: (table, kind, _normalize_constraint_definition(definition))
        for name, (table, kind, definition) in _CONTROL_CONSTRAINT_DEFINITIONS.items()
    }
    if actual_constraints != expected_constraints:
        raise ValueError("silicon_shadow control constraint contract is unrecognized")
    unexpected = conn.execute(
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='silicon_shadow' AND c.relkind IN ('v','m','S','f','p') LIMIT 1"
    ).fetchone()
    if unexpected is not None:
        raise ValueError("silicon_shadow control schema contains unknown objects")
    unexpected_index = conn.execute(
        "SELECT 1 FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace "
        "WHERE n.nspname='silicon_shadow' AND x.relkind='i' "
        "AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid=x.oid) LIMIT 1"
    ).fetchone()
    if unexpected_index is not None:
        raise ValueError("silicon_shadow control schema contains an unknown index")
    unexpected_function = conn.execute(
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='silicon_shadow' LIMIT 1"
    ).fetchone()
    if unexpected_function is not None:
        raise ValueError("silicon_shadow control schema contains an unknown function")
    unexpected_trigger = conn.execute(
        "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='silicon_shadow' AND NOT t.tgisinternal LIMIT 1"
    ).fetchone()
    if unexpected_trigger is not None:
        raise ValueError("silicon_shadow control schema contains an unknown trigger")
    unexpected_type = conn.execute(
        "SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
        "LEFT JOIN pg_class c ON c.oid=t.typrelid "
        "WHERE n.nspname='silicon_shadow' "
        "AND NOT ((t.typrelid<>0 AND c.relname=ANY(%s)) OR "
        "(t.typelem<>0 AND EXISTS (SELECT 1 FROM pg_type element_type "
        "JOIN pg_class element_class ON element_class.oid=element_type.typrelid "
        "WHERE element_type.oid=t.typelem AND element_class.relname=ANY(%s)))) LIMIT 1",
        (sorted(_CONTROL_OBJECTS), sorted(_CONTROL_OBJECTS)),
    ).fetchone()
    if unexpected_type is not None:
        raise ValueError("silicon_shadow control schema contains an unknown type")
    unexpected_policy = conn.execute(
        "SELECT 1 FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='silicon_shadow' LIMIT 1"
    ).fetchone()
    if unexpected_policy is not None:
        raise ValueError("silicon_shadow control schema contains an unknown policy")


def install_postgres_control_schema(target: Any) -> str:
    """Install or verify the removable, checksummed owned control schema."""
    sql_text = _CONTROL_SQL_PATH.read_text(encoding="utf-8")
    checksum = control_sql_checksum()
    with target.write() as conn:
        _lock_schema_and_control(conn)
        existing = conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname='silicon_shadow'"
        ).fetchone()
        if existing is None:
            conn.execute(sql_text)
            conn.execute(
                f"COMMENT ON SCHEMA silicon_shadow IS '{_control_schema_comment()}'"
            )
        _validate_control_schema(conn)
    return checksum


def _state_from_row(row: Any) -> RunState:
    raw_progress = _row_value(row, "progress", 11)
    progress = (
        json.loads(raw_progress) if isinstance(raw_progress, str) else raw_progress
    )
    if not isinstance(progress, Mapping):
        raise ValueError("shadow run progress is not a JSON object")
    return RunState(
        run_id=str(_row_value(row, "run_id", 0)),
        source_identity=str(_row_value(row, "source_identity", 1)),
        source_identity_hash=str(_row_value(row, "source_identity_hash", 2)),
        target_identity=str(_row_value(row, "target_identity", 3)),
        target_identity_hash=str(_row_value(row, "target_identity_hash", 4)),
        target_business_schema=str(_row_value(row, "target_business_schema", 5)),
        schema_pair=SchemaPair(
            sqlite_version=int(_row_value(row, "sqlite_version", 6)),
            postgres_version=int(_row_value(row, "postgres_version", 7)),
            epoch=int(_row_value(row, "schema_epoch", 8)),
        ),
        phase=ShadowPhase(str(_row_value(row, "phase", 9))),
        active_backend=str(_row_value(row, "active_backend", 10)),
        revision=int(_row_value(row, "revision", 12)),
        progress=dict(progress),
        sqlite_guard_report_hash=(
            str(value)
            if (value := _row_value(row, "sqlite_guard_report_hash", 13)) is not None
            else None
        ),
        postgres_guard_report_hash=(
            str(value)
            if (value := _row_value(row, "postgres_guard_report_hash", 14)) is not None
            else None
        ),
        capture_enabled=bool(_row_value(row, "capture_enabled", 15)),
    )


_RUN_SELECT = (
    "SELECT run_id,source_identity,source_identity_hash,target_identity,target_identity_hash,"
    "target_business_schema,sqlite_version,postgres_version,schema_epoch,phase,active_backend,"
    "progress,revision,sqlite_guard_report_hash,postgres_guard_report_hash,capture_enabled "
    "FROM silicon_shadow.runs"
)


def create_or_reuse_run(
    target: Any,
    report: PreflightReport,
    *,
    confirmation_token: str,
    confirmation_secret: bytes,
    now: int | None = None,
) -> RunState:
    """CAS state with a fixed PG-first lock order.

    The PostgreSQL pool, both advisory locks, the exact control schema, and the
    run row are acquired before SQLite ``BEGIN IMMEDIATE``.  SQLite is held only
    across its local live-gate check and the CAS on that already-locked PG row.
    """
    verify_confirmation_token(
        confirmation_token,
        report.binding,
        secret=confirmation_secret,
        now=now,
    )
    with target.write() as conn:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        identity_row = conn.execute(
            "SELECT current_database() AS database,current_schema() AS schema,"
            "(SELECT oid::text FROM pg_database "
            "WHERE datname=current_database()) AS database_oid"
        ).fetchone()
        if identity_row is None:
            raise ValueError("live PostgreSQL target identity is unavailable")
        live_target = fingerprint_postgres(
            _target_url(target), conn, identity_row=identity_row
        )
        if live_target.identity_hash != report.target.identity_hash:
            raise ValueError(
                "confirmation target identity no longer matches the live target"
            )
        schema = _row_value(identity_row, "schema", 1)
        if not isinstance(schema, str) or not schema:
            raise ValueError("live PostgreSQL target schema is unavailable")
        if _postgres_schema_version(conn) != report.schema_pair.postgres_version:
            raise ValueError("live target schema pair changed after preflight")
        existing = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (report.run_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO silicon_shadow.runs "
                "(run_id,source_identity,source_identity_hash,target_identity,target_identity_hash,"
                "target_business_schema,sqlite_version,postgres_version,schema_epoch,phase,active_backend) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'off','sqlite')",
                (
                    report.run_id,
                    report.source.redacted_identity,
                    report.source.identity_hash,
                    report.target.redacted_identity,
                    report.target.identity_hash,
                    schema,
                    report.schema_pair.sqlite_version,
                    report.schema_pair.postgres_version,
                    report.schema_pair.epoch,
                ),
            )
            existing = conn.execute(
                f"{_RUN_SELECT} WHERE run_id=%s", (report.run_id,)
            ).fetchone()
        state = _state_from_row(existing)
        if (
            state.source_identity_hash != report.source.identity_hash
            or state.target_identity_hash != report.target.identity_hash
            or state.schema_pair != report.schema_pair
            or state.target_business_schema != schema
        ):
            raise ValueError("shadow run id is already bound to different identities")
        return state


def get_run(target: Any, run_id: str) -> RunState:
    with target.connect() as conn:
        row = conn.execute(f"{_RUN_SELECT} WHERE run_id=%s", (run_id,)).fetchone()
    if row is None:
        raise ValueError("shadow run does not exist")
    return _state_from_row(row)


def update_forward_state(
    target: Any,
    *,
    run_id: str,
    expected_phase: ShadowPhase,
    expected_revision: int,
    new_phase: ShadowPhase,
    progress: Mapping[str, Any],
    source_url: str | None = None,
    source_conn: sqlite3.Connection | None = None,
    manifest: Manifest = MANIFEST,
) -> RunState:
    if expected_phase not in {ShadowPhase.OFF, ShadowPhase.SQLITE_TO_POSTGRES}:
        raise ValueError("forward control cannot update a later shadow phase")
    allowed = new_phase == expected_phase or (
        expected_phase is ShadowPhase.OFF
        and new_phase is ShadowPhase.SQLITE_TO_POSTGRES
    )
    if not allowed:
        raise ValueError("illegal forward shadow phase transition")
    encoded = _validated_progress(progress)
    needs_live_capture = new_phase is ShadowPhase.SQLITE_TO_POSTGRES
    if needs_live_capture and (source_url is None or source_conn is None):
        raise ValueError("live SQLite capture evidence is required for forward state")
    if source_conn is not None and source_conn.in_transaction:
        raise ValueError("forward state update requires an idle SQLite connection")
    sqlite_locked = False
    try:
        with target.write() as conn:
            _lock_schema_and_control(conn)
            _validate_control_schema(conn)
            row = conn.execute(
                f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError("shadow run does not exist")
            state = _state_from_row(row)
            if (
                state.phase is not expected_phase
                or state.revision != expected_revision
                or state.active_backend != "sqlite"
                or (
                    new_phase is ShadowPhase.SQLITE_TO_POSTGRES
                    and (
                        not state.capture_enabled
                        or state.sqlite_guard_report_hash is None
                        or state.postgres_guard_report_hash is None
                    )
                )
            ):
                raise ValueError("shadow state CAS failed")
            if needs_live_capture:
                source_conn.execute("BEGIN IMMEDIATE")
                sqlite_locked = True
                _validate_live_sqlite_capture(
                    source_url, source_conn, state=state, manifest=manifest
                )
            cursor = conn.execute(
                "UPDATE silicon_shadow.runs SET phase=%s,progress=%s::jsonb,"
                "revision=revision+1,updated_at=clock_timestamp() "
                "WHERE run_id=%s AND phase=%s AND revision=%s "
                "AND active_backend='sqlite' "
                "AND (%s <> 'sqlite_to_postgres' OR (capture_enabled=true "
                "AND sqlite_guard_report_hash IS NOT NULL "
                "AND postgres_guard_report_hash IS NOT NULL)) RETURNING run_id",
                (
                    new_phase.value,
                    encoded,
                    run_id,
                    expected_phase.value,
                    expected_revision,
                    new_phase.value,
                ),
            )
            if cursor.fetchone() is None:
                raise ValueError("shadow state CAS failed")
            row = conn.execute(f"{_RUN_SELECT} WHERE run_id=%s", (run_id,)).fetchone()
            result = _state_from_row(row)
        if sqlite_locked:
            source_conn.commit()
        return result
    except BaseException:
        if sqlite_locked:
            source_conn.rollback()
        raise


def _guard_definitions(manifest: Manifest) -> tuple[ReplicationGuardSpec, ...]:
    return replication_guard_specs(manifest)


def _guard_sql(definition: ReplicationGuardSpec, business_schema: str) -> Any:
    return sql.SQL("CREATE UNIQUE INDEX {} ON {}.{} ({})").format(
        sql.Identifier(definition.name),
        sql.Identifier(business_schema),
        sql.Identifier(definition.table),
        sql.SQL(", ").join(sql.Identifier(column) for column in definition.columns),
    )


def _execute_guard_statement(conn: Any, statement: Any) -> None:
    """Atomic-failure injection seam for guard installation tests."""
    conn.execute(statement)


def _verify_postgres_guard(
    conn: Any, definition: ReplicationGuardSpec, *, business_schema: str
) -> None:
    row = conn.execute(
        "SELECT t.relname AS table_name, i.indisunique, i.indisvalid, "
        "i.indisready,i.indislive,NOT i.indnullsnotdistinct AS nulls_distinct, "
        "i.indpred IS NULL AS no_predicate, i.indexprs IS NULL AS no_expression, "
        "ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord) "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
        "WHERE k.ord <= i.indnkeyatts ORDER BY k.ord) AS columns, "
        "i.indnatts=i.indnkeyatts AS no_include, am.amname AS access_method, "
        "NOT EXISTS (SELECT 1 FROM unnest(i.indclass) WITH ORDINALITY k(opcoid,ord) "
        "JOIN pg_opclass opc ON opc.oid=k.opcoid "
        "WHERE k.ord <= i.indnkeyatts AND NOT opc.opcdefault) AS default_opclasses, "
        "NOT EXISTS (SELECT 1 FROM unnest(i.indoption) WITH ORDINALITY k(option,ord) "
        "WHERE k.ord <= i.indnkeyatts AND k.option <> 0) AS default_order, "
        "NOT EXISTS (SELECT 1 FROM unnest(i.indkey) WITH ORDINALITY k(attnum,ord) "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=k.attnum "
        "JOIN pg_type ty ON ty.oid=a.atttypid "
        "LEFT JOIN pg_collation co ON co.oid=a.attcollation "
        "LEFT JOIN pg_namespace cn ON cn.oid=co.collnamespace "
        "WHERE k.ord <= i.indnkeyatts AND ty.typcollation <> 0 "
        "AND (co.collname IS DISTINCT FROM 'C' "
        "OR cn.nspname IS DISTINCT FROM 'pg_catalog')) AS reviewed_collation "
        "FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace "
        "JOIN pg_index i ON i.indexrelid=x.oid JOIN pg_class t ON t.oid=i.indrelid "
        "JOIN pg_am am ON am.oid=x.relam "
        "WHERE n.nspname=%s AND x.relname=%s",
        (business_schema, definition.name),
    ).fetchone()
    if row is None:
        raise ValueError(f"PostgreSQL shadow guard is missing: {definition.name}")
    columns = tuple(str(value) for value in _row_value(row, "columns", 8))
    if (
        str(_row_value(row, "table_name", 0)) != definition.table
        or not bool(_row_value(row, "indisunique", 1))
        or not bool(_row_value(row, "indisvalid", 2))
        or not bool(_row_value(row, "indisready", 3))
        or not bool(_row_value(row, "indislive", 4))
        or not bool(_row_value(row, "nulls_distinct", 5))
        or not bool(_row_value(row, "no_predicate", 6))
        or not bool(_row_value(row, "no_expression", 7))
        or columns != definition.columns
        or not bool(_row_value(row, "no_include", 9))
        or str(_row_value(row, "access_method", 10)) != "btree"
        or not bool(_row_value(row, "default_opclasses", 11))
        or not bool(_row_value(row, "default_order", 12))
        or not bool(_row_value(row, "reviewed_collation", 13))
    ):
        raise ValueError(
            f"PostgreSQL shadow guard definition mismatch: {definition.name}"
        )


def _validate_postgres_guard_set(
    conn: Any,
    definitions: tuple[ReplicationGuardSpec, ...],
    *,
    allow_missing: bool,
    business_schema: str,
) -> None:
    actual = {
        str(_row_value(row, "index_name", 0))
        for row in conn.execute(
            "SELECT x.relname AS index_name FROM pg_class x "
            "JOIN pg_namespace n ON n.oid=x.relnamespace "
            "WHERE n.nspname=%s AND x.relkind='i' "
            "AND x.relname LIKE %s ORDER BY x.relname",
            (business_schema, "shadow_uq_%"),
        ).fetchall()
    }
    expected = {definition.name for definition in definitions}
    if actual - expected or (not allow_missing and actual != expected):
        raise ValueError("PostgreSQL shadow replication-key guard set is unrecognized")


def _postgres_schema_version(conn: Any, business_schema: str | None = None) -> int:
    if business_schema is None:
        relation = conn.execute(
            "SELECT c.oid AS relation FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=current_schema() AND c.relname=%s AND c.relkind='r'",
            ("silicon_schema_migrations",),
        ).fetchone()
        ledger: Any = sql.Identifier("silicon_schema_migrations")
    else:
        relation = conn.execute(
            "SELECT c.oid AS relation FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND c.relkind='r'",
            (business_schema, "silicon_schema_migrations"),
        ).fetchone()
        ledger = sql.SQL("{}.{}").format(
            sql.Identifier(business_schema), sql.Identifier("silicon_schema_migrations")
        )
    if relation is None or _row_value(relation, "relation", 0) is None:
        return 0
    rows = (
        conn.execute(
            "SELECT version,checksum FROM silicon_schema_migrations ORDER BY version"
        ).fetchall()
        if business_schema is None
        else conn.execute(
            sql.SQL("SELECT version,checksum FROM {} ORDER BY version").format(ledger)
        ).fetchall()
    )
    versions = [int(_row_value(row, "version", 0)) for row in rows]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError("PostgreSQL migration ledger is not contiguous")
    if len(versions) > len(_POSTGRES_MIGRATIONS):
        raise ValueError("PostgreSQL migration ledger has a future schema version")
    for row, migration in zip(rows, _POSTGRES_MIGRATIONS[: len(rows)], strict=True):
        if str(_row_value(row, "checksum", 1)) != migration.checksum:
            raise ValueError("PostgreSQL migration ledger checksum mismatch")
    return versions[-1] if versions else 0


def prepare_postgres_replication_key_guards(
    target: Any,
    *,
    manifest: Manifest,
    run_id: str,
) -> GuardReport:
    """Atomically scan, install, and exactly verify all four PG key guards."""
    validate_manifest(manifest)
    if not run_id or run_id.strip() != run_id:
        raise ValueError("run_id must be a non-empty canonical string")
    definitions = _guard_definitions(manifest)
    if len(definitions) != 4:
        raise ValueError(
            "epoch-1 shadow manifest must define exactly four logical guards"
        )
    row_counts: list[tuple[str, int]] = []
    with target.write() as conn:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        run_row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if run_row is None:
            raise ValueError(
                "shadow run must exist before PostgreSQL guard installation"
            )
        run_state = _state_from_row(run_row)
        business_schema = run_state.target_business_schema
        version = _postgres_schema_version(conn, business_schema)
        if version != manifest.schema_pair.postgres_version:
            raise ValueError(
                "PostgreSQL target must be formally migrated to the exact running schema pair before guards"
            )
        validate_postgres_connection(
            conn,
            manifest,
            schema_version=version,
            expected_pair=manifest.schema_pair,
            business_schema=business_schema,
        )
        identity = fingerprint_postgres(_target_url(target), conn)
        if (
            run_state.phase is not ShadowPhase.OFF
            or run_state.schema_pair != manifest.schema_pair
            or run_state.target_identity_hash != identity.identity_hash
        ):
            raise ValueError("PostgreSQL guard target identity or run state changed")
        _validate_postgres_guard_set(
            conn,
            definitions,
            allow_missing=True,
            business_schema=business_schema,
        )
        for definition in definitions:
            table = sql.SQL("{}.{}").format(
                sql.Identifier(business_schema), sql.Identifier(definition.table)
            )
            columns = tuple(sql.Identifier(column) for column in definition.columns)
            nulls = sql.SQL(" OR ").join(
                sql.SQL("{} IS NULL").format(column) for column in columns
            )
            if conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE {} LIMIT 1").format(table, nulls)
            ).fetchone():
                raise ValueError(
                    f"nullable PostgreSQL replication key in {definition.table}"
                )
            key_sql = sql.SQL(", ").join(columns)
            if conn.execute(
                sql.SQL(
                    "SELECT 1 FROM {} GROUP BY {} HAVING COUNT(*) > 1 LIMIT 1"
                ).format(table, key_sql)
            ).fetchone():
                raise ValueError(
                    f"duplicate PostgreSQL replication key in {definition.table}"
                )
            count_row = conn.execute(
                sql.SQL("SELECT COUNT(*)::bigint AS count FROM {}").format(table)
            ).fetchone()
            row_counts.append(
                (definition.table, int(_row_value(count_row, "count", 0)))
            )
            existing = conn.execute(
                "SELECT 1 FROM pg_class x JOIN pg_namespace n ON n.oid=x.relnamespace "
                "WHERE n.nspname=%s AND x.relname=%s AND x.relkind='i'",
                (business_schema, definition.name),
            ).fetchone()
            if existing is None:
                _execute_guard_statement(conn, _guard_sql(definition, business_schema))
            _verify_postgres_guard(conn, definition, business_schema=business_schema)
        _validate_postgres_guard_set(
            conn,
            definitions,
            allow_missing=False,
            business_schema=business_schema,
        )
        report = GuardReport(
            side="postgres",
            run_id=run_id,
            schema_epoch=manifest.schema_pair.epoch,
            database_identity_hash=identity.identity_hash,
            definitions=definitions,
            row_counts=tuple(row_counts),
        )
    return report


def committed_sqlite_guard_report(
    conn: sqlite3.Connection,
    *,
    source_url: str,
    manifest: Manifest,
    run_id: str,
) -> GuardReport:
    validate_sqlite_capture(conn, manifest)
    control = conn.execute(
        "SELECT enabled,run_id,schema_epoch FROM shadow_capture_control WHERE singleton=1"
    ).fetchone()
    if control is None or str(control[1]) != run_id:
        raise ValueError("SQLite guard installation belongs to a different run")
    if int(control[2]) != manifest.schema_pair.epoch:
        raise ValueError("SQLite guard installation has a stale schema epoch")
    definitions = _guard_definitions(manifest)
    row_counts = tuple(
        (
            definition.table,
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote(definition.table)}"
                ).fetchone()[0]
            ),
        )
        for definition in definitions
    )
    identity = fingerprint_sqlite(source_url, conn)
    return GuardReport(
        side="sqlite",
        run_id=run_id,
        schema_epoch=manifest.schema_pair.epoch,
        database_identity_hash=identity.identity_hash,
        definitions=definitions,
        row_counts=row_counts,
    )


def commit_guard_report(
    target: Any,
    *,
    report: GuardReport,
    expected_revision: int,
    manifest: Manifest = MANIFEST,
) -> RunState:
    if report.side not in {"sqlite", "postgres"} or not report.committed:
        raise ValueError("only committed side-specific guard reports are accepted")
    if (
        report.schema_epoch != manifest.schema_pair.epoch
        or report.definitions != _guard_definitions(manifest)
        or {table for table, _count in report.row_counts}
        != {definition.table for definition in report.definitions}
        or any(count < 0 for _table, count in report.row_counts)
    ):
        raise ValueError(
            "guard report does not match the reviewed manifest definitions"
        )
    column = f"{report.side}_guard_report_hash"
    with target.write() as conn:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (report.run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("shadow run does not exist")
        state = _state_from_row(row)
        expected_identity = (
            state.source_identity_hash
            if report.side == "sqlite"
            else state.target_identity_hash
        )
        if (
            state.phase is not ShadowPhase.OFF
            or state.schema_pair.epoch != report.schema_epoch
            or expected_identity != report.database_identity_hash
        ):
            raise ValueError(
                "guard report identity or phase does not match the shadow run"
            )
        current_hash = getattr(state, column)
        if current_hash is not None:
            if current_hash != report.report_hash:
                raise ValueError("a different guard report is already committed")
            return state
        cursor = conn.execute(
            f"UPDATE silicon_shadow.runs SET {column}=%s,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE run_id=%s AND phase='off' AND revision=%s "
            f"AND {column} IS NULL RETURNING run_id",
            (report.report_hash, report.run_id, expected_revision),
        )
        if cursor.fetchone() is None:
            raise ValueError("guard report CAS failed")
        return _state_from_row(
            conn.execute(f"{_RUN_SELECT} WHERE run_id=%s", (report.run_id,)).fetchone()
        )


def _validate_live_postgres_forward_target(
    conn: Any,
    *,
    target: Any,
    state: RunState,
    postgres_report: GuardReport,
    manifest: Manifest,
) -> DatabaseFingerprint:
    version = _postgres_schema_version(conn, state.target_business_schema)
    if version != manifest.schema_pair.postgres_version:
        raise ValueError("live PostgreSQL schema pair changed before capture enable")
    validate_postgres_connection(
        conn,
        manifest,
        schema_version=version,
        expected_pair=manifest.schema_pair,
        business_schema=state.target_business_schema,
    )
    live_target = fingerprint_postgres(_target_url(target), conn)
    if (
        live_target.identity_hash != postgres_report.database_identity_hash
        or live_target.identity_hash != state.target_identity_hash
    ):
        raise ValueError("live PostgreSQL target identity changed")
    _validate_postgres_guard_set(
        conn,
        _guard_definitions(manifest),
        allow_missing=False,
        business_schema=state.target_business_schema,
    )
    for definition in _guard_definitions(manifest):
        _verify_postgres_guard(
            conn, definition, business_schema=state.target_business_schema
        )
    return live_target


def _validate_live_sqlite_capture(
    source_url: str,
    source_conn: sqlite3.Connection,
    *,
    state: RunState,
    manifest: Manifest,
) -> None:
    identity = fingerprint_sqlite(source_url, source_conn)
    if identity.identity_hash != state.source_identity_hash:
        raise ValueError("live SQLite source identity changed")
    validate_sqlite_capture(source_conn, manifest)
    row = source_conn.execute(
        "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
        "FROM shadow_capture_control WHERE singleton=1"
    ).fetchone()
    if row is None or tuple(row) != (
        1,
        0,
        0,
        state.run_id,
        manifest.schema_pair.epoch,
    ):
        raise ValueError("live SQLite capture gate is not enabled and safe")


def enable_sqlite_capture_after_guard_reports(
    source_conn: sqlite3.Connection,
    target: Any,
    *,
    source_url: str,
    sqlite_report: GuardReport,
    postgres_report: GuardReport,
    manifest: Manifest,
) -> None:
    """Enable capture only after both independently committed report CASes."""
    if sqlite_report.side != "sqlite" or postgres_report.side != "postgres":
        raise ValueError("both side-specific guard reports are required")
    if (
        sqlite_report.run_id != postgres_report.run_id
        or sqlite_report.schema_epoch != manifest.schema_pair.epoch
        or postgres_report.schema_epoch != manifest.schema_pair.epoch
        or sqlite_report.definitions != _guard_definitions(manifest)
        or postgres_report.definitions != _guard_definitions(manifest)
    ):
        raise ValueError("guard reports do not describe the same reviewed installation")
    with target.write() as conn:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (sqlite_report.run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("shadow run does not exist")
        state = _state_from_row(row)
        if (
            state.phase is not ShadowPhase.OFF
            or state.sqlite_guard_report_hash != sqlite_report.report_hash
            or state.postgres_guard_report_hash != postgres_report.report_hash
            or state.source_identity_hash != sqlite_report.database_identity_hash
            or state.target_identity_hash != postgres_report.database_identity_hash
        ):
            raise ValueError(
                "both committed guard-report CASes are required before capture"
            )
        _validate_live_postgres_forward_target(
            conn,
            target=target,
            state=state,
            postgres_report=postgres_report,
            manifest=manifest,
        )
    if source_conn.in_transaction:
        raise ValueError("capture enable requires an idle SQLite connection")
    source_conn.execute("BEGIN IMMEDIATE")
    try:
        live_source = fingerprint_sqlite(source_url, source_conn)
        if (
            live_source.identity_hash != sqlite_report.database_identity_hash
            or live_source.identity_hash != state.source_identity_hash
        ):
            raise ValueError(
                "live SQLite source identity changed before capture enable"
            )
        validate_sqlite_capture(source_conn, manifest)
        cursor = source_conn.execute(
            "UPDATE shadow_capture_control SET enabled=1 "
            "WHERE singleton=1 AND run_id=? AND schema_epoch=? AND enabled=0 "
            "AND write_frozen=0 AND apply_active=0",
            (sqlite_report.run_id, manifest.schema_pair.epoch),
        )
        if cursor.rowcount != 1:
            existing = source_conn.execute(
                "SELECT enabled,write_frozen,apply_active FROM shadow_capture_control "
                "WHERE singleton=1 AND run_id=? AND schema_epoch=?",
                (sqlite_report.run_id, manifest.schema_pair.epoch),
            ).fetchone()
            if existing is None or tuple(existing) != (1, 0, 0):
                raise ValueError("SQLite capture enable CAS failed")
        source_conn.commit()
    except BaseException:
        source_conn.rollback()
        raise

    # There is intentionally no cross-database transaction.  The SQLite commit
    # above is the durable fact; this revision CAS acknowledges it in PG.  If
    # this CAS loses connectivity, capture remains safely accumulating events
    # while phase transition stays blocked, and the same call reconciles it.
    with target.write() as conn:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (sqlite_report.run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("shadow run does not exist")
        state = _state_from_row(row)
        _validate_live_postgres_forward_target(
            conn,
            target=target,
            state=state,
            postgres_report=postgres_report,
            manifest=manifest,
        )
        if state.capture_enabled:
            return
        cursor = conn.execute(
            "UPDATE silicon_shadow.runs SET capture_enabled=true,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE run_id=%s AND phase='off' "
            "AND revision=%s AND sqlite_guard_report_hash=%s "
            "AND postgres_guard_report_hash=%s AND capture_enabled=false RETURNING run_id",
            (
                sqlite_report.run_id,
                state.revision,
                sqlite_report.report_hash,
                postgres_report.report_hash,
            ),
        )
        if cursor.fetchone() is None:
            raise ValueError("capture acknowledgement revision CAS failed")
