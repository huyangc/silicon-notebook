"""后台 fire-and-forget job 的唯一入口。

所有由 HTTP 请求触发的后台 daemon 线程都应经 `submit()` 提交，以保证三条不变量：

1. **一律 `contextvars.copy_context()` 传播发起请求的上下文。** worker 线程默认
   *不*继承调用线程的 ContextVar；不传播时 `_REQUEST_USER` 读空 → `current_user()`
   回退 `user-local` → 后台 job 会用 **user-local（系统默认）的模型**而非发起用户
   自己配置的模型，per-user 日志也落错子目录。event_logging 的设计注释即要求后台
   job「经 copy_context() 自然带上」`_REQUEST_USER` 与 `_log_owner`。
2. **顶层异常统一兜底**（best-effort 日志），不让 worker 线程静默崩溃。
3. **统一 daemon 命名线程**，便于诊断。
4. **后台 job 的进程级固定 worker 池，重活与轻活分两个池**（`BACKGROUND_MAINTENANCE_CONCURRENCY`
   / `BACKGROUND_LIGHT_JOB_CONCURRENCY`）。各类 job 各自已有单飞/去重闸，但彼此之间
   没有；同一时刻多个笔记本各排一个重活，数据库与模型扇出会成倍叠加。分池是因为量级
   差：小时级的全库重建不得饿死秒级的单表投影。提交到已存在的 worker 池后
   `submit()` 仍立即返回；排队超过 `_QUEUE_WARN_SECONDS` 由日志披露。尤其不能为
   每一个等槽的 job 再创建一个会阻塞在 semaphore 上的线程。

收敛前，routes.py 里 6 处线程启动各写各的：report/merge-review/ask 记得 copy_context、
build/rebuild-KG/conflict-resolve 忘了——正是「手抄易漏」的整类 bug。
"""
from __future__ import annotations

