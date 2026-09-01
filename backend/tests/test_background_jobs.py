"""W1-2: 后台 fire-and-forget job 的唯一入口 background_jobs.submit()。

核心不变量：一律 copy_context() 传播发起请求的上下文（per-user 模型 _REQUEST_USER +
per-user 日志归属 _log_owner 才能在 worker 线程生效，否则回退 user-local），并统一
顶层异常兜底不让 worker 静默崩溃。
"""
import contextvars
import logging
import threading

import pytest

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
        assert active[0]["name"] == "background_job"

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        snapshot = runtime.snapshot()
        assert snapshot["active_jobs"] == []
        assert snapshot["recent_jobs"][-1]["name"] == "background_job"
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
        assert runtime.snapshot()["active_jobs"][0]["name"] == "background_job"

        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        snapshot = runtime.snapshot()
        assert snapshot["active_jobs"] == []
        assert snapshot["recent_jobs"][-1]["name"] == "background_job"
        assert snapshot["recent_jobs"][-1]["status"] == "error"


def test_submit_keeps_job_active_until_pending_notification_finishes(
    tmp_path, monkeypatch
):
    notification_started = threading.Event()
    release_notification = threading.Event()

    def notify(_user_id: str) -> None:
        notification_started.set()
        assert release_notification.wait(timeout=5)

    monkeypatch.setattr(background_jobs, "_resolve_job_user", lambda: "user-safe")
    monkeypatch.setattr(background_jobs.pending_bus, "mark_dirty", notify)

    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        thread = background_jobs.submit(
            lambda: None,
            name="pending-notification-job",
            notify_pending=True,
        )
        assert notification_started.wait(timeout=5)
        active = runtime.snapshot()["active_jobs"]
        assert len(active) == 1
        assert active[0]["name"] == "background_job"

        release_notification.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        snapshot = runtime.snapshot()
        assert snapshot["active_jobs"] == []
        assert snapshot["recent_jobs"][-1]["status"] == "done"


@pytest.mark.parametrize(
    "label,expected",
    [
        ("buildkg-nb-private123", "buildkg"),
        ("report-plan-report-private123", "report-plan"),
        ("knowhow-project-table-private123", "knowhow-project"),
        ("knowhow-asset-sweep:nb-private123", "knowhow-asset-sweep"),
        ("index-pipeline-nb-private123", "index-pipeline"),
        ("ask-reasoning", "ask-reasoning"),
    ],
)
def test_submit_separates_thread_name_from_safe_diagnostic_operation(
    tmp_path, label, expected
):
    started = threading.Event()
    release = threading.Event()

    def representative_worker():
        started.set()
        assert release.wait(timeout=5)

    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        thread = background_jobs.submit(representative_worker, name=label)
        assert started.wait(timeout=5)
        assert thread.name == label
        active = runtime.snapshot()["active_jobs"]
        assert active[0]["name"] == expected
        assert "private123" not in str(active)
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_explicit_unallowlisted_name_does_not_inspect_callable_metadata():
    done = threading.Event()
    name_accessed = threading.Event()

    class CallableWithBrokenName:
        @property
        def __name__(self):
            name_accessed.set()
            raise RuntimeError("diagnostic metadata unavailable")

        def __call__(self):
            done.set()

    thread = background_jobs.submit(
        CallableWithBrokenName(), name="ordinary-thread-name"
    )
    assert done.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not name_accessed.is_set()


def test_unnamed_callable_object_does_not_inspect_arbitrary_name_property():
    done = threading.Event()
    name_accessed = threading.Event()

    class CallableWithBlockingName:
        @property
        def __name__(self):
            name_accessed.set()
            raise RuntimeError("must not inspect callable objects")

        def __call__(self):
            done.set()

    thread = background_jobs.submit(CallableWithBlockingName())
    assert done.wait(timeout=5)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not name_accessed.is_set()


