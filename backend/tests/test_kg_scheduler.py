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


def test_submit_window_returns_result():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=2, job_workers=2)
    assert scheduler.submit_window(lambda a, b: a + b, 2, 3).result() == 5


def test_getters_reflect_config():
    from app.services.kg import scheduler
    scheduler.configure(window_workers=7, job_workers=3)
    assert scheduler.max_workers() == 7 and scheduler.job_concurrency() == 3
