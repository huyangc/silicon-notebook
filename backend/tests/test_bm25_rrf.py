"""纯函数测试: bm25_scores / rrf_fuse + ask() RRF 分支集成测试。"""
from app.services.retrieval import bm25_scores, rrf_fuse


def test_bm25_ranks_specific_term_higher():
    docs = [
        ("d1", "cascode amplifier output resistance"),
        ("d2", "the the the the the the"),          # only stopword-ish/common
        ("d3", "cascode cascode current mirror"),
    ]
    s = bm25_scores("cascode", docs)
    # docs containing the query term score > 0; the unrelated doc is absent or 0
    assert s.get("d1", 0) > 0 and s.get("d3", 0) > 0
    assert "d2" not in s
    # d3 mentions cascode twice but saturates (BM25 tf saturation): still finite/ordered
    assert s["d3"] >= s["d1"] or s["d1"] >= s["d3"]   # both valid; just no crash


def test_bm25_empty_inputs():
    assert bm25_scores("", [("d1", "x")]) == {}
    assert bm25_scores("q", []) == {}


def test_rrf_fuse_combines_rankings():
    a = {"x": 0.9, "y": 0.1}          # ranking A: x best
    b = {"y": 0.9, "x": 0.1}          # ranking B: y best
    fused = rrf_fuse([a, b], k=60)
    # both appear once at rank1 and once at rank2 -> equal fused score
    assert abs(fused["x"] - fused["y"]) < 1e-9
    # a doc ranked #1 in both beats one ranked #2 in both
    fused2 = rrf_fuse([{"x": 1.0, "y": 0.0}, {"x": 1.0, "y": 0.0}])
    assert fused2["x"] > fused2["y"]


# --- 集成测试 ---

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate, AskRequest
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_rrf_path_ask_returns_relevant_hit(repo):
    """_retrieve_scored 在 retrieval_rrf_enabled=True 下走 RRF 分支不崩,且命中 cascode 相关 claim。
    P4-5: ask_fast 已退役,改为直接调 _retrieve_scored 验证 RRF 路径。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "cascode raises output resistance", "section_path": "1"},
         "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "current mirror copies current", "section_path": "1"},
         "evidence": []},
    ], [])

    repo.settings.retrieval_rrf_enabled = True
    hits = repo._retrieve_scored(nb.id, "cascode output resistance")
    # 返回正常结果,不崩
    assert hits is not None
    # 命中了与 cascode 相关的 claim A
    assert any(
        "cascode" in (h.payload.get("name") or "").lower() for h in hits
    ), "RRF path must surface the cascode claim"


def test_rrf_path_disabled_by_default(repo):
    """默认 retrieval_rrf_enabled=False,行为与原逻辑一致(smoke test)。"""
    assert repo.settings.retrieval_rrf_enabled is False
    nb = repo.create_notebook(NotebookCreate(name="nb2"))
    repo.store_kg(nb.id, None, [
        {"local_id": "X", "object_type": "claim",
         "payload": {"name": "gate oxide breakdown mechanism", "section_path": "1"},
         "evidence": []},
    ], [])
    resp = repo.ask(nb.id, AskRequest(question="gate oxide"))
    assert resp is not None


def test_rrf_scored_relevance_on_fused_scale_not_rrf(repo):
    # regression for the eval-found bug: _rrf_scored must put the [0,1] fused
    # keyword/semantic relevance on .relevance (for classify_evidence's tau),
    # NOT the tiny RRF score (which made every answer classify as "inferred").
    kg_objs = {
        "claim": [{"id": "o1", "payload": {"name": "cascode output resistance"},
                   "evidence": [], "status": "approved", "owner": "", "last_reviewed": ""}],
        "concept": [], "formula": [], "procedure": [],
    }
    hits = repo._rrf_scored("cascode output resistance", kg_objs, None)
    assert hits
    h = hits[0]
    assert h.relevance >= 0.35     # full keyword match -> crosses tau_high (grounded-capable)
    assert h.score < 0.1          # RRF micro-score, used only for ordering
    assert h.relevance != h.score


# --- _retrieve_scored RRF 分支(spy)测试 ---

def _seed_cascode_notebook(repo):
    """Seed a notebook with cascode/current-mirror claims and return its id."""
    nb = repo.create_notebook(NotebookCreate(name="spy-nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "cascode raises output resistance", "section_path": "1"},
         "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "current mirror copies current", "section_path": "1"},
         "evidence": []},
    ], [])
    return nb.id


def test_retrieve_scored_uses_rrf_when_enabled(repo, monkeypatch):
    """_retrieve_scored 在 retrieval_rrf_enabled=True 时必须委托给 _rrf_scored,
    且所有命中的 relevance 在 [0,1]。"""
    nb_id = _seed_cascode_notebook(repo)
    monkeypatch.setattr(repo.settings, "retrieval_rrf_enabled", True)
    calls = {}
    orig = repo._rrf_scored

    def _spy(query, kg_objs, knowledge_sims, element_sims=None):
        calls["hit"] = True
        return orig(query, kg_objs, knowledge_sims, element_sims)

    monkeypatch.setattr(repo, "_rrf_scored", _spy)
    hits = repo._retrieve_scored(nb_id, "cascode output resistance")
    assert calls.get("hit") is True, "_rrf_scored must be called when retrieval_rrf_enabled=True"
    assert all(0.0 <= h.relevance <= 1.0 for h in hits), "all relevance values must be in [0,1]"


def test_retrieve_scored_skips_rrf_when_disabled(repo, monkeypatch):
    """默认 retrieval_rrf_enabled=False 时,_rrf_scored 不应被调用。"""
    nb_id = _seed_cascode_notebook(repo)
    calls = {}

    def _spy(*a, **k):
        calls["hit"] = True
        return []

    monkeypatch.setattr(repo, "_rrf_scored", _spy)
    repo._retrieve_scored(nb_id, "cascode output resistance")  # default: retrieval_rrf_enabled=False
    assert "hit" not in calls, "_rrf_scored must NOT be called when retrieval_rrf_enabled=False"
