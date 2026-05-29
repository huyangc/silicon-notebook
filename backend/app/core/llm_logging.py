"""Structured logging for interactions with the LLM service.

Thin wrapper over the generic `EventLogger` that keeps the dedicated
`llm.jsonl` channel and the `llm_log_path` / `llm_log_enabled` settings for
backward compatibility. Records every chat / embedding call: whether it
happened, succeeded, how long it took, token usage, and — when it fails — the
error that otherwise gets swallowed by the callers' fallbacks.

Logging never affects the main flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from app.core.config import Settings
from app.core.event_logging import EventLogger, _ROOT_DIR, new_id


def new_interaction_id() -> str:
    return new_id("llm")


class LLMInteractionLogger:
    def __init__(self, settings: Settings):
        # Reuse EventLogger's single write/console implementation, but honor the
        # dedicated LLM settings (path + enable flag) for backward compatibility.
        self._events = EventLogger(settings, channel="llm")
        self._events.enabled = settings.llm_log_enabled
        self._events.max_chars = max(0, int(settings.llm_log_max_chars))
        path = Path(settings.llm_log_path)
        if not path.is_absolute():
            path = _ROOT_DIR / path
        self._events.path = path
        if self._events.enabled:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:  # pragma: no cover - never break startup
                self._events.logger.warning("could not create llm log dir at %s", path.parent)

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
