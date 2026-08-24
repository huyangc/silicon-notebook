"""Point-specific contracts for gap-consultation extensions.

A contributor at this point answers with pointers to material *outside* the
notebook.  It receives no core port of any kind — no evidence reader, no
scheduled model access, no connection probe, no settings, no repository — so
the surface it can reach is exactly the bounded query the core hands it.

The value types and the character limits are re-exported from
``app.domain.gap_consult`` so a plugin manifest and the core wire model read
the same constants instead of restating them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.gap_consult import (
    GAP_CONSULT_MAX_GAP_PHRASES,
    GAP_CONSULT_MAX_SUGGESTIONS,
    GAP_CONSULT_PHRASE_MAX_CHARS,
    GAP_CONSULT_QUESTION_MAX_CHARS,
    GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_URL_MAX_CHARS,
    GapConsultQuery,
    GapSuggestion,
)
from app.extension_sdk.contracts import (
    CancellationToken,
    ContributorResult,
)


ASK_GAP_CONSULT_POINT = "ask.gap_consult"


@dataclass(frozen=True, slots=True)
class GapConsultAvailabilityContext:
    """I/O-free live availability input, mirroring the completed-observer one."""

    contribution_id: str
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class GapConsultExtensionContext:
    """Per-contribution projection.

    ``query`` is the frozen egress surface — identical for every contributor
    and identical to what an audit of this run would show was sent outward.
    ``max_suggestions`` is this call's *remaining* budget and therefore may be
    smaller than ``query.max_suggestions``, which records what the run as a
    whole was willing to accept.

    There is deliberately no core port field here.  Adding one would make this
    an ordinary retrieval seat, and the whole premise of the point is that its
    output is not evidence.
    """

    query: GapConsultQuery
    cancellation: CancellationToken | None
    max_suggestions: int
    deadline_monotonic: float


class GapConsultContributor(Protocol):
    def consult(
        self, context: GapConsultExtensionContext
    ) -> ContributorResult[GapSuggestion]: ...


__all__ = [
    "ASK_GAP_CONSULT_POINT",
    "GAP_CONSULT_MAX_GAP_PHRASES",
    "GAP_CONSULT_MAX_SUGGESTIONS",
    "GAP_CONSULT_PHRASE_MAX_CHARS",
    "GAP_CONSULT_QUESTION_MAX_CHARS",
    "GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS",
    "GAP_SUGGESTION_SUMMARY_MAX_CHARS",
    "GAP_SUGGESTION_TITLE_MAX_CHARS",
    "GAP_SUGGESTION_URL_MAX_CHARS",
    "GapConsultAvailabilityContext",
    "GapConsultContributor",
    "GapConsultExtensionContext",
    "GapConsultQuery",
    "GapSuggestion",
]
