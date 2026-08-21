"""Task 25: 深度报告脱离连接执行域 —— 取消注册表 + 协调器 + runtime 接线。

冻结的执行契约:
- 取消事件在 submit **前**注册,worker 无论成败在 finally 注销;
- report_engine 模块的 register_cancel/cancel_report/unregister_cancel 是
  进程全局 REPORT_CANCELLATIONS 的显式委托(端点/模块/协调器三方同一注册表);
- runtime.report_cancellations 按身份就是 REPORT_CANCELLATIONS,且与(Task 23
  的)ask 取消注册表不是同一对象;
- 计划 job 名 report-plan-{rid} / 生成 job 名 report-gen-{rid},notify_pending;
- depth 从 report 行读、读失败回退 2;
- 经 background_jobs.submit 跑 → copy_context 传播调用线程上下文;
- 刻意无任何重启恢复面(no-recovery)。
"""
import contextvars
import threading

import pytest

from app.application.report_pipeline import CommittedReport, ReportAuditFacts
from app.services.report_execution import (
    REPORT_CANCELLATIONS,
    ReportCancellationRegistry,
    ReportGenerationGate,
    ReportExecutionCoordinator,
)


class _Reports:
    def __init__(self, depth=7, boom=False):
        self.depth, self.updates = depth, []
        self.boom = boom

    def get_report(self, notebook_id, report_id):
        if self.boom:
            raise KeyError(report_id)
        return {"id": report_id, "depth": self.depth}

    def update_report(self, *args, **kwargs):
        self.updates.append((args, kwargs))


class _Engine:
    def __init__(self, boom=False):
        self.boom = boom
        self.run_calls = []
        self.generate_calls = []

    def run(self, notebook_id, rid, question, history="", depth=2,
            auto_generate=False, require_intent_review=False,
            intent_contract=None, scope_reconfirm=None):
        if self.boom:
            raise RuntimeError("engine down")
        self.run_calls.append((notebook_id, rid, question, history, depth,
                               auto_generate, require_intent_review,
                               intent_contract))

    def generate(self, notebook_id, rid, question, depth=2):
        if self.boom:
            raise RuntimeError("engine down")
        self.generate_calls.append((notebook_id, rid, question, depth))


class _CapturingSubmitter:
    """捕获 worker 不立即跑:检验「注册先于 submit」与 job 命名。"""
    def __init__(self, registry, probe_key):
        self.registry = registry
        self.probe_key = probe_key
        self.calls = []

    def __call__(self, fn, *args, name=None, notify_pending=False, **kwargs):
        with self.registry._lock:
            registered = self.probe_key in self.registry._events
        self.calls.append({
            "fn": fn, "name": name, "notify_pending": notify_pending,
            "registered_at_submit": registered,
        })
        return None


def _coordinator(reports=None, engine=None, registry=None, submitter=None):
    engine = engine if engine is not None else _Engine()
    registry = registry if registry is not None else ReportCancellationRegistry()
    captured = {}

    def factory(*, user_id, cancel_event):
        captured["user_id"] = user_id
        captured["cancel_event"] = cancel_event
        return engine

    coord = ReportExecutionCoordinator(
        reports=reports if reports is not None else _Reports(),
        engine_factory=factory,
        cancellations=registry,
        job_submitter=submitter if submitter is not None else (
            lambda fn, *a, name=None, notify_pending=False, **k: fn()),
    )
    return coord, engine, registry, captured


# ---------------------------------------------------------------------------
# 注册表本体 + report_engine 模块委托共享同一进程全局实例
# ---------------------------------------------------------------------------

def test_registry_register_cancel_unregister_roundtrip():
    registry = ReportCancellationRegistry()
    event = threading.Event()
    assert registry.register("r1", event) is True
    assert registry.cancel("r1") is True and event.is_set()
    registry.unregister("r1")
    assert registry.cancel("r1") is False              # 注销后不再命中
    registry.unregister("r1")                          # 幂等


def test_generation_gate_queues_without_holding_more_than_capacity():
    gate = ReportGenerationGate(1)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_waiting = threading.Event()

    def first():
        with gate.slot():
            first_entered.set()
            assert release_first.wait(3)

    def second():
        with gate.slot(on_wait=second_waiting.set):
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    assert first_entered.wait(3)
    two.start()
    assert second_waiting.wait(3)
    assert not second_entered.is_set()
    release_first.set()
    one.join(3)
    two.join(3)
    assert second_entered.is_set()


