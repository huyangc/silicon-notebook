"""Ask cancellation registry + detached streaming execution (Task 23).

Owns the streaming-ask EXECUTION orchestration that previously lived inline in
``routes._stream_ask_events`` plus the facade's WS2a cancel-event registry:

* :class:`AskCancellationRegistry` — the in-process ``{job_id: cancel_event}``
  map.  The explicit cancel endpoint (owner-checked on the facade) is the ONLY
  true cancellation entry; transport disconnect never touches these events —
  the worker detaches from the connection and runs to completion, the answer
  is saved regardless (PR#220 WS2a/WS2b).
* :class:`AskExecutionCoordinator` — ``start()`` runs the frozen baseline
  sequence: begin the durable job in ONE transaction (which mutates
  ``payload.conversation_id`` in place at the baseline point — the coordinator
  observes the returned value and never mutates a second time) → register the
  cancel event → emit ``started`` with the durable job/conversation ids →
  emit the synthetic ``start`` step WITHOUT
  persistence (auto mode: the job is begun under the request-only ``auto`` id
  and this step moves INSIDE the worker, after ``resolve`` has selected the
  engine and written it back to the job row) → submit the worker through the
  copied-context job helper
  (contextvars propagation is the per-user model lifeline) → per real trace:
  persist first, fail-open (log and still deliver — error_policies.json:
  append_ask_trace) → the engine saves the answer → finish-job transaction →
  unregister the cancel event → (failed/cancelled) empty-conversation cleanup
  in a LATER transaction → terminal ``final``/``cancelled``/``error`` event →
  successful-path completion observers → sentinel.
  No terminal event is ever put on the delivery queue while its job remains
  registered; post-completion hooks cannot rewrite the response or reverse
  the already committed ``done`` state.

Composition rules (Gate 8): identity is EXPLICIT — ``user_id`` comes from the
caller per start; this module never reads request ContextVars and must not
import SQLiteRepository.  Task 24: the worker calls the runtime-owned
AskService (``ask`` resolver injected at composition — the service itself
dispatches through the frozen ask_modes registry and forwards ``on_trace``
only to streaming engines), replacing the Task-23 late-bound facade callback.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.repositories.ports import AskRequestKeyConflict
from app.services.ask_modes import AUTO_MODE
from app.services.cancellation import AskCancelled

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.event_logging import EventLogger
    from app.models.ask import AskRequest, AskResponse
    from app.repositories.ports import AskStateStorePort
    from app.services.ask_modes import AskMode


# The stream's terminal wording when a submission key was already spent in a
# different notebook. User-facing (it is what the ``error`` event carries).
KEY_CONFLICT_MESSAGE = "这次提交的标识已在另一个笔记本使用过，请重新提问"


class BackgroundJobSubmitter(Protocol):
    """The copied-context daemon-thread job helper contract — satisfied by the
    ``app.services.background_jobs`` module itself (copy_context() snapshots
    the CALLER's context, so per-user model resolution and per-user logs reach
    the detached worker)."""

    def submit(
        self,
        fn: Callable,
        *args,
        name: str | None = None,
        notify_pending: bool = False,
        **kwargs,
    ) -> threading.Thread: ...


class AskServicePort(Protocol):
    """The mode-engine contract the worker executes (Task 24): registry
    dispatch with EXPLICIT ``user_id``; ``on_trace`` reaches streaming engines
    only — the service owns that split, mirroring the frozen route runner."""

    def ask(
        self,
        notebook_id: str,
        payload: "AskRequest",
        *,
        user_id: str,
        job_id: str = "",
        cancel_event: "threading.Event | None" = None,
        on_trace: "Callable[[Any], None] | None" = None,
    ) -> "AskResponse": ...

    def validate_reasoning_submission(
        self, notebook_id: str, payload: "AskRequest",
    ) -> None: ...


class AskCancellationRegistry:
    """WS2a 在途 ask 的 {job_id: cancel_event} 进程内注册表。

    显式 cancel 端点据此 set 对应 worker 的 cancel_event(唯一真取消入口;
    断连不再取消)。``events``/``lock`` 公开为兼容句柄——facade 冻结的
    ``_ask_cancel_events``/``_ask_cancel_lock`` 属性透出同一对象(一个所有者)。
    """

    def __init__(self) -> None:
        self.events: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def register(self, key: str, event: threading.Event) -> None:
        with self.lock:
            self.events[key] = event

    def get(self, key: str) -> "threading.Event | None":
        with self.lock:
            return self.events.get(key)

    def cancel(self, key: str) -> bool:
        """set 在途 job 的 cancel_event;未注册(不存在/已终态注销)→ False。"""
        with self.lock:
            event = self.events.get(key)
        if event is None:
            return False
        event.set()
        return True

    def unregister(self, key: str) -> None:
        with self.lock:
            self.events.pop(key, None)


class AskExecutionCoordinator:
    """脱离连接的流式 ask 执行编排(交付队列 + 持久化 + 取消协作)。"""

    def __init__(
        self,
        *,
        ask_state: "AskStateStorePort",
        cancellations: AskCancellationRegistry,
        job_submitter: BackgroundJobSubmitter,
        event_log: "EventLogger",
        ask: "Callable[[], AskServicePort]",
        note_ask_completed: "Callable[[str, str, str], None] | None" = None,
        follow_poll_seconds: float = 0.5,
    ) -> None:
        self.ask_state = ask_state
        self.cancellations = cancellations
        self.job_submitter = job_submitter
        self.event_log = event_log
        self.ask = ask
        # Attach/follow mode (a re-submitted client_request_id): how often the
        # follower re-reads the existing job's row + trace while it runs. The
        # job may execute in another process, so the store is the only shared
        # channel; a half-second poll is well under the engine's own step
        # cadence. Tests shorten it.
        self.follow_poll_seconds = follow_poll_seconds
        # Agentic Memory P1 (T5): "this member finished an ask in this
        # notebook", the overlay chain's threshold signal. A late-bound
        # callable rather than the service object because this coordinator is
        # constructed before that service exists in the runtime — the same
        # reason ``ask`` above is an accessor. ``None`` = not wired (unit-test
        # doubles, older composition roots) and the call site degrades to a
        # no-op: the feature never changes whether an answer is delivered.
        self.note_ask_completed = note_ask_completed

    def start(
        self,
        notebook_id: str,
        payload: "AskRequest",
        mode: "AskMode | None",
        *,
        user_id: str,
        resolve: "Callable[[threading.Event], tuple[AskRequest, AskMode]] | None" = None,
    ) -> "queue.Queue[dict[str, Any] | None]":
        """启动一次脱离连接的流式 ask,返回交付队列(None 哨兵收尾)。

        建/接续会话 + 建 ask_jobs 行在 ``begin_durable_job`` 的单写事务里原子完成,
        conversation_id 在该事务体内就地写回 payload(基线时点)——本方法只观察
        返回值、绝不二次改写。job_id 先于一切 progress 发给客户端(供「停止」调
        cancel 端点)。传输断连只由调用方停止消费本队列,worker 照跑到完。
        执行体 = runtime-owned AskService(Task 24;on_trace 只达 streaming
        引擎——注册表派发在服务内,与基线 route-runner 的分派逐字等价)。

        ``resolve``(auto 模式):引擎尚未选定。job 先以 request-only 的 ``auto``
        id 建立、``started`` 先入队;worker 内再跑 ``resolve(cancel_event)``(引擎
        选择 + 已确认意图校验),把选定引擎写回 job 行(fail-open),再发该引擎的
        合成 start 步并执行。于是选择引擎期间的断连不再丢问题;显式 cancel 仍经
        cancel_event 中止选择。``mode`` 与 ``resolve`` 至少给一个。

        ``payload.client_request_id``(幂等键):走 ``begin_or_attach_durable_job``。
        该用户已用过这个键时不建第二个 job、不跑第二个引擎——``started`` 带既有 id
        入队后,由 :meth:`_follow` 从存储把该 job 的轨迹与终态回放给本客户端
        (``resolve`` 也不再调用)。键在另一笔记本已用过 → 流只含一个 ``error``。
        """
        if mode is None and resolve is None:
            raise ValueError("start() needs a resolved mode or a resolve callable")
        events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        cancel_event = threading.Event()
        service = self.ask()
        # This is the ONLY validate_reasoning_submission call on the streaming
        # path for a resolved mode — do not delete it as a duplicate of the
        # route.  The route ran its own copy until the model-inferred source
        # scope was removed; what it keeps is
        # ``_validate_confirmed_reasoning_intent``, which today happens to cover
        # the same conditions but is a separate check that either side may
        # extend.  It must stay above begin_durable_job so an invalid submission
        # fails before a durable job exists (test_source_scope.py pins that
        # ordering for both Ask paths).  With ``resolve`` the payload is only
        # known inside the worker, which validates it there.  The getattr is
        # fail-open only for the narrow unit-test doubles that intentionally
        # omit the method.
        # Idempotent re-submission first: a job this key already created is
        # attached to BEFORE any request-dependent validation — the original
        # submission passed it, and a retry must replay that job even if the
        # notebook's availability or mode registry changed since (the route
        # applies the same ordering ahead of its own preflight).
        attached_events = self.attach_existing(notebook_id, payload, user_id, events)
        if attached_events is not None:
            return attached_events
        validate = getattr(service, "validate_reasoning_submission", None)
        if resolve is None and validate is not None:
            validate(notebook_id, payload)
        mode_id = mode.id if mode is not None else AUTO_MODE
        if payload.client_request_id:
            # The key names ONE browser submission. The probe above found no
            # job, but a concurrent retry may land one between the probe and
            # this insert: the store's lookup+insert is atomic and reports it.
            try:
                job_id, conversation_id, attached = (
                    self.ask_state.begin_or_attach_durable_job(
                        notebook_id, payload, mode_id, user_id))
            except AskRequestKeyConflict:
                self._put_key_conflict(events)
                return events
            if attached:
                return self._follow(events, job_id, conversation_id)
        else:
            job_id, conversation_id = self.ask_state.begin_durable_job(
                notebook_id, payload, mode_id, user_id)
        self.cancellations.register(job_id, cancel_event)
        # conversation_id is durable before this event is delivered.  The UI
        # can therefore publish/reopen a first-turn session immediately,
        # without waiting for the model's final response.
        events.put({
            "event": "started",
            "job_id": job_id,
            "conversation_id": conversation_id,
        })

        def emit_start_step(engine_id: str) -> None:
            # 合成 start 步:只上流、不落 ask_trace_steps(基线语义)。auto 模式
            # 推迟到引擎选定之后,让它报告真正执行的引擎。
            events.put({"event": "progress", "step": {
                "step_type": "start", "summary": "启动检索",
                "detail": {"mode": engine_id}}})

        if resolve is None:
            emit_start_step(mode_id)

        def on_trace(step) -> None:
            if not cancel_event.is_set():
                payload_step = step.model_dump()
                # WS2b 持久化供重开会话回放:持久化先行,fail-open——raw store 上抛的
                # 任何失败在此记日志吞掉、照常交付,轨迹持久化失败绝不拖垮 ask
                # (error_policies.json: append_ask_trace)。
                try:
                    self.ask_state.append_trace(notebook_id, job_id, payload_step, user_id)
                except Exception:  # noqa: BLE001
                    self.event_log.logger.exception(
                        "append_ask_trace failed for %s", job_id)
                events.put({"event": "progress", "step": payload_step})

        def worker() -> None:
            run_payload = payload
            engine_id = mode_id
            try:
                if resolve is not None:
                    run_payload, engine = resolve(cancel_event)
                    engine_id = engine.id
                    if validate is not None:
                        validate(notebook_id, run_payload)
                    try:
                        self.ask_state.update_job_mode(job_id, engine_id)
                    except Exception:  # noqa: BLE001 — informational column; the run goes on
                        self.event_log.logger.exception(
                            "update_job_mode failed for %s", job_id)
                    emit_start_step(engine_id)
                response = service.ask(
                    notebook_id, run_payload, user_id=user_id,
                    job_id=job_id, on_trace=on_trace, cancel_event=cancel_event,
                )
                self._finish(job_id, "done", answer_id=response.answer_id)
                events.put({"event": "final", "response": response.model_dump()})
                # Agentic Memory P1 (T5, repair round): AFTER the terminal job
                # row AND after the "final" event is queued for the browser —
                # never inside ``_finish`` (frozen sequence) and never ahead
                # of the event the browser is waiting on. This call is
                # fail-open on its own (see its docstring), so moving it past
                # ``events.put`` cannot turn a delivered answer into a
                # reported failure; it only stops an overlay notification
                # from ever being able to delay or precede the terminal
                # event. Only the ``done`` path signals: a cancelled or
                # failed ask says nothing about how this person searches, and
                # counting it would let a broken provider drive the threshold.
                self._note_ask_completed(notebook_id, user_id, engine_id)
            except AskCancelled:
                self._finish(job_id, "cancelled")
                events.put({"event": "cancelled"})
            except Exception as exc:  # noqa: BLE001
                self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
                events.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)

        try:
            self.job_submitter.submit(worker, name=f"ask-{mode_id}")
        except BaseException as exc:
            self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise
        return events

    def attach_existing(
        self,
        notebook_id: str,
        payload: "AskRequest",
        user_id: str,
        events: "queue.Queue[dict[str, Any] | None] | None" = None,
    ) -> "queue.Queue[dict[str, Any] | None] | None":
        """Attach to the job ``payload.client_request_id`` already created for
        this user, if any: the delivery queue of :meth:`_follow`, or — for a
        key spent in ANOTHER notebook — a well-formed queue holding only an
        ``error`` (nothing is created). ``None`` when the payload has no key or
        the key is unknown, so the caller runs the normal submission path.

        Read-only apart from the queue; no validation, no availability check —
        those judged the ORIGINAL submission. Notebook authorization is the
        caller's (the route dependency), and the store scopes the key by user."""
        key = payload.client_request_id
        if not key:
            return None
        existing = self.ask_state.find_job_for_client_request(user_id, key)
        if existing is None:
            return None
        if events is None:
            events = queue.Queue()
        if existing["notebook_id"] != notebook_id:
            self._put_key_conflict(events)
            return events
        payload.conversation_id = existing["conversation_id"]
        return self._follow(events, existing["job_id"], existing["conversation_id"])

    @staticmethod
    def _put_key_conflict(events: "queue.Queue[dict[str, Any] | None]") -> None:
        # A well-formed transport (no job, no `started`): the client reused a
        # key across notebooks, which is its own defect. Displayable wording,
        # not an exception dump (AGENTS.md error policy).
        events.put({"event": "error", "error": KEY_CONFLICT_MESSAGE})
        events.put(None)

    def _follow(
        self,
        events: "queue.Queue[dict[str, Any] | None]",
        job_id: str,
        conversation_id: str,
    ) -> "queue.Queue[dict[str, Any] | None]":
        """Attach mode: stream an EXISTING job to this client.

        The same event vocabulary as a fresh run — ``started`` with the job's
        durable ids, then every persisted trace step (``progress``), then the
        terminal event the job row reports: ``final`` carrying the saved answer
        (``ask_answer_detail``), ``cancelled``, or ``error`` — so the browser
        cannot tell an attached stream from the original one. Nothing here
        executes, persists or finishes anything: the job's own worker owns its
        row, its trace, its answer and its cancel event (the explicit cancel
        endpoint still reaches that worker through the registry; this follower
        merely observes the resulting ``cancelled`` status). The synthetic
        ``start`` step is not replayed — it was never persisted, exactly as the
        reopen-a-running-job replay (``/ask/jobs/{id}``) never has it.

        A finished job replays in one pass; a running one is polled every
        ``follow_poll_seconds`` until its row leaves ``running``. The follower
        runs on the same copied-context job helper as a worker so a transport
        disconnect (the caller stops draining the queue) never blocks it.
        """
        events.put({
            "event": "started",
            "job_id": job_id,
            "conversation_id": conversation_id,
        })

        def follower() -> None:
            delivered = 0
            try:
                while True:
                    detail = self.ask_state.ask_job_detail(job_id)
                    trace = detail.get("trace") or []
                    for step in trace[delivered:]:
                        events.put({"event": "progress", "step": step})
                    delivered = len(trace)
                    status = detail.get("status")
                    if status == "running":
                        time.sleep(self.follow_poll_seconds)
                        continue
                    if status == "done":
                        answer = self.ask_state.ask_answer_detail(
                            str(detail.get("answer_id") or ""))
                        if answer is None:
                            events.put({"event": "error",
                                        "error": f"answer of ask job {job_id} is missing"})
                        else:
                            events.put({"event": "final", "response": answer["payload"]})
                    elif status == "cancelled":
                        events.put({"event": "cancelled"})
                    else:
                        events.put({"event": "error",
                                    "error": str(detail.get("error") or f"ask job {status}")})
                    return
            except Exception as exc:  # noqa: BLE001 — the transport must end well-formed
                events.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)

        self.job_submitter.submit(follower, name="ask-follow")
        return events

    def _note_ask_completed(
        self, notebook_id: str, user_id: str, mode_id: str
    ) -> None:
        """Signal the startup-frozen completed-observer host — fail-open.

        ⚠ This runs INSIDE the worker's ``try``, whose ``except Exception``
        finishes the job as *failed* and delivers an error event. An exception
        escaping here would therefore turn a delivered answer into a reported
        failure, which is why the swallow is here rather than only in the
        service: the service is fail-open too, but the coordinator must not
        depend on the collaborator it was handed being well-behaved.

        ``self.note_ask_completed`` (when wired — see ``repository_runtime.py
        ::RepositoryRuntime._note_ask_completed``) runs THREE ordered
        contributions, and their costs are not the same shape. T9 fix round:
        this docstring previously described only the first of the three
        (P1's agent-profile consolidation): one bounded indexed membership
        read (P2-T3's ``_member_can_read`` — see its COST note) plus one
        primary-key upsert, handing a callable to the light background pool
        (a queue put, never a blocking wait) only once its own threshold is
        reached. The SECOND link (P2's retrieval-experience distillation)
        follows the same "cheap synchronous check, background hand-off only
        past threshold" shape. The THIRD link (P3/T7's
        ``SearchProfileInferenceService.note_ask_completed``) does NOT: its
        threshold check is cheap, but once past it the job's bounded
        read-then-write runs SYNCHRONOUSLY, inline, under a process-local
        lock (see that module's own docstring for why) — "never a blocking
        wait" does not hold for this third link, only "bounded" does. All
        three calls remain fail-open at THIS call site regardless, so a slow
        or failing third link still cannot turn a delivered answer into a
        reported failure; it can only add its own bounded latency to this
        one worker's post-delivery bookkeeping step.
        """
        if self.note_ask_completed is None:
            return
        try:
            self.note_ask_completed(notebook_id, user_id, mode_id)
        except Exception:  # noqa: BLE001 — an answer was delivered; keep it that way
            self.event_log.logger.exception(
                "agent profile ask notification failed for notebook %s", notebook_id
            )

    def _finish(self, job_id: str, status: str, *, answer_id: str = "", error: str = "") -> None:
        """终态收尾的冻结次序:终态 job 行事务 → 注销 cancel_event →
        (cancelled/failed)空会话清理(更晚的独立事务)。调用方随后才把终态
        事件放上交付队列——job 仍在注册表上时绝不交付终态。"""
        conversation_id = self.ask_state.finish_job(
            job_id, status, answer_id=answer_id, error=error)
        self.cancellations.unregister(job_id)
        if status in ("cancelled", "failed") and conversation_id:
            self.ask_state.cleanup_empty_conversation(conversation_id)
