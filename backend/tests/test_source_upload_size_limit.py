"""Per-source file-size limit: one Settings value, one browser mirror, one 413 guard."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_source_upload_limit_defaults_to_50_binary_megabytes():
    settings = Settings(_env_file=None)
    assert settings.source_upload_max_mb == 50
    assert settings.source_upload_max_bytes == 50 * 1024 * 1024


def test_source_upload_limit_must_be_a_positive_supported_whole_megabyte_value():
    for value in ("0", "1.5", "1025"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, SOURCE_UPLOAD_MAX_MB=value)

    assert Settings(_env_file=None, SOURCE_UPLOAD_MAX_MB="1024").source_upload_max_bytes == (
        1024 * 1024 * 1024
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/upload-limit.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SOURCE_UPLOAD_MAX_MB", "1")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    from app.api import deps, source_routes
    from app.main import create_app

    deps.repository.cache_clear()
    monkeypatch.setattr(source_routes.kg_scheduler, "submit_job", lambda *_args, **_kwargs: None)
    return TestClient(create_app())


def _auth(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/register", json={"username": "z00123456", "password": "pw"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_authenticated_system_config_exposes_upload_guards(client):
    assert client.get("/api/system/config").status_code == 401

    response = client.get("/api/system/config", headers=_auth(client))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "source_upload_max_bytes": 1024 * 1024,
        "source_upload_max_files_per_batch": 20,
        "user_activity_view_enabled": True,
    }


def test_source_multipart_auth_runs_before_the_bounded_body_parser(client):
    response = client.post(
        "/api/notebooks/not-readable/sources",
        content=b"not even a valid multipart body",
        headers={"Content-Type": "multipart/form-data"},
    )
    assert response.status_code == 401


def test_authenticated_upload_without_multipart_content_type_returns_400(client):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        content=b"not a multipart body",
        headers=headers,
    )

    assert response.status_code == 400
    assert response.headers.get("X-User-Message") == "1"
    assert "multipart/form-data" in response.json()["detail"]


def test_upload_batch_above_fixed_file_count_guard_is_rejected_before_orchestration(
    client, monkeypatch
):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    from app.api import source_routes

    monkeypatch.setattr(
        source_routes.source_repository(),
        "upload_sources",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized multipart batch reached source persistence"
        ),
    )
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        files=[
            ("files", (f"source-{index}.md", b"x", "text/markdown"))
            for index in range(21)
        ],
        headers=headers,
    )

    assert response.status_code == 413
    assert response.headers.get("X-User-Message") == "1"
    assert "20" in response.json()["detail"]


def test_upload_metadata_fields_are_tightly_bounded_before_persistence(client, monkeypatch):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    from app.api import source_routes

    monkeypatch.setattr(
        source_routes.source_repository(),
        "upload_sources",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized multipart metadata reached source persistence"
        ),
    )
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        files=[("files", ("source.md", b"x", "text/markdown"))]
        + [("doc_types", (None, "")) for _ in range(41)],
        headers=headers,
    )

    assert response.status_code == 413
    assert response.headers.get("X-User-Message") == "1"


def test_upload_rejects_unknown_metadata_fields(client, monkeypatch):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    from app.api import source_routes

    monkeypatch.setattr(
        source_routes.source_repository(),
        "upload_sources",
        lambda *_args, **_kwargs: pytest.fail(
            "unexpected multipart metadata reached source persistence"
        ),
    )
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        files=[
            ("files", ("source.md", b"x", "text/markdown")),
            ("unexpected", (None, "value")),
        ],
        headers=headers,
    )

    assert response.status_code == 422


def test_upload_exactly_at_configured_single_file_limit_is_accepted(client):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        files=[("files", ("fits.md", b"x" * (1024 * 1024), "text/markdown"))],
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(response.json()) == 1


def test_upload_over_configured_single_file_limit_is_authoritatively_rejected(
    client, monkeypatch
):
    headers = _auth(client)
    notebook = client.post("/api/notebooks", json={"name": "n"}, headers=headers).json()
    from app.api import source_routes

    # The streaming multipart guard must reject before route orchestration or
    # repository persistence starts, not after reading the whole part into RAM.
    monkeypatch.setattr(
        source_routes.source_repository(),
        "upload_sources",
        lambda *_args, **_kwargs: pytest.fail(
            "oversized multipart reached source persistence"
        ),
    )
    response = client.post(
        f"/api/notebooks/{notebook['id']}/sources",
        files=[("files", ("too-large.md", b"x" * (1024 * 1024 + 1), "text/markdown"))],
        headers=headers,
    )

    assert response.status_code == 413
    assert response.headers.get("X-User-Message") == "1"
    assert "1 MB" in response.json()["detail"]
    assert str(1024 * 1024) in response.json()["detail"]
