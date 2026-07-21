#!/usr/bin/env python3
"""Bounded, read-only SQLite evidence for production DFX investigations.

This module deliberately depends only on the Python standard library and does
not import the backend application.  It inspects schema and aggregate metadata;
it never reads or renders application row content.
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote


REPORT_LIMIT_BYTES = 32 * 1024
MAX_SCHEMA_TABLES = 256
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
BUSY_TIMEOUT_MS = 1000


def _empty_evidence(notebook_id: Optional[str]) -> Dict[str, Any]:
    return {
        "version": 1,
        "status": "ok",
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
            "open_mode": "read-only",
            "immutable_snapshot": False,
            "query_only": True,
            "foreign_keys": True,
            "busy_timeout_ms": BUSY_TIMEOUT_MS,
            "transaction_open": False,
        },
        "mutations_executed": 0,
    }


def _pseudonym(value: Optional[str]) -> str:
    if not value:
        return "all"
    digest = hashlib.sha256(str(value).encode("utf-8", "replace")).hexdigest()[:10]
    return f"nb#{digest}"


def _append_degraded(
    evidence: Dict[str, Any], probe: str, category: str, exc: BaseException
) -> None:
    rows = evidence["degraded"]
    if len(rows) >= MAX_DEGRADED:
        return
    rows.append(
        {
            "probe": str(probe)[:80],
            "category": str(category)[:32],
            "exception": type(exc).__name__[:40],
        }
    )
    evidence["status"] = "degraded"


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


def _quote_identifier(value: str) -> str:
    """Quote only identifiers read from SQLite schema metadata."""
    return '"' + str(value).replace('"', '""') + '"'


def _read_size(path: Path) -> int:
    try:
        if not path.is_file():
            return 0
        return max(0, int(path.stat().st_size))
    except OSError:
        return 0


def _fetch_limited(cursor: sqlite3.Cursor, limit: int) -> Tuple[List[sqlite3.Row], bool]:
    rows: List[sqlite3.Row] = []
    for _ in range(max(0, int(limit)) + 1):
        row = cursor.fetchone()
        if row is None:
            return rows, False
        rows.append(row)
    return rows[:limit], True


def _safe_probe(
    evidence: Dict[str, Any], probe: str, operation
) -> Any:
    if any(
        row.get("category") in {"locked", "busy", "interrupted"}
        for row in evidence["degraded"]
    ):
        return None
    try:
        return operation()
    except (sqlite3.DatabaseError, OSError) as exc:
        _append_degraded(evidence, probe, _error_category(exc), exc)
        return None


def _header_uses_wal(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return False
    return (
        len(header) >= 20
        and header[:16] == b"SQLite format 3\x00"
        and (header[18] == 2 or header[19] == 2)
    )


def _open_read_only(
    path: Path, deadline: float, *, immutable_snapshot: bool
) -> sqlite3.Connection:
    encoded = quote(str(path.resolve()), safe="/")
    immutable = "&immutable=1" if immutable_snapshot else ""
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro{immutable}",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
    return connection


def _pragma_scalar(
    connection: sqlite3.Connection, evidence: Dict[str, Any], name: str
) -> Any:
    def read():
        row = connection.execute(f"PRAGMA {name}").fetchone()
        return None if row is None else row[0]

    return _safe_probe(evidence, f"pragma.{name}", read)


def _schema_tables(
    connection: sqlite3.Connection, evidence: Dict[str, Any]
) -> List[str]:
    def read():
        cursor = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' ORDER BY name LIMIT ?",
            (MAX_SCHEMA_TABLES + 1,),
        )
        rows, truncated = _fetch_limited(cursor, MAX_SCHEMA_TABLES)
        if truncated:
            _append_degraded(
                evidence,
                "schema.tables",
                "row_limit",
                RuntimeError("bounded"),
            )
        return [str(row[0]) for row in rows]

    return _safe_probe(evidence, "schema.tables", read) or []


def _foreign_keys(
    connection: sqlite3.Connection, evidence: Dict[str, Any], table: str
) -> List[sqlite3.Row]:
    def read():
        cursor = connection.execute(
            f"PRAGMA foreign_key_list({_quote_identifier(table)})"
        )
        rows, truncated = _fetch_limited(cursor, MAX_FOREIGN_KEYS_PER_TABLE)
        if truncated:
            _append_degraded(
                evidence,
                "schema.foreign_keys",
                "row_limit",
                RuntimeError("bounded"),
            )
        return rows

    return _safe_probe(evidence, "schema.foreign_keys", read) or []


def _index_prefixes(
    connection: sqlite3.Connection, evidence: Dict[str, Any], table: str
) -> List[Tuple[str, ...]]:
    def read_indexes():
        cursor = connection.execute(f"PRAGMA index_list({_quote_identifier(table)})")
        return _fetch_limited(cursor, MAX_INDEXES_PER_TABLE)

    result = _safe_probe(evidence, "schema.indexes", read_indexes)
    if result is None:
        return []
    index_rows, truncated = result
    if truncated:
        _append_degraded(
            evidence, "schema.indexes", "row_limit", RuntimeError("bounded")
        )
    prefixes: List[Tuple[str, ...]] = []
    for index_row in index_rows:
        # SQLite index_list: seq, name, unique, origin, partial.
        if len(index_row) >= 5 and int(index_row[4] or 0) != 0:
            continue
        index_name = str(index_row[1])

        def read_columns(name=index_name):
            cursor = connection.execute(
                f"PRAGMA index_info({_quote_identifier(name)})"
            )
            return _fetch_limited(cursor, MAX_INDEX_COLUMNS)

        column_result = _safe_probe(evidence, "schema.index_columns", read_columns)
        if column_result is None:
            continue
        column_rows, columns_truncated = column_result
        if columns_truncated:
            _append_degraded(
                evidence,
                "schema.index_columns",
                "row_limit",
                RuntimeError("bounded"),
            )
            continue
        columns = tuple(str(row[2]) for row in column_rows if row[2] is not None)
        if columns and len(columns) == len(column_rows):
            prefixes.append(columns)
    return prefixes


def _collect_fk_evidence(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    tables: Sequence[str],
) -> List[Tuple[str, Tuple[str, ...]]]:
    notebook_keys: List[Tuple[str, Tuple[str, ...]]] = []
    for table in tables:
        fk_rows = _foreign_keys(connection, evidence, table)
        grouped: Dict[int, List[sqlite3.Row]] = {}
        for row in fk_rows:
            grouped.setdefault(int(row[0]), []).append(row)
        if not grouped:
            continue
        prefixes = _index_prefixes(connection, evidence, table)
        for rows in grouped.values():
            ordered = sorted(rows, key=lambda row: int(row[1]))
            columns = tuple(str(row[3]) for row in ordered)
            referenced_table = str(ordered[0][2])
            covered = any(prefix[: len(columns)] == columns for prefix in prefixes)
            if not covered and len(evidence["missing_fk_indexes"]) < MAX_MISSING_INDEXES:
                evidence["missing_fk_indexes"].append(
                    {
                        "table": table[:128],
                        "columns": list(columns[:16]),
                        "references": referenced_table[:128],
                    }
                )
            if referenced_table == "notebooks":
                notebook_keys.append((table, columns))
                if len(evidence["notebook_references"]) < MAX_NOTEBOOK_REFERENCES:
                    evidence["notebook_references"].append(
                        {
                            "table": table[:128],
                            "columns": list(columns[:16]),
                            "indexed": covered,
                        }
                    )
    return notebook_keys[:MAX_NOTEBOOK_REFERENCES]


def _collect_notebook_counts(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    notebook_keys: Iterable[Tuple[str, Tuple[str, ...]]],
) -> None:
    if notebook_id is None:
        return
    seen = set()
    for table, columns in notebook_keys:
        if len(evidence["notebook_counts"]) >= MAX_NOTEBOOK_COUNTS:
            break
        if len(columns) != 1 or (table, columns) in seen:
            continue
        seen.add((table, columns))

        def count_rows(target=table, column=columns[0]):
            row = connection.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(target)} "
                f"WHERE {_quote_identifier(column)} = ?",
                (notebook_id,),
            ).fetchone()
            return 0 if row is None else max(0, int(row[0]))

        count = _safe_probe(evidence, "notebook.count", count_rows)
        if count is not None:
            evidence["notebook_counts"].append(
                {"table": table[:128], "rows": count}
            )


_PLAN_STATEMENTS = (
    ("notebook_delete", "EXPLAIN QUERY PLAN DELETE FROM notebooks WHERE id = ?"),
    (
        "source_files",
        "EXPLAIN QUERY PLAN SELECT file_path FROM sources WHERE notebook_id = ?",
    ),
    (
        "embedding_delete",
        "EXPLAIN QUERY PLAN DELETE FROM knowledge_embeddings WHERE notebook_id = ?",
    ),
    (
        "fts_delete",
        "EXPLAIN QUERY PLAN DELETE FROM kg_objects_fts WHERE notebook_id = ?",
    ),
)


def _collect_plans(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    missing_tables: Sequence[str],
) -> None:
    parameter = notebook_id if notebook_id is not None else "{id}"
    for probe, statement in _PLAN_STATEMENTS:
        remaining = MAX_PLAN_ROWS - len(evidence["delete_plan"])
        if remaining <= 0:
            _append_degraded(evidence, "plan", "row_limit", RuntimeError("bounded"))
            break

        def explain(sql=statement):
            cursor = connection.execute(sql, (parameter,))
            return _fetch_limited(cursor, remaining)

        result = _safe_probe(evidence, f"plan.{probe}", explain)
        if result is None:
            continue
        rows, truncated = result
        if truncated:
            _append_degraded(evidence, f"plan.{probe}", "row_limit", RuntimeError("bounded"))
        for row in rows:
            detail = str(row[3]).replace(str(parameter), "{id}")[:300]
            plan_row = {
                "probe": probe,
                "id": int(row[0]),
                "parent": int(row[1]),
                "detail": detail,
            }
            evidence["delete_plan"].append(plan_row)
            upper = detail.upper()
            if "SCAN" not in upper:
                continue
            for table in missing_tables:
                if table.upper() in upper:
                    evidence["relevant_scans"].append(
                        {"table": table[:128], "detail": detail}
                    )
                    break


def _collect_largest_tables(
    connection: sqlite3.Connection, evidence: Dict[str, Any]
) -> None:
    statement = (
        "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
        "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 20"
    )

    def read():
        cursor = connection.execute(statement)
        return _fetch_limited(cursor, MAX_LARGEST_TABLES)

    result = _safe_probe(evidence, "dbstat", read)
    if result is None:
        return
    rows, truncated = result
    if truncated:
        _append_degraded(evidence, "dbstat", "row_limit", RuntimeError("bounded"))
    evidence["largest_tables"] = [
        {"name": str(row[0])[:128], "bytes": int(row[1] or 0), "pages": int(row[2] or 0)}
        for row in rows
    ]


def collect_db_evidence(
    db_path, notebook_id: Optional[str] = None, deadline_seconds: float = 4.0
) -> Dict[str, Any]:
    """Collect bounded schema/aggregate evidence without changing the database."""
    path = Path(db_path)
    evidence = _empty_evidence(notebook_id)
    evidence["files"] = {
        "database_bytes": _read_size(path),
        "wal_bytes": _read_size(Path(str(path) + "-wal")),
        "shm_bytes": _read_size(Path(str(path) + "-shm")),
    }
    if not path.is_file():
        _append_degraded(evidence, "open", "missing", FileNotFoundError())
        return evidence

    try:
        requested = float(deadline_seconds)
    except (TypeError, ValueError, OverflowError):
        requested = 4.0
    duration = max(0.05, min(requested, MAX_DEADLINE_SECONDS))
    deadline = time.monotonic() + duration
    connection: Optional[sqlite3.Connection] = None
    try:
        wal_path = Path(str(path) + "-wal")
        shm_path = Path(str(path) + "-shm")
        wal_exists = wal_path.exists()
        shm_exists = shm_path.exists()
        if wal_exists != shm_exists:
            _append_degraded(
                evidence,
                "open.sidecars",
                "incomplete_sidecars",
                RuntimeError("incomplete"),
            )
            return evidence
        immutable_snapshot = not wal_exists and _header_uses_wal(path)
        evidence["safety"]["immutable_snapshot"] = immutable_snapshot
        connection = _open_read_only(
            path,
            deadline,
            immutable_snapshot=immutable_snapshot,
        )
        page_count = _pragma_scalar(connection, evidence, "page_count")
        freelist_count = _pragma_scalar(connection, evidence, "freelist_count")
        page_size = _pragma_scalar(connection, evidence, "page_size")
        journal_mode = _pragma_scalar(connection, evidence, "journal_mode")
        evidence["page_count"] = int(page_count) if page_count is not None else None
        evidence["freelist_count"] = (
            int(freelist_count) if freelist_count is not None else None
        )
        evidence["page_size"] = int(page_size) if page_size is not None else None
        if page_count is not None and page_size is not None:
            evidence["database_bytes_estimate"] = int(page_count) * int(page_size)
        if immutable_snapshot:
            # SQLite's immutable VFS reports "delete" because it never opens
            # the WAL machinery.  The database header remains the authoritative
            # journal setting for this sidecar-free snapshot.
            evidence["journal_mode"] = "wal"
        elif journal_mode is not None:
            evidence["journal_mode"] = str(journal_mode)[:20]

        tables = _schema_tables(connection, evidence)
        notebook_keys = _collect_fk_evidence(connection, evidence, tables)
        _collect_notebook_counts(connection, evidence, notebook_id, notebook_keys)
        missing_tables = [
            row["table"]
            for row in evidence["missing_fk_indexes"]
            if row.get("references") == "notebooks"
        ]
        _collect_plans(connection, evidence, notebook_id, missing_tables)
        _collect_largest_tables(connection, evidence)
        evidence["safety"]["transaction_open"] = bool(connection.in_transaction)
    except (sqlite3.DatabaseError, OSError) as exc:
        _append_degraded(evidence, "open", _error_category(exc), exc)
    finally:
        if connection is not None:
            try:
                connection.set_progress_handler(None, 0)
                connection.close()
            except sqlite3.DatabaseError as exc:
                _append_degraded(evidence, "close", _error_category(exc), exc)
    return evidence


def _bounded_text(text: str, limit: int = REPORT_LIMIT_BYTES) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= limit:
        return text
    marker = "\n[output_truncated=true]\n"
    budget = max(0, limit - len(marker.encode("utf-8")))
    clipped = encoded[:budget].decode("utf-8", "ignore")
    return clipped + marker


def render_db_report(evidence: Dict[str, Any]) -> str:
    """Render only copy-safe metadata in a bounded text report."""
    files = evidence.get("files") or {}
    lines = [
        "== silicon-notebook SQLite DFX evidence ==",
        f"status={evidence.get('status', 'degraded')} notebook={evidence.get('notebook', 'all')}",
        (
            "storage_bytes "
            f"database={int(files.get('database_bytes') or 0)} "
            f"wal={int(files.get('wal_bytes') or 0)} "
            f"shm={int(files.get('shm_bytes') or 0)}"
        ),
        (
            f"journal_mode={evidence.get('journal_mode', 'unknown')} "
            f"page_count={evidence.get('page_count')} "
            f"freelist_count={evidence.get('freelist_count')} "
            f"page_size={evidence.get('page_size')}"
        ),
        "",
        "largest_tables:",
    ]
    largest = list(evidence.get("largest_tables") or [])[:MAX_LARGEST_TABLES]
    lines.extend(
        f"- {row.get('name', 'unknown')}: bytes={int(row.get('bytes') or 0)} pages={int(row.get('pages') or 0)}"
        for row in largest
    )
    if not largest:
        lines.append("- unavailable")

    lines.extend(("", "notebook_child_counts:"))
    counts = list(evidence.get("notebook_counts") or [])[:MAX_NOTEBOOK_COUNTS]
    lines.extend(
        f"- {row.get('table', 'unknown')}: rows={int(row.get('rows') or 0)}"
        for row in counts
    )
    if not counts:
        lines.append("- not collected")

    lines.extend(("", "missing_foreign_key_indexes:"))
    missing = list(evidence.get("missing_fk_indexes") or [])[:MAX_MISSING_INDEXES]
    lines.extend(
        f"- {row.get('table', 'unknown')}({', '.join(row.get('columns') or [])})"
        for row in missing
    )
    if not missing:
        lines.append("- none detected")

    lines.extend(("", "notebook_delete_scans:"))
    scans = list(evidence.get("relevant_scans") or [])[:MAX_PLAN_ROWS]
    lines.extend(
        f"- {row.get('table', 'unknown')}: {row.get('detail', '')}" for row in scans
    )
    if not scans:
        lines.append("- none tied to an unindexed notebook child key")

    lines.extend(("", "degraded_evidence:"))
    degraded = list(evidence.get("degraded") or [])[:MAX_DEGRADED]
    lines.extend(
        f"- probe={row.get('probe', 'unknown')} category={row.get('category', 'unavailable')} exception={row.get('exception', 'Error')}"
        for row in degraded
    )
    if not degraded:
        lines.append("- none")

    recommendations = []
    if missing:
        recommendations.append("add left-prefix indexes for the listed foreign-key columns")
    if scans:
        recommendations.append("review notebook-delete cascade scan coverage before peak traffic")
    if int(files.get("wal_bytes") or 0) > max(int(files.get("database_bytes") or 0), 64 * 1024 * 1024):
        recommendations.append("investigate sustained WAL growth and long-lived readers")
    if degraded:
        recommendations.append("retry unavailable probes during lower lock pressure")
    if not recommendations:
        recommendations.append("no targeted database action from this bounded snapshot")
    lines.extend(("", "recommendations:"))
    lines.extend(f"- {item}" for item in recommendations)
    lines.append(f"mutations_executed={int(evidence.get('mutations_executed') or 0)}")
    return _bounded_text("\n".join(lines) + "\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect bounded read-only SQLite DFX metadata."
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
