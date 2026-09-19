"""Bounded cross-notebook recall, isolated from whole-library cold loaders."""
from dataclasses import dataclass, field
import heapq
import math
import time

from app.repositories.read_budget import (
    classify_read_failure, current_read_budget, read_budget,
)
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.services.cancellation import AskCancelled
from app.services.retrieval import score_chunks
from app.services.retrieval_run import current_retrieval_run
from app.services.source_scope import scoped_allowed_source_ids
from app.services.vector_index import build_matrix, query_sims, resolve_runtime_dim


# Allocation/pagination granularity only; all eligible rows are visited and
# result selection remains governed by the public candidate and library rails.
# 256 made an 11k-chunk library cost 45 server round trips, and under
# PostgreSQL each of those carried its own ``set_config`` round trip plus one
# bounded COUNT gate — the library burned its whole 5s budget on pagination
# overhead and was cancelled server-side. The page only bounds transient
# memory (one page of decoded vectors): at the 1024-dim runtime default that
# is 2048 x 1024 x 4B = 8 MiB per page, and at most one page is live per
# retrieval worker, so the process-wide peak is 8 MiB x
# ``global_ask_retrieval_concurrency`` (32 MiB at the default 4).
_GLOBAL_VECTOR_PAGE_SIZE = 2048

# Bounded over-fetch for indexes with no row→source sidecar: the ceiling can
# hide an unknown share of the neighbourhood, so widen k geometrically a
# couple of times rather than guessing a fixed multiple. Worst case is
# ``chunk_recall * 16`` labels examined, capped by the index itself.
_ANN_OVERFETCH_FACTOR = 4
_ANN_OVERFETCH_ROUNDS = 2


class GlobalRetrievalSkipped(RuntimeError):
    def __init__(self, reason="unavailable", error_type=""):
        self.reason = reason
        # The class that actually failed, for the content-free event. The skip
        # is often raised from a handler, so the receipt would otherwise name
        # this wrapper instead of the failure operators need to see.
        self.error_type = error_type or type(self).__name__
        super().__init__(reason)


@dataclass
class GlobalRetrievalResult:
    chunks: list
    ids: list
    matrix: object
    degraded: bool
    evidence_fingerprints: dict = field(default_factory=dict)


def emit_global_event(candidates, event):
    """Fail-open content-free telemetry; observability never fails a request."""
    event_log = getattr(candidates, "event_log", None)
    if event_log is None:
        return
    try:
        event_log.emit(event)
    except Exception:  # noqa: BLE001 — see docstring
        pass


def _emit_skipped(candidates, notebook_id, reason, exc, started):
    """Content-free receipt for a library this run could not finish.

    The skip used to be completely silent, so production could not tell an
    expired budget from a broken index. Only the exception CLASS name travels;
    its message may carry SQL, source text or the user's own question. EVERY
    skip exit reports here, including the bounded-FTS timeout raised inside
    ``_retrieve`` -- that one is the most common failure in production, and
    short-circuiting it left exactly the case operators needed unobserved.
    """
    emit_global_event(candidates, {
        "kind": "global_retrieval_skipped", "notebook_id": notebook_id,
        "reason": reason, "error_type": getattr(exc, "error_type", type(exc).__name__),
        "latency_ms": int((time.monotonic() - started) * 1000),
    })


def retrieve_global_candidates(candidates, notebook_id, query, *, deadline=None,
                               cancel_event=None):
    current = current_read_budget()
    if deadline is None:
        deadline = current.deadline if current else (
            time.monotonic() + candidates.settings.global_ask_notebook_timeout_seconds
        )
    started = time.monotonic()
    try:
        with read_budget(deadline, cancel_event) as budget:
            return _retrieve(candidates, notebook_id, query, budget)
    except AskCancelled:
        raise
    except GlobalRetrievalSkipped as exc:
        _emit_skipped(candidates, notebook_id, exc.reason, exc, started)
        raise
    except Exception as exc:
        # A statement the server cancelled AT the deadline and a local clock
        # that has just passed it are the same event observed from two sides;
        # classifying by the clock alone raced and mislabelled real timeouts as
        # "index unavailable". ``classify_read_failure`` owns the driver-level
        # half of that judgment so this layer never imports a database driver,
        # and it keeps an exhausted connection pool ("saturated") apart from a
        # query that actually ran out of time.
        reason = classify_read_failure(exc) or (
            "timeout" if time.monotonic() >= deadline else "unavailable"
        )
        _emit_skipped(candidates, notebook_id, reason, exc, started)
        raise GlobalRetrievalSkipped(reason, type(exc).__name__) from None


