"""W1-2: 后台 fire-and-forget job 的唯一入口 background_jobs.submit()。

核心不变量：一律 copy_context() 传播发起请求的上下文（per-user 模型 _REQUEST_USER +
per-user 日志归属 _log_owner 才能在 worker 线程生效，否则回退 user-local），并统一
顶层异常兜底不让 worker 静默崩溃。
"""
import contextvars
import threading

from app.core import diagnostics_runtime as diagnostics
from app.services import background_jobs

_probe = contextvars.ContextVar("bg_probe", default="DEFAULT")


def test_submit_propagates_caller_contextvars():
    """submit 的 worker 必须看到调用线程当时的 ContextVar 快照。"""
    token = _probe.set("caller-value")
    try:
        seen = {}
        done = threading.Event()

        def job():
            seen["v"] = _probe.get()
            done.set()

        t = background_jobs.submit(job, name="probe-job")
        assert done.wait(timeout=5)
        t.join(timeout=5)
        assert seen["v"] == "caller-value"
    finally:
        _probe.reset(token)


def test_bare_thread_does_not_propagate_control():
    """对照组：裸 threading.Thread 不传播 → worker 看到 default。
    这证明上面的传播是 submit 真的做了 copy_context，而非 ContextVar 本身跨线程可见。"""
    token = _probe.set("caller-value")
    try:
        seen = {}
        done = threading.Event()

        def job():
            seen["v"] = _probe.get()
            done.set()

        threading.Thread(target=job, daemon=True).start()
        assert done.wait(timeout=5)
        assert seen["v"] == "DEFAULT"
    finally:
        _probe.reset(token)


def test_submit_isolates_worker_exceptions():
    """worker 抛异常不得冒泡到调用方，线程应正常结束（顶层兜底）。"""
    done = threading.Event()

    def boom():
        done.set()
        raise ValueError("boom")

    t = background_jobs.submit(boom, name="boom-job")  # 不得抛
    assert done.wait(timeout=5)
    t.join(timeout=5)
    assert not t.is_alive()


def test_submit_forwards_positional_and_keyword_args():
    result = {}
    done = threading.Event()

    def job(a, b, c=None):
        result["v"] = (a, b, c)
        done.set()

    background_jobs.submit(job, 1, 2, c=3, name="args-job")
    assert done.wait(timeout=5)
    assert result["v"] == (1, 2, 3)


def test_submit_propagates_request_user_for_per_user_models():
    """直击 per-user 模型：submit 必须把 sqlite_repository._REQUEST_USER 这个真正驱动
    current_user()→resolve_model_config() 的 ContextVar 传到 worker，否则后台 KG job
    会回退 user-local、用错模型（build/rebuild/conflict-resolve 原来的 bug）。"""
    import types
    from app.services import sqlite_repository as sr

    token = sr._REQUEST_USER.set(types.SimpleNamespace(id="user-abc"))
    try:
        seen = {}
        done = threading.Event()

        def job():
            u = sr._REQUEST_USER.get()
            seen["id"] = u.id if u is not None else None
            done.set()

        background_jobs.submit(job, name="peruser-job")
        assert done.wait(timeout=5)
        assert seen["id"] == "user-abc"
    finally:
        sr._REQUEST_USER.reset(token)


def test_submit_returns_named_daemon_thread():
    done = threading.Event()
    t = background_jobs.submit(done.set, name="named-job")
    assert isinstance(t, threading.Thread)
    assert t.name == "named-job"
    assert t.daemon is True
    assert done.wait(timeout=5)


def test_submit_reports_active_and_completed_job(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def blocked_job():
        started.set()
        assert release.wait(timeout=5)

    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        thread = background_jobs.submit(blocked_job, name="diagnostic-job")
        assert started.wait(timeout=5)
        active = runtime.snapshot()["active_jobs"]
        assert len(active) == 1
        assert active[0]["name"] == "diagnostic-job"

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        snapshot = runtime.snapshot()
        assert snapshot["active_jobs"] == []
        assert snapshot["recent_jobs"][-1]["name"] == "diagnostic-job"
        assert snapshot["recent_jobs"][-1]["status"] == "done"


def test_submit_reports_failed_job_as_error(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def failing_job():
        started.set()
        assert release.wait(timeout=5)
        raise RuntimeError("expected diagnostic test failure")

    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        thread = background_jobs.submit(failing_job, name="failing-diagnostic-job")
        assert started.wait(timeout=5)
        assert runtime.snapshot()["active_jobs"][0]["name"] == "failing-diagnostic-job"

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        snapshot = runtime.snapshot()
        assert snapshot["active_jobs"] == []
        assert snapshot["recent_jobs"][-1]["name"] == "failing-diagnostic-job"
        assert snapshot["recent_jobs"][-1]["status"] == "error"
