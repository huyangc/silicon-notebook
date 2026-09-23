"""Federated chunk recall: the participant set's passages, not only active's.

Module-level functions taking the ``CandidateRetrievalService`` state as their
first positional argument.  Deliberately NOT new methods on
``retrieval_candidates.py``: that module is already the largest in the
service layer, and keeping the merge/fan-out policy here lets it be
unit-tested without a database.

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
* **...and then the fan-out itself is borrowed.**  A global run additionally
  installs a ``FederatedRunPlan`` (``federated_run.py``), and with one present
  this module stops owning its own concurrency: the tasks run on the run's
  SHARED executor under its fair window, each leg wrapped in a per-library
  ``read_budget``, each failure classified into one of four reason codes, and
  each library's receipt plus the selection's evidence fingerprints handed
  back through the plan's two callbacks.  Without a plan -- every run in
  production today -- ``_run_tasks`` builds its own pool exactly as before and
  not one of those behaviours is reachable.
"""
from __future__ import annotations

import contextvars
import hashlib
import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from app.services.cancellation import AskCancelled, raise_if_cancelled
# Only the NAME, from the dependency-free domain layer: these modules are
# not on the participant override's frozen reader whitelist, but their
# fail-soft handlers must re-raise its control exception instead of
# degrading an identity-attestation failure into an empty result set.
from app.domain.retrieval_control import RetrievalControlError
# The budget vocabulary itself is repository knowledge -- which driver failure
# means "the statement was cancelled" and which means "no connection could be
# leased" -- so it is imported rather than re-derived here; see
# ``read_budget.classify_read_failure``.  The reason codes it produces are
# exactly ``federated_run.LIBRARY_SKIP_REASONS``.
from app.repositories.read_budget import classify_read_failure, read_budget
from app.services.federated_run import (
    LibraryOutcome, current_federated_run_plan,
)
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

def _empty_leg() -> tuple:
    """What one leg returns when it failed.

    The empty shape ``_retrieve_chunks`` itself returns for "nothing found", so
    a failed leg is indistinguishable from an empty one to the merge -- the
    DIFFERENCE travels in the receipt.

    Built fresh on every call rather than shared as a module constant: the
    lists inside are MUTABLE and the merge hands them on (``pools`` collects
    them, ``parts`` keeps the id list).  One shared instance would make every
    failed leg in the process the same two lists.
    """
    return ([], [], None)

# Most-informative-first, for a library whose legs failed for several reasons.
# ``saturated`` wins over ``timeout`` on purpose, and it is the one ordering
# choice here that is not arbitrary: the user-facing sentence for a timeout
# asks them to narrow the scope, which -- when what actually happened is that
# no connection could be leased -- is advice about somebody else's load (the
# argument ``read_budget.classify_read_failure`` spells out).  So a call that
# saw a full pool at all reports the code whose copy guesses nothing.
# ``queue_deadline`` outranks ``unavailable`` because it is a fact about this
# run's budget rather than an unclassified fault.
_SKIP_SEVERITY = ("saturated", "timeout", "queue_deadline", "unavailable")


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
    # A leg that is NOT the semantic chunk leg: ``_producer_call`` hands the
    # task to this callable instead of calling ``_retrieve_chunks``. ``None``
    # -- every semantic leg -- keeps the historical call shape exactly. Its
    # users are the peer-mode keyword and exact-identifier arms
    # (``_run_supplement_arm``), which borrow this module's whole fan-out
    # rather than growing a second concurrency mechanism.
    producer: Callable[["_Task"], tuple] | None = None
    # Which recall arm the leg belongs to (``"keyword"`` / ``"exact"``),
    # stamped on its skip event so an operator can tell a supplementary leg's
    # skip from a semantic leg's (only the latter decides coverage). Empty --
    # and absent from the event -- for the
    # semantic legs, whose events keep their historical fields.
    arm: str = ""


class _AnyCancelled:
    """``is_set()`` over several cancellation sources, as ONE token.

    ``ReadBudget`` and ``raise_if_cancelled`` only ever ask a token for
    ``is_set()``, so a composite satisfies the same contract without a second
    plumbing path.  This is how "this call has given up" reaches the legs that
    are still executing -- without it, a leg whose parent already returned goes
    on holding a shared-executor seat and a database connection for the rest of
    its own per-library budget, while the OTHER global jobs compute their fair
    window from that same pool.

    A copy of ``global_ask._AnyCancelled`` rather than an import: this module
    may not depend on the job layer, and the class is four lines of ``any``.
    """

    __slots__ = ("_tokens",)

    def __init__(self, *tokens):
        self._tokens = tuple(token for token in tokens if token is not None)

    def is_set(self) -> bool:
        return any(token.is_set() for token in self._tokens)


@dataclass
class _LegClock:
    """What the PARENT needs to know about a leg it can no longer wait for.

    Created in the parent thread, written once by the worker, read by the
    parent when the phase expires.  One bool and one float written by a single
    writer: no lock, and no invariant spanning the two.

    ``producer_started`` is the ``queue_deadline``/``timeout`` split carried
    into the fan-out slot.  A leg parked on ``retrieval_fanout_slot``'s
    semaphore has asked the database NOTHING -- the run's own ``fanout_limit``
    is what it is waiting for -- so charging it "timeout" would tell the user
    to narrow a scope that was never queried.  The flag is set at the last
    moment before the producer call, and the per-library budget starts on the
    same line, so slot waiting is charged to neither.
    """

    producer_started: bool = False
    deadline: float = 0.0


@dataclass(frozen=True)
class _Legs:
    """What ONE federated call has to say about each participant library.

    Assembled across three places that each know only part of it -- the
    participant order comes from the seat, the dropped libraries from task
    preparation, the per-task reasons from the fan-out -- and consumed in one
    place, on the parent thread, where the candidate counts are also known.
    Bundled rather than threaded as three parameters so ``_merge_results``
    grows one optional argument instead of three.

    ``order`` is the FULL participant set, not the task table's own
    ``_task_participants``: a library whose preparation failed has no task and
    would otherwise vanish from the receipts entirely -- neither searched nor
    skipped -- which is the one outcome the coverage lists may not have.
    """

    order: tuple = ()
    reasons: tuple = ()
    dropped: dict = field(default_factory=dict)
    # This call's phase deadline, so the closing evidence read is bounded by
    # the same budget the legs were.
    deadline: float = 0.0


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

    THE RUN PLAN IS READ ONCE, HERE, and only in peer mode.  Once per call and
    not per leg, so every leg of one fan-out shares one phase deadline and one
    window source; and gated on ``federated_ask_active()`` so that a plan
    installed without an override -- a shape ``global_run.global_ask_run``
    makes unconstructible, but a test or a future caller could still assemble
    -- cannot switch the ordinary notebook path onto a borrowed executor.

    A call with NO sub-query returns before the participant seat is read, and
    therefore sends no receipts at all -- not even "skipped" ones.  That is the
    intended reading: with no leg there is no "searched" or "skipped" to
    report, and the run's coverage of a round it never fanned out is the job's
    question, not this module's.
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
    plan = current_federated_run_plan() if federated_ask_active() else None
    # ONE deadline for the whole call, derived BEFORE anything reads a
    # database: task preparation charges its per-library reads against it just
    # as the fan-out charges its legs, so a slow library cannot spend the phase
    # in a place the budget does not reach.
    deadline = (
        time.monotonic() + float(plan.phase_timeout_seconds)
        if plan is not None else 0.0
    )
    dropped: dict = {}
    tasks = _federated_tasks(
        candidates, active_notebook_id, participants, queries, drifted,
        # ``None`` without a plan, so the ordinary notebook path's skip event
        # keeps exactly the fields it has always had: there is no receipt seam
        # to carry a reason code to, and a code in the event alone would show
        # up in one deployment's telemetry and not another's.
        dropped=dropped if plan is not None else None,
        # The same deadline the fan-out will charge its legs against: with a
        # plan the preparation reads are inside the phase too, which is why it
        # had to be derived above rather than inside ``_run_planned_tasks``.
        plan=plan, deadline=deadline,
    )
    results, reasons = _run_tasks(candidates, tasks, plan, deadline)
    return _merge_results(
        candidates, _task_participants(tasks), tasks, results, len(queries),
        min_relevance=min_relevance, relative_relevance=relative_relevance,
        plan=plan,
        legs=_Legs(tuple(participants), tuple(reasons), dropped, deadline),
    )


