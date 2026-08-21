"""Stable application ports for consuming extension hosts without a registry."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar


RetrievalInvocation = Literal["selected_evidence", "chunk_candidates"]
GENERATED_QUESTION_ACCESS_CAPABILITY = "retrieval:generated_question_access"
T = TypeVar("T")


@dataclass(frozen=True)
class RetrievalEvidenceProposal:
    """Core-authored proposal shape consumed by a built-in adapter.

    This lives in ``domain`` so application services can supply authoritative
    evidence without importing the Extension SDK or registry runtime.
    """

    identity: str
    notebook_id: str
    source_id: str
    provenance_kind: str
    provenance_reference: str
    value: Any
    token_cost: int


class RetrievalProposalSourcePort(Protocol):
    """One request-local source behind a narrow built-in capability."""

    def propose(self) -> tuple[RetrievalEvidenceProposal, ...]: ...

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]: ...


class RetrievalEvidenceReadPort(Protocol):
    """Core-owned batch authority for one retrieval invocation."""

    def read(
        self, identities: tuple[str, ...]
    ) -> tuple[RetrievalEvidenceProposal, ...]: ...


@dataclass(frozen=True)
class RetrievalContributionCallContext:
    """Core-only inputs from a workflow to the shared contributor host."""

    actor_id: str
    notebook_id: str
    scope_id: str
    scope_narrowed: bool
    run_id: str
    run_kind: str
    cancellation: Any
    max_items: int
    max_tokens: int
    max_proposals: int
    admission_source: RetrievalEvidenceReadPort
    selected_source_graph_source: RetrievalProposalSourcePort | None
    connection_probe: Any
    generated_question_source: RetrievalProposalSourcePort | None = None
    deadline_monotonic: float | None = None


class RetrievalContributorHostPort(Protocol):
    def run(
        self,
        baseline: Sequence[T],
        *,
        invocation: RetrievalInvocation,
        context_factory: Callable[[], Any] | None = None,
        call_context: RetrievalContributionCallContext | None = None,
        baseline_identity: Callable[[T], str] | None = None,
        cancellation: Any | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        disabled_capabilities: frozenset[str] = frozenset(),
    ) -> Sequence[T]: ...


ParserSourceKind = Literal["file", "url"]
ParserExecutionBoundary = Literal["local", "private_service", "public_cloud"]
PARSER_SELF_HOSTED_PROVIDER = "parser.mineru_self_hosted"
PARSER_CLOUD_PROVIDER = "parser.mineru_cloud"
PARSER_BUILTIN_PROVIDER = "parser.builtin"


@dataclass(frozen=True)
class ParserSourceDescriptor:
    """Content-free source metadata used to freeze parser routing."""

    kind: ParserSourceKind
    suffix: str


@dataclass(frozen=True)
class ParserRoute:
    """Core-owned decision made for every frozen link before provider I/O."""

    allowed: bool
    execution: ParserExecutionBoundary
    reason_code: str = ""
    fallback_warning_code: str = ""


@dataclass(frozen=True)
class ParserProbe:
    """Side-effect-free result of one request-local provider probe."""

    accepted: bool
    value: Any = None
    reason_code: str = ""


@dataclass(frozen=True)
class ParserAdmission:
    accepted: bool
    reason_code: str = ""


@dataclass(frozen=True)
class ParsedSource:
    """Application result shared by the parser chain and ingestion workflow."""

    elements: tuple[Any, ...]
    parser_mode: str
    mineru_error: str = ""
    warning_code: str = ""


class ParserProviderChainCallPort(Protocol):
    """One request-local core adapter consumed by the extension host."""

    source: ParserSourceDescriptor
    cancellation: Any
    connection: Any

    def route(self, contribution_id: str) -> ParserRoute: ...

    def probe(self, contribution_id: str) -> ParserProbe: ...

    def admit(self, contribution_id: str, value: Any) -> ParserAdmission: ...

    def materialize(self, contribution_id: str, value: Any) -> ParsedSource: ...

    def warning(self, warning_code: str) -> None: ...

    def event(self, receipt: dict[str, object]) -> None: ...


class ParserProviderChainHostPort(Protocol):
    def run_application(
        self,
        baseline: ParsedSource,
        *,
        call: ParserProviderChainCallPort,
    ) -> ParsedSource: ...
