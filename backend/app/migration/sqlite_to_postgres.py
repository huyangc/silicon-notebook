"""Consistent, bounded SQLite to PostgreSQL snapshot migration.

The application never imports this module at runtime.  It owns the complete
one-shot migration boundary: read-only SQLite backup, schema upgrade on a
working copy, PostgreSQL schema preparation, ordered COPY, identity reseed,
full row checksums, and a credential-free receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
from psycopg import sql
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.core.database_url import database_identity, redact_database_url
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import (
    POSTGRES_BUSINESS_TABLES,
    POSTGRES_BYTEA_COLUMNS,
    POSTGRES_EMPTY_JSON_LIST_SENTINELS,
    POSTGRES_ROWID_ORDINAL_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
    SQLITE_RETIRED_TABLES,
)
from app.repositories.sqlite.bundle import SqlitePersistenceBundleFactory
from app.repositories.sqlite.migrations import SqliteMigrator
from app.services.vector_index import decode_vector, encode_vector


MIGRATION_ADVISORY_LOCK_NAME = "silicon-notebook:sqlite-to-postgres-import"
MIGRATION_LEDGER_TABLE = "silicon_schema_migrations"
DEFAULT_BATCH_ROWS = 500
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SNAPSHOT_NAME = re.compile(
    r"^sqlite-v(?P<version>[0-9]+)-(?P<digest>[0-9a-f]{16})\.snapshot\.db$"
)
_DIGEST_MODULUS = 1 << 256


class SqliteToPostgresMigrationError(RuntimeError):
    """A credential- and row-value-safe migration failure."""


@dataclass(frozen=True)
class SourcePreflight:
    path: str
    size_bytes: int
    schema_version: int
    journal_mode: str


@dataclass(frozen=True)
class TargetPreflight:
    redacted_url: str
    schema: str
    server_encoding: str
    prepared_version: int
    existing_business_tables: int


@dataclass(frozen=True)
class TableMigrationStat:
    table: str
    rows: int
    checksum: str


@dataclass(frozen=True)
class DataNormalizationStat:
    column: str
    nul_codepoints_escaped: int


@dataclass(frozen=True)
class MigrationResult:
    source: SourcePreflight
    target: TargetPreflight
    snapshot_path: str
    snapshot_sha256: str
    upgraded_from: int
    upgraded_to: int
    applied_sqlite_migrations: tuple[int, ...]
    total_rows: int
    tables: tuple[TableMigrationStat, ...]
    normalizations: tuple[DataNormalizationStat, ...]
    receipt_path: str


@dataclass(frozen=True)
class MigrationVerification:
    source_path: str
    snapshot_path: str
    target_redacted_url: str
    target_schema: str
    total_rows: int
    tables: tuple[TableMigrationStat, ...]


@dataclass(frozen=True)
class _PostgresColumn:
    name: str
    data_type: str
    udt_name: str
    nullable: bool
    identity: bool


@dataclass
class _CommutativeRowDigest:
    count: int = 0
    xor: int = 0
    total: int = 0

    def add(self, columns: Sequence[_PostgresColumn], values: Sequence[Any]) -> None:
        if len(columns) != len(values):
            raise AssertionError("checksum column/value length mismatch")
        row = hashlib.sha256()
        for column, value in zip(columns, values, strict=True):
            encoded = _canonical_value(column, value)
            row.update(len(encoded).to_bytes(8, byteorder="big"))
            row.update(encoded)
        number = int.from_bytes(row.digest(), byteorder="big")
        self.count += 1
        self.xor ^= number
        self.total = (self.total + number) % _DIGEST_MODULUS

    def hexdigest(self) -> str:
        final = hashlib.sha256()
        final.update(self.count.to_bytes(16, byteorder="big"))
        final.update(self.xor.to_bytes(32, byteorder="big"))
        final.update(self.total.to_bytes(32, byteorder="big"))
        return final.hexdigest()


@dataclass
class _TransformAudit:
    nul_escapes: dict[str, int]

    def __init__(self) -> None:
        self.nul_escapes = {}

    def record_nul(self, qualified: str, count: int) -> None:
        if count:
            self.nul_escapes[qualified] = self.nul_escapes.get(qualified, 0) + count


def _migration_lock_key() -> int:
    digest = hashlib.sha256(MIGRATION_ADVISORY_LOCK_NAME.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sqlite_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _validate_source_path(path: Path) -> Path:
    raw = Path(path)
    if raw.is_symlink():
        raise SqliteToPostgresMigrationError("SQLite source must not be a symlink")
    resolved = raw.resolve(strict=True)
    if not resolved.is_file():
        raise SqliteToPostgresMigrationError("SQLite source must be a regular file")
    return resolved


def inspect_source(path: Path) -> SourcePreflight:
    source = _validate_source_path(path)
    try:
        with _sqlite_read_only(source) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).lower()
            marker = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
    except sqlite3.Error as exc:
        raise SqliteToPostgresMigrationError(
            "SQLite source could not be read safely"
        ) from exc
    if marker is None:
        raise SqliteToPostgresMigrationError(
            "SQLite source is not a silicon-notebook database"
        )
    if not 1 <= version <= POSTGRES_SCHEMA_MANIFEST.sqlite_version:
        raise SqliteToPostgresMigrationError(
            "SQLite source schema is unsupported by this importer: "
            f"v{version}, expected 1..{POSTGRES_SCHEMA_MANIFEST.sqlite_version}"
        )
    return SourcePreflight(
        path=str(source),
        size_bytes=source.stat().st_size,
        schema_version=version,
        journal_mode=journal_mode,
    )


def _postgres_database(target_url: str, root_dir: Path) -> PostgresDatabase:
    if database_identity(target_url).scheme != "postgresql":
        raise SqliteToPostgresMigrationError(
            "migration target must be a PostgreSQL database URL"
        )
    settings = Settings(
        database_url=target_url,
        postgres_pool_min_size=1,
        postgres_pool_max_size=1,
        postgres_pool_acquire_timeout_seconds=30,
        postgres_statement_timeout_seconds=86_400,
        postgres_lock_timeout_seconds=30,
    )
    return PostgresDatabase(settings, root_dir)


def _target_tables(connection) -> set[str]:
    return {
        str(row["table_name"])
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
        ).fetchall()
    }


def _assert_target_empty(connection, tables: Iterable[str]) -> None:
    for table in sorted(tables):
        exists = connection.execute(
            sql.SQL("SELECT EXISTS(SELECT 1 FROM {} LIMIT 1) AS present").format(
                sql.Identifier(table)
            )
        ).fetchone()["present"]
        if bool(exists):
            raise SqliteToPostgresMigrationError(
                f"PostgreSQL target is not empty: table {table} contains rows"
            )


def inspect_target(database: PostgresDatabase) -> TargetPreflight:
    try:
        with database.connect() as connection:
            identity = connection.execute(
                "SELECT current_schema() AS schema, "
                "current_setting('server_encoding') AS encoding"
            ).fetchone()
            schema = str(identity["schema"] or "")
            encoding = str(identity["encoding"] or "")
            if not schema:
                raise SqliteToPostgresMigrationError(
                    "PostgreSQL target has no current schema"
                )
            if encoding != "UTF8":
                raise SqliteToPostgresMigrationError(
                    "PostgreSQL target server_encoding must be UTF8"
                )
            tables = _target_tables(connection)
            allowed = set(POSTGRES_BUSINESS_TABLES) | {MIGRATION_LEDGER_TABLE}
            unknown = sorted(tables - allowed)
            if unknown:
                raise SqliteToPostgresMigrationError(
                    "PostgreSQL target schema contains unrelated tables: "
                    + ", ".join(unknown)
                )
            business = tables & set(POSTGRES_BUSINESS_TABLES)
            _assert_target_empty(connection, business)
        prepared_version = PostgresMigrator(database).current_version()
    except SqliteToPostgresMigrationError:
        raise
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL target preflight failed"
        ) from exc
    return TargetPreflight(
        redacted_url=redact_database_url(database.settings.database_url),
        schema=schema,
        server_encoding=encoding,
        prepared_version=prepared_version,
        existing_business_tables=len(business),
    )


def preflight(
    *, source_path: Path, target_url: str, root_dir: Path
) -> tuple[SourcePreflight, TargetPreflight]:
    source = inspect_source(source_path)
    database = _postgres_database(target_url, root_dir)
    try:
        target = inspect_target(database)
    finally:
        database.close()
    return source, target


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256_file(path: Path, *, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def create_consistent_snapshot(
    *, source_path: Path, work_dir: Path, progress: Callable[[str], None]
) -> tuple[Path, str]:
    source = _validate_source_path(source_path)
    destination_dir = Path(work_dir).resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(destination_dir, 0o700)
    temporary = destination_dir / f".sqlite-snapshot-{uuid.uuid4().hex}.tmp"
    try:
        with _sqlite_read_only(source) as source_connection:
            with sqlite3.connect(temporary) as target_connection:
                progress("正在通过 SQLite backup API 创建一致性快照…")
                source_connection.backup(
                    target_connection,
                    pages=4096,
                    sleep=0.01,
                )
                target_connection.commit()
                target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target_connection.execute("PRAGMA journal_mode=DELETE")
                target_connection.commit()
        os.chmod(temporary, 0o600)
        with sqlite3.connect(temporary) as check:
            result = str(check.execute("PRAGMA quick_check").fetchone()[0])
            if result.lower() != "ok":
                raise SqliteToPostgresMigrationError(
                    "SQLite snapshot quick_check failed"
                )
            version = int(check.execute("PRAGMA user_version").fetchone()[0])
        _fsync_file(temporary)
        digest = _sha256_file(temporary)
        final = destination_dir / f"sqlite-v{version}-{digest[:16]}.snapshot.db"
        if final.exists():
            if _sha256_file(final) != digest:
                raise SqliteToPostgresMigrationError(
                    "snapshot destination collision"
                )
            temporary.unlink()
        else:
            os.replace(temporary, final)
            _fsync_directory(destination_dir)
        temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)
        return final, digest
    except SqliteToPostgresMigrationError:
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-wal").unlink(missing_ok=True)
        temporary.with_name(temporary.name + "-shm").unlink(missing_ok=True)
        raise SqliteToPostgresMigrationError(
            "could not create a consistent SQLite snapshot"
        ) from exc


def validate_existing_snapshot(
    *, snapshot_path: Path, work_dir: Path
) -> tuple[Path, str]:
    snapshot = _validate_source_path(snapshot_path)
    directory = Path(work_dir).resolve()
    if snapshot.parent != directory:
        raise SqliteToPostgresMigrationError(
            "existing snapshot must be inside the configured work directory"
        )
    match = _SNAPSHOT_NAME.fullmatch(snapshot.name)
    if match is None:
        raise SqliteToPostgresMigrationError(
            "existing snapshot name is not importer-owned"
        )
    for suffix in ("-wal", "-shm"):
        if snapshot.with_name(snapshot.name + suffix).exists():
            raise SqliteToPostgresMigrationError(
                "existing snapshot has an ambiguous SQLite sidecar"
            )
    try:
        with _sqlite_read_only(snapshot) as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        raise SqliteToPostgresMigrationError(
            "existing snapshot could not be validated"
        ) from exc
    if result.lower() != "ok" or version != int(match.group("version")):
        raise SqliteToPostgresMigrationError(
            "existing snapshot schema/integrity validation failed"
        )
    digest = _sha256_file(snapshot)
    if not digest.startswith(match.group("digest")):
        raise SqliteToPostgresMigrationError(
            "existing snapshot hash does not match its sealed name"
        )
    return snapshot, digest


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.resolve()}"


def prepare_upgraded_snapshot(
    *, snapshot_path: Path, work_dir: Path, root_dir: Path
) -> tuple[Path, tuple[int, ...]]:
    with _sqlite_read_only(snapshot_path) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if current == POSTGRES_SCHEMA_MANIFEST.sqlite_version:
        return snapshot_path, ()

    working = Path(work_dir).resolve() / f".sqlite-upgrade-{uuid.uuid4().hex}.db"
    shutil.copy2(snapshot_path, working)
    os.chmod(working, 0o600)
    settings = Settings(database_url=_sqlite_url(working))
    bundle = SqlitePersistenceBundleFactory().create(
        settings=settings,
        root_dir=root_dir,
        seams=SimpleNamespace(
            new_id=lambda prefix: f"{prefix}-{uuid.uuid4().hex}",
            now=lambda: datetime.now(timezone.utc).isoformat(),
            copy_chunk_size=lambda: 500,
            remap_json_ids=lambda value, _maps: value,
            in_chunk_size=lambda: 500,
        ),
    )
    database = bundle.database
    try:
        applied = tuple(SqliteMigrator(database, settings).migrate())
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "SQLite snapshot schema upgrade failed"
        ) from exc
    finally:
        database.close()

    try:
        with sqlite3.connect(working) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("PRAGMA journal_mode=DELETE")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if version != POSTGRES_SCHEMA_MANIFEST.sqlite_version:
            raise SqliteToPostgresMigrationError(
                "upgraded SQLite snapshot has the wrong schema version"
            )
        if integrity.lower() != "ok":
            raise SqliteToPostgresMigrationError(
                "upgraded SQLite snapshot quick_check failed"
            )
        return working, applied
    except SqliteToPostgresMigrationError:
        raise
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "upgraded SQLite snapshot validation failed"
        ) from exc


def _sqlite_business_tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table'"
    ).fetchall()
    virtual_roots = {
        str(row["name"])
        for row in rows
        if str(row["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    rebuilt = {
        str(row["name"])
        for row in rows
        if row["name"] in virtual_roots
        or any(str(row["name"]).startswith(f"{root}_") for root in virtual_roots)
    }
    ordinary = {
        str(row["name"])
        for row in rows
        if not str(row["name"]).startswith("sqlite_")
        and str(row["name"]) not in rebuilt
    }
    retired = ordinary & set(SQLITE_RETIRED_TABLES)
    for table in sorted(retired):
        count = int(
            connection.execute(
                f"SELECT COUNT(*) FROM {_quote_sqlite_identifier(table)}"
            ).fetchone()[0]
        )
        if count:
            raise SqliteToPostgresMigrationError(
                f"retired SQLite table {table} contains {count} rows; "
                "refusing to discard legacy data"
            )
    return ordinary - retired


def _postgres_columns(connection) -> dict[str, tuple[_PostgresColumn, ...]]:
    rows = connection.execute(
        "SELECT table_name,column_name,data_type,udt_name,is_nullable,is_identity "
        "FROM information_schema.columns WHERE table_schema=current_schema() "
        "ORDER BY table_name,ordinal_position"
    ).fetchall()
    result: dict[str, list[_PostgresColumn]] = {}
    for row in rows:
        table = str(row["table_name"])
        if table == MIGRATION_LEDGER_TABLE:
            continue
        result.setdefault(table, []).append(
            _PostgresColumn(
                name=str(row["column_name"]),
                data_type=str(row["data_type"]),
                udt_name=str(row["udt_name"]),
                nullable=str(row["is_nullable"]) == "YES",
                identity=str(row["is_identity"]) == "YES",
            )
        )
    return {table: tuple(columns) for table, columns in result.items()}


def _copy_order(connection, tables: set[str]) -> tuple[str, ...]:
    dependencies = {table: set() for table in tables}
    rows = connection.execute(
        "SELECT child.relname AS child,parent.relname AS parent "
        "FROM pg_constraint c "
        "JOIN pg_class child ON child.oid=c.conrelid "
        "JOIN pg_class parent ON parent.oid=c.confrelid "
        "JOIN pg_namespace n ON n.oid=child.relnamespace "
        "WHERE c.contype='f' AND n.nspname=current_schema()"
    ).fetchall()
    for row in rows:
        child = str(row["child"])
        parent = str(row["parent"])
        if child in tables and parent in tables and child != parent:
            dependencies[child].add(parent)
    remaining = set(tables)
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            table for table in remaining if not (dependencies[table] & remaining)
        )
        if not ready:
            raise SqliteToPostgresMigrationError(
                "PostgreSQL foreign-key graph contains an unsupported cycle"
            )
        ordered.extend(ready)
        remaining.difference_update(ready)
    return tuple(ordered)


def _parse_timestamp(value: Any, *, qualified: str) -> datetime | None:
    if value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise SqliteToPostgresMigrationError(
                f"invalid timestamp in {qualified}"
            ) from exc
    else:
        raise SqliteToPostgresMigrationError(
            f"invalid timestamp type in {qualified}"
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _escape_postgres_nul(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        count = value.count("\x00")
        return value.replace("\x00", "\\u0000"), count
    if isinstance(value, list):
        output = []
        count = 0
        for item in value:
            normalized, current = _escape_postgres_nul(item)
            output.append(normalized)
            count += current
        return output, count
    if isinstance(value, dict):
        output: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            normalized_key, key_count = _escape_postgres_nul(key)
            normalized_item, item_count = _escape_postgres_nul(item)
            if normalized_key in output:
                raise SqliteToPostgresMigrationError(
                    "JSON key collision after PostgreSQL NUL escaping"
                )
            output[normalized_key] = normalized_item
            count += key_count + item_count
        return output, count
    return value, 0


def _transform_sqlite_value(
    *,
    table: str,
    column: _PostgresColumn,
    value: Any,
    audit: _TransformAudit | None = None,
) -> Any:
    qualified = f"{table}.{column.name}"
    if value is None:
        return None
    if column.udt_name == "jsonb":
        if value == "" and qualified in POSTGRES_EMPTY_JSON_LIST_SENTINELS:
            return []
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode("utf-8")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SqliteToPostgresMigrationError(
                    f"invalid JSON in {qualified}"
                ) from exc
        value, nul_count = _escape_postgres_nul(value)
        if audit is not None:
            audit.record_nul(qualified, nul_count)
        return value
    if column.udt_name == "timestamptz":
        return _parse_timestamp(value, qualified=qualified)
    if column.udt_name == "bytea":
        if qualified not in POSTGRES_BYTEA_COLUMNS:
            raise SqliteToPostgresMigrationError(
                f"unclassified binary value in {qualified}"
            )
        try:
            vector = decode_vector(value)
        except Exception as exc:
            raise SqliteToPostgresMigrationError(
                f"invalid legacy vector in {qualified}"
            ) from exc
        if (
            vector is None
            or vector.ndim != 1
            or vector.size == 0
            or not np.isfinite(vector).all()
        ):
            raise SqliteToPostgresMigrationError(
                f"invalid legacy vector in {qualified}"
            )
        encoded = encode_vector(vector)
        if len(encoded) != int(vector.size) * np.dtype(np.float32).itemsize:
            raise SqliteToPostgresMigrationError(
                f"invalid encoded vector in {qualified}"
            )
        return encoded
    if column.udt_name == "bool":
        if value not in (0, 1, False, True):
            raise SqliteToPostgresMigrationError(
                f"invalid boolean value in {qualified}"
            )
        return bool(value)
    if isinstance(value, str) and "\x00" in value:
        value, nul_count = _escape_postgres_nul(value)
        if audit is not None:
            audit.record_nul(qualified, nul_count)
    return value


def _canonical_value(column: _PostgresColumn, value: Any) -> bytes:
    if value is None:
        return b"N"
    if column.udt_name == "jsonb":
        return b"J" + json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    if column.udt_name == "timestamptz":
        parsed = _parse_timestamp(value, qualified=column.name)
        assert parsed is not None
        return b"T" + parsed.isoformat(timespec="microseconds").encode("ascii")
    if column.udt_name == "bytea":
        return b"B" + bytes(value)
    if isinstance(value, bool):
        return b"O1" if value else b"O0"
    if isinstance(value, int):
        return b"I" + str(value).encode("ascii")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SqliteToPostgresMigrationError(
                f"non-finite float in {column.name}"
            )
        return b"F" + struct.pack("!d", value)
    if isinstance(value, memoryview):
        return b"B" + value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return b"B" + bytes(value)
    return b"S" + str(value).encode("utf-8")


def _sqlite_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_sqlite_identifier(table)})"
        ).fetchall()
    )


def _validate_schema_pair(
    sqlite_connection: sqlite3.Connection,
    postgres_connection,
) -> tuple[dict[str, tuple[_PostgresColumn, ...]], tuple[str, ...]]:
    source_tables = _sqlite_business_tables(sqlite_connection)
    expected = set(POSTGRES_BUSINESS_TABLES)
    if source_tables != expected:
        missing = sorted(expected - source_tables)
        extra = sorted(source_tables - expected)
        raise SqliteToPostgresMigrationError(
            "upgraded SQLite table manifest mismatch "
            f"(missing={missing}, extra={extra})"
        )
    target_tables = _target_tables(postgres_connection) - {MIGRATION_LEDGER_TABLE}
    if target_tables != expected:
        missing = sorted(expected - target_tables)
        extra = sorted(target_tables - expected)
        raise SqliteToPostgresMigrationError(
            "PostgreSQL table manifest mismatch "
            f"(missing={missing}, extra={extra})"
        )
    columns = _postgres_columns(postgres_connection)
    ordinal_tables = set(POSTGRES_ROWID_ORDINAL_TABLES)
    for table in sorted(expected):
        source_columns = _sqlite_columns(sqlite_connection, table)
        target_columns = tuple(column.name for column in columns[table])
        expected_columns = set(source_columns) | (
            {"ordinal"} if table in ordinal_tables else set()
        )
        if set(target_columns) != expected_columns or len(target_columns) != len(
            expected_columns
        ):
            raise SqliteToPostgresMigrationError(
                f"column manifest mismatch for table {table}"
            )
    return columns, _copy_order(postgres_connection, expected)


def _adapt_for_copy(column: _PostgresColumn, value: Any) -> Any:
    return Jsonb(value) if column.udt_name == "jsonb" and value is not None else value


def _safe_database_error_fact(exc: BaseException) -> str:
    sqlstate = getattr(exc, "sqlstate", None)
    diagnostic = getattr(exc, "diag", None)
    constraint = getattr(diagnostic, "constraint_name", None) if diagnostic else None
    facts = [type(exc).__name__]
    if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
        facts.append(f"sqlstate={sqlstate}")
    if isinstance(constraint, str) and re.fullmatch(r"[a-z0-9_]+", constraint):
        facts.append(f"constraint={constraint}")
    return " ".join(facts)


def _copy_table(
    *,
    sqlite_connection: sqlite3.Connection,
    postgres_connection,
    table: str,
    columns: tuple[_PostgresColumn, ...],
    batch_rows: int,
    audit: _TransformAudit | None = None,
) -> _CommutativeRowDigest:
    source_columns = _sqlite_columns(sqlite_connection, table)
    has_ordinal = table in set(POSTGRES_ROWID_ORDINAL_TABLES)
    select_columns = ",".join(_quote_sqlite_identifier(name) for name in source_columns)
    if has_ordinal:
        select_columns = f"rowid AS __sn_ordinal,{select_columns}"
    cursor = sqlite_connection.execute(
        f"SELECT {select_columns} FROM {_quote_sqlite_identifier(table)}"
    )
    digest = _CommutativeRowDigest()
    copy_statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table),
        sql.SQL(",").join(sql.Identifier(column.name) for column in columns),
    )
    try:
        with postgres_connection.cursor().copy(copy_statement) as copy:
            copy.set_types([column.udt_name for column in columns])
            while rows := cursor.fetchmany(batch_rows):
                for row in rows:
                    raw_values = tuple(
                        int(row["__sn_ordinal"])
                        if column.name == "ordinal"
                        else row[column.name]
                        for column in columns
                    )
                    logical = tuple(
                        _transform_sqlite_value(
                            table=table,
                            column=column,
                            value=value,
                            audit=audit,
                        )
                        for column, value in zip(columns, raw_values, strict=True)
                    )
                    digest.add(columns, logical)
                    copy.write_row(
                        tuple(
                            _adapt_for_copy(column, value)
                            for column, value in zip(columns, logical, strict=True)
                        )
                    )
    except SqliteToPostgresMigrationError:
        raise
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            f"COPY failed for table {table} after {digest.count} rows "
            f"({_safe_database_error_fact(exc)})"
        ) from exc
    return digest


def _target_digest(
    connection,
    *,
    table: str,
    columns: tuple[_PostgresColumn, ...],
    batch_rows: int,
) -> _CommutativeRowDigest:
    statement = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(",").join(sql.Identifier(column.name) for column in columns),
        sql.Identifier(table),
    )
    digest = _CommutativeRowDigest()
    cursor_name = f"sn_verify_{uuid.uuid4().hex}"
    with connection.cursor(name=cursor_name) as cursor:
        cursor.execute(statement)
        while rows := cursor.fetchmany(batch_rows):
            for row in rows:
                values = tuple(row[column.name] for column in columns)
                digest.add(columns, values)
    return digest


def _reseed_ordinal(connection, *, schema: str, table: str) -> None:
    sequence = connection.execute(
        "SELECT pg_get_serial_sequence(%s,'ordinal') AS name",
        (f'{schema}.{table}',),
    ).fetchone()["name"]
    if not sequence:
        raise SqliteToPostgresMigrationError(
            f"ordinal sequence is missing for table {table}"
        )
    maximum = connection.execute(
        sql.SQL("SELECT MAX(ordinal) AS value FROM {}").format(sql.Identifier(table))
    ).fetchone()["value"]
    connection.execute(
        "SELECT setval(%s::regclass,%s,%s)",
        (sequence, int(maximum or 1), maximum is not None),
    )


def _drop_rebuildable_indexes(connection) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT ci.relname AS index_name,pg_get_indexdef(i.indexrelid) AS definition "
        "FROM pg_index i "
        "JOIN pg_class ci ON ci.oid=i.indexrelid "
        "JOIN pg_class ct ON ct.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=ct.relnamespace "
        "WHERE n.nspname=current_schema() AND NOT i.indisprimary AND NOT i.indisunique "
        "ORDER BY ci.relname"
    ).fetchall()
    schema = str(
        connection.execute("SELECT current_schema() AS name").fetchone()["name"]
    )
    for row in rows:
        connection.execute(
            sql.SQL("DROP INDEX {}").format(
                sql.Identifier(schema, str(row["index_name"]))
            )
        )
    return tuple(str(row["definition"]) for row in rows)


def _lock_business_tables(connection, order: Sequence[str]) -> None:
    connection.execute(
        sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(
            sql.SQL(",").join(sql.Identifier(table) for table in order)
        )
    )


def copy_snapshot_to_postgres(
    *,
    snapshot_path: Path,
    database: PostgresDatabase,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    progress: Callable[[str], None],
) -> tuple[tuple[TableMigrationStat, ...], tuple[DataNormalizationStat, ...]]:
    if batch_rows <= 0:
        raise SqliteToPostgresMigrationError("batch_rows must be positive")
    try:
        version = PostgresMigrator(database).migrate(
            target_version=POSTGRES_SCHEMA_MANIFEST.postgres_version,
            statement_timeout_seconds=86_400,
        )
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL schema migration failed"
        ) from exc
    if version != POSTGRES_SCHEMA_MANIFEST.postgres_version:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL schema migration stopped at an unexpected version"
        )

    try:
        with _sqlite_read_only(snapshot_path) as source:
            source_version = int(source.execute("PRAGMA user_version").fetchone()[0])
            if source_version != POSTGRES_SCHEMA_MANIFEST.sqlite_version:
                raise SqliteToPostgresMigrationError(
                    "COPY source must be upgraded to the paired SQLite schema"
                )
            with database.write() as target:
                target.execute("SELECT pg_advisory_xact_lock(%s)", (_migration_lock_key(),))
                target.execute("SELECT set_config('statement_timeout','0',true)")
                target.execute("SELECT set_config('TimeZone','UTC',true)")
                columns, order = _validate_schema_pair(source, target)
                _lock_business_tables(target, order)
                _assert_target_empty(target, order)
                index_definitions = _drop_rebuildable_indexes(target)

                source_digests: dict[str, _CommutativeRowDigest] = {}
                audit = _TransformAudit()
                for position, table in enumerate(order, start=1):
                    progress(f"COPY {position}/{len(order)} {table}")
                    source_digests[table] = _copy_table(
                        sqlite_connection=source,
                        postgres_connection=target,
                        table=table,
                        columns=columns[table],
                        batch_rows=batch_rows,
                        audit=audit,
                    )

                schema = str(
                    target.execute("SELECT current_schema() AS name").fetchone()["name"]
                )
                for table in POSTGRES_ROWID_ORDINAL_TABLES:
                    _reseed_ordinal(target, schema=schema, table=table)

                stats: list[TableMigrationStat] = []
                for position, table in enumerate(order, start=1):
                    progress(f"VERIFY {position}/{len(order)} {table}")
                    actual = _target_digest(
                        target,
                        table=table,
                        columns=columns[table],
                        batch_rows=batch_rows,
                    )
                    expected = source_digests[table]
                    if (
                        actual.count != expected.count
                        or actual.hexdigest() != expected.hexdigest()
                    ):
                        raise SqliteToPostgresMigrationError(
                            f"row checksum mismatch for table {table}"
                        )
                    stats.append(
                        TableMigrationStat(
                            table=table,
                            rows=actual.count,
                            checksum=actual.hexdigest(),
                        )
                    )

                for position, definition in enumerate(index_definitions, start=1):
                    progress(
                        f"INDEX {position}/{len(index_definitions)} rebuilding"
                    )
                    target.execute(definition, prepare=False)
                progress(f"ANALYZE {len(order)} tables")
                target.execute(
                    sql.SQL("ANALYZE {}").format(
                        sql.SQL(",").join(sql.Identifier(table) for table in order)
                    )
                )
    except SqliteToPostgresMigrationError:
        raise
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL import transaction failed"
        ) from exc
    normalizations = tuple(
        DataNormalizationStat(column=column, nul_codepoints_escaped=count)
        for column, count in sorted(audit.nul_escapes.items())
    )
    return tuple(stats), normalizations


def _write_receipt(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path


def _load_migration_receipt(path: Path) -> tuple[Path, Mapping[str, Any]]:
    raw = Path(path)
    if raw.is_symlink():
        raise SqliteToPostgresMigrationError(
            "migration receipt must not be a symlink"
        )
    try:
        resolved = raw.resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > 4 * 1024 * 1024:
            raise SqliteToPostgresMigrationError("migration receipt is invalid")
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except SqliteToPostgresMigrationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SqliteToPostgresMigrationError(
            "migration receipt could not be read safely"
        ) from exc
    if not isinstance(payload, Mapping) or payload.get("format") != (
        "silicon-notebook-sqlite-to-postgres-v1"
    ):
        raise SqliteToPostgresMigrationError("migration receipt format is invalid")
    return resolved, payload


def _receipt_text(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SqliteToPostgresMigrationError("migration receipt is incomplete")
    return value


def _receipt_integer(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SqliteToPostgresMigrationError("migration receipt is incomplete")
    return value


def _receipt_mapping(mapping: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise SqliteToPostgresMigrationError("migration receipt is incomplete")
    return value


def _receipt_table_stats(payload: Mapping[str, Any]) -> tuple[TableMigrationStat, ...]:
    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, list):
        raise SqliteToPostgresMigrationError("migration receipt is incomplete")
    stats: list[TableMigrationStat] = []
    seen: set[str] = set()
    for raw in raw_tables:
        if not isinstance(raw, Mapping):
            raise SqliteToPostgresMigrationError("migration receipt is incomplete")
        table = _receipt_text(raw, "table")
        checksum = _receipt_text(raw, "checksum")
        rows = _receipt_integer(raw, "rows")
        if (
            table in seen
            or table not in set(POSTGRES_BUSINESS_TABLES)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise SqliteToPostgresMigrationError(
                "migration receipt table manifest is invalid"
            )
        seen.add(table)
        stats.append(TableMigrationStat(table=table, rows=rows, checksum=checksum))
    if seen != set(POSTGRES_BUSINESS_TABLES):
        raise SqliteToPostgresMigrationError(
            "migration receipt table manifest is incomplete"
        )
    return tuple(stats)


def verify_migration_receipt(
    *,
    source_path: Path,
    target_url: str,
    receipt_path: Path,
    work_dir: Path,
    root_dir: Path,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    progress: Callable[[str], None] = print,
) -> MigrationVerification:
    """Revalidate a completed import before atomically activating PostgreSQL.

    This deliberately repeats the expensive source snapshot and target checksum
    passes.  A receipt proves what was imported, not that the stopped SQLite
    source and PostgreSQL target are still at that exact state at cutover time.
    """
    if batch_rows <= 0:
        raise SqliteToPostgresMigrationError("batch_rows must be positive")
    _, payload = _load_migration_receipt(receipt_path)
    source_receipt = _receipt_mapping(payload, "source")
    snapshot_receipt = _receipt_mapping(payload, "snapshot")
    target_receipt = _receipt_mapping(payload, "target")
    expected_source = Path(_receipt_text(source_receipt, "path")).resolve()
    actual_source = _validate_source_path(source_path)
    if actual_source != expected_source:
        raise SqliteToPostgresMigrationError(
            "migration receipt belongs to a different SQLite source"
        )

    expected_snapshot = Path(_receipt_text(snapshot_receipt, "path"))
    sealed_snapshot, sealed_hash = validate_existing_snapshot(
        snapshot_path=expected_snapshot,
        work_dir=work_dir,
    )
    if sealed_hash != _receipt_text(snapshot_receipt, "sha256"):
        raise SqliteToPostgresMigrationError(
            "migration receipt snapshot hash does not match the sealed snapshot"
        )

    expected_redacted_target = _receipt_text(target_receipt, "redacted_url")
    try:
        actual_redacted_target = redact_database_url(target_url)
    except ValueError as exc:
        raise SqliteToPostgresMigrationError(
            "migration target URL is invalid"
        ) from exc
    if actual_redacted_target != expected_redacted_target:
        raise SqliteToPostgresMigrationError(
            "migration receipt belongs to a different PostgreSQL target"
        )
    expected_schema = _receipt_text(target_receipt, "schema")
    expected_stats = _receipt_table_stats(payload)
    expected_total = _receipt_integer(payload, "total_rows")
    if sum(stat.rows for stat in expected_stats) != expected_total:
        raise SqliteToPostgresMigrationError(
            "migration receipt total row count is inconsistent"
        )

    progress("正在确认停写后的 SQLite 与迁移快照完全一致…")
    current_snapshot, current_hash = create_consistent_snapshot(
        source_path=actual_source,
        work_dir=work_dir,
        progress=progress,
    )
    if current_hash != sealed_hash:
        raise SqliteToPostgresMigrationError(
            "SQLite source changed after the migration snapshot; run a new final migration"
        )

    progress("正在按 receipt 重新校验 PostgreSQL 全表 checksum…")
    database = _postgres_database(target_url, root_dir)
    try:
        if PostgresMigrator(database).current_version() != (
            POSTGRES_SCHEMA_MANIFEST.postgres_version
        ):
            raise SqliteToPostgresMigrationError(
                "PostgreSQL target schema version changed after migration"
            )
        with database.write(isolation_level="repeatable read") as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            actual_schema = str(
                connection.execute("SELECT current_schema() AS name").fetchone()["name"]
            )
            if actual_schema != expected_schema:
                raise SqliteToPostgresMigrationError(
                    "PostgreSQL target schema differs from the migration receipt"
                )
            columns = _postgres_columns(connection)
            if set(columns) != set(POSTGRES_BUSINESS_TABLES):
                raise SqliteToPostgresMigrationError(
                    "PostgreSQL target table manifest changed after migration"
                )
            for position, expected in enumerate(expected_stats, start=1):
                progress(
                    f"ACTIVATION VERIFY {position}/{len(expected_stats)} {expected.table}"
                )
                actual = _target_digest(
                    connection,
                    table=expected.table,
                    columns=columns[expected.table],
                    batch_rows=batch_rows,
                )
                if (
                    actual.count != expected.rows
                    or actual.hexdigest() != expected.checksum
                ):
                    raise SqliteToPostgresMigrationError(
                        f"PostgreSQL target no longer matches receipt table {expected.table}"
                    )
    except SqliteToPostgresMigrationError:
        raise
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "PostgreSQL activation verification failed"
        ) from exc
    finally:
        database.close()

    return MigrationVerification(
        source_path=str(actual_source),
        snapshot_path=str(current_snapshot),
        target_redacted_url=actual_redacted_target,
        target_schema=expected_schema,
        total_rows=expected_total,
        tables=expected_stats,
    )


def migrate(
    *,
    source_path: Path,
    target_url: str,
    work_dir: Path,
    root_dir: Path,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    progress: Callable[[str], None] = print,
    existing_snapshot: Path | None = None,
) -> MigrationResult:
    started_at = datetime.now(timezone.utc)
    source, target = preflight(
        source_path=source_path,
        target_url=target_url,
        root_dir=root_dir,
    )
    if existing_snapshot is None:
        snapshot, snapshot_hash = create_consistent_snapshot(
            source_path=source_path,
            work_dir=work_dir,
            progress=progress,
        )
    else:
        progress("正在验证并复用 sealed SQLite snapshot…")
        snapshot, snapshot_hash = validate_existing_snapshot(
            snapshot_path=existing_snapshot,
            work_dir=work_dir,
        )
    upgraded, applied = prepare_upgraded_snapshot(
        snapshot_path=snapshot,
        work_dir=work_dir,
        root_dir=root_dir,
    )
    database = _postgres_database(target_url, root_dir)
    try:
        stats, normalizations = copy_snapshot_to_postgres(
            snapshot_path=upgraded,
            database=database,
            batch_rows=batch_rows,
            progress=progress,
        )
    finally:
        database.close()
        if upgraded != snapshot:
            upgraded.unlink(missing_ok=True)
            upgraded.with_name(upgraded.name + "-wal").unlink(missing_ok=True)
            upgraded.with_name(upgraded.name + "-shm").unlink(missing_ok=True)

    finished_at = datetime.now(timezone.utc)
    timestamp = finished_at.strftime("%Y%m%dT%H%M%SZ")
    receipt = Path(work_dir).resolve() / f"migration-{timestamp}.receipt.json"
    payload = {
        "format": "silicon-notebook-sqlite-to-postgres-v1",
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "source": asdict(source),
        "snapshot": {"path": str(snapshot), "sha256": snapshot_hash},
        "sqlite_schema": {
            "before": source.schema_version,
            "after": POSTGRES_SCHEMA_MANIFEST.sqlite_version,
            "applied": list(applied),
            "retired_empty_tables_not_imported": list(SQLITE_RETIRED_TABLES),
        },
        "target": asdict(target),
        "postgres_schema_version": POSTGRES_SCHEMA_MANIFEST.postgres_version,
        "total_rows": sum(stat.rows for stat in stats),
        "tables": [asdict(stat) for stat in stats],
        "normalizations": [asdict(stat) for stat in normalizations],
        "storage": {
            "copied": False,
            "note": "database rows only; keep SILICON_NOTEBOOK_STORAGE_DIR available",
        },
    }
    _write_receipt(receipt, payload)
    return MigrationResult(
        source=source,
        target=target,
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_hash,
        upgraded_from=source.schema_version,
        upgraded_to=POSTGRES_SCHEMA_MANIFEST.sqlite_version,
        applied_sqlite_migrations=applied,
        total_rows=sum(stat.rows for stat in stats),
        tables=stats,
        normalizations=normalizations,
        receipt_path=str(receipt),
    )


def target_url_from_environment(name: str) -> str:
    if not _ENV_NAME.fullmatch(name):
        raise SqliteToPostgresMigrationError(
            "target environment variable name is invalid"
        )
    value = os.environ.get(name)
    if not value:
        raise SqliteToPostgresMigrationError(
            f"target environment variable {name} is not set"
        )
    return value