def test_generation_gate_ignores_wait_notification_failure_without_leaking_slot():
    gate = ReportGenerationGate(1)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    wait_notified = threading.Event()

    def first():
        with gate.slot():
            first_entered.set()
            assert release_first.wait(3)

    def broken_notification():
        wait_notified.set()
        raise RuntimeError("pool saturated")

    def second():
        with gate.slot(on_wait=broken_notification):
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    assert first_entered.wait(3)
    two.start()
    assert wait_notified.wait(3)
    assert not second_entered.is_set()
    release_first.set()
    one.join(3)
    two.join(3)
    assert second_entered.is_set()

    # The second waiter released its permit normally after the callback fault.
    with gate.slot():
        pass


def test_registry_refuses_overwrite_and_unregisters_by_event_identity():
    registry = ReportCancellationRegistry()
    first = threading.Event()
    second = threading.Event()

    assert registry.register("same", first) is True
    assert registry.register("same", second) is False
    registry.unregister("same", second)

    assert registry.cancel("same") is True
    assert first.is_set() and not second.is_set()
    registry.unregister("same", first)
    assert registry.cancel("same") is False


def test_module_cancel_functions_share_the_process_global_registry():
    from app.services import report_engine
    event = report_engine.register_cancel("rep-shared")
    try:
        assert REPORT_CANCELLATIONS.cancel("rep-shared") is True and event.is_set()
    finally:
        report_engine.unregister_cancel("rep-shared", event)
    assert report_engine.cancel_report("rep-shared") is False

    other = threading.Event()
    REPORT_CANCELLATIONS.register("rep-shared2", other)
    assert report_engine.cancel_report("rep-shared2") is True and other.is_set()
    report_engine.unregister_cancel("rep-shared2", other)
    assert REPORT_CANCELLATIONS.cancel("rep-shared2") is False


def test_module_cancel_registration_fails_closed_on_duplicate():
    from app.services import report_engine

    first = report_engine.register_cancel("rep-duplicate")
    try:
        with pytest.raises(RuntimeError, match="already has an active job"):
            report_engine.register_cancel("rep-duplicate")
        report_engine.unregister_cancel("rep-duplicate", threading.Event())
        assert report_engine.cancel_report("rep-duplicate") is True
    finally:
        report_engine.unregister_cancel("rep-duplicate", first)


# ---------------------------------------------------------------------------
# 协调器:注册先于 submit;成功/失败都注销;命名/notify/depth 语义
# ---------------------------------------------------------------------------

def test_start_plan_registers_before_submit_and_unregisters_on_success():
    registry = ReportCancellationRegistry()
    submitter = _CapturingSubmitter(registry, "rid-1")
    coord, engine, _reg, captured = _coordinator(
        registry=registry, submitter=submitter)
    coord.start_plan("nb", "rid-1", "q", "h", True, user_id="u1")
    call = submitter.calls[0]
    assert call["registered_at_submit"] is True        # 注册先于 submit
    assert call["name"] == "report-plan-rid-1" and call["notify_pending"] is True
    call["fn"]()                                       # 跑 worker
    assert engine.run_calls == [
        ("nb", "rid-1", "q", "h", 7, True, True, None)
    ]  # depth 从行读;首次只做问题理解
    assert captured["user_id"] == "u1"
    assert captured["cancel_event"] is not None
    assert registry.cancel("rid-1") is False           # 成功后注销


def test_start_plan_unregisters_when_engine_fails():
    registry = ReportCancellationRegistry()
    submitter = _CapturingSubmitter(registry, "rid-2")
    coord, _engine, _reg, _cap = _coordinator(
        engine=_Engine(boom=True), registry=registry, submitter=submitter)
    coord.start_plan("nb", "rid-2", "q", user_id="u1")
    with pytest.raises(RuntimeError, match="engine down"):
        submitter.calls[0]["fn"]()                     # 顶层兜底属 background_jobs
    assert registry.cancel("rid-2") is False           # 失败同样注销


