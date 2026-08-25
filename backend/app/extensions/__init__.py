"""Extension composition and runtime registry."""

from app.extensions.bootstrap import (
    ExtensionRuntime,
    build_extension_registry,
    build_extension_runtime,
    default_extension_runtime,
)
from app.extensions.capabilities import CapabilityDecisionCatalog
from app.extensions.discovery import (
    DiscoveredExtension,
    ExtensionDiscoveryError,
    capability_decisions_from_bundles,
    discover_deployment_extensions,
)
from app.extensions.ask import AskCompletedObserverHost
from app.extensions.ask_engine import AskEngineHost
from app.extensions.indexing import IndexingPipelineHost
from app.extensions.gap_consult import GapConsultHost
from app.extensions.report import ReportCompletedObserverHost
from app.extensions.report_export import ReportExporterHost
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError
from app.extensions.parser_chain import ParserChainCancelled, ParserProviderChainHost
from app.extensions.retrieval import RetrievalContributorHost, RetrievalHostCancelled

__all__ = [
    "DiscoveredExtension",
    "ExtensionDiscoveryError",
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "ExtensionRuntime",
    "CapabilityDecisionCatalog",
    "AskCompletedObserverHost",
    "AskEngineHost",
    "IndexingPipelineHost",
    "GapConsultHost",
    "ReportCompletedObserverHost",
    "ReportExporterHost",
    "ParserChainCancelled",
    "ParserProviderChainHost",
    "RetrievalContributorHost",
    "RetrievalHostCancelled",
    "build_extension_registry",
    "build_extension_runtime",
    "capability_decisions_from_bundles",
    "default_extension_runtime",
    "discover_deployment_extensions",
]
