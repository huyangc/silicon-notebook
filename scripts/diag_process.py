#!/usr/bin/env python3
"""Collect bounded, metadata-only process evidence for host diagnostics.

This module intentionally uses only the Python standard library and never
imports the backend application.  It may write only bounded files inside the
operator-provided diagnostics directory.
"""

from __future__ import annotations

import argparse
import errno
import json
import math
import os
import re
import signal
import socket
import sys
import time
import urllib.error
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
_DUMP_WAIT_SECONDS = 1.0
_DUMP_MAX_BYTES = 512 * 1024
_DUMP_FILE_MAX_BYTES = 8 * 1024 * 1024
_MAX_DUMP_THREADS = 256
_MAX_FRAMES_PER_THREAD = 64
_MAX_PARSED_DUMP_BYTES = 256 * 1024
_HTTP_TIMEOUT_SECONDS = 0.75
_HTTP_RESULT_KEYS = ("role", "status", "elapsed_ms", "result")
_THREAD_HEADER = re.compile(
    r"^(?:Current thread|Thread) 0x([0-9a-fA-F]+)(?: \([^\n]*\))?:\s*$"
)
_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+) in ([^\r\n]+)\s*$')
_ALLOWED_LIBRARY_DIRECTORIES = frozenset(
    {"sqlite3", "asyncio", "anyio", "urllib", "httpx", "openai"}
)
_ALLOWED_LIBRARY_FILES = frozenset({"threading.py", "shutil.py", "os.py"})
_RUNTIME_LIST_FIELDS = (
    "active_requests",
    "active_sql",
    "active_jobs",
    "recent_jobs",
)
_RUNTIME_DICT_FIELDS = ("readiness", "concurrency", "write_lock")


def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


class ProcAdapter:
    """Small, bounded adapter around the permitted per-process proc entries."""

    def __init__(self, root: Path | str = "/proc") -> None:
        self.root = Path(root)

    def _process_path(self, pid: int, leaf: str) -> Path:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("invalid pid")
        if leaf not in {"cmdline", "cwd", "stat", "status", "io", "fd", "task"}:
            raise ValueError("unsupported proc entry")
        return self.root / str(pid) / leaf

    def _read_bytes(self, pid: int, leaf: str) -> Optional[bytes]:
        try:
            with self._process_path(pid, leaf).open("rb") as handle:
                return handle.read(_MAX_PROC_FILE_BYTES + 1)[:_MAX_PROC_FILE_BYTES]
        except (OSError, ValueError):
            return None

    def list_pids(self) -> tuple[int, ...]:
        try:
            result: list[int] = []
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if len(result) >= _MAX_PIDS:
                        break
                    if not entry.name.isascii() or not entry.name.isdecimal():
                        continue
                    pid = _safe_int(entry.name)
                    if pid > 0:
                        result.append(pid)
            return tuple(sorted(result))
        except OSError:
            return ()

    def exists(self, pid: int) -> bool:
        return self.read_stat(pid) is not None

    def read_cmdline(self, pid: int) -> tuple[str, ...]:
        raw = self._read_bytes(pid, "cmdline")
        if raw is None:
            return ()
        return tuple(
            part.decode("utf-8", "replace")[:4_096]
            for part in raw.split(b"\0")[:128]
            if part
        )

    def read_cwd(self, pid: int) -> Optional[Path]:
        try:
            target = os.readlink(self._process_path(pid, "cwd"))
            return Path(target)
        except (OSError, ValueError):
            return None

    def read_stat(self, pid: int) -> Optional[str]:
        raw = self._read_bytes(pid, "stat")
        return None if raw is None else raw.decode("ascii", "replace")

    def read_status(self, pid: int) -> Optional[str]:
        raw = self._read_bytes(pid, "status")
        return None if raw is None else raw.decode("ascii", "replace")

    def read_io(self, pid: int) -> Optional[str]:
        raw = self._read_bytes(pid, "io")
        return None if raw is None else raw.decode("ascii", "replace")

    def count_fds(self, pid: int) -> int:
        try:
            count = 0
            with os.scandir(self._process_path(pid, "fd")) as entries:
                for _entry in entries:
                    count += 1
                    if count >= _MAX_FDS:
                        break
            return count
        except (OSError, ValueError):
            return 0

    def task_stats(self, pid: int) -> tuple[str, ...]:
        try:
            result: list[str] = []
            task_root = self._process_path(pid, "task")
            with os.scandir(task_root) as entries:
                for entry in entries:
                    if len(result) >= _MAX_TASKS:
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

    def identity(self, pid: int) -> Optional[dict[str, int]]:
        parsed = parse_proc_stat(self.read_stat(pid) or "")
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


