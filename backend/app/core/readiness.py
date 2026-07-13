"""Process-wide startup readiness state.

The backend defers all heavy first-use cost to the first request: constructing
the repository runs schema migrations (a pending ``CREATE INDEX`` on a large DB
is multi-second and silent) and the per-process count caches
(``knowledge_counts_cache``) start empty, so the first ``GET /notebooks`` pays a
cold recompute per notebook. That lands on whoever logs in first after a restart.

Instead we run migration + a cache warm pass in a background thread at startup
and gate every app route behind ``ready`` until it finishes — the frontend shows
a "服务启动中" screen and users never hit the cold path. Migration failure keeps
the service not-ready (the app is unusable on an un-migrated schema); per-notebook
warm failures are best-effort and do not block readiness.

Single-process backend (uvicorn --workers 1), so a module-global guarded by one
lock is the whole story.
"""
from __future__ import annotations

import threading
from typing import Optional

_lock = threading.Lock()
_state = {
    "ready": False,
    "phase": "starting",   # starting -> migrating -> warming -> ready | error
    "detail": "",
    "error": None,          # str when phase == "error"
    "warmed_notebooks": 0,
    "total_notebooks": 0,
}


def set_phase(phase: str, detail: str = "") -> None:
    with _lock:
        _state["phase"] = phase
        _state["detail"] = detail


def set_detail(detail: str, *, warmed: Optional[int] = None,
               total: Optional[int] = None) -> None:
    with _lock:
        _state["detail"] = detail
        if warmed is not None:
            _state["warmed_notebooks"] = warmed
        if total is not None:
            _state["total_notebooks"] = total


def mark_ready() -> None:
    with _lock:
        _state["ready"] = True
        _state["phase"] = "ready"
        _state["detail"] = ""
        _state["error"] = None


def mark_error(error: str) -> None:
    with _lock:
        _state["ready"] = False
        _state["phase"] = "error"
        _state["error"] = error


def is_ready() -> bool:
    with _lock:
        return bool(_state["ready"])


def snapshot() -> dict:
    with _lock:
        return dict(_state)


def reset() -> None:
    """Back to the pre-startup not-ready state. For tests exercising the gate;
    the process default is already not-ready until the lifespan warm-up runs."""
    with _lock:
        _state.update(ready=False, phase="starting", detail="", error=None,
                      warmed_notebooks=0, total_notebooks=0)
