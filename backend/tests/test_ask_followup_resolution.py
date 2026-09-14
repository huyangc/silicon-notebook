"""PR-C (T3/T4): 无 intent 的 reasoning 请求在入口层解析多轮跟进句。

今天的合同是:直连 `/ask`(不走 `/ask/intent` 的兼容客户端、MCP 之外的调用方)
遇到确定性歧义就 422。这一层加的是**命中之后的一次挽救**——用本人本库同一会话
里自己问过的话跑一次改写,改写句仍不清才 422。四条红线:

1. 清晰问题零代价:不读会话历史、不调改写模型,逐字退化到今天;
2. 放行时检索用改写句,**落库与展示仍是原句**;
3. 任何回落(无会话/他人会话/改写未配置/改写后仍模糊)都回到今天那条 422,且
   文案与改写产物无关;
4. 改写结论靠 contextvar 传到引擎,丢失或张冠李戴时引擎按原句判。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.ask_testkit import seed_ask_evidence
from tests.model_testkit import bind_chat_client

# 原句独自过不了确定性闸(只有指代),改写句点名对象后可以过。两句都在
# tests/test_followup_gate.py 里被同一对判据钉过。
FOLLOWUP = "它的参数呢"
REWRITTEN = "set_db 的参数有哪些"
FIRST_TURN = "set_db 有哪些用法"


class FakeRewriteClient:
    """只服务 ``query_rewrite`` workload 的替身,按 schema_hint 认自己的调用。"""

    configured = True
    model = "fake-rewrite"

    def __init__(self, query: str = REWRITTEN):
        self.query = query
        self.calls: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        assert schema_hint == '{"query":""}', schema_hint
        self.calls.append(messages[0]["content"])
        return json.dumps({"query": self.query})


def _client(tmp_path, monkeypatch) -> TestClient:
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


def _history_spy(monkeypatch, repo) -> list[tuple]:
    """数 `conversation_user_history` 被读了几次(引擎与 facade 共用这一个 store)。"""
    store = repo._runtime.ask_state
    reads: list[tuple] = []
    original = store.conversation_user_history

    def counted(*args, **kwargs):
        reads.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "conversation_user_history", counted)
    return reads


def _setup(tmp_path, monkeypatch, *, rewrite=REWRITTEN, bind=True):
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository

    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)
    rewriter = FakeRewriteClient(rewrite)
    if bind:
        bind_chat_client(repo, "query_rewrite", rewriter)
    reads = _history_spy(monkeypatch, repo)
    return client, repo, nb, rewriter, reads


def _first_turn(client, nb: str) -> str:
    """一轮清晰提问,建出一个属于本人本库、带答案的会话。"""
    first = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": FIRST_TURN, "mode": "reasoning"},
    )
    assert first.status_code == 200, first.text
    return first.json()["conversation_id"]


def _intent_step(body: dict) -> dict:
    steps = [s for s in (body.get("reasoning_trace") or []) if s["step_type"] == "intent"]
    assert len(steps) == 1, body.get("reasoning_trace")
    return steps[0]


def test_a_clear_question_costs_no_rewrite_and_no_history_read(tmp_path, monkeypatch):
    """闸放行的问题走的还是今天那条路:零改写调用、零历史读。

    这是绝大多数请求的形态,也是这一层唯一不能变贵的地方。
    """
    client, _repo, nb, rewriter, reads = _setup(tmp_path, monkeypatch)

    response = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": FIRST_TURN, "mode": "reasoning"},
    )

    assert response.status_code == 200, response.text
    assert rewriter.calls == []
    assert reads == []
    assert response.json()["retrieval_query"].startswith(FIRST_TURN)
    # 改写没跑 → intent 轨迹步没有可报的时长(带 intent 的请求才报客户端计时)。
    assert _intent_step(response.json())["duration_ms"] is None


def test_a_resolvable_followup_runs_on_the_rewrite_but_records_the_original(
    tmp_path, monkeypatch
):
    """有本人历史的跟进句放行:检索用改写句,落库与展示仍是用户原话。"""
    client, _repo, nb, rewriter, reads = _setup(tmp_path, monkeypatch)
    conversation_id = _first_turn(client, nb)
    assert rewriter.calls == []  # 首轮清晰,不该已经调过

    response = client.post(
        f"/api/notebooks/{nb}/ask",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(rewriter.calls) == 1
    assert FOLLOWUP in rewriter.calls[0]
    assert f"User: {FIRST_TURN}" in rewriter.calls[0]  # 喂进去的是本人问句
    assert len(reads) == 1

    # 检索面换成改写句。
    assert body["retrieval_query"].startswith(REWRITTEN)
    assert body["intent"]["resolved_question"] == REWRITTEN
    # 用户看到/存下来的仍是原话。
    assert body["intent"]["objective"] == FOLLOWUP
    assert _intent_step(body)["detail"]["resolved_question"] == REWRITTEN
    # 改写真的跑了 → 这一步有服务端计时可报。
    assert _intent_step(body)["duration_ms"] is not None

    turns = client.get(f"/api/conversations/{conversation_id}").json()["turns"]
    assert [turn["question"] for turn in turns] == [FIRST_TURN, FOLLOWUP]


def test_a_followup_without_history_still_gets_todays_422(tmp_path, monkeypatch):
    """无 `conversation_id` / 会话不属于本人 → 与今天逐字相同的 422。

    `tests/test_ask_modes_api.py::
    test_ask_reasoning_without_intent_gives_422_with_the_ambiguity_question`
    钉住另一句文案;这里钉指代不清那一句,并确认没有改写调用被浪费掉。
    """
    from app.services.query_intent import (
        clarification_gate_message,
        plan_query_intent,
    )

    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=1)
    )
    client, repo, nb, rewriter, _reads = _setup(tmp_path, monkeypatch)
    conversation_id = _first_turn(client, nb)

    no_conversation = client.post(
        f"/api/notebooks/{nb}/ask",
        json={"question": FOLLOWUP, "mode": "reasoning"},
    )
    assert no_conversation.status_code == 422
    assert no_conversation.json()["detail"] == expected

    # 同一个会话,换一个人问 —— 归属谓词不认,历史读回空串,回到原 422。
    store = repo._runtime.ask_state
    original_history = store.conversation_user_history
    monkeypatch.setattr(
        store,
        "conversation_user_history",
        lambda notebook_id, cid, user_id, limit=5: original_history(
            notebook_id, cid, "somebody-else", limit
        ),
    )
    foreign = client.post(
        f"/api/notebooks/{nb}/ask",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )
    assert foreign.status_code == 422
    assert foreign.json()["detail"] == expected
    assert rewriter.calls == []  # 没历史就不该烧改写调用


def test_an_unconfigured_rewrite_client_falls_back_to_todays_422(
    tmp_path, monkeypatch
):
    """`query_rewrite` 没配 → `_rewrite_followup_query` 原样回原句 → 原 422。"""
    from app.services.query_intent import (
        clarification_gate_message,
        plan_query_intent,
    )

    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=1)
    )
    client, _repo, nb, _rewriter, reads = _setup(tmp_path, monkeypatch, bind=False)
    conversation_id = _first_turn(client, nb)

    response = client.post(
        f"/api/notebooks/{nb}/ask",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == expected
    assert len(reads) == 1  # 历史读过了,只是改写这一步无人接手


def test_a_still_ambiguous_rewrite_422s_without_leaking_the_rewrite(
    tmp_path, monkeypatch
):
    """改写句仍模糊 → 422,且 detail 里不含改写产物的任何片段。"""
    from app.services.query_intent import (
        clarification_gate_message,
        plan_query_intent,
    )

    vague_rewrite = "分析一下这个模块的实现"
    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=1)
    )
    client, _repo, nb, rewriter, _reads = _setup(
        tmp_path, monkeypatch, rewrite=vague_rewrite
    )
    conversation_id = _first_turn(client, nb)

    response = client.post(
        f"/api/notebooks/{nb}/ask",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == expected
    assert vague_rewrite not in detail
    assert "模块" not in detail
    assert len(rewriter.calls) == 1


def test_an_over_long_rewrite_422s_before_any_job_exists(tmp_path, monkeypatch):
    """改写句超过合同上限 → 入口就按「改写失败」回到原句 422,不建 job、不外泄。

    没有这条守卫时入口闸放行、`_prepare_reasoning_ask` 装配合同才抛
    `ValidationError`:同步路径成 500,流式路径把带 `input_value=<改写句>` 的异常
    文本写进 `ask_jobs.error` 与浏览器收到的 error 事件。
    """
    from app.services.query_intent import (
        RESOLVED_QUESTION_MAX_CHARS,
        clarification_gate_message,
        plan_query_intent,
    )

    over_long = REWRITTEN + "详" * RESOLVED_QUESTION_MAX_CHARS
    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=1)
    )
    client, repo, nb, rewriter, _reads = _setup(
        tmp_path, monkeypatch, rewrite=over_long
    )
    conversation_id = _first_turn(client, nb)

    def _jobs() -> int:
        with repo._write() as db:
            return db.execute("SELECT COUNT(*) AS n FROM ask_jobs").fetchone()["n"]

    jobs_before = _jobs()

    response = client.post(
        f"/api/notebooks/{nb}/ask",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == expected
    assert "详详详" not in detail and REWRITTEN not in detail
    assert len(rewriter.calls) == 1
    assert _jobs() == jobs_before  # 422 在 begin_durable_job 之前


def test_the_stream_entry_point_carries_the_same_resolution(tmp_path, monkeypatch):
    """流式入口与同步入口同形:同一次改写、同一个检索面、同一条 422 顺序。

    contextvar 必须跨 `asyncio.to_thread` + `background_jobs.submit` 的两层
    `copy_context()` 到达脱离连接的 worker——路由不进 context 时这条红。
    """
    client, _repo, nb, rewriter, _reads = _setup(tmp_path, monkeypatch)
    conversation_id = _first_turn(client, nb)

    streamed = client.post(
        f"/api/notebooks/{nb}/ask/stream",
        json={
            "question": FOLLOWUP,
            "mode": "reasoning",
            "conversation_id": conversation_id,
        },
    )

    assert streamed.status_code == 200
    events = [json.loads(line) for line in streamed.text.splitlines() if line.strip()]
    assert events[-1]["event"] == "final"
    body = events[-1]["response"]
    assert len(rewriter.calls) == 1
    assert body["retrieval_query"].startswith(REWRITTEN)
    assert body["intent"]["resolved_question"] == REWRITTEN
    assert body["intent"]["objective"] == FOLLOWUP

    # 422 仍先于可用性 409/其它前置:无会话的模糊跟进句在流式上也是 422。
    rejected = client.post(
        f"/api/notebooks/{nb}/ask/stream",
        json={"question": FOLLOWUP, "mode": "reasoning"},
    )
    assert rejected.status_code == 422


def test_the_engine_judges_the_original_when_no_resolution_is_carried(
    tmp_path, monkeypatch
):
    """绕过入口直接调 `repo.ask`(没进 context)→ 引擎按原句判,文案同今天。"""
    from app.models.schemas import AskRequest
    from app.services.query_intent import (
        clarification_gate_message,
        plan_query_intent,
    )

    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=4)
    )
    client, repo, nb, rewriter, _reads = _setup(tmp_path, monkeypatch)
    conversation_id = _first_turn(client, nb)

    with pytest.raises(ValueError) as caught:
        repo.ask(
            nb,
            AskRequest(
                question=FOLLOWUP,
                mode="reasoning",
                conversation_id=conversation_id,
            ),
        )

    assert str(caught.value) == expected
    assert rewriter.calls == []  # 引擎自己绝不跑改写


def test_a_resolution_for_another_question_is_ignored(tmp_path, monkeypatch):
    """身份不符的 resolution 当作没有 —— 不拿别的问题的改写去检索。"""
    from app.models.schemas import AskRequest
    from app.services.ask_followup import (
        FollowupResolution,
        followup_resolution_context,
    )
    from app.services.query_intent import (
        clarification_gate_message,
        plan_query_intent,
    )

    expected = clarification_gate_message(
        plan_query_intent(None, FOLLOWUP, "", max_topics=4)
    )
    client, repo, nb, _rewriter, _reads = _setup(tmp_path, monkeypatch)
    conversation_id = _first_turn(client, nb)
    mismatched = FollowupResolution(
        question="完全是另一个问题",
        resolved_question=REWRITTEN,
        rewrite_ms=7,
        gate_message="",
    )

    with followup_resolution_context(mismatched):
        with pytest.raises(ValueError) as caught:
            repo.ask(
                nb,
                AskRequest(
                    question=FOLLOWUP,
                    mode="reasoning",
                    conversation_id=conversation_id,
                ),
            )

    assert str(caught.value) == expected
