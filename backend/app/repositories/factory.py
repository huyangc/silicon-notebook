"""The only formal active repository backend selector."""
from __future__ import annotations

from app.core.config import Settings
from app.core.database_url import database_identity
from app.domain.extensions import (
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.repositories.ports import NotebookRepository
from app.services.sqlite_repository import SQLiteRepository


class RepositoryBackendUnavailableError(RuntimeError):
    """The selected formal backend has no installed repository adapter."""


def create_repository(
    settings: Settings,
    *,
    retrieval_contributor_host: RetrievalContributorHostPort | None = None,
    parser_provider_chain_host: ParserProviderChainHostPort | None = None,
) -> NotebookRepository:
    host_kwargs = {}
    if retrieval_contributor_host is not None:
        host_kwargs["retrieval_contributor_host"] = retrieval_contributor_host
    if parser_provider_chain_host is not None:
        host_kwargs["parser_provider_chain_host"] = parser_provider_chain_host
    scheme = database_identity(settings.database_url).scheme
    if scheme == "sqlite":
        return SQLiteRepository(settings, **host_kwargs)
    if scheme == "postgresql":
        try:
            from app.repositories.postgres.repository import PostgresRepository
        except ModuleNotFoundError as exc:
            if exc.name in {
                "app.repositories.postgres",
                "app.repositories.postgres.repository",
            }:
                raise RepositoryBackendUnavailableError(
                    "PostgreSQL repository backend is not available"
                ) from None
            raise

        return PostgresRepository(settings, **host_kwargs)
    raise AssertionError("validated settings returned an unsupported scheme")
