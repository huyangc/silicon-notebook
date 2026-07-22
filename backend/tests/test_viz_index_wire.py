"""_viz_index 懒构建 + unified_graph/kg_neighbors 等价 + 检索隔离 + base 复用。"""
import os
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.services.kg_merge import limit_graph_by_degree
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
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


def test_neighbors_lazy_matches_db(repo):
    nb = _star(repo)
    full = repo._unified_graph_full(nb.id, "object")
    # 真·折叠 canonical id:取度数最高的折叠节点(MOSFET 概念,连 gain+bias)
    deg = {}
    for e in full["edges"]:
        deg[e["source_object_id"]] = deg.get(e["source_object_id"], 0) + 1
        deg[e["target_object_id"]] = deg.get(e["target_object_id"], 0) + 1
    hub_id = max(deg, key=deg.get)
    db_res = repo._kg_neighbors_db(nb.id, hub_id, 50)
    viz_res = repo.kg_neighbors(nb.id, hub_id, 50)
    db_ids = {n["id"] for n in db_res["nodes"]}
    viz_ids = {n["id"] for n in viz_res["nodes"]}
    # 非空且两路一致:hub + 2 个邻居(gain, bias)
    assert len(viz_ids) == 3
    assert viz_ids == db_ids
    assert {(e["source_object_id"], e["target_object_id"]) for e in viz_res["edges"]} == \
           {(e["source_object_id"], e["target_object_id"]) for e in db_res["edges"]}
    assert len(viz_res["edges"]) == 2


def test_scale_index_isolation(repo):
    nb = _star(repo)
    repo.unified_graph(nb.id, level="object", limit=2)  # 建 viz 索引
    # 检索路径不受污染:该库仍无检索 scale 索引
    assert repo._scale_index(nb.id) is None


def test_empty_notebook_falls_back(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    # 空库:_viz_index None,unified_graph 走全量派生(不报错,空结果)
    assert repo._viz_index(nb.id) is None
    g = repo.unified_graph(nb.id, level="object", limit=2)
    assert g["nodes"] == [] and g["total_nodes"] == 0
