"""T3: _rrf_scored dual-index relevance — element-sim must feed relevance."""
import pytest
from unittest.mock import patch
from app.services.retrieval import Evidence, RetrievedKnowledge
from tests.model_testkit import bind_embedding_client


def _make_repo(tmp_path, monkeypatch):
    """Minimal SQLiteRepository with tmp DB, no LLM, no embedder."""
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings(retrieval_rrf_k=60, retrieval_rrf_enabled=True)
    repo = SQLiteRepository(settings)
    bind_embedding_client(repo, FakeEmbedder(dim=16))
    return repo


def test_element_sim_raises_relevance_above_keyword_only(tmp_path, monkeypatch):
    """Object with NO knowledge-level embedding but a strong element-level sim must
    get relevance > the keyword-only baseline, i.e. has_vec=True and semantic
    reflects the element sim. This protects the dual-index best-of invariant in
    the RRF path (an object grounded only via an evidence-element embedding must
    not be downgraded to keyword-only relevance, which classify_evidence would
    then mark "inferred").

    Setup: one object with element_id='el-A', reachable via a partial keyword
    match (so it enters the RRF candidate set) but with NO object-level vector.
      knowledge_sims = {}            (no object-level vector hit)
      element_sims  = {'el-A': 0.9}  (strong element-level hit)

    Expected: relevance WITH element_sims is strictly greater than the SAME call
    WITHOUT element_sims (keyword-only), proving element_sims now feeds relevance.
    """
    repo = _make_repo(tmp_path, monkeypatch)
    ev = Evidence(
        element_id="el-A",
        source_id="src-x",
        source_title="src",
        element_type="paragraph",
        quoted_span="cascode stage Miller effect compensation",
        location_label="L1",
        confidence=1.0,
    )
    obj = {
        "id": "obj-1",
        "payload": {"name": "cascode"},
        "evidence": [ev],
        "status": "approved",
        "owner": "",
        "last_reviewed": "",
    }
    kg_objs = {"concept": [obj], "relation": [], "procedure": [], "parameter": []}
    knowledge_sims = {}          # NO object-level vector
    element_sims = {"el-A": 0.9}  # strong element-level match
    # Query overlaps ONE payload/evidence token ("cascode") out of five, so the
    # object enters the BM25/RRF candidate set while keyword_score stays low
    # (1/5 = 0.2). That keeps the keyword-only baseline modest so the element-sim
    # lift (0.9) is clearly visible in relevance.
    query = "cascode aaa bbb ccc ddd"

    # Baseline: same call WITHOUT element_sims -> keyword-only relevance.
    base = repo._rrf_scored(query, kg_objs, knowledge_sims)
    base_hit = next((r for r in base if r.object_id == "obj-1"), None)
    assert base_hit is not None, "obj-1 not in baseline results"

    # With element sim 0.9 injected, relevance must rise above the keyword-only
    # baseline (best-of picks the 0.9 element sim over the absent knowledge sim).
    results = repo._rrf_scored(query, kg_objs, knowledge_sims,
                               element_sims=element_sims)
    hit = next((r for r in results if r.object_id == "obj-1"), None)
    assert hit is not None, "obj-1 not in results"
    assert hit.relevance > base_hit.relevance, (
        f"relevance {hit.relevance:.4f} did not rise above keyword-only "
        f"baseline {base_hit.relevance:.4f} — element_sims not used"
    )


def test_knowledge_sim_still_works_in_rrf_path(tmp_path, monkeypatch):
    """Regression: object with only knowledge-level sim must still score correctly."""
    repo = _make_repo(tmp_path, monkeypatch)
    obj = {
        "id": "obj-2",
        "payload": {"name": "miller"},
        "evidence": [],
        "status": "approved",
        "owner": "",
        "last_reviewed": "",
    }
    kg_objs = {"concept": [obj], "relation": [], "procedure": [], "parameter": []}
    knowledge_sims = {"obj-2": 0.85}
    element_sims = {}

    results = repo._rrf_scored("miller compensation", kg_objs,
                                knowledge_sims, element_sims=element_sims)
    hit = next((r for r in results if r.object_id == "obj-2"), None)
    assert hit is not None
    assert hit.relevance > 0.1
