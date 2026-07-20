"""Tests for POST /kg/rebuild and POST /kg/relink endpoints (Task 4).

Pattern mirrors test_unified_kg_api.py and the existing build_kg endpoint tests.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


class _Client:
    def __init__(self, configured):
        self.configured = configured


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

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    called = []

    def fake_submit(fn, *args, **kwargs):
        called.append((fn, args, kwargs))

    real_repo._kg_llm_client = _Client(True)
    monkeypatch.setattr(background_jobs, "submit", fake_submit)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rebuilding"
    assert body["notebook_id"] == nb
    assert body["job_id"].startswith("kgj-")
    assert len(called) == 1
    assert called[0][1] == (nb, body["job_id"], "rebuild")


def test_build_uses_resolved_kg_role_not_primary(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(
        type(real_repo),
        "llm_client",
        property(lambda _self: _Client(False)),
    )
    monkeypatch.setattr(
        type(real_repo),
        "kg_llm_client",
        property(lambda _self: _Client(True)),
    )
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    response = client.post(f"/api/notebooks/{nb}/kg/build")

    assert response.status_code == 200
    assert response.json()["job_id"].startswith("kgj-")


def test_duplicate_running_build_returns_409(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    real_repo._kg_llm_client = _Client(True)
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    first = client.post(f"/api/notebooks/{nb}/kg/build")
    second = client.post(f"/api/notebooks/{nb}/kg/build")

    assert first.status_code == 200
    assert second.status_code == 409


def test_submission_failure_marks_job_failed(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    real_repo._kg_llm_client = _Client(True)
    monkeypatch.setattr(
        background_jobs,
        "submit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)

    response = no_raise_client.post(f"/api/notebooks/{nb}/kg/build")

    assert response.status_code == 500
    latest = real_repo._runtime.kg_build_jobs.latest(nb)
    assert latest["status"] == "failed"
    assert latest["error_code"] == "job_submission_failed"


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

def test_repo_rebuild_notebook_kg_delegates_to_lifecycle_runner(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'r.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    repo = SQLiteRepository(Settings())

    calls = []
    build_result = {"built": [], "failed": [], "skipped": []}

    def fake_rebuild(notebook_id):
        calls.append(notebook_id)
        return build_result

    repo._runtime.knowledge_lifecycle.rebuild_notebook_kg = fake_rebuild

    result = repo.rebuild_notebook_kg("nb-123")

    assert calls == ["nb-123"]
    assert result is build_result
