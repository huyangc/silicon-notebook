"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping

from app.extension_sdk import (
    SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY,
    Availability,
    AvailabilityStatus,
    ExtensionBundle,
    RetrievalAdmissionPolicy,
    RetrievalHostContext,
)
from app.extensions.builtin import (
    SELECTED_SOURCE_GRAPH_BUNDLE,
    SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID,
)
from app.extensions.capabilities import (
    CapabilityDecision,
    CapabilityDecisionCatalog,
)
from app.extensions.registry import ExtensionRegistry, frozen_registry
from app.extensions.retrieval import RetrievalContributorHost


@dataclass(frozen=True)
class ExtensionRuntime:
    registry: ExtensionRegistry
    retrieval_contributors: RetrievalContributorHost


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

    return build_extension_runtime(
        (SELECTED_SOURCE_GRAPH_BUNDLE,),
        capability_decisions={
            SELECTED_SOURCE_GRAPH_ACCESS_CAPABILITY: selected_graph_access,
        },
        retrieval_admission_policies={
            SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID: "atomic",
        },
    )
