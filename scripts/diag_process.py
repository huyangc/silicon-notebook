#!/usr/bin/env python3
"""Collect bounded, metadata-only process evidence for host diagnostics.

This module intentionally uses only the Python standard library and never
imports the backend application.  It may write only bounded files inside the
operator-provided diagnostics directory.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import itertools
import json
import math
import os
import re
import signal
import socket
import stat as stat_module
import sys
import sysconfig
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:  # POSIX in production; kept optional for graceful non-POSIX imports.
    import fcntl
except ImportError:  # pragma: no cover - Windows import boundary
    fcntl = None  # type: ignore[assignment]


_MAX_PIDS = 4_096
_MAX_PROC_FILE_BYTES = 64 * 1024
_MAX_FDS = 65_536
_MAX_TASKS = 4_096
_SAMPLE_SECONDS = 0.2
_SNAPSHOT_MAX_BYTES = 512 * 1024
_SNAPSHOT_STALE_SECONDS = 6.0
_SNAPSHOT_FUTURE_SKEW_SECONDS = 1.0
_DUMP_WAIT_SECONDS = 1.0
_DUMP_STABLE_POLLS = 3
_DUMP_POLL_SECONDS = 0.02
_DUMP_MAX_BYTES = 512 * 1024
_DUMP_FILE_MAX_BYTES = 8 * 1024 * 1024
_DUMP_VALIDATION_RESERVE_SECONDS = 0.1
_MAX_DUMP_THREADS = 256
_MAX_FRAMES_PER_THREAD = 64
_MAX_PARSED_DUMP_BYTES = 256 * 1024
_HTTP_TIMEOUT_SECONDS = 0.75
_HTTP_RESULT_KEYS = ("role", "status", "elapsed_ms", "result")
_THREAD_HEADER = re.compile(
    r"^(?:Current thread|Thread) 0x([0-9a-fA-F]+)(?: \([^\n]*\))?:\s*$"
)
_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+) in ([^\r\n]+)\s*$')
_LIBRARY_PACKAGES = ("sqlite3", "asyncio", "anyio", "urllib", "httpx", "openai")
_LIBRARY_FILES = ("threading", "shutil", "os")
_RUNTIME_LIST_FIELDS = (
    "active_requests",
    "active_sql",
    "active_jobs",
    "recent_jobs",
)
_RUNTIME_DICT_FIELDS = ("readiness", "concurrency", "write_lock")
_RUNTIME_REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "pid",
        "process_started_at",
        "heartbeat_at",
        "last_state_change_at",
        "state_revision",
        "snapshot_failures",
        *_RUNTIME_LIST_FIELDS,
        *_RUNTIME_DICT_FIELDS,
    }
)


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _expired(deadline: Optional[float], clock: Callable[[], float]) -> bool:
    return deadline is not None and clock() >= deadline


class ProcAdapter:
    """Small, bounded adapter around the permitted per-process proc entries."""

    def __init__(self, root: Path | str = "/proc") -> None:
        self.root = Path(root)
        self.scan_incomplete = False

    def _process_path(self, pid: int, leaf: str) -> Path:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("invalid pid")
        if leaf not in {"cmdline", "cwd", "stat", "status", "io", "fd", "task"}:
            raise ValueError("unsupported proc entry")
        return self.root / str(pid) / leaf

    def _read_bytes(
        self,
        pid: int,
        leaf: str,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[bytes]:
        if _expired(deadline, clock):
            return None
        try:
            with self._process_path(pid, leaf).open("rb") as handle:
                value = handle.read(_MAX_PROC_FILE_BYTES + 1)[:_MAX_PROC_FILE_BYTES]
            return None if _expired(deadline, clock) else value
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                self.scan_incomplete = True
            return None
        except ValueError:
            return None

    def list_pids(
        self,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[int, ...]:
        self.scan_incomplete = False
        try:
            result: list[int] = []
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if len(result) >= _MAX_PIDS + 1:
                        self.scan_incomplete = True
                        break
                    if _expired(deadline, clock):
                        self.scan_incomplete = True
                        break
                    if not entry.name.isascii() or not entry.name.isdecimal():
                        continue
                    pid = _safe_int(entry.name)
                    if pid > 0:
                        result.append(pid)
            return tuple(sorted(result))
        except OSError:
            self.scan_incomplete = True
            return ()

    def exists(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        return self.read_stat(pid, deadline=deadline, clock=clock) is not None

    def read_cmdline(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[str, ...]:
        raw = self._read_bytes(pid, "cmdline", deadline=deadline, clock=clock)
        if raw is None:
            return ()
        return tuple(
            part.decode("utf-8", "replace")[:4_096]
            for part in raw.split(b"\0")[:128]
            if part
        )

    def read_cwd(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[Path]:
        if _expired(deadline, clock):
            return None
        try:
            target = os.readlink(self._process_path(pid, "cwd"))
            return None if _expired(deadline, clock) else Path(target)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                self.scan_incomplete = True
            return None
        except ValueError:
            return None

    def read_stat(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[str]:
        raw = self._read_bytes(pid, "stat", deadline=deadline, clock=clock)
        return None if raw is None else raw.decode("ascii", "replace")

    def read_status(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[str]:
        raw = self._read_bytes(pid, "status", deadline=deadline, clock=clock)
        return None if raw is None else raw.decode("ascii", "replace")

    def read_io(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[str]:
        raw = self._read_bytes(pid, "io", deadline=deadline, clock=clock)
        return None if raw is None else raw.decode("ascii", "replace")

    def count_fds(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        try:
            count = 0
            with os.scandir(self._process_path(pid, "fd")) as entries:
                for _entry in entries:
                    count += 1
                    if count >= _MAX_FDS or _expired(deadline, clock):
                        break
            return count
        except (OSError, ValueError):
            return 0

    def task_stats(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> tuple[str, ...]:
        try:
            result: list[str] = []
            task_root = self._process_path(pid, "task")
            with os.scandir(task_root) as entries:
                for entry in entries:
                    if len(result) >= _MAX_TASKS or _expired(deadline, clock):
                        break
                    if not entry.name.isascii() or not entry.name.isdecimal():
                        continue
                    try:
                        with (task_root / entry.name / "stat").open("rb") as handle:
                            raw = handle.read(_MAX_PROC_FILE_BYTES + 1)[:_MAX_PROC_FILE_BYTES]
                        result.append(raw.decode("ascii", "replace"))
                    except OSError:
                        continue
            return tuple(result)
        except (OSError, ValueError):
            return ()

    def identity(
        self,
        pid: int,
        *,
        deadline: Optional[float] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> Optional[dict[str, int]]:
        parsed = parse_proc_stat(
            self.read_stat(pid, deadline=deadline, clock=clock) or ""
        )
        if parsed is None:
            return None
        return {"pid": pid, "starttime_ticks": int(parsed["starttime_ticks"])}


def parse_proc_stat(value: str) -> Optional[dict[str, int | str]]:
    """Parse the needed stat fields without being confused by ``comm``."""

    if not isinstance(value, str) or len(value) > _MAX_PROC_FILE_BYTES:
        return None
    close = value.rfind(")")
    open_paren = value.find("(")
    if open_paren <= 0 or close <= open_paren:
        return None
    pid = _safe_int(value[:open_paren].strip(), -1)
    fields = value[close + 1 :].strip().split()
    if pid <= 0 or len(fields) <= 21 or len(fields[0]) != 1:
        return None
    required = (fields[11], fields[12], fields[19], fields[21])
    try:
        user_ticks, system_ticks, starttime_ticks, rss_pages = map(int, required)
    except (TypeError, ValueError, OverflowError):
        return None
    return {
        "pid": pid,
        "state": fields[0],
        "process_ticks": max(0, user_ticks + system_ticks),
        "starttime_ticks": max(0, starttime_ticks),
        "rss_pages": max(0, rss_pages),
    }


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _candidate_cwd(root: Path, cwd: Optional[Path]) -> Optional[str]:
    if cwd is None:
        return None
    if _same_path(cwd, root):
        return "<repo>"
    if _same_path(cwd, root / "backend"):
        return "<repo>/backend"
    return None


def _adapter_call(
    method: Callable[..., Any],
    *args: Any,
    deadline: Optional[float],
    clock: Callable[[], float],
) -> Any:
    if _expired(deadline, clock):
        raise TimeoutError
    try:
        value = method(*args, deadline=deadline, clock=clock)
    except TypeError:
        value = method(*args)
    if _expired(deadline, clock):
        raise TimeoutError
    return value


def _safe_identity(
    proc: Any,
    pid: int,
    *,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Optional[dict[str, int]]:
    try:
        identity = _adapter_call(
            proc.identity, pid, deadline=deadline, clock=clock
        )
    except (OSError, ValueError, TypeError, KeyError, TimeoutError, StopIteration):
        return None
    if not isinstance(identity, dict):
        return None
    identity_pid = identity.get("pid")
    starttime = identity.get("starttime_ticks")
    if identity_pid != pid or isinstance(starttime, bool) or not isinstance(starttime, int):
        return None
    return {"pid": pid, "starttime_ticks": starttime}


class _ProcScanIncomplete(Exception):
    """Internal, detail-free marker for an incomplete proc scan."""


def _resolution_identity(
    proc: Any,
    pid: int,
    *,
    deadline: Optional[float],
    clock: Callable[[], float],
) -> Optional[dict[str, int]]:
    try:
        identity = _adapter_call(
            proc.identity, pid, deadline=deadline, clock=clock
        )
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ESRCH}:
            return None
        raise _ProcScanIncomplete from None
    except (ValueError, TypeError, KeyError, StopIteration):
        return None
    if not isinstance(identity, dict):
        return None
    identity_pid = identity.get("pid")
    starttime = identity.get("starttime_ticks")
    if identity_pid != pid or isinstance(starttime, bool) or not isinstance(starttime, int):
        return None
    return {"pid": pid, "starttime_ticks": starttime}


def _is_production_uvicorn_command(command: tuple[str, ...]) -> bool:
    if not command:
        return False
    if Path(command[0]).name == "uvicorn":
        return "app.main:app" in command[1:]
    python_name = Path(command[0]).name
    return bool(
        re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", python_name)
        and command[1:4] == ("-m", "uvicorn", "app.main:app")
    )


def resolve_backend_pid(
    root: Path | str,
    pid: Optional[int] = None,
    *,
    proc: Optional[Any] = None,
    self_pid: Optional[int] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Resolve one Uvicorn process without returning its raw command line."""

    adapter = proc or ProcAdapter()
    repo = Path(root).resolve(strict=False)
    own_pid = os.getpid() if self_pid is None else self_pid
    source = "operator" if pid is not None else "auto"
    empty = {"pid": None, "source": source, "candidates": []}

    if _expired(deadline, clock):
        return {"status": "scan_incomplete", **empty}

    if pid is not None:
        if pid == own_pid:
            return {"status": "missing", **empty}
        try:
            before = _resolution_identity(
                adapter, pid, deadline=deadline, clock=clock
            )
            after = _resolution_identity(
                adapter, pid, deadline=deadline, clock=clock
            )
        except (_ProcScanIncomplete, TimeoutError):
            return {"status": "scan_incomplete", **empty}
        if bool(getattr(adapter, "scan_incomplete", False)):
            return {"status": "scan_incomplete", **empty}
        if _expired(deadline, clock):
            return {"status": "scan_incomplete", **empty}
        if before is None or after is None or before != after:
            return {"status": "missing", **empty}
        return {
            "status": "ok",
            "pid": pid,
            "source": source,
            "identity": before,
            "candidates": [],
        }

    candidates: list[dict[str, Any]] = []
    identities: dict[int, dict[str, int]] = {}
    scan_incomplete = False
    try:
        pids: Iterable[int] = _adapter_call(
            adapter.list_pids, deadline=deadline, clock=clock
        )
        scan_incomplete = bool(getattr(adapter, "scan_incomplete", False))
        for index, candidate_pid in enumerate(pids):
            if index >= _MAX_PIDS:
                scan_incomplete = True
                break
            if _expired(deadline, clock):
                scan_incomplete = True
                break
            if candidate_pid == own_pid:
                continue
            try:
                before = _resolution_identity(
                    adapter, candidate_pid, deadline=deadline, clock=clock
                )
                command = _adapter_call(
                    adapter.read_cmdline,
                    candidate_pid,
                    deadline=deadline,
                    clock=clock,
                )
                cwd = _adapter_call(
                    adapter.read_cwd,
                    candidate_pid,
                    deadline=deadline,
                    clock=clock,
                )
                after = _resolution_identity(
                    adapter, candidate_pid, deadline=deadline, clock=clock
                )
                if bool(getattr(adapter, "scan_incomplete", False)):
                    scan_incomplete = True
                    break
            except _ProcScanIncomplete:
                scan_incomplete = True
                break
            except TimeoutError:
                scan_incomplete = True
                break
            except OSError as exc:
                if exc.errno not in {errno.ENOENT, errno.ESRCH}:
                    scan_incomplete = True
                    break
                continue
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            if before is None or after is None or before != after:
                continue
            if not isinstance(command, (tuple, list)):
                continue
            command_tokens = tuple(str(part) for part in command[:128])
            if not _is_production_uvicorn_command(command_tokens):
                continue
            safe_cwd = _candidate_cwd(repo, cwd)
            if safe_cwd is None:
                continue
            candidates.append({"pid": candidate_pid, "cwd": safe_cwd})
            identities[candidate_pid] = before
    except (OSError, ValueError, TypeError, KeyError, TimeoutError):
        scan_incomplete = True

    candidates.sort(key=lambda item: item["pid"])
    if scan_incomplete or _expired(deadline, clock):
        return {
            "status": "scan_incomplete",
            "pid": None,
            "source": source,
            "candidates": candidates,
        }
    if len(candidates) == 1:
        selected = candidates[0]
        return {
            "status": "ok",
            "pid": selected["pid"],
            "source": source,
            "identity": identities[selected["pid"]],
            "candidates": candidates,
        }
    return {
        "status": "ambiguous" if candidates else "missing",
        "pid": None,
        "source": source,
        "candidates": candidates,
    }