def test_start_plan_depth_read_failure_falls_back_to_2():
    coord, engine, _reg, _cap = _coordinator(reports=_Reports(boom=True))
    coord.start_plan("nb", "rid-3", "q", user_id="u1")
    assert engine.run_calls == [
        ("nb", "rid-3", "q", "", 2, False, True, None)
    ]


def test_start_plan_with_confirmed_intent_resumes_without_review_gate():
    coord, engine, _reg, _cap = _coordinator()
    contract = {"objective": "q", "confirmed": True}
    coord.start_plan(
        "nb", "rid-confirmed", "q", user_id="u1", intent_contract=contract
    )
    assert engine.run_calls == [
        ("nb", "rid-confirmed", "q", "", 7, False, False, contract)
    ]


def test_worker_observes_durable_cancel_that_preceded_event_registration():
    reports = _Reports()
    reports.get_report = lambda *_args: {
        "id": "rid-cancel-gap", "depth": 7, "status": "cancelled"
    }
    registry = ReportCancellationRegistry()
    submitted = []

    def submitter(fn, *args, name=None, notify_pending=False, **kwargs):
        submitted.append(fn)

    coord, engine, _reg, _captured = _coordinator(
        reports=reports, registry=registry, submitter=submitter
    )

    assert coord.start_plan(
        "nb", "rid-cancel-gap", "q", user_id="u1",
        intent_contract={"confirmed": True},
    ) is True
    submitted[0]()

    assert engine.run_calls == []
    assert registry.cancel("rid-cancel-gap") is False


@pytest.mark.parametrize("kind", ("plan", "generate"))
def test_ready_phase_handoff_replaces_old_event_without_losing_new_job(kind):
    registry = ReportCancellationRegistry()
    old = threading.Event()
    assert registry.register("rid-handoff", old) is True
    submitted = []

    def submitter(fn, *args, name=None, notify_pending=False, **kwargs):
        submitted.append(fn)

    coord, engine, _reg, _captured = _coordinator(
        registry=registry, submitter=submitter
    )
    if kind == "plan":
        assert coord.start_plan(
            "nb", "rid-handoff", "q", user_id="u",
            intent_contract={"confirmed": True},
        ) is True
    else:
        assert coord.start_generate(
            "nb", "rid-handoff", "q", user_id="u"
        ) is True

    registry.unregister("rid-handoff", old)
    assert registry.cancel("rid-handoff") is True
    assert old.is_set() is False
    submitted[0]()
    assert engine.run_calls == [] and engine.generate_calls == []
    assert registry.cancel("rid-handoff") is False


def test_start_generate_names_job_and_unregisters():
    registry = ReportCancellationRegistry()
    submitter = _CapturingSubmitter(registry, "rid-4")
    coord, engine, _reg, captured = _coordinator(
        registry=registry, submitter=submitter)
    coord.start_generate("nb", "rid-4", "q", 5, user_id="u2")
    call = submitter.calls[0]
    assert call["registered_at_submit"] is True
    assert call["name"] == "report-gen-rid-4" and call["notify_pending"] is True
    call["fn"]()
    assert engine.generate_calls == [("nb", "rid-4", "q", 5)]
    assert captured["user_id"] == "u2"
    assert registry.cancel("rid-4") is False


def test_start_generate_fails_closed_when_persisted_scope_cannot_be_read():
    reports = _Reports(boom=True)
    registry = ReportCancellationRegistry()
    submitter = _CapturingSubmitter(registry, "rid-scope-read")
    coord, engine, _reg, _captured = _coordinator(
        reports=reports, registry=registry, submitter=submitter
    )

    coord.start_generate("nb", "rid-scope-read", "q", user_id="u")
    with pytest.raises(KeyError, match="rid-scope-read"):
        submitter.calls[0]["fn"]()

    assert engine.generate_calls == []
    assert reports.updates[-1][1]["status"] == "failed"
    assert registry.cancel("rid-scope-read") is False


