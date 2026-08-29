import json
from types import SimpleNamespace

import numpy as np
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


def test_ppr_settings_defaults_off():
    s = Settings(_env_file=None)
    assert s.graph_ppr_enabled is True   # graph 模式默认走 PPR(2026-06-24)
    assert s.ppr_damping == 0.5
    assert s.ppr_passage_node_weight == 0.05
    assert s.ppr_top_chunks == 20
    assert s.ppr_fact_rerank_enabled is False


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_two_doc_moe(repo, suffix=""):
    """Two sources, each with an MoE concept node clustered together, each
    node's evidence pointing at a chunk in its own source.

    `suffix` namespaces the globally-unique ids (source/chunk/object/cluster)
    so the helper can seed a second notebook in the same DB without colliding.
    The concept canonical_id stays "K-moe" (shared) so cross-notebook concept
    bridging still works. Default suffix="" keeps existing callers byte-identical.
    """
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    sfx = suffix
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        src_a, src_b = f"src-A{sfx}", f"src-B{sfx}"
        cA, cB = f"cA{sfx}", f"cB{sfx}"
        e1, e2 = f"e1{sfx}", f"e2{sfx}"
        elA, elB = f"elA{sfx}", f"elB{sfx}"
        for sid, title in [(src_a, "DeepSeek paper"), (src_b, "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (cA, nb.id, src_a, "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps([elA]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (cB, nb.id, src_b, "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps([elB]), now))
        for oid, sid, el in [(e1, src_a, elA), (e2, src_b, elB)]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in (e1, e2):
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


class _OrderedMembershipSet(set):
    def __init__(self, values):
        super().__init__(values)
        self._iteration_order = tuple(values)

    def __iter__(self):
        return iter(self._iteration_order)


def test_runtime_ppr_memberships_and_complete_graph_ignore_set_iteration(
    repo, monkeypatch
):
    import app.services.kg.ppr as ppr_module

    notebook = _seed_two_doc_moe(repo)
    memberships_seen = []
    real_build = ppr_module.build_ppr_graph

    def recording_build(nodes, chunks, relations, memberships, clusters, **kwargs):
        memberships_seen.append(list(memberships))
        return real_build(
            nodes, chunks, relations, memberships, clusters, **kwargs
        )

    monkeypatch.setattr(ppr_module, "build_ppr_graph", recording_build)
    current = {
        "value": {
            "e2": _OrderedMembershipSet(("cB", "cA")),
            "e1": _OrderedMembershipSet(("cB", "cA")),
        }
    }
    monkeypatch.setattr(
        repo.retrieval.graph,
        "_ent_chunk_map",
        lambda _notebook_id: current["value"],
    )

    first = repo._ppr_graph(notebook.id)
    repo._vector_cache.invalidate(f"{notebook.id}:ppr_graph")
    current["value"] = {
        "e1": _OrderedMembershipSet(("cA", "cB")),
        "e2": _OrderedMembershipSet(("cA", "cB")),
    }
    second = repo._ppr_graph(notebook.id)

    expected_memberships = [
        ("e1", "cA"),
        ("e1", "cB"),
        ("e2", "cA"),
        ("e2", "cB"),
    ]
    assert memberships_seen == [expected_memberships, expected_memberships]

    def complete_snapshot(result):
        graph, key_to_idx, chunk_idx_to_id = result
        return (
            list(graph.nodes()),
            graph.weighted_edge_list(),
            list(key_to_idx.items()),
            list(chunk_idx_to_id.items()),
        )

    assert complete_snapshot(first) == complete_snapshot(second)


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


def test_ppr_reset_vector_seeds_entities_and_chunks(repo):
    nb = _seed_two_doc_moe(repo)
    G, key_to_idx, chunk_idx_to_id = repo._ppr_graph(nb.id)
    reset = repo._ppr_reset_vector(nb.id, "Mixture-of-Experts (MoE)", key_to_idx)
    assert isinstance(reset, dict) and reset
    assert all(w > 0 for w in reset.values())
    ent_idxs = {key_to_idx["e1"], key_to_idx["e2"]}
    chunk_idxs = {key_to_idx["chunk:cA"], key_to_idx["chunk:cB"]}
    assert ent_idxs & set(reset)
    assert chunk_idxs & set(reset)


def _seed_hub_vs_rare(repo):
    """eH 出现在 3 个 chunk,e1 出现在 1 个;二者都含 query 关键词 'Attention'。"""
    nb = repo.create_notebook(NotebookCreate(name="hub"))
    with repo._write() as db:
        now = "2026-06-23T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-H", nb.id, "paper", "md", "ready", now, now))
        for cid, el in [("h1", "eh1"), ("h2", "eh2"), ("h3", "eh3"), ("r1", "er1")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (cid, nb.id, "src-H", "Attention mechanism.", "S", json.dumps([el]), now))
        def _ev(els):
            return json.dumps([{"source_id": "src-H", "source_title": "", "element_id": e,
                                "element_type": "paragraph", "location_label": "p",
                                "quoted_span": "Attention", "confidence": 1.0} for e in els])
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eH", nb.id, "concept", "approved", "", json.dumps({"name": "Attention"}),
                    _ev(["eh1", "eh2", "eh3"]), "src-H", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("e1", nb.id, "concept", "approved", "", json.dumps({"name": "Attention"}),
                    _ev(["er1"]), "src-H", now, now))
        for oid in ("eH", "e1"):
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"cl-{oid}", nb.id, f"K-{oid}", oid, "Attention", "concept", now))
    return nb


