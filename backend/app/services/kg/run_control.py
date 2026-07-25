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
    ):
        self.job_id = job_id
        self._event = threading.Event()
        self._lock = threading.Condition()
        self._failure: KgBuildFailure | None = None
        self._publishing = False
        self._on_abort = on_abort

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def failure(self) -> KgBuildFailure | None:
        with self._lock:
            return self._failure

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
        return failure

    def raise_if_aborted(self) -> None:
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
    client.chat_json(
        [{"role": "user", "content": 'Return {"ok":true} and nothing else.'}],
        '{"ok":true}',
        max_tokens=16,
        bypass_cache=True,
    )
