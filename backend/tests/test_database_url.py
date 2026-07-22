import importlib
from dataclasses import FrozenInstanceError

import pytest

from app.core.config import Settings


def _database_url_module():
    return importlib.import_module("app.core.database_url")


def test_sqlite_database_identity_keeps_the_default_database_path():
    database_url = _database_url_module()
    identity = database_url.database_identity("sqlite:///.local/silicon_notebook.db")

    assert identity == database_url.DatabaseIdentity(
        scheme="sqlite",
        host=None,
        port=None,
        database=".local/silicon_notebook.db",
    )
    with pytest.raises(FrozenInstanceError):
        identity.database = "other"  # type: ignore[misc]


def test_postgresql_url_normalizes_only_the_legacy_scheme():
    database_url = _database_url_module()
    raw = "postgres://user:p%40ss@db.example:5432/notebook?sslmode=require"

    assert database_url.normalize_database_url(raw) == (
        "postgresql://user:p%40ss@db.example:5432/notebook?sslmode=require"
    )
    assert database_url.database_identity(raw) == database_url.DatabaseIdentity(
        scheme="postgresql",
        host="db.example",
        port=5432,
        database="notebook",
    )


def test_postgresql_url_parses_ipv6_and_percent_encoded_credentials_without_logging_them():
    database_url = _database_url_module()
    raw = (
        "postgresql://user%40example:p%40ss%3Aword@"
        "[2001:db8::1]:5433/notebook?sslmode=require&application_name=worker"
    )

    assert database_url.normalize_database_url(raw) == raw
    assert database_url.database_identity(raw) == database_url.DatabaseIdentity(
        scheme="postgresql",
        host="2001:db8::1",
        port=5433,
        database="notebook",
    )
    assert database_url.redact_database_url(raw) == "postgresql://[2001:db8::1]:5433/notebook"


@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://db.example",
        "postgresql://db.example/",
        "mysql://user:pass@db.example/notebook",
    ],
)
def test_database_urls_fail_closed_for_missing_database_or_unknown_scheme(raw):
    database_url = _database_url_module()
    with pytest.raises(ValueError):
        database_url.normalize_database_url(raw)


def test_settings_keep_sqlite_active_when_only_shadow_postgres_is_configured(monkeypatch):
    database_url = _database_url_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SHADOW_DATABASE_URL", raising=False)
    default_settings = Settings()
    monkeypatch.setenv(
        "SHADOW_DATABASE_URL",
        "postgres://user:pass@shadow.example:5432/notebook?sslmode=require",
    )

    settings = Settings()

    assert database_url.database_identity(settings.database_url) == database_url.database_identity(
        default_settings.database_url
    )
    assert database_url.database_identity(settings.database_url).scheme == "sqlite"
    assert settings.shadow_database_url == (
        "postgresql://user:pass@shadow.example:5432/notebook?sslmode=require"
    )
    assert settings.postgres_pool_min_size == 1
    assert settings.postgres_pool_max_size == 10
    assert settings.postgres_pool_acquire_timeout_seconds == 10
    assert settings.postgres_statement_timeout_seconds == 30
    assert settings.postgres_lock_timeout_seconds == 5


def test_settings_repr_redacts_active_and_shadow_database_credentials(monkeypatch):
    active = "postgresql://active-user:active-secret@db.example:5432/notebook?sslmode=require"
    shadow = "postgresql://shadow-user:shadow-secret@shadow.example:5432/notebook"
    monkeypatch.setenv("DATABASE_URL", active)
    monkeypatch.setenv("SHADOW_DATABASE_URL", shadow)

    diagnostic = repr(Settings())

    assert "active-secret" not in diagnostic
    assert "shadow-secret" not in diagnostic
    assert "active-user" not in diagnostic
    assert "shadow-user" not in diagnostic
    assert "postgresql://db.example:5432/notebook" in diagnostic
    assert "postgresql://shadow.example:5432/notebook" in diagnostic
