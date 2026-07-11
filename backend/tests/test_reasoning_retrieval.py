import json
import threading

import pytest


def test_trace_step_model_shape():
    from app.models.schemas import TraceStep
    t = TraceStep(step_type="plan", summary="规划了 2 个子查询", detail={"n": 2})
    d = t.model_dump()
    assert d["step_type"] == "plan"
    assert d["summary"].startswith("规划")
    assert d["detail"] == {"n": 2}
    assert d["duration_ms"] is None            # 默认无耗时,record() 时才回填
    t.duration_ms = 1234
    assert t.model_dump()["duration_ms"] == 1234


def test_ask_request_mode_defaults_chunk():
    from app.models.schemas import AskRequest
    assert AskRequest(question="x").mode == "chunk"
    assert AskRequest(question="x", mode="fast").mode == "fast"
    assert AskRequest(question="x", mode="reasoning").mode == "reasoning"


def test_ask_response_reasoning_trace_defaults_none_and_dumps():
    from app.models.schemas import AskResponse
    r = AskResponse(conclusion="x")
    assert r.reasoning_trace is None
    assert "reasoning_trace" in r.model_dump()


def test_reasoning_settings_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_max_steps == 50
    assert s.reasoning_max_subqueries == 5


def test_adaptive_top_n_settings_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.retrieval_top_n == 20            # base floor(旧 12 从未校准,提到 20)
    assert s.reasoning_top_n_per_query == 3
    assert s.reasoning_top_n_cap == 36


def test_adaptive_top_n_env(monkeypatch):
    monkeypatch.setenv("REASONING_TOP_N_PER_QUERY", "5")
    monkeypatch.setenv("REASONING_TOP_N_CAP", "20")
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_top_n_per_query == 5
    assert s.reasoning_top_n_cap == 20


def test_effective_top_n_scales_with_aspects():
    """证据预算=clamp(每方面席位×方面数, floor=retrieval_top_n(20), cap(36))。
    简单/少方面题=floor(20);对比题(如 3+8 兄弟=11 方面)→ 3×11=33,不再被总数挤薄;
    显式传入(报告逐节独立预算)直通。"""
    from app.services.reasoning_retrieval import effective_top_n

    class _S:
        retrieval_top_n = 20
        reasoning_top_n_per_query = 3
        reasoning_top_n_cap = 36

    s = _S()
    assert effective_top_n(s, None, 1) == 20    # 单方面:floor
    assert effective_top_n(s, None, 6) == 20    # 3×6=18 < floor → 仍 20
    assert effective_top_n(s, None, 7) == 21    # 3×7=21 > floor → 自适应接管
    assert effective_top_n(s, None, 11) == 33   # 对比题:3×11,自动扩容
    assert effective_top_n(s, None, 20) == 36   # 封顶 cap
    assert effective_top_n(s, 12, 11) == 12     # 显式传入(报告逐节)直通,不受方面数影响
    assert effective_top_n(s, None, 0) == 20    # 防御:0 方面按 1 算 → floor


def test_reasoning_quota_enabled_default():
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is True


def test_reasoning_quota_enabled_env(monkeypatch):
    monkeypatch.setenv("REASONING_QUOTA_ENABLED", "false")
    from app.core.config import Settings
    assert Settings().reasoning_quota_enabled is False


def test_reasoning_timeout_retry_knobs_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_timeout_seconds == 90
    assert s.reasoning_max_retries == 1


def test_reasoning_timeout_retry_knobs_env(monkeypatch):
    monkeypatch.setenv("REASONING_TIMEOUT_SECONDS", "33")
    monkeypatch.setenv("REASONING_MAX_RETRIES", "4")
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_timeout_seconds == 33
    assert s.reasoning_max_retries == 4


def test_plan_prompt_contains_question_and_schema():
    from app.services.prompts import plan_prompt, PLAN_SCHEMA_HINT
    p = plan_prompt("innovus 的 PR 流程", "User: ...\nAssistant: ...")
    assert "innovus 的 PR 流程" in p
    assert "User: ..." in p  # history_block 被插值进 prompt
    assert "sub_queries" in PLAN_SCHEMA_HINT
    assert "prefer" in PLAN_SCHEMA_HINT


def test_reflect_prompt_contains_summary_and_schema():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    p = reflect_prompt("问题X", "- [claim] A (id=k1)")
    assert "问题X" in p
    assert "id=k1" in p
    assert "next_action" in REFLECT_SCHEMA_HINT
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT
    assert "follow_chain" in REFLECT_SCHEMA_HINT
    assert "start_object_id" in REFLECT_SCHEMA_HINT


def test_answer_prompt_has_derivation_rigor_rules():
    """机理/推导题三条严谨性条款:分层组织+推断桥接、量纲一致+电路可实现形式、单源数值给区间。"""
    from app.services.prompts import answer_prompt
    p = answer_prompt("q", "ctx")
    assert "layer by layer" in p
    assert "dimensionally consistent" in p
    assert "that source's stated value" in p


