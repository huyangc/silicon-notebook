from app.core.config import Settings


def test_cloud_enabled_and_defaults_with_token(monkeypatch):
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-123")
    s = Settings()
    assert s.mineru_cloud_enabled is True
    assert s.mineru_api_base == "https://mineru.net"
    assert s.mineru_cloud_model_version == "vlm"
    assert s.mineru_cloud_language == "ch"
    assert s.mineru_cloud_timeout_seconds == 600
    assert s.mineru_cloud_poll_interval_seconds == 5


def test_cloud_disabled_without_token(monkeypatch):
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    s = Settings()
    assert s.mineru_cloud_enabled is False