def _ceiling_survivors(candidates, chunk_ids, allowed, budget):
    """Split ANN hits by source identity against the frozen ceiling."""
    sources = {}
    with candidates._connect() as db:
        for batch in candidates._in_batches(chunk_ids):
            sources.update(candidates.sources.global_chunk_source_ids(db, batch))
    budget.check()
    survivors = [chunk_id for chunk_id in chunk_ids if sources.get(chunk_id) in allowed]
    return survivors, len(chunk_ids) - len(survivors)


def _ann_candidates(candidates, notebook_id, index, vector, allowed, recall, budget):
    """Warm-ANN candidates plus whether the ceiling starved the neighbourhood.

    The chunk ANN index is built from EVERY chunk of a notebook
    (``scale_index_builder._paged_ann`` pages ``chunk_embeddings`` whole), so it
    also holds the Memory/Knowhow projection chunks that the global ceiling
    (``visible_source_ids_by_notebook``) excludes by source TYPE. Those rows are
    genuine neighbours, so a plain Top-K can come back entirely made of chunks
    the ceiling will discard: authority survives (the hydration filters below
    still bound the result) but recall collapses to zero while the receipt
    claims a successful semantic lane. That is exactly the failure mode
    ``docs/product-and-api.md`` rules out with "post-filtering alone is not
    authority because excluded candidates can consume Top-K".

    So when the index carries no row→source sidecar the neighbourhood is
    VERIFIED before it is trusted: a source-identity-only read says how many
    hits the ceiling admits, and a starved round is re-queried with a wider k
    (bounded). An index WITH a sidecar keeps the strict subset test, because
    there a Python-side HNSW filter could scan the whole index for a narrow
    scope. Returns ``(candidate_ids, starved, stats)``; ``stats`` is ``None``
    when no verification was needed.
    """
    import numpy as np

    labels = getattr(index, "chunk_ann_labels", None)
    names = getattr(index, "chunk_ann_source_names", None)
    if not labels or len(vector) != int(index.manifest["dim"]):
        return [], False, None
    verify = allowed is not None and names is None
    if allowed is not None and not verify and not set(names).issubset(set(allowed)):
        return [], False, None
    target = min(recall, len(labels))
    query_vector = np.asarray(vector, dtype=np.float32)
    k = target
    rounds = _ANN_OVERFETCH_ROUNDS
    while True:
        budget.check()
        # hnswlib defaults to ef=10, which at k=200 returns a badly truncated
        # neighbourhood. Mirror the single-notebook chunk lane
        # (``retrieval_candidates._retrieve_chunks_ann``), including its
        # convention of setting ef on the shared borrowed handle without a
        # lock. The two lanes derive slightly different values (this one from
        # the possibly over-fetched k, that one from ``chunk_recall``), but
        # both are >= their own k + 1, so whichever write lands last still
        # leaves every concurrent reader a wide enough search: the race is
        # benign and the search itself stays read-only.
        index.chunk_ann_handle.set_ef(max(k + 1, 64))
        labs, _ = index.chunk_ann_handle.knn_query(query_vector, k=k)
        # Native ANN is not forcibly interruptible. It returns before any
        # timed-out result can hydrate or reach synthesis; no extra worker
        # survives the deadline. No delta or auxiliary graph is loaded.
        budget.check()
        hits = list(dict.fromkeys(labels[int(label)] for label in labs[0]))
        if not verify:
            return hits[:target], False, None
        survivors, dropped = _ceiling_survivors(candidates, hits, allowed, budget)
        exhausted = k >= len(labels)
        if dropped == 0 or len(survivors) >= target or exhausted or rounds <= 0:
            break
        rounds -= 1
        k = min(k * _ANN_OVERFETCH_FACTOR, len(labels))
    # Having read the whole index is not starvation: the survivors are then the
    # complete admitted universe, however small. Starvation is "the ceiling ate
    # the Top-K and there are still unexamined labels".
    starved = dropped > 0 and len(survivors) < target and not exhausted
    return survivors[:target], starved, {
        "dropped": dropped, "survivors": len(survivors), "k": k,
    }