def test_unnamed_ordinary_function_uses_bounded_code_name(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def ordinary_worker() -> None:
        started.set()
        assert release.wait(timeout=5)

    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        thread = background_jobs.submit(ordinary_worker)
        assert started.wait(timeout=5)
        assert runtime.snapshot()["active_jobs"][0]["name"] == "ordinary_worker"
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_failed_job_log_uses_safe_operation_instead_of_raw_entity_id(caplog):
    def fail() -> None:
        raise RuntimeError("expected failure")

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.jobs"):
        thread = background_jobs.submit(
            fail, name="report-gen-report-private123"
        )
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert "background job failed: report-gen" in caplog.text
    assert "report-private123" not in caplog.text


# --- 后台 job 的进程级并发闸(重活池 / 轻活池) ----------------------------


HEAVY_JOB_NAMES = (
    "buildkg-nb-a",
    "rebuildkg-nb-a",
    "relinkkg-nb-a",
    "unifiedkg-nb-a",
    "conflictresolve-nb-a",
    "mergereview-nb-a",
    "index-pipeline-nb-a",
)
LIGHT_JOB_NAMES = (
    "papermeta-nb-a",
    "catalog-source-a",
    "knowhow-project-t1",
    "knowhow-legacy-reproject-t1",
    "knowhow-asset-sweep:nb-a",
)
# 批 3·W1 PR-3(D-2):第三个独立池——量级既非 LLM 扇出型重活,也非秒级轻活。
DELETE_JOB_NAMES = ("deletenb-nb-a",)
UNGATED_JOB_NAMES = ("ask-chunk", "ask-reasoning", "ask-graph",
                     "report-plan-r1", "report-gen-r1")


@pytest.fixture
def gate_capacity(monkeypatch):
    """把三个池的容量固定成 1(可调),并保证闸按当前配置重建。"""
    from app.core import config as config_module

    def _configure(heavy: int = 1, light: int = 1, delete: int = 1) -> None:
        settings = config_module.get_settings()
        monkeypatch.setattr(
            settings, "background_maintenance_concurrency", heavy, raising=False)
        monkeypatch.setattr(
            settings, "background_light_job_concurrency", light, raising=False)
        monkeypatch.setattr(
            settings, "notebook_delete_concurrency", delete, raising=False)
        background_jobs._reset_maintenance_gate_for_tests()

    _configure()
    return _configure


def _occupy_one_slot(name: str):
    """提交一个占住槽不放的 job,返回 (thread, 放行事件)。"""
    started = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        started.set()
        assert release.wait(timeout=5)

    thread = background_jobs.submit(blocked, name=name)
    assert started.wait(timeout=5)
    return thread, release


def test_gated_categories_are_a_subset_of_the_safe_prefix_table(gate_capacity):
    """进闸类别必须全部来自 `_SAFE_JOB_PREFIXES`——否则前缀判定永远匹配不到它,
    闸看着配好了却一个 job 都拦不住(纯拼写错误就能让整个特性静默失效)。"""
    known = {operation for _, operation in background_jobs._SAFE_JOB_PREFIXES}
    assert background_jobs._MAINTENANCE_OPERATIONS <= known
    # 三个池两两互斥,且并起来正好是进闸集合(没有孤儿类别)。
    heavy = background_jobs._HEAVY_MAINTENANCE_OPERATIONS
    light = background_jobs._LIGHT_MAINTENANCE_OPERATIONS
    delete = background_jobs._DELETE_OPERATIONS
    assert not (heavy & light)
    assert not (heavy & delete)
    assert not (light & delete)
    assert heavy | light | delete == background_jobs._MAINTENANCE_OPERATIONS
    # 交互路径与报告刻意在闸外。
    assert not ({"report-plan", "report-gen"} & background_jobs._MAINTENANCE_OPERATIONS)
    assert not (background_jobs._SAFE_ASK_JOB_NAMES
                & background_jobs._MAINTENANCE_OPERATIONS)


@pytest.mark.parametrize("name", HEAVY_JOB_NAMES + LIGHT_JOB_NAMES + DELETE_JOB_NAMES)
def test_each_gated_category_serializes_at_capacity_one(gate_capacity, name):
    """容量=1 时同池两个 job 串行:第二个必须等第一个结束才开始。

    顺序用 event 证明(不是 sleep):负向断言「第一个占槽期间第二个没开始」,
    正向断言「实际执行顺序就是提交顺序」——只有负向的话,一个从不执行第二个
    job 的实现也能骗过它。
    """
    order = []
    order_lock = threading.Lock()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def first() -> None:
        with order_lock:
            order.append("first")
        first_started.set()
        assert release_first.wait(timeout=5)

    def second() -> None:
        with order_lock:
            order.append("second")
        second_started.set()

    t1 = background_jobs.submit(first, name=name)
    assert first_started.wait(timeout=5)
    t2 = background_jobs.submit(second, name=name)
    assert not second_started.wait(timeout=0.2)   # 闸真的挡住了它

    release_first.set()
    assert second_started.wait(timeout=5)
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order == ["first", "second"]
    assert not t1.is_alive() and not t2.is_alive()


@pytest.mark.parametrize("light_name", LIGHT_JOB_NAMES)
def test_light_jobs_are_not_starved_by_a_saturated_heavy_pool(
    gate_capacity, light_name
):
    """两个池独立:重活池占满时秒级轻活仍立即执行(合池就会被饿死)。"""
    heavy, release = _occupy_one_slot("buildkg-nb-a")
    ran = threading.Event()
    light = background_jobs.submit(ran.set, name=light_name)
    assert ran.wait(timeout=5)

    release.set()
    heavy.join(timeout=5)
    light.join(timeout=5)


@pytest.mark.parametrize("heavy_name", HEAVY_JOB_NAMES)
def test_heavy_jobs_are_not_blocked_by_a_saturated_light_pool(
    gate_capacity, heavy_name
):
    """反方向同理:轻活池占满不得挡住重活(证明两把闸不是同一个对象)。"""
    light, release = _occupy_one_slot("knowhow-project-t1")
    ran = threading.Event()
    heavy = background_jobs.submit(ran.set, name=heavy_name)
    assert ran.wait(timeout=5)

    release.set()
    light.join(timeout=5)
    heavy.join(timeout=5)


def test_delete_jobs_are_not_starved_by_a_saturated_heavy_pool(gate_capacity):
    """删除池独立于重活池:重活池占满时删除作业仍立即执行。变异钉:把
    `deletenb` 判成重活类别(合进 `_HEAVY_MAINTENANCE_OPERATIONS`)会让本用例
    变红——第二个 job 会被挡在同一把重活闸后面而不是立即跑。"""
    heavy, release = _occupy_one_slot("buildkg-nb-a")
    ran = threading.Event()
    delete_job = background_jobs.submit(ran.set, name="deletenb-nb-a")
    assert ran.wait(timeout=5)

    release.set()
    heavy.join(timeout=5)
    delete_job.join(timeout=5)


def test_heavy_jobs_are_not_blocked_by_a_saturated_delete_pool(gate_capacity):
    """反方向同理:删除池占满不得挡住重活(证明删除闸不是重活闸的别名)。"""
    delete_job, release = _occupy_one_slot("deletenb-nb-a")
    ran = threading.Event()
    heavy = background_jobs.submit(ran.set, name="buildkg-nb-a")
    assert ran.wait(timeout=5)

    release.set()
    delete_job.join(timeout=5)
    heavy.join(timeout=5)


@pytest.mark.parametrize("name", UNGATED_JOB_NAMES)
def test_interactive_and_report_jobs_are_never_gated(gate_capacity, name):
    """ask-*(用户在线等)与 report-*(已有整篇准入闸)不进任何一个池。"""
    heavy, release_heavy = _occupy_one_slot("buildkg-nb-a")
    light, release_light = _occupy_one_slot("knowhow-project-t1")

    ran = threading.Event()
    ungated = background_jobs.submit(ran.set, name=name)
    assert ran.wait(timeout=5)

    release_heavy.set()
    release_light.set()
    heavy.join(timeout=5)
    light.join(timeout=5)
    ungated.join(timeout=5)


def test_maintenance_slot_is_released_when_the_job_raises(gate_capacity):
    """异常路径必须释放槽位,否则一次失败就永久少一个槽。"""
    boom_done = threading.Event()

    def boom() -> None:
        boom_done.set()
        raise RuntimeError("expected maintenance failure")

    failing = background_jobs.submit(boom, name="relinkkg-nb-a")
    assert boom_done.wait(timeout=5)
    failing.join(timeout=5)

    ran = threading.Event()
    following = background_jobs.submit(ran.set, name="unifiedkg-nb-a")
    assert ran.wait(timeout=5)
    following.join(timeout=5)


@pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning"
)
def test_maintenance_slot_is_released_when_the_job_raises_base_exception(
    gate_capacity,
):
    """BaseException(如 SystemExit)同样要释放:`except Exception` 接不到它们。"""
    exiting_done = threading.Event()

    def exiting() -> None:
        exiting_done.set()
        raise SystemExit(1)

    exiting_thread = background_jobs.submit(exiting, name="mergereview-nb-a")
    assert exiting_done.wait(timeout=5)
    exiting_thread.join(timeout=5)

    ran = threading.Event()
    following = background_jobs.submit(ran.set, name="buildkg-nb-b")
    assert ran.wait(timeout=5)
    following.join(timeout=5)


