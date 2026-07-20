# backend/tests/test_pool_report.py
"""周期性并发占用自报(共享 LLM/embedding gate + 源 job 池)的观测特性。

- scheduler.stats():in-flight(活跃)计数,submit_window/submit_job 在 worker 内 inc/dec;
  submit_job 仍走 ctx.run 传播 ContextVar(每用户 KG_LLM 配置的命脉,不得回归)。
- batch_ingest._format_pool_snapshot / _PoolReporter 只报告真实 gate snapshot。
"""
import contextvars
import threading
import time

import pytest

from app.services import batch_ingest as bi
from app.services.concept_merge_review import review_merge_candidates
from app.services.model_concurrency import (
    ConcurrencySnapshot,
    LimitedJsonChatClient,
    activate_model_concurrency,
)


@pytest.fixture(autouse=True)
def _reset_pools():
    from app.services.kg import scheduler
    scheduler.reset()
    yield
    scheduler.reset()


# ── scheduler.stats() 活跃计数 ────────────────────────────────────────────────

def test_stats_window_active_reflects_inflight():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(2)
        return "ok"

    fut = scheduler.submit_window(blocker)
    assert started.wait(2)
    s = scheduler.stats()
    assert s["window_active"] == 1
    assert s["window_max"] == 2
    release.set()
    assert fut.result(2) == "ok"
    # drained
    for _ in range(200):
        if scheduler.stats()["window_active"] == 0:
            break
        time.sleep(0.01)
    assert scheduler.stats()["window_active"] == 0


def test_stats_job_active_reflects_inflight():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    started = threading.Event()
    release = threading.Event()

    def blocker():
        started.set()
        release.wait(2)
        return "ok"

    fut = scheduler.submit_job(blocker)
    assert started.wait(2)
    s = scheduler.stats()
    assert s["job_active"] == 1
    assert s["job_max"] == 2
    release.set()
    assert fut.result(2) == "ok"
    for _ in range(200):
        if scheduler.stats()["job_active"] == 0:
            break
        time.sleep(0.01)
    assert scheduler.stats()["job_active"] == 0


def test_stats_before_pool_built_returns_zeros():
    """stats() 只读 globals、绝不 _ensure 建池:未建池前应返回 0(不炸)。"""
    from app.services.kg import scheduler
    scheduler.reset()
    s = scheduler.stats()
    assert s["window_active"] == 0 and s["job_active"] == 0


def test_submit_job_still_propagates_contextvar():
    """守卫:submit_job 必须仍通过 ctx.run 把 ContextVar 快照传给 worker
    (每用户 KG_LLM 配置的命脉——若退化成裸 submit(fn) 这里会读到默认值而失败)。"""
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    var: contextvars.ContextVar = contextvars.ContextVar("probe", default="DEFAULT")
    var.set("caller-value")
    seen = {}

    def reader():
        seen["v"] = var.get()
        return "ok"

    assert scheduler.submit_job(reader).result(2) == "ok"
    assert seen["v"] == "caller-value"


# ── _format_pool_snapshot 纯格式化 ───────────────────────────────────────────

def test_format_pool_snapshot_exact():
    s = {"window_active": 14, "window_max": 16, "job_active": 8, "job_max": 8}
    line = bi._format_pool_snapshot(
        "17:52:33",
        s,
        llm=ConcurrencySnapshot(active=14, maximum=16, waiting=3),
        embedding=ConcurrencySnapshot(active=6, maximum=20, waiting=4),
        done=5,
        total=40,
    )
    assert line == (
        "[pool 17:52:33] LLM 14/16 waiting=3"
        " · embedding 6/20 waiting=4 · source 8/8 · 源完成 5/40"
    )


def test_format_pool_snapshot_idle_gates():
    s = {"window_active": 0, "window_max": 1, "job_active": 0, "job_max": 1}
    line = bi._format_pool_snapshot(
        "00:00:03",
        s,
        llm=ConcurrencySnapshot(active=0, maximum=1, waiting=0),
        embedding=ConcurrencySnapshot(active=0, maximum=1, waiting=0),
        done=0,
        total=0,
    )
    assert line == (
        "[pool 00:00:03] LLM 0/1 waiting=0"
        " · embedding 0/1 waiting=0 · source 0/1"
    )


def test_format_pool_snapshot_label_when_no_source_total():
    """非抽取阶段(total=0)带 label(如 rebuild)→ 尾部显示 label 而非源完成。"""
    s = {"window_active": 2, "window_max": 16, "job_active": 0, "job_max": 8}
    line = bi._format_pool_snapshot(
        "00:00:03",
        s,
        llm=ConcurrencySnapshot(active=2, maximum=16, waiting=4),
        embedding=ConcurrencySnapshot(active=3, maximum=8, waiting=5),
        done=0,
        total=0,
        label="rebuild 阶段",
    )
    assert line == (
        "[pool 00:00:03] LLM 2/16 waiting=4"
        " · embedding 3/8 waiting=5 · source 0/8 · rebuild 阶段"
    )


