"""Agentic Memory P4 (T1+T2): step→anchor 归因的写侧 + ports 读侧投影。

范围**只到**「原始材料被可靠地记下并透传」——P2 registered 的下一阶段
(``retrieval_experience_projection.py`` 消费这批 id 折成
``attributable``/``anchored_hits``)是 T3+T4 的地盘,这里不碰。

写侧硬判据(见 ``reasoning_retrieval.py`` 里紧贴八个写点的注释、以及
``ask_service.py`` 里紧贴 synthesis 步的注释):走到发起 I/O 的路径就无条件写
``result_ids``/``anchor_evidence_ids``(零命中写 ``[]``),``skip`` 分支一律不写;
截断发生时补稀疏 ``result_ids_truncated``/``anchor_evidence_ids_truncated``
标记(修复轮 spec②,只在真截断那天出现)。
读侧硬判据(见 ``app.domain.retrieval_experience::project_run_step`` 的 docstring,
修复轮 Q-P1-1 改写):
非 synthesis/answer 步按 ``detail`` 里 **有没有** ``result_ids`` 这把键透传,
而不是按 step_type 猜;synthesis/answer 步的 ``anchor_evidence_ids`` **同样**
按键存在透传——同名但不携带锚点的步(枚举回答分支、逐节撰写进度步、
reasoning_retrieval.py 的候选池汇总 "answer" 步,以及 ``step_limit`` 截尾后
只剩这类步的 run)整段不投影这个字段,而不是投影一个看起来"零锚点"的空
列表。``result_ids_truncated``/``anchor_ids_truncated`` 同理按键存在透传。
``project_trace_step`` 对这批新键一个字都不碰。

八个写点(reasoning_retrieval.py,修复轮 spec①新增第⑧个):
  ① 初检索 retrieve 步  ② PPR seed  ③ 精确查找 seed  ④ 已确认方向补种
  (coverage)pass  ⑤ expand  ⑥ add_subquery 的 retrieve 步  ⑦ ppr 动作步
  ⑧ exact_lookup 动作步
外加 ask_service.py 的 synthesis 步(``anchor_evidence_ids``)。
"""
from __future__ import annotations

import json
import re
from unittest import mock

import pytest

from app.core.internal_observability import public_trace_steps
from app.models.ask import (
    TRACE_ANCHOR_EVIDENCE_IDS_MAX,
    TRACE_RESULT_IDS_MAX,
    AnswerAnchor,
)
from app.models.schemas import AskRequest, NotebookCreate
from app.domain.retrieval_experience import (
    _bounded_id_list,
    project_run_step,
    project_trace_step,
)
from app.services.reasoning_retrieval import ReasoningResult, ReasoningRetriever
from app.services.retrieval import RetrievedChunk, RetrievedElement
from tests.model_testkit import bind_chat_client
from tests.test_reasoning_retrieval import (
    _SeqLLM,
    _mk_rk,
    _retriever_counting_exact_lookup,
    _seed_manual_notebook,
    _seed_two_nodes,
    rrepo,  # noqa: F401 -- pytest fixture, resolved by name
)


# --------------------------------------------------------------- write side
# reasoning_retrieval.py's eight emit sites (修复轮 spec①新增第⑧个).


def test_initial_retrieve_step_carries_result_ids(rrepo):
    """① 初检索:result_ids == collected 里两个 KG 候选的 object_id。"""
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "")
    step = next(t for t in res.trace if t.step_type == "retrieve")
    assert set(step.detail["result_ids"]) == {h.object_id for h in res.top_hits[:2]}
    assert step.detail["count"] == len(step.detail["result_ids"])


