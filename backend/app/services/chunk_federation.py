"""Federated chunk recall: the participant set's passages, not only active's.

Module-level functions taking the ``CandidateRetrievalService`` state as their
first positional argument -- the shape ``global_retrieval.py`` already
established.  Deliberately NOT new methods on ``retrieval_candidates.py``:
that module is already the largest in the service layer, and keeping the
merge/fan-out policy here lets it be unit-tested without a database.

Two structural rules this module exists to hold:

* **Zero behaviour change for the ordinary single-library notebook.**  A
  participant set of one short-circuits to the pre-existing
  ``_retrieve_chunks_multi``/``_retrieve_chunks`` lanes and returns their
  values unchanged -- peer evidence selection is not even entered.
* **One flat fan-out.**  "Per library" and "per sub-query" are flattened into
  a single task table served by a single executor, so the peak concurrent
  database connection count stays what it was before federation existed
  instead of multiplying the two dimensions.
"""
from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Sequence

from app.services.cancellation import AskCancelled
from app.services.global_evidence import peer_evidence
from app.services.retrieval_run import memoized_retrieval_value


# The tier a participant set always gives its own active notebook, and the
# fallback ``_retrieval_participants`` itself uses for an unmapped id.
_ACTIVE_TIER = "personal"


@dataclass(frozen=True)
class FederatedChunkResult:
    """One federated chunk arm's result, shaped like ``_retrieve_chunks_multi``.

    ``collected`` -- ``{chunk_id: RetrievedChunk}`` after cross-library merge.
    ``per_query`` -- one group per ``(notebook, sub_query)`` task, in task
    order, for the downstream per-group quota fuse.
    ``ids``/``matrix`` -- the concatenated, ``collected``-masked vector rows.
    ``participants`` -- the libraries this call really searched, deterministic.
    """

    collected: dict
    per_query: list
    ids: list
    matrix: Any
    participants: tuple


@dataclass(frozen=True)
class _Task:
    notebook_id: str
    tier: str
    query: str
    context: contextvars.Context
    # A peer library is any participant other than the active notebook. It
    # carries a contextualized live source ceiling; the active notebook keeps
    # the historical bare positional call shape.
    peer: bool


def federation_participants(
    candidates, active_notebook_id: str,
) -> tuple[tuple[str, str], ...]:
    """``(notebook_id, tier)`` pairs, deterministic: active first, then MOUNT_ORDER.

    Already past ``notebook_in_scope`` (library dimension) and the
    ``chunk_federation_max_participants`` bound.  Reads the participant set
    only through ``candidates._retrieval_participants`` -- the single seat --
    never through ``participant_tiers`` directly.

    ⛔ Not an authorization predicate.  Authorization still runs through
    ``resolve_participants``/``mount_sql.py``; this is the retrieval
    consumption boundary, the same layer as ``scoped_participants``.
    """
    settings = candidates.settings
    if not settings.chunk_federation_enabled:
        # The rollback path: one library, so every caller takes the
        # short-circuit below and behaviour returns to pre-federation exactly.
        # No participant query is issued at all.
        return ((active_notebook_id, _ACTIVE_TIER),)
    participants = tuple(candidates._retrieval_participants(active_notebook_id))
    if not participants or participants[0][0] != active_notebook_id:
        # The seat's library-dimension cost guard dropped the active notebook
        # itself.  Federation has nothing to add to a set that no longer
        # contains the notebook being asked about; fall back to the
        # single-library lane rather than inventing a different active.
        return ((active_notebook_id, _ACTIVE_TIER),)
    maximum = settings.chunk_federation_max_participants
    if len(participants) <= maximum:
        return participants
    # Truncate along the deterministic order and say so.  Silently searching
    # fewer libraries than are mounted is exactly the kind of invisible
    # narrowing this event exists to make observable.
    _emit(candidates, {
        "kind": "chunk_federation_truncated",
        "notebook_id": active_notebook_id,
        "participants": len(participants),
        "kept": maximum,
    })
    return participants[:maximum]


def federated_chunk_candidates(
    candidates, active_notebook_id: str, sub_queries: Sequence[str], *,
    drifted: bool | None = None,
    min_relevance: float = 0.0,
    relative_relevance: float = 0.0,
) -> FederatedChunkResult:
    """Federated chunk recall over the participant set.

    A participant set of one (or no sub-query at all) returns the existing
    single-library lanes' values unchanged -- ``peer_evidence`` is not called.
    """
    participants = federation_participants(candidates, active_notebook_id)
    if len(participants) <= 1 or not sub_queries:
        return _single_library_result(
            candidates, active_notebook_id, list(sub_queries), participants,
        )
    tasks = _federated_tasks(
        candidates, active_notebook_id, participants, list(sub_queries), drifted,
    )
    results = _run_tasks(candidates, tasks)
    return _merge_results(
        candidates, participants, tasks, results,
        min_relevance=min_relevance, relative_relevance=relative_relevance,
    )


