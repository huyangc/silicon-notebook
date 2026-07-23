from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from tests.postgres.conftest import (
    _database_catalog,
    _require_dedicated_test_database,
    _safe_ascii_text,
    _url_with_search_path,
    _validate_database_catalog,
)
from tests.postgres.lane import (
    _password_free_url,
    _pgpass_entry,
    _prepare_target,
    _pytest_environment,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_postgres_gate(url: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHON_BIN"] = sys.executable
    env["TEST_POSTGRES_URL"] = url
    env.pop("TEST_POSTGRES_NON_C_URL", None)
    env.pop("TEST_POSTGRES_NON_UTF_URL", None)
    env.pop("POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED", None)
    return subprocess.run(
        ["bash", "scripts/check_postgres.sh"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )


def test_psycopg_binary_and_pool_have_explicit_compatible_major_ranges():
    requirements = {
        line.split("#", 1)[0].strip()
        for line in (REPO_ROOT / "backend" / "requirements.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.split("#", 1)[0].strip()
    }
    assert "psycopg[binary]>=3.2,<4" in requirements
    assert "psycopg-pool>=3.2,<4" in requirements
    assert not any(line.startswith("psycopg[binary,pool]") for line in requirements)


def test_scoped_url_adds_one_search_path_without_losing_other_options():
    schema = f"sn_t4_{'a' * 32}"
    scoped = _url_with_search_path(
        "postgresql://postgres@db.example/silicon_notebook_task4_test?sslmode=require",
        schema,
    )
    query = parse_qsl(urlsplit(scoped).query, keep_blank_values=True)
    assert query == [("sslmode", "require"), ("options", f"-csearch_path={schema}")]


@pytest.mark.parametrize("key", ["options", "OPTIONS", "Options"])
def test_scoped_url_rejects_existing_libpq_options(key):
    schema = f"sn_t4_{'b' * 32}"
    with pytest.raises(RuntimeError, match="options"):
        _url_with_search_path(
            "postgresql://postgres@db.example/silicon_notebook_task4_test"
            f"?{key}=-cstatement_timeout%3D0",
            schema,
        )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://postgres@db.example/postgres",
        "postgresql://postgres@db.example/silicon_notebook_production",
        "postgresql://postgres@db.example/silicon_notebook_task4",
    ],
)
def test_fixture_refuses_non_dedicated_database_names(url):
    with pytest.raises(RuntimeError, match="dedicated"):
        _require_dedicated_test_database(url)


def test_fixture_refuses_server_database_identity_mismatch():
    url = "postgresql://postgres@db.example/silicon_notebook_task4_test"
    with pytest.raises(RuntimeError, match="does not match"):
        _require_dedicated_test_database(url, "silicon_notebook_other_test")


def test_ascii_text_normalizer_decodes_sql_ascii_bytes_without_repr_coercion():
    assert _safe_ascii_text(b"silicon_notebook_non_utf_test", "database") == (
        "silicon_notebook_non_utf_test"
    )
    assert _safe_ascii_text(b"SQL_ASCII", "encoding") == "SQL_ASCII"
    with pytest.raises(RuntimeError, match="ASCII"):
        _safe_ascii_text(b"bad-\xff", "database")


@pytest.mark.parametrize("locale_key", ["daticulocale", "datlocale"])
def test_database_catalog_accepts_pg16_and_pg17_icu_locale_keys(locale_key):
    row = {
        "database": b"silicon_notebook_non_c_test",
        "encoding": b"UTF8",
        "catalog": {
            "datlocprovider": b"i",
            locale_key: b"en-US",
            # PostgreSQL 17 may expose C here for ICU databases; the provider
            # locale, not the libc compatibility fields, owns this check.
            "datcollate": b"C",
            "datctype": b"C",
        },
    }
    catalog = _database_catalog(row)
    _validate_database_catalog(catalog, expected="non-c")
    assert catalog.provider_locale == "en-US"


def test_database_catalog_validates_libc_locale_fields_for_non_c_target():
    catalog = _database_catalog(
        {
            "database": "silicon_notebook_non_c_test",
            "encoding": "UTF8",
            "catalog": {
                "datlocprovider": "c",
                "datcollate": "en_US.UTF-8",
                "datctype": "en_US.UTF-8",
            },
        }
    )
    _validate_database_catalog(catalog, expected="non-c")


def test_database_catalog_rejects_c_icu_provider_locale_for_non_c_target():
    catalog = _database_catalog(
        {
            "database": "silicon_notebook_non_c_test",
            "encoding": "UTF8",
            "catalog": {
                "datlocprovider": "i",
                "datlocale": "C",
                "datcollate": "en_US.UTF-8",
                "datctype": "en_US.UTF-8",
            },
        }
    )
    with pytest.raises(RuntimeError, match="provider locale"):
        _validate_database_catalog(catalog, expected="non-c")


def test_password_free_url_and_pgpass_entry_support_distinct_url_credentials():
    url = (
        "postgresql://lane_user:p%40ss%3Aword@[::1]:55432/"
        "silicon_notebook_lane_test?sslmode=require"
    )
    sanitized, password = _password_free_url(url)
    assert sanitized == (
        "postgresql://lane_user@[::1]:55432/silicon_notebook_lane_test?sslmode=require"
    )
    assert password == "p@ss:word"
    assert _pgpass_entry(url, password) == (
        "\\:\\:1:55432:silicon_notebook_lane_test:lane_user:p@ss\\:word"
    )


def test_pytest_environment_contains_only_password_free_urls_and_mode_0600_pgpass(
    monkeypatch,
):
    primary_secret = "sentinel-primary-secret"
    auxiliary_secret = "sentinel-auxiliary-secret"
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        f"postgresql://lane_user:{primary_secret}@db.example/"
        "silicon_notebook_lane_test",
    )
    monkeypatch.setenv(
        "TEST_POSTGRES_NON_C_URL",
        f"postgresql://other_user:{auxiliary_secret}@db.example/"
        "silicon_notebook_non_c_test",
    )
    targets = [
        _prepare_target("primary", "TEST_POSTGRES_URL", "utf8"),
        _prepare_target("non-C", "TEST_POSTGRES_NON_C_URL", "non-c"),
    ]
    assert all(target is not None for target in targets)
    with _pytest_environment([target for target in targets if target is not None]) as env:
        joined_urls = env["TEST_POSTGRES_URL"] + env["TEST_POSTGRES_NON_C_URL"]
        assert primary_secret not in joined_urls
        assert auxiliary_secret not in joined_urls
        assert "PGPASSWORD" not in env
        pgpass_path = Path(env["PGPASSFILE"])
        assert pgpass_path.stat().st_mode & 0o777 == 0o600
        pgpass = pgpass_path.read_text(encoding="utf-8")
        assert primary_secret in pgpass
        assert auxiliary_secret in pgpass
    assert not pgpass_path.exists()


@pytest.mark.parametrize(
    "url",
    [
        (
            "postgresql://lane_user:sentinel-options-secret@127.0.0.1:1/"
            "silicon_notebook_lane_test?OPTIONS=-cstatement_timeout%3D0"
        ),
        (
            "postgresql://lane_user:sentinel-malformed-secret@[::1/"
            "silicon_notebook_lane_test"
        ),
    ],
)
def test_gate_invalid_urls_fail_without_raw_url_secret_or_traceback(url):
    completed = _run_postgres_gate(url)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "invalid database configuration" in output
    assert url not in output
    assert "sentinel-" not in output
    assert "Traceback" not in output


def test_gate_connection_failure_prints_only_safe_identity():
    raw = (
        "postgresql://lane_user:sentinel-connect-secret@127.0.0.1:1/"
        "silicon_notebook_connection_test"
    )
    completed = _run_postgres_gate(raw)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "database=postgresql host=127.0.0.1:1" in output
    assert "db=silicon_notebook_connection_test" in output
    assert raw not in output
    assert "sentinel-connect-secret" not in output
    assert "Traceback" not in output


def test_gate_validates_auxiliary_url_policy_before_connecting_primary():
    env = os.environ.copy()
    env["PYTHON_BIN"] = sys.executable
    env["TEST_POSTGRES_URL"] = (
        "postgresql://lane_user:sentinel-primary@127.0.0.1:1/"
        "silicon_notebook_lane_test"
    )
    env["TEST_POSTGRES_NON_C_URL"] = (
        "postgresql://lane_user:sentinel-aux@127.0.0.1:1/"
        "silicon_notebook_non_c_test?options=-csearch_path%3Dpublic"
    )
    env.pop("TEST_POSTGRES_NON_UTF_URL", None)
    completed = subprocess.run(
        ["bash", "scripts/check_postgres.sh"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "PostgreSQL non-C UTF8 preflight failed: invalid database configuration" in output
    assert "connection or identity check failed" not in output
    assert "sentinel-" not in output
    assert "Traceback" not in output


def test_sqlite_default_subprocess_does_not_import_psycopg(tmp_path):
    env = os.environ.copy()
    env.pop("TEST_POSTGRES_URL", None)
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    sqlite_url = f"sqlite:///{tmp_path / 'offline-default.db'}"
    program = f"""
import sys
from app.core.config import Settings
from app.repositories.factory import create_repository
repo = create_repository(Settings(database_url={sqlite_url!r}))
assert not any(name == 'psycopg' or name.startswith('psycopg.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
