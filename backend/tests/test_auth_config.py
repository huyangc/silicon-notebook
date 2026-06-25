import importlib


def test_auth_settings_defaults(monkeypatch):
    monkeypatch.delenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", raising=False)
    from app.core.config import Settings
    s = Settings()
    assert s.admin_password == "admin"
    assert s.auth_optional is False


def test_auth_settings_env(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    from app.core.config import Settings
    s = Settings()
    assert s.admin_password == "s3cret"
    assert s.auth_optional is True