def merge_chunk_matrices(parts) -> tuple[list, Any]:
    """Concatenate each library's ``(ids, matrix)`` into one.

    Chunk ids are globally unique and ``build_matrix`` rows are already L2
    normalized against one runtime dimension, so this is a plain ``vstack``.
    A part whose matrix is ``None`` contributes no rows at all (its chunks
    fall back to the ``pair_sim == 0`` behaviour MMR already has for an id it
    cannot find).  A part whose width disagrees with the first accepted part
    is a stale-dimension artifact: drop that part WHOLE, fail-soft, never
    raise -- the same contract ``build_matrix(expected_dim=...)`` follows.
    A chunk seen twice (two sub-queries of one library) keeps its first row.
    """
    import numpy as np

    kept: list = []
    blocks: list = []
    seen: set = set()
    dim: int | None = None
    for ids, matrix in parts:
        if matrix is None or not ids:
            continue
        shape = getattr(matrix, "shape", None)
        if shape is None or len(shape) != 2 or shape[0] != len(ids):
            continue
        if dim is None:
            dim = int(shape[1])
        elif int(shape[1]) != dim:
            continue
        rows = [index for index, vid in enumerate(ids) if vid not in seen]
        if not rows:
            continue
        seen.update(ids[index] for index in rows)
        kept.extend(ids[index] for index in rows)
        blocks.append(matrix[np.asarray(rows, dtype=np.intp)])
    if not blocks:
        return [], None
    return kept, np.vstack(blocks)


def _single_library_result(
    candidates, active_notebook_id: str, sub_queries: list, participants,
) -> FederatedChunkResult:
    """Wrap the pre-federation lanes' own values, verbatim."""
    ids: list = []
    matrix = None
    if not sub_queries:
        collected: dict = {}
        per_query: list = []
    elif len(sub_queries) >= 2:
        collected, per_query, ids, matrix = candidates._retrieve_chunks_multi(
            active_notebook_id, sub_queries,
        )
    else:
        scored, ids, matrix = candidates._retrieve_chunks(
            active_notebook_id, sub_queries[0],
        )
        collected = {chunk.chunk_id: chunk for chunk in scored}
        per_query = [dict(collected)]
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(nid for nid, _ in participants) or (active_notebook_id,),
    )


def _federated_tasks(
    candidates, active_notebook_id: str, participants, sub_queries: list,
    drifted: bool | None,
) -> list:
    """The flat task table: library-major, sub-query-minor, deterministic.

    Both context variables the chunk lane reads are set HERE, before the
    fan-out, and reset immediately after every task's ``Context`` snapshot has
    been taken -- the discipline ``_retrieve_chunks_multi`` documents at
    length.  Resetting the live variables cannot affect the copies already
    taken, and keeps their live window as narrow as possible.
    """
    # ``chunk_lane`` 而不是 ``retrieval_candidates``:那边的 ``_gather_vector_chunks``
    # 调本模块,两边互取就是 import 环;这两个 ContextVar 与那把探针因此住在
    # 一个零服务层依赖的叶子模块里,``retrieval_candidates`` 按原名再导出。
    from app.services.chunk_lane import (
        _CHUNK_ARM_DRIFTED, _CHUNK_PEEK_ONLY, _lexical_gate_drift_probe,
    )

    if drifted is None:
        # Probed once per arm, for the SCOPE's own notebook.  A peer library
        # never consults this verdict (`_lexical_gate_source_scoped` gates it
        # on `notebook_id == scope.notebook_id`), so one probe is both correct
        # and the whole point of carrying it in a contextvar.
        drifted = _lexical_gate_drift_probe(candidates, active_notebook_id)
    tasks: list = []
    drift_token = _CHUNK_ARM_DRIFTED.set(drifted)
    try:
        for notebook_id, tier in participants:
            peer = notebook_id != active_notebook_id
            peek = peer and _peek_only(candidates, notebook_id)
            peek_token = _CHUNK_PEEK_ONLY.set(peek)
            try:
                tasks.extend(
                    _Task(
                        notebook_id, tier, query, contextvars.copy_context(),
                        peer,
                    )
                    for query in sub_queries
                )
            finally:
                _CHUNK_PEEK_ONLY.reset(peek_token)
    finally:
        _CHUNK_ARM_DRIFTED.reset(drift_token)
    return tasks


