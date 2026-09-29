"""Retrieval-time evidence attestation for citation producers OUTSIDE the
federated chunk channel.

A global Ask re-checks every citation once the answer exists: is the text the
citation rested on still the text in the library? The "before" half of that
comparison is the run's accumulated evidence table, which ONE seam fills --
``current_federated_run_plan().on_evidence`` (``FederatedRunPlan``'s rules 1-3).
The federated chunk channel publishes into it from ``chunk_federation``; every
other producer that mints a ``Citation`` / ``AnswerAnchor`` naming a
``source_elements`` row publishes through the two helpers here, and through
nothing else. There is no second table and no second callback.

Which helper a producer calls depends on what it has in hand:

* ``attest_read(producer, texts)`` -- the producer READ the element text itself
  (an overview excerpt, an enumerated row, a table cell). It hashes that text
  in-process and publishes it: zero extra reads, and the snapshot is by
  construction the text the run actually saw, which is exactly what rule 2 of
  the plan's contract demands of a published snapshot.
* ``attest_pointers(producer, element_ids)`` -- the producer cites elements it
  never read (a KG object's evidence pointer, a relation's occurrence). ONE
  batched by-id read through ``GlobalAskEvidenceReaderPort``, bounded by the
  run's per-library read budget and the run's cancel token, memoised for the
  rest of the run. It answers ``live`` / ``dead`` / ``unknown`` per id so the
  producer can drop a pointer that was already dangling before the question
  (J2) instead of minting a card that opens on nothing.

Both return immediately when no plan is installed: an ordinary notebook ask has
no terminal re-check (J8), so registering there is a no-op by design and costs
nothing -- not a read, not a hash.

What gets published, per id:

* a live element -> ``(source_id, element_text_sha(text))``;
* a read that failed (``unknown``) -> ``None``, the plan's STATED "travelled a
  producer and could not be fingerprinted"; the terminal check reports such a
  citation as ``unverifiable`` rather than accepting it unchecked;
* a dead pointer -> nothing. There is no text whose change could be detected,
  and publishing a pretend snapshot would make the terminal check call a
  pointer that was dead BEFORE the question "deleted during the answer".

The consumer keeps the FIRST real snapshot per element (``_RunState.
record_evidence``), so a later pointer read -- which may postdate an edit --
never overwrites a snapshot a reading producer took earlier.

Content-free telemetry: ``producer_evidence_attested`` and
``producer_evidence_unavailable`` carry the producer code and counts, never an
id, a text or an exception message.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping

from app.domain.evidence_fingerprint import element_text_sha
from app.domain.retrieval_control import RetrievalControlError
from app.repositories.global_ask_ports import GlobalAskEvidenceReaderPort
from app.repositories.read_budget import read_budget
from app.services.cancellation import AskCancelled
from app.services.federated_run import current_federated_run_plan


LIVE = "live"
DEAD = "dead"
UNKNOWN = "unknown"

# Producer codes travel into telemetry, so they are identifiers, never prose.
_PRODUCER_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass
class _AttestationSeat:
    """One global run's reader, event sink and pointer memo.

    Installed by the job thread around the whole run and carried into worker
    threads by ``copy_context()``, like every other seat of a global run; the
    memo dict is shared by reference across those copies, hence the lock.
    """

    reader: GlobalAskEvidenceReaderPort
    emit: Callable[[dict], None] | None = None
    settled: dict = field(default_factory=dict)
    lock: Any = field(default_factory=threading.Lock)


_SEAT: "ContextVar[_AttestationSeat | None]" = ContextVar(
    "evidence_attestation_seat", default=None,
)


@contextmanager
def evidence_attestation_seat(
    reader: GlobalAskEvidenceReaderPort, *, emit: Callable[[dict], None] | None = None,
) -> Iterator[None]:
    """Install this run's attestation reader for the duration of the block.

    Single writer, same rules as the other global-run seats
    (``federated_run.federated_run_plan``): nesting is a wiring bug, and the
    block must be entered and left in the same context.
    """
    if _SEAT.get() is not None:
        raise ValueError("an evidence attestation seat is already installed")
    token = _SEAT.set(_AttestationSeat(reader=reader, emit=emit))
    try:
        yield
    finally:
        _SEAT.reset(token)


def attest_read(producer: str, texts: Mapping[str, "tuple[str, str | None]"]) -> None:
    """Publish the snapshot of elements whose text the producer has in hand.

    ``texts`` maps ``element_id -> (source_id, full element text)``. The FULL
    text, not an excerpt: the terminal check re-reads the stored row and hashes
    all of it, so a producer that truncates to a preview must hash before it
    truncates. Entries without an element id or a source id are skipped -- a
    snapshot needs both halves, and the consumer reports a citation nobody
    attested as ``unverifiable``.
    """
    plan = current_federated_run_plan()
    if plan is None or not texts:
        return
    snapshot = {
        element_id: (source_id, element_text_sha(text))
        for element_id, (source_id, text) in texts.items()
        if element_id and source_id
    }
    if not snapshot:
        return
    plan.on_evidence(snapshot)
    _emit({
        "kind": "producer_evidence_attested", "producer": _code(producer),
        "method": "read", "elements": len(snapshot),
    })


def attest_pointers(producer: str, element_ids: Iterable[str]) -> dict[str, str]:
    """Attest elements the producer cites without having read their text.

    Returns ``{element_id: "live" | "dead" | "unknown"}`` for every distinct,
    non-empty id asked about, or ``{}`` when no plan is installed (nothing was
    checked; the caller keeps whatever it did before). ``dead`` means the row
    did not exist at retrieval time; ``unknown`` means the read failed or ran
    out of budget, and those ids were published as ``None``.

    One read per call at most, over the ids this run has not settled yet;
    ``live`` and ``dead`` are memoised for the rest of the run, ``unknown`` is
    retried by the next call. Cancellation and participant-attestation failures
    re-raise; any other read failure is fail-soft in the closed direction.
    """
    plan = current_federated_run_plan()
    if plan is None:
        return {}
    ids = list(dict.fromkeys(str(value) for value in element_ids if value))
    if not ids:
        return {}
    seat = _SEAT.get()
    if seat is None:
        return _refuse(plan, producer, ids, reason="no_reader", error_type="")
    with seat.lock:
        states = {key: seat.settled[key] for key in ids if key in seat.settled}
    pending = [key for key in ids if key not in states]
    if pending:
        states.update(_read_pointers(plan, seat, producer, pending))
    return {key: states[key] for key in ids}


def _read_pointers(plan, seat: _AttestationSeat, producer: str, pending: list) -> dict:
    """The one bounded read for ``attest_pointers``; publishes what it proved."""
    try:
        with read_budget(
            time.monotonic() + float(plan.notebook_timeout_seconds), plan.cancel,
        ):
            current = dict(seat.reader.evidence_fingerprints(pending))
    except (AskCancelled, RetrievalControlError):
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed, see attest_pointers
        return _refuse(
            plan, producer, pending, reason="read_failed",
            error_type=type(exc).__name__,
        )
    live = {key: current[key] for key in pending if key in current}
    states = {key: (LIVE if key in live else DEAD) for key in pending}
    with seat.lock:
        seat.settled.update(states)
    if live:
        plan.on_evidence(live)
    _emit({
        "kind": "producer_evidence_attested", "producer": _code(producer),
        "method": "pointers", "elements": len(pending),
        "live": len(live), "dead": len(pending) - len(live),
    })
    return states


def _refuse(plan, producer: str, ids: list, *, reason: str, error_type: str) -> dict:
    """State the ids as unreadable (``None``) and say why, content-free."""
    plan.on_evidence(dict.fromkeys(ids))
    event = {
        "kind": "producer_evidence_unavailable", "producer": _code(producer),
        "reason": reason, "elements": len(ids),
    }
    if error_type:
        event["error_type"] = error_type
    _emit(event)
    return dict.fromkeys(ids, UNKNOWN)


def _code(producer: str) -> str:
    """The producer code as it may appear in telemetry."""
    return producer if _PRODUCER_CODE.fullmatch(producer or "") else "unknown"


def _emit(event: dict) -> None:
    """Fail-open telemetry through the run's sink; never breaks a producer."""
    seat = _SEAT.get()
    emit = seat.emit if seat is not None else None
    if emit is None:
        return
    try:
        emit(event)
    except Exception:  # noqa: BLE001 - observability is fail-open by contract
        pass


__all__ = [
    "DEAD", "LIVE", "UNKNOWN",
    "attest_pointers", "attest_read", "evidence_attestation_seat",
]
