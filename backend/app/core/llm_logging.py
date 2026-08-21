"""Structured logging for interactions with the LLM service.

Thin wrapper over the generic `EventLogger` that keeps the dedicated
`llm.jsonl` channel and the `llm_log_path` / `llm_log_enabled` settings for
backward compatibility. Records every chat / embedding call: whether it
happened, succeeded, how long it took, token usage, and — when it fails — the
error that otherwise gets swallowed by the callers' fallbacks.

Logging never affects the main flow.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator

from app.core.config import Settings
from app.core.event_logging import EventLogger, _ROOT_DIR, new_id


def new_interaction_id() -> str:
    return new_id("llm")


_INTERACTION_SUPPORT_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_interaction_support_id", default=""
)


@contextmanager
def interaction_support_scope(support_id: str) -> Iterator[None]:
    """Correlate a raw protocol log with its scheduled invocation."""
    token = _INTERACTION_SUPPORT_ID.set(str(support_id))
    try:
        yield
    finally:
        _INTERACTION_SUPPORT_ID.reset(token)


def current_interaction_support_id() -> str:
    """Return the opaque scheduler correlation id for the active model call."""
    return _INTERACTION_SUPPORT_ID.get()


class LLMInteractionLogger:
    def __init__(self, settings: Settings):
        # Reuse EventLogger's single write/console implementation in per-user mode;
        # honor the dedicated LLM settings (path + enable flag) for backward compat.
        self._events = EventLogger(settings, channel="llm", per_user=True)
        self._events.enabled = settings.llm_log_enabled
        self._events.max_chars = max(0, int(settings.llm_log_max_chars))
        path = Path(settings.llm_log_path)
        if not path.is_absolute():
            path = _ROOT_DIR / path
        # per-user：base_dir + filename 取自 llm_log_path；owner 子目录在 emit 按需建。
        self._events.log_dir = path.parent
        self._events.filename = path.name
        self._events.path = path  # 兼容属性（.path 仍被 smoke / llm.py 读取）

    # Backward-compatible surface used by the smoke tests and llm.py.
    @property
    def enabled(self) -> bool:
        return self._events.enabled

    @property
    def path(self) -> Path:
        return self._events.path

    @path.setter
    def path(self, value: Path) -> None:
        self._events.path = value

    def clip(self, text: Any) -> str:
        return self._events.clip(text)

    def log(self, record: Dict[str, Any]) -> None:
        support_id = _INTERACTION_SUPPORT_ID.get()
        if support_id and not record.get("support_id"):
            record = {**record, "support_id": support_id}
        kind = record.get("kind", "?")
        model = record.get("model", "?")
        latency = record.get("latency_ms", "?")
        if record.get("status") == "ok":
            usage = record.get("usage") or {}
            extra = (
                f"tokens={usage.get('total_tokens')}"
                if usage
                else (f"dims={record['dims']}" if "dims" in record else "")
            )
            console = f"{kind} ok model={model} latency={latency}ms {extra}".rstrip()
        else:
            console = (
                f"{kind} ERROR model={model} latency={latency}ms "
                f"err={record.get('error', '')}"
            )
        self._events.emit(record, console=console)
