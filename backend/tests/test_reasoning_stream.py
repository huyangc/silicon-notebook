import json

from fastapi.testclient import TestClient


def test_reasoning_stream_emits_progress_before_final(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")

    from app.core.config import get_settings
    from app.api import routes
    from app.main import create_app

    get_settings.cache_clear()
    routes.repository.cache_clear()

    client = TestClient(create_app())
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    response = client.post(
        f"/api/notebooks/{notebook_id}/ask/stream",
        json={"question": "RTL到GDSII流程", "mode": "reasoning"},
    )

    assert response.status_code == 200
    events = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]
    kinds = [event["event"] for event in events]

    assert "progress" in kinds
    assert kinds[-1] == "final"
    assert kinds.index("progress") < kinds.index("final")
    assert events[0]["step"]["step_type"] == "start"
    assert any(event.get("step", {}).get("step_type") == "plan" for event in events)
    assert events[-1]["response"]["conversation_id"]
    assert events[-1]["response"]["reasoning_trace"]
