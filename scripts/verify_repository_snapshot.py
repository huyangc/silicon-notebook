#!/usr/bin/env python3
"""Backup-only verification that the current repository opens a pre-refactor
SQLite database without changing it.

~~~text
python scripts/verify_repository_snapshot.py \
  --database PATH \
  --storage-dir PATH
~~~

Safety contract (Task 28):

- The original database is opened only as a SQLite URI ``mode=ro``, only long
  enough to call ``Connection.backup()`` into a temporary file.  The
  repository is NEVER constructed with the original database path.
- The original storage directory is never handed to the repository either; the
  repository receives an empty temporary storage directory.  The original
  storage is only ``stat``-ed (file list / size / mtime) to prove it is
  untouched.
- Settings are built from explicit field values (never ambient ``.env`` /
  environment defaults) and every model/embedding/rerank/MinerU provider must
  be unconfigured/off; a socket guard rejects any network call while the
  repository is alive.
- The only rows allowed to change when the repository opens the backup are
  the documented startup normalizations: running ``merge_review_jobs`` become
  ``failed`` and running ``ask_jobs`` become ``interrupted`` (both with the
  restart error), missing built-in user/profile/whitelist/object-schema seed
  rows are inserted, and the ``user-local`` admin in-place upgrade rewrites
  username/role/password hash/salt/iterations/updated_at.  Every other
  existing primary key, row count and canonical row digest must match.
- stdout carries table names, counts and digests only — never row content.

Success line::

    repository-snapshot: PASS schema=v<source_user_version> changed_tables=0
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import socket
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings
from app.services.sqlite_repository import (
    SCHEMA_VERSION,
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)

RESTART_ERROR = "中断:服务重启"

# Tables whose rows the startup sequence may legitimately touch; every one is
# compared row-by-row against the documented allowance instead of digest-only.
SPECIAL_TABLES = (
    "users",
    "user_profiles",
    "concept_whitelist",
    "object_schemas",
    "merge_review_jobs",
    "ask_jobs",
)

# The admin in-place upgrade (`_seed`) rewrites exactly these user-local
# columns on every startup.
ADMIN_UPGRADE_COLUMNS = frozenset(
    {
        "username",
        "role",
        "password_hash",
        "password_salt",
        "password_iterations",
        "updated_at",
    }
)


def _fail(message: str) -> "SystemExit":
    return SystemExit(f"repository-snapshot: FAIL {message}")


# ---------------------------------------------------------------------------
# Offline settings (explicit field values only — hostile environments must not
# leak through; init kwargs take precedence over env/.env in pydantic-settings)
# ---------------------------------------------------------------------------

def offline_settings(database: Path, storage: Path) -> Settings:
    settings = Settings(
        database_url=f"sqlite:///{database}",
        storage_dir=str(storage),
        openai_compat_base_url="",
        openai_compat_api_key="",
        openai_compat_model="",
        reasoning_llm_base_url="",
        reasoning_llm_api_key="",
        reasoning_llm_model="",
        rewrite_llm_base_url="",
        rewrite_llm_api_key="",
        rewrite_llm_model="",
        kg_llm_base_url="",
        kg_llm_api_key="",
        kg_llm_model="",
        embed_provider="",
        embed_model="",
        embed_base_url="",
        embed_api_key="",
        rerank_model="",
        rerank_base_url="",
        rerank_api_key="",
        mineru_mode="off",
        mineru_api_url="",
        mineru_vlm_server_url="",
        mineru_api_token="",
        scale_index_auto_enabled=False,
        event_log_enabled=False,
        llm_log_enabled=False,
        debug_logs_enabled=False,
        auth_optional=True,
    )
    _assert_offline(settings)
    return settings


def _assert_offline(settings: Settings) -> None:
    offline_checks = {
        "llm_configured": settings.llm_configured,
        "reasoning_llm_configured": settings.reasoning_llm_configured,
        "rewrite_llm_configured": settings.rewrite_llm_configured,
        "kg_llm_configured": settings.kg_llm_configured,
        "embedder_configured": settings.embedder_configured,
        "rerank_model": bool(settings.rerank_model),
        "rerank_base_url": bool(settings.rerank_base_url),
        "mineru_enabled": settings.mineru_enabled,
        "mineru_cloud_enabled": settings.mineru_cloud_enabled,
        "scale_index_auto_enabled": settings.scale_index_auto_enabled,
        "event_log_enabled": settings.event_log_enabled,
        "llm_log_enabled": settings.llm_log_enabled,
        "debug_logs_enabled": settings.debug_logs_enabled,
    }
    leaked = sorted(name for name, value in offline_checks.items() if value)
    if leaked:
        raise _fail(f"provider configuration leaked into verification: {leaked}")


@contextmanager
def _no_network() -> Iterator[None]:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("repository-snapshot verification attempted network access")

    saved_create = socket.create_connection
    saved_connect = socket.socket.connect
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.socket.connect = refuse  # type: ignore[method-assign, assignment]
    try:
        yield
    finally:
        socket.create_connection = saved_create  # type: ignore[assignment]
        socket.socket.connect = saved_connect  # type: ignore[method-assign, assignment]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

@dataclass
class TableSnapshot:
    columns: Tuple[Tuple[str, str, int, Any, int], ...]
    column_names: Tuple[str, ...]
    pk_columns: Tuple[str, ...]
    sql: str
    virtual: bool
    row_count: int
    digest: str


@dataclass
class DatabaseSnapshot:
    user_version: int
    tables: Dict[str, TableSnapshot]
    indexes: Tuple[str, ...]
    special_rows: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]]


def _digest_update(h: "hashlib._Hash", value: Any) -> None:
    if value is None:
        segment = b"N"
    elif isinstance(value, bool):
        segment = b"I" + str(int(value)).encode()
    elif isinstance(value, int):
        segment = b"I" + str(value).encode()
    elif isinstance(value, float):
        segment = b"F" + repr(value).encode()
    elif isinstance(value, bytes):
        segment = b"B" + value
    else:
        segment = b"T" + str(value).encode("utf-8", "surrogatepass")
    h.update(str(len(segment)).encode())
    h.update(b":")
    h.update(segment)


def _comparable(value: Any) -> Any:
    if isinstance(value, bytes):
        return ("blob-sha256", hashlib.sha256(value).hexdigest())
    return value


def _table_digest(
    digest_conn: sqlite3.Connection,
    table: str,
    column_names: Sequence[str],
    pk_columns: Sequence[str],
) -> str:
    quoted = ", ".join(f'"{name}"' for name in column_names)
    h = hashlib.sha256()
    try:
        # rowid tables: rowid order is a stable physical identity and gives a
        # sequential scan; the rowid itself joins the digest (implicit PK).
        cursor = digest_conn.execute(
            f'SELECT rowid, {quoted} FROM "{table}" ORDER BY rowid'
        )
    except sqlite3.OperationalError:
        order = (
            ", ".join(f'"{name}"' for name in pk_columns) if pk_columns else quoted
        )
        cursor = digest_conn.execute(
            f'SELECT {quoted} FROM "{table}" ORDER BY {order}'
        )
    while True:
        rows = cursor.fetchmany(2000)
        if not rows:
            break
        for row in rows:
            for value in row:
                _digest_update(h, value)
    return h.hexdigest()


def _special_table_rows(
    meta_conn: sqlite3.Connection,
    table: str,
    column_names: Sequence[str],
    pk_columns: Sequence[str],
) -> Dict[Tuple[Any, ...], Dict[str, Any]]:
    quoted = ", ".join(f'"{name}"' for name in column_names)
    rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    if pk_columns:
        for row in meta_conn.execute(f'SELECT {quoted} FROM "{table}"'):
            data = {name: _comparable(row[index]) for index, name in enumerate(column_names)}
            rows[tuple(data[name] for name in pk_columns)] = data
    else:
        for row in meta_conn.execute(
            f'SELECT rowid, {quoted} FROM "{table}" ORDER BY rowid'
        ):
            data = {
                name: _comparable(row[index + 1])
                for index, name in enumerate(column_names)
            }
            rows[(row[0],)] = data
    return rows


def snapshot_database(
    db_path: Path, column_plan: Optional[Dict[str, Tuple[str, ...]]] = None
) -> DatabaseSnapshot:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    meta_conn = sqlite3.connect(uri, uri=True)
    digest_conn = sqlite3.connect(uri, uri=True)
    digest_conn.text_factory = bytes  # byte-exact TEXT digesting, no decode risk
    try:
        user_version = int(meta_conn.execute("PRAGMA user_version").fetchone()[0])
        master = meta_conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        indexes = tuple(sorted(name for kind, name, _sql in master if kind == "index"))
        tables: Dict[str, TableSnapshot] = {}
        special_rows: Dict[str, Dict[Tuple[Any, ...], Dict[str, Any]]] = {}
        for kind, name, sql in master:
            if kind != "table":
                continue
            info = meta_conn.execute(f'PRAGMA table_info("{name}")').fetchall()
            columns = tuple(
                (row[1], row[2], row[3], row[4], row[5]) for row in info
            )
            all_column_names = tuple(row[1] for row in info)
            pk_columns = tuple(
                row[1]
                for row in sorted(info, key=lambda item: item[5])
                if row[5]
            )
            virtual = "VIRTUAL TABLE" in (sql or "").upper()
            row_count = int(
                meta_conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            )
            planned = (column_plan or {}).get(name, all_column_names)
            digest_columns = tuple(c for c in planned if c in all_column_names)
            digest = (
                ""
                if virtual
                else _table_digest(digest_conn, name, digest_columns, pk_columns)
            )
            tables[name] = TableSnapshot(
                columns=columns,
                column_names=all_column_names,
                pk_columns=pk_columns,
                sql=sql or "",
                virtual=virtual,
                row_count=row_count,
                digest=digest,
            )
            if name in SPECIAL_TABLES:
                special_rows[name] = _special_table_rows(
                    meta_conn, name, digest_columns, pk_columns
                )
        return DatabaseSnapshot(
            user_version=user_version,
            tables=tables,
            indexes=indexes,
            special_rows=special_rows,
        )
    finally:
        meta_conn.close()
        digest_conn.close()


# ---------------------------------------------------------------------------
# Comparison (documented normalizations only)
# ---------------------------------------------------------------------------

@dataclass
class VerificationResult:
    ok: bool
    source_user_version: int
    final_user_version: int
    table_counts: Dict[str, int]
    table_digests: Dict[str, str]
    changed_tables: List[str]
    discrepancies: List[str]
    migration_added_tables: List[str]
    normalized: Dict[str, int]
    reads: Dict[str, int]
    storage_unchanged: bool
    source_unchanged: bool
    storage_file_count: int
    database: str = ""
    duration_seconds: float = 0.0


def _empty_normalized() -> Dict[str, int]:
    return {
        "merge_review_jobs": 0,
        "ask_jobs": 0,
        "seeded_users": 0,
        "seeded_user_profiles": 0,
        "seeded_concept_whitelist": 0,
        "seeded_object_schemas": 0,
        "admin_upgraded": 0,
    }


def _compare_special_rows(
    table: str,
    pre_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    post_rows: Dict[Tuple[Any, ...], Dict[str, Any]],
    normalized: Dict[str, int],
    problems: List[str],
) -> None:
    def rows_equal(pre: Dict[str, Any], post: Dict[str, Any], skip: frozenset) -> bool:
        keys = set(pre) | set(post)
        return all(pre.get(k) == post.get(k) for k in keys if k not in skip)

    for key, pre_row in pre_rows.items():
        post_row = post_rows.get(key)
        if post_row is None:
            problems.append(f"table={table} reason=row-deleted")
            continue
        if table == "users" and pre_row.get("id") == "user-local":
            if not rows_equal(pre_row, post_row, ADMIN_UPGRADE_COLUMNS):
                problems.append(f"table={table} reason=admin-row-changed-beyond-upgrade")
            elif not rows_equal(pre_row, post_row, frozenset()):
                normalized["admin_upgraded"] = 1
            continue
        if table == "merge_review_jobs" and pre_row.get("status") == "running":
            if (
                post_row.get("status") == "failed"
                and post_row.get("error") == RESTART_ERROR
                and rows_equal(pre_row, post_row, frozenset({"status", "error"}))
            ):
                normalized["merge_review_jobs"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if table == "ask_jobs" and pre_row.get("status") == "running":
            if (
                post_row.get("status") == "interrupted"
                and post_row.get("error") == RESTART_ERROR
                and rows_equal(pre_row, post_row, frozenset({"status", "error"}))
            ):
                normalized["ask_jobs"] += 1
            else:
                problems.append(f"table={table} reason=recovery-changed-other-fields")
            continue
        if not rows_equal(pre_row, post_row, frozenset()):
            problems.append(f"table={table} reason=row-changed")

    for key, post_row in post_rows.items():
        if key in pre_rows:
            continue
        if table == "users" and post_row.get("id") == "user-local":
            normalized["seeded_users"] += 1
        elif table == "user_profiles" and post_row.get("id") == "profile-local":
            normalized["seeded_user_profiles"] += 1
        elif table == "concept_whitelist" and post_row.get("note") == "builtin":
            normalized["seeded_concept_whitelist"] += 1
        elif table == "object_schemas" and post_row.get("source") == "builtin":
            normalized["seeded_object_schemas"] += 1
        else:
            problems.append(f"table={table} reason=row-inserted")


def compare_snapshots(
    pre: DatabaseSnapshot, post: DatabaseSnapshot
) -> Tuple[List[str], List[str], List[str], Dict[str, int]]:
    """Returns (changed_tables, discrepancies, migration_added_tables, normalized)."""
    migrated = pre.user_version < SCHEMA_VERSION
    discrepancies: List[str] = []
    migration_added: List[str] = []
    normalized = _empty_normalized()
    changed: Dict[str, List[str]] = {}

    def note(table: str, reason: str) -> None:
        changed.setdefault(table, []).append(reason)

    if post.user_version != SCHEMA_VERSION:
        discrepancies.append(
            f"user_version={post.user_version} expected={SCHEMA_VERSION}"
        )

    for name in pre.tables:
        if name not in post.tables:
            note(name, "table-dropped")
    for name, post_table in post.tables.items():
        if name in pre.tables:
            continue
        if not migrated:
            note(name, "table-added-without-migration")
        elif post_table.row_count != 0:
            note(name, "migration-added-table-not-empty")
        else:
            migration_added.append(name)

    for name, pre_table in pre.tables.items():
        post_table = post.tables.get(name)
        if post_table is None:
            continue
        pre_cols = set(pre_table.column_names)
        post_cols = set(post_table.column_names)
        if pre_cols - post_cols:
            note(name, "column-removed")
            continue
        if (post_cols - pre_cols) and not migrated:
            note(name, "column-added-without-migration")
        if pre_table.pk_columns != post_table.pk_columns:
            note(name, "primary-key-changed")
            continue
        if pre_table.sql != post_table.sql and not migrated:
            note(name, "schema-sql-changed")
        if name in SPECIAL_TABLES:
            problems: List[str] = []
            _compare_special_rows(
                name,
                pre.special_rows.get(name, {}),
                post.special_rows.get(name, {}),
                normalized,
                problems,
            )
            if problems:
                for problem in problems:
                    note(name, problem.split("reason=", 1)[-1])
            continue
        if pre_table.virtual or post_table.virtual:
            # FTS virtual tables are covered byte-exactly by their shadow
            # tables; only the row count is compared here.
            if pre_table.row_count != post_table.row_count:
                note(name, "virtual-count-changed")
            continue
        if pre_table.row_count != post_table.row_count:
            note(name, "row-count-changed")
        elif pre_table.digest != post_table.digest:
            note(name, "row-digest-changed")

    new_indexes = set(post.indexes) - set(pre.indexes)
    missing_indexes = set(pre.indexes) - set(post.indexes)
    if missing_indexes:
        discrepancies.append(f"indexes-dropped={sorted(missing_indexes)}")
    if new_indexes and not migrated:
        discrepancies.append(f"indexes-added-without-migration={sorted(new_indexes)}")

    changed_tables = sorted(changed)
    for table in changed_tables:
        for reason in changed[table]:
            discrepancies.append(f"table={table} reason={reason}")
    return changed_tables, discrepancies, sorted(migration_added), normalized


# ---------------------------------------------------------------------------
# Representative reads (repository behaves like a live open of the old data)
# ---------------------------------------------------------------------------

def _read_counts() -> Dict[str, int]:
    return {
        "notebooks": 0,
        "sources": 0,
        "knowledge_types": 0,
        "knowledge_rows": 0,
        "unified_status": 0,
        "conversations": 0,
        "answers": 0,
        "ask_jobs": 0,
        "reports": 0,
        "search_hits": 0,
    }


def exercise_reads(repo: Any, backup_path: Path) -> Dict[str, int]:
    counts = _read_counts()
    admin = repo.maintenance.resolve_owner_profile(None)
    if admin is None:
        raise _fail("seeded admin account resolution returned no profile")

    notebook_rows = repo.maintenance.notebook_rows()
    counts["notebooks"] = len(notebook_rows)
    ko_counts = repo.maintenance.kg_object_counts_by_notebook()
    ranked = sorted(
        notebook_rows,
        key=lambda row: (-(ko_counts.get(row["id"], 0)), row["id"]),
    )

    probe = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
    try:
        # Top KG notebooks plus notebooks that actually hold Ask jobs/reports,
        # so those read paths are exercised whenever the database has them.
        wanted = {row["id"] for row in ranked[:3]}
        for table in ("ask_jobs", "reports"):
            for row in probe.execute(
                f"SELECT DISTINCT notebook_id FROM {table} LIMIT 2"
            ):
                wanted.add(row[0])
        sample = [row for row in ranked if row["id"] in wanted][:6]
        usernames = {
            row[0]: row[1]
            for row in probe.execute("SELECT id, username FROM users")
        }
        for notebook in sample:
            notebook_id = notebook["id"]
            username = usernames.get(notebook.get("created_by") or "")
            owner = (
                repo.maintenance.resolve_owner_profile(username)
                if username
                else None
            ) or admin
            token = set_request_user(owner)
            try:
                repo.get_notebook(notebook_id)
                sources = repo.list_sources(notebook_id)
                counts["sources"] += len(sources)
                types = repo.knowledge_types(notebook_id)
                counts["knowledge_types"] += len(types)
                if types:
                    page = repo.list_knowledge(
                        notebook_id, types[0].object_type, limit=5
                    )
                    counts["knowledge_rows"] += len(page.items)
                if isinstance(repo.unified_kg_status(notebook_id), dict):
                    counts["unified_status"] += 1
                conversations = repo.list_conversations(notebook_id)
                counts["conversations"] += len(conversations)
                if conversations:
                    detail = repo.get_conversation(conversations[0].id)
                    counts["answers"] += len(detail.turns)
                job_row = probe.execute(
                    "SELECT id FROM ask_jobs WHERE notebook_id=? "
                    "ORDER BY rowid DESC LIMIT 1",
                    (notebook_id,),
                ).fetchone()
                if job_row and repo.ask_job_detail(job_row[0]):
                    counts["ask_jobs"] += 1
                reports = repo.list_reports(notebook_id)
                counts["reports"] += len(reports)
                if reports:
                    repo.get_report(notebook_id, reports[0]["id"])
                title = sources[0].title if sources else ""
                token_text = next(
                    (part for part in re.split(r"\s+", title) if len(part) >= 2),
                    title[:8],
                )
                needle = token_text[:12]
                if needle:
                    counts["search_hits"] += len(
                        repo.search_notebook(notebook_id, needle).hits
                    )
            finally:
                reset_request_user(token)
    finally:
        probe.close()
    return counts


# ---------------------------------------------------------------------------
# Storage / source metadata guards (stat only — file contents are never read)
# ---------------------------------------------------------------------------

def storage_manifest(storage: Path) -> List[Tuple[str, int, int]]:
    return sorted(
        (
            str(path.relative_to(storage)),
            path.stat().st_size,
            path.stat().st_mtime_ns,
        )
        for path in storage.rglob("*")
        if path.is_file()
    )


def _source_read_uri(database: Path) -> str:
    """Read-only URI for the original database.

    A WAL-flagged database opened via plain ``mode=ro`` still makes SQLite
    create 0-byte ``-wal``/``-shm`` sidecars next to the ORIGINAL file.  When
    neither sidecar exists there is no live connection and no WAL content to
    read, so ``immutable=1`` gives a byte-pure read with zero filesystem side
    effects.  When sidecars exist (live database or copied WAL content) the
    plain ``mode=ro`` path is required so committed WAL rows are visible; it
    creates no new files because the sidecars are already there.
    """
    wal = Path(f"{database}-wal")
    shm = Path(f"{database}-shm")
    if wal.exists() or shm.exists():
        return f"file:{database.as_posix()}?mode=ro"
    return f"file:{database.as_posix()}?mode=ro&immutable=1"


def _source_metadata(database: Path) -> List[Tuple[str, int, int]]:
    """size/mtime of the database and its -wal sidecar.  The -shm file is a
    rebuildable lock/index support file whose mtime legitimately moves when a
    read-only reader attaches to a live WAL database, so only its existence is
    tracked (it carries no persistent data)."""
    entries: List[Tuple[str, int, int]] = []
    for candidate in (database, Path(f"{database}-wal")):
        if candidate.exists():
            stat = candidate.stat()
            entries.append((candidate.name, stat.st_size, stat.st_mtime_ns))
    entries.append((f"{database.name}-shm", int(Path(f"{database}-shm").exists()), 0))
    return entries


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_snapshot(database: Path, storage_dir: Path) -> VerificationResult:
    import time as _time

    started = _time.monotonic()
    database = Path(database)
    storage_dir = Path(storage_dir)
    if not database.is_file():
        raise _fail(f"missing database: {database}")
    if not storage_dir.is_dir():
        raise _fail(f"missing storage dir: {storage_dir}")

    storage_before = storage_manifest(storage_dir)
    source_before = _source_metadata(database)

    temp_root = Path(tempfile.mkdtemp(prefix="repository-snapshot-"))
    try:
        backup_path = temp_root / "backup.db"
        temp_storage = temp_root / "storage"
        temp_storage.mkdir()

        source_conn = sqlite3.connect(_source_read_uri(database), uri=True)
        try:
            backup_conn = sqlite3.connect(backup_path)
            try:
                source_conn.backup(backup_conn)
            finally:
                backup_conn.close()
        finally:
            source_conn.close()

        pre = snapshot_database(backup_path)
        if pre.user_version > SCHEMA_VERSION:
            raise _fail(
                f"database user_version={pre.user_version} is newer than "
                f"SCHEMA_VERSION={SCHEMA_VERSION}"
            )

        settings = offline_settings(backup_path, temp_storage)
        repo_db = Path(settings.sqlite_path).resolve()
        repo_storage = Path(settings.storage_dir)
        original_db = database.resolve()
        original_storage = storage_dir.resolve()
        if repo_db == original_db or original_db in repo_db.parents:
            raise _fail("repository would be constructed with the original database")
        if repo_storage.is_symlink():
            raise _fail("repository storage directory must not be a symlink")
        resolved_storage = repo_storage.resolve()
        if (
            resolved_storage == original_storage
            or original_storage in resolved_storage.parents
        ):
            raise _fail("repository would be constructed with the original storage")

        with _no_network():
            repo = SQLiteRepository(settings)
            reads = exercise_reads(repo, backup_path)

        column_plan = {
            name: table.column_names for name, table in pre.tables.items()
        }
        post = snapshot_database(backup_path, column_plan=column_plan)
        changed_tables, discrepancies, migration_added, normalized = (
            compare_snapshots(pre, post)
        )
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

    storage_after = storage_manifest(storage_dir)
    source_after = _source_metadata(database)
    storage_unchanged = storage_after == storage_before
    source_unchanged = source_after == source_before
    if not storage_unchanged:
        discrepancies.append("original-storage-changed")
    if not source_unchanged:
        discrepancies.append("original-database-metadata-changed")

    return VerificationResult(
        ok=not changed_tables and not discrepancies,
        source_user_version=pre.user_version,
        final_user_version=post.user_version,
        table_counts={name: t.row_count for name, t in pre.tables.items()},
        table_digests={name: t.digest for name, t in pre.tables.items()},
        changed_tables=changed_tables,
        discrepancies=discrepancies,
        migration_added_tables=migration_added,
        normalized=normalized,
        reads=reads,
        storage_unchanged=storage_unchanged,
        source_unchanged=source_unchanged,
        storage_file_count=len(storage_before),
        database=str(database),
        duration_seconds=_time.monotonic() - started,
    )


def _print_report(result: VerificationResult) -> None:
    echo = print
    echo(
        "repository-snapshot: database "
        f"user_version=v{result.source_user_version} "
        f"final_user_version=v{result.final_user_version} "
        f"tables={len(result.table_counts)} "
        f"duration_s={result.duration_seconds:.1f}"
    )
    for name in sorted(result.table_counts):
        digest = result.table_digests.get(name, "")
        echo(
            f"repository-snapshot: table name={name} "
            f"rows={result.table_counts[name]} "
            f"digest={digest[:16] if digest else 'virtual'}"
        )
    if result.migration_added_tables:
        echo(
            "repository-snapshot: migration_added="
            + ",".join(result.migration_added_tables)
        )
    echo(
        "repository-snapshot: normalized "
        + " ".join(f"{key}={value}" for key, value in sorted(result.normalized.items()))
    )
    echo(
        "repository-snapshot: reads "
        + " ".join(f"{key}={value}" for key, value in sorted(result.reads.items()))
    )
    echo(
        "repository-snapshot: storage "
        f"files={result.storage_file_count} "
        f"unchanged={str(result.storage_unchanged).lower()} "
        f"source_unchanged={str(result.source_unchanged).lower()}"
    )
    for line in result.discrepancies:
        echo(f"repository-snapshot: changed {line}")
    status = "PASS" if result.ok else "FAIL"
    echo(
        f"repository-snapshot: {status} "
        f"schema=v{result.source_user_version} "
        f"changed_tables={len(result.changed_tables)}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify (backup-only) that the current repository opens a "
            "pre-refactor SQLite database without changing it."
        )
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--storage-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    result = verify_snapshot(args.database, args.storage_dir)
    _print_report(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
