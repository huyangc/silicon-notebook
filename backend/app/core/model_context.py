"""Request capacity and transport admission, independent of model routing."""
from __future__ import annotations

import json
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from app.core.config import DEFAULT_MODEL_CONTEXT_SAFETY_RATIO
from app.core.model_schema import response_format_for_hint

_PROVIDER_WRAPPER_PREFIX = (
    "You are the extraction and reasoning engine for "
    "silicon-notebook. Return valid JSON only, no markdown fences. "
    "Schema hint: "
)


def provider_messages(messages, response_schema_hint: str):
    """Assemble the wire messages once for counting, cache and transport.

    Caller mappings pass through by reference without mutation.
    """
    return [{"role": "system", "content": f"{_PROVIDER_WRAPPER_PREFIX}{response_schema_hint}"}, *messages]

# Protocol framing and hidden provider templates require headroom even when
# the visible payload has been counted. Unknown tokenizers use the UTF-8 byte
# ceiling rather than assuming a model name implies a particular vocabulary.
CONTEXT_SAFETY_RATIO = DEFAULT_MODEL_CONTEXT_SAFETY_RATIO
_ADMISSION: ContextVar[Callable[[], None] | None] = ContextVar("model_request_admission", default=None)


class ModelContextExceeded(ValueError):
    code = "model_context_exceeded"

    def __init__(self) -> None:
        super().__init__("本次材料超过当前模型的上下文容量，请减少材料或配置容量更大的模型。")


class RequestAdmissionError(RuntimeError):
    code = "report_context_budget_exhausted"

    def __init__(self) -> None:
        super().__init__("本轮报告的额外模型请求预算已耗尽。")


@contextmanager
def request_admission(callback: Callable[[], None]):
    token = _ADMISSION.set(callback)
    try:
        yield
    finally:
        _ADMISSION.reset(token)


def admit_request() -> None:
    callback = _ADMISSION.get()
    if callback is not None:
        try:
            callback()
        except Exception as exc:
            raise RequestAdmissionError() from exc


def is_context_error(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "code", None) in {
            "model_context_exceeded", "context_length_exceeded", "max_context_length",
            "context_window_exceeded", "input_too_long", "prompt_too_long",
        }:
            return True
        body = getattr(current, "body", None)
        if isinstance(body, dict):
            detail = body.get("error", body)
            if isinstance(detail, dict) and detail.get("code") in {
                "context_length_exceeded", "max_context_length", "context_window_exceeded",
                "input_too_long", "prompt_too_long",
            }:
                return True
        # Some compatible APIs omit machine codes. Require a client-error
        # status and explicit context wording, never classify all 400s alike.
        if getattr(current, "status_code", None) in (400, 413, 422):
            text = str(current).lower()
            if any(marker in text for marker in (
                "maximum context length", "context length exceeded",
                "context window exceeded", "input tokens exceed", "prompt is too long",
            )):
                return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class ContextBudget:
    context_window_tokens: int
    max_output_tokens: int
    input_limit: int | None = None
    counting_method: str = "conservative_utf8_bytes"
    safety_ratio: float = DEFAULT_MODEL_CONTEXT_SAFETY_RATIO

    @property
    def max_input_tokens(self) -> int:
        available = max(0, self.context_window_tokens - self.max_output_tokens
                        - math.ceil(self.context_window_tokens * self.safety_ratio))
        return min(available, self.input_limit) if self.input_limit is not None else available

    def input_tokens(self, messages, schema_hint: str) -> int:
        return self.wire_input_tokens(provider_messages(messages, schema_hint), response_format_for_hint(schema_hint))

    def wire_input_tokens(self, messages, response_format=None) -> int:
        payload = {"messages": messages}
        if response_format is not None:
            payload["response_format"] = response_format
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def fits(self, messages, schema_hint: str) -> bool:
        return self.input_tokens(messages, schema_hint) <= self.max_input_tokens

    def check_wire(self, messages, response_format=None) -> None:
        if self.wire_input_tokens(messages, response_format) > self.max_input_tokens:
            raise ModelContextExceeded()


def service_context_budget(service, requested_output: int | None = None, *,
                           safety_ratio: float = DEFAULT_MODEL_CONTEXT_SAFETY_RATIO) -> ContextBudget:
    output = service.max_output_tokens
    window = service.context_window_tokens
    if not isinstance(output, int) or not isinstance(window, int):
        raise ValueError("聊天模型缺少容量配置，请填写 context_window_tokens 和 max_output_tokens。")
    if isinstance(requested_output, int) and requested_output > 0:
        output = min(output, requested_output)
    return ContextBudget(window, output, service.max_input_tokens, safety_ratio=safety_ratio)
