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


def test_ask_request_mode_defaults_fast():
    from app.models.schemas import AskRequest
    assert AskRequest(question="x").mode == "fast"
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
    rr = ReasoningRetriever(rrepo, rrepo.settings)
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
    return ReasoningRetriever(repo, repo.settings)


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


def test_answer_kg_fast_path_does_not_pass_reasoning_overrides(rrepo):
    """Boundary guard: the fast-path _answer_kg must keep using the global
    default (no per-call timeout/max_retries), so extraction/fast paths are
    unaffected."""
    nb = _seed_two_nodes(rrepo)
    llm = _AnswerRecordingLLM()
    rrepo.llm_client = llm
    rrepo._answer_kg(nb.id, "问题", [], "")
    assert llm.calls, "_answer_kg must call chat_json"
    assert "timeout" not in llm.calls[0]
    assert "max_retries" not in llm.calls[0]


def test_plan_passes_reasoning_timeout_and_retries(rrepo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    rrepo.settings.reasoning_timeout_seconds = 90
    rrepo.settings.reasoning_max_retries = 1
    llm = _KwargsRecordingLLM(plan={"sub_queries": [{"query": "q"}]}, reflect={})
    rrepo.llm_client = llm
    ReasoningRetriever(rrepo, rrepo.settings).plan("问题", "")
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
    ReasoningRetriever(rrepo, rrepo.settings).reflect("问题", "summary")
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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert res.top_hits  # 召回到候选
    kinds = [t.step_type for t in res.trace]
    assert kinds[0] == "plan" and "retrieve" in kinds and kinds[-1] == "answer"


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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
    assert any(t.step_type == "expand" for t in res.trace)
    assert any(h.object_type == "procedure" for h in res.top_hits)  # 邻居被纳入


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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")
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
    ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "RTL到GDSII流程", "")

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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "原问题", "")

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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "原问题", "")

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
    res = ReasoningRetriever(rrepo, rrepo.settings).run(nb.id, "原问题", "")

    retrieve_steps = [t for t in res.trace if t.step_type == "retrieve"]
    assert retrieve_steps[0].detail["count"] == 1          # 只剩成功者
    assert {h.object_id for h in res.top_hits} == {"ok-id"}
