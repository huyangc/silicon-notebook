"""Point-specific contracts for source-local knowledge candidate projection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.extension_sdk.contracts import CancellationToken, ContributorResult
from app.domain.knowledge_projection import KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS


KNOWLEDGE_CANDIDATE_PROJECTOR_POINT = "knowledge.candidate_projector"
SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY = "data:scoped_source_elements:read"


@dataclass(frozen=True, slots=True)
class KnowledgeElementRef:
    """Opaque authority for one element in this projection invocation."""

    token: object


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateRef:
    """Request-local endpoint identity created by one contribution."""

    token: object


@dataclass(frozen=True, slots=True)
class KnowledgeElementView:
    ref: KnowledgeElementRef
    element_type: str
    location_label: str
    text: str


@dataclass(frozen=True, slots=True)
class KnowledgeSchemaView:
    object_type: str
    fields: tuple[str, ...]
    primary: str
    list_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionBudget:
    max_objects: int
    max_relations: int
    max_candidate_bytes: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionAvailabilityContext:
    contribution_id: str
    element_count: int
    schema_count: int
    budget: KnowledgeProjectionBudget


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceCandidate:
    element: KnowledgeElementRef
    quote: str


@dataclass(frozen=True, slots=True)
class KnowledgeObjectCandidate:
    ref: KnowledgeCandidateRef
    key: str
    object_type: str
    payload: dict[str, Any]
    evidence: tuple[KnowledgeEvidenceCandidate, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeRelationCandidate:
    source: KnowledgeCandidateRef
    target: KnowledgeCandidateRef
    edge_type: str
    evidence: tuple[KnowledgeEvidenceCandidate, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeCandidateBatch:
    objects: tuple[KnowledgeObjectCandidate, ...]
    relations: tuple[KnowledgeRelationCandidate, ...]


@dataclass(frozen=True, slots=True)
class KnowledgeProjectionContext:
    elements: tuple[KnowledgeElementView, ...]
    schemas: tuple[KnowledgeSchemaView, ...]
    cancellation: CancellationToken
    budget: KnowledgeProjectionBudget


@runtime_checkable
class KnowledgeCandidateProjector(Protocol):
    """Return one atomic candidate batch.

    The plugin never supplies source/notebook/generation/review/database ids.
    Core rebuilds evidence from exact request-local element refs, validates the
    active schema and edge vocabulary, assigns local ids, and performs the only
    write transaction.
    """

    def project(
        self, context: KnowledgeProjectionContext
    ) -> ContributorResult[KnowledgeCandidateBatch]: ...