def test_reflect_prompt_checks_coverage_aspect_by_aspect():
    """sufficient 判据升级为逐层/逐方面核查;ppr 指引扩到跨文档多层推导。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "aspect by aspect" in p
    assert "multi-layer derivation" in p


from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def rrepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 隔离 LLM/推理端点：清空真实 key，避免本地 .env(env_file=../.env) 让 reasoning
    # 测试打真实网络(reasoning_llm_client 不 configured 时回退到测试桩 llm_client)。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_retrieve_scored_returns_sorted_hits(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
    assert hits and hits[0].score >= (hits[-1].score if len(hits) > 1 else 0)
    assert any(h.object_type == "claim" for h in hits)


def test_retrieve_scored_filters_types(rrepo):
    nb = _seed_two_nodes(rrepo)
    hits = rrepo._retrieve_scored(nb.id, "布局布线", types=["procedure"])
    assert all(h.object_type == "procedure" for h in hits)


def test_retrieve_neighbors_follows_edges(rrepo):
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    neigh = rrepo._retrieve_neighbors(nb.id, claim.object_id)
    assert any(n.object_type == "procedure" for n in neigh)
    # 邻居 relevance/score 为占位 0,最终由 run() 用原问题统一重打分(见 Task 8)
    assert all(n.relevance == 0.0 and n.score == 0.0 for n in neigh)


def test_retrieve_neighbors_edge_type_filter(rrepo):
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    assert rrepo._retrieve_neighbors(nb.id, claim.object_id, edge_type="nonexistent") == []


def test_retrieve_elements_degrades_gracefully(rrepo):
    nb = _seed_two_nodes(rrepo)
    # 无 source_elements 时返回空列表,不报错
    assert rrepo._retrieve_elements(nb.id, "任意查询") == []


def test_toolbox_delegates_to_repo(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    hits = rr.search(nb.id, "RTL到GDSII流程", types=["claim"], prefer="keyword")
    assert all(h.object_type == "claim" for h in hits)
    claim = hits[0]
    neigh = rr.neighbors(nb.id, claim.object_id)
    assert any(n.object_type == "procedure" for n in neigh)
    ctx = rr.get(nb.id, claim.object_id)
    assert ctx.get("object_type") == "claim"
    assert rr.get(nb.id, "no-such-id") == {}     # KeyError 吞掉
    assert rr.search_elements(nb.id, "x") == []   # 无原文不报错


class _StubLLM:
    """按 schema_hint 返回预置 JSON;configured 可控。"""
    def __init__(self, plan=None, reflect=None, configured=True):
        self._plan = plan
        self._reflect = reflect
        self.configured = configured
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        return json.dumps(self._reflect)


def _rr_with_llm(repo, **llm):
    from app.services.reasoning_retrieval import ReasoningRetriever
    repo.llm_client = _StubLLM(**llm)
    return ReasoningRetriever.from_repository(repo, repo.settings)


class _KwargsRecordingLLM:
    """Records every chat_json call's kwargs so we can assert plan/reflect
    forward the reasoning-specific timeout/max_retries. Accepting **kwargs is
    itself part of the contract: the call sites must be passing them."""
    configured = True

    def __init__(self, plan, reflect):
        self._plan = plan
        self._reflect = reflect
        self.calls = []  # list of (schema_hint, kwargs)

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append((schema_hint, kwargs))
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        return json.dumps(self._reflect)


class _AnswerRecordingLLM:
    """Fake llm_client for repository answer paths: records chat_json kwargs,
    returns a minimal valid answer JSON."""
    configured = True

    def __init__(self):
        self.calls = []  # list of kwargs dicts

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls.append(kwargs)
        return json.dumps({"answer": "ok", "grounded": False})


def test_answer_reasoning_passes_reasoning_timeout_and_retries(rrepo):
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_timeout_seconds = 88
    rrepo.settings.reasoning_max_retries = 2
    llm = _AnswerRecordingLLM()
    rrepo.llm_client = llm
    rrepo._answer_reasoning(nb.id, "问题", [], [], "")
    assert llm.calls, "_answer_reasoning must call chat_json"
    assert llm.calls[0].get("timeout") == 88
    assert llm.calls[0].get("max_retries") == 2


def test_refine_context_passes_reasoning_kwargs(rrepo):
    """Boundary guard: _refine_context passes timeout+max_retries (from settings)
    to the client — so the refine call inherits the same overrides as the
    reasoning answer call, keeping the two tightly coupled."""
    rrepo.settings.reasoning_timeout_seconds = 77
    rrepo.settings.reasoning_max_retries = 3
    rrepo.settings.kg_query_refine_enabled = True
    llm = _AnswerRecordingLLM()
    # Call _refine_context directly with a non-empty context block.
    result = rrepo._refine_context("问题", "k1: RTL到GDSII流程概述", llm)
    assert llm.calls, "_refine_context must call chat_json"
    assert llm.calls[0].get("timeout") == 77
    assert llm.calls[0].get("max_retries") == 3


def test_plan_passes_reasoning_timeout_and_retries(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    rrepo.settings.reasoning_timeout_seconds = 90
    rrepo.settings.reasoning_max_retries = 1
    llm = _KwargsRecordingLLM(plan={"sub_queries": [{"query": "q"}]}, reflect={})
    rrepo.llm_client = llm
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).plan("问题", "")
    assert llm.calls, "plan must call chat_json"
    _, kwargs = llm.calls[0]
    assert kwargs.get("timeout") == rrepo.settings.reasoning_timeout_seconds
    assert kwargs.get("max_retries") == rrepo.settings.reasoning_max_retries


def test_reflect_passes_reasoning_timeout_and_retries(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    rrepo.settings.reasoning_timeout_seconds = 77
    rrepo.settings.reasoning_max_retries = 3
    llm = _KwargsRecordingLLM(
        plan={}, reflect={"next_action": "answer", "sufficient": True})
    rrepo.llm_client = llm
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).reflect("问题", "summary")
    assert llm.calls, "reflect must call chat_json"
    _, kwargs = llm.calls[0]
    assert kwargs.get("timeout") == 77
    assert kwargs.get("max_retries") == 3


def test_plan_parses_subqueries(rrepo):
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "RTL综合", "types": ["claim"], "prefer": "keyword", "reason": "r"},
        {"query": "布线", "types": ["bogus"], "prefer": "weird"},
    ]})
    subs = rr.plan("问题", "")
    assert [s.query for s in subs] == ["RTL综合", "布线"]
    assert subs[0].types == ["claim"] and subs[0].prefer == "keyword"
    assert subs[1].types == [] and subs[1].prefer == "balanced"  # 非法值被清洗


def test_plan_truncates_to_max_subqueries(rrepo):
    rrepo.settings.reasoning_max_subqueries = 2
    rr = _rr_with_llm(rrepo, plan={"sub_queries": [
        {"query": "a"}, {"query": "b"}, {"query": "c"}]})
    assert len(rr.plan("q", "")) == 2


def test_plan_falls_back_on_bad_json(rrepo):
    rr = _rr_with_llm(rrepo, plan={"garbage": 1})
    subs = rr.plan("原问题X", "")
    assert len(subs) == 1 and subs[0].query == "原问题X"


def test_plan_falls_back_when_llm_unconfigured(rrepo):
    rr = _rr_with_llm(rrepo, configured=False)
    subs = rr.plan("原问题Y", "")
    assert len(subs) == 1 and subs[0].query == "原问题Y"


def test_reflect_parses_expand(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "sufficient": False, "next_action": "expand_graph",
        "expand": {"object_id": "ko-1", "edge_type": "relates", "direction": "out"},
        "reason": "深挖"})
    d = rr.reflect("q", "summary")
    assert d.next_action == "expand_graph" and d.expand_object_id == "ko-1"
    assert d.expand_edge_type == "relates" and d.expand_direction == "out"


def test_reflect_bad_json_becomes_answer(rrepo):
    rr = _rr_with_llm(rrepo, reflect=["not", "a", "dict"])
    d = rr.reflect("q", "s")
    assert d.next_action == "answer" and d.sufficient is True


def test_reflect_falls_back_when_llm_unconfigured(rrepo):
    rr = _rr_with_llm(rrepo, reflect={"next_action": "expand_graph"}, configured=False)
    d = rr.reflect("q", "s")
    assert d.next_action == "answer" and d.sufficient is True


def test_reflect_parses_add_subquery(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "add_subquery",
        "new_sub_query": {"query": "补充查询", "types": ["procedure"], "prefer": "semantic"}})
    d = rr.reflect("q", "s")
    assert d.next_action == "add_subquery"
    assert d.new_sub_query is not None
    assert d.new_sub_query.query == "补充查询"
    assert d.new_sub_query.types == ["procedure"] and d.new_sub_query.prefer == "semantic"


def test_reflect_parses_search_elements(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "search_elements", "elements_query": "原文检索词"})
    d = rr.reflect("q", "s")
    assert d.next_action == "search_elements"
    assert d.elements_query == "原文检索词"


def test_reflect_parses_follow_chain(rrepo):
    rr = _rr_with_llm(rrepo, reflect={
        "next_action": "follow_chain",
        "follow_chain": {
            "start_object_id": "ko-a", "target_object_id": "ko-c",
            "edge_type": "derived_from", "direction": "in",
        },
    })
    d = rr.reflect("q", "s")
    assert d.next_action == "follow_chain"
    assert d.chain_start_object_id == "ko-a"
    assert d.chain_target_object_id == "ko-c"
    assert d.chain_edge_type == "derived_from"
    assert d.chain_direction == "in"


def _seed_follow_chain(repo):
    nb = repo.create_notebook(NotebookCreate(name="follow-chain"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Premise A", "section_path": "A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Bridge B", "section_path": "B"}, "evidence": []},
        {"local_id": "C", "object_type": "claim",
         "payload": {"name": "Conclusion C", "section_path": "C"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "derived_from", "evidence": [{"quote": "A directly yields B"}]},
        {"source_local_id": "B", "target_local_id": "C",
         "edge_type": "derived_from", "evidence": [{"quote": "B directly yields C"}]},
    ])
    with repo._connect() as db:
        ids = {json.loads(r["payload"])["name"]: r["id"] for r in db.execute(
            "SELECT id,payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,))}
    return nb, ids


class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。"""
    configured = True
    def __init__(self, plan, reflects):
        self._plan = plan
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def test_run_plan_then_answer(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True, "reason": "够了"}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert res.top_hits  # 召回到候选
    kinds = [t.step_type for t in res.trace]
    assert kinds[0] == "plan" and "retrieve" in kinds and kinds[-1] == "answer"
    # record() 给每步回填墙钟耗时 —— 全部为非负整数,直达前端展示
    assert all(isinstance(t.duration_ms, int) and t.duration_ms >= 0 for t in res.trace)


