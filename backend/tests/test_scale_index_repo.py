import json, os, pytest
import numpy as np
from scipy.sparse import load_npz
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_build_scale_index_writes_artifacts(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    manifest = repo._runtime.scale_builder.build(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    for f in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy", "ann.bin", "ann_labels.npy", "manifest.json"):
        assert os.path.exists(os.path.join(d, f)), f
    assert manifest["n_nodes"] >= 2
    node_ids_arr = np.load(os.path.join(d, "node_ids.npy"), allow_pickle=True)
    idf_arr = np.load(os.path.join(d, "idf.npy"))
    G = load_npz(os.path.join(d, "graph.npz"))
    assert len(idf_arr) == len(node_ids_arr) == G.shape[0] == G.shape[1]


def test_build_frees_matrices_and_hands_persist_prebuilt_anns(repo, monkeypatch):
    """Memory diet (OOM audit P0-3): build() pre-builds every ANN inline and
    frees the matrix BEFORE persist, so save_scale_index receives prebuilt_*
    hnsw handles — never the multi-GB matrices — and the whole-object-graph
    unified_cache dict is evicted right after viz_arrays. Reverting to
    matrices-at-persist (the old shape) fails here."""
    import app.services.kg.scale_index as si

    captured: dict = {}
    real_save = si.save_scale_index

    def spy_save(out_dir, **kw):
        captured.update(kw)
        return real_save(out_dir, **kw)

    monkeypatch.setattr(si, "save_scale_index", spy_save)
    invalidated: list = []
    monkeypatch.setattr(
        repo._runtime.scale_builder, "invalidate_unified_cache",
        lambda nb: invalidated.append(nb),
    )

    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    repo._runtime.scale_builder.build(nb.id)

    # matrices freed → not handed to persist
    assert captured.get("ann_vectors") is None             # KG matrix freed post-synonym
    assert "chunk_ann_vectors" not in captured             # freed after chunk ANN built
    assert "relation_ann_vectors" not in captured          # freed after relation ANN built
    # persist writes prebuilt hnsw handles instead (keys always present)
    assert "prebuilt_chunk_ann" in captured and "prebuilt_relation_ann" in captured
    assert captured.get("prebuilt_ann") is not None        # KG index built inline (has embeddings)
    # the whole-object-graph dict was evicted after viz_arrays
    assert nb.id in invalidated


def test_build_never_materializes_a_whole_embedding_matrix(repo, monkeypatch):
    """OOM (codex PR#347 round 1, then batch-3 W4 T-W4-3.3): the build used to
    load ONE whole-notebook matrix per ANN leg and merely free it early — a
    shape that only worked because every alias got dropped in the right order
    (``ann_vectors`` ALIASES ``ann_matrix_raw``, so ``del ann_vectors`` alone
    left ~33GB pinned). The paged feed removes the object instead of freeing
    it: hnswlib copies each page, so no leg ever holds more than one page.

    Two halves, and both fail on a revert to the whole-matrix load: the
    whole-notebook loader must not be called for ANY of the three build tables
    at all, and every page matrix handed to hnswlib must be dead by persist."""
    import weakref
    import gc as _gc

    builder = repo._runtime.scale_builder
    whole_matrix_tables: list = []
    page_refs: list = []
    real_em = builder.projections.embedding_matrix
    real_pages = builder.projections.embedding_pages

    def spy_em(notebook_id, table, id_column, object_ids=None, **kw):
        if object_ids is None:
            whole_matrix_tables.append(table)
        return real_em(notebook_id, table, id_column, object_ids, **kw)

    def spy_pages(notebook_id, table, id_column, *args, **kw):
        for ids, matrix in real_pages(notebook_id, table, id_column, *args, **kw):
            page_refs.append(weakref.ref(matrix))
            yield ids, matrix

    monkeypatch.setattr(builder.projections, "embedding_matrix", spy_em)
    monkeypatch.setattr(builder.projections, "embedding_pages", spy_pages)

    persist_state: dict = {}
    real_save = builder.artifacts.save_full

    def spy_save(notebook_id, artifacts, **kwargs):
        _gc.collect()
        persist_state["alive"] = [ref for ref in page_refs if ref() is not None]
        return real_save(notebook_id, artifacts, **kwargs)

    monkeypatch.setattr(builder.artifacts, "save_full", spy_save)

    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)
    builder.build(nb.id)

    assert page_refs, "the KG leg must have been fed at least one page"
    assert whole_matrix_tables == []
    assert persist_state.get("alive") == []


def test_rebuild_releases_concept_reps_before_derivation_tail(repo, monkeypatch):
    """OOM audit P0-2: concept mean-vectors `reps` (~18GB at 4.4M seeds) is dead
    after clustering — rebuild must drop it BEFORE the derivation tail (canonical
    relations / mention bridge / build_viz / communities), not hold it stacked on
    those stages. Weakref a rep array; by build_viz (the tail) it must be gone.
    Reverting the `del reps` fails here."""
    import weakref
    import gc as _gc

    lifecycle = repo._runtime.knowledge_lifecycle
    holder: dict = {}
    real_reps = lifecycle._stream_seed_reps

    def spy_reps(notebook_id, object_type, seed_fn, **kw):
        reps, mc, sfn = real_reps(notebook_id, object_type, seed_fn, **kw)
        if object_type == "concept" and reps:
            holder["ref"] = weakref.ref(next(iter(reps.values())))
        return reps, mc, sfn

    monkeypatch.setattr(lifecycle, "_stream_seed_reps", spy_reps)

    tail: dict = {}
    real_viz = repo._runtime.scale_artifacts.build_viz

    def spy_viz(notebook_id):
        _gc.collect()
        ref = holder.get("ref")
        tail["reps_alive"] = ref is not None and ref() is not None
        return real_viz(notebook_id)

    monkeypatch.setattr(repo._runtime.scale_artifacts, "build_viz", spy_viz)

    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)

    assert holder.get("ref") is not None        # reps captured (concepts have embeddings)
    assert tail.get("reps_alive") is False       # ...and released before build_viz