def federated_keyword_chunk_candidates(
    candidates, active_notebook_id: str, needle: str,
    leg: Callable[[str, "tuple | None"], list],
) -> list:
    """PEER mode's bilingual-keyword arm: one keyword leg per participant.

    ``leg(notebook_id, visible)`` is the single-library keyword search for ONE
    library under that library's frozen ceiling; it runs inside the task's own
    ``Context`` on the same fan-out the semantic legs use (``_run_tasks``: the
    borrowed executor, the fair window, this call's phase deadline, the
    per-library ``read_budget``, the fan-out slot and cancellation), so the arm
    owns no concurrency of its own.

    The participant set and each library's ceiling come from the one place the
    semantic legs take them (``_bounded_participants`` and
    ``_federated_tasks``/``_peer_visible_sources``).  The SILENT form of the
    seat on purpose: the truncation fact is announced once per call by the
    semantic arm, and a second copy would read like a second cut.

    ⛔ NO RECEIPTS.  Coverage (searched/skipped) is the semantic legs' verdict
    alone: this function never reaches ``_merge_results``,
    ``_report_receipts`` or ``plan.on_library``, so a keyword leg that timed
    out or failed is only "this library had no keyword hits" -- fail-open,
    with its skip event marked ``arm="keyword"``.

    ⛔ BUT EVIDENCE, YES (codex #787 r1).  The merged list -- exactly what the
    caller receives, after interleave/dedup/cap -- goes through
    ``_report_evidence`` (``on_evidence_groups`` + ``on_evidence``) under a
    run plan, as a ``FederatedCollected`` shaped like the semantic merge's.  A
    passage only this arm retrieved must carry a retrieval-time fingerprint:
    absent from the run's table, the citation re-check would read it as a
    non-federated element and wave a stale citation through.  The consumer's
    merge is directional (a real snapshot is never overwritten by a later
    ``None``), so this cannot undo what a semantic leg already attested.
    Without a plan nothing is registered, as for the semantic legs.  ``AskCancelled`` and the attestation error still
    propagate through ``_run_one``/``_run_tasks`` exactly as for a semantic leg.

    Failures are therefore visible ONLY in telemetry: the summary event's
    ``failed_libraries`` and the per-leg skip events.  The reasoning trace's
    ``keyword_failed`` flag covers the single-notebook path alone -- in peer
    mode this function never raises for a failed leg, even when every library
    failed, and must not start to: the ``chunk``-mode caller has no ``try``
    around it, so raising would fail the whole answer over a supplement.

    ⛔ The whole arm, preparation included, is bounded by ONE per-library
    budget (``_supplement_arm_deadline``), not by the phase.

    Its per-leg FTS never OPENS the run's per-library FTS circuit, though it
    obeys one that is already open (``_chunk_fts_hits(trip_circuit=False)``):
    that circuit is shared with the semantic legs' own lexical union.

    Merge: each library's hits in its own keyword-score order, interleaved
    round-robin (every library's 1st, then every 2nd, ...), first occurrence
    of a ``chunk_id`` wins, capped at ``global_ask_candidate_limit`` -- so no
    library can fill THIS merged list by volume and its length has a fixed
    bound.  (What a caller then selects from it is the caller's rule: the
    reasoning seed re-ranks by score.)
    Every hit is stamped with its owning library, as the semantic legs' are in
    peer mode.

    Emits one content-free ``ask_stage``/``global_keyword_arm`` summary.
    """
    started = time.perf_counter()
    arm = _run_supplement_arm(
        candidates, active_notebook_id, needle, leg, "keyword",
    )
    columns = [
        _stamped(task, sorted(
            value, key=lambda hit: -float(hit.relevance or 0.0),
        ))
        for task, value in zip(arm.tasks, arm.values)
    ]
    merged = _interleave_capped(
        columns, int(candidates.settings.global_ask_candidate_limit),
    )
    _register_supplement_evidence(candidates, arm, merged)
    _emit(candidates, {
        "kind": "ask_stage",
        "stage": "global_keyword_arm",
        "site": "global_keyword_arm",
        "participants": len(arm.participants),
        "libraries_with_hits": sum(1 for column in columns if column),
        "merged": len(merged),
        "failed_libraries": arm.failed_libraries,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    })
    return merged


