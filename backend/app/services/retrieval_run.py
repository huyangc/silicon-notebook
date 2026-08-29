"""One request/report-stage retrieval execution context.

The ContextVar carries a *shared mutable state object*.  ``copy_context()``
therefore preserves request isolation while worker threads in the same run see
one embedding single-flight and one leaf-I/O semaphore.  Orchestrators must not
acquire the semaphore: only the actual retrieval call is wrapped, otherwise an
outer worker could hold the last slot while waiting for its own child query.
"""
from __future__ import annotations

import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterator, Optional, TypeVar
from uuid import uuid4

from app.services.cancellation import CancelEvent, raise_if_cancelled


T = TypeVar("T")
_CANCEL_POLL_SECONDS = 0.05
_RUN_KINDS = frozenset({
    "ask_chunk",
    "ask_graph",
    "ask_reasoning",
    "ask_plugin_engine",
    "report_generation",
    "report_planning",
})


@dataclass
class _PendingEmbedding:
    ready: threading.Event


class RetrievalRunState:
    """Thread-safe state shared by every copied context in one retrieval run."""

    def __init__(self, *, run_kind: str, fanout_limit: Optional[int],
                 correlation_id: str = "",
                 actor_id: str = "",
                 cancel_event: CancelEvent = None) -> None:
        self.run_kind = str(run_kind)
        self.run_id = f"rr-{uuid4().hex[:12]}"
        self.correlation_id = str(correlation_id or "")
        # Core-only authority identity. It is deliberately absent from event().
        self.actor_id = str(actor_id or "")
        self.cancel_event = cancel_event
        self.fanout_limit = (
            max(1, int(fanout_limit)) if fanout_limit is not None else None
        )
        self._fanout = (
            threading.BoundedSemaphore(self.fanout_limit)
            if self.fanout_limit is not None else None
        )
        self._lock = threading.RLock()
        self._embedding_cache: dict[Hashable, Any] = {}
        self._embedding_pending: dict[Hashable, _PendingEmbedding] = {}
        # identity-keyed per-run probe memo (see memoized_probe)
        self._probe_cache: dict[Hashable, tuple[Any, Any]] = {}
        self.embedding_requests = 0
        self.embedding_hits = 0
        self.embedding_errors = 0
        self.fanout_acquires = 0
        self.fanout_waits = 0
        self.fanout_wait_ms = 0

    def memoized_embedding(self, key: Hashable, compute: Callable[[], T]) -> T:
        """Return one successful value per key, single-flight across threads.

        A failed or ``None`` result is never retained.  Waiters wake and retry
        through the ordinary miss path, preserving the historical transient
        retry semantics instead of sharing a poisoned empty result.
        """
        while True:
            owner = False
            with self._lock:
                if key in self._embedding_cache:
                    self.embedding_hits += 1
                    return self._embedding_cache[key]
                pending = self._embedding_pending.get(key)
                if pending is None:
                    pending = _PendingEmbedding(threading.Event())
                    self._embedding_pending[key] = pending
                    self.embedding_requests += 1
                    owner = True
            if owner:
                break
            pending.ready.wait()

        try:
            value = compute()
        except BaseException:
            with self._lock:
                self.embedding_errors += 1
                current = self._embedding_pending.pop(key, None)
                if current is not None:
                    current.ready.set()
            raise
        with self._lock:
            if value is not None:
                self._embedding_cache[key] = value
            current = self._embedding_pending.pop(key, None)
            if current is not None:
                current.ready.set()
        return value

    def memoized_probe(
        self, key: Hashable, identity: Any, compute: Callable[[], T]
    ) -> T:
        """Per-run memo for a repeated read-only probe whose answer is fixed by
        ``identity`` (热路径修复批 2 · R2-3).

        Deliberately NOT ``memoized_embedding``: that one drops falsy/``None``
        results so a transient embedding failure retries. A probe's answer IS
        very often ``False``, and re-running it because it was falsy would
        defeat the whole point. Every value is retained, ``False`` included.

        ``identity`` is compared by ``is``, not ``==``: the callers hand in a
        frozen context object (e.g. the active ``ActiveSourceScope``) whose
        ``__eq__`` would hash 48k-element frozensets on every call — the exact
        cost this memo exists to remove. Storing it alongside the value also
        pins the reference for as long as the entry lives, so identity can
        never be recycled onto a different object underneath the memo. A
        different-but-equal object simply re-probes: conservative, never wrong.

        No single-flight. Unlike an embedding call (an outbound model request
        worth ~seconds), a probe is a couple of bounded local reads: two
        threads racing the same key each run it once, then the first writer's
        value wins for the rest of the run — which is what keeps every reader
        in the run consistent. Serialising them behind a pending-event would
        cost more than the duplicate it prevents.
        """
        with self._lock:
            hit = self._probe_cache.get(key)
            if hit is not None and hit[0] is identity:
                return hit[1]

        value = compute()

        with self._lock:
            hit = self._probe_cache.get(key)
            if hit is not None and hit[0] is identity:
                return hit[1]
            self._probe_cache[key] = (identity, value)
        return value

    @contextmanager
    def fanout_slot(self) -> Iterator[None]:
        """Acquire one leaf-retrieval slot; a run without a limit is a no-op."""
        semaphore = self._fanout
        if semaphore is None:
            yield
            return
        raise_if_cancelled(self.cancel_event)
        started = time.monotonic()
        acquired = semaphore.acquire(blocking=False)
        waited = not acquired
        if waited:
            with self._lock:
                self.fanout_waits += 1
            try:
                while not acquired:
                    acquired = semaphore.acquire(timeout=_CANCEL_POLL_SECONDS)
                    if not acquired:
                        raise_if_cancelled(self.cancel_event)
            finally:
                # A cancelled waiter still consumed wall time.  Record it even
                # though it never acquired (and therefore must never release).
                wait_ms = int((time.monotonic() - started) * 1000)
                with self._lock:
                    self.fanout_wait_ms += max(0, wait_ms)
        with self._lock:
            self.fanout_acquires += 1
        try:
            # Cancellation can race the successful acquire.  Check once more
            # before yielding control to the actual leaf and release in finally.
            raise_if_cancelled(self.cancel_event)
            yield
        finally:
            semaphore.release()

    def event(self) -> dict[str, Any]:
        """Content-free aggregate suitable for the shared event logger."""
        with self._lock:
            event = {
                "kind": "retrieval_run_stats",
                "run_kind": (
                    self.run_kind if self.run_kind in _RUN_KINDS else "unknown"
                ),
                "run_id": self.run_id,
                "embedding_requests": self.embedding_requests,
                "embedding_hits": self.embedding_hits,
                "embedding_errors": self.embedding_errors,
                "fanout_acquires": self.fanout_acquires,
                "fanout_waits": self.fanout_waits,
                "fanout_wait_ms": self.fanout_wait_ms,
            }
            if self.fanout_limit is not None:
                event["fanout_limit"] = self.fanout_limit
            if self.correlation_id:
                event["correlation_id"] = self.correlation_id
            return event