@pytest.mark.parametrize(
    ("runtime_dim", "warns"),
    ((None, True), (4096, True), (1024, False)),
)
def test_build_warns_when_effective_width_is_full_native(
    monkeypatch, runtime_dim, warns
):
    """OOM audit P2-6 (codex PR#353 r4): warn whenever the EFFECTIVE build width
    is full native — EMBED_RUNTIME_DIM unset (0) OR >= EMBED_DIM (a no-op
    truncation the validator permits) — not only when it is 0. A truncating
    runtime (< EMBED_DIM) stays quiet. NOT fatal."""
    from app.services.scale_index_builder import (
        _warn_if_scale_index_uses_full_width,
    )

    monkeypatch.setenv("EMBED_DIM", "4096")
    if runtime_dim is None:
        monkeypatch.delenv("EMBED_RUNTIME_DIM", raising=False)
    else:
        monkeypatch.setenv("EMBED_RUNTIME_DIM", str(runtime_dim))
    messages: list[str] = []

    class Logger:
        def warning(self, message, *_args):
            messages.append(str(message))

    _warn_if_scale_index_uses_full_width(Settings(), Logger(), "nb-test")

    assert bool(messages) is warns
    assert all("effective width" in message for message in messages)


def test_persist_ann_fails_loud_on_labels_without_vectors_or_matching_index(tmp_path):
    """_persist_ann must NOT write an EMPTY index next to N labels (row-count
    mismatch → silent recall collapse). With no vectors and no size-matching
    prebuilt it fails loud; n_labels==0 still writes a valid empty index."""
    from app.services.kg.scale_index import _persist_ann

    with pytest.raises(ValueError, match="row-count-mismatched"):
        _persist_ann(str(tmp_path), "ann.bin", None, None, 3, 16, 200)
    # a notebook with no KG vectors still writes a valid (empty) ann.bin
    _persist_ann(str(tmp_path), "empty.bin", None, None, 0, 16, 200)
    assert os.path.exists(os.path.join(str(tmp_path), "empty.bin"))


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


