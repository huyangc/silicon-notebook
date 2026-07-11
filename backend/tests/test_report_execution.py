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

from app.services.report_execution import (
    REPORT_CANCELLATIONS,
    ReportCancellationRegistry,
    ReportExecutionCoordinator,
)


class _Reports:
    def __init__(self, depth=7, boom=False):
        self.depth = depth
        self.boom = boom

    def get_report(self, notebook_id, report_id):
        if self.boom:
            raise KeyError(report_id)
        return {"id": report_id, "depth": self.depth}

    def update_report(self, *a, **k):
        pass


class _Engine:
    def __init__(self, boom=False):
        self.boom = boom
        self.run_calls = []
        self.generate_calls = []

    def run(self, notebook_id, rid, question, history="", depth=2,
            auto_generate=False):
        if self.boom:
            raise RuntimeError("engine down")
        self.run_calls.append((notebook_id, rid, question, history, depth,
                               auto_generate))

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
        self.calls.append({
            "fn": fn, "name": name, "notify_pending": notify_pending,
            # cancel() 命中即证明 submit 时已注册(顺带 set 事件,假引擎不受影响)
            "registered_at_submit": self.registry.cancel(self.probe_key),
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
    registry.register("r1", event)
    assert registry.cancel("r1") is True and event.is_set()
    registry.unregister("r1")
    assert registry.cancel("r1") is False              # 注销后不再命中
    registry.unregister("r1")                          # 幂等


def test_module_cancel_functions_share_the_process_global_registry():
    from app.services import report_engine
    event = report_engine.register_cancel("rep-shared")
    try:
        assert REPORT_CANCELLATIONS.cancel("rep-shared") is True and event.is_set()
    finally:
        report_engine.unregister_cancel("rep-shared")
    assert report_engine.cancel_report("rep-shared") is False

    other = threading.Event()
    REPORT_CANCELLATIONS.register("rep-shared2", other)
    assert report_engine.cancel_report("rep-shared2") is True and other.is_set()
    report_engine.unregister_cancel("rep-shared2")
    assert REPORT_CANCELLATIONS.cancel("rep-shared2") is False


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
    assert engine.run_calls == [("nb", "rid-1", "q", "h", 7, True)]  # depth 从行读
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
    assert engine.run_calls == [("nb", "rid-3", "q", "", 2, False)]


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


def test_coordinator_submits_through_background_jobs_with_copied_context():
    from app.services import background_jobs
    probe = contextvars.ContextVar("task25_exec_probe", default="")
    probe.set("ctx-report")
    seen = {}
    done = threading.Event()

    class _CtxEngine:
        def run(self, *a, **k):
            seen["ctx"] = probe.get()
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
    import app.api.routes as routes_mod
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
