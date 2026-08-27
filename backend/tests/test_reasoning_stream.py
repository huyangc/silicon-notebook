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
    streamed_preview = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent/stream",
        json={"question": "RTL到GDSII流程"},
    )
    assert streamed_preview.status_code == 200
    preview_events = [
        json.loads(line)
        for line in streamed_preview.text.splitlines()
        if line.strip()
    ]
    assert preview_events[0] == {
        "event": "started",
        "stage": "ask_intent",
        "elapsed_ms": 0,
    }
    assert preview_events[-1]["event"] == "final"
    assert preview_events[-1]["result"]["resolved_question"].startswith(
        "RTL 到 GDSII"
    )
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


def test_reasoning_trace_covers_understanding_memory_and_synthesis(tmp_path, monkeypatch):
    """轨迹要覆盖此前不可见的三段:问题理解的耗时(它跑在持久 job 之前,只能由
    客户端量)、私有记忆是否参与作答、以及答案合成本身 —— 合成往往是整轮里最长
    的一段,却曾经既不上屏也不计入总耗时。"""
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
    from app.models.memory import MemoryHit
    from app.services.memory_retrieval import MemoryRetriever

    get_settings.cache_clear()
    ask_routes.repository.cache_clear()

    monkeypatch.setattr(
        MemoryRetriever,
        "notebook_memory_hits",
        lambda self, user_id, notebook_id, query, limit=8: [
            MemoryHit(memory_id="m1", title="签核偏好", text="先跑静态时序",
                      status="confirmed", authority=3, score=0.42),
        ],
    )

    client = TestClient(create_app())
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

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

    contract = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": "RTL到GDSII流程"},
    ).json()

    response = client.post(
        f"/api/notebooks/{notebook_id}/ask/stream",
        json={
            "question": "RTL到GDSII流程",
            "mode": "reasoning",
            "intent": {
                "contract": contract,
                "resolved_question": contract["resolved_question"],
                "answers": [],
                "understanding_ms": 1500,
            },
        },
    )
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    steps = [event["step"] for event in events if event["event"] == "progress"]
    kinds = [step["step_type"] for step in steps]

    # 理解必须先于记忆检索上屏:记忆要跑一次 embedding + 向量扫描,在它之前轨迹
    # 若还是空的,用户就得对着「等待后端事件…」等一段已经在跑的活。
    assert kinds[:3] == ["start", "intent", "memory"], kinds
    assert steps[1]["duration_ms"] == 1500        # 客户端量到的理解耗时被采信
    assert steps[2]["detail"]["count"] == 1
    # 记忆那一步是「embedding + 向量扫描」在轨迹里的唯一交代;不计时等于把这段
    # 从「覆盖整轮」的总耗时里悄悄漏掉。
    assert isinstance(steps[2]["duration_ms"], int)
    # 只声称召回,不声称被答案采纳:合成未必发生(模型未配/离线确定性模式),
    # 说「参考了 N 条」就是假账(codex 第 4 轮 P2)。
    assert steps[2]["summary"] == "找到 1 条相关记忆"

    synthesis = steps[-1]
    assert synthesis["step_type"] == "synthesis", kinds
    assert isinstance(synthesis["duration_ms"], int)
    # 引用数取模型真正绑上的 anchors,不取 citations —— 后者是「每条检索到的证据
    # 一张卡」,零绑定的回答上会写成「引用 10 处证据」(codex 第 7 轮 P2)。
    assert synthesis["detail"]["anchors"] == len(
        events[-1]["response"]["anchors"]
    )
    assert synthesis["summary"] == (
        f"已生成答案，引用 {synthesis['detail']['anchors']} 处证据"
    )

    # 重开会话回放的是持久化轨迹,这三段同样要在里面。
    persisted = [row["step_type"] for row in events[-1]["response"]["reasoning_trace"]]
    assert persisted[0] == "intent"
    assert "memory" in persisted
    assert persisted[-1] == "synthesis"