def _safe_identity(proc: Any, pid: int) -> Optional[dict[str, int]]:
    try:
        identity = proc.identity(pid)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(identity, dict):
        return None
    identity_pid = identity.get("pid")
    starttime = identity.get("starttime_ticks")
    if identity_pid != pid or isinstance(starttime, bool) or not isinstance(starttime, int):
        return None
    return {"pid": pid, "starttime_ticks": starttime}


def resolve_backend_pid(
    root: Path | str,
    pid: Optional[int] = None,
    *,
    proc: Optional[Any] = None,
    self_pid: Optional[int] = None,
) -> dict[str, Any]:
    """Resolve one Uvicorn process without returning its raw command line."""

    adapter = proc or ProcAdapter()
    repo = Path(root).resolve(strict=False)
    own_pid = os.getpid() if self_pid is None else self_pid
    source = "operator" if pid is not None else "auto"

    if pid is not None:
        if pid == own_pid:
            return {"status": "missing", "pid": None, "source": source, "candidates": []}
        try:
            exists = adapter.exists(pid)
        except (OSError, ValueError, TypeError, KeyError):
            exists = False
        identity = _safe_identity(adapter, pid) if exists else None
        if not exists or identity is None:
            return {"status": "missing", "pid": None, "source": source, "candidates": []}
        return {
            "status": "ok",
            "pid": pid,
            "source": source,
            "identity": identity,
            "candidates": [],
        }

    candidates: list[dict[str, Any]] = []
    identities: dict[int, dict[str, int]] = {}
    try:
        pids: Iterable[int] = adapter.list_pids()
    except (OSError, ValueError, TypeError):
        pids = ()
    try:
        for index, candidate_pid in enumerate(pids):
            if index >= _MAX_PIDS:
                break
            if candidate_pid == own_pid:
                continue
            try:
                command = adapter.read_cmdline(candidate_pid)
                cwd = adapter.read_cwd(candidate_pid)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
            if not isinstance(command, (tuple, list)):
                continue
            command_tokens = tuple(str(part) for part in command[:128])
            has_uvicorn = any(
                token == "uvicorn" or Path(token).name == "uvicorn"
                for token in command_tokens
            )
            if not has_uvicorn or "app.main:app" not in command_tokens:
                continue
            safe_cwd = _candidate_cwd(repo, cwd)
            if safe_cwd is None:
                continue
            identity = _safe_identity(adapter, candidate_pid)
            if identity is None:
                continue
            candidates.append({"pid": candidate_pid, "cwd": safe_cwd})
            identities[candidate_pid] = identity
    except (OSError, ValueError, TypeError, KeyError):
        pass

    candidates.sort(key=lambda item: item["pid"])
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
    proc: Optional[Any] = None,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    clock_ticks: Optional[int] = None,
    page_size: Optional[int] = None,
) -> dict[str, Any]:
    """Take two bounded proc samples and return numeric process metadata."""

    adapter = proc or ProcAdapter()
    ticks_per_second = clock_ticks or int(os.sysconf("SC_CLK_TCK"))
    system_page_size = page_size or int(os.sysconf("SC_PAGE_SIZE"))
    try:
        first = parse_proc_stat(adapter.read_stat(pid) or "")
        first_at = float(clock())
        if first is None:
            return {"pid": pid, "status": "unavailable"}
        sleeper(_SAMPLE_SECONDS)
        second = parse_proc_stat(adapter.read_stat(pid) or "")
        second_at = float(clock())
        if second is None or first["starttime_ticks"] != second["starttime_ticks"]:
            return {"pid": pid, "status": "identity_changed"}
        status = _key_values(adapter.read_status(pid))
        io_values = _key_values(adapter.read_io(pid))
        d_state_threads = 0
        for task_stat in tuple(adapter.task_stats(pid))[:_MAX_TASKS]:
            parsed_task = parse_proc_stat(task_stat)
            if parsed_task is not None and parsed_task["state"] == "D":
                d_state_threads += 1
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
            "fds": max(0, min(_MAX_FDS, int(adapter.count_fds(pid)))),
            "read_bytes": _number_prefix(io_values.get("read_bytes", "")),
            "write_bytes": _number_prefix(io_values.get("write_bytes", "")),
            "d_state_threads": d_state_threads,
            "uptime_seconds": round(uptime_seconds, 3),
        }
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


