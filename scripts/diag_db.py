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
import stat
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import diag_common


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
MAX_BASE_RECALL_MOUNTS = 64
MAX_BASE_RECALL_REFERENCES = 256
MAX_BASE_RECALL_JSON_BYTES = 256 * 1024
MAX_DEADLINE_SECONDS = 10.0
MAX_BUSY_TIMEOUT_MS = 1000
MAX_STALE_WORKSPACES = 8
MAX_DIAGNOSTICS_ENTRIES = 64
MAX_WORKSPACE_ENTRIES = 8
STALE_WORKSPACE_AGE_SECONDS = 24 * 60 * 60

_NOATIME_FLAG = getattr(os, "O_NOATIME", None)
_NOATIME_AVAILABLE = _NOATIME_FLAG is not None
_NOFOLLOW_FLAG = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK_FLAG = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC_FLAG = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAG = getattr(os, "O_DIRECTORY", 0)
_ALLOW_UNSAFE_WORKSPACE_PATH_FOR_TESTS = False
_NOATIME_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EACCES,
    errno.EINVAL,
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    getattr(errno, "ENOTSUP", errno.EINVAL),
}

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
    "source_file_plan",
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


class _UnsafeFile(Exception):
    pass


class _NoAtimeUnavailable(Exception):
    pass


class _UnsafeDiagnostics(Exception):
    pass


class _PinnedSource:
    def __init__(self, name: str, path: Path, descriptor: int, identity) -> None:
        self.name = name
        self.path = path
        self.descriptor = descriptor
        self.identity = identity


class _PinnedDirectory:
    def __init__(self, path: Path, descriptor: int, identity) -> None:
        self.path = path
        self.descriptor = descriptor
        self.identity = identity


class _SnapshotWorkspace(_PinnedDirectory):
    def __init__(
        self,
        path: Path,
        descriptor: int,
        identity,
        name: str,
        lock_descriptor: int,
    ) -> None:
        super().__init__(path, descriptor, identity)
        self.name = name
        self.lock_descriptor = lock_descriptor


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
        "source_file_plan": [],
        "relevant_scans": [],
        "base_recall": {},
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
    if isinstance(exc, FileNotFoundError):
        return "missing"
    if isinstance(exc, PermissionError):
        return "permission"
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


def _stat_identity(metadata) -> Tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _path_identity(path: Path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return None
    return _stat_identity(metadata)


def _descriptor_identity(descriptor: int) -> Tuple[int, int, int, int, int]:
    return _stat_identity(os.fstat(descriptor))


def _source_paths(path: Path) -> Dict[str, Path]:
    return {
        "database": path,
        "wal": Path(str(path) + "-wal"),
        "shm": Path(str(path) + "-shm"),
        "journal": Path(str(path) + "-journal"),
    }


def _capture_source_state(paths: Dict[str, Path]) -> Dict[str, Any]:
    return {name: _path_identity(path) for name, path in paths.items()}


def _validate_source_state(paths: Dict[str, Path]) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    for name, path in paths.items():
        try:
            metadata = os.lstat(path)
        except FileNotFoundError:
            if name == "database":
                raise
            state[name] = None
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise _UnsafeFile()
        state[name] = _stat_identity(metadata)
    return state


def _set_file_evidence(evidence: Dict[str, Any], state) -> None:
    evidence["files"] = {
        "database_bytes": int((state.get("database") or (0, 0, 0, 0, 0))[2]),
        "wal_bytes": int((state.get("wal") or (0, 0, 0, 0, 0))[2]),
        "shm_bytes": int((state.get("shm") or (0, 0, 0, 0, 0))[2]),
    }


def _pin_source_files(
    paths: Dict[str, Path], state: Dict[str, Any]
) -> Dict[str, _PinnedSource]:
    if not _NOATIME_AVAILABLE or _NOATIME_FLAG is None:
        raise _NoAtimeUnavailable()
    flags = (
        os.O_RDONLY
        | _NOATIME_FLAG
        | _NOFOLLOW_FLAG
        | _NONBLOCK_FLAG
        | _CLOEXEC_FLAG
    )
    pinned: Dict[str, _PinnedSource] = {}
    try:
        for name, path in paths.items():
            expected = state.get(name)
            if expected is None:
                continue
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                if exc.errno in _NOATIME_UNSUPPORTED_ERRNOS and _NOATIME_FLAG:
                    raise _NoAtimeUnavailable() from exc
                if exc.errno in {errno.ELOOP, errno.ENXIO}:
                    raise _UnsafeFile() from exc
                raise
            try:
                metadata = os.fstat(descriptor)
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            identity = _stat_identity(metadata)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(descriptor)
                raise _UnsafeFile()
            if identity != expected:
                os.close(descriptor)
                raise RuntimeError("source-changed")
            pinned[name] = _PinnedSource(name, path, descriptor, identity)
    except BaseException:
        for source in pinned.values():
            try:
                os.close(source.descriptor)
            except OSError:
                pass
        raise
    return pinned


def _close_pinned_sources(
    pinned: Dict[str, _PinnedSource], evidence: Optional[Dict[str, Any]] = None
) -> None:
    for source in pinned.values():
        try:
            os.close(source.descriptor)
        except OSError as exc:
            if evidence is not None:
                _append_degraded(evidence, "source.close", "cleanup_error", exc)


def _pinned_sources_unchanged(pinned: Dict[str, _PinnedSource]) -> bool:
    try:
        return all(
            _descriptor_identity(source.descriptor) == source.identity
            and stat.S_ISREG(os.fstat(source.descriptor).st_mode)
            for source in pinned.values()
        )
    except OSError:
        return False


def _acquire_source_read_lock(descriptor: int) -> None:
    try:
        fcntl.lockf(
            descriptor,
            fcntl.LOCK_SH | fcntl.LOCK_NB,
            1,
            _SHARED_FIRST,
            os.SEEK_SET,
        )
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            raise _SourceLocked() from exc
        raise


def _release_source_read_lock(descriptor: int) -> None:
    fcntl.lockf(descriptor, fcntl.LOCK_UN, 1, _SHARED_FIRST, os.SEEK_SET)


def _safe_release_source_read_lock(
    evidence: Dict[str, Any], descriptor: int
) -> None:
    try:
        _release_source_read_lock(descriptor)
    except OSError as exc:
        _append_degraded(evidence, "source.unlock", "cleanup_error", exc)


def _copy_pinned_file(
    source: _PinnedSource, writer, deadline: float, byte_limit: int
) -> int:
    copied = 0
    if source.identity[2] > byte_limit:
        raise ValueError("snapshot-byte-limit")
    os.lseek(source.descriptor, 0, os.SEEK_SET)
    while True:
        _require_deadline(deadline)
        chunk = os.read(source.descriptor, COPY_CHUNK_BYTES)
        if not chunk:
            break
        if copied + len(chunk) > byte_limit:
            raise ValueError("snapshot-byte-limit")
        writer.write(chunk)
        copied += len(chunk)
    return copied


def _directory_identity(metadata) -> Tuple[int, int]:
    return (int(metadata.st_dev), int(metadata.st_ino))


def _valid_directory(metadata, *, private: bool) -> bool:
    permissions = stat.S_IMODE(metadata.st_mode)
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and (permissions & 0o077 == 0 if private else permissions & 0o022 == 0)
    )


