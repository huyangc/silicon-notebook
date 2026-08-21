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
    )
