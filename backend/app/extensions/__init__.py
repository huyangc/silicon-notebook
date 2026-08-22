"""Extension composition and runtime registry."""

from app.extensions.bootstrap import (
    ExtensionRuntime,
    build_extension_registry,
    build_extension_runtime,
    default_extension_runtime,
)
from app.extensions.capabilities import CapabilityDecisionCatalog
from app.extensions.ask import AnswerAuditorHost, AskCompletedObserverHost
from app.extensions.report import ReportAuditorHost, ReportCompletedObserverHost
from app.extensions.element_enrichment import SourceElementEnricherHost
from app.extensions.knowledge_projection import KnowledgeCandidateProjectorHost
from app.extensions.agent_tools import AgentToolProviderHost
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError
from app.extensions.parser_chain import ParserChainCancelled, ParserProviderChainHost
from app.extensions.retrieval import RetrievalContributorHost, RetrievalHostCancelled

__all__ = [
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "ExtensionRuntime",
    "CapabilityDecisionCatalog",
    "AnswerAuditorHost",
    "AskCompletedObserverHost",
    "ReportAuditorHost",
    "ReportCompletedObserverHost",
    "SourceElementEnricherHost",
    "KnowledgeCandidateProjectorHost",
    "AgentToolProviderHost",
    "ParserChainCancelled",
    "ParserProviderChainHost",
    "RetrievalContributorHost",
    "RetrievalHostCancelled",
    "build_extension_registry",
    "build_extension_runtime",
    "default_extension_runtime",
]
