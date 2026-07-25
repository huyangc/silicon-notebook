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
from typing import TYPE_CHECKING, Any, Callable, Protocol

from app.services.model_work import ModelPriority, model_work_scope

if TYPE_CHECKING:
    from app.core.config import Settings


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


class ReportExecutionCoordinator:
    def __init__(self, *, reports, engine_factory: ReportEngineFactory,
                 cancellations: ReportCancellationRegistry,
                 job_submitter: BackgroundJobSubmitter) -> None:
        self.reports = reports
        self.engine_factory = engine_factory
        self.cancellations = cancellations
        self.job_submitter = job_submitter

    def start_plan(self, notebook_id: str, report_id: str, question: str,
                   history: str = "", auto_generate: bool = False, *,
                   user_id: str = "", intent_contract=None) -> bool:
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
                    priority=ModelPriority.REPORT, parent_id=report_id
                ):
                    engine = self.engine_factory(user_id=user_id, cancel_event=cancel)
                    if intent_contract is None:
                        engine.run(
                            notebook_id, report_id, question, history, depth=depth,
                            auto_generate=auto_generate,
                            require_intent_review=True)
                    else:
                        engine.run(
                            notebook_id, report_id, question, history, depth=depth,
                            auto_generate=auto_generate,
                            intent_contract=intent_contract)
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
                       depth: int = 2, *, user_id: str = "") -> bool:
        """阶段2(生成)后台 job:用已确认的 outline 跑 generate → done。"""
        cancel = threading.Event()
        if not self.cancellations.register(report_id, cancel, replace=True):
            return False

        def worker():
            try:
                try:
                    report = self.reports.get_report(notebook_id, report_id)
                    if report.get("status") == "cancelled" or cancel.is_set():
                        return
                except Exception:
                    pass
                with model_work_scope(
                    priority=ModelPriority.REPORT, parent_id=report_id
                ):
                    self.engine_factory(user_id=user_id, cancel_event=cancel).generate(
                        notebook_id, report_id, question, depth=depth)
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