def test_capacity_admits_configured_number_of_jobs_per_pool(gate_capacity):
    """容量=2 时恰好两个同池 job 可并发,第三个必须等待(闸读的是部署配置值)。"""
    gate_capacity(heavy=2, light=1)
    barrier = threading.Barrier(3, timeout=5)
    order = []
    order_lock = threading.Lock()
    third_started = threading.Event()
    release = threading.Event()

    def occupy() -> None:
        with order_lock:
            order.append("occupy")
        barrier.wait()
        assert release.wait(timeout=5)

    def third() -> None:
        with order_lock:
            order.append("third")
        third_started.set()

    a = background_jobs.submit(occupy, name="buildkg-nb-a")
    b = background_jobs.submit(occupy, name="rebuildkg-nb-b")
    barrier.wait()   # 两个 job 与本线程一起到齐 → 它们确实同时在跑

    c = background_jobs.submit(third, name="relinkkg-nb-c")
    assert not third_started.wait(timeout=0.2)

    release.set()
    assert third_started.wait(timeout=5)
    for thread in (a, b, c):
        thread.join(timeout=5)
    assert order == ["occupy", "occupy", "third"]


def test_queued_maintenance_jobs_share_fixed_workers_not_one_waiting_thread_each(
    gate_capacity,
):
    """队列里的每个 job 只能有句柄，不能各自占一个阻塞在 semaphore 的线程。"""
    occupying, release = _occupy_one_slot("buildkg-nb-a")
    queued = [
        background_jobs.submit(lambda: None, name=f"rebuildkg-nb-{index}")
        for index in range(8)
    ]

    executor = background_jobs._executors[background_jobs._HEAVY_POOL]
    assert executor.capacity == 1
    assert len(executor._workers) == 1
    assert sum(worker.is_alive() for worker in executor._workers) == 1
    assert all(handle.is_alive() for handle in queued)

    release.set()
    for handle in (occupying, *queued):
        handle.join(timeout=5)
        assert not handle.is_alive()


