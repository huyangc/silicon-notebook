"""Point-specific contract for optional parsed-element metadata enrichment."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.extension_sdk.contracts import CancellationToken, ContributorResult


SOURCE_ELEMENT_ENRICHER_POINT = "source.element_enricher"
SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY = "source:element_content_access"


@dataclass(frozen=True, slots=True)
class ElementRef:
    """Opaque request-local authority; candidates must return this exact object."""

    token: object


@dataclass(frozen=True, slots=True)
class ElementView:
    ref: ElementRef
    element_type: str
    location_label: str
    text: str
    caption: str = ""


@dataclass(frozen=True, slots=True)
class ElementEnrichmentBudget:
    max_proposals: int
    max_metadata_bytes: int
    max_caption_chars: int
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class ElementEnrichmentCandidate:
    element: ElementRef
    metadata: dict[str, Any]
    caption: str = ""


@dataclass(frozen=True, slots=True)
class ElementEnrichmentAvailabilityContext:
    contribution_id: str
    element_count: int
    budget: ElementEnrichmentBudget


@dataclass(frozen=True, slots=True)
class ElementEnrichmentContext:
    elements: tuple[ElementView, ...]
    cancellation: CancellationToken
    budget: ElementEnrichmentBudget


@runtime_checkable
class ElementEnricher(Protocol):
    """Return one atomic ``AVAILABLE`` batch or a failure-only empty result.

    ``PARTIAL`` and ``UNAVAILABLE`` cannot carry candidates at this point: core
    admission isolates contributors and never persists a partially validated
    contribution.
    """

    def enrich(
        self, context: ElementEnrichmentContext
    ) -> ContributorResult[ElementEnrichmentCandidate]: ...
