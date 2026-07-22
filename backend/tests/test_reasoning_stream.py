import json

from fastapi.testclient import TestClient
from tests.model_testkit import bind_chat_client, bind_all_embedding_clients


class _ReasoningLLM:
    configured = True

    def chat_json(self, messages, schema, **kwargs):
        if "sub_queries" in schema:
            return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
        if "next_action" in schema:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "RTL到GDSII流程 [k1].", "grounded": True})


def test_reasoning_stream_emits_progress_before_final(tmp_path, monkeypatch):
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

    client = TestClient(create_app())
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    # 需要至少一个 KG 节点，否则 P4-6 门控直接返回 kg_required=True（无 plan 步骤）
    from app.core.config import get_settings as _gs
    from app.services.embedding import FakeEmbedder
    repo = ask_routes.repository()
    bind_all_embedding_clients(repo, FakeEmbedder(dim=_gs().embed_dim))
    llm = _ReasoningLLM()
    bind_chat_client(repo, "reasoning_agent", llm)
    bind_chat_client(repo, "ask_answer", llm)
    repo.store_kg(notebook_id, None, [
        {"local_id": "K1", "object_type": "concept",
         "payload": {"name": "RTL到GDSII流程概述"}, "evidence": []}
    ], [])

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

    # WS2a: 首事件为 started(带 job_id),随后才是 progress/start。
    assert kinds[0] == "started" and events[0]["job_id"]
    assert "progress" in kinds
    assert kinds[-1] == "final"
    assert kinds.index("progress") < kinds.index("final")
    assert events[1]["step"]["step_type"] == "start"
    assert any(event.get("step", {}).get("step_type") == "plan" for event in events)
    assert events[-1]["response"]["conversation_id"]
    assert events[-1]["response"]["reasoning_trace"]
