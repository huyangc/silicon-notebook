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

  The reserve is TWO halves with one number between them.  This module
  withholds the candidates it needs from the cross-library cap
  (``_withheld_active``) and hands the number downstream on the candidate
  mapping (``FederatedCollected.active_reserve``); the seats themselves are
  enforced once per branch, on the FINISHED selection, by the single pure
  function ``retrieval.enforce_active_floor`` -- reached from the quota fuse
  (``quota_fuse_baseline_first``) and from MMR (``apply_active_reserve`` in
  ``RetrievalService.select_chunk_candidates``, which also serves
  ``reasoning_retrieval.search_chunks``).  Nothing about the reserve lives in
  ``per_query`` any more: ``ask_chunk`` appends its own keyword/exact groups
  after this module has produced them, so a rule written into the fusion's
  INPUTS only binds the inputs this module produced.  The ``mix`` branch is NOT covered, and that gap is
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
* **...unless there IS no subject.**  Under a participant override
  (``federated_ask_active()``) that whole third rule is off.  The nominal
  active is ``notebook_ids[0]``, a naming anchor with no retrieval privilege,
  so it must not hold a reserved share (``_reserve_size`` -> 0), must not be
  the one leg left unstamped, must not skip the per-library source ceiling,
  and must not be the one leg allowed to cold-load a scale index -- the last
  three are one predicate, ``_peer_leg``, true for every leg.  The
  cross-library merge switches rails too (``_merge_thresholds``): a set the
  user picked library by library gets the documented global qualification
  floors and NO ``peer_floor``, because ``chunk_federation_peer_floor`` was
  invented for reference libraries that merely happen to be mounted.  Every
  one of these forks is unreachable in production today -- nothing installs an
  override yet -- so the absent-override path stays byte-identical.
