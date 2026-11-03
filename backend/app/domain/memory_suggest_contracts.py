"""Auto user-memory suggestions — wire vocabulary and protocol constants.

Design: ``docs/superpowers/specs/2026-10-28-auto-user-memory-design_zh.md``
(approved 2026-10-28, D1–D21). Pure values, zero ``app.services`` /
``app.repositories`` dependency: the store (both backends), the trigger, the
job, the signal functions and the API all import their codes from here so one
spelling cannot drift between layers.

What lives here and why:

* **Codes** — ``SuggestedSignal`` (the nullable ``memory_items.suggested_signal``
  column's value domain; the column itself carries no CHECK on either backend,
  design §10 D5(b)), the per-user ``memory_mode`` domain, the job ``status``
  values (the only two the CHECK admits) and the terminal ``last_outcome``
  codes, the fingerprint kinds, the system revision reasons and the
  ``memory_lifecycle`` actions.
* **Protocol constants** — values that *define the algorithm* (clustering
  threshold, window shape, fingerprint caps, give-up count). Changing one
  changes what a "signal" is, so they are named constants, not deployment
  knobs. Cost and budget values (trigger, backlog, daily cap, per-run caps,
  character limits, expiry, sweep page) are ``Settings`` fields instead
  (``AUTO_MEMORY_SUGGEST_*``). Exact values are registered in
  ``docs/product-and-api*.md``.
* **Content-free telemetry** — the closed key set of the ``memory_suggest_run``
  event and the content keys no memory-suggest event may ever carry (design
  §6.4).
"""
from __future__ import annotations

from typing import Literal

# ------------------------------------------------------------------ workload
#: The chat workload of the one bounded model call per run (D4 / D18).
MEMORY_SUGGEST_WORKLOAD = "memory_suggest"

# ------------------------------------------------------------------ signals
#: Application-level domain; adding a signal needs no storage migration.
#: NULL on the column means "not a system suggestion".
SuggestedSignal = Literal["clarified", "repeated", "corrected"]
SIGNAL_CLARIFIED: SuggestedSignal = "clarified"
SIGNAL_REPEATED: SuggestedSignal = "repeated"
SIGNAL_CORRECTED: SuggestedSignal = "corrected"
SUGGESTED_SIGNALS: tuple[SuggestedSignal, ...] = (SIGNAL_CLARIFIED, SIGNAL_REPEATED, SIGNAL_CORRECTED)
#: The memory list's virtual origin filter value: rows with a
#: ``suggested_signal`` (their stored origin stays ``ask_answer``).
ORIGIN_SUGGESTED = "suggested"
assert set(SuggestedSignal.__args__) == set(SUGGESTED_SIGNALS), (  # type: ignore[attr-defined]
    "SuggestedSignal and SUGGESTED_SIGNALS must name exactly the same values"
)

#: What the model may label one kept item as (closed output, §4.2 step 7).
SuggestionKind = Literal["term", "fact", "correction"]
SUGGESTION_KINDS: tuple[SuggestionKind, ...] = ("term", "fact", "correction")

# ------------------------------------------------------------------ memory_mode
#: ``user_profiles.memory_mode`` value domain. Validated as a Literal on the
#: request model only; neither backend adds a CHECK (SQLite would need a table
#: rebuild). Readers fold anything else back to ``manual`` (G27).
MemoryMode = Literal["manual", "suggest"]
MEMORY_MODE_MANUAL: MemoryMode = "manual"
MEMORY_MODE_SUGGEST: MemoryMode = "suggest"
MEMORY_MODES: tuple[MemoryMode, ...] = (MEMORY_MODE_MANUAL, MEMORY_MODE_SUGGEST)


def normalize_memory_mode(value: object) -> MemoryMode:
    """Fold a stored ``memory_mode`` into its closed domain.

    Any value the application does not know (a hand-edited row, a value a
    future version wrote before a rollback) reads as ``manual``: the switch
    is opt-in, so the safe reading of "unknown" is "off".
    """
    if value == MEMORY_MODE_SUGGEST:
        return MEMORY_MODE_SUGGEST
    return MEMORY_MODE_MANUAL


