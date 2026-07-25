"""Barrier-aware, redacted SQLite/PostgreSQL shadow verification.

The verifier deliberately lives outside both repository adapters.  SQLite is
still the only live authority: a verification run records one SQLite snapshot
barrier, waits for the forward checkpoint, pins a PostgreSQL repeatable-read
snapshot, and excludes only keys proven concurrent by the durable change log.

Large databases are streamed through a private temporary SQLite spool.  Raw
business values never enter persistent verification reports; those contain
only manifest table names, SHA-256 key identities, fixed categories, and safe
summaries.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any

from psycopg import IsolationLevel, sql
from psycopg.types.json import Jsonb

from app.migration.shadow.bulk_copy import (
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
    validate_postgres_connection,
    validate_sqlite_schema,
)
from app.migration.shadow.postgres_catalog import (
    EXPECTED_CONSTRAINTS,
    validate_postgres_v11_catalog,
)
from app.migration.shadow.replicator import Direction, _json_object_exact
from app.migration.shadow.snapshot import (
    _change_log_high_water,
    assert_live_sqlite_path,
    open_fresh_live_sqlite,
)
from app.migration.shadow.transform import (
    PostgresColumn,
    canonical_json_value,
    transform_sqlite_row,
)
from app.migration.shadow.types import Manifest, TableSpec
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TOKEN = re.compile(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff]")
_DOMAIN_TABLES = frozenset(
    {
        "notebooks",
        "sources",
        "knowledge_objects",
        "memory_items",
        "knowhow_tables",
        "reports",
        "notebook_members",
    }
)
_FORWARD_DIRECTION = Direction.SQLITE_TO_POSTGRES.value
_DEFAULT_CHUNK_ROWS = 1_000
_DEFAULT_DIFFERENCE_LIMIT = 10_000
_DEFAULT_BARRIER_TTL_SECONDS = 3_600
_DEFAULT_WAIT_TIMEOUT_SECONDS = 60.0
_DEFAULT_POLL_SECONDS = 0.1
_DEFAULT_EMBEDDING_SAMPLE = 128
_COSINE_MINIMUM = 0.99999
_NORM_RELATIVE_TOLERANCE = 1e-6
_RECALL_LOSS_MAXIMUM = 0.01
_TOP10_OVERLAP_MINIMUM = 0.90


class VerificationLevel(StrEnum):
    STRUCTURAL = "structural"
    FULL = "full"
    CUTOVER = "cutover"


class VerificationStatus(StrEnum):
    COMPLETE = "complete"
    DRIFT = "drift"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class VerificationNotReady(RuntimeError):
    """The target checkpoint did not reach the source barrier in time."""


class VerificationFailed(RuntimeError):
    """A verifier protocol/schema/identity invariant failed closed."""


@dataclass(frozen=True)
class VerificationDifference:
    table_name: str
    key_hash: str | None
    category: str
    safe_summary: str


@dataclass(frozen=True)
class TableVerification:
    table_name: str
    source_rows: int
    target_rows: int
    stable_source_rows: int
    stable_target_rows: int
    concurrent_rows: int
    compared_rows: int
    source_root_hash: str
    target_root_hash: str

    @property
    def coverage(self) -> float:
        denominator = max(self.source_rows, self.target_rows)
        if denominator == 0:
            return 1.0
        return min(1.0, self.compared_rows / denominator)


@dataclass(frozen=True)
class RetrievalVerification:
    query_id: str
    recall_at_12: float
    top10_overlap: float
    citation_ids_match: bool
    source_ids_match: bool


@dataclass(frozen=True)
class VerificationReport:
    verification_id: str
    run_id: str
    level: VerificationLevel
    status: VerificationStatus
    source_barrier: int
    target_checkpoint: int
    source_seen: int
    concurrent_keys: int
    tables: tuple[TableVerification, ...]
    retrieval: tuple[RetrievalVerification, ...]
    embedding_samples: int
    differences: tuple[VerificationDifference, ...]
    differences_truncated: bool
    duration_seconds: float

    @property
    def coverage(self) -> float:
        rows = sum(max(table.source_rows, table.target_rows) for table in self.tables)
        if rows == 0:
            return 1.0
        compared = sum(table.compared_rows for table in self.tables)
        return min(1.0, compared / rows)


@dataclass(frozen=True)
class VerificationHooks:
    """Deterministic test seams at the three barrier boundaries."""

    after_source_snapshot: Callable[[int], None] | None = None
    after_target_snapshot: Callable[[int], None] | None = None
    after_concurrent_scan: Callable[[int], None] | None = None


@dataclass(frozen=True)
class _RetrievalQuery:
    query_id: str
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class _VectorSummary:
    length: int
    dimension: int
    norm: float
    sha256: str
    valid: bool


@dataclass(frozen=True)
class _StartState:
    verification_id: str
    level: VerificationLevel
    business_schema: str
    source_identity_hash: str
    target_identity_hash: str
    contracts: Mapping[str, tuple[PostgresColumn, ...]]
    previous_complete: bool


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("shadow verifier manifest contains an unsafe identifier")
    return f'"{identifier}"'


def _row_value(row: Any, name: str, position: int) -> Any:
    if isinstance(row, Mapping) or hasattr(row, "keys"):
        return row[name]
    return row[position]


def _decimal_text(value: Decimal | int | float) -> str:
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise ValueError("shadow verifier numeric value is invalid") from exc
    if not number.is_finite():
        raise ValueError("shadow verifier numeric value is non-finite")
    if number == 0:
        return "0"
    normalized = number.normalize()
    text = format(normalized, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _canonical_json_tree(value: Any) -> Any:
    if isinstance(value, Jsonb):
        value = value.obj
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return ["number", _decimal_text(value)]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("shadow verifier JSON keys must be strings")
        return [
            "object",
            [[key, _canonical_json_tree(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_json_tree(child) for child in value]]
    raise ValueError("shadow verifier JSON value is unsupported")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic JSON bytes, preserving null and rejecting non-finite."""

    return json.dumps(
        _canonical_json_tree(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_replication_key(
    spec: TableSpec, values: Mapping[str, Any] | Sequence[tuple[str, Any]]
) -> str:
    """Encode a stable key in manifest column order (never sorted object order)."""

    mapping = dict(values)
    if tuple(mapping) != spec.replication_key and isinstance(values, Sequence):
        names = tuple(name for name, _value in values)
        if names != spec.replication_key:
            raise ValueError("shadow verifier replication key shape changed")
    if set(mapping) != set(spec.replication_key):
        raise ValueError("shadow verifier replication key shape changed")
    encoded = [
        [name, _canonical_scalar(mapping[name], None, None)]
        for name in spec.replication_key
    ]
    return json.dumps(
        encoded, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _canonical_path(value: str, storage_root: Path) -> tuple[Any, bool]:
    if "\x00" in value:
        return ["path-invalid"], True
    candidate = Path(value)
    try:
        root = storage_root.resolve(strict=True)
        resolved = (
            candidate.resolve(strict=True)
            if candidate.is_absolute()
            else (root / candidate).resolve(strict=True)
        )
        relative = resolved.relative_to(root)
        valid = resolved.is_file()
    except (OSError, ValueError):
        return ["path-invalid"], True
    return ["path", relative.as_posix()], not valid


def _canonical_scalar(
    value: Any,
    column: PostgresColumn | None,
    storage_root: Path | None,
    *,
    is_path: bool = False,
) -> Any:
    data_type = None if column is None else column.data_type
    if value is None:
        return ["null"]
    if is_path:
        if not isinstance(value, str) or storage_root is None:
            return ["path-invalid"]
        return _canonical_path(value, storage_root)[0]
    if data_type == "jsonb":
        parsed = value.obj if isinstance(value, Jsonb) else value
        if isinstance(parsed, str):
            parsed = canonical_json_value(parsed)
        return ["json", _canonical_json_tree(parsed)]
    if data_type == "bytea" or isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return ["bytes", len(payload), hashlib.sha256(payload).hexdigest()]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        instant = value.astimezone(timezone.utc)
        return ["instant", instant.isoformat(timespec="microseconds")]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return ["number", _decimal_text(value)]
    raise ValueError("shadow verifier scalar value is unsupported")


def canonical_row_bytes(
    spec: TableSpec,
    columns: Sequence[PostgresColumn],
    values: Sequence[Any],
    *,
    storage_root: Path,
) -> tuple[bytes, bool]:
    if len(columns) != len(values):
        raise ValueError("shadow verifier row shape changed")
    encoded: list[list[Any]] = []
    path_error = False
    for column, value in zip(columns, values, strict=True):
        is_path = column.name in spec.path_columns and value not in (None, "")
        normalized = _canonical_scalar(value, column, storage_root, is_path=is_path)
        if is_path:
            _path_value, invalid = _canonical_path(str(value), storage_root)
            path_error = path_error or invalid
        encoded.append([column.name, normalized])
    return (
        json.dumps(
            encoded,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        path_error,
    )


def vector_summary(value: bytes | bytearray | memoryview) -> _VectorSummary:
    payload = bytes(value)
    digest = hashlib.sha256(payload).hexdigest()
    if len(payload) % 4:
        return _VectorSummary(len(payload), 0, 0.0, digest, False)
    dimension = len(payload) // 4
    squared = 0.0
    valid = True
    try:
        for (item,) in struct.iter_unpack("<f", payload):
            if not math.isfinite(item):
                valid = False
                break
            squared += float(item) * float(item)
    except struct.error:
        valid = False
    return _VectorSummary(
        len(payload), dimension, math.sqrt(squared) if valid else 0.0, digest, valid
    )


def vector_cosine(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or len(left) % 4:
        raise ValueError("shadow verifier vectors have different dimensions")
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for (a,), (b,) in zip(
        struct.iter_unpack("<f", left), struct.iter_unpack("<f", right), strict=True
    ):
        if not math.isfinite(a) or not math.isfinite(b):
            raise ValueError("shadow verifier vector contains a non-finite value")
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0.0 or right_norm == 0.0:
        return 1.0 if left == right else 0.0
    return dot / math.sqrt(left_norm * right_norm)


def _query_tokens(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_TOKEN.findall(text.casefold())))


def _retrieval_score(text: str, tokens: Sequence[str]) -> int:
    folded = text.casefold()
    return sum(folded.count(token) for token in tokens)


def _hash(value: str | bytes) -> str:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


class _DifferenceCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.items: list[VerificationDifference] = []
        self.truncated = False

    def add(
        self,
        table_name: str,
        category: str,
        safe_summary: str,
        key_hash: str | None = None,
    ) -> None:
        if len(self.items) >= self.limit:
            self.truncated = True
            return
        self.items.append(
            VerificationDifference(table_name, key_hash, category, safe_summary)
        )

    def finalized(self) -> tuple[VerificationDifference, ...]:
        if not self.truncated:
            return tuple(self.items)
        marker = VerificationDifference(
            "__report__",
            None,
            "truncated",
            "additional redacted verification differences were omitted",
        )
        if len(self.items) == self.limit:
            return (*self.items[:-1], marker)
        return (*self.items, marker)


class _Spool:
    def __init__(self) -> None:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="silicon-shadow-verify-", suffix=".db"
        )
        os.close(descriptor)
        self.path = Path(raw_path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE source_facts(
                table_name TEXT NOT NULL,
                key_json TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                row_bytes INTEGER NOT NULL,
                path_error INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(table_name,key_json)
            ) WITHOUT ROWID;
            CREATE TABLE target_facts(
                table_name TEXT NOT NULL,
                key_json TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                row_bytes INTEGER NOT NULL,
                path_error INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(table_name,key_json)
            ) WITHOUT ROWID;
            CREATE TABLE concurrent_keys(
                table_name TEXT NOT NULL,
                key_json TEXT NOT NULL,
                PRIMARY KEY(table_name,key_json)
            ) WITHOUT ROWID;
            CREATE TABLE source_vectors(
                table_name TEXT NOT NULL,key_json TEXT NOT NULL,column_name TEXT NOT NULL,
                byte_length INTEGER NOT NULL,dimension INTEGER NOT NULL,norm REAL NOT NULL,
                sha256 TEXT NOT NULL,valid INTEGER NOT NULL,
                PRIMARY KEY(table_name,key_json,column_name)
            ) WITHOUT ROWID;
            CREATE TABLE target_vectors(
                table_name TEXT NOT NULL,key_json TEXT NOT NULL,column_name TEXT NOT NULL,
                byte_length INTEGER NOT NULL,dimension INTEGER NOT NULL,norm REAL NOT NULL,
                sha256 TEXT NOT NULL,valid INTEGER NOT NULL,
                PRIMARY KEY(table_name,key_json,column_name)
            ) WITHOUT ROWID;
            CREATE TABLE source_retrieval(
                query_id TEXT NOT NULL,key_json TEXT NOT NULL,score INTEGER NOT NULL,
                citation_hash TEXT NOT NULL,source_hash TEXT NOT NULL,
                PRIMARY KEY(query_id,key_json)
            ) WITHOUT ROWID;
            CREATE TABLE target_retrieval(
                query_id TEXT NOT NULL,key_json TEXT NOT NULL,score INTEGER NOT NULL,
                citation_hash TEXT NOT NULL,source_hash TEXT NOT NULL,
                PRIMARY KEY(query_id,key_json)
            ) WITHOUT ROWID;
            CREATE TABLE signals(
                name TEXT NOT NULL PRIMARY KEY,
                value INTEGER NOT NULL
            ) WITHOUT ROWID;
            """
        )

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class ShadowVerifier:
    def __init__(
        self,
        source: Any,
        target: Any,
        *,
        manifest: Manifest = MANIFEST,
        storage_root: str | Path | None = None,
        chunk_rows: int = _DEFAULT_CHUNK_ROWS,
        difference_limit: int = _DEFAULT_DIFFERENCE_LIMIT,
        barrier_ttl_seconds: int = _DEFAULT_BARRIER_TTL_SECONDS,
        wait_timeout_seconds: float = _DEFAULT_WAIT_TIMEOUT_SECONDS,
        poll_seconds: float = _DEFAULT_POLL_SECONDS,
        embedding_sample_limit: int = _DEFAULT_EMBEDDING_SAMPLE,
        golden_path: str | Path | None = None,
        hooks: VerificationHooks | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if isinstance(chunk_rows, bool) or not 1 <= chunk_rows <= 100_000:
            raise ValueError("shadow verifier chunk_rows is invalid")
        if isinstance(difference_limit, bool) or not 1 <= difference_limit <= 100_000:
            raise ValueError("shadow verifier difference_limit is invalid")
        if not 60 <= barrier_ttl_seconds <= 86_400:
            raise ValueError("shadow verifier barrier TTL is invalid")
        if wait_timeout_seconds < 0 or poll_seconds < 0:
            raise ValueError("shadow verifier wait timing is invalid")
        if not 0 <= embedding_sample_limit <= 10_000:
            raise ValueError("shadow verifier embedding sample limit is invalid")
        self.source = source
        self.target = target
        self.manifest = manifest
        self.chunk_rows = chunk_rows
        self.difference_limit = difference_limit
        self.barrier_ttl_seconds = barrier_ttl_seconds
        self.wait_timeout_seconds = wait_timeout_seconds
        self.poll_seconds = poll_seconds
        self.embedding_sample_limit = embedding_sample_limit
        configured_storage = (
            Path(storage_root)
            if storage_root is not None
            else Path(source.settings.storage_dir)
        )
        self.storage_root = configured_storage
        self.golden_path = (
            Path(golden_path)
            if golden_path is not None
            else Path(__file__).parents[3]
            / "tests"
            / "fixtures"
            / "shadow_retrieval_golden.json"
        )
        self.hooks = hooks or VerificationHooks()
        self.sleep = sleep
        self._specs = {spec.name: spec for spec in manifest.replicated}

    def _state_is_verifiable(self, state: Any) -> bool:
        return (
            state.phase
            in {ShadowPhase.SQLITE_TO_POSTGRES, ShadowPhase.CUTOVER_READONLY}
            and state.active_backend == "sqlite"
            and state.capture_enabled
            and state.sqlite_guard_report_hash is not None
            and state.postgres_guard_report_hash is not None
            and state.schema_pair == self.manifest.schema_pair
        )

    def _queries(self, level: VerificationLevel) -> tuple[_RetrievalQuery, ...]:
        if level is VerificationLevel.STRUCTURAL:
            return ()
        try:
            payload = json.loads(self.golden_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise VerificationFailed(
                "shadow retrieval golden fixture is unavailable"
            ) from None
        if not isinstance(payload, Mapping) or payload.get("version") != 1:
            raise VerificationFailed("shadow retrieval golden fixture is invalid")
        raw_queries = payload.get("queries")
        if not isinstance(raw_queries, list) or not raw_queries:
            raise VerificationFailed("shadow retrieval golden fixture has no queries")
        result: list[_RetrievalQuery] = []
        seen: set[str] = set()
        for item in raw_queries:
            if not isinstance(item, Mapping):
                raise VerificationFailed("shadow retrieval golden query is invalid")
            query_id = item.get("id")
            text = item.get("query")
            if (
                not isinstance(query_id, str)
                or not query_id
                or query_id in seen
                or not isinstance(text, str)
                or not text.strip()
            ):
                raise VerificationFailed("shadow retrieval golden query is invalid")
            tokens = _query_tokens(text)
            if not tokens:
                raise VerificationFailed("shadow retrieval golden query has no tokens")
            seen.add(query_id)
            result.append(_RetrievalQuery(query_id, text, tokens))
        return tuple(result)

    def _validate_target_state(self, conn: Any, run_id: str) -> Any:
        _lock_schema_and_control(conn)
        _validate_control_schema(conn)
        row = conn.execute(
            f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (run_id,)
        ).fetchone()
        if row is None:
            raise VerificationFailed("shadow verification run does not exist")
        state = _state_from_row(row)
        _validated_progress(state.progress)
        live_target = fingerprint_postgres(_target_url(self.target), conn)
        if not self._state_is_verifiable(state) or (
            state.target_identity_hash != live_target.identity_hash
        ):
            raise VerificationFailed("shadow verification target binding is invalid")
        return state

    def _validate_target_catalog(self, conn: Any, state: Any) -> None:
        version = _postgres_schema_version(conn, state.target_business_schema)
        if version != self.manifest.schema_pair.postgres_version:
            raise VerificationFailed("shadow verification target schema changed")
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
        validate_postgres_v11_catalog(
            conn,
            business_schema=state.target_business_schema,
            manifest=self.manifest,
            guard_names=frozenset(item.name for item in definitions),
        )

    def _start(
        self,
        run_id: str,
        level: VerificationLevel,
        barrier: int,
        source_identity_hash: str,
    ) -> _StartState:
        verification_id = uuid.uuid4().hex
        with self.target.write() as conn:
            state = self._validate_target_state(conn, run_id)
            if state.source_identity_hash != source_identity_hash:
                raise VerificationFailed("shadow verification source binding changed")
            poison = conn.execute(
                "SELECT source_seq FROM silicon_shadow.poison_events "
                "WHERE run_id=%s AND direction=%s ORDER BY source_seq LIMIT 1",
                (run_id, _FORWARD_DIRECTION),
            ).fetchone()
            if poison is not None:
                raise VerificationFailed("shadow verification is blocked by poison")
            self._validate_target_catalog(conn, state)
            contracts = {
                spec.name: _postgres_columns(conn, spec, state.target_business_schema)
                for spec in self.manifest.replicated
            }
            previous = conn.execute(
                "SELECT status FROM silicon_shadow.verification_runs "
                "WHERE run_id=%s AND level IN ('full','cutover') "
                "ORDER BY started_at DESC,verification_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            previous_complete = (
                previous is not None
                and str(_row_value(previous, "status", 0))
                == VerificationStatus.COMPLETE.value
            )
            conn.execute(
                "INSERT INTO silicon_shadow.verification_runs"
                "(verification_id,run_id,level,status,source_barrier) "
                "VALUES (%s,%s,%s,'running',%s)",
                (verification_id, run_id, level.value, barrier),
            )
            conn.execute(
                "INSERT INTO silicon_shadow.verifier_barriers"
                "(verification_id,run_id,source_barrier,expires_at) "
                "VALUES (%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))",
                (verification_id, run_id, barrier, self.barrier_ttl_seconds),
            )
            return _StartState(
                verification_id,
                level,
                state.target_business_schema,
                source_identity_hash,
                state.target_identity_hash,
                contracts,
                previous_complete,
            )

    def _refresh_barrier(self, started: _StartState, run_id: str) -> None:
        with self.target.write() as conn:
            state = self._validate_target_state(conn, run_id)
            if (
                state.source_identity_hash != started.source_identity_hash
                or state.target_identity_hash != started.target_identity_hash
            ):
                raise VerificationFailed("shadow verification binding changed")
            cursor = conn.execute(
                "UPDATE silicon_shadow.verifier_barriers "
                "SET expires_at=clock_timestamp()+(%s * interval '1 second') "
                "WHERE verification_id=%s AND run_id=%s RETURNING verification_id",
                (self.barrier_ttl_seconds, started.verification_id, run_id),
            )
            if cursor.fetchone() is None:
                raise VerificationFailed("shadow verification barrier disappeared")

    def _source_row(
        self,
        raw: Any,
        spec: TableSpec,
        columns: Sequence[PostgresColumn],
        sqlite_columns: Sequence[str],
    ) -> tuple[tuple[Any, ...], Mapping[str, Any]]:
        mapping = {
            name: _row_value(raw, name, position)
            for position, name in enumerate(sqlite_columns)
        }
        if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
            mapping["ordinal"] = _row_value(raw, "ordinal", len(sqlite_columns))
        return transform_sqlite_row(spec, columns, mapping), mapping

    def _insert_fact(
        self,
        spool: _Spool,
        side: str,
        spec: TableSpec,
        columns: Sequence[PostgresColumn],
        values: Sequence[Any],
        key_mapping: Mapping[str, Any],
        queries: Sequence[_RetrievalQuery],
        vector_samples: dict[tuple[str, str, str], bytes],
    ) -> None:
        key_json = canonical_replication_key(spec, key_mapping)
        key_hash = _hash(key_json)
        row_bytes, path_error = canonical_row_bytes(
            spec, columns, values, storage_root=self.storage_root
        )
        spool.conn.execute(
            f"INSERT INTO {side}_facts"
            "(table_name,key_json,key_hash,row_hash,row_bytes,path_error) "
            "VALUES (?,?,?,?,?,?)",
            (
                spec.name,
                key_json,
                key_hash,
                _hash(row_bytes),
                len(row_bytes),
                int(path_error),
            ),
        )
        value_map = {
            column.name: value for column, value in zip(columns, values, strict=True)
        }
        for column_name in spec.blob_columns:
            payload = value_map.get(column_name)
            if payload is None:
                continue
            if not isinstance(payload, (bytes, bytearray, memoryview)):
                raise VerificationFailed("shadow embedding storage is not bytea")
            raw = bytes(payload)
            summary = vector_summary(raw)
            spool.conn.execute(
                f"INSERT INTO {side}_vectors VALUES (?,?,?,?,?,?,?,?)",
                (
                    spec.name,
                    key_json,
                    column_name,
                    summary.length,
                    summary.dimension,
                    summary.norm,
                    summary.sha256,
                    int(summary.valid),
                ),
            )
            sample_key = (spec.name, key_json, column_name)
            if side == "source" and len(vector_samples) < self.embedding_sample_limit:
                vector_samples[sample_key] = raw
        if spec.name == "chunks" and queries:
            text = value_map.get("text")
            source_id = value_map.get("source_id")
            if isinstance(text, str) and isinstance(source_id, str):
                for query in queries:
                    score = _retrieval_score(text, query.tokens)
                    if score:
                        spool.conn.execute(
                            f"INSERT INTO {side}_retrieval VALUES (?,?,?,?,?)",
                            (
                                query.query_id,
                                key_json,
                                score,
                                key_hash,
                                _hash(source_id),
                            ),
                        )

    def _stream_source(
        self,
        conn: sqlite3.Connection,
        started: _StartState,
        spool: _Spool,
        run_id: str,
        queries: Sequence[_RetrievalQuery],
        vector_samples: dict[tuple[str, str, str], bytes],
    ) -> None:
        foreign_key_violation = conn.execute("PRAGMA foreign_key_check").fetchone()
        spool.conn.execute(
            "INSERT INTO signals(name,value) VALUES ('source_foreign_key_violation',?)",
            (int(foreign_key_violation is not None),),
        )
        spool.conn.commit()
        for table_index, spec in enumerate(
            sorted(self.manifest.replicated, key=lambda item: item.copy_rank)
        ):
            info = conn.execute(f"PRAGMA table_info({_quote(spec.name)})").fetchall()
            sqlite_columns = tuple(str(_row_value(row, "name", 1)) for row in info)
            columns = _copy_columns(sqlite_columns, started.contracts[spec.name], spec)
            selected = ",".join(_quote(name) for name in sqlite_columns)
            if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
                selected += ",rowid AS ordinal"
            order = ",".join(_quote(name) for name in spec.replication_key)
            cursor = conn.execute(
                f"SELECT {selected} FROM {_quote(spec.name)} ORDER BY {order}"
            )
            while rows := cursor.fetchmany(self.chunk_rows):
                for raw in rows:
                    values, mapping = self._source_row(
                        raw, spec, columns, sqlite_columns
                    )
                    self._insert_fact(
                        spool,
                        "source",
                        spec,
                        columns,
                        values,
                        {name: mapping[name] for name in spec.replication_key},
                        queries,
                        vector_samples,
                    )
                spool.conn.commit()
            if table_index % 8 == 7:
                self._refresh_barrier(started, run_id)

    def _wait_checkpoint(self, started: _StartState, run_id: str, barrier: int) -> int:
        deadline = time.monotonic() + self.wait_timeout_seconds
        while True:
            with self.target.connect() as conn:
                row = conn.execute(
                    "SELECT c.last_seq,r.source_identity_hash,r.target_identity_hash,"
                    "r.phase,r.active_backend,r.capture_enabled,"
                    "EXISTS(SELECT 1 FROM silicon_shadow.poison_events p "
                    "WHERE p.run_id=r.run_id AND p.direction=%s) AS poisoned "
                    "FROM silicon_shadow.runs r JOIN silicon_shadow.checkpoints c "
                    "ON c.run_id=r.run_id AND c.direction=%s WHERE r.run_id=%s",
                    (_FORWARD_DIRECTION, _FORWARD_DIRECTION, run_id),
                ).fetchone()
            if row is None:
                raise VerificationFailed("shadow verification checkpoint disappeared")
            if (
                str(_row_value(row, "source_identity_hash", 1))
                != started.source_identity_hash
                or str(_row_value(row, "target_identity_hash", 2))
                != started.target_identity_hash
                or str(_row_value(row, "phase", 3))
                not in {
                    ShadowPhase.SQLITE_TO_POSTGRES.value,
                    ShadowPhase.CUTOVER_READONLY.value,
                }
                or str(_row_value(row, "active_backend", 4)) != "sqlite"
                or not bool(_row_value(row, "capture_enabled", 5))
                or bool(_row_value(row, "poisoned", 6))
            ):
                raise VerificationFailed(
                    "shadow verification binding or poison changed"
                )
            checkpoint = int(_row_value(row, "last_seq", 0))
            if checkpoint >= barrier:
                return checkpoint
            if time.monotonic() >= deadline:
                raise VerificationNotReady(
                    "PostgreSQL shadow checkpoint did not reach the verification barrier"
                )
            self._refresh_barrier(started, run_id)
            self.sleep(self.poll_seconds)

    def _pin_target(
        self, conn: Any, started: _StartState, run_id: str
    ) -> tuple[Any, int]:
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        conn.read_only = True
        row = conn.execute(f"{_RUN_SELECT} WHERE run_id=%s", (run_id,)).fetchone()
        if row is None:
            raise VerificationFailed("shadow verification run disappeared")
        state = _state_from_row(row)
        checkpoint_row = conn.execute(
            "SELECT last_seq FROM silicon_shadow.checkpoints "
            "WHERE run_id=%s AND direction=%s",
            (run_id, _FORWARD_DIRECTION),
        ).fetchone()
        if checkpoint_row is None:
            raise VerificationFailed("shadow verification checkpoint disappeared")
        checkpoint = int(_row_value(checkpoint_row, "last_seq", 0))
        live_target = fingerprint_postgres(_target_url(self.target), conn)
        if (
            not self._state_is_verifiable(state)
            or state.source_identity_hash != started.source_identity_hash
            or state.target_identity_hash != started.target_identity_hash
            or live_target.identity_hash != started.target_identity_hash
            or state.target_business_schema != started.business_schema
            or state.schema_pair != self.manifest.schema_pair
        ):
            raise VerificationFailed("shadow verification pinned target changed")
        poison = conn.execute(
            "SELECT 1 FROM silicon_shadow.poison_events "
            "WHERE run_id=%s AND direction=%s LIMIT 1",
            (run_id, _FORWARD_DIRECTION),
        ).fetchone()
        if poison is not None:
            raise VerificationFailed("shadow verification is blocked by poison")
        return state, checkpoint

    def _cutover_source_state(self, run_id: str) -> tuple[bool, int]:
        conn, binding = open_fresh_live_sqlite(self.source)
        try:
            conn.execute("BEGIN")
            control = conn.execute(
                "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
                "FROM shadow_capture_control WHERE singleton=1"
            ).fetchone()
            if control is None:
                raise VerificationFailed("shadow cutover source control disappeared")
            enabled, write_frozen, apply_active, bound_run, epoch = tuple(control)
            if (
                enabled != 1
                or apply_active != 0
                or str(bound_run) != run_id
                or int(epoch) != self.manifest.schema_pair.epoch
            ):
                raise VerificationFailed("shadow cutover source binding changed")
            high_water = _change_log_high_water(conn)
            assert_live_sqlite_path(self.source, binding)
            return write_frozen == 1, high_water
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def _scan_concurrent(
        self, spool: _Spool, run_id: str, barrier: int
    ) -> tuple[int, int]:
        conn, binding = open_fresh_live_sqlite(self.source)
        try:
            conn.execute("BEGIN")
            seen = _change_log_high_water(conn)
            if seen < barrier:
                raise VerificationFailed(
                    "shadow verification source high-water regressed"
                )
            expected = barrier + 1
            cursor = conn.execute(
                "SELECT seq,run_id,table_name,key_json,operation,schema_epoch "
                "FROM shadow_change_log WHERE seq>? AND seq<=? ORDER BY seq",
                (barrier, seen),
            )
            count = 0
            while rows := cursor.fetchmany(self.chunk_rows):
                for row in rows:
                    seq = int(_row_value(row, "seq", 0))
                    if seq != expected:
                        raise VerificationFailed(
                            "shadow verification change-log continuity failed"
                        )
                    expected += 1
                    if (
                        str(_row_value(row, "run_id", 1)) != run_id
                        or int(_row_value(row, "schema_epoch", 5))
                        != self.manifest.schema_pair.epoch
                    ):
                        raise VerificationFailed(
                            "shadow verification change-log binding failed"
                        )
                    table = str(_row_value(row, "table_name", 2))
                    spec = self._specs.get(table)
                    if spec is None:
                        raise VerificationFailed(
                            "shadow verification change-log table is unknown"
                        )
                    pairs = _json_object_exact(str(_row_value(row, "key_json", 3)))
                    if tuple(name for name, _value in pairs) != spec.replication_key:
                        raise VerificationFailed(
                            "shadow verification change-log key shape changed"
                        )
                    key_json = canonical_replication_key(spec, pairs)
                    spool.conn.execute(
                        "INSERT OR IGNORE INTO concurrent_keys VALUES (?,?)",
                        (table, key_json),
                    )
                    count += 1
                spool.conn.commit()
            if expected != seen + 1:
                raise VerificationFailed(
                    "shadow verification retained log does not cover its barrier"
                )
            assert_live_sqlite_path(self.source, binding)
            return seen, count
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def _stream_target(
        self,
        conn: Any,
        started: _StartState,
        spool: _Spool,
        queries: Sequence[_RetrievalQuery],
        vector_samples: Mapping[tuple[str, str, str], bytes],
        cosine_results: dict[tuple[str, str, str], float],
    ) -> None:
        for spec in sorted(self.manifest.replicated, key=lambda item: item.copy_rank):
            columns = started.contracts[spec.name]
            expressions = []
            for column in columns:
                identifier = sql.Identifier(column.name)
                expressions.append(
                    sql.SQL("{}::text AS {}").format(identifier, identifier)
                    if column.data_type == "jsonb"
                    else identifier
                )
            statement = sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
                sql.SQL(",").join(expressions),
                _qualified(started.business_schema, spec.name),
                sql.SQL(",").join(
                    sql.Identifier(name) for name in spec.replication_key
                ),
            )
            cursor = conn.cursor(name=f"shadow_verify_{spec.copy_rank}")
            cursor.itersize = self.chunk_rows
            cursor.execute(statement)
            while rows := cursor.fetchmany(self.chunk_rows):
                for raw in rows:
                    values: list[Any] = []
                    for position, column in enumerate(columns):
                        value = _row_value(raw, column.name, position)
                        if column.data_type == "jsonb" and value is not None:
                            value = canonical_json_value(str(value))
                        values.append(value)
                    key_mapping = {
                        name: _row_value(
                            raw,
                            name,
                            next(
                                index
                                for index, column in enumerate(columns)
                                if column.name == name
                            ),
                        )
                        for name in spec.replication_key
                    }
                    self._insert_fact(
                        spool,
                        "target",
                        spec,
                        columns,
                        values,
                        key_mapping,
                        queries,
                        {},
                    )
                    key_json = canonical_replication_key(spec, key_mapping)
                    value_map = {
                        column.name: value
                        for column, value in zip(columns, values, strict=True)
                    }
                    for column_name in spec.blob_columns:
                        sample_key = (spec.name, key_json, column_name)
                        source_vector = vector_samples.get(sample_key)
                        target_vector = value_map.get(column_name)
                        if source_vector is not None and isinstance(
                            target_vector, (bytes, bytearray, memoryview)
                        ):
                            try:
                                cosine_results[sample_key] = vector_cosine(
                                    source_vector, bytes(target_vector)
                                )
                            except ValueError:
                                cosine_results[sample_key] = -1.0
                spool.conn.commit()
            cursor.close()

    def _stable_clause(self, alias: str) -> str:
        return (
            "NOT EXISTS(SELECT 1 FROM concurrent_keys c "
            f"WHERE c.table_name={alias}.table_name AND c.key_json={alias}.key_json)"
        )

    def _table_reports(
        self, spool: _Spool, differences: _DifferenceCollector
    ) -> tuple[TableVerification, ...]:
        source_fk = spool.conn.execute(
            "SELECT value FROM signals WHERE name='source_foreign_key_violation'"
        ).fetchone()
        if source_fk is None or int(source_fk[0]):
            differences.add(
                "__source__",
                "foreign_key",
                "source foreign-key data is invalid",
            )
        reports: list[TableVerification] = []
        for spec in sorted(self.manifest.replicated, key=lambda item: item.copy_rank):
            table = spec.name
            source_rows = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM source_facts WHERE table_name=?", (table,)
                ).fetchone()[0]
            )
            target_rows = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM target_facts WHERE table_name=?", (table,)
                ).fetchone()[0]
            )
            concurrent = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM concurrent_keys WHERE table_name=?", (table,)
                ).fetchone()[0]
            )
            stable_source = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM source_facts s WHERE table_name=? AND "
                    + self._stable_clause("s"),
                    (table,),
                ).fetchone()[0]
            )
            stable_target = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM target_facts t WHERE table_name=? AND "
                    + self._stable_clause("t"),
                    (table,),
                ).fetchone()[0]
            )
            missing = spool.conn.execute(
                "SELECT s.key_hash FROM source_facts s LEFT JOIN target_facts t "
                "ON t.table_name=s.table_name AND t.key_json=s.key_json "
                "WHERE s.table_name=? AND t.key_json IS NULL AND "
                + self._stable_clause("s")
                + " LIMIT ?",
                (table, self.difference_limit + 1),
            ).fetchall()
            extra = spool.conn.execute(
                "SELECT t.key_hash FROM target_facts t LEFT JOIN source_facts s "
                "ON s.table_name=t.table_name AND s.key_json=t.key_json "
                "WHERE t.table_name=? AND s.key_json IS NULL AND "
                + self._stable_clause("t")
                + " LIMIT ?",
                (table, self.difference_limit + 1),
            ).fetchall()
            mismatched = spool.conn.execute(
                "SELECT s.key_hash FROM source_facts s JOIN target_facts t "
                "ON t.table_name=s.table_name AND t.key_json=s.key_json "
                "WHERE s.table_name=? AND s.row_hash<>t.row_hash AND "
                + self._stable_clause("s")
                + " LIMIT ?",
                (table, self.difference_limit + 1),
            ).fetchall()
            for row in missing:
                differences.add(
                    table, "missing_target", "stable source row is missing", str(row[0])
                )
            for row in extra:
                differences.add(
                    table, "extra_target", "stable target row is extra", str(row[0])
                )
            for row in mismatched:
                differences.add(
                    table, "row_hash", "stable normalized row differs", str(row[0])
                )
            for side in ("source", "target"):
                path_errors = spool.conn.execute(
                    f"SELECT f.key_hash FROM {side}_facts f WHERE f.table_name=? "
                    "AND f.path_error=1 AND " + self._stable_clause("f") + " LIMIT ?",
                    (table, self.difference_limit + 1),
                ).fetchall()
                for row in path_errors:
                    differences.add(
                        table,
                        "file_reference",
                        f"stable {side} file reference is invalid",
                        str(row[0]),
                    )
            source_digest = hashlib.sha256()
            target_digest = hashlib.sha256()
            for side, digest in (("source", source_digest), ("target", target_digest)):
                cursor = spool.conn.execute(
                    f"SELECT f.key_json,f.row_hash FROM {side}_facts f "
                    "WHERE f.table_name=? AND "
                    + self._stable_clause("f")
                    + " ORDER BY f.key_json",
                    (table,),
                )
                compared = 0
                while rows := cursor.fetchmany(self.chunk_rows):
                    for row in rows:
                        digest.update(str(row[0]).encode("utf-8"))
                        digest.update(b"\x00")
                        digest.update(str(row[1]).encode("ascii"))
                        digest.update(b"\n")
                        compared += 1
            matched = int(
                spool.conn.execute(
                    "SELECT COUNT(*) FROM source_facts s JOIN target_facts t "
                    "ON t.table_name=s.table_name AND t.key_json=s.key_json "
                    "WHERE s.table_name=? AND s.row_hash=t.row_hash AND "
                    + self._stable_clause("s"),
                    (table,),
                ).fetchone()[0]
            )
            reports.append(
                TableVerification(
                    table,
                    source_rows,
                    target_rows,
                    stable_source,
                    stable_target,
                    concurrent,
                    matched,
                    source_digest.hexdigest(),
                    target_digest.hexdigest(),
                )
            )
        return tuple(reports)

    def _verify_vectors(
        self,
        spool: _Spool,
        differences: _DifferenceCollector,
        cosine_results: Mapping[tuple[str, str, str], float],
    ) -> int:
        invalid = spool.conn.execute(
            "SELECT v.table_name,f.key_hash FROM source_vectors v "
            "JOIN source_facts f ON f.table_name=v.table_name AND f.key_json=v.key_json "
            "WHERE v.valid=0 AND "
            + self._stable_clause("v")
            + " UNION ALL SELECT v.table_name,f.key_hash FROM target_vectors v "
            "JOIN target_facts f ON f.table_name=v.table_name AND f.key_json=v.key_json "
            "WHERE v.valid=0 AND " + self._stable_clause("v") + " LIMIT ?",
            (self.difference_limit + 1,),
        ).fetchall()
        for row in invalid:
            differences.add(
                str(row[0]),
                "embedding_invalid",
                "stable embedding is invalid",
                str(row[1]),
            )
        mismatch = spool.conn.execute(
            "SELECT s.table_name,f.key_hash FROM source_vectors s JOIN target_vectors t "
            "ON t.table_name=s.table_name AND t.key_json=s.key_json AND t.column_name=s.column_name "
            "JOIN source_facts f ON f.table_name=s.table_name AND f.key_json=s.key_json "
            "WHERE (s.byte_length<>t.byte_length OR s.dimension<>t.dimension) AND "
            + self._stable_clause("s")
            + " LIMIT ?",
            (self.difference_limit + 1,),
        ).fetchall()
        for row in mismatch:
            differences.add(
                str(row[0]),
                "embedding_dimension",
                "stable embedding dimensions differ",
                str(row[1]),
            )
        samples = 0
        for (table, key_json, _column), cosine in cosine_results.items():
            concurrent = spool.conn.execute(
                "SELECT 1 FROM concurrent_keys WHERE table_name=? AND key_json=?",
                (table, key_json),
            ).fetchone()
            if concurrent is not None:
                continue
            samples += 1
            if cosine < _COSINE_MINIMUM:
                key = spool.conn.execute(
                    "SELECT key_hash FROM source_facts WHERE table_name=? AND key_json=?",
                    (table, key_json),
                ).fetchone()
                differences.add(
                    table,
                    "embedding_cosine",
                    "stable sampled embedding cosine is below tolerance",
                    None if key is None else str(key[0]),
                )
        norms = spool.conn.execute(
            "SELECT s.table_name,f.key_hash,s.norm,t.norm FROM source_vectors s "
            "JOIN target_vectors t ON t.table_name=s.table_name AND t.key_json=s.key_json "
            "AND t.column_name=s.column_name JOIN source_facts f "
            "ON f.table_name=s.table_name AND f.key_json=s.key_json "
            "WHERE " + self._stable_clause("s") + " LIMIT ?",
            (self.difference_limit + 1,),
        ).fetchall()
        for row in norms:
            left, right = float(row[2]), float(row[3])
            if abs(left - right) > _NORM_RELATIVE_TOLERANCE * max(left, right, 1.0):
                differences.add(
                    str(row[0]),
                    "embedding_norm",
                    "stable embedding norms differ",
                    str(row[1]),
                )
        return samples

    def _top_retrieval(self, spool: _Spool, side: str, query_id: str, limit: int):
        return spool.conn.execute(
            f"SELECT r.citation_hash,r.source_hash FROM {side}_retrieval r "
            "WHERE r.query_id=? AND NOT EXISTS(SELECT 1 FROM concurrent_keys c "
            "WHERE c.table_name='chunks' AND c.key_json=r.key_json) "
            "ORDER BY r.score DESC,r.key_json LIMIT ?",
            (query_id, limit),
        ).fetchall()

    def _verify_retrieval(
        self,
        spool: _Spool,
        queries: Sequence[_RetrievalQuery],
        differences: _DifferenceCollector,
    ) -> tuple[RetrievalVerification, ...]:
        reports: list[RetrievalVerification] = []
        for query in queries:
            source12 = self._top_retrieval(spool, "source", query.query_id, 12)
            target12 = self._top_retrieval(spool, "target", query.query_id, 12)
            source_citations = [str(row[0]) for row in source12]
            target_citations = [str(row[0]) for row in target12]
            source_sources = {str(row[1]) for row in source12}
            target_sources = {str(row[1]) for row in target12}
            baseline = set(source_citations)
            recall = (
                1.0
                if not baseline
                else len(baseline & set(target_citations)) / len(baseline)
            )
            source10 = set(source_citations[:10])
            target10 = set(target_citations[:10])
            overlap = 1.0 if not source10 else len(source10 & target10) / len(source10)
            citations_match = set(source_citations) == set(target_citations)
            sources_match = source_sources == target_sources
            if 1.0 - recall > _RECALL_LOSS_MAXIMUM:
                differences.add(
                    "chunks",
                    "retrieval_recall",
                    "retrieval recall@12 loss exceeds one point",
                    _hash(query.query_id),
                )
            if overlap < _TOP10_OVERLAP_MINIMUM:
                differences.add(
                    "chunks",
                    "retrieval_overlap",
                    "retrieval top-10 overlap is below threshold",
                    _hash(query.query_id),
                )
            if not citations_match:
                differences.add(
                    "chunks",
                    "retrieval_citations",
                    "retrieval citation ids differ",
                    _hash(query.query_id),
                )
            if not sources_match:
                differences.add(
                    "chunks",
                    "retrieval_sources",
                    "retrieval source ids differ",
                    _hash(query.query_id),
                )
            reports.append(
                RetrievalVerification(
                    query.query_id,
                    recall,
                    overlap,
                    citations_match,
                    sources_match,
                )
            )
        return tuple(reports)

    def _verify_constraints(
        self, conn: Any, schema: str, differences: _DifferenceCollector
    ) -> None:
        unvalidated = conn.execute(
            "SELECT c.conname FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
            "WHERE n.nspname=%s AND NOT c.convalidated LIMIT 1",
            (schema,),
        ).fetchone()
        if unvalidated is not None:
            differences.add(
                "__catalog__", "constraint", "target constraint is not validated"
            )
        for constraint in EXPECTED_CONSTRAINTS.values():
            if constraint.kind != "f" or constraint.referenced_table is None:
                continue
            child_alias = sql.Identifier("c")
            parent_alias = sql.Identifier("p")
            join = sql.SQL(" AND ").join(
                sql.SQL("{}.{}={}.{}").format(
                    child_alias,
                    sql.Identifier(child),
                    parent_alias,
                    sql.Identifier(parent),
                )
                for child, parent in zip(
                    constraint.columns, constraint.referenced_columns, strict=True
                )
            )
            nonnull = sql.SQL(" AND ").join(
                sql.SQL("{}.{} IS NOT NULL").format(child_alias, sql.Identifier(child))
                for child in constraint.columns
            )
            first_parent = constraint.referenced_columns[0]
            orphan = conn.execute(
                sql.SQL(
                    "SELECT 1 FROM {} {} LEFT JOIN {} {} ON {} WHERE {} AND {}.{} IS NULL LIMIT 1"
                ).format(
                    _qualified(schema, constraint.table),
                    child_alias,
                    _qualified(schema, constraint.referenced_table),
                    parent_alias,
                    join,
                    nonnull,
                    parent_alias,
                    sql.Identifier(first_parent),
                )
            ).fetchone()
            if orphan is not None:
                differences.add(
                    constraint.table,
                    "foreign_key",
                    "target foreign-key data is invalid",
                )

    def _finish(
        self,
        started: _StartState,
        run_id: str,
        status: VerificationStatus,
        target_checkpoint: int,
        differences: Sequence[VerificationDifference],
    ) -> None:
        with self.target.write() as conn:
            state = self._validate_target_state(conn, run_id)
            if (
                state.source_identity_hash != started.source_identity_hash
                or state.target_identity_hash != started.target_identity_hash
            ):
                raise VerificationFailed(
                    "shadow verification binding changed before report"
                )
            row = conn.execute(
                "SELECT source_barrier,status,level FROM silicon_shadow.verification_runs "
                "WHERE verification_id=%s AND run_id=%s FOR UPDATE",
                (started.verification_id, run_id),
            ).fetchone()
            if (
                row is None
                or str(_row_value(row, "status", 1)) != "running"
                or str(_row_value(row, "level", 2)) != started.level.value
            ):
                raise VerificationFailed("shadow verification report state changed")
            conn.execute(
                "DELETE FROM silicon_shadow.verification_differences WHERE verification_id=%s",
                (started.verification_id,),
            )
            for number, difference in enumerate(differences, 1):
                conn.execute(
                    "INSERT INTO silicon_shadow.verification_differences"
                    "(verification_id,difference_no,table_name,key_hash,category,safe_summary) "
                    "VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        started.verification_id,
                        number,
                        difference.table_name,
                        difference.key_hash,
                        difference.category,
                        difference.safe_summary,
                    ),
                )
            conn.execute(
                "UPDATE silicon_shadow.verification_runs SET status=%s,target_checkpoint=%s,"
                "completed_at=clock_timestamp() WHERE verification_id=%s",
                (status.value, target_checkpoint, started.verification_id),
            )
            conn.execute(
                "DELETE FROM silicon_shadow.verifier_barriers WHERE verification_id=%s",
                (started.verification_id,),
            )
            if status is VerificationStatus.COMPLETE:
                conn.execute(
                    "UPDATE silicon_shadow.verification_runs SET status='superseded' "
                    "WHERE run_id=%s AND verification_id<>%s AND status='drift' "
                    "AND completed_at IS NOT NULL AND completed_at < clock_timestamp() "
                    "AND ((%s='structural' AND level='structural') "
                    "OR (%s='full' AND level IN ('structural','full')) "
                    "OR %s='cutover')",
                    (
                        run_id,
                        started.verification_id,
                        started.level.value,
                        started.level.value,
                        started.level.value,
                    ),
                )

    def _finish_failed(self, started: _StartState, run_id: str) -> None:
        try:
            with self.target.write() as conn:
                _lock_schema_and_control(conn)
                _validate_control_schema(conn)
                conn.execute(
                    "UPDATE silicon_shadow.verification_runs SET status='failed',"
                    "completed_at=clock_timestamp() WHERE verification_id=%s AND run_id=%s "
                    "AND status='running'",
                    (started.verification_id, run_id),
                )
                conn.execute(
                    "DELETE FROM silicon_shadow.verifier_barriers WHERE verification_id=%s",
                    (started.verification_id,),
                )
        except Exception:
            # The TTL remains the crash-safe retention fence if cleanup cannot
            # reach PostgreSQL. Never hide the original verifier failure.
            pass

    def verify(self, *, run_id: str, level: VerificationLevel) -> VerificationReport:
        if not isinstance(level, VerificationLevel):
            try:
                level = VerificationLevel(level)
            except (TypeError, ValueError):
                raise ValueError("shadow verification level is invalid") from None
        if not isinstance(run_id, str) or not run_id or run_id.strip() != run_id:
            raise ValueError("shadow verification run id is invalid")
        started_at = time.monotonic()
        queries = self._queries(level)
        spool = _Spool()
        started: _StartState | None = None
        target_checkpoint = 0
        source_seen = 0
        try:
            source_conn, binding = open_fresh_live_sqlite(self.source)
            try:
                source_conn.execute("BEGIN")
                source_identity = fingerprint_sqlite(
                    self.source.settings.database_url, source_conn
                )
                validate_sqlite_schema(
                    source_conn,
                    self.manifest,
                    expected_pair=self.manifest.schema_pair,
                )
                validate_sqlite_capture(source_conn, self.manifest)
                control = source_conn.execute(
                    "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
                    "FROM shadow_capture_control WHERE singleton=1"
                ).fetchone()
                if control is None:
                    raise VerificationFailed("shadow source capture control is missing")
                enabled, write_frozen, apply_active, bound_run, epoch = tuple(control)
                if (
                    enabled != 1
                    or apply_active != 0
                    or str(bound_run) != run_id
                    or int(epoch) != self.manifest.schema_pair.epoch
                    or (level is VerificationLevel.CUTOVER and write_frozen != 1)
                ):
                    raise VerificationFailed("shadow source capture binding is invalid")
                barrier = _change_log_high_water(source_conn)
                started = self._start(
                    run_id, level, barrier, source_identity.identity_hash
                )
                vector_samples: dict[tuple[str, str, str], bytes] = {}
                self._stream_source(
                    source_conn,
                    started,
                    spool,
                    run_id,
                    queries,
                    vector_samples,
                )
                assert_live_sqlite_path(self.source, binding)
            finally:
                if source_conn.in_transaction:
                    source_conn.rollback()
                source_conn.close()
            if self.hooks.after_source_snapshot is not None:
                self.hooks.after_source_snapshot(barrier)
            target_checkpoint = self._wait_checkpoint(started, run_id, barrier)
            differences = _DifferenceCollector(self.difference_limit)
            cosine_results: dict[tuple[str, str, str], float] = {}
            with self.target.connect() as target_conn:
                state, pinned_checkpoint = self._pin_target(
                    target_conn, started, run_id
                )
                target_checkpoint = pinned_checkpoint
                if target_checkpoint < barrier:
                    raise VerificationNotReady(
                        "PostgreSQL pinned checkpoint is behind the source barrier"
                    )
                if self.hooks.after_target_snapshot is not None:
                    self.hooks.after_target_snapshot(target_checkpoint)
                source_seen, _events = self._scan_concurrent(spool, run_id, barrier)
                if self.hooks.after_concurrent_scan is not None:
                    self.hooks.after_concurrent_scan(source_seen)
                self._stream_target(
                    target_conn,
                    started,
                    spool,
                    queries,
                    vector_samples,
                    cosine_results,
                )
                self._verify_constraints(
                    target_conn, state.target_business_schema, differences
                )
            tables = self._table_reports(spool, differences)
            retrieval = self._verify_retrieval(spool, queries, differences)
            embedding_samples = (
                self._verify_vectors(spool, differences, cosine_results)
                if level in {VerificationLevel.FULL, VerificationLevel.CUTOVER}
                else 0
            )
            concurrent_keys = int(
                spool.conn.execute("SELECT COUNT(*) FROM concurrent_keys").fetchone()[0]
            )
            if level is VerificationLevel.FULL:
                for table in tables:
                    if (
                        table.table_name in _DOMAIN_TABLES
                        and table.source_root_hash != table.target_root_hash
                    ):
                        differences.add(
                            table.table_name,
                            "repository_contract",
                            "selected repository projection differs",
                        )
            if level is VerificationLevel.CUTOVER:
                still_frozen, final_source_seen = self._cutover_source_state(run_id)
                source_seen = final_source_seen
                if not still_frozen:
                    differences.add(
                        "__control__",
                        "cutover_frozen",
                        "cutover source is no longer write-frozen",
                    )
                if barrier != target_checkpoint or barrier != source_seen:
                    differences.add(
                        "__control__",
                        "cutover_watermark",
                        "cutover watermarks are not equal",
                    )
                if concurrent_keys:
                    differences.add(
                        "__control__",
                        "cutover_concurrent",
                        "cutover verification found concurrent keys",
                    )
                if not started.previous_complete:
                    differences.add(
                        "__control__",
                        "cutover_history",
                        "cutover requires a preceding complete full verification",
                    )
                if any(table.coverage < 1.0 for table in tables):
                    differences.add(
                        "__control__",
                        "cutover_coverage",
                        "cutover verification coverage is incomplete",
                    )
            status = (
                VerificationStatus.DRIFT
                if differences.items or differences.truncated
                else VerificationStatus.COMPLETE
            )
            finalized_differences = differences.finalized()
            self._finish(
                started,
                run_id,
                status,
                target_checkpoint,
                finalized_differences,
            )
            return VerificationReport(
                started.verification_id,
                run_id,
                level,
                status,
                barrier,
                target_checkpoint,
                source_seen,
                concurrent_keys,
                tables,
                retrieval,
                embedding_samples,
                finalized_differences,
                differences.truncated,
                max(0.0, time.monotonic() - started_at),
            )
        except VerificationNotReady:
            if started is not None:
                try:
                    self._finish(
                        started,
                        run_id,
                        VerificationStatus.INCOMPLETE,
                        target_checkpoint,
                        (),
                    )
                except Exception:
                    self._finish_failed(started, run_id)
            raise
        except BaseException:
            if started is not None:
                self._finish_failed(started, run_id)
            raise
        finally:
            spool.close()


def verify(
    source: Any,
    target: Any,
    *,
    run_id: str,
    level: VerificationLevel,
    **kwargs: Any,
) -> VerificationReport:
    """Convenience entry point used by the operator CLI in the next task."""

    return ShadowVerifier(source, target, **kwargs).verify(run_id=run_id, level=level)
