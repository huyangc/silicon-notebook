from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    from app.core.config import get_settings
    from app.api import ask_routes
    from app.main import create_app
    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    return TestClient(create_app())


def test_ask_modes_endpoint_lists_user_facing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/ask-modes").json()
    assert [m["id"] for m in body] == ["chunk", "reasoning", "graph"]
    assert {m["id"]: m["requires_kg"] for m in body} == {
        "chunk": False, "reasoning": True, "graph": True}


def test_unknown_mode_returns_422_not_silent_fast(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": "bogus"})
    assert r.status_code == 422
    assert "bogus" in str(r.json()["detail"])
    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"})
    assert rs.status_code == 422


def test_chunk_mode_streams_start_then_final(tmp_path, monkeypatch):
    import json
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    # WS2a: 首事件现为 started(带 job_id,供前端「停止」调 cancel 端点),
    # 随后才是 progress/start。
    assert kinds[0] == "started" and events[0]["job_id"]
    assert kinds[1] == "progress" and events[1]["step"]["step_type"] == "start"
    assert events[1]["step"]["detail"]["mode"] == "chunk"
    assert kinds[-1] == "final"
    assert "reasoning_trace" not in events[-1]["response"] or \
        not events[-1]["response"]["reasoning_trace"]


def test_ask_stream_runs_through_the_runtime_ask_service(tmp_path, monkeypatch):
    """Task 24: 流式端点经 AskExecutionCoordinator 调 runtime-owned AskService
    (不再是 facade 回调)—— stub 掉服务的 ask 即可拦到整条流的 final 响应。"""
    import json
    from app.api.ask_routes import repository
    from app.models.schemas import AskResponse

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    service = repo._runtime.ask_service()
    seen = {}

    def fake_ask(
        notebook_id,
        payload,
        *,
        user_id,
        on_trace=None,
        cancel_event=None,
        job_id=None,
    ):
        seen["user_id"] = user_id
        return AskResponse(conclusion="service-stub", conversation_id=payload.conversation_id or "")

    monkeypatch.setattr(service, "ask", fake_ask, raising=False)
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert events[-1]["event"] == "final"
    assert events[-1]["response"]["conclusion"] == "service-stub"
    assert seen["user_id"] == repo.current_user().id