_RETRIEVAL_RUN: "contextvars.ContextVar[RetrievalRunState | None]" = (
    contextvars.ContextVar("retrieval_run", default=None)
)


def current_retrieval_run() -> Optional[RetrievalRunState]:
    return _RETRIEVAL_RUN.get()


@contextmanager
def retrieval_run(*, run_kind: str, event_log: Any = None,
                  fanout_limit: Optional[int] = None,
                  correlation_id: str = "",
                  actor_id: str = "",
                  cancel_event: CancelEvent = None) -> Iterator[RetrievalRunState]:
    """Install an isolated run and restore an enclosing run exactly on exit."""
    state = RetrievalRunState(
        run_kind=run_kind,
        fanout_limit=fanout_limit,
        correlation_id=correlation_id,
        actor_id=actor_id,
        cancel_event=cancel_event,
    )
    token = _RETRIEVAL_RUN.set(state)
    try:
        yield state
    finally:
        event = state.event()
        try:
            try:
                if event_log is not None:
                    event_log.emit(event)
            except Exception:
                # Observability is fail-open by contract.
                pass
        finally:
            # Do not leak state even when an injected sink raises BaseException.
            _RETRIEVAL_RUN.reset(token)


def memoized_query_embedding(key: Hashable, compute: Callable[[], T]) -> T:
    state = current_retrieval_run()
    return state.memoized_embedding(key, compute) if state is not None else compute()


def memoized_run_probe(key: Hashable, identity: Any, compute: Callable[[], T]) -> T:
    """``RetrievalRunState.memoized_probe`` outside a run = plain ``compute()``.

    Mirrors ``memoized_query_embedding``'s shape: no run installed (direct
    service/test callers, background jobs) means no memo and byte-identical
    behaviour to not having this function at all.
    """
    state = current_retrieval_run()
    return (
        state.memoized_probe(key, identity, compute)
        if state is not None else compute()
    )


@contextmanager
def retrieval_fanout_slot() -> Iterator[None]:
    state = current_retrieval_run()
    if state is None:
        yield
        return
    with state.fanout_slot():
        yield


__all__ = [
    "RetrievalRunState",
    "current_retrieval_run",
    "memoized_query_embedding",
    "memoized_run_probe",
    "retrieval_fanout_slot",
    "retrieval_run",
]
