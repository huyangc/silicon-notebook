"""THE one entry point that puts a process into global-ask mode.

A global run is four facts, and they are only ever true together:

1. ``participant_override`` -- the libraries this run searches, plus the
   identity that earned them.  Read by the retrieval layer through
   ``federated_ask_active()``.
2. ``source_scope_context(..., notebook_source_ceilings=..., subjectless=True)``
   -- each participant's frozen visible-source list, plus the explicit "this
   run has no subject library" bit.  Read by the citation/prompt side through
   ``subjectless_run_active()`` and by every filtering point through
   ``peer_scope_ceiling_active()``.
3. ``detached_ask_turn`` -- the conversation turn, handed down instead of read
   from ``ask_state``, so nothing is written to a per-notebook answers table.
4. ``federated_run_plan`` -- the shared executor, the fair window, the phase
   deadline, the cancel token and the one return seam for receipts and
   evidence fingerprints.

WHY ONE MANAGER, AND WHY IT ASSERTS SO LOUDLY
---------------------------------------------
The retrieval layer and the ask layer deliberately read DIFFERENT predicates:
``ask_service`` is the host of registered fail-soft handlers and sits next to
authorization, so it must never learn -- let alone be able to replace -- which
libraries a run may search, and it is kept off the participant-override
module's reader whitelist by a zero-slack equality guard.  Two predicates can
disagree.  Installing them from one place, in one act, is what makes the
disagreement unconstructible.

Half an install is not a degraded run, it is a WRONG one, and silently:

* override without ceilings -- the retrieval legs enter peer mode while the ask
  side stays in single-library mode.  The nominal active's private Memory folds
  into a cross-library answer, its citations alone get their origin blanked
  while every peer's is kept, and the workbook lane keeps analysing reference
  libraries mounted under the anchor but never selected for this question.
* ceilings without the override -- the ask side goes peer while retrieval still
  fans out over the anchor's mount table, which is a different set of libraries
  than the user selected.

The full-map assertion on the ceilings closes a second, quieter version of the
same split.  ``ask_service._peer_ceiling_participants`` treats "this notebook
has no ceiling entry" as fail-CLOSED (drop the library), while
``ActiveSourceScope.allows`` treats the same fact as fail-OPEN (admit every
source).  Those two answers can only be made to agree by guaranteeing the
ceilings are a TOTAL map over the participant set -- so a library whose visible
source list is empty must appear as ``frozenset()`` and must never be skipped
because the set was falsy.  ``filter_retrieval_items`` needs the same
guarantee for the nominal active specifically: without its own entry it falls
through to "no ceiling binds here" and stops defending the one library the run
is keyed by.

⛔ The plan placed this module under ``app/application/``.  It cannot live
there -- see the note in ``app/services/federated_run.py``, which also explains
why the seats sit in that leaf rather than here.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import Any, Iterator, Mapping

from app.domain.retrieval_control import ParticipantOverrideError
from app.services.federated_run import (
    DetachedAskTurn,
    FederatedRunPlan,
    detached_ask_turn,
    federated_run_plan,
)
from app.services.retrieval_participants import participant_override
from app.services.source_scope import source_scope_context


def _validated_ceilings(
    override: Any, ceilings: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    """The ceilings as a total map over the participant set, or raise.

    Equality of the KEY SETS, not containment in either direction.  A missing
    key is the fail-closed/fail-open split described in the module docstring; an
    extra key is a participant set that has already drifted from the one the
    authorization check ran over, and admitting it would freeze a source list
    for a library this run may not read at all.
    """
    participants = set(override.notebook_ids)
    given = set(ceilings)
    if given != participants:
        # Content-free: ids are the fact every wiring bug is diagnosed from and
        # the override already publishes them, but nothing else about the
        # libraries or the actor goes into a message that lands in a traceback.
        raise ParticipantOverrideError(
            "source ceilings must cover exactly the participant set "
            f"(missing {sorted(participants - given)}, "
            f"unexpected {sorted(given - participants)})"
        )
    return {
        notebook_id: frozenset(str(value) for value in ceilings[notebook_id])
        for notebook_id in participants
    }


@contextmanager
def global_ask_run(
    override: Any,
    ceilings: Mapping[str, Any],
    turn: DetachedAskTurn,
    plan: FederatedRunPlan,
    *,
    nominal_active: str,
) -> Iterator[None]:
    """Install all four facts of a global run, or none of them.

    ``override`` arrives already built and already attested: constructing a
    ``ParticipantOverride`` is an authorization act that belongs with the
    ``can_read_many`` check, and doing it here would put a second, unreviewed
    way to mint one inside the installer.

    ``nominal_active`` is stated by the caller rather than derived from
    ``override.notebook_ids[0]``, and then checked against it.  The caller
    computes it from the job's resolved libraries and keys the retrieval run,
    the scope and the trace by it; deriving it here would make the two silently
    equal by construction and turn a drift between the job's list and the
    override's into a run answering under the wrong anchor -- the one library
    whose ceiling ``filter_retrieval_items`` would then fail to find.

    ⛔ NO HALF STATE.  Every check runs before the first seat is installed, and
    the seats go on an ``ExitStack``, so a failure part-way through unwinds the
    ones already installed.  On the way out -- normal return, exception or
    cancellation alike -- all four context variables are reset.

    The ``ExitStack`` is what carries that guarantee, so no seat may be entered
    outside it and no reference to an entered one may be pinned anywhere.  A
    seat entered and then simply abandoned happens to unwind on its own (the
    abandoned context manager is finalized, which runs its ``finally``), but a
    pinned one stays installed for the life of the reference -- that is the one
    shape that really does leave half a run behind, and
    ``test_half_install_raises`` is the assertion that catches it.

    ⛔ ENTER AND EXIT ON THE SAME THREAD, IN THE SAME CONTEXT.  Every seat here
    resets a ``ContextVar`` by token.  Wrap the whole run; the fan-out's worker
    threads receive the installed values through ``copy_context()``.
    """
    if nominal_active != override.nominal_active_id:
        raise ParticipantOverrideError(
            "nominal active notebook is not the override's first participant"
        )
    frozen = _validated_ceilings(override, ceilings)
    with ExitStack() as stack:
        stack.enter_context(participant_override(override))
        stack.enter_context(source_scope_context(
            nominal_active,
            # The local and library dimensions are UNSUBMITTED, not empty: a run
            # with no subject library has no checkbox list of its own, and
            # fabricating one would freeze it into the scope payloads and make
            # ``covers_notebook`` start excluding participants.
            None,
            None,
            notebook_source_ceilings=frozen,
            subjectless=True,
        ))
        stack.enter_context(detached_ask_turn(turn))
        stack.enter_context(federated_run_plan(plan))
        yield
