from fastapi.testclient import TestClient
import pytest

from tests.ask_testkit import seed_ask_evidence


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


def test_ask_on_empty_notebook_is_rejected_409(tmp_path, monkeypatch):
    """PR#334 硬约束权威闸门:无任何可检索证据的空库,/ask 与 /ask/stream 都以 409
    拒绝(带 X-User-Message 用户文案),不产生凭空回答;塞一条证据后放行。"""
    from app.api.ask_routes import repository
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": "chunk"})
    assert r.status_code == 409
    assert r.headers.get("X-User-Message") == "1"
    assert "来源" in r.json()["detail"]

    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "chunk"})
    assert rs.status_code == 409
    assert rs.headers.get("X-User-Message") == "1"

    # 未知模式(422)先于可用性(409)判定,即便空库也应报模式错。
    bad = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"})
    assert bad.status_code == 422

    seed_ask_evidence(repository(), nb)
    ok = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "chunk"})
    assert ok.status_code == 200


def test_chunk_mode_streams_start_then_final(tmp_path, monkeypatch):
    import json
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    # WS2a: 首事件现为 started(带 job_id,供前端「停止」调 cancel 端点),
    # 随后才是 progress/start。
    assert kinds[0] == "started" and events[0]["job_id"]
    assert events[0]["conversation_id"] == events[-1]["response"]["conversation_id"]
    assert kinds[1] == "progress" and events[1]["step"]["step_type"] == "start"
    assert events[1]["step"]["detail"]["mode"] == "chunk"
    assert kinds[-1] == "final"
    assert "reasoning_trace" not in events[-1]["response"] or \
        not events[-1]["response"]["reasoning_trace"]


@pytest.mark.parametrize("selected", ["chunk", "reasoning"])
def test_auto_mode_is_resolved_by_backend_before_durable_job(
    tmp_path, monkeypatch, selected
):
    import json
    from app.api.ask_routes import repository
    from app.models.ask import QueryIntentContract
    from app.models.schemas import AskResponse

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    seed_ask_evidence(repo, nb)
    routed = []
    def preview_auto_intent(
        notebook_id, question, history="", cancel_event=None
    ):
        routed.append((notebook_id, question, history))
        return QueryIntentContract(
            objective=question,
            resolved_question=question,
            intent_type="compare" if selected == "reasoning" else "explain",
        )

    monkeypatch.setattr(repo, "preview_reasoning_intent", preview_auto_intent)
    service = repo._runtime.ask_service()
    seen = {}

    def fake_ask(notebook_id, payload, **kwargs):
        seen["mode"] = payload.mode
        seen["retrieval_effort"] = payload.retrieval_effort
        return AskResponse(
            conclusion="routed",
            conversation_id=payload.conversation_id or "",
            mode=payload.mode,
        )

    monkeypatch.setattr(service, "ask", fake_ask, raising=False)
    response = client.post(
        f"/api/notebooks/{nb}/ask/stream",
        json={
            "question": "比较两个方案的取舍",
            "mode": "auto",
            "retrieval_effort": "exhaustive",
        },
    )

    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert routed == [(nb, "比较两个方案的取舍", "")]
    assert seen["mode"] == selected
    assert seen["retrieval_effort"] == "standard"
    assert events[1]["step"]["detail"]["mode"] == selected
    assert events[-1]["response"]["mode"] == selected


def test_auto_mode_stream_close_cancels_inflight_classifier(monkeypatch):
    import asyncio
    import threading

    from app.api import ask_routes
    from app.models.ask import AskRequest
    from app.services.cancellation import AskCancelled

    started = threading.Event()
    stopped = threading.Event()
    seen = {}

    class _Repo:
        def preview_reasoning_intent(
            self, notebook_id, question, history="", cancel_event=None
        ):
            seen["cancel_event"] = cancel_event
            started.set()
            cancel_event.wait(timeout=1)
            stopped.set()
            raise AskCancelled()

    class _Request:
        async def is_disconnected(self):
            return False

    monkeypatch.setattr(ask_routes, "ASK_STREAM_HEARTBEAT_SECONDS", 0.0)

    async def run():
        stream = ask_routes._stream_auto_ask_events(
            _Repo(),
            "notebook-a",
            AskRequest(question="q", mode="auto"),
            "",
            _Request(),
            scope_receipt=None,
        )
        await anext(stream)
        assert started.is_set()
        await stream.aclose()

    asyncio.run(run())
    assert seen["cancel_event"].is_set()
    assert stopped.wait(timeout=1)


def test_ask_stream_runs_through_the_runtime_ask_service(tmp_path, monkeypatch):
    """Task 24: 流式端点经 AskExecutionCoordinator 调 runtime-owned AskService
    (不再是 facade 回调)—— stub 掉服务的 ask 即可拦到整条流的 final 响应。"""
    import json
    from app.api.ask_routes import repository
    from app.models.schemas import AskResponse

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    seed_ask_evidence(repo, nb)  # PR#334:空库 ask 会 409,先塞一条证据
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


def test_ask_refuses_an_over_length_question(tmp_path, monkeypatch):
    """提问必须在**提交**这一刻就有界。

    问答会话公开分享页把每轮 `question` **原样**发给匿名访客(截断用户自撰 artifact
    而不披露违反「用户编辑的数据不得静默截断」,那正是 codex #522 R1 拿掉旧 2,000
    公开上限的理由)——所以「原样返回」只有在写入侧拒收超长问题时才是有界的。这是
    codex #525 R1 P2 对报告侧提的同一条,平移到 Ask 的三个入口。

    与前端 `ASK_INPUT_LIMITS.questionMaxChars` 是同一条护栏的两半。
    """
    from app.models.ask import ASK_QUESTION_MAX_CHARS

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    over = "问" * (ASK_QUESTION_MAX_CHARS + 1)
    at_cap = "问" * ASK_QUESTION_MAX_CHARS

    for path in (f"/api/notebooks/{nb}/ask", f"/api/notebooks/{nb}/ask/stream"):
        r = client.post(path, json={"question": over, "mode": "chunk"})
        assert r.status_code == 422, (path, r.status_code)
    # 意图预检本来就有这条闸;一并钉住,免得三个入口日后分叉——预检 422 而执行放行,
    # 等于逐步推理在浏览器里被拦、同一个问题却能从别处提交进来。
    r = client.post(f"/api/notebooks/{nb}/ask/intent", json={"question": over})
    assert r.status_code == 422

    # 拒绝,不是裁短了存:库里不能留下一份被悄悄截过的问题。超限在 pydantic 校验期
    # 就被挡下,连会话容器都不该建出来。
    assert client.get(f"/api/notebooks/{nb}/conversations").json() == []

    # 恰好等于上限**不是** 422——空转保护:一个恒 422 的实现过不了这一段。
    # 这里的 409 来自空库可用性闸(证据为零),它排在 body 校验之后,所以「不是 422」
    # 恰好证明问题本身已经通过校验。
    for path in (f"/api/notebooks/{nb}/ask", f"/api/notebooks/{nb}/ask/stream"):
        ok = client.post(path, json={"question": at_cap, "mode": "chunk"})
        assert ok.status_code == 409, (path, ok.status_code, ok.text)
