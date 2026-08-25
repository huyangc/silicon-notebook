from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    from app.core.config import get_settings
    from app.api import deps
    from app.extensions import default_extension_runtime

    get_settings.cache_clear()
    deps.repository.cache_clear()
    default_extension_runtime.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


def _notebook(client: TestClient) -> str:
    response = client.post("/api/notebooks", json={"name": "pipeline api"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_reader_projection_is_sanitized_and_builtin_is_selected(client):
    notebook_id = _notebook(client)

    response = client.get(f"/api/notebooks/{notebook_id}/indexing-pipeline")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "pipeline_id",
        "version",
        "available",
        "missing",
        "pending",
        "options",
        "changed",
        "warning_count",
        "rebuild_status",
        "job_id",
    }
    assert body["pipeline_id"] is None
    assert body["available"] is True
    assert body["missing"] is False
    assert body["options"][0]["label"] == "内建管线"
    serialized = response.text.lower()
    for forbidden in ("reason", "exception", "endpoint", "credential", "path"):
        assert forbidden not in serialized
    summary = client.get(f"/api/notebooks/{notebook_id}").json()
    assert summary["indexing_pipeline_id"] is None
    assert summary["indexing_pipeline_version"] == "builtin.chunk.v1"
    assert summary["indexing_pipeline_available"] is True
    assert summary["indexing_pipeline_missing"] is False
    assert summary["indexing_pipeline_pending"] is False
    assert summary["indexing_pipeline_stale"] is False


def test_patch_null_is_idempotent_and_does_not_create_a_job(client):
    notebook_id = _notebook(client)

    response = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": None},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert response.json()["pending"] is False
    assert response.json()["job_id"] is None


def test_missing_selected_plugin_keeps_get_readable_and_new_index_write_is_409(
    client,
):
    notebook_id = _notebook(client)
    from app.api.deps import repository

    repo = repository()
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline=?,"
            "indexing_pipeline_version=?,indexing_pipeline_generation=?,"
            "indexing_pipeline_job_id='' WHERE id=?",
            ("removed.pipeline", "v7", "old-generation", notebook_id),
        )
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,updated_at,indexing_pipeline_id,"
            "indexing_pipeline_version) VALUES (?,?,?,?,?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET "
            "indexing_pipeline_id=excluded.indexing_pipeline_id,"
            "indexing_pipeline_version=excluded.indexing_pipeline_version",
            (notebook_id, 0, repo._runtime.seams.now(), "removed.pipeline", "v7"),
        )

    readable = client.get(f"/api/notebooks/{notebook_id}/indexing-pipeline")
    blocked = client.post(
        f"/api/notebooks/{notebook_id}/scale-index/rebuild",
        json={"when": "now", "mode": "full"},
    )

    assert readable.status_code == 200
    assert readable.json()["missing"] is True
    assert readable.json()["available"] is False
    assert blocked.status_code == 409
    assert blocked.headers["X-User-Message"] == "1"
    assert "旧索引仍可读取" in blocked.json()["detail"]
    summary = client.get(f"/api/notebooks/{notebook_id}").json()
    assert summary["indexing_pipeline_id"] == "removed.pipeline"
    assert summary["indexing_pipeline_version"] == "v7"
    assert summary["indexing_pipeline_available"] is False
    assert summary["indexing_pipeline_missing"] is True
    assert summary["indexing_pipeline_pending"] is False
    assert summary["indexing_pipeline_stale"] is False
