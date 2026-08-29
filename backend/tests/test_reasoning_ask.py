import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


class _SeqLLM:
    configured = True
    def __init__(self, plan, reflects, answer):
        self._plan, self._reflects, self._answer = plan, list(reflects), answer
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps(self._answer)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _bind_reasoning(repo, client):
    bind_chat_client(repo, "reasoning_agent", client)
    bind_chat_client(repo, "evidence_refine", client)
    bind_chat_client(repo, "ask_answer", client)


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
    ], [])
    return nb


def test_reasoning_ask_returns_trace_and_evidence_level(arepo):
    nb = _seed(arepo)
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "RTL到GDSII是标准流程 [k1].", "grounded": True}))
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace and resp.reasoning_trace[0].step_type == "intent"
    # 断言 plan 在 intent 之后出现,而不是紧邻:两者之间还有 Memory 召回那一步
    # (命中记 memory,零命中记带耗时的 skip)。「紧邻」是偶然相邻,不是合同。
    kinds = [step.step_type for step in resp.reasoning_trace]
    assert "plan" in kinds and kinds.index("plan") > kinds.index("intent")
    assert ("chat", "reasoning_agent") in arepo._runtime.models.calls
    assert ("chat", "evidence_refine") in arepo._runtime.models.calls
    assert ("chat", "ask_answer") in arepo._runtime.models.calls
    assert resp.evidence_level in {"grounded", "overview", "inferred"}
    assert resp.conversation_id


def test_reasoning_ask_requests_all_mandatory_topic_seeds_beyond_first_round_width(
    arepo, monkeypatch
):
    """P1-4b 修复项 2: ask_service 的使能行
    `max(limits.max_initial_subqueries, 1 + len(mandatory_topics))` 必须真正传导到
    `ReasoningRetriever.run` 收到的 `intent_queries`,而不是在请求阶段就被砍回首轮
    并发宽度 —— 砍回首轮宽度会让补种(coverage pass)没有源头方向可补,退化成
    "首轮之外的已确认主题永远补不上"。overview 档首轮宽度只有 2,3 个必答主题
    应让请求长度到 4(1 权威问题 + 3 个主题),而不是被夹到 2。"""
    from app.models.ask import AskIntentConfirmation, QueryIntentContract, QueryIntentTopic
    from app.services.reasoning_retrieval import ReasoningRetriever, ReasoningResult

    nb = _seed(arepo)
    question = "RTL 到 GDSII 的完整实现流程有哪些阶段？"
    contract = QueryIntentContract(
        objective=question,
        resolved_question=question,
        mandatory_topics=[
            QueryIntentTopic(id="t1", title="综合", question="RTL 综合阶段的关键步骤"),
            QueryIntentTopic(id="t2", title="布局布线", question="布局布线阶段的关键指标"),
            QueryIntentTopic(id="t3", title="签核", question="签核阶段涉及的检查项"),
        ],
        needs_clarification=False,
        confirmed=True,
    )
    captured: dict = {}

    def _spy_run(self, notebook_id, question, history="", on_step=None, **kwargs):
        captured["intent_queries"] = kwargs.get("intent_queries")
        return ReasoningResult()

    monkeypatch.setattr(ReasoningRetriever, "run", _spy_run)

    arepo.ask(nb.id, AskRequest(
        question=question, mode="reasoning", retrieval_effort="overview",
        intent=AskIntentConfirmation(
            contract=contract, resolved_question=question, answers=[]),
    ))

    seeded = captured.get("intent_queries")
    assert seeded is not None, "ReasoningRetriever.run 必须收到 intent_queries"
    # 首轮宽度(overview=2)只约束首轮并发,不该反过来把请求本身砍回 2。
    assert len(seeded) == 4