def _key_values(text: Optional[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    if not text:
        return result
    for line in text.splitlines()[:1_024]:
        key, separator, value = line.partition(":")
        if separator and key.isascii() and len(key) <= 64:
            result[key] = value.strip()[:128]
    return result


def _number_prefix(value: str) -> int:
    match = re.match(r"^(\d+)", value)
    return _safe_int(match.group(1)) if match else 0


def sample_process(
    pid: int,
    *,
    expected_identity: Optional[dict[str, int]] = None,
    proc: Optional[Any] = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    clock_ticks: Optional[int] = None,
    page_size: Optional[int] = None,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Take two bounded proc samples and return numeric process metadata."""

    adapter = proc or ProcAdapter()
    ticks_per_second = clock_ticks or int(os.sysconf("SC_CLK_TCK"))
    system_page_size = page_size or int(os.sysconf("SC_PAGE_SIZE"))
    try:
        if _expired(deadline, clock):
            return {"pid": pid, "status": "deadline"}
        first = parse_proc_stat(
            _adapter_call(
                adapter.read_stat, pid, deadline=deadline, clock=clock
            )
            or ""
        )
        first_at = float(clock())
        if first is None:
            return {"pid": pid, "status": "unavailable"}
        first_identity = {
            "pid": pid,
            "starttime_ticks": int(first["starttime_ticks"]),
        }
        if expected_identity is not None and first_identity != expected_identity:
            return {"pid": pid, "status": "identity_changed"}
        if deadline is not None and first_at + _SAMPLE_SECONDS > deadline:
            return {"pid": pid, "status": "deadline"}
        sleeper(
            _SAMPLE_SECONDS
            if deadline is None
            else min(_SAMPLE_SECONDS, max(0.0, deadline - first_at))
        )
        if _expired(deadline, clock):
            return {"pid": pid, "status": "deadline"}
        second = parse_proc_stat(
            _adapter_call(
                adapter.read_stat, pid, deadline=deadline, clock=clock
            )
            or ""
        )
        second_at = float(clock())
        if second is None or first["starttime_ticks"] != second["starttime_ticks"]:
            return {"pid": pid, "status": "identity_changed"}
        second_identity = {
            "pid": pid,
            "starttime_ticks": int(second["starttime_ticks"]),
        }
        if expected_identity is not None and second_identity != expected_identity:
            return {"pid": pid, "status": "identity_changed"}
        if _expired(deadline, clock):
            return {"pid": pid, "status": "deadline"}
        status = _key_values(
            _adapter_call(
                adapter.read_status, pid, deadline=deadline, clock=clock
            )
        )
        io_values = _key_values(
            _adapter_call(adapter.read_io, pid, deadline=deadline, clock=clock)
        )
        d_state_threads = 0
        task_stats = _adapter_call(
            adapter.task_stats, pid, deadline=deadline, clock=clock
        )
        for task_stat in itertools.islice(task_stats, _MAX_TASKS):
            if _expired(deadline, clock):
                return {"pid": pid, "status": "deadline"}
            parsed_task = parse_proc_stat(task_stat)
            if parsed_task is not None and parsed_task["state"] == "D":
                d_state_threads += 1
        fds = _adapter_call(
            adapter.count_fds, pid, deadline=deadline, clock=clock
        )
        if _expired(deadline, clock):
            return {"pid": pid, "status": "deadline"}
        final_identity = _safe_identity(
            adapter, pid, deadline=deadline, clock=clock
        )
        if _expired(deadline, clock):
            return {"pid": pid, "status": "deadline"}
        if final_identity != (expected_identity or second_identity):
            return {"pid": pid, "status": "identity_changed"}
        elapsed = max(0.000_001, second_at - first_at)
        process_tick_delta = max(
            0, int(second["process_ticks"]) - int(first["process_ticks"])
        )
        cpu_percent = round(
            (process_tick_delta / max(1, ticks_per_second)) / elapsed * 100.0,
            3,
        )
        rss_kib = _number_prefix(status.get("VmRSS", ""))
        rss_bytes = rss_kib * 1024
        if not rss_kib:
            rss_bytes = int(second["rss_pages"]) * max(1, system_page_size)
        uptime_seconds = max(
            0.0, second_at - int(second["starttime_ticks"]) / max(1, ticks_per_second)
        )
        return {
            "pid": pid,
            "state": str(second["state"]),
            "cpu_percent": cpu_percent,
            "rss_bytes": rss_bytes,
            "threads": _number_prefix(status.get("Threads", "")),
            "fds": max(0, min(_MAX_FDS, int(fds))),
            "read_bytes": _number_prefix(io_values.get("read_bytes", "")),
            "write_bytes": _number_prefix(io_values.get("write_bytes", "")),
            "d_state_threads": d_state_threads,
            "uptime_seconds": round(uptime_seconds, 3),
        }
    except TimeoutError:
        return {"pid": pid, "status": "deadline"}
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return {"pid": pid, "status": "unavailable"}


def _parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or len(value) > 128:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_json_metadata(value: Any, *, depth: int = 0, budget: Optional[list[int]] = None) -> bool:
    if budget is None:
        budget = [20_000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 16:
        return False
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, str):
        return len(value.encode("utf-8", "replace")) <= 4_096
    if isinstance(value, list):
        return len(value) <= 4_096 and all(
            _valid_json_metadata(item, depth=depth + 1, budget=budget)
            for item in value
        )
    if isinstance(value, dict):
        if len(value) > 4_096:
            return False
        return all(
            isinstance(key, str)
            and len(key) <= 128
            and _valid_json_metadata(item, depth=depth + 1, budget=budget)
            for key, item in value.items()
        )
    return False


def _owned_regular(stat_result: os.stat_result) -> bool:
    effective_uid = getattr(os, "geteuid", lambda: stat_result.st_uid)()
    return (
        stat_module.S_ISREG(stat_result.st_mode)
        and stat_result.st_uid == effective_uid
        and stat_result.st_nlink == 1
    )


def load_runtime_snapshot(
    path: Path | str,
    pid: int,
    *,
    now: Optional[datetime] = None,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Read and validate one bounded runtime heartbeat snapshot."""

    snapshot_path = Path(path)
    if _expired(deadline, clock):
        return {"status": "deadline", "fresh": False, "snapshot": None}
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(snapshot_path, flags)
        with os.fdopen(descriptor, "rb", buffering=0) as handle:
            file_stat = os.fstat(handle.fileno())
            if not _owned_regular(file_stat) or file_stat.st_size > _SNAPSHOT_MAX_BYTES:
                return {"status": "malformed", "fresh": False, "snapshot": None}
            raw = handle.read(_SNAPSHOT_MAX_BYTES + 1)
    except OSError:
        return {"status": "missing", "fresh": False, "snapshot": None}
    if _expired(deadline, clock):
        return {"status": "deadline", "fresh": False, "snapshot": None}
    if len(raw) > _SNAPSHOT_MAX_BYTES:
        return {"status": "malformed", "fresh": False, "snapshot": None}
    try:
        snapshot = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"status": "malformed", "fresh": False, "snapshot": None}
    if not isinstance(snapshot, dict):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if not _valid_json_metadata(snapshot):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if not _RUNTIME_REQUIRED_FIELDS.issubset(snapshot):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        return {"status": "unsupported_schema", "fresh": False, "snapshot": None}
    if type(snapshot.get("pid")) is not int or snapshot["pid"] != pid:
        return {"status": "pid_mismatch", "fresh": False, "snapshot": None}
    if any(
        type(snapshot.get(field)) is not int or snapshot[field] < 0
        for field in ("state_revision", "snapshot_failures")
    ):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if any(not isinstance(snapshot.get(field), list) for field in _RUNTIME_LIST_FIELDS):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if any(not isinstance(snapshot.get(field), dict) for field in _RUNTIME_DICT_FIELDS):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    heartbeat = _parse_utc(snapshot.get("heartbeat_at"))
    if heartbeat is None or any(
        _parse_utc(snapshot.get(field)) is None
        for field in ("process_started_at", "last_state_change_at")
    ):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    age = (captured_at.astimezone(timezone.utc) - heartbeat).total_seconds()
    if not math.isfinite(age):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if age < -_SNAPSHOT_FUTURE_SKEW_SECONDS:
        return {"status": "future_heartbeat", "fresh": False, "snapshot": None}
    age = max(0.0, age)
    fresh = age <= _SNAPSHOT_STALE_SECONDS
    return {
        "status": "ok" if fresh else "stale",
        "fresh": fresh,
        "age_seconds": round(age, 3),
        "snapshot": snapshot,
    }


def probe_http(
    role: str,
    url: str,
    *,
    timeout: float = _HTTP_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    opener: Optional[Callable[..., Any]] = None,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Probe liveness without reading or retaining any response body."""

    started = clock()
    status: Optional[int] = None
    result = "error"
    try:
        parsed_url = urllib.parse.urlsplit(url)
        host = parsed_url.hostname
        address = ipaddress.ip_address(host or "")
        valid_target = (
            parsed_url.scheme in {"http", "https"}
            and parsed_url.username is None
            and parsed_url.password is None
            and address.is_loopback
        )
    except (ValueError, TypeError):
        valid_target = False
    if not valid_target:
        return dict(zip(_HTTP_RESULT_KEYS, (str(role)[:32], None, 0, result)))
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    remaining = _HTTP_TIMEOUT_SECONDS
    if deadline is not None:
        remaining = min(remaining, max(0.0, deadline - started))
    if remaining <= 0:
        return dict(zip(_HTTP_RESULT_KEYS, (str(role)[:32], None, 0, "timeout")))
    try:
        with opener(
            url, timeout=min(remaining, max(0.001, timeout))
        ) as response:
            status = int(response.status)
            result = "ok" if 200 <= status < 400 else "error"
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        result = "error"
    except (TimeoutError, socket.timeout):
        result = "timeout"
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            result = "timeout"
        elif isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) == errno.ECONNREFUSED:
            result = "refused"
        else:
            result = "error"
    except (OSError, ValueError):
        result = "error"
    elapsed_ms = max(0, int(round((clock() - started) * 1_000)))
    return dict(zip(_HTTP_RESULT_KEYS, (str(role)[:32], status, elapsed_ms, result)))


def _sigusr1_is_caught(
    proc: Any,
    pid: int,
    *,
    deadline: Optional[float] = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    try:
        status = _key_values(
            _adapter_call(
                proc.read_status, pid, deadline=deadline, clock=clock
            )
        )
        caught = int(status.get("SigCgt", ""), 16)
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return False
    signal_number = int(signal.SIGUSR1)
    return bool(caught & (1 << (signal_number - 1)))


def _capture_result(status: str, **fields: Any) -> dict[str, Any]:
    result = {
        "status": status,
        "identity_verified": status in {"ok", "partial"},
        "dump": "",
    }
    result.update(fields)
    return result


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _owned_directory(stat_result: os.stat_result) -> bool:
    effective_uid = getattr(os, "geteuid", lambda: stat_result.st_uid)()
    return stat_module.S_ISDIR(stat_result.st_mode) and stat_result.st_uid == effective_uid


def _path_matches_descriptor(
    name: str,
    descriptor_stat: os.stat_result,
    *,
    directory_fd: int,
) -> bool:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return _same_file(current, descriptor_stat)


def _directory_path_matches(path: Path, descriptor_stat: os.stat_result) -> bool:
    try:
        current = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return _same_file(current, descriptor_stat)


def _capture_files_safe(
    directory: Path,
    directory_stat: os.stat_result,
    directory_fd: int,
    lock_fd: int,
    lock_stat: os.stat_result,
    dump_fd: int,
    dump_stat: os.stat_result,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> bool:
    try:
        if _expired(deadline, clock):
            raise TimeoutError
        current_directory = os.fstat(directory_fd)
        current_lock = os.fstat(lock_fd)
        current_dump = os.fstat(dump_fd)
        if _expired(deadline, clock):
            raise TimeoutError
        return (
            _owned_directory(current_directory)
            and _same_file(current_directory, directory_stat)
            and _directory_path_matches(directory, current_directory)
            and _owned_regular(current_lock)
            and _same_file(current_lock, lock_stat)
            and _path_matches_descriptor(
                "incident.lock", current_lock, directory_fd=directory_fd
            )
            and _owned_regular(current_dump)
            and _same_file(current_dump, dump_stat)
            and _path_matches_descriptor(
                "thread-dumps.log", current_dump, directory_fd=directory_fd
            )
            and not _expired(deadline, clock)
        )
    except OSError:
        return False


def _deadline_partial(offset: int) -> dict[str, Any]:
    return _capture_result(
        "partial",
        offset=offset,
        bytes=0,
        truncated=False,
        complete=False,
        partial_reason="deadline",
    )


def _default_pidfd_open(pid: int, flags: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        raise OSError(errno.ENOSYS, "pidfd unavailable")
    return opener(pid, flags)


def _default_pidfd_send(
    pidfd: int,
    signum: int,
    siginfo: Any,
    flags: int,
) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise OSError(errno.ENOSYS, "pidfd signal unavailable")
    sender(pidfd, signum, siginfo, flags)


def capture_thread_dump(
    pid: int,
    diagnostics_dir: Path | str,
    *,
    proc: Optional[Any] = None,
    expected_identity: Optional[dict[str, int]] = None,
    pidfd_open_fn: Optional[Callable[[int, int], int]] = None,
    pidfd_send_fn: Optional[Callable[[int, int, Any, int], None]] = None,
    ftruncate_fn: Callable[[int, int], None] = os.ftruncate,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    platform: Optional[str] = None,
    timeout: float = _DUMP_WAIT_SECONDS,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Request the already-installed non-terminating SIGUSR1 dump handler."""

    if not hasattr(signal, "SIGUSR1"):
        return _capture_result("unsupported")
    if fcntl is None:
        return _capture_result("unsupported")
    platform_name = sys.platform if platform is None else platform
    if not platform_name.startswith("linux"):
        return _capture_result("unsupported")
    adapter = proc or ProcAdapter()
    open_pidfd = pidfd_open_fn or _default_pidfd_open
    send_pidfd = pidfd_send_fn or _default_pidfd_send
    started = clock()
    local_deadline = started + min(_DUMP_WAIT_SECONDS, max(0.001, float(timeout)))
    capture_deadline = local_deadline if deadline is None else min(deadline, local_deadline)
    capture_window = max(0.0, capture_deadline - started)
    validation_reserve = min(
        _DUMP_VALIDATION_RESERVE_SECONDS, capture_window * 0.25
    )
    quiescence_deadline = capture_deadline - validation_reserve
    if clock() >= capture_deadline:
        return _capture_result("deadline")
    directory = Path(diagnostics_dir)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | nonblock | cloexec
    file_flags = os.O_RDWR | nofollow | nonblock | cloexec
    directory_fd = -1
    pidfd = -1
    try:
        try:
            directory_fd = os.open(directory, directory_flags)
        except OSError:
            return _capture_result("unsafe_path")
        directory_stat = os.fstat(directory_fd)
        if not _owned_directory(directory_stat) or not _directory_path_matches(directory, directory_stat):
            return _capture_result("unsafe_path")
        try:
            lock_fd = os.open(
                "incident.lock",
                file_flags | os.O_CREAT,
                0o600,
                dir_fd=directory_fd,
            )
        except OSError:
            return _capture_result("unsafe_path")
        lock_stat = os.fstat(lock_fd)
        if not _owned_regular(lock_stat) or not _path_matches_descriptor(
            "incident.lock", lock_stat, directory_fd=directory_fd
        ):
            os.close(lock_fd)
            return _capture_result("unsafe_path")
        with os.fdopen(lock_fd, "a+b") as lock_handle:
            while True:
                try:
                    fcntl.flock(
                        lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError:
                    if clock() >= capture_deadline:
                        return _capture_result("lock_timeout")
                    sleeper(
                        min(
                            _DUMP_POLL_SECONDS,
                            max(0.0, capture_deadline - clock()),
                        )
                    )
            try:
                dump_fd = os.open(
                    "thread-dumps.log", file_flags, dir_fd=directory_fd
                )
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return _capture_result("dump_unavailable")
                return _capture_result("unsafe_path")
            with os.fdopen(dump_fd, "r+b", buffering=0) as dump_handle:
                dump_stat = os.fstat(dump_handle.fileno())
                if not _owned_regular(dump_stat) or not _path_matches_descriptor(
                    "thread-dumps.log", dump_stat, directory_fd=directory_fd
                ):
                    return _capture_result("unsafe_path")
                offset = dump_stat.st_size
                before_identity = _safe_identity(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                baseline_identity = expected_identity or before_identity
                if before_identity is None:
                    return _capture_result("missing_process")
                if before_identity != baseline_identity:
                    return _capture_result("identity_changed")
                caught = _sigusr1_is_caught(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                if not caught:
                    return _capture_result("signal_unavailable")
                second_identity = _safe_identity(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if second_identity != baseline_identity:
                    return _capture_result("identity_changed")
                try:
                    pidfd = open_pidfd(pid, 0)
                except (OSError, ValueError, PermissionError):
                    return _capture_result("pidfd_unavailable")
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                try:
                    files_safe = _capture_files_safe(
                        directory,
                        directory_stat,
                        directory_fd,
                        lock_handle.fileno(),
                        lock_stat,
                        dump_handle.fileno(),
                        dump_stat,
                        deadline=capture_deadline,
                        clock=clock,
                    )
                except TimeoutError:
                    return _capture_result("deadline")
                if not files_safe:
                    return _capture_result("unsafe_path")
                third_identity = _safe_identity(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                if third_identity != baseline_identity:
                    return _capture_result("identity_changed")
                caught = _sigusr1_is_caught(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                if not caught:
                    return _capture_result("signal_unavailable")
                fourth_identity = _safe_identity(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _capture_result("deadline")
                if fourth_identity != baseline_identity:
                    return _capture_result("identity_changed")
                try:
                    send_pidfd(pidfd, signal.SIGUSR1, None, 0)
                except (OSError, ValueError, PermissionError):
                    return _capture_result("signal_failed")
                size = offset
                last_size = offset
                stable_polls = 0
                grew = False
                while clock() < quiescence_deadline:
                    try:
                        if _expired(quiescence_deadline, clock):
                            break
                        size = os.fstat(dump_handle.fileno()).st_size
                    except OSError:
                        return _capture_result("dump_unavailable")
                    if size > offset:
                        grew = True
                        if size == last_size:
                            stable_polls += 1
                        else:
                            stable_polls = 0
                            last_size = size
                        if stable_polls >= _DUMP_STABLE_POLLS:
                            break
                    sleeper(
                        min(
                            _DUMP_POLL_SECONDS,
                            max(0.0, quiescence_deadline - clock()),
                        )
                    )
                complete = grew and stable_polls >= _DUMP_STABLE_POLLS
                quiescence_exhausted = (
                    not complete and clock() >= quiescence_deadline
                )
                after_identity = _safe_identity(
                    adapter, pid, deadline=capture_deadline, clock=clock
                )
                if _expired(capture_deadline, clock):
                    return _deadline_partial(offset)
                if after_identity != baseline_identity:
                    return _capture_result("identity_changed", offset=offset)
                if not _sigusr1_is_caught(
                    adapter, pid, deadline=capture_deadline, clock=clock
                ):
                    if _expired(capture_deadline, clock):
                        return _deadline_partial(offset)
                    return _capture_result("signal_unavailable", offset=offset)
                try:
                    files_safe = _capture_files_safe(
                        directory,
                        directory_stat,
                        directory_fd,
                        lock_handle.fileno(),
                        lock_stat,
                        dump_handle.fileno(),
                        dump_stat,
                        deadline=capture_deadline,
                        clock=clock,
                    )
                except TimeoutError:
                    return _deadline_partial(offset)
                if not files_safe:
                    return _capture_result("unsafe_path", offset=offset)
                if _expired(capture_deadline, clock):
                    return _deadline_partial(offset)
                final_size = os.fstat(dump_handle.fileno()).st_size
                if _expired(capture_deadline, clock):
                    return _deadline_partial(offset)
                if final_size != last_size:
                    complete = False
                try:
                    if _expired(capture_deadline, clock):
                        return _deadline_partial(offset)
                    dump_handle.seek(offset)
                    appended = dump_handle.read(_DUMP_MAX_BYTES + 1)[:_DUMP_MAX_BYTES]
                except OSError:
                    return _capture_result("dump_unavailable", offset=offset)
                if _expired(capture_deadline, clock):
                    return _deadline_partial(offset)
                truncated = final_size - offset > _DUMP_MAX_BYTES
                degraded: list[str] = []
                if complete and final_size > _DUMP_FILE_MAX_BYTES:
                    try:
                        files_safe = _capture_files_safe(
                            directory,
                            directory_stat,
                            directory_fd,
                            lock_handle.fileno(),
                            lock_stat,
                            dump_handle.fileno(),
                            dump_stat,
                            deadline=capture_deadline,
                            clock=clock,
                        )
                        if not files_safe:
                            return _capture_result("unsafe_path", offset=offset)
                        ftruncate_fn(dump_handle.fileno(), 0)
                    except OSError:
                        degraded.append("retention_failed")
                    except TimeoutError:
                        degraded.append("retention_failed")
                status = "ok" if complete and not truncated else "partial"
                partial_reason = None
                if truncated:
                    partial_reason = "byte_cap"
                elif not complete:
                    partial_reason = (
                        "deadline"
                        if quiescence_exhausted or clock() >= capture_deadline
                        else "ongoing_write"
                    )
                return _capture_result(
                    status,
                    offset=offset,
                    bytes=len(appended),
                    truncated=truncated,
                    complete=status == "ok",
                    partial_reason=partial_reason,
                    dump=appended.decode("utf-8", "replace"),
                    degraded=degraded,
                )
    except OSError:
        return _capture_result("lock_unavailable")
    finally:
        if pidfd >= 0:
            try:
                os.close(pidfd)
            except OSError:
                pass
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _default_trusted_library_paths() -> dict[str, tuple[Path, ...]]:
    paths = sysconfig.get_paths()
    stdlib_roots = tuple(
        Path(value).resolve(strict=False)
        for key in ("stdlib", "platstdlib")
        if (value := paths.get(key))
    )
    site_roots = tuple(
        Path(value).resolve(strict=False)
        for key in ("purelib", "platlib")
        if (value := paths.get(key))
    )
    result: dict[str, tuple[Path, ...]] = {}
    for token in _LIBRARY_PACKAGES:
        roots = stdlib_roots if token in {"sqlite3", "asyncio", "urllib"} else site_roots
        result[token] = tuple(root / token for root in roots)
    for token in _LIBRARY_FILES:
        result[token] = tuple(root / f"{token}.py" for root in stdlib_roots)
    return result


def _matches_trusted_path(path: Path, trusted: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=True)
        resolved_trusted = trusted.resolve(strict=True)
        if not stat_module.S_ISREG(os.stat(resolved_path).st_mode):
            return False
        if trusted.suffix == ".py":
            return (
                stat_module.S_ISREG(os.stat(resolved_trusted).st_mode)
                and resolved_path == resolved_trusted
            )
        if not stat_module.S_ISDIR(os.stat(resolved_trusted).st_mode):
            return False
        resolved_path.relative_to(resolved_trusted)
        return True
    except (OSError, ValueError):
        return False


def _sanitized_frame(
    line: str,
    repo: Path,
    trusted_library_paths: dict[str, tuple[Path, ...]],
) -> Optional[str]:
    match = _FRAME.match(line)
    if match is None:
        return None
    raw_path, line_number, function = match.groups()
    path = Path(raw_path)
    try:
        relative = path.resolve(strict=False).relative_to(repo.resolve(strict=False))
    except (OSError, ValueError):
        relative = None
    safe_function = re.sub(r"[^A-Za-z0-9_<>. -]", "?", function)[:128]
    if relative is not None:
        safe_path = "<repo>/" + relative.as_posix()
    else:
        token = next(
            (
                name
                for name, trusted_paths in trusted_library_paths.items()
                if any(_matches_trusted_path(path, trusted) for trusted in trusted_paths)
            ),
            None,
        )
        if token is None:
            return None
        safe_path = f"<library:{token}>"
    return f"{safe_path}:{_safe_int(line_number)} in {safe_function}"


def parse_thread_dump(
    dump: str | bytes,
    repo_root: Path | str,
    *,
    trusted_library_paths: Optional[dict[str, tuple[Path, ...]]] = None,
) -> dict[str, list[str]]:
    """Return bounded frame metadata, omitting all non-allow-listed paths."""

    if isinstance(dump, bytes):
        text = dump[:_DUMP_MAX_BYTES].decode("utf-8", "replace")
    else:
        text = str(dump).encode("utf-8", "replace")[:_DUMP_MAX_BYTES].decode(
            "utf-8", "replace"
        )
    repo = Path(repo_root)
    trusted_paths = (
        _default_trusted_library_paths()
        if trusted_library_paths is None
        else trusted_library_paths
    )
    result: dict[str, list[str]] = {}
    omitted: dict[str, int] = {}
    current: Optional[str] = None
    output_bytes = 0
    for line in text.splitlines():
        header = _THREAD_HEADER.match(line)
        if header is not None:
            if len(result) >= _MAX_DUMP_THREADS:
                current = None
                continue
            current = header.group(1).lower().lstrip("0") or "0"
            result.setdefault(current, [])
            omitted.setdefault(current, 0)
            continue
        if current is None or not line.lstrip().startswith("File "):
            continue
        safe = _sanitized_frame(line, repo, trusted_paths)
        if safe is None:
            omitted[current] += 1
            continue
        if len(result[current]) >= _MAX_FRAMES_PER_THREAD:
            omitted[current] += 1
            continue
        encoded_size = len(safe.encode("utf-8")) + 1
        if output_bytes + encoded_size > _MAX_PARSED_DUMP_BYTES:
            omitted[current] += 1
            continue
        result[current].append(safe)
        output_bytes += encoded_size
    for thread_id, count in omitted.items():
        if count:
            result[thread_id].append(f"<{count} frame(s) omitted>")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Collect bounded process metadata")
    parser.add_argument("--root", default=".")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--diagnostics-dir", default=".local/diagnostics")
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000/api/ready")
    parser.add_argument("--frontend-url", default="http://127.0.0.1:3000/")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve(strict=False)
    diagnostics = Path(args.diagnostics_dir)
    if not diagnostics.is_absolute():
        diagnostics = root / diagnostics
    deadline = time.monotonic() + 10.0
    resolution = resolve_backend_pid(root, args.pid, deadline=deadline)
    candidates = resolution.get("candidates")
    safe_resolution = {
        "status": resolution.get("status"),
        "pid": resolution.get("pid"),
        "source": resolution.get("source"),
        "candidate_count": len(candidates) if isinstance(candidates, list) else 0,
        "candidates": candidates[:16] if isinstance(candidates, list) else [],
    }
    evidence: dict[str, Any] = {
        "resolution": safe_resolution,
        "health": [
            probe_http("backend", args.backend_url, deadline=deadline),
            probe_http("frontend", args.frontend_url, deadline=deadline),
        ],
    }
    selected_pid = resolution.get("pid")
    if isinstance(selected_pid, int):
        evidence["process"] = sample_process(
            selected_pid,
            expected_identity=resolution.get("identity"),
            deadline=deadline,
        )
        runtime = load_runtime_snapshot(
            diagnostics / "runtime.json", selected_pid, deadline=deadline
        )
        evidence["runtime"] = {
            key: runtime[key]
            for key in ("status", "fresh", "age_seconds")
            if key in runtime
        }
        capture = capture_thread_dump(
            selected_pid,
            diagnostics,
            expected_identity=resolution.get("identity"),
            deadline=deadline,
        )
        evidence["capture"] = {
            key: value for key, value in capture.items() if key != "dump"
        }
        evidence["stacks"] = parse_thread_dump(capture.get("dump", ""), root)
    rendered = json.dumps(evidence, ensure_ascii=False, allow_nan=False, sort_keys=True)
    if len(rendered.encode("utf-8")) > 32_767:
        stacks = evidence.pop("stacks", None)
        evidence["stacks"] = {
            "status": "truncated",
            "thread_count": len(stacks) if isinstance(stacks, dict) else 0,
        }
        rendered = json.dumps(
            evidence, ensure_ascii=False, allow_nan=False, sort_keys=True
        )
    payload = rendered.encode("utf-8")
    if len(payload) > 32_767:  # Defensive: the metadata-only fallback is tiny.
        payload = b'{"status":"output_truncated"}'
    sys.stdout.buffer.write(payload + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
