"""Generic structured event logging.

A single tiny base used by all observability in the backend: HTTP request logs,
async pipeline stage logs, status-machine transitions, and (via
`llm_logging.LLMInteractionLogger`) LLM interaction logs.

Each `EventLogger` writes one JSON line per event to `.local/logs/<channel>.jsonl`
and emits a brief line on `silicon_notebook.<channel>`. Writing is best-effort:
a logging failure can never break the request / pipeline it is observing.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from app.core.config import Settings

_ROOT_DIR = Path(__file__).resolve().parents[3]


def new_id(prefix: str = "ev") -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


class EventLogger:
    def __init__(self, settings: Settings, channel: str = "events"):
        self.channel = channel
        self.enabled = getattr(settings, "event_log_enabled", True)
        self.max_chars = max(0, int(getattr(settings, "llm_log_max_chars", 4000)))
        self.logger = logging.getLogger(f"silicon_notebook.{channel}")
        log_dir = Path(getattr(settings, "event_log_dir", ".local/logs"))
        if not log_dir.is_absolute():
            log_dir = _ROOT_DIR / log_dir
        self.path = log_dir / f"{channel}.jsonl"
        if self.enabled:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:  # pragma: no cover - never break startup
                self.logger.warning("could not create log dir at %s", self.path.parent)

    def clip(self, text: Any) -> str:
        text = "" if text is None else str(text)
        if self.max_chars and len(text) > self.max_chars:
            return text[: self.max_chars] + f"...[+{len(text) - self.max_chars} chars]"
        return text

    def emit(self, event: Dict[str, Any], *, console: str = "") -> None:
        """Append `event` as a JSON line and emit a brief console line.

        `console` overrides the auto console summary. Wrapped so a logging
        failure never propagates to the caller.
        """
        if not self.enabled:
            return
        event.setdefault("ts", datetime.now().isoformat())
        event.setdefault("channel", self.channel)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover - logging must not break flow
            self.logger.warning("failed to write %s log line", self.channel, exc_info=False)

        status = event.get("status")
        line = console or self._auto_console(event)
        if status in (None, "ok", "done", "start"):
            self.logger.info("%s", line)
        elif status == "slow":
            self.logger.warning("SLOW %s", line)
        else:
            self.logger.warning("%s", line)

    @staticmethod
    def _auto_console(event: Dict[str, Any]) -> str:
        parts = []
        for key in ("kind", "stage", "method", "path", "status", "status_code"):
            if key in event and event[key] not in (None, ""):
                parts.append(str(event[key]))
        if "latency_ms" in event:
            parts.append(f"{event['latency_ms']}ms")
        if event.get("error"):
            parts.append(f"err={event['error']}")
        return " ".join(parts) or "event"
