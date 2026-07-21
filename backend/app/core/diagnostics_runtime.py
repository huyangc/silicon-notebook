"""Bounded, metadata-only process diagnostics for live incident inspection.

The runtime is deliberately independent from FastAPI and the repository layer.
Call sites use the module-level helpers, which become no-ops unless one runtime
is installed for the process.
"""

from __future__ import annotations

import contextvars
import faulthandler
import hashlib
import itertools
import json
import os
import re
import signal
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


SCHEMA_VERSION = 1
_MAX_ACTIVE = 1_000
_MAX_RECENT_JOBS = 100
_NOTEBOOK_ROUTE = re.compile(r"^/api/notebooks/([^/?#]+)(?:/.*)?$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SqlMetadata:
    verb: str
    table: str
    fingerprint: str


def normalize_sql_metadata(sql: str) -> SqlMetadata:
    collapsed = " ".join(str(sql).strip().split())
    scrubbed = re.sub(r"'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b", "?", collapsed)
    verb_match = re.match(r"(?i)^([A-Z]+)", scrubbed)
    table_match = re.search(
        r"(?i)\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)",
        scrubbed,
    )
    return SqlMetadata(
        verb=verb_match.group(1).upper() if verb_match else "UNKNOWN",
        table=table_match.group(1) if table_match else "",
        fingerprint=hashlib.sha256(
            scrubbed.upper().encode("utf-8", "replace")
        ).hexdigest()[:12],
    )


@dataclass(frozen=True)
class _DiagnosticContext:
    request_id: Optional[str] = None
    job_id: Optional[str] = None
    notebook_id: Optional[str] = None
    phase: Optional[str] = None


_diagnostic_context: contextvars.ContextVar[_DiagnosticContext] = (
    contextvars.ContextVar("diagnostic_context", default=_DiagnosticContext())
)
_runtime_lock = threading.Lock()
_installed_runtime: Optional["DiagnosticsRuntime"] = None


def _context_fields(context: _DiagnosticContext) -> dict[str, Optional[str]]:
    return {
        "request_id": context.request_id,
        "job_id": context.job_id,
        "notebook_id": context.notebook_id,
        "phase": context.phase,
    }


def _request_metadata(path: str) -> tuple[str, Optional[str]]:
    path_only = str(path).split("?", 1)[0].split("#", 1)[0]
    match = _NOTEBOOK_ROUTE.match(path_only)
    if match is not None:
        return "/api/notebooks/{id}", match.group(1)
    return path_only, None


def _numeric_tree(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, dict) and depth < 3:
        return {
            str(key)[:80]: normalized
            for key, item in value.items()
            if (normalized := _numeric_tree(item, depth=depth + 1)) is not None
        }
    return None


class DiagnosticsRuntime:
    """Own the bounded registries and heartbeat files for one process."""

    def __init__(
        self,
        diagnostics_dir: Path,
        readiness_provider: Callable[[], dict[str, Any]],
        concurrency_provider: Callable[[], dict[str, Any]],
        *,
        interval_seconds: float = 2.0,
        enable_signal: bool = True,
    ) -> None:
        self.diagnostics_dir = Path(diagnostics_dir)
        self._readiness_provider = readiness_provider
        self._concurrency_provider = concurrency_provider
        self._interval_seconds = max(0.001, float(interval_seconds))
        self._enable_signal = bool(enable_signal)

        self._lock = threading.Lock()
        self._active_requests: dict[int, dict[str, Any]] = {}
        self._active_sql: dict[int, dict[str, Any]] = {}
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._write_waiters: dict[int, dict[str, Any]] = {}
        self._write_holder: Optional[dict[str, Any]] = None
        self._recent_jobs: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_JOBS)
        self._ids = itertools.count(1)

        started = _utc_now()
        self._process_started_at = started
        self._last_state_change_at = started
        self._state_revision = 1
        self._snapshot_failures = 0

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dump_handle: Any = None
        self._owns_signal = False
        self.signal_capture_available = False

    def _changed_locked(self) -> None:
        self._state_revision += 1
        self._last_state_change_at = _utc_now()
        self._wake.set()

    def _snapshot_failed(self, count: int = 1) -> None:
        with self._lock:
            self._snapshot_failures += count
            self._state_revision += 1
            self._last_state_change_at = _utc_now()

    def _bounded_add_locked(
        self, registry: dict[int, dict[str, Any]], entry: dict[str, Any]
    ) -> Optional[int]:
        if len(registry) >= _MAX_ACTIVE:
            return None
        identifier = next(self._ids)
        registry[identifier] = entry
        self._changed_locked()
        return identifier

    @staticmethod
    def _active_entry(**values: Any) -> dict[str, Any]:
        return {
            **values,
            "thread_id": threading.get_ident(),
            "started_at": _utc_now(),
            "_started_monotonic": time.monotonic(),
        }

    def _enter_request(
        self, request_id: str, method: str, path: str
    ) -> tuple[Optional[int], contextvars.Token[_DiagnosticContext]]:
        normalized_path, notebook_id = _request_metadata(path)
        previous = _diagnostic_context.get()
        context = _DiagnosticContext(
            request_id=str(request_id),
            job_id=previous.job_id,
            notebook_id=notebook_id,
            phase=previous.phase,
        )
        context_token = _diagnostic_context.set(context)
        entry = self._active_entry(
            method=str(method).upper()[:16],
            path=normalized_path[:256],
            **_context_fields(context),
        )
        with self._lock:
            token = self._bounded_add_locked(self._active_requests, entry)
        return token, context_token

    def _exit_request(
        self,
        token: Optional[int],
        context_token: contextvars.Token[_DiagnosticContext],
    ) -> None:
        try:
            if token is not None:
                with self._lock:
                    if self._active_requests.pop(token, None) is not None:
                        self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def _enter_phase(
        self, phase: str
    ) -> tuple[contextvars.Token[_DiagnosticContext], list[tuple[dict[int, dict[str, Any]], int, Any]]]:
        previous = _diagnostic_context.get()
        context = _DiagnosticContext(
            request_id=previous.request_id,
            job_id=previous.job_id,
            notebook_id=previous.notebook_id,
            phase=str(phase)[:160],
        )
        context_token = _diagnostic_context.set(context)
        changed: list[tuple[dict[int, dict[str, Any]], int, Any]] = []
        thread_id = threading.get_ident()
        with self._lock:
            for registry in (
                self._active_requests,
                self._active_jobs,
                self._active_sql,
                self._write_waiters,
            ):
                for identifier, entry in registry.items():
                    if entry["thread_id"] != thread_id:
                        continue
                    if context.request_id is not None and entry.get("request_id") != context.request_id:
                        continue
                    if context.job_id is not None and entry.get("job_id") != context.job_id:
                        continue
                    changed.append((registry, identifier, entry.get("phase")))
                    entry["phase"] = context.phase
            if self._write_holder is not None and self._write_holder["thread_id"] == thread_id:
                changed.append(({}, -1, self._write_holder.get("phase")))
                self._write_holder["phase"] = context.phase
            if changed:
                self._changed_locked()
        return context_token, changed

    def _exit_phase(
        self,
        context_token: contextvars.Token[_DiagnosticContext],
        changed: list[tuple[dict[int, dict[str, Any]], int, Any]],
    ) -> None:
        try:
            with self._lock:
                restored = False
                for registry, identifier, old_phase in changed:
                    if identifier == -1:
                        if self._write_holder is not None:
                            self._write_holder["phase"] = old_phase
                            restored = True
                        continue
                    entry = registry.get(identifier)
                    if entry is not None:
                        entry["phase"] = old_phase
                        restored = True
                if restored:
                    self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def _enter_sql(self, mode: str, sql: str, operation: str) -> Optional[int]:
        metadata = normalize_sql_metadata(sql)
        entry = self._active_entry(
            mode=str(mode)[:24],
            operation=str(operation)[:160],
            verb=metadata.verb,
            table=metadata.table,
            fingerprint=metadata.fingerprint,
            **_context_fields(_diagnostic_context.get()),
        )
        with self._lock:
            return self._bounded_add_locked(self._active_sql, entry)

    def _exit_sql(self, token: Optional[int]) -> None:
        if token is None:
            return
        with self._lock:
            if self._active_sql.pop(token, None) is not None:
                self._changed_locked()

    def _enter_job(
        self, name: str
    ) -> tuple[Optional[int], contextvars.Token[_DiagnosticContext]]:
        previous = _diagnostic_context.get()
        job_id = f"job-{next(self._ids)}"
        context = _DiagnosticContext(
            request_id=previous.request_id,
            job_id=job_id,
            notebook_id=previous.notebook_id,
            phase=previous.phase,
        )
        context_token = _diagnostic_context.set(context)
        entry = self._active_entry(
            name=str(name)[:160],
            **_context_fields(context),
        )
        with self._lock:
            token = self._bounded_add_locked(self._active_jobs, entry)
        return token, context_token

    def _exit_job(
        self,
        token: Optional[int],
        context_token: contextvars.Token[_DiagnosticContext],
        status: str,
    ) -> None:
        try:
            if token is None:
                return
            with self._lock:
                entry = self._active_jobs.pop(token, None)
                if entry is None:
                    return
                finished = dict(entry)
                finished["status"] = status
                finished["completed_at"] = _utc_now()
                finished["duration_ms"] = round(
                    (time.monotonic() - finished.pop("_started_monotonic")) * 1_000,
                    3,
                )
                self._recent_jobs.append(finished)
                self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def begin_write_wait(self, operation: str) -> Optional[int]:
        thread_id = threading.get_ident()
        with self._lock:
            if (
                self._write_holder is not None
                and self._write_holder["thread_id"] == thread_id
            ):
                return None
            entry = self._active_entry(
                operation=str(operation)[:160],
                **_context_fields(_diagnostic_context.get()),
            )
            return self._bounded_add_locked(self._write_waiters, entry)

    def write_wait_cancelled(self, waiter: object) -> None:
        if not isinstance(waiter, int):
            return
        with self._lock:
            if self._write_waiters.pop(waiter, None) is not None:
                self._changed_locked()

    def write_acquired(self, waiter: object, operation: str) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if (
                self._write_holder is not None
                and self._write_holder["thread_id"] == thread_id
            ):
                self._write_holder["depth"] += 1
                self._changed_locked()
                return
            if isinstance(waiter, int):
                self._write_waiters.pop(waiter, None)
            self._write_holder = self._active_entry(
                operation=str(operation)[:160],
                depth=1,
                **_context_fields(_diagnostic_context.get()),
            )
            self._changed_locked()

    def write_released(self) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if self._write_holder is None or self._write_holder["thread_id"] != thread_id:
                return
            self._write_holder["depth"] -= 1
            if self._write_holder["depth"] <= 0:
                self._write_holder = None
            self._changed_locked()

    @staticmethod
    def _render_entry(entry: dict[str, Any], now: float) -> dict[str, Any]:
        rendered = {
            key: value for key, value in entry.items() if key != "_started_monotonic"
        }
        rendered["duration_ms"] = round(
            max(0.0, now - entry["_started_monotonic"]) * 1_000, 3
        )
        return rendered

    def _provider_snapshot(self) -> tuple[dict[str, Any], dict[str, Any]]:
        failures = 0
        try:
            raw_readiness = self._readiness_provider()
        except Exception:
            raw_readiness = {}
            failures += 1
        try:
            raw_concurrency = self._concurrency_provider()
        except Exception:
            raw_concurrency = {}
            failures += 1
        if failures:
            self._snapshot_failed(failures)

        readiness: dict[str, Any] = {}
        if isinstance(raw_readiness, dict):
            if isinstance(raw_readiness.get("ready"), bool):
                readiness["ready"] = raw_readiness["ready"]
            if isinstance(raw_readiness.get("phase"), str):
                readiness["phase"] = raw_readiness["phase"][:80]
            for key in ("warmed_notebooks", "total_notebooks"):
                value = raw_readiness.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    readiness[key] = value

        concurrency = _numeric_tree(raw_concurrency)
        return readiness, concurrency if isinstance(concurrency, dict) else {}

    def snapshot(self) -> dict[str, Any]:
        readiness, concurrency = self._provider_snapshot()
        now = time.monotonic()
        with self._lock:
            active_requests = [
                self._render_entry(entry, now)
                for entry in self._active_requests.values()
            ]
            active_sql = [
                self._render_entry(entry, now) for entry in self._active_sql.values()
            ]
            active_jobs = [
                self._render_entry(entry, now) for entry in self._active_jobs.values()
            ]
            waiters = [
                self._render_entry(entry, now)
                for entry in self._write_waiters.values()
            ]
            holder = (
                None
                if self._write_holder is None
                else self._render_entry(self._write_holder, now)
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "pid": os.getpid(),
                "process_started_at": self._process_started_at,
                "heartbeat_at": _utc_now(),
                "last_state_change_at": self._last_state_change_at,
                "state_revision": self._state_revision,
                "snapshot_failures": self._snapshot_failures,
                "readiness": readiness,
                "concurrency": concurrency,
                "active_requests": active_requests,
                "active_sql": active_sql,
                "write_lock": {"holder": holder, "waiters": waiters},
                "active_jobs": active_jobs,
                "recent_jobs": list(self._recent_jobs),
            }

    def _write_snapshot(self) -> None:
        try:
            snapshot = self.snapshot()
            temporary = self.diagnostics_dir / "runtime.json.tmp"
            destination = self.diagnostics_dir / "runtime.json"
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    snapshot,
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except Exception:
            self._snapshot_failed()

    def _writer(self) -> None:
        next_write = time.monotonic()
        while not self._stop.is_set():
            remaining = max(0.0, next_write - time.monotonic())
            self._wake.wait(remaining)
            self._wake.clear()
            if self._stop.is_set():
                break
            if time.monotonic() < next_write:
                continue
            self._write_snapshot()
            next_write = time.monotonic() + self._interval_seconds

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
        try:
            self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            dump_path = self.diagnostics_dir / "thread-dumps.log"
            dump_path.write_bytes(b"")
            self._dump_handle = dump_path.open("a", encoding="utf-8", buffering=1)
        except Exception:
            self._snapshot_failed()
            self._dump_handle = None

        self.signal_capture_available = False
        if (
            self._enable_signal
            and os.name == "posix"
            and hasattr(signal, "SIGUSR1")
            and threading.current_thread() is threading.main_thread()
            and self._dump_handle is not None
        ):
            try:
                faulthandler.register(
                    signal.SIGUSR1,
                    file=self._dump_handle,
                    all_threads=True,
                    chain=False,
                )
                self._owns_signal = True
                self.signal_capture_available = True
            except Exception:
                self._snapshot_failed()

        self._stop.clear()
        self._wake.set()
        thread = threading.Thread(
            target=self._writer,
            name="diagnostics-snapshot",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2 * self._interval_seconds)
        if self._owns_signal:
            try:
                faulthandler.unregister(signal.SIGUSR1)
            except Exception:
                pass
            self._owns_signal = False
            self.signal_capture_available = False
        handle = self._dump_handle
        self._dump_handle = None
        if handle is not None:
            try:
                handle.flush()
                handle.close()
            except Exception:
                pass
        with self._lock:
            self._thread = None


