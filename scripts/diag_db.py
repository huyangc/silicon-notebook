#!/usr/bin/env python3
"""Bounded, source-side-effect-free SQLite evidence for production DFX.

The source database is never opened through SQLite.  A non-blocking POSIX
shared-lock probe rejects rollback-mode exclusive writers, then bounded copies
of the database and WAL are inspected inside a temporary diagnostics directory.
Source file identities are validated before, during, and after inspection; any
race discards all database-derived evidence.
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote


REPORT_LIMIT_BYTES = 32 * 1024
EVIDENCE_LIMIT_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
MAX_SCHEMA_TABLES = 256
MAX_RAW_IDENTIFIER_BYTES = 4096
MAX_IDENTIFIER_BYTES = 128
MAX_FOREIGN_KEYS_PER_TABLE = 64
MAX_INDEXES_PER_TABLE = 128
MAX_INDEX_COLUMNS = 64
MAX_MISSING_INDEXES = 128
MAX_NOTEBOOK_REFERENCES = 128
MAX_NOTEBOOK_COUNTS = 128
MAX_PLAN_ROWS = 128
MAX_LARGEST_TABLES = 20
MAX_DEGRADED = 64
MAX_DEADLINE_SECONDS = 10.0
MAX_BUSY_TIMEOUT_MS = 1000

_PENDING_BYTE = 0x40000000
_SHARED_FIRST = _PENDING_BYTE + 2
_TERMINAL_CATEGORIES = frozenset(
    {"locked", "busy", "interrupted", "deadline", "source_changed"}
)
_DERIVED_LISTS = (
    "largest_tables",
    "notebook_references",
    "notebook_counts",
    "missing_fk_indexes",
    "delete_plan",
    "relevant_scans",
)
_EXPLICIT_NOTEBOOK_TABLES = (
    "knowledge_embeddings",
    "kg_objects_fts",
    "chunks_fts",
)
_DELETE_STATEMENTS = (
    (
        "knowledge_embeddings_delete",
        "knowledge_embeddings",
        "EXPLAIN QUERY PLAN DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
    ),
    (
        "kg_objects_fts_delete",
        "kg_objects_fts",
        "EXPLAIN QUERY PLAN DELETE FROM kg_objects_fts WHERE notebook_id = ?",
    ),
    (
        "chunks_fts_delete",
        "chunks_fts",
        "EXPLAIN QUERY PLAN DELETE FROM chunks_fts WHERE notebook_id = ?",
    ),
    (
        "notebook_delete",
        "notebooks",
        "EXPLAIN QUERY PLAN DELETE FROM notebooks WHERE id = ?",
    ),
)


class _DeadlineExceeded(Exception):
    pass


class _SourceLocked(Exception):
    pass


def _pseudonym(value: Optional[str]) -> str:
    if not value:
        return "all"
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:10]
    return f"nb#{digest}"


def _empty_evidence(notebook_id: Optional[str]) -> Dict[str, Any]:
    return {
        "version": 1,
        "status": "ok",
        "evidence_complete": False,
        "notebook": _pseudonym(notebook_id),
        "files": {"database_bytes": 0, "wal_bytes": 0, "shm_bytes": 0},
        "journal_mode": "unknown",
        "page_count": None,
        "freelist_count": None,
        "page_size": None,
        "database_bytes_estimate": None,
        "largest_tables": [],
        "notebook_references": [],
        "notebook_counts": [],
        "missing_fk_indexes": [],
        "delete_plan": [],
        "relevant_scans": [],
        "degraded": [],
        "safety": {
            "open_mode": "read-only-snapshot",
            "snapshot_used": False,
            "snapshot_bytes": 0,
            "source_unchanged": False,
            "query_only": True,
            "foreign_keys": True,
            "busy_timeout_ms": 0,
            "transaction_open": False,
        },
        "mutations_executed": 0,
    }


def _append_degraded(
    evidence: Dict[str, Any], probe: str, category: str, exc: BaseException
) -> None:
    rows = evidence["degraded"]
    clean_probe = _clean_text(probe, 80)
    clean_category = _clean_text(category, 32)
    if not any(
        row.get("probe") == clean_probe and row.get("category") == clean_category
        for row in rows
    ) and len(rows) < MAX_DEGRADED:
        rows.append(
            {
                "probe": clean_probe,
                "category": clean_category,
                "exception": _clean_text(type(exc).__name__, 40),
            }
        )
    evidence["status"] = "degraded"
    evidence["evidence_complete"] = False


def _error_category(exc: BaseException) -> str:
    message = str(exc).lower()
    if "interrupted" in message:
        return "interrupted"
    if "locked" in message:
        return "locked"
    if "busy" in message:
        return "busy"
    if "no such table" in message or "no such column" in message:
        return "missing_schema"
    if "not a database" in message or "malformed" in message:
        return "corrupt"
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "permission"
    return "unavailable"


def _clean_text(value: Any, max_bytes: int) -> str:
    text = "".join(
        character if character.isprintable() and character not in "\r\n\t" else " "
        for character in str(value)
    )
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def _safe_identifier(
    value: Any, evidence: Dict[str, Any], probe: str = "schema.identifier"
) -> str:
    raw = str(value)
    encoded = raw.encode("utf-8", "replace")
    valid = (
        bool(raw)
        and len(encoded) <= MAX_IDENTIFIER_BYTES
        and all(character.isalnum() or character == "_" for character in raw)
    )
    if valid:
        return raw
    _append_degraded(evidence, probe, "identifier_sanitized", ValueError())
    digest = hashlib.sha256(encoded).hexdigest()[:10]
    return f"schema#{digest}"


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _deadline_ok(evidence: Dict[str, Any], probe: str, deadline: float) -> bool:
    if time.monotonic() < deadline:
        return True
    _append_degraded(evidence, probe, "deadline", _DeadlineExceeded())
    return False


def _require_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _DeadlineExceeded()


def _safe_probe(
    evidence: Dict[str, Any], probe: str, deadline: float, operation
) -> Any:
    if any(row.get("category") in _TERMINAL_CATEGORIES for row in evidence["degraded"]):
        return None
    if not _deadline_ok(evidence, probe, deadline):
        return None
    try:
        return operation()
    except _DeadlineExceeded as exc:
        _append_degraded(evidence, probe, "deadline", exc)
    except (sqlite3.DatabaseError, OSError) as exc:
        _append_degraded(evidence, probe, _error_category(exc), exc)
    return None


def _file_identity(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _descriptor_identity(descriptor: int) -> Tuple[int, int, int, int]:
    stat = os.fstat(descriptor)
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _source_paths(path: Path) -> Dict[str, Path]:
    return {
        "database": path,
        "wal": Path(str(path) + "-wal"),
        "shm": Path(str(path) + "-shm"),
        "journal": Path(str(path) + "-journal"),
    }


def _capture_source_state(paths: Dict[str, Path]) -> Dict[str, Optional[Tuple[int, int, int, int]]]:
    return {name: _file_identity(path) for name, path in paths.items()}


def _set_file_evidence(evidence: Dict[str, Any], state) -> None:
    evidence["files"] = {
        "database_bytes": int((state.get("database") or (0, 0, 0, 0))[2]),
        "wal_bytes": int((state.get("wal") or (0, 0, 0, 0))[2]),
        "shm_bytes": int((state.get("shm") or (0, 0, 0, 0))[2]),
    }


def _acquire_source_read_lock(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        fcntl.lockf(
            descriptor,
            fcntl.LOCK_SH | fcntl.LOCK_NB,
            1,
            _SHARED_FIRST,
            os.SEEK_SET,
        )
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise _SourceLocked() from exc
        raise
    return descriptor


def _release_source_read_lock(descriptor: int) -> None:
    try:
        fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, _SHARED_FIRST, os.SEEK_SET)
    finally:
        os.close(descriptor)


def _copy_source_file(
    source: Path, target: Path, deadline: float, byte_limit: int
) -> int:
    copied = 0
    with source.open("rb") as reader, target.open("xb") as writer:
        while True:
            _require_deadline(deadline)
            chunk = reader.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            if copied + len(chunk) > byte_limit:
                raise ValueError("snapshot-byte-limit")
            writer.write(chunk)
            copied += len(chunk)
    return copied


def _copy_locked_database(
    descriptor: int, target: Path, deadline: float, byte_limit: int
) -> int:
    copied = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    with target.open("xb") as writer:
        while True:
            _require_deadline(deadline)
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            if copied + len(chunk) > byte_limit:
                raise ValueError("snapshot-byte-limit")
            writer.write(chunk)
            copied += len(chunk)
    return copied


def _open_read_only(
    path: Path, deadline: float, *, immutable_snapshot: bool = False
) -> sqlite3.Connection:
    _require_deadline(deadline)
    remaining = max(0.001, deadline - time.monotonic())
    timeout = min(1.0, remaining)
    busy_timeout_ms = max(1, min(MAX_BUSY_TIMEOUT_MS, int(remaining * 1000)))
    encoded = quote(str(path.resolve()), safe="/")
    immutable = "&immutable=1" if immutable_snapshot else ""
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro{immutable}",
        uri=True,
        timeout=timeout,
    )
    connection.row_factory = sqlite3.Row
    if hasattr(connection, "setlimit"):
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


def _fetch_limited(
    cursor: sqlite3.Cursor, limit: int, deadline: float
) -> Tuple[List[sqlite3.Row], bool]:
    rows: List[sqlite3.Row] = []
    for _ in range(max(0, int(limit)) + 1):
        _require_deadline(deadline)
        row = cursor.fetchone()
        if row is None:
            return rows, False
        rows.append(row)
    return rows[:limit], True


def _pragma_scalar(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    name: str,
    deadline: float,
) -> Any:
    def read():
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return None if row is None else row[0]

    return _safe_probe(evidence, f"pragma.{name}", deadline, read)


def _schema_tables(
    connection: sqlite3.Connection, evidence: Dict[str, Any], deadline: float
) -> List[Tuple[str, str]]:
    def read():
        cursor = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' ORDER BY name LIMIT ?",
            (MAX_SCHEMA_TABLES + 1,),
        )
        rows, truncated = _fetch_limited(cursor, MAX_SCHEMA_TABLES, deadline)
        if truncated:
            _append_degraded(evidence, "schema.tables", "row_limit", RuntimeError())
        result = []
        for row in rows:
            _require_deadline(deadline)
            raw = str(row[0])
            if len(raw.encode("utf-8", "replace")) > MAX_RAW_IDENTIFIER_BYTES:
                _append_degraded(
                    evidence, "schema.tables", "identifier_limit", ValueError()
                )
                continue
            result.append((raw, _safe_identifier(raw, evidence)))
        return result

    return _safe_probe(evidence, "schema.tables", deadline, read) or []


def _foreign_keys(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    table: str,
    deadline: float,
) -> List[sqlite3.Row]:
    def read():
        cursor = connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})")
        rows, truncated = _fetch_limited(cursor, MAX_FOREIGN_KEYS_PER_TABLE, deadline)
        if truncated:
            _append_degraded(
                evidence, "schema.foreign_keys", "row_limit", RuntimeError()
            )
        return rows

    return _safe_probe(evidence, "schema.foreign_keys", deadline, read) or []


def _index_prefixes(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    table: str,
    deadline: float,
) -> List[Tuple[str, ...]]:
    def read_indexes():
        cursor = connection.execute(f"PRAGMA index_list({_quote_identifier(table)})")
        return _fetch_limited(cursor, MAX_INDEXES_PER_TABLE, deadline)

    result = _safe_probe(evidence, "schema.indexes", deadline, read_indexes)
    if result is None:
        return []
    index_rows, truncated = result
    if truncated:
        _append_degraded(evidence, "schema.indexes", "row_limit", RuntimeError())
    prefixes: List[Tuple[str, ...]] = []
    for index_row in index_rows:
        if not _deadline_ok(evidence, "schema.indexes", deadline):
            break
        if len(index_row) >= 5 and int(index_row[4] or 0) != 0:
            continue
        index_name = str(index_row[1])
        if len(index_name.encode("utf-8", "replace")) > MAX_RAW_IDENTIFIER_BYTES:
            _append_degraded(
                evidence, "schema.indexes", "identifier_limit", ValueError()
            )
            continue

        def read_columns(name=index_name):
            cursor = connection.execute(f"PRAGMA index_info({_quote_identifier(name)})")
            return _fetch_limited(cursor, MAX_INDEX_COLUMNS, deadline)

        column_result = _safe_probe(
            evidence, "schema.index_columns", deadline, read_columns
        )
        if column_result is None:
            continue
        column_rows, columns_truncated = column_result
        if columns_truncated:
            _append_degraded(
                evidence, "schema.index_columns", "row_limit", RuntimeError()
            )
            continue
        columns = tuple(str(row[2]) for row in column_rows if row[2] is not None)
        if columns and len(columns) == len(column_rows):
            prefixes.append(columns)
    return prefixes


def _collect_fk_evidence(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    tables: Sequence[Tuple[str, str]],
    deadline: float,
) -> List[Tuple[str, str, Tuple[str, ...]]]:
    notebook_keys: List[Tuple[str, str, Tuple[str, ...]]] = []
    for raw_table, display_table in tables:
        if not _deadline_ok(evidence, "schema.foreign_keys", deadline):
            break
        fk_rows = _foreign_keys(connection, evidence, raw_table, deadline)
        grouped: Dict[int, List[sqlite3.Row]] = {}
        for row in fk_rows:
            if not _deadline_ok(evidence, "schema.foreign_keys", deadline):
                break
            grouped.setdefault(int(row[0]), []).append(row)
        if not grouped:
            continue
        prefixes = _index_prefixes(connection, evidence, raw_table, deadline)
        for rows in grouped.values():
            if not _deadline_ok(evidence, "schema.foreign_keys", deadline):
                break
            ordered = sorted(rows, key=lambda row: int(row[1]))
            raw_columns = tuple(str(row[3]) for row in ordered)
            columns = tuple(_safe_identifier(value, evidence) for value in raw_columns)
            raw_reference = str(ordered[0][2])
            reference = _safe_identifier(raw_reference, evidence)
            covered = any(
                prefix[: len(raw_columns)] == raw_columns for prefix in prefixes
            )
            if not covered and len(evidence["missing_fk_indexes"]) < MAX_MISSING_INDEXES:
                evidence["missing_fk_indexes"].append(
                    {
                        "table": display_table,
                        "columns": list(columns[:16]),
                        "references": reference,
                    }
                )
            if raw_reference == "notebooks":
                notebook_keys.append((raw_table, display_table, raw_columns))
                if len(evidence["notebook_references"]) < MAX_NOTEBOOK_REFERENCES:
                    evidence["notebook_references"].append(
                        {
                            "table": display_table,
                            "columns": list(columns[:16]),
                            "indexed": covered,
                        }
                    )
    return notebook_keys[:MAX_NOTEBOOK_REFERENCES]


def _count_table(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    raw_table: str,
    raw_column: str,
    notebook_id: str,
    deadline: float,
) -> Optional[int]:
    def read():
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(raw_table)} "
            f"WHERE {_quote_identifier(raw_column)} = ?",
            (notebook_id,),
        ).fetchone()
        return 0 if row is None else max(0, int(row[0]))

    return _safe_probe(evidence, "notebook.count", deadline, read)


def _collect_notebook_counts(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    notebook_keys: Iterable[Tuple[str, str, Tuple[str, ...]]],
    table_names: set,
    deadline: float,
) -> None:
    if notebook_id is None:
        return
    seen = set()
    for table in _EXPLICIT_NOTEBOOK_TABLES:
        if not _deadline_ok(evidence, "notebook.count", deadline):
            return
        if table not in table_names:
            continue
        count = _count_table(
            connection, evidence, table, "notebook_id", notebook_id, deadline
        )
        if count is not None:
            evidence["notebook_counts"].append({"table": table, "rows": count})
            seen.add(table)
    for raw_table, display_table, columns in notebook_keys:
        if not _deadline_ok(evidence, "notebook.count", deadline):
            return
        if len(evidence["notebook_counts"]) >= MAX_NOTEBOOK_COUNTS:
            _append_degraded(evidence, "notebook.count", "row_limit", RuntimeError())
            return
        if len(columns) != 1 or raw_table in seen:
            continue
        count = _count_table(
            connection, evidence, raw_table, columns[0], notebook_id, deadline
        )
        if count is not None:
            evidence["notebook_counts"].append(
                {"table": display_table, "rows": count}
            )
            seen.add(raw_table)


def _safe_plan_detail(
    value: Any, table_display: Dict[str, str], notebook_parameter: str
) -> str:
    detail = str(value).replace(notebook_parameter, "{id}")
    for raw, display in sorted(table_display.items(), key=lambda item: len(item[0]), reverse=True):
        detail = detail.replace(raw, display)
    return _clean_text(detail, 300)


def _collect_plans(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    missing_tables: Sequence[Tuple[str, str]],
    table_display: Dict[str, str],
    deadline: float,
) -> None:
    parameter = notebook_id if notebook_id is not None else "{id}"
    for probe, target_table, statement in _DELETE_STATEMENTS:
        if not _deadline_ok(evidence, f"plan.{probe}", deadline):
            break
        remaining = MAX_PLAN_ROWS - len(evidence["delete_plan"])
        if remaining <= 0:
            _append_degraded(evidence, "plan", "row_limit", RuntimeError())
            break

        def explain(sql=statement):
            cursor = connection.execute(sql, (parameter,))
            return _fetch_limited(cursor, remaining, deadline)

        result = _safe_probe(evidence, f"plan.{probe}", deadline, explain)
        if result is None:
            continue
        rows, truncated = result
        if truncated:
            _append_degraded(evidence, f"plan.{probe}", "row_limit", RuntimeError())
        for row in rows:
            if not _deadline_ok(evidence, f"plan.{probe}", deadline):
                return
            detail = _safe_plan_detail(row[3], table_display, str(parameter))
            evidence["delete_plan"].append(
                {
                    "probe": probe,
                    "id": int(row[0]),
                    "parent": int(row[1]),
                    "detail": detail,
                }
            )
            if "SCAN" not in detail.upper():
                continue
            if target_table in _EXPLICIT_NOTEBOOK_TABLES:
                evidence["relevant_scans"].append(
                    {"table": target_table, "detail": detail}
                )
                continue
            upper = detail.upper()
            for raw_table, display_table in missing_tables:
                if raw_table.upper() in upper or display_table.upper() in upper:
                    evidence["relevant_scans"].append(
                        {"table": display_table, "detail": detail}
                    )
                    break


def _collect_largest_tables(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    deadline: float,
) -> None:
    statement = (
        "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
        "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 20"
    )

    def read():
        cursor = connection.execute(statement)
        return _fetch_limited(cursor, MAX_LARGEST_TABLES, deadline)

    result = _safe_probe(evidence, "dbstat", deadline, read)
    if result is None:
        return
    rows, truncated = result
    if truncated:
        _append_degraded(evidence, "dbstat", "row_limit", RuntimeError())
    evidence["largest_tables"] = [
        {
            "name": _safe_identifier(row[0], evidence, "dbstat.identifier"),
            "bytes": int(row[1] or 0),
            "pages": int(row[2] or 0),
        }
        for row in rows
        if _deadline_ok(evidence, "dbstat", deadline)
    ]


def _collect_snapshot_evidence(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    deadline: float,
) -> None:
    page_count = _pragma_scalar(connection, evidence, "page_count", deadline)
    freelist_count = _pragma_scalar(connection, evidence, "freelist_count", deadline)
    page_size = _pragma_scalar(connection, evidence, "page_size", deadline)
    journal_mode = _pragma_scalar(connection, evidence, "journal_mode", deadline)
    evidence["page_count"] = int(page_count) if page_count is not None else None
    evidence["freelist_count"] = int(freelist_count) if freelist_count is not None else None
    evidence["page_size"] = int(page_size) if page_size is not None else None
    if page_count is not None and page_size is not None:
        evidence["database_bytes_estimate"] = int(page_count) * int(page_size)
    if journal_mode is not None:
        evidence["journal_mode"] = _clean_text(journal_mode, 20)

    tables = _schema_tables(connection, evidence, deadline)
    table_display = {raw: display for raw, display in tables}
    table_names = set(table_display)
    notebook_keys = _collect_fk_evidence(connection, evidence, tables, deadline)
    _collect_notebook_counts(
        connection,
        evidence,
        notebook_id,
        notebook_keys,
        table_names,
        deadline,
    )
    missing_tables = [
        (raw, display)
        for raw, display, _columns in notebook_keys
        if any(
            row.get("table") == display and not row.get("indexed")
            for row in evidence["notebook_references"]
        )
    ]
    _collect_plans(
        connection,
        evidence,
        notebook_id,
        missing_tables,
        table_display,
        deadline,
    )
    _collect_largest_tables(connection, evidence, deadline)
    evidence["safety"]["transaction_open"] = bool(connection.in_transaction)


def _discard_raced_evidence(
    evidence: Dict[str, Any], notebook_id: Optional[str], final_state
) -> Dict[str, Any]:
    discarded = _empty_evidence(notebook_id)
    _set_file_evidence(discarded, final_state)
    discarded["safety"]["snapshot_used"] = True
    discarded["safety"]["source_unchanged"] = False
    _append_degraded(
        discarded, "snapshot.validation", "source_changed", RuntimeError()
    )
    return discarded


def _bound_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    encoded = json.dumps(evidence, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= EVIDENCE_LIMIT_BYTES:
        return evidence
    _append_degraded(evidence, "evidence", "evidence_limit", RuntimeError())
    evidence["evidence_complete"] = False
    while len(
        json.dumps(evidence, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ) > EVIDENCE_LIMIT_BYTES:
        candidates = [name for name in _DERIVED_LISTS if evidence.get(name)]
        if not candidates:
            break
        largest = max(candidates, key=lambda name: len(evidence[name]))
        remove = max(1, len(evidence[largest]) // 2)
        del evidence[largest][-remove:]
    return evidence


def collect_db_evidence(
    db_path, notebook_id: Optional[str] = None, deadline_seconds: float = 4.0
) -> Dict[str, Any]:
    """Collect bounded metadata from a validated copy, never a source SQLite open."""
    evidence = _empty_evidence(notebook_id)
    try:
        requested = float(deadline_seconds)
        if requested != requested:
            requested = 4.0
    except (TypeError, ValueError, OverflowError):
        requested = 4.0
    deadline = time.monotonic() + max(0.001, min(requested, MAX_DEADLINE_SECONDS))

    path = Path(db_path)
    if not path.is_file():
        _append_degraded(evidence, "open", "missing", FileNotFoundError())
        return _bound_evidence(evidence)
    path = path.resolve()
    paths = _source_paths(path)
    initial_state = _capture_source_state(paths)
    _set_file_evidence(evidence, initial_state)
    lock_descriptor: Optional[int] = None
    try:
        if not _deadline_ok(evidence, "source.lock", deadline):
            return _bound_evidence(evidence)
        try:
            lock_descriptor = _acquire_source_read_lock(path)
        except _SourceLocked as exc:
            _append_degraded(evidence, "source.lock", "locked", exc)
            return _bound_evidence(evidence)

        before_state = _capture_source_state(paths)
        _set_file_evidence(evidence, before_state)
        if before_state.get("database") != _descriptor_identity(lock_descriptor):
            return _bound_evidence(
                _discard_raced_evidence(evidence, notebook_id, before_state)
            )
        if before_state.get("journal") is not None:
            _append_degraded(
                evidence,
                "snapshot.rollback_journal",
                "active_rollback_journal",
                RuntimeError(),
            )
            return _bound_evidence(evidence)
        source_bytes = sum(
            int((before_state.get(name) or (0, 0, 0, 0))[2])
            for name in ("database", "wal")
        )
        if source_bytes > MAX_SNAPSHOT_BYTES:
            _append_degraded(
                evidence, "snapshot.copy", "snapshot_byte_limit", ValueError()
            )
            return _bound_evidence(evidence)

        diagnostics_dir = path.parent / "diagnostics"
        diagnostics_dir.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="diag-db-", dir=diagnostics_dir) as temporary:
            snapshot = Path(temporary) / "snapshot.db"
            copied = _copy_locked_database(
                lock_descriptor, snapshot, deadline, MAX_SNAPSHOT_BYTES
            )
            if before_state.get("wal") is not None:
                copied += _copy_source_file(
                    paths["wal"],
                    Path(str(snapshot) + "-wal"),
                    deadline,
                    MAX_SNAPSHOT_BYTES - copied,
                )
            evidence["safety"]["snapshot_used"] = True
            evidence["safety"]["snapshot_bytes"] = copied
            middle_state = _capture_source_state(paths)
            if middle_state != before_state:
                return _bound_evidence(
                    _discard_raced_evidence(evidence, notebook_id, middle_state)
                )

            connection: Optional[sqlite3.Connection] = None
            try:
                connection = _open_read_only(snapshot, deadline)
                remaining_ms = max(
                    1, min(MAX_BUSY_TIMEOUT_MS, int((deadline - time.monotonic()) * 1000))
                )
                evidence["safety"]["busy_timeout_ms"] = remaining_ms
                _collect_snapshot_evidence(connection, evidence, notebook_id, deadline)
            except _DeadlineExceeded as exc:
                _append_degraded(evidence, "snapshot.open", "deadline", exc)
            except (sqlite3.DatabaseError, OSError) as exc:
                _append_degraded(evidence, "snapshot.open", _error_category(exc), exc)
            finally:
                if connection is not None:
                    try:
                        connection.set_progress_handler(None, 0)
                        connection.close()
                    except sqlite3.DatabaseError as exc:
                        _append_degraded(evidence, "snapshot.close", _error_category(exc), exc)

            after_state = _capture_source_state(paths)
            if after_state != before_state:
                return _bound_evidence(
                    _discard_raced_evidence(evidence, notebook_id, after_state)
                )
            evidence["safety"]["source_unchanged"] = True
            evidence["evidence_complete"] = evidence["status"] == "ok"
    except _DeadlineExceeded as exc:
        _append_degraded(evidence, "snapshot.copy", "deadline", exc)
    except (sqlite3.DatabaseError, OSError, ValueError) as exc:
        category = (
            "snapshot_byte_limit"
            if str(exc) == "snapshot-byte-limit"
            else _error_category(exc)
        )
        _append_degraded(evidence, "snapshot", category, exc)
    finally:
        if lock_descriptor is not None:
            _release_source_read_lock(lock_descriptor)
    return _bound_evidence(evidence)


def _bounded_text(text: str, limit: int = REPORT_LIMIT_BYTES) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    marker = "\n[output_truncated=true]\n"
    budget = max(0, limit - len(marker.encode("utf-8")))
    return encoded[:budget].decode("utf-8", "ignore") + marker


def render_db_report(evidence: Dict[str, Any]) -> str:
    """Render only copy-safe metadata in a bounded text report."""
    files = evidence.get("files") or {}
    lines = [
        "== silicon-notebook SQLite DFX evidence ==",
        (
            f"status={evidence.get('status', 'degraded')} "
            f"complete={str(bool(evidence.get('evidence_complete'))).lower()} "
            f"notebook={_clean_text(evidence.get('notebook', 'all'), 32)}"
        ),
        (
            "storage_bytes "
            f"database={int(files.get('database_bytes') or 0)} "
            f"wal={int(files.get('wal_bytes') or 0)} "
            f"shm={int(files.get('shm_bytes') or 0)}"
        ),
        (
            f"journal_mode={_clean_text(evidence.get('journal_mode', 'unknown'), 20)} "
            f"page_count={evidence.get('page_count')} "
            f"freelist_count={evidence.get('freelist_count')} "
            f"page_size={evidence.get('page_size')}"
        ),
        "",
        "largest_tables:",
    ]
    largest = list(evidence.get("largest_tables") or [])[:MAX_LARGEST_TABLES]
    lines.extend(
        f"- {_clean_text(row.get('name', 'unknown'), MAX_IDENTIFIER_BYTES)}: "
        f"bytes={int(row.get('bytes') or 0)} pages={int(row.get('pages') or 0)}"
        for row in largest
    )
    if not largest:
        lines.append("- unavailable")

    lines.extend(("", "notebook_child_counts:"))
    counts = list(evidence.get("notebook_counts") or [])[:MAX_NOTEBOOK_COUNTS]
    lines.extend(
        f"- {_clean_text(row.get('table', 'unknown'), MAX_IDENTIFIER_BYTES)}: "
        f"rows={int(row.get('rows') or 0)}"
        for row in counts
    )
    if not counts:
        lines.append("- not collected")

    lines.extend(("", "missing_foreign_key_indexes:"))
    missing = list(evidence.get("missing_fk_indexes") or [])[:MAX_MISSING_INDEXES]
    lines.extend(
        f"- {_clean_text(row.get('table', 'unknown'), MAX_IDENTIFIER_BYTES)}"
        f"({', '.join(_clean_text(item, MAX_IDENTIFIER_BYTES) for item in row.get('columns') or [])})"
        for row in missing
    )
    if not missing:
        lines.append("- none detected")

    lines.extend(("", "notebook_delete_scans:"))
    scans = list(evidence.get("relevant_scans") or [])[:MAX_PLAN_ROWS]
    lines.extend(
        f"- {_clean_text(row.get('table', 'unknown'), MAX_IDENTIFIER_BYTES)}: "
        f"{_clean_text(row.get('detail', ''), 300)}"
        for row in scans
    )
    if not scans:
        lines.append("- none detected in the four delete statements")

    lines.extend(("", "degraded_evidence:"))
    degraded = list(evidence.get("degraded") or [])[:MAX_DEGRADED]
    lines.extend(
        f"- probe={_clean_text(row.get('probe', 'unknown'), 80)} "
        f"category={_clean_text(row.get('category', 'unavailable'), 32)} "
        f"exception={_clean_text(row.get('exception', 'Error'), 40)}"
        for row in degraded
    )
    if not degraded:
        lines.append("- none")

    recommendations = []
    if missing:
        recommendations.append("add left-prefix indexes for the listed foreign-key columns")
    if scans:
        recommendations.append("review notebook-delete scan coverage before peak traffic")
    if int(files.get("wal_bytes") or 0) > max(
        int(files.get("database_bytes") or 0), 64 * 1024 * 1024
    ):
        recommendations.append("investigate sustained WAL growth and long-lived readers")
    if degraded:
        recommendations.append("retry unavailable probes after source activity settles")
    if not recommendations:
        recommendations.append("no targeted database action from this bounded snapshot")
    lines.extend(("", "recommendations:"))
    lines.extend(f"- {item}" for item in recommendations)
    lines.append(f"mutations_executed={int(evidence.get('mutations_executed') or 0)}")
    return _bounded_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect bounded source-side-effect-free SQLite DFX metadata."
    )
    parser.add_argument("--db", default=".local/silicon_notebook.db")
    parser.add_argument("--notebook-id", default=None)
    parser.add_argument("--deadline-seconds", type=float, default=4.0)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    evidence = collect_db_evidence(
        args.db,
        notebook_id=args.notebook_id,
        deadline_seconds=args.deadline_seconds,
    )
    sys.stdout.write(render_db_report(evidence))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
