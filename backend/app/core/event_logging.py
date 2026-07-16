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
import gzip
import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple
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


# 日志归档：把「非今天」的天文件 gzip。单线程池串行执行，best-effort，绝不阻塞写入。
_archive_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")
_DATED_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _gzip_day_file(plain: Path) -> None:
    """把某天明文 jsonl 压成同名 .gz，再删明文。先写 .gz.tmp 再原子 rename，故读取器
    「先明文缺则 .gz」在任一时刻至少一份可读、绝不读到半个 gz。异常吞掉（下次启动补）。"""
    try:
        plain = Path(plain)
        if not plain.exists():
            return
        gz = plain.parent / (plain.name + ".gz")
        if gz.exists():
            return
        tmp = plain.parent / (plain.name + ".gz.tmp")
        with plain.open("rb") as fin, gzip.open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, gz)
        plain.unlink()
    except Exception:  # pragma: no cover - 归档失败不致命
        pass


# 跨天补压账目：(目录, channel) → 最近一次写入所属的 day 字符串。本进程内某序列
# 首次见到新 day 时，把「上一 day」的明文提交压缩；O(1) 摊还，emit 热路径零额外 IO。
_last_write_day: "Dict[Tuple[str, str], str]" = {}
_last_write_lock = threading.Lock()


def archive_stale_days(settings) -> None:
    """启动扫一遍：glob 全局与 per-user 一层子目录下的带日期明文，date<today 且无 .gz
    者提交压缩。老无日期单文件（legacy）不匹配 _DATED_RE，天然不碰。best-effort。"""
    try:
        log_dir = Path(getattr(settings, "event_log_dir", ".local/logs"))
        if not log_dir.is_absolute():
            log_dir = _ROOT_DIR / log_dir
        if not log_dir.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for p in list(log_dir.glob("*.jsonl")) + list(log_dir.glob("*/*.jsonl")):
            m = _DATED_RE.search(p.name)
            if m and m.group(1) < today:
                _archive_pool.submit(_gzip_day_file, p)
    except Exception:  # pragma: no cover
        pass


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

    def _dir(self) -> Path:
        if not self.per_user:
            return self.log_dir
        try:
            sub = owner_dir(get_log_owner())
        except Exception:  # pragma: no cover
            sub = "_system"
        return self.log_dir / sub

    def _target_path_for_day(self, day: str) -> Path:
        return self._dir() / f"{self.channel}-{day}.jsonl"

    def emit(self, event: Dict[str, Any], *, console: str = "") -> None:
        """Append `event` as a JSON line and emit a brief console line.

        `console` overrides the auto console summary. Wrapped so a logging
        failure never propagates to the caller. Writes to a per-day file
        (`<channel>-YYYY-MM-DD.jsonl`); when this call crosses a day boundary
        for this (dir, channel), the previous day's plain file is enqueued
        for background gzip.
        """
        if not self.enabled:
            return
        now = datetime.now()
        event.setdefault("ts", now.isoformat())
        event.setdefault("channel", self.channel)
        day = now.strftime("%Y-%m-%d")
        target = self._target_path_for_day(day)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover - logging must not break flow
            self.logger.warning("failed to write %s log line", self.channel, exc_info=False)
        self._maybe_archive_prev_day(target.parent, day)

        status = event.get("status")
        line = console or self._auto_console(event)
        if status in (None, "ok", "done", "start"):
            self.logger.info("%s", line)
        elif status == "slow":
            self.logger.warning("SLOW %s", line)
        else:
            self.logger.warning("%s", line)

    def _maybe_archive_prev_day(self, dir_: Path, day: str) -> None:
        """本进程内某 (目录, channel) 序列首次见到新 day 时，把上一 day 的明文提交
        压缩。prev is None（本进程首写）不压——无从判断是否翻天；启动时的
        archive_stale_days 已经扫过历史遗留。O(1) 摊还，绝不阻塞 emit。"""
        try:
            key = (str(dir_), self.channel)
            with _last_write_lock:
                prev = _last_write_day.get(key)
                should = prev is not None and prev != day
                if prev != day:
                    _last_write_day[key] = day
            if should:
                _archive_pool.submit(_gzip_day_file, dir_ / f"{self.channel}-{prev}.jsonl")
        except Exception:  # pragma: no cover - 归档入队绝不破坏 emit
            pass

    # 源处理流水线状态 → 给人看的中文阶段名。仅用于 console 摘要(logger 输出);
    # jsonl 里写入的仍是原始 status 值,前端/DB 状态机不受影响。不在表内的 status
    # (如 pipeline 的 start/done/ok)保持原样。
    _STATUS_LABELS = {
        "queued": "排队中",
        "parsing": "解析文档中",
        "parsed": "解析完成",
        "extracting": "抽取知识图谱中",
        "extracted": "处理完成",
        "failed": "失败",
    }

    @staticmethod
    def _auto_console(event: Dict[str, Any]) -> str:
        parts = []
        for key in ("kind", "stage", "method", "path", "status", "status_code"):
            if key in event and event[key] not in (None, ""):
                val = event[key]
                # 源状态事件(kind=status): 略去技术性 kind, status 值译成中文阶段名,
                # 让日志对人可读(如「解析文档中」而非「status parsing」)。
                if key == "kind" and val == "status":
                    continue
                if key == "status":
                    val = EventLogger._STATUS_LABELS.get(val, val)
                parts.append(str(val))
        if "latency_ms" in event:
            parts.append(f"{event['latency_ms']}ms")
        if event.get("error"):
            parts.append(f"err={event['error']}")
        return " ".join(parts) or "event"
