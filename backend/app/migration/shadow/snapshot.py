"""Atomic, identity-bound SQLite snapshots for forward shadow migration."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.migration.shadow.capture import validate_sqlite_capture
from app.migration.shadow.identity import fingerprint_sqlite
from app.migration.shadow.manifest import MANIFEST, validate_sqlite_schema
from app.migration.shadow.types import Manifest
from app.repositories.sqlite.database import SqliteDatabase


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    run_id: str
    source_identity_hash: str
    schema_epoch: int
    baseline_seq: int
    size_bytes: int
    sha256: str


class SnapshotCancelled(RuntimeError):
    """Raised when an operator cancellation interrupts SQLite backup."""


@dataclass(frozen=True)
class LiveSqlitePathBinding:
    configured_path: Path
    resolved_path: Path
    device: int
    inode: int


def _live_sqlite_path_binding(source: SqliteDatabase) -> LiveSqlitePathBinding:
    configured = Path(source.db_path).absolute()
    try:
        resolved = configured.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise ValueError("live SQLite source path is unavailable") from None
    if not resolved.is_file():
        raise ValueError("live SQLite source must be a regular file")
    return LiveSqlitePathBinding(
        configured_path=configured,
        resolved_path=resolved,
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
    )


def assert_live_sqlite_path(
    source: SqliteDatabase,
    expected: LiveSqlitePathBinding,
) -> None:
    if _live_sqlite_path_binding(source) != expected:
        raise ValueError("live SQLite source path changed during shadow operation")


def open_fresh_live_sqlite(
    source: SqliteDatabase,
) -> tuple[sqlite3.Connection, LiveSqlitePathBinding]:
    """Open the file currently named by ``source.db_path``, never its TLS cache.

    This is a cooperative-operations fence, not protection against a hostile
    process continuously swapping pathnames. We require the resolved path and
    inode to remain stable across open, after transaction validation, and as
    close as possible to publication/commit. An atomic replacement outside
    those checks is an unsupported operator race and is caught by the next
    required path check.
    """
    before = _live_sqlite_path_binding(source)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            before.resolved_path.as_uri() + "?mode=rw",
            uri=True,
            timeout=source.settings.db_busy_timeout_ms / 1000,
            check_same_thread=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            f"PRAGMA busy_timeout = {int(source.settings.db_busy_timeout_ms)}"
        )
        after = _live_sqlite_path_binding(source)
        if after != before:
            raise ValueError("live SQLite source path changed while opening")
        main = [
            row
            for row in connection.execute("PRAGMA database_list").fetchall()
            if str(_row_value(row, "name", 1)) == "main"
        ]
        if len(main) != 1:
            raise ValueError("live SQLite connection has no unique main database")
        try:
            connected = Path(str(_row_value(main[0], "file", 2))).resolve(strict=True)
        except OSError:
            raise ValueError("live SQLite connection path is unavailable") from None
        if connected != before.resolved_path:
            raise ValueError("live SQLite connection opened a different path")
        return connection, before
    except BaseException:
        if connection is not None:
            connection.close()
        raise


def _cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise SnapshotCancelled("SQLite snapshot was cancelled")


def _sha256_file(path: Path, cancel_requested: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            _cancelled(cancel_requested)
            digest.update(chunk)
    return digest.hexdigest()


def _row_value(row: Any, name: str, position: int) -> Any:
    if hasattr(row, "keys"):
        return row[name]
    return row[position]


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
            raise SnapshotCancelled("SQLite snapshot was cancelled") from None
        raise
    finally:
        conn.set_progress_handler(None, 0)


def _integrity_check(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("PRAGMA integrity_check").fetchall()


def _change_log_high_water(conn: sqlite3.Connection) -> int:
    """Return the global allocated change-log high water, including retention."""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0) AS row_high_water,"
        "COALESCE((SELECT seq FROM sqlite_sequence "
        "WHERE name='shadow_change_log'),0) AS allocated_high_water "
        "FROM shadow_change_log"
    ).fetchone()
    return max(
        int(_row_value(row, "row_high_water", 0)),
        int(_row_value(row, "allocated_high_water", 1)),
    )


def _validate_snapshot_file(
    path: Path,
    *,
    run_id: str,
    manifest: Manifest,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        _cancelled(cancel_requested)
        with _cancellable_validation(conn, cancel_requested):
            integrity = _integrity_check(conn)
            if [str(_row_value(row, "integrity_check", 0)) for row in integrity] != [
                "ok"
            ]:
                raise ValueError("SQLite snapshot integrity validation failed")
            _cancelled(cancel_requested)
            validate_sqlite_schema(conn, manifest, expected_pair=manifest.schema_pair)
        _cancelled(cancel_requested)
        validate_sqlite_capture(conn, manifest)
        control = conn.execute(
            "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
            "FROM shadow_capture_control WHERE singleton=1"
        ).fetchone()
        if control is None or tuple(control) != (
            1,
            0,
            0,
            run_id,
            manifest.schema_pair.epoch,
        ):
            raise ValueError(
                "SQLite snapshot does not contain the expected capture run"
            )
        _cancelled(cancel_requested)
        # seq is global AUTOINCREMENT protocol state. Include old runs and the
        # sqlite_sequence allocation after retention so the next event is
        # exactly H0+1 even when the current run has no rows.
        return _change_log_high_water(conn)
    finally:
        conn.close()


def create_snapshot(
    source: SqliteDatabase,
    destination: Path,
    *,
    run_id: str,
    manifest: Manifest = MANIFEST,
    cancel_requested: Callable[[], bool] | None = None,
    progress: Callable[[int, int, int], None] | None = None,
    pages: int = 256,
    sleep: float = 0.01,
) -> SnapshotInfo:
    """Publish a consistent backup only after full run/schema validation.

    ``destination`` is the final, run-specific pathname. Backup writes to an
    exclusively-created private sibling and publishes with an atomic hard link.
    Existing destinations are never overwritten, even by a retry of the same
    run; callers must reuse the already recorded :class:`SnapshotInfo`.
    """
    if not run_id or run_id.strip() != run_id:
        raise ValueError("run_id must be a non-empty canonical string")
    if isinstance(pages, bool) or not isinstance(pages, int) or pages <= 0:
        raise ValueError("snapshot backup pages must be a positive integer")
    final_path = Path(destination).absolute()
    parent = final_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError:
        pass
    parent_stat = parent.lstat()
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("shadow snapshot directory must be a real directory")
    if (
        parent_stat.st_uid != os.geteuid()
        or parent_stat.st_mode & 0o077
        or parent_stat.st_mode & 0o700 != 0o700
    ):
        raise ValueError("shadow snapshot directory must be owner-only")
    if os.path.lexists(final_path):
        raise FileExistsError("shadow snapshot destination already exists")
    # The operator-supplied run id is identity data, not a pathname fragment.
    temporary = final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")
    temporary_fd = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_RDWR,
        0o600,
    )
    os.close(temporary_fd)
    destination_conn: sqlite3.Connection | None = None
    live_conn: sqlite3.Connection | None = None
    published = False
    result: SnapshotInfo | None = None
    try:
        source_url = source.settings.database_url
        live_conn, live_binding = open_fresh_live_sqlite(source)
        source_identity = fingerprint_sqlite(source_url, live_conn)
        with _cancellable_validation(live_conn, cancel_requested):
            validate_sqlite_schema(
                live_conn, manifest, expected_pair=manifest.schema_pair
            )
            validate_sqlite_capture(live_conn, manifest)
            live_control = live_conn.execute(
                "SELECT enabled,write_frozen,apply_active,run_id,schema_epoch "
                "FROM shadow_capture_control WHERE singleton=1"
            ).fetchone()
        if live_control is None or tuple(live_control) != (
            1,
            0,
            0,
            run_id,
            manifest.schema_pair.epoch,
        ):
            raise ValueError("live SQLite capture run is not enabled and safe")
        assert_live_sqlite_path(source, live_binding)
        destination_conn = sqlite3.connect(temporary)

        def on_progress(status: int, remaining: int, total: int) -> None:
            if cancel_requested is not None and cancel_requested():
                raise SnapshotCancelled("SQLite snapshot was cancelled")
            if progress is not None:
                progress(status, remaining, total)

        # SQLite's backup API holds only short source read locks between page
        # batches, so WAL writers can continue while it produces one coherent
        # destination image.
        live_conn.backup(
            destination_conn,
            pages=pages,
            progress=on_progress,
            sleep=sleep,
        )
        # A pathname replacement after validation can leave this connection on
        # the old inode. Refuse publication unless the current path is still
        # the exact file opened above.
        assert_live_sqlite_path(source, live_binding)
        destination_conn.close()
        destination_conn = None

        baseline_seq = _validate_snapshot_file(
            temporary,
            run_id=run_id,
            manifest=manifest,
            cancel_requested=cancel_requested,
        )
        _cancelled(cancel_requested)
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        size_bytes = temporary.stat().st_size
        digest = _sha256_file(temporary, cancel_requested)
        _cancelled(cancel_requested)
        # Recheck again immediately before atomic publication. POSIX still has
        # an unavoidable instruction-sized check/link window; shadow migration
        # therefore requires cooperative operators not to replace the live DB.
        assert_live_sqlite_path(source, live_binding)
        # Hard-link publication is atomic and fails if *anything* already owns
        # the final pathname. Unlike os.replace it has no check/use overwrite
        # race. The temporary and final names are in one directory/filesystem.
        try:
            os.link(temporary, final_path, follow_symlinks=False)
        except FileExistsError:
            raise FileExistsError(
                "shadow snapshot destination already exists"
            ) from None
        published = True
        live_conn.close()
        live_conn = None
        result = SnapshotInfo(
            path=final_path,
            run_id=run_id,
            source_identity_hash=source_identity.identity_hash,
            schema_epoch=manifest.schema_pair.epoch,
            baseline_seq=baseline_seq,
            size_bytes=size_bytes,
            sha256=digest,
        )
        try:
            temporary.unlink()
        except OSError:
            # The final link is already a fully validated immutable result.
            # A cleanup retry happens below; a stale private sibling must not
            # make callers discard a successfully published snapshot.
            pass
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return result
    except BaseException:
        if live_conn is not None:
            live_conn.close()
        if destination_conn is not None:
            destination_conn.close()
        if published:
            try:
                final_path.unlink()
                directory_fd = os.open(parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                published = False
            except OSError:
                # A validated final path is a recoverable successful publish;
                # never report failure while leaving that path behind.
                if result is not None and final_path.is_file():
                    if (
                        final_path.stat().st_size == result.size_bytes
                        and _sha256_file(final_path) == result.sha256
                    ):
                        # The first directory fsync failed and rollback could
                        # not remove the final link.  A successful retry is the
                        # only durability evidence that permits reporting this
                        # already-validated result as published.
                        directory_fd = os.open(parent, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                        return result
                raise
        raise
    finally:
        if live_conn is not None:
            live_conn.close()
        try:
            temporary.unlink()
        except OSError:
            pass
