from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import psycopg
import pytest

from tests.postgres import lane as postgres_lane
from tests.postgres.conftest import (
    _database_catalog,
    _require_dedicated_test_database,
    _safe_ascii_text,
    _url_with_search_path,
    _validate_database_catalog,
)
from tests.postgres.lane import (
    _create_pgpass_file,
    _password_free_url,
    _pgpass_entry,
    _prepare_target,
    _pytest_environment,
    _run_pytest,
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
    assert _pgpass_entry(url, r"colon:and\backslash") == (
        "\\:\\:1:55432:silicon_notebook_lane_test:lane_user:"
        "colon\\:and\\\\backslash"
    )
    with pytest.raises(RuntimeError, match="exact"):
        _pgpass_entry(
            "postgresql://%2A@db.example/silicon_notebook_lane_test",
            "password",
        )
    with pytest.raises(RuntimeError, match="exact"):
        _password_free_url(
            "postgresql://%2A@db.example/silicon_notebook_lane_test"
        )


@pytest.mark.parametrize(
    "query",
    [
        "password=sentinel-query-password",
        "sslpassword=sentinel-query-sslpassword",
        "host=override.example",
        "hostaddr=127.0.0.2",
        "port=55433",
        "user=override_user",
        "dbname=silicon_notebook_override_test",
        "service=override_service",
        "servicefile=%2Ftmp%2Foverride-service",
        "passfile=%2Ftmp%2Foverride-passfile",
        "options=-csearch_path%3Dpublic",
        "PASS%57ORD=sentinel-encoded-password",
        "sslmode=require&SSLMODE=disable",
        "application_name=unreviewed",
        "pass%ZZword=malformed-percent",
    ],
    ids=[
        "password",
        "sslpassword",
        "host",
        "hostaddr",
        "port",
        "user",
        "dbname",
        "service",
        "servicefile",
        "passfile",
        "options",
        "encoded-mixed-case-password",
        "repeated-sslmode",
        "unknown-key",
        "malformed-percent",
    ],
)
def test_connection_url_policy_rejects_identity_credential_and_unknown_overrides(query):
    raw = (
        "postgresql://lane_user:sentinel-authority-secret@127.0.0.1:1/"
        f"silicon_notebook_lane_test?{query}"
    )
    completed = _run_postgres_gate(raw)
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "invalid database configuration" in output
    assert "connection or identity check failed" not in output
    assert raw not in output
    assert "sentinel-" not in output
    assert "Traceback" not in output


def test_password_free_url_canonicalizes_only_the_reviewed_sslmode_behavior():
    sanitized, password = _password_free_url(
        "postgresql://lane_user:secret@db.example/"
        "silicon_notebook_lane_test?SSLMODE=verify-full"
    )
    assert sanitized == (
        "postgresql://lane_user@db.example:5432/"
        "silicon_notebook_lane_test?sslmode=verify-full"
    )
    assert password == "secret"
    effective = psycopg.conninfo.conninfo_to_dict(sanitized)
    assert effective == {
        "dbname": "silicon_notebook_lane_test",
        "host": "db.example",
        "port": "5432",
        "user": "lane_user",
        "sslmode": "verify-full",
    }


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


def test_pytest_child_environment_drops_parent_database_and_libpq_secrets(
    monkeypatch,
    tmp_path,
):
    inherited_pgpass = tmp_path / "parent.pgpass"
    inherited_pgpass.write_text(
        "*:*:*:*:sentinel-inherited-passfile-secret\n", encoding="utf-8"
    )
    secret_env = {
        "DATABASE_URL": "postgresql://u:sentinel-database-url@bad/db",
        "SHADOW_DATABASE_URL": "postgresql://u:sentinel-shadow-url@bad/db",
        "PGPASSWORD": "sentinel-pgpassword",
        "PGHOST": "sentinel-pghost",
        "PGHOSTADDR": "sentinel-pghostaddr",
        "PGPORT": "6543",
        "PGUSER": "sentinel-pguser",
        "PGDATABASE": "sentinel-pgdatabase",
        "PGSERVICE": "sentinel-pgservice",
        "PGSERVICEFILE": "sentinel-pgservicefile",
        "PGOPTIONS": "sentinel-pgoptions",
        "PGSSLPASSWORD": "sentinel-pgsslpassword",
        "PGPASSFILE": str(inherited_pgpass),
        "POSTGRES_PASSWORD": "sentinel-postgres-password",
    }
    for key, value in secret_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user:sentinel-target-password@db.example/"
        "silicon_notebook_lane_test",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    failed = _run_postgres_gate(
        "postgresql://lane_user:sentinel-gate-target@127.0.0.1:1/"
        "silicon_notebook_lane_test?hostaddr=127.0.0.2"
    )
    complete_output = failed.stdout + failed.stderr
    assert failed.returncode != 0
    assert "sentinel-" not in complete_output
    assert "Traceback" not in complete_output
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(args[0], 0, "", "")

    monkeypatch.setattr(postgres_lane.subprocess, "run", fake_run)
    assert _run_pytest([target]) == 0
    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert set(child_env) <= (
        postgres_lane._CHILD_ENV_ALLOWLIST
        | {
            "PYTHONPATH",
            "SILICON_NOTEBOOK_ENV_FILE",
            "TEST_POSTGRES_URL",
            "PGPASSFILE",
        }
    )
    forbidden_keys = (set(secret_env) - {"PGPASSFILE"}) | {
        "POSTGRES_USER",
        "POSTGRES_DB",
    }
    assert forbidden_keys.isdisjoint(child_env)
    assert child_env["PGPASSFILE"] != str(inherited_pgpass)
    rendered = repr(child_env)
    for sentinel in (
        "sentinel-database-url",
        "sentinel-shadow-url",
        "sentinel-pgpassword",
        "sentinel-pgservice",
        "sentinel-target-password",
        "sentinel-postgres-password",
    ):
        assert sentinel not in rendered
    assert not Path(child_env["PGPASSFILE"]).exists()


def test_generated_pgpass_exact_entries_precede_inherited_wildcards(
    monkeypatch,
    tmp_path,
):
    inherited = tmp_path / "inherited.pgpass"
    inherited.write_text("*:*:*:*:wrong-wildcard-password\n", encoding="utf-8")
    monkeypatch.setenv("PGPASSFILE", str(inherited))
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user:correct-target-password@db.example:55432/"
        "silicon_notebook_lane_test",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    with _pytest_environment([target]) as env:
        lines = Path(env["PGPASSFILE"]).read_text(encoding="utf-8").splitlines()
        assert lines == [
            "db.example:55432:silicon_notebook_lane_test:lane_user:correct-target-password",
            "*:*:*:*:wrong-wildcard-password",
        ]


def test_passwordless_target_gets_explicit_empty_pgpass_not_home_fallback(
    monkeypatch,
):
    monkeypatch.delenv("PGPASSWORD", raising=False)
    monkeypatch.delenv("PGPASSFILE", raising=False)
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user@db.example/silicon_notebook_lane_test",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    with _pytest_environment([target]) as env:
        pgpass_path = Path(env["PGPASSFILE"])
        assert pgpass_path.exists()
        assert pgpass_path.read_bytes() == b""
        assert pgpass_path.name.startswith("silicon-notebook-pgpass-")
    assert not pgpass_path.exists()


def test_partial_pgpass_write_failure_closes_once_unlinks_and_logs_no_secret(
    monkeypatch,
    capsys,
):
    created: list[Path] = []
    original_mkstemp = postgres_lane.tempfile.mkstemp
    original_close = postgres_lane.os.close
    write_calls = 0
    close_calls = 0

    def recording_mkstemp(*args, **kwargs):
        descriptor, raw_path = original_mkstemp(*args, **kwargs)
        created.append(Path(raw_path))
        return descriptor, raw_path

    def partial_write(descriptor, payload):
        nonlocal write_calls
        write_calls += 1
        if write_calls == 1:
            return original_write(descriptor, payload[:4])
        raise OSError("sentinel-partial-write-secret")

    def recording_close(descriptor):
        nonlocal close_calls
        close_calls += 1
        original_close(descriptor)

    original_write = postgres_lane.os.write
    monkeypatch.setattr(postgres_lane.tempfile, "mkstemp", recording_mkstemp)
    with pytest.raises(RuntimeError, match="credential file"):
        _create_pgpass_file(
            b"sentinel-pgpass-payload",
            write_fn=partial_write,
            close_fn=recording_close,
        )
    assert write_calls == 2
    assert close_calls == 1
    assert created and not created[0].exists()
    captured = capsys.readouterr()
    assert "sentinel-" not in (captured.out + captured.err)


def test_pgpass_close_failure_still_unlinks_and_does_not_double_close(
    monkeypatch,
    capsys,
):
    created: list[Path] = []
    original_mkstemp = postgres_lane.tempfile.mkstemp
    original_close = postgres_lane.os.close
    close_calls = 0

    def recording_mkstemp(*args, **kwargs):
        descriptor, raw_path = original_mkstemp(*args, **kwargs)
        created.append(Path(raw_path))
        return descriptor, raw_path

    def failing_close(descriptor):
        nonlocal close_calls
        close_calls += 1
        original_close(descriptor)
        raise OSError("sentinel-close-secret")

    monkeypatch.setattr(postgres_lane.tempfile, "mkstemp", recording_mkstemp)
    with pytest.raises(RuntimeError, match="credential file"):
        _create_pgpass_file(
            b"sentinel-pgpass-payload",
            close_fn=failing_close,
        )
    assert close_calls == 1
    assert created and not created[0].exists()
    captured = capsys.readouterr()
    assert "sentinel-" not in (captured.out + captured.err)


def test_pgpass_cleanup_retries_unlink_without_hiding_the_write_failure(
    monkeypatch,
    capsys,
):
    created: list[Path] = []
    original_mkstemp = postgres_lane.tempfile.mkstemp
    unlink_calls = 0

    def recording_mkstemp(*args, **kwargs):
        descriptor, raw_path = original_mkstemp(*args, **kwargs)
        created.append(Path(raw_path))
        return descriptor, raw_path

    def failing_write(_descriptor, _payload):
        raise OSError("sentinel-original-write-failure")

    def flaky_unlink(path: Path):
        nonlocal unlink_calls
        unlink_calls += 1
        if unlink_calls == 1:
            raise OSError("sentinel-first-unlink-failure")
        path.unlink()

    monkeypatch.setattr(postgres_lane.tempfile, "mkstemp", recording_mkstemp)
    with pytest.raises(RuntimeError, match="credential file"):
        _create_pgpass_file(
            b"sentinel-pgpass-payload",
            write_fn=failing_write,
            unlink_fn=flaky_unlink,
        )
    assert unlink_calls == 2
    assert created and not created[0].exists()
    captured = capsys.readouterr()
    assert "sentinel-" not in (captured.out + captured.err)


def test_pgpass_is_removed_when_pytest_launcher_is_interrupted(monkeypatch):
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user:interrupt-secret@db.example/"
        "silicon_notebook_lane_test",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    observed_path: Path | None = None
    with pytest.raises(KeyboardInterrupt):
        with _pytest_environment([target]) as env:
            observed_path = Path(env["PGPASSFILE"])
            assert observed_path.exists()
            raise KeyboardInterrupt
    assert observed_path is not None
    assert not observed_path.exists()


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
    ids=["options", "malformed"],
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