def test_initial_retrieve_step_truncates_result_ids(rrepo):
    """变异验证目标①:去掉写点的 ``[:TRACE_RESULT_IDS_MAX]`` 会让这条用例翻红。

    25 个候选(单条子查询、无去重),超过 TRACE_RESULT_IDS_MAX=20 的上限。
    """
    nb = rrepo.create_notebook(NotebookCreate(name="nb"))
    kg_objects = [
        {"local_id": f"C{i}", "object_type": "claim",
         "payload": {"name": f"节点{i}", "section_path": str(i)}, "evidence": []}
        for i in range(25)
    ]
    rrepo.store_kg(nb.id, None, kg_objects, [])
    # 默认单条子查询的候选上限(DEFAULT_REASONING_PER_QUERY_LIMIT=8)本身就
    # 低于 TRACE_RESULT_IDS_MAX=20 —— 抬高它,让这条用例测的是「result_ids
    # 截断」而不是「每查询召回上限」。
    rrepo.settings.reasoning_per_query_limit = 30
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "节点"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "节点", "")
    step = next(t for t in res.trace if t.step_type == "retrieve")
    assert step.detail["count"] > TRACE_RESULT_IDS_MAX
    assert len(step.detail["result_ids"]) == TRACE_RESULT_IDS_MAX


def test_initial_retrieve_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:① 初检索的零命中形状——空笔记本,result_ids 必须是 ``[]``、
    键必须存在(不是缺席,那是 skip 分支才有的信号)。"""
    nb = rrepo.create_notebook(NotebookCreate(name="nb"))
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "不存在的东西"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "不存在的东西", "")
    step = next(t for t in res.trace if t.step_type == "retrieve")
    assert step.detail["count"] == 0
    assert step.detail["result_ids"] == []
    assert "result_ids" in step.detail


def test_ppr_seed_step_carries_result_ids_and_zero_hit_writes_empty_list(rrepo):
    """② PPR seed:内容(stub 两段新 chunk)+ 零命中(真实 ppr_retrieve 返回空)。

    同一条用例覆盖变异验证目标②的一半:零命中步必须写 ``result_ids: []``,
    不是缺席(那才是 skip 分支的信号)。
    """
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    seed_chunk = RetrievedChunk(chunk_id="ck-seed-1", source_id="s1",
                                source_title="t", section_path="1", text="x",
                                relevance=0.5, score=0.5)
    seed_chunk2 = RetrievedChunk(chunk_id="ck-seed-2", source_id="s1",
                                 source_title="t", section_path="1", text="y",
                                 relevance=0.4, score=0.4)
    rr.ppr_retrieve = lambda notebook_id, query: [seed_chunk, seed_chunk2]
    res = rr.run(nb.id, "这个流程是怎样的", "")
    step = next(t for t in res.trace if t.step_type == "ppr")
    assert step.detail["result_ids"] == ["ck-seed-1", "ck-seed-2"]

    # 零命中分支:一个真的没有覆盖来源的问题,ppr_retrieve 走真实实现返回空。
    rr2 = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res2 = rr2.run(nb.id, "这个流程是怎样的", "")
    step2 = next(t for t in res2.trace if t.step_type == "ppr")
    assert step2.detail["found"] == 0
    assert step2.detail["result_ids"] == []
    assert "result_ids" in step2.detail          # 键必须存在,不能缺席


def test_exact_lookup_seed_step_carries_result_ids(rrepo):
    """③ 精确查找 seed。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "set_db"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    calls: list = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "set_db 命令是怎样的", "")
    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail["result_ids"] == ["ck-main", "ck-args"]


def test_exact_lookup_seed_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:③ 精确查找 seed 的零命中形状——问题里的标识符不在手册里。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "no_such_cmd"}]},
        reflects=[{"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "no_such_cmd 命令是怎样的", "")
    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail["found"] == 0
    assert step.detail["result_ids"] == []


def test_expand_step_carries_result_ids(rrepo):
    """④ expand:result_ids == 邻居对象的 object_id。"""
    nb = _seed_two_nodes(rrepo)
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                 if h.object_type == "claim")
    proc = next(h for h in rrepo._retrieve_scored(nb.id, "RTL到GDSII流程")
                if h.object_type == "procedure")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "")
    step = next(t for t in res.trace if t.step_type == "expand")
    assert step.detail["result_ids"] == [proc.object_id]


