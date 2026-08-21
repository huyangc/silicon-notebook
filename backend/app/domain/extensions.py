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


# Ask post-completion contracts deliberately live below the SDK/runtime.  The
# durable workflow can therefore invoke a frozen host without importing the
# extension registry, and plugins never receive repository/service objects.
ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY = (
    "ask:agent_profile_completed_access"
)
ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY = (
    "ask:retrieval_experience_completed_access"
)
ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY = (
    "ask:search_profile_completed_access"
)


@dataclass(frozen=True)
class AnswerAuditSnapshot:
    """Content-free, immutable facts about an already persisted answer.

    PR-07 intentionally exposes no answer/question/evidence text.  A future
    content auditor needs a separate privacy and full-stack delivery review;
    the first contract can still identify structural grounding risks without
    handing a plugin the mutable ``AskResponse`` graph.
    """

    mode_id: str
    grounded: bool
    evidence_level: str
    citation_count: int
    anchor_count: int
    model_error_count: int
    answer_chars: int
    conclusion_chars: int
    max_findings: int
    deadline_monotonic: float


@dataclass(frozen=True)
class CompletedAskNotification:
    """The smallest core notification needed by the three built-in observers."""

    actor_id: str
    notebook_id: str
    mode_id: str


class AgentProfileAskCompletedPort(Protocol):
    def notify(self) -> None: ...


class RetrievalExperienceAskCompletedPort(Protocol):
    def notify(self) -> None: ...


class SearchProfileAskCompletedPort(Protocol):
    def notify(self) -> None: ...


@dataclass(frozen=True)
class AskCompletedObserverCallContext:
    notification: CompletedAskNotification
    agent_profile: AgentProfileAskCompletedPort | None
    retrieval_experience: RetrievalExperienceAskCompletedPort | None
    search_profile: SearchProfileAskCompletedPort | None
    connection_probe: Any
    deadline_monotonic: float


class AnswerAuditorHostPort(Protocol):
    def audit_application(
        self,
        snapshot: AnswerAuditSnapshot,
        *,
        connection_probe: Any,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[Any, ...]: ...


class AskCompletedObserverHostPort(Protocol):
    def observe_application(
        self,
        call_context: AskCompletedObserverCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None: ...


# Deep Report post-terminal contracts mirror Ask's governance but remain
# point-specific: the report observer needs actor+notebook identity while the
# auditor sees only counts from an already committed artifact.
REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY = (
    "report:agent_profile_completed_access"
)


@dataclass(frozen=True)
class ReportAuditSnapshot:
    report_id: str
    section_count: int
    successful_section_count: int
    failed_section_count: int
    reference_count: int
    gap_count: int
    claim_ledgers_available: int
    claim_ledgers_partial: int
    unsupported_high_risk_assertions: int
    content_chars: int
    synthesis_status: str
    max_findings: int
    deadline_monotonic: float


@dataclass(frozen=True)
class CompletedReportNotification:
    report_id: str
    actor_id: str
    notebook_id: str
    terminal_status: str


class AgentProfileReportCompletedPort(Protocol):
    def notify(self) -> None: ...


@dataclass(frozen=True)
class ReportCompletedObserverCallContext:
    notification: CompletedReportNotification
    agent_profile: AgentProfileReportCompletedPort | None
    connection_probe: Any
    deadline_monotonic: float


class ReportAuditorHostPort(Protocol):
    def audit_application(
        self,
        snapshot: ReportAuditSnapshot,
        *,
        connection_probe: Any,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[Any, ...]: ...


class ReportCompletedObserverHostPort(Protocol):
    def observe_application(
        self,
        call_context: ReportCompletedObserverCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None: ...