def test_coordinator_submits_through_background_jobs_with_copied_context():
    from app.services import background_jobs
    probe = contextvars.ContextVar("task25_exec_probe", default="")
    probe.set("ctx-report")
    seen = {}
    done = threading.Event()

    class _CtxEngine:
        def run(self, *a, **k):
            from app.services.model_work import (
                ModelPriority, make_model_work_context,
            )
            seen["ctx"] = probe.get()
            seen["model"] = make_model_work_context(
                workload_id="report_section", priority=ModelPriority.BACKGROUND,
                parent_id="wrong-parent",
            )
            done.set()

    coord = ReportExecutionCoordinator(
        reports=_Reports(),
        engine_factory=lambda *, user_id, cancel_event: _CtxEngine(),
        cancellations=ReportCancellationRegistry(),
        job_submitter=background_jobs.submit,
    )
    coord.start_plan("nb", "rid-ctx", "q", user_id="u")
    assert done.wait(5)
    assert seen["ctx"] == "ctx-report"                 # copy_context 传播不回归
    assert seen["model"].priority.value == "report"
    assert seen["model"].parent_id == "rid-ctx"


def test_committed_report_hook_runs_before_cancel_unregister_after_scope_exit():
    registry = ReportCancellationRegistry()
    seen = []
    facts = ReportAuditFacts(1, 1, 0, 0, 0, 0, 0, 0, 4, "not_requested")

    class _CommittedEngine:
        def generate(self, notebook_id, rid, question, depth=2):
            return CommittedReport(notebook_id, rid, "actor", facts)

    def after(committed):
        from app.services import model_work
        from app.services.retrieval_run import current_retrieval_run
        from app.services.source_scope import current_source_scope
        seen.append((
            committed,
            registry.cancel("rid-done"),
            model_work._CURRENT_SCOPE.get(),
            current_retrieval_run(),
            current_source_scope(),
        ))

    coord = ReportExecutionCoordinator(
        reports=_Reports(),
        engine_factory=lambda **_kwargs: _CommittedEngine(),
        cancellations=registry,
        job_submitter=lambda fn, **_kwargs: fn(),
        after_completed=after,
    )
    assert coord.start_generate("nb", "rid-done", "q", user_id="actor")
    assert seen[0][0].actor_id == "actor"
    assert seen[0][1] is True
    assert seen[0][2] is None
    assert seen[0][3:] == (None, None)
    assert registry.cancel("rid-done") is False


def test_auto_generate_plan_and_manual_generate_share_one_completion_hook():
    facts = ReportAuditFacts(1, 1, 0, 0, 0, 0, 0, 0, 4, "not_requested")
    calls = []

    class _CommittedEngine:
        def run(self, notebook_id, rid, *_args, **_kwargs):
            return CommittedReport(notebook_id, rid, "actor", facts)

        def generate(self, notebook_id, rid, *_args, **_kwargs):
            return CommittedReport(notebook_id, rid, "actor", facts)

    coord = ReportExecutionCoordinator(
        reports=_Reports(),
        engine_factory=lambda **_kwargs: _CommittedEngine(),
        cancellations=ReportCancellationRegistry(),
        job_submitter=lambda fn, **_kwargs: fn(),
        after_completed=lambda committed: calls.append(committed.report_id),
    )
    assert coord.start_plan(
        "nb", "rep-auto", "q", auto_generate=True, user_id="actor"
    )
    assert coord.start_generate("nb", "rep-manual", "q", user_id="actor")
    assert calls == ["rep-auto", "rep-manual"]


def test_completion_hook_runs_after_generation_gate_is_released():
    gate = ReportGenerationGate(1)
    hook_entered = threading.Event()
    release_hook = threading.Event()
    second_entered = threading.Event()
    threads = []
    facts = ReportAuditFacts(1, 1, 0, 0, 0, 0, 0, 0, 4, "not_requested")

    class _GatedEngine:
        def generate(self, notebook_id, rid, _question, depth=2):
            with gate.slot():
                pass
            return CommittedReport(notebook_id, rid, "actor", facts)

    def submitter(fn, **_kwargs):
        thread = threading.Thread(target=fn)
        threads.append(thread)
        thread.start()
        return thread

    def after(_committed):
        hook_entered.set()
        assert release_hook.wait(3)

    coord = ReportExecutionCoordinator(
        reports=_Reports(),
        engine_factory=lambda **_kwargs: _GatedEngine(),
        cancellations=ReportCancellationRegistry(),
        job_submitter=submitter,
        after_completed=after,
    )
    assert coord.start_generate("nb", "rep-gate", "q", user_id="actor")
    assert hook_entered.wait(3)

    def enter_second():
        with gate.slot():
            second_entered.set()

    second = threading.Thread(target=enter_second)
    second.start()
    assert second_entered.wait(1)
    release_hook.set()
    threads[0].join(3)
    second.join(3)


