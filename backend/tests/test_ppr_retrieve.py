import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


def test_ppr_settings_defaults_off():
    s = Settings(_env_file=None)
    assert s.graph_ppr_enabled is False
    assert s.ppr_damping == 0.5
    assert s.ppr_passage_node_weight == 0.05
    assert s.ppr_top_chunks == 20


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """Two sources, each with an MoE concept node clustered together, each
    node's evidence pointing at a chunk in its own source."""
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


def test_ent_chunk_map(repo):
    nb = _seed_two_doc_moe(repo)
    m = repo._ent_chunk_map(nb.id)
    assert m["e1"] == {"cA"}
    assert m["e2"] == {"cB"}


def test_ppr_graph_has_cross_doc_bridge(repo):
    nb = _seed_two_doc_moe(repo)
    G, key_to_idx, chunk_idx_to_id = repo._ppr_graph(nb.id)
    assert set(chunk_idx_to_id.values()) == {"cA", "cB"}
    assert "cluster:K-moe" in key_to_idx
    router = key_to_idx["cluster:K-moe"]
    assert set(G.successor_indices(router)) == {key_to_idx["e1"], key_to_idx["e2"]}


def test_ppr_graph_cache_evicted_on_invalidate(repo):
    """_invalidate_unified_cache must remove the ppr_graph cache entry so that a
    same-second KG edit (unchanged version-tuple counts/timestamps) cannot serve
    a stale graph.  After invalidation a fresh _ppr_graph call rebuilds from DB.

    Proof strategy:
    1. Seed and prime the cache (call _ppr_graph once).
    2. While STILL INSIDE the same second, delete the concept_cluster rows so the
       underlying data changes but the version-tuple stays the same (same counts
       now = 0 for clusters, but we verify eviction, not version staleness).
    3. Call _invalidate_unified_cache.
    4. Assert the key is gone from _vector_cache._store (eviction test).
    5. Call _ppr_graph again — it must rebuild from the now-empty clusters table
       and return a graph with no cluster router node (proves it was NOT served
       from the pre-invalidation stale cache).

    This test FAILS if the production line
        self._vector_cache.invalidate(f"{notebook_id}:ppr_graph")
    is removed from _invalidate_unified_cache.
    """
    nb = _seed_two_doc_moe(repo)
    # Prime the cache — graph should have the cluster:K-moe router
    G1, key_to_idx1, _ = repo._ppr_graph(nb.id)
    assert "cluster:K-moe" in key_to_idx1, "pre-condition: cluster must be present"
    cache_key = f"{nb.id}:ppr_graph"
    assert cache_key in repo._vector_cache._store, "pre-condition: cache must be populated"

    # Delete all concept_clusters rows within the same second (version-tuple counts
    # for concept_clusters drop to 0, but MAX(created_at) becomes '' — if the cache
    # were NOT evicted explicitly, the stale (old version) entry would still be
    # served because the new version *is* different; the real danger is an
    # in-place SAME-COUNT same-timestamp edit, but eviction is the safeguard for
    # that.  We test the eviction itself directly.)
    with repo._write() as db:
        db.execute("DELETE FROM concept_clusters WHERE notebook_id=?", (nb.id,))

    # Invalidate the unified cache (this is the call under test)
    repo._invalidate_unified_cache(nb.id)

    # The cache key must be gone
    assert cache_key not in repo._vector_cache._store, (
        "ppr_graph cache entry must be evicted by _invalidate_unified_cache"
    )

    # A fresh call must rebuild from DB (no cluster_groups → no cluster router node)
    G2, key_to_idx2, _ = repo._ppr_graph(nb.id)
    assert "cluster:K-moe" not in key_to_idx2, (
        "after invalidation and cluster deletion, rebuilt graph must have no cluster router"
    )


def test_ppr_retrieve_surfaces_other_document(repo):
    """问 DeepSeek 的 MoE,PPR 应经同概念簇把 GLM 那篇的 chunk(cB)也召回。
    cC 是来自第三个无关源的对照组(无 MoE 词汇,不在任何概念簇),
    用于证明 cB 的召回是通过 cluster 桥接而非 dense 种子"碰巧"召回的:
    桥接证明参见 test_run_ppr_bridges_across_documents (test_ppr.py)。"""
    nb = _seed_two_doc_moe(repo)

    # 插入第三个无关源/chunk/实体作对照组(不入任何 concept_cluster)
    import json as _json
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("src-C", nb.id, "Quant paper", "md", "ready", now, now))
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("cC", nb.id, "src-C", "Quantization reduces memory footprint.",
             "Quant", _json.dumps(["elC"]), now))
        ev = _json.dumps([{"source_id": "src-C", "source_title": "", "element_id": "elC",
                           "element_type": "paragraph", "location_label": "p1",
                           "quoted_span": "Quantization", "confidence": 1.0}])
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("e3", nb.id, "concept", "approved", "",
             _json.dumps({"name": "Quantization"}), ev, "src-C", now, now))
        # e3 intentionally NOT added to concept_clusters — no bridge to MoE cluster

    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts architecture")
    ids = [c.chunk_id for c in chunks]
    assert "cA" in ids
    assert "cB" in ids                     # 关键:别的文档也进来了(桥接成功)
    assert all(0.0 <= c.relevance <= 1.0 for c in chunks)
    # The bridged chunk must score higher than the unbridged control (no path to MoE cluster)
    score = {c.chunk_id: c.relevance for c in chunks}
    assert score["cB"] > score.get("cC", 0.0), (
        "cB (bridged via cluster:K-moe) must outrank cC (no cluster bridge)"
    )


def test_ppr_retrieve_empty_when_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo._ppr_retrieve(nb.id, "anything") == []


class _StubAnswerLLM:
    configured = True
    def chat_json(self, *a, **k):
        return '{"answer": "DeepSeek 与 GLM 都用 MoE [k1][k2].", "grounded": true}'


def test_ask_graph_ppr_cites_multiple_documents(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", True)
    repo.llm_client = _StubAnswerLLM()
    repo._reasoning_llm_client = _StubAnswerLLM()
    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="graph"))
    assert resp.mode == "graph"
    src_ids = {c.source_id for c in resp.citations}
    assert "src-A" in src_ids and "src-B" in src_ids


def test_ask_graph_ppr_off_keeps_kg_path(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="MoE", mode="graph"))
    assert resp.mode == "graph"
    assert not any(s.step_type == "ppr" for s in (resp.reasoning_trace or []))
