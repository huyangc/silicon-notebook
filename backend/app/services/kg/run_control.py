from __future__ import annotations

from dataclasses import dataclass
import inspect
import random
import threading
from typing import Any, Callable

from app.core.config import Settings
from app.core.llm import is_transient_llm_error, llm_status_code


MODEL_UNAVAILABLE_MESSAGE = (
    "模型服务暂时不可用，本次分析已停止；"
    "已完成内容已保留，请在服务恢复后继续分析未完成内容。"
)
MODEL_AUTH_FAILED_MESSAGE = (
    "模型服务认证失败，本次分析已停止；请检查 API Key 或访问权限后重试。"
)
MODEL_REQUEST_REJECTED_MESSAGE = (
    "模型服务拒绝了知识分析请求；请检查模型名称、地址和兼容性设置后重试。"
)
MODEL_RESPONSE_INVALID_MESSAGE = (
    "模型服务未返回可解析的知识分析结果；"
    "请检查模型兼容性或输出 token 上限后重试。"
)


@dataclass(frozen=True)
class KgBuildFailure:
    code: str
    user_message: str


class KgBuildAborted(RuntimeError):
    def __init__(self, failure: KgBuildFailure):
        super().__init__(failure.user_message)
        self.failure = failure


class KgExtractionRunControl:
    def __init__(
        self,
        job_id: str,
        *,
        on_abort: Callable[[KgBuildFailure], None] | None = None,
        parent: "KgExtractionRunControl | None" = None,
    ):
        self.job_id = job_id
        self._event = threading.Event()
        self._lock = threading.Condition()
        self._failure: KgBuildFailure | None = None
        self._publishing = False
        self._on_abort = on_abort
        self._parent = parent
        # 子控制器注册表(仅 job 级持有)。父熔断必须唤醒每个在飞的子控制器,否则
        # 子作用域里正在 wait_backoff 的窗口会睡满退避才发现整个任务已经停了。
        # 由 release_child 在来源收尾时摘除,规模因此有界于来源级并发度。
        self._children: set["KgExtractionRunControl"] = set()
        self._children_lock = threading.Lock()

    def child(self) -> "KgExtractionRunControl":
        """派生一个**来源级**控制器:它的 abort 只掐自己这一路的在飞窗口,不熔断整个任务。

        跳过模式(离线批量)用它把「模型不可用」的作用域从任务收窄到单个来源:该来源的
        兄弟窗口照常被取消并排空(extract_graph 自己的 drain),其余来源不受影响。父控制器
        仍然向下传导——Ctrl-C 与达到连续失败阈值后的升级熔断必须能穿透到每一个子作用域。
        """
        sub = KgExtractionRunControl(self.job_id, parent=self)
        with self._children_lock:
            if self._event.is_set():
                # 已经熔断:子控制器出生即中止,别让它再发一次请求。
                sub._event.set()
            else:
                self._children.add(sub)
        return sub

    def release_child(self, sub: "KgExtractionRunControl") -> None:
        with self._children_lock:
            self._children.discard(sub)

    @property
    def aborted(self) -> bool:
        if self._event.is_set():
            return True
        return self._parent is not None and self._parent.aborted

    @property
    def cancel_event(self) -> threading.Event:
        """Expose the task signal to cancellation-aware model transports.

        The shared OpenAI-compatible client uses a non-null cancellation signal
        to select its streaming JSON path.  The signal stays owned by this run
        control; callers may observe it but must open the circuit through
        :meth:`abort` so the classified failure is published first.
        """
        return self._event

    @property
    def failure(self) -> KgBuildFailure | None:
        with self._lock:
            if self._failure is not None:
                return self._failure
        return self._parent.failure if self._parent is not None else None

    def abort(
        self, failure: KgBuildFailure, *, notify: bool = True
    ) -> KgBuildFailure:
        """notify=False:只置熔断标志把在飞窗口唤醒,不走 on_abort 公布状态。
        供操作者主动中断(Ctrl-C/SIGTERM)使用——那条路径要先让窗口停下再排空,
        状态由它自己的收尾统一公布,且不得记模型侧的 circuit_opened 事件。"""
        with self._lock:
            while self._publishing and self._failure is None:
                self._lock.wait()
            if self._failure is not None:
                return self._failure
            self._publishing = True
        try:
            if notify and self._on_abort is not None:
                self._on_abort(failure)
        except Exception:
            # State publication is retried by the outer source/job catch;
            # never replace the classified model failure with telemetry or
            # persistence plumbing from this window thread.
            pass
        finally:
            # Publish the circuit only after the first-abort callback returns.
            # Concurrent abort/raise callers wait on the Condition, so
            # window/source drain cannot overtake durable ``stopping``.
            with self._lock:
                self._failure = failure
                self._event.set()
                self._publishing = False
                self._lock.notify_all()
            # 熔断已发布后才唤醒子控制器:它们的 failure 属性回退到父的,先设事件
            # 后设 failure 会让子作用域醒来时读到空 failure、当成没中止。
            with self._children_lock:
                children = list(self._children)
                self._children.clear()
            for sub in children:
                sub._event.set()
        return failure

    def raise_if_aborted(self) -> None:
        # 父(任务级)熔断优先:子作用域的跳过语义只对**自己**发起的中止成立,
        # 任务级熔断必须原样穿透,否则 Ctrl-C / 升级熔断会被当成"跳过这一个源"。
        if self._parent is not None:
            self._parent.raise_if_aborted()
        with self._lock:
            while self._publishing and self._failure is None:
                self._lock.wait()
            failure = self._failure
        if failure is not None:
            raise KgBuildAborted(failure)

    def wait_backoff(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            self.raise_if_aborted()


def _failure_for(exc: Exception) -> KgBuildFailure | None:
    code = str(getattr(exc, "code", "") or "")
    if code in {
        "model_queue_full",
        "model_queue_timeout",
        "model_service_unavailable",
        "provider_rate_limited",
        "provider_unavailable",
    }:
        return KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
    if code == "provider_auth":
        return KgBuildFailure("model_auth_failed", MODEL_AUTH_FAILED_MESSAGE)
    if code in {
        "unknown_model",
        "model_not_found",
        "model_rejected",
        "protocol_mismatch",
        "unsupported_protocol",
        "capability_mismatch",
        "unsupported_capability",
    }:
        return KgBuildFailure(
            "model_request_rejected", MODEL_REQUEST_REJECTED_MESSAGE
        )
    status = llm_status_code(exc)
    if is_transient_llm_error(exc):
        return KgBuildFailure("model_unavailable", MODEL_UNAVAILABLE_MESSAGE)
    if status in (401, 403):
        return KgBuildFailure("model_auth_failed", MODEL_AUTH_FAILED_MESSAGE)
    if status is not None:
        return KgBuildFailure(
            "model_request_rejected",
            MODEL_REQUEST_REJECTED_MESSAGE,
        )
    return None


class TaskScopedKgClient:
    def __init__(
        self,
        delegate: Any,
        settings: Settings,
        control: KgExtractionRunControl,
    ):
        self._delegate = delegate
        self._settings = settings
        self.control = control

    @property
    def configured(self) -> bool:
        return bool(getattr(self._delegate, "configured", False))

    @property
    def model(self) -> str:
        return str(getattr(self._delegate, "model", ""))

    @property
    def settings(self) -> Settings:
        return self._settings

    def chat_json(
        self,
        messages: list[dict[str, str]],
        response_schema_hint: str,
        **kwargs: Any,
    ) -> str:
        attempts = 1 + self._settings.kg_llm_max_retries
        call_kwargs: dict[str, Any] = {
            **kwargs,
            "timeout": self._settings.kg_llm_timeout_seconds,
            "max_retries": 0,
            # A non-null cancellation signal selects the shared client's
            # streaming transport. Long KG replies then remain alive while
            # chunks arrive and an opened circuit can stop sibling streams.
            "cancel_event": self.control.cancel_event,
        }
        method = getattr(self._delegate, "chat_json", None)
        if not callable(method):
            raise AttributeError("configured KG client has no chat_json method")
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):
            signature = None
        if (
            signature is not None
            and not any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
        ):
            call_kwargs = {
                key: value for key, value in call_kwargs.items()
                if key in signature.parameters
            }
        for attempt in range(attempts):
            self.control.raise_if_aborted()
            try:
                result = method(
                    messages,
                    response_schema_hint,
                    **call_kwargs,
                )
                self.control.raise_if_aborted()
                return result
            except KgBuildAborted:
                raise
            except Exception as exc:
                # A sibling may have opened the circuit while this request was
                # consuming its stream. The transport reports cancellation;
                # preserve the KG failure that caused it rather than leaking a
                # generic AskCancelled from the shared client.
                if self.control.aborted:
                    self.control.raise_if_aborted()
                failure = _failure_for(exc)
                if failure is None:
                    raise
                if is_transient_llm_error(exc) and attempt + 1 < attempts:
                    backoff = min(2 ** attempt, 30)
                    self.control.wait_backoff(
                        backoff + random.uniform(0, backoff)
                    )
                    continue
                first_failure = self.control.abort(failure)
                raise KgBuildAborted(first_failure) from exc
        raise AssertionError("KG model attempt loop exited unexpectedly")


