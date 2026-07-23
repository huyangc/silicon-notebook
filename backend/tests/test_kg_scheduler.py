import threading, time
import pytest


@pytest.fixture(autouse=True)
def _reset_pools():
    from app.services.kg import scheduler
    scheduler.reset()
    yield
    scheduler.reset()


def _peak_counter():
    state = {"cur": 0, "peak": 0}
    lock = threading.Lock()
    def task():
        with lock:
            state["cur"] += 1; state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.05)
        with lock:
            state["cur"] -= 1
        return "ok"
    return state, task


def _blocked_task(started, release, result="ok"):
    def task():
        started.set()
        release.wait(timeout=5)
        return result
    return task


def test_window_pool_caps_concurrency():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    state, task = _peak_counter()
    futs = [scheduler.submit_window(task) for _ in range(6)]
    assert [f.result() for f in futs] == ["ok"] * 6
    assert state["peak"] <= 2


def test_job_pool_caps_concurrency():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=4, job_workers=2)
    state, task = _peak_counter()
    futs = [scheduler.submit_job(task) for _ in range(5)]
    [f.result() for f in futs]
    assert state["peak"] <= 2


def test_window_stats_report_one_queued_task():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=1, job_workers=1)
    started = threading.Event()
    release = threading.Event()
    first = scheduler.submit_window(_blocked_task(started, release, "first"))
    assert started.wait(timeout=2)
    second = scheduler.submit_window(lambda: "second")
    try:
        assert scheduler.stats() == {
            "window_active": 1,
            "window_max": 1,
            "window_waiting": 1,
            "job_active": 0,
            "job_max": 1,
            "job_waiting": 0,
        }
    finally:
        release.set()
    assert first.result(timeout=2) == "first"
    assert second.result(timeout=2) == "second"


def test_job_stats_report_one_queued_task():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=1, job_workers=1)
    started = threading.Event()
    release = threading.Event()
    first = scheduler.submit_job(_blocked_task(started, release, "first"))
    assert started.wait(timeout=2)
    second = scheduler.submit_job(lambda: "second")
    try:
        assert scheduler.stats() == {
            "window_active": 0,
            "window_max": 1,
            "window_waiting": 0,
            "job_active": 1,
            "job_max": 1,
            "job_waiting": 1,
        }
    finally:
        release.set()
    assert first.result(timeout=2) == "first"
    assert second.result(timeout=2) == "second"


@pytest.mark.parametrize(
    ("submit_name", "waiting_key"),
    (("submit_window", "window_waiting"), ("submit_job", "job_waiting")),
)
def test_cancelled_queued_task_clears_waiting_once(submit_name, waiting_key):
    from app.services.kg import scheduler
    scheduler.configure(window_workers=1, job_workers=1)
    submit = getattr(scheduler, submit_name)
    started = threading.Event()
    release = threading.Event()
    first = submit(_blocked_task(started, release, "first"))
    assert started.wait(timeout=2)
    cancelled = submit(lambda: "must-not-run")
    try:
        assert scheduler.stats()[waiting_key] == 1
        assert cancelled.cancel()
        assert scheduler.stats()[waiting_key] == 0
        assert cancelled.cancel()
        assert scheduler.stats()[waiting_key] == 0
    finally:
        release.set()
    assert first.result(timeout=2) == "first"


@pytest.mark.parametrize(
    ("pool_name", "submit_name", "waiting_key"),
    (
        ("_window_pool", "submit_window", "window_waiting"),
        ("_job_pool", "submit_job", "job_waiting"),
    ),
)
def test_submit_failure_restores_waiting(pool_name, submit_name, waiting_key, monkeypatch):
    from app.services.kg import scheduler
    scheduler.configure(window_workers=1, job_workers=1)
    pool = getattr(scheduler, pool_name)

    def fail_submit(*_args, **_kwargs):
        raise RuntimeError("submit failed")

    monkeypatch.setattr(pool, "submit", fail_submit)
    with pytest.raises(RuntimeError, match="submit failed"):
        getattr(scheduler, submit_name)(lambda: None)
    assert scheduler.stats()[waiting_key] == 0


@pytest.mark.parametrize(
    ("submit_name", "active_key", "waiting_key"),
    (
        ("submit_window", "window_active", "window_waiting"),
        ("submit_job", "job_active", "job_waiting"),
    ),
)
def test_task_exception_clears_active_and_waiting(submit_name, active_key, waiting_key):
    from app.services.kg import scheduler
    scheduler.configure(window_workers=1, job_workers=1)

    def fail():
        raise RuntimeError("task failed")

    future = getattr(scheduler, submit_name)(fail)
    with pytest.raises(RuntimeError, match="task failed"):
        future.result(timeout=2)
    assert scheduler.stats()[active_key] == 0
    assert scheduler.stats()[waiting_key] == 0


def _kg_json():
    import json
    return json.dumps({"nodes": [{"local_id": "a", "type": "Concept",
                                  "name": "current mirror", "ev": 0}], "edges": []})


def test_extract_graph_goes_through_window_pool(monkeypatch):
    from app.services.kg import scheduler
    from app.services import kg_ingest
    scheduler.configure(window_workers=2, job_workers=2)
    seen = {"n": 0}
    real = scheduler.submit_window
    def spy(fn, /, *a, **k):
        seen["n"] += 1
        return real(fn, *a, **k)
    monkeypatch.setattr(kg_ingest, "submit_window", spy)

    class FakeLLM:
        def chat_json(self, messages, hint):
            return _kg_json()

    raw = "Para one alpha.\n\nPara two beta.\n\nPara three gamma."
    g = kg_ingest.extract_graph(FakeLLM(), raw, "doc.md", "textbook", n=20, m=0)
    assert seen["n"] >= 1
    assert g.nodes


def test_window_cap_holds_across_concurrent_extract_graph(monkeypatch):
    from app.services.kg import scheduler
    from app.services import kg_ingest
    import threading, time
    scheduler.configure(window_workers=3, job_workers=4)
    cur = {"v": 0, "peak": 0}; lock = threading.Lock()

    class SlowLLM:
        def chat_json(self, messages, hint):
            with lock:
                cur["v"] += 1; cur["peak"] = max(cur["peak"], cur["v"])
            time.sleep(0.05)
            with lock:
                cur["v"] -= 1
            return _kg_json()

    raw = "\n\n".join(f"Para {i} word{i}." for i in range(6))
    errs = []
    def run():
        try:
            kg_ingest.extract_graph(SlowLLM(), raw, "d.md", "textbook", n=20, m=0)
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs
    assert cur["peak"] <= 3
