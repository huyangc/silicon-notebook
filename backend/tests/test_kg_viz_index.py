"""_viz_index 大库不同步构建:后台线程 + GET 返回 building 状态,小库行为不变。"""
import os
import shutil
import time
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


def _clear_viz(repo, nb_id):
    """Wipe any viz index that rebuild_unified_kg proactively built, so we can
    test the lazy/async paths as if nothing had been built yet."""
    shutil.rmtree(repo._viz_index_dir(nb_id), ignore_errors=True)
    repo._viz_idx_cache.pop(nb_id, None)


def _wait_until_not_building(repo, nb_id, cap_seconds=10):
    deadline = time.time() + cap_seconds
    while nb_id in repo._viz_building:
        if time.time() > deadline:
            raise AssertionError("background viz build did not finish within cap")
        time.sleep(0.05)


def test_unified_graph_large_nb_returns_building_placeholder(repo, monkeypatch):
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    manifest_path = os.path.join(repo._viz_index_dir(nb.id), "manifest.json")
    assert not os.path.exists(manifest_path)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    # No synchronous build happened as part of this call: either the manifest
    # still doesn't exist yet, or a background thread raced ahead of us — but
    # the RETURN itself must be the placeholder, not a real graph.
    assert result["viz_building"] is True
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["total_nodes"] == 0
    assert result["total_edges"] == 0
    assert result["truncated"] is False
    assert nb.id in repo._viz_building or manifest_path  # background thread was spawned
    _wait_until_not_building(repo, nb.id)


def test_unified_graph_building_then_ready(repo, monkeypatch):
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    assert result.get("viz_building") is True
    _wait_until_not_building(repo, nb.id)
    result2 = repo.unified_graph(nb.id, level="object", limit=10)
    assert not result2.get("viz_building")
    assert len(result2["nodes"]) > 0


def test_unified_graph_small_nb_unchanged(repo):
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    # default threshold is large — small nb builds synchronously (legacy behavior)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    assert not result.get("viz_building")
    assert len(result["nodes"]) > 0
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))


def test_viz_stale_served_while_rebuilding(repo, monkeypatch):
    nb = _star(repo)
    # viz index already built fresh by rebuild_unified_kg above.
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    # Drift the KG version without re-running rebuild_unified_kg (so the disk
    # viz index becomes stale relative to _scale_index_version).
    repo.store_kg(nb.id, None, [
        {"local_id": "d", "object_type": "concept", "payload": {"name": "leakage", "section_path": ""}, "evidence": []},
    ], [])
    repo._viz_idx_cache.pop(nb.id, None)
    result = repo.unified_graph(nb.id, level="object", limit=10)
    # Stale data is benign to serve immediately.
    assert len(result["nodes"]) > 0
    assert not result.get("viz_building")
    # A background refresh was kicked off (may have already finished).
    _wait_until_not_building(repo, nb.id)


def test_unified_kg_status_reports_viz_building(repo, monkeypatch):
    nb = _star(repo)
    _clear_viz(repo, nb.id)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    repo.unified_graph(nb.id, level="object", limit=10)  # spawns background build
    status = repo.unified_kg_status(nb.id)
    assert "viz_building" in status
    _wait_until_not_building(repo, nb.id)
    status2 = repo.unified_kg_status(nb.id)
    assert status2["viz_building"] is False