def test_maintenance_job_handle_cannot_be_started_twice(gate_capacity):
    """句柄已经代表已提交任务；start() 不能悄悄另起一个空 Thread。"""
    done = threading.Event()
    handle = background_jobs.submit(done.set, name="buildkg-nb-a")
    with pytest.raises(RuntimeError, match="already submitted"):
        handle.start()
    assert done.wait(timeout=5)
    handle.join(timeout=5)


def test_shared_worker_logs_and_survives_base_exception(gate_capacity, caplog):
    """SystemExit 不得杀掉固定 worker，日志只能带池/类别而不能泄露异常内容。"""
    ran = threading.Event()

    def exiting() -> None:
        raise SystemExit("private job detail")

    with caplog.at_level(logging.ERROR, logger="silicon_notebook.jobs"):
        exiting_handle = background_jobs.submit(exiting, name="buildkg-nb-private")
        exiting_handle.join(timeout=5)
        following = background_jobs.submit(ran.set, name="rebuildkg-nb-a")
        assert ran.wait(timeout=5)
        following.join(timeout=5)

    assert "background worker survived base exception" in caplog.text
    assert "pool=maintenance" in caplog.text
    assert "operation=buildkg" in caplog.text
    assert "private job detail" not in caplog.text


def test_queued_maintenance_job_uses_submit_time_context_snapshot(gate_capacity):
    """固定 worker 不继承请求上下文，必须运行每个 job 自己捕获的 Context。"""
    occupying, release = _occupy_one_slot("buildkg-nb-a")
    token = _probe.set("queued-caller-value")
    try:
        seen = {}
        done = threading.Event()

        def queued_job() -> None:
            seen["value"] = _probe.get()
            done.set()

        queued = background_jobs.submit(queued_job, name="rebuildkg-nb-b")
    finally:
        _probe.reset(token)

    release.set()
    assert done.wait(timeout=5)
    occupying.join(timeout=5)
    queued.join(timeout=5)
    assert seen["value"] == "queued-caller-value"