def current_runtime() -> Optional[DiagnosticsRuntime]:
    with _runtime_lock:
        return _installed_runtime


@contextmanager
def install_runtime(runtime: DiagnosticsRuntime) -> Iterator[DiagnosticsRuntime]:
    global _installed_runtime
    installed_here = False
    with _runtime_lock:
        if _installed_runtime is None:
            _installed_runtime = runtime
            installed_here = True
        elif _installed_runtime is not runtime:
            raise RuntimeError("cannot install a different diagnostics runtime")
    try:
        yield runtime
    finally:
        if installed_here:
            with _runtime_lock:
                if _installed_runtime is runtime:
                    _installed_runtime = None


@contextmanager
def activate_runtime(
    diagnostics_dir: Path,
    readiness_provider: Callable[[], dict[str, Any]],
    concurrency_provider: Callable[[], dict[str, Any]],
    *,
    interval_seconds: float = 2.0,
    enable_signal: bool = True,
) -> Iterator[DiagnosticsRuntime]:
    runtime = DiagnosticsRuntime(
        diagnostics_dir,
        readiness_provider,
        concurrency_provider,
        interval_seconds=interval_seconds,
        enable_signal=enable_signal,
    )
    runtime.start()
    try:
        with install_runtime(runtime):
            yield runtime
    finally:
        runtime.stop()


