"""Dependency-light contracts for the retrieval contributor extension point.

The host owns baseline preservation, scope/provenance validation, budgets and
events.  Contributors receive only a frozen request description plus narrow
capability ports; they never receive the baseline sequence, repository facade,
database connection, raw model client or mutable request context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, Protocol, TypeVar, runtime_checkable

from app.domain.extensions import (
    RetrievalContributorHostPort,
    RetrievalInvocation,
)
from app.extension_sdk.contracts import (
    ActorRef,
    CancellationToken,
    ContributorResult,
    NotebookRef,
)


RETRIEVAL_CONTRIBUTOR_POINT = "retrieval.contributor"
RETRIEVAL_SCOPE_READER_CAPABILITY = "retrieval:scope_bound_evidence"
SCHEDULED_MODEL_ACCESS_CAPABILITY = "model:scheduled_access"
RetrievalAdmissionPolicy = Literal["additive", "atomic"]
EvidenceProvenanceKind = Literal[
    "chunk", "element", "knowledge_object", "relation", "ppr"
]


@dataclass(frozen=True)
class RetrievalRunRef:
    id: str
    kind: str


@dataclass(frozen=True)
class FrozenRetrievalScopeRef:
    """Opaque identity for a server-frozen retrieval participant set."""

    id: str
    narrowed: bool


@dataclass(frozen=True)
class RetrievalContributionBudget:
    """A contributor-only budget that cannot borrow from baseline retrieval."""

    max_items: int
    max_tokens: int
    max_proposals: int
    deadline_monotonic: float | None = None


@dataclass(frozen=True)
class EvidenceProvenance:
    kind: EvidenceProvenanceKind
    reference: str


T = TypeVar("T")


@dataclass(frozen=True)
class EvidenceCandidate(Generic[T]):
    """Typed additive evidence proposed for validation by the core host."""

    identity: str
    notebook_id: str
    source_id: str
    provenance: EvidenceProvenance
    value: T
    token_cost: int


@dataclass(frozen=True)
class EvidenceReadRequest:
    """Bounded identities requested through a core-enforced frozen scope."""

    identities: tuple[str, ...]


@runtime_checkable
class ScopeBoundEvidenceReader(Protocol):
    """Core-owned bounded hydrate; implementations filter before reading."""

    def read(
        self, request: EvidenceReadRequest
    ) -> tuple[EvidenceCandidate[Any], ...]: ...


@runtime_checkable
class ScheduledModelAccess(Protocol):
    """A point-bound workload handle, never a cached physical client."""

    def chat_json(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class ConnectionLeaseProbe(Protocol):
    """Reports whether core code currently holds a database connection."""

    def is_connection_held(self) -> bool: ...


@dataclass(frozen=True)
class RetrievalExtensionContext:
    invocation: RetrievalInvocation
    actor: ActorRef
    notebook: NotebookRef
    scope: FrozenRetrievalScopeRef
    run: RetrievalRunRef
    cancellation: CancellationToken
    budget: RetrievalContributionBudget
    reader: ScopeBoundEvidenceReader | None
    models: ScheduledModelAccess | None


@dataclass(frozen=True)
class RetrievalAvailabilityContext:
    """I/O-free metadata supplied to a contribution availability probe."""

    plugin_id: str
    contribution_id: str
    invocation: RetrievalInvocation
    cancellation: CancellationToken | None = None


@dataclass(frozen=True)
class RetrievalHostContext:
    """Core-only call state projected into one narrower plugin context."""

    invocation: RetrievalInvocation
    actor: ActorRef
    notebook: NotebookRef
    scope: FrozenRetrievalScopeRef
    run: RetrievalRunRef
    cancellation: CancellationToken
    budget: RetrievalContributionBudget
    admission_reader: ScopeBoundEvidenceReader
    model_access: ScheduledModelAccess | None
    connection: ConnectionLeaseProbe
    event_sink: Callable[[dict[str, object]], None] | None = None


@runtime_checkable
class RetrievalContributor(Protocol):
    invocations: frozenset[RetrievalInvocation]

    def contribute(
        self, context: RetrievalExtensionContext
    ) -> ContributorResult[EvidenceCandidate[Any]]: ...


@dataclass(frozen=True)
class RetrievalContributionEvent:
    """Content-free event shape constructed by the host, never a plugin."""

    kind: str
    contribution_id: str
    outcome: str
    accepted_count: int
    dropped_count: int
    elapsed_ms: int
    failure_code: str = ""
