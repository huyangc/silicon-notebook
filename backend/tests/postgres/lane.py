"""Fail-closed launcher for the opt-in PostgreSQL integration lane."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.core.database_url import (
    database_identity,
    database_status,
    normalize_database_url,
    redact_database_url,
)
from tests.postgres.conftest import (
    _database_catalog,
    _require_dedicated_test_database,
    _validate_database_catalog,
    _validate_test_url_options,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
_MALFORMED_PERCENT_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_CHILD_ENV_ALLOWLIST = {
    "CI",
    "GITHUB_ACTIONS",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_COLOR",
    "PATH",
    "SYSTEM_VERSION_COMPAT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
}


@dataclass(frozen=True)
class _Target:
    label: str
    env_name: str
    expected: str
    raw_url: str
    sanitized_url: str
    url_password: str | None
    safe_status: str


def _password_free_url(url: str) -> tuple[str, str | None]:
    """Return an equivalent URL without a password and the decoded password."""
    safe_query = _validate_test_url_options(url)
    normalized = normalize_database_url(url)
    parsed = urlsplit(normalized)
    if parsed.scheme != "postgresql":
        raise RuntimeError("PostgreSQL integration target must use PostgreSQL")

    username = parsed.username
    if not username:
        raise RuntimeError("PostgreSQL integration target must include a user")
    if _MALFORMED_PERCENT_ESCAPE.search(username):
        raise RuntimeError("PostgreSQL integration target user is malformed")
    decoded_username = unquote(username, encoding="utf-8", errors="strict")
    if "*" in decoded_username or any(
        character in decoded_username for character in ("\x00", "\r", "\n")
    ):
        raise RuntimeError("PostgreSQL integration target user is not exact")
    encoded_username = quote(decoded_username, safe="")
    raw_password = parsed.password
    if raw_password is not None and _MALFORMED_PERCENT_ESCAPE.search(raw_password):
        raise RuntimeError("PostgreSQL integration target password is malformed")
    password = (
        unquote(raw_password, encoding="utf-8", errors="strict")
        if raw_password is not None
        else None
    )
    if password is not None and any(
        character in password for character in ("\x00", "\r", "\n")
    ):
        raise RuntimeError("PostgreSQL integration target password is malformed")
    host = parsed.hostname
    if host is None:
        raise RuntimeError("PostgreSQL integration target must include a host")
    display_host = f"[{host}]" if ":" in host else host
    port = parsed.port or 5432
    display_host = f"{display_host}:{port}"
    netloc = f"{encoded_username}@{display_host}"
    sanitized = urlunsplit(
        ("postgresql", netloc, parsed.path, urlencode(safe_query), "")
    )

    effective = conninfo_to_dict(sanitized)
    expected = {
        "host": host,
        "port": str(port),
        "user": decoded_username,
        "dbname": database_identity(sanitized).database,
    }
    if any(effective.get(key) != value for key, value in expected.items()):
        raise RuntimeError("PostgreSQL integration target identity is ambiguous")
    forbidden = {
        "password",
        "sslpassword",
        "hostaddr",
        "service",
        "servicefile",
        "passfile",
        "options",
    }
    if forbidden.intersection(effective):
        raise RuntimeError("PostgreSQL integration target contains an unsafe override")
    return sanitized, password


def _pgpass_escape(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise RuntimeError("PostgreSQL credential field contains an invalid character")
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _unlink_pgpass_file(
    path: Path,
    *,
    suppress_errors: bool,
    unlink_fn: Callable[[Path], None] | None = None,
) -> None:
    failure: BaseException | None = None
    for _attempt in range(2):
        try:
            if unlink_fn is None:
                path.unlink(missing_ok=True)
            else:
                unlink_fn(path)
            return
        except BaseException as exc:
            failure = exc
    if not suppress_errors and failure is not None:
        raise RuntimeError("could not remove temporary PostgreSQL credential file") from None


def _create_pgpass_file(
    payload: bytes,
    *,
    write_fn=os.write,
    close_fn=os.close,
    unlink_fn: Callable[[Path], None] | None = None,
) -> Path:
    """Create one 0600 pgpass file with single-owner fd and failure cleanup."""
    descriptor: int | None = None
    path: Path | None = None
    failure: BaseException | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix="silicon-notebook-pgpass-")
        path = Path(raw_path)
        os.chmod(path, 0o600)
        offset = 0
        while offset < len(payload):
            written = write_fn(descriptor, payload[offset:])
            if not isinstance(written, int) or written <= 0:
                raise OSError("temporary PostgreSQL credential write made no progress")
            offset += written
    except BaseException as exc:
        failure = exc
    finally:
        if descriptor is not None:
            owned_descriptor = descriptor
            descriptor = None
            try:
                close_fn(owned_descriptor)
            except BaseException as exc:
                if failure is None:
                    failure = exc

    if failure is not None:
        if path is not None:
            _unlink_pgpass_file(
                path,
                suppress_errors=True,
                unlink_fn=unlink_fn,
            )
        raise RuntimeError("could not create temporary PostgreSQL credential file") from None
    if path is None:
        raise RuntimeError("could not create temporary PostgreSQL credential file")
    return path


def _pgpass_entry(url: str, password: str) -> str:
    """Build one exact-target pgpass line without ever logging it."""
    identity = database_identity(url)
    parsed = urlsplit(normalize_database_url(url))
    username = unquote(parsed.username) if parsed.username is not None else "*"
    host = identity.host or "*"
    port = str(identity.port or 5432)
    match_fields = (host, port, identity.database, username)
    if any("*" in value for value in match_fields):
        raise RuntimeError("PostgreSQL pgpass target fields must be exact")
    return ":".join(
        _pgpass_escape(value)
        for value in (*match_fields, password)
    )


def _prepare_target(label: str, env_name: str, expected: str) -> _Target | None:
    raw_url = os.environ.get(env_name)
    if not raw_url:
        return None
    _validate_test_url_options(raw_url)
    identity = database_identity(raw_url)
    if identity.scheme != "postgresql":
        raise RuntimeError("PostgreSQL integration target must use PostgreSQL")
    _require_dedicated_test_database(raw_url)
    sanitized_url, password = _password_free_url(raw_url)
    # Exercise both safe-diagnostic helpers while the URL is known valid. They
    # deliberately discard userinfo and query options.
    safe_status = database_status(raw_url)
    _ = redact_database_url(raw_url)
    return _Target(
        label=label,
        env_name=env_name,
        expected=expected,
        raw_url=raw_url,
        sanitized_url=sanitized_url,
        url_password=password,
        safe_status=safe_status,
    )


def _inspect_target(target: _Target) -> bool:
    try:
        identity = database_identity(target.raw_url)
        connect_args: dict[str, object] = {
            "row_factory": dict_row,
            "connect_timeout": 5,
        }
        if target.url_password is not None:
            connect_args["password"] = target.url_password
        with psycopg.connect(target.sanitized_url, **connect_args) as connection:
            row = connection.execute(
                "SELECT current_database() AS database, "
                "current_setting('server_encoding') AS encoding, "
                "to_jsonb(d) AS catalog "
                "FROM pg_database AS d WHERE datname=current_database()"
            ).fetchone()
        if row is None:
            raise RuntimeError("PostgreSQL catalog row is missing")
        catalog = _database_catalog(row)
        if catalog.database != identity.database:
            raise RuntimeError("PostgreSQL database identity mismatch")
        _validate_database_catalog(catalog, expected=target.expected)
    except BaseException:
        print(
            f"PostgreSQL {target.label} preflight failed: "
            f"{target.safe_status} (connection or identity check failed)",
            file=sys.stderr,
        )
        return False
    print(f"PostgreSQL {target.label} preflight ok: {target.safe_status}")
    return True


@contextmanager
def _pytest_environment(targets: list[_Target]):
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key in _CHILD_ENV_ALLOWLIST
    }
    child_env["PYTHONPATH"] = str(BACKEND_ROOT)
    child_env["SILICON_NOTEBOOK_ENV_FILE"] = ""
    for target in targets:
        child_env[target.env_name] = target.sanitized_url

    fallback_password = os.environ.get("PGPASSWORD")
    entries = [
        _pgpass_entry(
            target.sanitized_url,
            target.url_password
            if target.url_password is not None
            else fallback_password,
        )
        for target in targets
        if target.url_password is not None or fallback_password is not None
    ]
    existing_bytes = b""
    existing = os.environ.get("PGPASSFILE")
    if existing:
        try:
            existing_bytes = Path(existing).read_bytes()
        except FileNotFoundError:
            # A stale inherited path is deliberately not forwarded to pytest.
            existing_bytes = b""

    generated_bytes = (
        ("\n".join(entries) + "\n").encode("utf-8") if entries else b""
    )
    if existing_bytes and not existing_bytes.endswith(b"\n"):
        existing_bytes += b"\n"
    payload = generated_bytes + existing_bytes

    # Always set an explicit file, even when empty, so libpq cannot fall back to
    # an unrelated ~/.pgpass under the allowlisted HOME. Peer/trust auth still works.
    pgpass_path = _create_pgpass_file(payload)
    child_env["PGPASSFILE"] = str(pgpass_path)
    try:
        yield child_env
    finally:
        _unlink_pgpass_file(
            pgpass_path,
            suppress_errors=sys.exc_info()[0] is not None,
        )


def _run_pytest(targets: list[_Target]) -> int:
    with _pytest_environment(targets) as child_env:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "-n",
                "0",
                "--tb=short",
                "--maxfail=1",
                "-m",
                "postgres_integration",
                "tests/postgres",
            ],
            cwd=BACKEND_ROOT,
            env=child_env,
            check=False,
        )
    return completed.returncode


def main() -> int:
    specs = (
        ("primary", "TEST_POSTGRES_URL", "utf8"),
        ("non-C UTF8", "TEST_POSTGRES_NON_C_URL", "non-c"),
        ("non-UTF negative", "TEST_POSTGRES_NON_UTF_URL", "non-utf"),
    )
    targets: list[_Target] = []
    for label, env_name, expected in specs:
        try:
            target = _prepare_target(label, env_name, expected)
        except BaseException:
            print(
                f"PostgreSQL {label} preflight failed: invalid database configuration",
                file=sys.stderr,
            )
            return 2
        if target is not None:
            targets.append(target)

    for target in targets:
        if not _inspect_target(target):
            return 2
    try:
        return _run_pytest(targets)
    except BaseException:
        print(
            "PostgreSQL integration gate failed: credential transport or test "
            "launcher error",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
