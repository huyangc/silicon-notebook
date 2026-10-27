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
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterator, Optional, TypeVar
from uuid import uuid4

from app.core.request_context import request_user_id
from app.services.cancellation import CancelEvent, raise_if_cancelled


T = TypeVar("T")
_CANCEL_POLL_SECONDS = 0.05
#: How many native query vectors one run keeps per offering side ("memory" /
#: "stage") for the hand-over (``share_query_vector``), least recently used out
#: first. A side's offers only ever push out its OWN older ones: the retrieval
#: stage fans its sub-queries out on parallel workers, and a sibling worker's
#: offer must not evict the question Memory handed over before the worker
#: that wants it has read it (the streamed-Memory characterization flake).
QUERY_VECTOR_SHARE_LIMIT = 2
#: Whether a Memory recall in this context offers its vector at all
#: (``offering_query_vectors``). Off by default: only a recall whose question
#: the retrieval stage is about to embed (an Ask's Memory phase, a report
#: scout) keeps its vector; a report's per-section recalls, each with its own
#: text that nothing reads back, keep nothing -- the run holds no more than
#: it did before the hand-over.
_OFFERING_QUERY_VECTORS: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "offering_query_vectors", default=False)
_RUN_KINDS = frozenset({
    "ask_chunk",
    "ask_reasoning",
    "ask_plugin_engine",
    "report_generation",
    "report_planning",
})
#: The run kinds that keep a chunk-recall memo (``keep_chunk_recall``): the
#: ones whose retrieval searches raw elements, the memo's only reader
#: (reasoning's ``search_elements``, a report's element probes, the plugin
#: element seam). ``ask_chunk`` never searches elements, so it keeps nothing;
#: an unknown kind keeps nothing either.
_CHUNK_RECALL_MEMO_RUN_KINDS = frozenset({
    "ask_reasoning",
    "ask_plugin_engine",
    "report_generation",
    "report_planning",
})
#: How many chunk recalls one run keeps at most, least recently used out
#: first. The row bound the caller passes (``keep_chunk_recall``) is the
#: memory bound; this one only stops a run of many EMPTY recalls (each a
#: zero-row entry) from growing the table without limit.
CHUNK_RECALL_MEMO_MAX_ENTRIES = 64


@dataclass
class _PendingEmbedding:
    ready: threading.Event


