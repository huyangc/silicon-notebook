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