def test_run_expand_graph_records_trace(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id},
             "reason": "深挖关系"},
            {"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert any(t.step_type == "expand" for t in res.trace)
    assert any(h.object_type == "procedure" for h in res.top_hits)  # 邻居被纳入


def test_run_follow_chain_records_trace_and_keeps_transient_chain(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb, ids = _seed_follow_chain(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    streamed = []
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "Premise A derived conclusion"}]},
        reflects=[
            {"next_action": "follow_chain", "follow_chain": {
                "start_object_id": ids["Premise A"],
                "edge_type": "derived_from", "direction": "out"}},
            {"next_action": "answer", "sufficient": True},
        ],
    )
    with rrepo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "Premise A 如何推到 Conclusion C", "", on_step=streamed.append)
    assert len(res.chains) == 1
    assert res.chains[0].target_name == "Conclusion C"
    assert res.chains[0].query_relevance > 0
    step = next(t for t in res.trace if t.step_type == "follow_chain")
    assert step.detail["hops"] == 2 and step.detail["count"] == 1
    assert step.detail["paths"] == [{
        "source": "Premise A", "via": "Bridge B", "target": "Conclusion C",
        "edge_type": "derived_from",
        "trust": pytest.approx(res.chains[0].chain_trust),
        "validity_scope": {},
    }]
    assert any(t.step_type == "follow_chain" for t in streamed)
    with rrepo._connect() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    assert after == before == 2