# ── live gate snapshot ───────────────────────────────────────────────────────

def test_active_model_state_snapshots_drive_formatting():
    release = threading.Event()
    embed_started = threading.Event()

    def blocking_embed():
        embed_started.set()
        release.wait(2)

    with activate_model_concurrency(llm_max=3, embed_max=2) as state:
        future = state.embedding.submit(blocking_embed, task_prefix="probe")
        try:
            assert embed_started.wait(2)
            with state.llm.slot():
                line = bi._format_pool_snapshot(
                    "00:00:03",
                    {
                        "window_active": 0,
                        "window_max": 3,
                        "job_active": 0,
                        "job_max": 4,
                    },
                    llm=state.llm.snapshot(),
                    embedding=state.embedding.snapshot(),
                    done=0,
                    total=0,
                )
                assert "LLM 1/3 waiting=0" in line
                assert "embedding 1/2 waiting=0" in line
        finally:
            release.set()
        future.result(timeout=2)


# ── _PoolReporter 后台自报 ───────────────────────────────────────────────────

def test_pool_reporter_interval_zero_starts_no_thread(capsys):
    with bi._PoolReporter(interval=0, total=10) as r:
        assert r._thread is None
        time.sleep(0.05)
    out = capsys.readouterr().out
    assert "[pool" not in out


def test_pool_reporter_prints_and_reflects_done(capsys):
    logs = []
    with activate_model_concurrency(llm_max=7, embed_max=3):
        with bi._PoolReporter(interval=0.02, total=40, log=logs.append) as r:
            r.done = 5
            time.sleep(0.1)   # ~5 intervals
    out = capsys.readouterr().out
    assert "[pool" in out
    assert "LLM 0/7 waiting=0" in out
    assert "embedding 0/3 waiting=0" in out
    assert "源完成 5/40" in out
    assert any(
        e.get("phase") == "pool"
        and e.get("done") == 5
        and e.get("total") == 40
        and e.get("llm_active") == 0
        and e.get("llm_max") == 7
        and e.get("llm_waiting") == 0
        and e.get("embed_active") == 0
        and e.get("embed_max") == 3
        and e.get("embed_waiting") == 0
        for e in logs
    )


def test_pool_reporter_exception_in_loop_never_raises(capsys, monkeypatch):
    """报告循环内任何异常(坏 stats/print)都被吞,绝不打断摄取。"""
    from app.services.kg import scheduler

    def boom():
        raise RuntimeError("stats blew up")

    monkeypatch.setattr(scheduler, "stats", boom)
    # 不应抛
    with bi._PoolReporter(interval=0.02, total=1) as r:
        r.done = 0
        time.sleep(0.06)
    # 到这里没炸即通过


# ── rebuild LLM calls share the same gate ────────────────────────────────────

def test_parallel_rebuild_review_calls_are_reported_by_shared_llm_gate():
    """Real merge-review worker calls use the already-resolved limited client.

    With two parallel review chunks and llm_max=1, the live gate must report one
    active and one waiting call; no thread-name estimation or special rebuild
    counters are involved.
    """

    class _BlockingReviewClient:
        configured = True

        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()

        def chat_json(self, *_args, **_kwargs):
            self.started.set()
            self.release.wait(2)
            return '{"decisions":[]}'

    raw = _BlockingReviewClient()
    candidates = [
        {
            "id": f"candidate-{i}",
            "canonical_a": f"A{i}",
            "canonical_b": f"B{i}",
        }
        for i in range(2)
    ]
    completed = threading.Event()
    errors: list[BaseException] = []

    with activate_model_concurrency(llm_max=1, embed_max=1) as state:
        limited = LimitedJsonChatClient(raw, state.llm)

        def run_review():
            try:
                review_merge_candidates(
                    limited,
                    candidates,
                    batch_size=1,
                    max_workers=2,
                )
            except BaseException as exc:  # surface worker failures on the test thread
                errors.append(exc)
            finally:
                completed.set()

        review_thread = threading.Thread(target=run_review, daemon=True)
        review_thread.start()
        try:
            assert raw.started.wait(2)
            deadline = time.time() + 2
            snapshot = state.llm.snapshot()
            while snapshot.waiting != 1 and time.time() < deadline:
                time.sleep(0.01)
                snapshot = state.llm.snapshot()

            line = bi._format_pool_snapshot(
                "00:00:03",
                {
                    "window_active": 0,
                    "window_max": 1,
                    "job_active": 0,
                    "job_max": 2,
                },
                llm=snapshot,
                embedding=state.embedding.snapshot(),
                done=0,
                total=0,
                label="rebuild 阶段",
            )
            assert line == (
                "[pool 00:00:03] LLM 1/1 waiting=1"
                " · embedding 0/1 waiting=0 · source 0/2 · rebuild 阶段"
            )
        finally:
            raw.release.set()
            review_thread.join(2)

        assert completed.is_set()
        assert errors == []
        assert state.llm.snapshot() == ConcurrencySnapshot(
            active=0,
            maximum=1,
            waiting=0,
        )
