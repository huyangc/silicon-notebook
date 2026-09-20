"""Global source Ask ownership, authority, and detached execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
import json
import threading
import time
from uuid import uuid4

from app.core.ask_retrieval_policy import DEFAULT_RETRIEVAL_EFFORT
from app.models.ask import AskRequest
from app.models.global_ask import (
    GlobalAskJob, GlobalAskRequest, GlobalConversationDetail,
    GlobalNotebookScope, GLOBAL_ASK_PAGE_MAX, GLOBAL_ASK_PAGE_SIZE,
    GlobalAskSkippedNotebook, global_answer_citations,
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

    ``lock`` is not decoration. The receipt callbacks fire on whichever thread
    made the federated call and the trace callback fires from the engine's own
    stack; both mutate the same job object the owning thread persists.
    """

    __slots__ = (
        "order", "evidence", "succeeded", "failed", "degraded", "federated",
        "error", "lock",
    )

    def __init__(self, order):
        self.order = list(order)
        self.evidence: dict = {}
        self.succeeded: set = set()
        self.failed: dict = {}
        self.degraded: set = set()
        self.federated = False
        self.error: BaseException | None = None
        self.lock = threading.RLock()

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

        Merge, never replace: a reasoning run publishes a map per round and the
        citation re-check asks about every element that reached the answer, so
        an empty map from a round that selected nothing must not erase what the
        earlier rounds established.
        """
        if not mapping:
            return
        with self.lock:
            self.evidence.update(mapping)

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
        """``(searched, degraded, skipped_reasons)`` in resolved order.

        Built by ``_ordered_insert`` from sets, whose iteration order is
        unspecified: a poller must never see the receipts permute between two
        progress saves for reasons that have nothing to do with the run.

        A run that never federated at all -- a document overview or a
        collection enumeration answers straight from the participant-aware
        enumeration lanes without a single federated chunk call -- reports
        every resolved library as searched and none as skipped. Calling them
        skipped would tell the user to narrow a scope that was in fact read in
        full, and inventing a reason code for a query that was never issued is
        exactly what ``queue_deadline`` exists NOT to do.
        """
        with self.lock:
            if not self.federated:
                return list(self.order), [], {}
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
        return searched, degraded, skipped


# Name prefix of the SHARED retrieval pool's threads. The job thread must not
# be one of them -- see ``_execute``.
_RETRIEVAL_THREAD_PREFIX = "global-ask-retrieve"


class GlobalAskService:
    def __init__(self, *, store, notebooks, can_read, sources, settings, ask=None,
                 rewrite_query=None, can_read_many=None, event_log=None,
                 retrieve=None, synthesize=None, prepare_query=None):
        self.store = store
        self.notebooks = notebooks
        self.can_read = can_read
        self.can_read_many = can_read_many
        self.sources = sources
        # The ONE single-library engine. A global run is the same engine under
        # a participant override, not a second retrieval and synthesis stack.
        self.ask = ask
        self.settings = settings
        # ⛔ Legacy injection points of the retired global-only pipeline. ``_run``
        # calls none of them; they stay as accepted-and-ignored keywords only so
        # that removing the pipeline (and the composition that still passes
        # them) is a separate, independently revertible change.
        self.retrieve = retrieve
        self.synthesize = synthesize
        self.rewrite_query = rewrite_query
        self.prepare_query = prepare_query
        # Optional content-free sink. Skips decided here (never queued before
        # the phase budget ran out) never reach the retrieval module's own
        # emitter, so they would otherwise be the one skip with no receipt.
        self.event_log = event_log
        self._lock = threading.RLock()
        self._events = {}
        self._workers = {}
        self._pending = 0
        self._closed = False
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
        """No detached worker may reopen persistence after runtime shutdown."""
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
        """
        try:
            normalized = GlobalAskRequest.model_validate_json(stored_json).model_dump_json()
        except Exception:  # noqa: BLE001 - see docstring
            normalized = stored_json
        return normalized == request_json

    def start(self, payload: GlobalAskRequest, *, user_id, allowed_notebook_ids=None,
              submitted_via="web", authority_check=None):
        spec = self._resolve_mode(payload.mode)
        request_json = payload.model_dump_json()
        previous = self.store.request_job(user_id, payload.client_request_id)
        if previous is not None:
            job, old_request = previous
            if not self._same_request(old_request, request_json):
                raise GlobalAskError(409, "请求标识已用于其他问题，请重新提交。")
            self._check(job.resolved_notebook_ids, user_id, allowed_notebook_ids, authority_check)
            return job
        with self._lock:
            if self._closed:
                raise GlobalAskError(503, "服务正在关闭，请稍后重新提交问题。")
            if len(self._workers) + self._pending >= self.settings.global_ask_max_concurrent:
                raise GlobalAskError(429, "全局问答正在处理其他任务，请稍后重试。")
            self._pending += 1
        try:
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
                    self.store.create(
                        job, user_id, payload.client_request_id, request_json, submitted_via,
                        new_conversation=conversation is None,
                    )
                    created = True
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
                    if previous[1] != request_json:
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

    def _retrieval_window(self):
        """How many libraries THIS job may keep in flight right now.

        The retrieval pool is shared, so a job that submitted all of its
        libraries at once would own every execution slot and every later job
        would sit in the FIFO queue until its own phase budget expired -- the
        second questioner got zero searched libraries and an answer with no
        evidence. Dividing the slots by the number of live jobs keeps the pool
        exactly full (4 slots / 4 jobs = 1 each) without raising the connection
        bound the startup pool budget is computed from.

        The share is recomputed on every top-up, so a job that started alone
        holds wider ground only until its next library finishes: a newcomer
        then waits at most one library, never a whole phase.
        """
        with self._lock:
            active = max(1, len(self._workers))
        return max(1, int(self.settings.global_ask_retrieval_concurrency) // active)

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
        """Rebuild the durable coverage lists and save them."""
        searched, degraded, skipped = state.coverage()
        job.searched_notebook_ids = searched
        job.degraded_notebook_ids = degraded
        job.skipped_notebooks = [
            GlobalAskSkippedNotebook(
                notebook_id=notebook_id,
                reason=_SKIP_COPY.get(skipped[notebook_id], ""),
            )
            for notebook_id in job.resolved_notebook_ids
            if notebook_id in skipped
        ]
        if not self._save_if_open(job, user_id, progress=progress):
            raise AskCancelled()

    def _append_trace(self, job, user_id, state, step):
        """Stream one reasoning step into the job a poller is reading.

        Fail-OPEN in full, exactly like the single-library
        ``append_trace_fail_open``: a trace is how the user watches a run, and
        losing a step -- or losing the storage the step was going to -- must
        never be the reason an otherwise good answer fails.

        One save per step, deliberately un-throttled. It matches what the
        notebook-scoped path does, it keeps the polled progress exact, and the
        steps are bounded by the reasoning round ceiling. The rows are also
        transient: the finished job clears ``trace`` because
        ``answer.reasoning_trace`` is the authority, so nothing here decides
        what the durable turn finally contains.
        """
        try:
            with state.lock:
                job.trace.append(step)
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
        assert not threading.current_thread().name.startswith(
            _RETRIEVAL_THREAD_PREFIX
        ), "the global job thread must not be a shared retrieval-pool worker"
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
                    cancel_event=plan.cancel,
                    on_trace=lambda step: self._append_trace(
                        job, user_id, state, step,
                    ),
                )
        except BaseException:
            # A callback's failure outranks whatever reached this frame: a
            # cancellation raised because the phase token was set by a revoked
            # share is that revocation, not a user pressing stop.
            state.raise_first_error()
            raise
        state.raise_first_error()
        raise_if_cancelled(event)
        self._check(job.resolved_notebook_ids, user_id, allowed, authority_check)
        if self._validate_citations(response.citations, state.evidence, source_ceiling):
            # VOIDED WHOLE, never patched. Dropping the dead markers would
            # leave the claims that depended on them standing, and re-running
            # the engine would re-retrieve and re-freeze everything -- a
            # different answer wearing this one's identity.
            self._emit({
                "kind": "global_ask_citations_void",
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
        job.answer = response
        # ``answer.reasoning_trace`` is the authority once the run finishes;
        # keeping the streamed copy too would double the persisted payload.
        job.trace = []
        job.status = "done"
        self._save_if_open(job, user_id)

    def _validate_citations(self, citations, evidence, source_ceiling):
        """Does every citation still describe the library as it is NOW?

        Both halves of the frozen-evidence contract survive the engine switch:

        1. the cited source is still inside the ceiling frozen when the job was
           admitted, AND is still visible in its own library;
        2. the cited element's fingerprint and its source ownership are
           unchanged since retrieval read them.

        ``evidence`` is the accumulated retrieval-time snapshot published by
        every federated call of this run. A cited element that is a real source
        element but has NO entry there cannot be verified -- which is exactly
        what a round whose fingerprint read failed publishes -- so it is
        refused rather than accepted unverified.

        Citation kinds that are not source elements at all -- knowledge-graph
        objects and relations, document-overview anchors -- have no fingerprint
        by nature: the live read does not know their id either. They are held
        to half (1) alone. The two cases are told apart by the LIVE read rather
        than by the snapshot, so an element that existed at retrieval time and
        has since been deleted is absent from ``current`` while present in
        ``evidence``, and is refused instead of being mistaken for a graph
        object.

        Returns True when the answer must be voided.
        """
        if not citations:
            return False
        current = self.sources.evidence_fingerprints(list(dict.fromkeys(
            citation.element_id for citation in citations if citation.element_id
        )))
        visible: dict = {}
        for citation in citations:
            notebook_id = citation.notebook_id
            if not notebook_id:
                # Peer mode stamps every citation. A blank origin means a
                # normalization point was missed, and an unattributable
                # citation cannot be checked against any library's ceiling.
                return True
            if notebook_id not in visible:
                visible[notebook_id] = set(
                    self.sources.all_visible_source_ids(notebook_id)
                )
            if (citation.source_id not in source_ceiling.get(notebook_id, ())
                    or citation.source_id not in visible[notebook_id]):
                return True
            before = evidence.get(citation.element_id)
            after = current.get(citation.element_id)
            if before is None and after is None:
                continue
            if before is None or after is None or before != after:
                return True
            if after[0] != citation.source_id:
                return True
        return False

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
