"""build_viz_index:lite 折叠等价 _unified_graph_full('object') + 落盘 + 空图 None。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _star(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_lite_graph_equals_full(repo):
    nb = _star(repo)
    full = repo._unified_graph_full(nb.id, "object")
    lite = repo._derive_object_graph_lite(nb.id)
    # 逐字段相等:节点(id/type/name,同序)与边集
    assert [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in lite["nodes"]] == \
           [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in full["nodes"]]
    assert [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in lite["edges"]] == \
           [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in full["edges"]]


def test_build_viz_index_persists_and_manifest(repo):
    nb = _star(repo)
    manifest = repo.build_viz_index(nb.id)
    assert manifest is not None
    assert manifest["n_viz_nodes"] == 3
    assert manifest["n_viz_edges"] == 2
    assert manifest["version"] == repo._scale_index_version(nb.id)
    # 落在 kg_viz/,不在 kg_index/
    import os
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    assert not os.path.exists(os.path.join(str(repo.settings.storage_dir), "kg_index", nb.id, "manifest.json"))


def test_build_viz_index_empty_notebook_returns_none(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.build_viz_index(nb.id) is None
