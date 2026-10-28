"""Run-local reuse of one chunk recall by the raw-element arm.

The raw-element search (``CandidateRetrievalService._retrieve_elements``) has
no ANN of its own: on its bounded route it first runs the FULL chunk recall
(``_retrieve_chunks``: ANN + full text + hydration, window
``max(CHUNK_RECALL, 4 x limit)``) and then hydrates elements by primary key
from the chunks it got.  In one retrieval run the semantic legs
(reasoning's seed, ``search_chunks``, a direction's passages), the empty-seed
top-up and earlier element searches already made that very recall for the
same library and the same text -- in a global run, once per library, up to
``REASONING_MAX_ELEMENT_SEARCHES`` x 8 libraries times.  This module keeps
what the element arm needs of each recall the run made, and serves it back.

What is kept, and why only that:

* **A light projection, never the recall itself.**  ``_retrieve_chunks``
  returns ``(scored, ids, matrix)``; the matrix can be a fresh copy of the
  library's whole vector matrix (the bounded brute-force lane masks the shared
  cache per call) and ``scored`` carries every chunk's full text.  The element
  arm discards the matrix and reads, of each chunk, only its id, source,
  library, element ids, scores and whether only the question index found it
  (``_retrieve_elements_from_chunks``).  So that is all a kept recall holds
  (``_light_row``), and a hit rebuilds text-less chunks from it.  The semantic
  legs need the matrix (MMR, the cross-library matrix merge), so they are
  writers only: serving them would mean keeping matrices.
* **Bounded, least recently used out first**: at most
  ``chunk_federation_max_participants x chunk_recall`` rows (one federated
  fan-out's full windows) and ``CHUNK_RECALL_MEMO_MAX_ENTRIES`` entries per
  run; a recall larger than the row bound is not kept.
* **Discarded**: a run kind whose element searches cannot read a recall back
  (``ask_chunk``, report planning) keeps nothing, and neither does a recall
  no element search could match (``_recall_readable_by_element_arm``); a
  recall during which any lane failed open (``chunk_lane
  .note_recall_failure``, called directly, through ``_note_model_error`` or
  through the hooks below; ``test_chunk_recall_failure_guard`` holds every
  handler on the tree to it) is not kept, so a later element search retries
  exactly as before instead of inheriting a degraded pool; a recall that
  failed verify-on-read is not kept, and the flip drops every unbound recall
  of that library (``forget_unbound_chunk_recalls``).

Only ``CandidateRetrievalService._element_chunk_recall`` reads the memo; every
``_retrieve_chunks`` writes it.  A hit is a snapshot of an earlier moment of
the run: a write, a delete or a scale-index generation swap in between can
make a fresh recall differ, never widen the frozen ceiling.

The key is everything the recall's result depends on that can differ inside
one run (``recall_slot``): the service, library, text, window, the
EFFECTIVE source ceiling (digested; ``None`` when the run pushed the list
down), the lexical lane's bounded-scan answer, the two per-task lane flags
and the library's FTS circuit.  A ceiling-drift flip (``verify_unbound_read``
turning the library's verdict to "binds") changes the effective ceiling from
``None`` to the list, so a key taken after the flip can never meet a recall
read before it; a hit on an unbound recall re-reads the ceiling before it is
served, closing the window between taking the key and reading the memo.

Serving never waits, never counts a model request and never changes what a
miss does: two legs missing the same key both recall, as before.  Nothing is
kept or served outside a retrieval run.
"""
from __future__ import annotations

import contextvars
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from app.domain.retrieval import RetrievalSupport, RetrievedChunk
from app.services.cancellation import raise_if_cancelled
from app.services.chunk_lane import (
    _CHUNK_ARM_DRIFTED,
    _CHUNK_PEEK_ONLY,
    _CHUNK_PEER_LEG,
    _CHUNK_RECALL_FAILURES,
    _lexical_gate_drift_probe,
    note_recall_failure,
)
from app.services.retrieval import is_generated_question_only_chunk
from app.services.retrieval_run import (
    ChunkRecallEntry,
    RetrievalRunState,
    current_retrieval_run,
)

# What a rebuilt chunk carries when only the question index found it: the one
# shape ``is_generated_question_only_chunk`` reads as supplemental.
_QUESTION_INDEX_ONLY = (RetrievalSupport("generated_question", "chunk"),)


@dataclass(frozen=True)
class RecallSlot:
    """One memoised recall's key and what its computation needs to know."""

    run: RetrievalRunState
    key: tuple
    notebook_id: str
    unbound: bool
    # The lexical-gate drift verdict this key was taken under, handed to the
    # recall so it is not probed a second time; ``None`` = the recall
    # resolves it itself, exactly as without a memo.
    drifted: Optional[bool]
    max_rows: int