def test_unnamed_job_is_never_gated_by_callable_name(gate_capacity):
    """准入只看显式 name:一个恰好叫 buildkg 的函数不得因此进闸。"""
    occupying, release = _occupy_one_slot("buildkg-nb-a")
    ran = threading.Event()

    def buildkg() -> None:
        ran.set()

    unnamed = background_jobs.submit(buildkg)
    assert ran.wait(timeout=5)

    release.set()
    occupying.join(timeout=5)
    unnamed.join(timeout=5)


def test_queued_job_is_disclosed_in_logs_without_ids(gate_capacity, monkeypatch,
                                                     caplog):
    """排队久了要在日志里说清楚:库里的状态在排队期间仍是 running/queued,
    日志是运维分辨「还没开始跑」与「跑着但很慢」的唯一手段。只带池名/类别名/
    秒数——绝不带 notebook/source id。"""
    monkeypatch.setattr(background_jobs, "_QUEUE_WARN_SECONDS", 0.05)
    occupying, release = _occupy_one_slot("buildkg-nb-private123")

    ran = threading.Event()
    with caplog.at_level(logging.INFO, logger="silicon_notebook.jobs"):
        queued = background_jobs.submit(ran.set, name="rebuildkg-nb-private456")
        assert not ran.wait(timeout=0.3)          # 确实在排队(已越过阈值)
        release.set()
        assert ran.wait(timeout=5)
        occupying.join(timeout=5)
        queued.join(timeout=5)

    assert "background job still queued" in caplog.text
    assert "operation=rebuildkg" in caplog.text
    assert "background job started after queueing" in caplog.text
    assert "private123" not in caplog.text
    assert "private456" not in caplog.text


def test_job_that_runs_without_queueing_logs_nothing(gate_capacity, caplog):
    """没排队就不该有排队日志(闸不能给每个 job 都加一行噪音)。"""
    ran = threading.Event()
    with caplog.at_level(logging.INFO, logger="silicon_notebook.jobs"):
        thread = background_jobs.submit(ran.set, name="buildkg-nb-a")
        assert ran.wait(timeout=5)
        thread.join(timeout=5)
    assert "queued" not in caplog.text


def test_queued_job_is_not_reported_as_an_active_job(gate_capacity, tmp_path):
    """排队必须发生在 `diagnostics.job_scope` 之外:等槽的 job 不是「在跑的
    job」,算进 active_jobs 会让运维看到一个永远在忙、实际零 I/O 的任务。"""
    with diagnostics.activate_runtime(
        tmp_path,
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    ) as runtime:
        occupying, release = _occupy_one_slot("buildkg-nb-a")

        ran = threading.Event()
        queued = background_jobs.submit(ran.set, name="rebuildkg-nb-b")
        assert not ran.wait(timeout=0.2)
        # 占槽的那个在跑 → 恰好一个 active job;排队的那个不在其中。
        assert len(runtime.snapshot()["active_jobs"]) == 1

        release.set()
        assert ran.wait(timeout=5)
        occupying.join(timeout=5)
        queued.join(timeout=5)