def test_resolve_fold_escalates_to_full_after_kg_rebuild(repo):
    """索引建成后若发生过「刷新图谱」(全量重聚类,推进 last_rebuild_at),fold 只把 delta
    新源拼接进旧索引、不重读全量图,却在末尾把 version 盖成最新 → 索引标最新实则陈旧
    (旧节点重聚类没进索引)。故 _resolve_scale_mode 检测到 last_rebuild_at > built_at
    时把 fold 升级成 full,让重聚类真正收敛。加新源(incremental_fuse)不碰 last_rebuild_at,
    故「纯新增来源」不会被误升级。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "X", "section_path": ""}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    # 索引刚建成、其后无 rebuild → fold 保持 fold(不误升级)。
    assert repo._resolve_scale_mode(nb.id, "fold") == "fold"
    # 模拟索引建成后的一次「刷新图谱」:last_rebuild_at 推进到明显更晚。
    with repo._write() as db:
        db.execute("UPDATE unified_kg_state SET last_rebuild_at=? WHERE notebook_id=?",
                   ("2099-01-01T00:00:00", nb.id))
    assert repo._resolve_scale_mode(nb.id, "fold") == "full"


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
        vec = repo._runtime.models.embedding("retrieval_query_embedding").embed_query("MOSFET")
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
    repo.replace_notebook_bases(active.id, [base.id], "user-local")
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
        vec = repo._runtime.models.embedding("retrieval_query_embedding").embed_query(concept_name)
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
    repo._runtime.models.embedding("retrieval_query_embedding").dim = 16  # already 16 from fixture

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
        vec_j = repo._runtime.models.embedding("retrieval_query_embedding").embed_query("j")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", ("obj-act-j", active.id, json.dumps(vec_j), now))
    repo.rebuild_unified_kg(active.id)

    # Verify that 'i' and 'j' are indeed similar enough with this embedder.
    vec_i = np.array(repo._runtime.models.embedding("retrieval_query_embedding").embed_query("i"), dtype=np.float64)
    vec_j_arr = np.array(repo._runtime.models.embedding("retrieval_query_embedding").embed_query("j"), dtype=np.float64)
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
                v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    manifest = repo._runtime.scale_builder.build(nb.id)
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
    """build_scale_index must time each internal stage (kg_matrix/ann_build/
    synonym/gather/transition/chunk_matrix/viz_arrays/persist) and emit a
    scale_index_build event per stage plus a final total — for locating the
    bottleneck stage on large (490k-object) deployments. Task 1 reordered the
    pipeline so the KG matrix loads and the ONE shared hnsw build happen
    before gather (ann_build/synonym are new stages; gather no longer builds
    its own hnsw). Disk manifest.json carries the pre-persist stages
    (persist/total aren't known until after the file is written); the
    RETURNED manifest dict additionally carries persist+total."""
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

    # Returned manifest: 10 keys (9 stages + total)
    expected_stages = {"kg_matrix", "ann_build", "synonym", "gather", "transition",
                       "chunk_matrix", "relation_matrix", "viz_arrays",
                       "source_partitions", "persist"}
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

    # Disk manifest.json: only the pre-persist stages (persist/total unknown
    # until after the file itself is written; source_partitions runs after the
    # manifest file is saved, so it is equally absent on disk).
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    with open(os.path.join(d, "manifest.json")) as fh:
        disk_manifest = json.load(fh)
    assert set(disk_manifest["build_ms"].keys()) == (
        expected_stages - {"persist", "source_partitions"}
    )

    # Events: 11 scale_index_build events (10 stages + total), each with
    # notebook_id/stage/latency_ms.
    scale_events = [e for e in events if e.get("kind") == "scale_index_build"]
    assert len(scale_events) == 11
    stages_seen = {e["stage"] for e in scale_events}
    assert stages_seen == expected_stages | {"total"}
    for e in scale_events:
        assert e["notebook_id"] == nb.id
        assert isinstance(e["latency_ms"], int)
        assert e["latency_ms"] >= 0


def test_build_scale_index_on_stage_callback(repo):
    """build_scale_index(on_stage=...) must invoke the callback once per
    stage — the same 8 stages as the scale_index_build events, plus total —
    right when each stage's timing is recorded, so a CLI caller can print
    real-time per-stage progress on long (490k-object) builds without
    depending on the events logger (which doesn't print to the terminal)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)

    calls = []
    manifest = repo.build_scale_index(nb.id, on_stage=lambda stage, ms: calls.append((stage, ms)))

    expected_stages = {"kg_matrix", "ann_build", "synonym", "gather", "transition",
                       "chunk_matrix", "relation_matrix", "viz_arrays",
                       "source_partitions", "persist", "total"}
    assert len(calls) == 11
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
            v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([cid])[0]
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["c1"])[0]
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["c1"])[0]
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
                v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
        # Raw chunk insert bypasses build_chunks_for_source's seq bump; drop the
        # seq-gated chunk-count memo so status() recomputes the new chunk total.
        from app.repositories.sqlite import knowledge_counts_cache
        knowledge_counts_cache.invalidate(nb.id)
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


