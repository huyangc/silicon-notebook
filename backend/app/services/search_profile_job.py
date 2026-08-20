"""Agentic Memory P3 (B-Profile, T7): the deterministic, ZERO-LLM inference
job that fills in one field of a person's ``search_profile`` document —
``answer_language`` — from the language of their own recent questions.

**Why v1 infers exactly ONE field, and no more (main-agent ruling ②,
registered here rather than only in the plan doc):**

* **``answer_language``** is the only field with a cheap, deterministic,
  non-modeling signal: a question's script is a property of its own
  characters (see :func:`app.services.search_profile.classify_ask_language`),
  so inferring it costs one bounded SQL read and zero model calls.
* **Retrieval-effort mode** (chunk/graph/reasoning, or a depth tier) has NO
  legal consumer in this document's schema — ``search_profile`` only carries
  ``answer_language``/``answer_shape``/``answer_detail``/``domain_terms``,
  none of which is a retrieval-scope or effort field (the T8 injection side
  structurally asserts this: :func:`app.services.search_profile.
  render_style_block`'s output can never contain a scope/effort word). Adding
  a mode-majority field here would be pure risk with no code anywhere to read
  it.
* **Domain terms** ("frequently used vocabulary") would mean fingerprinting a
  person's actual question CONTENT across notebooks and freezing it into a
  prompt document — the exact kind of cross-notebook content aggregation this
  repository's isolation rules exist to prevent elsewhere (compare
  ``retrieval_experience_projection``'s structural ban on free text). v1
  leaves this field to the person's own editing (``PATCH
  /me/search-profile``); a future version that wants to infer it needs its
  own privacy design, not a corner of this one.

**Trigger shape, and why it does NOT reuse ``RetrievalExperienceDistillation
Service``'s async/single-flight machinery wholesale:** that service defers
its actual work to the background job pool because its one call is an LLM
round-trip that can run for seconds; blocking every ask-completion behind it
would be a real latency cost. This job's entire "run" is one bounded SELECT
(:data:`app.repositories.ports.SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT` rows)
plus at most one primary-key UPSERT — the same "bounded and zero-model, so it
does not need its own queue" argument ``mcp_server.add_observation`` already
makes for skipping a background hand-off. It therefore runs SYNCHRONOUSLY,
under the SAME process-local lock that claims the trigger, rather than
handing off to ``background_jobs``: a coarse lock held for one bounded read
plus one bounded write of DB work is simpler and no slower in practice than a
per-user lock dict plus a running-set plus a requeue loop, and
single-flight-per-user falls out of it for free (see
:meth:`SearchProfileInferenceService.note_ask_completed`'s docstring). T9 fix
round: this used to say "microseconds" — the honest worst-case bound is NOT
that tight. The SQLite write path (``IdentityStore.set_user_search_profile``)
takes the store's own process-local ``write()`` lock too, so the two locks
can stack when a concurrent writer (a person's own ``PATCH
/me/search-profile`` edit, or another triggered inference run for a
DIFFERENT user sharing the same store) is already inside it; the actual
worst-case wait this lock can impose on an unrelated user's
``note_ask_completed`` call is bounded by one SQLite write-lock acquisition —
``settings.db_busy_timeout_ms`` — not a microsecond-scale constant.

**Counter is process-local and resets on restart** — the same registered
trade-off as ``RetrievalExperienceDistillationService._pending`` and
``AgentProfileConsolidationService``'s in-memory pieces: this is a pure
increment, so a restart costs at most one deferred inference round, never
correctness. Persisting a per-user counter would need either a table of its
own for one integer per user, or a column on ``user_profiles`` this feature
does not otherwise need.

**Fail-open in full.** This hangs off a hook that fires AFTER an answer has
already been delivered (``AskExecutionCoordinator.start``'s ``worker()``,
via ``RepositoryRuntime._note_ask_completed``); a delivered answer must never
be affected by a background preference-inference failure. Every ordinary
exception is logged and swallowed; ``KeyboardInterrupt``/``SystemExit`` keep
propagating.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from typing import Any, Mapping

from app.repositories.ports import (
    SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO,
    SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES,
    SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT,
    AskStateStorePort,
    IdentityRepository,
)
from app.services.reasoning_retrieval import search_profile_wiring_active

_log = logging.getLogger("silicon_notebook.search_profile_job")

#: The two candidate values this v1 job may ever write. Deliberately a
#: closed tuple rather than deriving it from ``ANSWER_LANGUAGE_VALUES``
#: (which also contains ``"auto"``, a value this job must NEVER write — see
#: the module docstring's "not writing ≠ writing auto" rule, inherited from
#: ``search_profile.merge_field``'s own "clearing is deletion" rule).
_WRITABLE_LANGUAGES: "tuple[str, ...]" = ("zh", "en")


class SearchProfileInferenceService:
    """Per-user threshold gate, in-process single flight, one bounded
    read-then-write. See the module docstring for why this differs in shape
    from its P1/P2 siblings.
    """

    def __init__(
        self,
        *,
        settings: Any,
        ask_state: "AskStateStorePort | None",
        identity: "IdentityRepository | None",
        event_log: Any,
    ) -> None:
        self.settings = settings
        self.ask_state = ask_state
        self.identity = identity
        self.event_log = event_log
        # ⚠ PROCESS-LOCAL, resets on restart — see the module docstring.
        # Keyed by user id: unlike the P2 experience library (one
        # deployment-global counter), this job's threshold is per PERSON, so
        # a single scalar would let one very active user's questions
        # permanently starve everyone else's inference.
        self._lock = threading.Lock()
        self._pending: dict[str, int] = {}

    # ------------------------------------------------------------- triggering
    def note_ask_completed(self, user_id: str) -> None:
        """One ask finished for ``user_id``, somewhere in the deployment.

        ⚠ FAIL-OPEN IN FULL — see the module docstring.

        Single-flight-per-user falls out of doing the actual read+write
        INSIDE this same critical section, rather than handing off to a
        background thread: the counter check, its reset to 0, and the bounded
        read+write all happen while ``self._lock`` is held, so two threads
        racing to cross this user's threshold at the same instant cannot both
        see the pre-reset count — the second one, whenever it acquires the
        lock, always finds the counter already back at 0 (or 1, from its own
        increment) and starts a fresh window rather than double-triggering.
        The cost is that a DIFFERENT user's ``note_ask_completed`` call
        briefly waits behind this one's bounded DB work; that is an
        intentionally accepted trade against the complexity of a
        per-user lock dict plus a running-set plus a requeue loop for work
        this cheap and this rare (once per ``user_search_profile_trigger``
        asks, per person).
        """
        try:
            if not user_id:
                return
            if not search_profile_wiring_active(self.settings, self.identity):
                return
            if self.ask_state is None:
                return
            trigger = max(
                1, int(getattr(self.settings, "user_search_profile_trigger", 20))
            )
            with self._lock:
                count = self._pending.get(user_id, 0) + 1
                if count < trigger:
                    self._pending[user_id] = count
                    return
                self._pending[user_id] = 0
                self._run_locked(user_id)
        except Exception:  # noqa: BLE001 — never break a delivered answer
            _log.exception(
                "search profile inference trigger failed for user %s", user_id
            )

    # ------------------------------------------------------------------ run
    def _run_locked(self, user_id: str) -> None:
        """The read-then-write. Called with :attr:`_lock` already held.

        Never raises to :meth:`note_ask_completed` — every exit path emits
        exactly one event and returns; the caller's own ``try`` is a second,
        redundant safety net (defence in depth, not the primary contract).
        """
        started = time.monotonic()
        try:
            samples = self.ask_state.recent_user_ask_languages(
                user_id, limit=SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT
            )
            languages = [
                row.get("language")
                for row in samples
                if isinstance(row, Mapping) and row.get("language")
            ]
            total = len(languages)
            if total < SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES:
                self._emit(
                    "skipped",
                    latency_ms=_elapsed_ms(started),
                    samples=total,
                    reason="insufficient_samples",
                )
                return
            counts = Counter(languages)
            winner = None
            winner_count = 0
            for language in _WRITABLE_LANGUAGES:
                candidate_count = counts.get(language, 0)
                if candidate_count > winner_count:
                    winner, winner_count = language, candidate_count
            if winner is None or winner_count / total < SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO:
                self._emit(
                    "skipped",
                    latency_ms=_elapsed_ms(started),
                    samples=total,
                    reason="no_clear_majority",
                )
                return
            self.identity.set_user_search_profile(
                user_id, {"answer_language": winner}, origin="job"
            )
            self._emit(
                "done",
                latency_ms=_elapsed_ms(started),
                samples=total,
                language=winner,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open, see module docstring
            self._emit(
                "failed",
                latency_ms=_elapsed_ms(started),
                reason=type(exc).__name__,
            )
            _log.exception(
                "search profile inference run failed for user %s", user_id
            )

    # ------------------------------------------------------------ bookkeeping
    def _emit(self, status: str, *, latency_ms: int, **extra: Any) -> None:
        """Counts and duration only, plus (when written) the closed-enum
        language value itself — NEVER a question, a notebook id or a user
        id.

        ``user_id`` is deliberately absent from the payload: this hook runs
        inside the same worker thread ``AskExecutionCoordinator`` submitted
        for the ask that triggered it, which already carries that person's
        ``_log_owner`` ContextVar (``background_jobs.submit`` copies it via
        ``copy_context()``) — so the per-user event log already routes this
        line to the right person's own log directory without the payload
        needing to name them a second time.
        """
        try:
            self.event_log.emit(
                {
                    "kind": "search_profile_inferred",
                    "status": status,
                    "latency_ms": int(latency_ms),
                    "samples": 0,
                    **extra,
                }
            )
        except Exception:  # noqa: BLE001 — diagnostics never break a run
            pass


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "SearchProfileInferenceService",
]
