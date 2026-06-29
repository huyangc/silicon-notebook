"""Tests for POST /kg/rebuild and POST /kg/relink endpoints (Task 4).

Pattern mirrors test_unified_kg_api.py and the existing build_kg endpoint tests.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /notebooks/{id}/kg/rebuild
# ---------------------------------------------------------------------------

def test_rebuild_kg_404_unknown_notebook(client):
    r = client.post("/api/notebooks/no-such-notebook/kg/rebuild")
    assert r.status_code == 404


def test_rebuild_kg_409_llm_not_configured(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # By default the test repo has no LLM configured → 409
    r = client.post(f"/api/notebooks/{nb}/kg/rebuild")
    assert r.status_code == 409
    assert "LLM" in r.json()["detail"]


def test_rebuild_kg_200_launches_background_and_returns_rebuilding(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    # Patch the repository singleton to have an LLM configured and
    # capture whether rebuild_notebook_kg is invoked.
    from app.api import deps
    real_repo = deps.repository()

    called = []

    def fake_rebuild(notebook_id):
        called.append(notebook_id)

    real_repo.llm_client = MagicMock(configured=True)
    real_repo.rebuild_notebook_kg = fake_rebuild
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rebuilding"
    assert body["notebook_id"] == nb

    # Give the daemon thread a moment to run (TestClient is sync so the
    # thread may still be running; we just verify the key was accepted).
    import time
    deadline = time.time() + 5
    while not called and time.time() < deadline:
        time.sleep(0.05)
    assert called == [nb], f"rebuild_notebook_kg not called; called={called}"


# ---------------------------------------------------------------------------
# POST /notebooks/{id}/kg/relink
# ---------------------------------------------------------------------------

def test_relink_kg_404_unknown_notebook(client):
    r = client.post("/api/notebooks/no-such-notebook/kg/relink")
    assert r.status_code == 404


def test_relink_kg_200_returns_stats(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    real_repo = deps.repository()

    expected = {"isolated_before": 3, "edges_added": 2, "isolated_after": 1}
    real_repo.relink_notebook_kg = MagicMock(return_value=expected)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert r.status_code == 200
    assert r.json() == expected
    real_repo.relink_notebook_kg.assert_called_once_with(nb)


def test_relink_kg_no_llm_check(client, monkeypatch):
    """relink is deterministic — it must succeed even with no LLM configured."""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    real_repo = deps.repository()

    # Explicitly mark LLM as unconfigured
    real_repo.llm_client = MagicMock(configured=False)
    real_repo.relink_notebook_kg = MagicMock(return_value={
        "isolated_before": 0, "edges_added": 0, "isolated_after": 0
    })
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert r.status_code == 200   # must NOT return 409


# ---------------------------------------------------------------------------
# Repo unit test: rebuild_notebook_kg calls delete then build
# ---------------------------------------------------------------------------

def test_repo_rebuild_notebook_kg_calls_delete_then_build(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings())

    call_order = []
    delete_result = {"knowledge_objects": 2}
    build_result = {"built": [], "failed": [], "skipped": []}

    def fake_delete(notebook_id):
        call_order.append(("delete", notebook_id))
        return delete_result

    def fake_build(notebook_id):
        call_order.append(("build", notebook_id))
        return build_result

    repo.delete_notebook_kg = fake_delete
    repo.build_notebook_kg = fake_build

    result = repo.rebuild_notebook_kg("nb-123")

    assert call_order == [("delete", "nb-123"), ("build", "nb-123")]
    assert result is build_result