@pytest.mark.parametrize("legacy_sidecar", [False, True])
def test_fold_scale_index_delta(repo, legacy_sidecar):
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
                v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts([name])[0]
                col = "chunk_id" if tbl == "chunk_embeddings" else "object_id"
                db.execute(
                    f"INSERT INTO {tbl} ({col},notebook_id,vector,created_at) VALUES (?,?,?,?)",
                    (key, nb.id, json.dumps(v), now))

    add("s1", "o1", "c1", "current mirror", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    if legacy_sidecar:
        # Simulate a pre-source-sidecar artifact.  The next ordinary delta
        # fold must upgrade all existing ANN labels as well as the new rows.
        artifact_dir = os.path.join(
            repo.settings.storage_dir, "kg_index", nb.id
        )
        manifest_path = os.path.join(artifact_dir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest.pop("has_chunk_ann_sources", None)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        for filename in (
            "chunk_ann_source_names.npy",
            "chunk_ann_source_codes.npy",
            "chunk_ann_source_counts.npy",
        ):
            os.remove(os.path.join(artifact_dir, filename))
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["current mirror"])[0]
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
    # fold 路径写入的 manifest 键是 total_build_ms(不是 build_ms —— 那是分阶段耗时
    # dict,两者只差一个字母),读点(scale_artifact_runtime.status)消费的就是这个键。
    assert isinstance(idx.manifest.get("total_build_ms"), int)
    assert idx.manifest["total_build_ms"] >= 0
    # ann 含新对象、chunk_ann 含新 chunk
    assert "o2" in set(idx.ann_labels)
    assert idx.chunk_ann_labels is not None and "c2" in set(idx.chunk_ann_labels)
    assert "o1" in set(idx.ann_labels) and "c1" in set(idx.chunk_ann_labels)
    assert idx.chunk_ann_source_names is not None
    source_by_chunk = {
        chunk_id: idx.chunk_ann_source_names[
            int(idx.chunk_ann_source_codes[row])
        ]
        for row, chunk_id in enumerate(idx.chunk_ann_labels)
    }
    assert source_by_chunk["c1"] == "s1"
    assert source_by_chunk["c2"] == "s2"
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["c1"])[0]
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["c1"])[0]
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
        v = repo._runtime.models.embedding("retrieval_query_embedding").embed_texts(["c2"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)", ("c2", nb.id, json.dumps(v), now))

    # _resolve_scale_mode(auto) 应选 fold(有索引)
    assert repo._resolve_scale_mode(nb.id, "auto") == "fold"

    # 监视 fold 被调用
    calls = {"fold": 0, "build": 0}
    orig_fold = repo._runtime.scale_artifacts.fold
    orig_build = repo._runtime.scale_artifacts.build
    def spy_fold(nbid, *a, **kw):
        calls["fold"] += 1
        return orig_fold(nbid, *a, **kw)
    def spy_build(nbid, *a, **kw):
        calls["build"] += 1
        return orig_build(nbid, *a, **kw)
    monkeypatch.setattr(repo._runtime.scale_artifacts, "fold", spy_fold)
    monkeypatch.setattr(repo._runtime.scale_artifacts, "build", spy_build)

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


# ── Task 1: hnsw 只建一次 + ef_construction 可配 (build_scale_index perf) ────


def test_save_scale_index_prebuilt_ann_used_directly(tmp_path):
    """save_scale_index(prebuilt_ann=idx) must persist that EXACT index (skip
    its own init_index+add_items) — loaded back, its knn results must agree
    with an index built independently from the same ann_vectors."""
    import hnswlib
    import scipy.sparse as sp
    from app.services.kg import scale_index as si

    rng = np.random.RandomState(1)
    n, dim = 30, 8
    vecs = rng.randn(n, dim).astype(np.float32)
    labels = [f"o{i}" for i in range(n)]

    prebuilt = hnswlib.Index(space="cosine", dim=dim)
    prebuilt.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
    prebuilt.add_items(vecs, np.arange(n))

    out_dir = str(tmp_path / "idx")
    manifest = si.save_scale_index(
        out_dir,
        node_ids=labels,
        transition=sp.csr_matrix((n, n)),
        idf=[1.0] * n,
        chunk_index=[],
        ann_vectors=vecs,           # ignored when prebuilt_ann is given
        ann_labels=labels,
        manifest={"version": "v1", "dim": dim},
        prebuilt_ann=prebuilt,
    )
    assert manifest["version"] == "v1"  # manifest passthrough unchanged

    loaded = hnswlib.Index(space="cosine", dim=dim)
    loaded.load_index(os.path.join(out_dir, "ann.bin"), max_elements=n)
    loaded.set_ef(50)

    independent = hnswlib.Index(space="cosine", dim=dim)
    independent.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
    independent.add_items(vecs, np.arange(n))
    independent.set_ef(50)

    q = vecs[0]
    lab_loaded, _ = loaded.knn_query(q, k=5)
    lab_indep, _ = independent.knn_query(q, k=5)
    assert set(lab_loaded[0].tolist()) == set(lab_indep[0].tolist())
    assert lab_loaded[0][0] == lab_indep[0][0] == 0    # top1 (self) agrees


def test_save_scale_index_prebuilt_ann_element_count_mismatch_falls_back(tmp_path):
    """If prebuilt_ann's element count != len(ann_labels), save_scale_index must
    defensively fall back to building fresh from ann_vectors (never emit a
    corrupt/misaligned ann.bin)."""
    import hnswlib
    import scipy.sparse as sp
    from app.services.kg import scale_index as si

    rng = np.random.RandomState(2)
    n, dim = 10, 8
    vecs = rng.randn(n, dim).astype(np.float32)
    labels = [f"o{i}" for i in range(n)]

    mismatched = hnswlib.Index(space="cosine", dim=dim)
    mismatched.init_index(max_elements=5, ef_construction=200, M=16, random_seed=42)
    mismatched.add_items(vecs[:5], np.arange(5))   # only 5 elements, but ann_labels has 10

    out_dir = str(tmp_path / "idx2")
    si.save_scale_index(
        out_dir,
        node_ids=labels,
        transition=sp.csr_matrix((n, n)),
        idf=[1.0] * n,
        chunk_index=[],
        ann_vectors=vecs,
        ann_labels=labels,
        manifest={"version": "v1", "dim": dim},
        prebuilt_ann=mismatched,
    )
    loaded = hnswlib.Index(space="cosine", dim=dim)
    loaded.load_index(os.path.join(out_dir, "ann.bin"), max_elements=n)
    loaded.set_ef(50)
    lab, _ = loaded.knn_query(vecs[9], k=1)
    assert lab[0][0] == 9   # element 9 only exists if the fallback (full) build ran


def test_save_scale_index_ef_construction_forwarded(tmp_path, monkeypatch):
    """save_scale_index's own (non-prebuilt) hnsw build must forward
    ef_construction to init_index instead of hardcoding 200."""
    import hnswlib
    import scipy.sparse as sp
    from app.services.kg import scale_index as si

    rng = np.random.RandomState(3)
    n, dim = 6, 4
    vecs = rng.randn(n, dim).astype(np.float32)
    labels = [f"o{i}" for i in range(n)]

    seen_kwargs = []
    real_init = hnswlib.Index.init_index

    def spy_init(self, *a, **kw):
        seen_kwargs.append(kw)
        return real_init(self, *a, **kw)

    monkeypatch.setattr(hnswlib.Index, "init_index", spy_init)
    si.save_scale_index(
        str(tmp_path / "idx3"),
        node_ids=labels,
        transition=sp.csr_matrix((n, n)),
        idf=[1.0] * n,
        chunk_index=[],
        ann_vectors=vecs,
        ann_labels=labels,
        manifest={"version": "v1", "dim": dim},
        ef_construction=99,
    )
    assert any(kw.get("ef_construction") == 99 for kw in seen_kwargs)


def test_build_scale_index_builds_hnsw_once_for_kg_synonym_and_ann(repo, monkeypatch):
    """End-to-end: when emb_synonym is enabled, hnswlib.Index() must be
    constructed exactly ONCE for the KG embeddings (shared by the synonym-edge
    KNN pass and the persisted ann.bin) — down from 2 before this task. The
    chunk ANN build is a separate matrix/index and counts separately.

    store_kg's db_relations always get embedded via _embed_relations_batch
    (independent of RELATION_RETRIEVAL_ENABLED), so this fixture's one
    `depends_on` relation also gets its own relation-ANN hnsw build (relation
    ANN is a separate index too — same "counts separately" shape as chunk_ann)
    → total constructions == 2 (KG + relation), not 1."""
    import hnswlib
    nb = repo.create_notebook(NotebookCreate(name="base"))
    # Need >=2 KG objects with embeddings so _vector_matrix has rows and
    # emb_synonym_edges actually runs the ANN build path (not the n<2 short-circuit).
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "op-amp", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)

    calls = {"n": 0}
    real_init = hnswlib.Index.__init__

    def spy_init(self, *a, **kw):
        calls["n"] += 1
        return real_init(self, *a, **kw)

    monkeypatch.setattr(hnswlib.Index, "__init__", spy_init)
    manifest = repo._runtime.scale_builder.build(nb.id)

    # KG-embedding hnsw built once (shared: synonym KNN + persisted ann.bin).
    # No chunks were stored in this fixture → chunk_ann build is skipped
    # (chunk_ann_labels empty). One relation was stored and auto-embedded by
    # store_kg → relation-ANN hnsw builds once more → total constructions == 2.
    assert calls["n"] == 2, f"expected exactly 2 hnswlib.Index() constructions (KG + relation), got {calls['n']}"
    assert manifest["n_nodes"] >= 3


