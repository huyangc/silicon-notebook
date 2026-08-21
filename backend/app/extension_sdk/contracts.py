"""Dependency-light Extension SDK contracts.

The SDK describes extension topology and typed outcomes.  It intentionally
contains no repository, runtime, settings, transport, or plugin implementation
imports.  Point-specific contexts are added beside their host instead of
growing a universal service-locator context.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Generic, Literal, Protocol, TypeVar, runtime_checkable


EXTENSION_API_VERSION = "1"


class ContributionKind(str, Enum):
    PROVIDER = "provider"
    PROVIDER_CHAIN = "provider_chain"
    CONTRIBUTOR = "contributor"
    AUDITOR = "auditor"
    OBSERVER = "observer"


class AvailabilityStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class ExtensionResultStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"


class ProviderAcceptance(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class ExtensionFailureKind(str, Enum):
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INVALID_RESULT = "invalid_result"
    FAILED = "failed"


@dataclass(frozen=True)
class ActorRef:
    id: str


@dataclass(frozen=True)
class NotebookRef:
    id: str


@runtime_checkable
class CancellationToken(Protocol):
    def is_set(self) -> bool: ...


@dataclass(frozen=True)
class Availability:
    status: AvailabilityStatus
    reason_code: str = ""

    @classmethod
    def available(cls) -> "Availability":
        return cls(AvailabilityStatus.AVAILABLE)


@dataclass(frozen=True)
class ExtensionFailure:
    """Sanitized extension failure; ``code`` must be stable and content-free."""

    kind: ExtensionFailureKind
    code: str


@dataclass(frozen=True)
class ProviderChainAttempt:
    acceptance: ProviderAcceptance
    reason_code: str = ""
    warning_code: str = ""


T = TypeVar("T")


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    value: T | None
    status: ExtensionResultStatus
    failure: ExtensionFailure | None = None


@dataclass(frozen=True)
class ProviderChainResult(Generic[T]):
    value: T | None
    attempt: ProviderChainAttempt
    failure: ExtensionFailure | None = None


@dataclass(frozen=True)
class ContributorResult(Generic[T]):
    items: tuple[T, ...]
    status: ExtensionResultStatus
    failure: ExtensionFailure | None = None


@dataclass(frozen=True)
class AuditorResult(Generic[T]):
    audit: T | None
    status: ExtensionResultStatus
    failure: ExtensionFailure | None = None


@dataclass(frozen=True)
class ObserverReceipt:
    status: ExtensionResultStatus
    failure: ExtensionFailure | None = None


@dataclass(frozen=True)
class ContributionDeclaration:
    id: str
    point: str
    kind: ContributionKind


@dataclass(frozen=True)
class ExtensionManifest:
    id: str
    version: str
    api_version: str
    display_name: str
    trust: Literal["builtin", "isolated"]
    contributions: tuple[ContributionDeclaration, ...]
    # Capability requirements.  The Phase-1 capability catalog will validate
    # that each named capability has a decision entry; these are not plugin ids.
    requires: tuple[str, ...] = ()
    optional_requires: tuple[str, ...] = ()
    # Explicit plugin-to-plugin topology, kept separate from capabilities.
    depends_on: tuple[str, ...] = ()


AvailabilityProbe = Callable[[object | None], Availability]


@dataclass(frozen=True)
class ExtensionContribution:
    declaration: ContributionDeclaration
    implementation: Any
    availability: AvailabilityProbe | None = None


class ExtensionRegistrar(Protocol):
    def add(self, contribution: ExtensionContribution) -> None: ...

    def add_provider(self, contribution: ExtensionContribution) -> None: ...

    def add_provider_chain_link(self, contribution: ExtensionContribution) -> None: ...

    def add_contributor(self, contribution: ExtensionContribution) -> None: ...

    def add_auditor(self, contribution: ExtensionContribution) -> None: ...

    def add_observer(self, contribution: ExtensionContribution) -> None: ...


class ExtensionBundle(Protocol):
    manifest: ExtensionManifest

    def register(self, registrar: ExtensionRegistrar) -> None: ...