def test_expand_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:④ expand 的零命中形状——展开一个没有任何边的孤立节点。"""
    nb = rrepo.create_notebook(NotebookCreate(name="nb"))
    rrepo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "孤立节点", "section_path": "1"}, "evidence": []},
    ], [])
    claim = next(h for h in rrepo._retrieve_scored(nb.id, "孤立节点")
                 if h.object_type == "claim")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "孤立节点", "types": ["claim"]}]},
        reflects=[
            {"next_action": "expand_graph", "expand": {"object_id": claim.object_id}},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "孤立节点", "")
    step = next(t for t in res.trace if t.step_type == "expand")
    assert step.detail["found"] == 0
    assert step.detail["result_ids"] == []


def test_add_subquery_retrieve_step_carries_result_ids(rrepo):
    """⑤ add_subquery 的 retrieve 步:仅新增的 object_id,不含已收集过的。"""
    nb = rrepo.create_notebook(NotebookCreate(name="nb"))
    rrepo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "RTL到GDSII流程概述", "section_path": "1"}, "evidence": []},
        {"local_id": "P1", "object_type": "procedure",
         "payload": {"name": "布局布线步骤", "section_path": "2"}, "evidence": []},
    ], [])
    proc = next(h for h in rrepo._retrieve_scored(nb.id, "布局布线步骤")
                if h.object_type == "procedure")
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程", "types": ["claim"]}]},
        reflects=[
            {"next_action": "add_subquery",
             "new_sub_query": {"query": "布局布线步骤", "types": ["procedure"]}},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "")
    steps = [t for t in res.trace if t.step_type == "retrieve"]
    followup_step = next(t for t in steps if t.detail.get("query") == "布局布线步骤")
    assert followup_step.detail["result_ids"] == [proc.object_id]


def test_add_subquery_retrieve_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:⑤ add_subquery 的零命中形状——子查询命中的对象已全部在
    ``collected`` 里(布局布线步骤在首轮无类型限制的查询里已经被收进来)。"""
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "RTL到GDSII流程"}]},
        reflects=[
            {"next_action": "add_subquery",
             "new_sub_query": {"query": "布局布线步骤"}},
            {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "RTL到GDSII流程", "")
    steps = [t for t in res.trace if t.step_type == "retrieve"]
    followup_step = next(t for t in steps if t.detail.get("query") == "布局布线步骤")
    assert followup_step.detail["new"] == 0
    assert followup_step.detail["result_ids"] == []


def test_coverage_pass_retrieve_step_carries_result_ids(rrepo, monkeypatch):
    """⑥ 已确认方向补种(coverage pass,修复轮 spec①新增):首轮装不下的
    已确认方向在预算内被补跑时,同样要写 result_ids——这条路径此前是唯一
    漏掉的写点,不写就会让"已确认方向"这条归因链在起点断掉,即便命中了
    答案锚点也读不出来。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [
            _mk_rk(f"{query}-{i}", f"{query}-{i}") for i in range(4)
        ])
    result = retriever.run(
        nb.id,
        "完整问题 set_db",
        # overview:首轮 2 个 → 主题二/主题三 溢出到补种。
        intent_queries=["完整问题", "主题一方向", "主题二方向", "主题三方向"],
        limits=ask_retrieval_limits("overview"),
    )
    covered = [s for s in result.trace
              if s.step_type == "retrieve"
              and (s.detail or {}).get("source") == "confirmed_intent"]
    assert covered, "首轮装不下的已确认方向必须补种"
    for step in covered:
        assert "result_ids" in step.detail
        assert len(step.detail["result_ids"]) == 4  # 4 条新 object_id,零去重


def test_coverage_pass_retrieve_step_writes_empty_result_ids_on_zero_hits(
    rrepo, monkeypatch,
):
    """Q-P2-3:⑥ 已确认方向补种的零命中形状——search 桩返回空列表。"""
    from app.core.ask_retrieval_policy import ask_retrieval_limits

    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "planner 不应执行"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
    ))
    retriever = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    monkeypatch.setattr(
        retriever, "search",
        lambda notebook_id, query, types=None, prefer="balanced": [])
    result = retriever.run(
        nb.id,
        "完整问题",
        intent_queries=["完整问题", "主题一方向", "主题二方向", "主题三方向"],
        limits=ask_retrieval_limits("overview"),
    )
    covered = [s for s in result.trace
              if s.step_type == "retrieve"
              and (s.detail or {}).get("source") == "confirmed_intent"]
    assert covered, "首轮装不下的已确认方向必须补种"
    for step in covered:
        assert step.detail["new"] == 0
        assert step.detail["result_ids"] == []


def test_ppr_action_step_carries_result_ids(rrepo):
    """⑦ ppr 动作步:与 seed 不同的新 chunk,证明这是独立写点。"""
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": "跨文档"},
                  {"next_action": "answer", "sufficient": True}]))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    seed_chunk = RetrievedChunk(chunk_id="ck-seed", source_id="s1",
                                source_title="t", section_path="1", text="x",
                                relevance=0.5, score=0.5)
    action_chunk = RetrievedChunk(chunk_id="ck-action", source_id="s1",
                                  source_title="t", section_path="1", text="y",
                                  relevance=0.4, score=0.4)
    calls = {"n": 0}

    def _ppr(notebook_id, query):
        calls["n"] += 1
        return [seed_chunk] if calls["n"] == 1 else [action_chunk]

    rr.ppr_retrieve = _ppr
    res = rr.run(nb.id, "这个流程是怎样的", "")
    action_step = next(t for t in res.trace
                       if t.step_type == "ppr" and t.detail.get("phase") == "action")
    assert action_step.detail["result_ids"] == ["ck-action"]


def test_ppr_action_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:⑦ ppr 动作步的零命中形状——动作调用返回空列表。"""
    nb = _seed_two_nodes(rrepo)
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": "跨文档"},
                  {"next_action": "answer", "sufficient": True}]))
    rr = ReasoningRetriever.from_repository(rrepo, rrepo.settings)
    rr.ppr_retrieve = lambda notebook_id, query: []
    res = rr.run(nb.id, "这个流程是怎样的", "")
    action_step = next(t for t in res.trace
                       if t.step_type == "ppr" and t.detail.get("phase") == "action")
    assert action_step.detail["found"] == 0
    assert action_step.detail["result_ids"] == []


