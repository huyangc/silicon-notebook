"""Federated chunk recall: the participant set's passages, not only active's.

Module-level functions taking the ``CandidateRetrievalService`` state as their
first positional argument -- the shape ``global_retrieval.py`` already
established.  Deliberately NOT new methods on ``retrieval_candidates.py``:
that module is already the largest in the service layer, and keeping the
merge/fan-out policy here lets it be unit-tested without a database.

Three structural rules this module exists to hold:

* **Zero behaviour change for the ordinary single-library notebook.**  A
  participant set of one short-circuits to the pre-existing
  ``_retrieve_chunks_multi``/``_retrieve_chunks`` lanes and returns their
  values unchanged -- peer evidence selection is not even entered, and no
  reserve/regrouping rule below is applied.
* **One flat fan-out, and every leaf holds the run's fan-out slot.**  "Per
  library" and "per sub-query" are flattened into a single task table served
  by a single executor, so the two dimensions are not multiplied.  The honest
  ceiling is this: on the **Ask** path (no ``fanout_limit``) the peak number of
  concurrent ``_retrieve_chunks`` calls is ``chunk_fanout_max_workers`` (8 by
  default), which is HIGHER than the pre-federation typical
  ``min(len(sub_queries), 8)`` -- the same 8 worker seats are now shared by
  ``libraries x sub-queries`` tasks instead of ``sub-queries`` tasks, so a
  notebook with few sub-queries and several mounted libraries genuinely runs
  more leaves at once than it used to.  On the **report** path the binding
  ceiling is the run's own ``fanout_limit``, because ``_retrieve_for`` (and the
  single-participant short-circuit, and ``_retrieve_chunks_multi``'s own
  sub-query pool) each acquire ``retrieval_fanout_slot`` around the actual
  producer call rather than letting an orchestrator hold one -- see
  ``retrieval_run``'s module docstring for why the slot may only wrap the leaf.
* **The current notebook is the subject; mounted reference libraries are the
  supplement.**  Recall is federated, but SELECTION is federation-aware:
  ``per_query`` stays one group per sub-query (so a library count cannot
  dilute the downstream quota), and the active notebook keeps a reserved share
  of the final evidence (``chunk_federation_active_reserve``).  The active
  notebook's own hits are deliberately NOT stamped with its id -- ``""`` means
  active on every consumer already (``evidence_context.chunk_citations``,
  ``source_scope.filter_retrieval_items``, ``domain.citation_origin
  .foreign_notebook_id``), so "empty == active" is the one clean predicate the
  selection layer needs and no new consumer has to remember to normalise.

  The reserve covers the two selection branches this module feeds: MMR (via
  ``apply_active_reserve`` in ``RetrievalService.select_chunk_candidates``,
  which also serves ``reasoning_retrieval.search_chunks``) and the quota fuse
  (via ``_reserve_lanes``).  The ``mix`` branch is NOT covered, and that gap is
  now a known risk rather than a neutral choice.  The original argument was
  structural -- mix's ordering comes from a rerank MODEL over the whole pool
  and its cut is a token budget, so a reserved seat there has to be carved out
  of ``select_with_reserves_baseline_first``'s existing reserve rules rather
  than bolted on here -- and that part still holds.  What no longer holds is
  its PREMISE.  When the decision was made, mix's second lane (KG-overlay
  source chunks) resolved evidence only inside the active notebook, so the pool
  always contained some active passages no matter how strong a mounted library
  was.  Since ``graph_retrieval._kg_source_chunks`` gained cross-library
  resolution, all THREE mix lanes can come back entirely peer-owned: an active
  notebook holding two short notes at 0.30-0.40 against a large reference
  library full of strong hits can finish a mix answer with no passage of its
  own surviving the rerank and the token budget.  Fixing it means one more
  active-lane rule inside ``select_with_reserves_baseline_first``; it is
  registered in ``fangan_todo.md``'s retrieval section and deliberately not
  done here, because that function's reserve rules are their own change.
"""
from __future__ import annotations

import contextvars
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Sequence

from app.services.cancellation import AskCancelled
from app.services.global_evidence import peer_evidence
from app.services.retrieval_run import (
    memoized_retrieval_value, retrieval_fanout_slot,
)


# The tier a participant set always gives its own active notebook, and the
# fallback ``_retrieval_participants`` itself uses for an unmapped id.
_ACTIVE_TIER = "personal"


