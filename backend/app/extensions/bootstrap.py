"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
import logging
from types import MappingProxyType
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
    AGENT_PROFILE_WORKSPACE_UI_CAPABILITY,
    ASK_AGENT_PROFILE_COMPLETED_BUNDLE,
    ASK_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE,
    ASK_RETRIEVAL_EXPERIENCE_COMPLETED_CONTRIBUTION_ID,
    ASK_SEARCH_PROFILE_COMPLETED_BUNDLE,
    ASK_SEARCH_PROFILE_COMPLETED_CONTRIBUTION_ID,
    REPORT_AGENT_PROFILE_COMPLETED_BUNDLE,
    REPORT_AGENT_PROFILE_COMPLETED_CONTRIBUTION_ID,
    REPORT_MARKDOWN_EXPORTER_BUNDLE,
    REPORT_MARKDOWN_EXPORTER_PLUGIN_ID,
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
from app.extensions.discovery import (
    ExtensionDiscoveryError,
    capability_decisions_from_bundles,
    discover_deployment_extensions,
)
from app.extensions.registry import ExtensionRegistry, frozen_registry
from app.extensions.parser_chain import ParserProviderChainHost
from app.extensions.retrieval import RetrievalContributorHost
from app.extensions.ask import AskCompletedObserverHost
from app.extensions.report import ReportCompletedObserverHost
from app.extensions.report_export import ReportExporterHost


_BOUND_AGENT_PROFILE_UI_PORT = object()

# Deployment-plugin rejections are logged here before being re-raised.  The
# record carries a plugin id, a stable reason code, and an exception class name
# and nothing else — never `exc.names`, `str(exc)`, a module path, a file path,
# or any settings value (see app.extensions.discovery).
_DISCOVERY_LOGGER = logging.getLogger("silicon_notebook.extensions")


@dataclass(frozen=True)
class ExtensionRuntime:
    registry: ExtensionRegistry
    retrieval_contributors: RetrievalContributorHost
    parser_chain: ParserProviderChainHost
    ask_completed_observers: AskCompletedObserverHost
    report_completed_observers: ReportCompletedObserverHost
    report_exporter: ReportExporterHost
    # Validated settings instance per deployment plugin, keyed by plugin id.
    # Built-in bundles never appear here.  The mapping is read-only so a later
    # consumer (the plugin route host) cannot mutate the frozen composition.
    plugin_settings: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )


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
    trusted_report_exporter_plugins: frozenset[str] = frozenset(),
    plugin_settings: Mapping[str, object] | None = None,
) -> ExtensionRuntime:
    registry = build_extension_registry(
        bundles, capability_decisions=capability_decisions
    )
    return ExtensionRuntime(
        registry=registry,
        plugin_settings=MappingProxyType(dict(plugin_settings or {})),
        retrieval_contributors=RetrievalContributorHost(
            registry,
            admission_policies=retrieval_admission_policies,
            event_sink=event_sink,
        ),
        parser_chain=ParserProviderChainHost(
            registry,
            event_sink=event_sink,
        ),
        ask_completed_observers=AskCompletedObserverHost(
            registry,
            event_sink=event_sink,
        ),
        report_completed_observers=ReportCompletedObserverHost(
            registry,
            event_sink=event_sink,
        ),
        report_exporter=ReportExporterHost(
            registry,
            trusted_plugin_ids=trusted_report_exporter_plugins,
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

    def agent_profile_ui_access(_context: object | None) -> Availability:
        # The default repository always binds the Agent Profile port.  Reuse the
        # feature's single deployment predicate here instead of restating its
        # Settings flag in the UI projection.  No store method, database query,
        # model call, or user data is touched by this live decision.
        from app.core.config import get_settings
        from app.services.reasoning_retrieval import profile_wiring_active

        if profile_wiring_active(get_settings(), _BOUND_AGENT_PROFILE_UI_PORT):
            return Availability.available()
        return Availability(
            AvailabilityStatus.DISABLED,
            "agent_profile_disabled",
        )

    builtin_bundles = (
        ASK_AGENT_PROFILE_COMPLETED_BUNDLE,
        ASK_RETRIEVAL_EXPERIENCE_COMPLETED_BUNDLE,
        ASK_SEARCH_PROFILE_COMPLETED_BUNDLE,
        REPORT_AGENT_PROFILE_COMPLETED_BUNDLE,
        REPORT_MARKDOWN_EXPORTER_BUNDLE,
        PARSER_BUILTIN_BUNDLE,
        GENERATED_QUESTION_BUNDLE,
        PARSER_CLOUD_BUNDLE,
        SELECTED_SOURCE_GRAPH_BUNDLE,
        PARSER_SELF_HOSTED_BUNDLE,
    )
    core_decisions = {
        AGENT_PROFILE_WORKSPACE_UI_CAPABILITY: agent_profile_ui_access,
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
    }

    # Lazily read Settings: this module is imported during composition, and the
    # deployment plugin list must be resolved per call (the lru_cache above is
    # the single freeze point) rather than at import time.
    from app.core.config import get_settings

    try:
        discovered = discover_deployment_extensions(
            get_settings().extensions_config
        )
        # Deployment plugins are appended after the built-in tuple, in plugin-id
        # order, so an unset EXTENSIONS_CONFIG leaves the frozen topology
        # byte-identical to what shipped with this build.
        bundles = builtin_bundles + tuple(item.bundle for item in discovered)
        capability_decisions = capability_decisions_from_bundles(
            bundles, core_decisions
        )
    except ExtensionDiscoveryError as exc:
        _DISCOVERY_LOGGER.error(
            "extension discovery FAILED — service will not start: "
            "plugin=%s reason=%s exc=%s",
            exc.plugin_id,
            exc.reason,
            exc.exception_type,
        )
        raise

    return build_extension_runtime(
        bundles,
        capability_decisions=capability_decisions,
        retrieval_admission_policies={
            GENERATED_QUESTION_CONTRIBUTION_ID: "atomic",
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
        trusted_report_exporter_plugins=frozenset(
            {REPORT_MARKDOWN_EXPORTER_PLUGIN_ID}
        ),
        plugin_settings={
            item.plugin_id: item.settings for item in discovered
        },
    )