@dataclass(frozen=True)
class ChunkRecallEntry:
    """One kept chunk recall (``RetrievalRunState.keep_chunk_recall``).

    ``rows`` is whatever light projection the keeper chose (it is only
    counted here); ``unbound`` marks a recall read WITHOUT its source list
    (the run's verdict said the ceiling binds nothing), which stays valid only
    while that verdict does (``forget_unbound_chunk_recalls``)."""

    rows: tuple
    notebook_id: str
    unbound: bool


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
        self._value_cache: dict[Hashable, Any] = {}
        self._value_pending: dict[Hashable, _PendingEmbedding] = {}
        # Native query vectors handed between the run's consumers of one
        # embedder (``share_query_vector``): (id(embedder), text) -> (embedder,
        # vector, owner), at most QUERY_VECTOR_SHARE_LIMIT per owner, LRU. The
        # owner names the side that offered it ("memory" / "stage"). The embedder
        # is kept beside the vector so a lookup can check identity, never only
        # an id that a freed object could reuse.
        self._query_vectors: (
            "OrderedDict[tuple[int, str], tuple[Any, Any, str]]") = OrderedDict()
        self._chunk_fts_open: set[str] = set()
        self._kg_lexical_open: set[str] = set()
        self._peer_unprepared: dict[str, str] = {}
        # Chunk recalls the raw-element arm may read back (``chunk_recall``),
        # LRU, bounded by rows and by entries; ``_chunk_recall_rows`` is the
        # sum of the kept entries' rows, maintained under ``_lock``.
        self._chunk_recalls: "OrderedDict[Hashable, ChunkRecallEntry]" = OrderedDict()
        self._chunk_recall_rows = 0
        self.chunk_recall_hits = 0
        self.chunk_recall_stores = 0
        self.embedding_requests = 0
        self.embedding_hits = 0
        self.embedding_errors = 0
        self.fanout_acquires = 0
        self.fanout_waits = 0
        self.fanout_wait_ms = 0
        self.chunk_fts_timeouts = 0
        self.chunk_fts_circuit_skips = 0
        self.kg_lexical_timeouts = 0
        self.kg_lexical_circuit_skips = 0

    def peek_embedding(self, key: Hashable):
        """Read a completed query embedding without waiting or submitting work."""
        with self._lock:
            return self._embedding_cache.get(key)

    def memoized_embedding(self, key: Hashable, compute: Callable[[], T]) -> T:
        """Return one successful value per key, single-flight across threads.

        A failed or ``None`` result is never retained.  Waiters wake and retry
        through the ordinary miss path, preserving the historical transient
        retry semantics instead of sharing a poisoned empty result.
        """
        while True:
            owner = False
            cache_hit = False
            with self._lock:
                if key in self._embedding_cache:
                    self.embedding_hits += 1
                    cached = self._embedding_cache[key]
                    cache_hit = True
                else:
                    pending = self._embedding_pending.get(key)
                    if pending is None:
                        pending = _PendingEmbedding(threading.Event())
                        self._embedding_pending[key] = pending
                        self.embedding_requests += 1
                        owner = True
            if cache_hit:
                raise_if_cancelled(self.cancel_event)
                return cached
            if owner:
                break
            while not pending.ready.wait(timeout=_CANCEL_POLL_SECONDS):
                raise_if_cancelled(self.cancel_event)
            # Completion and cancellation can race.  A successful Event.wait()
            # must not skip the cancellation check and return freshly cached
            # work to a report leaf that has already been cancelled.
            raise_if_cancelled(self.cancel_event)

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

    def shared_query_vector(self, embedder: Any, text: str) -> Any:
        """The native vector ``embedder`` produced for exactly ``text`` earlier
        in this run, or ``None``. See the module-level ``shared_query_vector``."""
        key = (id(embedder), text)
        with self._lock:
            entry = self._query_vectors.get(key)
            if entry is None or entry[0] is not embedder:
                return None
            self._query_vectors.move_to_end(key)
        return entry[1]

    def share_query_vector(self, embedder: Any, text: str, vector: Any,
                           owner: str) -> None:
        """Offer ``embedder``'s native vector for ``text`` to the run's other
        consumers. ``None`` is never kept: a failed embed is no answer."""
        if vector is None:
            return
        key = (id(embedder), text)
        with self._lock:
            self._query_vectors[key] = (embedder, vector, owner)
            self._query_vectors.move_to_end(key)
            owned = [k for k, entry in self._query_vectors.items()
                     if entry[2] == owner]
            for stale in owned[:-QUERY_VECTOR_SHARE_LIMIT]:
                del self._query_vectors[stale]

    def drop_query_vectors(self, owner: str) -> None:
        """Forget every vector ``owner`` offered (see ``drop_query_vectors``)."""
        with self._lock:
            for key in [key for key, entry in self._query_vectors.items()
                        if entry[2] == owner]:
                del self._query_vectors[key]

    def memoized_value(self, key: Hashable, compute: Callable[[], T]) -> T:
        """Single-flight one successful run-local infrastructure value.

        Unlike the embedding cache this records no model counters.  It is for
        small, content-free snapshots shared by report leaf queries (for
        example one notebook's current source-id tuple).  Exceptions are not
        retained, so a later caller may retry the ordinary fail-soft path.
        """
        while True:
            owner = False
            cache_hit = False
            with self._lock:
                if key in self._value_cache:
                    cached = self._value_cache[key]
                    cache_hit = True
                else:
                    pending = self._value_pending.get(key)
                    if pending is None:
                        pending = _PendingEmbedding(threading.Event())
                        self._value_pending[key] = pending
                        owner = True
            if cache_hit:
                raise_if_cancelled(self.cancel_event)
                return cached
            if owner:
                break
            while not pending.ready.wait(timeout=_CANCEL_POLL_SECONDS):
                raise_if_cancelled(self.cancel_event)
            raise_if_cancelled(self.cancel_event)

        try:
            value = compute()
        except BaseException:
            with self._lock:
                current = self._value_pending.pop(key, None)
                if current is not None:
                    current.ready.set()
            raise
        with self._lock:
            self._value_cache[key] = value
            current = self._value_pending.pop(key, None)
            if current is not None:
                current.ready.set()
        return value

    def chunk_fts_permitted(self, notebook_id: str) -> bool:
        """Whether this run may issue another generic FTS probe for a library."""
        key = str(notebook_id)
        with self._lock:
            if key not in self._chunk_fts_open:
                return True
            self.chunk_fts_circuit_skips += 1
            return False

    def note_chunk_fts_timeout(self, notebook_id: str) -> None:
        """Open one run-local circuit after the first bounded FTS timeout."""
        key = str(notebook_id)
        with self._lock:
            if key not in self._chunk_fts_open:
                self._chunk_fts_open.add(key)
                self.chunk_fts_timeouts += 1

    def chunk_fts_open(self, notebook_id: str) -> bool:
        """Whether the library's FTS circuit is open, WITHOUT counting a skip
        (``chunk_fts_permitted`` counts one): a recall's memo key reads it,
        since a recall made after the circuit opened has no lexical half."""
        with self._lock:
            return str(notebook_id) in self._chunk_fts_open

    def keeps_chunk_recalls(self) -> bool:
        """Whether this run keeps a chunk-recall memo at all (by run kind)."""
        return self.run_kind in _CHUNK_RECALL_MEMO_RUN_KINDS

    def chunk_recall(self, key: Hashable) -> Optional[ChunkRecallEntry]:
        """The kept recall for ``key`` (refreshed as most recently used), or
        ``None``. Never waits: a miss is the caller's own recall."""
        with self._lock:
            entry = self._chunk_recalls.get(key)
            if entry is not None:
                self._chunk_recalls.move_to_end(key)
            return entry

    def note_chunk_recall_hit(self) -> None:
        with self._lock:
            self.chunk_recall_hits += 1

    def keep_chunk_recall(self, key: Hashable, entry: ChunkRecallEntry, *,
                          max_rows: int) -> None:
        """Keep ``entry`` under ``key``, then drop least recently used entries
        until the table holds at most ``max_rows`` rows and
        ``CHUNK_RECALL_MEMO_MAX_ENTRIES`` entries. An entry larger than
        ``max_rows`` on its own is not kept at all."""
        if len(entry.rows) > max_rows:
            return
        with self._lock:
            previous = self._chunk_recalls.pop(key, None)
            if previous is not None:
                self._chunk_recall_rows -= len(previous.rows)
            self._chunk_recalls[key] = entry
            self._chunk_recall_rows += len(entry.rows)
            self.chunk_recall_stores += 1
            while (self._chunk_recall_rows > max_rows
                   or len(self._chunk_recalls) > CHUNK_RECALL_MEMO_MAX_ENTRIES):
                _stale, dropped = self._chunk_recalls.popitem(last=False)
                self._chunk_recall_rows -= len(dropped.rows)

    def forget_chunk_recall(self, key: Hashable, entry: ChunkRecallEntry) -> None:
        """Drop ``key`` only while it still holds THIS ``entry``: a reader
        that found its entry stale must not delete the fresh one a concurrent
        recall has kept under the same key since."""
        with self._lock:
            if self._chunk_recalls.get(key) is entry:
                del self._chunk_recalls[key]
                self._chunk_recall_rows -= len(entry.rows)

    def forget_unbound_chunk_recalls(self, notebook_id: str) -> None:
        """Drop every recall of ``notebook_id`` read without its source list:
        the run's verdict for that library has just flipped to "binds", so no
        later recall of it reads unbound and none of these may be served."""
        with self._lock:
            for key in [key for key, entry in self._chunk_recalls.items()
                        if entry.unbound and entry.notebook_id == notebook_id]:
                self._chunk_recall_rows -= len(self._chunk_recalls.pop(key).rows)

    def kg_lexical_permitted(self, notebook_id: str) -> bool:
        """Whether this run may issue another ANN-supplement KG lexical read."""
        key = str(notebook_id)
        with self._lock:
            if key not in self._kg_lexical_open:
                return True
            self.kg_lexical_circuit_skips += 1
            return False

    def note_kg_lexical_timeout(self, notebook_id: str) -> None:
        """Open only the KG supplement circuit for this notebook and run."""
        key = str(notebook_id)
        with self._lock:
            if key not in self._kg_lexical_open:
                self._kg_lexical_open.add(key)
                self.kg_lexical_timeouts += 1

    def note_peer_unprepared(self, notebook_id: str, reason: str) -> None:
        """Remember that the passage lane could not prepare this library.

        Written by ``chunk_federation._federated_tasks`` when a peer's
        per-library preparation read fails; the FIRST reason wins.  Read only
        by the peer workbook lane (``chunk_federation.federated_library_sources``)
        so it does not spend a second budget on the same failed read.  The
        passage lane itself does not consult it: each federated call keeps
        retrying its own preparation, as it always has.
        """
        with self._lock:
            self._peer_unprepared.setdefault(str(notebook_id), str(reason))

    def peer_unprepared_reason(self, notebook_id: str) -> str:
        """The recorded reason for this library, or ``""`` if none."""
        with self._lock:
            return self._peer_unprepared.get(str(notebook_id), "")

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
        """Content-free aggregate suitable for the shared event logger.

        ``embedding_requests`` counts misses of the run's embedding memo, not
        model requests actually sent: a miss the query-vector hand-over
        (``shared_query_vector``) answers is counted without a request, and
        Memory recall's own embeds never pass the memo at all.
        ``chunk_recall_hits``/``chunk_recall_stores`` count the chunk recalls
        the raw-element arm read back from the run's recall memo instead of
        recalling again, and the recalls kept for it (``chunk_recall_memo``)."""
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
                "chunk_fts_timeouts": self.chunk_fts_timeouts,
                "chunk_fts_circuit_skips": self.chunk_fts_circuit_skips,
                "kg_lexical_timeouts": self.kg_lexical_timeouts,
                "kg_lexical_circuit_skips": self.kg_lexical_circuit_skips,
                "chunk_recall_hits": self.chunk_recall_hits,
                "chunk_recall_stores": self.chunk_recall_stores,
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