def _retrieve(candidates, notebook_id, query, budget):
    allowed = scoped_allowed_source_ids(notebook_id)
    if allowed is not None and not allowed:
        return GlobalRetrievalResult([], [], None, False)
    recall = candidates.settings.chunk_recall
    budget.check()
    index = candidates._peek_warm_chunk_index(notebook_id)
    run = current_retrieval_run()
    vector = run.peek_embedding(query[:candidates.settings.embed_truncate_chars]) if run else None
    budget.check()
    candidate_ids = []
    semantic = False
    starved = False
    stats = None
    if index is not None and vector is not None:
        candidate_ids, starved, stats = _ann_candidates(
            candidates, notebook_id, index, vector, allowed, recall, budget,
        )
        # The semantic lane counts only if it CONTRIBUTED surviving candidates,
        # not if ``knn_query`` was merely called.
        semantic = bool(candidate_ids)
    if vector is not None and (not semantic or starved):
        # The paged lane applies ``allowed`` inside the SQL producer, so it
        # cannot be starved by rows the ceiling excludes. It refuses libraries
        # over its own size rail by returning no semantic pool.
        exact, exact_semantic = _small_notebook_candidates(
            candidates, notebook_id, vector, allowed, recall, budget,
        )
        if exact_semantic:
            candidate_ids, semantic, starved = exact, True, False
    if starved:
        emit_global_event(candidates, {
            "kind": "global_retrieval_ann_starved", "notebook_id": notebook_id,
            **stats,
        })
    # Bound lexical candidates at the SQL producer, including on cold/small
    # notebooks. Neither whole-table text reads nor shared matrices are used.
    try:
        with candidates._connect() as db:
            hits = candidates._chunk_fts_hits(
                db, notebook_id, query, k=recall, allowed_source_ids=allowed,
            )
    except ChunkLexicalSearchTimeout as exc:
        budget.check()
        if not candidate_ids:
            raise GlobalRetrievalSkipped("timeout", type(exc).__name__) from None
        hits = []
    budget.check()
    # A starved ANN lane still produced real semantic candidates, but they are
    # a partial neighbourhood, so the receipt must say so.
    degraded = not semantic or starved
    candidate_ids = list(dict.fromkeys([
        *candidate_ids, *(hit["chunk_id"] for hit in hits),
    ]))
    if not candidate_ids:
        return GlobalRetrievalResult([], [], None, degraded)
    rows = {}
    vrows = []
    with candidates._connect() as db:
        for batch in candidates._in_batches(candidate_ids):
            # Every chunk's text and evidence fingerprints originate in ONE
            # statement snapshot, including PostgreSQL READ COMMITTED. A later
            # independent element snapshot cannot attest an earlier passage.
            rows.update(candidates.sources.global_candidate_evidence(db, batch))
            vrows.extend(candidates.embeddings.rows_by_ids(
                db, "chunk_embeddings", "chunk_id", batch,
            ))
    chunks = []
    fingerprints = {}
    conflicts = set()
    for row in rows.values():
        elements = row["element_ids"]
        evidence = row["element_fingerprints"]
        if not elements or any(
            element not in evidence or evidence[element][0] != row["source_id"]
            for element in elements
        ):
            continue
        if allowed is not None and row["source_id"] not in allowed:
            continue
        for element, fingerprint in evidence.items():
            if element in fingerprints and fingerprints[element] != fingerprint:
                conflicts.add(element)
            fingerprints[element] = fingerprint
        chunks.append({
            "chunk_id": row["id"], "source_id": row["source_id"],
            "text": row["text"], "section_path": row["section_path"],
            "source_title": row["source_title"], "element_ids": elements,
        })
    chunks = [chunk for chunk in chunks if not conflicts.intersection(chunk["element_ids"])]
    kept_ids = {chunk["chunk_id"] for chunk in chunks}
    ids, matrix = build_matrix(
        ((row["vid"], row["vector"]) for row in vrows if row["vid"] in kept_ids),
        runtime_dim=resolve_runtime_dim(candidates.settings),
        expected_dim=len(vector) if vector is not None else None,
    )
    budget.check()
    if allowed is not None:
        chunks = [chunk for chunk in chunks if chunk["source_id"] in allowed]
        ids, matrix = candidates._mask_vector_matrix(
            ids, matrix, {chunk["chunk_id"] for chunk in chunks},
        )
    sims = query_sims(vector, ids, matrix) if vector is not None else None
    scored = score_chunks(query, chunks, vector, sims, limit=recall)
    budget.check()
    selected_elements = {element for chunk in scored for element in chunk.element_ids}
    return GlobalRetrievalResult(scored, ids, matrix, degraded, {
        element: fingerprints[element] for element in selected_elements
    })


