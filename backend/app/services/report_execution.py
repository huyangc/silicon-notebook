"""深度报告的脱离连接执行域(Task 25)。

进程全局 ``REPORT_CANCELLATIONS`` 是取消注册表的唯一所有者:
``report_engine.register_cancel/cancel_report/unregister_cancel`` 是它的显式
委托,``RepositoryRuntime.wire_report_execution`` 按同一身份把它注入
``ReportExecutionCoordinator`` —— 端点(cancel)/模块函数/协调器三方看到的
永远是同一个实例。

协调器只做编排,冻结自 routes 的 ``_launch_plan_job``/``_launch_generate_job``:
取消事件在 submit **前**注册 → 经 ``background_jobs.submit`` 形状的
``job_submitter`` 起 daemon 线程(copy_context 传播 per-user 模型/日志归属,
顶层异常兜底)→ worker 无论成败在 finally 注销。规划 job 名
``report-plan-{rid}``、生成 job 名 ``report-gen-{rid}``,均 notify_pending。

刻意不提供任何重启恢复(no-recovery):进程死 → job 死,报告行停在最后
写入的状态,由用户显式重跑。
"""
from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.application.report_pipeline import CommittedReport
from app.services.cancellation import AskCancelled, raise_if_cancelled
from app.services.model_work import ModelPriority, model_work_scope

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.services.source_scope import CeilingReaders


class BackgroundJobSubmitter(Protocol):
    """``app.services.background_jobs.submit`` 的结构契约。"""

    def __call__(self, fn: Callable, *args, name: str | None = None,
                 notify_pending: bool = False, **kwargs) -> threading.Thread: ...


class ReportEngineFactory(Protocol):
    """按发起用户/取消事件构造一台端口化 ReportEngine(runtime 注入)。"""

    def __call__(self, *, user_id: str,
                 cancel_event: threading.Event | None,
                 settings: "Settings | None" = None) -> Any: ...


class ReportCancellationRegistry:
    """report_id → threading.Event(活动后台 job 才在册)。"""

    def __init__(self) -> None:
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def register(
        self, key: str, event: threading.Event, *, replace: bool = False
    ) -> bool:
        with self._lock:
            if key in self._events and not replace:
                return False
            self._events[key] = event
            return True

    def cancel(self, key: str) -> bool:
        with self._lock:
            event = self._events.get(key)
        if event is not None:
            event.set()
            return True
        return False

    def unregister(self, key: str, event: threading.Event | None = None) -> None:
        with self._lock:
            if event is None or self._events.get(key) is event:
                self._events.pop(key, None)


# 进程全局唯一所有者(见模块 docstring;runtime 按身份引用,不建副本)。
REPORT_CANCELLATIONS = ReportCancellationRegistry()


# Deadline of each ceiling read's budget: PostgreSQL's default
# ``statement_timeout`` (30 s), i.e. no shorter than what a read had before.
CEILING_READ_SECONDS = 30.0


def cancellable_ceiling_readers(
    readers: "CeilingReaders | None",
    cancel_event: Any,
    *,
    seconds: float = CEILING_READ_SECONDS,
) -> "CeilingReaders | None":
    """``readers`` whose every read runs under a budget carrying ``cancel_event``.

    ``default_ceiling_context`` reads the ACTIVE notebook (participants, its
    visible set, its hidden half, its Memory sources) with no ``read_budget``
    of its own, so without this a Stop pressed during those reads waited for
    them to finish.  Each call here enters ``read_budget(now + seconds,
    cancel_event)``: on SQLite the progress handler interrupts the statement
    as soon as the event is set; on PostgreSQL the budget is checked before
    each statement and a budgeted connection caps every statement at
    ``postgres_chunk_fts_timeout_seconds`` (3 s by default), so a Stop waits
    at most that long instead of up to the 30 s ``statement_timeout``.  (The
    same cap already applies to each mounted library's read, which runs under
    a budget inside the constructor; nested budgets only get shorter.)

    This only makes the read stoppable; it does not decide what an
    interrupted read means.  The constructor still gets ``cancel_event`` and
    turns an interrupted MOUNTED read into ``AskCancelled`` (rather than a
    skipped library); the entry point turns an interrupted ACTIVE-notebook
    read into ``AskCancelled`` (rather than a failed report).  ``None``
    readers or no cancel event -> ``readers`` unchanged.
    """
    if readers is None or cancel_event is None:
        return readers
    import dataclasses
    import time

    from app.repositories.read_budget import read_budget

    def bounded(read: Callable) -> Callable:
        def call(*args):
            with read_budget(time.monotonic() + float(seconds), cancel_event):
                return read(*args)
        return call

    return dataclasses.replace(
        readers,
        participants=bounded(readers.participants),
        visible=bounded(readers.visible),
        hidden=bounded(readers.hidden),
        memory_sources=bounded(readers.memory_sources),
    )


