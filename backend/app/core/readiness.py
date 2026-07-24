"""Process-wide startup readiness state.

The backend defers all heavy first-use cost to the first request: constructing
the repository runs schema migrations (a pending ``CREATE INDEX`` on a large DB
is multi-second and silent) and the per-process count caches
(``knowledge_counts_cache``) start empty, while scale-index CSR/ANN and reusable
single-index PPR cores are also process-local. Without startup preloading, the
first large-library query pays that multi-GB cold load. Those costs otherwise
land on whoever logs in or asks first after a restart.

Instead we run migration + a cache warm pass in a background thread at startup
and gate every app route behind ``ready`` until it finishes — the frontend shows
a "服务启动中" screen and users never hit the cold path. Migration or strict
scale-artifact preload failure keeps the service not-ready; per-notebook
count-cache warm failures remain best-effort.

Single-process backend (uvicorn --workers 1), so a module-global guarded by one
lock is the whole story.
"""
from __future__ import annotations

import threading
from typing import Optional

from app.core.database_url import database_status

_lock = threading.Lock()
_state = {
    "ready": False,
    # Active cycle: starting -> migrating -> warming -> preloading_indexes
    # -> ready | error.
    # Lifespan exit publishes the separate terminal ``stopped`` state.
    "phase": "starting",
    "detail": "",
    "error": None,          # str when phase == "error"
    "warmed_notebooks": 0,
    "total_notebooks": 0,
    "preloaded_indexes": 0,
    "total_indexes": 0,
}


def set_phase(phase: str, detail: str = "") -> None:
    with _lock:
        _state["phase"] = phase
        _state["detail"] = detail


def set_detail(detail: str, *, warmed: Optional[int] = None,
               total: Optional[int] = None,
               preloaded: Optional[int] = None,
               total_indexes: Optional[int] = None) -> None:
    with _lock:
        _state["detail"] = detail
        if warmed is not None:
            _state["warmed_notebooks"] = warmed
        if total is not None:
            _state["total_notebooks"] = total
        if preloaded is not None:
            _state["preloaded_indexes"] = preloaded
        if total_indexes is not None:
            _state["total_indexes"] = total_indexes


def mark_ready() -> None:
    with _lock:
        _state["ready"] = True
        _state["phase"] = "ready"
        _state["detail"] = ""
        _state["error"] = None


def startup_error(error: BaseException, database_url: str) -> str:
    """Return a credential-free startup diagnostic and backend identity."""
    return f"{type(error).__name__}: database initialization failed ({database_status(database_url)})"


def mark_error(error: str) -> None:
    with _lock:
        _state["ready"] = False
        _state["phase"] = "error"
        _state["error"] = error


def mark_stopped() -> None:
    """Publish the post-lifespan state without retaining stale readiness."""
    with _lock:
        _state.update(
            ready=False,
            phase="stopped",
            detail="",
            error=None,
            warmed_notebooks=0,
            total_notebooks=0,
            preloaded_indexes=0,
            total_indexes=0,
        )


def is_ready() -> bool:
    with _lock:
        return bool(_state["ready"])


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def reset() -> None:
    """Begin a fresh lifecycle in the pre-startup, not-ready state."""
    with _lock:
        _state.update(
            ready=False,
            phase="starting",
            detail="",
            error=None,
            warmed_notebooks=0,
            total_notebooks=0,
            preloaded_indexes=0,
            total_indexes=0,
        )