# ------------------------------------------------------------------ job rows
#: ``memory_suggest_jobs.status`` — the only two values the CHECK admits (G2).
JOB_STATUS_IDLE = "idle"
JOB_STATUS_RUNNING = "running"
JOB_STATUSES: tuple[str, ...] = (JOB_STATUS_IDLE, JOB_STATUS_RUNNING)

#: ``memory_suggest_jobs.last_outcome`` — every exit path of a run settles to
#: exactly one of these (G2). The reason code or exception class name of
#: ``skipped`` / ``failed`` goes to ``failure_reason``.
OUTCOME_DONE = "done"
OUTCOME_NO_SIGNAL = "no_signal"
OUTCOME_SKIPPED = "skipped"
OUTCOME_BACKLOG = "backlog"
OUTCOME_DISABLED = "disabled"
OUTCOME_DAILY_CAP = "daily_cap"
OUTCOME_MODEL_REJECTED = "model_rejected"
OUTCOME_MODEL_UNAVAILABLE = "model_unavailable"
OUTCOME_FAILED = "failed"
JOB_OUTCOMES: tuple[str, ...] = (
    OUTCOME_DONE,
    OUTCOME_NO_SIGNAL,
    OUTCOME_SKIPPED,
    OUTCOME_BACKLOG,
    OUTCOME_DISABLED,
    OUTCOME_DAILY_CAP,
    OUTCOME_MODEL_REJECTED,
    OUTCOME_MODEL_UNAVAILABLE,
    OUTCOME_FAILED,
)
#: Outcomes that count as one failure of the same batch (cursor unchanged,
#: ``consecutive_failures += 1``; design §4.2 cursor table, G24).
FAILURE_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_MODEL_REJECTED, OUTCOME_MODEL_UNAVAILABLE, OUTCOME_FAILED}
)

#: ``failure_reason`` of a run a process restart interrupted. The startup
#: sweep settles such a row as ``failed`` and counts it as one failure of the
#: same batch (``consecutive_failures += 1``, cursor unchanged), exactly like
#: any other ``failed`` run: a batch that keeps crashing the process is given
#: up after ``MAX_CONSECUTIVE_FAILURES`` like one that keeps failing cleanly.
FAILURE_REASON_INTERRUPTED = "interrupted"

#: ``failure_reason`` codes of a ``skipped`` run (re-checked at claim time).
SKIP_USER_DISABLED = "user_disabled"
SKIP_NOTEBOOK_UNAVAILABLE = "notebook_unavailable"
SKIP_NO_READ_ACCESS = "no_read_access"
#: ``SuggestionRevoked.reason`` when the job row this run claimed is gone or
#: carries another ``claim_token`` (switch turned off, an exit or a loss of
#: read access deleted it, or a later run re-claimed it).
REVOKED_CLAIM_LOST = "claim_lost"
#: Every ``SuggestionRevoked.reason`` value.
REVOKED_REASONS: tuple[str, ...] = (
    SKIP_NOTEBOOK_UNAVAILABLE,
    SKIP_NO_READ_ACCESS,
    SKIP_USER_DISABLED,
    REVOKED_CLAIM_LOST,
)


class SuggestionRevoked(Exception):
    """The suggestion write's own transaction found the run no longer entitled
    to write (design §4.2 step 8): the notebook is gone or not live, the owner
    lost read access, the owner's switch is off, or the claimed job row is gone
    or re-claimed. Nothing was written. ``reason`` is one of
    ``REVOKED_REASONS`` -- a code, never content."""

    def __init__(self, reason: str) -> None:
        if reason not in REVOKED_REASONS:
            raise ValueError(f"unknown revocation reason: {reason!r}")
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ fingerprints
FINGERPRINT_PROPOSED = "proposed"
FINGERPRINT_DECLINED = "declined"
FINGERPRINT_KINDS: tuple[str, ...] = (FINGERPRINT_PROPOSED, FINGERPRINT_DECLINED)

# ------------------------------------------------------------------ revisions / events
#: ``memory_revisions.reason`` of the two system-authored revisions (G12).
REVISION_REASON_SUGGESTED = "suggested"
REVISION_REASON_EXPIRED = "expired"