def test_hnsw_ef_construction_default_and_env(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.hnsw_ef_construction == 200
    monkeypatch.setenv("HNSW_EF_CONSTRUCTION", "77")
    s2 = Settings()
    assert s2.hnsw_ef_construction == 77


# ── Task 6: _resolve_index_owner (index_done 通知发给谁) ─────────────────────

def test_resolve_index_owner_prefers_request_user(repo):
    """`_REQUEST_USER` 设了值(发起线程 copy_context 已传播)→ 优先用它的 id,
    即便 notebook 另有 created_by(常规「刷新图谱」按钮路径:请求用户未必是
    notebook 的 created_by,如只读成员触发)。两个 id 都须是真实 users 行
    (created_by 有 FK REFERENCES users(id)),故用 create_user 造第二个用户。"""
    from app.services import sqlite_repository as sr

    creator = repo.create_user("a00111111", "pw")
    nb = repo.create_notebook(NotebookCreate(name="owner-test"))
    with repo._write() as db:
        db.execute("UPDATE notebooks SET created_by=? WHERE id=?", (creator.id, nb.id))

    requester = repo.create_user("a00222222", "pw")
    token = sr._REQUEST_USER.set(requester)
    try:
        assert repo._resolve_index_owner(nb.id) == requester.id
        assert requester.id != creator.id  # 确认真走的是 _REQUEST_USER 分支而非巧合同值
    finally:
        sr._REQUEST_USER.reset(token)


def test_resolve_index_owner_falls_back_to_created_by(repo):
    """无 `_REQUEST_USER`(如 idle 调度器/自动 fold,没有发起请求上下文)→ 回退查
    `notebooks.created_by`。"""
    from app.services import sqlite_repository as sr

    creator = repo.create_user("a00333333", "pw")
    nb = repo.create_notebook(NotebookCreate(name="owner-test2"))
    with repo._write() as db:
        db.execute("UPDATE notebooks SET created_by=? WHERE id=?", (creator.id, nb.id))

    assert sr._REQUEST_USER.get() is None  # fixture/test isolation: nothing left set
    assert repo._resolve_index_owner(nb.id) == creator.id


def test_resolve_index_owner_none_when_neither_available(repo):
    """既无 `_REQUEST_USER` 又查不到 notebook(如已删除/id 不存在,覆盖自动 fold
    的边角)→ None,调用方(_notify_index_done)应静默跳过通知而非报错。"""
    from app.services import sqlite_repository as sr
    assert sr._REQUEST_USER.get() is None
    assert repo._resolve_index_owner("nonexistent-notebook-id") is None


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
