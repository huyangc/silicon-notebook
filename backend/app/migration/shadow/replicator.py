"""Fail-stop SQLite-to-PostgreSQL forward shadow replicator.

This module is intentionally not wired to application startup.  A later
operator-only worker command will construct it explicitly after snapshot COPY
has published H0.  The live SQLite database remains the sole write authority.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import PoolTimeout

from app.migration.shadow.bulk_copy import (
    _canonical_bytes,
    _copy_columns,
    _postgres_columns,
    _qualified,
)
from app.migration.shadow.capture import validate_sqlite_capture
from app.migration.shadow.control import (
    ShadowPhase,
    _RUN_SELECT,
    _guard_definitions,
    _lock_schema_and_control,
    _postgres_schema_version,
    _state_from_row,
    _target_url,
    _validate_control_schema,
    _validate_postgres_guard_set,
    _validated_progress,
    _verify_postgres_guard,
)
from app.migration.shadow.identity import fingerprint_postgres, fingerprint_sqlite
from app.migration.shadow.manifest import (
    MANIFEST,
    replication_guard_specs,
    validate_postgres_connection,
    validate_sqlite_schema,
)
from app.migration.shadow.metrics import ReplicationMetric, ShadowMetrics
from app.migration.shadow.postgres_catalog import (
    EXPECTED_COLUMNS,
    EXPECTED_CONSTRAINTS,
    EXPECTED_OPERATIONAL_INDEXES,
    _SCHEMA_LABEL,
    validate_postgres_business_catalog,
)
from app.migration.shadow.snapshot import (
    _change_log_high_water,
    assert_live_sqlite_path,
    open_fresh_live_sqlite,
)
from app.migration.shadow.transform import PostgresColumn, transform_sqlite_row
from app.migration.shadow.types import Manifest, TableSpec
from app.repositories.postgres.database import (
    PostgresDatabaseError,
    PostgresPoolTimeout,
)
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TRANSIENT_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available / lock_timeout
        "57014",  # query_canceled / statement_timeout
    }
)
_MAX_BATCH_EVENTS = 4096
_MAX_BATCH_BYTES = 64 * 1024 * 1024
_MAX_DEPENDENCY_ROWS_PER_EVENT = 64
_MAX_DEFERRED_PASSES = 8
_MAX_DEFERRED_STATEMENT_FACTOR = 32
_MAX_TARGET_STATEMENTS = 16_384
_DEFAULT_BATCH_EVENTS = 256
_DEFAULT_BATCH_BYTES = 8 * 1024 * 1024
_SQLITE_INT64_MIN = -(2**63)
_SQLITE_INT64_MAX = 2**63 - 1
_ORDERING_SQLSTATES = frozenset({"23503", "23505", "23P01"})
_UNIQUE_SQLSTATE = "23505"
_UNIQUE_PREDICATES = {
    "idx_catalog_jobs_one_active": (
        "status IN ('queued', 'running')",
        ("status", "in", "'queued'", "'running'"),
    ),
    "idx_kg_build_jobs_one_running": (
        "status = 'running'",
        ("status", "=", "'running'"),
    ),
    "idx_groups_invite_token": (
        "invite_token IS NOT NULL",
        ("invite_token", "is", "not", "null"),
    ),
    "idx_memory_answer_once": (
        "source_answer_id IS NOT NULL",
        ("source_answer_id", "is", "not", "null"),
    ),
    "idx_notebooks_share_token": (
        "share_token IS NOT NULL",
        ("share_token", "is", "not", "null"),
    ),
    # Report public share tokens: same shape as the notebook token index above.
    # A partial unique index must be pinned here or replicator construction
    # fails closed for the whole schema version.
    "idx_reports_share_token": (
        "share_token IS NOT NULL",
        ("share_token", "is", "not", "null"),
    ),
    # Conversation public share tokens (v29): same shape as the report/
    # notebook token indexes above.
    "idx_conversations_share_token": (
        "share_token IS NOT NULL",
        ("share_token", "is", "not", "null"),
    ),
    "idx_promotion_object": (
        "status NOT IN ('approved', 'rejected')",
        ("status", "not", "in", "'approved'", "'rejected'"),
    ),
    "idx_sources_memory_id": (
        "memory_id IS NOT NULL AND memory_id != ''",
        ("memory_id", "is", "not", "null", "and", "memory_id", "<>", "''"),
    ),
    "idx_users_username": ("username != ''", ("username", "<>", "''")),
    # notebook_share_requests (v28): a pending-only uniqueness constraint on
    # (notebook_id, group_id). See idx_catalog_jobs_one_active above for the
    # same partial-unique-over-an-enumerated-status shape.
    "uq_share_requests_one_pending": (
        "status = 'pending'",
        ("status", "=", "'pending'"),
    ),
    # agent_observations (v33): the add_observation idempotency index. Same
    # NULL-park shape as the share-token indexes above, except the park
    # column is client_request_id rather than a token — a write that omits
    # a client_request_id simply does not participate in this partial
    # unique surface.
    "idx_agent_observations_request": (
        "client_request_id IS NOT NULL",
        ("client_request_id", "is", "not", "null"),
    ),
    # notebook_delete_jobs (batch 3·W1 PR-3 Phase A, v48): the delete job's
    # own single-flight defense-in-depth index. Same enumerated-status shape
    # as idx_catalog_jobs_one_active above, three values instead of two.
    "idx_notebook_delete_jobs_one_active": (
        "status IN ('queued', 'running', 'waiting')",
        ("status", "in", "'queued'", "'running'", "'waiting'"),
    ),
}


class Direction(StrEnum):
    SQLITE_TO_POSTGRES = "sqlite_to_postgres"
    POSTGRES_TO_SQLITE = "postgres_to_sqlite"


@dataclass(frozen=True)
class BatchResult:
    run_id: str
    direction: Direction
    checkpoint_before: int
    checkpoint_after: int
    source_max_seq: int
    events_read: int
    rows_applied: int
    hydrated_bytes: int
    retries: int
    duration_seconds: float
    oversized_event: bool = False

    @property
    def lag(self) -> int:
        return max(0, self.source_max_seq - self.checkpoint_after)


class ShadowReplicationError(RuntimeError):
    """Base class for safe forward-replication failures."""


class RetryExhausted(ShadowReplicationError):
    """A transient target/source failure exceeded the bounded retry budget."""

    def __init__(self, message: str, *, retries: int) -> None:
        super().__init__(message)
        self.retries = retries


class PoisonEventRecorded(ShadowReplicationError):
    """A deterministic event/control failure was recorded and blocked progress."""

    def __init__(self, source_seq: int, category: str) -> None:
        super().__init__(
            f"shadow replication stopped at poison seq {source_seq}: {category}"
        )
        self.source_seq = source_seq
        self.category = category


class WorkerLeaseUnavailable(ShadowReplicationError):
    """Another non-expired worker owns this run/direction."""


class OrderingBlocked(ShadowReplicationError):
    """A valid current source state needs a wider event window to converge."""


class StaleFailureDiscarded(ShadowReplicationError):
    """Failure evidence no longer matches the live run/checkpoint binding."""


class CapacityExceeded(ShadowReplicationError):
    """A fixed safety budget was exhausted without classifying source data."""


class _SourceBindingError(ShadowReplicationError):
    """The configured SQLite path no longer names the bound source file."""

    def __init__(
        self,
        *,
        batch_events: int = 0,
        source_max_seq: int = 0,
    ) -> None:
        super().__init__("shadow SQLite source binding validation failed")
        self.batch_events = batch_events
        self.source_max_seq = source_max_seq


class CrashInjected(BaseException):
    """Test-only crash signal; BaseException prevents retry/poison handling."""


class _IntegerRangeError(ValueError):
    pass


@dataclass
class _StatementBudget:
    limit: int
    used: int = 0

    def consume(self) -> None:
        if self.used >= self.limit:
            raise OrderingBlocked("shadow target statement safety budget was exhausted")
        self.used += 1


@dataclass(frozen=True)
class _Poison:
    source_seq: int
    category: str
    safe_summary: str


class _EventFailure(Exception):
    def __init__(
        self,
        poison: _Poison,
        snapshot: _TargetSnapshot | None = None,
        *,
        batch_events: int = 0,
        source_max_seq: int = 0,
    ) -> None:
        super().__init__(poison.safe_summary)
        self.poison = poison
        self.snapshot = snapshot
        self.batch_events = batch_events
        self.source_max_seq = source_max_seq


@dataclass(frozen=True)
class _SourceEvent:
    seq: int
    table: str
    operation: str
    key: tuple[tuple[str, Any], ...]

    @property
    def key_values(self) -> tuple[Any, ...]:
        return tuple(value for _name, value in self.key)

    @property
    def identity(self) -> tuple[str, tuple[tuple[str, Any], ...]]:
        return self.table, self.key


@dataclass(frozen=True)
class _Apply:
    event: _SourceEvent
    spec: TableSpec
    columns: tuple[PostgresColumn, ...]
    row: tuple[Any, ...] | None
    encoded_bytes: int
    synthetic: bool = False


class _BundleAccumulator:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.latest_bundles: dict[
            tuple[str, tuple[tuple[str, Any], ...]],
            tuple[int, tuple[_Apply, ...]],
        ] = {}
        self.synthetic_references: dict[
            tuple[str, tuple[tuple[str, Any], ...]], int
        ] = {}
        self.synthetic_bytes: dict[tuple[str, tuple[tuple[str, Any], ...]], int] = {}
        self.total_bytes = 0

    def _contribution(self, identity: tuple[str, tuple[tuple[str, Any], ...]]) -> int:
        actual = self.latest_bundles.get(identity)
        if actual is not None:
            return actual[1][-1].encoded_bytes
        if self.synthetic_references.get(identity, 0):
            return self.synthetic_bytes[identity]
        return 0

    def _add_bundle(self, last_seq: int, bundle: tuple[_Apply, ...]) -> None:
        actual_identity = bundle[-1].event.identity
        affected = {
            actual_identity,
            *(item.event.identity for item in bundle[:-1]),
        }
        before = sum(self._contribution(identity) for identity in affected)
        for item in bundle[:-1]:
            identity = item.event.identity
            references = self.synthetic_references.get(identity, 0)
            if references == 0:
                self.synthetic_bytes[identity] = item.encoded_bytes
            elif self.synthetic_bytes[identity] != item.encoded_bytes:
                raise RuntimeError("shadow synthetic dependency hydration diverged")
            self.synthetic_references[identity] = references + 1
        self.latest_bundles[actual_identity] = (last_seq, bundle)
        self.total_bytes += (
            sum(self._contribution(identity) for identity in affected) - before
        )

    def _remove_bundle(self, bundle: tuple[_Apply, ...]) -> None:
        actual_identity = bundle[-1].event.identity
        affected = {
            actual_identity,
            *(item.event.identity for item in bundle[:-1]),
        }
        before = sum(self._contribution(identity) for identity in affected)
        del self.latest_bundles[actual_identity]
        for item in bundle[:-1]:
            identity = item.event.identity
            references = self.synthetic_references[identity] - 1
            if references == 0:
                del self.synthetic_references[identity]
                del self.synthetic_bytes[identity]
            else:
                self.synthetic_references[identity] = references
        self.total_bytes += (
            sum(self._contribution(identity) for identity in affected) - before
        )

    def accept(self, last_seq: int, bundle: tuple[_Apply, ...]) -> bool:
        actual_identity = bundle[-1].event.identity
        previous = self.latest_bundles.get(actual_identity)
        if previous is not None:
            self._remove_bundle(previous[1])
        self._add_bundle(last_seq, bundle)
        if self.total_bytes <= self.max_bytes or len(self.latest_bundles) == 1:
            return True
        self._remove_bundle(bundle)
        if previous is not None:
            self._add_bundle(previous[0], previous[1])
        return False

    @property
    def oversized(self) -> bool:
        return self.total_bytes > self.max_bytes

    def applies(self) -> list[_Apply]:
        actual_identities = set(self.latest_bundles)
        emitted: set[tuple[str, tuple[tuple[str, Any], ...]]] = set()
        applies: list[_Apply] = []
        for _seq, bundle in sorted(
            self.latest_bundles.values(), key=lambda item: item[0]
        ):
            for item in bundle[:-1]:
                identity = item.event.identity
                if identity in actual_identities or identity in emitted:
                    continue
                emitted.add(identity)
                applies.append(item)
            final = bundle[-1]
            if final.event.identity not in emitted:
                emitted.add(final.event.identity)
                applies.append(final)
        if sum(item.encoded_bytes for item in applies) != self.total_bytes:
            raise RuntimeError("shadow coalesced byte accounting diverged")
        return applies


@dataclass(frozen=True)
class _Deferred:
    item: _Apply
    sqlstate: str
    constraint_name: str | None


class _ParkStrategy(StrEnum):
    REPLICATION_KEY = "replication_key"
    NULL = "null"
    SENTINEL_TEXT = "sentinel_text"
    SENTINEL_BIGINT = "sentinel_bigint"
    LEAF_DELETE = "leaf_delete"


@dataclass(frozen=True)
class _UniqueSurface:
    name: str
    table: str
    columns: tuple[str, ...]
    strategy: _ParkStrategy
    park_column: str | None
    predicate: str | None


@dataclass(frozen=True)
class _TargetSnapshot:
    checkpoint: int
    revision: int
    source_identity_hash: str
    target_identity_hash: str
    business_schema: str
    contracts: Mapping[str, tuple[PostgresColumn, ...]]


def _row_value(row: Any, name: str, position: int) -> Any:
    if isinstance(row, Mapping) or hasattr(row, "keys"):
        return row[name]
    return row[position]


def _validate_existing_poison(existing: Any, poison: _Poison) -> None:
    if existing is None:
        return
    existing_value = (
        int(_row_value(existing, "source_seq", 0)),
        str(_row_value(existing, "category", 1)),
        str(_row_value(existing, "safe_summary", 2)),
    )
    if existing_value == (
        poison.source_seq,
        poison.category,
        poison.safe_summary,
    ):
        return
    raise StaleFailureDiscarded("shadow poison already exists for this run direction")


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("shadow replication manifest contains an unsafe identifier")
    return f'"{identifier}"'


def _is_transient(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            PostgresPoolTimeout,
            PoolTimeout,
            psycopg.OperationalError,
            psycopg.InterfaceError,
        ),
    ):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text or "interrupt" in text
    if isinstance(exc, psycopg.Error):
        state = getattr(exc, "sqlstate", None)
        return isinstance(state, str) and (
            state.startswith("08") or state in _TRANSIENT_SQLSTATES
        )
    # Pool lifecycle errors intentionally erase libpq details; acquisition and
    # connection health failures are therefore conservatively retryable.
    return type(exc) is PostgresDatabaseError


def _json_object_exact(value: str) -> list[tuple[str, Any]]:
    if not isinstance(value, str):
        raise ValueError("shadow event replication key must be JSON text")

    def pairs(items: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
        names = [name for name, _child in items]
        if any(not isinstance(name, str) for name in names) or len(names) != len(
            set(names)
        ):
            raise ValueError("shadow event replication key contains duplicate fields")
        return items

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (json.JSONDecodeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("shadow event replication key is invalid") from exc
    if not isinstance(decoded, list) or any(
        not isinstance(item, tuple) or len(item) != 2 for item in decoded
    ):
        raise ValueError("shadow event replication key must be a JSON object")
    return decoded


def _validate_key_value(declared_type: str, value: Any) -> None:
    normalized = declared_type.strip().upper()
    if value is None or isinstance(value, (dict, list, bool, bytes, bytearray)):
        raise ValueError("shadow event replication key value is invalid")
    if "INT" in normalized and not isinstance(value, int):
        raise ValueError("shadow event integer replication key has the wrong type")
    if "INT" in normalized and not _SQLITE_INT64_MIN <= value <= _SQLITE_INT64_MAX:
        raise _IntegerRangeError(
            "shadow event integer replication key exceeds SQLite int64"
        )
    if any(
        token in normalized for token in ("CHAR", "CLOB", "TEXT")
    ) and not isinstance(value, str):
        raise ValueError("shadow event text replication key has the wrong type")
    if any(
        token in normalized for token in ("REAL", "FLOA", "DOUB")
    ) and not isinstance(value, (int, float)):
        raise ValueError("shadow event numeric replication key has the wrong type")


def _fk_max_ancestor_closure() -> int:
    parents: dict[str, list[str]] = {}
    for constraint in EXPECTED_CONSTRAINTS.values():
        if constraint.kind == "f" and constraint.referenced_table is not None:
            parents.setdefault(constraint.table, []).append(constraint.referenced_table)

    def visit(table: str, path: frozenset[str]) -> int:
        if table in path:
            raise ValueError(f"{_SCHEMA_LABEL} foreign-key graph contains a cycle")
        return 1 + sum(
            visit(parent, path | {table}) for parent in parents.get(table, ())
        )

    return max((visit(table, frozenset()) for table in EXPECTED_COLUMNS), default=0)


def _build_unique_surfaces(manifest: Manifest) -> dict[str, _UniqueSurface]:
    specs = {spec.name: spec for spec in manifest.replicated}
    fk_columns: set[tuple[str, str]] = set()
    checked_columns: set[tuple[str, str]] = set()
    incoming_fk_tables: set[str] = set()
    for constraint in EXPECTED_CONSTRAINTS.values():
        # Backend-local ephemeral tables are present in the paired catalog but
        # deliberately absent from shadow COPY/capture/apply.  Their keys,
        # checks, and outgoing FKs therefore cannot constrain parking a
        # replicated event.
        if constraint.table not in specs:
            continue
        if constraint.kind == "f":
            fk_columns.update((constraint.table, name) for name in constraint.columns)
            if constraint.referenced_table in specs:
                incoming_fk_tables.add(constraint.referenced_table)
                fk_columns.update(
                    (constraint.referenced_table, name)
                    for name in constraint.referenced_columns
                )
        elif constraint.kind == "c":
            checked_columns.update(
                (constraint.table, token)
                for token in constraint.check_tokens
                if token in EXPECTED_COLUMNS.get(constraint.table, {})
            )

    raw: list[tuple[str, str, tuple[str, ...], str | None]] = []
    for name, constraint in EXPECTED_CONSTRAINTS.items():
        if constraint.table in specs and constraint.kind in {"p", "u"}:
            raw.append((name, constraint.table, constraint.columns, None))
    seen_predicates: set[str] = set()
    for name, index in EXPECTED_OPERATIONAL_INDEXES.items():
        if index.table not in specs:
            continue
        if not index.unique:
            continue
        columns: list[str] = []
        for key in index.keys:
            if not key or key[0] not in EXPECTED_COLUMNS[index.table]:
                raise ValueError(f"{_SCHEMA_LABEL} unique index is not column-parkable")
            columns.append(key[0])
        predicate = None
        if index.predicate_tokens:
            predicate_pin = _UNIQUE_PREDICATES.get(name)
            if predicate_pin is None or predicate_pin[1] != index.predicate_tokens:
                raise ValueError(f"{_SCHEMA_LABEL} partial unique predicate is unpinned")
            predicate = predicate_pin[0]
            seen_predicates.add(name)
        raw.append((name, index.table, tuple(columns), predicate))
    if seen_predicates != set(_UNIQUE_PREDICATES):
        raise ValueError(f"{_SCHEMA_LABEL} partial unique predicate pins drifted")
    for guard in replication_guard_specs(manifest):
        raw.append((guard.name, guard.table, guard.columns, None))

    result: dict[str, _UniqueSurface] = {}
    for name, table, columns, predicate in raw:
        spec = specs[table]
        strategy: _ParkStrategy | None = None
        park_column: str | None = None
        if set(columns) == set(spec.replication_key):
            strategy = _ParkStrategy.REPLICATION_KEY
        else:
            for column in columns:
                if EXPECTED_COLUMNS[table][column].nullable:
                    strategy = _ParkStrategy.NULL
                    park_column = column
                    break
            if strategy is None:
                for column in columns:
                    contract = EXPECTED_COLUMNS[table][column]
                    if (
                        contract.data_type in {"text", "bigint"}
                        and (table, column) not in fk_columns
                        and (table, column) not in checked_columns
                        and (contract.data_type != "text" or contract.collation == "C")
                    ):
                        strategy = (
                            _ParkStrategy.SENTINEL_TEXT
                            if contract.data_type == "text"
                            else _ParkStrategy.SENTINEL_BIGINT
                        )
                        park_column = column
                        break
            if strategy is None and table not in incoming_fk_tables:
                strategy = _ParkStrategy.LEAF_DELETE
            if strategy is None:
                raise ValueError(
                    f"{_SCHEMA_LABEL} unique surface is not safely parkable: {name}"
                )
        result[name] = _UniqueSurface(
            name, table, columns, strategy, park_column, predicate
        )
    if len(result) != len(raw):
        raise ValueError(f"{_SCHEMA_LABEL} unique surface names are ambiguous")
    return result


class ShadowReplicator:
    """One fail-stop consumer for one forward shadow run."""

    def __init__(
        self,
        source: Any,
        target: Any,
        *,
        manifest: Manifest = MANIFEST,
        metrics: ShadowMetrics | None = None,
        max_retries: int = 5,
        base_backoff_seconds: float = 0.05,
        max_backoff_seconds: float = 2.0,
        lease_seconds: int = 30,
        idle_seconds: float = 0.25,
        crash_injector: Callable[[str], None] | None = None,
        random_source: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 0 <= max_retries <= 20
        ):
            raise ValueError("shadow retry count must be between zero and twenty")
        if base_backoff_seconds < 0 or max_backoff_seconds < base_backoff_seconds:
            raise ValueError("shadow retry backoff is invalid")
        if isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("shadow worker lease must be positive")
        if idle_seconds < 0:
            raise ValueError("shadow idle delay must be non-negative")
        self.source = source
        self.target = target
        self.manifest = manifest
        self.metrics = metrics or ShadowMetrics()
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.lease_seconds = lease_seconds
        self.idle_seconds = idle_seconds
        self.crash_injector = crash_injector
        self.random = random_source or random.Random()
        self.sleep = sleep
        self.worker_id = uuid.uuid4().hex
        self._specs = {spec.name: spec for spec in manifest.replicated}
        self._unique_surfaces = _build_unique_surfaces(manifest)
        if _fk_max_ancestor_closure() >= _MAX_DEPENDENCY_ROWS_PER_EVENT:
            raise ValueError(f"{_SCHEMA_LABEL} foreign-key closure exceeds its hard cap")
        self._run_lock = threading.Lock()

    def _crash(self, boundary: str) -> None:
        if self.crash_injector is not None:
            self.crash_injector(boundary)

    def _backoff(self, retry: int) -> None:
        ceiling = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2 ** max(0, retry - 1)),
        )
        self.sleep(ceiling * (0.5 + self.random.random() * 0.5))

    def _retry(self, operation: Callable[[], Any]) -> tuple[Any, int]:
        retries = 0
        while True:
            try:
                return operation(), retries
            except CrashInjected:
                raise
            except Exception as exc:
                if not _is_transient(exc) or retries >= self.max_retries:
                    if _is_transient(exc):
                        raise RetryExhausted(
                            "shadow replication transient retry budget exhausted",
                            retries=retries,
                        ) from None
                    if retries:
                        setattr(exc, "shadow_retry_count", retries)
                    raise
                retries += 1
                self._backoff(retries)

    def _validate_target_state(
        self, conn: Any, run_id: str, direction: Direction
    ) -> Any:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("shadow forward run does not exist")
        state = _state_from_row(row)
        self._validate_bound_target_state(conn, state, direction)
        return state

    def _validate_bound_target_state(
        self, conn: Any, state: Any, direction: Direction
    ) -> None:
        _validated_progress(state.progress)
        live_target = fingerprint_postgres(_target_url(self.target), conn)
        if (
            direction is not Direction.SQLITE_TO_POSTGRES
            or state.phase is not ShadowPhase.SQLITE_TO_POSTGRES
            or state.active_backend != "sqlite"
            or not state.capture_enabled
            or state.sqlite_guard_report_hash is None
            or state.postgres_guard_report_hash is None
            or state.schema_pair != self.manifest.schema_pair
            or state.target_identity_hash != live_target.identity_hash
        ):
            raise ValueError("shadow run is not an active committed forward run")

    @staticmethod
    def _validate_checkpoint_progress(
        state: Any, checkpoint: int, snapshot: _TargetSnapshot
    ) -> None:
        _validated_progress(state.progress)
        if state.progress.get("applied_seq") != checkpoint:
            raise _EventFailure(
                _Poison(
                    checkpoint + 1,
                    "schema",
                    "shadow control progress does not match checkpoint",
                ),
                snapshot,
            )

    def _claim_worker(self, conn: Any, run_id: str, direction: Direction) -> None:
        row = conn.execute(
            "INSERT INTO silicon_shadow.workers "
            "(run_id,direction,worker_id,lease_expires_at,heartbeat_at) "
            "VALUES (%s,%s,%s,clock_timestamp()+(%s * interval '1 second'),clock_timestamp()) "
            "ON CONFLICT (run_id,direction) DO UPDATE SET worker_id=EXCLUDED.worker_id,"
            "lease_expires_at=EXCLUDED.lease_expires_at,heartbeat_at=clock_timestamp() "
            "WHERE silicon_shadow.workers.worker_id=EXCLUDED.worker_id "
            "OR silicon_shadow.workers.lease_expires_at <= clock_timestamp() "
            "RETURNING worker_id",
            (run_id, direction.value, self.worker_id, self.lease_seconds),
        ).fetchone()
        if row is None or str(_row_value(row, "worker_id", 0)) != self.worker_id:
            raise WorkerLeaseUnavailable("shadow forward worker lease is already held")

    def _lock_business_catalog(
        self, conn: Any, schema: str, *, block_external_writes: bool = False
    ) -> None:
        ordered = sorted(self.manifest.replicated, key=lambda item: item.copy_rank)
        relations = [
            _qualified(schema, "silicon_schema_migrations"),
            *(_qualified(schema, spec.name) for spec in ordered),
        ]
        conn.execute(
            sql.SQL("LOCK TABLE {} IN {} MODE").format(
                sql.SQL(", ").join(relations),
                sql.SQL(
                    "SHARE ROW EXCLUSIVE" if block_external_writes else "ROW EXCLUSIVE"
                ),
            )
        )

    def _validate_target_catalog(self, conn: Any, state: Any) -> None:
        version = _postgres_schema_version(conn, state.target_business_schema)
        if version != self.manifest.schema_pair.postgres_version:
            raise ValueError("shadow target schema version changed")
        validate_postgres_connection(
            conn,
            self.manifest,
            schema_version=version,
            expected_pair=self.manifest.schema_pair,
            business_schema=state.target_business_schema,
        )
        definitions = _guard_definitions(self.manifest)
        _validate_postgres_guard_set(
            conn,
            definitions,
            allow_missing=False,
            business_schema=state.target_business_schema,
        )
        for definition in definitions:
            _verify_postgres_guard(
                conn, definition, business_schema=state.target_business_schema
            )
        validate_postgres_business_catalog(
            conn,
            business_schema=state.target_business_schema,
            manifest=self.manifest,
            guard_names=frozenset(item.name for item in definitions),
        )

    def _target_snapshot(self, run_id: str, direction: Direction) -> _TargetSnapshot:
        with self.target.write() as conn:
            _lock_schema_and_control(conn)
            _validate_control_schema(conn)
            run_row = conn.execute(
                f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
            ).fetchone()
            if run_row is None:
                raise ValueError("shadow forward run does not exist")
            state = _state_from_row(run_row)
            poison = conn.execute(
                "SELECT source_seq FROM silicon_shadow.poison_events "
                "WHERE run_id=%s AND direction=%s ORDER BY source_seq LIMIT 1",
                (run_id, direction.value),
            ).fetchone()
            if poison is not None:
                raise PoisonEventRecorded(
                    int(_row_value(poison, "source_seq", 0)), "existing"
                )
            self._claim_worker(conn, run_id, direction)
            checkpoint = conn.execute(
                "SELECT last_seq,revision FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction=%s FOR UPDATE",
                (run_id, direction.value),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("shadow forward checkpoint is missing")
            checkpoint_seq = int(_row_value(checkpoint, "last_seq", 0))
            checkpoint_revision = int(_row_value(checkpoint, "revision", 1))
            failure_snapshot = _TargetSnapshot(
                checkpoint=checkpoint_seq,
                revision=checkpoint_revision,
                source_identity_hash=state.source_identity_hash,
                target_identity_hash=state.target_identity_hash,
                business_schema=state.target_business_schema,
                contracts={},
            )
            try:
                self._validate_checkpoint_progress(
                    state, checkpoint_seq, failure_snapshot
                )
                self._validate_bound_target_state(conn, state, direction)
                self._lock_business_catalog(conn, state.target_business_schema)
                self._validate_target_catalog(conn, state)
            except (ValueError, psycopg.ProgrammingError, psycopg.DataError) as exc:
                text = str(exc).lower()
                category = (
                    "identity"
                    if "identity" in text or "active committed" in text
                    else "schema"
                )
                raise _EventFailure(
                    _Poison(
                        checkpoint_seq + 1,
                        category,
                        "shadow run identity validation failed"
                        if category == "identity"
                        else "shadow schema validation failed",
                    ),
                    failure_snapshot,
                ) from None
            contracts = {
                spec.name: _postgres_columns(conn, spec, state.target_business_schema)
                for spec in self.manifest.replicated
            }
            return _TargetSnapshot(
                checkpoint=checkpoint_seq,
                revision=checkpoint_revision,
                source_identity_hash=state.source_identity_hash,
                target_identity_hash=state.target_identity_hash,
                business_schema=state.target_business_schema,
                contracts=contracts,
            )

    def _decode_event(
        self,
        row: Any,
        *,
        expected_seq: int,
        run_id: str,
        declared_types: Mapping[str, Mapping[str, str]],
    ) -> _SourceEvent:
        try:
            seq = int(_row_value(row, "seq", 0))
            if seq != expected_seq:
                raise _EventFailure(
                    _Poison(expected_seq, "gap", "source sequence continuity failed")
                )
            if str(_row_value(row, "run_id", 1)) != run_id:
                raise ValueError("wrong run")
            table = str(_row_value(row, "table_name", 2))
            spec = self._specs.get(table)
            if spec is None:
                raise ValueError("unknown table")
            operation = str(_row_value(row, "operation", 4))
            if operation not in {"upsert", "delete"}:
                raise ValueError("unknown operation")
            epoch = _row_value(row, "schema_epoch", 5)
            if isinstance(epoch, bool) or int(epoch) != self.manifest.schema_pair.epoch:
                raise ValueError("wrong epoch")
            pairs = _json_object_exact(_row_value(row, "key_json", 3))
            if [name for name, _value in pairs] != list(spec.replication_key):
                raise ValueError("wrong key shape")
            for name, value in pairs:
                _validate_key_value(declared_types[table][name], value)
            return _SourceEvent(seq, table, operation, tuple(pairs))
        except _EventFailure:
            raise
        except _IntegerRangeError:
            raise _EventFailure(
                _Poison(
                    expected_seq,
                    "conversion",
                    "source row conversion failed",
                )
            ) from None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise _EventFailure(
                _Poison(expected_seq, "event_contract", "source event contract failed")
            ) from None

    def _read_source(
        self,
        snapshot: _TargetSnapshot,
        *,
        run_id: str,
        max_events: int,
        max_bytes: int,
    ) -> tuple[list[_Apply], int, int, bool]:
        try:
            conn, binding = open_fresh_live_sqlite(self.source)
        except sqlite3.OperationalError as exc:
            if _is_transient(exc):
                raise
            raise _SourceBindingError() from None
        except ValueError:
            raise _SourceBindingError() from None
        before_changes = conn.total_changes
        try:
            conn.execute("BEGIN")
            try:
                identity = fingerprint_sqlite(self.source.settings.database_url, conn)
            except ValueError:
                raise _SourceBindingError() from None
            if identity.identity_hash != snapshot.source_identity_hash:
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "identity",
                        "shadow run identity validation failed",
                    ),
                    snapshot,
                )
            try:
                validate_sqlite_schema(
                    conn,
                    self.manifest,
                    expected_pair=self.manifest.schema_pair,
                    validate_rows=False,
                )
                validate_sqlite_capture(conn, self.manifest)
            except sqlite3.OperationalError as exc:
                if _is_transient(exc):
                    raise
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "schema",
                        "shadow schema validation failed",
                    ),
                    snapshot,
                ) from None
            except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "schema",
                        "shadow schema validation failed",
                    ),
                    snapshot,
                ) from None
            control = conn.execute(
                "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
                "FROM shadow_capture_control WHERE singleton=1"
            ).fetchone()
            if control is None or tuple(control) != (
                1,
                0,
                0,
                run_id,
                self.manifest.schema_pair.epoch,
            ):
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "identity",
                        "shadow run identity validation failed",
                    ),
                    snapshot,
                )
            source_max = _change_log_high_water(conn)
            if snapshot.checkpoint > source_max:
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "gap",
                        "source sequence continuity failed",
                    )
                )
            rows = conn.execute(
                "SELECT seq,run_id,table_name,key_json,operation,schema_epoch "
                "FROM shadow_change_log WHERE seq>? ORDER BY seq LIMIT ?",
                (snapshot.checkpoint, max_events),
            ).fetchall()
            if not rows:
                if source_max != snapshot.checkpoint:
                    raise _EventFailure(
                        _Poison(
                            snapshot.checkpoint + 1,
                            "gap",
                            "source sequence continuity failed",
                        )
                    )
                return [], source_max, snapshot.checkpoint, False

            sqlite_columns: dict[str, tuple[str, ...]] = {}
            declared_types: dict[str, dict[str, str]] = {}
            for spec in self.manifest.replicated:
                info = conn.execute(
                    f"PRAGMA table_info({_quote(spec.name)})"
                ).fetchall()
                sqlite_columns[spec.name] = tuple(
                    str(_row_value(item, "name", 1)) for item in info
                )
                declared_types[spec.name] = {
                    str(_row_value(item, "name", 1)): str(
                        _row_value(item, "type", 2) or ""
                    )
                    for item in info
                }
                _copy_columns(
                    sqlite_columns[spec.name], snapshot.contracts[spec.name], spec
                )

            events: list[_SourceEvent] = []
            for offset, row in enumerate(rows, 1):
                try:
                    events.append(
                        self._decode_event(
                            row,
                            expected_seq=snapshot.checkpoint + offset,
                            run_id=run_id,
                            declared_types=declared_types,
                        )
                    )
                except _EventFailure as exc:
                    exc.batch_events = offset
                    exc.source_max_seq = source_max
                    raise
            next_seq_present: bool | None = None
            if len(rows) == max_events and events[-1].seq < source_max:
                next_seq_present = (
                    conn.execute(
                        "SELECT 1 FROM shadow_change_log WHERE seq=?",
                        (events[-1].seq + 1,),
                    ).fetchone()
                    is not None
                )
            self._validate_source_window(
                snapshot,
                events,
                row_count=len(rows),
                max_events=max_events,
                source_max=source_max,
                next_seq_present=next_seq_present,
            )
            hydrated_cache: dict[
                tuple[str, tuple[tuple[str, Any], ...]],
                tuple[tuple[Any, ...] | None, int, dict[str, Any] | None],
            ] = {}
            foreign_keys: dict[
                str, tuple[tuple[str, tuple[tuple[str, str], ...]], ...]
            ] = {}

            def hydrate(
                event: _SourceEvent, *, synthetic: bool = False
            ) -> tuple[_Apply, dict[str, Any] | None]:
                spec = self._specs[event.table]
                target_columns = snapshot.contracts[event.table]
                if event.operation == "delete":
                    return (
                        _Apply(event, spec, target_columns, None, 0, synthetic),
                        None,
                    )
                cached = hydrated_cache.get(event.identity)
                if cached is None:
                    source_names = sqlite_columns[event.table]
                    where = " AND ".join(
                        f"{_quote(name)}=?" for name in spec.replication_key
                    )
                    select = "*"
                    if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
                        select = "*, rowid AS ordinal"
                    hydrated = conn.execute(
                        f"SELECT {select} FROM {_quote(spec.name)} WHERE {where}",
                        event.key_values,
                    ).fetchone()
                    if hydrated is None:
                        cached = (None, 0, None)
                    else:
                        mapping = {
                            name: _row_value(hydrated, name, position)
                            for position, name in enumerate(source_names)
                        }
                        if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
                            mapping["ordinal"] = _row_value(
                                hydrated, "ordinal", len(source_names)
                            )
                        transformed = transform_sqlite_row(
                            spec, target_columns, mapping
                        )
                        cached = (
                            transformed,
                            len(_canonical_bytes(transformed, target_columns)),
                            mapping,
                        )
                    hydrated_cache[event.identity] = cached
                row_data, encoded, mapping = cached
                return (
                    _Apply(
                        event,
                        spec,
                        target_columns,
                        row_data,
                        encoded,
                        synthetic,
                    ),
                    mapping,
                )

            def fk_groups(table: str):
                cached = foreign_keys.get(table)
                if cached is not None:
                    return cached
                grouped: dict[int, tuple[str, list[tuple[str, str]]]] = {}
                for row in conn.execute(
                    f"PRAGMA foreign_key_list({_quote(table)})"
                ).fetchall():
                    group_id = int(_row_value(row, "id", 0))
                    parent = str(_row_value(row, "table", 2))
                    child_column = str(_row_value(row, "from", 3))
                    parent_column = _row_value(row, "to", 4)
                    if not parent_column or parent not in self._specs:
                        continue
                    entry = grouped.setdefault(group_id, (parent, []))
                    entry[1].append((child_column, str(parent_column)))
                result = tuple(
                    (parent, tuple(columns))
                    for _group_id, (parent, columns) in sorted(grouped.items())
                )
                foreign_keys[table] = result
                return result

            def dependency_bundle(event: _SourceEvent) -> list[_Apply]:
                item, mapping = hydrate(event)
                if item.row is None or mapping is None:
                    return [item]
                result: list[_Apply] = []
                seen: set[tuple[str, tuple[tuple[str, Any], ...]]] = {event.identity}

                def visit(
                    child_event: _SourceEvent,
                    child_mapping: Mapping[str, Any],
                ) -> None:
                    for parent_table, columns in fk_groups(child_event.table):
                        values = tuple(
                            child_mapping[child] for child, _parent in columns
                        )
                        if any(value is None for value in values):
                            continue
                        predicate = " AND ".join(
                            f"{_quote(parent)}=?" for _child, parent in columns
                        )
                        parent_spec = self._specs[parent_table]
                        select = "*"
                        if parent_table in POSTGRES_ROWID_ORDINAL_TABLES:
                            select = "*, rowid AS ordinal"
                        parent_row = conn.execute(
                            f"SELECT {select} FROM {_quote(parent_table)} "
                            f"WHERE {predicate}",
                            values,
                        ).fetchone()
                        if parent_row is None:
                            continue
                        parent_names = sqlite_columns[parent_table]
                        parent_mapping = {
                            name: _row_value(parent_row, name, position)
                            for position, name in enumerate(parent_names)
                        }
                        if parent_table in POSTGRES_ROWID_ORDINAL_TABLES:
                            parent_mapping["ordinal"] = _row_value(
                                parent_row, "ordinal", len(parent_names)
                            )
                        parent_event = _SourceEvent(
                            child_event.seq,
                            parent_table,
                            "upsert",
                            tuple(
                                (name, parent_mapping[name])
                                for name in parent_spec.replication_key
                            ),
                        )
                        if parent_event.identity in seen:
                            continue
                        seen.add(parent_event.identity)
                        if len(seen) > _MAX_DEPENDENCY_ROWS_PER_EVENT:
                            raise CapacityExceeded(
                                "foreign-key dependency closure is too large"
                            )
                        parent_item, canonical_mapping = hydrate(
                            parent_event, synthetic=True
                        )
                        if parent_item.row is None or canonical_mapping is None:
                            continue
                        visit(parent_event, canonical_mapping)
                        result.append(parent_item)

                visit(event, mapping)
                result.append(item)
                return result

            accumulator = _BundleAccumulator(max_bytes)
            oversized = False
            accepted_last_seq = snapshot.checkpoint

            for index, event in enumerate(events):
                try:
                    bundle = tuple(
                        dependency_bundle(event)
                        if event.operation == "upsert"
                        else (hydrate(event)[0],)
                    )
                except _EventFailure as exc:
                    exc.batch_events = index + 1
                    exc.source_max_seq = source_max
                    raise
                except CapacityExceeded as exc:
                    exc.batch_events = index + 1
                    exc.source_max_seq = source_max
                    raise
                except (sqlite3.Error, KeyError, TypeError, ValueError, OverflowError):
                    raise _EventFailure(
                        _Poison(
                            event.seq,
                            "conversion",
                            "source row conversion failed",
                        ),
                        snapshot,
                        batch_events=index + 1,
                        source_max_seq=source_max,
                    ) from None

                if not accumulator.accept(event.seq, bundle):
                    break
                oversized = accumulator.oversized
                accepted_last_seq = event.seq
            if accepted_last_seq == snapshot.checkpoint:
                raise RuntimeError("shadow batch failed to accept its first event")

            applies = accumulator.applies()
            try:
                assert_live_sqlite_path(self.source, binding)
            except ValueError:
                raise _SourceBindingError(
                    batch_events=accepted_last_seq - snapshot.checkpoint,
                    source_max_seq=source_max,
                ) from None
            if conn.total_changes != before_changes:
                raise RuntimeError("shadow source reader mutated SQLite")
            return applies, source_max, accepted_last_seq, oversized
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    @staticmethod
    def _validate_source_window(
        snapshot: _TargetSnapshot,
        events: Sequence[_SourceEvent],
        *,
        row_count: int,
        max_events: int,
        source_max: int,
        next_seq_present: bool | None = None,
    ) -> None:
        if events[-1].seq < source_max and (
            row_count < max_events or next_seq_present is False
        ):
            raise _EventFailure(
                _Poison(
                    events[-1].seq + 1,
                    "gap",
                    "source sequence continuity failed",
                ),
                snapshot,
                batch_events=row_count,
                source_max_seq=source_max,
            )

    def _apply_statement(self, conn: Any, schema: str, item: _Apply) -> None:
        key_columns = item.spec.replication_key
        if item.event.operation == "delete" or item.row is None:
            predicate = sql.SQL(" AND ").join(
                sql.SQL("{}=%s").format(sql.Identifier(name)) for name in key_columns
            )
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE ").format(
                    _qualified(schema, item.spec.name)
                )
                + predicate,
                item.event.key_values,
            )
            return
        names = tuple(column.name for column in item.columns)
        updates = tuple(name for name in names if name not in key_columns)
        conflict = sql.SQL(", ").join(sql.Identifier(name) for name in key_columns)
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) ").format(
            _qualified(schema, item.spec.name),
            sql.SQL(", ").join(sql.Identifier(name) for name in names),
            sql.SQL(", ").join(sql.Placeholder() for _name in names),
            conflict,
        )
        if updates:
            statement += sql.SQL("DO UPDATE SET ") + sql.SQL(", ").join(
                sql.SQL("{}=EXCLUDED.{}").format(
                    sql.Identifier(name), sql.Identifier(name)
                )
                for name in updates
            )
        else:
            statement += sql.SQL("DO NOTHING")
        conn.execute(statement, item.row)

    @staticmethod
    def _deferred_integrity(item: _Apply, exc: psycopg.IntegrityError) -> _Deferred:
        state = str(getattr(exc, "sqlstate", "") or "")
        if state not in _ORDERING_SQLSTATES:
            raise _EventFailure(
                _Poison(
                    item.event.seq,
                    "constraint",
                    "target constraint rejected event",
                )
            ) from None
        constraint_name = getattr(getattr(exc, "diag", None), "constraint_name", None)
        return _Deferred(
            item,
            state,
            str(constraint_name) if constraint_name else None,
        )

    @staticmethod
    def _item_values(item: _Apply) -> dict[str, Any]:
        if item.row is None:
            return {}
        return {
            column.name: value
            for column, value in zip(item.columns, item.row, strict=True)
        }

    @staticmethod
    def _key_predicate(columns: Sequence[str]) -> sql.Composed:
        return sql.SQL(" AND ").join(
            sql.SQL("{} IS NOT DISTINCT FROM %s").format(sql.Identifier(name))
            for name in columns
        )

    def _parking_candidate(
        self,
        conn: Any,
        schema: str,
        surface: _UniqueSurface,
        values: Mapping[str, Any],
        consume_statement: Callable[[], None],
    ) -> Any:
        assert surface.park_column is not None
        scope_columns = tuple(
            name for name in surface.columns if name != surface.park_column
        )
        scope_parts: list[sql.Composed] = []
        parameter_values: list[Any] = []
        for name in scope_columns:
            value = values[name]
            if value is None:
                scope_parts.append(sql.SQL("{} IS NULL").format(sql.Identifier(name)))
            else:
                scope_parts.append(sql.SQL("{} = %s").format(sql.Identifier(name)))
                parameter_values.append(value)
        scope = sql.SQL(" AND ").join(scope_parts) if scope_parts else sql.SQL("TRUE")
        parameters = tuple(parameter_values)
        if surface.predicate is not None:
            scope += sql.SQL(" AND (") + sql.SQL(surface.predicate) + sql.SQL(")")
        table = _qualified(schema, surface.table)
        column = sql.Identifier(surface.park_column)

        def query(statement: sql.Composed) -> Any:
            try:
                consume_statement()
                return conn.execute(statement, parameters).fetchone()
            except (
                psycopg.errors.ProgramLimitExceeded,
                psycopg.DataError,
            ):
                raise CapacityExceeded(
                    "shadow unique parking candidate search exceeded capacity"
                ) from None

        if surface.strategy is _ParkStrategy.SENTINEL_TEXT:
            row = query(
                sql.SQL(
                    'SELECT CASE WHEN MAX({column} COLLATE "C") IS NULL THEN chr(1) '
                    'ELSE MAX({column} COLLATE "C") || chr(1) END AS candidate '
                    "FROM {table} WHERE {scope}"
                ).format(column=column, table=table, scope=scope)
            )
            candidate = None if row is None else _row_value(row, "candidate", 0)
        else:
            bounds = query(
                sql.SQL(
                    "SELECT MIN({column}) AS lo,MAX({column}) AS hi "
                    "FROM {table} WHERE {scope} AND {column} IS NOT NULL"
                ).format(column=column, table=table, scope=scope)
            )
            lo = None if bounds is None else _row_value(bounds, "lo", 0)
            hi = None if bounds is None else _row_value(bounds, "hi", 1)
            if lo is None or hi is None:
                candidate = 0
            elif lo > _SQLITE_INT64_MIN:
                candidate = lo - 1
            elif hi < _SQLITE_INT64_MAX:
                candidate = hi + 1
            else:
                gap = query(
                    sql.SQL(
                        "WITH vals AS (SELECT DISTINCT {column} AS value "
                        "FROM {table} WHERE {scope} AND {column} IS NOT NULL),"
                        "ordered AS (SELECT value::numeric AS value,"
                        "lead(value::numeric) OVER (ORDER BY value) AS next_value "
                        "FROM vals) SELECT (value+1)::bigint AS candidate FROM ordered "
                        "WHERE next_value-value>1 ORDER BY value LIMIT 1"
                    ).format(column=column, table=table, scope=scope)
                )
                candidate = None if gap is None else _row_value(gap, "candidate", 0)
        if candidate is None:
            raise CapacityExceeded("shadow unique parking surface is physically full")
        return candidate

    def _park_unique_cycles(
        self,
        conn: Any,
        schema: str,
        pending: Sequence[_Deferred],
        final_by_identity: Mapping[tuple[str, tuple[tuple[str, Any], ...]], _Apply],
        parked: set[tuple[str, tuple[str, tuple[tuple[str, Any], ...]]]],
        consume_statement: Callable[[], None],
    ) -> tuple[_Apply, ...]:
        def execute(statement: Any, parameters: Sequence[Any] = ()) -> Any:
            consume_statement()
            return conn.execute(statement, parameters)

        restore: dict[tuple[str, tuple[tuple[str, Any], ...]], _Apply] = {}
        for deferred in pending:
            if (
                deferred.sqlstate != _UNIQUE_SQLSTATE
                or deferred.constraint_name is None
            ):
                continue
            surface = self._unique_surfaces.get(deferred.constraint_name)
            item = deferred.item
            if (
                surface is None
                or surface.table != item.spec.name
                or surface.strategy is _ParkStrategy.REPLICATION_KEY
            ):
                continue
            values = self._item_values(item)
            unique_values = tuple(values[name] for name in surface.columns)
            own_key = item.event.key_values
            statement = sql.SQL("SELECT {} FROM {} WHERE ").format(
                sql.SQL(", ").join(
                    sql.Identifier(name) for name in item.spec.replication_key
                ),
                _qualified(schema, item.spec.name),
            ) + self._key_predicate(surface.columns)
            if surface.predicate is not None:
                statement += (
                    sql.SQL(" AND (") + sql.SQL(surface.predicate) + sql.SQL(")")
                )
            statement += sql.SQL(" AND NOT (")
            statement += self._key_predicate(item.spec.replication_key)
            statement += sql.SQL(") LIMIT 2")
            conflicts = execute(
                statement,
                (*unique_values, *own_key),
            ).fetchall()
            if len(conflicts) != 1:
                continue
            conflict_key = tuple(
                (
                    name,
                    _row_value(conflicts[0], name, position),
                )
                for position, name in enumerate(item.spec.replication_key)
            )
            identity = (item.spec.name, conflict_key)
            parked_key = (surface.name, identity)
            if parked_key in parked:
                continue
            final = final_by_identity.get(identity)
            if final is None or final.row is None:
                continue

            key_values = tuple(value for _name, value in conflict_key)
            key_predicate = self._key_predicate(item.spec.replication_key)
            parked_ok = False
            if surface.strategy is _ParkStrategy.LEAF_DELETE:
                execute(
                    sql.SQL("DELETE FROM {} WHERE ").format(
                        _qualified(schema, item.spec.name)
                    )
                    + key_predicate,
                    key_values,
                )
                parked_ok = True
            elif surface.strategy is _ParkStrategy.NULL:
                assert surface.park_column is not None
                execute(
                    sql.SQL("UPDATE {} SET {}=NULL WHERE ").format(
                        _qualified(schema, item.spec.name),
                        sql.Identifier(surface.park_column),
                    )
                    + key_predicate,
                    key_values,
                )
                parked_ok = True
            else:
                assert surface.park_column is not None
                sentinel = self._parking_candidate(
                    conn, schema, surface, values, consume_statement
                )
                savepoint = sql.Identifier(f"shadow_park_{len(parked)}")
                execute(sql.SQL("SAVEPOINT {}").format(savepoint))
                try:
                    execute(
                        sql.SQL("UPDATE {} SET {}=%s WHERE ").format(
                            _qualified(schema, item.spec.name),
                            sql.Identifier(surface.park_column),
                        )
                        + key_predicate,
                        (sentinel, *key_values),
                    )
                except (
                    psycopg.errors.ProgramLimitExceeded,
                    psycopg.DataError,
                ):
                    execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(savepoint))
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                    raise CapacityExceeded(
                        "shadow unique parking update exceeded capacity"
                    ) from None
                except psycopg.IntegrityError:
                    execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(savepoint))
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                else:
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                    parked_ok = True
            if parked_ok:
                parked.add(parked_key)
                restore[identity] = final
        return tuple(restore.values())

    def _apply_batch(
        self,
        snapshot: _TargetSnapshot,
        applies: Sequence[_Apply],
        *,
        run_id: str,
        direction: Direction,
        last_seq: int,
        has_future_events: bool,
    ) -> int:
        with self.target.write() as conn:
            state = self._validate_target_state(conn, run_id, direction)
            self._claim_worker(conn, run_id, direction)
            existing_poison = conn.execute(
                "SELECT source_seq FROM silicon_shadow.poison_events "
                "WHERE run_id=%s AND direction=%s ORDER BY source_seq LIMIT 1",
                (run_id, direction.value),
            ).fetchone()
            if existing_poison is not None:
                raise PoisonEventRecorded(
                    int(_row_value(existing_poison, "source_seq", 0)), "existing"
                )
            live_target = fingerprint_postgres(_target_url(self.target), conn)
            if (
                state.target_business_schema != snapshot.business_schema
                or state.source_identity_hash != snapshot.source_identity_hash
                or state.target_identity_hash != snapshot.target_identity_hash
                or live_target.identity_hash != snapshot.target_identity_hash
            ):
                raise _EventFailure(
                    _Poison(
                        snapshot.checkpoint + 1,
                        "identity",
                        "shadow run identity validation failed",
                    ),
                    snapshot,
                )
            # The target is inactive. SHARE ROW EXCLUSIVE on the migration
            # ledger and every business table closes direct-DDL and non-worker
            # RowExclusive DML races while still allowing read-only inspection.
            self._lock_business_catalog(
                conn, state.target_business_schema, block_external_writes=True
            )
            self._validate_target_catalog(conn, state)
            checkpoint = conn.execute(
                "SELECT last_seq,revision FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction=%s FOR UPDATE",
                (run_id, direction.value),
            ).fetchone()
            if checkpoint is None:
                raise ValueError("shadow forward checkpoint disappeared")
            current_checkpoint = int(_row_value(checkpoint, "last_seq", 0))
            current_revision = int(_row_value(checkpoint, "revision", 1))
            self._validate_checkpoint_progress(state, current_checkpoint, snapshot)
            if (
                current_checkpoint == last_seq
                and current_revision == snapshot.revision + 1
                and state.progress.get("applied_seq") == last_seq
            ):
                # The preceding attempt committed and only its client-side
                # commit acknowledgement was lost. Never replay or poison it.
                return last_seq
            if (current_checkpoint, current_revision) != (
                snapshot.checkpoint,
                snapshot.revision,
            ):
                raise ValueError("shadow forward checkpoint changed concurrently")
            self._crash("before_target")
            statement_budget = _StatementBudget(
                min(
                    _MAX_TARGET_STATEMENTS,
                    max(1, len(applies)) * _MAX_DEFERRED_STATEMENT_FACTOR,
                )
            )

            def execute(statement: Any, parameters: Sequence[Any] = ()) -> Any:
                statement_budget.consume()
                return conn.execute(statement, parameters)

            parked: set[tuple[str, tuple[str, tuple[tuple[str, Any], ...]]]] = set()

            def attempt(item: _Apply, savepoint_name: str) -> _Deferred | None:
                savepoint = sql.Identifier(savepoint_name)
                execute(sql.SQL("SAVEPOINT {}").format(savepoint))
                try:
                    statement_budget.consume()
                    self._apply_statement(conn, state.target_business_schema, item)
                except psycopg.IntegrityError as exc:
                    execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(savepoint))
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                    return self._deferred_integrity(item, exc)
                except (psycopg.DataError, psycopg.ProgrammingError):
                    execute(sql.SQL("ROLLBACK TO SAVEPOINT {}").format(savepoint))
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                    raise _EventFailure(
                        _Poison(
                            item.event.seq,
                            "schema",
                            "target schema rejected event",
                        )
                    ) from None
                else:
                    execute(sql.SQL("RELEASE SAVEPOINT {}").format(savepoint))
                    parked.difference_update(
                        parked_key
                        for parked_key in tuple(parked)
                        if parked_key[1] == item.event.identity
                    )
                    return None

            deferred: list[_Deferred] = []
            initial_progressed = False
            for index, item in enumerate(applies):
                blocked = attempt(item, f"shadow_event_{index}")
                if blocked is not None:
                    deferred.append(blocked)
                else:
                    initial_progressed = True
                if index == len(applies) // 2:
                    self._crash("mid_batch")

            final_by_identity = {
                item.event.identity: item for item in applies if item.row is not None
            }
            for pass_number in range(_MAX_DEFERRED_PASSES):
                if not deferred:
                    break
                pending: list[_Deferred] = []
                progressed = False
                if pass_number == 0 and not initial_progressed:
                    pending.extend(deferred)
                else:
                    for index, blocked in enumerate(deferred):
                        retried = attempt(
                            blocked.item,
                            f"shadow_retry_{pass_number}_{index}",
                        )
                        if retried is None:
                            progressed = True
                        else:
                            pending.append(retried)
                if pending and not progressed:
                    restores = self._park_unique_cycles(
                        conn,
                        state.target_business_schema,
                        pending,
                        final_by_identity,
                        parked,
                        statement_budget.consume,
                    )
                    if restores:
                        pending_identities = {
                            blocked.item.event.identity for blocked in pending
                        }
                        pending.extend(
                            _Deferred(item, _UNIQUE_SQLSTATE, None)
                            for item in restores
                            if item.event.identity not in pending_identities
                        )
                        deferred = pending
                        continue
                    if has_future_events:
                        raise OrderingBlocked(
                            "shadow event ordering requires a wider batch window"
                        )
                    failed = min(pending, key=lambda item: item.item.event.seq)
                    raise _EventFailure(
                        _Poison(
                            failed.item.event.seq,
                            "constraint",
                            "target constraint rejected event",
                        )
                    ) from None
                deferred = pending
            if deferred or parked:
                raise OrderingBlocked(
                    "shadow deferred ordering pass budget was exhausted"
                )
            self._crash("before_checkpoint")
            cursor = execute(
                "UPDATE silicon_shadow.checkpoints SET last_seq=%s,revision=revision+1,"
                "updated_at=clock_timestamp() WHERE run_id=%s AND direction=%s "
                "AND last_seq=%s AND revision=%s RETURNING last_seq",
                (
                    last_seq,
                    run_id,
                    direction.value,
                    snapshot.checkpoint,
                    snapshot.revision,
                ),
            )
            if cursor.fetchone() is None:
                raise ValueError("shadow forward checkpoint CAS failed")
            progress = dict(state.progress)
            progress.update(stage="forward", applied_seq=last_seq)
            encoded_progress = _validated_progress(progress)
            run_cursor = execute(
                "UPDATE silicon_shadow.runs SET progress=%s::jsonb,revision=revision+1,"
                "updated_at=clock_timestamp() WHERE run_id=%s AND revision=%s RETURNING run_id",
                (encoded_progress, run_id, state.revision),
            )
            if run_cursor.fetchone() is None:
                raise ValueError("shadow forward run progress CAS failed")
            self._crash("after_checkpoint_before_commit")
        self._crash("after_commit_before_next_source_read")
        return last_seq

    def _record_poison(
        self,
        run_id: str,
        direction: Direction,
        poison: _Poison,
        snapshot: _TargetSnapshot,
    ) -> int:
        def write() -> None:
            with self.target.write() as conn:
                _lock_schema_and_control(conn)
                _validate_control_schema(conn)
                row = conn.execute(
                    f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("shadow poison run does not exist")
                state = _state_from_row(row)
                _validated_progress(state.progress)
                live_target = fingerprint_postgres(_target_url(self.target), conn)
                if (
                    state.phase is not ShadowPhase.SQLITE_TO_POSTGRES
                    or state.active_backend != "sqlite"
                    or not state.capture_enabled
                    or state.schema_pair != self.manifest.schema_pair
                    or state.source_identity_hash != snapshot.source_identity_hash
                    or state.target_identity_hash != snapshot.target_identity_hash
                    or live_target.identity_hash != snapshot.target_identity_hash
                    or state.target_business_schema != snapshot.business_schema
                ):
                    raise StaleFailureDiscarded(
                        "shadow poison run binding changed before publication"
                    )
                self._claim_worker(conn, run_id, direction)
                checkpoint = conn.execute(
                    "SELECT last_seq,revision FROM silicon_shadow.checkpoints "
                    "WHERE run_id=%s AND direction=%s FOR UPDATE",
                    (run_id, direction.value),
                ).fetchone()
                if checkpoint is None:
                    raise ValueError("shadow poison checkpoint disappeared")
                if (
                    int(_row_value(checkpoint, "last_seq", 0)),
                    int(_row_value(checkpoint, "revision", 1)),
                ) != (snapshot.checkpoint, snapshot.revision):
                    raise StaleFailureDiscarded(
                        "shadow poison evidence is stale after checkpoint progress"
                    )
                existing = conn.execute(
                    "SELECT source_seq,category,safe_summary "
                    "FROM silicon_shadow.poison_events "
                    "WHERE run_id=%s AND direction=%s "
                    "ORDER BY source_seq LIMIT 1 FOR UPDATE",
                    (run_id, direction.value),
                ).fetchone()
                if existing is not None:
                    _validate_existing_poison(existing, poison)
                    return
                cursor = conn.execute(
                    "INSERT INTO silicon_shadow.poison_events "
                    "(run_id,direction,source_seq,category,safe_summary) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING source_seq",
                    (
                        run_id,
                        direction.value,
                        poison.source_seq,
                        poison.category,
                        poison.safe_summary,
                    ),
                )
                if cursor.fetchone() is None:
                    existing = conn.execute(
                        "SELECT category,safe_summary FROM silicon_shadow.poison_events "
                        "WHERE run_id=%s AND direction=%s AND source_seq=%s",
                        (run_id, direction.value, poison.source_seq),
                    ).fetchone()
                    if existing is None or (
                        str(_row_value(existing, "category", 0)),
                        str(_row_value(existing, "safe_summary", 1)),
                    ) != (poison.category, poison.safe_summary):
                        raise ValueError("shadow poison replay does not match")

        _result, retries = self._retry(write)
        return retries

    @staticmethod
    def _poison_for(exc: Exception, expected_seq: int) -> _Poison:
        if isinstance(exc, _EventFailure):
            return exc.poison
        if isinstance(exc, _SourceBindingError):
            return _Poison(
                expected_seq,
                "identity",
                "shadow run identity validation failed",
            )
        if isinstance(exc, psycopg.IntegrityError):
            return _Poison(
                expected_seq, "constraint", "target constraint rejected event"
            )
        if isinstance(exc, (psycopg.ProgrammingError, psycopg.DataError)):
            return _Poison(expected_seq, "schema", "target schema rejected event")
        text = str(exc).lower()
        if "contiguous" in text or "retention" in text or "ahead" in text:
            return _Poison(expected_seq, "gap", "source sequence continuity failed")
        if "identity" in text or "different run" in text:
            return _Poison(
                expected_seq, "identity", "shadow run identity validation failed"
            )
        if "schema" in text or "catalog" in text or "guard" in text:
            return _Poison(expected_seq, "schema", "shadow schema validation failed")
        if "change log" in text or "replication key" in text or "operation" in text:
            return _Poison(
                expected_seq, "event_contract", "source event contract failed"
            )
        return _Poison(expected_seq, "conversion", "source row conversion failed")

    def run_batch(
        self,
        *,
        run_id: str,
        direction: Direction,
        max_events: int,
        max_bytes: int,
    ) -> BatchResult:
        direction = self._validate_batch_request(
            run_id, direction, max_events, max_bytes
        )
        started = time.monotonic()
        if not self._run_lock.acquire(blocking=False):
            self.metrics.record(
                ReplicationMetric(
                    run_id,
                    direction.value,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    max(0.0, time.monotonic() - started),
                    0,
                    0,
                )
            )
            raise WorkerLeaseUnavailable(
                "this shadow replicator already has an active batch"
            )
        try:
            return self._run_batch(
                run_id=run_id,
                direction=direction,
                max_events=max_events,
                max_bytes=max_bytes,
            )
        finally:
            self._run_lock.release()

    @staticmethod
    def _validate_batch_request(
        run_id: str,
        direction: Direction,
        max_events: int,
        max_bytes: int,
    ) -> Direction:
        if not isinstance(direction, Direction):
            try:
                direction = Direction(direction)
            except (TypeError, ValueError):
                raise ValueError("shadow replication direction is invalid") from None
        if direction is not Direction.SQLITE_TO_POSTGRES:
            raise ValueError(
                "the first shadow replicator supports forward direction only"
            )
        if not isinstance(run_id, str) or not run_id or run_id.strip() != run_id:
            raise ValueError("shadow run id is invalid")
        limits = (
            ("max_events", max_events, _MAX_BATCH_EVENTS),
            ("max_bytes", max_bytes, _MAX_BATCH_BYTES),
        )
        for name, value, upper_bound in limits:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= upper_bound
            ):
                raise ValueError(f"shadow {name} must be between one and {upper_bound}")
        return direction

    def _run_batch(
        self,
        *,
        run_id: str,
        direction: Direction,
        max_events: int,
        max_bytes: int,
    ) -> BatchResult:
        direction = self._validate_batch_request(
            run_id, direction, max_events, max_bytes
        )
        started = time.monotonic()
        retries = 0
        checkpoint_before = 0
        checkpoint_after = 0
        source_max = 0
        events_read = 0
        rows_applied = 0
        hydrated_bytes = 0
        oversized = False
        poison_count = 0
        try:
            try:
                snapshot, used = self._retry(
                    lambda: self._target_snapshot(run_id, direction)
                )
            except _EventFailure as exc:
                retries += int(getattr(exc, "shadow_retry_count", 0))
                poison = exc.poison
                failure_snapshot = exc.snapshot
                if failure_snapshot is None:
                    raise RuntimeError(
                        "shadow poison lost its target binding"
                    ) from None
                checkpoint_before = failure_snapshot.checkpoint
                checkpoint_after = checkpoint_before
                source_max = checkpoint_before
                retries += self._record_poison(
                    run_id, direction, poison, failure_snapshot
                )
                poison_count = 1
                raise PoisonEventRecorded(poison.source_seq, poison.category) from None
            retries += used
            checkpoint_before = snapshot.checkpoint
            checkpoint_after = checkpoint_before
            try:
                (applies, source_max, accepted_last_seq, oversized), used = self._retry(
                    lambda: self._read_source(
                        snapshot,
                        run_id=run_id,
                        max_events=max_events,
                        max_bytes=max_bytes,
                    )
                )
                retries += used
                events_read = accepted_last_seq - checkpoint_before
                rows_applied = len(applies)
                hydrated_bytes = sum(item.encoded_bytes for item in applies)
                if not applies:
                    if events_read != 0:
                        raise RuntimeError(
                            "shadow empty batch advanced its source prefix"
                        )
                else:
                    checkpoint_after, used = self._retry(
                        lambda: self._apply_batch(
                            snapshot,
                            applies,
                            run_id=run_id,
                            direction=direction,
                            last_seq=accepted_last_seq,
                            has_future_events=source_max > accepted_last_seq,
                        )
                    )
                    retries += used
            except CrashInjected:
                raise
            except Exception as exc:
                failure_events = getattr(exc, "batch_events", 0)
                failure_source_max = getattr(exc, "source_max_seq", 0)
                if isinstance(failure_events, int) and not isinstance(
                    failure_events, bool
                ):
                    events_read = max(events_read, failure_events)
                if isinstance(failure_source_max, int) and not isinstance(
                    failure_source_max, bool
                ):
                    source_max = max(source_max, failure_source_max)
                if isinstance(
                    exc,
                    (
                        CapacityExceeded,
                        OrderingBlocked,
                        RetryExhausted,
                        WorkerLeaseUnavailable,
                        PoisonEventRecorded,
                    ),
                ):
                    raise
                retries += int(getattr(exc, "shadow_retry_count", 0))
                poison = self._poison_for(exc, checkpoint_before + 1)
                retries += self._record_poison(run_id, direction, poison, snapshot)
                poison_count = 1
                raise PoisonEventRecorded(poison.source_seq, poison.category) from None
            duration = max(0.0, time.monotonic() - started)
            return BatchResult(
                run_id,
                direction,
                checkpoint_before,
                checkpoint_after,
                source_max,
                events_read,
                rows_applied,
                hydrated_bytes,
                retries,
                duration,
                oversized,
            )
        except RetryExhausted as exc:
            retries += exc.retries
            raise
        except PoisonEventRecorded as exc:
            retries += int(getattr(exc, "shadow_retry_count", 0))
            poison_count = 1
            raise
        except Exception as exc:
            retries += int(getattr(exc, "shadow_retry_count", 0))
            raise
        finally:
            duration = max(0.0, time.monotonic() - started)
            self.metrics.record(
                ReplicationMetric(
                    run_id,
                    direction.value,
                    checkpoint_before,
                    checkpoint_after,
                    source_max,
                    max(0, source_max - checkpoint_after),
                    events_read,
                    rows_applied,
                    hydrated_bytes,
                    duration,
                    retries,
                    poison_count,
                )
            )

    def run_forever(
        self,
        *,
        run_id: str,
        direction: Direction,
        stop: threading.Event,
    ) -> None:
        if not isinstance(stop, threading.Event):
            raise TypeError("shadow replicator stop must be a threading.Event")
        max_events = _DEFAULT_BATCH_EVENTS
        max_bytes = _DEFAULT_BATCH_BYTES
        while not stop.is_set():
            try:
                result = self.run_batch(
                    run_id=run_id,
                    direction=direction,
                    max_events=max_events,
                    max_bytes=max_bytes,
                )
            except OrderingBlocked:
                if max_events >= _MAX_BATCH_EVENTS and max_bytes >= _MAX_BATCH_BYTES:
                    raise
                max_events = min(_MAX_BATCH_EVENTS, max_events * 2)
                max_bytes = min(_MAX_BATCH_BYTES, max_bytes * 2)
                continue
            max_events = _DEFAULT_BATCH_EVENTS
            max_bytes = _DEFAULT_BATCH_BYTES
            if result.events_read == 0:
                stop.wait(self.idle_seconds)