import contextvars
import heapq
import inspect
import logging
import queue
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
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
    ("agentprofile-", "agentprofile"),
    ("retrievalexperience-", "retrievalexperience"),
    ("catalog-", "catalog"),
    ("mergereview-", "mergereview"),
    ("report-plan-", "report-plan"),
    ("report-gen-", "report-gen"),
    ("rebuildkg-", "rebuildkg"),
    ("relinkkg-", "relinkkg"),
    ("unifiedkg-", "unifiedkg"),
    ("papermeta-", "papermeta"),
    ("buildkg-", "buildkg"),
    ("index-pipeline-", "index-pipeline"),
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
    # `set_indexing_pipeline` 切换分块/索引管线时提交的整库重建
    # (`RepositoryFacade.execute_indexing_pipeline_rebuild`):chunks → 可选 KG →
    # 原子发布尾段,量级与 rebuildkg/unifiedkg 同档——都是整库重建,不是
    # 单来源/单表的轻量投影,故同样进重活池,而不是轻活池。
    "index-pipeline",
})
_LIGHT_MAINTENANCE_OPERATIONS = frozenset({
    "papermeta",
    "catalog",
    # 「AI 对这个库的理解」的巡固:一次有界 LLM 调用 + 几条有界聚合查询,与
    # catalog/papermeta 同量级,故进**轻活**池。放进重活池会被小时级的全库重建
    # 饿死——而它每 N 次来源变更才排一次,饿死等于这个特性在活跃的大库上永远
    # 不生效(恰恰是最需要它的库)。
    "agentprofile",
    # Agentic Memory P2 的检索打法蒸馏:同样是一次有界 LLM 调用 + 一条有界读,
    # 与它的 P1 兄弟同量级、同理由进**轻活**池。它比 agentprofile 更该待在这里
    # ——全库只有这一条链,进重活池被全库重建饿死就等于整个特性永不生效。
    "retrievalexperience",
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
_executor_lock = threading.Lock()
_executors: dict[str, "_MaintenanceExecutor"] = {}


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
    except Exception:  # noqa: BLE001 — 配置不可用时保守用默认容量,绝不放弃容量保护
        return default


class _JobHandle(threading.Thread):
    """兼容 ``threading.Thread`` 最常用观察接口的已提交 job 句柄。

    维护任务由进程级 worker 执行，不会为每一个 job 启动一个 ``Thread``；但大量调用
    方及测试会对 submit 的返回值调用 ``join()`` / ``is_alive()``，也会读取名字和
    daemon 标记。这个未启动的 Thread 子类把这份观察契约映射到完成事件，避免把队列
    实现细节泄露给调用方。
    """

    def __init__(self, name: str | None) -> None:
        super().__init__(name=name, daemon=True)
        self._finished = threading.Event()

    def _finish(self) -> None:
        self._finished.set()

    def start(self) -> None:
        """This is already a submitted job, not an unstarted worker Thread."""
        raise RuntimeError("background job handle is already submitted")

    def is_alive(self) -> bool:
        return not self._finished.is_set()

    def join(self, timeout: float | None = None) -> None:
        self._finished.wait(timeout)


@dataclass(slots=True)
class _QueuedMaintenanceJob:
    queue_id: int
    context: contextvars.Context
    observed: Callable[[], None]
    handle: _JobHandle
    operation: str
    submitted_at: float
    warned: bool = False


class _MaintenanceExecutor:
    """每个维护池一个固定 worker 队列，且只以固定数量的线程等待工作。

    ``queue.Queue`` 有意不把尚未执行的 fire-and-forget job 静默拒绝或丢弃。容量
    限制的是实际执行的任务数；队列期间由单个 monitor（而非一个 job 一个等待线程）
    做超时披露。容量在本 executor 创建时冻结，和旧的进程级容量合同一致。
    """

    _STOP = object()

    def __init__(self, pool: str, capacity: int) -> None:
        self.pool = pool
        self.capacity = capacity
        self._queue: queue.Queue[_QueuedMaintenanceJob | object] = queue.Queue()
        self._condition = threading.Condition()
        # OrderedDict gives O(1) removal by job id. A list.remove() here turns
        # a saturated queue draining in FIFO order into O(Q²) scans.
        self._pending: OrderedDict[int, _QueuedMaintenanceJob] = OrderedDict()
        # The monitor only needs the next disclosure deadline; scanning all
        # pending jobs on every worker dequeue would recreate O(Q²) work.
        # Stale keys are left in the heap and discarded when they reach its top.
        self._deadline_heap: list[tuple[float, int, int]] = []
        self._deadline_sequence = 0
        self._closed = False
        # Count of jobs a worker has dequeued and is currently running
        # (``item.context.run(...)`` in flight), guarded by ``self._condition``
        # the same way ``self._pending`` is. Test-only teardown (see
        # ``wait_idle``) needs BOTH "nothing queued" and "nothing running" to
        # call a pool actually idle — a job already dequeued is invisible to
        # ``self._pending`` alone.
        self._active = 0
        self._workers = [
            threading.Thread(
                target=self._worker,
                name=f"background-{pool}-{index + 1}",
                daemon=True,
            )
            for index in range(capacity)
        ]
        self._monitor = threading.Thread(
            target=self._monitor_queue,
            name=f"background-{pool}-queue-monitor",
            daemon=True,
        )
        for worker in self._workers:
            worker.start()
        self._monitor.start()

    def submit(
        self,
        context: contextvars.Context,
        observed: Callable[[], None],
        operation: str,
        name: str | None,
    ) -> _JobHandle:
        handle = _JobHandle(name)
        queued = _QueuedMaintenanceJob(
            queue_id=0,
            context=context,
            observed=observed,
            handle=handle,
            operation=operation,
            submitted_at=time.monotonic(),
        )
        with self._condition:
            if self._closed:
                # Only reachable from the test-only reset race. Production executors are
                # process-lifetime objects; retry against the replacement rather than
                # dropping a submitted job.
                raise RuntimeError("maintenance executor is closed")
            self._deadline_sequence += 1
            queued_id = self._deadline_sequence
            queued.queue_id = queued_id
            self._pending[queued_id] = queued
            heapq.heappush(
                self._deadline_heap,
                (
                    queued.submitted_at + _QUEUE_WARN_SECONDS,
                    self._deadline_sequence,
                    queued_id,
                ),
            )
            self._queue.put(queued)
            self._condition.notify_all()
        return handle

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is self._STOP:
                return
            assert isinstance(item, _QueuedMaintenanceJob)
            with self._condition:
                if self._pending.pop(item.queue_id, None) is None:
                    # Defensive only: a future cancellation path may have removed
                    # the pending marker after this worker dequeued its item.
                    pass
                waited = time.monotonic() - item.submitted_at
                if item.warned:
                    _log.info(
                        "background job started after queueing: pool=%s operation=%s waited=%.0fs",
                        self.pool, item.operation, waited,
                    )
                # Dequeued and about to run: no longer "pending", now "active"
                # — ``wait_idle`` needs this window covered too, or a test's
                # teardown can observe an empty ``self._pending`` while the
                # job it just submitted is still executing on this thread.
                self._active += 1
                self._condition.notify_all()
            try:
                item.context.run(item.observed)
            except BaseException:
                # ``_observed`` handles normal exceptions. A BaseException must not
                # terminate a shared worker (and reduce the configured capacity).
                # Do not use exception()/exc_info: exception payloads can contain
                # user data, while this process-level receipt must stay content-free.
                _log.error(
                    "background worker survived base exception: pool=%s operation=%s",
                    self.pool,
                    item.operation,
                )
            finally:
                item.handle._finish()
                with self._condition:
                    self._active -= 1
                    self._condition.notify_all()

    def _monitor_queue(self) -> None:
        while True:
            with self._condition:
                while not self._closed and not self._pending:
                    self._condition.wait()
                if self._closed:
                    return
                while self._deadline_heap:
                    deadline, _sequence, queued_id = self._deadline_heap[0]
                    item = self._pending.get(queued_id)
                    if item is None or item.warned:
                        heapq.heappop(self._deadline_heap)
                        continue
                    wait_seconds = deadline - time.monotonic()
                    if wait_seconds > 0:
                        self._condition.wait(wait_seconds)
                        break
                    heapq.heappop(self._deadline_heap)
                    item.warned = True
                    _log.warning(
                        "background job still queued: pool=%s operation=%s waited=%.0fs",
                        self.pool, item.operation,
                        time.monotonic() - item.submitted_at,
                    )
                else:
                    # All pending jobs have already been disclosed; a worker dequeue
                    # or the next submit wakes us. Do not poll or rescan the queue.
                    self._condition.wait()

    def shutdown_for_tests(self) -> None:
        """Stop idle worker/monitor threads after a test; never used in production."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        for _worker in self._workers:
            self._queue.put(self._STOP)

    def wait_idle(self, deadline: float) -> bool:
        """Block, bounded by ``deadline`` (a ``time.monotonic()`` value), until
        this pool has nothing queued AND nothing currently running. Returns
        whether it actually went idle (``False`` on timeout). Test-only.

        Needed because ``shutdown_for_tests`` only stops IDLE workers — a job
        already dequeued keeps running ``item.context.run(...)`` on its own
        thread after the executor object is discarded and replaced. Without
        this, a maintenance job submitted late in one test (the "AI 对这个库
        的理解" background consolidation is exactly this shape: cheap and
        fire-and-forget) can still be mid-flight — writing to that test's
        now-torn-down temporary database, or emitting an event like
        ``agent_profile_consolidated`` — while the NEXT test has already
        started and is asserting on a fresh one.
        """
        with self._condition:
            while self._pending or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return not self._pending and not self._active
                self._condition.wait(remaining)
            return True


def _maintenance_executor(name: str | None) -> tuple[_MaintenanceExecutor, str] | None:
    """返回显式维护类别的固定 executor；其他 job 保持原有直接线程语义。"""
    resolved = _maintenance_pool(name)
    if resolved is None:
        return None
    pool, operation = resolved
    with _executor_lock:
        executor = _executors.get(pool)
        if executor is None:
            executor = _MaintenanceExecutor(pool, _pool_capacity(pool))
            _executors[pool] = executor
    return executor, operation


def _drain_maintenance_executors_for_tests(timeout: float = 10.0) -> None:
    """Wait, bounded, for every live maintenance pool to go idle before it is
    discarded. Test-only teardown helper — see ``_MaintenanceExecutor.wait_idle``
    for why this is necessary and not merely defensive.
    """
    with _executor_lock:
        executors = tuple(_executors.values())
    deadline = time.monotonic() + timeout
    for executor in executors:
        assert executor.wait_idle(deadline), (
            f"background maintenance pool {executor.pool!r} did not drain its "
            "queue and in-flight job during test teardown"
        )


def _reset_maintenance_gate_for_tests() -> None:
    """丢弃测试创建的固定 worker 池，让下次 submit 重读当前 Settings。"""
    with _executor_lock:
        executors = tuple(_executors.values())
        _executors.clear()
    for executor in executors:
        executor.shutdown_for_tests()


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
    """在传播了调用线程上下文的后台执行上下文里跑 ``fn(*args, **kwargs)``。

    快照必须在调用线程（通常是请求处理线程）里捕获，故 copy_context() 在此同步调用。

    `notify_pending=True` 时，job 完成（无论成功/失败）后刷新发起用户的「待确认中心」
    snapshot；通知失败绝不影响/冒泡出 job 本身。

    维护类 job（见 `_HEAVY_MAINTENANCE_OPERATIONS` / `_LIGHT_MAINTENANCE_OPERATIONS`）
    进入所属的固定容量 worker 池。submit 本身永远立即返回，请求线程不因排队而阻塞；
    返回句柄保留原 ``Thread`` 的 ``join`` / ``is_alive`` / ``name`` / ``daemon``
    观察契约。非维护 job 沿用原有每任务 daemon-thread 语义。
    """
    ctx = contextvars.copy_context()
    try:
        diagnostic_name = _diagnostic_job_name(fn, name)
    except Exception:  # noqa: BLE001 — diagnostics metadata is best-effort only
        diagnostic_name = "background_job"
    maintenance = _maintenance_executor(name)

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

    if maintenance is not None:
        executor, operation = maintenance
        try:
            return executor.submit(ctx, _observed, operation, name)
        except RuntimeError:
            # A test-only reset can race a submit. Production executors never close;
            # resolving once more against the replacement preserves fire-and-forget
            # delivery instead of turning that race into a dropped maintenance job.
            replacement = _maintenance_executor(name)
            if replacement is None:
                raise
            executor, operation = replacement
            return executor.submit(ctx, _observed, operation, name)

    thread = threading.Thread(target=lambda: ctx.run(_observed), name=name, daemon=True)
    thread.start()
    return thread
