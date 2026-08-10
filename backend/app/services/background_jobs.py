"""后台 fire-and-forget job 的唯一入口。

所有由 HTTP 请求触发的后台 daemon 线程都应经 `submit()` 提交，以保证三条不变量：

1. **一律 `contextvars.copy_context()` 传播发起请求的上下文。** worker 线程默认
   *不*继承调用线程的 ContextVar；不传播时 `_REQUEST_USER` 读空 → `current_user()`
   回退 `user-local` → 后台 job 会用 **user-local（系统默认）的模型**而非发起用户
   自己配置的模型，per-user 日志也落错子目录。event_logging 的设计注释即要求后台
   job「经 copy_context() 自然带上」`_REQUEST_USER` 与 `_log_owner`。
2. **顶层异常统一兜底**（best-effort 日志），不让 worker 线程静默崩溃。
3. **统一 daemon 命名线程**，便于诊断。
4. **后台 job 的进程级并发闸，重活与轻活分两个池**（`BACKGROUND_MAINTENANCE_CONCURRENCY`
   / `BACKGROUND_LIGHT_JOB_CONCURRENCY`）。各类 job 各自已有单飞/去重闸，但彼此之间
   没有；同一时刻多个笔记本各排一个重活，数据库与模型扇出会成倍叠加。分池是因为量级
   差：小时级的全库重建不得饿死秒级的单表投影。闸在 worker 线程内获取——`submit()`
   仍立即返回；排队超过 `_QUEUE_WARN_SECONDS` 由日志披露。

收敛前，routes.py 里 6 处线程启动各写各的：report/merge-review/ask 记得 copy_context、
build/rebuild-KG/conflict-resolve 忘了——正是「手抄易漏」的整类 bug。
"""
from __future__ import annotations

import contextvars
import inspect
import logging
import re
import threading
import time
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
    ("catalog-", "catalog"),
    ("mergereview-", "mergereview"),
    ("report-plan-", "report-plan"),
    ("report-gen-", "report-gen"),
    ("rebuildkg-", "rebuildkg"),
    ("relinkkg-", "relinkkg"),
    ("unifiedkg-", "unifiedkg"),
    ("papermeta-", "papermeta"),
    ("buildkg-", "buildkg"),
)
_SAFE_ASK_JOB_NAMES = frozenset({"ask-chunk", "ask-reasoning", "ask-graph"})
_CALLABLE_OPERATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,79}$")
# 进闸的后台 job 类别(取 `_SAFE_JOB_PREFIXES` 的 operation 名)。每一类各自已有
# 单飞/去重闸,但它们**互相之间**此前没有闸:同一时刻若干笔记本各排一个活,数据库
# 连接与模型扇出会成倍叠加。
#
# **两个池而不是一个**,判据是量级差而不是重要性:重活是小时级的全库重建/分析,
# 轻活是秒级的单表投影、单来源识别、单来源元数据补抽。合用一个池时,四个重建就能
# 把一个用户点一下就该出结果的格子投影饿死到几十分钟后——那不是「后台任务排队」,
# 是产品坏了。两个池各自有独立容量,互不占用。
#
# 刻意**不进闸**的两类:
#   * `ask-*`  —— 用户在线等的交互路径,排队即体感卡死;
#   * `report-*` —— worker 内已有 `ReportGenerationGate` 整篇准入闸,再套一层
#     只会让两把闸的容量互相干扰。
_HEAVY_MAINTENANCE_OPERATIONS = frozenset({
    "buildkg",
    "rebuildkg",
    "relinkkg",
    "unifiedkg",
    "conflictresolve",
    "mergereview",
})
_LIGHT_MAINTENANCE_OPERATIONS = frozenset({
    "papermeta",
    "catalog",
    "knowhow-project",
    "knowhow-legacy-reproject",
    "knowhow-asset-sweep",
})
_MAINTENANCE_OPERATIONS = _HEAVY_MAINTENANCE_OPERATIONS | _LIGHT_MAINTENANCE_OPERATIONS
_HEAVY_POOL = "maintenance"
_LIGHT_POOL = "light"
_DEFAULT_MAINTENANCE_CONCURRENCY = 4
_DEFAULT_LIGHT_JOB_CONCURRENCY = 4
# 排队超过这么久就记一条 warning:运维要能分辨「任务在跑但很慢」与「任务压根还没
# 开始跑」。库里的任务状态在排队期间仍是 running/queued(闸不写数据库,也不该写:
# 那会让一个纯进程内的准入决定变成持久状态),所以日志是目前唯一的区分手段。
_QUEUE_WARN_SECONDS = 30.0
_gate_lock = threading.Lock()
_gates: dict[str, threading.BoundedSemaphore] = {}


def _maintenance_pool(name: str | None) -> tuple[str, str] | None:
    """按**显式** job 名前缀判定 (池名, 类别名);不进闸返回 None。

    刻意只看调用方给的 `name`,不看 `_diagnostic_job_name` 回退出来的可调用对象
    名:那条回退是给诊断展示用的,拿它当准入判据会让「某个函数恰好叫 buildkg」
    这种巧合改变并发行为。名字不在清单里 = 不进闸(保守放行,绝不误挡)。
    """
    if not name:
        return None
    for prefix, operation in _SAFE_JOB_PREFIXES:
        if not name.startswith(prefix):
            continue
        if operation in _HEAVY_MAINTENANCE_OPERATIONS:
            return _HEAVY_POOL, operation
        if operation in _LIGHT_MAINTENANCE_OPERATIONS:
            return _LIGHT_POOL, operation
        return None
    return None