def recall_slot(candidates, notebook_id: str, query: str, recall: int,
                ceiling, *, producer_explicit: bool,
                drifted: Optional[bool],
                ceiling_is_frozen: bool = False) -> Optional[RecallSlot]:
    """The memo key for one ``_retrieve_chunks`` call, or ``None`` (no run,
    a run kind that keeps nothing, or an empty ceiling: nothing to reuse).

    ``ceiling`` is the call's EFFECTIVE ceiling (``_chunk_source_ceiling``).
    The lexical lane reads ``producer_explicit`` and the drift verdict only
    through ``_lexical_gate_source_scoped``, so the key holds that answer; the
    verdict is resolved (keyword, the arm's context variable, else the probe)
    only when the answer depends on it, and handed down so the recall does
    not probe again.

    ``ceiling_is_frozen``: the caller handed no list, so a non-``None``
    ceiling is the run's frozen library ceiling (or a report's memoised
    authorised sources) -- the same list on every call of this run -- and its
    digest is computed once per run, scope and library."""
    run = current_retrieval_run()
    if run is None or not run.keeps_chunk_recalls():
        return None
    if ceiling is not None and not ceiling:
        return None
    resolved = drifted if drifted is not None else _CHUNK_ARM_DRIFTED.get()
    gate = candidates._lexical_gate_source_scoped
    bounded = gate(ceiling, notebook_id, explicit=producer_explicit, drifted=False)
    if bounded != gate(ceiling, notebook_id, explicit=producer_explicit,
                       drifted=True):
        if resolved is None:
            # Probed here, before the recall's own ``computing`` block opens,
            # so its failure note is captured here: a key resolved from a
            # failed probe (answered "no drift") is not one to keep or serve
            # under, and the recall then resolves the verdict itself.
            with computing() as probe_failures:
                resolved = _lexical_gate_drift_probe(candidates, notebook_id)
            if probe_failures:
                return None
        bounded = gate(ceiling, notebook_id, explicit=producer_explicit,
                       drifted=resolved)
    settings = candidates.settings
    window = int(recall or settings.chunk_recall)
    key = (
        # The service itself, not its ``id()``: the run holds it for its own
        # short life, so no later object can take over its address.
        candidates, notebook_id, query, window,
        _frozen_digest(run, notebook_id, ceiling) if ceiling_is_frozen
        else _ceiling_digest(ceiling),
        bool(bounded), bool(_CHUNK_PEEK_ONLY.get()), bool(_CHUNK_PEER_LEG.get()),
        run.chunk_fts_open(notebook_id),
    )
    return RecallSlot(
        run=run, key=key, notebook_id=notebook_id, unbound=ceiling is None,
        drifted=resolved,
        max_rows=max(1, int(getattr(
            settings, "chunk_federation_max_participants", 8)))
        * max(1, int(settings.chunk_recall)),
    )


def _frozen_digest(run: RetrievalRunState, notebook_id: str, ceiling):
    """``_ceiling_digest`` of a frozen ceiling, once per run, scope and
    library (``recall_slot``'s ``ceiling_is_frozen``)."""
    if ceiling is None:
        return None
    from app.services.source_scope import current_source_scope

    scope = current_source_scope()
    owner, digest = run.memoized_value(
        ("chunk_recall_ceiling_digest", id(scope), notebook_id, len(ceiling)),
        lambda: (scope, _ceiling_digest(ceiling)),
    )
    # The scope object is kept beside its digest, so an address a freed scope
    # left behind can never hand its digest to another one.
    return digest if owner is scope else _ceiling_digest(ceiling)


def _ceiling_digest(ceiling) -> Optional[str]:
    """A fixed-size stand-in for the ceiling list (up to tens of thousands of
    ids): the memo must not keep the list itself.  Order-sensitive, which can
    only cost a hit."""
    if ceiling is None:
        return None
    digest = hashlib.blake2b(digest_size=16)
    digest.update("\n".join(str(value) for value in ceiling).encode("utf-8"))
    return f"{len(ceiling)}:{digest.hexdigest()}"


def serve(slot: RecallSlot, still_unbound) -> Optional[list]:
    """The kept recall as text-less chunks, or ``None`` (a miss).

    ``still_unbound()`` re-reads whether the library's ceiling is still pushed
    down; an unbound recall is served only while it is (the verdict may have
    flipped between taking the key and reading the memo), and dropped -- only
    this entry, never a fresher one kept since -- when it is not."""
    entry = slot.run.chunk_recall(slot.key)
    if entry is None:
        return None
    if entry.unbound and not still_unbound():
        slot.run.forget_chunk_recall(slot.key, entry)
        return None
    raise_if_cancelled(slot.run.cancel_event)
    slot.run.note_chunk_recall_hit()
    return [_rebuilt(row) for row in entry.rows]