def test_specificity_divides_hub_entity_by_chunk_count(repo):
    nb = _seed_hub_vs_rare(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    q = "Attention"
    rel = {h.object_id: h.relevance for h in repo.federated_retrieve(nb.id, q)}
    reset = repo._ppr_reset_vector(nb.id, q, key_to_idx)
    iH, i1 = key_to_idx["eH"], key_to_idx["e1"]
    assert reset[iH] == rel["eH"] / 3      # eH 在 3 chunk → 降权
    assert reset[i1] == rel["e1"]           # e1 在 1 chunk → 不变


class _FilterLLM:
    """recognition-memory stub:固定只保留 'ekeep'(模拟 LLM 判定 edrop 无关)。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        return '{"relevant_ids": ["ekeep"]}'


def _seed_relevant_irrelevant(repo):
    nb = repo.create_notebook(NotebookCreate(name="rr"))
    with repo._write() as db:
        now = "2026-06-23T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("src-R", nb.id, "p", "md", "ready", now, now))
        for cid, el in [("ck", "elk"), ("cd", "eld")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "src-R", "topic", "S", json.dumps([el]), now))
        def _ev(e): return json.dumps([{"source_id": "src-R", "source_title": "", "element_id": e,
                                        "element_type": "paragraph", "location_label": "p",
                                        "quoted_span": "topic", "confidence": 1.0}])
        for oid, nm, el in [("ekeep", "topic keep", "elk"), ("edrop", "topic drop", "eld")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": nm}), _ev(el), "src-R", now, now))
        for oid in ("ekeep", "edrop"):
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"cl-{oid}", nb.id, f"K-{oid}", oid, "topic", "concept", now))
    return nb


def test_fact_rerank_filters_irrelevant_seed(repo, monkeypatch):
    nb = _seed_relevant_irrelevant(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    monkeypatch.setattr(repo.settings, "ppr_fact_rerank_enabled", True)
    bind_chat_client(repo, "evidence_refine", _FilterLLM())
    reset = repo._ppr_reset_vector(nb.id, "topic", key_to_idx)
    assert key_to_idx["ekeep"] in reset
    assert key_to_idx["edrop"] not in reset


def test_fact_rerank_fail_open_when_no_llm(repo, monkeypatch):
    nb = _seed_relevant_irrelevant(repo)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    monkeypatch.setattr(repo.settings, "ppr_fact_rerank_enabled", True)
    class _Down: configured = False
    bind_chat_client(repo, "evidence_refine", _Down())
    reset = repo._ppr_reset_vector(nb.id, "topic", key_to_idx)
    assert key_to_idx["ekeep"] in reset
    assert key_to_idx["edrop"] in reset


def test_precision_changes_do_not_touch_chunk_or_reasoning(repo):
    """fact-rerank 默认值正确,且 chunk(通用问答)与 reasoning
    模式不引用 PPR 路径 —— 隔离回归护栏。"""
    s = repo.settings
    assert s.ppr_fact_rerank_enabled is False
    import inspect
    from app.services.sqlite_repository import SQLiteRepository
    csrc = inspect.getsource(SQLiteRepository.ask_chunk)
    assert "_ppr_retrieve" not in csrc and "_ppr_reset_vector" not in csrc
    rsrc = inspect.getsource(SQLiteRepository.ask_reasoning)
    assert "_ppr_retrieve" not in rsrc and "_ppr_reset_vector" not in rsrc


def test_ppr_graph_variant_edges(repo):
    nb = repo.create_notebook(NotebookCreate(name="ver"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("s", nb.id, "p", "md", "ready", now, now))
        for oid, nm in [("v2", "DeepSeek-V2"), ("v3", "DeepSeek-V3")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": nm}), "[]", "s", now, now))
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{oid}", nb.id, f"K-{oid}", oid, nm, "concept", now))
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    assert key_to_idx["v3"] in set(G.successor_indices(key_to_idx["v2"]))  # variant edge built


def test_ppr_graph_federates_base_tier(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        for nb_id, oid, sid, cid, el in [(base.id, "eb", "sb", "cb", "elb"),
                                         (active.id, "ea", "sa", "ca", "ela")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb_id, "p", "md", "ready", now, now))
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb_id, sid, "MoE", "S", json.dumps([el]), now))
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb_id, "concept", "approved", "", json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{oid}", nb_id, "K-moe", oid, "MoE", "concept", now))
    G_on, idx_on, _ = repo._ppr_graph(active.id)
    assert "eb" in idx_on and "chunk:cb" in idx_on and "ea" in idx_on
    router = idx_on["cluster:K-moe"]
    assert {idx_on["ea"], idx_on["eb"]} <= set(G_on.successor_indices(router))


def test_ppr_graph_emb_synonym_edges(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="emb"))
    with repo._write() as db:
        now = "2026-06-24T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s", nb.id, "p", "md", "ready", now, now))
        vecs = {"e0": [1.0] + [0.0]*15, "e1": [1.0] + [0.0]*15, "e2": [0.0, 1.0] + [0.0]*14}
        for oid in ("e0", "e1", "e2"):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": oid}), "[]", "s", now, now))
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, json.dumps(vecs[oid]), now))
            db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"c{oid}", nb.id, f"K-{oid}", oid, oid, "concept", now))
    monkeypatch.setattr(repo.settings, "ppr_emb_synonym_enabled", True)
    G, key_to_idx, _ = repo._ppr_graph(nb.id)
    assert key_to_idx["e1"] in set(G.successor_indices(key_to_idx["e0"]))   # e0≈e1 → emb edge
    assert key_to_idx["e2"] not in set(G.successor_indices(key_to_idx["e0"]))  # orthogonal → none


def test_scale_ppr_caches_combined_graph(repo, monkeypatch):
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))

    active = _seed_two_doc_moe(repo, suffix="-act")
    repo.rebuild_unified_kg(active.id)
    repo.replace_notebook_bases(active.id, [base.id], "user-local")

    import app.services.kg.scale_index as si
    calls = {"n": 0}
    real_splice = si.splice_active
    def counting_splice(*a, **k):
        calls["n"] += 1
        return real_splice(*a, **k)
    monkeypatch.setattr(si, "splice_active", counting_splice)

    r1 = repo.scale_ppr(active.id, "Mixture of Experts")
    n_after_first = calls["n"]
    assert n_after_first > 0                     # 首查确有 splice(未命中缓存)
    r2 = repo.scale_ppr(active.id, "Mixture of Experts")
    assert calls["n"] == n_after_first          # 第二次命中缓存、不再 splice
    assert isinstance(r1, list) and isinstance(r2, list)


def test_self_only_combined_graph_reuses_scale_index_identity(repo):
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    repo._preload_scale_retrieval_artifacts()
    index = repo._scale_index(base.id, allow_stale=True)

    graph = repo.retrieval.graph._scale_combined_graph(base.id, [(base.id, index)])

    assert graph["combined_ids"] is index.node_ids
    assert graph["combined_index"] is index.node_index
    assert graph["combined_idf"] is index.idf
    assert graph["combined_A"] is index._ppr_transition
    assert graph["combined_chunk_ids"] is index._ppr_chunk_ids
    assert set(graph["combined_chunk_ids"]) == {
        index.node_ids[int(i)] for i in index.chunk_index
    }

    # The shared VectorCache may evict the wrapper during a long-lived process;
    # a miss must still be O(1) over the ScaleIndex-owned prepared core.
    repo._vector_cache.invalidate(f"{base.id}:scale_combined")
    rebuilt = repo.retrieval.graph._scale_combined_graph(
        base.id, [(base.id, index)]
    )
    assert rebuilt["combined_A"] is index._ppr_transition
    assert rebuilt["combined_chunk_ids"] is index._ppr_chunk_ids


def test_self_only_scale_ppr_preserves_float64_idf_seed_arithmetic(
    repo, monkeypatch
):
    base = repo.create_notebook(NotebookCreate(name="kb"))
    target_id = "e1"
    target_position = 0
    index = SimpleNamespace(
        ann_labels=[target_id],
        manifest={"dim": 16},
    )
    combined_idf = np.array([np.float32(1.0 / 3.0), 1.0], dtype=np.float32)
    similarity = 0.4465957211525855

    class Ann:
        def set_ef(self, _value):
            pass

        def knn_query(self, _query, k):
            assert k >= 1
            return [[0]], [[1.0 - similarity]]

    captured = {}

    def capture_ppr(_transition, reset, **_kwargs):
        captured["reset"] = reset.copy()
        return np.zeros_like(reset)

    service = repo.retrieval.graph
    monkeypatch.setattr(
        service,
        "_scale_index",
        lambda notebook_id, allow_stale=False: (
            index if notebook_id == base.id else None
        ),
    )
    monkeypatch.setattr(
        service,
        "_scale_combined_graph",
        lambda *_args, **_kwargs: {
            "combined_ids": [target_id, "cA"],
            "combined_A": SimpleNamespace(dtype=np.float64),
            "combined_index": {target_id: target_position, "cA": 1},
            "combined_chunk_ids": ["cA"],
            "combined_idf": combined_idf,
        },
    )
    monkeypatch.setattr(
        service, "_embed_query", lambda _question: [1.0] * 16
    )
    monkeypatch.setattr(service, "_open_scale_ann", lambda *_args: Ann())
    monkeypatch.setattr(service, "_retrieve_chunks", lambda *_args: ([], [], None))
    import app.services.kg.scale_index as scale_index

    monkeypatch.setattr(scale_index, "personalized_ppr", capture_ppr)

    assert service._scale_ppr_impl(base.id, "q") == []
    expected = similarity * float(np.float32(1.0 / 3.0))
    assert captured["reset"][target_position] == expected


def test_gather_kg_graph_source_scoping(repo):
    from app.models.schemas import NotebookCreate
    import json
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    def add(sid, oid, cid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, name, "", "[]", now))
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": cid,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": name, "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                       "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), ev, sid, now, now))
    add("s1", "o1", "c1", "alpha", 1)
    add("s2", "o2", "c2", "beta", 2)
    # 全库(默认 None)= 两个 source 都在
    node_ids, edges, chunk_ids, kg_ids, _ = repo._gather_kg_graph(nb.id)
    assert set(kg_ids) == {"o1", "o2"} and set(chunk_ids) == {"c1", "c2"}
    # 只取 s2 域 = 只有 o2/c2
    n2, e2, c2, k2, _ = repo._gather_kg_graph(nb.id, source_ids=["s2"])
    assert set(k2) == {"o2"} and set(c2) == {"c2"} and "o1" not in set(n2)
    # 空 source_ids = 空
    assert repo._gather_kg_graph(nb.id, source_ids=[]) == ([], [], [], [], {})


def test_gather_kg_graph_as_arrays_matches_string_path(repo):
    """Oracle test: as_arrays=True must produce the SAME graph as the default
    string path — same node_ids (order included, since chunk_index/idf in
    build_scale_index are positional against it), and edges that decode back
    to the identical (undirected, deduped, first-wins-weight) edge set. Uses
    _seed_two_doc_moe (2 sources, a relation-free concept cluster) plus an
    explicit knowledge_relations row so relations/memberships/hub edges are
    all exercised, and a *duplicate* relation row to prove first-wins dedup
    (both paths must keep the FIRST weight, not overwrite)."""
    import numpy as np
    nb = _seed_two_doc_moe(repo)
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("r1", nb.id, "src-A", "e1", "e2", "kind_of", "[]", now))
        # Duplicate relation (same unordered pair) — first-wins dedup must
        # keep the row processed first (SQLite returns insertion order here).
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("r2", nb.id, "src-A", "e2", "e1", "kind_of", "[]", now))

    node_ids_s, edges_s, chunk_ids_s, kg_ids_s, mc_s = repo._gather_kg_graph(nb.id)
    node_ids_a, (src_a, tgt_a, w_a), chunk_ids_a, kg_ids_a, mc_a = \
        repo._gather_kg_graph(nb.id, as_arrays=True)

    # node_ids identical (content AND order — array path indexes positionally).
    assert node_ids_a == node_ids_s
    assert chunk_ids_a == chunk_ids_s
    assert kg_ids_a == kg_ids_s
    assert mc_a == mc_s
    assert src_a.dtype == np.int32 and tgt_a.dtype == np.int32 and w_a.dtype == np.float32

    # Decode array edges back to (str,str,float) using the string path's
    # own node_ids order, and compare as SETS (both directions present in
    # both paths, so set-equality is the right equivalence check).
    decoded = {(node_ids_s[a], node_ids_s[b], float(w))
               for a, b, w in zip(src_a.tolist(), tgt_a.tolist(), w_a.tolist())}
    expected = {(a, b, float(w)) for a, b, w in edges_s}
    assert decoded == expected
    assert len(decoded) > 0  # sanity: graph actually has edges (relations+memberships+hub)
    # first-wins dedup: e1<->e2 duplicate relation collapsed to ONE undirected
    # pair (both directions present, but with the same single weight — 1.0).
    assert ("e1", "e2", 1.0) in decoded and ("e2", "e1", 1.0) in decoded


def test_gather_kg_graph_as_arrays_empty(repo):
    from app.models.schemas import NotebookCreate
    import numpy as np
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    node_ids, (src, tgt, w), chunk_ids, kg_ids, mc = repo._gather_kg_graph(nb.id, as_arrays=True)
    assert node_ids == [] and chunk_ids == [] and kg_ids == [] and mc == {}
    assert src.size == 0 and tgt.size == 0 and w.size == 0


def test_gather_kg_graph_as_arrays_source_scoped_empty(repo):
    import numpy as np
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="kb2"))
    node_ids, (src, tgt, w), chunk_ids, kg_ids, mc = \
        repo._gather_kg_graph(nb.id, source_ids=[], as_arrays=True)
    assert node_ids == [] and chunk_ids == [] and kg_ids == [] and mc == {}
    assert src.size == 0 and tgt.size == 0 and w.size == 0


def test_build_scale_index_does_not_populate_vector_cache(repo, monkeypatch):
    """Task 2 (memory diet): build_scale_index must load its kg/chunk
    embedding matrices DIRECTLY (COUNT-hinted build_matrix), never through
    _vector_matrix()/_vector_cache — a build's multi-GB matrices are
    single-use and must not become long-lived cache entries (they'd either
    evict useful query-time entries or just sit there after the build
    process/request is done). Assert no `{nb}:matrix:*` key appears in
    _vector_cache._store after a build. The QUERY path (_vector_matrix
    itself, used by e.g. ask_chunk/hybrid retrieval) is untouched and still
    caches — covered by other tests; this test only guards the build path."""
    nb = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(nb.id)
    monkeypatch.setattr(
        repo._runtime.retrieval_snapshots,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("full build must not read through RetrievalSnapshotCache")
        ),
    )
    before_keys = {k for k in repo._vector_cache._store if ":matrix:" in k}
    repo._runtime.scale_builder.build(nb.id)
    after_keys = {k for k in repo._vector_cache._store if ":matrix:" in k}
    new_keys = after_keys - before_keys
    assert not new_keys, f"build_scale_index must not populate VectorCache matrix entries: {new_keys}"


def test_scale_ppr_uses_self_index(repo, monkeypatch):
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    # 没有"别的" base —— 旧行为 base_indexes=[] → return [] → 回退 rustworkx
    import app.services.kg.ppr as ppr_mod
    called = {"n": 0}
    real = ppr_mod.build_ppr_graph
    monkeypatch.setattr(
        ppr_mod, "build_ppr_graph",
        lambda *a, **k: (called.__setitem__("n", called["n"] + 1), real(*a, **k))[1])
    ranked = repo.scale_ppr(base.id, "Mixture of Experts")
    assert ranked != []                         # self index 生效,非空
    # scale_ppr 自身不应触发 rustworkx build_ppr_graph(那是 _ppr_retrieve 的回退)
    assert called["n"] == 0


def test_scale_ppr_self_index_reaches_new_upload(repo, monkeypatch):
    """opt-in self-delta splice: with scale_search_include_delta=True, a
    newly-uploaded (post-watermark) source's chunk is still reachable via the
    self-delta splice onto the PPR combined graph. (Default is now OFF — see
    test_indexed_only_principle.py — so this test explicitly enables the
    opt-in to exercise the splice branch.)"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    base = _seed_two_doc_moe(repo)
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (base.id,))
    # build 后新上传一篇(delta),其 chunk 应能经 self-delta splice 参与 PPR
    import json
    with repo._write() as db:
        now = "2026-07-09T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s-new", base.id, "new", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c-new", base.id, "s-new", "Mixture of Experts routing", "", "[]", now))
        ev = json.dumps([{"source_id": "s-new", "source_title": "", "element_id": "c-new",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "MoE", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                   "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("o-new", base.id, "concept", "approved", "", json.dumps({"name": "Mixture-of-Experts (MoE)"}),
                    ev, "s-new", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
                   "canonical_name,object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("cl-new", base.id, "K-moe", "o-new", "Mixture-of-Experts (MoE)", "concept", now))
    ranked = repo.scale_ppr(base.id, "Mixture of Experts")
    assert "c-new" in {cid for cid, _ in ranked}   # 新上传 chunk 经 self-delta 可达
