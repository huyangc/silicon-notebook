"""Credential-free identities and confirmation tokens for shadow operations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.core.database_url import database_identity


_TOKEN_VERSION = 1


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _row_value(row: Any, name: str, position: int) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return row[position]


@dataclass(frozen=True)
class DatabaseFingerprint:
    """A display-safe identity plus a hash of the exact underlying identity."""

    backend: str
    redacted_identity: str
    identity_hash: str

    def __post_init__(self) -> None:
        if self.backend not in {"sqlite", "postgresql"}:
            raise ValueError("unsupported database fingerprint backend")
        if len(self.identity_hash) != 64:
            raise ValueError("database identity hash must be SHA-256")


def fingerprint_sqlite(
    database_url: str,
    conn: sqlite3.Connection,
) -> DatabaseFingerprint:
    """Bind a SQLite URL, open connection, and filesystem object without leaking paths."""
    identity = database_identity(database_url)
    if identity.scheme != "sqlite":
        raise ValueError("SQLite source identity requires a sqlite URL")
    try:
        configured = Path(identity.database).expanduser().resolve(strict=True)
    except OSError:
        raise ValueError("SQLite source path is unavailable") from None
    if not configured.is_file():
        raise ValueError("SQLite source must be a regular file")
    database_rows = conn.execute("PRAGMA database_list").fetchall()
    main_rows = [
        row for row in database_rows if str(_row_value(row, "name", 1)) == "main"
    ]
    if len(main_rows) != 1:
        raise ValueError("SQLite source connection has no unique main database")
    try:
        connected = Path(str(_row_value(main_rows[0], "file", 2))).resolve(strict=True)
    except OSError:
        raise ValueError("SQLite source connection path is unavailable") from None
    if connected != configured:
        raise ValueError(
            "SQLite source URL and open connection identify different files"
        )
    try:
        stat = configured.stat()
    except OSError:
        raise ValueError("SQLite source metadata is unavailable") from None
    exact = {
        "backend": "sqlite",
        "path": os.fspath(configured),
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }
    # Basename is diagnostic only.  Parent directories may contain usernames,
    # tenant identifiers, or deployment secrets and are hash-only.
    display = "sqlite:///<redacted>"
    return DatabaseFingerprint("sqlite", display, _digest(exact))


def fingerprint_postgres(
    database_url: str,
    conn: Any,
    *,
    identity_row: Any | None = None,
) -> DatabaseFingerprint:
    """Bind URL routing identity to the actual database OID and active schema."""
    identity = database_identity(database_url)
    if identity.scheme != "postgresql":
        raise ValueError("PostgreSQL target identity requires a postgresql URL")
    row = identity_row
    if row is None:
        row = conn.execute(
            "SELECT current_database() AS database, current_schema() AS schema, "
            "(SELECT oid::text FROM pg_database WHERE datname=current_database()) AS database_oid"
        ).fetchone()
    database = str(_row_value(row, "database", 0))
    schema = str(_row_value(row, "schema", 1))
    database_oid = str(_row_value(row, "database_oid", 2))
    if database != identity.database:
        raise ValueError(
            "PostgreSQL target URL and server identify different databases"
        )
    port = identity.port or 5432
    exact = {
        "backend": "postgresql",
        "host": (identity.host or "").lower(),
        "port": port,
        "database": database,
        "database_oid": database_oid,
        "schema": schema,
    }
    host = identity.host or "<unknown>"
    if ":" in host:
        host = f"[{host}]"
    display = f"postgresql://{host}:{port}/{database}?schema={schema}"
    return DatabaseFingerprint("postgresql", display, _digest(exact))


def reject_same_database(
    source: DatabaseFingerprint,
    target: DatabaseFingerprint,
) -> None:
    if hmac.compare_digest(source.identity_hash, target.identity_hash):
        raise ValueError("shadow source and target must be different databases")


@dataclass(frozen=True)
class ConfirmationBinding:
    run_id: str
    source_identity_hash: str
    target_identity_hash: str
    sqlite_version: int
    postgres_version: int
    schema_epoch: int

    def payload(self, *, issued_at: int, expires_at: int, nonce: str) -> dict[str, Any]:
        return {
            "v": _TOKEN_VERSION,
            "run_id": self.run_id,
            "source": self.source_identity_hash,
            "target": self.target_identity_hash,
            "sqlite": self.sqlite_version,
            "postgres": self.postgres_version,
            "epoch": self.schema_epoch,
            "iat": issued_at,
            "exp": expires_at,
            "nonce": nonce,
        }


def issue_confirmation_token(
    binding: ConfirmationBinding,
    *,
    secret: bytes,
    now: int | None = None,
    ttl_seconds: int = 900,
) -> str:
    if len(secret) < 32:
        raise ValueError("confirmation secret must contain at least 32 bytes")
    if not binding.run_id or binding.run_id.strip() != binding.run_id:
        raise ValueError("run_id must be a non-empty canonical string")
    if ttl_seconds <= 0 or ttl_seconds > 3600:
        raise ValueError("confirmation token lifetime must be within one hour")
    issued_at = int(time.time() if now is None else now)
    payload = _canonical_json(
        binding.payload(
            issued_at=issued_at,
            expires_at=issued_at + ttl_seconds,
            nonce=secrets.token_hex(16),
        )
    )
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    return ".".join(
        base64.urlsafe_b64encode(part).rstrip(b"=").decode("ascii")
        for part in (payload, signature)
    )


def _decode_token_part(value: str) -> bytes:
    if not value or any(
        character
        not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise ValueError("invalid confirmation token")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ValueError("invalid confirmation token") from exc
    canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(canonical, value):
        raise ValueError("invalid confirmation token")
    return decoded


def verify_confirmation_token(
    token: str,
    binding: ConfirmationBinding,
    *,
    secret: bytes,
    now: int | None = None,
) -> None:
    if len(secret) < 32:
        raise ValueError("confirmation secret must contain at least 32 bytes")
    try:
        payload_part, signature_part = token.split(".")
    except ValueError as exc:
        raise ValueError("invalid confirmation token") from exc
    payload = _decode_token_part(payload_part)
    signature = _decode_token_part(signature_part)
    expected_signature = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("invalid confirmation token")
    try:
        decoded = json.loads(
            payload,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
    ) as exc:
        raise ValueError("invalid confirmation token") from exc
    if not isinstance(decoded, dict):
        raise ValueError("invalid confirmation token")
    current = int(time.time() if now is None else now)
    try:
        issued_at = decoded["iat"]
        expires_at = decoded["exp"]
        nonce = decoded["nonce"]
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, int)
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, int)
            or not 0 <= issued_at <= 2**63 - 1
            or not issued_at < expires_at <= 2**63 - 1
            or not isinstance(nonce, str)
            or re.fullmatch(r"[0-9a-f]{32}", nonce) is None
        ):
            raise ValueError
        expected = binding.payload(
            issued_at=issued_at,
            expires_at=expires_at,
            nonce=nonce,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("invalid confirmation token") from exc
    if decoded != expected or not expected["nonce"]:
        raise ValueError("confirmation token is not bound to this shadow run")
    if current < expected["iat"] - 30 or current > expected["exp"]:
        raise ValueError("confirmation token has expired")