def test_exact_lookup_action_step_carries_result_ids(rrepo):
    """⑧ exact_lookup 动作步(镜像既有 exact-equality 用例,单独钉一次)。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "set_db"},
                  {"next_action": "answer", "sufficient": True}]))
    calls: list = []
    res = _retriever_counting_exact_lookup(rrepo, calls).run(
        nb.id, "这个命令怎么用", "")             # 问题本身无名称 → seed 不触发
    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail["result_ids"] == ["ck-main", "ck-args"]


def test_exact_lookup_action_step_writes_empty_result_ids_on_zero_hits(rrepo):
    """Q-P2-3:⑧ exact_lookup 动作步的零命中形状——名称不在手册里。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "no_such_cmd"},
                  {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "这个命令怎么用", "")
    step = next(t for t in res.trace if t.step_type == "exact_lookup")
    assert step.detail["found"] == 0
    assert step.detail["result_ids"] == []


def test_skip_branches_never_write_result_ids(rrepo):
    """硬判据的另一半:skip 分支一个都不写 ``result_ids`` 键。

    用 exact_lookup 已知会走 ``exact_term_not_identifier`` skip 分支的低选择度
    词试探——它有账目、有 summary,但绝不该有 result_ids(那是 I/O 从未发起
    的分支)。"""
    nb = _seed_manual_notebook(rrepo)
    rrepo.settings.graph_ppr_enabled = False
    bind_chat_client(rrepo, "reasoning_agent", _SeqLLM(
        plan={"sub_queries": [{"query": "布局布线"}]},
        reflects=[{"next_action": "exact_lookup", "exact_term": "第 2.1 节"},
                  {"next_action": "answer", "sufficient": True}]))
    res = ReasoningRetriever.from_repository(rrepo, rrepo.settings).run(
        nb.id, "这个流程是怎样的", "")
    skip = next(t for t in res.trace
               if t.step_type == "skip"
               and t.detail.get("reason") == "exact_term_not_identifier")
    assert "result_ids" not in skip.detail