"""
from __future__ import annotations

import contextvars
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, Sequence

from app.services.cancellation import AskCancelled
# Only the NAME, from the dependency-free domain layer: these modules are
# not on the participant override's frozen reader whitelist, but their
# fail-soft handlers must re-raise its control exception instead of
# degrading an identity-attestation failure into an empty result set.
from app.domain.retrieval_control import RetrievalControlError
from app.services.global_evidence import peer_evidence
# PEER MODE.  ``federated_ask_active()`` answers "is this run searching a
# participant SET rather than one notebook with borrowed libraries", which is
# the one question that turns the three "the current notebook is the subject"
# rules in this module's docstring off.  Reading it here makes this module the
# override's newest whitelisted reader (``test_participant_override_guard.py``);
# it never INSTALLS one, and none of these names may be re-exported from here.
from app.services.retrieval_participants import (
    assert_override_matches_run, federated_ask_active,
)
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
    A ``FederatedCollected`` (see below) when several libraries took part, so
    the active-notebook floor travels with the candidates; an ordinary ``dict``
    on the single-participant short-circuit.
    ``per_query`` -- the downstream quota fuse's groups: **one group per
    sub-query**, exactly as before federation.  Each library's hits for one
    sub-query are merged into that sub-query's single group, so the quota a
    sub-query gets does not shrink as libraries are mounted, and each entry
    carries THAT sub-query's own relevance (the merged representative's
    provenance, this leg's score -- see ``_sub_query_groups``).  Nothing else
    is in this list: the active reserve is NOT expressed here, because
    ``ask_chunk`` appends its own keyword/exact groups after receiving it and
    any rule written into the groups is only a rule about the groups this
    module produced.
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


class FederatedCollected(dict):
    """``{chunk_id: RetrievedChunk}`` that also carries the active-notebook floor.

    An ATTRIBUTE on the mapping rather than a parameter, because there is no
    parameter to use.  ``ask_chunk``'s multi branch receives this mapping,
    mutates it in place (``_merge_multi_direct_chunk_hits``), appends its own
    quota groups and hands the same object to ``quota_fuse_baseline_first`` --
    and that function is called from inside ``ask_chunk``, which sits under a
    zero-slack function-length ceiling and may not be edited to thread one more
    argument through.  The mapping is therefore the only carrier that survives
    the whole path, and in-place mutation is exactly what keeps it alive.

    ``active_reserve`` is a plain class-level default so that a rebuilt copy
    (``FederatedCollected(rows)``) is always well-formed; callers that rebuild
    the mapping must re-attach the value explicitly -- see
    ``with_active_reserve``.
    """

    active_reserve: int = 0


def with_active_reserve(rows: dict, active_reserve: int) -> dict:
    """Re-attach the floor to a mapping rebuilt from a federated one.

    Returns the input untouched when there is no floor to carry, so the
    single-participant and ``CHUNK_FEDERATION_ACTIVE_RESERVE=0`` paths keep
    handing an ordinary ``dict`` downstream and stay value-identical.
    """
    if not active_reserve:
        return rows
    carried = FederatedCollected(rows)
    carried.active_reserve = int(active_reserve)
    return carried


@dataclass(frozen=True)
class _Task:
    notebook_id: str
    tier: str
    query: str
    context: contextvars.Context
    # A peer library is any participant other than the active notebook -- and
    # in peer mode, EVERY participant (see ``_peer_leg``). It carries a
    # contextualized live source ceiling; a non-peer active notebook keeps the
    # historical bare positional call shape.
    peer: bool
    # That ceiling itself, enumerated ONCE per peer in the parent thread (see
    # ``_federated_tasks``). ``None`` only for a non-peer active notebook,
    # whose call shape stays the bare positional one.
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

    ⛔ THE OVERRIDE PRE-CHECK IS THE FIRST LINE, AND IT IS OUTSIDE EVERY
    ``try``.  This function is the one entry through which every federated
    consumer (the vector legs, ``_kg_object_owners``) reaches the participant
    seat; it always runs after the retrieval run exists and before any producer
    is called, and it runs in the PARENT thread -- outside ``_run_tasks``'
    fail-soft frame.  So an attestation failure explodes here, at the moment
    the participant set is first needed, instead of inside a worker where a
    handler could turn it into a library that silently searched nothing.
    ``assert_override_matches_run`` is side-effect free and a no-op without an
    override, which is why this site is deliberately ABSENT from the guard's
    ``_SEAT_FAILSOFT_SITES``: it must stay unwrapped.
    """
    if federated_ask_active():
        assert_override_matches_run()
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
        #
        # NOT reachable with a participant override, and the reason is
        # structural rather than incidental: the seat filters with
        # ``notebook_in_scope`` -> ``ActiveSourceScope.covers_notebook``, whose
        # FIRST branch returns True for a blank id and for the scope's own
        # notebook.  An override resolves only for the nominal active it
        # declares (``resolve_retrieval_participants`` raises otherwise) and
        # that same id is what the run's ``source_scope_context`` is keyed by,
        # so the head of the list is exactly the notebook ``covers_notebook``
        # never drops.  Pinned by
        # ``test_participant_override_retrieval.py::
        # test_library_scope_can_still_narrow_an_override``, which unchecks
        # every peer and asserts the active survives.
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

    ⛔ THE SHORT-CIRCUIT IS OFF IN PEER MODE, even for a one-library set.  The
    single-library lane has no per-library budget, no cross-library
    qualification floor (``peer_evidence``) and no stamping, so a global ask
    over one selected notebook would silently be answered by a different set of
    rules than the same ask over two.  "The user selected one library" is not
    the same fact as "this notebook has no mounted references", which is the
    only fact the short-circuit was written for.
    """
    if not sub_queries:
        # Nothing to search, so nothing was searched: answer before reading the
        # participant seat at all. Consulting it here would issue a mount query
        # and could even emit a truncation event for a call that is about to be
        # a no-op, which reads to an operator as invisible narrowing.
        return FederatedChunkResult({}, [], [], None, ())
    participants = federation_participants(candidates, active_notebook_id)
    if len(participants) <= 1 and not federated_ask_active():
        return _single_library_result(
            candidates, active_notebook_id, list(sub_queries), participants,
        )
    queries = list(sub_queries)
    tasks = _federated_tasks(
        candidates, active_notebook_id, participants, queries, drifted,
    )
    results = _run_tasks(candidates, tasks)
    return _merge_results(
        candidates, _task_participants(tasks), tasks, results, len(queries),
        min_relevance=min_relevance, relative_relevance=relative_relevance,
    )


def _task_participants(tasks: list) -> tuple:
    """The ``(notebook_id, tier)`` pairs the task table actually carries.

    Not the same as what ``federation_participants`` returned: a peer whose
    preparation failed is dropped from the table, and the result must say so
    rather than list a library it never queried.  ``dict.fromkeys`` keeps the
    deterministic library-major order.
    """
    return tuple(dict.fromkeys((task.notebook_id, task.tier) for task in tasks))


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

    ``_CHUNK_PEER_LEG`` is the widest of the three (every non-active task, and
    in peer mode every task at all) and turns off the optional
    generated-question contributor seam for peer libraries; see its own comment
    in ``chunk_lane`` for the cost/semantics argument, and note that a SMALL
    peer keeps its ordinary index lane, which is why it is not folded into
    ``_CHUNK_PEEK_ONLY``.  Peer mode therefore closes the generated-question
    supplement for the whole arm, with no rule of its own.

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
    same memo key, so the run-local freeze semantics are unchanged.  That move
    put a real database read in the PARENT thread, which is why each peer's
    preparation carries its own failure isolation below -- a peer that cannot
    be prepared is dropped from the table entirely and therefore never appears
    in ``FederatedChunkResult.participants`` (see ``_task_participants``),
    which is the field's whole meaning: the libraries this arm actually
    searched.
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
        # and the whole point of carrying it in a contextvar.  In peer mode the
        # nominal active still matches that id test, but `restricted` is
        # constantly False there (a global run submits `narrowed=None` and no
        # local mode/source_ids, so `ceiling_active` is False too), so the
        # lexical arm is not routed by this verdict for anyone.
        drifted = _lexical_gate_drift_probe(candidates, active_notebook_id)
    tasks: list = []
    drift_token = _CHUNK_ARM_DRIFTED.set(drifted)
    try:
        for notebook_id, tier in participants:
            peer = _peer_leg(active_notebook_id, notebook_id)
            visible = None
            if peer:
                started = time.perf_counter()
                try:
                    visible = _peer_visible_sources(candidates, notebook_id)
                except AskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - one library must not fail the arm
                    # Preparation runs in the PARENT thread, before any task
                    # exists, so ``_run_one``'s own handler cannot reach this:
                    # a peer whose source enumeration times out would otherwise
                    # abort the whole arm -- including the active notebook's
                    # own retrieval, which has nothing to do with that library.
                    # Skip that one peer instead, and skip it FAIL-CLOSED: the
                    # alternative "query it without a ceiling" would turn an
                    # unreadable source list into a wider search than the
                    # healthy path performs. Emitted here, in the caller's own
                    # context, so the event keeps its log owner.
                    _emit(candidates, {
                        "kind": "chunk_federation_skipped",
                        "notebook_id": notebook_id,
                        "error_type": type(exc).__name__,
                        "latency_ms": round(
                            (time.perf_counter() - started) * 1000
                        ),
                    })
                    continue
            # ``_peek_only`` needs no guard of its own: its probe already
            # answers "peek only" -- the conservative side -- for any failure,
            # and only cancellation propagates out of the memo.
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


def _peer_leg(active_id: str, notebook_id: str) -> bool:
    """Is this task's library a PEER -- i.e. not the privileged subject?

    ONE predicate, FOUR consequences, which is the whole reason it exists as a
    named function rather than an inline comparison:

    1. ``_merge_results`` stamps the hit with its owning ``notebook_id``;
    2. ``_retrieve_for`` pushes that library's frozen source ceiling down to the
       producer (``allowed_source_ids=..., producer_explicit=False``) instead of
       making the bare positional call;
    3. ``_peek_only`` is consulted, so a LARGE library may only borrow an
       already-warm scale index and never cold-load one;
    4. ``_CHUNK_PEER_LEG`` is set, which turns the generated-question recall
       supplement off for that leg.

    In peer mode every leg is a peer, including the nominal active: it holds no
    retrieval privilege, so all four must apply to it too.  Consequence 2 is
    the one that would fail CLOSED-looking but OPEN: without it the nominal
    active would be the only participant whose ceiling was never pushed below
    the producer's ``LIMIT``.  Consequence 3 is what keeps the documented
    promise that an eight-library ask never evicts the warm indexes the
    ordinary single-notebook path depends on.
    """
    return federated_ask_active() or notebook_id != active_id


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
    loading its index is a cost that exists today either way -- but in PEER
    mode there is no such library, so ``_peer_leg`` is true for the nominal
    active too and this probe runs for it like any other participant.  An
    unreadable copy-stats probe answers "peek only" -- the conservative side.

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
    except RetrievalControlError:
        # Nor is a participant-override attestation failure. Registered
        # defence in depth: the seat is read in the PARENT thread before this
        # task table exists, so today nothing inside ``_retrieve_for`` can
        # raise it -- but this is the one handler that turns any per-leg
        # failure into a silently missing library, so it must not be the
        # place a future seat read goes to die.
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
    identical means those doubles need no change.  In PEER mode the nominal
    active has no such privilege and takes the peer call shape below, so its
    own frozen ceiling reaches the producer before any ``LIMIT`` -- the seat
    that ``source_scope.filter_retrieval_items`` would otherwise be left to
    cover on its own, after the rows had already competed for Top-K.

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

    **Only peer hits are stamped** -- outside peer mode, where ``_peer_leg``
    makes every leg a peer and therefore every hit carries its real owner, so
    that each citation can name the library it came from.  Below is why the
    ordinary single-notebook ask does the opposite.  Tagging the active notebook's own hits
    too would make "single participant -> empty id / several participants ->
    active's own id" a difference every new consumer has to remember to
    normalise, for no gain: ``evidence_context.chunk_citations``,
    ``source_scope.filter_retrieval_items`` and
    ``domain.citation_origin.foreign_notebook_id`` all already read an empty id
    as "the active notebook".  Leaving it empty makes ``not hit.notebook_id``
    the selection layer's clean "this is the user's own library" predicate and
    keeps the value identical to the single-library lane's.

    **Baseline first, supplements appended.**  The generated-question index's
    contract (``docs/product-and-api.md``, "Optional generated-question recall
    supplement") is that a question-only hit may only be *appended* after
    baseline hits -- it "does not evict or reorder them".  The single-library
    lane honours that structurally: ``_retrieve_chunks_multi`` aggregates every
    baseline row before considering any supplement, and there is no shared
    budget for a supplement to spend.  ``peer_evidence`` introduces one
    (``chunk_recall``), so this module has to restore the contract explicitly:
    the cross-library merge runs over BASELINE pools only, and supplements get
    their own pass whose results are appended.  Otherwise one active-library
    question-only hit against ``chunk_recall`` peer baseline hits silently
    costs a baseline candidate that nothing downstream can recover --
    ``quota_fuse_baseline_first`` can only reorder what reached ``collected``.
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
    selected = _select_baseline_then_supplements(
        candidates,
        [_fold_library_pool(pools[notebook_id]) for notebook_id, _ in participants],
        min_relevance=min_relevance,
        relative_relevance=relative_relevance,
    )
    collected = with_active_reserve(
        {hit.chunk_id: hit for hit in selected},
        _reserve_size(candidates.settings, _reserve_k(candidates.settings)),
    )
    per_query = _sub_query_groups(per_task, collected, sub_count)
    ids, matrix = _merge_selected_matrices(candidates, tasks, parts, collected)
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(notebook_id for notebook_id, _ in participants),
    )


def _select_baseline_then_supplements(
    candidates, folded_pools: list, *,
    min_relevance: float, relative_relevance: float,
) -> list:
    """Cross-library selection that a supplement can extend but never displace.

    The split happens AFTER ``_fold_library_pool``, so a passage that is both a
    historical hit and a question-index hit has already collapsed into its
    baseline representative with the union of both support sets -- exactly the
    single-library ordering rule -- and is therefore counted as baseline here.

    Two passes over ``peer_evidence``, each with its own ``chunk_recall``
    budget.  Supplements do not share the baseline's budget (that is the whole
    point), and they keep a budget of their own rather than being unbounded
    because the producer-side caps (``GENERATED_QUESTION_RECALL`` and friends)
    are per library, so a federated arm would otherwise admit
    ``participants x`` that many.  The same ``hit.text`` identity
    ``peer_evidence`` uses inside one pass is applied ACROSS the two, so a
    supplement whose text a baseline hit already carries takes no extra seat.

    **The active reserve is withheld from the merge, not recovered after it.**
    ``peer_evidence``'s budget is a real cap: ten active hits at 0.12 against
    seven libraries contributing 200 hits each at 1.0 leaves only a handful of
    active candidates in a ``chunk_recall``-sized pool, and neither selection
    branch can then supply the seats ``chunk_federation_active_reserve``
    promises -- ``enforce_active_floor`` draws its replacements from that pool.
      So ``_reserve_size`` of the active notebook's strongest
    qualified baseline hits are taken out FIRST and the rest of the libraries
    compete for ``chunk_recall - len(kept)``: the promise is paid out of the
    budget rather than on top of it, so the pool never grows.  The withheld
    hits are removed from every pool before the competition, by chunk id AND by
    ``hit.text``, which is the same identity ``peer_evidence`` de-duplicates on
    -- otherwise a peer copy of the same passage could take a second seat that
    one merge call would never have handed out.  Everything here is inert at
    ``reserve = 0`` (and unreachable for a single participant), which is what
    keeps the rollback path value-identical.
    """
    from app.services.retrieval import partition_generated_question_chunks

    min_relevance, relative_relevance, peer_floor = _merge_thresholds(
        candidates.settings, min_relevance, relative_relevance,
    )
    splits = [partition_generated_question_chunks(pool) for pool in folded_pools]
    baseline_pools = [baseline for baseline, _supplemental in splits]
    budget = (
        candidates.settings.global_ask_candidate_limit
        if federated_ask_active() else candidates.settings.chunk_recall
    )
    kept, unqualified = _withheld_active(
        candidates, baseline_pools, budget,
        min_relevance=min_relevance, relative_relevance=relative_relevance,
    )
    if kept:
        blocked_ids = {hit.chunk_id for hit in kept} | unqualified
        blocked_text = {hit.text for hit in kept}
        baseline_pools = [
            [
                hit for hit in pool
                if hit.chunk_id not in blocked_ids and hit.text not in blocked_text
            ]
            for pool in baseline_pools
        ]
    selected = kept + peer_evidence(
        baseline_pools, max(0, budget - len(kept)),
        min_relevance=min_relevance,
        relative_relevance=relative_relevance,
        peer_floor=peer_floor,
    )
    supplemental = [optional for _baseline, optional in splits]
    if not any(supplemental):
        # The default deployment (``GENERATED_QUESTION_INDEX_MODE=off``) never
        # produces one, and must not pay a second pass to find that out.
        return selected
    seen = {hit.text for hit in selected}
    return selected + [
        hit for hit in peer_evidence(
            supplemental, budget,
            min_relevance=min_relevance,
            relative_relevance=relative_relevance,
            peer_floor=peer_floor,
        )
        if hit.text not in seen
    ]


def _merge_thresholds(
    settings, min_relevance: float, relative_relevance: float,
) -> tuple[float, float, float]:
    """``(min_relevance, relative_relevance, peer_floor)`` for the merge.

    Two ORTHOGONAL knobs, and peer mode moves both -- in opposite directions,
    which is why one function answers for the pair rather than two scattered
    conditionals:

    * **The per-library qualification floors** become the documented global
      rails (``GLOBAL_ASK_MIN_RELEVANCE`` / ``GLOBAL_ASK_RELATIVE_RELEVANCE``).
      They are numeric rails published in ``docs/product-and-api.md`` for
      exactly this question -- "which of a selected library's passages count as
      evidence at all" -- and the chunk lane's own neutral 0/0 would make them
      untrue the moment global ask started running through this module.
    * **``peer_floor`` becomes 0**, i.e. the historical INCOMPARABLE-pools
      contract: every library with a qualified hit keeps one reserved slot.
      ``chunk_federation_peer_floor`` exists to stop N reference libraries that
      merely happen to be MOUNTED from each spending a slot on a worthless
      rank-1 hit.  A peer-mode participant set is the opposite situation: the
      user picked those libraries one by one for this question, and the product
      promise is that a library with qualifying evidence keeps a distinct
      passage.  The absolute floor above is what excludes the irrelevant ones.

    Returned as a tuple and re-bound by the caller so ``_withheld_active`` sees
    the same numbers as the two ``peer_evidence`` passes; it is inert in peer
    mode anyway (``_reserve_size`` is 0), and outside peer mode this returns
    its inputs unchanged, so the non-override path is value-identical.
    """
    if federated_ask_active():
        return (
            float(settings.global_ask_min_relevance),
            float(settings.global_ask_relative_relevance),
            0.0,
        )
    return (
        min_relevance, relative_relevance,
        settings.chunk_federation_peer_floor,
    )


def _withheld_active(
    candidates, baseline_pools: list, budget: int, *,
    min_relevance: float, relative_relevance: float,
) -> tuple:
    """The active notebook's hits that the cross-library cap may not discard.

    Returns ``(kept, unqualified_ids)``: the withheld hits, and the active hits
    the lane's own qualification floor rejects.  The caller must drop the
    second set from the competition too -- see below.

    ``_reserve_size`` of them, strongest first, capped by how many qualified
    baseline hits the active notebook actually has -- the same number the
    finished selection is later held to (``FederatedCollected.active_reserve``
    -> ``retrieval.enforce_active_floor``), read from the same two settings, so
    the two halves agree without either being told.  This half only guarantees
    that the CANDIDATES survive the cross-library cap; the floor itself is
    enforced once, on the finished selection.

    De-duplicated by ``hit.text`` -- the identity ``peer_evidence`` uses --
    before the count is taken.  ``_fold_library_pool``'s identity includes
    ``source_id``, so two active sources holding the same passage keep both
    copies; withholding both would spend the reserve twice on one piece of
    evidence, and the copies would then never meet the text de-duplication
    ``peer_evidence`` would have applied to them.

    Identified by ``not hit.notebook_id`` over the flattened pools rather than
    by taking pool 0.  The active notebook IS pool 0 today (the seat guarantees
    it or falls back to the single-library lane), but a predicate that reads
    what it means survives a reordering that a positional index would silently
    misinterpret as "reserve seats for whichever library came first".

    Two bounds this set must respect, because it bypasses the pass that would
    otherwise apply them:

    * ``peer_evidence``'s own QUALIFICATION floor for the active lane --
      ``max(min_relevance, own peak * relative_relevance)`` -- and its
      finite-score requirement.  Both are neutral (0) on the chunk lane today,
      but global ask passes real values from documented numeric rails, and a
      reserve that smuggled sub-floor evidence past them would quietly make
      those rails untrue for one library.  The floor is computed from the
      lane's peak BEFORE withholding, and the hits it rejects are reported back
      so the caller can drop them from the competition as well: leaving them in
      would hand ``peer_evidence`` an active lane whose peak is now the
      *withheld-from* remainder, and a relative floor recomputed on that lower
      peak lets exactly the evidence this bound just rejected back in through
      the lane's guaranteed slot.
    * the recall budget itself.  ``chunk_recall`` is not validated against
      ``chunk_mmr_k``, so a deployment that shrinks it below the reserve would
      otherwise end up with a pool LARGER than ``chunk_recall`` and made
      entirely of the active notebook.  The reserve is a share of the final
      seats, never a licence to grow the candidate pool.

    Note the intended second-order effect on ``peer_floor``'s comparable mode:
    withholding these hits lowers the active lane's peak in the competition
    that follows, so when the active notebook is itself the strongest library
    the admission threshold peers must clear drops slightly.  That is the right
    reading -- the active notebook has already been served, and the remaining
    budget is a contest among what is left.
    """
    size = _reserve_size(candidates.settings, _reserve_k(candidates.settings))
    if not size:
        return [], set()
    ranked = [
        hit for hit in _qualified_active(
            [hit for pool in baseline_pools for hit in pool]
        )
        if math.isfinite(_score(hit))
    ]
    if not ranked:
        return [], set()
    floor = max(min_relevance, _score(ranked[0]) * relative_relevance)
    qualified: list = []
    seen_text: set = set()
    for hit in ranked:
        if _score(hit) < floor or hit.text in seen_text:
            continue
        seen_text.add(hit.text)
        qualified.append(hit)
    unqualified = {
        hit.chunk_id for hit in ranked if _score(hit) < floor
    }
    kept = qualified[:min(size, max(0, int(budget)))]
    return kept, (unqualified if kept else set())


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

    **A group carries THIS sub-query's relevance.**  ``quota_fuse`` assigns
    every candidate to the group where ``relevance(h)`` is highest and breaks
    ties on the lowest group index, so handing every group the merged
    representative -- whose relevance is the maximum across sub-queries -- makes
    all of a chunk's groups tie and collapses it into the first one.  Two
    sub-queries with overlapping recall windows then stop covering two
    directions: with scores ``(0.9, 0.89, 0.1)`` and ``(0.1, 0.1, 0.8)`` and
    ``fuse_k=2`` the fuse returns the first direction's top two instead of one
    from each.  ``_retrieve_chunks_multi`` is the ground truth here: its
    ``per_query[i]`` is ``{c.chunk_id: c for c in scored}`` -- that sub-query's
    OWN objects with that sub-query's own scores.

    Provenance still comes from the merged representative (supports union,
    ``exact_lookup``, ``notebook_id``), because ``quota_fuse`` hands the
    group's object straight to the answer and a partial support set there would
    under-report why a passage was retrieved.  So a group's entry is the
    representative with this leg's ``relevance``/``score`` restored -- the
    representative object itself whenever they already agree, which is the
    common case and what keeps the single-library values untouched.

    Nothing about the active reserve appears here.  It used to: a dedicated
    single-hit group per reserved seat, placed first, which ``quota_fuse``'s
    round-robin then had to serve.  That construction is only as strong as the
    group list this module hands over, and ``ask_chunk`` appends its own
    keyword/exact groups to it afterwards -- a direct hit that re-scores a
    reserved chunk higher moves it into the new group and the reserved lane
    ends up empty.  The floor therefore belongs after the fusion, not in its
    inputs; see ``retrieval.enforce_active_floor``.

    Order inside a group is by that sub-query's own score descending, ties
    keeping task order, so it is deterministic; ``quota_fuse`` re-sorts by
    relevance anyway, so this only fixes what a reader sees.
    """
    buckets: list = [[] for _ in range(max(1, sub_count))]
    for index, hits in enumerate(per_task):
        bucket = buckets[index % len(buckets)]
        for hit in hits:
            representative = collected.get(hit.chunk_id)
            if representative is None:
                continue
            bucket.append(_as_leg_scored(representative, hit))
    return [
        {hit.chunk_id: hit for hit in sorted(bucket, key=lambda h: -_score(h))}
        for bucket in buckets
    ]