def test_reasoning_uses_distinct_planning_refinement_and_answer_workloads(arepo):
    class _Planner:
        configured = True

        def __init__(self):
            self.calls = 0

        def chat_json(self, messages, schema_hint, **kwargs):
            self.calls += 1
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            assert "next_action" in schema_hint
            return json.dumps({"next_action": "answer", "sufficient": True})

    class _Refiner:
        configured = True

        def __init__(self):
            self.calls = 0

        def chat_json(self, messages, schema_hint, **kwargs):
            assert '"relevant"' in schema_hint
            self.calls += 1
            return json.dumps({"relevant": ["RTL到GDSII是标准实现流程"]})

    class _Answer:
        configured = True

        def __init__(self):
            self.calls = 0

        def chat_json(self, messages, schema_hint, **kwargs):
            assert '"answer"' in schema_hint
            self.calls += 1
            return json.dumps({
                "answer": "RTL到GDSII是标准实现流程 [k1].",
                "grounded": True,
            })

    planner, refiner, answer = _Planner(), _Refiner(), _Answer()
    bind_chat_client(arepo, "reasoning_agent", planner)
    bind_chat_client(arepo, "evidence_refine", refiner)
    bind_chat_client(arepo, "ask_answer", answer)
    arepo.settings.kg_query_refine_enabled = True
    nb = _seed(arepo)

    response = arepo.ask(
        nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning")
    )

    assert response.answer
    assert planner.calls >= 2
    assert refiner.calls == 1
    assert answer.calls == 1


def test_fast_mode_unaffected_and_no_trace(arepo):
    nb = _seed(arepo)
    _bind_reasoning(arepo, _SeqLLM(plan={}, reflects=[],
                                   answer={"answer": "x", "grounded": False}))
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程"))  # 默认 fast
    assert resp.reasoning_trace is None


def test_reasoning_degrades_gracefully_on_llm_error(arepo):
    nb = _seed(arepo)
    class _BoomLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kwargs):
            raise RuntimeError("boom")
    _bind_reasoning(arepo, _BoomLLM())
    # LLM 全程抛错: run 内 plan/reflect 各自容错降级,answer 合成失败被吞,
    # 但检索不依赖 LLM,故仍返回合法 AskResponse(轨迹与候选仍在)。
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    assert resp.reasoning_trace is not None        # 检索轨迹仍构建
    assert resp.answer == ""                        # 答案合成失败 → 空
    assert resp.evidence_level == "inferred"        # 无 anchor → 推断档
    assert resp.conversation_id


def _seed_graph(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "depends_on", "evidence": []},
    ])
    return nb


def test_reasoning_expand_then_answer_end_to_end(arepo):
    nb = _seed_graph(arepo)
    claim = next((h for h in arepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                  if h.object_type == "claim"), None)
    assert claim is not None
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "流程概述 [k1],其布线步骤见 [k2].", "grounded": True}))
    resp = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    step_kinds = [t.step_type for t in resp.reasoning_trace]
    assert "expand" in step_kinds
    assert step_kinds.index("expand") < step_kinds.index("answer")  # 深挖发生在合成之前
    # 深挖到的 procedure 进入了 related_knowledge
    assert any(r.object_type == "procedure" for r in resp.related_knowledge)


def test_reasoning_search_elements_fallback_path(arepo):
    nb = _seed_graph(arepo)
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "完全不相关的冷门问题zzz"}]},
        reflects=[
            {"next_action": "search_elements", "elements_query": "zzz"},
            {"next_action": "answer", "sufficient": True}],
        answer={"answer": "知识库未覆盖,以下为推断。", "grounded": False}))
    resp = arepo.ask(nb.id, AskRequest(question="冷门zzz", mode="reasoning"))
    assert any(t.step_type == "fallback" for t in resp.reasoning_trace)
    # inferred 的根因: LLM 答案无 [k] 标记 → anchors 为空 → classify_evidence 落入 inferred
    # (无 anchor 时既不能 grounded 也不能 overview,与 KG 候选相关度高低无关)
    assert not resp.anchors
    assert resp.evidence_level == "inferred"


