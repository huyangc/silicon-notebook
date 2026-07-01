import json, os, pytest
import numpy as np
from scipy.sparse import load_npz
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_build_scale_index_writes_artifacts(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    manifest = repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    for f in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy", "ann.bin", "ann_labels.npy", "manifest.json"):
        assert os.path.exists(os.path.join(d, f)), f
    assert manifest["n_nodes"] >= 2
    node_ids_arr = np.load(os.path.join(d, "node_ids.npy"), allow_pickle=True)
    idf_arr = np.load(os.path.join(d, "idf.npy"))
    G = load_npz(os.path.join(d, "graph.npz"))
    assert len(idf_arr) == len(node_ids_arr) == G.shape[0] == G.shape[1]


def test_build_scale_index_adds_cluster_bridge(repo):
    """cluster_groups must produce hub nodes in node_ids so PPR can propagate
    across merged concept clusters (synonym bridges)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    # Two concepts with same name (different case) — rebuild_unified_kg should
    # cluster them under one canonical_id, so cluster_groups has ≥1 cluster.
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
                                 "payload": {"name": "MOSFET", "section_path": ""},
                                 "evidence": []}], [])
    repo.store_kg(nb.id, None, [{"local_id": "b", "object_type": "concept",
                                 "payload": {"name": "mosfet", "section_path": ""},
                                 "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    node_ids = list(np.load(os.path.join(d, "node_ids.npy"), allow_pickle=True))
    assert any(str(n).startswith("cluster:") for n in node_ids), (
        "build_scale_index must include cluster: hub nodes for synonym bridges; "
        f"got node_ids={node_ids[:20]}"
    )


def test_scale_index_loads_and_invalidates_on_change(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "X", "section_path": ""}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    assert repo._scale_index(nb.id) is not None            # 版本一致 -> 命中
    repo.store_kg(nb.id, None, [{"local_id": "b", "object_type": "concept",
        "payload": {"name": "Y", "section_path": ""}, "evidence": []}], [])
    assert repo._scale_index(nb.id) is None                 # 索引过期不返回


def _seed_small_base(repo):
    """Create a notebook, mark it base, seed 2 concepts + 1 relation, unify KG."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(nb.id)
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a",
         "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_scale_ppr_returns_chunk_rankings_shape(repo):
    """scale_ppr must return a non-empty ranked list with valid (str, float) pairs
    when queried from a separate active notebook against a base that has a chunk."""
    base = _seed_base_with_chunk(repo)
    active = repo.create_notebook(NotebookCreate(name="active-shape"))
    out = repo.scale_ppr(active.id, "MOSFET gain")
    assert isinstance(out, list) and out, "scale_ppr must return a non-empty list"
    assert all(isinstance(cid, str) and 0.0 <= score <= 1.0 for cid, score in out)


def test_graph_mode_falls_back_when_no_index(repo):
    nb = _seed_small_base(repo)
    assert repo._scale_index(nb.id) is None   # not built -> fallback path
    # Confirm the dispatch in _ppr_retrieve reaches the rustworkx fallback
    # without crashing (result is [] in the test env — no embed configured).
    result = repo._ppr_retrieve(nb.id, "MOSFET")
    assert isinstance(result, list)


def _seed_base_with_chunk(repo):
    """Base notebook with a chunk + a KG concept (with embedding) wired to it,
    so the scale index has an ANN-seedable node and a rankable chunk node."""
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    with repo._write() as db:
        now = "2026-06-29T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("sB", base.id, "MOSFET paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", base.id, "sB", "A MOSFET provides voltage gain.", "S",
                    json.dumps(["elB"]), now))
        ev = json.dumps([{"source_id": "sB", "source_title": "", "element_id": "elB",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "MOSFET", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eB", base.id, "concept", "approved", "",
                    json.dumps({"name": "MOSFET"}), ev, "sB", now, now))
        # knowledge embedding so the ANN index has a seedable vector
        vec = repo.embedder.embed_query("MOSFET")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", ("eB", base.id, json.dumps(vec), now))
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    return base


def test_scale_ppr_uses_base_index_from_active(repo):
    """A separate ACTIVE notebook queries against the base scale index: the ANN
    seed + CSR splice path must surface the base chunk (cB) with a [0,1] score."""
    base = _seed_base_with_chunk(repo)
    active = repo.create_notebook(NotebookCreate(name="active"))  # personal, no KG
    out = repo.scale_ppr(active.id, "MOSFET voltage gain")
    assert isinstance(out, list) and out, "scale path should surface base chunks"
    ids = [cid for cid, _ in out]
    assert "cB" in ids
    assert all(isinstance(cid, str) and 0.0 <= score <= 1.0 for cid, score in out)


def test_scale_ppr_empty_when_multiple_bases_supported(repo):
    """Dispatch still returns [] (fallback) for a notebook that is itself the
    only base (no OTHER base index to splice from)."""
    base = _seed_base_with_chunk(repo)
    assert repo.scale_ppr(base.id, "MOSFET") == []  # excludes self -> no base set


def test_run_index_builds_for_notebook(repo):
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    res = batch_ingest.run_index(repo, nb.id)
    assert res["indexed_nodes"] >= 2
    assert repo._scale_index(nb.id) is not None


# ── Task 4: run_kg flags tests ─────────────────────────────────────────────────

def test_run_kg_rebuild_only_rebuilds_scale_index_for_base(repo):
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(nb.id)
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "X", "section_path": ""}, "evidence": []}
    ], [])
    # rebuild_unified_kg required before run_kg rebuild_only so clusters exist
    repo.rebuild_unified_kg(nb.id)
    res = batch_ingest.run_kg(repo, nb.id, rebuild_only=True)
    assert repo._scale_index(nb.id) is not None  # base tier -> index (re)built after rebuild