def _small_notebook_candidates(candidates, notebook_id, vector, allowed, recall, budget):
    """Score temporary vector pages while retaining only the bounded top K."""
    maximum = candidates.settings.global_ask_small_notebook_max_chunks
    brute_guard = candidates.settings.chunk_bruteforce_max_chunks
    if brute_guard > 0:
        maximum = min(maximum, brute_guard)
    if maximum <= 0:
        return [], False
    heap = []
    after = ""
    examined = 0
    semantic = False
    while examined < maximum:
        budget.check()
        page_size = min(_GLOBAL_VECTOR_PAGE_SIZE, maximum - examined)
        with candidates._connect() as db:
            # The bounded size gate is an aggregate over up to ``maximum + 1``
            # index tuples. Paying it once per page made it the dominant cost of
            # the whole lane, so it runs on the first page (refuse an over-cap
            # library before reading any vector) and once more after the scan
            # (below), which is what actually decides whether this pool may be
            # published. Pages in between read vectors only.
            admitted, rows = candidates.embeddings.global_small_chunk_vector_page(
                db, notebook_id, allowed_source_ids=allowed, max_chunks=maximum,
                after=after, page_size=page_size, size_gate=examined == 0,
            )
        budget.check()
        if not admitted:
            # The library is already over the rail. Nothing is read; it remains
            # eligible for bounded FTS.
            return [], False
        if not rows:
            break

        def checked_rows():
            for row in rows:
                budget.check()
                yield row["vid"], row["vector"]

        ids, matrix = build_matrix(
            checked_rows(), runtime_dim=resolve_runtime_dim(candidates.settings),
            expected_dim=len(vector),
        )
        budget.check()
        for chunk_id, score in query_sims(vector, ids, matrix).items():
            if not math.isfinite(score):
                continue
            semantic = True
            entry = (score, chunk_id)
            if len(heap) < recall:
                heapq.heappush(heap, entry)
            elif entry > heap[0]:
                heapq.heapreplace(heap, entry)
        budget.check()
        examined += len(rows)
        after = max(row["vid"] for row in rows)
        if len(rows) < page_size:
            break
    if examined:
        # One closing snapshot decides whether this pool may be published at
        # all. It replaces the per-page gate: a library that grew past the rail
        # while we were scanning fails admission here, so a prefix never
        # becomes a "completed semantic scan". The ``remaining`` half
        # additionally covers concurrent deletion/insertion, which can keep
        # physical size under the rail while moving new rows beyond our cursor.
        # A scan that read nothing has nothing to publish, so it is skipped.
        budget.check()
        with candidates._connect() as db:
            admitted, remaining = candidates.embeddings.global_small_chunk_vector_page(
                db, notebook_id, allowed_source_ids=allowed, max_chunks=maximum,
                after=after, page_size=1,
            )
        budget.check()
        if not admitted or (examined >= maximum and remaining):
            return [], False
    return [chunk_id for _, chunk_id in sorted(heap, reverse=True)], semantic
