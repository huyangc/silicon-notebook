"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Callable, Mapping

from app.extension_sdk import ExtensionBundle, RetrievalAdmissionPolicy
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

    return build_extension_runtime()
