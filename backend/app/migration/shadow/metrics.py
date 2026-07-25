"""Redacted in-process metrics for temporary shadow replication workers."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplicationMetric:
    """One safe observation.

    Deliberately absent are table names, replication keys, row values, database
    URLs, worker ids, and exception text.  ``run_id`` is the only operational
    identity retained by the shadow control protocol.
    """

    run_id: str
    direction: str
    checkpoint_before: int
    checkpoint_after: int
    source_max_seq: int
    lag: int
    batch_events: int
    batch_rows: int
    batch_bytes: int
    duration_seconds: float
    retries: int
    poison_count: int


class ShadowMetrics:
    """Small thread-safe metric sink suitable for CLI status and tests."""

    def __init__(
        self,
        emit: Callable[[ReplicationMetric], None] | None = None,
        *,
        history_limit: int = 256,
    ) -> None:
        if isinstance(history_limit, bool) or history_limit < 1:
            raise ValueError("shadow metric history limit must be positive")
        self._emit = emit
        self._history_limit = history_limit
        self._lock = threading.Lock()
        self._history: list[ReplicationMetric] = []

    def record(self, metric: ReplicationMetric) -> None:
        if not isinstance(metric, ReplicationMetric):
            raise TypeError("shadow metric has the wrong type")
        with self._lock:
            self._history.append(metric)
            del self._history[: -self._history_limit]
        if self._emit is not None:
            try:
                self._emit(metric)
            except Exception:
                # Observability cannot turn an already committed checkpoint
                # into an apparent replication failure.
                pass

    def snapshot(self) -> tuple[ReplicationMetric, ...]:
        with self._lock:
            return tuple(self._history)