def current_viewer_id() -> str:
    """Who the participant set is resolved for (M3: a mount is effective only
    for a viewer who may read the mounted library, or for its mounter).

    1. The retrieval run's ``actor_id`` -- reports, Global Ask and reattached
       runs execute on worker threads, so the run's own authority identity
       wins over whatever request context happens to be copied in;
    2. otherwise the requesting user (``request_context.get_request_user``),
       which ``deps.get_current_user`` sets for every authenticated request
       -- under ``AUTH_OPTIONAL`` to the seeded local account, so a tokenless
       request loses nothing;
    3. otherwise ``''``: no viewer, so only public libraries and ``everyone``
       grants take part.  Deliberately NOT ``identity.current_user()``: its
       no-request fallback is the seeded local account, and judging a mount
       for HIM on a worker thread, a CLI or the startup warm-up would judge it
       for the wrong person.  A background path without an actor fails
       closed.

    Resolved at CALL time, never cached: a value captured when a service was
    built would pin one user's participant set onto every later request.
    """
    state = _RETRIEVAL_RUN.get()
    if state is not None and state.actor_id:
        return state.actor_id
    return str(request_user_id() or "")


def memoized_query_embedding(key: Hashable, compute: Callable[[], T]) -> T:
    state = current_retrieval_run()
    return state.memoized_embedding(key, compute) if state is not None else compute()


