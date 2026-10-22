"""Retrieval-layer half of in-document navigation: one source's outline rows,
its chunks' similarity to the run's queries, and a section read back whole.

Reasoning's ``browse_sections`` / ``read_section`` and the section-hint block
reach these through ``RetrievalPort.section_outline`` /
``section_chunk_similarities`` / ``section_chunks`` (``RetrievalService``,
delegating to ``CandidateRetrievalService``). The outline's folding into
heading occurrences, levels and ranges is the pure ``section_outline`` module;
this one only reads.

Authority comes first and is the retrieval layer's own
(``section_source_admitted``): the run's source ceiling judged directly on
``(notebook_id, source_id)``, the peer-mode rule that a library without a
per-notebook ceiling is not a participant, and -- for any library that is not
the run's own -- membership in that library's VISIBLE sources, so another
member's Memory / Knowhow projection in a reference library is never
outlined or read. A source outside it answers exactly like a source that does
not exist -- an empty result, no event. ``notebook_id`` is always the library
that OWNS ``source_id`` (a mounted or peer library's own id), so a mounted
library's chunks are read under their owner, and the ``RetrievedChunk``\\ s
handed back carry that owner.

Every read is bounded by ``Settings.reasoning_section_outline_max_rows``
(``REASONING_SECTION_OUTLINE_MAX_ROWS``): the outline scan, and therefore the
chunk ids the other two calls accept (an id must be one of the scanned outline
rows -- the ownership check) and the vectors read for a source. A section read
also stops at ``Settings.reasoning_section_read_max_chunks``, hydrates in
bounded ``IN`` batches, and scores against the vectors of just those chunks.

Caches, keys independent of each other; the ceiling is judged before every
read of any of them, never inside one:

* run-local (``memoized_retrieval_value``): the outline (with the source's
  chunk generation, ``sources.chunked_at``) per ``(notebook_id, source_id)``;
  the notebook matrix's warmth verdict per ``notebook_id`` (one version
  aggregate per library per run, none for a large library, whose chunk matrix
  is never warm-loaded); a source's chunk vectors per
  ``(notebook_id, source_id, dim)``, byte-bounded at
  ``SECTION_VECTOR_RUN_MEMO_MAX_BYTES``;
* process-wide (``SectionVectorCache`` on the candidate service): a source's
  chunk vectors across runs, keyed ``(database, notebook_id, source_id,
  chunked_at, outline fingerprint, dim)`` and LRU-bounded at
  ``REASONING_SECTION_VECTOR_CACHE_MAX_BYTES`` (0 = off). Re-chunking changes
  ``chunked_at`` and the chunk ids, so either key part retires the entry.
  Only a source whose every outline chunk has a vector is kept there: a
  source still being embedded would otherwise stay incomplete until it is
  re-chunked. Known limit: re-embedding a source in place with a model of the
  same dimension (no re-chunk) is not seen until the entry is evicted -- the
  repository has no embedding-drift signal to key on.

Both caches are single-flight: concurrent first readers of one source (report
sections in parallel) decode it once. Memory: one source takes about
``max_rows x runtime_dim x 4`` bytes (4000 x 1024 x 4 B = 16 MB at the
defaults). No score memo: a source's section scores are one small matmul over
cached vectors.

``section_vector_stats()`` reports this run's vector reads;
``collect_section_vector_stats()`` one caller's own (the hint block's trace
key). ``embedded_query_texts`` keeps only texts this run already embedded.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Iterable, Iterator, Sequence

import numpy as np

from app.domain.retrieval import RetrievedChunk
from app.services.cancellation import raise_if_cancelled
from app.services.navigation_filter import (
    drop_navigation_rows,
    navigation_filter_enabled,
    row_navigation,
)
from app.services.retrieval import _fuse, _tokens, keyword_basis
from app.services.retrieval_run import current_retrieval_run, memoized_retrieval_value
from app.services.source_scope import current_source_scope
from app.services.vector_index import build_matrix, resolve_runtime_dim

#: ``(chunk_id, section_path, navigation)`` -- one outline row.
OutlineRow = tuple[str, str, bool]

#: The whole run's cached section vectors stop growing past this many bytes
#: (eight sources at the default scan bound and a 1024-dim runtime space).
SECTION_VECTOR_RUN_MEMO_MAX_BYTES = 128 * 1024 * 1024

_CHUNK_TABLE = "chunk_embeddings"
_CHUNK_ID_COLUMN = "chunk_id"
_VECTORS_EVENT = "section_vectors_unavailable"
_WAIT_POLL_SECONDS = 0.25


def _visible_sources(svc: Any, notebook_id: str) -> frozenset[str]:
    """That library's visible sources for this run (the federation's own
    frozen read, ``chunk_federation._peer_visible_sources``)."""
    from app.services.chunk_federation import _peer_visible_sources

    return memoized_retrieval_value(
        ("section_visible_sources", notebook_id),
        lambda: frozenset(_peer_visible_sources(svc, notebook_id)),
    )


def section_source_admitted(svc: Any, notebook_id: str, source_id: str) -> bool:
    """May this run read ``source_id`` of library ``notebook_id`` at all?

    * no ids -- no;
    * no scope (a direct service caller, the historical whole-library path)
      -- yes, as ``source_allowed`` answers;
    * a subjectless (peer-mode) run whose ceilings do not name the library --
      no: there every participant carries its own per-notebook ceiling, and
      ``allows``' historical "a mounted library is not governed by the active
      notebook's checkboxes" answer would otherwise admit a non-participant;
    * ``ActiveSourceScope.allows`` -- the library dimension, the library's
      per-notebook ceiling, the local ceiling of the scope's own notebook (the
      candidate filter's predicate);
    * a library that is not the run's own (a reference library, every library
      of a subjectless run): the source must also be one of its VISIBLE
      sources. ``allows`` admits any source of a mounted library without a
      per-notebook ceiling, hidden projections included; the chunk channel's
      peer legs read only the visible list, and so does this;
    * the run's own library: a VISIBLE source, or one of the asker's own
      hidden sources the ceiling carries (``hidden_source_ids``: their Memory,
      the Knowhow they may read). ``allows`` alone admits every source there
      while the local ceiling is off (a scope submitted for the library
      dimension only), another member's Memory projection included.

    Every "no" is the same empty answer a missing source gets.
    """
    if not notebook_id or not source_id:
        return False
    scope = current_source_scope()
    if scope is None:
        return True
    if scope.subjectless and scope.source_ceiling_for(notebook_id) is None:
        return False
    if not scope.allows(notebook_id, source_id):
        return False
    if source_id in _visible_sources(svc, notebook_id):
        return True
    own = not scope.subjectless and notebook_id == scope.notebook_id
    return own and source_id in scope.hidden_source_ids


def _max_scan(settings: Any) -> int:
    return max(0, int(settings.reasoning_section_outline_max_rows))


@dataclass(frozen=True)
class _Outline:
    """One outline read: the rows, the truncation flag, the source's chunk
    generation (``""`` when the source has none recorded) and a fingerprint of
    the row ids (the process vector cache's guard against a re-chunk that kept
    ``chunked_at``)."""

    rows: tuple[OutlineRow, ...]
    truncated: bool
    chunked_at: str
    fingerprint: str


_NO_OUTLINE = _Outline((), False, "", "")


def _outline(svc: Any, notebook_id: str, source_id: str) -> _Outline:
    if not section_source_admitted(svc, notebook_id, source_id):
        return _NO_OUTLINE
    max_scan = _max_scan(svc.settings)
    if max_scan <= 0:
        return _NO_OUTLINE
    drop_navigation = navigation_filter_enabled(svc.settings)

    def read() -> _Outline:
        with svc._connect() as db:
            rows, truncated = svc.chunks.source_outline_rows(
                db, notebook_id, source_id, max_scan,
            )
        outline = tuple(
            (
                str(row["id"]),
                str(row["section_path"] or ""),
                drop_navigation and row_navigation(row),
            )
            for row in rows
        )
        try:
            chunked_at = str(rows[0]["source_chunked_at"] or "") if rows else ""
        except (KeyError, IndexError):  # a row shape without the generation
            chunked_at = ""
        digest = hashlib.blake2b(
            "\x1f".join(row[0] for row in outline).encode(), digest_size=16,
        ).hexdigest()
        return _Outline(outline, bool(truncated), chunked_at, digest)

    return memoized_retrieval_value(
        ("section_outline", notebook_id, source_id), read,
    )


def section_outline(
    svc: Any, notebook_id: str, source_id: str,
) -> tuple[tuple[OutlineRow, ...], bool]:
    """The source's outline rows in document order and whether the scan was cut.

    ``navigation`` is the stored marker under the read-side switch
    (``RETRIEVAL_NAVIGATION_FILTER_ENABLED``): with the switch off every row
    reads ``False``, so the outline and ``section_chunks`` agree on what a
    navigation page is. Empty for a source outside the ceiling, a missing
    source and a zero scan bound alike.
    """
    outline = _outline(svc, notebook_id, source_id)
    return outline.rows, outline.truncated


def _member_rows(
    svc: Any, notebook_id: str, source_id: str, chunk_ids: Iterable[str],
) -> list[OutlineRow]:
    """The outline rows of ``chunk_ids`` that belong to this source, document
    order, each once. The ownership check: an id outside the source's
    (ceiling-judged, bounded) outline is dropped."""
    rows = _outline(svc, notebook_id, source_id).rows
    if not rows:
        return []
    wanted = {str(chunk_id) for chunk_id in chunk_ids}
    return [row for row in rows if row[0] in wanted]


@dataclass(frozen=True)
class _SourceVectors:
    """One source's chunk vectors in the runtime similarity space.
    ``complete``: every outline chunk has its vector here."""

    index: dict[str, int]
    matrix: np.ndarray
    complete: bool = False


_NO_VECTORS = _SourceVectors({}, np.zeros((0, 0), dtype=np.float32))


class SectionVectorCache:
    """Byte-bounded LRU, single-flight per key: concurrent first readers of a
    key wait for the one load instead of decoding in parallel. A load that
    raises is not retained (waiters retry it); a value the caller's
    ``keep`` refuses, or one larger than the budget, is handed back without
    being kept. Used both process-wide (one per candidate service) and per
    run (``_run_vector_memo``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[Hashable, tuple[_SourceVectors, int]] = OrderedDict()
        self._pending: dict[Hashable, threading.Event] = {}
        self._bytes = 0

    def peek(self, key: Hashable) -> _SourceVectors | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0]

    def get(
        self,
        key: Hashable,
        load: Callable[[], _SourceVectors],
        *,
        max_bytes: int,
        keep: Callable[[_SourceVectors], bool] = lambda _value: True,
    ) -> tuple[_SourceVectors, bool]:
        """``(value, hit)``; ``hit`` is True when no load ran for this call."""
        while True:
            with self._lock:
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    return entry[0], True
                pending = self._pending.get(key)
                if pending is None:
                    pending = self._pending[key] = threading.Event()
                    break
            run = current_retrieval_run()
            while not pending.wait(_WAIT_POLL_SECONDS):
                if run is not None:
                    raise_if_cancelled(run.cancel_event)
        try:
            value = load()
        except BaseException:
            with self._lock:
                self._pending.pop(key, None)
            pending.set()
            raise
        size = int(value.matrix.nbytes)
        with self._lock:
            if keep(value) and size <= max_bytes:
                self._entries[key] = (value, size)
                self._bytes += size
                while self._bytes > max_bytes and len(self._entries) > 1:
                    _old_key, (_old, old_size) = self._entries.popitem(last=False)
                    self._bytes -= old_size
            self._pending.pop(key, None)
        pending.set()
        return value, False

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "bytes": self._bytes}


