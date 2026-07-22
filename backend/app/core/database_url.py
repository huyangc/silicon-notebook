"""Strict, backend-neutral database URL validation and safe diagnostics."""

from dataclasses import dataclass
from typing import Literal
from urllib.parse import SplitResult, urlsplit, urlunsplit


@dataclass(frozen=True)
class DatabaseIdentity:
    scheme: Literal["sqlite", "postgresql"]
    host: str | None
    port: int | None
    database: str


def _normalized_parts(raw: str) -> tuple[str, SplitResult]:
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise ValueError("database URL must be a non-empty, whitespace-free string")

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("invalid database URL") from exc

    scheme = parsed.scheme.lower()
    if scheme == "postgres":
        scheme = "postgresql"
    if scheme not in {"sqlite", "postgresql"}:
        raise ValueError(f"unsupported database URL scheme: {parsed.scheme or '<missing>'}")
    if not parsed.scheme:
        raise ValueError("database URL must include a scheme")

    normalized = f"{scheme}{raw[len(parsed.scheme):]}"
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("invalid database URL") from exc

    if parsed.fragment:
        raise ValueError("database URL must not include a fragment")
    if scheme == "sqlite":
        if parsed.netloc or not parsed.path or not parsed.path.startswith("/"):
            raise ValueError("sqlite database URLs must use sqlite:///path")
    elif parsed.hostname is None or not parsed.path or parsed.path == "/":
        raise ValueError("postgresql database URL must include host and database")

    return normalized, parsed


def normalize_database_url(raw: str) -> str:
    """Validate a supported URL and normalize only a legacy PostgreSQL scheme."""
    normalized, _ = _normalized_parts(raw)
    return normalized


def database_identity(raw: str) -> DatabaseIdentity:
    """Return the non-secret identity used for backend selection and diagnostics."""
    normalized, parsed = _normalized_parts(raw)
    if parsed.scheme == "sqlite":
        return DatabaseIdentity("sqlite", None, None, normalized[len("sqlite:///") :])
    return DatabaseIdentity(
        "postgresql",
        parsed.hostname,
        parsed.port,
        parsed.path[1:],
    )


def redact_database_url(raw: str) -> str:
    """Return a safe URL diagnostic without userinfo, query options, or fragments."""
    _, parsed = _normalized_parts(raw)
    if parsed.scheme == "sqlite":
        return urlunsplit(("sqlite", "", parsed.path, "", ""))

    host = parsed.hostname
    assert host is not None  # validated by _normalized_parts
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit(("postgresql", netloc, parsed.path, "", ""))


def sanitize_database_url_for_error(raw: object) -> str:
    """Return a fail-closed safe input value for validation error structures."""
    if not isinstance(raw, str):
        return "<invalid database URL>"
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
    except ValueError:
        return "<invalid database URL>"

    scheme = parsed.scheme.lower() or "invalid"
    if host is None:
        netloc = ""
    else:
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((scheme, netloc, "", "", ""))
