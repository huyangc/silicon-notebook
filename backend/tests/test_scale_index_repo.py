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
    nb = _seed_small_base(repo)
    repo.build_scale_index(nb.id)
    out = repo.scale_ppr(nb.id, "MOSFET gain")
    assert isinstance(out, list)
    assert all(isinstance(cid, str) and 0.0 <= score <= 1.0 for cid, score in out)


def test_graph_mode_falls_back_when_no_index(repo):
    nb = _seed_small_base(repo)
    assert repo._scale_index(nb.id) is None   # not built -> fallback path


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