class _VectorStats:
    """This run's section-vector reads (``section_vector_stats``)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.vectors_read = 0
        self.vector_ms = 0.0
        self.loads = 0
        self.process_cache_hits = 0

    def note_load(self, rows: int, elapsed_ms: float) -> None:
        with self._lock:
            self.vectors_read += int(rows)
            self.vector_ms += float(elapsed_ms)
            self.loads += 1

    def note_process_hit(self) -> None:
        with self._lock:
            self.process_cache_hits += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "vectors_read": self.vectors_read,
                "vector_ms": int(round(self.vector_ms)),
                "vector_loads": self.loads,
                "process_cache_hits": self.process_cache_hits,
            }


_ZERO_STATS = _VectorStats().snapshot()

#: The open ``collect_section_vector_stats`` scopes of this context (outermost
#: first). A thread started with ``contextvars.copy_context().run`` inherits
#: them, so a pool's reads count for the scope that started it.
_STATS_SCOPES: ContextVar[tuple[_VectorStats, ...]] = ContextVar(
    "section_vector_stats_scopes", default=(),
)


def _run_stats() -> _VectorStats:
    return memoized_retrieval_value(("section_vector_stats",), _VectorStats)


def _stats_sinks() -> tuple[_VectorStats, ...]:
    """Where a read is counted: the run's totals and every open scope. Outside
    a retrieval run nothing is counted (both views then read zero)."""
    if current_retrieval_run() is None:
        return ()
    return (_run_stats(), *_STATS_SCOPES.get())


def _note_load(rows: int, elapsed_ms: float) -> None:
    for stats in _stats_sinks():
        stats.note_load(rows, elapsed_ms)


def _note_process_hit() -> None:
    for stats in _stats_sinks():
        stats.note_process_hit()


@contextmanager
def collect_section_vector_stats() -> Iterator[_VectorStats]:
    """Count the section-vector reads made inside this block (and in threads
    it starts through ``contextvars.copy_context().run``) -- unlike the
    run-wide ``section_vector_stats``, which concurrent report sections share.
    ``.snapshot()`` gives the same four keys; ``vector_ms`` is wall-clock time
    summed per read, so reads overlapping in a pool count their overlap more
    than once. Like the run totals, nothing is counted outside a retrieval
    run."""
    stats = _VectorStats()
    token = _STATS_SCOPES.set((*_STATS_SCOPES.get(), stats))
    try:
        yield stats
    finally:
        _STATS_SCOPES.reset(token)


def section_vector_stats() -> dict[str, int]:
    """This retrieval run's section-vector reads, for a sparse trace key:

    * ``vectors_read`` -- vector rows read from the database (outline-wide
      loads plus a section read's own rows);
    * ``vector_ms`` -- wall-clock milliseconds spent in those reads and
      decodes, summed per read (concurrent reads each count their own);
    * ``vector_loads`` -- how many such reads ran;
    * ``process_cache_hits`` -- sources served from the process-wide cache
      without a read.

    All zero outside a retrieval run and in a run that read no vectors.
    Shared by everything in the run (report sections in parallel included):
    for one caller's own reads use ``collect_section_vector_stats``."""
    if current_retrieval_run() is None:
        return dict(_ZERO_STATS)
    return _run_stats().snapshot()


def _run_vector_memo() -> SectionVectorCache:
    return memoized_retrieval_value(("section_vector_memo",), SectionVectorCache)


def _emit_vectors_event(
    svc: Any, notebook_id: str, source_id: str, reason: str, **counts: int,
) -> None:
    """Content-free and fail-open (ids, a reason code and counts only), once
    per run for each ``(library, source, reason)``."""
    def emit() -> bool:
        try:
            svc.event_log.emit({
                "kind": _VECTORS_EVENT,
                "notebook_id": notebook_id,
                "source_id": source_id,
                "reason": reason,
                **counts,
            })
        except Exception:  # noqa: BLE001 — telemetry never changes retrieval
            pass
        return True

    memoized_retrieval_value(
        ("section_vectors_event", notebook_id, source_id, reason), emit,
    )


def _warm_matrix_version(svc: Any, notebook_id: str):
    """The notebook chunk matrix's cache version when it is already warm, else
    ``None`` -- judged once per library per run. A large (not copyable)
    library answers ``None`` without the version aggregate: its brute-force
    path never loads that matrix (``_retrieve_chunks_baseline``'s ``large``
    guard), so the aggregate would only ever be paid to learn "cold"."""
    def judge():
        try:
            if not svc.notebook_copy_stats(notebook_id)["copyable"]:
                return None
            with svc._connect() as db:
                return svc._warm_vector_matrix_version(db, notebook_id, _CHUNK_TABLE)
        except Exception:  # noqa: BLE001 — an unreadable verdict reads by id
            return None

    return memoized_retrieval_value(("section_matrix_warmth", notebook_id), judge)


def _database_key(settings: Any) -> str:
    """Which database the ids belong to. A digest of the whole
    ``DATABASE_URL`` rather than ``database_identity``: the identity drops the
    query string, and PostgreSQL deployments and test schemas select their
    schema there (``options=-csearch_path=...``), so two schemas of one
    database holding the same ids would otherwise share entries. A digest, so
    no credential is retained in a cache key."""
    return hashlib.blake2b(
        str(getattr(settings, "database_url", "") or "").encode(), digest_size=12,
    ).hexdigest()


def _process_key(
    svc: Any, notebook_id: str, source_id: str, outline: _Outline, dim: int,
):
    return (
        _database_key(svc.settings), notebook_id, source_id,
        outline.chunked_at, outline.fingerprint, int(dim),
    )


def _source_vectors(
    svc: Any, notebook_id: str, source_id: str, dim: int,
) -> _SourceVectors:
    """The outline chunks' ``dim``-wide vectors: the run memo, then the
    process cache, then a read (single-flight at both levels).

    A read borrows a notebook matrix already warm in the process cache in that
    dimension and masks it to the outline (``_borrow_vector_matrix`` never
    loads); otherwise it reads only the outline's rows (``vector_rows_for_ids``)
    and decodes them with the runtime truncation dim and ``expected_dim=dim``
    -- so both paths keep exactly the rows whose runtime vector has the
    query's dimension.
    """
    outline = _outline(svc, notebook_id, source_id)
    ids = [row[0] for row in outline.rows]

    def load() -> _SourceVectors:
        if not ids:
            return _NO_VECTORS
        started = time.perf_counter()
        kept, matrix = _warm_rows(svc, notebook_id, ids, dim)
        read, db_rows = len(kept), 0
        if matrix is None:
            kept, matrix, read = _rows_by_id(svc, notebook_id, ids, dim)
            db_rows = read
        _note_load(db_rows, (time.perf_counter() - started) * 1000.0)
        if not kept or matrix.size == 0:
            _emit_vectors_event(
                svc, notebook_id, source_id,
                "dim_mismatch" if read else "no_chunk_vectors",
                outline_rows=len(ids), query_dim=int(dim),
            )
            return _NO_VECTORS
        return _SourceVectors(
            {chunk_id: position for position, chunk_id in enumerate(kept)},
            matrix,
            complete=len(kept) == len(ids),
        )

    def through_process() -> _SourceVectors:
        process = getattr(svc, "section_vector_cache", None)
        budget = int(getattr(svc.settings, "reasoning_section_vector_cache_max_bytes", 0))
        if process is None or budget <= 0 or not outline.chunked_at or not ids:
            return load()
        value, hit = process.get(
            _process_key(svc, notebook_id, source_id, outline, dim), load,
            max_bytes=budget, keep=lambda value: value.complete,
        )
        if hit:
            _note_process_hit()
        return value

    value, _hit = _run_vector_memo().get(
        (notebook_id, source_id, int(dim)), through_process,
        max_bytes=SECTION_VECTOR_RUN_MEMO_MAX_BYTES,
    )
    return value


def _cached_source_vectors(
    svc: Any, notebook_id: str, source_id: str, dim: int,
) -> _SourceVectors | None:
    """The source's vectors when either cache already holds them -- a peek,
    never a read."""
    cached = _run_vector_memo().peek((notebook_id, source_id, int(dim)))
    if cached is not None:
        return cached
    process = getattr(svc, "section_vector_cache", None)
    outline = _outline(svc, notebook_id, source_id)
    if process is None or not outline.chunked_at:
        return None
    cached = process.peek(_process_key(svc, notebook_id, source_id, outline, dim))
    if cached is not None:
        _note_process_hit()
    return cached


def _warm_rows(svc: Any, notebook_id: str, ids: Sequence[str], dim: int):
    """``(kept ids, matrix)`` masked out of the warm notebook matrix, or
    ``([], None)`` when there is none to borrow in this dimension, or it holds
    none of these chunks."""
    version = _warm_matrix_version(svc, notebook_id)
    if version is None:
        return [], None
    warm = svc._borrow_vector_matrix(notebook_id, _CHUNK_TABLE, version)
    if warm is None or warm[1].ndim != 2 or warm[1].shape[1] != dim:
        return [], None
    kept, matrix = svc._mask_vector_matrix(warm[0], warm[1], set(ids))
    if not kept:
        # The warm matrix holds none of this source's chunks (a source embedded
        # after the matrix was built under a version that has not moved yet):
        # read its own rows instead of concluding it has no vectors.
        return [], None
    return kept, matrix


def _rows_by_id(svc: Any, notebook_id: str, ids: Sequence[str], dim: int):
    """``(kept ids, matrix, rows read)`` from these chunks' own vector rows."""
    rows: list = []
    with svc._connect() as db:
        for batch in svc._in_batches(ids):
            rows.extend(svc.embeddings.vector_rows_for_ids(
                db, notebook_id, _CHUNK_TABLE, _CHUNK_ID_COLUMN, batch,
            ))
    kept, matrix = build_matrix(
        ((row["vid"], row["vector"]) for row in rows),
        runtime_dim=resolve_runtime_dim(svc.settings),
        expected_dim=int(dim),
    )
    return kept, matrix, len(rows)


def _query_vectors(svc: Any, texts: Sequence[str]) -> list[np.ndarray]:
    """Normalised query vectors through the run's query-embedding memo."""
    vectors: list[np.ndarray] = []
    for text in texts:
        vector = svc._embed_query(text)
        if not vector:
            continue
        array = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        if array.ndim == 1 and norm > 0:
            vectors.append(array / norm)
    return vectors


def embedded_query_texts(settings: Any, texts: Iterable[str]) -> list[str]:
    """The texts (stripped, deduplicated) whose query embedding this retrieval
    run has ALREADY computed (``RetrievalRunState.peek_embedding``, the key
    ``_embed_query`` memoises under) -- so a caller that only wants to reuse
    vectors, like the section-hint block, adds no embedding call. Outside a
    run nothing is memoised and every text is returned."""
    cleaned = _clean_texts(texts)
    run = current_retrieval_run()
    if run is None:
        return cleaned
    limit = int(settings.embed_truncate_chars)
    return [text for text in cleaned if run.peek_embedding(text[:limit]) is not None]


def _clean_texts(query_texts: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(
        text.strip() for text in query_texts
        if isinstance(text, str) and text.strip()
    ))


def _max_cosines(
    vectors: _SourceVectors, chunk_ids: Sequence[str], queries: Sequence[np.ndarray],
) -> dict[str, float]:
    if not vectors.index or vectors.matrix.ndim != 2:
        return {}
    dim = vectors.matrix.shape[1]
    present = [chunk_id for chunk_id in chunk_ids if chunk_id in vectors.index]
    usable = [query for query in queries if query.shape[0] == dim]
    if not present or not usable:
        return {}
    rows = vectors.matrix[[vectors.index[chunk_id] for chunk_id in present]]
    best = (rows @ np.stack(usable).T).max(axis=1)
    return {chunk_id: float(score) for chunk_id, score in zip(present, best)}


def section_chunk_similarities(
    svc: Any,
    notebook_id: str,
    source_id: str,
    chunk_ids: Iterable[str],
    query_texts: Iterable[str],
) -> dict[str, float]:
    """``{chunk_id: max cosine over the query texts}`` for the chunks of
    ``chunk_ids`` that belong to this source and have a vector.

    The query side fixes the dimension (the first embedded query's; a query
    of another width is left out). Loads the whole source's vectors once
    (cached, see the module docstring). ``{}`` when nothing is admitted, no
    query text embeds, or the source has no vector of that dimension -- the
    last with a content-free ``section_vectors_unavailable`` event
    (``dim_mismatch`` when vectors exist in another width,
    ``no_chunk_vectors`` otherwise), once per run and source.
    """
    members = _member_rows(svc, notebook_id, source_id, chunk_ids)
    texts = _clean_texts(query_texts)
    if not members or not texts:
        return {}
    queries = _query_vectors(svc, texts)
    if not queries:
        return {}
    dim = int(queries[0].shape[0])
    vectors = _source_vectors(svc, notebook_id, source_id, dim)
    return _max_cosines(vectors, [row[0] for row in members], queries)


def _read_similarities(
    svc: Any, notebook_id: str, source_id: str, chunk_ids: Sequence[str],
    question: str,
) -> dict[str, float]:
    """A section read's cosines: the cached source vectors when a cache
    already holds them, otherwise just these ``chunk_ids``' own rows -- never
    a whole-source decode for a dozen chunks."""
    texts = _clean_texts([question])
    if not chunk_ids or not texts:
        return {}
    queries = _query_vectors(svc, texts)
    if not queries:
        return {}
    dim = int(queries[0].shape[0])
    cached = _cached_source_vectors(svc, notebook_id, source_id, dim)
    if cached is None:
        started = time.perf_counter()
        kept, matrix, read = _rows_by_id(svc, notebook_id, chunk_ids, dim)
        _note_load(read, (time.perf_counter() - started) * 1000.0)
        cached = _SourceVectors(
            {chunk_id: position for position, chunk_id in enumerate(kept)}, matrix,
        ) if kept else _NO_VECTORS
    return _max_cosines(cached, chunk_ids, queries)


def section_chunks(
    svc: Any,
    notebook_id: str,
    source_id: str,
    chunk_ids: Iterable[str],
    limit: int,
    question: str,
) -> list[RetrievedChunk]:
    """The chunks of ``chunk_ids`` that belong to this source, read back in
    document order, navigation pages dropped, at most ``limit`` (and never
    more than ``reasoning_section_read_max_chunks``).

    Each is scored against ``question`` with ordinary chunk retrieval's ruler
    -- ``_fuse(keyword coverage, cosine(question vector, chunk vector))``, no
    floor -- and carries ``section_read=True`` and its owning library.
    Navigation pages are dropped twice: by the outline's marker before the
    read (so they never take one of the ``limit`` reads) and by the hydrated
    rows' own column.
    """
    limit = min(int(limit), int(svc.settings.reasoning_section_read_max_chunks))
    if limit <= 0:
        return []
    drop_navigation = navigation_filter_enabled(svc.settings)
    members = [
        row for row in _member_rows(svc, notebook_id, source_id, chunk_ids)
        if not row[2]
    ][:limit]
    if not members:
        return []
    order = {row[0]: position for position, row in enumerate(members)}
    hydrated: list = []
    with svc._connect() as db:
        for batch in svc._in_batches(order):
            hydrated.extend(svc.chunks.hydrate_rows(db, batch))
    rows = sorted(
        (
            row for row in hydrated
            if str(row["id"]) in order and str(row["source_id"]) == source_id
        ),
        key=lambda row: order[str(row["id"])],
    )
    rows = drop_navigation_rows(rows, enabled=drop_navigation)
    if not rows:
        return []
    question = str(question or "")
    sims = _read_similarities(
        svc, notebook_id, source_id, [str(row["id"]) for row in rows], question,
    )
    basis = keyword_basis(question)
    chunks: list[RetrievedChunk] = []
    for row in rows:
        chunk_id = str(row["id"])
        text = str(row["text"] or "")
        relevance = _fuse(
            basis.coverage(set(_tokens(text)), text),
            sims.get(chunk_id, 0.0),
            chunk_id in sims,
        )
        chunks.append(RetrievedChunk(
            chunk_id=chunk_id,
            source_id=str(row["source_id"]),
            source_title=str(row["source_title"] or ""),
            section_path=str(row["section_path"] or ""),
            text=text,
            element_ids=list(json.loads(row["element_ids"] or "[]")),
            score=relevance,
            relevance=relevance,
            notebook_id=notebook_id,
            section_read=True,
        ))
    return chunks


__all__ = [
    "OutlineRow",
    "SECTION_VECTOR_RUN_MEMO_MAX_BYTES",
    "SectionVectorCache",
    "collect_section_vector_stats",
    "embedded_query_texts",
    "section_chunk_similarities",
    "section_chunks",
    "section_outline",
    "section_source_admitted",
    "section_vector_stats",
]
