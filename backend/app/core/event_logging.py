"""Generic structured event logging.

A single tiny base used by all observability in the backend: HTTP request logs,
async pipeline stage logs, status-machine transitions, and (via
`llm_logging.LLMInteractionLogger`) LLM interaction logs.

Each `EventLogger` writes one JSON line per event to `.local/logs/<channel>.jsonl`
and emits a brief line on `silicon_notebook.<channel>`. Writing is best-effort:
a logging failure can never break the request / pipeline it is observing.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4

from app.core.config import Settings

_ROOT_DIR = Path(__file__).resolve().parents[3]


def new_id(prefix: str = "ev") -> str:
    return f"{prefix}-{uuid4().hex[:8]}"


# 请求级「日志归属」槽：由 sqlite_repository.set_request_user 同步维护，emit 时据此
# 决定写哪个用户子目录。与 _REQUEST_USER 配对，但定义在 core 层以免 EventLogger 依赖
# service 层（保持分层）。后台 job 经 contextvars.copy_context() 自然带上。
_log_owner: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "log_owner", default=None)

_OWNER_RE = re.compile(r"^user-[a-z0-9]+$")


def set_log_owner(owner: "str | None"):
    return _log_owner.set(owner)


def reset_log_owner(token) -> None:
    _log_owner.reset(token)


def get_log_owner() -> "str | None":
    return _log_owner.get()


def is_safe_owner(owner: str) -> bool:
    """owner 子目录名白名单：owner = user.id，形如 user-local（seeded admin）或
    user-<hex>（注册用户）；外加系统兜底 _system。禁 / 和 .. 防路径穿越。"""
    return owner == "_system" or bool(_OWNER_RE.match(owner or ""))


def owner_dir(owner: "str | None") -> str:
    """把当前 owner 映射到日志子目录名。owner 必须是 user.id（user-local 或 user-<hex>）。
    空/未设 → 'user-local'（离线/本地即 seeded admin）；非法（username 等非 id 值）→
    '_system' 兜底。"""
    if not owner:
        return "user-local"
    return owner if is_safe_owner(owner) else "_system"


def _anchor(p: "str | Path") -> Path:
    """相对路径锚定到仓库根（与 EventLogger 解析 log_dir 的规则一致）。"""
    p = Path(p)
    return p if p.is_absolute() else _ROOT_DIR / p


def llm_log_dir_aligned(llm_log_path: "str | Path", event_log_dir: "str | Path") -> bool:
    """per-user llm 日志写在 dirname(llm_log_path)/<owner>/，而 debug_logs 按
    event_log_dir/<owner>/ 读；两者锚定到仓库根后须指向同一目录，否则查看器读不到
    per-user 的 llm 日志。相对/绝对混用时必须先锚定再比较（字符串比较会误报）。"""
    return _anchor(llm_log_path).parent.resolve() == _anchor(event_log_dir).resolve()


class EventLogger:
    def __init__(self, settings: Settings, channel: str = "events", *, per_user: bool = False):
        self.channel = channel
        self.enabled = getattr(settings, "event_log_enabled", True)
        self.max_chars = max(0, int(getattr(settings, "llm_log_max_chars", 4000)))
        self.logger = logging.getLogger(f"silicon_notebook.{channel}")
        log_dir = Path(getattr(settings, "event_log_dir", ".local/logs"))
        if not log_dir.is_absolute():
            log_dir = _ROOT_DIR / log_dir
        self.per_user = per_user
        self.log_dir = log_dir
        self.filename = f"{channel}.jsonl"
        self.path = log_dir / self.filename  # 全局路径（per_user=False 用；也是历史兼容属性）
        if self.enabled and not per_user:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:  # pragma: no cover - never break startup
                self.logger.warning("could not create log dir at %s", self.path.parent)

    def clip(self, text: Any) -> str:
        text = "" if text is None else str(text)
        if self.max_chars and len(text) > self.max_chars:
            return text[: self.max_chars] + f"...[+{len(text) - self.max_chars} chars]"
        return text

    def _target_path(self) -> Path:
        if not self.per_user:
            return self.path
        try:
            sub = owner_dir(get_log_owner())
        except Exception:  # pragma: no cover - owner 解析绝不破坏写入
            sub = "_system"
        return self.log_dir / sub / self.filename

    def emit(self, event: Dict[str, Any], *, console: str = "") -> None:
        """Append `event` as a JSON line and emit a brief console line.

        `console` overrides the auto console summary. Wrapped so a logging
        failure never propagates to the caller.
        """
        if not self.enabled:
            return
        event.setdefault("ts", datetime.now().isoformat())
        event.setdefault("channel", self.channel)
        target = self._target_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
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
