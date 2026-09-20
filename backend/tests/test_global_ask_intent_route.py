"""D1-5: 全局问答的意图预检——HTTP 端点 + 服务层的范围/鉴权/只读契约。

两层用例分工:
* HTTP 层(``TestClient`` 起真实 app)钉住路由接线本身——请求体校验、空问题
  422、stream 版心跳传输能把最终合同送到——这些是只有真正过一遍 FastAPI 才能
  发现的接线问题。
* 服务层(复用 ``test_global_ask.py`` 的 ``setup``/``_EngineDouble``)钉住
  ``GlobalAskService.preview_intent`` 自己的契约:范围解析与鉴权复检跟
  ``start()`` 同一条路径、预检期间参与集覆盖已经装好且等于解析出的库、以及
  全程不落一次库写。这些用精确摆布 ``can_read``/store 桩子最简单,过真实 app
  反而绕远。
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.models.ask import QueryIntentContract
from app.models.global_ask import GlobalAskIntentPreviewRequest, GlobalAskRequest
from app.services.global_ask import GlobalAskError
from app.services.retrieval_participants import (
    current_participant_override, federated_ask_active,
)
from tests.test_global_ask import setup, finished  # noqa: F401 -- shared fixture


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
    from fastapi.testclient import TestClient

    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    return TestClient(create_app())


# -- HTTP 层:路由接线 -------------------------------------------------------

def test_intent_endpoint_returns_contract(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/notebooks", json={"name": "nb"})
    response = client.post("/api/global-ask/intent", json={"question": "分析一下这批资料"})
    assert response.status_code == 200
    body = response.json()
    assert body["resolved_question"]
    assert body["needs_clarification"] is False
    # 预检不建会话/任务。
    assert client.get("/api/global-ask/conversations").json() == []


def test_intent_endpoint_rejects_empty_question(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/global-ask/intent", json={"question": "   "})
    assert response.status_code == 422


def test_intent_endpoint_stream_emits_final_contract(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    client.post("/api/notebooks", json={"name": "nb"})
    response = client.post(
        "/api/global-ask/intent/stream", json={"question": "分析一下这批资料"},
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[0] == {"event": "started", "stage": "global_ask_intent", "elapsed_ms": 0}
    assert events[-1]["event"] == "final"
    assert events[-1]["result"]["resolved_question"]
    assert client.get("/api/global-ask/conversations").json() == []


# -- 服务层:范围/鉴权/只读契约 ------------------------------------------------

def test_intent_preview_rechecks_authority_the_same_way_start_does(setup):
    service, readable, _, _ = setup
    scope = {"mode": "include", "notebook_ids": ["a", "no-access"]}
    with pytest.raises(GlobalAskError) as start_error:
        service.start(GlobalAskRequest(question="q", notebook_scope=scope), user_id="u")
    with pytest.raises(GlobalAskError) as preview_error:
        service.preview_intent(
            GlobalAskIntentPreviewRequest(question="q", notebook_scope=scope), user_id="u",
        )
    assert preview_error.value.status_code == start_error.value.status_code == 404
    assert preview_error.value.message == start_error.value.message


def test_intent_preview_rejects_foreign_conversation(setup):
    service, _, _, _ = setup
    owned = finished(service, service.start(GlobalAskRequest(question="first"), user_id="u"))
    with pytest.raises(GlobalAskError) as error:
        service.preview_intent(
            GlobalAskIntentPreviewRequest(
                question="second", conversation_id=owned.conversation_id,
            ),
            user_id="somebody-else",
        )
    assert error.value.status_code == 404


def test_intent_preview_writes_nothing(setup):
    service, _, _, _ = setup

    def fail(*args, **kwargs):
        pytest.fail("intent preview must not write any durable row")

    service.store.create = fail
    service.store.save = fail
    service.store.save_progress = fail
    service.store.rename = fail
    service.store.delete = fail

    contract = service.preview_intent(
        GlobalAskIntentPreviewRequest(question="compare these notebooks"), user_id="u",
    )
    assert isinstance(contract, QueryIntentContract)
    assert service.list_conversations(user_id="u") == []


def test_intent_preview_runs_under_the_same_participant_set(setup):
    service, readable, _, _ = setup
    readable.add("c")
    seen: dict = {}

    def probe(notebook_id, question, history="", *, cancel_event=None):
        seen["active"] = federated_ask_active()
        override = current_participant_override()
        seen["ids"] = set(override.notebook_ids) if override else set()
        seen["nominal_active"] = notebook_id
        return QueryIntentContract(objective=question, resolved_question=question)

    service.ask.preview_reasoning_intent = probe
    service.preview_intent(
        GlobalAskIntentPreviewRequest(question="compare"), user_id="u",
    )
    assert seen["active"] is True
    assert seen["ids"] == readable
    assert seen["nominal_active"] in readable
    # The context must not leak past the call.
    assert federated_ask_active() is False
