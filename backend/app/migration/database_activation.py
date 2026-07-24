"""Verified, atomic local ``DATABASE_URL`` activation after an offline import."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from dotenv import dotenv_values

from app.core.database_url import (
    DatabaseIdentity,
    database_identity,
    database_status,
    normalize_database_url,
)
from app.migration.sqlite_to_postgres import (
    DEFAULT_BATCH_ROWS,
    MigrationVerification,
    SqliteToPostgresMigrationError,
    _fsync_directory,
    verify_migration_receipt,
)


_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*="
)
_MANAGED_KEYS = ("DATABASE_URL", "SHADOW_DATABASE_URL")
_MAX_ENV_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class DatabaseActivationResult:
    env_path: str
    backup_path: str | None
    previous_database: str
    active_database: str
    env_sha256_before: str
    env_sha256_after: str
    changed: bool
    verification: MigrationVerification


def _sqlite_path(identity: DatabaseIdentity, *, root_dir: Path) -> Path:
    if identity.scheme != "sqlite":
        raise SqliteToPostgresMigrationError(
            "active DATABASE_URL must still select SQLite before activation"
        )
    path = Path(identity.database)
    return (path if path.is_absolute() else root_dir / path).resolve()


def _load_env(path: Path) -> tuple[Path, bytes, str, dict[str, str], os.stat_result]:
    raw = Path(path).expanduser()
    if raw.is_symlink():
        raise SqliteToPostgresMigrationError("activation env file must not be a symlink")
    try:
        resolved = raw.resolve(strict=True)
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ENV_BYTES:
            raise SqliteToPostgresMigrationError("activation env file is invalid")
        data = resolved.read_bytes()
        text = data.decode("utf-8")
    except SqliteToPostgresMigrationError:
        raise
    except (OSError, UnicodeError) as exc:
        raise SqliteToPostgresMigrationError(
            "activation env file could not be read safely"
        ) from exc

    occurrences = {key: 0 for key in _MANAGED_KEYS}
    for line in text.splitlines(keepends=True):
        match = _ASSIGNMENT.match(line)
        if match and match.group("key") in occurrences:
            occurrences[match.group("key")] += 1
    if occurrences["DATABASE_URL"] != 1 or occurrences["SHADOW_DATABASE_URL"] > 1:
        raise SqliteToPostgresMigrationError(
            "activation env file must contain exactly one DATABASE_URL and at most one "
            "SHADOW_DATABASE_URL"
        )
    try:
        parsed = dotenv_values(stream=None, dotenv_path=resolved, interpolate=False)
    except Exception as exc:
        raise SqliteToPostgresMigrationError(
            "activation env file could not be parsed safely"
        ) from exc
    values: dict[str, str] = {}
    for key in _MANAGED_KEYS:
        value = parsed.get(key)
        if value is not None:
            if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
                raise SqliteToPostgresMigrationError(
                    f"activation env file has an invalid {key}"
                )
            values[key] = value
    return resolved, data, text, values, metadata


def _env_quote(value: str) -> str:
    """Quote a value for an env file read by both python-dotenv and shell ``source``.

    Single quotes make the value literal in both loaders (no interpolation and no
    escape processing), which is correct for URLs containing ``$``, ``"`` or
    ``\\``. A literal single quote cannot be represented safely for both loaders
    at once — ``shlex.quote`` emits shell concatenation (``'...'"'"'...'``) that
    python-dotenv misparses — so fail closed rather than write a value that would
    make ``DATABASE_URL`` silently unreadable on the next start.
    """
    if "'" in value:
        raise SqliteToPostgresMigrationError(
            "database URL contains a single quote that cannot be written safely to "
            "the activation env file; set DATABASE_URL manually after activation"
        )
    return f"'{value}'"


def _render_env(text: str, *, active_url: str, shadow_url: str) -> bytes:
    replacements = {
        "DATABASE_URL": f"DATABASE_URL={_env_quote(active_url)}",
        "SHADOW_DATABASE_URL": f"SHADOW_DATABASE_URL={_env_quote(shadow_url)}",
    }
    rendered: list[str] = []
    replaced: set[str] = set()
    for line in text.splitlines(keepends=True):
        match = _ASSIGNMENT.match(line)
        key = match.group("key") if match else ""
        if key not in replacements:
            rendered.append(line)
            continue
        newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
        # Preserve the matched prefix (leading whitespace and an optional
        # ``export``) so a sourced env file keeps exporting DATABASE_URL after
        # cutover instead of demoting it to a plain, unexported shell variable
        # that child processes would not inherit.
        rendered.append(match.group("prefix") + replacements[key] + newline)
        replaced.add(key)
    if "SHADOW_DATABASE_URL" not in replaced:
        if rendered and not rendered[-1].endswith(("\n", "\r")):
            rendered.append("\n")
        rendered.append(replacements["SHADOW_DATABASE_URL"] + "\n")
    return "".join(rendered).encode("utf-8")


def _write_exclusive(path: Path, data: bytes, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _atomic_activate_env(
    *, env_path: Path, source_path: Path, target_url: str, root_dir: Path
) -> tuple[str | None, str, str, str, str, bool]:
    resolved, original, text, values, metadata = _load_env(env_path)
    active_url = values.get("DATABASE_URL")
    assert active_url is not None
    try:
        normalized_target = normalize_database_url(target_url)
        active_identity = database_identity(active_url)
        target_identity = database_identity(normalized_target)
        shadow_url = values.get("SHADOW_DATABASE_URL")
        shadow_identity = database_identity(shadow_url) if shadow_url else None
    except ValueError as exc:
        raise SqliteToPostgresMigrationError(
            "activation env file contains an invalid database URL"
        ) from exc
    if target_identity.scheme != "postgresql":
        raise SqliteToPostgresMigrationError(
            "activation target must be a PostgreSQL DATABASE_URL"
        )

    source = Path(source_path).resolve()
    if active_identity.scheme == "postgresql":
        # Idempotence requires the FULL normalized URL to match, not just the
        # host/port/database that DatabaseIdentity compares: an active URL with a
        # different password or search_path must fail closed rather than report
        # "already active, unchanged" and let the backend start on the wrong
        # credentials or schema.
        if (
            normalize_database_url(active_url) != normalized_target
            or shadow_identity is None
        ):
            raise SqliteToPostgresMigrationError(
                "DATABASE_URL is already PostgreSQL but does not match this migration"
            )
        if _sqlite_path(shadow_identity, root_dir=root_dir) != source:
            raise SqliteToPostgresMigrationError(
                "SHADOW_DATABASE_URL does not preserve the migrated SQLite source"
            )
        digest = hashlib.sha256(original).hexdigest()
        return None, database_status(shadow_url), database_status(active_url), digest, digest, False

    if _sqlite_path(active_identity, root_dir=root_dir) != source:
        raise SqliteToPostgresMigrationError(
            "active DATABASE_URL does not point to the migrated SQLite source"
        )
    if shadow_identity is not None and shadow_identity != target_identity:
        raise SqliteToPostgresMigrationError(
            "SHADOW_DATABASE_URL does not match the migration target"
        )

    replacement = _render_env(
        text,
        active_url=normalized_target,
        shadow_url=active_url,
    )
    before_hash = hashlib.sha256(original).hexdigest()
    after_hash = hashlib.sha256(replacement).hexdigest()
    parent = resolved.parent
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = parent / f"{resolved.name}.pre-postgres-{timestamp}-{uuid.uuid4().hex[:8]}.bak"
    temporary = parent / f"{resolved.name}.postgres-{uuid.uuid4().hex}.tmp"
    # The replacement and backup hold database credentials; mask off any
    # group/world bits a permissive source env file (e.g. 0644) would otherwise
    # propagate, so the file with the PostgreSQL password stays owner-only.
    mode = stat.S_IMODE(metadata.st_mode) & 0o600
    replaced = False
    try:
        _write_exclusive(backup, original, mode=mode)
        _write_exclusive(temporary, replacement, mode=mode)
        current_metadata = resolved.lstat()
        if (
            not stat.S_ISREG(current_metadata.st_mode)
            or current_metadata.st_dev != metadata.st_dev
            or current_metadata.st_ino != metadata.st_ino
            or resolved.read_bytes() != original
        ):
            raise SqliteToPostgresMigrationError(
                "activation env file changed during verification"
            )
        os.replace(temporary, resolved)
        replaced = True
        _fsync_directory(parent)
    except SqliteToPostgresMigrationError:
        temporary.unlink(missing_ok=True)
        if not replaced:
            backup.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if not replaced:
            backup.unlink(missing_ok=True)
        raise SqliteToPostgresMigrationError(
            "could not atomically activate PostgreSQL in the env file"
        ) from exc

    return (
        str(backup),
        database_status(active_url),
        database_status(normalized_target),
        before_hash,
        after_hash,
        True,
    )


def activate_postgres_from_receipt(
    *,
    source_path: Path,
    target_url: str,
    receipt_path: Path,
    work_dir: Path,
    env_path: Path,
    root_dir: Path,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    progress: Callable[[str], None] = print,
    skip_target_reverify: bool = False,
) -> DatabaseActivationResult:
    """Verify both databases, then atomically switch a stopped local deployment.

    ``skip_target_reverify`` (operator ``--fast-activation``) forwards to
    :func:`verify_migration_receipt`; it keeps the SQLite source re-snapshot
    cutover anchor and the target schema/manifest checks, dropping only the
    redundant second full-table PostgreSQL checksum read on a large target.
    """
    verification = verify_migration_receipt(
        source_path=source_path,
        target_url=target_url,
        receipt_path=receipt_path,
        work_dir=work_dir,
        root_dir=root_dir,
        batch_rows=batch_rows,
        progress=progress,
        skip_target_reverify=skip_target_reverify,
    )
    backup, previous, active, before_hash, after_hash, changed = _atomic_activate_env(
        env_path=env_path,
        source_path=source_path,
        target_url=target_url,
        root_dir=root_dir,
    )
    return DatabaseActivationResult(
        env_path=str(Path(env_path).expanduser().resolve()),
        backup_path=backup,
        previous_database=previous,
        active_database=active,
        env_sha256_before=before_hash,
        env_sha256_after=after_hash,
        changed=changed,
        verification=verification,
    )
