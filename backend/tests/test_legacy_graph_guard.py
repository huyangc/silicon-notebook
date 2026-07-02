"""C6: legacy GET /notebooks/{id}/graph (knowledge_graph) has no frontend
callers and — unlike unified_graph — had NO large-notebook guard: it always
materializes every non-deprecated knowledge_objects row (full payload) into
Python with no cap. Large notebooks now get an HTTP 413 pointing at
/unified-kg instead of a synchronous multi-GB in-memory build; small
notebooks are unaffected.
"""
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, KnowledgeGraphTooLargeError
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def client(repo, monkeypatch):
    """TestClient with app.api.routes.repository() overridden to the fixture
    repo (house pattern, see test_edge_review_queue.py). require_notebook_read
    still resolves via app.api.deps.repository() — a separate SQLiteRepository
    instance pointed at the SAME on-disk sqlite file (env vars from `repo`'s
    monkeypatch.setenv are still active when deps.repository() first runs),
    so both can see the same data."""
    from fastapi.testclient import TestClient
    import app.api.routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    return TestClient(app)


def _seed_notebook_with_n_objects(repo, n):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    for i in range(n):
        repo._test_insert_object(nb.id, "concept", {"name": f"c{i}"})
    return nb.id


def test_knowledge_graph_repo_raises_over_threshold(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 3)
    nb_id = _seed_notebook_with_n_objects(repo, 5)
    with pytest.raises(KnowledgeGraphTooLargeError):
        repo.knowledge_graph(nb_id)


def test_knowledge_graph_repo_unchanged_under_threshold(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 100)
    nb_id = _seed_notebook_with_n_objects(repo, 5)
    g = repo.knowledge_graph(nb_id)
    assert len(g.nodes) == 5


def test_graph_endpoint_413_for_large_notebook(client, repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 3)
    nb_id = _seed_notebook_with_n_objects(repo, 5)
    resp = client.get(f"/api/notebooks/{nb_id}/graph")
    assert resp.status_code == 413
    assert "unified-kg" in resp.json()["detail"]


def test_graph_endpoint_unchanged_for_small_notebook(client, repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 100)
    nb_id = _seed_notebook_with_n_objects(repo, 5)
    resp = client.get(f"/api/notebooks/{nb_id}/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["nodes"]) == 5


def test_graph_endpoint_missing_notebook_still_404(client):
    resp = client.get("/api/notebooks/does-not-exist/graph")
    assert resp.status_code == 404


def test_graph_endpoint_exactly_at_threshold_not_refused(client, repo, monkeypatch):
    """Boundary: exactly at the threshold (not over it) must still succeed —
    only a count STRICTLY GREATER than the threshold is refused, matching
    unified_graph's `>` (not `>=`) comparison."""
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 5)
    nb_id = _seed_notebook_with_n_objects(repo, 5)
    resp = client.get(f"/api/notebooks/{nb_id}/graph")
    assert resp.status_code == 200