def shared_query_vector(embedder: Any, text: str) -> Any:
    """A native query vector another consumer of this run already paid for.

    Memory recall (``MemoryRetriever._hits``) and the retrieval stage
    (``CandidateRetrievalService._embed_query``) both embed the research
    question with the query seat's embedder. This is the hand-over between
    them, and it is deliberately narrower than the run's embedding memo:

    * the vector is the embedder's NATIVE output, untouched. Memory scores it
      against native-dimension stored vectors; the retrieval stage truncates
      its own copy to ``EMBED_RUNTIME_DIM`` after reading it;
    * it is keyed by the EXACT text each side sends to ``embed_query`` and by
      the embedder object itself, so two sides share only when they would
      have sent the same text to the same model; any other pair embeds on
      its own, as before;
    * an unconfigured embedder (offline mode) never shares: Memory then
      embeds with the offline stand-in as before, and the retrieval stage
      does not embed at all;
    * it never waits, retries, counts or checks cancellation: a miss is the
      caller's ordinary embed with its own fail-soft handling, a failed or
      ``None`` embed is never offered, and outside a run nothing is shared;
    * a Memory recall offers its vector only inside
      ``offering_query_vectors()``, and at most ``QUERY_VECTOR_SHARE_LIMIT``
      vectors per offering side are kept per run (LRU, a side evicting only
      its own), so a run that offers many texts nobody reads back holds no
      more than that, and nothing at all for recalls that never offer.

    Two limits it shares with the run's embedding memo, deliberately: a vector
    is reused as it came back, so a malformed answer (a wrong width, all
    zeros) that Memory received is what the stage then uses too, exactly as
    the memo already hands one stage embed to every later caller; and
    identity is the embedder OBJECT, so a scheduled adapter rebound to another
    model by a hot reload mid-run still counts as the same embedder -- the
    memo pins the run's first vector across such a reload in the same way.
    """
    state = current_retrieval_run()
    if state is None or not _shares(embedder):
        return None
    return state.shared_query_vector(embedder, text)