# ------------------------------------------------------- ask_service.py write


def _minimal_notebook_with_source(repo):
    now = "2026-08-20T00:00:00+08:00"
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", nb.id, "论文一", "pdf", "extracted", "extracted", now, now),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-000", "s1", "formula", "p1", "版图设计要点 公式", "{}", now),
        )
    repo.collection_catalog.invalidate()
    return nb


def _element(element_id, text="版图设计要点", source_id="s1"):
    return RetrievedElement(
        element_id=element_id, source_id=source_id, source_title="论文一",
        location_label="p1", element_type="formula", text=text, score=0.9,
    )


class _CaptureAnswerLLM:
    configured = True
    model = "stub"

    def __init__(self, answer_text="结论[k4001]"):
        self._answer_text = answer_text

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "版图设计要点"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": self._answer_text, "grounded": True})


def _ask_reasoning(repo, notebook, result, llm):
    for service in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, service, llm)
    service = repo._runtime.ask_service()
    with mock.patch.object(ReasoningRetriever, "run", lambda self, *a, **k: result):
        return service.ask_reasoning(
            notebook.id, AskRequest(question="综述一下版图设计要点", mode="reasoning"),
            user_id=repo.current_user().id,
        )


def test_synthesis_step_carries_the_bound_anchor_ids(rrepo):
    """synthesis 步的 anchor_evidence_ids == 真正绑上的 [k] 锚点(不是全部候选)。

    喂两个元素、只引用其中一个 —— 断言 anchor_evidence_ids 只有 e1,不含 e2,
    证明它读的是 ``anchors``(接地信号)而不是 ``elements``(候选池)。
    """
    nb = _minimal_notebook_with_source(rrepo)
    result = ReasoningResult(elements=[_element("e1"), _element("e2", text="另一条")])
    response = _ask_reasoning(rrepo, nb, result, _CaptureAnswerLLM("结论[k4001]"))
    step = next(t for t in response.reasoning_trace if t.step_type == "synthesis")
    assert step.detail["anchor_evidence_ids"] == ["e1"]
    assert "anchor_ids_truncated" not in step.detail
    assert [a.object_id for a in response.anchors] == ["e1"]


def test_synthesis_step_writes_empty_list_on_zero_anchors(rrepo):
    """零锚点(模型没引用任何 [k])仍无条件写 ``anchor_evidence_ids: []``。"""
    nb = _minimal_notebook_with_source(rrepo)
    result = ReasoningResult(elements=[_element("e1")])
    response = _ask_reasoning(rrepo, nb, result, _CaptureAnswerLLM("结论(无引用)"))
    step = next(t for t in response.reasoning_trace if t.step_type == "synthesis")
    assert step.detail["anchor_evidence_ids"] == []
    assert response.anchors == []


