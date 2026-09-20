"""Global source Ask ownership, authority, and detached execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

from app.core.ask_retrieval_policy import DEFAULT_RETRIEVAL_EFFORT
from app.models.ask import AskRequest, QueryIntentContract
from app.models.global_ask import (
    GlobalAskIntentPreviewRequest, GlobalAskJob, GlobalAskRequest,
    GlobalConversationDetail, GlobalNotebookScope, GLOBAL_ASK_PAGE_MAX,
    GLOBAL_ASK_PAGE_SIZE, GlobalAskSkippedNotebook, global_answer_citations,
)
from app.models.sources import SourceElement
from app.services.ask_followup import followup_resolution_context
from app.services.ask_modes import UnknownAskMode
from app.services.cancellation import AskCancelled, raise_if_cancelled
from app.services.federated_run import DetachedAskTurn, FederatedRunPlan
from app.services.global_run import global_ask_run
from app.services.retrieval_participants import ParticipantOverride


# Reasons a library produced no evidence, paired with the Chinese receipt the
# user reads. The copy never guesses a cause; the reason code is what travels
# in telemetry (see ``docs/operations.md``).
_SKIP_COPY = {
    # The phase budget ran out while this library was still queued: nothing
    # was asked of the database, so "select fewer notebooks" would be wrong.
    "queue_deadline": "检索未开始，请稍后重试。",
    "timeout": "检索超时，请缩小范围后重试。",
    "saturated": "检索未完成，请稍后重试。",
    "unavailable": "检索未完成，请稍后重试。",
}

# The one sentence a run says when its own citations no longer describe the
# library as it is now. Lifted to a constant because two places produce it: the
# evidence re-check below and the regression test that pins it.
_EVIDENCE_CHANGED_COPY = (
    "引用原文在回答期间发生了变化，暂时无法提供可靠结论，请重新提问。"
)
_UNKNOWN_ENGINE_COPY = "不支持的问答引擎，请刷新页面后重试。"
_PLUGIN_ENGINE_COPY = "全局问答暂不支持该引擎，请选择其他引擎后重试。"

# How often a running job's reasoning trace is rewritten into its row. Two
# bounds rather than one: the time bound keeps a slow run's panel moving, the
# step bound keeps a burst from waiting out the timer. Both are deliberately
# NOT settings -- they tune a write amplification, not a quality or cost budget
# an operator has any basis to choose (see ``_append_trace``).
_TRACE_SAVE_SECONDS = 0.5
_TRACE_SAVE_STEPS = 5

# Why a citation's evidence could not be attested. Content-free; the copy the
# user sees is the same one sentence in every case, but an operator has to be
# able to tell "the original changed" from "this citation carries no library
# at all", because the second is a normalization gap in the citation
# producers, not a race with an editing user.
_VOID_CHANGED = "changed"          # snapshot exists and no longer matches
_VOID_UNREADABLE = "unreadable"    # travelled the federated channel, unreadable
_VOID_UNATTRIBUTED = "unattributed"  # no notebook id: a D0 normalization gap
_VOID_OUT_OF_CEILING = "out_of_ceiling"  # source left the frozen ceiling


@dataclass(frozen=True)
class _PreparedIntentPreview:
    """Everything phase two of the intent preview needs, already authorized.

    Frozen and explicit rather than a tuple: it crosses from the request
    thread (where the authority check ran) to a worker thread, and a caller
    must not be able to widen the participant set between the two phases.
    """

    question: str
    nominal_active: str
    override: Any
    source_ceiling: Mapping[str, Any]
    turn: DetachedAskTurn


class _Absent:
    """'This element never travelled the federated chunk channel.'

    A sentinel rather than ``None``, because ``None`` is already a STATED
    value in the evidence table -- 'it did travel that channel and could
    not be fingerprinted' -- and the two mean opposite things to the
    citation re-check: absence is accepted under the ceiling check alone,
    ``None`` is refused.
    """

    __slots__ = ()


_ABSENT = _Absent()


class GlobalAskError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _now():
    return datetime.now(timezone.utc).isoformat()


class _AnyCancelled:
    """``is_set()`` over several cancellation sources, as one token.

    ``raise_if_cancelled`` and ``ReadBudget`` only ever ask a cancel token for
    ``is_set()``, so a composite satisfies the same contract without a second
    plumbing path. This is how one library's hard failure reaches the other
    libraries already executing on retrieval threads.
    """

    __slots__ = ("_tokens",)

    def __init__(self, *tokens):
        self._tokens = tuple(token for token in tokens if token is not None)

    def is_set(self):
        return any(token.is_set() for token in self._tokens)


def _ordered_insert(values, value, order, key=lambda item: item):
    """Insert into a list already sorted by ``order``, keeping it sorted.

    Progress rows are written as libraries finish, but a poller must never see
    the receipt order jump around: every intermediate save is a subsequence of
    ``resolved_notebook_ids``. Participant counts are capped at 8, so the
    linear rank lookup is cheaper than maintaining an index.
    """
    rank = order.index(key(value))
    for position, existing in enumerate(values):
        if order.index(key(existing)) > rank:
            values.insert(position, value)
            return
    values.append(value)


class _RunState:
    """ONE global run's cross-call aggregation. Never service state.

    ``GlobalAskService`` is a process singleton with several jobs in flight, so
    every field here has to belong to the run rather than to ``self``: an
    evidence map or a coverage set hung off the service would let one user's
    citation re-check read another user's fingerprints.

    WHY THE AGGREGATION LIVES HERE AND NOT IN THE FEDERATION
    --------------------------------------------------------
    ``chunk_federation`` reports one receipt per participant per FEDERATED
    CALL, and a reasoning run federates once per retrieval round. A library
    that answered round 1 and timed out in round 2 therefore produces two
    receipts, and only this class knows both. The fold is:

    * never answered in ANY round -> skipped, with the most recent round's
      reason code (the older one described a state the library may have left);
    * answered at least once, but some round failed or came back degraded ->
      searched AND degraded;
    * answered everywhere -> searched.

    ``lock`` is not decoration, and it is not only about the job thread. A
    reasoning run fans its sub-queries out over the engine's OWN pool, and each
    of those threads enters the federation independently -- so ``on_library``
    and ``on_evidence`` fire from SEVERAL threads at once, all folding into
    this one object and all persisting the same job row. Every read and write
    here is therefore under the lock, and ``coverage`` hands back a SNAPSHOT
    with a monotonic sequence number so two concurrent progress saves cannot
    land out of order and publish a coverage list that went backwards.
    """

    __slots__ = (
        "order", "evidence", "siblings", "succeeded", "failed", "degraded",
        "federated", "error", "lock", "publish_lock", "_sequence", "_published",
        "traced_at", "traced_at_count",
    )

    def __init__(self, order):
        self.order = list(order)
        # When the trace was last persisted, and how many steps were in it then.
        self.traced_at = 0.0
        self.traced_at_count = 0
        self.evidence: dict = {}
        # ``{element_id: {the other elements of every passage it appeared in}}``
        # -- see ``record_evidence_groups``.
        self.siblings: dict = {}
        self.succeeded: set = set()
        self.failed: dict = {}
        self.degraded: set = set()
        self.federated = False
        self.error: BaseException | None = None
        self.lock = threading.RLock()
        # A SECOND lock, held across "claim the snapshot, mutate the job row,
        # write it". ``lock`` guards the aggregation and must never be held
        # across a database write, or a receipt callback would block every
        # other federated call's callbacks for the length of that write. This
        # one is what makes the writes themselves serial, so a stale snapshot
        # cannot land after a fresher one even though both passed their claim.
        # ⛔ EVERY in-flight write of this job's row takes it, not only the
        # coverage ones: ``save_progress`` persists the whole row, so a trace
        # write outside this lock would be a second, unsequenced publisher of
        # the coverage fields (see ``_append_trace``).
        self.publish_lock = threading.Lock()
        self._sequence = 0
        self._published = 0

    def record(self, notebook_id, outcome) -> None:
        with self.lock:
            self.federated = True
            if outcome.skipped:
                self.failed[notebook_id] = outcome.reason
                return
            self.succeeded.add(notebook_id)
            if outcome.degraded:
                self.degraded.add(notebook_id)

    def record_evidence(self, mapping) -> None:
        """Merge one federated call's retrieval-time fingerprints.

        THREE STATES, per ``FederatedRunPlan.on_evidence``'s contract:

        * a ``(source_id, fingerprint)`` pair is the retrieval-time snapshot;
        * ``None`` states "this element came through the federated chunk
          channel and its fingerprint could not be read" -- a refusal, carried
          explicitly so a failed read fails closed element by element;
        * ABSENCE means the element never travelled that channel at all.

        Merging therefore has a direction. A real snapshot is authoritative and
        is never overwritten by a later ``None`` (rule 2 of the contract: the
        earliest snapshot in the run is a legitimate "before"), while a later
        successful read does replace a ``None``. Plain ``update`` would get
        this backwards and let one failed round refuse citations an earlier
        round had already attested.
        """
        if not mapping:
            return
        with self.lock:
            for element_id, snapshot in mapping.items():
                if snapshot is None and self.evidence.get(element_id) is not None:
                    continue
                self.evidence[element_id] = snapshot

    def record_evidence_groups(self, groups) -> None:
        """Merge one federated call's multi-element passages into the sibling map.

        Rule 4 of ``FederatedRunPlan.on_evidence``'s contract. A citation names
        ONE element -- ``evidence_context.chunk_citations`` publishes
        ``element_ids[0]`` -- while the passage the reader is shown, and the
        text the answer actually rested on, may span several. Without this map
        an edit to the third element of a five-element passage changes the
        quoted material and the re-check never looks at it.

        Every member of a group becomes every other member's sibling, and the
        union accumulates across rounds for the same reason the fingerprint
        table does: a passage selected in round 1 and again in round 3 is one
        passage, and a citation minted from either round rests on all of it.
        """
        if not groups:
            return
        with self.lock:
            for group in groups:
                members = tuple(dict.fromkeys(
                    element_id for element_id in group if element_id
                ))
                if len(members) < 2:
                    continue
                for element_id in members:
                    self.siblings.setdefault(element_id, set()).update(
                        other for other in members if other != element_id
                    )

    def evidence_snapshot(self) -> dict:
        """A copy the citation re-check can iterate while callbacks still fire."""
        with self.lock:
            return dict(self.evidence)

    def sibling_snapshot(self) -> dict:
        """The sibling map as SORTED tuples, for a deterministic re-check.

        Sorted rather than handed over as sets: two siblings of one citation
        can fail for two different reasons, and set iteration order varies with
        the interpreter's hash seed, so an unsorted map would let the same run
        be voided under ``changed`` in one process and ``unreadable`` in the
        next -- the operator-facing half of the void event is exactly what that
        distinction is for.
        """
        with self.lock:
            return {
                element_id: tuple(sorted(members))
                for element_id, members in self.siblings.items()
            }

    def fail(self, exc: BaseException) -> None:
        """Remember the FIRST failure raised inside a callback."""
        with self.lock:
            if self.error is None:
                self.error = exc

    def raise_first_error(self) -> None:
        """Re-raise a callback's failure on the owning thread.

        The callbacks run deep inside ``AskService.ask``, whose retrieval path
        is full of fail-soft handlers by design. A revoked authority raised
        there could therefore be degraded into "this library found nothing" and
        the run would deliver an answer anyway. Recording it and re-raising it
        here makes the outcome independent of whether any frame in between
        swallowed the raise.
        """
        with self.lock:
            error = self.error
        if error is not None:
            raise error

    def coverage(self):
        """``(sequence, searched, degraded, skipped_reasons)`` in resolved order.

        Built by ``_ordered_insert`` from sets, whose iteration order is
        unspecified: a poller must never see the receipts permute between two
        progress saves for reasons that have nothing to do with the run.

        The whole snapshot is taken under the lock and stamped with a
        monotonically increasing ``sequence``. Several federated calls run
        concurrently under a reasoning run, so two callbacks can be between
        "read the state" and "write the row" at the same time; without the
        stamp the slower writer could publish the older snapshot last and a
        poller would watch the coverage list shrink. ``publish`` is what
        enforces the ordering -- see it for why the check belongs there.

        A run that never federated at all -- a document overview or a
        collection enumeration answers straight from the participant-aware
        enumeration lanes without a single federated chunk call -- reports
        every resolved library as searched and none as skipped. Calling them
        skipped would tell the user to narrow a scope that was in fact read in
        full, and inventing a reason code for a query that was never issued is
        exactly what ``queue_deadline`` exists NOT to do.
        """
        with self.lock:
            self._sequence += 1
            sequence = self._sequence
            if not self.federated:
                return sequence, list(self.order), [], {}
            searched, degraded = [], []
            for notebook_id in self.succeeded:
                _ordered_insert(searched, notebook_id, self.order)
            # Answered SOMEWHERE and failed somewhere is degraded whichever way
            # round the two rounds happened: the library is in the answer, but
            # the answer was assembled from less than the library had.
            for notebook_id in self.degraded | (
                self.succeeded & set(self.failed)
            ):
                _ordered_insert(degraded, notebook_id, self.order)
            skipped = {
                notebook_id: reason
                for notebook_id, reason in self.failed.items()
                if notebook_id not in self.succeeded
            }
        return sequence, searched, degraded, skipped

    def claim_publish(self, sequence) -> bool:
        """May a writer holding ``sequence`` still publish it?

        False once a NEWER snapshot has been published. The check and the
        bookkeeping are one atomic step, which is the whole point: two
        concurrent receipt callbacks otherwise interleave as "both check, both
        pass, the older one writes last" and the persisted coverage goes
        backwards -- a library disappears from ``searched`` and then comes
        back, which a poller renders as a run losing ground.
        """
        with self.lock:
            if sequence <= self._published:
                return False
            self._published = sequence
            return True


# Name prefix of the SHARED retrieval pool's threads. The job thread must not
# be one of them -- see ``_execute``.
_RETRIEVAL_THREAD_PREFIX = "global-ask-retrieve"
# Fields that ride along with a request without identifying it; see
# ``GlobalAskService._same_request``.
_REQUEST_IDENTITY_EXCLUDES = frozenset({"intent"})


class GlobalAskService:
    def __init__(self, *, store, notebooks, can_read, sources, settings, ask=None,
                 can_read_many=None, event_log=None):
        self.store = store
        self.notebooks = notebooks
        self.can_read = can_read
        self.can_read_many = can_read_many
        self.sources = sources
        # The ONE single-library engine. A global run is the same engine under
        # a participant override, not a second retrieval and synthesis stack.
        self.ask = ask
        self.settings = settings
        # Optional content-free sink. Skips decided here (never queued before
        # the phase budget ran out) never reach the retrieval module's own
        # emitter, so they would otherwise be the one skip with no receipt.
        self.event_log = event_log
        self._lock = threading.RLock()
        self._events = {}
        self._workers = {}
        self._pending = 0
        self._closed = False
        # Monotonic clock, injectable so the trace throttle can be tested with
        # a handshake instead of a wall-clock sleep.
        self._clock = time.monotonic
        # How many FEDERATED CALLS are in flight across the whole process --
        # not how many jobs. See ``_retrieval_window``.
        self._federated_calls = 0
        # ONE retrieval pool per service instance, shared by every job it runs.
        # The runtime composes exactly one service per process, which is what
        # makes ``global_ask_retrieval_concurrency`` a real upper bound on
        # retrieval-held database connections and lets the startup pool budget
        # add it to ``global_ask_max_concurrent``. Per-job pools would instead
        # multiply by the job capacity and blow through the PostgreSQL pool
        # (default max size 10).
        self._retrieval_pool = ThreadPoolExecutor(
            max_workers=max(1, int(settings.global_ask_retrieval_concurrency)),
            thread_name_prefix=_RETRIEVAL_THREAD_PREFIX,
        )

    def _check(self, ids, user_id, allowed_notebook_ids=None, authority_check=None):
        ids = set(ids)
        allowed = None if allowed_notebook_ids is None else set(allowed_notebook_ids)
        if authority_check is not None:
            try:
                current = authority_check()
            except Exception as exc:
                raise GlobalAskError(403, "访问权限已变化，请重新连接后重试。") from exc
            if current is not None:
                allowed = set(current) if allowed is None else allowed.intersection(current)
        if allowed is not None and not ids.issubset(allowed):
            raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")
        readable = (set(self.can_read_many(sorted(ids), user_id)) if self.can_read_many else
                    {notebook_id for notebook_id in ids if self.can_read(notebook_id, user_id)})
        if not ids.issubset(readable):
            raise GlobalAskError(404, "部分笔记本已无法访问，请重新选择范围。")

    def _conversation(self, conversation_id, user_id):
        row = self.store.conversation(conversation_id, user_id)
        if row is None:
            raise GlobalAskError(404, "对话不存在，请刷新列表。")
        return row

    def _save_if_open(self, job, user_id, *, progress=False):
        """No detached worker may reopen persistence after runtime shutdown.

        ⛔ THE LOCK GUARDS THE FLAG, NOT THE WRITE. ``self._lock`` is the
        process-level lock ``_retrieval_window`` and admission also take, so
        holding it across a database write makes one job's slow write block
        every other job's top-up loop -- while those jobs' per-library budgets
        keep running. The flag is read under the lock and the write happens
        outside it.

        The resulting window is one row: a shutdown that flips ``_closed``
        between the read and the write lets that one write through. It cannot
        resurrect a job, because the durable guard is in the statement itself
        -- both store writes are ``WHERE ... status='running'`` and report
        their ``rowcount``, so a job the shutdown (or a cancellation, or a
        deletion) has already moved out of ``running`` matches no row and this
        returns False exactly as it did before.
        """
        with self._lock:
            if self._closed:
                return False
        return self.store.save_progress(job, user_id) if progress else self.store.save(job, user_id)

    def _history(self, conversation, ids, user_id, allowed, authority_check):
        """``(history, user_history)`` in the shapes the engine already speaks.

        Byte-for-byte the projection ``AskStateStore._conversation_histories``
        builds for a notebook conversation -- ``User: …`` / ``Assistant: …``
        pairs, oldest first, plus the question-only half. The engine's prompt
        blocks and its follow-up gate both read these, so a global conversation
        that rendered its turns differently would hand the same model two
        dialects of the same block depending on which entry point asked.
        """
        history, questions = [], []
        if conversation and self.settings.global_ask_history_turns:
            turns = self.store.completed_history(conversation.id, user_id, self.settings.global_ask_history_turns)
            admitted = [turn for turn in turns if set(turn["notebook_ids"]).issubset(ids)]
            if admitted:
                self._check({notebook_id for turn in admitted for notebook_id in turn["notebook_ids"]},
                            user_id, allowed, authority_check)
            for turn in reversed(admitted):
                history.append("User: " + turn["question"] + "\nAssistant: " + turn["answer"])
                questions.append("User: " + turn["question"])
        return "\n".join(history), "\n".join(questions)

    def _resolve_mode(self, mode):
        """The engine this request names, or the Chinese reason it cannot run.

        Resolution is the engine's own registry -- retired aliases included --
        so a stale tab that still says ``fast`` normalizes exactly as it does
        on the single-library entry point instead of 422-ing here alone.

        Deployment engines are refused outright. ``ask_plugin_engine`` keeps
        answering through the real mount predicate (its ``source_keys``
        universe is also the authority its ``fetch()`` is checked against), and
        what that means under a participant override is undefined -- so the
        global entry point declines rather than inventing a semantics.
        """
        try:
            spec = self.ask._resolve_ask_mode(mode)
        except UnknownAskMode as exc:
            raise GlobalAskError(422, _UNKNOWN_ENGINE_COPY) from exc
        if spec.handler == "ask_plugin_engine":
            raise GlobalAskError(422, _PLUGIN_ENGINE_COPY)
        return spec

    def _reasoning_preflight(self, spec, notebook_id, payload, user_history):
        """Freeze a reasoning submission BEFORE any durable row exists.

        Same two steps, in the same order, as the single-library entry point
        runs above ``begin_job_current``: a submitted intent is validated, and
        an elliptical follow-up gets one chance to resolve against this
        member's own prior questions. Both must happen before ``store.create``,
        or a rejected question leaves a job row and a conversation behind.

        The history is handed in rather than read: a global conversation's
        turns live in this service's own store, and ``ask_state`` holds none of
        them, so the engine's default read would always come back empty and
        every follow-up would 422.
        """
        if spec.id != "reasoning":
            return None
        try:
            self.ask.validate_reasoning_submission(notebook_id, payload)
        except ValueError as exc:
            # Complete user copy by contract (see ``validate_confirmed_intent``).
            raise GlobalAskError(422, str(exc)) from exc
        resolution = self.ask.resolve_reasoning_followup(
            notebook_id, payload, history=user_history,
        )
        if resolution.gate_message:
            raise GlobalAskError(422, resolution.gate_message)
        return resolution

    @staticmethod
    def _same_request(stored_json: str, request_json: str) -> bool:
        """Is a stored request the same submission as this one?

        Normalize the STORED string through today's model before comparing.
        A raw string comparison makes every new request field a retroactive
        idempotency break: a job submitted before the field existed was stored
        without it, so the client's honest retry of the very same question
        under the very same ``client_request_id`` would be rejected as "this id
        already belongs to another question" -- and the client cannot fix that,
        because the difference is a field its old payload never had.

        An unparsable stored string falls back to the literal comparison: it
        was not written by this model, so normalizing it is not possible and
        treating it as a match would be a guess.

        The confirmed ``intent`` is NOT part of a request's identity. It is
        derived from the question by a model call the client repeats on every
        retry (the browser re-runs the preview, MCP re-runs it inside the same
        tool call), so two honest submissions of one question under one
        ``client_request_id`` carry different confirmations -- a different
        ``understanding_ms`` at the very least. Comparing it turned the retry
        that idempotency exists for into a permanent 409. What identifies the
        request is what the user chose: question, scope, conversation, engine
        and effort.
        """
        try:
            stored = GlobalAskRequest.model_validate_json(stored_json)
            current = GlobalAskRequest.model_validate_json(request_json)
        except Exception:  # noqa: BLE001 - see docstring
            return stored_json == request_json
        return (stored.model_dump(exclude=_REQUEST_IDENTITY_EXCLUDES)
                == current.model_dump(exclude=_REQUEST_IDENTITY_EXCLUDES))

    def _resolve_run_scope(self, payload, user_id, allowed_notebook_ids, authority_check):
        """The scope ``start()`` and ``preview_intent`` must resolve IDENTICALLY.

        Byte-for-byte the block ``start()`` used to run inline: same
        conversation lookup, same scope defaulting, same participant-count
        ceiling, same two authority re-checks around the source-ceiling
        freeze, same history projection. Pulled out so a preview and the
        submission it precedes cannot drift onto two library sets from the
        same inputs -- a preview that resolved a different set than the run
        it previews would hand the user a confirmation that does not describe
        what actually gets asked.

        Returns ``(conversation, scope, ids, allowed, source_ceiling, history,
        user_history)``.
        """
        conversation = self._conversation(payload.conversation_id, user_id) if payload.conversation_id else None
        scope = payload.notebook_scope or (conversation.notebook_scope if conversation else GlobalNotebookScope())
        rows = self.notebooks(user_id)
        names = rows if isinstance(rows, dict) else {row.id: row.name for row in rows}
        allowed = None if allowed_notebook_ids is None else frozenset(allowed_notebook_ids)
        ids = list(scope.notebook_ids) if scope.mode == "include" else [
            key for key in names if allowed is None or key in allowed
        ]
        if not ids:
            raise GlobalAskError(422, "没有可访问的笔记本，请先创建笔记本并添加资料。")
        if len(ids) > self.settings.global_ask_max_notebooks:
            ceiling = self.settings.global_ask_max_notebooks
            # ``all`` resolved past the ceiling: the caller (an MCP client
            # above all) cannot narrow a scope it never sent, so the message
            # has to say that an explicit selection is the way out.
            raise GlobalAskError(422, (
                f"可访问的笔记本超过 {ceiling} 个，请选择不超过 {ceiling} 个笔记本后重试。"
                if scope.mode != "include" else
                f"本次范围超过 {ceiling} 个笔记本，请选择不超过 {ceiling} 个笔记本后重试。"
            ))
        self._check(ids, user_id, allowed, authority_check)
        # Freeze every source ceiling before starting any retrieval or detached work.
        source_rows = self.sources.visible_source_ids_by_notebook(ids)
        source_ceiling = {notebook_id: frozenset(source_rows[notebook_id]) for notebook_id in ids}
        self._check(ids, user_id, allowed, authority_check)
        history, user_history = self._history(
            conversation, ids, user_id, allowed, authority_check,
        )
        return conversation, scope, ids, allowed, source_ceiling, history, user_history

    def prepare_intent_preview(self, payload: GlobalAskIntentPreviewRequest, *,
                                user_id, allowed_notebook_ids=None,
                                authority_check=None):
        """Phase ONE of the intent preview: resolve scope and re-check authority.

        Split out from the model call for one reason: everything in here can
        fail with a PRECISE, user-facing status (404 for a conversation that is
        not this member's, 422 for an empty or over-wide scope, 404 for access
        revoked since the page loaded), and those statuses only survive if the
        step runs BEFORE a streaming response has begun. Once the first NDJSON
        frame is out the HTTP status is already 200 and every refusal degrades
        into a content-free ``error`` frame -- which is exactly what the
        streaming endpoint used to do to all of them.

        The blocking endpoint runs the same two phases, so the two transports
        cannot drift on what they refuse or on what they say.

        Read-only: no job row, no conversation row, no store write. Resolution
        is ``_resolve_run_scope``, the identical block ``start()`` uses, so a
        preview and the submission it precedes cannot land on two library sets
        from the same inputs.
        """
        conversation, _scope, ids, _allowed, source_ceiling, full_history, user_history = (
            self._resolve_run_scope(payload, user_id, allowed_notebook_ids, authority_check)
        )
        return _PreparedIntentPreview(
            question=payload.question,
            nominal_active=ids[0],
            override=ParticipantOverride(
                notebook_ids=tuple(ids), tiers={}, attested_actor_id=user_id,
            ),
            source_ceiling=source_ceiling,
            turn=DetachedAskTurn(
                conversation_id=(
                    conversation.id if conversation else "gconv-" + uuid4().hex
                ),
                history=full_history, user_history=user_history,
            ),
        )

    def run_intent_preview(self, prepared, *, cancel_event=None) -> QueryIntentContract:
        """Phase TWO: the understanding pass, under a real global-run context.

        Installs the SAME four facts ``_execute`` installs for a real run: the
        participant override over the resolved libraries, their just-frozen
        source ceilings, the detached turn, and a ``FederatedRunPlan`` whose
        callbacks are never invoked (see ``_preview_federated_plan``).

        That last part is defensive rather than load-bearing today:
        ``AskService.preview_reasoning_intent`` is documented CORPUS-BLIND
        (``AskExecutionPort.preview_reasoning_intent``'s docstring says so
        verbatim) -- it hands the question and history straight to
        ``plan_query_intent``, which never reads a notebook, a mount table or
        the participant override, so the peer-mode context this installs is
        never actually read on this path. Installing it anyway is what keeps
        that true by CONSTRUCTION rather than by one function's current
        implementation: if the understanding step ever grows a corpus-aware
        step, it inherits the resolved participant set for free instead of
        silently reading the nominal active's own mount table.
        """
        plan = self._preview_federated_plan(cancel_event or threading.Event())
        with global_ask_run(
            prepared.override, prepared.source_ceiling, prepared.turn, plan,
            nominal_active=prepared.nominal_active,
        ):
            return self.ask.preview_reasoning_intent(
                prepared.nominal_active, prepared.question,
                prepared.turn.user_history, cancel_event=cancel_event,
            )

    def preview_intent(self, payload: GlobalAskIntentPreviewRequest, *, user_id,
                        allowed_notebook_ids=None, authority_check=None,
                        cancel_event=None) -> QueryIntentContract:
        """Both phases, for callers that are not streaming (MCP, tests).

        Kept as one method so the two-phase split cannot become two different
        behaviours; the HTTP endpoints call the phases separately only so the
        first one's status codes reach the client.
        """
        prepared = self.prepare_intent_preview(
            payload, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids,
            authority_check=authority_check,
        )
        return self.run_intent_preview(prepared, cancel_event=cancel_event)

    def _preview_federated_plan(self, cancel_event) -> FederatedRunPlan:
        """A ``FederatedRunPlan`` whose callbacks are never invoked.

        ``global_ask_run`` requires one of the four facts it installs
        all-or-nothing (see its docstring), but ``preview_intent`` never makes
        a federated call -- see ``preview_intent``'s own docstring for why.
        This exists only so the plan can be installed in the same shape
        ``_execute`` installs it in, borrowing the same shared retrieval pool
        and fair-window accessor a real run would (never entered as an
        executor, so borrowing it costs nothing) rather than fabricating a
        second, untested plan shape for the preview path alone.
        """
        return FederatedRunPlan(
            phase_timeout_seconds=float(self.settings.global_ask_retrieval_timeout_seconds),
            notebook_timeout_seconds=float(self.settings.global_ask_notebook_timeout_seconds),
            executor=self._retrieval_pool,
            window=self._retrieval_window,
            cancel=cancel_event,
            on_library=lambda *_args: None,
            on_evidence=lambda *_args: None,
            on_evidence_groups=lambda *_args: None,
            call_scope=self._federated_call,
        )

    def replay(self, payload: GlobalAskRequest, *, user_id,
               allowed_notebook_ids=None, authority_check=None):
        """The job this ``client_request_id`` already created, or ``None``.

        Public so a transport that has to SPEND something before it can call
        ``start`` -- MCP runs the reasoning preview, a model call, inside the
        same tool call -- can recognise a retry first and return the existing
        job instead of paying for a second understanding it would then throw
        away. Same authority re-check as ``start``: a replay hands back a job's
        content, so it is refused once any of its libraries became unreadable.
        """
        previous = self.store.request_job(user_id, payload.client_request_id)
        if previous is None:
            return None
        job, old_request = previous
        if not self._same_request(old_request, payload.model_dump_json()):
            raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。")
        self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids, authority_check)
        return job

    def start(self, payload: GlobalAskRequest, *, user_id, allowed_notebook_ids=None,
              submitted_via="web", authority_check=None):
        spec = self._resolve_mode(payload.mode)
        request_json = payload.model_dump_json()
        replayed = self.replay(
            payload, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids,
            authority_check=authority_check,
        )
        if replayed is not None:
            return replayed
        with self._lock:
            if self._closed:
                raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
            if len(self._workers) + self._pending >= self.settings.global_ask_max_concurrent:
                raise GlobalAskError(429, "全局问答正在处理其他任务，请稍后重试。")
            self._pending += 1
        try:
            conversation, scope, ids, allowed, source_ceiling, history, user_history = (
                self._resolve_run_scope(payload, user_id, allowed_notebook_ids, authority_check)
            )
            job = GlobalAskJob(
                job_id="gask-" + uuid4().hex,
                conversation_id=conversation.id if conversation else "gconv-" + uuid4().hex,
                status="running", question=payload.question, created_at=_now(),
                notebook_scope=scope, resolved_notebook_ids=ids,
                mode=spec.id,
                # ⛔ CLAMPED, not carried. ``deep`` multiplies the reasoning
                # round ceiling, and every round federates over the whole
                # participant set: eight libraries at the deep round count does
                # not fit inside ``global_ask_retrieval_timeout_seconds``, so
                # the run would spend its budget and report most of its
                # libraries skipped. v1 therefore answers a ``deep`` request at
                # ``standard`` rather than accepting a level it cannot honour.
                retrieval_effort=DEFAULT_RETRIEVAL_EFFORT,
            )
            followup = self._reasoning_preflight(
                spec, ids[0], self._ask_payload(job, payload.intent), user_history,
            )
            event = threading.Event()
            context = copy_context()
            worker = threading.Thread(
                target=lambda: context.run(
                    self._run, job.model_copy(deep=True), user_id, history,
                    user_history, event, allowed, authority_check, source_ceiling,
                    followup, payload.intent,
                ),
                daemon=True, name="global-ask",
            )
            created = False
            try:
                with self._lock:
                    if self._closed:
                        raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
                # OUTSIDE the lock: this is a database write, and the lock is
                # also what ``_retrieval_window`` takes on every top-up of
                # every RUNNING job. Holding it here makes one submission's
                # insert stall every in-flight run's fan-out. The admission
                # bound is already reserved by ``_pending`` above, so nothing
                # here depends on the insert being inside it.
                self.store.create(
                    job, user_id, payload.client_request_id, request_json, submitted_via,
                    new_conversation=conversation is None,
                )
                created = True
                with self._lock:
                    # Re-read: a shutdown may have landed while the row was
                    # being written. ``created`` is already true, so the
                    # handler below turns the row terminal instead of leaving
                    # a running job with no worker behind it.
                    if self._closed:
                        raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
                    self._events[job.job_id] = event
                    self._workers[job.job_id] = worker
                    worker.start()
            except Exception as exc:
                if created:
                    with self._lock:
                        self._events.pop(job.job_id, None)
                        self._workers.pop(job.job_id, None)
                    job.status = "failed"
                    job.error = "任务未能启动，请重新提交问题。"
                    self.store.save(job, user_id)
                    raise
                previous = self.store.request_job(user_id, payload.client_request_id)
                if previous is not None:
                    # The SAME comparison the fast path above uses: a literal
                    # string compare here would make the race-recovery branch
                    # reject a legacy-shaped row the fast path accepts.
                    if not self._same_request(previous[1], request_json):
                        raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。") from exc
                    self._check(previous[0].resolved_notebook_ids, user_id, allowed, authority_check)
                    return previous[0]
                if conversation and self.store.running_job_ids(conversation.id, user_id):
                    raise GlobalAskError(409, "这段对话仍在回答，请等待完成或停止后重试。") from exc
                raise
            return job
        finally:
            with self._lock:
                self._pending -= 1

    @contextmanager
    def _federated_call(self):
        """Count ONE federated call for as long as it is in flight.

        The federation enters this around each whole fan-out (see
        ``FederatedRunPlan.call_scope``), so the counter is the number of
        fan-outs competing for the shared pool right now. Decremented in
        ``finally``: a call that dies of an expired phase, a cancellation or an
        attestation failure must give its share back, or the window shrinks
        permanently for the rest of the process's life.
        """
        with self._lock:
            self._federated_calls += 1
        try:
            yield
        finally:
            with self._lock:
                self._federated_calls = max(0, self._federated_calls - 1)

    def _retrieval_window(self):
        """How many legs ONE federated call may keep in flight right now.

        The retrieval pool is shared, so a fan-out that submitted all of its
        legs at once would own every execution slot and every other fan-out
        would sit in the FIFO queue until its own phase budget expired -- that
        questioner got zero searched libraries and an answer with no evidence.

        The divisor is the number of IN-FLIGHT FEDERATED CALLS, not the number
        of live jobs, and that distinction is the whole point. A reasoning job
        fans its sub-queries out over the engine's own pool, and every one of
        those threads enters the federation independently: one job, eight
        sub-queries and eight libraries is up to 64 legs queued behind however
        many workers the shared pool has. Dividing by "live jobs" hands that
        single job the entire pool and the other 60-odd legs expire on the
        phase deadline -- and the user is told "检索未开始", which describes a
        contention problem as a scope problem.

        The share is recomputed on every top-up, so a call that started alone
        gives ground back as soon as another one begins: a newcomer waits at
        most one leg, never a whole phase.
        """
        with self._lock:
            calls = max(1, self._federated_calls)
        return max(1, int(self.settings.global_ask_retrieval_concurrency) // calls)

    def _emit(self, event):
        """Fail-open content-free telemetry; observability never fails a run."""
        if self.event_log is None:
            return
        try:
            self.event_log.emit(event)
        except Exception:  # noqa: BLE001 - see docstring
            pass

    @staticmethod
    def _ask_payload(job, intent=None):
        """The single-library request a global run answers.

        Both scope dimensions are UNSUBMITTED rather than empty: a run over a
        participant set has no checkbox list of its own, and an explicit empty
        list would freeze into the scope payloads and start excluding the very
        libraries the override installed. ``conversation_id`` is None because
        the conversation is handed down as a detached turn -- passing the
        global conversation id would send the engine looking for a
        ``conversations`` row that does not exist in ``ask_state``.
        """
        return AskRequest(
            question=job.question, mode=job.mode, intent=intent,
            retrieval_effort=job.retrieval_effort,
            conversation_id=None, source_scope=None, base_scope=None,
        )

    def _on_library(self, job, user_id, state, allowed, authority_check, phase,
                    notebook_id, outcome):
        """One library's receipt, on the thread that made the federated call.

        This is the run's MID-RETRIEVAL authority re-check, and it is the same
        check the serial fan-out used to do inside each library's retrieval: a
        share revoked while eight libraries were being searched must fail the
        whole request, not quietly drop one library's evidence and answer from
        the rest.

        Two things happen on failure, and both are needed:

        * the phase token is set, so the legs already executing stop at their
          next budget check instead of holding a retrieval slot for a run that
          is already lost;
        * the exception is RECORDED before it is raised. The raise leaves this
          frame into the retrieval path, which is full of fail-soft handlers by
          design; the recording is what makes the outcome independent of
          whether one of them swallows it (see ``_RunState.raise_first_error``).
        """
        try:
            self._check([notebook_id], user_id, allowed, authority_check)
            state.record(notebook_id, outcome)
            self._publish_coverage(job, user_id, state, progress=True)
        except BaseException as exc:
            state.fail(exc)
            phase.set()
            raise

    def _publish_coverage(self, job, user_id, state, *, progress):
        """Rebuild the durable coverage lists and save them, in snapshot order.

        Serial per run and ordered by sequence: a reasoning run federates from
        several sub-query threads at once, so without both properties two
        receipts can interleave into a persisted coverage list that loses a
        library it had already reported.
        """
        sequence, searched, degraded, skipped = state.coverage()
        with state.publish_lock:
            if not state.claim_publish(sequence):
                return
            job.searched_notebook_ids = searched
            job.degraded_notebook_ids = degraded
            job.skipped_notebooks = [
                GlobalAskSkippedNotebook(
                    notebook_id=notebook_id,
                    # An unknown code must still say something: an empty note
                    # renders as a library listed with no reason at all.
                    reason=_SKIP_COPY.get(
                        skipped[notebook_id], _SKIP_COPY["unavailable"],
                    ),
                )
                for notebook_id in job.resolved_notebook_ids
                if notebook_id in skipped
            ]
            open_for_writing = self._save_if_open(job, user_id, progress=progress)
        if not open_for_writing:
            raise AskCancelled()

    def _append_trace(self, job, user_id, state, step):
        """Stream one reasoning step into the job a poller is reading.

        Fail-OPEN in full, like the single-library ``append_trace_fail_open``:
        a trace is how the user watches a run, and losing a step -- or losing
        the storage the step was going to -- must never be the reason an
        otherwise good answer fails.

        THROTTLED, unlike the notebook-scoped path, which is append-only: it
        INSERTs one row per step into a child table, so its cost per step is
        constant. A global job has no child table; its trace lives inside the
        job's single JSON payload, so persisting every step rewrites the whole
        list and the run costs O(n²) bytes in the number of steps. Steps still
        enter memory immediately -- the throttle only decides when the row is
        rewritten -- and the terminal write always happens, so what the
        finished turn contains is not affected. ``_TRACE_SAVE_*`` bound the two
        ways a poller can fall behind: elapsed time and accumulated steps.

        ⛔ THE WRITE GOES THROUGH ``publish_lock``, like every other progress
        write. ``save_progress`` persists the WHOLE job row, coverage lists
        included, so this is not a trace-only write however it reads here: a
        reasoning run appends trace steps from the same threads that are
        federating, and a trace write that serialized the row while a receipt
        thread was mid-publish could commit an older coverage list after a
        newer one -- a poller would watch a library it had already reported
        disappear, and a cancellation right afterwards would leave the stale
        list durable. Holding the publish lock across "serialize and write"
        makes that impossible without a sequence of its own: the coverage
        fields on ``job`` are only ever mutated under this same lock and only
        after ``claim_publish``, so whatever this write serializes is by
        construction the newest claimed snapshot. ``state.lock`` is still
        released first -- no database write happens under it (see
        ``_publish_coverage``), and taking the two in this order everywhere
        keeps them ordered publish-then-state.
        """
        try:
            with state.lock:
                job.trace.append(step)
                now = self._clock()
                due = (
                    len(job.trace) - state.traced_at_count >= _TRACE_SAVE_STEPS
                    or now - state.traced_at >= _TRACE_SAVE_SECONDS
                )
                if due:
                    state.traced_at = now
                    state.traced_at_count = len(job.trace)
            if due:
                with state.publish_lock:
                    self._save_if_open(job, user_id, progress=True)
        except Exception:  # noqa: BLE001 - see docstring
            pass

    def _run(self, job, user_id, history, user_history, event, allowed,
             authority_check, source_ceiling, followup=None, intent=None):
        try:
            self._execute(job, user_id, history, user_history, event, allowed,
                          authority_check, source_ceiling, followup, intent)
        except AskCancelled:
            job.status, job.response, job.answer = "cancelled", None, None
            self._save_if_open(job, user_id)
        except Exception as exc:
            job.status, job.response, job.answer = "failed", None, None
            # Only copy that was written to be read by a user reaches the row;
            # an upstream exception's text may carry SQL, a path or source
            # content, and never becomes an error message.
            job.error = (
                exc.message if isinstance(exc, GlobalAskError)
                else "回答未完成，请检查模型服务和资料状态后重试。"
            )
            self._save_if_open(job, user_id)
        finally:
            with self._lock:
                self._events.pop(job.job_id, None)
                self._workers.pop(job.job_id, None)

    def _execute(self, job, user_id, history, user_history, event, allowed,
                 authority_check, source_ceiling, followup, intent):
        """One global turn, answered by the single-library engine.

        ⛔ THIS MUST NOT RUN ON ``self._retrieval_pool``. The federation calls
        ``wait()`` on that pool from whichever thread it was called on, so a
        job thread that was itself one of the pool's workers would block a slot
        waiting for slots -- a classic pool deadlock, and with
        ``global_ask_retrieval_concurrency`` jobs in flight it is not a rare
        interleaving but the steady state. The job thread is created in
        ``start`` as a plain ``Thread``; the assertion below is what keeps a
        later "let's reuse the pool" refactor from turning that into a hang.

        The five authority re-checks of the serial pipeline all survive, in the
        same places relative to what they protect: at the start of the run,
        once per library as its receipt arrives (``_on_library``), after the
        answer exists, after the citations have been re-checked, and once more
        immediately before the answer is persisted.
        """
        if threading.current_thread().name.startswith(_RETRIEVAL_THREAD_PREFIX):
            # NOT an ``assert``: ``python -O`` drops those, and this one guards
            # a deadlock rather than a typo.
            raise RuntimeError(
                "the global job thread must not be a shared retrieval-pool worker"
            )
        state = _RunState(job.resolved_notebook_ids)
        raise_if_cancelled(event)
        self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
        # Built here, AFTER the authority check it attests to and never before:
        # ``attested_actor_id`` means "the user whose ``can_read_many`` just
        # passed over exactly these libraries".
        override = ParticipantOverride(
            notebook_ids=tuple(job.resolved_notebook_ids),
            # Unstated: these are the user's own notebooks, and ``pairs()``
            # defaults to the conservative ``personal``. Claiming ``base``
            # would describe a private notebook as a published reference.
            tiers={},
            attested_actor_id=user_id,
        )
        # One token for "the user cancelled" OR "this run gave up", so a leg
        # only ever has to answer ``is_set()``.
        phase = threading.Event()
        plan = FederatedRunPlan(
            phase_timeout_seconds=float(self.settings.global_ask_retrieval_timeout_seconds),
            notebook_timeout_seconds=float(self.settings.global_ask_notebook_timeout_seconds),
            executor=self._retrieval_pool,
            window=self._retrieval_window,
            cancel=_AnyCancelled(event, phase),
            on_library=lambda notebook_id, outcome: self._on_library(
                job, user_id, state, allowed, authority_check, phase,
                notebook_id, outcome,
            ),
            on_evidence=state.record_evidence,
            on_evidence_groups=state.record_evidence_groups,
            # The fair share is per FAN-OUT, and a reasoning run has many in
            # flight at once. See ``_retrieval_window``.
            call_scope=self._federated_call,
        )
        try:
            with global_ask_run(
                override,
                # TOTAL map over the participant set, the nominal active
                # included and an empty visible list written as ``frozenset()``
                # rather than skipped -- see ``global_run``'s module docstring
                # for what a partial map splits open.
                {
                    notebook_id: frozenset(source_ceiling[notebook_id])
                    for notebook_id in job.resolved_notebook_ids
                },
                DetachedAskTurn(
                    conversation_id=job.conversation_id,
                    history=history,
                    user_history=user_history,
                ),
                plan,
                nominal_active=job.resolved_notebook_ids[0],
            ), followup_resolution_context(followup):
                response = self.ask.ask(
                    job.resolved_notebook_ids[0],
                    self._ask_payload(job, intent),
                    user_id=user_id,
                    job_id=job.job_id,
                    # The job's OWN event, not ``plan.cancel``. The engine's
                    # reasoning stage boundary asserts its cancellation handle
                    # is a real ``threading.Event`` (``_assert_reasoning_
                    # runtime``), and the composite token is not one -- passing
                    # it made every reasoning run fail at the stage boundary
                    # before it retrieved anything. The composite is the
                    # FEDERATION's token and still reaches the legs through
                    # ``plan.cancel``; it carries this event, so a user
                    # cancellation reaches both sides. The other half -- a
                    # revoked share raised inside a receipt callback -- reaches
                    # the legs through ``phase`` and reaches the job through
                    # ``_RunState.raise_first_error``.
                    cancel_event=event,
                    on_trace=lambda step: self._append_trace(
                        job, user_id, state, step,
                    ),
                )
        except (Exception, AskCancelled):
            # A callback's failure outranks whatever reached this frame: a
            # cancellation raised because the phase token was set by a revoked
            # share is that revocation, not a user pressing stop, and the
            # SECURITY fact is the one the terminal state must describe.
            #
            # Deliberately NOT ``BaseException``: a ``KeyboardInterrupt`` or a
            # ``SystemExit`` is the interpreter going away, and rewriting it
            # into "your share was revoked" would both mislead and delay the
            # shutdown by whatever the rewritten exception's handling costs.
            state.raise_first_error()
            raise
        state.raise_first_error()
        raise_if_cancelled(event)
        self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
        void_reason = self._validate_citations(
            response.citations, state.evidence_snapshot(), source_ceiling, event,
            siblings=state.sibling_snapshot(),
        )
        if void_reason:
            # VOIDED WHOLE, never patched. Dropping the dead markers would
            # leave the claims that depended on them standing, and re-running
            # the engine would re-retrieve and re-freeze everything -- a
            # different answer wearing this one's identity.
            #
            # The user always reads the same sentence, but the REASON travels
            # in the event: "the original changed" is a race with an editing
            # user and needs no action, while ``unattributed`` is a
            # normalization gap in a citation producer and needs a fix.
            self._emit({
                "kind": "global_ask_citations_void",
                "reason": void_reason,
                "libraries": len(job.resolved_notebook_ids),
                "citations": len(response.citations),
            })
            response.answer = _EVIDENCE_CHANGED_COPY
            response.conclusion = _EVIDENCE_CHANGED_COPY
            response.grounded = False
            # ``grounded`` alone is not enough: the badge the reader actually
            # sees is ``evidence_level``, and every evidence attachment beside
            # a "please ask again" sentence is an offer to inspect material
            # this run has just declared it can no longer vouch for. The
            # reasoning trace stays -- it describes the process, not the
            # evidence, and it is what explains why the answer was withdrawn.
            response.evidence_level = "inferred"
            response.top_relevance = 0.0
            response.anchors = []
            response.citations = []
            response.related_knowledge = []
            response.result_sets = []
        raise_if_cancelled(event)
        self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
        self._publish_coverage(job, user_id, state, progress=True)
        # Every citation names its owning library in peer mode (the nominal
        # active included), so this is the real per-library attribution rather
        # than "whatever was not the current notebook".
        job.cited_notebook_ids = list(dict.fromkeys(
            citation.notebook_id for citation in response.citations
            if citation.notebook_id
        ))
        # The completion stamp is ``AskStateStore.save_answer``'s job on the
        # notebook path; the detached turn bypasses that store, so it is set
        # here or the workspace shows the SUBMISSION time as the answer time.
        if not response.answered_at:
            response.answered_at = _now()
        job.answer = response
        # ``answer.reasoning_trace`` is the authority once the run finishes;
        # keeping the streamed copy too would double the persisted payload.
        job.trace = []
        job.status = "done"
        self._save_if_open(job, user_id)

    def _validate_citations(self, citations, evidence, source_ceiling, event=None,
                            *, siblings=None):
        """Does every citation still describe the library as it is NOW?

        Returns a void REASON code, or ``""`` when every citation still holds.

        Two halves, and which ones apply depends on how the cited element
        reached the answer. ``evidence`` is the run's accumulated
        retrieval-time table, whose three states are a contract, not an
        implementation detail (``FederatedRunPlan.on_evidence``, rule 3):

        =====================  ==========  ===============================
        snapshot state         live read   verdict
        =====================  ==========  ===============================
        ``(source, print)``    matches     accept (both halves pass)
        ``(source, print)``    differs     ``changed``
        ``(source, print)``    missing     ``changed`` (element deleted)
        ``None``               any         ``unreadable`` -- it travelled
                                           the federated channel and could
                                           not be fingerprinted, so it is
                                           not attestable
        absent                 same source accept -- the row is still where
                                           the citation says it is
        absent                 other source ``changed`` -- it moved
        absent                 missing     accept -- no ``source_elements``
                                           row was ever cited (a graph
                                           object or relation); the ceiling
                                           half is the whole check
        =====================  ==========  ===============================

        EVERY SUPPORTING ELEMENT, not only the one the citation names.
        ``evidence_context.chunk_citations`` publishes ``element_ids[0]`` as
        the citation's ``element_id``, but a chunk is assembled from as many
        source elements as it took to reach ~600 characters, and the answer
        rested on all of their text. ``siblings`` -- the run's fold of
        ``FederatedRunPlan.on_evidence_groups``, ``{element_id: the others of
        every passage it appeared in}`` -- is what makes the rest of the
        passage reachable; each sibling is then held to the same table above,
        with one difference stated in ``_validate_siblings``. Absent (no
        groups, no plan, single-element passages) it is inert and the check is
        exactly the per-citation one.

        ABSENCE is the case the first cut got wrong. A document overview and a
        collection enumeration cite real ``source_elements`` rows and make no
        federated call at all, so refusing "no snapshot" voided 100% of those
        answers. Their honest check is the one above: frozen ceiling, still
        visible, and the row still belongs to the source the citation names.

        The ceiling half applies to every kind, and runs FIRST: it is the only
        check a graph object gets, and it is the cheap one.

        ⛔ BOUNDED AND CANCELLABLE. This runs after the engine's own run has
        exited, so nothing above it is watching the clock any more: the
        per-notebook visibility reads get the run's per-library budget and the
        cancel token is polled before each one.
        """
        if not citations:
            return ""
        from app.repositories.read_budget import read_budget

        siblings = siblings or {}
        cited = [
            citation.element_id for citation in citations if citation.element_id
        ]
        # ONE read, widened rather than repeated: the siblings are read in the
        # same batch as the elements the citations name, so covering a whole
        # passage costs no extra round trip. Sorted so the request is
        # deterministic for a given selection.
        current = self.sources.evidence_fingerprints(list(dict.fromkeys(
            cited + sorted({
                sibling for element_id in cited
                for sibling in siblings.get(element_id, ())
            })
        )))
        visible: dict = {}
        for citation in citations:
            notebook_id = citation.notebook_id
            if not notebook_id:
                # Peer mode stamps every citation, the nominal active included.
                # A blank origin is a missed normalization point in one of the
                # citation producers, not a race with an editing user -- and an
                # unattributable citation cannot be checked against ANY
                # library's ceiling, so it fails closed under its own code.
                return _VOID_UNATTRIBUTED
            if notebook_id not in visible:
                raise_if_cancelled(event)
                with read_budget(
                    time.monotonic()
                    + float(self.settings.global_ask_notebook_timeout_seconds),
                    event,
                ):
                    visible[notebook_id] = set(
                        self.sources.all_visible_source_ids(notebook_id)
                    )
            if (citation.source_id not in source_ceiling.get(notebook_id, ())
                    or citation.source_id not in visible[notebook_id]):
                return _VOID_OUT_OF_CEILING
            before = evidence.get(citation.element_id, _ABSENT)
            after = current.get(citation.element_id)
            if before is None:
                return _VOID_UNREADABLE
            if before is _ABSENT:
                # Never came through the federated chunk channel. Either it is
                # not a source element at all (accept: the ceiling half is the
                # whole check), or it is one and must still sit under the
                # source the citation names.
                #
                # ⛔ "MISSING NOW" IS DELIBERATELY NOT A REFUSAL HERE, and the
                # cost is stated rather than hidden: an element that WAS live
                # at retrieval and was deleted during synthesis is published as
                # a citation card that opens on nothing. Refusing instead would
                # be worse and would also be a lie -- a non-empty
                # ``element_id`` does not attest that the row was ever live in
                # this run. Element ids are NOT re-issued on a re-ingest --
                # ``source_ingestion`` mints them deterministically from
                # ``(source, index)``, so the same id comes back -- but a
                # reparse that yields FEWER elements, and a row-level Knowhow
                # deletion, both leave stored ids with no row behind them, and
                # ``knowledge_store._enrich_evidence`` hands such a dangling id
                # straight back to a KG object's ``evidence[]`` (which is why
                # ``collection_item_citations`` hunts for the FIRST LIVE
                # occurrence). So "missing now" covers both "deleted just now"
                # and "dead since long before this question" -- and the copy
                # says the original CHANGED DURING THE ANSWER. The honest fix
                # is a retrieval-time FINGERPRINT snapshot from the four
                # producers that never touch this channel; it is registered in
                # ``fangan_todo.md`` under 检索.
                if after is not None and after[0] != citation.source_id:
                    return _VOID_CHANGED
            elif after is None or before != after or after[0] != citation.source_id:
                return _VOID_CHANGED
            void = self._validate_siblings(
                citation, evidence, current, siblings.get(citation.element_id, ()),
                source_ceiling, visible,
            )
            if void:
                return void
        return ""

    def _validate_siblings(self, citation, evidence, current, siblings,
                           source_ceiling, visible):
        """Re-check the rest of the passage one citation was minted from.

        Same table as ``_validate_citations``' own, with ONE difference:
        absence is refused here instead of accepted. A sibling is only known to
        this map because the federated chunk channel published the passage it
        belongs to, and that channel fingerprints every selected element in the
        same read -- so a sibling with no entry at all is a contradiction
        between two halves of one call, not the ordinary "this citation never
        travelled the channel". Refusing is the fail-closed side of a
        disagreement nobody can interpret, and it costs nothing in practice
        because the state is unreachable.

        The ceiling half runs off the SNAPSHOT's source id rather than the
        citation's: the elements of one chunk share a source today, so this
        only ever restates the check the citation itself already passed --
        which is exactly why it is written to be defended from the snapshot
        instead of assumed. ``visible`` is already populated for this library,
        so no sibling costs a second visibility read.
        """
        for sibling in siblings:
            before = evidence.get(sibling, _ABSENT)
            if before is None or before is _ABSENT:
                return _VOID_UNREADABLE
            if (before[0] not in source_ceiling.get(citation.notebook_id, ())
                    or before[0] not in visible[citation.notebook_id]):
                return _VOID_OUT_OF_CEILING
            if current.get(sibling) != before:
                return _VOID_CHANGED
        return ""

    def get_job(self, job_id, *, user_id, allowed_notebook_ids=None):
        job = self.store.job(job_id, user_id)
        if job is None:
            raise GlobalAskError(404, "问答任务不存在，请刷新对话。")
        self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids)
        return job

    def cancel(self, job_id, *, user_id, allowed_notebook_ids=None):
        job = self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        if job.status == "running":
            with self._lock:
                event = self._events.get(job_id)
                if event is not None:
                    event.set()
            job.status, job.response, job.answer = "cancelled", None, None
            if not self.store.save(job, user_id):
                return self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        return job

    @staticmethod
    def _validate_page(limit, offset):
        if not 1 <= limit <= GLOBAL_ASK_PAGE_MAX or offset < 0:
            raise GlobalAskError(422, "分页参数无效，请刷新后重试。")

    def list_conversations(self, *, user_id, limit=GLOBAL_ASK_PAGE_SIZE, offset=0):
        self._validate_page(limit, offset)
        return self.store.list_conversations(user_id, limit, offset)

    def conversation(self, conversation_id, *, user_id, allowed_notebook_ids=None,
                     limit=GLOBAL_ASK_PAGE_SIZE, offset=0):
        self._validate_page(limit, offset)
        conversation = self._conversation(conversation_id, user_id)
        jobs = self.store.jobs(conversation_id, user_id, limit + 1, offset)
        has_more = len(jobs) > limit
        turns = jobs[:limit]
        self._check({notebook_id for job in turns for notebook_id in job.resolved_notebook_ids},
                    user_id, allowed_notebook_ids)
        return GlobalConversationDetail(
            **conversation.model_dump(), turns=list(reversed(turns)), has_more=has_more,
            next_offset=offset + limit if has_more else None,
        )

    def rename_conversation(self, conversation_id, title, *, user_id):
        self._conversation(conversation_id, user_id)
        self.store.rename(conversation_id, user_id, title)
        return self._conversation(conversation_id, user_id)

    def delete_conversation(self, conversation_id, *, user_id):
        self._conversation(conversation_id, user_id)
        with self._lock:
            for job_id in self.store.running_job_ids(conversation_id, user_id):
                event = self._events.get(job_id)
                if event is not None:
                    event.set()
            self.store.delete(conversation_id, user_id)

    def cited_element(self, job_id, element_id, *, user_id, allowed_notebook_ids=None):
        job = self.get_job(job_id, user_id=user_id, allowed_notebook_ids=allowed_notebook_ids)
        # One projection for both payload shapes: a turn answered by the shared
        # engine carries ``answer``, a turn from before the switch carries the
        # legacy ``response``, and a drill-down must work on either.
        references = global_answer_citations(job)
        citation = next((item for item in references if item.element_id == element_id), None)
        if citation is None:
            raise GlobalAskError(404, "引用不存在，请重新打开答案。")
        self._check([citation.notebook_id], user_id, allowed_notebook_ids)
        try:
            source = self.sources.get_source(citation.source_id)
        except KeyError as exc:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。") from exc
        if source.notebook_id != citation.notebook_id or source.type in {"memory", "knowhow"}:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。")
        row = self.sources.evidence_elements([element_id]).get(element_id)
        if row is None or row["source_id"] != citation.source_id:
            raise GlobalAskError(404, "引用原文已不可用，请重新提问。")
        self._check([citation.notebook_id], user_id, allowed_notebook_ids)
        data = dict(row)
        if isinstance(data.get("metadata"), str):
            data["metadata"] = json.loads(data["metadata"])
        return SourceElement.model_validate(data)

    def close(self):
        with self._lock:
            self._closed = True
            for event in self._events.values():
                event.set()
            workers = list(self._workers.values())
        deadline = time.monotonic() + self.settings.global_ask_shutdown_timeout_seconds
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        # Never widen the shutdown budget: drop queued retrievals and let any
        # in-flight one finish against its own already-cancelled read budget.
        self._retrieval_pool.shutdown(wait=False, cancel_futures=True)