def keep(slot: RecallSlot, scored, still_unbound) -> None:
    """Keep the light projection of a verified, failure-free recall.

    An unbound recall is kept only while the ceiling is still pushed down: a
    flip during the read made the recall itself bind (``_retrieve_chunks_once``
    takes the ceiling afresh), and a key that names no ceiling must not hold a
    bound read."""
    if slot.unbound and not still_unbound():
        return
    try:
        rows = tuple(_light_row(chunk) for chunk in scored)
    except (AttributeError, TypeError):
        # Not the producer's chunk shape: nothing this module can serve back.
        return
    slot.run.keep_chunk_recall(
        slot.key,
        ChunkRecallEntry(rows=rows, notebook_id=slot.notebook_id,
                         unbound=slot.unbound),
        max_rows=slot.max_rows,
    )


def forget_unbound(slot: RecallSlot) -> None:
    """The recall's verify-on-read flipped the library's verdict."""
    slot.run.forget_unbound_chunk_recalls(slot.notebook_id)


def _light_row(chunk) -> tuple:
    return (
        str(chunk.chunk_id), str(chunk.source_id), str(chunk.notebook_id or ""),
        tuple(str(value or "") for value in (chunk.element_ids or ())),
        float(chunk.score or 0.0), float(chunk.relevance or 0.0),
        is_generated_question_only_chunk(chunk),
    )


def _rebuilt(row: tuple) -> RetrievedChunk:
    chunk_id, source_id, notebook_id, element_ids, score, relevance, only_q = row
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id, source_title="",
        section_path="", text="", element_ids=list(element_ids), score=score,
        relevance=relevance, notebook_id=notebook_id,
        retrieval_supports=_QUESTION_INDEX_ONLY if only_q else (),
    )


@contextmanager
def computing() -> Iterator[list]:
    """The block computes one memoised recall: collect its fail-open notes."""
    failures: list = []
    token = _CHUNK_RECALL_FAILURES.set(failures)
    try:
        yield failures
    finally:
        _CHUNK_RECALL_FAILURES.reset(token)


def noting(stage: str, callback: Callable[[], Any]) -> Callable[[], Any]:
    """``callback`` that first notes a failure of the recall being computed:
    for the failure hooks a lane hands to a component that swallows its own
    exceptions (the generated-question call's ``failure_event``)."""
    def hook():
        note_recall_failure(stage)
        return callback()
    return hook


# The slot ``CandidateRetrievalService._element_chunk_recall`` already took for
# the ``_retrieve_chunks`` call it is about to make (``handing``): ``(call,
# slot)``, where ``call`` names that call by identity.  A context variable
# because ``_retrieve_chunks`` has whole-method test doubles a new keyword
# would break; a double never reads it.
_HANDED_SLOT: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "_chunk_recall_handed_slot", default=None
)


@contextmanager
def handing(slot: Optional[RecallSlot], call: tuple) -> Iterator[None]:
    """Hand ``slot`` (``None`` included: "no memo for this call") to the
    ``_retrieve_chunks`` call named by ``call`` inside the block."""
    token = _HANDED_SLOT.set((call, slot))
    try:
        yield
    finally:
        _HANDED_SLOT.reset(token)


def handed(call: tuple) -> tuple[bool, Optional[RecallSlot]]:
    """``(True, slot)`` when the enclosing ``handing`` was for exactly this
    call -- the same service, library, text, window, the same list OBJECT and
    attestation -- consumed so no nested call can take it; else ``(False,
    None)`` and the caller takes its own slot."""
    handed_call = _HANDED_SLOT.get()
    if handed_call is None:
        return False, None
    expected, slot = handed_call
    if (len(expected) != len(call)
            or any(left is not right and left != right
                   for left, right in zip(expected, call))
            or expected[4] is not call[4]):
        return False, None
    _HANDED_SLOT.set(None)
    return True, slot


def noting_contribution(reason: str) -> None:
    """The contribution host's ``on_degraded``: a contributor did not get to
    answer this call (the host decides which outcomes those are,
    ``extensions.retrieval.DEGRADED_CONTRIBUTION_OUTCOMES``), so the recall
    being computed is degraded."""
    note_recall_failure(f"retrieval_contribution:{reason}")


__all__ = [
    "RecallSlot",
    "computing",
    "handed",
    "handing",
    "forget_unbound",
    "keep",
    "note_recall_failure",
    "noting",
    "noting_contribution",
    "recall_slot",
    "serve",
]