def test_reasoning_conversation_persists(arepo):
    nb = _seed_graph(arepo)
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True}))
    t1 = arepo.ask(nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"))
    detail = arepo.get_conversation(t1.conversation_id)
    assert detail.turn_count == 1
    # 存回的轮次能反序列化(reasoning_trace 不破坏 AskResponse 往返)
    assert detail.turns[0].response.evidence_level == t1.evidence_level


def test_reasoning_service_streams_trace_with_explicit_user_id(arepo):
    """Task 24: reasoning 引擎在 AskService 上以显式 keyword-only user_id 运行,
    on_trace 逐步上流、答案照存 —— 与 facade 路径同一引擎、同一语义。"""
    nb = _seed(arepo)
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "服务级答案 [k1].", "grounded": True}))
    service = arepo._runtime.ask_service()
    steps = []
    resp = service.ask_reasoning(
        nb.id, AskRequest(question="RTL到GDSII流程", mode="reasoning"),
        user_id=arepo.current_user().id, on_trace=steps.append)
    assert steps and steps[0].step_type == "intent"     # 意图确认先于任何检索
    # on_trace 逐步可见。同上:plan 在 intent 之后,中间隔着 Memory 召回那一步。
    streamed = [step.step_type for step in steps]
    assert "plan" in streamed and streamed.index("plan") > streamed.index("intent")
    assert resp.mode == "reasoning" and resp.answer_id  # 引擎收口照存答案
    assert arepo.get_conversation(resp.conversation_id).turn_count == 1


def test_reasoning_zero_evidence_uses_the_shared_no_evidence_message(arepo, monkeypatch):
    """检索阶段真的一无所获(不是「这一条通道零命中」,而是 top_hits/elements/
    chunks/chains/memory_hits/structured_batch/enumerations/collection_map 全部
    为空)时,`_draft_reasoning_response` 的合成 `elif answer_client.configured
    and (top_hits or elements or chunks or chains or memory_hits or
    structured_batch is not None or enumerations or collection_map_prompt_block):`
    整段短路跳过,落到与 chunk 共用的 `_NO_RETRIEVAL_EVIDENCE_MESSAGE` 确定性
    文案——已退役的 graph 问答引擎删除前,它自己的镜像分支(「not top_hits and
    not seed_ids」早退)是仓库里唯一间接练习到这条消息的用例,该引擎删除后这条
    reasoning 分支全仓零测试覆盖。

    直接 monkeypatch `ReasoningRetriever.run_stage`(检索阶段唯一的类型化产出口,
    见 `execute_reasoning_retrieval_stage`)返回全空快照,比手工拼出
    `PreparedReasoningAsk`/`ResponseDraftInput` 这两层内部冻结 dataclass 更可靠:
    `_run_reasoning_stage` 其余的装配(intent 理解、Memory 召回、结构化批等)仍
    走生产代码路径,只有检索这一段被替换成确定性的「什么都没找到」。
    """
    from app.application.ask_reasoning import ReasoningEvidenceSnapshot
    from app.services.ask_service import _NO_RETRIEVAL_EVIDENCE_MESSAGE
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed(arepo)
    _bind_reasoning(arepo, _SeqLLM(
        plan={"sub_queries": [{"query": "无关问题"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "不应该被使用", "grounded": False}))

    def _empty_run_stage(self, stage, runtime):
        return ReasoningEvidenceSnapshot(
            top_hits=(), elements=(), trace=(), chunks=(), chains=(),
            attempted=(), enumerations=(), collection_map_text="",
            outline=(), outline_evidence=(), baseline_manifest=None,
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _empty_run_stage)
    resp = arepo.ask(nb.id, AskRequest(question="完全无关的问题", mode="reasoning"))

    assert resp.conclusion == _NO_RETRIEVAL_EVIDENCE_MESSAGE
    assert resp.llm_mode == "deterministic"
    assert not resp.answer
    assert not resp.anchors
