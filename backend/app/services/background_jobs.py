"""后台 fire-and-forget job 的唯一入口。

所有由 HTTP 请求触发的后台 daemon 线程都应经 `submit()` 提交，以保证三条不变量：

1. **一律 `contextvars.copy_context()` 传播发起请求的上下文。** worker 线程默认
   *不*继承调用线程的 ContextVar；不传播时 `_REQUEST_USER` 读空 → `current_user()`
   回退 `user-local` → 后台 job 会用 **user-local（系统默认）的模型**而非发起用户
   自己配置的模型，per-user 日志也落错子目录。event_logging 的设计注释即要求后台
   job「经 copy_context() 自然带上」`_REQUEST_USER` 与 `_log_owner`。
2. **顶层异常统一兜底**（best-effort 日志），不让 worker 线程静默崩溃。
3. **统一 daemon 命名线程**，便于诊断。

收敛前，routes.py 里 6 处线程启动各写各的：report/merge-review/ask 记得 copy_context、
build/rebuild-KG/conflict-resolve 忘了——正是「手抄易漏」的整类 bug。
"""
from __future__ import annotations

import contextvars
import logging
import re
import threading
from typing import Callable

from app.core import diagnostics_runtime as diagnostics
from app.core.request_context import request_user_id
from app.services.pending_bus import pending_bus

_log = logging.getLogger("silicon_notebook.jobs")
_SAFE_JOB_PREFIXES = (
    ("knowhow-legacy-reproject-", "knowhow-legacy-reproject"),
    ("knowhow-asset-sweep:", "knowhow-asset-sweep"),
    ("knowhow-project-", "knowhow-project"),
    ("conflictresolve-", "conflictresolve"),
    ("mergereview-", "mergereview"),
    ("report-plan-", "report-plan"),
    ("report-gen-", "report-gen"),
    ("rebuildkg-", "rebuildkg"),
    ("papermeta-", "papermeta"),
    ("buildkg-", "buildkg"),
)
_SAFE_ASK_JOB_NAMES = frozenset({"ask-chunk", "ask-reasoning", "ask-graph"})
_CALLABLE_OPERATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")


def _resolve_job_user() -> str | None:
    """在 job 线程(copy_context 已传播)里解析发起用户 id。"""
    try:
        return request_user_id()
    except Exception:  # noqa: BLE001
        return None


def _diagnostic_job_name(fn: Callable, label: str) -> str:
    if label in _SAFE_ASK_JOB_NAMES:
        return label
    for prefix, operation in _SAFE_JOB_PREFIXES:
        if label.startswith(prefix):
            return operation
    callable_name = getattr(fn, "__name__", "")
    if isinstance(callable_name, str) and _CALLABLE_OPERATION.fullmatch(callable_name):
        return callable_name
    return "background_job"


def submit(fn: Callable, *args, name: str | None = None,
           notify_pending: bool = False, **kwargs) -> threading.Thread:
    """在传播了调用线程上下文的 daemon 线程里跑 `fn(*args, **kwargs)`，返回该线程。

    快照必须在调用线程（通常是请求处理线程）里捕获，故 copy_context() 在此同步调用。

    `notify_pending=True` 时，job 完成（无论成功/失败）后刷新发起用户的「待确认中心」
    snapshot；通知失败绝不影响/冒泡出 job 本身。
    """
    ctx = contextvars.copy_context()
    label = name or getattr(fn, "__name__", "job")
    try:
        diagnostic_name = _diagnostic_job_name(fn, label)
    except Exception:  # noqa: BLE001 — diagnostics metadata is best-effort only
        diagnostic_name = "background_job"

    def _run() -> None:
        try:
            with diagnostics.job_scope(diagnostic_name):
                try:
                    fn(*args, **kwargs)
                except Exception:  # noqa: BLE001 — 后台线程顶层兜底，绝不静默死
                    _log.exception("background job failed: %s", diagnostic_name)
                    raise
                finally:
                    if notify_pending:
                        uid = _resolve_job_user()
                        if uid:
                            try:
                                pending_bus.mark_dirty(uid)
                            except Exception:  # noqa: BLE001 — 通知失败绝不影响 job
                                _log.exception(
                                    "pending mark_dirty failed: %s", diagnostic_name
                                )
        except Exception:
            # The exception was already logged inside the observed lifecycle;
            # preserve submit()'s fire-and-forget isolation contract.
            pass

    thread = threading.Thread(target=lambda: ctx.run(_run), name=name, daemon=True)
    thread.start()
    return thread
