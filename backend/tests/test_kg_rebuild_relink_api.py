"""Tests for POST /kg/rebuild and POST /kg/relink endpoints (Task 4).

Pattern mirrors test_unified_kg_api.py and the existing build_kg endpoint tests.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
from tests.model_testkit import bind_chat_client


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

    bind_chat_client(real_repo, "kg_extract", _Client(True))
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
    bind_chat_client(real_repo, "ask_answer", _Client(False))
    bind_chat_client(real_repo, "kg_extract", _Client(True))
    submitted = []
    monkeypatch.setattr(
        background_jobs,
        "submit",
        lambda *args, **kwargs: submitted.append((args, kwargs)),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    response = client.post(f"/api/notebooks/{nb}/kg/build")

    assert response.status_code == 200
    assert response.json()["job_id"].startswith("kgj-")
    assert submitted[0][1]["retry_partial"] is True


def test_duplicate_running_build_returns_409(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    bind_chat_client(real_repo, "kg_extract", _Client(True))
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
    bind_chat_client(real_repo, "kg_extract", _Client(True))
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


def test_relink_kg_200_launches_background_and_returns_relinking(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    called = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: called.append((fn, args, kwargs)),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "relinking"
    assert body["notebook_id"] == nb
    assert body["job_id"].startswith("rlj-")
    # The click must not have done the work on the request thread.
    assert len(called) == 1
    assert called[0][1] == (nb, body["job_id"])
    assert called[0][2]["name"] == f"relinkkg-{nb}"


def test_relink_refuses_pending_pipeline_before_claim_or_submit(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "pending"}).json()["id"]
    from app.api import deps
    from app.domain.indexing_pipeline import IndexingPipelineUnavailableError
    from app.services import background_jobs

    real_repo = deps.repository()
    with real_repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_job_id=? WHERE id=?",
            ("pending:generation", nb),
        )
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit", lambda *args, **kwargs: submitted.append(args)
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    response = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert response.status_code == 409
    assert submitted == []
    assert real_repo._runtime.knowledge_lifecycle.notebook_relink_status(nb)[
        "status"
    ] == "idle"
    with pytest.raises(IndexingPipelineUnavailableError):
        real_repo.run_notebook_relink_job(nb, "rlj-never-started")


def test_relink_kg_no_llm_check(client, monkeypatch):
    """relink is deterministic — it must succeed even with no LLM configured."""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()

    # Explicitly mark LLM as unconfigured
    bind_chat_client(real_repo, "kg_extract", MagicMock(configured=False))
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert r.status_code == 200   # must NOT return 409


def test_relink_kg_second_click_returns_409_while_running(client, monkeypatch):
    """Per-notebook single flight: the second click must not queue a duplicate."""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda *a, **k: submitted.append(a),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    first = client.post(f"/api/notebooks/{nb}/kg/relink")
    second = client.post(f"/api/notebooks/{nb}/kg/relink")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.headers.get("X-User-Message")     # 中文文案经 user_error 上屏
    assert len(submitted) == 1

    # A different notebook is a different slot.
    other = client.post("/api/notebooks", json={"name": "nb2"}).json()["id"]
    assert client.post(f"/api/notebooks/{other}/kg/relink").status_code == 200


def test_relink_submission_failure_releases_the_claim(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)

    assert no_raise_client.post(f"/api/notebooks/{nb}/kg/relink").status_code == 500
    # The slot must be free again, not stuck in `running` forever.
    status = client.get(f"/api/notebooks/{nb}/kg/relink/status").json()
    assert status["running"] is False
    assert status["status"] == "failed"

    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    assert client.post(f"/api/notebooks/{nb}/kg/relink").status_code == 200


def test_relink_status_reports_idle_then_terminal_counts(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    idle = client.get(f"/api/notebooks/{nb}/kg/relink/status")
    assert idle.status_code == 200
    assert idle.json() == {
        "job_id": "", "notebook_id": nb, "status": "idle", "running": False,
        "isolated_before": 0, "edges_added": 0, "isolated_after": 0,
    }

    # Run the job body inline (submit is the only thing we stub out) so the
    # terminal counters the browser polls for come from a real relink pass.
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: fn(*args),
    )
    started = client.post(f"/api/notebooks/{nb}/kg/relink").json()
    done = client.get(f"/api/notebooks/{nb}/kg/relink/status").json()
    assert done["job_id"] == started["job_id"]
    assert done["status"] == "succeeded"
    assert done["running"] is False


def test_relink_status_404_unknown_notebook(client):
    assert client.get("/api/notebooks/no-such/kg/relink/status").status_code == 404


# ---------------------------------------------------------------------------
# POST /notebooks/{id}/unified-kg/rebuild  (「重新合并」, PR-E)
# ---------------------------------------------------------------------------

def test_unified_rebuild_404_unknown_notebook(client):
    assert client.post("/api/notebooks/no-such/unified-kg/rebuild").status_code == 404


def test_unified_rebuild_200_launches_background_and_returns_rebuilding(
    client, monkeypatch
):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    called = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: called.append((fn, args, kwargs)),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    r = client.post(f"/api/notebooks/{nb}/unified-kg/rebuild")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rebuilding"
    assert body["notebook_id"] == nb
    assert body["job_id"].startswith("ukj-")
    # The old synchronous `{"clusters": N}` must be gone: a count returned before
    # the pass ran would be a lie the browser then renders.
    assert "clusters" not in body
    # The click must not have done the work on the request thread.
    assert len(called) == 1
    assert called[0][1] == (nb, body["job_id"])
    assert called[0][2]["name"] == f"unifiedkg-{nb}"


def test_unified_rebuild_no_llm_check(client, monkeypatch):
    """Clustering is deterministic — it must succeed with no LLM configured."""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    bind_chat_client(real_repo, "kg_extract", MagicMock(configured=False))
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    assert client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").status_code == 200


def test_unified_rebuild_second_click_returns_409_while_running(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    submitted = []
    monkeypatch.setattr(
        background_jobs, "submit", lambda *a, **k: submitted.append(a),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    first = client.post(f"/api/notebooks/{nb}/unified-kg/rebuild")
    second = client.post(f"/api/notebooks/{nb}/unified-kg/rebuild")

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.headers.get("X-User-Message")     # 中文文案经 user_error 上屏
    assert "重新合并" in second.json()["detail"]
    assert len(submitted) == 1

    other = client.post("/api/notebooks", json={"name": "nb2"}).json()["id"]
    assert client.post(f"/api/notebooks/{other}/unified-kg/rebuild").status_code == 200


def test_relink_and_rebuild_409_each_other_and_name_the_real_holder(
    client, monkeypatch
):
    """One shared slot: both passes rewrite the clusters/communities. The 409 must
    name the action actually running, or the user waits on the wrong button."""
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    assert client.post(f"/api/notebooks/{nb}/kg/relink").status_code == 200
    blocked = client.post(f"/api/notebooks/{nb}/unified-kg/rebuild")
    assert blocked.status_code == 409
    assert "补上关联" in blocked.json()["detail"]

    # Release the relink claim, then prove the block works the other way too.
    real_repo.fail_notebook_relink_submission(
        nb, client.get(f"/api/notebooks/{nb}/kg/relink/status").json()["job_id"]
    )
    assert client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").status_code == 200
    blocked = client.post(f"/api/notebooks/{nb}/kg/relink")
    assert blocked.status_code == 409
    assert "重新合并" in blocked.json()["detail"]


def test_unified_rebuild_submission_failure_releases_the_claim(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(deps, "repository", lambda: real_repo)
    no_raise_client = TestClient(client.app, raise_server_exceptions=False)

    assert no_raise_client.post(
        f"/api/notebooks/{nb}/unified-kg/rebuild"
    ).status_code == 500
    status = client.get(f"/api/notebooks/{nb}/unified-kg/rebuild/status").json()
    assert status["running"] is False
    assert status["status"] == "failed"

    monkeypatch.setattr(background_jobs, "submit", lambda *a, **k: None)
    assert client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").status_code == 200


def test_unified_rebuild_status_reports_idle_then_terminal_count(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    from app.api import deps
    from app.services import background_jobs
    real_repo = deps.repository()
    monkeypatch.setattr(deps, "repository", lambda: real_repo)

    idle = client.get(f"/api/notebooks/{nb}/unified-kg/rebuild/status")
    assert idle.status_code == 200
    assert idle.json() == {
        "job_id": "", "notebook_id": nb, "status": "idle", "running": False,
        "clusters": 0,
    }

    # Run the job body inline (submit is the only thing stubbed out) so the
    # terminal counter the browser polls for comes from a real rebuild pass.
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: fn(*args),
    )
    started = client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").json()
    done = client.get(f"/api/notebooks/{nb}/unified-kg/rebuild/status").json()
    assert done["job_id"] == started["job_id"]
    assert done["status"] == "succeeded"
    assert done["running"] is False


def test_unified_rebuild_status_404_unknown_notebook(client):
    assert client.get(
        "/api/notebooks/no-such/unified-kg/rebuild/status"
    ).status_code == 404


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

    repo._runtime.indexing_pipeline.require_write_admission = lambda _notebook_id: None
    repo._runtime.knowledge_lifecycle.rebuild_notebook_kg = fake_rebuild

    result = repo.rebuild_notebook_kg("nb-123")

    assert calls == ["nb-123"]
    assert result is build_result