@dataclass(frozen=True)
class FederatedChunkResult:
    """One federated chunk arm's result, shaped like ``_retrieve_chunks_multi``.

    ``collected`` -- ``{chunk_id: RetrievedChunk}`` after cross-library merge.
    ``per_query`` -- the downstream quota fuse's groups.  **One group per
    sub-query**, exactly as before federation: each library's hits for one
    sub-query are merged into that sub-query's single group, so the quota a
    sub-query gets does not shrink as libraries are mounted.  Prepended to
    those are the active notebook's reserved lanes (see ``_reserve_lanes``);
    with ``chunk_federation_active_reserve = 0``, or on the single-participant
    short-circuit, there are none and the list is exactly the historical shape.
    ``ids``/``matrix`` -- the concatenated vector rows, already restricted to
    ``collected`` (the restriction happens per library, before concatenation).
    ``participants`` -- the libraries this call fanned out to, deterministic
    and empty when nothing was searched at all.
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
    # That ceiling itself, enumerated ONCE per peer in the parent thread (see
    # ``_federated_tasks``). ``None`` for the active notebook, whose call shape
    # stays the bare positional one.
    visible: tuple | None


def _bounded_participants(
    candidates, active_notebook_id: str,
) -> tuple[tuple[tuple[str, str], ...], int]:
    """``(participants, mounted_total)`` -- the seat, the filter and the bound.

    Side-effect free on purpose: the truncation EVENT belongs to the arm that
    is about to search, not to every consumer that merely needs to know which
    libraries that arm will cover.  ``mounted_total`` is ``0`` unless the bound
    actually cut something, so the one announcing caller below is the only
    place that has to know the event's shape.
    """
    settings = candidates.settings
    if not settings.chunk_federation_enabled:
        # The rollback path: one library, so every caller takes the
        # short-circuit below and behaviour returns to pre-federation exactly.
        # No participant query is issued at all.
        return ((active_notebook_id, _ACTIVE_TIER),), 0
    participants = tuple(candidates._retrieval_participants(active_notebook_id))
    if not participants or participants[0][0] != active_notebook_id:
        # The seat's library-dimension cost guard dropped the active notebook
        # itself.  Federation has nothing to add to a set that no longer
        # contains the notebook being asked about; fall back to the
        # single-library lane rather than inventing a different active.
        return ((active_notebook_id, _ACTIVE_TIER),), 0
    maximum = settings.chunk_federation_max_participants
    if len(participants) <= maximum:
        return participants, 0
    return participants[:maximum], len(participants)


def federation_participants(
    candidates, active_notebook_id: str,
) -> tuple[tuple[str, str], ...]:
    """``(notebook_id, tier)`` pairs, deterministic: active first, then MOUNT_ORDER.

    Already past ``notebook_in_scope`` (library dimension) and the
    ``chunk_federation_max_participants`` bound.  Reads the participant set
    only through ``candidates._retrieval_participants`` -- the single seat --
    never through ``participant_tiers`` directly.

    THE ANNOUNCING form: it emits ``chunk_federation_truncated`` when the bound
    cut the set, so it belongs to the arm that is about to run the fan-out.  A
    consumer that only needs the membership question answered takes
    ``federation_participant_ids`` instead and stays silent -- see its
    docstring for why a second announcement of the same fact is worse than
    none.

    ⛔ Not an authorization predicate.  Authorization still runs through
    ``resolve_participants``/``mount_sql.py``; this is the retrieval
    consumption boundary, the same layer as ``scoped_participants``.
    """
    participants, mounted_total = _bounded_participants(
        candidates, active_notebook_id,
    )
    if mounted_total:
        # Truncate along the deterministic order and say so.  Silently
        # searching fewer libraries than are mounted is exactly the kind of
        # invisible narrowing this event exists to make observable.
        _emit(candidates, {
            "kind": "chunk_federation_truncated",
            "notebook_id": active_notebook_id,
            "participants": mounted_total,
            "kept": len(participants),
        })
    return participants


def federation_participant_ids(
    candidates, active_notebook_id: str,
) -> frozenset:
    """The ids the chunk lane will actually search, as a membership test.

    Same seat, same library filter, same ``chunk_federation_max_participants``
    bound as ``federation_participants`` -- and deliberately NO truncation
    event.  The one fact "this ask searched fewer libraries than are mounted"
    is emitted once, by the arm that does the fan-out; a second copy from a
    consumer that is only intersecting its own list against the set would make
    the event's count read like two separate truncations of one ask.

    This exists so a SECOND consumer of the participant set cannot silently
    drift wider than the chunk fan-out.  ``notebook_in_scope`` is not a
    substitute: with no ``base_scope`` submitted it answers True for any id at
    all, so it cannot bound a list assembled from somewhere else.

    The set is re-resolved per call rather than memoized, matching
    ``federation_participants``: the participant seat is deliberately un-memoed
    so a run cannot pin a mount table it read at a different moment.
    """
    participants, _mounted_total = _bounded_participants(
        candidates, active_notebook_id,
    )
    return frozenset(notebook_id for notebook_id, _tier in participants)


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
    if not sub_queries:
        # Nothing to search, so nothing was searched: answer before reading the
        # participant seat at all. Consulting it here would issue a mount query
        # and could even emit a truncation event for a call that is about to be
        # a no-op, which reads to an operator as invisible narrowing.
        return FederatedChunkResult({}, [], [], None, ())
    participants = federation_participants(candidates, active_notebook_id)
    if len(participants) <= 1:
        return _single_library_result(
            candidates, active_notebook_id, list(sub_queries), participants,
        )
    queries = list(sub_queries)
    tasks = _federated_tasks(
        candidates, active_notebook_id, participants, queries, drifted,
    )
    results = _run_tasks(candidates, tasks)
    return _merge_results(
        candidates, participants, tasks, results, len(queries),
        min_relevance=min_relevance, relative_relevance=relative_relevance,
    )


def merge_chunk_matrices(parts, keep_ids=None, *, dim=None, on_drop=None):
    """Concatenate each library's ``(ids, matrix)`` into one, already narrowed.

    Chunk ids are globally unique and ``build_matrix`` rows are already L2
    normalized against one runtime dimension, so this is a plain ``vstack``.
    A part whose matrix is ``None`` contributes no rows at all (its chunks
    fall back to the ``pair_sim == 0`` behaviour MMR already has for an id it
    cannot find).  A chunk seen twice (two sub-queries of one library) keeps
    its first row, so part order decides the representative row.

    ``keep_ids`` -- the ids the merge is allowed to carry, applied PER PART
    **before** the ``vstack``.  Each part's ``(ids, mat)`` is the WHOLE
    library's matrix (unscoped: a reference to the ``_vector_matrix`` process
    cache itself), so masking only after concatenating would first materialize
    every participant's entire library in one array -- 3 reference libraries x
    10k chunks x 1024 float32 is ~320MB of transient peak for one ask, plus one
    Python-level membership test per row of every library.  Narrowing first is
    value-identical (part order and first-wins de-duplication are unchanged);
    it only never builds the rows nobody selected.  ``None`` keeps everything,
    which is what a caller that has no selection yet wants.

    ``dim`` -- the expected row width.  A part of another width is a
    stale-dimension artifact: drop that part WHOLE, fail-soft, never raise --
    the same contract ``build_matrix(expected_dim=...)`` follows, and
    ``on_drop(index, anchor)`` is called for it so the caller can say so.
    ``anchor`` is the width actually enforced, which is not ``dim`` when the
    runtime declared none and the majority rule below resolved it.  The anchor
    must come from the RUNTIME (``resolve_runtime_dim``) rather than from
    "whatever the first accepted part happened to be": part 0 is the active
    notebook, so a single stale active library would otherwise discard every
    current-dimension peer instead of itself.  When the runtime declares no
    truncation dimension at all (``EMBED_RUNTIME_DIM=0``, the default), fall
    back to the width most parts agree on, then to the first part's width --
    both of which are still majority-safe in a way "part 0 wins" is not.
    Masking makes a part empty rather than absent, which is another reason the
    anchor cannot be discovered from the first part that survives.
    """
    import numpy as np

    kept: list = []
    blocks: list = []
    seen: set = set()
    usable = [
        (index, ids, matrix) for index, (ids, matrix) in enumerate(parts)
        if matrix is not None and ids
        and getattr(matrix, "shape", None) is not None
        and len(matrix.shape) == 2 and matrix.shape[0] == len(ids)
    ]
    anchor = int(dim) if dim else _dimension_anchor(usable)
    for index, ids, matrix in usable:
        if anchor is not None and int(matrix.shape[1]) != anchor:
            if on_drop is not None:
                on_drop(index, anchor)
            continue
        rows = [
            position for position, vid in enumerate(ids)
            if vid not in seen and (keep_ids is None or vid in keep_ids)
        ]
        if not rows:
            continue
        seen.update(ids[position] for position in rows)
        kept.extend(ids[position] for position in rows)
        blocks.append(matrix[np.asarray(rows, dtype=np.intp)])
    if not blocks:
        return [], None
    return kept, np.vstack(blocks)


def _dimension_anchor(usable) -> int | None:
    """The width most parts agree on; ties and empties fall back to the first."""
    counts: dict = {}
    for _index, _ids, matrix in usable:
        width = int(matrix.shape[1])
        counts[width] = counts.get(width, 0) + 1
    if not counts:
        return None
    # ``max`` over insertion-ordered items keeps the FIRST width on a tie, so a
    # two-library disagreement resolves to the active notebook's width -- the
    # pre-existing behaviour, now only reached when there is no majority.
    return max(counts, key=lambda width: counts[width])


def _single_library_result(
    candidates, active_notebook_id: str, sub_queries: list, participants,
) -> FederatedChunkResult:
    """Wrap the pre-federation lanes' own values, verbatim.

    ``sub_queries`` is never empty here: ``federated_chunk_candidates`` answers
    that case before the participant seat is read at all.

    ``collected`` keys by ``chunk_id`` and therefore assumes chunk ids are
    unique WITHIN one recall leg -- pinned behaviourally (ANN / FTS-degraded /
    brute-force lanes alike) by ``test_chunk_federation_peek.py::
    test_one_recall_leg_never_repeats_a_chunk_id`` rather than by this comment
    alone.

    The fan-out slot sits on the leaf here too: the multi-query branch already
    takes one per sub-query inside ``_retrieve_chunks_multi``, and the single
    query branch is itself the leaf, so it takes its own.
    """
    if len(sub_queries) >= 2:
        collected, per_query, ids, matrix = candidates._retrieve_chunks_multi(
            active_notebook_id, sub_queries,
        )
    else:
        with retrieval_fanout_slot():
            scored, ids, matrix = candidates._retrieve_chunks(
                active_notebook_id, sub_queries[0],
            )
        collected = {chunk.chunk_id: chunk for chunk in scored}
        per_query = [dict(collected)]
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(nid for nid, _ in participants),
    )


def _federated_tasks(
    candidates, active_notebook_id: str, participants, sub_queries: list,
    drifted: bool | None,
) -> list:
    """The flat task table: library-major, sub-query-minor, deterministic.

    All three context variables the chunk lane reads are set HERE, before the
    fan-out, and reset immediately after every task's ``Context`` snapshot has
    been taken -- the discipline ``_retrieve_chunks_multi`` documents at
    length.  Resetting the live variables cannot affect the copies already
    taken, and keeps their live window as narrow as possible.

    ``_CHUNK_PEER_LEG`` is the widest of the three (every non-active task) and
    turns off the optional generated-question contributor seam for peer
    libraries; see its own comment in ``chunk_lane`` for the cost/semantics
    argument, and note that a SMALL peer keeps its ordinary index lane, which
    is why it is not folded into ``_CHUNK_PEEK_ONLY``.

    No ``set``/``reset`` pair is optional, and the failure modes are silent in
    production rather than loud:

    * Without ``_CHUNK_ARM_DRIFTED`` being set (or without ``drifted`` being
      threaded in from the caller), ``_retrieve_chunks_baseline`` re-probes
      ``_unsafe_source_scope_restricted`` once per (library, sub-query) instead
      of once per arm -- a 1 -> N x M multiplication of a live scope read.
    * Without the ``_CHUNK_PEEK_ONLY`` ``reset``, the LAST participant's value
      survives into the caller's own context.  When that participant is a large
      peer library, every later chunk retrieval in this same ask silently loses
      its vector lane (peek + no warm index degrades straight to FTS).

    Both are pinned by ``test_chunk_federation.py`` reading the variables from
    inside the producer double and from the caller's context afterwards.

    Each peer's source ceiling is enumerated HERE too, once per library, not
    inside the worker: without an ambient ``retrieval_run`` the memo degrades
    to a pass-through, so reading it per task would be one live source
    enumeration per (library, sub-query).  With a run it still goes through the
    same memo key, so the run-local freeze semantics are unchanged.
    """
    # ``chunk_lane`` 而不是 ``retrieval_candidates``:那边的 ``_gather_vector_chunks``
    # 调本模块,两边互取就是 import 环;这两个 ContextVar 与那把探针因此住在
    # 一个零服务层依赖的叶子模块里,``retrieval_candidates`` 按原名再导出。
    from app.services.chunk_lane import (
        _CHUNK_ARM_DRIFTED, _CHUNK_PEEK_ONLY, _CHUNK_PEER_LEG,
        _lexical_gate_drift_probe,
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
            visible = _peer_visible_sources(candidates, notebook_id) if peer else None
            peek = peer and _peek_only(candidates, notebook_id)
            peek_token = _CHUNK_PEEK_ONLY.set(peek)
            peer_token = _CHUNK_PEER_LEG.set(peer)
            try:
                tasks.extend(
                    _Task(
                        notebook_id, tier, query, contextvars.copy_context(),
                        peer, visible,
                    )
                    for query in sub_queries
                )
            finally:
                _CHUNK_PEER_LEG.reset(peer_token)
                _CHUNK_PEEK_ONLY.reset(peek_token)
    finally:
        _CHUNK_ARM_DRIFTED.reset(drift_token)
    return tasks


def _peer_visible_sources(candidates, notebook_id: str) -> tuple:
    """This peer library's live visible ceiling, frozen for the run.

    ``all_visible_source_ids`` only -- never ``hidden_source_ids``.  The
    requesting user is usually not a member of a reference library, and that
    library's Memory/Knowhow projections cannot reach the chunk channel today
    at all.  Letting them through here would be a brand-new authorization
    surface, not a federation of what is already readable.
    """
    return memoized_retrieval_value(
        ("federated_chunk_visible", notebook_id),
        lambda: tuple(candidates.sources.all_visible_source_ids(notebook_id)),
    )


def _peek_only(candidates, notebook_id: str) -> bool:
    """May this peer task only BORROW an already-warm index, never load one?

    True for a LARGE library the user is not in.  Cold-loading such a library's
    scale index would evict the warm indexes the single-notebook path depends
    on, which is a cost federation is not allowed to impose.  Never set for the
    active notebook: that is the library the user is actually using, and
    loading its index is a cost that exists today either way.  An unreadable
    copy-stats probe answers "peek only" -- the conservative side.

    Frozen per library for the run, exactly like ``_peer_visible_sources``
    above and for the same reason: ``notebook_copy_stats``' version signal
    opens a pooled connection of its own, and the three chunk entry points
    (plus every reflect action) would otherwise re-probe each peer.  A library
    crossing the large/small threshold mid-run must not switch lanes halfway
    through one ask either.  Without an ambient run the memo is a pass-through.
    """
    def _probe() -> bool:
        try:
            return not candidates.notebook_copy_stats(notebook_id)["copyable"]
        except Exception:  # noqa: BLE001 - never cold-load on an unreadable probe
            return True

    return memoized_retrieval_value(
        ("federated_chunk_peek_only", notebook_id), _probe,
    )


def _run_tasks(candidates, tasks: list) -> list:
    """One executor for the whole flattened table, bounded by one setting."""
    workers = min(len(tasks), max(1, candidates.settings.chunk_fanout_max_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda task: _run_one(candidates, task), tasks))


def _run_one(candidates, task: _Task):
    started = time.perf_counter()
    try:
        return task.context.run(_retrieve_for, candidates, task)
    except AskCancelled:
        # Cancellation is the user's own decision, never a per-library
        # degradation to swallow.
        raise
    except Exception as exc:  # noqa: BLE001 - one library must not fail the arm
        # Emit inside THIS TASK's own context, not the worker thread's bare
        # one. The events logger is per-user and picks its directory from the
        # ``_log_owner`` ContextVar (``app.core.event_logging``), which a pool
        # thread starts out without -- so emitting here without the context
        # would file every federated skip under ``user-local`` no matter who
        # asked. The context is no longer entered once ``run`` above has
        # raised, so running it a second time is legal.
        task.context.run(_emit, candidates, {
            "kind": "chunk_federation_skipped",
            "notebook_id": task.notebook_id,
            "error_type": type(exc).__name__,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        return ([], [], None)


def _retrieve_for(candidates, task: _Task):
    """One task's producer call.

    The ACTIVE library is called positionally with no keyword at all: several
    existing suites replace ``_retrieve_chunks`` wholesale with a narrower
    double and only ever have that one library, so keeping this call shape
    identical means those doubles need no change.

    A PEER library gets its own ceiling (enumerated once per library in
    ``_federated_tasks``, see ``_peer_visible_sources``) pushed down to the
    producer, before any ``LIMIT``, with ``producer_explicit=False``: this is a
    contextualized live enumeration, not a producer's own genuinely narrow
    universe, and attesting True would turn off the corpus-language probe and
    switch the lexical arm.

    The run's fan-out slot is acquired HERE, around the one producer call, and
    nowhere above: ``reasoning_retrieval.search_chunks`` used to wrap its
    ``retrieve_chunk_candidates`` call in a slot, which after this module took
    over stopped bounding anything (the real leaves are these pool threads) and
    would self-deadlock at ``fanout_limit=1`` if both layers held one.  A
    worker parked here is still cancellable: ``RetrievalRunState.fanout_slot``
    polls ``cancel_event`` while it waits.
    """
    with retrieval_fanout_slot():
        if not task.peer:
            return candidates._retrieve_chunks(task.notebook_id, task.query)
        return candidates._retrieve_chunks(
            task.notebook_id, task.query,
            allowed_source_ids=task.visible, producer_explicit=False,
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
    candidates, participants, tasks: list, results: list, sub_count: int, *,
    min_relevance: float, relative_relevance: float,
) -> FederatedChunkResult:
    """Fold within each library, pool by PARTICIPANT order, then select.

    Thread completion order is scheduler noise; evidence selection may not be.

    The within-library fold is the same one ``_retrieve_chunks_multi`` performs
    for a single notebook's sub-queries, reusing the same two functions rather
    than a second copy of the rule -- see ``_fold_library_pool``.  It has to
    happen BEFORE ``peer_evidence``, whose only identity is ``hit.text``:
    picking a representative by text alone would let a generated-question-only
    row (higher raw score) stand in for the same passage's semantic hit, and
    the downstream quota fuse would then classify that passage as supplemental,
    contradicting ``is_generated_question_only_chunk``'s promise that a
    collision with a historical channel is never supplemental.  Cross-SOURCE
    identical text is still merged by ``peer_evidence`` -- that one is the
    intended "two libraries hold the same paper" behaviour.

    **Only peer hits are stamped.**  Tagging the active notebook's own hits
    too would make "single participant -> empty id / several participants ->
    active's own id" a difference every new consumer has to remember to
    normalise, for no gain: ``evidence_context.chunk_citations``,
    ``source_scope.filter_retrieval_items`` and
    ``domain.citation_origin.foreign_notebook_id`` all already read an empty id
    as "the active notebook".  Leaving it empty makes ``not hit.notebook_id``
    the selection layer's clean "this is the user's own library" predicate and
    keeps the value identical to the single-library lane's.
    """
    pools: dict = {notebook_id: [] for notebook_id, _ in participants}
    per_task: list = []
    parts: list = []
    for task, (scored, ids, matrix) in zip(tasks, results):
        tagged = (
            [replace(hit, notebook_id=task.notebook_id) for hit in scored]
            if task.peer else list(scored)
        )
        pools[task.notebook_id].append(tagged)
        per_task.append(tagged)
        parts.append((ids, matrix))
    selected = peer_evidence(
        [_fold_library_pool(pools[notebook_id]) for notebook_id, _ in participants],
        candidates.settings.chunk_recall,
        min_relevance=min_relevance,
        relative_relevance=relative_relevance,
        peer_floor=candidates.settings.chunk_federation_peer_floor,
    )
    collected = {hit.chunk_id: hit for hit in selected}
    per_query = _reserve_lanes(candidates, collected) + _sub_query_groups(
        per_task, collected, sub_count,
    )
    ids, matrix = _merge_selected_matrices(candidates, tasks, parts, collected)
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(notebook_id for notebook_id, _ in participants),
    )


def _sub_query_groups(per_task: list, collected: dict, sub_count: int) -> list:
    """One group per SUB-QUERY, every library's hits for it merged in.

    The task table is library-major/sub-query-minor, so task ``i`` answers
    sub-query ``i % sub_count``.

    Why not one group per ``(library, sub_query)`` task, which is what a flat
    task table produces for free: ``quota_fuse`` is a round-robin ACROSS
    groups, so the number of groups is the denominator of every sub-query's
    quota.  Four sub-queries against four libraries would be 16 groups for the
    16 fused seats -- the active notebook fixed at 4 -- and eight libraries
    would be 32 groups for 16 seats, so the libraries at the tail of
    MOUNT_ORDER would get none at all.  Merging back to one group per
    sub-query restores exactly today's denominator: mounting libraries widens
    the candidate pool without re-cutting the quota.

    Each group's value is the ``collected`` representative rather than this
    task's own instance: the cross-sub-query fold may have unioned supports
    into a different object for the same chunk id, and ``quota_fuse`` hands the
    group's object straight to the answer.  Order inside a group is by
    comparable score descending (one producer, one 0..1 scale -- the premise
    ``CHUNK_FEDERATION_PEER_FLOOR``'s comparable mode already rests on), ties
    keeping task order, so it is deterministic; ``quota_fuse`` re-sorts by
    relevance anyway, so this only fixes what a reader sees.
    """
    buckets: list = [[] for _ in range(max(1, sub_count))]
    for index, hits in enumerate(per_task):
        bucket = buckets[index % len(buckets)]
        bucket.extend(
            collected[hit.chunk_id] for hit in hits if hit.chunk_id in collected
        )
    return [
        {hit.chunk_id: hit for hit in sorted(bucket, key=lambda h: -_score(h))}
        for bucket in buckets
    ]


def _reserve_lanes(candidates, collected: dict) -> list:
    """The active notebook's reserved seats, as dedicated single-hit groups.

    The product rule is in ``Settings.chunk_federation_active_reserve``: the
    current notebook is the subject, mounted reference libraries are the
    supplement, so the active notebook keeps at least ``ceil(k * reserve)`` of
    the final seats whenever it has qualified candidates at all.

    Why the multi-query branch gets its floor from the DATA rather than from a
    post-selection repair: that branch's selection is
    ``quota_fuse_baseline_first``, called directly inside ``ask_chunk`` (a
    function under a zero-slack length ceiling), so ``per_query`` is the only
    surface between this module and the seats.  It is also enough to make the
    floor exact rather than approximate.  ``quota_fuse`` assigns each candidate
    to the group where its relevance is highest and breaks ties on the LOWEST
    group index, so one dedicated single-hit group per reserved seat, placed
    first, captures exactly those hits and hands each of them a seat in the
    very first round-robin round.  One group per seat (not one group holding
    them all) is required: several reserved hits in one group would all be
    assigned to it and it would still yield only one per round.  Singletons
    also mean the lanes cannot take MORE than the floor -- any further active
    hit competes on merit inside its own sub-query group, which is what "at
    least ``ceil(k * reserve)``, the rest to whoever is strongest" means.

    ``k`` is ``chunk_mmr_k``: ``ChunkRetrievalPlan.fuse_k`` is that same field
    (its docstring calls the equality an explicit contract), and the single
    branch's MMR ``k`` is the same one -- so both branches reserve the same
    share without this module having to be told the caller's ``k``.

    Only baseline (non generated-question-only) hits qualify: a supplemental
    row is fused in a later phase, so reserving a seat for one would move a
    seat without moving evidence.  When the active notebook has fewer qualified
    candidates than the floor, the floor is its actual count -- an empty active
    notebook reserves nothing and this returns ``[]``.
    """
    size = _reserve_size(candidates.settings, _reserve_k(candidates.settings))
    if not size:
        return []
    return [{hit.chunk_id: hit} for hit in _qualified_active(collected.values())[:size]]


def _reserve_k(settings) -> int:
    return int(getattr(settings, "chunk_mmr_k", 0) or 0)


def _reserve_size(settings, k: int) -> int:
    """``ceil(k * reserve)``, clamped to ``k``; ``0`` switches the rule off."""
    reserve = float(getattr(settings, "chunk_federation_active_reserve", 0.0) or 0.0)
    if reserve <= 0 or k <= 0:
        return 0
    return min(int(k), int(math.ceil(k * reserve)))


def _score(hit) -> float:
    return float(hit.relevance or hit.score or 0.0)


def _qualified_active(hits) -> list:
    """Active-library hits eligible to hold a reserved seat, strongest first.

    "Active" is ``not hit.notebook_id`` -- see ``_merge_results`` for why the
    active leg is the only one left unstamped.  Stable sort, so equal scores
    keep the caller's own deterministic order.
    """
    from app.services.retrieval import is_generated_question_only_chunk

    qualified = [
        hit for hit in hits
        if not hit.notebook_id and not is_generated_question_only_chunk(hit)
    ]
    qualified.sort(key=lambda hit: -_score(hit))
    return qualified


def apply_active_reserve(settings, selected, pool, k: int) -> list:
    """Repair one already-made selection so the active notebook holds its floor.

    The MMR branch's counterpart to ``_reserve_lanes``: ``select_chunk_candidates``
    has both the finished selection and the candidate pool in hand, so the
    cheapest honest fix is to hand the trailing PEER seats back to the
    strongest active candidates that MMR passed over, leaving every other
    position -- and therefore the diversity ordering MMR produced -- untouched.

    Inert by construction where it must be: with a single participant (or
    ``CHUNK_FEDERATION_ENABLED=0``) every hit is active, so the floor is
    already met and this returns the same list; with ``reserve = 0`` it does
    not even look.  The floor is capped by how many qualified active
    candidates exist, so a notebook whose own library genuinely has nothing to
    say never evicts real peer evidence.
    """
    size = _reserve_size(settings, k)
    if not size:
        return list(selected)
    chosen = list(selected)
    have = sum(1 for hit in chosen if not hit.notebook_id)
    if have >= size:
        return chosen
    seen = {hit.chunk_id for hit in chosen}
    spare = [
        hit for hit in _qualified_active(pool) if hit.chunk_id not in seen
    ]
    index = len(chosen) - 1
    for hit in spare[: size - have]:
        while index >= 0 and not chosen[index].notebook_id:
            index -= 1
        if index < 0:
            # Nothing peer-owned left to hand back; the selection is already
            # as local as its inputs allow.
            break
        chosen[index] = hit
        index -= 1
    return chosen


def _fold_library_pool(groups: list) -> list:
    """Collapse ONE library's per-sub-query hits, exactly as the single-library
    lane does for one notebook's sub-queries.

    ``source_chunk_content_key`` + ``prefer_stronger_chunk_candidate`` are the
    two functions ``_retrieve_chunks_multi`` uses; so is the ordering rule that
    every historical row is aggregated before any generated-question-only
    supplement is considered, so an early question-only row cannot take the
    representative position of a semantic hit a later sub-query found.
    """
    from app.services.retrieval import partition_generated_question_chunks

    collected: dict = {}
    supplemental: list = []
    for hits in groups:
        baseline, optional = partition_generated_question_chunks(hits)
        supplemental.extend(optional)
        for hit in baseline:
            _fold_one(collected, hit)
    for hit in supplemental:
        _fold_one(collected, hit)
    return list(collected.values())


def _fold_one(collected: dict, hit) -> None:
    from app.services.retrieval import prefer_stronger_chunk_candidate
    from app.services.source_element_selection import source_chunk_content_key

    key = source_chunk_content_key(hit)
    current = collected.get(key)
    collected[key] = (
        hit if current is None
        else prefer_stronger_chunk_candidate(current, hit)
    )


def _merge_selected_matrices(candidates, tasks: list, parts: list, collected: dict):
    """Concatenate only the SELECTED rows, and say which library was discarded.

    ``keep_ids`` is pushed into the merge instead of masking afterwards so the
    whole-library matrices never meet in one array -- see
    ``merge_chunk_matrices``.  Restricting to ``collected`` before scoring/MMR
    is itself a contract, not an optimisation: a chunk the merge dropped must
    not survive as a hidden diversity reference row.
    """
    from app.services.vector_index import resolve_runtime_dim

    dropped: dict = {}

    def _on_drop(index: int, anchor: int) -> None:
        dropped.setdefault(
            tasks[index].notebook_id, (int(parts[index][1].shape[1]), anchor),
        )

    ids, matrix = merge_chunk_matrices(
        parts, set(collected),
        dim=resolve_runtime_dim(candidates.settings), on_drop=_on_drop,
    )
    for notebook_id, (width, expected) in dropped.items():
        # Content-free: two row widths and a library id. Silence here would
        # read as "federation found nothing similar" while the real cause is
        # one library still carrying vectors of a retired dimension.
        _emit(candidates, {
            "kind": "chunk_federation_dim_mismatch",
            "notebook_id": notebook_id,
            "dim": width,
            "expected_dim": expected,
        })
    return ids, matrix
