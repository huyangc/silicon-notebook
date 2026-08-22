"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping

from app.extension_sdk import (
    ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY,
    ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    GENERATED_QUESTION_ACCESS_CAPABILITY,
    PARSER_BUILTIN_ACCESS_CAPABILITY,
    PARSER_CLOUD_ACCESS_CAPABILITY,
    PARSER_SELF_HOSTED_ACCESS_CAPABILITY,
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    Availability,
    AvailabilityStatus,
    AskCompletedAvailabilityContext,
    ReportCompletedAvailabilityContext,
    ExtensionBundle,
    ParserHostContext,
    RetrievalAdmissionPolicy,
    RetrievalHostContext,
)
from app.extensions.builtin import (
    ASK_AGENT_PROFILE_COMPLETED_BUNDLE,
    ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID,
    ASK_SEARCH_PROFILE_COMPLETED_BUNDLE,
    ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID,
    REPORT_AGENT_PROFILE_COMPLETED_BUNDLE,
    REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
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
from app.extensions.ask import AnswerAuditorHost, AskCompletedObserverHost
from app.extensions.report import ReportAuditorHost, ReportCompletedObserverHost
from app.extensions.element_enrichment import SourceElementEnricherHost
from app.extensions.knowledge_projection import KnowledgeCandidateProjectorHost
from app.extensions.agent_tools import AgentToolProviderHost


@dataclass(frozen=True)
class ExtensionRuntime:
    registry: ExtensionRegistry
    retrieval_contributors: RetrievalContributorHost
    parser_chain: ParserProviderChainHost
    answer_auditors: AnswerAuditorHost
    ask_completed_observers: AskCompletedObserverHost
    report_auditors: ReportAuditorHost
    report_completed_observers: ReportCompletedObserverHost
    element_enrichers: SourceElementEnricherHost
    knowledge_candidate_projectors: KnowledgeCandidateProjectorHost
    agent_tools: AgentToolProviderHost


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
    trusted_agent_tool_plugins: frozenset[str] = frozenset(),
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
        answer_auditors=AnswerAuditorHost(
            registry,
            event_sink=event_sink,
        ),
        ask_completed_observers=AskCompletedObserverHost(
            registry,
            event_sink=event_sink,
        ),
        report_auditors=ReportAuditorHost(
            registry,
            event_sink=event_sink,
        ),
        report_completed_observers=ReportCompletedObserverHost(
            registry,
            event_sink=event_sink,
        ),
        element_enrichers=SourceElementEnricherHost(
            registry,
            event_sink=event_sink,
        ),
        knowledge_candidate_projectors=KnowledgeCandidateProjectorHost(
            registry,
            event_sink=event_sink,
        ),
        agent_tools=AgentToolProviderHost(
            registry,
            trusted_plugin_ids=trusted_agent_tool_plugins,
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

    def ask_completed_access(
        context: object | None, expected_contribution_id: str
    ) -> Availability:
        if (
            type(context) is AskCompletedAvailabilityContext
            and context.contribution_id == expected_contribution_id
            and context.access_available is True
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "ask_completed_access_unavailable",
        )

    def report_completed_access(context: object | None) -> Availability:
        if (
            type(context) is ReportCompletedAvailabilityContext
            and context.contribution_id
            == REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID
            and context.access_available is True
        ):
            return Availability.available()
        return Availability(
            AvailabilityStatus.UNAVAILABLE,
            "report_completed_access_unavailable",
        )

    return build_extension_runtime(
        (
            ASK_AGENT_PROFILE_COMPLETED_BUNDLE,
            ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE,
            ASK_SEARCH_PROFILE_COMPLETED_BUNDLE,
            REPORT_AGENT_PROFILE_COMPLETED_BUNDLE,
            PARSER_BUILTIN_BUNDLE,
            GENERATED_QUESTION_BUNDLE,
            PARSER_CLOUD_BUNDLE,
            SELECTED_SOURCE_GRAPH_BUNDLE,
            PARSER_SELF_HOSTED_BUNDLE,
        ),
        capability_decisions={
            ASK_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY: lambda context: (
                ask_completed_access(
                    context,
                    ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
                )
            ),
            ASK_RETRIEVAL_EXPERIENCE_COMPLETED_ACCESS_CAPABILITY: (
                lambda context: ask_completed_access(
                    context,
                    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID,
                )
            ),
            ASK_SEARCH_PROFILE_COMPLETED_ACCESS_CAPABILITY: lambda context: (
                ask_completed_access(
                    context,
                    ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID,
                )
            ),
            REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY: (
                report_completed_access
            ),
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