def test_run_follow_chain_rejects_start_outside_current_candidates(rrepo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[
            {"next_action": "follow_chain", "follow_chain": {
                "start_object_id": "ko-guessed-not-a-candidate",
                "edge_type": "derived_from", "direction": "out"}},
            {"next_action": "answer", "sufficient": True},
        ],
    )
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    def unexpected_follow_chain(*_args, **_kwargs):
        pytest.fail("follow_chain must not run for an arbitrary non-candidate id")

    monkeypatch.setattr(rr, "follow_chain", unexpected_follow_chain)
    result = rr.run(nb.id, "RTL到GDSII流程", "")
    skip = next(t for t in result.trace
                if t.detail.get("reason") == "chain_start_not_candidate")
    assert skip.step_type == "skip"
    assert result.chains == []


def test_answer_reasoning_renders_citable_hops_and_uncited_inference(rrepo):
    nb, ids = _seed_follow_chain(rrepo)
    chain_result = rrepo._follow_chain(
        nb.id, ids["Premise A"], edge_type="derived_from")
    assert chain_result.inferences

    class _ChainAnswerLLM:
        configured = True

        def __init__(self):
            self.prompt = ""

        def chat_json(self, messages, schema_hint, **kwargs):
            self.prompt = messages[-1]["content"]
            return json.dumps({
                "answer": (
                    "Premise A directly yields Bridge B and Bridge B directly "
                    "yields Conclusion C.[k2001, k2002] "
                    "（推断）Premise A therefore indirectly yields Conclusion C."
                ),
                "grounded": True,
            })

    llm = _ChainAnswerLLM()
    rrepo.llm_client = llm
    rrepo.settings.kg_query_refine_enabled = False
    answer, grounded, anchors = rrepo._answer_reasoning(
        nb.id, "derive", chain_result.nodes, [], "",
        chains=chain_result.inferences)
    assert grounded is True
    assert "[Query-time typed inference; NOT directly stated]" in llm.prompt
    assert "Premise A --derived_from--> Bridge B" in llm.prompt
    assert "Bridge B --derived_from--> Conclusion C" in llm.prompt
    assert "attach NO [k] marker" in llm.prompt
    assert "（推断）" in answer
    assert [a.object_type for a in anchors] == ["relation", "relation"]
    assert [a.snippet for a in anchors] == ["A directly yields B", "B directly yields C"]


def test_grouped_answer_markers_fail_closed_when_any_key_is_unknown(rrepo):
    id_map = {
        "k2001": {
            "object_id": "rel-ab", "object_type": "relation",
            "name": "A --derived_from--> B",
        },
        "k2002": {
            "object_id": "rel-bc", "object_type": "relation",
            "name": "B --derived_from--> C",
        },
    }
    anchors = rrepo._parse_answer_anchors("premises [k2001, k2002]", id_map)
    assert [a.key for a in anchors] == ["k2001", "k2002"]
    assert rrepo._parse_answer_anchors("mixed [k2001, k9999]", id_map) == []


def test_run_dedups_expand_and_respects_step_cap(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.settings.reasoning_max_steps = 3
    # 始终要求 expand 同一节点 → 去重后无新增,且步数撞上限强制收尾
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "expand_graph",
                   "expand": {"object_id": claim.object_id}}] * 10)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) <= 3                 # circuit breaker 生效
    assert res.trace[-1].step_type == "answer"     # 仍正常收尾


def test_run_add_subquery_without_payload_continues(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    # add_subquery 但缺 new_sub_query: 应记 skip 并继续(不提前 break),下一轮才 answer
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "add_subquery"},
                  {"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) == 2                  # 两轮 reflect 都执行,未提前终止
    assert any(t.step_type == "skip" for t in res.trace)
    assert res.trace[-1].step_type == "answer"


def test_run_feeds_no_progress_signal_to_reflect_after_fruitless_retrieval(rrepo):
    """复现根因:某次检索动作未带来任何新证据时,下一轮 reflect 的输入必须携带
    '无新进展'信号,让模型据此自主决定是否直接作答;否则模型盲目重复同一动作,
    一路空转到 reasoning_max_steps —— 这正是推理模式'整体运行很久'的根因。

    注意:本用例不替模型拍板(不强制 answer),只断言信号被喂回 reflect。
    是否作答仍由模型决定(契合用户要求:始终进 reflect,由模型判断)。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)  # 该库无 source_elements → search_elements 恒返回 []

    captured_reflect_prompts: list[str] = []

    class _RecordingLLM:
        configured = True

        def __init__(self):
            # 第1轮 search_elements(必 0 新增,因无原文段),第2轮 answer
            self._reflects = [
                {"next_action": "search_elements", "elements_query": "q"},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            captured_reflect_prompts.append(messages[-1]["content"])
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    rrepo.llm_client = _RecordingLLM()
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

    assert len(captured_reflect_prompts) == 2
    # 初检索命中 KG 节点(有新增)→ 首轮 reflect 不应带"无新进展"信号
    assert "未带来新证据" not in captured_reflect_prompts[0]
    # search_elements 零新增 → 次轮 reflect 必须带"无新进展"信号(待实现)
    assert "未带来新证据" in captured_reflect_prompts[1]


def _mk_rk(object_id, name):
    """构造一个可辨识的 RetrievedKnowledge(payload.name 带标记,便于断言去重保留了哪条)。"""
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=object_id, object_type="claim",
                              payload={"name": name})


def test_run_initial_retrieval_is_parallel(rrepo, monkeypatch):
    """并发性测试:≥3 个子查询的初检索必须并发执行。

    用 threading.Barrier(parties=子查询数) 证明:每个子查询的 search 调用内
    都 barrier.wait(timeout)。串行实现下只有 1 个线程能到达 barrier,wait 超时
    抛 BrokenBarrierError → 本测试 RED;并行实现下所有线程同时到达 → GREEN。

    reflect 桩首步即 answer,让 reflect 循环立刻结束,聚焦初检索。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    subq = [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}]
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": subq},
        reflects=[{"next_action": "answer", "sufficient": True}])

    barrier = threading.Barrier(len(subq))

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        # 串行:第一个线程在此 wait,无人来汇合 → 超时抛 BrokenBarrierError。
        # 并行:三个线程同时到达 → 全部放行。
        barrier.wait(timeout=3)
        return [_mk_rk(f"id-{query}", query)]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    # 并发成立才能跑到这里(否则 search 抛 BrokenBarrierError 被吞 → 三条都丢)。
    # 三个子查询各贡献一个不同 id → 初检索计数为 3。
    retrieve_steps = [t for t in res.trace if t.step_type == "retrieve"]
    assert retrieve_steps and retrieve_steps[0].detail["count"] == 3


