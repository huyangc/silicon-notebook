from __future__ import annotations

from dataclasses import dataclass
import random
import threading
from typing import Any

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
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._failure: KgBuildFailure | None = None

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    @property
    def failure(self) -> KgBuildFailure | None:
        with self._lock:
            return self._failure

    def abort(self, failure: KgBuildFailure) -> KgBuildFailure:
        with self._lock:
            if self._failure is None:
                self._failure = failure
                self._event.set()
            return self._failure

    def raise_if_aborted(self) -> None:
        failure = self.failure
        if failure is not None:
            raise KgBuildAborted(failure)

    def wait_backoff(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            self.raise_if_aborted()


def _failure_for(exc: Exception) -> KgBuildFailure | None:
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
        call_kwargs = {
            **kwargs,
            "timeout": self._settings.kg_llm_timeout_seconds,
            "max_retries": 0,
        }
        for attempt in range(attempts):
            self.control.raise_if_aborted()
            try:
                result = self._delegate.chat_json(
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


def probe_kg_model(client: TaskScopedKgClient) -> None:
    client.chat_json(
        [{"role": "user", "content": 'Return {"ok":true} and nothing else.'}],
        '{"ok":true}',
        max_tokens=16,
    )