#: ``memory_lifecycle`` actions this feature adds (design §6.4).
LIFECYCLE_SUGGESTION_CREATED = "suggestion_created"
LIFECYCLE_SUGGESTION_EXPIRED = "suggestion_expired"

#: The per-run event kind.
MEMORY_SUGGEST_RUN_EVENT = "memory_suggest_run"
#: Closed key set of a ``memory_suggest_run`` event payload (besides the
#: logger's own envelope). Counts, codes and ids only.
MEMORY_SUGGEST_RUN_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "notebook_id",
        "owner_id",
        "outcome",
        "signals_by_kind",
        "deduped",
        "declined_cached",
        "candidates_written",
        "model_status",
        "latency_ms",
        "neighbors_vector",
    }
)
#: Closed payload of the two system-suggestion lifecycle actions; ``kind``
#: belongs to the event envelope, just as it does for run events above.
MEMORY_SUGGEST_LIFECYCLE_EVENT_KEYS: frozenset[str] = frozenset({
    "memory_id", "notebook_id", "owner_id", "origin", "status", "action",
    "suggested_signal",
})
#: Content-bearing keys no memory-suggest event may carry (design §6.4).
MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS: frozenset[str] = frozenset(
    {
        "question",
        "questions",
        "answer",
        "answers",
        "title",
        "content",
        "content_md",
        "statement",
        "tags",
        "options",
        "clarification",
        "clarification_answers",
        "excerpt",
        "prompt",
        "reply",
        "error",
        "message",
    }
)
assert not (MEMORY_SUGGEST_RUN_EVENT_KEYS & MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS)
assert not (MEMORY_SUGGEST_LIFECYCLE_EVENT_KEYS & MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS)

# ------------------------------------------------------------------ provenance
#: Provenance keys shared with ``ask_answer`` memories (G13).
PROVENANCE_QUESTION = "question"
PROVENANCE_ANSWER_ID = "answer_id"
PROVENANCE_CONVERSATION_ID = "conversation_id"
PROVENANCE_MODE = "mode"
PROVENANCE_EVIDENCE_LEVEL = "evidence_level"
PROVENANCE_CITATIONS = "citations"
PROVENANCE_ANCHORS = "anchors"
#: The suggestion block: ``{signal, kind, answer_ids, cluster_size, refines}``.
PROVENANCE_SUGGESTION = "suggestion"
SUGGESTION_PROVENANCE_KEYS: tuple[str, ...] = (
    "signal",
    "kind",
    "answer_ids",
    "cluster_size",
    "refines",
)

# ------------------------------------------------------------------ algorithm
#: Leader clustering: a question joins a leader when the Jaccard similarity of
#: their keyword bases is at least this (design §3.2).
REPEATED_JACCARD_MIN = 0.6
#: A repeated-question cluster needs at least this many questions.
REPEATED_MIN_CLUSTER_SIZE = 3
#: The repeated-class window: the most recent ``REPEATED_WINDOW_LIMIT`` asks in
#: ``[max(window_started_at, now - REPEATED_WINDOW_DAYS), now]``.
REPEATED_WINDOW_DAYS = 30
REPEATED_WINDOW_LIMIT = 40
#: Ascending scan bound of the clarified class after the cursor (G25).
CLARIFIED_SCAN_MAX = 40
#: A fingerprint needs at least this many keyword-basis terms (design §5.1).
FINGERPRINT_MIN_TERMS = 2
#: Member fingerprints registered per repeated cluster.
CLUSTER_FINGERPRINT_MAX = 10
#: Near neighbours per side (vector / lexical) shown to the model.
NEIGHBOR_K = 3
#: Vector-side neighbour scan: the most recently updated rows, same bound as
#: ``memory_retrieval_rows``' clamp.
NEIGHBOR_VECTOR_SCAN_MAX = 500
#: The same batch failing this many runs in a row is given up (cursor passes).
MAX_CONSECUTIVE_FAILURES = 3
#: Answer selection only considers grounded answers (G6).
GROUNDED_EVIDENCE_LEVEL = "grounded"
