"""The only formal active repository backend selector."""
from __future__ import annotations

from app.core.config import Settings
from app.core.database_url import database_identity
from app.domain.extensions import (
    AskCompletedObserverHostPort,
    ReportCompletedObserverHostPort,
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.domain.gap_consult import GapConsultHostPort
from app.domain.ask_engine import AskEngineHostPort
from app.domain.indexing_pipeline import IndexingPipelineHostPort
from app.repositories.ports import NotebookRepository
from app.services.sqlite_repository import SQLiteRepository


class RepositoryBackendUnavailableError(RuntimeError):
    """The selected formal backend has no installed repository adapter."""


def create_repository(
    settings: Settings,
    *,
    retrieval_contributor_host: RetrievalContributorHostPort | None = None,
    parser_provider_chain_host: ParserProviderChainHostPort | None = None,
    ask_completed_observer_host: AskCompletedObserverHostPort | None = None,
    report_completed_observer_host: ReportCompletedObserverHostPort | None = None,
    ask_engine_host: AskEngineHostPort | None = None,
    indexing_pipeline_host: IndexingPipelineHostPort | None = None,
    gap_consult_host: GapConsultHostPort | None = None,
    migrate: bool = True,
    seed: bool = True,
) -> NotebookRepository:
    """``migrate``/``seed`` forward the schema-ownership seam both backend
    adapters already expose (``PostgresRepository``/``SQLiteRepository`` —
    see their docstrings). Defaults keep every existing caller's behaviour
    byte-for-byte: the two keywords are only added to the constructor call
    when a caller explicitly asks for something other than the default, so a
    stand-in/mocked backend that does not accept them (as in
    ``test_repository_factory.py``) is unaffected.

    Only a **read-only** tool that does not own the schema may pass
    ``migrate=False, seed=False`` — today that is
    ``scripts/reflect_shadow_rig.py``'s `search` subcommand, which runs
    retrieval against the live main database and must never trigger a schema
    migration or the seed step's unconditional admin-password rewrite.
    """
    host_kwargs = {}
    if migrate is not True or seed is not True:
        host_kwargs["migrate"] = migrate
        host_kwargs["seed"] = seed
    if retrieval_contributor_host is not None:
        host_kwargs["retrieval_contributor_host"] = retrieval_contributor_host
    if parser_provider_chain_host is not None:
        host_kwargs["parser_provider_chain_host"] = parser_provider_chain_host
    if ask_completed_observer_host is not None:
        host_kwargs["ask_completed_observer_host"] = ask_completed_observer_host
    if report_completed_observer_host is not None:
        host_kwargs["report_completed_observer_host"] = report_completed_observer_host
    if ask_engine_host is not None:
        host_kwargs["ask_engine_host"] = ask_engine_host
    if indexing_pipeline_host is not None:
        host_kwargs["indexing_pipeline_host"] = indexing_pipeline_host
    if gap_consult_host is not None:
        host_kwargs["gap_consult_host"] = gap_consult_host
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