def test_run_initial_retrieval_preserves_order_and_dedup(rrepo, monkeypatch):
    """顺序/去重确定性测试:并发后,重复 object_id 仍保留"按子查询顺序的第一个"版本。

    两个子查询命中有重叠 id 的结果但顺序不同:
      sq1 -> [shared(标记A), only1]
      sq2 -> [shared(标记B), only2]
    并发收集后,shared 必须保留 sq1 的版本(标记A),证明纳入顺序按子查询原序、
    而非线程完成顺序。用注入的可辨识 payload 直接断言去重保留了哪一条。

    注意:run() 末尾会用原问题对 collected 统一重打分,但这里注入的 id
    不在 _seed_two_nodes 的真实库内 → _retrieve_scored 取不到 → 回退到 collected
    版本,故 top_hits 里 shared 的 payload 即为去重保留的那条。
    """
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "first"}, {"query": "second"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])

    returns = {
        "first": [_mk_rk("shared", "A-from-first"), _mk_rk("only1", "only1")],
        "second": [_mk_rk("shared", "B-from-second"), _mk_rk("only2", "only2")],
    }

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        return returns[query]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    by_id = {h.object_id: h for h in res.top_hits}
    assert set(by_id) == {"shared", "only1", "only2"}      # 去重:shared 只一份
    # 第一个出现(sq1=first)的版本胜出
    assert by_id["shared"].payload["name"] == "A-from-first"


def test_run_initial_retrieval_swallows_single_search_failure(rrepo, monkeypatch):
    """容错:任一子查询 search 抛异常不应让整个 run 崩,失败者记空结果,其余正常。"""
    from app.services.reasoning_retrieval import ReasoningRetriever

    nb = _seed_two_nodes(rrepo)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "boom"}, {"query": "ok"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])

    def fake_search(self, notebook_id, query, types=None, prefer="balanced"):
        if query == "boom":
            raise RuntimeError("search blew up")
        return [_mk_rk("ok-id", "ok")]

    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    retrieve_steps = [t for t in res.trace if t.step_type == "retrieve"]
    assert retrieve_steps[0].detail["count"] == 1          # 只剩成功者
    assert {h.object_id for h in res.top_hits} == {"ok-id"}


# ---- 退化循环熔断 (reasoning loop guard) ----

def test_reasoning_loop_guard_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_stale_limit == 3
    assert s.reasoning_max_element_searches == 5


def test_run_stale_breaker_on_repeated_visited_expand(rrepo, monkeypatch):
    """模式A: reflect 反复请求展开同一已访问节点 → 连续无进展, stale 熔断提前收尾
    (远早于 reasoning_max_steps=50, 不再空转几十轮)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_stale_limit = 3
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(ReasoningRetriever, "neighbors",
                        lambda self, n, oid, edge_type=None, direction="both": [_mk_rk("B", "nodeB")])
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "q"}]},
        reflects=[{"next_action": "expand_graph", "expand": {"object_id": "A"}}] * 40)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) <= 5             # stale 熔断: 远小于 50
    assert res.trace[-1].step_type == "answer" # 仍正常收尾


def test_run_caps_repeated_element_search(rrepo, monkeypatch):
    """模式B: reflect 反复 search_elements 且每次都有"新"原文段(no_progress 不触发),
    靠 element 搜索次数上限熔断, 不空转到 reasoning_max_steps。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.services.retrieval import RetrievedElement
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_max_element_searches = 4
    rrepo.settings.reasoning_stale_limit = 3
    counter = {"n": 0}

    def fake_elements(self, n, q):
        counter["n"] += 1
        return [RetrievedElement(element_id=f"e{counter['n']}", source_id="s",
                                 source_title="src", location_label="L",
                                 element_type="paragraph", text="原文段")]

    monkeypatch.setattr(ReasoningRetriever, "search_elements", fake_elements)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "q"}]},
        reflects=[{"next_action": "search_elements", "elements_query": "q"}] * 40)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    assert counter["n"] <= 4                   # 实际执行的 element 检索不超过上限
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) < 20             # 远小于 50
    assert res.trace[-1].step_type == "answer"


