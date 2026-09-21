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


def _frames(response):
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_a_scope_failure_keeps_its_status_and_sentence_on_both_transports(tmp_path, monkeypatch):
    """范围/鉴权拒绝:两条传输给出同一个状态码与同一句中文,只是载体不同。

    阻塞端点 ``/intent`` 仍是真实的 HTTP 状态码。流式端点现在**先开流**——范围解析
    (两次鉴权复核 + 冻结至多 8 个库的可见来源清单,大库上是秒级)放进了心跳覆盖的那段
    工作里,否则这段时间响应一个字节都没发出去,代理/网关的首字节超时会把连接掐掉,
    浏览器只能报「问题理解没能完成」。拒绝因此经 ``error`` 帧带出:``status`` +
    ``message`` 就是 ``user_error`` 的状态码与整句,客户端照样分得清「这段对话不是你的」
    和「模型服务挂了」。
    """
    client = _client(tmp_path, monkeypatch)
    client.post("/api/notebooks", json={"name": "nb"})
    body = {"question": "分析一下这批资料", "conversation_id": "gconv-foreign"}

    blocking = client.post("/api/global-ask/intent", json=body)
    assert blocking.status_code == 404
    assert blocking.headers.get("X-User-Message") == "1"
    assert "对话不存在" in blocking.text

    streamed = client.post("/api/global-ask/intent/stream", json=body)
    assert streamed.status_code == 200
    frames = _frames(streamed)
    assert frames[0]["event"] == "started"
    assert frames[-1]["event"] == "error"
    assert frames[-1]["status"] == 404
    assert "对话不存在" in frames[-1]["message"]
    assert frames[-1]["error"] == "global_ask_intent_failed"
    assert all(frame["event"] != "final" for frame in frames)


def test_an_empty_scope_is_refused_inside_the_stream_with_its_status(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post(
        "/api/global-ask/intent/stream", json={"question": "分析一下这批资料"},
    )
    assert response.status_code == 200
    last = _frames(response)[-1]
    assert last["event"] == "error" and last["status"] == 422
    assert "没有可访问的笔记本" in last["message"]


def test_the_stream_opens_before_scope_resolution_finishes(monkeypatch):
    """The first byte must not wait for scope resolution.

    Driven at the ASGI-body level, not through ``TestClient`` (which buffers the
    whole body): ``started`` has to come out of the iterator WHILE
    ``prepare_intent_preview`` is still parked.
    """
    import asyncio
    import threading
    from types import SimpleNamespace

    from app.api import global_ask_routes
    from app.models.global_ask import GlobalAskIntentPreviewRequest as Payload

    preparing, release = threading.Event(), threading.Event()
    contract = QueryIntentContract(
        objective="q", resolved_question="q", intent_type="explain",
    )

    class Service:
        def prepare_intent_preview(self, payload, *, user_id):
            preparing.set()
            assert release.wait(5)
            return "prepared"

        def run_intent_preview(self, prepared, *, cancel_event=None):
            assert prepared == "prepared"
            return contract

    class Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(global_ask_routes, "global_ask_service", lambda: Service())

    async def run():
        response = await global_ask_routes.preview_global_ask_intent_stream(
            Payload(question="q"), Request(), SimpleNamespace(id="u"),
        )
        body = response.body_iterator
        first = json.loads(await asyncio.wait_for(body.__anext__(), 5))
        assert first["event"] == "started"
        # Scope resolution is running and has NOT finished: the header and the
        # first frame are already out.
        assert await asyncio.to_thread(preparing.wait, 5)
        assert not release.is_set()
        release.set()
        rest = [json.loads(line) async for line in body if line.strip()]
        return rest

    rest = asyncio.run(run())
    assert rest[-1]["event"] == "final"
    assert rest[-1]["result"]["resolved_question"] == "q"


def test_an_in_stream_failure_is_logged_by_class_name_only(caplog):
    """The error frame is content-free by design and the request log records a
    200, so without a log line an in-stream failure leaves no trace at all --
    and the line must not carry the exception text."""
    import asyncio
    import logging

    from app.api.task_stream import task_event_stream

    secret = "SELECT text FROM chunks /private/path 机密原文"

    def work():
        raise RuntimeError(secret)

    class Request:
        async def is_disconnected(self):
            return False

    async def run():
        return [json.loads(line) async for line in task_event_stream(
            Request(), work, stage="global_ask_intent", error_code="global_ask_intent_failed",
        )]

    with caplog.at_level(logging.INFO, logger="silicon_notebook.task_stream"):
        frames = asyncio.run(run())
    assert frames[-1] == {"event": "error", "stage": "global_ask_intent", "error": "global_ask_intent_failed"}
    messages = [record.getMessage() for record in caplog.records]
    assert any("RuntimeError" in message and "global_ask_intent" in message for message in messages)
    assert all(secret not in message for message in messages)
    assert all(record.exc_info is None for record in caplog.records)


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
