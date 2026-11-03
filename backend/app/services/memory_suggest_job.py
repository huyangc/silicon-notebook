"""Auto user-memory suggestions: the per-(notebook, owner) background run
(design 2026-10-28 §4.2–§4.4, §5.1, §6.4; plan T7b).

Protocol: the ``agent_profile_jobs`` one (``agent_profile_job.py``). ``start``
claims the durable single-flight row FIRST and only then hands the run to the
light maintenance pool (``notify_pending=False``); a claim that loses (no row,
or a run already in flight) returns at once, which is what makes the
trigger's "call ``start_job`` on every ask past the threshold" single-flight.
A submission that fails settles on the spot. Every exit path of ``run``
settles exactly one terminal ``last_outcome`` under the run's ``claim_token``
and emits exactly one content-free ``memory_suggest_run`` event.

One run, in order (each step only when the previous one passed):

1. re-check the owner's switch and read access to the live notebook
   (``skipped`` + ``SKIP_*`` code otherwise);
2. expire this (owner, notebook)'s overdue drafts;
3. zero-call gates: the backlog of unadopted suggestions (``backlog``) and the
   deployment wiring (``disabled``);
4. two sampling reads (G25) -> signal items, minus what the fingerprint table
   already knows -> ``select_items``; nothing left is ``no_signal``;
5. the chat workload unbound is ``model_unavailable`` (checked before the
   quota, so a deployment without the model spends none);
6. one conditional quota reservation per UTC day (``daily_cap``);
7. one batched embedding (``memory_embedding``; unavailable = lexical
   neighbours only, G7) and one neighbour read;
8. one bounded chat call; a malformed reply is ``model_rejected`` and writes
   no negative cache;
9. ``MemoryService.create_suggestions`` (its own transaction re-checks access,
   switch and claim; ``SuggestionRevoked`` abandons the whole batch as
   ``skipped``), then the negative cache for the items judged no / duplicate /
   unanswered.

Cursor, failure count and give-up follow the design's cursor table (G24); see
``settle_plan``. Every store call is its own short transaction: the run never
holds the job row's lock while another statement locks ``users`` or
``memory_suggest_users`` (plan B11).

Privacy (design §6.1): every read goes through ``MemorySuggestStorePort``,
whose statements bind the owner in SQL (pinned by
``tests/test_memory_suggest_privacy_guard.py``); this module imports
repositories through ``app.repositories.ports`` only (G28). Logs and events
carry codes, counts and exception class names, never content (§6.4).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence, get_args

from app.core.memory_inputs import MEMORY_TITLE_MAX_CHARS
from app.core.model_json import parse_model_json_object
from app.domain.memory_suggest_contracts import (
    CLARIFIED_SCAN_MAX,
    FAILURE_OUTCOMES,
    FAILURE_REASON_INTERRUPTED,
    MAX_CONSECUTIVE_FAILURES,
    MEMORY_SUGGEST_RUN_EVENT,
    MEMORY_SUGGEST_WORKLOAD,
    NEIGHBOR_K,
    OUTCOME_BACKLOG,
    OUTCOME_DAILY_CAP,
    OUTCOME_DISABLED,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_MODEL_REJECTED,
    OUTCOME_MODEL_UNAVAILABLE,
    OUTCOME_NO_SIGNAL,
    OUTCOME_SKIPPED,
    PROVENANCE_ANCHORS,
    PROVENANCE_ANSWER_ID,
    PROVENANCE_CITATIONS,
    PROVENANCE_CONVERSATION_ID,
    PROVENANCE_EVIDENCE_LEVEL,
    PROVENANCE_MODE,
    PROVENANCE_QUESTION,
    PROVENANCE_SUGGESTION,
    REPEATED_WINDOW_DAYS,
    REPEATED_WINDOW_LIMIT,
    SIGNAL_CLARIFIED,
    SIGNAL_CORRECTED,
    SIGNAL_REPEATED,
    SKIP_NO_READ_ACCESS,
    SKIP_USER_DISABLED,
    SuggestionRevoked,
)
from app.models.memory import MemoryStatus, MemoryWrite, SuggestionWriteResult
from app.repositories.ports import MemorySuggestStorePort
from app.services import background_jobs
from app.services.memory_suggest_signals import (
    SUGGESTION_CONTENT_MAX_CHARS,
    RenderLimits,
    SignalCursor,
    SignalItem,
    SuggestionDecision,
    clarified_signals,
    common_scan_end_cursor,
    corrected_signals,
    earlier_cursor,
    neighbor_id_sets,
    parse_suggest_reply,
    render_items,
    repeated_signals,
    select_items,
)
from app.services.memory_suggest_wiring import (
    memory_suggest_enabled_signals,
    memory_suggest_wiring_active,
)
from app.services.model_work import model_artifact_scope
from app.services.prompts import MEMORY_SUGGEST_SCHEMA_HINT, memory_suggest_prompt

_log = logging.getLogger("silicon_notebook.memory_suggest")

#: Light-pool job name prefix (``background_jobs._SAFE_JOB_PREFIXES``). It
#: names the notebook only: which member a run is for stays out of logs.
_JOB_NAME_PREFIX = "memorysuggest-"
#: The neighbour vectors are compared with the stored memory vectors, so the
#: query side uses the very workload that wrote them (G7).
NEIGHBOR_EMBEDDING_WORKLOAD = "memory_embedding"
#: Every memory status: the neighbour read sees drafts, dismissed and
#: deprecated rows too (design §4.2 step 6).
NEIGHBOR_STATUSES: tuple[str, ...] = get_args(MemoryStatus)

#: ``ModelInvocationError.code`` values the run classifies (callers classify by
#: code, never by exception type -- ``model_work.TRANSIENT_MODEL_ERROR_CODES``).
_CODE_MALFORMED = "malformed_response"
_CODE_NOT_CONFIGURED = "model_not_configured"

#: ``failure_reason`` codes of this module (besides ``SKIP_*`` / revocation
#: codes and exception class names).
REASON_MODEL_UNCONFIGURED = "model_unconfigured"
REASON_SUBMISSION_FAILED = "submission_failed"

#: ``model_status`` of the run event.
MODEL_NOT_CALLED = "not_called"
MODEL_OK = "ok"
MODEL_REJECTED = "rejected"
MODEL_UNAVAILABLE = "unavailable"
MODEL_ERROR = "error"
#: The batch failed ``MAX_CONSECUTIVE_FAILURES`` runs in a row and the cursor
#: passed it (the event's ``outcome`` keeps the failure code of this run).
MODEL_GAVE_UP = "gave_up"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_claim_token() -> str:
    return uuid.uuid4().hex


def _instant(value: str) -> datetime:
    """An ISO instant as an aware UTC datetime (naive reads as UTC, the
    signal module's convention)."""
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SuggestionMemoryPort(Protocol):
    """What a run needs from ``MemoryService``."""

    def create_suggestions(
        self,
        *,
        notebook_id: str,
        owner_id: str,
        claim_token: str,
        writes: Sequence[MemoryWrite],
        proposed_fingerprints: Sequence[str],
        declined_fingerprints: Sequence[str] = (),
        declined_expires_at: str | None = None,
        now: str | None = None,
    ) -> SuggestionWriteResult: ...

    def expire_suggestions(
        self,
        *,
        now: str,
        limit: int,
        owner_id: str | None = None,
        notebook_id: str | None = None,
    ) -> int: ...


class NotebookReadAccessPort(Protocol):
    """The one read-access predicate (``access_sql.NOTEBOOK_READ_SQL``: owner,
    member or live grant, on a live notebook)."""

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool: ...


@dataclass
class RunState:
    """What one run decided; ``settle_plan`` and the event read it."""

    outcome: str = OUTCOME_FAILED
    failure_reason: str = ""
    #: How far the cursor may move once the batch is known (``select_items``);
    #: ``None`` = not computed, or nothing to advance to.
    cursor_end: SignalCursor | None = None
    signals_by_kind: dict[str, int] = field(default_factory=dict)
    deduped: int = 0
    declined_cached: int = 0
    candidates_written: int = 0
    model_status: str = MODEL_NOT_CALLED
    neighbors_vector: bool = False

    def end(self, outcome: str, reason: str = "") -> None:
        self.outcome = outcome
        self.failure_reason = reason


def settle_plan(
    outcome: str, previous_failures: int, cursor_end: SignalCursor | None
) -> tuple[tuple[str, str, bool] | None, int, bool]:
    """``(cursor, consecutive_failures, gave_up)`` for a terminal outcome
    (design §4.2 cursor table, G24).

    * ``done`` / ``no_signal``: the cursor moves to ``cursor_end``; the failure
      count resets.
    * ``skipped`` / ``backlog`` / ``disabled`` / ``daily_cap``: nothing moves.
    * ``model_rejected`` / ``model_unavailable`` / ``failed``: the cursor
      stays and the count grows by one -- unless that makes
      ``MAX_CONSECUTIVE_FAILURES``: then the cursor passes the batch
      (``cursor_end``), the count resets and the run reports ``gave_up``.
    """
    as_tuple = (
        (cursor_end.created_at, cursor_end.id, cursor_end.inclusive)
        if cursor_end is not None else None
    )
    previous = max(0, int(previous_failures))
    if outcome in (OUTCOME_DONE, OUTCOME_NO_SIGNAL):
        return as_tuple, 0, False
    if outcome in FAILURE_OUTCOMES:
        failures = previous + 1
        if failures >= MAX_CONSECUTIVE_FAILURES:
            return as_tuple, 0, True
        return None, failures, False
    return None, previous, False


def _neighbor_text(item: SignalItem, limit: int) -> str:
    """The text an item looks for its neighbours with: what the person stated
    (clarified: the answers) or asked (repeated: the chosen question)."""
    if item.signal == SIGNAL_CLARIFIED:
        text = " ".join(answer for _question, answer in item.clarifications)
    else:
        text = item.question
    return " ".join(text.split())[: max(1, int(limit))]


def _provenance(
    item: SignalItem,
    decision: SuggestionDecision,
    sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """G13: the ``ask_answer`` keys plus the ``suggestion`` block. A repeated
    item also carries its bound answer's ``citations`` and ``anchors`` --
    ``sources`` is that answer's ``answer_projections(with_sources=True)``
    row, read with the owner predicate like every other read of the run
    (plan appendix B14; never the owner-less ``ask_state`` read)."""
    provenance: dict[str, Any] = {
        PROVENANCE_QUESTION: item.question,
        PROVENANCE_ANSWER_ID: item.answer_id,
        PROVENANCE_CONVERSATION_ID: item.conversation_id,
        PROVENANCE_MODE: item.mode,
        PROVENANCE_EVIDENCE_LEVEL: item.evidence_level,
    }
    if item.signal == SIGNAL_REPEATED:
        provenance[PROVENANCE_CITATIONS] = list((sources or {}).get("citations") or [])
        provenance[PROVENANCE_ANCHORS] = list((sources or {}).get("anchors") or [])
    provenance[PROVENANCE_SUGGESTION] = {
        "signal": item.signal,
        "kind": decision.kind,
        "answer_ids": list(item.answer_ids) or ([item.answer_id] if item.answer_id else []),
        "cluster_size": item.cluster_size or 1,
        "refines": decision.refines,
        "conflicts_with_sources": decision.conflicts_with_sources,
    }
    return provenance


class MemorySuggestJob:
    """Claim, one bounded run, one terminal settle per (notebook, owner)."""

    def __init__(
        self,
        *,
        store: MemorySuggestStorePort,
        access: NotebookReadAccessPort,
        settings: Any,
        models: Any,
        event_log: Any,
        new_id: Callable[[str], str],
        memory: SuggestionMemoryPort | None = None,
        clock: Callable[[], str] = _utc_now,
        new_token: Callable[[], str] = _new_claim_token,
        submit: Callable[..., Any] | None = None,
    ) -> None:
        self.store = store
        self.access = access
        self.settings = settings
        self.models = models
        self.event_log = event_log
        self.new_id = new_id
        # ``MemoryService`` is composed after this job (``wire_memory``); until
        # it is attached ``start`` claims nothing.
        self.memory = memory
        self.clock = clock
        self.new_token = new_token
        self._submit = submit

    def attach_memory(self, memory: SuggestionMemoryPort) -> None:
        self.memory = memory

    # ------------------------------------------------------------- entry points
    def start(self, notebook_id: str, owner_id: str) -> bool:
        """Claim this (notebook, owner)'s row and enqueue one run; never waits.

        ``False`` = nothing enqueued: no row, a run already in flight (the
        single-flight), the memory service not wired yet, or the submission
        failed (then the claim was settled ``failed`` on the spot)."""
        if self.memory is None or not notebook_id or not owner_id:
            return False
        token = self.new_token()
        claimed = self.store.claim(
            notebook_id, owner_id, claim_token=token, now=self.clock()
        )
        if claimed is None:
            return False
        submit = self._submit if self._submit is not None else background_jobs.submit
        try:
            submit(
                self.run,
                notebook_id,
                owner_id,
                claim_token=token,
                claimed=claimed,
                name=f"{_JOB_NAME_PREFIX}{notebook_id}",
                notify_pending=False,
            )
        except BaseException as exc:
            state = RunState()
            state.end(OUTCOME_FAILED, REASON_SUBMISSION_FAILED)
            self._finish(notebook_id, owner_id, token, claimed, state, time.perf_counter())
            if not isinstance(exc, Exception):
                raise
            _log.warning("memory suggestion submission failed: %s", type(exc).__name__)
            return False
        return True

    def sweep_on_start(self) -> int:
        """Settle every row a previous process left ``running`` as one failed
        run of its batch (``sweep_stale_on_start``, plan B4). Not gated on the
        deployment switch: a stuck row is hygiene, not a feature. Only the one
        process that owns the job pool may call it (plan B11; T13 wires it)."""
        try:
            return int(self.store.sweep_stale_on_start(now=self.clock()) or 0)
        except Exception as exc:
            _log.warning("memory suggestion startup sweep failed: %s", type(exc).__name__)
            return 0

    def run(
        self,
        notebook_id: str,
        owner_id: str,
        *,
        claim_token: str,
        claimed: Mapping[str, Any],
    ) -> str:
        """Execute one claimed run to exactly one terminal outcome; returns it.

        ``claimed`` is the row ``claim`` returned: its cursor, window start,
        failure count and ``pending_signal`` snapshot (consumed on every exit
        path, so asks counted during the run survive to trigger the next)."""
        started = time.perf_counter()
        state = RunState()
        try:
            with model_artifact_scope(notebook_id=notebook_id, parent_id=claim_token):
                self._execute(notebook_id, owner_id, claim_token, claimed, state)
        except (KeyboardInterrupt, SystemExit):
            state.end(OUTCOME_FAILED, FAILURE_REASON_INTERRUPTED)
            self._finish(notebook_id, owner_id, claim_token, claimed, state, started)
            raise
        except Exception as exc:
            state.end(OUTCOME_FAILED, type(exc).__name__)
            _log.warning("memory suggestion run failed: %s", type(exc).__name__)
        self._finish(notebook_id, owner_id, claim_token, claimed, state, started)
        return state.outcome

    # ---------------------------------------------------------------- the run
    def _execute(
        self,
        notebook_id: str,
        owner_id: str,
        claim_token: str,
        claimed: Mapping[str, Any],
        state: RunState,
    ) -> None:
        settings = self.settings
        memory = self.memory
        if memory is None:
            raise RuntimeError("memory service is not wired")
        now = self.clock()
        current = _instant(now)
        user = self.store.user_state(owner_id)
        if not user or not user.get("enabled"):
            return state.end(OUTCOME_SKIPPED, SKIP_USER_DISABLED)
        if not self.access.user_can_read_notebook(notebook_id, owner_id):
            # The predicate folds "live" in: a notebook in its delete window
            # reads as unreadable too.
            return state.end(OUTCOME_SKIPPED, SKIP_NO_READ_ACCESS)
        if int(claimed.get("consecutive_failures") or 0) >= MAX_CONSECUTIVE_FAILURES:
            # Only restarts get the count this far (``sweep_stale_on_start``
            # adds one per run a dead process left ``running``; a settled
            # failure gives up at the limit itself): a batch that keeps
            # killing the process is never sent again. Sample only to learn
            # how far the cursor may pass it, and give up (review P3-1).
            self._sample(notebook_id, owner_id, claimed, now, current, state)
            return state.end(OUTCOME_FAILED, FAILURE_REASON_INTERRUPTED)
        memory.expire_suggestions(
            now=now,
            limit=int(settings.auto_memory_suggest_expiry_sweep_limit),
            owner_id=owner_id,
            notebook_id=notebook_id,
        )
        backlog = self.store.unadopted_candidate_count(owner_id, notebook_id)
        if backlog >= int(settings.auto_memory_suggest_backlog_max):
            return state.end(OUTCOME_BACKLOG)
        if not memory_suggest_wiring_active(settings):
            return state.end(OUTCOME_DISABLED)
        items = self._sample(notebook_id, owner_id, claimed, now, current, state)
        if not items:
            return state.end(OUTCOME_NO_SIGNAL)
        if not self._configured(MEMORY_SUGGEST_WORKLOAD):
            state.model_status = MODEL_UNAVAILABLE
            return state.end(OUTCOME_MODEL_UNAVAILABLE, REASON_MODEL_UNCONFIGURED)
        if not self.store.reserve_daily_quota(
            owner_id,
            today_utc=current.date().isoformat(),
            cap=int(settings.auto_memory_suggest_daily_cap),
            now=now,
        ):
            return state.end(OUTCOME_DAILY_CAP)
        neighbors = self._neighbors(notebook_id, owner_id, items, state)
        decisions = self._decide(items, neighbors, state)
        if decisions is None:
            return None
        self._write(notebook_id, owner_id, claim_token, items, decisions, now, current, state)
        return None

    def _sample(
        self,
        notebook_id: str,
        owner_id: str,
        claimed: Mapping[str, Any],
        now: str,
        current: datetime,
        state: RunState,
    ) -> list[SignalItem]:
        """The ascending and repeated reads, plus the enabled corrected read;
        one batched read each for owner-bound projections, feedback
        and the answers that already have a memory, the fingerprint prefilter,
        then ``select_items``. Records ``cursor_end`` and the counts."""
        settings = self.settings
        store = self.store
        cursor = SignalCursor(
            str(claimed["cursor_created_at"]),
            str(claimed["cursor_id"]),
            inclusive=bool(claimed["cursor_inclusive"]),
        )
        # Save this fence BEFORE every sampling read. If an earlier running
        # ask finishes between scans, a later status read would lose the fence
        # even though the first scan never sampled that ask.
        pending = store.earliest_unfinished_ask(
            owner_id, notebook_id, cursor_created_at=cursor.created_at,
            cursor_id=cursor.id, inclusive=cursor.inclusive,
        )
        scan = store.clarified_scan(
            owner_id,
            notebook_id,
            cursor_created_at=cursor.created_at,
            cursor_id=cursor.id,
            inclusive=cursor.inclusive,
            limit=CLARIFIED_SCAN_MAX,
        )
        enabled = memory_suggest_enabled_signals(settings)
        corrected_scan = store.corrected_scan(
            owner_id, notebook_id,
            cursor_created_at=cursor.created_at, cursor_id=cursor.id,
            inclusive=cursor.inclusive,
            window_started_at=str(claimed["window_started_at"]),
            limit=CLARIFIED_SCAN_MAX,
        ) if SIGNAL_CORRECTED in enabled else []
        lower = max(
            _instant(str(claimed["window_started_at"])),
            current - timedelta(days=REPEATED_WINDOW_DAYS),
        )
        window = store.repeated_window(
            owner_id, notebook_id, lower=lower.isoformat(), limit=REPEATED_WINDOW_LIMIT
        )
        reviewed_answer_ids = list(dict.fromkeys(
            str(row["answer_id"]) for row in (*scan, *window) if row.get("answer_id")
        ))
        answer_ids = list(dict.fromkeys([
            *reviewed_answer_ids,
            *(str(row[key]) for row in corrected_scan
            for key in ("answer_id", "previous_answer_id") if row.get(key)
            ),
        ]))
        projections = store.answer_projections(
            owner_id,
            answer_ids,
            excerpt_chars=int(settings.auto_memory_suggest_answer_excerpt_max_chars),
            clarification_chars=int(settings.auto_memory_suggest_clarification_max_chars),
        )
        # Paired prior answers need text, not extra feedback/Memory reads.
        feedback = store.latest_feedback(owner_id, reviewed_answer_ids)
        with_memory = store.answers_with_memory(owner_id, reviewed_answer_ids)
        clarified = clarified_signals(
            scan, projections, notebook_id=notebook_id, answers_with_memory=with_memory
        )
        repeated = repeated_signals(
            window, cursor, projections, feedback,
            notebook_id=notebook_id, answers_with_memory=with_memory,
        )
        corrected = corrected_signals(corrected_scan, projections, notebook_id=notebook_id)
        fingerprints = list(dict.fromkeys(
            fingerprint for item in (*clarified, *repeated, *corrected)
            for fingerprint in item.lookup_fingerprints
        ))
        present = (
            store.fingerprints_present(owner_id, notebook_id, fingerprints, now=now)
            if fingerprints else set()
        )
        fresh_clarified = []
        batch_content: set[str] = set()
        for item in clarified:
            # Only registrable CONTENT keys merge asks in this batch. Short
            # replies keep independent ask identities; the database still owns
            # proposed/declined persistence in the original write transaction.
            if item.known_in(present) or batch_content.intersection(item.proposed_fingerprints):
                continue
            fresh_clarified.append(item)
            batch_content.update(item.proposed_fingerprints)
        fresh_repeated = [item for item in repeated if not item.known_in(present)]
        fresh_corrected = [item for item in corrected if not item.known_in(present)]
        state.deduped = (len(clarified) - len(fresh_clarified)) + (
            len(repeated) - len(fresh_repeated)
        ) + len(corrected) - len(fresh_corrected)
        items, cursor_end = select_items(
            fresh_clarified,
            fresh_repeated,
            enabled_signals=enabled,
            cap=int(settings.auto_memory_suggest_items_per_run),
            scan_end=common_scan_end_cursor(cursor, scan, corrected_scan, enabled),
            corrected=fresh_corrected,
        )
        state.cursor_end = self._held_by_unfinished_ask(pending, cursor_end)
        counts: dict[str, int] = {}
        for item in items:
            counts[item.signal] = counts.get(item.signal, 0) + 1
        state.signals_by_kind = counts
        return items

    def _held_by_unfinished_ask(
        self,
        pending: Mapping[str, Any] | None,
        cursor_end: SignalCursor | None,
    ) -> SignalCursor | None:
        """``cursor_end``, but never past the owner's earliest ask here that
        is still running (review, open question 2): the scans read finished
        asks only, so passing an earlier one that finishes later would lose
        it. The cursor stops ON that ask (inclusive); asks after it that this
        run already handled are scanned again next time and dropped by their
        fingerprints (plan appendix B18). ``pending`` is the in-window fence
        read before all scans; never refresh its status after sampling."""
        if cursor_end is None:
            return None
        if pending is None:
            return cursor_end
        created_at, ask_id = str(pending["created_at"]), str(pending["id"])
        return earlier_cursor(cursor_end, SignalCursor(created_at, ask_id, inclusive=True))

    def _neighbors(
        self,
        notebook_id: str,
        owner_id: str,
        items: Sequence[SignalItem],
        state: RunState,
    ) -> dict[str, list[dict[str, Any]]]:
        """One batched embedding (when the workload is bound and answers) and
        one neighbour read over every status; lexical only otherwise (G7)."""
        limit = int(self.settings.auto_memory_suggest_question_max_chars)
        texts = [_neighbor_text(item, limit) for item in items]
        vectors: list[list[float]] = []
        if self._configured(NEIGHBOR_EMBEDDING_WORKLOAD):
            try:
                embedded = self.models.embedding(NEIGHBOR_EMBEDDING_WORKLOAD).embed_texts(texts)
                vectors = [[float(value) for value in vector] for vector in embedded]
            except Exception as exc:
                _log.warning(
                    "memory suggestion neighbour embedding unavailable: %s",
                    type(exc).__name__,
                )
                vectors = []
            if len(vectors) != len(texts):
                vectors = []
        state.neighbors_vector = bool(vectors)
        rows = self.store.suggest_neighbors(
            owner_id,
            notebook_id,
            query_vectors=vectors,
            lexical_queries=texts,
            statuses=NEIGHBOR_STATUSES,
            k=NEIGHBOR_K,
        )
        return {
            item.item_id: list(rows[index]) if index < len(rows) else []
            for index, item in enumerate(items)
        }

    def _decide(
        self,
        items: Sequence[SignalItem],
        neighbors: Mapping[str, Sequence[Mapping[str, Any]]],
        state: RunState,
    ) -> dict[str, SuggestionDecision] | None:
        """The one chat call and its validation. ``None`` = the run ended
        here (``model_rejected`` / ``model_unavailable``); any other failure
        raises and the run ends ``failed``."""
        settings = self.settings
        prompt = memory_suggest_prompt(
            render_items(items, neighbors, RenderLimits.from_settings(settings)),
            title_max_chars=MEMORY_TITLE_MAX_CHARS,
            content_max_chars=SUGGESTION_CONTENT_MAX_CHARS,
            max_kept=int(settings.auto_memory_suggest_candidates_per_run),
        )
        try:
            raw = self.models.chat(MEMORY_SUGGEST_WORKLOAD).chat_json(
                [{"role": "user", "content": prompt}],
                MEMORY_SUGGEST_SCHEMA_HINT,
                max_tokens=int(settings.auto_memory_suggest_output_max_tokens),
            )
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code == _CODE_MALFORMED:
                state.model_status = MODEL_REJECTED
                state.end(OUTCOME_MODEL_REJECTED, code)
                return None
            if code == _CODE_NOT_CONFIGURED:
                state.model_status = MODEL_UNAVAILABLE
                state.end(OUTCOME_MODEL_UNAVAILABLE, REASON_MODEL_UNCONFIGURED)
                return None
            state.model_status = MODEL_ERROR
            raise
        served, confirmed = neighbor_id_sets(neighbors)
        try:
            parsed = parse_model_json_object(
                str(raw or ""), MEMORY_SUGGEST_SCHEMA_HINT, allow_repair=False
            )
            decisions = parse_suggest_reply(
                json.loads(parsed.content),
                {item.item_id for item in items},
                served,
                confirmed,
            )
        except ValueError as exc:
            # ``SuggestReplyRejected`` / ``ModelJsonRepairError`` carry a
            # stable ``reason``; a bare JSON error has none.
            state.model_status = MODEL_REJECTED
            state.end(
                OUTCOME_MODEL_REJECTED,
                str(getattr(exc, "reason", "") or type(exc).__name__),
            )
            return None
        state.model_status = MODEL_OK
        return decisions

    def _write(
        self,
        notebook_id: str,
        owner_id: str,
        claim_token: str,
        items: Sequence[SignalItem],
        decisions: Mapping[str, SuggestionDecision],
        now: str,
        current: datetime,
        state: RunState,
    ) -> None:
        """The drafts (at most ``candidates_per_run``, in item order) through
        ``create_suggestions`` -- whose transaction re-checks access, switch
        and claim even when there is nothing to write -- then the negative
        cache, in that same transaction (review P3-2: an exit or a switch-off
        that wins the locks leaves no fingerprint behind either).
        ``SuggestionRevoked`` ends the run ``skipped`` with nothing written at
        all."""
        cap = int(self.settings.auto_memory_suggest_candidates_per_run)
        kept = [
            (item, decisions[item.item_id])
            for item in items if decisions[item.item_id].writable
        ]
        chosen, over_cap = kept[:cap], kept[cap:]
        # An ascending item the model kept beyond the cap is not lost: the
        # cursor stops ON it (inclusive) and the next run sends it again
        # (review P3-1). Every draft written here is before it -- ascending
        # signals precede repeated ones -- and carries its stable fingerprint.
        held = next((item for item, _ in over_cap if item.signal in {SIGNAL_CLARIFIED, SIGNAL_CORRECTED}), None)
        if held is not None:
            state.cursor_end = earlier_cursor(
                state.cursor_end,
                SignalCursor(held.ask_created_at, held.ask_id, inclusive=True),
            )
        declined = [item for item in items if decisions[item.item_id].declined]
        bound_answers = [
            item.answer_id for item, _ in chosen
            if item.signal == SIGNAL_REPEATED and item.answer_id
        ]
        sources = (
            self.store.answer_projections(
                owner_id,
                bound_answers,
                excerpt_chars=1,
                clarification_chars=1,
                with_sources=True,
            )
            if bound_answers else {}
        )
        writes = [
            MemoryWrite(
                id=self.new_id("memory"),
                notebook_id=notebook_id,
                created_by=owner_id,
                origin="ask_answer",
                status="candidate",
                title=decision.title,
                content_md=decision.content,
                tags=list(decision.tags),
                created_at=now,
                updated_at=now,
                source_answer_id=(
                    item.answer_id if item.signal == SIGNAL_REPEATED and item.answer_id else None
                ),
                provenance=_provenance(item, decision, sources.get(item.answer_id)),
                suggested_signal=item.signal,
            )
            for item, decision in chosen
        ]
        proposed = list(dict.fromkeys(
            fingerprint for item, _decision in chosen for fingerprint in item.on_write
        ))
        declined_fingerprints = list(dict.fromkeys(
            fingerprint for item in declined for fingerprint in item.on_decline
        ))
        memory = self.memory
        if memory is None:
            raise RuntimeError("memory service is not wired")
        try:
            result = memory.create_suggestions(
                notebook_id=notebook_id,
                owner_id=owner_id,
                claim_token=claim_token,
                writes=writes,
                proposed_fingerprints=proposed,
                declined_fingerprints=declined_fingerprints,
                declined_expires_at=(
                    current + timedelta(days=int(self.settings.auto_memory_suggest_declined_ttl_days))
                ).isoformat(),
                now=now,
            )
        except SuggestionRevoked as exc:
            return state.end(OUTCOME_SKIPPED, exc.reason)
        state.declined_cached = len(declined)
        state.candidates_written = len(result.created)
        state.deduped += int(result.deduped)
        return state.end(OUTCOME_DONE)

    # ---------------------------------------------------------------- settle
    def _configured(self, workload: str) -> bool:
        configured = getattr(self.models, "configured", None)
        if not callable(configured):
            return False
        try:
            return bool(configured(workload))
        except Exception as exc:
            _log.warning("memory suggestion readiness check failed: %s", type(exc).__name__)
            return False

    def _finish(
        self,
        notebook_id: str,
        owner_id: str,
        claim_token: str,
        claimed: Mapping[str, Any],
        state: RunState,
        started: float,
    ) -> None:
        """Settle by the cursor table and emit the run event. A settle that
        fails (or finds the row gone / re-claimed) never replaces the run's
        own outcome: a ``running`` leftover falls to the startup sweep."""
        cursor, failures, gave_up = settle_plan(
            state.outcome, int(claimed.get("consecutive_failures") or 0), state.cursor_end
        )
        if gave_up:
            state.model_status = MODEL_GAVE_UP
        try:
            self.store.settle(
                notebook_id,
                owner_id,
                claim_token=claim_token,
                outcome=state.outcome,
                failure_reason=state.failure_reason,
                cursor=cursor,
                consecutive_failures=failures,
                consumed_pending=int(claimed.get("pending_signal") or 0),
                candidates_written=state.candidates_written,
                now=self.clock(),
            )
        except Exception as exc:
            _log.warning("memory suggestion settle failed: %s", type(exc).__name__)
        self._emit(notebook_id, owner_id, state, started)

    def _emit(
        self, notebook_id: str, owner_id: str, state: RunState, started: float
    ) -> None:
        """``memory_suggest_run``: exactly ``MEMORY_SUGGEST_RUN_EVENT_KEYS``
        (counts, codes and ids, design §6.4) besides the ``kind`` envelope."""
        try:
            self.event_log.emit({
                "kind": MEMORY_SUGGEST_RUN_EVENT,
                "notebook_id": notebook_id,
                "owner_id": owner_id,
                "outcome": state.outcome,
                "signals_by_kind": dict(state.signals_by_kind),
                "deduped": int(state.deduped),
                "declined_cached": int(state.declined_cached),
                "candidates_written": int(state.candidates_written),
                "model_status": state.model_status,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "neighbors_vector": bool(state.neighbors_vector),
            })
        except Exception as exc:
            _log.warning("memory suggestion event failed: %s", type(exc).__name__)


__all__ = [
    "MemorySuggestJob",
    "NEIGHBOR_EMBEDDING_WORKLOAD",
    "RunState",
    "settle_plan",
]
