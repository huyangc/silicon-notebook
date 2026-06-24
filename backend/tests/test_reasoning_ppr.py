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


def test_reflect_decision_has_ppr_query():
    from app.services.reasoning_retrieval import ReflectDecision
    assert ReflectDecision().ppr_query == ""


def test_reasoning_result_has_chunks():
    from app.services.reasoning_retrieval import ReasoningResult
    assert ReasoningResult().chunks == []


def test_ppr_retrieve_wrapper_delegates_cross_doc(repo):
    """薄封装委托 repo._ppr_retrieve:问 DeepSeek 的 MoE,经概念簇桥接到 GLM 那篇的 cB。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    rr = ReasoningRetriever(repo, repo.settings)
    chunks = rr.ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts architecture")
    ids = {c.chunk_id for c in chunks}
    assert "cA" in ids and "cB" in ids
    assert all(0.0 <= c.relevance <= 1.0 for c in chunks)


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

    class _Repo:
        def __init__(self): self.reasoning_llm_client = _LLM()

    rr = ReasoningRetriever(_Repo(), Settings(_env_file=None))
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
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ids = {c.chunk_id for c in result.chunks}
    assert "cA" in ids and "cB" in ids               # seed pass 拉到跨文档 chunk
    assert any(s.step_type == "ppr" for s in result.trace)


def test_run_no_seed_when_flag_off(repo, monkeypatch):
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyLLM()
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    assert result.chunks == []
    assert not any(s.step_type == "ppr" for s in result.trace)
