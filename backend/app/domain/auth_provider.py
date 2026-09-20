"""Provider-neutral external-authentication values and application port."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


# Protocol rails are named here so the SDK host and application service share
# one source.  Provider-specific payload fields remain private to the plugin.
AUTH_PROVIDER_ID_MAX_CHARS = 128
AUTH_PROVIDER_NAMESPACE_MAX_CHARS = 256
AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS = 128
AUTH_PROVIDER_PUBLIC_LABEL_MAX_CHARS = 80
AUTH_PROVIDER_ENDPOINT_MAX_CHARS = 2048
AUTH_PROVIDER_SUBJECT_MAX_CHARS = 512
AUTH_PROVIDER_USERNAME_MAX_CHARS = 512
AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS = 512
AUTH_PROVIDER_AUTHORIZATION_URL_MAX_CHARS = 8192
AUTH_PROVIDER_STABLE_ID_PATTERN = r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$"
_AUTH_PROVIDER_STABLE_ID = re.compile(AUTH_PROVIDER_STABLE_ID_PATTERN)


def is_auth_provider_stable_id(value: object, *, max_chars: int) -> bool:
    """Return whether a provider identifier is canonical and bounded."""

    return (
        type(value) is str
        and len(value) <= max_chars
        and bool(_AUTH_PROVIDER_STABLE_ID.fullmatch(value))
    )


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """A verified external identity, normalized by one provider plugin."""

    provider_namespace: str
    subject: str
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class AuthProviderDescriptor:
    """Core-owned projection of the startup-frozen provider topology."""

    plugin_id: str
    contribution_id: str
    provider_id: str
    provider_namespace: str
    configuration_generation: str
    public_label: str
    authorization_endpoint: str
    supports_pkce: bool


class AuthProviderError(RuntimeError):
    """Sanitized host failure carrying one stable, content-free code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AuthProviderHostPort(Protocol):
    def describe(self) -> AuthProviderDescriptor | None: ...

    def ensure_available(self) -> None: ...

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str: ...

    def authenticate(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        timeout_seconds: float,
    ) -> ExternalIdentity: ...


__all__ = [
    "AUTH_PROVIDER_AUTHORIZATION_URL_MAX_CHARS",
    "AUTH_PROVIDER_CONFIGURATION_GENERATION_MAX_CHARS",
    "AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS",
    "AUTH_PROVIDER_ENDPOINT_MAX_CHARS",
    "AUTH_PROVIDER_ID_MAX_CHARS",
    "AUTH_PROVIDER_NAMESPACE_MAX_CHARS",
    "AUTH_PROVIDER_PUBLIC_LABEL_MAX_CHARS",
    "AUTH_PROVIDER_SUBJECT_MAX_CHARS",
    "AUTH_PROVIDER_STABLE_ID_PATTERN",
    "AUTH_PROVIDER_USERNAME_MAX_CHARS",
    "AuthProviderDescriptor",
    "AuthProviderError",
    "AuthProviderHostPort",
    "ExternalIdentity",
    "is_auth_provider_stable_id",
]
