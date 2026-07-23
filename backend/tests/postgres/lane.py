"""Fail-closed launcher for the opt-in PostgreSQL integration lane."""

from __future__ import annotations

import os
import re
import signal
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
_CHILD_REAP_TIMEOUT_SECONDS = 5.0
_CHILD_WAIT_POLL_SECONDS = 0.2


class _LauncherSignal(BaseException):
    """A scoped launcher signal that must unwind credential/child ownership."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


class _LauncherResources:
    """Own launcher resources before scoped signal handlers become active."""

    def __init__(self) -> None:
        self._pending_signum: int | None = None
        self._child: subprocess.Popen[str] | None = None
        self._pgpass_paths: set[Path] = set()

    @property
    def pending_signum(self) -> int | None:
        return self._pending_signum

    def request_signal(self, signum: int) -> None:
        if self._pending_signum is None:
            self._pending_signum = signum
        child = self._child
        if child is not None:
            try:
                _signal_owned_child(child, signal.SIGTERM)
            except Exception:
                # A signal handler only records state and makes a best-effort,
                # non-blocking termination request. Safe checkpoints reap.
                pass

    def register_pgpass(self, path: Path) -> None:
        self._pgpass_paths.add(path)

    def release_pgpass(self, path: Path) -> None:
        self._pgpass_paths.discard(path)

    def register_child(self, process: subprocess.Popen[str]) -> None:
        if self._child is not None:
            raise RuntimeError("PostgreSQL launcher already owns a child")
        self._child = process
        if self._pending_signum is not None:
            self.request_signal(self._pending_signum)

    def release_child(self, process: subprocess.Popen[str]) -> None:
        if self._child is process:
            self._child = None

    def checkpoint(self) -> None:
        if self._pending_signum is not None:
            raise _LauncherSignal(self._pending_signum)

    def exit_status(self, normal_status: int) -> int:
        if self._pending_signum is not None:
            return 128 + self._pending_signum
        return normal_status

    def cleanup(self) -> None:
        failure: BaseException | None = None
        child = self._child
        if child is not None:
            try:
                _terminate_and_reap(child)
            except BaseException as exc:
                failure = exc
            finally:
                if child.returncode is not None:
                    self.release_child(child)
        for path in tuple(self._pgpass_paths):
            try:
                _unlink_pgpass_file(path, suppress_errors=True)
            except BaseException as exc:
                if failure is None or not isinstance(exc, Exception):
                    failure = exc
            finally:
                if not path.exists():
                    self.release_pgpass(path)
        if failure is not None:
            raise failure


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
    failure: Exception | None = None
    interruption: BaseException | None = None
    for _attempt in range(2):
        try:
            if unlink_fn is None:
                path.unlink(missing_ok=True)
            else:
                unlink_fn(path)
        except BaseException as exc:
            if isinstance(exc, Exception):
                failure = exc
            elif interruption is None:
                interruption = exc
        else:
            if interruption is not None:
                raise interruption
            return
    if interruption is not None:
        raise interruption
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
                if failure is None or not isinstance(exc, Exception):
                    failure = exc

    if failure is not None:
        if path is not None:
            try:
                _unlink_pgpass_file(
                    path,
                    suppress_errors=True,
                    unlink_fn=unlink_fn,
                )
            except BaseException as cleanup_interruption:
                if isinstance(failure, Exception):
                    failure = cleanup_interruption
        if isinstance(failure, Exception):
            raise RuntimeError(
                "could not create temporary PostgreSQL credential file"
            ) from None
        raise failure
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
        if target.url_password is not None:
            raise RuntimeError("isolated preflight URL must not contain a password")
        with psycopg.connect(
            target.sanitized_url,
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
    except Exception:
        print(
            f"PostgreSQL {target.label} preflight failed: "
            f"{target.safe_status} (connection or identity check failed)",
            file=sys.stderr,
        )
        return False
    print(f"PostgreSQL {target.label} preflight ok: {target.safe_status}")
    return True


@contextmanager
def _pytest_environment(
    targets: list[_Target],
    *,
    owner: _LauncherResources | None = None,
):
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
        if owner is not None:
            owner.register_pgpass(pgpass_path)
            owner.checkpoint()
        yield child_env
    finally:
        try:
            _unlink_pgpass_file(
                pgpass_path,
                suppress_errors=sys.exc_info()[0] is not None,
            )
        finally:
            if owner is not None and not pgpass_path.exists():
                owner.release_pgpass(pgpass_path)


def _run_preflight(
    child_env: dict[str, str],
    *,
    capture_output: bool = False,
    owner: _LauncherResources | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_child(
        _preflight_command(),
        child_env,
        capture_output=capture_output,
        owner=owner,
    )


def _preflight_command() -> list[str]:
    return [sys.executable, "-m", "tests.postgres.lane", "--preflight"]


def _pytest_command() -> list[str]:
    return [
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
    ]


def _conventional_child_status(returncode: int) -> int:
    return 128 + abs(returncode) if returncode < 0 else returncode


def _signal_owned_child(process: subprocess.Popen[str], signum: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
    except ProcessLookupError:
        return


def _terminate_and_reap(process: subprocess.Popen[str]) -> None:
    """Boundedly stop the exact child/session and always reap its leader."""
    if process.poll() is None:
        _signal_owned_child(process, signal.SIGTERM)
    try:
        process.wait(timeout=_CHILD_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_owned_child(process, signal.SIGKILL)
        process.wait(timeout=_CHILD_REAP_TIMEOUT_SECONDS)

    # The leader is reaped, but a descendant could have survived SIGTERM. The
    # child owns a fresh POSIX session/process group, so this cannot target the
    # launcher or an unrelated sibling.
    if os.name == "posix":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        _signal_owned_child(process, signal.SIGKILL)


def _run_child(
    command: list[str],
    child_env: dict[str, str],
    *,
    capture_output: bool = False,
    owner: _LauncherResources | None = None,
) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[str] | None = None
    try:
        if owner is not None:
            owner.checkpoint()
        process = subprocess.Popen(
            command,
            cwd=BACKEND_ROOT,
            env=child_env,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            start_new_session=os.name == "posix",
        )
        if owner is not None:
            owner.register_child(process)
            owner.checkpoint()
            while True:
                try:
                    stdout, stderr = process.communicate(
                        timeout=_CHILD_WAIT_POLL_SECONDS
                    )
                    break
                except subprocess.TimeoutExpired:
                    if owner.pending_signum is not None:
                        _terminate_and_reap(process)
                        stdout, stderr = process.communicate()
                        break
        else:
            stdout, stderr = process.communicate()
    except BaseException:
        if process is not None:
            try:
                _terminate_and_reap(process)
            except BaseException:
                # Preserve the signal/KeyboardInterrupt being unwound. The
                # bounded TERM/KILL/reap sequence has already been attempted.
                pass
        if owner is not None and owner.pending_signum is not None:
            owner.checkpoint()
        raise
    finally:
        if (
            owner is not None
            and process is not None
            and process.returncode is not None
        ):
            owner.release_child(process)
    if owner is not None:
        owner.checkpoint()
    return subprocess.CompletedProcess(
        command,
        _conventional_child_status(process.returncode),
        stdout,
        stderr,
    )


def _run_pytest_in_environment(
    child_env: dict[str, str],
    *,
    owner: _LauncherResources | None = None,
) -> int:
    completed = _run_child(_pytest_command(), child_env, owner=owner)
    return completed.returncode


@contextmanager
def _launcher_signal_handlers(owner: _LauncherResources):
    """Record SIGINT/SIGTERM without raising in a resource-return window."""
    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    def record(signum, _frame):
        owner.request_signal(signum)

    try:
        for signum in previous:
            signal.signal(signum, record)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _run_pytest(targets: list[_Target]) -> int:
    """Unit-testable compatibility wrapper around the isolated pytest launch."""
    owner = _LauncherResources()
    status = 2
    with _launcher_signal_handlers(owner):
        try:
            with _pytest_environment(targets, owner=owner) as child_env:
                owner.checkpoint()
                status = _run_pytest_in_environment(child_env, owner=owner)
                owner.checkpoint()
        except _LauncherSignal:
            pass
        except BaseException:
            owner.checkpoint()
            raise
        finally:
            try:
                owner.cleanup()
            except BaseException:
                owner.checkpoint()
                raise
    return owner.exit_status(status)


def _run_isolated_gate(targets: list[_Target]) -> int:
    """Run preflight and pytest inside one exact environment/pgpass lifetime."""
    owner = _LauncherResources()
    status = 2
    with _launcher_signal_handlers(owner):
        try:
            with _pytest_environment(targets, owner=owner) as child_env:
                owner.checkpoint()
                preflight = _run_preflight(child_env, owner=owner)
                owner.checkpoint()
                if preflight.returncode != 0:
                    status = preflight.returncode
                else:
                    status = _run_pytest_in_environment(child_env, owner=owner)
                    owner.checkpoint()
        except _LauncherSignal:
            pass
        except BaseException:
            owner.checkpoint()
            raise
        finally:
            try:
                owner.cleanup()
            except BaseException:
                owner.checkpoint()
                raise
    return owner.exit_status(status)


def _preflight_mode_main() -> int:
    forbidden_environment = {
        key
        for key in os.environ
        if (key.startswith("PG") and key != "PGPASSFILE")
        or key
        in {
            "DATABASE_URL",
            "SHADOW_DATABASE_URL",
            "POSTGRES_DB",
            "POSTGRES_PASSWORD",
            "POSTGRES_USER",
        }
    }
    if forbidden_environment or not os.environ.get("PGPASSFILE"):
        print(
            "PostgreSQL preflight failed: isolated environment is invalid",
            file=sys.stderr,
        )
        return 2

    specs = (
        ("primary", "TEST_POSTGRES_URL", "utf8"),
        ("non-C UTF8", "TEST_POSTGRES_NON_C_URL", "non-c"),
        ("non-UTF negative", "TEST_POSTGRES_NON_UTF_URL", "non-utf"),
    )
    targets: list[_Target] = []
    for label, env_name, expected in specs:
        try:
            target = _prepare_target(label, env_name, expected)
            if target is not None and target.url_password is not None:
                raise RuntimeError("isolated preflight URL contains a password")
        except Exception:
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
    return 0


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
        except Exception:
            print(
                f"PostgreSQL {label} preflight failed: invalid database configuration",
                file=sys.stderr,
            )
            return 2
        if target is not None:
            targets.append(target)

    try:
        return _run_isolated_gate(targets)
    except _LauncherSignal as interruption:
        return 128 + interruption.signum
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "PostgreSQL integration gate failed: credential transport or test "
            "launcher error",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    if sys.argv[1:] == ["--preflight"]:
        raise SystemExit(_preflight_mode_main())
    if sys.argv[1:]:
        print("PostgreSQL integration gate failed: invalid invocation", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