class ReportGenerationGate:
    """Bound whole-report database fan-out within one backend process.

    The physical model scheduler already caps model calls, but retrieval happens
    before those calls and can otherwise multiply across concurrent reports.
    Waiting workers own no database connection and remain promptly cancellable.
    """

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, int(capacity))
        self._slots = threading.BoundedSemaphore(self.capacity)

    @contextmanager
    def slot(self, *, cancel_event=None, on_wait: Callable[[], None] | None = None):
        notified = False
        acquired = False
        try:
            while not acquired:
                if cancel_event is not None and cancel_event.is_set():
                    raise AskCancelled()
                acquired = self._slots.acquire(timeout=0.25)
                if not acquired and not notified and on_wait is not None:
                    notified = True
                    # Queue progress is observability only.  In particular, a
                    # saturated PostgreSQL pool must not turn this best-effort
                    # status write into a failed report while the worker owns
                    # no generation slot and is deliberately waiting.
                    try:
                        on_wait()
                    except Exception:
                        pass
            yield
        finally:
            if acquired:
                self._slots.release()


class ReportExecutionCoordinator:
    def __init__(self, *, reports, engine_factory: ReportEngineFactory,
                 cancellations: ReportCancellationRegistry,
                 job_submitter: BackgroundJobSubmitter,
                 ceiling_readers: "CeilingReaders",
                 after_completed: Callable[[CommittedReport], None] | None = None,
                 ) -> None:
        self.reports = reports
        self.engine_factory = engine_factory
        self.cancellations = cancellations
        self.job_submitter = job_submitter
        # Required: a worker with no readers could only run unscoped, and an
        # unscoped report reads every member's Memory projections.
        self.ceiling_readers = ceiling_readers
        self.after_completed = after_completed

    def _after_completed(self, committed: CommittedReport | None) -> None:
        if type(committed) is not CommittedReport or self.after_completed is None:
            return
        try:
            self.after_completed(committed)
        except Exception:
            # The durable report already won its terminal CAS. Optional
            # post-terminal work can never rewrite that outcome.
            return

    def _default_ceiling(self, notebook_id: str, report_id: str, user_id: str,
                         cancel: threading.Event, source_scope, base_scope,
                         progress: str) -> ExitStack:
        """Enter the phase's retrieval ceiling; return the stack that holds it.

        ``default_ceiling_context`` with the phase's scope dimensions: a
        persisted (route re-frozen) dimension is used as-is, an omitted one is
        frozen here -- the notebook's visible sources plus the creator's own
        hidden half, every mounted library to its visible sources only -- so a
        report created without any scope no longer reads other members'
        Memory projections.  The built local dimension is not a user choice
        (``source_provided`` stays False), so ``understanding.source_scope``
        is still persisted as None.

        A reader failure FAILS the phase (report marked failed, exception
        re-raised): the worker never falls back to running unscoped.  A stop
        during the reads propagates as ``AskCancelled`` for the caller to end
        the worker quietly, as it already does for a cancelled report; every
        read runs under a budget carrying ``cancel``
        (``cancellable_ceiling_readers``), so a Stop does not wait for it.

        COST, measured by the quality review on this branch (real stores,
        49k visible sources, no mounted library, median of 3; SQLite at
        machine load 4 to tens): the constructor itself is 3 reads (4 with
        the Memory channel closed), 24-27 ms.  What dominates is that an
        installed ceiling turns on the per-call drift probe (two full reads
        of the notebook's visible set and hidden half per probe) for every
        retrieval inside the phase, where the unscoped worker used to probe
        nothing: a 6-section report probes 0 times in intent understanding,
        32 in planning and 102 in generation (17 per section).  Wall clock
        before / ceiling with the probe stubbed out / this branch: planning
        404-469 / 825-983 / 11,646-11,692 ms on SQLite and 1,322-3,856 /
        2,374-2,767 / 4,969-7,998 ms on PostgreSQL; generation 705-873 /
        1,615-2,136 / 7,052-7,549 ms and 6,076-10,375 / 3,339-3,970 /
        16,701-18,041 ms.  (PostgreSQL's "before" is noisy; the probe's share
        there is after minus noprobe: +2.6-5.2 s planning, +13-14 s
        generation.)  The probe's per-call cost is owned by E1-2 (a one-row
        digest in place of the two full reads); the rest of the noprobe gap
        is the frozen id list each chunk statement now binds.
        """
        from app.services.source_scope import default_ceiling_context

        stack = ExitStack()
        try:
            stack.enter_context(default_ceiling_context(
                notebook_id, user_id,
                cancellable_ceiling_readers(self.ceiling_readers, cancel),
                local_scope=source_scope, base_scope=base_scope,
                cancel_event=cancel,
            ))
        except AskCancelled:
            raise
        except Exception as exc:
            # A read the Stop interrupted is the Stop, not a reader failure.
            raise_if_cancelled(cancel)
            try:
                self.reports.update_report(
                    notebook_id, report_id, status="failed",
                    error=f"{type(exc).__name__}: {exc}", progress=progress,
                )
            except Exception:
                pass
            raise
        return stack

    def start_plan(self, notebook_id: str, report_id: str, question: str,
                   history: str = "", auto_generate: bool = False, *,
                   user_id: str = "", intent_contract=None,
                   source_scope=None, base_scope=None,
                   scope_reconfirm=None) -> bool:
        """Run pre-retrieval understanding or resume with a confirmed contract."""
        cancel = threading.Event()
        # A previous phase publishes intent_ready/outline_ready immediately
        # before its finally unregisters.  A CAS-claimed next phase may safely
        # replace that old event: identity-aware unregister prevents the old
        # worker from removing this new registration.
        if not self.cancellations.register(
            report_id, cancel, replace=intent_contract is not None
        ):
            return False

        def worker():
            committed = None
            try:
                depth = 2
                try:
                    report = self.reports.get_report(notebook_id, report_id)
                    if report.get("status") == "cancelled" or cancel.is_set():
                        return
                    depth = int(report.get("depth", 2))
                except Exception:
                    pass
                with model_work_scope(
                    priority=ModelPriority.REPORT,
                    parent_id=report_id,
                    actor_id=user_id,
                    notebook_id=notebook_id,
                    question=question,
                ):
                    engine = self.engine_factory(user_id=user_id, cancel_event=cancel)
                    effective_scope = source_scope
                    if effective_scope is None and isinstance(intent_contract, dict):
                        effective_scope = intent_contract.get("source_scope")
                    effective_base_scope = base_scope
                    if effective_base_scope is None and isinstance(intent_contract, dict):
                        effective_base_scope = intent_contract.get("base_scope")
                    try:
                        ceiling = self._default_ceiling(
                            notebook_id, report_id, user_id, cancel,
                            effective_scope, effective_base_scope, "规划失败",
                        )
                    except AskCancelled:
                        return
                    with ceiling:
                        if intent_contract is None:
                            committed = engine.run(
                                notebook_id, report_id, question, history, depth=depth,
                                auto_generate=auto_generate,
                                require_intent_review=True,
                                scope_reconfirm=scope_reconfirm)
                        else:
                            committed = engine.run(
                                notebook_id, report_id, question, history, depth=depth,
                                auto_generate=auto_generate,
                                intent_contract=intent_contract)
                # Preserve the historical active-job window: completion work
                # runs after model/source/retrieval scopes exit, but before the
                # coordinator removes this exact cancellation registration.
                self._after_completed(committed)
            finally:
                self.cancellations.unregister(report_id, cancel)

        # submit() 统一 copy_context() 传播 per-user 上下文并兜底顶层异常
        try:
            self.job_submitter(worker, name=f"report-plan-{report_id}",
                               notify_pending=True)
        except BaseException as exc:
            try:
                self.reports.update_report(
                    notebook_id, report_id, status="failed",
                    error=f"{type(exc).__name__}: {exc}", progress="规划失败",
                )
            finally:
                self.cancellations.unregister(report_id, cancel)
            raise
        return True

    def start_generate(self, notebook_id: str, report_id: str, question: str,
                       depth: int = 2, *, user_id: str = "",
                       source_scope=None, base_scope=None) -> bool:
        """阶段2(生成)后台 job:用已确认的 outline 跑 generate → done。"""
        cancel = threading.Event()
        if not self.cancellations.register(report_id, cancel, replace=True):
            return False

        def worker():
            committed = None
            try:
                effective_scope = source_scope
                effective_base_scope = base_scope
                try:
                    report = self.reports.get_report(notebook_id, report_id)
                    if report.get("status") == "cancelled" or cancel.is_set():
                        return
                    understanding = report.get("understanding") or {}
                    if effective_scope is None:
                        effective_scope = understanding.get("source_scope")
                    if effective_base_scope is None:
                        effective_base_scope = understanding.get("base_scope")
                except Exception as exc:
                    # The persisted understanding contract is the authority for
                    # generation.  A transient read failure must never erase a
                    # user-selected source ceiling and continue against the
                    # whole notebook.
                    try:
                        self.reports.update_report(
                            notebook_id, report_id, status="failed",
                            error=f"{type(exc).__name__}: {exc}", progress="失败",
                        )
                    except Exception:
                        pass
                    raise
                with model_work_scope(
                    priority=ModelPriority.REPORT,
                    parent_id=report_id,
                    actor_id=user_id,
                    notebook_id=notebook_id,
                    question=question,
                ):
                    try:
                        ceiling = self._default_ceiling(
                            notebook_id, report_id, user_id, cancel,
                            effective_scope, effective_base_scope, "失败",
                        )
                    except AskCancelled:
                        return
                    with ceiling:
                        committed = self.engine_factory(user_id=user_id, cancel_event=cancel).generate(
                            notebook_id, report_id, question, depth=depth)
                self._after_completed(committed)
            finally:
                self.cancellations.unregister(report_id, cancel)

        try:
            self.job_submitter(worker, name=f"report-gen-{report_id}",
                               notify_pending=True)
        except BaseException as exc:
            try:
                self.reports.update_report(
                    notebook_id, report_id, status="failed",
                    error=f"{type(exc).__name__}: {exc}", progress="失败",
                )
            finally:
                self.cancellations.unregister(report_id, cancel)
            raise
        return True