def _open_verified_directory(
    path: Path, metadata, *, private: bool, dir_fd: Optional[int] = None
) -> _PinnedDirectory:
    flags = os.O_RDONLY | _DIRECTORY_FLAG | _NOFOLLOW_FLAG | _CLOEXEC_FLAG
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, flags, dir_fd=dir_fd)
        descriptor_metadata = os.fstat(descriptor)
        if (
            not _valid_directory(descriptor_metadata, private=private)
            or _directory_identity(descriptor_metadata) != _directory_identity(metadata)
        ):
            raise _UnsafeDiagnostics()
        return _PinnedDirectory(path, descriptor, _directory_identity(metadata))
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _open_diagnostics_directory(parent: Path) -> _PinnedDirectory:
    directory = parent / "diagnostics"
    try:
        metadata = os.lstat(directory)
    except FileNotFoundError:
        try:
            # This parent is shared with the backend heartbeat writer, whose
            # fail-closed runtime contract requires an exact owner-only mode.
            os.mkdir(directory, mode=0o700)
        except FileExistsError:
            pass
        metadata = os.lstat(directory)
    if not _valid_directory(metadata, private=False):
        raise _UnsafeDiagnostics()
    try:
        return _open_verified_directory(directory, metadata, private=False)
    except OSError as exc:
        raise _UnsafeDiagnostics() from exc


def _open_snapshot_root(diagnostics: _PinnedDirectory) -> _PinnedDirectory:
    name = "db-snapshots"
    path = diagnostics.path / name
    try:
        os.mkdir(name, mode=0o700, dir_fd=diagnostics.descriptor)
    except FileExistsError:
        pass
    try:
        metadata = os.stat(
            name, dir_fd=diagnostics.descriptor, follow_symlinks=False
        )
    except OSError as exc:
        raise _UnsafeDiagnostics() from exc
    if not _valid_directory(metadata, private=True):
        raise _UnsafeDiagnostics()
    try:
        result = _open_verified_directory(
            Path(name),
            metadata,
            private=True,
            dir_fd=diagnostics.descriptor,
        )
        result.path = path
        return result
    except OSError as exc:
        raise _UnsafeDiagnostics() from exc


def _directory_path_unchanged(
    directory: _PinnedDirectory, *, private: bool
) -> bool:
    try:
        path_metadata = os.lstat(directory.path)
        descriptor_metadata = os.fstat(directory.descriptor)
    except OSError:
        return False
    return (
        _valid_directory(path_metadata, private=private)
        and _valid_directory(descriptor_metadata, private=private)
        and _directory_identity(path_metadata) == directory.identity
        and _directory_identity(descriptor_metadata) == directory.identity
    )


def _create_snapshot_workspace(root: _PinnedDirectory) -> _SnapshotWorkspace:
    flags = os.O_RDONLY | _DIRECTORY_FLAG | _NOFOLLOW_FLAG | _CLOEXEC_FLAG
    for attempt in range(8):
        name = f"diag-db-{os.getpid():x}-{time.monotonic_ns():x}-{attempt}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root.descriptor)
        except FileExistsError:
            continue
        descriptor: Optional[int] = None
        lock_descriptor: Optional[int] = None
        try:
            metadata = os.stat(name, dir_fd=root.descriptor, follow_symlinks=False)
            if not _valid_directory(metadata, private=True):
                raise _UnsafeDiagnostics()
            descriptor = os.open(name, flags, dir_fd=root.descriptor)
            descriptor_metadata = os.fstat(descriptor)
            if (
                not _valid_directory(descriptor_metadata, private=True)
                or _directory_identity(descriptor_metadata)
                != _directory_identity(metadata)
            ):
                raise _UnsafeDiagnostics()
            lock_flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | _NOFOLLOW_FLAG
                | _CLOEXEC_FLAG
                | _NONBLOCK_FLAG
            )
            lock_descriptor = os.open(
                ".active.lock", lock_flags, 0o600, dir_fd=descriptor
            )
            lock_metadata = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.getuid()
                or lock_metadata.st_nlink != 1
            ):
                raise _UnsafeDiagnostics()
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _SnapshotWorkspace(
                root.path / name,
                descriptor,
                _directory_identity(metadata),
                name,
                lock_descriptor,
            )
        except BaseException:
            try:
                if descriptor is not None:
                    os.unlink(".active.lock", dir_fd=descriptor)
            except (OSError, TypeError):
                pass
            for opened in (lock_descriptor, descriptor):
                if opened is not None:
                    try:
                        os.close(opened)
                    except OSError:
                        pass
            try:
                os.rmdir(name, dir_fd=root.descriptor)
            except OSError:
                pass
            raise
    raise _UnsafeDiagnostics()