def _pool_capacity(pool: str) -> int:
    default = (
        _DEFAULT_MAINTENANCE_CONCURRENCY if pool == _HEAVY_POOL
        else _DEFAULT_LIGHT_JOB_CONCURRENCY
    )
    try:
        from app.core.config import get_settings

        settings = get_settings()
        configured = (
            settings.background_maintenance_concurrency if pool == _HEAVY_POOL
            else settings.background_light_job_concurrency
        )
        return max(1, int(configured))
    except Exception:  # noqa: BLE001 — 配置不可用时保守用默认容量,绝不放弃闸
        return default


def _maintenance_slot(
    name: str | None,
) -> tuple[str, str, threading.BoundedSemaphore] | None:
    """进闸 job 的 (池名, 类别名, 信号量);非进闸类别返回 None(零开销)。

    惰性建闸:本模块被导入时 Settings 未必就绪(且导入期读配置会把一份部署值
    钉死在 import 时刻)。每个池的容量在**首次**需要它时读一次并缓存——容量是
    进程级不变量,中途换容量会让已在等待的 worker 与新 worker 看到两个不同的上限。
    """
    resolved = _maintenance_pool(name)
    if resolved is None:
        return None
    pool, operation = resolved
    with _gate_lock:
        gate = _gates.get(pool)
        if gate is None:
            gate = threading.BoundedSemaphore(_pool_capacity(pool))
            _gates[pool] = gate
    return pool, operation, gate


def _acquire_with_queue_disclosure(
    gate: threading.BoundedSemaphore, pool: str, operation: str
) -> None:
    """阻塞取槽,排队久了在日志里说清楚(只带池名/类别名/秒数,无 id 无正文)。

    没有这条披露,一个排队中的 job 与一个卡死的 job 在外部看来完全一样。
    """
    started = time.monotonic()
    warned = False
    while not gate.acquire(timeout=_QUEUE_WARN_SECONDS):
        if not warned:
            warned = True
            _log.warning(
                "background job still queued: pool=%s operation=%s waited=%.0fs",
                pool, operation, time.monotonic() - started,
            )
    if warned:
        _log.info(
            "background job started after queueing: pool=%s operation=%s waited=%.0fs",
            pool, operation, time.monotonic() - started,
        )


def _reset_maintenance_gate_for_tests() -> None:
    """丢弃已建的闸,让下一次 submit 按当前 Settings 重新解析容量。

    仅供测试:生产里容量是进程级不变量(见 `_maintenance_slot`)。
    """
    with _gate_lock:
        _gates.clear()


def _resolve_job_user() -> str | None:
    """在 job 线程(copy_context 已传播)里解析发起用户 id。"""
    try:
        return request_user_id()
    except Exception:  # noqa: BLE001
        return None


def _diagnostic_job_name(fn: Callable, explicit_name: str | None) -> str:
    if explicit_name is not None:
        if explicit_name in _SAFE_ASK_JOB_NAMES:
            return explicit_name
        for prefix, operation in _SAFE_JOB_PREFIXES:
            if explicit_name.startswith(prefix):
                return operation
        return "background_job"
    if not (inspect.isfunction(fn) or inspect.ismethod(fn)):
        return "background_job"
    callable_name = fn.__name__
    if isinstance(callable_name, str) and _CALLABLE_OPERATION.fullmatch(callable_name):
        return callable_name
    return "background_job"


def submit(fn: Callable, *args, name: str | None = None,
           notify_pending: bool = False, **kwargs) -> threading.Thread:
    """在传播了调用线程上下文的 daemon 线程里跑 `fn(*args, **kwargs)`，返回该线程。

    快照必须在调用线程（通常是请求处理线程）里捕获，故 copy_context() 在此同步调用。

    `notify_pending=True` 时，job 完成（无论成功/失败）后刷新发起用户的「待确认中心」
    snapshot；通知失败绝不影响/冒泡出 job 本身。

    维护类 job（见 `_HEAVY_MAINTENANCE_OPERATIONS` / `_LIGHT_MAINTENANCE_OPERATIONS`）
    另受所属池的进程级并发闸约束。闸在 **worker 线程内**获取：submit 本身永远立即
    返回，请求线程不因排队而阻塞。
    """
    ctx = contextvars.copy_context()
    try:
        diagnostic_name = _diagnostic_job_name(fn, name)
    except Exception:  # noqa: BLE001 — diagnostics metadata is best-effort only
        diagnostic_name = "background_job"
    slot = _maintenance_slot(name)

    def _observed() -> None:
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

    def _run() -> None:
        if slot is None:
            _observed()
            return
        pool, operation, gate = slot
        # 排队期间刻意在 `diagnostics.job_scope` **之外**:等槽的 job 不是
        # 「正在跑的 job」,把它算进 active_jobs 会让运维看到一个永远在忙、
        # 实际一行 SQL 都没发的任务。排队本身由日志披露(见 helper)。
        _acquire_with_queue_disclosure(gate, pool, operation)
        try:
            _observed()
        finally:
            # try/finally 而非 with:释放必须覆盖 BaseException(KeyboardInterrupt/
            # SystemExit 也会穿过 worker 线程),漏一次释放就永久少一个槽位。
            gate.release()

    thread = threading.Thread(target=lambda: ctx.run(_run), name=name, daemon=True)
    thread.start()
    return thread
