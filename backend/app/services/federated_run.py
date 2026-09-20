"""The two seats a global run installs besides its participant set and its
per-notebook source ceilings: the DETACHED conversation turn and the FEDERATED
RUN PLAN.

Why this module holds only data and context variables
-----------------------------------------------------
Its consumers are the two heaviest modules in the service layer --
``chunk_federation`` reads the run plan to borrow the shared executor, the fair
window, the phase budget and the cancel token, and ``ask_service`` reads the
detached turn to answer without touching ``ask_state``.  Neither may acquire an
import edge to the participant-override module just to reach a seat, and
``ask_service`` in particular is barred from that module by a zero-slack guard.

So the seats live here, in a leaf that imports NOTHING from ``app``, while the
manager that installs them -- together with the override and the source
ceilings, as one all-or-nothing act -- lives in ``app/services/global_run.py``.
Reading a seat therefore costs one import of this leaf and no more.

⛔ The plan placed this module under ``app/application/``.  It cannot live
there: ``scripts/check_architecture_boundaries.py`` restricts ``app.application``
to ``ALLOWED_APPLICATION_PREFIXES`` (``app.core.ask_retrieval_policy``, two
named ``app.domain`` modules, ``app.models.ask``), and the installing manager
must import ``app.services.retrieval_participants`` and
``app.services.source_scope``.  Splitting the seats away from the manager into
``app.application`` would leave the manager in the service layer anyway and buy
nothing, so both halves stay here and the split is by DEPENDENCY WEIGHT, which
is the property the consumers actually need.

Context variables rather than parameters, for the same reason
``source_scope`` and ``retrieval_run`` are: the seats are installed where the
job is authorized and read many frames below, across a fan-out whose worker
threads receive them through ``copy_context()``.  Threading them through every
signature in between would put "this is a global run" into the type of every
function on the path.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping


# The reason codes a library's receipt may carry, and the ONLY ones.  They are
# the vocabulary ``global_ask._SKIP_COPY`` translates into the Chinese the user
# reads, and ``read_budget.classify_read_failure`` is what produces most of
# them, so a fifth code invented at a call site would travel all the way to a
# receipt with no copy for it and render as an empty skip note.
LIBRARY_SKIP_REASONS = frozenset({
    # The phase budget ran out while this library was still queued: nothing was
    # asked of the database, which is why it is not "timeout".
    "queue_deadline",
    "timeout",
    "saturated",
    "unavailable",
})
_LIBRARY_STATUSES = frozenset({"ok", "skipped"})


@dataclass(frozen=True)
class LibraryOutcome:
    """One participant library's receipt, produced on the CALLING thread.

    Field-for-field the durable half of ``global_ask._NotebookOutcome``: that
    class also carries the candidate pool and the evidence map, which are the
    worker's RESULT rather than its receipt and now travel back through
    ``FederatedRunPlan.on_evidence`` and the federation's own merge.  What is
    left here is exactly what the job thread writes into the persisted coverage
    lists.

    The producer is the federation's merge step, which runs on the thread that
    asked for retrieval, after the fan-out has been folded: the worker threads
    only ever RETURN values, so nothing here is assembled under a race and the
    receipts follow the participant order rather than thread scheduling.

    Frozen: a receipt is inserted into a list a poller reads, so it must not be
    editable after the fact by whichever side still holds a reference.

    ``status`` and ``reason`` are not redundant.  "Skipped" is the fact the
    coverage list is built from; the reason code is what telemetry carries and
    what picks the user-facing sentence.  The two are validated against each
    other because a skip with no reason renders as an empty note, and an "ok"
    carrying a reason would put a failure code on a library that answered.
    """

    status: str
    reason: str = ""
    degraded: bool = False
    candidate_count: int = 0

    def __post_init__(self) -> None:
        if self.status not in _LIBRARY_STATUSES:
            raise ValueError(
                f"library outcome status must be one of "
                f"{sorted(_LIBRARY_STATUSES)}"
            )
        if self.reason and self.reason not in LIBRARY_SKIP_REASONS:
            raise ValueError(
                f"library outcome reason must be one of "
                f"{sorted(LIBRARY_SKIP_REASONS)} or empty"
            )
        if (self.status == "skipped") != bool(self.reason):
            raise ValueError(
                "a skipped library needs a reason code and an answering one "
                "must not carry it"
            )
        if self.candidate_count < 0:
            raise ValueError("library outcome candidate_count must not be negative")

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"


@dataclass(frozen=True)
class DetachedAskTurn:
    """The conversation turn a global run answers for, WITHOUT an ask_state row.

    A global job owns its own history model and its own persistence; the
    per-notebook ``answers`` table has no row for a question asked of eight
    libraries at once, and writing one would attribute the turn to whichever
    library happened to be the naming anchor.  So the turn is handed down
    instead of looked up, and the persistence step at the other end of the run
    returns without writing.

    ``history`` / ``user_history`` default to empty rather than to None: every
    consumer concatenates them into a prompt block, and "no history" and "an
    empty history" must not be two shapes on that path.
    """

    conversation_id: str
    history: str = ""
    user_history: str = ""


@dataclass(frozen=True)
class FederatedRunPlan:
    """The process-level retrieval budget a global run lends to the federation.

    Everything here exists because the federation's own defaults are wrong for
    a run that fans out over a whole participant set:

    * ``executor`` is the ONE pool per process, and therefore the only real
      upper bound on retrieval-held database connections.  A federation that
      builds its own ``ThreadPoolExecutor`` per run multiplies that bound by the
      job capacity.  ⛔ It is borrowed, never owned: the consumer must not enter
      it as a context manager, because leaving that block shuts down a pool the
      next job still needs.
    * ``window`` is re-read rather than captured, so the fair share shrinks and
      grows as other fan-outs come and go.
    * ``call_scope`` is how ``window`` knows how many there are. The consumer
      enters it around ONE whole fan-out -- every leg of one call inside one
      scope, released in ``finally`` -- and the owner counts the open scopes.
      Without it the only thing the owner can count is JOBS, and a reasoning
      job is not one fan-out: it federates once per sub-query, from several of
      the engine's own threads at once, so a per-job share hands one job the
      whole pool while its own remaining legs queue up behind it and expire.
      ``None`` (the default) means "nobody is counting" and the consumer must
      treat it as a no-op, which is what keeps the ordinary notebook path --
      which has no plan at all -- byte-identical.
    * ``phase_timeout_seconds`` is the budget of ONE federated call -- one
      fan-out over the participant set -- and the consumer turns it into an
      absolute deadline when that call begins.  It is deliberately NOT an
      absolute ``time.monotonic()`` value fixed when the run starts: a
      reasoning run calls the federation once per retrieval round, with model
      calls in between, so a run-absolute deadline would declare every round
      after the first already expired and the whole participant set skipped
      with ``queue_deadline``.  What the value still may not be is a budget
      restarted per LEG: the consumer derives one deadline per call and every
      leg of that call shares it, which is what keeps a fan-out from outliving
      the phase by however many legs it has.
    * ``cancel`` carries every cancellation source as one token, which is how
      one library's hard failure reaches the libraries already executing.
    * ``on_library`` / ``on_evidence`` are the single return seam.  Receipts and
      the retrieval-time evidence fingerprints both travel it, so there is one
      place where worker results become job state and one thread doing the
      writing.

    ``on_evidence`` is ACCUMULATING, and its three rules are a contract, not an
    implementation detail of either side:

    1. **The consumer merges.**  A run federates once per retrieval round, so
       this callback fires once per round with that round's selection; the
       receiver folds each map into one table (``update``) rather than
       replacing it.
    2. **The earliest snapshot in the run is a legitimate "before".**  The
       re-check asks whether the evidence changed BETWEEN retrieval and the
       answer, so whichever round first fingerprinted an element answers that
       question for the whole run -- which is what lets the federation skip
       re-reading elements it has already published.
    3. **Three states, and "unreadable" is stated, never implied.**  A value is
       either a ``(source_id, fingerprint)`` snapshot, or ``None`` -- "this
       element came through the federated chunk channel and its fingerprint
       could not be read".  ``None`` is refusal: a citation resting on it is
       not attestable and must be refused rather than accepted unverified, which
       is what makes a failed read fail CLOSED element by element.  A real
       snapshot is never overwritten by ``None`` and a later successful read
       replaces one.  ABSENCE means something else entirely: the element never
       travelled this channel at all.  Document overviews, collection
       enumerations and graph objects cite real ``source_elements`` rows without
       a single federated call, so treating absence as refusal would void every
       such answer; the consumer holds those citations to the frozen source
       ceiling and to the element still existing under the same source.

    Callables rather than objects: this is a leaf module, and typing these
    fields would drag the federation's and the job's types into it and create
    the import edge the module exists to avoid.
    """

    phase_timeout_seconds: float
    notebook_timeout_seconds: float
    executor: Any
    window: Callable[[], int]
    cancel: Any
    on_library: Callable[[str, LibraryOutcome], None]
    on_evidence: Callable[[Mapping[str, "tuple[str, str] | None"]], None]
    # ``Callable[[], ContextManager[None]] | None``, typed loosely for the same
    # reason the callables above are: this is a leaf module. Last and
    # defaulted, so every existing construction of this plan keeps working.
    call_scope: Any = None


_DETACHED_TURN: "ContextVar[DetachedAskTurn | None]" = ContextVar(
    "detached_ask_turn", default=None,
)
_RUN_PLAN: "ContextVar[FederatedRunPlan | None]" = ContextVar(
    "federated_run_plan", default=None,
)


@contextmanager
def detached_ask_turn(turn: DetachedAskTurn) -> Iterator[None]:
    """Install the detached turn for the duration of the block.

    Nesting is refused, including a nested turn equal to the installed one, for
    the reason ``participant_override`` spells out: two writers each believing
    they own the run means whichever exits first restores a seat the other is
    still using.  There is exactly one writer by contract -- the manager in
    ``global_run`` -- so nesting is a wiring bug in every form.

    ⛔ ENTER AND EXIT IN THE SAME CONTEXT.  ``ContextVar.reset(token)`` raises
    when the token was created elsewhere, so this must not be entered on one
    thread and left on another.  Wrap the whole run and let ``copy_context()``
    carry the value into the workers.
    """
    if _DETACHED_TURN.get() is not None:
        raise ValueError("a detached ask turn is already installed")
    token = _DETACHED_TURN.set(turn)
    try:
        yield
    finally:
        _DETACHED_TURN.reset(token)


@contextmanager
def federated_run_plan(plan: FederatedRunPlan) -> Iterator[None]:
    """Install the run plan for the duration of the block.

    Same single-writer and same-context rules as ``detached_ask_turn``; the
    reset lives in ``finally`` so a cancelled run cannot leave a plan -- and
    with it a reference to a shared executor -- installed on a thread that goes
    back into a pool.
    """
    if _RUN_PLAN.get() is not None:
        raise ValueError("a federated run plan is already installed")
    token = _RUN_PLAN.set(plan)
    try:
        yield
    finally:
        _RUN_PLAN.reset(token)


def current_detached_ask_turn() -> DetachedAskTurn | None:
    """This run's detached turn, or None for an ordinary notebook-scoped ask."""
    return _DETACHED_TURN.get()


def current_federated_run_plan() -> FederatedRunPlan | None:
    """This run's retrieval plan, or None when the federation owns its own."""
    return _RUN_PLAN.get()
