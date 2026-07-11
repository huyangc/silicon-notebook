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
  cancel event → emit ``started`` → emit the synthetic ``start`` step WITHOUT
  persistence → submit the worker through the copied-context job helper
  (contextvars propagation is the per-user model lifeline) → per real trace:
  persist first, fail-open (log and still deliver — error_policies.json:
  append_ask_trace) → the runner saves the answer → finish-job transaction →
  unregister the cancel event → (failed/cancelled) empty-conversation cleanup
  in a LATER transaction → terminal ``final``/``cancelled``/``error`` event →
  sentinel.  No terminal event is ever put on the delivery queue while its job
  remains registered.

Composition rules (Gate 8): identity is EXPLICIT — ``user_id`` comes from the
caller per start; this module never reads request ContextVars and must not
import SQLiteRepository.  The ``runner`` is a late-bound facade callback
supplied per start; Task 24 replaces it with AskService.
"""
from __future__ import annotations

import queue
import threading
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.services.cancellation import AskCancelled

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.event_logging import EventLogger
    from app.models.schemas import AskRequest, AskResponse
    from app.repositories.ports import AskStateStorePort
    from app.services.ask_modes import AskMode


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
    ) -> None:
        self.ask_state = ask_state
        self.cancellations = cancellations
        self.job_submitter = job_submitter
        self.event_log = event_log

    def start(
        self,
        notebook_id: str,
        payload: "AskRequest",
        mode: "AskMode",
        *,
        user_id: str,
        runner: "Callable[..., AskResponse]",
    ) -> "queue.Queue[dict[str, Any] | None]":
        """启动一次脱离连接的流式 ask,返回交付队列(None 哨兵收尾)。

        建/接续会话 + 建 ask_jobs 行在 ``begin_durable_job`` 的单写事务里原子完成,
        conversation_id 在该事务体内就地写回 payload(基线时点)——本方法只观察
        返回值、绝不二次改写。job_id 先于一切 progress 发给客户端(供「停止」调
        cancel 端点)。传输断连只由调用方停止消费本队列,worker 照跑到完。
        """
        events: "queue.Queue[dict[str, Any] | None]" = queue.Queue()
        cancel_event = threading.Event()
        job_id, _conversation_id = self.ask_state.begin_durable_job(
            notebook_id, payload, mode.id, user_id)
        self.cancellations.register(job_id, cancel_event)
        events.put({"event": "started", "job_id": job_id})
        # 合成 start 步:只上流、不落 ask_trace_steps(基线语义)。
        events.put({"event": "progress", "step": {
            "step_type": "start", "summary": "启动检索", "detail": {"mode": mode.id}}})

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
            try:
                response = runner(
                    notebook_id, payload, on_trace=on_trace, cancel_event=cancel_event,
                ) if mode.streaming else runner(
                    notebook_id, payload, cancel_event=cancel_event,
                )
                self._finish(job_id, "done", answer_id=response.answer_id)
                events.put({"event": "final", "response": response.model_dump()})
            except AskCancelled:
                self._finish(job_id, "cancelled")
                events.put({"event": "cancelled"})
            except Exception as exc:  # noqa: BLE001
                self._finish(job_id, "failed", error=f"{type(exc).__name__}: {exc}")
                events.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                events.put(None)

        self.job_submitter.submit(worker, name=f"ask-{mode.id}")
        return events

    def _finish(self, job_id: str, status: str, *, answer_id: str = "", error: str = "") -> None:
        """终态收尾的冻结次序:终态 job 行事务 → 注销 cancel_event →
        (cancelled/failed)空会话清理(更晚的独立事务)。调用方随后才把终态
        事件放上交付队列——job 仍在注册表上时绝不交付终态。"""
        conversation_id = self.ask_state.finish_job(
            job_id, status, answer_id=answer_id, error=error)
        self.cancellations.unregister(job_id)
        if status in ("cancelled", "failed") and conversation_id:
            self.ask_state.cleanup_empty_conversation(conversation_id)
