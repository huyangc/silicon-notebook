"""The two deployment rails of the web notebook export / import
(``NOTEBOOK_TRANSFER_WEB_MAX_MB``, ``NOTEBOOK_TRANSFER_TTL_SECONDS``) and the byte
limit the browser reads from ``GET /api/system/config``."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings


def test_settings_defaults_and_rails():
    settings = Settings(_env_file=None)
    assert settings.notebook_transfer_web_max_mb == 512
    assert settings.notebook_transfer_web_max_bytes == 512 * 1024 * 1024
    assert settings.notebook_transfer_ttl_seconds == 86400
    for value in ("0", "1.5", "1025"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, NOTEBOOK_TRANSFER_WEB_MAX_MB=value)
    for value in ("599", "604801"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, NOTEBOOK_TRANSFER_TTL_SECONDS=value)
    assert Settings(
        _env_file=None, NOTEBOOK_TRANSFER_WEB_MAX_MB="1024"
    ).notebook_transfer_web_max_bytes == 1024 * 1024 * 1024
    assert Settings(
        _env_file=None, NOTEBOOK_TRANSFER_TTL_SECONDS="600"
    ).notebook_transfer_ttl_seconds == 600


def test_system_config_exposes_the_web_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'config.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("NOTEBOOK_TRANSFER_WEB_MAX_MB", "3")
    from app.api import deps
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        client = TestClient(create_app())
        response = client.post(
            "/api/auth/register", json={"username": "c10000003", "password": "pw"}
        )
        assert response.status_code == 200, response.text
        headers = {"Authorization": f"Bearer {response.json()['token']}"}
        assert client.get("/api/system/config").status_code == 401
        payload = client.get("/api/system/config", headers=headers).json()
        assert payload["notebook_transfer_max_bytes"] == 3 * 1024 * 1024
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()