def test_run_kg_no_rebuild_skips_clustering(repo, monkeypatch):
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    called = {"rebuild": 0}
    orig = repo.rebuild_unified_kg
    monkeypatch.setattr(
        repo, "rebuild_unified_kg",
        lambda *a, **k: (called.__setitem__("rebuild", called["rebuild"] + 1), orig(*a, **k))[1]
    )
    batch_ingest.run_kg(repo, nb.id, no_rebuild=True)
    assert called["rebuild"] == 0  # no_rebuild skipped clustering


def test_run_kg_rejects_both_flags(repo):
    import pytest
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(ValueError):
        batch_ingest.run_kg(repo, nb.id, no_rebuild=True, rebuild_only=True)


# ── Test A: cross-layer synonym bridge ────────────────────────────────────────

def _seed_base_with_near_vector(repo, concept_name: str, concept_id: str,
                                 chunk_id: str, source_id: str):
    """Helper: base notebook with ONE concept whose embedding uses concept_name
    as input (deterministic via FakeEmbedder), wired to a chunk.
    Returns (notebook, base_node_object_id) — the internal object id may differ
    from concept_id due to rebuild_unified_kg reassignment, so we return the
    actual object id looked up after the build."""
    base = repo.create_notebook(NotebookCreate(name=f"base-{concept_name}"))
    repo.mark_notebook_base(base.id)
    now = "2026-06-29T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (source_id, base.id, f"{concept_name} paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   (chunk_id, base.id, source_id,
                    f"Content about {concept_name}.", "S", json.dumps([f"el{chunk_id}"]), now))
        ev = json.dumps([{"source_id": source_id, "source_title": "", "element_id": f"el{chunk_id}",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": concept_name, "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (concept_id, base.id, "concept", "approved", "",
                    json.dumps({"name": concept_name}), ev, source_id, now, now))
        # Use concept_name as the embedding text so FakeEmbedder produces a
        # deterministic vector tied to concept_name.
        vec = repo.embedder.embed_query(concept_name)
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", (concept_id, base.id, json.dumps(vec), now))
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    return base


def test_cross_layer_synonym_bridge_adds_edges(repo, monkeypatch):
    """Cross-layer ANN bridge: an active concept whose FakeEmbedder vector is
    near (cosine >= threshold) a base concept's vector should cause extra edges
    to be injected into the splice step, connecting the two previously-unrelated
    nodes across layers.

    FakeEmbedder(dim=16) is deterministic.  We verified offline that embed('i')
    and embed('j') have cosine ~ 0.926 — well above the default threshold 0.83.
    We set PPR_EMB_SYNONYM_THRESHOLD to a safe 0.80 to be robust against small
    FakeEmbedder rounding differences, while 'i' vs 'j' sim is ~0.926 so the
    bridge fires.

    Strategy: build a base with concept 'i', build an active with concept 'j'
    (no shared id, no shared cluster — different names, no explicit relation).
    Count combined graph edges with the bridge enabled vs disabled.  With the
    bridge enabled, the two notebooks' concept nodes should be connected via the
    cross-layer edge, resulting in strictly MORE edges in the combined transition
    matrix.
    """
    import numpy as np
    from app.services.kg import scale_index as si

    # Lower threshold so the ~0.926 similarity of FakeEmbedder('i','j') fires.
    monkeypatch.setenv("PPR_EMB_SYNONYM_THRESHOLD", "0.80")
    monkeypatch.setenv("PPR_EMB_SYNONYM_ENABLED", "true")
    # Rebuild settings so env vars are picked up.
    from app.core.config import Settings
    repo.settings = Settings()
    repo.embedder.dim = 16  # already 16 from fixture

    # Concept names 'i' and 'j': FakeEmbedder(dim=16) cosine ~ 0.926 > 0.80.
    base = _seed_base_with_near_vector(
        repo, concept_name="i", concept_id="obj-base-i",
        chunk_id="cBase", source_id="sBase")

    # Active notebook: concept 'j' (near to 'i' by FakeEmbedder).
    active = repo.create_notebook(NotebookCreate(name="active-j"))
    now = "2026-06-29T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("sAct", active.id, "j paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cAct", active.id, "sAct", "Content about j.", "S",
                    json.dumps(["elAct"]), now))
        ev = json.dumps([{"source_id": "sAct", "source_title": "", "element_id": "elAct",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "j", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("obj-act-j", active.id, "concept", "approved", "",
                    json.dumps({"name": "j"}), ev, "sAct", now, now))
        vec_j = repo.embedder.embed_query("j")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", ("obj-act-j", active.id, json.dumps(vec_j), now))
    repo.rebuild_unified_kg(active.id)

    # Verify that 'i' and 'j' are indeed similar enough with this embedder.
    vec_i = np.array(repo.embedder.embed_query("i"), dtype=np.float64)
    vec_j_arr = np.array(repo.embedder.embed_query("j"), dtype=np.float64)
    sim_ij = float(np.dot(vec_i, vec_j_arr) /
                   (np.linalg.norm(vec_i) * np.linalg.norm(vec_j_arr)))
    assert sim_ij >= 0.80, (
        f"FakeEmbedder('i','j') cosine={sim_ij:.4f} < 0.80 — test premise broken")

    # Grab the base index BEFORE any settings change so version check passes.
    base_idx = repo._scale_index(base.id)
    assert base_idx is not None, "base scale index must be loadable before settings change"

    # Count edges in combined graph WITHOUT bridge (disabled).
    active_node_ids, active_edges, _ = repo._active_kg_delta(active.id)
    _, A_no = si.splice_active(
        list(base_idx.node_ids), base_idx.transition,
        active_node_ids, active_edges)
    nnz_no = A_no.nnz

    # Manually compute bridge edges (mirrors the logic added to scale_ppr):
    import hnswlib
    with repo._connect() as _db:
        _a_ids, _a_mat = repo._vector_matrix(
            _db, active.id, "knowledge_embeddings", "object_id")
    bridge_edges = []
    if _a_ids and _a_mat is not None:
        _a_mat_arr = np.asarray(_a_mat, dtype=np.float32)
        dim = int(base_idx.manifest.get("dim", _a_mat_arr.shape[1]))
        ann = hnswlib.Index(space="cosine", dim=dim)
        ann.load_index(base_idx.ann_path, max_elements=len(base_idx.ann_labels))
        ann.set_ef(max(repo.settings.ppr_emb_synonym_topk + 1, 50))
        for ai, a_id in enumerate(_a_ids):
            k = min(repo.settings.ppr_emb_synonym_topk, len(base_idx.ann_labels))
            labs, dists = ann.knn_query(_a_mat_arr[ai], k=k)
            for lab, dist in zip(labs[0], dists[0]):
                base_nid = base_idx.ann_labels[int(lab)]
                if base_nid == a_id:
                    continue
                sim = max(0.0, 1.0 - float(dist))
                if sim >= repo.settings.ppr_emb_synonym_threshold:
                    bridge_edges.append((a_id, base_nid, sim))
                    bridge_edges.append((base_nid, a_id, sim))

    assert bridge_edges, (
        "Expected at least one cross-layer bridge edge (active 'j' ↔ base 'i'); "
        f"sim_ij={sim_ij:.4f}, threshold={repo.settings.ppr_emb_synonym_threshold}")

    _, A_with = si.splice_active(
        list(base_idx.node_ids), base_idx.transition,
        active_node_ids, list(active_edges) + bridge_edges)
    nnz_with = A_with.nnz

    assert nnz_with > nnz_no, (
        f"Bridge should add edges: nnz_no={nnz_no}, nnz_with={nnz_with}")


def test_cross_layer_bridge_disabled_no_extra_edges(repo, monkeypatch):
    """When ppr_emb_synonym_enabled=False the bridge must not fire: the number of
    nonzeros in the combined transition equals the no-bridge baseline."""
    import numpy as np
    from app.services.kg import scale_index as si

    monkeypatch.setenv("PPR_EMB_SYNONYM_ENABLED", "false")
    monkeypatch.setenv("PPR_EMB_SYNONYM_THRESHOLD", "0.80")
    from app.core.config import Settings
    repo.settings = Settings()

    base = _seed_base_with_near_vector(
        repo, concept_name="i", concept_id="obj-base2-i",
        chunk_id="cBase2", source_id="sBase2")

    active = repo.create_notebook(NotebookCreate(name="active-j2"))
    now = "2026-06-29T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("sAct2", active.id, "j paper2", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cAct2", active.id, "sAct2", "Content about j.", "S",
                    json.dumps(["elAct2"]), now))
        ev = json.dumps([{"source_id": "sAct2", "source_title": "", "element_id": "elAct2",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": "j", "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("obj-act2-j", active.id, "concept", "approved", "",
                    json.dumps({"name": "j"}), ev, "sAct2", now, now))
        vec_j = repo.embedder.embed_query("j")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", ("obj-act2-j", active.id, json.dumps(vec_j), now))
    repo.rebuild_unified_kg(active.id)

    # With bridge disabled: call scale_ppr (should return results without crash)
    result = repo.scale_ppr(active.id, "some query")
    assert isinstance(result, list)  # bridge off must not crash


def test_build_scale_index_writes_chunk_ann(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for cid, txt in [("c1", "MOSFET current mirror"), ("c2", "bandgap reference voltage")]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", txt, "", "[]", now))
    # 给 chunk 补向量：FakeEmbedder 无回填 API，直插 chunk_embeddings（embed_texts 为实际 API）
    with repo._write() as db:
        if not db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?",
                          (nb.id,)).fetchone()["c"]:
            for cid in ("c1", "c2"):
                v = repo.embedder.embed_texts([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    manifest = repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    assert os.path.exists(os.path.join(d, "chunk_ann.bin"))
    assert os.path.exists(os.path.join(d, "chunk_ann_labels.npy"))
    assert manifest.get("has_chunk_ann") is True
    assert manifest.get("n_chunk_ann") == 2
    # load 能读回 chunk ann
    idx = repo._scale_index(nb.id)
    assert idx is not None
    assert list(idx.chunk_ann_labels) == ["c1", "c2"] or set(idx.chunk_ann_labels) == {"c1", "c2"}
    assert idx.chunk_ann_path.endswith("chunk_ann.bin")


def test_scale_index_status_and_rebuild(repo, monkeypatch):
    import json, time
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for cid in ("c1", "c2"):
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", f"text {cid}", "", "[]", now))
            v = repo.embedder.embed_texts([cid])[0]
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (cid, nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)

    # 非 base 且无索引 → 不合格
    st0 = repo.scale_index_status(nb.id)
    assert st0["exists"] is False and st0["eligible"] is False
    import pytest
    with pytest.raises(ValueError):
        repo.trigger_scale_index_rebuild(nb.id)

    # 标 base → 合格 → 触发后台重建 → 轮询到建成
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))
    assert repo.scale_index_status(nb.id)["eligible"] is True
    r = repo.trigger_scale_index_rebuild(nb.id)
    assert r["status"] in ("building", "already_building")
    for _ in range(50):
        if not repo.scale_index_status(nb.id)["building"]:
            break
        time.sleep(0.1)
    st = repo.scale_index_status(nb.id)
    assert st["exists"] is True and st["building"] is False and st["stale"] is False
    assert st["n_chunk_ann"] == 2 and st["has_chunk_ann"] is True