@contextmanager
def request_scope(request_id: str, method: str, path: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    if runtime is not None:
        try:
            entered = runtime._enter_request(request_id, method, path)
        except Exception:
            entered = None
    try:
        yield
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_request(*entered)
            except Exception:
                pass


@contextmanager
def diagnostic_phase(phase: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    if runtime is not None:
        try:
            entered = runtime._enter_phase(phase)
        except Exception:
            entered = None
    try:
        yield
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_phase(*entered)
            except Exception:
                pass


@contextmanager
def sql_scope(mode: str, sql: str, operation: str) -> Iterator[None]:
    runtime = current_runtime()
    token = None
    if runtime is not None:
        try:
            token = runtime._enter_sql(mode, sql, operation)
        except Exception:
            token = None
    try:
        yield
    finally:
        if runtime is not None:
            try:
                runtime._exit_sql(token)
            except Exception:
                pass


@contextmanager
def job_scope(name: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    status = "done"
    if runtime is not None:
        try:
            entered = runtime._enter_job(name)
        except Exception:
            entered = None
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_job(*entered, status)
            except Exception:
                pass


def begin_write_wait(operation: str) -> object:
    runtime = current_runtime()
    if runtime is None:
        return None
    try:
        return runtime.begin_write_wait(operation)
    except Exception:
        return None


def write_wait_cancelled(waiter: object) -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_wait_cancelled(waiter)
    except Exception:
        pass


def write_acquired(waiter: object, operation: str) -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_acquired(waiter, operation)
    except Exception:
        pass


def write_released() -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_released()
    except Exception:
        pass