def share_query_vector(embedder: Any, text: str, vector: Any, *,
                       owner: str) -> None:
    """Offer a native query vector to the run's other consumers (see
    ``shared_query_vector`` for the conditions under which one is read).
    ``owner`` -- ``"memory"`` or ``"stage"`` -- is who offered it."""
    state = current_retrieval_run()
    if state is None or not _shares(embedder):
        return
    state.share_query_vector(embedder, text, vector, owner)


def drop_query_vectors(owner: str) -> None:
    """Forget the vectors ``owner`` offered earlier in this run.

    Only Memory recall calls it (with ``"memory"``), before it embeds a text
    nobody offered: its own earlier offers were for other texts, so dropping
    them first keeps the run from holding an extra native vector while the
    next one is computed. The retrieval stage never drops anything: its
    workers run in parallel, and dropping on one worker's miss would race the
    sibling that is about to read the handed-over question. A stage offer is
    the very list the embedding memo already holds, so keeping it is free.
    """
    state = current_retrieval_run()
    if state is not None:
        state.drop_query_vectors(owner)


@contextmanager
def offering_query_vectors() -> Iterator[None]:
    """Let the Memory recalls inside this block offer their query vector to
    the retrieval run (see ``_OFFERING_QUERY_VECTORS`` for who opts in)."""
    token = _OFFERING_QUERY_VECTORS.set(True)
    try:
        yield
    finally:
        _OFFERING_QUERY_VECTORS.reset(token)


def offers_query_vectors() -> bool:
    """Whether a Memory recall here offers its vector (``offering_query_vectors``)."""
    return _OFFERING_QUERY_VECTORS.get()


def _shares(embedder: Any) -> bool:
    """Whether ``embedder`` takes part in the hand-over: only a configured
    one. A ``configured`` probe that raises counts as not configured, so the
    hand-over never turns Memory's fail-soft recall into an error."""
    try:
        return bool(getattr(embedder, "configured", False))
    except Exception:
        return False


def memoized_retrieval_value(key: Hashable, compute: Callable[[], T]) -> T:
    state = current_retrieval_run()
    return state.memoized_value(key, compute) if state is not None else compute()


@contextmanager
def retrieval_fanout_slot() -> Iterator[None]:
    state = current_retrieval_run()
    if state is None:
        yield
        return
    with state.fanout_slot():
        yield


__all__ = [
    "ChunkRecallEntry",
    "RetrievalRunState",
    "current_retrieval_run",
    "current_viewer_id",
    "memoized_query_embedding",
    "memoized_retrieval_value",
    "offering_query_vectors",
    "offers_query_vectors",
    "retrieval_fanout_slot",
    "retrieval_run",
    "drop_query_vectors",
    "share_query_vector",
    "shared_query_vector",
]
