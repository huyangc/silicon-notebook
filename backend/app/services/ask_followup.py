"""PR-C: carry one reasoning Ask's follow-up resolution from the API entry
point to the engine's intent seam.

Why a context variable instead of a request field: the rewrite is a
*server-side* preflight, not something a client submits.  ``AskRequest``
deliberately gains no field for it -- ``api_contract.json`` stays unchanged and
no caller can hand the engine a "resolved question" of its own choosing.  The
value has to survive from ``ask_routes`` (where the 422 gate runs, above the
durable job) down to ``AskService._prepare_reasoning_ask``, which is reached
through several handler return paths and, for streaming, through a worker
thread that detaches from the connection.  ``background_jobs.submit`` snapshots
the caller's context, so entering this manager around the call that starts the
run reaches the detached worker unchanged.  Shape copied deliberately from the
display-only scope-receipt carrier in ``app.services.source_scope`` (named in
prose rather than spelled out, because that module's own guard test pins the
exact set of files that mention its identifier): one ContextVar, one set/reset
contextmanager, one reader -- two carriers with the same lifetime should not
have two different spellings.

Consumers MUST verify ``resolution.question`` against the question they were
handed before using ``resolved_question``.  A context variable outlives the
call that set it if a caller ever reuses a thread's context for a second
request, and a rewrite of somebody else's question is worse than no rewrite:
the engine falls back to the original wording, which is exactly the behavior
this whole feature is an improvement over.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class FollowupResolution:
    """One reasoning request's follow-up verdict, decided before any job exists.

    ``question`` is the user's original wording, stripped -- the identity this
    resolution belongs to, and the ONLY wording that reaches persistence
    (``ask_jobs.question``, the conversation turn) and error copy.

    ``resolved_question`` is what retrieval should search for: the rewrite when
    one was made and passed the gate, otherwise ``question`` itself.

    ``rewrite_ms`` is the wall time the rewrite step cost, and is ``None``
    whenever no rewrite step ran (a clear question, or a request that arrived
    with a client-confirmed intent).  It is a trace duration, never a gate
    input.

    ``gate_message`` is non-empty exactly when the request must fail closed;
    it is complete user copy derived from the ORIGINAL question's seed, never
    from the rewrite (see ``query_intent.followup_gate``).  A resolution with a
    non-empty ``gate_message`` never reaches the engine -- the entry point
    raises on it -- so its other fields are diagnostic only.
    """

    question: str
    resolved_question: str
    rewrite_ms: int | None
    gate_message: str


_CURRENT_FOLLOWUP_RESOLUTION: ContextVar[FollowupResolution | None] = ContextVar(
    "current_followup_resolution", default=None
)


@contextmanager
def followup_resolution_context(
    resolution: "FollowupResolution | None",
) -> Iterator[None]:
    """Carry this request's follow-up resolution to the engine's intent seam.

    Entered by the API entry point AFTER its 422 gate, around the call that
    runs (or starts) the Ask.  ``None`` is a legal value and means "no
    resolution for this run" -- the engine then judges the original question,
    byte for byte what it did before this feature existed.
    """
    token = _CURRENT_FOLLOWUP_RESOLUTION.set(resolution)
    try:
        yield
    finally:
        _CURRENT_FOLLOWUP_RESOLUTION.reset(token)


def current_followup_resolution() -> "FollowupResolution | None":
    """This run's follow-up resolution, or None when the caller never entered
    the context above (direct ``repo.ask`` callers, background replays)."""
    return _CURRENT_FOLLOWUP_RESOLUTION.get()
