"""Context-local, content-free progress for relink observers (CLI and jobs)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

RelinkProgressObserver = Callable[[dict], None]
_observers: ContextVar[tuple[RelinkProgressObserver, ...]] = ContextVar(
    "relink_progress_observers", default=(),
)
_COUNT_FIELDS = (
    "processed_sources", "total_sources", "pending_sources", "edges_added",
)


@contextmanager
def observe_relink_progress(callback: RelinkProgressObserver) -> Iterator[None]:
    """Subscribe within the caller's context; nested subscribers fan out."""
    token = _observers.set((*_observers.get(), callback))
    try:
        yield
    finally:
        _observers.reset(token)


def report_relink_progress(payload: dict) -> None:
    """Publish counts only. Observability failures never fail maintenance."""
    observers = _observers.get()
    if not observers:
        return
    safe = {key: max(0, int(payload.get(key, 0))) for key in _COUNT_FIELDS}
    safe["elapsed_seconds"] = max(0.0, float(payload.get("elapsed_seconds", 0)))
    safe["resumed"] = bool(payload.get("resumed", False))
    if "preparing" in payload:
        safe["preparing"] = bool(payload["preparing"])
    for callback in observers:
        try:
            callback(dict(safe))
        except Exception:
            # No exception text: an observer may carry deployment-private data.
            pass
