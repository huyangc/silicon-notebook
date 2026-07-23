"""Fail-closed launcher for the opt-in PostgreSQL integration lane."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import psycopg
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
    _validate_test_url_options(url)
    normalized = normalize_database_url(url)
    parsed = urlsplit(normalized)
    if parsed.scheme != "postgresql":
        raise RuntimeError("PostgreSQL integration target must use PostgreSQL")

    username = parsed.username
    encoded_username = quote(unquote(username), safe="") if username is not None else ""
    raw_password = parsed.password
    password = unquote(raw_password) if raw_password is not None else None
    host = parsed.hostname
    if host is None:
        raise RuntimeError("PostgreSQL integration target must include a host")
    display_host = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        display_host = f"{display_host}:{parsed.port}"
    netloc = (
        f"{encoded_username}@{display_host}" if username is not None else display_host
    )
    sanitized = urlunsplit(("postgresql", netloc, parsed.path, parsed.query, ""))
    return sanitized, password


def _pgpass_escape(value: str) -> str:
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise RuntimeError("PostgreSQL credential field contains an invalid character")
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _pgpass_entry(url: str, password: str) -> str:
    """Build one exact-target pgpass line without ever logging it."""
    identity = database_identity(url)
    parsed = urlsplit(normalize_database_url(url))
    username = unquote(parsed.username) if parsed.username is not None else "*"
    host = identity.host or "*"
    port = str(identity.port or 5432)
    return ":".join(
        _pgpass_escape(value)
        for value in (host, port, identity.database, username, password)
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
        with psycopg.connect(
            target.raw_url,
            row_factory=dict_row,
            connect_timeout=5,
        ) as connection:
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
    child_env = os.environ.copy()
    for target in targets:
        child_env[target.env_name] = target.sanitized_url

    fallback_password = child_env.get("PGPASSWORD")
    entries = [
        _pgpass_entry(
            target.raw_url,
            target.url_password
            if target.url_password is not None
            else fallback_password,
        )
        for target in targets
        if target.url_password is not None or fallback_password is not None
    ]
    child_env.pop("PGPASSWORD", None)

    pgpass_path: Path | None = None
    if entries:
        descriptor, raw_path = tempfile.mkstemp(prefix="silicon-notebook-pgpass-")
        pgpass_path = Path(raw_path)
        try:
            os.chmod(pgpass_path, 0o600)
            existing = child_env.get("PGPASSFILE")
            existing_bytes = b""
            if existing:
                existing_bytes = Path(existing).read_bytes()
            with os.fdopen(descriptor, "wb") as stream:
                if existing_bytes:
                    stream.write(existing_bytes)
                    if not existing_bytes.endswith(b"\n"):
                        stream.write(b"\n")
                stream.write(("\n".join(entries) + "\n").encode("utf-8"))
            descriptor = -1
            child_env["PGPASSFILE"] = str(pgpass_path)
            yield child_env
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            pgpass_path.unlink(missing_ok=True)
    else:
        # Peer/trust authentication and an existing PGPASSFILE remain usable for
        # passwordless local targets; no plaintext credential is added to pytest.
        yield child_env


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