def test_run_does_not_break_while_progressing(rrepo, monkeypatch):
    """熔断不误杀: 只要每轮 expand 带来新节点(有进展), stale 一直重置, 不提前终止
    —— 保证有效深挖不被熔断打断。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_max_steps = 50
    rrepo.settings.reasoning_stale_limit = 3
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("seed", "seed")])
    seq = {"n": 0}

    def fake_neighbors(self, n, oid, edge_type=None, direction="both"):
        seq["n"] += 1
        return [_mk_rk(f"nb{seq['n']}", f"nb{seq['n']}")]   # 每轮全新邻居

    monkeypatch.setattr(ReasoningRetriever, "neighbors", fake_neighbors)
    reflects = [{"next_action": "expand_graph", "expand": {"object_id": f"x{i}"}}
                for i in range(5)]                          # 5 轮深挖不同节点
    reflects.append({"next_action": "answer", "sufficient": True})
    rrepo.llm_client = _SeqLLM(plan={"sub_queries": [{"query": "q"}]}, reflects=reflects)
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    reflect_steps = [t for t in res.trace if t.step_type == "reflect"]
    assert len(reflect_steps) == 6             # 5 轮有进展深挖 + 1 轮 answer, 未误熔断


def test_run_feeds_visited_nodes_to_reflect(rrepo, monkeypatch):
    """已访问节点回喂: 展开过的节点应出现在后续 reflect 的输入里, 提示模型勿重复请求
    (治模式A的根源——模型反复请求同一节点)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_stale_limit = 10  # 调高避免熔断先于断言触发
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_mk_rk("A", "nodeA")])
    monkeypatch.setattr(ReasoningRetriever, "neighbors",
                        lambda self, n, oid, edge_type=None, direction="both": [_mk_rk("B", "nodeB")])
    prompts = []

    class _RecLLM:
        configured = True

        def __init__(self):
            self._r = [{"next_action": "expand_graph", "expand": {"object_id": "A"}},
                       {"next_action": "answer", "sufficient": True}]

        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "q"}]})
            prompts.append(messages[-1]["content"])
            return json.dumps(self._r.pop(0) if self._r else {"next_action": "answer", "sufficient": True})

    rrepo.llm_client = _RecLLM()
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q", "")
    assert len(prompts) >= 2
    # 第1轮 expand A 后, 第2轮 reflect 输入应带"已展开/已访问"节点提示, 含节点标识
    assert "nodeA" in prompts[1] or "A" in prompts[1]
    assert ("已展开" in prompts[1] or "已访问" in prompts[1] or "visited" in prompts[1].lower())


def _rk(oid, rel, otype="claim"):
    """构造带 relevance 的 RetrievedKnowledge(配额测试用)。"""
    from app.services.retrieval import RetrievedKnowledge
    return RetrievedKnowledge(object_id=oid, object_type=otype,
                              payload={"name": oid}, relevance=rel)


