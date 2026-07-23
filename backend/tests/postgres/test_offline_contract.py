from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
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
    _run_isolated_gate,
    _run_preflight,
    _run_pytest,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


def _pgpass_fields(line: str) -> list[str]:
    fields: list[str] = []
    field: list[str] = []
    escaped = False
    for character in line.rstrip("\n"):
        if escaped:
            field.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == ":" and len(fields) < 4:
            fields.append("".join(field))
            field = []
        else:
            field.append(character)
    if escaped:
        raise RuntimeError("invalid pgpass escape")
    fields.append("".join(field))
    if len(fields) != 5:
        raise RuntimeError("invalid pgpass field count")
    return fields


def _current_target_password(url: str) -> str:
    parsed = urlsplit(url)
    expected = (
        parsed.hostname,
        str(parsed.port or 5432),
        parsed.path.removeprefix("/"),
        parsed.username,
    )
    pgpass_path = os.environ.get("PGPASSFILE")
    if not pgpass_path:
        raise RuntimeError("isolated PostgreSQL lane did not provide PGPASSFILE")
    for raw_line in Path(pgpass_path).read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        fields = _pgpass_fields(raw_line)
        if all(actual == "*" or actual == wanted for actual, wanted in zip(fields, expected)):
            return fields[4]
    raise RuntimeError("isolated PostgreSQL credential was not found")


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


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path.name}")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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

    def fake_child(command, child_env, *, capture_output=False):
        captured["args"] = (command,)
        captured["env"] = child_env
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(postgres_lane, "_run_child", fake_child)
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


