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
    GAP_CONSULT_MAX_QUERY_SOURCES,
    GAP_CONSULT_MAX_QUERIES_PER_SOURCE,
    GAP_CONSULT_PHRASE_MAX_CHARS,
    GAP_CONSULT_QUESTION_MAX_CHARS,
    GAP_SOURCE_DISPLAY_NAME_MAX_CHARS,
    GAP_SOURCE_LANGUAGES_MAX,
    GAP_SOURCE_LANGUAGE_MAX_CHARS,
    GAP_SOURCE_CONTENT_TYPE_MAX_CHARS,
    GAP_SOURCE_QUERY_ADVICE_MAX_CHARS,
    GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GAP_SUGGESTION_URL_MAX_CHARS,
    GAP_SUGGESTION_ACTUAL_QUERY_MAX_CHARS,
    GapConsultQuery,
    GapSuggestion,
)
from app.extension_sdk.contracts import (
    CancellationToken,
    ContributorResult,
)


ASK_GAP_CONSULT_POINT = "ask.gap_consult"


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    """A plugin-owned source the query model may select for consultation."""

    source_id: str
    display_name: str
    languages: tuple[str, ...] = ()
    content_type: str = ""
    query_advice: str = ""


@dataclass(frozen=True, slots=True)
class GapConsultAvailabilityContext:
    """I/O-free live availability input, mirroring the completed-observer one."""

    contribution_id: str
    deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class GapConsultExtensionContext:
    """Per-contribution projection.

    ``query`` is the contributor-specific projection of the frozen egress
    surface: each contributor sees only its own selected source phrases.
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
    # Optional at runtime: contributors without this method are not offered to
    # the source-selection model and are not consulted.
    def describe_sources(self) -> tuple[SourceDescriptor, ...]: ...

    def consult(
        self, context: GapConsultExtensionContext
    ) -> ContributorResult[GapSuggestion]: ...


__all__ = [
    "ASK_GAP_CONSULT_POINT",
    "GAP_CONSULT_MAX_GAP_PHRASES",
    "GAP_CONSULT_MAX_SUGGESTIONS",
    "GAP_CONSULT_MAX_QUERY_SOURCES",
    "GAP_CONSULT_MAX_QUERIES_PER_SOURCE",
    "GAP_CONSULT_PHRASE_MAX_CHARS",
    "GAP_CONSULT_QUESTION_MAX_CHARS",
    "GAP_SOURCE_DISPLAY_NAME_MAX_CHARS",
    "GAP_SOURCE_LANGUAGES_MAX",
    "GAP_SOURCE_LANGUAGE_MAX_CHARS",
    "GAP_SOURCE_CONTENT_TYPE_MAX_CHARS",
    "GAP_SOURCE_QUERY_ADVICE_MAX_CHARS",
    "GAP_SUGGESTION_SOURCE_LABEL_MAX_CHARS",
    "GAP_SUGGESTION_SUMMARY_MAX_CHARS",
    "GAP_SUGGESTION_TITLE_MAX_CHARS",
    "GAP_SUGGESTION_URL_MAX_CHARS",
    "GAP_SUGGESTION_ACTUAL_QUERY_MAX_CHARS",
    "GapConsultAvailabilityContext",
    "GapConsultContributor",
    "GapConsultExtensionContext",
    "GapConsultQuery",
    "GapSuggestion",
    "SourceDescriptor",
]