def federated_exact_chunk_candidates(
    candidates, active_notebook_id: str, query: str,
    leg: Callable[[str, "tuple | None"], list],
) -> list:
    """PEER mode's exact-identifier arm: one whole-section lookup per library.

    The same shape as ``federated_keyword_chunk_candidates`` and the same
    machinery (``_run_supplement_arm``): one task per participant on the
    semantic legs' fan-out, each under that library's frozen ceiling, the
    whole arm inside ONE per-library budget (``_supplement_arm_deadline``).
    ``leg(notebook_id, visible)`` returns that library's lookup as SECTIONS
    (``exact_lookup.exact_lookup_sections``: a list of chunk lists).

    ⛔ The caller has already established that the question names something
    worth probing (``exact_lookup.exact_lookup_terms``) -- this function reads
    the participant seat and starts tasks unconditionally, so an
    identifier-free question must never reach it.

    ⛔ NO RECEIPTS, NO BANNER, BUT EVIDENCE -- exactly the keyword arm's
    rules, for the same reasons: coverage is the semantic legs' verdict alone,
    a failed or timed-out leg is only "this library had no exact section"
    (a skip event with ``arm="exact"``, nothing else), and the merged list is
    registered through ``_report_evidence`` under a run plan so a passage only
    this arm retrieved still carries a retrieval-time fingerprint.
    ``AskCancelled`` and the attestation error propagate.

    No FTS circuit is involved: the lookup issues its own
    ``knowledge.chunk_exact_search`` probe, which never went through
    ``_chunk_fts_hits`` and therefore neither obeys nor opens that circuit --
    the single-library path's behaviour, unchanged.

    Merge by WHOLE SECTION (``_interleave_sections_capped``): library 1's
    first section, library 2's first section, ..., then every library's
    second, and so on; a ``chunk_id`` already taken is dropped; the total is
    capped at ``global_ask_candidate_limit`` without ever cutting a section --
    the first section that does not fit ends the merge.  Handing a caller half
    of a command's section is the failure this channel exists to prevent.
    Every hit keeps its ``exact_lookup`` flag and is stamped with its library.

    Emits one content-free ``ask_stage``/``global_exact_arm`` summary.
    """
    started = time.perf_counter()
    # ``drifted=False``: the lexical-lane drift verdict routes only the
    # keyword/semantic FTS arms; an exact leg never reads it, so probing it
    # (two live source reads) would be pure waste.
    arm = _run_supplement_arm(
        candidates, active_notebook_id, query, leg, "exact", drifted=False,
    )
    columns = [
        [_stamped(task, section) for section in value]
        for task, value in zip(arm.tasks, arm.values)
    ]
    merged, sections = _interleave_sections_capped(
        columns, int(candidates.settings.global_ask_candidate_limit),
    )
    _register_supplement_evidence(candidates, arm, merged)
    _emit(candidates, {
        "kind": "ask_stage",
        "stage": "global_exact_arm",
        "site": "global_exact_arm",
        "participants": len(arm.participants),
        "libraries_with_hits": sum(1 for column in columns if column),
        "merged": len(merged),
        "sections": sections,
        "failed_libraries": arm.failed_libraries,
        "latency_ms": round((time.perf_counter() - started) * 1000),
    })
    return merged


@dataclass(frozen=True)
class _SupplementArm:
    """What one fail-open supplementary arm's fan-out produced.

    ``values`` is one entry per task (= per library: these arms run one query
    each), in task-table order; a failed leg's entry is empty.
    ``failed_libraries`` counts every library that was dropped in preparation,
    raised, or was skipped with a reason code.
    """

    participants: tuple
    plan: Any
    deadline: float
    tasks: list
    values: list
    failed_libraries: int


def _run_supplement_arm(
    candidates, active_notebook_id: str, query: str, leg, arm: str, *,
    drifted: bool | None = None,
) -> _SupplementArm:
    """The shared fan-out of the peer-mode keyword and exact-identifier arms.

    Participants and each library's ceiling come from the one place the
    semantic legs take them (``_bounded_participants`` and
    ``_federated_tasks``/``_peer_visible_sources``) -- the SILENT form of the
    seat on purpose: the truncation fact is announced once per call by the
    semantic arm, and a second copy would read like a second cut.  Each leg
    runs on ``_run_tasks`` (borrowed executor, fair window, per-library
    ``read_budget``, fan-out slot, cancellation) with ``producer``/``arm`` set,
    so neither arm owns any concurrency of its own and every skip event it
    causes carries ``arm``.

    A leg's failure is re-raised into ``_run_one`` (which classifies and
    emits it) after being noted here; nothing in this function reaches
    ``_merge_results``, ``_report_receipts`` or ``plan.on_library``.

    ``drifted`` is handed to ``_federated_tasks`` as is: ``None`` (the keyword
    arm) probes the lexical-lane drift verdict once for the arm, as the
    semantic arm does; an arm whose legs never read it passes a value.
    """
    participants, _mounted_total = _bounded_participants(
        candidates, active_notebook_id,
    )
    plan = current_federated_run_plan()
    deadline = _supplement_arm_deadline(plan)
    failed: dict = {}

    def _producer(task: _Task) -> tuple:
        try:
            return (list(leg(task.notebook_id, task.visible)), [], None)
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception:
            # Written by one worker per library (distinct keys) and read by the
            # parent only after the fan-out returned; re-raised so ``_run_one``
            # classifies and emits it like any other leg.
            failed[task.notebook_id] = True
            raise

    dropped: dict = {}
    tasks = _federated_tasks(
        candidates, active_notebook_id, participants, [query], drifted,
        dropped=dropped, plan=plan, deadline=deadline,
        producer=_producer, arm=arm,
    )
    results, reasons = (
        _run_tasks(candidates, tasks, plan, deadline) if tasks else ([], [])
    )
    unhealthy = set(dropped) | set(failed) | {
        task.notebook_id for task, reason in zip(tasks, reasons) if reason
    }
    return _SupplementArm(
        participants=participants, plan=plan, deadline=deadline, tasks=tasks,
        values=[list(value[0]) for value in results],
        failed_libraries=len(unhealthy),
    )


def _stamped(task: _Task, hits) -> list:
    """``hits`` stamped with the task's library, as the semantic legs' are."""
    return [
        replace(hit, notebook_id=task.notebook_id) if task.peer else hit
        for hit in hits
    ]


def _register_supplement_evidence(candidates, arm: _SupplementArm,
                                  merged: list) -> None:
    """Retrieval-time evidence for exactly what a supplementary arm hands out.

    The same read, rules and consumer the semantic selection uses
    (``_report_evidence``), so a passage only a supplementary arm found is
    re-checked like any other federated one instead of looking "never
    travelled this channel" (codex #787 r1).  No receipt.  Nothing without a
    run plan, as for the semantic legs, and nothing for an empty list.
    """
    if arm.plan is None or not merged:
        return
    _report_evidence(
        candidates, arm.plan,
        FederatedCollected({hit.chunk_id: hit for hit in merged}),
        arm.deadline,
    )


def _supplement_arm_deadline(plan) -> float:
    """A supplementary arm's WHOLE-arm deadline: one per-library budget.

    Shared by the peer-mode keyword and exact-identifier arms.  Derived once,
    before the first read (task preparation included), like the semantic
    arm's -- but shorter.  These arms are fail-open supplements, so on a
    saturated database each may cost at most about one
    ``notebook_timeout_seconds`` on top of the semantic fan-out rather than a
    whole ``phase_timeout_seconds``; and never more than the phase either.
    ``0.0`` without a plan, the value every unplanned caller passes.
    """
    if plan is None:
        return 0.0
    return time.monotonic() + min(
        float(plan.phase_timeout_seconds), float(plan.notebook_timeout_seconds),
    )


def _interleave_sections_capped(columns: list, limit: int) -> tuple:
    """``(merged, sections)``: per-library section lists, merged section-wise.

    Round-robin over the libraries one WHOLE section at a time.  Chunks
    already taken are dropped from a later section (a section left empty by
    that is skipped, not counted); the first section whose remaining chunks do
    not fit under ``limit`` ends the merge, so no section is ever cut.
    ``sections`` counts the sections that contributed.
    """
    merged: list = []
    seen: set = set()
    sections = 0
    for position in range(max((len(column) for column in columns), default=0)):
        for column in columns:
            if position >= len(column):
                continue
            fresh: list = []
            taken: set = set()
            for hit in column[position]:
                if hit.chunk_id in seen or hit.chunk_id in taken:
                    continue
                taken.add(hit.chunk_id)
                fresh.append(hit)
            if not fresh:
                continue
            if len(merged) + len(fresh) > limit:
                return merged, sections
            seen.update(taken)
            merged.extend(fresh)
            sections += 1
    return merged, sections