def test_synthesis_step_truncates_anchor_evidence_ids(rrepo):
    """anchor_evidence_ids 截到 TRACE_ANCHOR_EVIDENCE_IDS_MAX=96,并给稀疏截断标。

    直接换掉 ``_parse_answer_anchors`` 的返回值来构造 120 个锚点——绕开
    "让模型真的引用 120 个 [k]" 这件不现实的事,只验证截断这一段代码本身。
    """
    nb = _minimal_notebook_with_source(rrepo)
    result = ReasoningResult(elements=[_element("e1")])
    fake_anchors = [
        AnswerAnchor(key=f"k{i}", object_id=f"obj-{i}", object_type="element",
                    label="x")
        for i in range(120)
    ]
    llm = _CaptureAnswerLLM("结论")
    for service in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(rrepo, service, llm)
    service = rrepo._runtime.ask_service()
    with mock.patch.object(ReasoningRetriever, "run", lambda self, *a, **k: result), \
         mock.patch.object(type(service), "_parse_answer_anchors",
                           lambda self, *a, **k: fake_anchors):
        response = service.ask_reasoning(
            nb.id, AskRequest(question="综述一下版图设计要点", mode="reasoning"),
            user_id=rrepo.current_user().id,
        )
    step = next(t for t in response.reasoning_trace if t.step_type == "synthesis")
    assert len(step.detail["anchor_evidence_ids"]) == TRACE_ANCHOR_EVIDENCE_IDS_MAX
    # 写侧(ask_service.py)的稀疏标键名是 anchor_evidence_ids_truncated;
    # app.domain.retrieval_experience::project_run_step 读它后再重发成
    # anchor_ids_truncated —— 两个名字不同层各自成立,见下面读侧那组用例。
    assert step.detail["anchor_evidence_ids_truncated"] is True

    projected = project_run_step(step.model_dump())
    assert projected["anchor_ids_truncated"] is True


# ---------------------------------------------- domain.retrieval_experience read side


def test_bounded_id_list_drops_non_str_and_empty_and_truncates():
    assert _bounded_id_list(None, 5) == []
    assert _bounded_id_list("not-a-list", 5) == []
    assert _bounded_id_list([1, "", None, True, "a", "b"], 5) == ["a", "b"]
    assert _bounded_id_list([f"x{i}" for i in range(10)], 3) == ["x0", "x1", "x2"]


def test_project_run_step_projects_result_ids_when_key_present_including_empty():
    step = {"step_type": "retrieve", "summary": "s",
            "detail": {"count": 0, "result_ids": []}}
    projected = project_run_step(step)
    assert projected["result_ids"] == []


def test_project_run_step_omits_result_ids_when_key_absent():
    """区分「老轨迹/skip 分支,压根没有这把键」与「有键但是空列表」。

    变异验证目标②的另一半:如果实现把"零命中写 []"错改成"空值就不写",
    这条用例会因为 ``result_ids`` 意外出现在一个从未声明它的输入上而失败——
    更准确地说,它钉的是反方向:输入没有这把键时,输出也绝不能凭空长出一个。
    """
    step = {"step_type": "retrieve", "summary": "s", "detail": {"count": 3}}
    projected = project_run_step(step)
    assert "result_ids" not in projected


def test_project_run_step_truncates_result_ids_defense_in_depth():
    step = {"step_type": "expand", "summary": "s",
            "detail": {"found": 30,
                       "result_ids": [f"ko-{i}" for i in range(30)]}}
    projected = project_run_step(step)
    assert len(projected["result_ids"]) == TRACE_RESULT_IDS_MAX


def test_project_run_step_synthesis_branch_projects_anchor_evidence_ids():
    step = {"step_type": "synthesis", "summary": "s",
            "detail": {"citations": 1, "anchors": 1,
                       "anchor_evidence_ids": ["e1"]}}
    projected = project_run_step(step)
    assert projected["anchor_evidence_ids"] == ["e1"]
    assert "anchor_ids_truncated" not in projected


def test_project_run_step_synthesis_branch_empty_anchor_list_is_present():
    step = {"step_type": "answer", "summary": "s",
            "detail": {"citations": 0, "anchors": 0, "anchor_evidence_ids": []}}
    projected = project_run_step(step)
    assert projected["anchor_evidence_ids"] == []


