# backend/tests/test_pool_report.py
"""周期性线程池占用自报(KG-LLM window 池 / 源 job 池 / embed 线程)的观测特性。

- scheduler.stats():in-flight(活跃)计数,submit_window/submit_job 在 worker 内 inc/dec;
  submit_job 仍走 ctx.run 传播 ContextVar(每用户 KG_LLM 配置的命脉,不得回归)。
- batch_ingest._format_pool_snapshot / _live_embed_thread_counts / _PoolReporter 纯观测。
"""
import contextvars
import threading
import time

import pytest

from app.services import batch_ingest as bi


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
    from collections import Counter
    s = {"window_active": 14, "window_max": 16, "job_active": 8, "job_max": 8}
    embed = Counter({"bg": 6, "pool": 20})
    line = bi._format_pool_snapshot("17:52:33", s, embed, 5, 40)
    assert line == (
        "[pool 17:52:33] KG-LLM(window) 14/16 · 源(job) 8/8"
        " · embed 6bg+20pool · 源完成 5/40"
    )


def test_format_pool_snapshot_missing_embed_keys_zero():
    from collections import Counter
    s = {"window_active": 0, "window_max": 1, "job_active": 0, "job_max": 1}
    # total=0 且无 label → 尾部无「源完成」(该段仅抽取期显示);此处重点验 embed 缺键→0bg+0pool。
    line = bi._format_pool_snapshot("00:00:03", s, Counter(), 0, 0)
    assert line == (
        "[pool 00:00:03] KG-LLM(window) 0/1 · 源(job) 0/1"
        " · embed 0bg+0pool"
    )


def test_format_pool_snapshot_label_when_no_source_total():
    """非抽取阶段(total=0)带 label(如 rebuild)→ 尾部显示 label 而非源完成。"""
    from collections import Counter
    s = {"window_active": 2, "window_max": 16, "job_active": 0, "job_max": 8}
    line = bi._format_pool_snapshot("00:00:03", s, Counter({"pool": 3}), 0, 0, label="rebuild 阶段")
    assert line == (
        "[pool 00:00:03] KG-LLM(window) 2/16 · 源(job) 0/8"
        " · embed 0bg+3pool · rebuild 阶段"
    )


# ── _live_embed_thread_counts 线程名统计 ─────────────────────────────────────

def test_live_embed_thread_counts():
    base = bi._live_embed_thread_counts()
    release = threading.Event()
    started = [threading.Event() for _ in range(3)]

    def blocker(ev):
        ev.set()
        release.wait(2)

    threads = [
        threading.Thread(target=blocker, args=(started[0],), name="embed-x", daemon=True),
        threading.Thread(target=blocker, args=(started[1],), name="embed-z", daemon=True),
        threading.Thread(target=blocker, args=(started[2],), name="emb-y", daemon=True),
    ]
    for t in threads:
        t.start()
    for ev in started:
        assert ev.wait(2)
    try:
        c = bi._live_embed_thread_counts()
        # ≥ 基线 + 我们起的(bg=2 个 embed-*, pool=1 个 emb-*)
        assert c["bg"] >= base.get("bg", 0) + 2
        assert c["pool"] >= base.get("pool", 0) + 1
    finally:
        release.set()
        for t in threads:
            t.join(2)


# ── _PoolReporter 后台自报 ───────────────────────────────────────────────────

def test_pool_reporter_interval_zero_starts_no_thread(capsys):
    with bi._PoolReporter(interval=0, total=10) as r:
        assert r._thread is None
        time.sleep(0.05)
    out = capsys.readouterr().out
    assert "[pool" not in out


def test_pool_reporter_prints_and_reflects_done(capsys):
    logs = []
    with bi._PoolReporter(interval=0.02, total=40, log=logs.append) as r:
        r.done = 5
        time.sleep(0.1)   # ~5 intervals
    out = capsys.readouterr().out
    assert "[pool" in out
    # 至少一行反映 done/total
    assert "源完成 5/40" in out
    # 结构化 log 也发了 pool 相 phase
    assert any(e.get("phase") == "pool" and e.get("done") == 5 and e.get("total") == 40
               for e in logs)


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
