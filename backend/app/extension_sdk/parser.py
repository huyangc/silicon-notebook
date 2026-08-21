"""Point-specific contracts for the dormant parser ProviderChain.

The probe side can only produce an inert proposal.  Admission and
materialization remain core callbacks owned by the host, so a rejected parser
link has no persistence or asset port through which it could leave side
effects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from app.extension_sdk.contracts import (
    CancellationToken,
    ProviderChainResult,
)


PARSER_PROVIDER_CHAIN_POINT = "source.parser_chain"
PARSER_SELF_HOSTED_ACCESS_CAPABILITY = "parser:mineru_self_hosted_access"
PARSER_CLOUD_ACCESS_CAPABILITY = "parser:mineru_cloud_access"
PARSER_BUILTIN_ACCESS_CAPABILITY = "parser:builtin_access"

ParserSourceKind = Literal["file", "url"]
ParserExecutionBoundary = Literal["local", "private_service", "public_cloud"]


@dataclass(frozen=True)
class ParserSourceRef:
    """Content-free routing metadata; never a path, URL, title, or filename."""

    kind: ParserSourceKind
    suffix: str


@dataclass(frozen=True)
class ParserRouteDecision:
    """Core-owned applicability decision made before any availability probe."""

    allowed: bool
    execution: ParserExecutionBoundary
    reason_code: str = ""
    fallback_warning_code: str = ""


@dataclass(frozen=True)
class ParserProposal:
    """Inert request-local output; only core admission may authorize it."""

    contribution_id: str
    value: Any


@dataclass(frozen=True)
class ParserAdmissionDecision:
    accepted: bool
    reason_code: str = ""


@dataclass(frozen=True)
class ParserAvailabilityContext:
    """I/O-free metadata supplied to a live contribution availability probe."""

    plugin_id: str
    contribution_id: str
    source: ParserSourceRef
    cancellation: CancellationToken | None = None


@runtime_checkable
class ParserLinkAccess(Protocol):
    """One narrow request-bound probe; it has no persistence collaborator."""

    def probe(self) -> ProviderChainResult[ParserProposal]: ...


@runtime_checkable
class ParserConnectionLeaseProbe(Protocol):
    def is_connection_held(self) -> bool: ...


@dataclass(frozen=True)
class ParserExtensionContext:
    source: ParserSourceRef
    cancellation: CancellationToken
    access: ParserLinkAccess | None


@dataclass(frozen=True)
class ParserHostContext:
    contribution_id: str
    source: ParserSourceRef
    cancellation: CancellationToken
    access: ParserLinkAccess
    connection: ParserConnectionLeaseProbe


@runtime_checkable
class ParserChainLink(Protocol):
    def probe(
        self, context: ParserExtensionContext
    ) -> ProviderChainResult[ParserProposal]: ...


@dataclass(frozen=True)
class ParserChainEvent:
    """Content-free host receipt; plugins never construct or emit this shape."""

    kind: str
    contribution_id: str
    outcome: str
    failure_code: str
    warning_code: str
    elapsed_ms: int