def _copy_workspace_artifact(
    source: _PinnedSource,
    workspace: _SnapshotWorkspace,
    name: str,
    deadline: float,
    byte_limit: int,
    evidence: Dict[str, Any],
) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW_FLAG | _CLOEXEC_FLAG
    descriptor: Optional[int] = None
    writer = None
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=workspace.descriptor)
        writer = os.fdopen(descriptor, "wb")
        descriptor = None
        metadata = os.fstat(writer.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise _UnsafeDiagnostics()
        return _copy_pinned_file(source, writer, deadline, byte_limit)
    finally:
        if writer is not None:
            try:
                writer.close()
            except OSError as exc:
                _append_degraded(evidence, "snapshot.copy_close", "cleanup_error", exc)
        elif descriptor is not None:
            _safe_close_descriptor(evidence, descriptor, "snapshot.copy_close")


def _workspace_sqlite_path(workspace: _SnapshotWorkspace) -> Path:
    proc_directory = Path("/proc/self/fd") / str(workspace.descriptor)
    try:
        proc_metadata = os.stat(proc_directory)
        descriptor_metadata = os.fstat(workspace.descriptor)
        if (
            _valid_directory(proc_metadata, private=True)
            and _directory_identity(proc_metadata)
            == _directory_identity(descriptor_metadata)
            == workspace.identity
        ):
            return proc_directory / "snapshot.db"
    except OSError:
        pass
    if _ALLOW_UNSAFE_WORKSPACE_PATH_FOR_TESTS and _directory_path_unchanged(
        workspace, private=True
    ):
        return workspace.path / "snapshot.db"
    raise _UnsafeDiagnostics()


def _safe_close_descriptor(
    evidence: Dict[str, Any], descriptor: Optional[int], probe: str
) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError as exc:
        _append_degraded(evidence, probe, "cleanup_error", exc)


def _cleanup_snapshot_workspace(
    root: _PinnedDirectory,
    workspace: _SnapshotWorkspace,
    evidence: Dict[str, Any],
) -> None:
    allowed = (
        "snapshot.db",
        "snapshot.db-wal",
        "snapshot.db-shm",
        "snapshot.db-journal",
    )
    for name in allowed:
        try:
            metadata = os.stat(
                name, dir_fd=workspace.descriptor, follow_symlinks=False
            )
        except FileNotFoundError:
            continue
        except OSError as exc:
            _append_degraded(evidence, "snapshot.cleanup", "cleanup_error", exc)
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            _append_degraded(
                evidence, "snapshot.cleanup", "unsafe_cleanup_entry", RuntimeError()
            )
            continue
        try:
            os.unlink(name, dir_fd=workspace.descriptor)
        except OSError as exc:
            _append_degraded(evidence, "snapshot.cleanup", "cleanup_error", exc)
    try:
        os.unlink(".active.lock", dir_fd=workspace.descriptor)
    except OSError as exc:
        _append_degraded(evidence, "snapshot.cleanup", "cleanup_error", exc)
    try:
        current = os.stat(
            workspace.name, dir_fd=root.descriptor, follow_symlinks=False
        )
        if (
            not _valid_directory(current, private=True)
            or _directory_identity(current) != workspace.identity
        ):
            raise _UnsafeDiagnostics()
        os.rmdir(workspace.name, dir_fd=root.descriptor)
    except _UnsafeDiagnostics as exc:
        _append_degraded(
            evidence, "snapshot.cleanup", "unsafe_cleanup_entry", exc
        )
    except OSError as exc:
        _append_degraded(evidence, "snapshot.cleanup", "cleanup_error", exc)
    try:
        fcntl.flock(workspace.lock_descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        _append_degraded(evidence, "snapshot.unlock", "cleanup_error", exc)
    _safe_close_descriptor(evidence, workspace.lock_descriptor, "snapshot.lock_close")
    _safe_close_descriptor(evidence, workspace.descriptor, "snapshot.directory_close")


def _cleanup_stale_workspaces(
    root: _PinnedDirectory, evidence: Dict[str, Any], deadline: float
) -> None:
    """Bounded cleanup of old inactive workspaces through pinned descriptors."""
    now_ns = time.time_ns()
    inspected = 0
    total = 0
    try:
        iterator = os.scandir(root.descriptor)
    except OSError as exc:
        _append_degraded(evidence, "snapshot.stale_cleanup", "cleanup_error", exc)
        return
    with iterator:
        for entry in iterator:
            if time.monotonic() >= deadline:
                _append_degraded(
                    evidence,
                    "snapshot.stale_cleanup",
                    "deadline",
                    _DeadlineExceeded(),
                )
                return
            total += 1
            if total > MAX_DIAGNOSTICS_ENTRIES:
                _append_degraded(
                    evidence,
                    "snapshot.stale_cleanup",
                    "cleanup_entry_limit",
                    RuntimeError(),
                )
                return
            name = entry.name
            if not name.startswith("diag-db-"):
                continue
            if inspected >= MAX_STALE_WORKSPACES:
                _append_degraded(
                    evidence,
                    "snapshot.stale_cleanup",
                    "cleanup_workspace_limit",
                    RuntimeError(),
                )
                return
            inspected += 1
            child: Optional[int] = None
            marker: Optional[int] = None
            marker_locked = False
            try:
                metadata = os.stat(
                    name, dir_fd=root.descriptor, follow_symlinks=False
                )
                if (
                    not _valid_directory(metadata, private=True)
                    or now_ns - metadata.st_mtime_ns
                    < STALE_WORKSPACE_AGE_SECONDS * 1_000_000_000
                ):
                    continue
                flags = os.O_RDONLY | _DIRECTORY_FLAG | _NOFOLLOW_FLAG | _CLOEXEC_FLAG
                child = os.open(name, flags, dir_fd=root.descriptor)
                child_metadata = os.fstat(child)
                if (
                    not _valid_directory(child_metadata, private=True)
                    or _directory_identity(child_metadata)
                    != _directory_identity(metadata)
                ):
                    raise _UnsafeDiagnostics()
                marker_flags = (
                    os.O_RDWR | _NOFOLLOW_FLAG | _NONBLOCK_FLAG | _CLOEXEC_FLAG
                )
                marker = os.open(".active.lock", marker_flags, dir_fd=child)
                marker_metadata = os.fstat(marker)
                if (
                    not stat.S_ISREG(marker_metadata.st_mode)
                    or marker_metadata.st_uid != os.getuid()
                    or marker_metadata.st_nlink != 1
                ):
                    raise _UnsafeDiagnostics()
                try:
                    fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    marker_locked = True
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        continue
                    raise
                entries: List[str] = []
                with os.scandir(child) as child_iterator:
                    for child_entry in child_iterator:
                        if time.monotonic() >= deadline:
                            raise _DeadlineExceeded()
                        entries.append(child_entry.name)
                        if len(entries) > MAX_WORKSPACE_ENTRIES:
                            raise _UnsafeDiagnostics()
                allowed = {
                    ".active.lock",
                    "snapshot.db",
                    "snapshot.db-wal",
                    "snapshot.db-shm",
                    "snapshot.db-journal",
                }
                if any(item not in allowed for item in entries):
                    raise _UnsafeDiagnostics()
                for item in entries:
                    item_metadata = os.stat(
                        item, dir_fd=child, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISREG(item_metadata.st_mode)
                        or item_metadata.st_uid != os.getuid()
                        or item_metadata.st_nlink != 1
                    ):
                        raise _UnsafeDiagnostics()
                for item in entries:
                    if item != ".active.lock":
                        os.unlink(item, dir_fd=child)
                os.unlink(".active.lock", dir_fd=child)
                current = os.stat(
                    name, dir_fd=root.descriptor, follow_symlinks=False
                )
                if (
                    not _valid_directory(current, private=True)
                    or _directory_identity(current) != _directory_identity(metadata)
                ):
                    raise _UnsafeDiagnostics()
                os.rmdir(name, dir_fd=root.descriptor)
            except _DeadlineExceeded as exc:
                _append_degraded(
                    evidence, "snapshot.stale_cleanup", "deadline", exc
                )
                return
            except (_UnsafeDiagnostics, OSError) as exc:
                _append_degraded(
                    evidence,
                    "snapshot.stale_cleanup",
                    "unsafe_cleanup_entry",
                    exc,
                )
                continue
            finally:
                if marker_locked and marker is not None:
                    try:
                        fcntl.flock(marker, fcntl.LOCK_UN)
                    except OSError as exc:
                        _append_degraded(
                            evidence,
                            "snapshot.stale_unlock",
                            "cleanup_error",
                            exc,
                        )
                _safe_close_descriptor(evidence, marker, "snapshot.stale_lock_close")
                _safe_close_descriptor(evidence, child, "snapshot.stale_directory_close")


def _open_read_only(
    path: Path, deadline: float, *, immutable_snapshot: bool = False
) -> sqlite3.Connection:
    _require_deadline(deadline)
    remaining = max(0.001, deadline - time.monotonic())
    timeout = min(1.0, remaining)
    busy_timeout_ms = max(1, min(MAX_BUSY_TIMEOUT_MS, int(remaining * 1000)))
    encoded = quote(str(path), safe="/")
    immutable = "&immutable=1" if immutable_snapshot else ""
    connection = sqlite3.connect(
        f"file:{encoded}?mode=ro{immutable}",
        uri=True,
        timeout=timeout,
    )
    try:
        connection.row_factory = sqlite3.Row
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 1024 * 1024)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.set_progress_handler(
            lambda: int(time.monotonic() >= deadline), 1000
        )
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass
        raise
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
    notebook_columns: Dict[str, Tuple[str, List[str]]] = {}
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
            if not covered:
                if len(evidence["missing_fk_indexes"]) < MAX_MISSING_INDEXES:
                    evidence["missing_fk_indexes"].append(
                        {
                            "table": display_table,
                            "columns": list(columns[:16]),
                            "references": reference,
                        }
                    )
                else:
                    _append_degraded(
                        evidence,
                        "schema.foreign_keys",
                        "missing_index_limit",
                        RuntimeError(),
                    )
            if raw_reference == "notebooks":
                if len(evidence["notebook_references"]) < MAX_NOTEBOOK_REFERENCES:
                    evidence["notebook_references"].append(
                        {
                            "table": display_table,
                            "columns": list(columns[:16]),
                            "indexed": covered,
                        }
                    )
                else:
                    _append_degraded(
                        evidence,
                        "schema.foreign_keys",
                        "notebook_reference_limit",
                        RuntimeError(),
                    )
                entry = notebook_columns.setdefault(raw_table, (display_table, []))
                for raw_column in raw_columns:
                    if raw_column not in entry[1]:
                        entry[1].append(raw_column)
    result = [
        (raw_table, display, tuple(columns))
        for raw_table, (display, columns) in notebook_columns.items()
    ]
    if len(result) > MAX_NOTEBOOK_REFERENCES:
        _append_degraded(
            evidence,
            "schema.foreign_keys",
            "notebook_reference_limit",
            RuntimeError(),
        )
    return result[:MAX_NOTEBOOK_REFERENCES]


def _count_table(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    raw_table: str,
    raw_columns: Sequence[str],
    notebook_id: str,
    deadline: float,
) -> Optional[int]:
    def read():
        predicate = " OR ".join(
            f"{_quote_identifier(raw_column)} = ?" for raw_column in raw_columns
        )
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(raw_table)} "
            f"WHERE {predicate}",
            tuple(notebook_id for _column in raw_columns),
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
    notebook_keys = list(notebook_keys)
    key_columns = {
        raw_table: columns for raw_table, _display, columns in notebook_keys
    }
    seen = set()
    for table in _EXPLICIT_NOTEBOOK_TABLES:
        if not _deadline_ok(evidence, "notebook.count", deadline):
            return
        if table not in table_names:
            continue
        count = _count_table(
            connection,
            evidence,
            table,
            key_columns.get(table, ("notebook_id",)),
            notebook_id,
            deadline,
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
        if not columns or raw_table in seen:
            continue
        count = _count_table(
            connection, evidence, raw_table, columns, notebook_id, deadline
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


def _collect_source_file_plan(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    table_display: Dict[str, str],
    deadline: float,
) -> None:
    parameter = notebook_id if notebook_id is not None else "{id}"

    def explain():
        cursor = connection.execute(
            "EXPLAIN QUERY PLAN SELECT file_path FROM sources WHERE notebook_id = ?",
            (parameter,),
        )
        return _fetch_limited(cursor, MAX_PLAN_ROWS, deadline)

    result = _safe_probe(evidence, "plan.source_file_select", deadline, explain)
    if result is None:
        return
    rows, truncated = result
    if truncated:
        _append_degraded(
            evidence, "plan.source_file_select", "row_limit", RuntimeError()
        )
    for row in rows:
        if not _deadline_ok(evidence, "plan.source_file_select", deadline):
            return
        evidence["source_file_plan"].append(
            {
                "probe": "source_file_select",
                "id": int(row[0]),
                "parent": int(row[1]),
                "detail": _safe_plan_detail(row[3], table_display, str(parameter)),
            }
        )


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


def _base_recall_empty() -> Dict[str, Any]:
    return {
        "selected": False,
        "selection": "none",
        "notebook": None,
        "notebook_count": 0,
        "public_base_count": 0,
        "mounts_total": 0,
        "mounts_active": 0,
        "mounts_inactive": 0,
        "active_kg_objects": 0,
        "active_embeddings": 0,
        "mounted_bases": [],
        "latest_report": {
            "exists": False,
            "references_total": 0,
            "base_tier_references": 0,
            "personal_tier_references": 0,
            "mounted_base_references": 0,
            "malformed_references": 0,
        },
    }


def _base_recall_table_columns(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    table: str,
    deadline: float,
) -> set[str]:
    def read():
        rows, truncated = _fetch_limited(
            connection.execute(f"PRAGMA table_info({_quote_identifier(table)})"),
            MAX_INDEX_COLUMNS,
            deadline,
        )
        if truncated:
            _append_degraded(
                evidence, "base_recall.schema", "row_limit", RuntimeError()
            )
        return {str(row[1]) for row in rows if len(row) > 1}

    return _safe_probe(evidence, "base_recall.schema", deadline, read) or set()


def _base_recall_count_map(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    table: str,
    notebook_ids: Sequence[str],
    deadline: float,
) -> Dict[str, int]:
    if not notebook_ids:
        return {}
    placeholders = ",".join("?" for _value in notebook_ids)

    def read():
        rows, truncated = _fetch_limited(
            connection.execute(
                f"SELECT notebook_id, COUNT(*) FROM {_quote_identifier(table)} "
                f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                tuple(notebook_ids),
            ),
            MAX_BASE_RECALL_MOUNTS + 1,
            deadline,
        )
        if truncated:
            _append_degraded(
                evidence, f"base_recall.{table}", "row_limit", RuntimeError()
            )
        return {str(row[0]): max(0, int(row[1] or 0)) for row in rows}

    return _safe_probe(evidence, f"base_recall.{table}", deadline, read) or {}


def _collect_base_recall_evidence(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    requested_notebook_id: Optional[str],
    table_names: set[str],
    deadline: float,
) -> None:
    """Collect only bounded aggregates from the already-owned DB snapshot."""

    recall = _base_recall_empty()
    evidence["base_recall"] = recall
    required_notebooks = {"id", "tier", "created_by", "status"}
    if "notebooks" not in table_names or not required_notebooks.issubset(
        _base_recall_table_columns(
            connection, evidence, "notebooks", deadline
        )
    ):
        _append_degraded(
            evidence, "base_recall.schema", "missing_schema", RuntimeError()
        )
        return

    def notebook_totals():
        return connection.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN tier='base' THEN 1 ELSE 0 END) "
            "FROM notebooks"
        ).fetchone()

    totals = _safe_probe(
        evidence, "base_recall.notebooks", deadline, notebook_totals
    )
    if totals is None:
        return
    recall["notebook_count"] = max(0, int(totals[0] or 0))
    recall["public_base_count"] = max(0, int(totals[1] or 0))

    selected_id: Optional[str] = None
    selection = "none"
    candidate = str(requested_notebook_id or "")
    if candidate and len(candidate.encode("utf-8", "replace")) <= 200:
        row = _safe_probe(
            evidence,
            "base_recall.selection",
            deadline,
            lambda: connection.execute(
                "SELECT id FROM notebooks WHERE id=?", (candidate,)
            ).fetchone(),
        )
        if row is not None:
            selected_id = str(row[0])
            selection = "operator"

    report_columns = (
        _base_recall_table_columns(connection, evidence, "reports", deadline)
        if "reports" in table_names
        else set()
    )
    required_reports = {
        "notebook_id",
        "references_json",
        "status",
        "created_at",
    }
    if selected_id is None and required_reports.issubset(report_columns):
        row = _safe_probe(
            evidence,
            "base_recall.selection",
            deadline,
            lambda: connection.execute(
                "SELECT notebook_id FROM reports WHERE status='done' "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone(),
        )
        if row is not None:
            selected_id = str(row[0])
            selection = "latest_report"
    if selected_id is None:
        row = _safe_probe(
            evidence,
            "base_recall.selection",
            deadline,
            lambda: connection.execute(
                f"SELECT id FROM notebooks WHERE tier!='base' AND {diag_common.NOTEBOOK_LIVE_SQL} "
                "ORDER BY id LIMIT 1"
            ).fetchone(),
        )
        if row is not None:
            selected_id = str(row[0])
            selection = "first_personal"
    if selected_id is None:
        row = _safe_probe(
            evidence,
            "base_recall.selection",
            deadline,
            lambda: connection.execute(
                f"SELECT id FROM notebooks WHERE {diag_common.NOTEBOOK_LIVE_SQL} "
                "ORDER BY id LIMIT 1"
            ).fetchone(),
        )
        if row is not None:
            selected_id = str(row[0])
            selection = "first"
    if selected_id is None:
        return
    recall["selected"] = True
    recall["selection"] = selection
    recall["notebook"] = "notebook#1"

    count_tables = {}
    for table in (
        "knowledge_objects",
        "knowledge_embeddings",
        "chunks",
        "knowledge_relations",
    ):
        if table in table_names and "notebook_id" in _base_recall_table_columns(
            connection, evidence, table, deadline
        ):
            count_tables[table] = True
    active_counts = {
        table: _base_recall_count_map(
            connection, evidence, table, (selected_id,), deadline
        ).get(selected_id, 0)
        for table in count_tables
    }
    recall["active_kg_objects"] = active_counts.get("knowledge_objects", 0)
    recall["active_embeddings"] = active_counts.get("knowledge_embeddings", 0)

    mount_columns = (
        _base_recall_table_columns(
            connection, evidence, "notebook_bases", deadline
        )
        if "notebook_bases" in table_names
        else set()
    )
    mount_rows: list[sqlite3.Row] = []
    if {"notebook_id", "base_notebook_id"}.issubset(mount_columns):
        result = _safe_probe(
            evidence,
            "base_recall.mounts",
            deadline,
            lambda: _fetch_limited(
                connection.execute(
                    "SELECT b.id, b.tier, b.status, b.created_by, a.created_by "
                    "FROM notebook_bases e "
                    "JOIN notebooks b ON b.id=e.base_notebook_id "
                    "JOIN notebooks a ON a.id=e.notebook_id "
                    "WHERE e.notebook_id=? AND b.id!=e.notebook_id "
                    "ORDER BY CASE WHEN b.tier='base' THEN 0 ELSE 1 END, b.id",
                    (selected_id,),
                ),
                MAX_BASE_RECALL_MOUNTS,
                deadline,
            ),
        )
        if result is not None:
            mount_rows, truncated = result
            if truncated:
                _append_degraded(
                    evidence, "base_recall.mounts", "row_limit", RuntimeError()
                )
    elif "notebook_bases" not in table_names:
        _append_degraded(
            evidence, "base_recall.mounts", "missing_schema", RuntimeError()
        )

    mount_ids = [str(row[0]) for row in mount_rows]
    maps = {
        table: _base_recall_count_map(
            connection, evidence, table, mount_ids, deadline
        )
        for table in count_tables
    }
    active_mount_ids: set[str] = set()
    mounted: list[Dict[str, Any]] = []
    for index, row in enumerate(mount_rows, 1):
        _require_deadline(deadline)
        base_id = str(row[0])
        tier = str(row[1] or "personal")
        status = str(row[2] or "")
        same_owner = str(row[3] or "") == str(row[4] or "")
        active = status not in diag_common.NOTEBOOK_HIDDEN_STATUSES and (
            tier == "base" or same_owner
        )
        if active:
            active_mount_ids.add(base_id)
            reason = ""
        elif status == "copying":
            reason = "copying"
        else:
            reason = "not_authorized"
        mounted.append(
            {
                "notebook": f"base#{index}",
                "tier": tier if tier in {"base", "personal"} else "unknown",
                "active": active,
                "inactive_reason": reason,
                "kg_objects": maps.get("knowledge_objects", {}).get(base_id, 0),
                "embeddings": maps.get("knowledge_embeddings", {}).get(base_id, 0),
                "chunks": maps.get("chunks", {}).get(base_id, 0),
                "relations": maps.get("knowledge_relations", {}).get(base_id, 0),
            }
        )
    recall["mounted_bases"] = mounted
    recall["mounts_total"] = len(mounted)
    recall["mounts_active"] = sum(1 for row in mounted if row["active"])
    recall["mounts_inactive"] = len(mounted) - recall["mounts_active"]

    if required_reports.issubset(report_columns):
        row = _safe_probe(
            evidence,
            "base_recall.report",
            deadline,
            lambda: connection.execute(
                "SELECT references_json FROM reports "
                "WHERE notebook_id=? AND status='done' "
                "ORDER BY created_at DESC LIMIT 1",
                (selected_id,),
            ).fetchone(),
        )
        if row is not None:
            report = recall["latest_report"]
            report["exists"] = True
            raw = str(row[0] or "[]")
            if len(raw.encode("utf-8", "replace")) > MAX_BASE_RECALL_JSON_BYTES:
                _append_degraded(
                    evidence,
                    "base_recall.report",
                    "reference_limit",
                    RuntimeError(),
                )
                return
            try:
                references = json.loads(raw)
            except (json.JSONDecodeError, TypeError, ValueError):
                references = []
                report["malformed_references"] = 1
            if not isinstance(references, list):
                references = []
                report["malformed_references"] = 1
            if len(references) > MAX_BASE_RECALL_REFERENCES:
                _append_degraded(
                    evidence,
                    "base_recall.report",
                    "reference_limit",
                    RuntimeError(),
                )
                references = references[:MAX_BASE_RECALL_REFERENCES]
            report["references_total"] = len(references)
            for reference in references:
                _require_deadline(deadline)
                if not isinstance(reference, dict):
                    report["malformed_references"] += 1
                    continue
                tier = reference.get("tier")
                if tier == "base":
                    report["base_tier_references"] += 1
                elif tier == "personal":
                    report["personal_tier_references"] += 1
                else:
                    report["malformed_references"] += 1
                notebook = reference.get("notebook_id")
                if isinstance(notebook, str) and notebook in active_mount_ids:
                    report["mounted_base_references"] += 1


def _collect_snapshot_evidence(
    connection: sqlite3.Connection,
    evidence: Dict[str, Any],
    notebook_id: Optional[str],
    deadline: float,
    projection: Optional[str] = None,
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
    if projection == "base-recall":
        _collect_base_recall_evidence(
            connection, evidence, notebook_id, table_names, deadline
        )
        evidence["safety"]["transaction_open"] = bool(connection.in_transaction)
        return
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
    _collect_source_file_plan(
        connection,
        evidence,
        notebook_id,
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


def _discard_unsafe_diagnostics_evidence(
    notebook_id: Optional[str], final_state
) -> Dict[str, Any]:
    discarded = _empty_evidence(notebook_id)
    _set_file_evidence(discarded, final_state)
    discarded["safety"]["snapshot_used"] = True
    discarded["safety"]["source_unchanged"] = False
    _append_degraded(
        discarded,
        "diagnostics.validation",
        "unsafe_diagnostics",
        _UnsafeDiagnostics(),
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
    db_path,
    notebook_id: Optional[str] = None,
    deadline_seconds: float = 4.0,
    projection: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect bounded metadata from a validated copy, never a source SQLite open."""
    evidence = _empty_evidence(notebook_id)
    if projection not in (None, "base-recall"):
        _append_degraded(
            evidence, "projection", "unavailable", ValueError()
        )
        return _bound_evidence(evidence)
    try:
        requested = float(deadline_seconds)
        if requested != requested:
            requested = 4.0
    except (TypeError, ValueError, OverflowError):
        requested = 4.0
    deadline = time.monotonic() + max(0.001, min(requested, MAX_DEADLINE_SECONDS))

    try:
        path = Path(os.path.abspath(os.fspath(db_path)))
        paths = _source_paths(path)
        initial_state = _validate_source_state(paths)
    except FileNotFoundError as exc:
        _append_degraded(evidence, "open", "missing", exc)
        return _bound_evidence(evidence)
    except PermissionError as exc:
        _append_degraded(evidence, "source.validation", "permission", exc)
        return _bound_evidence(evidence)
    except _UnsafeFile as exc:
        _append_degraded(evidence, "source.validation", "unsafe_file", exc)
        return _bound_evidence(evidence)
    except OSError as exc:
        _append_degraded(
            evidence, "source.validation", _error_category(exc), exc
        )
        return _bound_evidence(evidence)
    except ValueError as exc:
        _append_degraded(evidence, "source.validation", "unavailable", exc)
        return _bound_evidence(evidence)
    _set_file_evidence(evidence, initial_state)
    if not _NOATIME_AVAILABLE or _NOATIME_FLAG is None:
        _append_degraded(
            evidence, "source.open", "noatime_unavailable", _NoAtimeUnavailable()
        )
        return _bound_evidence(evidence)

    pinned: Dict[str, _PinnedSource] = {}
    lock_held = False
    diagnostics: Optional[_PinnedDirectory] = None
    snapshot_root: Optional[_PinnedDirectory] = None
    workspace: Optional[_SnapshotWorkspace] = None
    before_state = initial_state
    try:
        try:
            pinned = _pin_source_files(paths, initial_state)
        except _NoAtimeUnavailable as exc:
            _append_degraded(evidence, "source.open", "noatime_unavailable", exc)
            return _bound_evidence(evidence)
        except _UnsafeFile as exc:
            _append_degraded(evidence, "source.open", "unsafe_file", exc)
            return _bound_evidence(evidence)
        except RuntimeError as exc:
            if str(exc) == "source-changed":
                evidence = _discard_raced_evidence(
                    evidence, notebook_id, _capture_source_state(paths)
                )
                return _bound_evidence(evidence)
            raise

        before_state = _capture_source_state(paths)
        _set_file_evidence(evidence, before_state)
        if before_state != initial_state or not _pinned_sources_unchanged(pinned):
            evidence = _discard_raced_evidence(evidence, notebook_id, before_state)
            return _bound_evidence(evidence)
        if before_state.get("journal") is not None:
            _append_degraded(
                evidence,
                "snapshot.rollback_journal",
                "active_rollback_journal",
                RuntimeError(),
            )
            return _bound_evidence(evidence)
        source_bytes = sum(
            int((before_state.get(name) or (0, 0, 0, 0, 0))[2])
            for name in ("database", "wal")
        )
        if source_bytes > MAX_SNAPSHOT_BYTES:
            _append_degraded(
                evidence, "snapshot.copy", "snapshot_byte_limit", ValueError()
            )
            return _bound_evidence(evidence)

        try:
            diagnostics = _open_diagnostics_directory(path.parent)
            snapshot_root = _open_snapshot_root(diagnostics)
            _cleanup_stale_workspaces(snapshot_root, evidence, deadline)
            if (
                not _directory_path_unchanged(diagnostics, private=False)
                or not _directory_path_unchanged(snapshot_root, private=True)
            ):
                raise _UnsafeDiagnostics()
            workspace = _create_snapshot_workspace(snapshot_root)
        except _UnsafeDiagnostics as exc:
            _append_degraded(
                evidence, "diagnostics.directory", "unsafe_diagnostics", exc
            )
            return _bound_evidence(evidence)

        if not _deadline_ok(evidence, "source.lock", deadline):
            return _bound_evidence(evidence)
        try:
            _acquire_source_read_lock(pinned["database"].descriptor)
            lock_held = True
        except _SourceLocked as exc:
            _append_degraded(evidence, "source.lock", "locked", exc)
            return _bound_evidence(evidence)

        pre_copy_state = _capture_source_state(paths)
        if pre_copy_state != before_state or not _pinned_sources_unchanged(pinned):
            evidence = _discard_raced_evidence(evidence, notebook_id, pre_copy_state)
            return _bound_evidence(evidence)

        middle_state = before_state
        try:
            copied = _copy_workspace_artifact(
                pinned["database"],
                workspace,
                "snapshot.db",
                deadline,
                MAX_SNAPSHOT_BYTES,
                evidence,
            )
            if "wal" in pinned:
                copied += _copy_workspace_artifact(
                    pinned["wal"],
                    workspace,
                    "snapshot.db-wal",
                    deadline,
                    MAX_SNAPSHOT_BYTES - copied,
                    evidence,
                )
            evidence["safety"]["snapshot_used"] = True
            evidence["safety"]["snapshot_bytes"] = copied
            middle_state = _capture_source_state(paths)
            if middle_state != before_state or not _pinned_sources_unchanged(pinned):
                evidence = _discard_raced_evidence(
                    evidence, notebook_id, middle_state
                )
                return _bound_evidence(evidence)
        finally:
            if lock_held:
                _safe_release_source_read_lock(
                    evidence, pinned["database"].descriptor
                )
                lock_held = False

        try:
            snapshot = _workspace_sqlite_path(workspace)
            if (
                not _directory_path_unchanged(diagnostics, private=False)
                or not _directory_path_unchanged(snapshot_root, private=True)
                or not _directory_path_unchanged(workspace, private=True)
            ):
                raise _UnsafeDiagnostics()
        except _UnsafeDiagnostics:
            evidence = _discard_unsafe_diagnostics_evidence(
                notebook_id, _capture_source_state(paths)
            )
            return _bound_evidence(evidence)

        connection: Optional[sqlite3.Connection] = None
        try:
            connection = _open_read_only(snapshot, deadline)
            if (
                not _directory_path_unchanged(diagnostics, private=False)
                or not _directory_path_unchanged(snapshot_root, private=True)
                or not _directory_path_unchanged(workspace, private=True)
            ):
                raise _UnsafeDiagnostics()
            remaining_ms = max(
                1,
                min(
                    MAX_BUSY_TIMEOUT_MS,
                    int((deadline - time.monotonic()) * 1000),
                ),
            )
            evidence["safety"]["busy_timeout_ms"] = remaining_ms
            _collect_snapshot_evidence(
                connection, evidence, notebook_id, deadline, projection
            )
        except _UnsafeDiagnostics:
            evidence = _discard_unsafe_diagnostics_evidence(
                notebook_id, _capture_source_state(paths)
            )
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
                    _append_degraded(
                        evidence,
                        "snapshot.close",
                        _error_category(exc),
                        exc,
                    )

        after_state = _capture_source_state(paths)
        if after_state != before_state or not _pinned_sources_unchanged(pinned):
            evidence = _discard_raced_evidence(evidence, notebook_id, after_state)
            return _bound_evidence(evidence)
        if (
            not _directory_path_unchanged(diagnostics, private=False)
            or not _directory_path_unchanged(snapshot_root, private=True)
            or not _directory_path_unchanged(workspace, private=True)
        ):
            evidence = _discard_unsafe_diagnostics_evidence(
                notebook_id, after_state
            )
            return _bound_evidence(evidence)
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
        if lock_held and "database" in pinned:
            _safe_release_source_read_lock(evidence, pinned["database"].descriptor)
        if workspace is not None and snapshot_root is not None:
            _cleanup_snapshot_workspace(snapshot_root, workspace, evidence)
        if snapshot_root is not None:
            _safe_close_descriptor(
                evidence, snapshot_root.descriptor, "snapshot.root_close"
            )
        if diagnostics is not None:
            _safe_close_descriptor(
                evidence, diagnostics.descriptor, "diagnostics.directory_close"
            )
        _close_pinned_sources(pinned, evidence)
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


def _main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect bounded source-side-effect-free SQLite DFX metadata."
    )
    parser.add_argument("--db", default=".local/silicon_notebook.db")
    # 显式 --db 是用户自己指点的文件,照办;缺省值则是一个关于部署形态的**假设**,
    # 而 PostgreSQL 部署上那个假设会安静地答错。

    parser.add_argument("--notebook-id", default=None)
    parser.add_argument("--deadline-seconds", type=float, default=4.0)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not _explicit_db_flag(sys.argv[1:] if argv is None else argv):
        # Anchor on the repository that contains this script, not the cwd:
        # invoked by absolute path from elsewhere, cwd is unrelated to the
        # deployment and a PostgreSQL install would fall back to "default
        # SQLite" — the stale-file misdiagnosis this guard exists to stop.
        target = diag_common.resolve_database_target(str(_SCRIPT_DIR.parent))
        if not target.is_sqlite:
            sys.stdout.write(
                f"{target.skip_note('本诊断')}\n"
                "如确实要检查那个 SQLite 文件，显式传 --db。\n"
            )
            return 0
        # 缺省值是「约定位置」,而 DATABASE_URL 可能指向别处;显式 --db 已在上面短路。
        # 兜底目录也必须锚在仓库上:未配置 DATABASE_URL 时 resolve 不给路径,按
        # cwd 取 `.local/` 会重新读到工作目录下那份陈旧库。
        resolved = target.resolve_sqlite_file(str(_SCRIPT_DIR.parent / ".local"))
        if resolved:
            args.db = resolved
    evidence = collect_db_evidence(
        args.db,
        notebook_id=args.notebook_id,
        deadline_seconds=args.deadline_seconds,
    )
    sys.stdout.write(render_db_report(evidence))
    return 0


def _explicit_db_flag(argv) -> bool:
    return any(arg == "--db" or arg.startswith("--db=") for arg in argv or ())


def main(argv=None) -> int:
    return diag_common.run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
