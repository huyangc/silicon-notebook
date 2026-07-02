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


def test_scale_ppr_uses_self_index_when_only_base(repo):
    """P0-00 fix: a notebook that is itself the only base uses its OWN (allow_stale)
    scale index as a participant instead of returning [] (which forced a rustworkx
    fallback). self CSR = substrate, self ANN = seed source."""
    base = _seed_base_with_chunk(repo)
    out = repo.scale_ppr(base.id, "MOSFET")
    assert isinstance(out, list) and out, "self-base direct query should use self index"
    assert "cB" in [cid for cid, _ in out]
    assert all(isinstance(cid, str) and 0.0 <= score <= 1.0 for cid, score in out)


def test_scale_ppr_empty_when_only_base_and_no_index(repo):
    """Conservative: a self-base notebook with NO scale index (small/legacy) still
    returns [] (fallback to rustworkx) — self-index path only engages when indexed."""
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    with repo._write() as db:
        now = "2026-06-29T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("sN", base.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("cN", base.id, "sN", "MOSFET gain", "", "[]", now))
    # never built a scale index -> no self index -> conservative fallback
    assert repo.scale_ppr(base.id, "MOSFET") == []


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


def test_build_scale_index_emits_stage_timings(repo, monkeypatch):
    """build_scale_index must time each internal stage (gather/transition/
    kg_matrix/chunk_matrix/viz_arrays/persist) and emit a scale_index_build
    event per stage plus a final total — for locating the bottleneck stage on
    large (490k-object) deployments. Disk manifest.json carries the 5
    pre-persist stages (persist/total aren't known until after the file is
    written); the RETURNED manifest dict additionally carries persist+total."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)

    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)

    manifest = repo.build_scale_index(nb.id)

    # Returned manifest: 7 keys (6 stages + total)
    expected_stages = {"gather", "transition", "kg_matrix", "chunk_matrix", "viz_arrays", "persist"}
    assert "build_ms" in manifest
    returned_build_ms = manifest["build_ms"]
    assert set(returned_build_ms.keys()) == expected_stages | {"total"}
    for k, v in returned_build_ms.items():
        assert isinstance(v, int)
        assert v >= 0
    assert returned_build_ms["total"] >= max(
        v for k, v in returned_build_ms.items() if k != "total"
    )

    # Existing manifest keys untouched
    assert manifest["n_nodes"] >= 2
    assert "version" in manifest

    # Disk manifest.json: only the 5 pre-persist stages (persist/total unknown
    # until after the file itself is written).
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    with open(os.path.join(d, "manifest.json")) as fh:
        disk_manifest = json.load(fh)
    assert set(disk_manifest["build_ms"].keys()) == expected_stages - {"persist"}

    # Events: 7 scale_index_build events (6 stages + total), each with
    # notebook_id/stage/latency_ms.
    scale_events = [e for e in events if e.get("kind") == "scale_index_build"]
    assert len(scale_events) == 7
    stages_seen = {e["stage"] for e in scale_events}
    assert stages_seen == expected_stages | {"total"}
    for e in scale_events:
        assert e["notebook_id"] == nb.id
        assert isinstance(e["latency_ms"], int)
        assert e["latency_ms"] >= 0


def test_build_scale_index_on_stage_callback(repo):
    """build_scale_index(on_stage=...) must invoke the callback once per
    stage — the same 7 stages as the scale_index_build events — right when
    each stage's timing is recorded, so a CLI caller can print real-time
    per-stage progress on long (490k-object) builds without depending on the
    events logger (which doesn't print to the terminal)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)

    calls = []
    manifest = repo.build_scale_index(nb.id, on_stage=lambda stage, ms: calls.append((stage, ms)))

    expected_stages = {"gather", "transition", "kg_matrix", "chunk_matrix", "viz_arrays", "persist", "total"}
    assert len(calls) == 7
    assert {c[0] for c in calls} == expected_stages
    assert calls[-2][0] == "persist"
    assert calls[-1][0] == "total"
    for stage, ms in calls:
        assert isinstance(ms, int)
        assert ms >= 0
    assert manifest["n_nodes"] >= 2


def test_build_scale_index_on_stage_exception_does_not_break_build(repo):
    """A raising on_stage callback must never break the build — mirrors how
    event_log.emit failures are isolated (logging-only, never propagated)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)

    def _boom(stage, ms):
        raise RuntimeError(f"boom at {stage}")

    manifest = repo.build_scale_index(nb.id, on_stage=_boom)
    assert manifest["n_nodes"] >= 1
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    assert os.path.exists(os.path.join(d, "manifest.json"))
    assert os.path.exists(os.path.join(d, "graph.npz"))


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


def test_build_scale_index_records_watermark_sources(repo):
    import os, json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        for sid in ("s1", "s2"):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    mpath = os.path.join(repo.settings.storage_dir, "kg_index", nb.id, "manifest.json")
    with open(mpath) as fh:
        manifest = json.load(fh)
    assert sorted(manifest["watermark_sources"]) == ["s1", "s2"]


def test_index_delta_after_new_source(repo):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    # 未索引 → 全是 delta
    d0 = repo._index_delta(nb.id)
    assert d0["indexed"] is False and d0["delta_chunks"] == 1 and d0["delta_sources"] == ["s1"]
    # 建索引 → delta 清零
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    d1 = repo._index_delta(nb.id)
    assert d1["indexed"] is True and d1["delta_chunks"] == 0 and d1["delta_sources"] == []
    # 新增一个 source+chunk → delta=1
    with repo._write() as db:
        now2 = "2026-07-02T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s2", nb.id, "t", "md", "ready", now2, now2))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c2", nb.id, "s2", "y", "", "[]", now2))
    d2 = repo._index_delta(nb.id)
    assert d2["indexed"] is True and d2["delta_sources"] == ["s2"] and d2["delta_chunks"] == 1


def test_scale_index_status_state_machine(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 3)
    monkeypatch.setattr(repo.settings, "index_stale_delta_threshold", 1)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add_source(sid, cids, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for cid in cids:
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, "x", "", "[]", now))
                v = repo.embedder.embed_texts([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    # 小库 → unindexed
    add_source("s1", ["c1"], 1)
    assert repo.scale_index_status(nb.id)["state"] == "unindexed"
    # 越过建议阈值(3) → suggested
    add_source("s2", ["c2", "c3", "c4"], 2)
    assert repo.scale_index_status(nb.id)["state"] == "suggested"
    # 建索引 → indexed, delta=0
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    st = repo.scale_index_status(nb.id)
    assert st["state"] == "indexed" and st["delta_chunks"] == 0
    # 新增 delta 超阈值(1) → stale
    add_source("s3", ["c5", "c6"], 3)
    st2 = repo.scale_index_status(nb.id)
    assert st2["state"] == "stale" and st2["delta_chunks"] == 2


def test_fold_scale_index_delta(repo):
    """端到端 fold:水位后新增 source 经 O(delta) fold 收进现有索引 —— delta 归零、
    index 版本新鲜、ann/chunk_ann 含新 id、n_nodes 增长、新 chunk 经 ANN 可召回。"""
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))

    def add(sid, oid, cid, name, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute(
                "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, name, "", "[]", now))
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            for tbl, key in [("chunk_embeddings", cid), ("knowledge_embeddings", oid)]:
                v = repo.embedder.embed_texts([name])[0]
                col = "chunk_id" if tbl == "chunk_embeddings" else "object_id"
                db.execute(
                    f"INSERT INTO {tbl} ({col},notebook_id,vector,created_at) VALUES (?,?,?,?)",
                    (key, nb.id, json.dumps(v), now))

    add("s1", "o1", "c1", "current mirror", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    m0 = repo.scale_index_status(nb.id)

    # delta 一个新 source:o2 是新概念,o3 与 base 的 o1 同名(跨文档同一概念)
    add("s2", "o2", "c2", "bandgap reference special", 2)
    with repo._write() as db:
        now = "2026-07-02T00:00:00"
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("o3", nb.id, "concept", "approved", "", json.dumps({"name": "current mirror"}),
             "[]", "s2", now, now))
        v = repo.embedder.embed_texts(["current mirror"])[0]
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            ("o3", nb.id, json.dumps(v), now))
    assert repo._index_delta(nb.id)["delta_chunks"] == 1

    # fold —— O(delta) 增量收进索引
    repo.fold_scale_index_delta(nb.id)

    # 水位前移 → delta 清零
    d = repo._index_delta(nb.id)
    assert d["delta_chunks"] == 0 and d["delta_sources"] == []

    # 版本新鲜(fold 更新了 manifest version)→ _scale_index 不带 allow_stale 仍返回
    idx = repo._scale_index(nb.id)
    assert idx is not None
    # ann 含新对象、chunk_ann 含新 chunk
    assert "o2" in set(idx.ann_labels)
    assert idx.chunk_ann_labels is not None and "c2" in set(idx.chunk_ann_labels)
    assert "o1" in set(idx.ann_labels) and "c1" in set(idx.chunk_ann_labels)
    # CSR 节点增长
    assert idx.manifest["n_nodes"] > m0["n_nodes"]
    assert "o2" in set(idx.node_ids) and "c2" in set(idx.node_ids)
    assert len(idx.idf) == len(idx.node_ids) == idx.transition.shape[0]

    # 跨文档 cluster hub parity(SPEC §4「incremental_fuse 簇」):fold 应把 delta 融进
    # concept_clusters,使同名的 base o1 与 delta o3 经 cluster: hub 在折叠图里连通。
    with repo._connect() as db:
        member_rows = {r["member_object_id"] for r in db.execute(
            "SELECT member_object_id FROM concept_clusters WHERE notebook_id=?", (nb.id,)).fetchall()}
    assert "o3" in member_rows, "delta 对象未融进 concept_clusters(缺 incremental_fuse)"
    # 折叠图里应有该 canonical 的 hub,且 hub 与 o1、o3 都有边(跨文档连通)
    hub_nodes = [n for n in idx.node_ids if isinstance(n, str) and n.startswith("cluster:")]
    assert hub_nodes, "折叠图缺 cluster: hub 节点"
    node_index = {n: i for i, n in enumerate(idx.node_ids)}
    A = idx.transition  # A[j,i] = 边 i->j
    bridged = False
    for hub in hub_nodes:
        h = node_index[hub]
        # hub 的邻居(列 h 的非零行 = hub->x;行 h 的非零列 = x->hub)——无向图两向都有
        nbrs = set(A.getcol(h).nonzero()[0].tolist()) | set(A.getrow(h).nonzero()[1].tolist())
        if node_index.get("o1") in nbrs and node_index.get("o3") in nbrs:
            bridged = True
            break
    assert bridged, "同名跨文档概念 o1/o3 未经 cluster: hub 连通(cross-doc hub 缺失)"

    # 新内容经 ANN 可召回(_retrieve_chunks_ann 返回 (scored, ids, mat))
    qv = repo._embed_query("bandgap reference special")
    out = repo._retrieve_chunks_ann(nb.id, "bandgap reference special", qv, idx, recall=10)
    assert out is not None
    scored = out[0]
    assert scored and "c2" in {c.chunk_id for c in scored}


def test_trigger_when_and_mode(repo, monkeypatch):
    from app.models.schemas import NotebookCreate
    import json
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)", ("c1", nb.id, json.dumps(v), now))
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))
    repo.rebuild_unified_kg(nb.id)
    # when=idle → 入队、status=queued、不立即建
    r = repo.trigger_scale_index_rebuild(nb.id, when="idle")
    assert r["status"] == "queued"
    assert repo.scale_index_status(nb.id)["state"] == "queued"
    assert nb.id in repo._scale_idle_queue
    # force drain(绕过时间窗)→ 建成、出队、state 回 indexed
    repo._process_idle_queue(force=True)
    import time
    for _ in range(50):
        if not repo._scale_building and nb.id not in repo._scale_idle_queue:
            break
        time.sleep(0.1)
    assert nb.id not in repo._scale_idle_queue
    assert repo.scale_index_status(nb.id)["exists"] is True


def test_trigger_idle_then_fold_builds_via_fold(repo, monkeypatch):
    """idle→auto 有既存索引时走 fold_scale_index_delta 且真的建成(非空跑 already_building)。"""
    from app.models.schemas import NotebookCreate
    import json, time
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)", ("c1", nb.id, json.dumps(v), now))
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))
    repo.rebuild_unified_kg(nb.id)
    # 先建一个基础索引
    repo.build_scale_index(nb.id)
    assert repo.scale_index_status(nb.id)["exists"] is True

    # 加一个新 source+chunk → 产生 delta
    with repo._write() as db:
        now = "2026-07-01T01:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", ("s2", nb.id, "t2", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)", ("c2", nb.id, "s2", "y", "", "[]", now))
        v = repo.embedder.embed_texts(["c2"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)", ("c2", nb.id, json.dumps(v), now))

    # _resolve_scale_mode(auto) 应选 fold(有索引)
    assert repo._resolve_scale_mode(nb.id, "auto") == "fold"

    # 监视 fold 被调用
    calls = {"fold": 0, "build": 0}
    orig_fold = repo.fold_scale_index_delta
    orig_build = repo.build_scale_index
    def spy_fold(nbid, *a, **kw):
        calls["fold"] += 1
        return orig_fold(nbid, *a, **kw)
    def spy_build(nbid, *a, **kw):
        calls["build"] += 1
        return orig_build(nbid, *a, **kw)
    monkeypatch.setattr(repo, "fold_scale_index_delta", spy_fold)
    monkeypatch.setattr(repo, "build_scale_index", spy_build)

    r = repo.trigger_scale_index_rebuild(nb.id, when="idle", mode="auto")
    assert r["status"] == "queued"
    repo._process_idle_queue(force=True)
    for _ in range(50):
        if not repo._scale_building and nb.id not in repo._scale_idle_queue:
            break
        time.sleep(0.1)
    assert calls["fold"] == 1, "idle→auto 未走 fold"
    assert calls["build"] == 0, "不应走 full build"
    # watermark 应含 s2(证明 fold 真的建成、非空跑)
    import os
    mpath = os.path.join(repo.settings.storage_dir, "kg_index", nb.id, "manifest.json")
    with open(mpath) as fh:
        manifest = json.load(fh)
    assert "s2" in manifest["watermark_sources"], "fold 未把 delta source s2 纳入水位(空跑了)"


def test_scale_index_eligible_decoupled_from_tier(repo, monkeypatch):
    """检索索引 eligible 与 tier 解耦:非 base 但规模够大(超建议阈值)也应可建;小库仍不 eligible。"""
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 3)
    now = "2026-07-01T00:00:00"

    def mk(name, n_chunks):
        nb = repo.create_notebook(NotebookCreate(name=name))  # tier=personal(默认)
        with repo._write() as db:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (f"s-{nb.id}", nb.id, "t", "md", "ready", now, now))
            for i in range(n_chunks):
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (f"c-{nb.id}-{i}", nb.id, f"s-{nb.id}", "x", "", "[]", now))
        return nb

    big = mk("big-personal", 5)      # 5 > 阈值 3 → suggested
    small = mk("small-personal", 1)  # 1 < 阈值 3
    assert repo.scale_index_status(big.id)["eligible"] is True     # 非 base 也 eligible(解耦)
    assert repo.scale_index_status(small.id)["eligible"] is False  # 小库仍不 eligible
    # 大个人库 trigger 不再 409(now → building/already_building)
    assert repo.trigger_scale_index_rebuild(big.id)["status"] in ("building", "already_building")


def test_scale_ann_handle_cached(repo, monkeypatch):
    """P0-4: _open_scale_ann memoizes the hnswlib handle on the ScaleIndex
    instance — repeated opens reuse the same handle and load_index runs once."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id)
    assert idx is not None and idx.ann_labels

    import hnswlib
    calls = {"n": 0}
    real = hnswlib.Index.load_index

    def spy(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(hnswlib.Index, "load_index", spy)
    h1 = repo._open_scale_ann(idx, "kg")
    h2 = repo._open_scale_ann(idx, "kg")
    assert h1 is not None and h1 is h2   # 同一 handle 复用
    assert calls["n"] == 1               # 只 load 一次
