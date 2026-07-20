"""Tests for POST /notebooks/{id}/paper-meta/backfill (Task 5).

Pattern mirrors test_kg_rebuild_relink_api.py (owner-gated, background-job
notebook endpoint: 404 unknown notebook, 409 LLM not configured, 200 +
background dispatch polling) and test_notebook_share_readonly.py's
_client/_login helpers (multi-user owner-gate 404 precedent).
"""
import time
from types import SimpleNamespace

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


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def test_backfill_endpoint_queues_missing(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    real_repo = deps.repository()

    called = []

    def fake_backfill(notebook_id):
        called.append(notebook_id)

    real_repo.llm_client = MagicMock(configured=True)
    real_repo.sources_missing_paper_meta = MagicMock(return_value=["src-1", "src-2"])
    real_repo.backfill_paper_metadata = fake_backfill
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/paper-meta/backfill")
    assert r.status_code == 200
    assert r.json() == {"queued": 2}

    deadline = time.time() + 5
    while not called and time.time() < deadline:
        time.sleep(0.05)
    assert called == [nb], f"backfill_paper_metadata not called; called={called}"


def test_backfill_endpoint_zero_noop(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    real_repo = deps.repository()

    real_repo.llm_client = MagicMock(configured=True)
    real_repo.sources_missing_paper_meta = MagicMock(return_value=[])
    backfill_mock = MagicMock()
    real_repo.backfill_paper_metadata = backfill_mock
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/paper-meta/backfill")
    assert r.status_code == 200
    assert r.json() == {"queued": 0}
    backfill_mock.assert_not_called()


def test_backfill_requires_llm(client):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # By default the test repo has no LLM configured → 409
    r = client.post(f"/api/notebooks/{nb}/paper-meta/backfill")
    assert r.status_code == 409
    assert "LLM" in r.json()["detail"]


def test_backfill_owner_gate(client):
    owner_h = _login(client, "p00000001")
    nb = client.post("/api/notebooks", json={"name": "nb"}, headers=owner_h).json()["id"]
    bob_h = _login(client, "p00000002")
    r = client.post(f"/api/notebooks/{nb}/paper-meta/backfill", headers=bob_h)
    assert r.status_code == 404


def test_backfill_endpoint_submits_notify_pending(client, monkeypatch):
    """兜底刷新：端点提交 job 时带 notify_pending=True（Task 6，铃铛集成 §3.3）。"""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps, routes as routes_mod

    real_repo = deps.repository()
    real_repo.llm_client = MagicMock(configured=True)
    real_repo.sources_missing_paper_meta = MagicMock(return_value=["src-1"])
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    calls = []
    monkeypatch.setattr(
        routes_mod,
        "background_jobs",
        SimpleNamespace(submit=lambda *a, **k: calls.append(k)),
    )

    r = client.post(f"/api/notebooks/{nb}/paper-meta/backfill")
    assert r.status_code == 200
    assert r.json() == {"queued": 1}
    assert calls == [{"name": f"papermeta-{nb}", "notify_pending": True}]