def _interleave_capped(columns: list, limit: int) -> list:
    """Round-robin over per-library ranked lists, de-duplicated, capped."""
    merged: list = []
    seen: set = set()
    for position in range(max((len(column) for column in columns), default=0)):
        for column in columns:
            if len(merged) >= limit:
                return merged
            if position >= len(column):
                continue
            hit = column[position]
            if hit.chunk_id in seen:
                continue
            seen.add(hit.chunk_id)
            merged.append(hit)
    return merged


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
    drifted: bool | None, *, dropped: dict | None = None, plan=None,
    deadline: float = 0.0, producer=None, arm: str = "",
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

    ``dropped`` is where that skip is RECORDED when the caller wants receipts:
    ``{notebook_id: reason}`` for each peer whose preparation failed.  An
    out-parameter rather than a second return value because the task table is
    read positionally by existing callers, and a plain return of the drops from
    here would report them in preparation order -- the receipts must follow the
    participant order instead, which only the caller knows in full.

    ⛔ WITH A PLAN, PREPARATION IS INSIDE THE BUDGET TOO.  Those two live reads
    run SERIALLY in this thread, before any task exists and therefore before
    the executor, the fair window and the per-library budgets have anything to
    bound -- so an unbudgeted preparation lets ONE slow library spend the whole
    phase before a single healthy library has been submitted, which is the
    exact failure per-library budgets exist to prevent.  Each peer's pair of
    reads therefore gets ``min(this call's phase deadline, now + the plan's
    per-library budget)`` and the run's cancel token, and a peer whose
    preparation expires or fails is dropped alone (the isolation this function
    already had) while the rest of the set continues.  Once the phase deadline
    itself has passed, the remaining peers are dropped WITHOUT being asked
    anything, so their code is ``queue_deadline`` rather than ``timeout`` --
    the same split ``_run_planned_legs`` makes, for the same reason.

    ``plan``/``deadline`` are ``None``/``0.0`` for every caller without a run
    plan, and that path is byte-identical: no budget is opened, no deadline is
    consulted, and the two reads keep their present shapes.

    ``producer``/``arm`` are carried onto every task (and ``arm`` onto the
    preparation-skip events) for a non-semantic arm that reuses this exact
    preparation -- the same participant loop, the same frozen per-library
    ceiling, the same drop rules -- instead of a second copy of them.  Both
    default to the semantic leg's values, which leave its tasks and events
    unchanged.
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
            peek = False
            if peer:
                started = time.perf_counter()
                if plan is not None and deadline - time.monotonic() <= 0:
                    # The phase is over and this library has been asked
                    # NOTHING, so it is queued-out rather than timed-out: the
                    # copy for ``timeout`` tells the user to narrow a scope
                    # that was never searched. Same code, same argument as a
                    # task still waiting in ``_run_planned_legs``. The event
                    # keeps the field-for-field shape of the preparation
                    # failure below; an empty ``error_type`` and a zero latency
                    # are what "nothing ran" looks like in it.
                    if dropped is not None:
                        dropped[notebook_id] = "queue_deadline"
                    event = {
                        "kind": "chunk_federation_skipped",
                        "notebook_id": notebook_id,
                        "error_type": "",
                        "latency_ms": 0,
                        "reason": "queue_deadline",
                    }
                    if arm:
                        event["arm"] = arm
                    _emit(candidates, event)
                    continue
                # ``min`` of the two bounds, never the per-library one alone,
                # for the reason ``_budgeted_retrieve_for`` spells out for a
                # leg: a preparation begun just before the phase ends may not
                # hold a connection past the end of the call.
                prepared_by = 0.0 if plan is None else min(
                    deadline,
                    time.monotonic() + float(plan.notebook_timeout_seconds),
                )
                try:
                    visible, peek = _prepared_peer(
                        candidates, notebook_id, plan, prepared_by,
                    )
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
                    event = {
                        "kind": "chunk_federation_skipped",
                        "notebook_id": notebook_id,
                        "error_type": type(exc).__name__,
                        "latency_ms": round(
                            (time.perf_counter() - started) * 1000
                        ),
                    }
                    if dropped is not None:
                        # The driver's own answer wins, exactly as it does for
                        # a leg (``_run_one``): a statement the server
                        # cancelled AT the deadline and a local clock that has
                        # just passed it are one event seen from two sides.
                        # Only an UNCLASSIFIED failure is decided by the clock,
                        # and without a plan there is no clock to decide with,
                        # so it stays ``unavailable``.
                        dropped[notebook_id] = classify_read_failure(exc) or (
                            "timeout"
                            if plan is not None
                            and time.monotonic() >= prepared_by
                            else "unavailable"
                        )
                        event["reason"] = dropped[notebook_id]
                    if arm:
                        event["arm"] = arm
                    _emit(candidates, event)
                    continue
            peek_token = _CHUNK_PEEK_ONLY.set(peek)
            peer_token = _CHUNK_PEER_LEG.set(peer)
            try:
                tasks.extend(
                    _Task(
                        notebook_id, tier, query, contextvars.copy_context(),
                        peer, visible, producer, arm,
                    )
                    for query in sub_queries
                )
            finally:
                _CHUNK_PEER_LEG.reset(peer_token)
                _CHUNK_PEEK_ONLY.reset(peek_token)
    finally:
        _CHUNK_ARM_DRIFTED.reset(drift_token)
    return tasks


def _prepared_peer(candidates, notebook_id: str, plan, deadline: float) -> tuple:
    """``(visible ceiling, peek-only)`` for one peer, under ONE read budget.

    The two reads are paired here rather than left where they were because
    they are one unit of work to the caller: both are live per-library reads
    issued serially in the parent thread before any task exists, both open a
    pooled connection of their own, and a library that cannot answer the first
    has nothing to contribute whatever the second would have said.  Sharing one
    budget also makes the bound mean what it says -- two budgets of the same
    size would let one library spend twice the stated per-library allowance.

    ⛔ THE BUDGET IS THE ONLY DIFFERENCE, and it exists only with a plan.
    Without one this is the pre-existing pair of calls in the pre-existing
    order, ``_peek_only`` included -- it swallows every failure of its own
    probe and answers the conservative side, so putting it inside the caller's
    handler changes nothing it can reach, while leaving it outside the budget
    would leave exactly half of the finding open.
    """
    if plan is None:
        return (
            _peer_visible_sources(candidates, notebook_id),
            _peek_only(candidates, notebook_id),
        )
    # ⛔ BEFORE the budget, and the same line ``_run_one`` opens with. A run
    # that is already cancelled must not open a connection at all, and the
    # answer is ``AskCancelled`` rather than a skip: entering a budget whose
    # token is set raises ``ReadBudgetExceeded``, which classifies as
    # ``timeout`` and would persist a user's own stop as a coverage list
    # reading "every library timed out".
    raise_if_cancelled(plan.cancel)
    with read_budget(deadline, plan.cancel):
        return (
            _peer_visible_sources(candidates, notebook_id),
            _peek_only(candidates, notebook_id),
        )


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