def test_memory_step_reports_recall_not_attribution(tmp_path, monkeypatch):
    """反向护栏:记忆步记的是「引擎召回了什么」,不是「答案归因了什么」。

    codex 第 7 轮 P2 建议改成「只在记忆真被模型绑成锚点时才记」,已驳回:
    1. 那会把这一步推到答案合成之后,而它携带的正是召回本身的耗时,实时轨迹里
       就不再出现在事情真正发生的位置;
    2. 记忆进了 prompt 却没被引用是常态,按锚点过滤会变成漏报 —— 轨迹会说没查过
       记忆,而实际上查了、也喂给了模型。
    归因由答案里的 [k] 引用承担。这条用例钉住这个取舍:记忆确实被召回、但答案
    一个记忆锚点都没绑时,记忆步照样在,且措辞不得声称被采用。"""
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
    from app.models.memory import MemoryHit
    from app.services.memory_retrieval import MemoryRetriever

    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    monkeypatch.setattr(
        MemoryRetriever, "notebook_memory_hits",
        lambda self, user_id, notebook_id, query, limit=8: [
            MemoryHit(memory_id="m1", title="签核偏好", text="先跑静态时序",
                      status="confirmed", authority=3, score=0.42),
        ],
    )

    client = TestClient(create_app())
    notebook_id = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

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

    contract = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": "RTL到GDSII流程"},
    ).json()
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
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    final = events[-1]["response"]
    memory = next(
        row for row in final["reasoning_trace"] if row["step_type"] == "memory"
    )
    # _ReasoningLLM 只吐 [k1],绑不到那条记忆 —— 记忆步仍须存在(它记的是召回)。
    assert not [
        anchor for anchor in final["anchors"] if anchor.get("object_type") == "memory"
    ]
    assert memory["detail"]["count"] == 1
    # 措辞不得声称被采用:这几个词一出现,记录就从「召回」滑成了「归因」。
    for claimed in ("参考", "采用", "使用", "用了", "依据"):
        assert claimed not in memory["summary"], memory["summary"]


def test_reasoning_trace_omits_memory_step_without_hits(tmp_path, monkeypatch):
    """没命中就不打「记忆」标签 —— 那个标签只在真的找到东西时出现。

    但那段耗时不能跟着一起消失:候选查询与 embedding 调用照样发生了,丢掉它
    「轨迹覆盖整轮」就不成立(codex 第 10 轮 P2)。所以改记一条 skip 步,沿用检索器
    记录跳过工作的同一套词汇。"""
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

    contract = client.post(
        f"/api/notebooks/{notebook_id}/ask/intent",
        json={"question": "RTL到GDSII流程"},
    ).json()
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
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    kinds = [event["step"]["step_type"] for event in events if event["event"] == "progress"]
    assert "memory" not in kinds
    assert kinds[:3] == ["start", "intent", "skip"]
    # 零命中那一步仍须带上真实耗时,否则「覆盖整轮」的总耗时会系统性偏低。
    skipped = [
        event["step"] for event in events
        if event["event"] == "progress" and event["step"]["step_type"] == "skip"
    ][0]
    assert skipped["summary"] == "未找到相关记忆"
    assert isinstance(skipped["duration_ms"], int)
    # 已经推送过的步骤必须出现在 final 里,否则 final 一替换在途 turn 就把用户
    # 刚看着走过的轨迹当场抹掉,历史里也留不下(codex 第 2 轮 P2)。
    assert [row["step_type"] for row in events[-1]["response"]["reasoning_trace"]][0] \
        == "intent"
    # 未回传 understanding_ms 时不能编一个 0 出来冒充「理解只花了 0ms」。
    intent = next(
        event["step"] for event in events
        if event["event"] == "progress" and event["step"]["step_type"] == "intent"
    )
    assert intent["duration_ms"] is None
