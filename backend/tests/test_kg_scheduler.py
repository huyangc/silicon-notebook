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
