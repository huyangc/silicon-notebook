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
    assert [m["id"] for m in body] == ["chunk", "reasoning"]
    # 两个内置引擎都不再把知识图谱当硬前提(reasoning 无图时走原文段落检索),
    # 前端内置表的 requiresKg 由 scripts/check_ask_modes_contract.py 对账。
    assert {m["id"]: m["requires_kg"] for m in body} == {
        "chunk": False, "reasoning": False}


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


def test_auto_is_a_retired_alias_for_reasoning(tmp_path, monkeypatch):
    """`auto` 曾是简化界面的**请求级选择器**:后端先跑一次分类模型,再挑引擎。
    选择器已下线(简化界面直接提交 reasoning),但 `auto` 作为**退役别名**继续被
    接受并映射到 reasoning——尚未刷新的旧标签页不 422。

    退役别名不是引擎:它不出现在 /ask-modes,也不出现在 422 的 valid 列表里。
    这条路径上不再有任何路由模型调用:路由层的 `preview_reasoning_intent`
    (分类器唯一的入口)零次被调,而两个端点都以 reasoning 执行——同步 /ask 的
    响应、流的合成 start 步、以及持久化的 ask_jobs.mode 三处口径一致。
    """
    import json
    from app.api.ask_routes import repository

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    seed_ask_evidence(repo, nb)
    previewed = []
    original_preview = repo.preview_reasoning_intent

    def counted_preview(*args, **kwargs):
        previewed.append(args)
        return original_preview(*args, **kwargs)

    monkeypatch.setattr(repo, "preview_reasoning_intent", counted_preview)

    # 清晰问题、不带 intent:兼容旧客户端的确定性闸放行,引擎侧也不额外调模型。
    question = "RTL到GDSII流程"
    streamed = client.post(
        f"/api/notebooks/{nb}/ask/stream",
        json={"question": question, "mode": "auto"},
    )
    assert streamed.status_code == 200
    events = [json.loads(l) for l in streamed.text.splitlines() if l.strip()]
    assert events[0]["event"] == "started"
    assert events[1]["step"]["step_type"] == "start"
    assert events[1]["step"]["detail"]["mode"] == "reasoning"
    assert events[-1]["event"] == "final"
    assert events[-1]["response"]["mode"] == "reasoning"
    # 持久状态从建行那一刻起就是真正执行的引擎,没有事后改写。
    job = repo._runtime.ask_state.ask_job_status(events[0]["job_id"])
    assert job["mode"] == "reasoning"

    synchronous = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": question, "mode": "auto"},
    )
    assert synchronous.status_code == 200
    assert synchronous.json()["mode"] == "reasoning"

    assert previewed == []
    # 零调用断言只有在计数器真的挂在路由使用的那个 seam 上才有意义:走一次确实
    # 会调理解模型的端点,计数从 0 变 1,证明上面的零不是空断言。
    previewed_intent = client.post(
        f"/api/notebooks/{nb}/ask/intent", json={"question": question}
    )
    assert previewed_intent.status_code == 200
    assert len(previewed) == 1
    assert "auto" not in [m["id"] for m in client.get("/api/ask-modes").json()]
    bad = client.post(
        f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"}
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["valid"] == ["chunk", "reasoning"]


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


def test_ask_sync_and_stream_record_web_submission_channel(tmp_path, monkeypatch):
    """网页的两个提交面 -- 同步 /ask 与流式 /ask/stream -- 都必须把建出的
    ask_jobs 行记成 submitted_via == "web"，与 MCP ask_notebook 记的 "mcp"
    (见 test_memory_mcp.py) 和未记录的 "" 区分开。"""
    import json

    from app.api.ask_routes import repository

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    seed_ask_evidence(repo, nb)

    def _submitted_via(job_id: str) -> str:
        with repo._connect() as db:
            row = db.execute(
                "SELECT submitted_via FROM ask_jobs WHERE id=?", (job_id,)
            ).fetchone()
        return row["submitted_via"]

    def _ask_job_ids() -> set:
        with repo._connect() as db:
            return {row["id"] for row in db.execute("SELECT id FROM ask_jobs")}

    streamed = client.post(
        f"/api/notebooks/{nb}/ask/stream",
        json={"question": "网页流式提问", "mode": "chunk"},
    )
    assert streamed.status_code == 200
    events = [json.loads(l) for l in streamed.text.splitlines() if l.strip()]
    assert _submitted_via(events[0]["job_id"]) == "web"

    before = _ask_job_ids()
    synchronous = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": "网页同步提问", "mode": "chunk"},
    )
    assert synchronous.status_code == 200
    new_ids = _ask_job_ids() - before
    assert len(new_ids) == 1
    assert _submitted_via(next(iter(new_ids))) == "web"


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


def test_ask_reasoning_without_intent_gives_422_with_the_ambiguity_question(
    tmp_path, monkeypatch
):
    """直连 `/ask`(不带 `intent`,兼容旧客户端)遇确定性歧义时,422 的 `detail`
    带上具体的歧义问题,而不是裸的「问题仍有关键歧义，请先确认问题理解」。

    T3:`ask_routes._validate_confirmed_reasoning_intent` 的 `payload.intent is
    None` 分支改走 `clarification_gate_message(seed)`。用「分析一下」而非「分析
    一下这个」——后者含「这个」,会先撞 `_UNRESOLVED_REFERENCE` 而非
    `_GENERIC_REQUEST`,产出不同措辞的确定性行;这里选择真正触发「你希望分析的
    具体对象和最关心的问题是什么」这条文案的问句。
    """
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    r = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": "分析一下", "mode": "reasoning"},
    )
    assert r.status_code == 422
    assert "你希望分析的具体对象" in r.json()["detail"]


def test_engine_side_reasoning_gate_shares_the_clarification_copy():
    """引擎内的兼容后备闸(`AskService._confirmed_reasoning_intent`,`intent is None`
    分支)与路由层 422、MCP 前置闸共用同一份 `clarification_gate_message`。

    这条分支在 HTTP 与 MCP 两条正式入口上已被前置闸挡住、正常不可达,只对绕过入口
    直接调 `repo.ask` 的调用方生效——正因为没有端到端路径覆盖它,单独钉住文案,
    否则这一处漂回裸字面量「问题仍有关键歧义，请先确认问题理解」时全量测试仍然绿,
    三处文案就分家了。
    """
    from app.models.ask import AskRequest
    from app.services.ask_service import AskService

    payload = AskRequest(question="分析一下", mode="reasoning")
    with pytest.raises(ValueError) as excinfo:
        AskService._confirmed_reasoning_intent(payload, "")
    text = str(excinfo.value)
    assert text.startswith("问题仍有关键歧义，请先确认问题理解：")
    assert "你希望分析的具体对象和最关心的问题是什么" in text
