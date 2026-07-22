"""viz 索引状态探针:只读不构建 + 三态(未建/已就绪/待刷新)。"""
import os
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
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


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_probe_never_builds(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    # 但 rebuild 会主动建;为测"未建"态,删掉 kg_viz 再探
    import shutil
    shutil.rmtree(repo._viz_index_dir(nb.id), ignore_errors=True)
    repo._viz_idx_cache.pop(nb.id, None)
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is False
    # 探针没有偷偷构建
    assert not os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))


def test_rebuild_refreshes_viz_index(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is True
    assert probe["viz_nodes"] == 2
    assert probe["viz_stale"] is False


def test_stale_after_mutation(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)         # 建了新鲜索引
    repo._viz_idx_cache.pop(nb.id, None)
    # 变更 KG(加对象)→ version 变 → 磁盘旧索引变 stale
    repo.store_kg(nb.id, None, [
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [])
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is True


def test_unified_kg_status_carries_viz_fields(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    st = repo.unified_kg_status(nb.id)
    assert st["viz_indexed"] is True
    assert st["viz_nodes"] == 2
    assert "viz_edges" in st and "viz_stale" in st
