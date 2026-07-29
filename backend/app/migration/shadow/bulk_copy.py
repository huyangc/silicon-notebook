"""Bounded, resumable SQLite snapshot COPY into the guarded PG target."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

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
from app.migration.shadow.capture import validate_sqlite_capture
from app.migration.shadow.identity import fingerprint_postgres, fingerprint_sqlite
from app.migration.shadow.manifest import (
    validate_manifest,
    validate_postgres_connection,
    validate_sqlite_schema,
)
from app.migration.shadow.snapshot import (
    LiveSqlitePathBinding,
    SnapshotInfo,
    _change_log_high_water,
    assert_live_sqlite_path,
    open_fresh_live_sqlite,
)
from app.migration.shadow.postgres_catalog import validate_postgres_business_catalog
from app.migration.shadow.transform import (
    PostgresColumn,
    canonical_json_value,
    transform_sqlite_row,
)
from app.migration.shadow.types import Manifest, TableSpec
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_INITIAL_ROLLING_HASH = "0" * 64


@dataclass(frozen=True)
class TableCopyReport:
    table_name: str
    rows_copied: int
    bytes_copied: int
    rolling_hash: str


@dataclass(frozen=True)
class CopyReport:
    run_id: str
    snapshot_sha256: str
    baseline_seq: int
    tables: tuple[TableCopyReport, ...]
    rows_copied: int
    bytes_copied: int


class CopyCancelled(RuntimeError):
    """Raised between committed batches after operator cancellation."""


@dataclass(frozen=True)
class LiveSourceFence:
    connection: sqlite3.Connection
    binding: LiveSqlitePathBinding
    source: Any


def _quote(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("shadow COPY manifest contains an unsafe identifier")
    return f'"{identifier}"'


def _row_value(row: Any, name: str, position: int) -> Any:
    if isinstance(row, Mapping) or hasattr(row, "keys"):
        return row[name]
    return row[position]


def _sha256_file(path: Path, cancel_requested: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _poll_cancel(cancel_requested)
            digest.update(chunk)
    return digest.hexdigest()


def _poll_cancel(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise CopyCancelled("PostgreSQL snapshot COPY was cancelled")


@contextmanager
def _cancellable_validation(
    conn: sqlite3.Connection,
    cancel_requested: Callable[[], bool] | None,
):
    interrupted = False

    def progress() -> int:
        nonlocal interrupted
        interrupted = cancel_requested is not None and cancel_requested()
        return int(interrupted)

    conn.set_progress_handler(progress, 1_000)
    try:
        yield
    except sqlite3.OperationalError:
        if interrupted:
            raise CopyCancelled("PostgreSQL snapshot COPY was cancelled") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)


def _integrity_check(conn: sqlite3.Connection) -> Any:
    return conn.execute("PRAGMA integrity_check").fetchone()


def _set_statement_timeout(conn: Any, seconds: float) -> None:
    milliseconds = max(1, math.ceil(seconds * 1000))
    conn.execute(
        "SELECT set_config('statement_timeout',%s,true)",
        (f"{milliseconds}ms",),
    )


def _qualified(schema: str, table: str) -> Any:
    if not isinstance(schema, str) or not schema or "\x00" in schema:
        raise ValueError("shadow COPY contains an unsafe schema identifier")
    if not _IDENTIFIER.fullmatch(table):
        raise ValueError("shadow COPY contains an unsafe schema identifier")
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _validate_snapshot_info(snapshot: SnapshotInfo, manifest: Manifest) -> None:
    if not isinstance(snapshot, SnapshotInfo):
        raise ValueError("shadow snapshot metadata type is invalid")
    if not isinstance(snapshot.path, Path) or not snapshot.path.is_absolute():
        raise ValueError("shadow snapshot path must be absolute")
    if (
        not isinstance(snapshot.run_id, str)
        or not snapshot.run_id
        or snapshot.run_id.strip() != snapshot.run_id
    ):
        raise ValueError("shadow snapshot run id is invalid")
    if (
        not isinstance(snapshot.source_identity_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", snapshot.source_identity_hash) is None
    ):
        raise ValueError("shadow snapshot source identity is invalid")
    for name, value, minimum in (
        ("schema epoch", snapshot.schema_epoch, 1),
        ("baseline sequence", snapshot.baseline_seq, 0),
        ("size", snapshot.size_bytes, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"shadow snapshot {name} is invalid")
    if snapshot.schema_epoch != manifest.schema_pair.epoch:
        raise ValueError("shadow snapshot schema epoch is invalid")
    if (
        not isinstance(snapshot.sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", snapshot.sha256) is None
    ):
        raise ValueError("shadow snapshot SHA-256 is invalid")


def _begin_live_source_fence(
    source: Any,
    snapshot: SnapshotInfo,
    manifest: Manifest,
    *,
    expected_schema_contract: tuple[Any, ...],
) -> LiveSourceFence:
    source_url = source.settings.database_url
    conn, binding = open_fresh_live_sqlite(source)
    try:
        conn.execute("BEGIN IMMEDIATE")
        identity = fingerprint_sqlite(source_url, conn)
        validate_sqlite_schema(
            conn,
            manifest,
            expected_pair=manifest.schema_pair,
            validate_rows=False,
        )
        validate_sqlite_capture(conn, manifest)
        if _sqlite_schema_contract(conn, manifest) != expected_schema_contract:
            raise ValueError("live SQLite column contract drifted from the snapshot")
        row = conn.execute(
            "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
            "FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()
        if (
            identity.identity_hash != snapshot.source_identity_hash
            or row is None
            or tuple(row)
            != (
                1,
                0,
                0,
                snapshot.run_id,
                snapshot.schema_epoch,
            )
        ):
            raise ValueError("live SQLite capture run is not enabled and safe")
        assert_live_sqlite_path(source, binding)
        return LiveSourceFence(conn, binding, source)
    except BaseException:
        try:
            if conn.in_transaction:
                conn.rollback()
        finally:
            conn.close()
        raise


def _validate_live_source(
    source: Any,
    snapshot: SnapshotInfo,
    manifest: Manifest,
    *,
    expected_schema_contract: tuple[Any, ...],
) -> None:
    fence = _begin_live_source_fence(
        source,
        snapshot,
        manifest,
        expected_schema_contract=expected_schema_contract,
    )
    try:
        assert_live_sqlite_path(source, fence.binding)
        fence.connection.commit()
    except BaseException:
        if fence.connection.in_transaction:
            fence.connection.rollback()
        raise
    finally:
        fence.connection.close()


def _sqlite_schema_contract(
    conn: sqlite3.Connection,
    manifest: Manifest,
) -> tuple[Any, ...]:
    table_sql = {
        str(_row_value(row, "name", 0)): " ".join(
            str(_row_value(row, "sql", 1) or "").split()
        )
        for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name IN ("
            + ",".join("?" for _ in manifest.replicated_names)
            + ") ORDER BY name",
            manifest.replicated_names,
        ).fetchall()
    }
    result: list[Any] = []
    for spec in manifest.replicated:
        columns = tuple(
            (
                int(_row_value(row, "cid", 0)),
                str(_row_value(row, "name", 1)),
                str(_row_value(row, "type", 2) or ""),
                bool(_row_value(row, "notnull", 3)),
                _row_value(row, "dflt_value", 4),
                int(_row_value(row, "pk", 5)),
                int(_row_value(row, "hidden", 6)),
            )
            for row in conn.execute(
                f"PRAGMA table_xinfo({_quote(spec.name)})"
            ).fetchall()
        )
        result.append((spec.name, table_sql.get(spec.name), columns))
    return tuple(result)


@contextmanager
def _final_write_with_live_fence(target: Any):
    """Commit PG while retaining an already-validated live SQLite lease."""
    live_fence: LiveSourceFence | None = None

    def retain(fence: LiveSourceFence) -> None:
        nonlocal live_fence
        if live_fence is not None:
            raise RuntimeError("final live SQLite fence was retained twice")
        live_fence = fence

    try:
        with target.write() as pg_conn:
            yield pg_conn, retain
            if live_fence is not None:
                # Still inside target.write(): this is immediately before its
                # __exit__ commits PostgreSQL H0.
                assert_live_sqlite_path(
                    live_fence.source,
                    live_fence.binding,
                )
    except BaseException:
        if live_fence is not None and live_fence.connection.in_transaction:
            live_fence.connection.rollback()
        raise
    else:
        if live_fence is not None and live_fence.connection.in_transaction:
            live_fence.connection.commit()
    finally:
        if live_fence is not None:
            live_fence.connection.close()


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return _canonical_value(value.obj)
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(child) for child in value]
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return {"$timestamp": parsed.astimezone(timezone.utc).isoformat()}
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return {"$decimal": "0" if normalized == 0 else format(normalized, "f")}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": bytes(value).hex()}
    return value


def _canonical_json_number(value: int | float | Decimal) -> list[Any]:
    if isinstance(value, bool):
        raise ValueError("shadow JSON boolean is not a number")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("shadow JSON contains a non-finite number")
        number = Decimal(repr(value))
    else:
        number = Decimal(value)
    if not number.is_finite():
        raise ValueError("shadow JSON contains a non-finite number")
    if number.is_zero():
        return ["number", 0, "0", 0]
    parts = number.as_tuple()
    digits = list(parts.digits)
    exponent = int(parts.exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return [
        "number",
        int(parts.sign),
        "".join(str(digit) for digit in digits),
        exponent,
    ]


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return _canonical_json_value(value.obj)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("shadow JSON object keys must be strings")
        return [
            "object",
            [[key, _canonical_json_value(value[key])] for key in sorted(value)],
        ]
    if isinstance(value, (list, tuple)):
        return ["array", [_canonical_json_value(child) for child in value]]
    if isinstance(value, bool):
        return ["boolean", value]
    if value is None:
        return ["null"]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, (int, float, Decimal)):
        return _canonical_json_number(value)
    raise ValueError("shadow JSON proof contains an unsupported value")


def _canonical_bytes(
    values: Sequence[Any],
    columns: Sequence[PostgresColumn] | None = None,
) -> bytes:
    if columns is not None and len(values) != len(columns):
        raise ValueError("shadow canonical row has the wrong column count")
    canonical = [
        (
            (
                ["json", _canonical_json_value(value)]
                if isinstance(value, Jsonb)
                else ["sql_null"]
                if value is None
                else ["json", _canonical_json_value(value)]
            )
            if columns is not None and columns[index].data_type == "jsonb"
            else ["json", _canonical_json_value(value)]
            if isinstance(value, Jsonb)
            else _canonical_value(value)
        )
        for index, value in enumerate(values)
    ]
    return json.dumps(
        canonical,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _advance_hash(
    previous: str,
    values: Sequence[Any],
    columns: Sequence[PostgresColumn] | None = None,
) -> tuple[str, int]:
    encoded = _canonical_bytes(values, columns)
    digest = hashlib.sha256(bytes.fromhex(previous) + encoded).hexdigest()
    return digest, len(encoded)


def _open_snapshot(
    snapshot: SnapshotInfo,
    cancel_requested: Callable[[], bool] | None = None,
) -> sqlite3.Connection:
    path = Path(snapshot.path)
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError:
        raise ValueError("shadow snapshot file is unavailable") from None
    if path.is_symlink() or not path.is_file() or stat.st_size != snapshot.size_bytes:
        raise ValueError("shadow snapshot file metadata changed")
    if stat.st_mode & 0o077:
        raise ValueError("shadow snapshot permissions are not owner-only")
    if _sha256_file(path, cancel_requested) != snapshot.sha256:
        raise ValueError("shadow snapshot content hash changed")
    uri = resolved.as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        current_resolved = path.resolve(strict=True)
        current_stat = current_resolved.stat()
        if current_resolved != resolved or (
            int(current_stat.st_dev),
            int(current_stat.st_ino),
        ) != (int(stat.st_dev), int(stat.st_ino)):
            raise ValueError("shadow snapshot file changed while opening")
        return conn
    except BaseException:
        conn.close()
        raise


def _snapshot_columns(conn: sqlite3.Connection, spec: TableSpec) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({_quote(spec.name)})").fetchall()
    columns = tuple(str(_row_value(row, "name", 1)) for row in rows)
    if not columns:
        raise ValueError("shadow snapshot table has no columns")
    return columns


def _postgres_columns(
    conn: Any, spec: TableSpec, business_schema: str
) -> tuple[PostgresColumn, ...]:
    rows = conn.execute(
        "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
        (business_schema, spec.name),
    ).fetchall()
    columns = tuple(
        PostgresColumn(
            name=str(_row_value(row, "column_name", 0)),
            data_type=str(_row_value(row, "data_type", 1)),
            nullable=str(_row_value(row, "is_nullable", 2)) == "YES",
        )
        for row in rows
    )
    if not columns:
        raise ValueError("PostgreSQL COPY table has no columns")
    return columns


def _copy_columns(
    sqlite_columns: Sequence[str],
    postgres_columns: Sequence[PostgresColumn],
    spec: TableSpec,
) -> tuple[PostgresColumn, ...]:
    expected = list(sqlite_columns)
    if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
        expected.append("ordinal")
    if [column.name for column in postgres_columns] != expected:
        raise ValueError("SQLite/PostgreSQL COPY column contract changed")
    return tuple(postgres_columns)


def _order_sql(spec: TableSpec, *, prefix: str = "") -> str:
    # SQLite columns use BINARY by default and every PostgreSQL text column in
    # the compatibility schema is COLLATE "C". Avoid an explicit COLLATE on
    # mixed integer/text composite keys: PostgreSQL rejects it for integers.
    return ", ".join(f"{prefix}{_quote(column)}" for column in spec.replication_key)


def _snapshot_batch(
    conn: sqlite3.Connection,
    spec: TableSpec,
    sqlite_columns: Sequence[str],
    postgres_columns: Sequence[PostgresColumn],
    *,
    last_key: Sequence[Any] | None,
    batch_rows: int,
) -> list[tuple[tuple[Any, ...], tuple[Any, ...]]]:
    selected = ", ".join(_quote(column) for column in sqlite_columns)
    if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
        selected = f"{selected}, rowid AS ordinal"
    where = ""
    params: list[Any] = []
    if last_key is not None:
        if len(last_key) != len(spec.replication_key):
            raise ValueError("shadow COPY checkpoint key has the wrong shape")
        keys = ", ".join(_quote(column) for column in spec.replication_key)
        placeholders = ", ".join("?" for _ in spec.replication_key)
        where = f" WHERE ({keys}) > ({placeholders})"
        params.extend(last_key)
    cursor = conn.execute(
        f"SELECT {selected} FROM {_quote(spec.name)}{where} "
        f"ORDER BY {_order_sql(spec)} LIMIT ?",
        (*params, batch_rows),
    )
    raw_rows = cursor.fetchmany(batch_rows)
    if len(raw_rows) > batch_rows:
        raise RuntimeError("shadow COPY source read exceeded its batch bound")
    result: list[tuple[tuple[Any, ...], tuple[Any, ...]]] = []
    for row in raw_rows:
        values = transform_sqlite_row(spec, postgres_columns, row)
        key = tuple(row[column] for column in spec.replication_key)
        result.append((key, values))
    return result


def _progress_row(conn: Any, run_id: str, table: str, *, lock: bool = False) -> Any:
    suffix = " FOR UPDATE" if lock else ""
    return conn.execute(
        "SELECT last_key_json,rows_copied,bytes_copied,rolling_hash,complete "
        "FROM silicon_shadow.copy_progress WHERE run_id=%s AND table_name=%s" + suffix,
        (run_id, table),
    ).fetchone()


def _decode_progress(row: Any | None) -> tuple[list[Any] | None, int, int, str, bool]:
    if row is None:
        return None, 0, 0, _INITIAL_ROLLING_HASH, False
    last_key = _row_value(row, "last_key_json", 0)
    if last_key is not None and not isinstance(last_key, list):
        raise ValueError("shadow COPY checkpoint key is invalid")
    rolling = _row_value(row, "rolling_hash", 3)
    rolling_hash = _INITIAL_ROLLING_HASH if rolling is None else str(rolling)
    if re.fullmatch(r"[0-9a-f]{64}", rolling_hash) is None:
        raise ValueError("shadow COPY rolling hash is invalid")
    rows_copied = _row_value(row, "rows_copied", 1)
    bytes_copied = _row_value(row, "bytes_copied", 2)
    complete = _row_value(row, "complete", 4)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (rows_copied, bytes_copied)
    ) or not isinstance(complete, bool):
        raise ValueError("shadow COPY checkpoint counters are invalid")
    return (
        last_key,
        rows_copied,
        bytes_copied,
        rolling_hash,
        complete,
    )


def _target_cursor(
    conn: Any,
    spec: TableSpec,
    columns: Sequence[PostgresColumn],
    *,
    business_schema: str,
    batch_rows: int,
) -> Any:
    cursor = conn.cursor(name=f"shadow_copy_{uuid.uuid4().hex}")
    cursor.itersize = batch_rows
    cursor.arraysize = batch_rows
    selected = sql.SQL(", ").join(
        (
            sql.SQL("{}::text AS {}").format(
                sql.Identifier(column.name),
                sql.Identifier(column.name),
            )
            if column.data_type == "jsonb"
            else sql.Identifier(column.name)
        )
        for column in columns
    )
    cursor.execute(
        sql.SQL("SELECT {} FROM {} ORDER BY {}").format(
            selected,
            _qualified(business_schema, spec.name),
            sql.SQL(", ").join(
                sql.Identifier(column) for column in spec.replication_key
            ),
        )
    )
    return cursor


def _validate_target_prefix(
    pg_conn: Any,
    sqlite_conn: sqlite3.Connection,
    spec: TableSpec,
    sqlite_columns: Sequence[str],
    postgres_columns: Sequence[PostgresColumn],
    checkpoint: tuple[list[Any] | None, int, int, str, bool],
    *,
    business_schema: str,
    batch_rows: int,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    last_key, expected_count, expected_bytes, expected_hash, _complete = checkpoint
    target_count = int(
        _row_value(
            pg_conn.execute(
                sql.SQL("SELECT COUNT(*)::bigint AS count FROM {}").format(
                    _qualified(business_schema, spec.name)
                )
            ).fetchone(),
            "count",
            0,
        )
    )
    if target_count != expected_count:
        raise ValueError("PostgreSQL COPY target does not match its checkpoint")
    _poll_cancel(cancel_requested)
    target_cursor = _target_cursor(
        pg_conn,
        spec,
        postgres_columns,
        business_schema=business_schema,
        batch_rows=batch_rows,
    )
    try:
        source_last: list[Any] | None = None
        count = 0
        bytes_copied = 0
        rolling = _INITIAL_ROLLING_HASH
        cursor_key: list[Any] | None = None
        while count < expected_count:
            _poll_cancel(cancel_requested)
            requested = min(batch_rows, expected_count - count)
            rows = _snapshot_batch(
                sqlite_conn,
                spec,
                sqlite_columns,
                postgres_columns,
                last_key=cursor_key,
                batch_rows=requested,
            )
            if not rows:
                raise ValueError("shadow COPY checkpoint extends past the snapshot")
            target_rows = target_cursor.fetchmany(requested)
            if len(target_rows) != len(rows) or len(target_rows) > batch_rows:
                raise ValueError(
                    "PostgreSQL COPY target prefix is not bounded or complete"
                )
            for (key, values), target in zip(rows, target_rows, strict=True):
                target_values = tuple(
                    (
                        Jsonb(canonical_json_value(raw_value))
                        if column.data_type == "jsonb" and isinstance(raw_value, str)
                        else raw_value
                    )
                    for position, column in enumerate(postgres_columns)
                    for raw_value in (_row_value(target, column.name, position),)
                )
                if _canonical_bytes(values, postgres_columns) != _canonical_bytes(
                    target_values, postgres_columns
                ):
                    raise ValueError(
                        "PostgreSQL COPY target row drifted from the snapshot"
                    )
                rolling, row_bytes = _advance_hash(
                    rolling,
                    values,
                    postgres_columns,
                )
                bytes_copied += row_bytes
                count += 1
                source_last = list(key)
            cursor_key = source_last
        if target_cursor.fetchmany(1):
            raise ValueError("PostgreSQL COPY target has rows beyond its checkpoint")
        if (
            count != expected_count
            or bytes_copied != expected_bytes
            or rolling != expected_hash
            or source_last != last_key
        ):
            raise ValueError("PostgreSQL COPY checkpoint proof is inconsistent")
    finally:
        target_cursor.close()


def _validate_copy_target(
    conn: Any,
    target: Any,
    snapshot: SnapshotInfo,
    manifest: Manifest,
    *,
    full_catalog: bool = False,
    sqlite_conn: sqlite3.Connection | None = None,
) -> Any:
    _lock_schema_and_control(conn)
    _validate_control_schema(conn)
    state_row = conn.execute(
        f"{_RUN_SELECT} WHERE run_id=%s FOR UPDATE", (snapshot.run_id,)
    ).fetchone()
    if state_row is None:
        raise ValueError("shadow COPY run does not exist")
    state = _state_from_row(state_row)
    _validated_progress(state.progress)
    version = _postgres_schema_version(conn, state.target_business_schema)
    if version != manifest.schema_pair.postgres_version:
        raise ValueError("PostgreSQL COPY target schema version changed")
    live_target = fingerprint_postgres(_target_url(target), conn)
    if (
        state.phase is not ShadowPhase.SQLITE_TO_POSTGRES
        or state.active_backend != "sqlite"
        or not state.capture_enabled
        or state.sqlite_guard_report_hash is None
        or state.postgres_guard_report_hash is None
        or state.source_identity_hash != snapshot.source_identity_hash
        or state.target_identity_hash != live_target.identity_hash
        or state.schema_pair != manifest.schema_pair
        or snapshot.schema_epoch != manifest.schema_pair.epoch
    ):
        raise ValueError("shadow COPY run is not a committed forward run")
    if full_catalog:
        if sqlite_conn is None:
            raise ValueError("full PostgreSQL catalog validation needs the snapshot")
        _validate_full_copy_target_catalog(
            conn,
            state=state,
            manifest=manifest,
            sqlite_conn=sqlite_conn,
        )
    progress = state.progress
    recorded_hash = progress.get("snapshot_sha256")
    recorded_seq = progress.get("baseline_seq")
    if (recorded_hash is None) != (recorded_seq is None):
        raise ValueError("shadow COPY snapshot binding is incomplete")
    if recorded_hash is not None and recorded_hash != snapshot.sha256:
        raise ValueError("shadow COPY run is bound to a different snapshot")
    if recorded_seq is not None and recorded_seq != snapshot.baseline_seq:
        raise ValueError("shadow COPY run has a different baseline sequence")
    return state


def _validate_full_copy_target_catalog(
    conn: Any,
    *,
    state: Any,
    manifest: Manifest,
    sqlite_conn: sqlite3.Connection,
) -> None:
    version = _postgres_schema_version(conn, state.target_business_schema)
    if version != manifest.schema_pair.postgres_version:
        raise ValueError("PostgreSQL COPY target schema version changed")
    definitions = _guard_definitions(manifest)
    validate_postgres_connection(
        conn,
        manifest,
        schema_version=version,
        expected_pair=manifest.schema_pair,
        business_schema=state.target_business_schema,
    )
    _validate_postgres_guard_set(
        conn,
        definitions,
        allow_missing=False,
        business_schema=state.target_business_schema,
    )
    for definition in definitions:
        _verify_postgres_guard(
            conn,
            definition,
            business_schema=state.target_business_schema,
        )
    validate_postgres_business_catalog(
        conn,
        business_schema=state.target_business_schema,
        manifest=manifest,
        guard_names=frozenset(item.name for item in definitions),
    )
    for spec in manifest.replicated:
        _copy_columns(
            _snapshot_columns(sqlite_conn, spec),
            _postgres_columns(conn, spec, state.target_business_schema),
            spec,
        )


def _lock_copy_catalog_tables(
    conn: Any,
    *,
    business_schema: str,
    manifest: Manifest,
) -> tuple[TableSpec, ...]:
    ordered_specs = tuple(sorted(manifest.replicated, key=lambda item: item.copy_rank))
    conn.execute(
        sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
            sql.SQL(", ").join(
                (
                    _qualified(business_schema, "silicon_schema_migrations"),
                    *(_qualified(business_schema, spec.name) for spec in ordered_specs),
                )
            )
        )
    )
    return ordered_specs


def _lock_identity_sequences(
    conn: Any,
    *,
    business_schema: str,
    ordered_specs: Sequence[TableSpec],
) -> tuple[tuple[str, str, str, int], ...]:
    ordinal_tables = tuple(
        spec.name
        for spec in ordered_specs
        if spec.name in POSTGRES_ROWID_ORDINAL_TABLES
    )
    rows = conn.execute(
        "SELECT t.relname AS table_name,sn.nspname AS sequence_schema,"
        "s.relname AS sequence_name,s.oid AS sequence_oid,q.seqcache "
        "FROM pg_class t "
        "JOIN pg_namespace tn ON tn.oid=t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attname='ordinal' "
        "JOIN pg_depend d ON d.refobjid=t.oid AND d.refobjsubid=a.attnum "
        "AND d.deptype='i' "
        "JOIN pg_class s ON s.oid=d.objid AND s.relkind='S' "
        "JOIN pg_namespace sn ON sn.oid=s.relnamespace "
        "JOIN pg_sequence q ON q.seqrelid=s.oid "
        "WHERE tn.nspname=%s AND t.relname=ANY(%s)",
        (business_schema, list(ordinal_tables)),
    ).fetchall()
    by_table = {
        str(_row_value(row, "table_name", 0)): (
            str(_row_value(row, "sequence_schema", 1)),
            str(_row_value(row, "sequence_name", 2)),
            int(_row_value(row, "sequence_oid", 3)),
            int(_row_value(row, "seqcache", 4)),
        )
        for row in rows
    }
    if set(by_table) != set(ordinal_tables) or any(
        schema != business_schema for schema, _name, _oid, _cache in by_table.values()
    ):
        raise ValueError("PostgreSQL identity sequence dependency drifted")
    locked: list[tuple[str, str, str, int]] = []
    for table in ordinal_tables:
        sequence_schema, sequence_name, sequence_oid, sequence_cache = by_table[table]
        # A same-value ALTER obtains ShareRowExclusiveLock until transaction
        # end. That conflicts with both direct ALTER SEQUENCE and RowExclusive
        # state writers such as setval()/nextval(). Preserve a drifted cache so
        # the subsequent exact catalog validation can reject it.
        conn.execute(
            sql.SQL("ALTER SEQUENCE {}.{} CACHE {}").format(
                sql.Identifier(sequence_schema),
                sql.Identifier(sequence_name),
                sql.Literal(sequence_cache),
            )
        )
        # setval() takes a transaction-held lock that conflicts with direct
        # ALTER SEQUENCE. It is non-transactional, so this single statement
        # writes back the exact catalog-bound current state without advancing.
        state = conn.execute(
            sql.SQL(
                "SELECT setval(%s::oid::regclass,last_value,is_called) AS last_value "
                "FROM {}.{}"
            ).format(sql.Identifier(sequence_schema), sql.Identifier(sequence_name)),
            (sequence_oid,),
        ).fetchone()
        if state is None:
            raise ValueError("PostgreSQL identity sequence state is missing")
        locked.append((table, sequence_schema, sequence_name, sequence_oid))
    return tuple(locked)


def _reseed_and_verify_identity_sequences(
    conn: Any,
    *,
    business_schema: str,
    sequences: Sequence[tuple[str, str, str, int]],
) -> None:
    if len(sequences) != len(POSTGRES_ROWID_ORDINAL_TABLES):
        raise ValueError("PostgreSQL final identity sequence set drifted")
    for table, sequence_schema, sequence_name, sequence_oid in sequences:
        maximum = conn.execute(
            sql.SQL("SELECT MAX(ordinal)::bigint AS maximum FROM {}.{}").format(
                sql.Identifier(business_schema), sql.Identifier(table)
            )
        ).fetchone()
        max_value = _row_value(maximum, "maximum", 0)
        expected_value = 1 if max_value is None else int(max_value)
        expected_called = max_value is not None
        # Bind the committed H0 to the exact table-derived sequence state. If
        # this final transaction fails PostgreSQL may restore its pre-finalize
        # state; no H0 is published, and a retry re-locks and converges again.
        conn.execute(
            "SELECT setval(%s::oid::regclass,%s,%s)",
            (sequence_oid, expected_value, expected_called),
        ).fetchone()
        actual = conn.execute(
            sql.SQL("SELECT last_value,is_called FROM {}.{}").format(
                sql.Identifier(sequence_schema), sql.Identifier(sequence_name)
            )
        ).fetchone()
        if actual is None or (
            int(_row_value(actual, "last_value", 0)),
            bool(_row_value(actual, "is_called", 1)),
        ) != (expected_value, expected_called):
            raise ValueError("PostgreSQL final identity sequence reseed drifted")


def _bind_snapshot(conn: Any, state: Any, snapshot: SnapshotInfo) -> None:
    if state.progress.get("snapshot_sha256") is not None:
        return
    existing_progress = conn.execute(
        "SELECT 1 FROM silicon_shadow.copy_progress WHERE run_id=%s LIMIT 1",
        (snapshot.run_id,),
    ).fetchone()
    if existing_progress is not None:
        raise ValueError("shadow COPY checkpoint lost its snapshot binding")
    encoded = _validated_progress(
        {
            "stage": "copy",
            "snapshot_sha256": snapshot.sha256,
            "baseline_seq": snapshot.baseline_seq,
        }
    )
    updated = conn.execute(
        "UPDATE silicon_shadow.runs SET progress=%s::jsonb,revision=revision+1,"
        "updated_at=clock_timestamp() WHERE run_id=%s AND revision=%s RETURNING run_id",
        (encoded, snapshot.run_id, state.revision),
    ).fetchone()
    if updated is None:
        raise ValueError("shadow COPY snapshot binding CAS failed")


def _copy_batch(
    conn: Any,
    spec: TableSpec,
    columns: Sequence[PostgresColumn],
    rows: Sequence[tuple[tuple[Any, ...], tuple[Any, ...]]],
    *,
    business_schema: str,
) -> None:
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        _qualified(business_schema, spec.name),
        sql.SQL(", ").join(sql.Identifier(column.name) for column in columns),
    )
    with conn.cursor().copy(statement) as copier:
        for _key, values in rows:
            copier.write_row(values)


def _reseed_ordinal(conn: Any, spec: TableSpec, *, business_schema: str) -> None:
    row = conn.execute(
        "SELECT seq.oid::bigint AS sequence_oid FROM pg_class tbl "
        "JOIN pg_namespace ns ON ns.oid=tbl.relnamespace "
        "JOIN pg_attribute att ON att.attrelid=tbl.oid AND att.attname=%s "
        "JOIN pg_depend dep ON dep.refclassid='pg_class'::regclass "
        "AND dep.refobjid=tbl.oid AND dep.refobjsubid=att.attnum "
        "AND dep.classid='pg_class'::regclass AND dep.deptype IN ('a','i') "
        "JOIN pg_class seq ON seq.oid=dep.objid AND seq.relkind='S' "
        "WHERE ns.nspname=%s AND tbl.relname=%s",
        ("ordinal", business_schema, spec.name),
    ).fetchall()
    if len(row) != 1:
        raise ValueError("PostgreSQL ordinal identity sequence dependency is invalid")
    sequence_oid = int(_row_value(row[0], "sequence_oid", 0))
    maximum = conn.execute(
        sql.SQL("SELECT MAX(ordinal)::bigint AS maximum FROM {}").format(
            _qualified(business_schema, spec.name)
        )
    ).fetchone()
    max_value = _row_value(maximum, "maximum", 0)
    if max_value is None:
        conn.execute("SELECT setval(%s::oid::regclass,1,false)", (sequence_oid,))
    else:
        conn.execute(
            "SELECT setval(%s::oid::regclass,%s,true)",
            (sequence_oid, int(max_value)),
        )


def _finish_table(
    target: Any,
    snapshot: SnapshotInfo,
    manifest: Manifest,
    spec: TableSpec,
    checkpoint: tuple[list[Any] | None, int, int, str, bool],
    *,
    sqlite_conn: sqlite3.Connection,
    sqlite_columns: Sequence[str],
    postgres_columns: Sequence[PostgresColumn],
    batch_rows: int,
    source: Any,
    snapshot_schema_contract: tuple[Any, ...],
    cancel_requested: Callable[[], bool] | None,
    statement_timeout_seconds: float,
) -> None:
    _last_key, _rows, _bytes, _rolling, complete = checkpoint
    if complete:
        return
    with target.write() as conn:
        _set_statement_timeout(conn, statement_timeout_seconds)
        state = _validate_copy_target(conn, target, snapshot, manifest)
        _validate_live_source(
            source,
            snapshot,
            manifest,
            expected_schema_contract=snapshot_schema_contract,
        )
        _bind_snapshot(conn, state, snapshot)
        # Hold off any accidental external writer from the prefix proof through
        # reseed and the completion CAS. The target is not the active backend.
        conn.execute(
            sql.SQL("LOCK TABLE {} IN SHARE MODE").format(
                _qualified(state.target_business_schema, spec.name)
            )
        )
        current = _decode_progress(
            _progress_row(conn, snapshot.run_id, spec.name, lock=True)
        )
        if current != checkpoint:
            raise ValueError("shadow COPY table checkpoint changed concurrently")
        _validate_target_prefix(
            conn,
            sqlite_conn,
            spec,
            sqlite_columns,
            postgres_columns,
            current,
            business_schema=state.target_business_schema,
            batch_rows=batch_rows,
            cancel_requested=cancel_requested,
        )
        if _snapshot_batch(
            sqlite_conn,
            spec,
            sqlite_columns,
            postgres_columns,
            last_key=current[0],
            batch_rows=1,
        ):
            raise ValueError("shadow COPY table completion is not at snapshot end")
        if spec.name in POSTGRES_ROWID_ORDINAL_TABLES:
            _reseed_ordinal(conn, spec, business_schema=state.target_business_schema)
        cursor = conn.execute(
            "INSERT INTO silicon_shadow.copy_progress "
            "(run_id,table_name,last_key_json,rows_copied,bytes_copied,rolling_hash,complete) "
            "VALUES (%s,%s,%s::jsonb,%s,%s,%s,true) "
            "ON CONFLICT (run_id,table_name) DO UPDATE SET complete=true,"
            "updated_at=clock_timestamp() WHERE silicon_shadow.copy_progress.complete=false "
            "AND silicon_shadow.copy_progress.rows_copied=%s "
            "AND silicon_shadow.copy_progress.bytes_copied=%s "
            "AND silicon_shadow.copy_progress.rolling_hash IS NOT DISTINCT FROM %s RETURNING table_name",
            (
                snapshot.run_id,
                spec.name,
                json.dumps(checkpoint[0]) if checkpoint[0] is not None else None,
                checkpoint[1],
                checkpoint[2],
                checkpoint[3],
                checkpoint[1],
                checkpoint[2],
                checkpoint[3],
            ),
        )
        if cursor.fetchone() is None:
            raise ValueError("shadow COPY table completion CAS failed")


def _finalize_copy(
    target: Any,
    source: Any,
    snapshot: SnapshotInfo,
    manifest: Manifest,
    reports: Sequence[TableCopyReport],
    *,
    sqlite_conn: sqlite3.Connection,
    snapshot_schema_contract: tuple[Any, ...],
    batch_rows: int,
    migration_statement_timeout_seconds: float,
    cancel_requested: Callable[[], bool] | None,
    statement_timeout_seconds: float,
) -> None:
    # The current compatibility target -- ``manifest.schema_pair``, asserted
    # against the migrator's reported version just below -- is already
    # schema-complete.  Calling the formal migrator still validates every
    # checksummed ledger entry; no stale v2->v6 shadow-only migration path is
    # used.  That "v2->v6" is a deliberate historical reference to the retired
    # shadow-only path, not a current version, so it does not track bumps.
    _poll_cancel(cancel_requested)
    migrated = PostgresMigrator(target).migrate(
        target_version=manifest.schema_pair.postgres_version,
        statement_timeout_seconds=migration_statement_timeout_seconds,
    )
    if migrated != manifest.schema_pair.postgres_version:
        raise ValueError("PostgreSQL formal migration ledger is incomplete")
    _poll_cancel(cancel_requested)
    with _final_write_with_live_fence(target) as (conn, retain_live_fence):
        _set_statement_timeout(conn, statement_timeout_seconds)
        state = _validate_copy_target(
            conn,
            target,
            snapshot,
            manifest,
        )
        ordered_specs = _lock_copy_catalog_tables(
            conn,
            business_schema=state.target_business_schema,
            manifest=manifest,
        )
        identity_sequences = _lock_identity_sequences(
            conn,
            business_schema=state.target_business_schema,
            ordered_specs=ordered_specs,
        )
        # The cooperative migration/control/run locks establish the run-bound
        # schema first. ACCESS EXCLUSIVE then closes the direct-DDL/write TOCTOU
        # window for every business relation before any exact catalog proof.
        _validate_full_copy_target_catalog(
            conn,
            state=state,
            manifest=manifest,
            sqlite_conn=sqlite_conn,
        )
        progress_rows = conn.execute(
            "SELECT table_name,complete FROM silicon_shadow.copy_progress "
            "WHERE run_id=%s ORDER BY table_name",
            (snapshot.run_id,),
        ).fetchall()
        if {
            str(_row_value(row, "table_name", 0))
            for row in progress_rows
            if bool(_row_value(row, "complete", 1))
        } != set(manifest.replicated_names):
            raise ValueError("shadow COPY cannot finalize before every table completes")
        invalid_fk = conn.execute(
            "SELECT 1 FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=t.relnamespace "
            "WHERE n.nspname=%s AND c.contype='f' AND NOT c.convalidated LIMIT 1",
            (state.target_business_schema,),
        ).fetchone()
        if invalid_fk is not None:
            raise ValueError(
                "PostgreSQL COPY target contains an unvalidated foreign key"
            )
        # Re-prove every completed prefix while all business-table writes are
        # locked out, then publish H0 in this same transaction.
        for spec in ordered_specs:
            _poll_cancel(cancel_requested)
            sqlite_columns = _snapshot_columns(sqlite_conn, spec)
            postgres_columns = _copy_columns(
                sqlite_columns,
                _postgres_columns(conn, spec, state.target_business_schema),
                spec,
            )
            table_checkpoint = _decode_progress(
                _progress_row(conn, snapshot.run_id, spec.name, lock=True)
            )
            if not table_checkpoint[4]:
                raise ValueError("shadow COPY final proof found an incomplete table")
            _validate_target_prefix(
                conn,
                sqlite_conn,
                spec,
                sqlite_columns,
                postgres_columns,
                table_checkpoint,
                business_schema=state.target_business_schema,
                batch_rows=batch_rows,
                cancel_requested=cancel_requested,
            )
            if _snapshot_batch(
                sqlite_conn,
                spec,
                sqlite_columns,
                postgres_columns,
                last_key=table_checkpoint[0],
                batch_rows=1,
            ):
                raise ValueError("shadow COPY final proof is not at snapshot end")
            _poll_cancel(cancel_requested)
            conn.execute(
                sql.SQL("ANALYZE {}").format(
                    _qualified(state.target_business_schema, spec.name)
                )
            )
        _reseed_and_verify_identity_sequences(
            conn,
            business_schema=state.target_business_schema,
            sequences=identity_sequences,
        )
        # PG migration/control/run/table locks are already held. Acquire the
        # SQLite lease only after all long proof/ANALYZE work, retain it across
        # H0 SQL *and the target.write() commit*, then release it. No PG pool,
        # advisory lock, catalog scan, or long proof waits behind this fence.
        _poll_cancel(cancel_requested)
        live_fence = _begin_live_source_fence(
            source,
            snapshot,
            manifest,
            expected_schema_contract=snapshot_schema_contract,
        )
        retain_live_fence(live_fence)
        _bind_snapshot(conn, state, snapshot)
        _poll_cancel(cancel_requested)
        checkpoint = conn.execute(
            "INSERT INTO silicon_shadow.checkpoints(run_id,direction,last_seq,revision) "
            "VALUES (%s,'sqlite_to_postgres',%s,0) "
            "ON CONFLICT (run_id,direction) DO NOTHING RETURNING last_seq",
            (snapshot.run_id, snapshot.baseline_seq),
        ).fetchone()
        if checkpoint is None:
            checkpoint = conn.execute(
                "SELECT last_seq FROM silicon_shadow.checkpoints "
                "WHERE run_id=%s AND direction='sqlite_to_postgres'",
                (snapshot.run_id,),
            ).fetchone()
        if (
            checkpoint is None
            or int(_row_value(checkpoint, "last_seq", 0)) != snapshot.baseline_seq
        ):
            raise ValueError("shadow forward checkpoint CAS failed")
        total_rows = sum(report.rows_copied for report in reports)
        total_bytes = sum(report.bytes_copied for report in reports)
        progress = _validated_progress(
            {
                "stage": "forward",
                "snapshot_sha256": snapshot.sha256,
                "baseline_seq": snapshot.baseline_seq,
                "applied_seq": snapshot.baseline_seq,
                "tables_complete": len(reports),
                "rows_copied": total_rows,
                "bytes_copied": total_bytes,
                "migration_version": manifest.schema_pair.postgres_version,
            },
        )
        conn.execute(
            "UPDATE silicon_shadow.runs SET progress=%s::jsonb,revision=revision+1,"
            "updated_at=clock_timestamp() WHERE run_id=%s AND progress IS DISTINCT FROM %s::jsonb "
            "RETURNING run_id",
            (progress, snapshot.run_id, progress),
        ).fetchone()
        # This is the last cooperative path/inode check before leaving the PG
        # write context commits H0. BEGIN IMMEDIATE remains held on the exact
        # opened file through that commit and is released only afterward.
        assert_live_sqlite_path(source, live_fence.binding)


def copy_snapshot(
    snapshot: SnapshotInfo,
    source: Any,
    target: Any,
    manifest: Manifest,
    batch_rows: int = 2_000,
    *,
    cancel_requested: Callable[[], bool] | None = None,
    migration_statement_timeout_seconds: float = 3_600,
    statement_timeout_seconds: float = 300,
) -> CopyReport:
    """Copy one immutable snapshot in manifest order using committed prefixes.

    Each batch and its prefix checkpoint commit in the same PostgreSQL
    transaction. A restart verifies every existing target row against the
    snapshot and checkpoint before continuing. Business tables have no run
    ownership, so this function never truncates or deletes them; unexplained
    target rows or mutations fail closed.
    """
    validate_manifest(manifest)
    if (
        isinstance(batch_rows, bool)
        or not isinstance(batch_rows, int)
        or not 1 <= batch_rows <= 100_000
    ):
        raise ValueError("shadow COPY batch_rows must be in 1..100000")
    _validate_snapshot_info(snapshot, manifest)
    if (
        isinstance(statement_timeout_seconds, bool)
        or not isinstance(statement_timeout_seconds, (int, float))
        or not math.isfinite(statement_timeout_seconds)
        or not 0 < statement_timeout_seconds <= 86_400
    ):
        raise ValueError("shadow COPY statement timeout is invalid")
    sqlite_conn = _open_snapshot(snapshot, cancel_requested)
    reports: list[TableCopyReport] = []
    try:
        with _cancellable_validation(sqlite_conn, cancel_requested):
            integrity = _integrity_check(sqlite_conn)
            if integrity is None or str(integrity[0]) != "ok":
                raise ValueError("shadow snapshot integrity changed")
            validate_sqlite_schema(
                sqlite_conn,
                manifest,
                expected_pair=manifest.schema_pair,
            )
        control = sqlite_conn.execute(
            "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
            "FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()
        if control is None or tuple(control) != (
            1,
            0,
            0,
            snapshot.run_id,
            snapshot.schema_epoch,
        ):
            raise ValueError("shadow snapshot run metadata changed")
        h0 = _change_log_high_water(sqlite_conn)
        if h0 != snapshot.baseline_seq:
            raise ValueError("shadow snapshot baseline sequence changed")
        snapshot_schema_contract = _sqlite_schema_contract(sqlite_conn, manifest)

        with target.write() as conn:
            _set_statement_timeout(conn, statement_timeout_seconds)
            state = _validate_copy_target(
                conn,
                target,
                snapshot,
                manifest,
                full_catalog=True,
                sqlite_conn=sqlite_conn,
            )
            _validate_live_source(
                source,
                snapshot,
                manifest,
                expected_schema_contract=snapshot_schema_contract,
            )
            _bind_snapshot(conn, state, snapshot)

        for spec in sorted(manifest.replicated, key=lambda item: item.copy_rank):
            sqlite_columns = _snapshot_columns(sqlite_conn, spec)
            with target.write() as conn:
                _set_statement_timeout(conn, statement_timeout_seconds)
                state = _validate_copy_target(conn, target, snapshot, manifest)
                postgres_columns = _copy_columns(
                    sqlite_columns,
                    _postgres_columns(conn, spec, state.target_business_schema),
                    spec,
                )
                checkpoint = _decode_progress(
                    _progress_row(conn, snapshot.run_id, spec.name, lock=True)
                )
                _validate_target_prefix(
                    conn,
                    sqlite_conn,
                    spec,
                    sqlite_columns,
                    postgres_columns,
                    checkpoint,
                    business_schema=state.target_business_schema,
                    batch_rows=batch_rows,
                    cancel_requested=cancel_requested,
                )
            while not checkpoint[4]:
                _poll_cancel(cancel_requested)
                rows = _snapshot_batch(
                    sqlite_conn,
                    spec,
                    sqlite_columns,
                    postgres_columns,
                    last_key=checkpoint[0],
                    batch_rows=batch_rows,
                )
                if not rows:
                    _finish_table(
                        target,
                        snapshot,
                        manifest,
                        spec,
                        checkpoint,
                        sqlite_conn=sqlite_conn,
                        sqlite_columns=sqlite_columns,
                        postgres_columns=postgres_columns,
                        batch_rows=batch_rows,
                        source=source,
                        snapshot_schema_contract=snapshot_schema_contract,
                        cancel_requested=cancel_requested,
                        statement_timeout_seconds=statement_timeout_seconds,
                    )
                    checkpoint = (*checkpoint[:4], True)
                    break
                next_hash = checkpoint[3]
                next_bytes = checkpoint[2]
                for _key, values in rows:
                    next_hash, row_bytes = _advance_hash(
                        next_hash,
                        values,
                        postgres_columns,
                    )
                    next_bytes += row_bytes
                next_rows = checkpoint[1] + len(rows)
                next_key = list(rows[-1][0])
                with target.write() as conn:
                    _set_statement_timeout(conn, statement_timeout_seconds)
                    state = _validate_copy_target(conn, target, snapshot, manifest)
                    _validate_live_source(
                        source,
                        snapshot,
                        manifest,
                        expected_schema_contract=snapshot_schema_contract,
                    )
                    _bind_snapshot(conn, state, snapshot)
                    current = _decode_progress(
                        _progress_row(conn, snapshot.run_id, spec.name, lock=True)
                    )
                    if current != checkpoint:
                        raise ValueError(
                            "shadow COPY table checkpoint changed concurrently"
                        )
                    _copy_batch(
                        conn,
                        spec,
                        postgres_columns,
                        rows,
                        business_schema=state.target_business_schema,
                    )
                    cursor = conn.execute(
                        "INSERT INTO silicon_shadow.copy_progress "
                        "(run_id,table_name,last_key_json,rows_copied,bytes_copied,rolling_hash,complete) "
                        "VALUES (%s,%s,%s::jsonb,%s,%s,%s,false) "
                        "ON CONFLICT (run_id,table_name) DO UPDATE SET "
                        "last_key_json=EXCLUDED.last_key_json,rows_copied=EXCLUDED.rows_copied,"
                        "bytes_copied=EXCLUDED.bytes_copied,rolling_hash=EXCLUDED.rolling_hash,"
                        "updated_at=clock_timestamp() WHERE silicon_shadow.copy_progress.complete=false "
                        "AND silicon_shadow.copy_progress.rows_copied=%s "
                        "AND silicon_shadow.copy_progress.bytes_copied=%s "
                        "AND silicon_shadow.copy_progress.rolling_hash IS NOT DISTINCT FROM %s "
                        "RETURNING table_name",
                        (
                            snapshot.run_id,
                            spec.name,
                            json.dumps(next_key, separators=(",", ":")),
                            next_rows,
                            next_bytes,
                            next_hash,
                            checkpoint[1],
                            checkpoint[2],
                            None
                            if checkpoint[3] == _INITIAL_ROLLING_HASH
                            and checkpoint[1] == 0
                            else checkpoint[3],
                        ),
                    )
                    if cursor.fetchone() is None:
                        raise ValueError("shadow COPY batch checkpoint CAS failed")
                checkpoint = (next_key, next_rows, next_bytes, next_hash, False)
            reports.append(
                TableCopyReport(
                    table_name=spec.name,
                    rows_copied=checkpoint[1],
                    bytes_copied=checkpoint[2],
                    rolling_hash=checkpoint[3],
                )
            )
        _finalize_copy(
            target,
            source,
            snapshot,
            manifest,
            reports,
            sqlite_conn=sqlite_conn,
            snapshot_schema_contract=snapshot_schema_contract,
            batch_rows=batch_rows,
            migration_statement_timeout_seconds=migration_statement_timeout_seconds,
            cancel_requested=cancel_requested,
            statement_timeout_seconds=statement_timeout_seconds,
        )
    finally:
        sqlite_conn.close()
    return CopyReport(
        run_id=snapshot.run_id,
        snapshot_sha256=snapshot.sha256,
        baseline_seq=snapshot.baseline_seq,
        tables=tuple(reports),
        rows_copied=sum(report.rows_copied for report in reports),
        bytes_copied=sum(report.bytes_copied for report in reports),
    )