def _as_leg_scored(representative, hit):
    """The merged representative, scored as THIS leg scored it."""
    if (
        hit is representative
        or (hit.relevance == representative.relevance
            and hit.score == representative.score)
    ):
        return representative
    return replace(representative, relevance=hit.relevance, score=hit.score)


def _reserve_k(settings) -> int:
    return int(getattr(settings, "chunk_mmr_k", 0) or 0)


def _reserve_size(settings, k: int) -> int:
    """``ceil(k * reserve)``, clamped to ``k``; ``0`` switches the rule off.

    ZERO IN PEER MODE, and this one line covers all three consumers of the
    number: ``_withheld_active`` returns ``([], set())`` on its first branch,
    ``_merge_results``' ``with_active_reserve`` hands an ordinary ``dict``
    downstream instead of a ``FederatedCollected``, and ``apply_active_reserve``
    reaches ``retrieval.enforce_active_floor`` with ``floor=0``, whose first
    short-circuit makes it inert.  So neither ``enforce_active_floor`` nor
    ``quota_fuse_baseline_first`` needs to know peer mode exists -- and
    ``_qualified_active``'s ``not hit.notebook_id`` predicate, which peer-mode
    stamping would have made permanently false, becomes unreachable rather than
    wrong.
    """
    if federated_ask_active():
        return 0
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
    """The MMR branch's entry to the ONE floor implementation.

    ``RetrievalService.select_chunk_candidates`` calls this after MMR, which on
    the ``single`` chunk branch is genuinely the finished selection: ``ask_chunk``
    has already merged its keyword and exact-lookup hits into ``scored`` before
    asking for the selection, so there is no later producer to defeat the floor
    the way there is on the multi branch.  All this adds is the settings read --
    the same ``_reserve_size``/``_reserve_k`` pair the federated merge uses, so
    both branches reserve one number -- and then it delegates to
    ``retrieval.enforce_active_floor``, which owns the rule (tail-first peer
    replacement, supplements given up before evidence, text de-duplication,
    inert without a peer hit or without a floor).
    """
    from app.services.retrieval import enforce_active_floor

    return enforce_active_floor(selected, pool, _reserve_size(settings, k))


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
