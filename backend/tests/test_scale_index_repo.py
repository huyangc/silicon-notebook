import json, os, pytest
import numpy as np
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
