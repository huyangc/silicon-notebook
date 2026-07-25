import json

from fastapi.testclient import TestClient
from tests.model_testkit import bind_chat_client, bind_all_embedding_clients


class _ReasoningLLM:
    configured = True

    def __init__(self):
        self.intent_prompts = []

    def chat_json(self, messages, schema, **kwargs):
        if "mandatory_topics" in schema:
            self.intent_prompts.append(messages[-1]["content"])
            return json.dumps({
                "normalized_question": "RTL 到 GDSII 的完整实现流程是什么？",
                "intent_type": "explain",
                "entities": ["RTL", "GDSII"],
                "mandatory_topics": [{
                    "title": "实现流程",
                    "question": "RTL 到 GDSII 包含哪些阶段？",
                    "retrieval_queries": [
                        "RTL 到 GDSII 实现流程",
                        "RTL 到 GDSII 签核阶段",
                    ],
                }],
                "ambiguities": [],
                "confidence": 0.95,
                "needs_clarification": False,
            })
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

    preview = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": "RTL到GDSII流程"},
    )
    assert preview.status_code == 200
    contract = preview.json()
    assert contract["resolved_question"].startswith("RTL 到 GDSII")
    # 意图预检不能提前创建会话或 Ask job。
    assert client.get(
        f"/api/notebooks/{notebook_id}/conversations"
    ).json() == []

    bypassed_preview = {
        "question": "帮我分析一下这个问题",
        "mode": "reasoning",
    }
    assert client.post(
        f"/api/notebooks/{notebook_id}/ask/stream", json=bypassed_preview
    ).status_code == 422
    assert client.post(
        f"/api/notebooks/{notebook_id}/ask", json=bypassed_preview
    ).status_code == 422
    assert client.get(
        f"/api/notebooks/{notebook_id}/conversations"
    ).json() == []

    invalid_payload = {
        "question": "RTL到GDSII流程",
        "mode": "reasoning",
        "intent": {
            "contract": {**contract, "objective": "另一个问题"},
            "resolved_question": contract["resolved_question"],
            "answers": [],
        },
    }
    assert client.post(
        f"/api/notebooks/{notebook_id}/ask/stream", json=invalid_payload
    ).status_code == 422
    assert client.post(
        f"/api/notebooks/{notebook_id}/ask", json=invalid_payload
    ).status_code == 422
    # Semantic confirmation failures happen before either durable entry exists.
    assert client.get(
        f"/api/notebooks/{notebook_id}/conversations"
    ).json() == []

    response = client.post(
        f"/api/notebooks/{notebook_id}/ask/stream",
        json={
            "question": "RTL到GDSII流程",
            "mode": "reasoning",
            "intent": {
                "contract": contract,
                "resolved_question": contract["resolved_question"],
                "answers": [],
            },
        },
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
    assert any(event.get("step", {}).get("step_type") == "intent" for event in events)
    assert any(event.get("step", {}).get("step_type") == "plan" for event in events)
    assert events[-1]["response"]["conversation_id"]
    assert events[0]["conversation_id"] == events[-1]["response"]["conversation_id"]
    assert events[-1]["response"]["reasoning_trace"]
    assert events[-1]["response"]["intent"]["confirmed"] is True
    assert events[-1]["response"]["retrieval_query"].startswith("RTL到GDSII流程")
    assert "RTL 到 GDSII 的完整实现流程是什么？" in (
        events[-1]["response"]["retrieval_query"]
    )
    plan = next(
        event["step"] for event in events
        if event.get("step", {}).get("step_type") == "plan"
    )
    assert plan["detail"]["source"] == "confirmed_intent"
    planned_queries = [row["query"] for row in plan["detail"]["sub_queries"]]
    assert len(planned_queries) == 3
    assert planned_queries[0].startswith("RTL到GDSII流程")
    assert planned_queries[1].startswith("RTL 到 GDSII 实现流程")
    assert planned_queries[2].startswith("RTL 到 GDSII 签核阶段")
    assert all("RTL 到 GDSII 的完整实现流程是什么？" in row for row in planned_queries)

    # Conversation context for the next corpus-blind preview includes only
    # prior user wording, never the corpus-derived assistant answer.
    second_preview = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={
            "question": "这个流程有哪些签核点？",
            "conversation_id": events[0]["conversation_id"],
        },
    )
    assert second_preview.status_code == 200
    assert "User: RTL到GDSII流程" in llm.intent_prompts[-1]
    assert "RTL到GDSII流程 [k1]" not in llm.intent_prompts[-1]