def test_no_restart_recovery_surface_is_added():
    import app.services.report_execution as module
    banned = ("resume", "recover", "restart", "requeue")
    for name in dir(ReportExecutionCoordinator):
        assert not any(token in name.lower() for token in banned), name
    for name in dir(module):
        assert not any(token in name.lower() for token in banned), name


# ---------------------------------------------------------------------------
# runtime 接线(真 repo):注册表身份 + 路由 helper 委托
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    return SQLiteRepository(Settings(_env_file=None))


def test_runtime_registry_identity_and_distinct_from_ask(repo):
    runtime = repo._runtime
    assert runtime.report_cancellations is REPORT_CANCELLATIONS
    # Task 23 落地后 ask 有自己的注册表;现在(无该属性)此不变量同样成立。
    assert getattr(runtime, "ask_cancellations", None) is not REPORT_CANCELLATIONS
    assert isinstance(runtime.report_execution, ReportExecutionCoordinator)
    assert runtime.report_execution.cancellations is REPORT_CANCELLATIONS
    assert runtime.report_execution.reports is runtime.report_store


def test_routes_launch_helpers_delegate_to_runtime_coordinator(repo, monkeypatch):
    import app.api.report_routes as routes_mod
    calls = []

    class _Coord:
        def start_plan(self, *a, **k):
            calls.append(("plan", a, k))

        def start_generate(self, *a, **k):
            calls.append(("gen", a, k))

    monkeypatch.setattr(repo._runtime, "report_execution", _Coord())
    routes_mod._launch_plan_job(repo, "nb", "rid", "q", "h", True)
    routes_mod._launch_generate_job(repo, "nb", "rid", "q", 5)
    assert calls[0][0] == "plan" and calls[0][1] == ("nb", "rid", "q", "h", True)
    assert calls[1][0] == "gen" and calls[1][1] == ("nb", "rid", "q", 5)
    assert calls[0][2]["user_id"] and calls[1][2]["user_id"]   # 发起者身份随行


@pytest.mark.parametrize(
    ("launch", "progress"),
    [
        (lambda coord: coord.start_plan("nb", "rid-submit", "q", user_id="u"),
         "规划失败"),
        (lambda coord: coord.start_generate("nb", "rid-submit", "q", user_id="u"),
         "失败"),
    ],
)
def test_submit_failure_marks_report_failed_and_unregisters(launch, progress):
    class _RaisingSubmitter:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, fn, *args, name=None, notify_pending=False, **kwargs):
            raise self.exc

    reports = _Reports()
    registry = ReportCancellationRegistry()
    coord, _engine, _reg, _captured = _coordinator(
        reports=reports,
        registry=registry,
        submitter=_RaisingSubmitter(RuntimeError("thread start failed")),
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        launch(coord)

    assert registry.cancel("rid-submit") is False
    assert reports.updates == [(('nb', 'rid-submit'), {
        "status": "failed",
        "error": "RuntimeError: thread start failed",
        "progress": progress,
    })]


def test_submit_failure_marks_the_existing_report_row_failed(repo):
    from app.models.schemas import NotebookCreate

    class _RaisingSubmitter:
        def __call__(self, fn, *args, name=None, notify_pending=False, **kwargs):
            raise RuntimeError("thread start failed")

    notebook = repo._runtime.catalog.create_notebook(
        NotebookCreate(name="n", purpose="p")
    )
    coordinator = repo._runtime.report_execution
    report_id = coordinator.reports.create_report(notebook.id, "q")
    original_submitter = coordinator.job_submitter
    coordinator.job_submitter = _RaisingSubmitter()
    try:
        with pytest.raises(RuntimeError, match="thread start failed"):
            coordinator.start_plan(notebook.id, report_id, "q", user_id="admin")
    finally:
        coordinator.job_submitter = original_submitter

    report = coordinator.reports.get_report(notebook.id, report_id)
    assert report["status"] == "failed"
    assert report["progress"] == "规划失败"
    assert report["error"] == "RuntimeError: thread start failed"
    assert coordinator.cancellations.cancel(report_id) is False
