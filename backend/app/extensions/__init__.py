"""Extension composition and runtime registry."""

from app.extensions.bootstrap import (
    ExtensionRuntime,
    build_extension_registry,
    build_extension_runtime,
    default_extension_runtime,
)
from app.extensions.capabilities import CapabilityDecisionCatalog
from app.extensions.ask import AskCompletedObserverHost
from app.extensions.report import ReportCompletedObserverHost
from app.extensions.report_export import ReportExporterHost
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError
from app.extensions.parser_chain import ParserChainCancelled, ParserProviderChainHost
from app.extensions.retrieval import RetrievalContributorHost, RetrievalHostCancelled

__all__ = [
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "ExtensionRuntime",
    "CapabilityDecisionCatalog",
    "AskCompletedObserverHost",
    "ReportCompletedObserverHost",
    "ReportExporterHost",
    "ParserChainCancelled",
    "ParserProviderChainHost",
    "RetrievalContributorHost",
    "RetrievalHostCancelled",
    "build_extension_registry",
    "build_extension_runtime",
    "default_extension_runtime",
]
