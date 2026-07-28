import importlib
import pytest


def _database_url_module():
    return importlib.import_module("app.core.database_url")


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
