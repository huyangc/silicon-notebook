"""Bounded cross-notebook recall, isolated from whole-library cold loaders."""
from dataclasses import dataclass, field
import time

from app.repositories.read_budget import (
    ReadBudgetExceeded, current_read_budget, read_budget,
)
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.services.cancellation import AskCancelled
from app.services.retrieval import score_chunks
from app.services.retrieval_run import current_retrieval_run
from app.services.source_scope import scoped_allowed_source_ids
from app.services.vector_index import build_matrix, query_sims


class GlobalRetrievalSkipped(RuntimeError):
    def __init__(self, reason="unavailable"):
        self.reason = reason
        super().__init__(reason)


@dataclass
class GlobalRetrievalResult:
    chunks: list
    ids: list
    matrix: object
    degraded: bool
    evidence_fingerprints: dict = field(default_factory=dict)


def retrieve_global_candidates(candidates, notebook_id, query, *, deadline=None,
                               cancel_event=None):
    current = current_read_budget()
    if deadline is None:
        deadline = current.deadline if current else (
            time.monotonic() + candidates.settings.global_ask_notebook_timeout_seconds
        )
    try:
        with read_budget(deadline, cancel_event) as budget:
            return _retrieve(candidates, notebook_id, query, budget)
    except AskCancelled:
        raise
    except GlobalRetrievalSkipped:
        raise
    except Exception as exc:
        if isinstance(exc, ReadBudgetExceeded) or time.monotonic() >= deadline:
            raise GlobalRetrievalSkipped("timeout") from None
        raise GlobalRetrievalSkipped("unavailable") from None


def _retrieve(candidates, notebook_id, query, budget):
    import numpy as np

    allowed = scoped_allowed_source_ids(notebook_id)
    if allowed is not None and not allowed:
        return GlobalRetrievalResult([], [], None, False)
    recall = candidates.settings.chunk_recall
    budget.check()
    index = candidates.scale_runtime.catalog.peek_warm_chunk_index(notebook_id)
    run = current_retrieval_run()
    vector = run.peek_embedding(query[:candidates.settings.embed_truncate_chars]) if run else None
    budget.check()
    candidate_ids = []
    semantic = False
    if index is not None and vector is not None:
        labels = getattr(index, "chunk_ann_labels", None)
        names = getattr(index, "chunk_ann_source_names", None)
        # A Python HNSW filter can scan the entire index for a tiny selected
        # scope. Only use the direct native query when every indexed source is
        # inside the frozen ceiling; otherwise use bounded source-first FTS.
        scope_complete = allowed is None or (
            names is not None and set(names).issubset(set(allowed))
        )
        if labels and scope_complete and len(vector) == int(index.manifest["dim"]):
            budget.check()
            labs, _ = index.chunk_ann_handle.knn_query(
                np.asarray(vector, dtype=np.float32), k=min(recall, len(labels)),
            )
            # Native ANN is not forcibly interruptible. It returns before any
            # timed-out result can hydrate or reach synthesis; no extra worker
            # survives the deadline. No delta or auxiliary graph is loaded.
            budget.check()
            candidate_ids.extend(labels[int(label)] for label in labs[0])
            semantic = True
    # Bound lexical candidates at the SQL producer, including on cold/small
    # notebooks. Neither whole-table text reads nor shared matrices are used.
    try:
        with candidates._connect() as db:
            hits = candidates._chunk_fts_hits(
                db, notebook_id, query, k=recall, allowed_source_ids=allowed,
            )
    except ChunkLexicalSearchTimeout:
        budget.check()
        if not candidate_ids:
            raise GlobalRetrievalSkipped("timeout") from None
        hits = []
    budget.check()
    candidate_ids = list(dict.fromkeys([
        *candidate_ids, *(hit["chunk_id"] for hit in hits),
    ]))
    if not candidate_ids:
        return GlobalRetrievalResult([], [], None, not semantic)
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
        (row["vid"], row["vector"]) for row in vrows if row["vid"] in kept_ids
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
    return GlobalRetrievalResult(scored, ids, matrix, not semantic, {
        element: fingerprints[element] for element in selected_elements
    })
