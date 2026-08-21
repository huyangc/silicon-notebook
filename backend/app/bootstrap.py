"""Application composition root joining core adapters and extension hosts."""
from __future__ import annotations

from app.core.config import Settings
from app.extensions import ExtensionRuntime, default_extension_runtime
from app.repositories.factory import create_repository
from app.repositories.ports import NotebookRepository


def application_extension_runtime() -> ExtensionRuntime:
    return default_extension_runtime()


def create_application_repository(settings: Settings) -> NotebookRepository:
    runtime = application_extension_runtime()
    return create_repository(
        settings,
        retrieval_contributor_host=runtime.retrieval_contributors,
        parser_provider_chain_host=runtime.parser_chain,
        answer_auditor_host=(
            runtime.answer_auditors if runtime.answer_auditors.has_auditors else None
        ),
        ask_completed_observer_host=runtime.ask_completed_observers,
        report_auditor_host=(
            runtime.report_auditors if runtime.report_auditors.has_auditors else None
        ),
        report_completed_observer_host=runtime.report_completed_observers,
        element_enricher_host=(
            runtime.element_enrichers
            if runtime.element_enrichers.has_contributors
            else None
        ),
        knowledge_candidate_projector_host=(
            runtime.knowledge_candidate_projectors
            if runtime.knowledge_candidate_projectors.has_contributors
            else None
        ),
    )