def test_quota_rerank_rescues_weak_group(rrepo, monkeypatch):
    """配额核心: 弱势子查询组(分数低)也保底进 top-N, 不被强势组通吃。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qV3": [_rk("A", 0.5), _rk("B", 0.45)],
        "qR1": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85), _rk("F", 0.8)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["A", "B", "C", "D", "E", "F"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qV3", "qR1"], top_n=2)
    ids = [h.object_id for h in hits]
    assert "A" in ids and "C" in ids       # 两组各贡献队首(全局会是 C,D)
    assert counts == [1, 1, 0]               # [qV3, qR1, 兜底组]: 各子查询 1 条、兜底 0


def test_quota_rerank_roundrobin_balance(rrepo, monkeypatch):
    """组大小悬殊(4 vs 2)时 top_n=4 内两组都有名额, 不被大组占满。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    per_q = {
        "qA": [_rk("a1", .9), _rk("a2", .8), _rk("a3", .7), _rk("a4", .6)],
        "qB": [_rk("b1", .95), _rk("b2", .85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["a1","a2","a3","a4","b1","b2"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA", "qB"], top_n=4)
    ids = {h.object_id for h in hits}
    assert "b1" in ids and "b2" in ids       # 小组的 2 条都进(round-robin 保底)
    assert counts == [2, 2, 0]                # [qA, qB, 兜底组]: 4 名额两组均分、兜底 0


def test_quota_rerank_tolerates_subquery_failure(rrepo, monkeypatch):
    """某子查询 search 抛错 → 该组空, 其余组正常出候选, 不崩。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    def fake_search(self, n, q, types=None, prefer="balanced"):
        if q == "boom":
            raise RuntimeError("search blew up")
        return [_rk("C", 0.9), _rk("D", 0.8)]
    monkeypatch.setattr(ReasoningRetriever, "search", fake_search)
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    collected = {oid: _rk(oid, 0.0) for oid in ["C", "D"]}
    hits, counts = rr._quota_rerank(nb.id, collected, ["boom", "ok"], top_n=2)
    ids = {h.object_id for h in hits}
    assert ids == {"C", "D"}                  # 失败组空, ok 组正常
    assert counts[0] == 0                      # 失败组贡献 0


def test_quota_rerank_fallback_group_last(rrepo, monkeypatch):
    """所有子查询都查不到的候选(relevance 全 0)进兜底组, 优先级最低但仍可入选。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    # X 不在任何子查询结果 → 兜底组
    collected = {"A": _rk("A", 0.0), "X": _rk("X", 0.0)}
    hits, counts = rr._quota_rerank(nb.id, collected, ["qA"], top_n=2)
    ids = [h.object_id for h in hits]
    assert ids[0] == "A"                       # 子查询组优先
    assert "X" in ids                          # 兜底组仍入选(名额没满时)
    assert counts[-1] == 1                     # 最后一个 count 是兜底组


def test_run_quota_path_keeps_both_groups(rrepo, monkeypatch):
    """复合(≥2 子查询)+ 开关开 → 走配额, top_hits 同时含两组候选(弱势组不被挤掉)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    rrepo.settings.retrieval_top_n = 2
    # 自适应预算下,"紧预算"由 cap 表达(retrieval_top_n 只是 floor,2 方面会被
    # per_query×2=6 抬高):cap=2 钉住总预算,保住本测试「配额救弱势组」的原意。
    rrepo.settings.reasoning_top_n_cap = 2
    # 用纯字母 key 避免 expand_query 内 normalize_terms 插入空格后 dict 查找失效
    per_q = {
        "subV": [_rk("A", 0.5), _rk("B", 0.45)],
        "subR": [_rk("C", 0.95), _rk("D", 0.9), _rk("E", 0.85)],
    }
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": per_q.get(q, []))
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "subV"}, {"query": "subR"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "subV subR", "")
    ids = {h.object_id for h in res.top_hits}
    assert "A" in ids and "C" in ids          # 配额救回弱势组 A(全局 top-2 会是 C,D)
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert ans.detail.get("quota") == [1, 1]  # 可观测: 每子查询贡献数


def test_run_single_subquery_uses_global(rrepo, monkeypatch):
    """单子查询 → 不进配额, 走原全局重排(行为不变)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = True
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "only"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "only", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 全局路径不带 quota


def test_run_quota_disabled_uses_global(rrepo, monkeypatch):
    """开关关 → 复合问题也走全局重排。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    rrepo.settings.reasoning_quota_enabled = False
    monkeypatch.setattr(ReasoningRetriever, "search",
                        lambda self, n, q, types=None, prefer="balanced": [_rk("A", 0.9)])
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "q1"}, {"query": "q2"}]},
        reflects=[{"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "q1 q2", "")
    ans = next(t for t in res.trace if t.step_type == "answer")
    assert "quota" not in (ans.detail or {})   # 开关关 → 全局路径


def test_plan_uses_expand_query(rrepo, monkeypatch):
    import app.services.query_rewrite as qr
    monkeypatch.setattr(qr, "expand_query", lambda *a, **k: qr.ExpandedQuery(
        query="x", sub_queries=[qr.SubQuerySpec("sub A", types=["concept"]),
                                qr.SubQuerySpec("sub B")]))
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    subs = r.plan("中文复合问题")
    assert [s.query for s in subs] == ["sub A", "sub B"] and subs[0].types == ["concept"]


def test_run_expand_summary_uses_node_name_not_id(rrepo):
    """trace 可读性: expand step 的 summary 应显示节点名(人读), 而非裸 object_id。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[{"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
                  {"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    expand = next(t for t in res.trace if t.step_type == "expand")
    assert "RTL到GDSII流程概述" in expand.summary          # 人读名
    assert claim.object_id not in expand.summary           # 不再暴露裸 id
    assert expand.detail.get("name") == "RTL到GDSII流程概述"  # detail 带 name
    assert expand.detail.get("object_id") == claim.object_id  # detail 仍保留 id(机器/调试)


def test_run_duplicate_subquery_skipped_not_rerun(rrepo, monkeypatch):
    """add_subquery 重复已试过的子查询(含与初始 plan 重复、归一化等价)→ 硬跳过:
    不再执行 search,记 skip trace(reason=duplicate_subquery)。治「反复补充同一条
    子查询白烧检索」;跳过属零新增,stale 熔断语义不变。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _RepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                # 与 plan 子查询同文本 → 应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},
                # 仅大小写/空白差异,归一化后仍重复 → 也应跳过
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "  rtl到gdsii流程 "}},
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    rrepo.llm_client = _RepeatLLM()
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    calls: list[str] = []
    orig_search = retriever.search

    def _spy(nb_id, query, types=None, prefer="balanced"):
        calls.append(query)
        return orig_search(nb_id, query, types, prefer)

    monkeypatch.setattr(retriever, "search", _spy)
    steps = []
    retriever.run(nb.id, "RTL到GDSII流程", "", on_step=steps.append)

    # search 只在初检索执行 1 次;两次重复 add_subquery 均被跳过、未重跑
    assert calls == ["RTL到GDSII流程"]
    skips = [s for s in steps if s.step_type == "skip"
             and s.detail.get("reason") == "duplicate_subquery"]
    assert len(skips) == 2
    assert "跳过重复子查询" in skips[0].summary


def test_run_feeds_attempted_subqueries_to_reflect(rrepo):
    """已执行过的子查询账目(文本+新增证据数+尝试次数)必须回喂 reflect prompt:
    ①首轮即含初始 plan 的子查询与各自新增数(治「plan 对 reflect 不可见→首轮就
    复述 plan 已跑过的」);②重复被跳过后,下一轮 prompt 含尝试次数(账目变化使
    prompt 非不动点,LLM 缓存不会原样吐回上一轮决策)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    captured: list[str] = []

    class _RecordingRepeatLLM:
        configured = True

        def __init__(self):
            self._reflects = [
                {"next_action": "add_subquery",
                 "new_sub_query": {"query": "RTL到GDSII流程"}},  # 重复 plan → 跳过
                {"next_action": "answer", "sufficient": True},
            ]

        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [
                    {"query": "RTL到GDSII流程"}, {"query": "时序收敛方法"}]})
            captured.append(messages[-1]["content"])
            nxt = self._reflects.pop(0) if self._reflects else {
                "next_action": "answer", "sufficient": True}
            return json.dumps(nxt)

    rrepo.llm_client = _RecordingRepeatLLM()
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

    assert len(captured) == 2
    # ① 首轮:初始 plan 两条子查询都在账目里,且带新增数与去重告诫
    assert "已执行过的子查询" in captured[0]
    assert "RTL到GDSII流程" in captured[0] and "时序收敛方法" in captured[0]
    assert "新增" in captured[0] and "勿重复" in captured[0]
    assert "已试" not in captured[0]          # 首轮各 1 次,不显示次数
    # ② 重复被跳过后:该条账目显示已试 2 次 → 两轮 prompt 必不同(破缓存不动点)
    assert "已试2次" in captured[1]
    assert captured[0] != captured[1]


def test_window_helper_head_tail_split():
    """_window: 超窗保留最早 head 条+最新 tail 条并报省略数;不超窗原样返回。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    head, tail, omitted = ReasoningRetriever._window(list(range(15)), 6, 4)
    assert head == list(range(6))
    assert tail == [11, 12, 13, 14]
    assert omitted == 5
    head, tail, omitted = ReasoningRetriever._window(list(range(10)), 6, 4)
    assert head == list(range(10)) and tail == [] and omitted == 0


def test_summarize_shows_recent_tail_when_over_window(rrepo):
    """collected 超 30 条时,summary 必须含最近加入的尾段(修「新增证据落在
    前 30 条窗口外 → summary 不变 → reflect 误判无进展/重复请求」盲区);
    ≤30 条时输出与旧行为完全一致(无省略标记)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    r = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    big = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(45)}
    out = r._summarize(big, [], [])
    assert "节点0" in out and "节点19" in out        # 头段(最早 20 条)
    assert "节点35" in out and "节点44" in out       # 尾段(最新 10 条)
    assert "节点25" not in out                       # 中间被省略
    assert "省略中间 15 条" in out
    small = {f"ko-{i}": _mk_rk(f"ko-{i}", f"节点{i}") for i in range(30)}
    out2 = r._summarize(small, [], [])
    assert "省略" not in out2 and "节点29" in out2


def test_reflect_prompt_warns_against_resubmitting_tried_subqueries():
    """静态指令层也要有勿重复告诫(动态账目回喂之外的第二层):expand_graph
    文案明写可反复展开,add_subquery 原本连'勿重复'都没有——治理不对称。"""
    from app.services.prompts import reflect_prompt
    p = reflect_prompt("q", "s")
    assert "Never re-submit" in p


def test_run_exposes_attempted_ledger_and_top_n_override(rrepo):
    """报告管线依赖:run() 返回 attempted 账目(query/new/tries),且 top_n 参数
    覆盖 settings.retrieval_top_n(每节独立预算)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)

    class _PlanOnlyLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kwargs):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            return json.dumps({"next_action": "answer", "sufficient": True})

    rrepo.llm_client = _PlanOnlyLLM()
    result = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "", top_n=1)
    assert len(result.top_hits) <= 1                       # top_n 覆盖生效
    assert result.attempted and result.attempted[0]["query"] == "RTL到GDSII流程"
    assert set(result.attempted[0]) == {"query", "new", "tries"}


def test_run_max_steps_override_caps_reflect_loop(rrepo):
    """max_steps 覆盖 settings.reasoning_max_steps,封顶 reflect 轮数(报告滑块用)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_nodes(rrepo)
    calls = {"reflect": 0}

    class _LoopLLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            if "sub_queries" in schema_hint:
                return json.dumps({"sub_queries": [{"query": "RTL到GDSII流程"}]})
            calls["reflect"] += 1
            return json.dumps({"next_action": "search_elements", "elements_query": "q"})  # 永不 answer

    rrepo.llm_client = _LoopLLM()
    ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", max_steps=2)
    assert calls["reflect"] <= 2      # 被 max_steps=2 封顶(而非 settings 的 50)


def test_run_expand_community_fans_out_peers(rrepo, monkeypatch):
    """expand_community 动作:对焦点社区兄弟发子查询、记 expand_community trace。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    import app.services.communities as C
    nb = _seed_two_nodes(rrepo)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "DeepSeek-V4"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "DeepSeek-V4",
             "reason": "需要同类"},
            {"next_action": "answer", "sufficient": True}])
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(retriever.communities, "first_base_notebook_id", lambda *a: nb.id)
    monkeypatch.setattr(
        retriever.communities,
        "resolve_comparison_peers",
        lambda *a, **k: (["RTL综合", "布线"], "community"),
    )
    res = retriever.run(nb.id, "DeepSeek-V4 相比其他", "")
    assert any(t.step_type == "expand_community" for t in res.trace)
    attempted_q = [a["query"] for a in res.attempted]
    assert "RTL综合" in attempted_q and "布线" in attempted_q


def test_from_repository_passes_configured_sibling_threshold(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever

    rrepo.settings.sibling_min_bridge = 5
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)

    assert retriever.communities.sibling_min_bridge == 5


def test_run_expand_community_no_base_noop(rrepo, monkeypatch):
    """无 base 库 → 不 fan-out,优雅继续(fail-open)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    import app.services.communities as C
    nb = _seed_two_nodes(rrepo)
    called = {"peers": 0}
    def _peers(*a, **k):
        called["peers"] += 1
        return []
    monkeypatch.setattr(C, "community_peers", _peers)
    monkeypatch.setattr(C, "first_base_notebook_id", lambda *a, **k: None)
    rrepo.llm_client = _SeqLLM(
        plan={"sub_queries": [{"query": "X"}]},
        reflects=[
            {"next_action": "expand_community", "community_focal": "X"},
            {"next_action": "answer", "sufficient": True}])
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(nb.id, "X 相比其他", "")
    assert called["peers"] == 0                     # base 为 None → 根本不调 community_peers
    assert any(t.step_type == "expand_community" for t in res.trace)
