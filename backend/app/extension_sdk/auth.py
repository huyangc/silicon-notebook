"""Point-specific SDK contract for the single external auth provider."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from app.domain.auth_provider import ExternalIdentity


AUTH_PROVIDER_POINT = "auth.provider"


@dataclass(frozen=True, slots=True)
class AuthProviderDescription:
    """Provider-authored immutable metadata; core adds registry ownership."""

    provider_id: str
    provider_namespace: str
    configuration_generation: str
    public_label: str
    authorization_endpoint: str
    supports_pkce: bool


@dataclass(frozen=True, slots=True)
class AuthProviderCodeContext:
    """The only external-protocol inputs exposed for one code exchange."""

    code: str
    redirect_uri: str
    code_verifier: str | None
    deadline_monotonic: float


class AuthProviderFailureKind(str, Enum):
    CANCELLED = "cancelled"
    INVALID_CODE = "invalid_code"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    CONFIGURATION = "configuration"
    PROTOCOL = "protocol"


class AuthProviderFailure(RuntimeError):
    """Typed plugin failure with no provider response or exception text."""

    def __init__(self, kind: AuthProviderFailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


class AuthProvider(Protocol):
    def describe(self) -> AuthProviderDescription: ...

    def authorization_parameters(self) -> tuple[tuple[str, str], ...]: ...

    def authenticate_code(
        self, context: AuthProviderCodeContext
    ) -> ExternalIdentity: ...


__all__ = [
    "AUTH_PROVIDER_POINT",
    "AuthProvider",
    "AuthProviderCodeContext",
    "AuthProviderDescription",
    "AuthProviderFailure",
    "AuthProviderFailureKind",
    "ExternalIdentity",
]