def _peek_only(candidates, notebook_id: str) -> bool:
    """May this peer task only BORROW an already-warm index, never load one?

    True for a LARGE library the user is not in.  Cold-loading such a library's
    scale index would evict the warm indexes the single-notebook path depends
    on, which is a cost federation is not allowed to impose.  Never set for the
    active notebook: that is the library the user is actually using, and
    loading its index is a cost that exists today either way.  An unreadable
    copy-stats probe answers "peek only" -- the conservative side.
    """
    try:
        return not candidates.notebook_copy_stats(notebook_id)["copyable"]
    except Exception:  # noqa: BLE001 - never cold-load on an unreadable probe
        return True


def _run_tasks(candidates, tasks: list) -> list:
    """One executor for the whole flattened table, bounded by one setting."""
    workers = min(len(tasks), max(1, candidates.settings.chunk_fanout_max_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda task: _run_one(candidates, task), tasks))


def _run_one(candidates, task: _Task):
    try:
        return task.context.run(_retrieve_for, candidates, task)
    except AskCancelled:
        # Cancellation is the user's own decision, never a per-library
        # degradation to swallow.
        raise
    except Exception as exc:  # noqa: BLE001 - one library must not fail the arm
        _emit(candidates, {
            "kind": "chunk_federation_skipped",
            "notebook_id": task.notebook_id,
            "error_type": type(exc).__name__,
        })
        return ([], [], None)


def _retrieve_for(candidates, task: _Task):
    """One task's producer call.

    The ACTIVE library is called positionally with no keyword at all: several
    existing suites replace ``_retrieve_chunks`` wholesale with a narrower
    double and only ever have that one library, so keeping this call shape
    identical means those doubles need no change.

    A PEER library gets its own ceiling pushed down to the producer, before
    any ``LIMIT``:

    * ``all_visible_source_ids`` only -- never ``hidden_source_ids``.  The
      requesting user is usually not a member of a reference library, and that
      library's Memory/Knowhow projections cannot reach the chunk channel
      today at all.  Letting them through here would be a brand-new
      authorization surface, not a federation of what is already readable.
    * ``producer_explicit=False`` -- this is a contextualized live enumeration,
      not a producer's own genuinely narrow universe.  Attesting True would
      turn off the corpus-language probe and switch the lexical arm.
    * run-local memo -- an upload or auto-fold mid-run must not widen a run
      that is already in flight.
    """
    if not task.peer:
        return candidates._retrieve_chunks(task.notebook_id, task.query)
    visible = memoized_retrieval_value(
        ("federated_chunk_visible", task.notebook_id),
        lambda: tuple(candidates.sources.all_visible_source_ids(task.notebook_id)),
    )
    return candidates._retrieve_chunks(
        task.notebook_id, task.query,
        allowed_source_ids=visible, producer_explicit=False,
    )


def _emit(candidates, event: dict) -> None:
    """Content-free telemetry, fail-open: never break retrieval to report."""
    emit = getattr(getattr(candidates, "event_log", None), "emit", None)
    if not callable(emit):
        return
    try:
        emit(event)
    except Exception:  # noqa: BLE001 - observability is fail-open by contract
        pass


def _merge_results(
    candidates, participants, tasks: list, results: list, *,
    min_relevance: float, relative_relevance: float,
) -> FederatedChunkResult:
    """Pool by PARTICIPANT order, not completion order, then select.

    Thread completion order is scheduler noise; evidence selection may not be.
    """
    pools: dict = {notebook_id: [] for notebook_id, _ in participants}
    per_task: list = []
    parts: list = []
    for task, (scored, ids, matrix) in zip(tasks, results):
        tagged = [replace(hit, notebook_id=task.notebook_id) for hit in scored]
        pools.setdefault(task.notebook_id, []).extend(tagged)
        per_task.append({hit.chunk_id: hit for hit in tagged})
        parts.append((ids, matrix))
    selected = peer_evidence(
        [pools[notebook_id] for notebook_id, _ in participants],
        candidates.settings.chunk_recall,
        min_relevance=min_relevance,
        relative_relevance=relative_relevance,
    )
    collected = {hit.chunk_id: hit for hit in selected}
    per_query = [
        {cid: hit for cid, hit in group.items() if cid in collected}
        for group in per_task
    ]
    ids, matrix = merge_chunk_matrices(parts)
    if matrix is not None:
        # Mask BEFORE scoring/MMR ever sees the matrix: a chunk the merge
        # dropped must not survive as a hidden diversity reference row.
        ids, matrix = candidates._mask_vector_matrix(ids, matrix, set(collected))
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(notebook_id for notebook_id, _ in participants),
    )