def _run_tasks(candidates, tasks: list, plan=None, deadline: float = 0.0) -> tuple:
    """``(results, reasons)`` -- one entry per task, in task-table order.

    ⛔ WITHOUT A PLAN THIS IS THE PRE-EXISTING FUNCTION.  Same own executor,
    same ``chunk_fanout_max_workers`` bound, same ``pool.map`` over the table
    in order, same ``with`` (that pool is this call's own and must be shut
    down).  ``reasons`` is then all-empty: there is no receipt seam to carry a
    code to, and inventing one would change the events the ordinary notebook
    path emits.
    """
    if plan is not None:
        return _run_planned_tasks(candidates, tasks, plan, deadline)
    workers = min(len(tasks), max(1, candidates.settings.chunk_fanout_max_workers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        legs = list(pool.map(lambda task: _run_one(candidates, task), tasks))
    return [value for value, _reason in legs], [reason for _value, reason in legs]


def _run_planned_tasks(candidates, tasks: list, plan, deadline: float) -> tuple:
    """The global run's fan-out: borrowed executor, fair window, one deadline.

    Structurally ``global_ask._fan_out``, moved one layer down and re-keyed
    from libraries to TASKS, with the four properties that function existed for
    kept intact:

    * **The executor is borrowed, never owned.**  No ``with``: it is the one
      pool per process and therefore the only real bound on retrieval-held
      database connections, so leaving a ``with`` block would shut down a pool
      the next job still needs.  Nothing here shuts it down or waits on it.
    * **The window is re-read on every top-up**, so a job that started alone
      gives ground back as soon as one of its tasks finishes and a newcomer
      waits at most one task rather than a whole phase.
    * **The phase deadline is derived once per call** by the caller (see
      ``FederatedRunPlan.phase_timeout_seconds``): a reasoning run enters this
      function once per retrieval round with model calls in between, and a
      deadline fixed when the run started would declare rounds 2..n dead on
      arrival.
    * **Leaving sets the phase token.**  ``phase`` is this call's own "I am no
      longer waiting for you" flag, composed with the run's cancel token into
      the one token every leg's ``read_budget`` polls.  Without it, ONE leg's
      hard failure (an attestation error, a cancellation) returns the parent
      immediately while the other legs keep a shared-executor seat and a
      database connection for the rest of their own per-library budget -- and
      the OTHER jobs' fair window is computed from that same pool.  It is set
      in ``finally``, so every exit -- expiry, failure, success -- releases
      them.
    * **Expiry is two different facts, and the leg decides which.**  A task
      that never reached its producer call asked the database NOTHING --
      whether it was still queued here or parked on the run's own fan-out
      semaphore -- so it is ``queue_deadline``, whose copy must not blame the
      library's size or the selected scope.  Only a leg that got as far as the
      producer is charged ``timeout``.  Either way it is then ABANDONED rather
      than waited on: its budget is capped by this same deadline, so it cannot
      outlive the phase, and blocking here would spend the synthesis budget of
      a call that has already given up on its result.

    Fairness is per LIBRARY, not per task: the table is library-major, so
    submitting it in order would let one library's sub-queries fill a window of
    2 while seven libraries wait.  ``_round_robin`` re-orders the SUBMISSION
    (never the results, which stay in task-table order for the merge) so each
    library gets its first sub-query in before any library gets its second.

    ⛔ NOT GUARDED, AND UNGUARDABLE FROM HERE: the caller must not itself be
    running on ``plan.executor``.  This function blocks in ``wait`` for tasks
    it has just submitted to that pool, so being one of that pool's own workers
    is the classic self-deadlock -- the seat needed to finish the work is the
    one the waiter is sitting in.  ``_refuse_pool_thread`` catches the shape
    that is cheap to detect (the pool's own thread-name prefix); an executor
    that renames its threads defeats it, so the rule stands on this paragraph
    and not on that check.
    """
    _refuse_pool_thread(plan)
    with _call_scope(plan):
        return _run_planned_legs(candidates, tasks, plan, deadline)


@contextmanager
def _call_scope(plan):
    """Declare this whole fan-out in flight, for as long as it runs.

    The owner counts open scopes and divides the shared pool by that count
    (see ``FederatedRunPlan.call_scope``), so the scope has to wrap the entire
    call -- from before the first submission to after the last leg is either
    harvested or abandoned -- and has to be released on every exit. A plan
    without one is a no-op, which is what keeps every non-global caller
    unchanged.
    """
    scope = getattr(plan, "call_scope", None)
    if scope is None:
        yield
        return
    with scope():
        yield


def _run_planned_legs(candidates, tasks: list, plan, deadline: float) -> tuple:
    """``_run_planned_tasks``' body, inside its call scope."""
    phase = threading.Event()
    abort = _AnyCancelled(plan.cancel, phase)
    results: list = [_empty_leg() for _ in tasks]
    reasons: list = [""] * len(tasks)
    clocks: list = [_LegClock() for _ in tasks]
    pending = _round_robin(tasks)
    futures: dict = {}
    try:
        while pending or futures:
            if deadline - time.monotonic() <= 0:
                _harvest_finished(futures, results, reasons)
                for index in pending:
                    reasons[index] = "queue_deadline"
                    _emit_leg_skipped(candidates, tasks[index], "queue_deadline", "", 0)
                for index in futures.values():
                    # The leg itself says whether it ever reached a producer.
                    reasons[index] = (
                        "timeout" if clocks[index].producer_started
                        else "queue_deadline"
                    )
                    # ``lane`` distinguishes this row from the one the leg
                    # itself may still emit when it finally gives up: the leg
                    # is abandoned, not finished, so both facts are real and an
                    # operator must be able to tell them apart instead of
                    # reading one leg's expiry as two skips.
                    _emit_leg_skipped(
                        candidates, tasks[index], reasons[index], "", 0,
                        lane="abandoned",
                    )
                break
            while pending and len(futures) < max(1, int(plan.window())):
                index = pending.pop(0)
                futures[plan.executor.submit(
                    _run_one, candidates, tasks[index], plan, deadline,
                    abort, clocks[index],
                )] = index
            done, _not_done = wait(
                list(futures), timeout=max(0.0, deadline - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                index = futures.pop(future)
                # Re-raises cancellation and attestation failures on THIS
                # thread, exactly as ``pool.map`` does above: neither is a
                # per-library degradation, and both must fail the whole arm.
                results[index], reasons[index] = future.result()
    finally:
        # Queued tasks must not start, and the legs already running must stop
        # holding a pool seat for a call that is over. ``cancel()`` handles the
        # first; ``phase`` is what reaches the second, at its next
        # ``budget.check()``. The callback keeps an abandoned failure from
        # being an exception nobody ever retrieves.
        phase.set()
        for future in futures:
            if not future.cancel():
                future.add_done_callback(_discard_leg)
    # AFTER the loop, and only the run's own token: a phase that merely ran out
    # of budget produces receipts, but a cancelled RUN must not be answered
    # with "every library timed out" -- mid-flight cancellation reaches the
    # legs as an expired budget, which would otherwise be classified exactly
    # that way and persisted as a coverage list.
    raise_if_cancelled(plan.cancel)
    return results, reasons


def _harvest_finished(futures: dict, results: list, reasons: list) -> None:
    """Take the results of legs that already finished cleanly at the deadline.

    A leg that completed while the parent was in its last ``wait`` has a real
    result; discarding it as "timeout" would throw away evidence the database
    already produced. Legs that finished by RAISING are re-raised here instead:
    ``_run_one`` only lets cancellation and attestation failures out, and
    neither may be downgraded into a per-library skip just because the clock
    also happened to run out.
    """
    for future in list(futures):
        if not future.done() or future.cancelled():
            continue
        error = future.exception()
        if error is not None:
            raise error
        index = futures.pop(future)
        results[index], reasons[index] = future.result()


def _refuse_pool_thread(plan) -> None:
    """Refuse to fan out from a thread of the very pool being fanned out to.

    Best effort by design (see ``_run_planned_tasks``): it recognises a
    ``ThreadPoolExecutor``'s own naming convention and nothing else, because
    the alternative -- asking the executor which threads are its own -- is not
    part of the ``Executor`` interface.
    """
    prefix = getattr(plan.executor, "_thread_name_prefix", "")
    if prefix and threading.current_thread().name.startswith(str(prefix)):
        raise RuntimeError(
            "a federated fan-out may not run on its own executor's thread"
        )


def _discard_leg(future) -> None:
    """Consume an abandoned leg's outcome so nothing is left unretrieved."""
    try:
        future.exception()
    except Exception:  # noqa: BLE001 - the phase is already over
        pass


def _round_robin(tasks: list) -> list:
    """Task indexes re-ordered library-minor: every library's q1, then q2...

    The table itself stays library-major (``per_query`` grouping depends on
    it); only the order tasks are SUBMITTED in changes, which is the order that
    decides who gets the scarce window seats.
    """
    columns: dict = {}
    for index, task in enumerate(tasks):
        columns.setdefault(task.notebook_id, []).append(index)
    ordered: list = []
    for position in range(max((len(column) for column in columns.values()), default=0)):
        for column in columns.values():
            if position < len(column):
                ordered.append(column[position])
    return ordered


def _run_one(candidates, task: _Task, plan=None, deadline: float = 0.0,
             cancel=None, clock=None) -> tuple:
    """``(value, reason)`` for one task; ``reason`` is empty unless it failed.

    ⛔ THE PER-LIBRARY BUDGET DOES NOT START HERE.  It starts one frame below,
    after the run's fan-out slot has been acquired (``_budgeted_retrieve_for``),
    because neither kind of waiting may be charged to it: not the wait behind
    this call's own window -- the property ``global_ask._retrieve_notebook``
    documents -- and not the wait on the run's ``fanout_limit`` semaphore,
    which is a second queue nobody asked the database anything from.  What this
    frame contributes is the PHASE cap, so that waiting cannot let a late task
    run past the end of the call either.
    """
    started = time.perf_counter()
    if clock is None:
        clock = _LegClock(deadline=deadline)
    if plan is not None:
        # A run already cancelled -- or a call that has already stopped waiting
        # -- must not open a connection at all, and the answer is
        # ``AskCancelled`` rather than a skip: the user's own decision is never
        # a per-library degradation. A cancel that arrives mid-flight is seen
        # by ``budget.check()`` instead, which is why the parent re-checks the
        # run's token before it publishes any receipt.
        raise_if_cancelled(cancel if cancel is not None else plan.cancel)
    try:
        if plan is None:
            return task.context.run(_retrieve_for, candidates, task), ""
        return task.context.run(
            _budgeted_retrieve_for, candidates, task, plan, deadline,
            cancel if cancel is not None else plan.cancel, clock,
        ), ""
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
        reason = ""
        if plan is not None and not clock.producer_started:
            # It never got past the fan-out slot, so whatever went wrong, this
            # library was never asked anything. Same code as a task that was
            # still queued in the parent, and for the same reason.
            reason = "queue_deadline"
        elif plan is not None:
            # A statement the server cancelled AT the deadline and a local
            # clock that has just passed it are one event seen from two
            # sides, so the driver's own answer wins and the clock only
            # decides what an UNCLASSIFIED failure was.
            reason = classify_read_failure(exc) or (
                "timeout" if time.monotonic() >= clock.deadline
                or _keyword_lexical_timeout(task, exc)
                else "unavailable"
            )
        # Emit inside THIS TASK's own context, not the worker thread's bare
        # one. The events logger is per-user and picks its directory from the
        # ``_log_owner`` ContextVar (``app.core.event_logging``), which a pool
        # thread starts out without -- so emitting here without the context
        # would file every federated skip under ``user-local`` no matter who
        # asked. The context is no longer entered once ``run`` above has
        # raised, so running it a second time is legal.
        task.context.run(
            _emit_leg_skipped, candidates, task, reason,
            type(exc).__name__,
            round((time.perf_counter() - started) * 1000),
        )
        return _empty_leg(), reason


def _keyword_lexical_timeout(task: _Task, exc: BaseException) -> bool:
    """A keyword leg whose lexical probe hit its own statement timeout.

    ``ChunkLexicalSearchTimeout`` is the adapter's bounded-FTS timeout, not a
    driver failure ``classify_read_failure`` knows, so without this a keyword
    leg's timeout would be filed as ``unavailable``.  Scoped to the keyword
    arm on purpose: the semantic legs' classification stays exactly as it was.
    """
    if task.arm != "keyword":
        return False
    from app.repositories.ports import ChunkLexicalSearchTimeout

    return isinstance(exc, ChunkLexicalSearchTimeout)


def _emit_leg_skipped(candidates, task: _Task, reason: str, error_type: str,
                      latency_ms: int, *, lane: str = "") -> None:
    """One leg's content-free skip receipt.

    ``reason`` is ADDED to the event rather than replacing anything, and only
    when there is one: without a run plan there are no reason codes at all and
    the event must stay the field-for-field one the ordinary notebook path has
    always emitted. Nothing here carries the query, the library's contents or
    an exception message -- only a class name.
    """
    event = {
        "kind": "chunk_federation_skipped",
        "notebook_id": task.notebook_id,
        "error_type": error_type,
        "latency_ms": latency_ms,
    }
    if reason:
        event["reason"] = reason
    if lane:
        event["lane"] = lane
    if task.arm:
        event["arm"] = task.arm
    _emit(candidates, event)


def _budgeted_retrieve_for(candidates, task: _Task, plan, deadline: float,
                           cancel, clock: _LegClock):
    """The producer call under this library's own read budget.

    ⛔ THE ORDER OF THESE THREE LINES IS THE CONTRACT.  The fan-out slot is
    acquired FIRST, the budget is opened SECOND, and the flag that says "this
    library was actually asked something" is set between them.  Inverting the
    first two -- which is what wrapping ``_retrieve_for`` whole would do --
    charges the wait on the run's ``fanout_limit`` semaphore to the library's
    read budget, and since that wait polls only the run's own cancel event, the
    leg then expires having issued no query at all and is reported as a
    ``timeout``: the user is told to narrow a scope that was never searched.

    The budget is entered INSIDE the task's own context -- ``read_budget``
    installs a ContextVar and each task owns a separate ``Context``, which is
    what keeps one library's expiry from being another's -- and ``cancel`` is
    the composite token, so this call giving up releases the leg too.

    ``min`` of the two bounds, never the notebook timeout alone: a leg that
    started just before the phase ended may not keep a connection for its full
    per-library budget after the call has stopped waiting for it.
    """
    with retrieval_fanout_slot():
        clock.deadline = min(
            deadline, time.monotonic() + float(plan.notebook_timeout_seconds),
        )
        clock.producer_started = True
        with read_budget(clock.deadline, cancel):
            return _producer_call(candidates, task)


def _retrieve_for(candidates, task: _Task):
    """One task's producer call, inside the run's fan-out slot.

    The slot is acquired HERE, around the one producer call, and nowhere
    above: ``reasoning_retrieval.search_chunks`` used to wrap its
    ``retrieve_chunk_candidates`` call in a slot, which after this module took
    over stopped bounding anything (the real leaves are these pool threads) and
    would self-deadlock at ``fanout_limit=1`` if both layers held one.  A
    worker parked here is still cancellable: ``RetrievalRunState.fanout_slot``
    polls ``cancel_event`` while it waits.

    The planned path does not call this function -- it takes the same two steps
    itself, in the same order, so that it can open the read budget BETWEEN them
    (``_budgeted_retrieve_for``).  Both paths make the producer call through
    the one ``_producer_call`` below, so the call shapes cannot drift apart.
    """
    with retrieval_fanout_slot():
        return _producer_call(candidates, task)


def _producer_call(candidates, task: _Task):
    """The producer call itself -- the ONE place either path issues it.

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

    A task that carries its own ``producer`` (a non-semantic arm borrowing
    this fan-out) is handed to it instead; every semantic leg has ``None``
    there, so neither call shape above changes.
    """
    if task.producer is not None:
        return task.producer(task)
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
    min_relevance: float, relative_relevance: float, plan=None, legs=None,
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

    **The two callbacks fire from HERE, on the calling thread, last.**  This is
    the only frame that holds all three things a receipt needs -- the
    participant order, each leg's verdict and each library's candidate count
    after the within-library fold -- and it is the parent thread, so the job
    stays the single writer of its own coverage lists and the worker threads
    only ever return values.  Receipts go first because ``on_library`` is also
    where the run re-checks authority per library and may fail the whole call;
    publishing evidence fingerprints for a run that is about to be refused
    would be work done for nobody.
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
    folded = {
        notebook_id: _fold_library_pool(pools[notebook_id])
        for notebook_id, _tier in participants
    }
    selected = _select_baseline_then_supplements(
        candidates,
        [folded[notebook_id] for notebook_id, _tier in participants],
        min_relevance=min_relevance,
        relative_relevance=relative_relevance,
    )
    collected = with_active_reserve(
        {hit.chunk_id: hit for hit in selected},
        _reserve_size(candidates.settings, _reserve_k(candidates.settings)),
    )
    per_query = _sub_query_groups(per_task, collected, sub_count)
    ids, matrix = _merge_selected_matrices(candidates, tasks, parts, collected)
    if plan is not None:
        legs = legs or _Legs()
        _report_receipts(plan, legs, tasks, folded)
        _report_evidence(candidates, plan, collected, legs.deadline)
    return FederatedChunkResult(
        collected, per_query, ids, matrix,
        tuple(notebook_id for notebook_id, _ in participants),
    )


def _report_receipts(plan, legs, tasks: list, folded: dict) -> None:
    """One ``LibraryOutcome`` per participant, in participant order.

    Aggregated within THIS call only.  A reasoning run federates once per
    retrieval round, so a library that answered round 1 and timed out in round
    2 produces two receipts; folding those into the run's coverage lists is the
    job's decision, not the federation's, and doing it here would require this
    module to remember state across calls it is not the owner of.

    The three shapes, and why the middle one cannot carry its reason:

    * every leg failed -> ``skipped`` with the most informative code among them
      (``_SKIP_SEVERITY``).  A library that issued several sub-queries and lost
      them all searched nothing, whatever the mixture of causes;
    * SOME legs failed -> ``ok`` and ``degraded``.  The library did contribute
      evidence, so the coverage list must not call it skipped, but the answer
      was assembled from less than the library had -- which is exactly what
      ``degraded`` means to the reader.  It carries NO reason code:
      ``LibraryOutcome`` refuses an answering library that holds a skip reason,
      and rightly so, because ``_SKIP_COPY`` would then render a "retrieval did
      not finish" sentence next to a library whose passages are in the answer;
    * none failed -> plain ``ok``.

    ``candidate_count`` is the library's pool as it entered the cross-library
    merge: after the within-library fold (so one passage found by three
    sub-queries counts once) and before ``peer_evidence`` (so it measures what
    the library OFFERED, not how much of it won a seat).
    """
    failures: dict = {}
    totals: dict = {}
    for task, reason in zip(tasks, legs.reasons):
        totals[task.notebook_id] = totals.get(task.notebook_id, 0) + 1
        if reason:
            failures.setdefault(task.notebook_id, []).append(reason)
    for notebook_id, _tier in legs.order:
        prepare_failure = legs.dropped.get(notebook_id)
        if prepare_failure:
            outcome = LibraryOutcome(status="skipped", reason=prepare_failure)
        else:
            failed = failures.get(notebook_id, ())
            total = totals.get(notebook_id, 0)
            if total and len(failed) == total:
                outcome = LibraryOutcome(
                    status="skipped", reason=_worst_reason(failed),
                )
            else:
                outcome = LibraryOutcome(
                    status="ok", degraded=bool(failed),
                    candidate_count=len(folded.get(notebook_id, ())),
                )
        plan.on_library(notebook_id, outcome)


def _worst_reason(reasons) -> str:
    """The most informative code among one library's failed legs."""
    for reason in _SKIP_SEVERITY:
        if reason in reasons:
            return reason
    return "unavailable"


def _text_sha(text) -> str:
    """The digest the passage snapshot compares against, on this side."""
    return hashlib.sha256((text or "").encode()).hexdigest()


def _report_evidence(candidates, plan, collected: dict, deadline: float) -> None:
    """The retrieval-time fingerprints of everything this call SELECTED.

    ONE read per call at most, over the PASSAGES of the finished selection --
    not one per library, and not over every candidate that was ever considered.
    The consumer (the job's citation re-check) only ever asks about evidence
    that reached the answer, so widening this to the candidate pool would
    multiply the read by the recall budget for rows nobody can cite.

    BY PASSAGE, and that is a correctness property rather than a batching
    convenience.  Element ids are reused DETERMINISTICALLY across a re-ingest
    (``source_ingestion`` mints ``el-<source>-<index>``), so "the row with this
    id" is not the same claim as "the text this run retrieved".  A source
    reparsed between the retrieval leg's read and this one leaves new text
    under every one of the old ids; fingerprinting by id alone would file that
    NEW text as the OLD passage's evidence, and the re-check -- which reads the
    same new text again -- would compare it against itself, find it identical,
    and publish an answer written from text nobody can still find as grounded.
    So the snapshot comes back keyed by chunk, carrying the passage's own text
    digest beside its elements' fingerprints out of ONE database snapshot
    (``GlobalAskSourceStorePort.passage_evidence_snapshot``), and a hit is
    attested only when that digest matches the text this call actually
    selected.  The residual race is narrower and harmless: if the source was
    reparsed in between and the new passage text is byte-identical, the answer
    rests on text that is still there, which is exactly what attestation
    claims; any other edit changes the digest and is caught.

    ACCUMULATING, which is what makes the per-run de-duplication correct rather
    than merely cheap.  The consumer merges each call's map into one table, and
    any snapshot taken during this run is a legitimate "before" for the
    re-check, so a passage an earlier round already attested must not be read
    again: a five-round reasoning run would otherwise re-read every selected
    passage five times, and the rounds share most of their selection.  The
    seen-set settles CHUNKS, not elements -- an element is only ever as
    attested as the passage it was read out of -- and a passage that failed its
    text check stays out of it, so the next round retries it.  The set is
    run-local, so two runs never borrow each other's snapshots, and without an
    ambient retrieval run it degrades to reading every time.

    Fail-soft, and the direction of the failure is deliberate.  An unreadable
    batch publishes ``None`` for each of those elements, which is what the
    re-check treats as "not attestable" -- so the citations resting on them are
    refused rather than accepted unverified.  (Publishing nothing would not do:
    absence is how an element that never travelled this channel looks, and
    those are held to the ceiling only.)  A passage whose text no longer
    matches, or that has disappeared outright, publishes ``None`` for every
    element it declared by the same rule and for the same reason: it travelled
    this channel and cannot be vouched for.  ``None`` WINS over a snapshot from
    another hit in the same call -- one element can sit in two selected
    passages, and an element whose surrounding text moved under it is not
    attestable just because some other passage still holds it.  (The rule that
    a real snapshot already published is not overwritten by a later ``None``
    belongs to the consumer's merge and is unchanged.)
    Cancellation and attestation failures are not failures of this read and
    re-raise.

    Bounded by its OWN per-library-sized budget, deliberately NOT by what is
    left of the phase.  The phase runs out exactly when some library was slow,
    and that is the case the per-library skip exists to survive: the libraries
    that did answer produced a selection, and refusing to attest it because a
    peer ate the phase would turn "one slow library is skipped" into "the whole
    answer is voided" (absence here means every citation is refused).  It is
    one bounded read on the calling thread, after the fan-out has released its
    executor seats, so it cannot extend the phase for anyone else.

    ⛔ Deliberately absent from the guard's ``_SEAT_FAILSOFT_SITES``: the try
    body is a single source-store read, so it cannot reach the participant seat
    and swallowing it cannot turn an identity failure into a silently narrower
    search.

    The GROUPING goes back too, just before the fingerprints and by the same
    rule (``FederatedRunPlan.on_evidence_groups``): which elements belong to
    one selected passage is structural, so it is published on every call
    regardless of whether the fingerprint read succeeded, and it is NOT subject
    to the seen-set -- that set exists to stop re-READING settled passages, and
    a group costs no read at all.
    """
    seen = memoized_retrieval_value(
        ("federated_chunk_evidence_seen",), lambda: set(),
    )
    # A passage declaring no elements has nothing to attest, so it neither
    # costs a read nor enters the seen-set.
    pending = [
        chunk_id for chunk_id, hit in collected.items()
        if chunk_id not in seen and hit.element_ids
    ]
    fingerprints: dict = {}
    if pending:
        element_ids = list(dict.fromkeys(
            element_id for chunk_id in pending
            for element_id in collected[chunk_id].element_ids
        ))
        try:
            with read_budget(
                time.monotonic() + float(plan.notebook_timeout_seconds),
                plan.cancel,
            ):
                snapshot = dict(
                    candidates.sources.passage_evidence_snapshot(pending)
                )
        except (AskCancelled, RetrievalControlError):
            raise
        except Exception as exc:  # noqa: BLE001 - see docstring
            _emit(candidates, {
                "kind": "chunk_federation_evidence_unavailable",
                "reason": "read_failed",
                "error_type": type(exc).__name__,
                "elements": len(element_ids),
            })
            # STATED as unreadable, not left out: absence from the accumulated
            # table means "never travelled this channel" (overviews, graph
            # objects), which the re-check holds to the ceiling only. These
            # elements did travel it, so they must be refusable by name.
            fingerprints = dict.fromkeys(element_ids)
        else:
            fingerprints = _attest_passages(
                candidates, collected, pending, element_ids, snapshot, seen,
            )
    groups = getattr(plan, "on_evidence_groups", None)
    if groups is not None:
        groups(_evidence_groups(collected))
    plan.on_evidence(fingerprints)


def _attest_passages(candidates, collected: dict, pending: list,
                     element_ids: list, snapshot: dict, seen: set) -> dict:
    """Turn one passage snapshot into the per-element table the consumer folds.

    Every REQUESTED element gets a stated value, because absence means "never
    travelled this channel" and is held to the source ceiling alone -- so
    leaving one out would wave through exactly the element this read failed to
    vouch for.  Three ways to end up ``None``: the passage is gone, its text
    moved, or the passage still stands but this element's row does not (a
    re-ingest that produced fewer elements).  The first two refuse the whole
    passage; only the third is per element.

    Refusals are applied LAST so they cannot be undone by an attestation that a
    different hit contributed for the same element -- see ``_report_evidence``
    on why ``None`` wins within one call.
    """
    fingerprints: dict = dict.fromkeys(element_ids)
    refused: set = set()
    stale = 0
    for chunk_id in pending:
        hit = collected[chunk_id]
        passage = snapshot.get(chunk_id)
        if passage is None or passage["text_sha"] != _text_sha(hit.text):
            stale += 1
            refused.update(hit.element_ids)
            continue
        # Attested out of the same statement that proved the text -- so this
        # passage is settled for the rest of the run.
        seen.add(chunk_id)
        for element_id in hit.element_ids:
            if fingerprints.get(element_id) is None:
                fingerprints[element_id] = passage["elements"].get(element_id)
    for element_id in refused:
        fingerprints[element_id] = None
    if stale:
        _emit(candidates, {
            "kind": "chunk_federation_evidence_unavailable",
            "reason": "passage_changed",
            "passages": stale,
            "elements": len(refused),
        })
    return fingerprints


def _evidence_groups(collected: dict) -> list:
    """The multi-element passages among this call's selection, de-duplicated.

    A one-element hit is left out entirely rather than published as a group of
    one: the citation minted from it already names that element, so a group
    would only restate what the fingerprint table says and make the consumer's
    sibling map pay for a relationship with no second member.
    """
    return list(dict.fromkeys(
        tuple(hit.element_ids) for hit in collected.values()
        if hit.element_ids and len(hit.element_ids) >= 2
    ))


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
