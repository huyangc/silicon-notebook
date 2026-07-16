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


def test_mineru_image_retention_defaults(monkeypatch):
    from app.core.config import Settings
    for k in ("MINERU_RETURN_IMAGES", "MINERU_MAX_IMAGE_BYTES", "MINERU_MAX_IMAGES_PER_SOURCE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings()
    assert s.mineru_return_images is True
    assert s.mineru_max_image_bytes == 5 * 1024 * 1024
    assert s.mineru_max_images_per_source == 200


def test_mineru_return_images_env_off(monkeypatch):
    from app.core.config import Settings
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "0")
    assert Settings().mineru_return_images is False