def test_project_run_step_synthesis_branch_carries_the_sparse_truncated_flag():
    step = {"step_type": "synthesis", "summary": "s",
            "detail": {"citations": 5, "anchors": 5,
                       "anchor_evidence_ids": ["e1"] * 96,
                       "anchor_evidence_ids_truncated": True}}
    projected = project_run_step(step)
    assert projected["anchor_ids_truncated"] is True


def test_project_run_step_synthesis_branch_omits_the_truncated_flag_when_false():
    step = {"step_type": "synthesis", "summary": "s",
            "detail": {"citations": 1, "anchors": 1,
                       "anchor_evidence_ids": ["e1"]}}
    projected = project_run_step(step)
    assert "anchor_ids_truncated" not in projected


def test_project_run_step_synthesis_branch_missing_ids_key_omits_the_field():
    """修复轮 Q-P1-1:旧轨迹(P4 之前写下的行,或本来就不带锚点的同名步——
    枚举回答分支、逐节撰写进度步、reasoning_retrieval.py 的候选池汇总
    "answer" 步)没有 anchor_evidence_ids 键——按键存在投影(镜像
    result_ids 的规则):这个字段整体不出现,不是出现一个看起来"零锚点"的
    空列表。``count``(来自 "anchors" 整数键,与 anchor_evidence_ids 无关)
    照常投影,不受这条规则影响。"""
    step = {"step_type": "synthesis", "summary": "s",
            "detail": {"citations": 3, "anchors": 2}}
    projected = project_run_step(step)
    assert "anchor_evidence_ids" not in projected
    assert projected["count"] == 2


def test_project_trace_step_never_surfaces_the_new_keys():
    """变异验证目标③:project_trace_step 一字不改——反向守卫。

    同一批既带 result_ids、又带 anchor_evidence_ids 的 step,分别喂给两个
    投影函数;project_trace_step 的返回值里绝不能出现这两个键。如果有人把
    这批新逻辑错挪到 project_trace_step 里(而不是只留在 project_run_step),
    这条用例翻红。
    """
    retrieve_step = {"step_type": "retrieve", "summary": "s",
                     "detail": {"count": 1, "result_ids": ["ko-1"]}}
    synthesis_step = {"step_type": "synthesis", "summary": "s",
                      "detail": {"citations": 1, "anchors": 1,
                                "anchor_evidence_ids": ["e1"],
                                "anchor_evidence_ids_truncated": True}}
    for step in (retrieve_step, synthesis_step):
        base = project_trace_step(step)
        assert "result_ids" not in base
        assert "anchor_evidence_ids" not in base
        assert "anchor_ids_truncated" not in base
        # project_run_step 在同样的输入上必须真的带出这些键——否则上面的
        # "不带出"断言只是因为两个函数都不带,不能证明区别在 project_trace_step。
        run_projected = project_run_step(step)
        assert ("result_ids" in run_projected) or ("anchor_evidence_ids" in run_projected)


def test_public_trace_steps_passes_result_ids_through_unfiltered():
    """公开/覆盖层过滤只按 step_type 整步过滤,不剥内部键——真源见
    ``docs/product-and-api.md``「问答会话公开分享」契约:公开面结构上
    不带 reasoning_trace,所以这里不是泄漏点;但过滤函数本身的行为必须钉住,
    免得日后有人往它里面加一条按 detail 键剥离的逻辑而不自知。
    """
    steps = [
        {"step_type": "retrieve", "summary": "s",
         "detail": {"count": 1, "result_ids": ["ko-1"]}},
        {"step_type": "source_subgraph", "summary": "internal",
         "detail": {"result_ids": ["ko-2"]}},   # 内部步:整步被过滤
        {"step_type": "synthesis", "summary": "s",
         "detail": {"anchors": 1, "anchor_evidence_ids": ["e1"]}},
    ]
    visible = public_trace_steps(steps)
    assert [s["step_type"] for s in visible] == ["retrieve", "synthesis"]
    assert visible[0]["detail"]["result_ids"] == ["ko-1"]
    assert visible[1]["detail"]["anchor_evidence_ids"] == ["e1"]
