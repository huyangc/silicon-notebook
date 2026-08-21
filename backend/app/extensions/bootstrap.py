"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping

from app.extension_sdk import (
    GENERATED_QUESTION_ACCESS_CAPABILITY,
    PARSER_BUILTIN_ACCESS_CAPABILITY,
    PARSER_CLOUD_ACCESS_CAPABILITY,
    PARSER_SELF_HOSTED_ACCESS_CAPABILITY,
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    Availability,
    AvailabilityStatus,
    ExtensionBundle,
    ParserHostContext,
    RetrievalAdmissionPolicy,
    RetrievalHostContext,
)
from app.extensions.builtin import (
    GENERATED_QUESTION_BUNDLE,
    GENERATED_QUESTION_CONTRIBUTION_ID,
    PARSER_BUILTIN_BUNDLE,
    PARSER_BUILTIN_CONTRIBUTION_ID,
    PARSER_CLOUD_BUNDLE,
    PARSER_CLOUD_CONTRIBUTION_ID,
    PARSER_SELF_HOSTED_BUNDLE,
    PARSER_SELF_HOSTED_CONTRIBUTION_ID,
    SELECTED_SOURCE_GRAPH_BUNDLE,
    SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
)
from app.extensions.capabilities import (
    CapabilityDecision,
    CapabilityDecisionCatalog,
)
from app.extensions.registry import ExtensionRegistry, frozen_registry
from app.extensions.parser_chain import ParserProviderChainHost
from app.extensions.retrieval import RetrievalContributorHost


@dataclass(frozen=True)
class ExtensionRuntime:
    registry: ExtensionRegistry
    retrieval_contributors: RetrievalContributorHost
    parser_chain: ParserProviderChainHost


def build_extension_registry(
    bundles: Iterable[ExtensionBundle] = (),
    *,
    capability_decisions: dict[str, CapabilityDecision] | None = None,
) -> ExtensionRegistry:
    return frozen_registry(
        bundles,
        capability_catalog=CapabilityDecisionCatalog(capability_decisions),
    )


def build_extension_runtime(
    bundles: Iterable[ExtensionBundle] = (),
    *,
    capability_decisions: dict[str, CapabilityDecision] | None = None,
    retrieval_admission_policies: Mapping[
        str, RetrievalAdmissionPolicy
    ] | None = None,
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> ExtensionRuntime:
    registry = build_extension_registry(
        bundles, capability_decisions=capability_decisions
    )
    return ExtensionRuntime(
        registry=registry,
        retrieval_contributors=RetrievalContributorHost(
            registry,
            admission_policies=retrieval_admission_policies,
            event_sink=event_sink,
        ),
        parser_chain=ParserProviderChainHost(
            registry,
            event_sink=event_sink,
        ),
    )


@lru_cache(maxsize=1)
def default_extension_runtime() -> ExtensionRuntime:
    """The process-wide frozen topology shared by HTTP, CLI and workers."""

    def selected_graph_access(context: object | None) -> Availability:
        if (
            type(context) is RetrievalHostContext
            and context.selected_source_graph_access is not None
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "selected_source_graph_access_unavailable",
        )

    def generated_question_access(context: object | None) -> Availability:
        if (
            type(context) is RetrievalHostContext
            and context.generated_question_access is not None
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "generated_question_access_unavailable",
        )

    def parser_access(
        context: object | None, expected_contribution_id: str
    ) -> Availability:
        if (
            type(context) is ParserHostContext
            and context.contribution_id == expected_contribution_id
            and context.access is not None
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "parser_link_access_unavailable",
        )

    return build_extension_runtime(
        (
            PARSER_BUILTIN_BUNDLE,
            GENERATED_QUESTION_BUNDLE,
            PARSER_CLOUD_BUNDLE,
            SELECTED_SOURCE_GRAPH_BUNDLE,
            PARSER_SELF_HOSTED_BUNDLE,
        ),
        capability_decisions={
            GENERATED_QUESTION_ACCESS_CAPABILITY: generated_question_access,
            SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY: selected_graph_access,
            PARSER_SELF_HOSTED_ACCESS_CAPABILITY: lambda context: parser_access(
                context, PARSER_SELF_HOSTED_CONTRIBUTION_ID
            ),
            PARSER_CLOUD_ACCESS_CAPABILITY: lambda context: parser_access(
                context, PARSER_CLOUD_CONTRIBUTION_ID
            ),
            PARSER_BUILTIN_ACCESS_CAPABILITY: lambda context: parser_access(
                context, PARSER_BUILTIN_CONTRIBUTION_ID
            ),
        },
        retrieval_admission_policies={
            GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
    )