class TaskScopedKgClients:
    """Resolve and cache controlled adapters for all KG chat workloads."""

    def __init__(self, provider: Any, settings: Settings, control: KgExtractionRunControl):
        self._provider = provider
        self._settings = settings
        self.control = control
        self._clients: dict[str, TaskScopedKgClient] = {}

    def chat(self, workload_id: str) -> TaskScopedKgClient:
        client = self._clients.get(workload_id)
        if client is None:
            client = TaskScopedKgClient(
                self._provider.chat(workload_id), self._settings, self.control
            )
            self._clients[workload_id] = client
        return client

    def configured(self, workload_id: str) -> bool:
        return self._provider.configured(workload_id)

    def parallelism(self, workload_id: str) -> int:
        return self._provider.parallelism(workload_id)


def probe_kg_model(client: TaskScopedKgClient) -> None:
    if not callable(getattr(client._delegate, "chat_json", None)):
        # Compatibility callers historically used configured-only test doubles
        # when no extraction target existed. Production model clients always
        # implement chat_json and are still probed before any destructive work.
        return
    try:
        client.chat_json(
            [{"role": "user", "content": 'Return {"ok":true} and nothing else.'}],
            '{"ok":true}',
            # Do not impose a second, tiny completion budget here. Reasoning-capable
            # providers may spend that entire budget before emitting visible JSON,
            # yielding an HTTP-200 response with empty ``content``. Omitting the
            # override reuses chat_json's single configured short-output budget.
            bypass_cache=True,
        )
    except Exception as exc:
        # This classification is probe-only. Extraction windows intentionally
        # isolate malformed JSON as a failed window, while optional refine/glean
        # stages degrade best-effort; converting it in _failure_for() would turn
        # all three into a notebook-wide circuit break.
        if str(getattr(exc, "code", "") or "") != "malformed_response":
            raise
        failure = client.control.abort(
            KgBuildFailure(
                "model_response_invalid", MODEL_RESPONSE_INVALID_MESSAGE
            )
        )
        raise KgBuildAborted(failure) from exc