def test_preflight_and_pytest_share_one_minimal_environment_and_pgpass(
    monkeypatch,
):
    poison = {
        "PGHOSTADDR": "127.0.0.2",
        "PGHOST": "poison-host",
        "PGPORT": "6543",
        "PGUSER": "poison-user",
        "PGDATABASE": "poison-database",
        "PGSERVICE": "poison-service",
        "PGSERVICEFILE": "/tmp/poison-service-file",
        "PGOPTIONS": "-csearch_path=poison",
        "PGSSLMODE": "verify-full",
        "PGTARGETSESSIONATTRS": "primary",
        "PGSSLPASSWORD": "sentinel-ssl-password",
        "DATABASE_URL": "postgresql://u:sentinel-parent-db@poison/db",
        "SHADOW_DATABASE_URL": "postgresql://u:sentinel-parent-shadow@poison/db",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user:sentinel-target-secret@db.example:55432/"
        "silicon_notebook_lane_test?sslmode=require",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_child(command, child_env, *, capture_output=False):
        calls.append((command, child_env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(postgres_lane, "_run_child", fake_child)
    assert _run_isolated_gate([target]) == 0
    assert len(calls) == 2
    preflight_command, preflight_env = calls[0]
    pytest_command, pytest_env = calls[1]
    assert preflight_command[-1] == "--preflight"
    assert "pytest" in pytest_command
    assert preflight_env is pytest_env
    assert set(poison).isdisjoint(preflight_env)
    assert preflight_env["TEST_POSTGRES_URL"] == (
        "postgresql://lane_user@db.example:55432/"
        "silicon_notebook_lane_test?sslmode=require"
    )
    rendered = repr(calls)
    assert "sentinel-" not in rendered
    assert "poison-" not in rendered
    assert not Path(preflight_env["PGPASSFILE"]).exists()


def test_signalled_child_exit_is_mapped_to_conventional_shell_status(
    monkeypatch,
):
    class SignalExitProcess:
        pid = 7654321
        returncode = -signal.SIGTERM

        def communicate(self):
            return "", ""

    monkeypatch.setattr(
        postgres_lane.subprocess,
        "Popen",
        lambda *args, **kwargs: SignalExitProcess(),
    )
    assert _run_isolated_gate([]) == 128 + signal.SIGTERM


def test_main_maps_keyboard_interrupt_without_generic_launcher_error(
    monkeypatch,
    capsys,
):
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user:sentinel-keyboard-secret@db.example/"
        "silicon_notebook_keyboard_test",
    )
    monkeypatch.delenv("TEST_POSTGRES_NON_C_URL", raising=False)
    monkeypatch.delenv("TEST_POSTGRES_NON_UTF_URL", raising=False)
    observed_path: Path | None = None

    def interrupted(_command, child_env, *, capture_output=False):
        nonlocal observed_path
        observed_path = Path(child_env["PGPASSFILE"])
        assert observed_path.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(postgres_lane, "_run_child", interrupted)
    assert postgres_lane.main() == 130
    assert observed_path is not None and not observed_path.exists()
    captured = capsys.readouterr()
    assert "launcher error" not in (captured.out + captured.err)
    assert "sentinel-" not in (captured.out + captured.err)


def test_launcher_restores_prior_signal_handlers():
    before = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    with postgres_lane._launcher_signal_handlers():
        assert all(
            signal.getsignal(signum) is not handler
            for signum, handler in before.items()
        )
    assert {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    } == before


def test_child_cleanup_uses_bounded_term_then_kill_and_reaps(monkeypatch):
    calls: list[tuple[str, int | float | None]] = []

    class StubbornProcess:
        pid = 7654321

        def poll(self):
            return None

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if len([entry for entry in calls if entry[0] == "wait"]) == 1:
                raise subprocess.TimeoutExpired("blocked-child", timeout)
            return -signal.SIGKILL

    def fake_killpg(_pid, signum):
        if signum == 0:
            raise ProcessLookupError
        calls.append(("signal", signum))

    monkeypatch.setattr(postgres_lane.os, "killpg", fake_killpg)
    postgres_lane._terminate_and_reap(StubbornProcess())
    assert calls == [
        ("signal", signal.SIGTERM),
        ("wait", postgres_lane._CHILD_REAP_TIMEOUT_SECONDS),
        ("signal", signal.SIGKILL),
        ("wait", postgres_lane._CHILD_REAP_TIMEOUT_SECONDS),
    ]


@pytest.mark.parametrize(
    ("phase", "signum", "expected_status"),
    [
        ("preflight", signal.SIGTERM, 143),
        ("pytest", signal.SIGTERM, 143),
        ("preflight", signal.SIGINT, 130),
        ("pytest", signal.SIGINT, 130),
    ],
)
def test_launcher_signal_reaps_blocked_child_and_removes_pgpass(
    tmp_path,
    phase,
    signum,
    expected_status,
):
    if os.name != "posix":
        return
    pid_file = tmp_path / "blocked-child.pid"
    blocker = [
        sys.executable,
        "-c",
        (
            "import os,time; from pathlib import Path; "
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            "time.sleep(60)"
        ),
    ]
    success = [sys.executable, "-c", "raise SystemExit(0)"]
    wrapper = (
        "from tests.postgres import lane; "
        f"lane._preflight_command=lambda: {blocker if phase == 'preflight' else success!r}; "
        f"lane._pytest_command=lambda: {blocker if phase == 'pytest' else success!r}; "
        "raise SystemExit(lane.main())"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "backend")
    env["TMPDIR"] = str(tmp_path)
    env["TEST_POSTGRES_URL"] = (
        "postgresql://lane_user:sentinel-signal-secret@db.example/"
        "silicon_notebook_signal_test"
    )
    env.pop("TEST_POSTGRES_NON_C_URL", None)
    env.pop("TEST_POSTGRES_NON_UTF_URL", None)
    launcher = subprocess.Popen(
        [sys.executable, "-c", wrapper],
        cwd=REPO_ROOT / "backend",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    child_pid: int | None = None
    try:
        _wait_for_file(pid_file)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        assert list(tmp_path.glob("silicon-notebook-pgpass-*"))
        launcher.send_signal(signum)
        stdout, stderr = launcher.communicate(timeout=10)
    finally:
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=5)
        if child_pid is not None and _process_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)
    output = stdout + stderr
    assert launcher.returncode == expected_status
    assert child_pid is not None and not _process_exists(child_pid)
    assert not list(tmp_path.glob("silicon-notebook-pgpass-*"))
    assert "sentinel-" not in output
    assert "Traceback" not in output


@pytest.mark.postgres_integration
def test_valid_preflight_ignores_poisoned_parent_libpq_environment(monkeypatch):
    current_url = os.environ["TEST_POSTGRES_URL"]
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None
    poison = {
        "PGHOSTADDR": "203.0.113.7",
        "PGHOST": "definitely.invalid",
        "PGPORT": "1",
        "PGUSER": "poison-user",
        "PGDATABASE": "poison-database",
        "PGSERVICE": "poison-service",
        "PGSERVICEFILE": "/does/not/exist",
        "PGOPTIONS": "-csearch_path=poison",
        "PGSSLMODE": "verify-full",
        "PGTARGETSESSIONATTRS": "standby",
        "PGSSLPASSWORD": "sentinel-parent-ssl-password",
    }
    for key, value in poison.items():
        monkeypatch.setenv(key, value)
    with _pytest_environment([target]) as child_env:
        completed = _run_preflight(child_env, capture_output=True)
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "preflight ok" in output
    assert "sentinel-" not in output
    assert current_url not in output
    assert set(poison).isdisjoint(child_env)


@pytest.mark.postgres_integration
def test_invalid_url_cannot_be_redirected_by_parent_pghostaddr(
    monkeypatch,
    tmp_path,
):
    current_url = os.environ["TEST_POSTGRES_URL"]
    parsed = urlsplit(current_url)
    password = _current_target_password(current_url)
    invalid_url = (
        f"postgresql://{parsed.username}@definitely.invalid:{parsed.port or 5432}"
        f"{parsed.path}"
    )
    inherited = tmp_path / "invalid-host.pgpass"
    inherited.write_text(_pgpass_entry(invalid_url, password) + "\n", encoding="utf-8")
    inherited.chmod(0o600)

    env = os.environ.copy()
    env["PYTHON_BIN"] = sys.executable
    env["TEST_POSTGRES_URL"] = invalid_url
    env.pop("TEST_POSTGRES_NON_C_URL", None)
    env.pop("TEST_POSTGRES_NON_UTF_URL", None)
    env["POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED"] = "0"
    env["PGHOSTADDR"] = parsed.hostname or "127.0.0.1"
    env["PGPASSFILE"] = str(inherited)
    env["PGSSLPASSWORD"] = "sentinel-parent-ssl-password"
    completed = subprocess.run(
        ["bash", "scripts/check_postgres.sh"],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "preflight ok" not in output
    assert "connection or identity check failed" in output
    assert invalid_url not in output
    assert password not in output
    assert "sentinel-" not in output
    assert "Traceback" not in output


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


def test_pgpass_creation_preserves_keyboard_interrupt_after_cleanup(
    monkeypatch,
    capsys,
):
    created: list[Path] = []
    original_mkstemp = postgres_lane.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, raw_path = original_mkstemp(*args, **kwargs)
        created.append(Path(raw_path))
        return descriptor, raw_path

    def interrupted_write(_descriptor, _payload):
        raise KeyboardInterrupt

    monkeypatch.setattr(postgres_lane.tempfile, "mkstemp", recording_mkstemp)
    with pytest.raises(KeyboardInterrupt):
        _create_pgpass_file(
            b"sentinel-keyboard-pgpass-secret",
            write_fn=interrupted_write,
        )
    assert created and not created[0].exists()
    captured = capsys.readouterr()
    assert "sentinel-" not in (captured.out + captured.err)


def test_preflight_does_not_swallow_keyboard_interrupt(monkeypatch):
    monkeypatch.setenv(
        "TEST_POSTGRES_URL",
        "postgresql://lane_user@db.example/silicon_notebook_keyboard_test",
    )
    target = _prepare_target("primary", "TEST_POSTGRES_URL", "utf8")
    assert target is not None

    def interrupted_connect(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(postgres_lane.psycopg, "connect", interrupted_connect)
    with pytest.raises(KeyboardInterrupt):
        postgres_lane._inspect_target(target)


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
