"""Extension composition and runtime registry."""

from app.extensions.bootstrap import (
    ExtensionRuntime,
    build_extension_registry,
    build_extension_runtime,
    default_extension_runtime,
)
from app.extensions.capabilities import CapabilityDecisionCatalog
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError
from app.extensions.retrieval import RetrievalContributorHost, RetrievalHostCancelled

__all__ = [
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "ExtensionRuntime",
    "CapabilityDecisionCatalog",
    "RetrievalContributorHost",
    "RetrievalHostCancelled",
    "build_extension_registry",
    "build_extension_runtime",
    "default_extension_runtime",
]
