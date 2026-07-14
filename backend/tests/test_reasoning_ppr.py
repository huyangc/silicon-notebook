import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """两个源,各一个 MoE 概念节点,经 concept_clusters(canonical_id=K-moe)桥接;
    每节点 evidence 指向本源的 chunk。复刻 test_ppr_retrieve.py 同名助手。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


def test_reflect_prompt_and_schema_expose_ppr():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    assert "ppr_retrieve" in REFLECT_SCHEMA_HINT
    assert "ppr_query" in REFLECT_SCHEMA_HINT
    p = reflect_prompt("对比 DeepSeek 与 GLM", "- [concept] MoE (id=k1)")
    assert "ppr_retrieve" in p
    # 既有 4 动作不丢
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT


def test_reflect_parses_ppr_retrieve_decision():
    from app.services.reasoning_retrieval import ReasoningRetriever
    from app.core.config import Settings

    class _LLM:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"next_action": "ppr_retrieve",
                               "ppr_query": "DeepSeek vs GLM MoE", "reason": "需跨文档对比"})

    class _Retrieval:
        pass

    class _Models:
        reasoning_llm_client = _LLM()

    class _Communities:
        pass

    rr = ReasoningRetriever(
        retrieval=_Retrieval(), model_clients=_Models(), communities=_Communities(),
        settings=Settings(_env_file=None),
    )
    d = rr.reflect("对比题", "候选摘要")
    assert d.next_action == "ppr_retrieve"
    assert d.ppr_query == "DeepSeek vs GLM MoE"


class _AnswerOnlyLLM:
    """plan 出单子查询;reflect 永远 answer(不选 ppr_retrieve)→ 只靠 seed pass。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "都用 MoE [k1].", "grounded": True})


def test_run_seed_pass_populates_cross_doc_chunks_when_flag_on(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    assert repo.settings.graph_ppr_enabled is True   # 默认开
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ids = {c.chunk_id for c in result.chunks}
    assert "cA" in ids and "cB" in ids               # seed pass 拉到跨文档 chunk
    assert any(s.step_type == "ppr" for s in result.trace)


def test_run_no_seed_when_flag_off(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    assert result.chunks == []
    assert not any(s.step_type == "ppr" for s in result.trace)


class _ScriptedReflectLLM:
    """plan 单子查询;reflect 按 reflects 列表逐轮弹出;其余=answer。"""
    configured = True
    def __init__(self, reflects):
        self._reflects = list(reflects)
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps(self._reflects.pop(0) if self._reflects
                              else {"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "ok [k1].", "grounded": True})


def test_ppr_retrieve_action_caps_at_max(repo, monkeypatch):
    """连发 4 次 ppr_retrieve 决策 → 仅前 3 次执行(phase=action),第 4 次写 skip
    (ppr_retrieve_cap)。flag 保持默认开(seed 跑,但 seed 是 phase=seed,按 phase 过滤
    不污染动作计数)。抬高 stale_limit 防『动作 0 新增→stale 早熔断』掩盖 cap。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "reasoning_stale_limit", 99)
    repo._reasoning_llm_client = _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": f"q{i}"} for i in range(4)]
        + [{"next_action": "answer", "sufficient": True}])
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "对比题")
    ppr_actions = [s for s in result.trace
                   if s.step_type == "ppr" and s.detail.get("phase") == "action"]
    caps = [s for s in result.trace
            if s.step_type == "skip" and s.detail.get("reason") == "ppr_retrieve_cap"]
    assert len(ppr_actions) == 3       # 上限 _MAX_PPR_RETRIEVES=3
    assert len(caps) >= 1              # 第 4 次被 cap


def test_ppr_retrieve_action_skipped_when_flag_off(repo, monkeypatch):
    """flag 关 → 即便 agent 显式选 ppr_retrieve 也被 skip(ppr_disabled),不跑 PPR。
    honors GRAPH_PPR_ENABLED 作为 reasoning 的 PPR 总开关(off=零 PageRank)。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo._reasoning_llm_client = _ScriptedReflectLLM(
        reflects=[{"next_action": "ppr_retrieve", "ppr_query": "q"},
                  {"next_action": "answer", "sufficient": True}])
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "对比题")
    assert any(s.step_type == "skip" and s.detail.get("reason") == "ppr_disabled"
               for s in result.trace)
    assert result.chunks == []


def test_answer_context_id_offset_shifts_keys(repo):
    """加 id_offset 后 KG 键从 k1001 起(与 chunk 段 k1..N 不撞)。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    assert hits
    block, id_map = repo._answer_context(nb.id, hits, id_offset=repo._MIX_KG_KEY_BASE)
    assert all(int(k[1:]) > repo._MIX_KG_KEY_BASE for k in id_map)   # k1001+
    # 默认 offset=0 保持旧行为
    _b0, id0 = repo._answer_context(nb.id, hits)
    assert "k1" in id0


def test_answer_reasoning_mixes_chunks_as_citable(repo):
    """chunks 非空 → 上下文含 chunk 段、id_map 含 chunk 锚(object_type=chunk)、
    答案 [k1] 解析为 chunk 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts")
    assert chunks

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "跨文档证据 [k1].", "grounded": True})
    repo._reasoning_llm_client = _Echo()

    answer, grounded, anchors = repo._answer_reasoning(
        nb.id, "对比 MoE", hits, [], chunks=chunks)
    assert anchors and anchors[0].object_type == "chunk"   # k1 = chunk 段一等引用


def test_answer_reasoning_empty_chunks_unchanged(repo):
    """chunks 为空 → 走旧 KG-only 路径,k1 是 KG 锚。"""
    nb = _seed_two_doc_moe(repo)
    hits = repo._retrieve_scored(nb.id, "Mixture-of-Experts")[:2]

    class _Echo:
        configured = True
        def chat_json(self, messages, schema_hint, **kw):
            return json.dumps({"answer": "KG 证据 [k1].", "grounded": True})
    repo._reasoning_llm_client = _Echo()

    answer, grounded, anchors = repo._answer_reasoning(nb.id, "MoE", hits, [], chunks=None)
    assert anchors and anchors[0].object_type == "concept"  # k1 = KG 锚(旧行为)


def test_reasoning_ask_seed_grounds_in_cross_doc_chunk_end_to_end(repo):
    """端到端:flag 开 + reflect 只 answer(纯靠 seed pass)→ 跨文档 chunk 被升为可引用证据
    ([k1] 落在 chunk 段 → resp.anchors 含 chunk 锚),且 seed pass 在轨迹里。"""
    nb = _seed_two_doc_moe(repo)
    repo.llm_client = _AnswerOnlyLLM()
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    resp = repo.ask(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="reasoning"))
    assert resp.mode == "reasoning"
    assert any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))   # seed pass 跑了
    assert any(a.object_type == "chunk" for a in resp.anchors)               # 跨文档 chunk 成了可引用证据


def test_reasoning_ask_flag_off_no_ppr(repo, monkeypatch):
    """flag 关 → 无 ppr 轨迹,回到今天行为(无跨文档 chunk 注入)。"""
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo.llm_client = _AnswerOnlyLLM()
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    resp = repo.ask(nb.id, AskRequest(question="MoE", mode="reasoning"))
    assert not any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))


def test_chunk_relevance_within_unit_interval(repo):
    """守 [0,1]:reasoning 累积的 chunk relevance 全在单位区间。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    result = ReasoningRetriever.from_repository(repo, repo.settings).run(nb.id, "MoE 对比")
    assert all(0.0 <= c.relevance <= 1.0 for c in result.chunks)
