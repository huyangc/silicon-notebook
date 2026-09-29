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


def _auth_settings(**overrides):
    from app.core.config import Settings
    return Settings(_env_file=None, **overrides)


def test_intranet_http_auth_origin_rejected_without_opt_in():
    import pytest

    for field in ("auth_public_base_url", "auth_frontend_base_url"):
        with pytest.raises(ValueError, match="AUTH_ALLOW_INSECURE_HTTP"):
            _auth_settings(**{field: "http://notebook.corp.example"})


def test_loopback_http_auth_origin_needs_no_opt_in():
    s = _auth_settings(auth_public_base_url="http://127.0.0.1:8000",
                       auth_frontend_base_url="http://localhost:3000")
    assert s.auth_allow_insecure_http is False
    assert s.auth_public_base_url == "http://127.0.0.1:8000"


def test_intranet_http_auth_origin_accepted_with_opt_in(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_INSECURE_HTTP", "true")
    monkeypatch.setenv("AUTH_PUBLIC_BASE_URL", "http://notebook.corp.example/")
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.auth_allow_insecure_http is True
    assert s.auth_public_base_url == "http://notebook.corp.example"


def test_opt_in_keeps_the_rest_of_origin_shape_checks():
    import pytest

    for bad in ("ftp://notebook.corp.example", "http://notebook.corp.example/app",
                "http://user@notebook.corp.example", "http://notebook.corp.example?x=1"):
        with pytest.raises(ValueError):
            _auth_settings(auth_allow_insecure_http=True, auth_public_base_url=bad)