def load_runtime_snapshot(
    path: Path | str,
    pid: int,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Read and validate one bounded runtime heartbeat snapshot."""

    snapshot_path = Path(path)
    try:
        with snapshot_path.open("rb") as handle:
            raw = handle.read(_SNAPSHOT_MAX_BYTES + 1)
    except OSError:
        return {"status": "missing", "fresh": False, "snapshot": None}
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
    if snapshot.get("schema_version") != 1:
        return {"status": "unsupported_schema", "fresh": False, "snapshot": None}
    if snapshot.get("pid") != pid:
        return {"status": "pid_mismatch", "fresh": False, "snapshot": None}
    if any(not isinstance(snapshot.get(field), list) for field in _RUNTIME_LIST_FIELDS):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    if any(not isinstance(snapshot.get(field), dict) for field in _RUNTIME_DICT_FIELDS):
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    heartbeat = _parse_utc(snapshot.get("heartbeat_at"))
    if heartbeat is None:
        return {"status": "invalid_shape", "fresh": False, "snapshot": None}
    captured_at = now or datetime.now(timezone.utc)
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    age = max(0.0, (captured_at.astimezone(timezone.utc) - heartbeat).total_seconds())
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
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Probe liveness without reading or retaining any response body."""

    started = clock()
    status: Optional[int] = None
    result = "error"
    try:
        with opener(
            url, timeout=min(_HTTP_TIMEOUT_SECONDS, max(0.001, timeout))
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
    elapsed_ms = min(750, max(0, int(round((clock() - started) * 1_000))))
    return dict(zip(_HTTP_RESULT_KEYS, (str(role)[:32], status, elapsed_ms, result)))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _identity_before_signal(proc: Optional[Any], pid: int) -> Optional[dict[str, int]]:
    if proc is not None:
        return _safe_identity(proc, pid)
    if sys.platform.startswith("linux"):
        return _safe_identity(ProcAdapter(), pid)
    return {"pid": pid, "starttime_ticks": -1} if _pid_alive(pid) else None


def _sigusr1_is_caught(proc: Any, pid: int) -> bool:
    try:
        status = _key_values(proc.read_status(pid))
        caught = int(status.get("SigCgt", ""), 16)
    except (AttributeError, OSError, TypeError, ValueError, KeyError):
        return False
    signal_number = int(signal.SIGUSR1)
    return bool(caught & (1 << (signal_number - 1)))


def _capture_result(status: str, **fields: Any) -> dict[str, Any]:
    result = {"status": status, "identity_verified": status == "ok", "dump": ""}
    result.update(fields)
    return result


def capture_thread_dump(
    pid: int,
    diagnostics_dir: Path | str,
    *,
    proc: Optional[Any] = None,
    expected_identity: Optional[dict[str, int]] = None,
    kill_fn: Callable[[int, int], None] = os.kill,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    platform: Optional[str] = None,
    timeout: float = _DUMP_WAIT_SECONDS,
) -> dict[str, Any]:
    """Request the already-installed non-terminating SIGUSR1 dump handler."""

    if not hasattr(signal, "SIGUSR1"):
        return _capture_result("unsupported")
    if fcntl is None:
        return _capture_result("unsupported")
    platform_name = sys.platform if platform is None else platform
    if platform_name.startswith("win"):
        return _capture_result("unsupported")
    directory = Path(diagnostics_dir)
    lock_path = directory / "incident.lock"
    dump_path = directory / "thread-dumps.log"
    try:
        if directory.is_symlink():
            return _capture_result("unsafe_path")
        directory.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink() or dump_path.is_symlink():
            return _capture_result("unsafe_path")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | nofollow,
            0o600,
        )
        deadline = clock() + min(_DUMP_WAIT_SECONDS, max(0.001, float(timeout)))
        with os.fdopen(lock_fd, "a+b") as lock_handle:
            while True:
                try:
                    fcntl.flock(
                        lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                    break
                except BlockingIOError:
                    if clock() >= deadline:
                        return _capture_result("lock_timeout")
                    sleeper(min(0.02, max(0.0, deadline - clock())))
            before_identity = _identity_before_signal(proc, pid)
            if before_identity is None:
                return _capture_result("missing_process")
            if expected_identity is not None and before_identity != expected_identity:
                return _capture_result("identity_changed")
            try:
                dump_fd = os.open(dump_path, os.O_RDWR | nofollow)
            except OSError:
                return _capture_result("dump_unavailable")
            with os.fdopen(dump_fd, "r+b", buffering=0) as dump_handle:
                offset = os.fstat(dump_handle.fileno()).st_size
                # Re-read immediately before signaling to narrow PID-reuse exposure.
                immediate_identity = _identity_before_signal(proc, pid)
                if immediate_identity != before_identity:
                    return _capture_result("identity_changed")
                if platform_name.startswith("linux"):
                    signal_proc = proc or ProcAdapter()
                    if not _sigusr1_is_caught(signal_proc, pid):
                        return _capture_result("signal_unavailable")
                try:
                    kill_fn(pid, signal.SIGUSR1)
                except (OSError, ValueError):
                    return _capture_result("signal_failed")
                size = offset
                while clock() < deadline:
                    try:
                        size = os.fstat(dump_handle.fileno()).st_size
                    except OSError:
                        return _capture_result("dump_unavailable")
                    if size > offset:
                        break
                    sleeper(min(0.02, max(0.0, deadline - clock())))
                if size <= offset:
                    return _capture_result("timeout", offset=offset)
                after_identity = _identity_before_signal(proc, pid)
                if after_identity != before_identity:
                    return _capture_result("identity_changed", offset=offset)
                try:
                    dump_handle.seek(offset)
                    appended = dump_handle.read(_DUMP_MAX_BYTES + 1)[:_DUMP_MAX_BYTES]
                except OSError:
                    return _capture_result("dump_unavailable", offset=offset)
                truncated = size - offset > _DUMP_MAX_BYTES
                if size > _DUMP_FILE_MAX_BYTES:
                    try:
                        os.ftruncate(dump_handle.fileno(), 0)
                    except OSError:
                        pass
                return _capture_result(
                    "ok",
                    offset=offset,
                    bytes=len(appended),
                    truncated=truncated,
                    dump=appended.decode("utf-8", "replace"),
                )
    except OSError:
        return _capture_result("lock_unavailable")


def _sanitized_frame(line: str, repo: Path) -> Optional[str]:
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
    elif (
        path.name.lower() in _ALLOWED_LIBRARY_FILES
        or any(
            part.lower() in _ALLOWED_LIBRARY_DIRECTORIES
            for part in path.parts[:-1]
        )
    ):
        safe_path = "<library>/" + path.name
    else:
        return None
    return f"{safe_path}:{_safe_int(line_number)} in {safe_function}"


def parse_thread_dump(dump: str | bytes, repo_root: Path | str) -> dict[str, list[str]]:
    """Return bounded frame metadata, omitting all non-allow-listed paths."""

    if isinstance(dump, bytes):
        text = dump[:_DUMP_MAX_BYTES].decode("utf-8", "replace")
    else:
        text = str(dump).encode("utf-8", "replace")[:_DUMP_MAX_BYTES].decode(
            "utf-8", "replace"
        )
    repo = Path(repo_root)
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
        safe = _sanitized_frame(line, repo)
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
    resolution = resolve_backend_pid(root, args.pid)
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
            probe_http("backend", args.backend_url),
            probe_http("frontend", args.frontend_url),
        ],
    }
    selected_pid = resolution.get("pid")
    if isinstance(selected_pid, int):
        evidence["process"] = sample_process(selected_pid)
        runtime = load_runtime_snapshot(
            diagnostics / "runtime.json", selected_pid
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
