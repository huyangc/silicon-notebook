"""The only formal active repository backend selector."""
from __future__ import annotations

from app.core.config import Settings
from app.core.database_url import database_identity
from app.repositories.ports import NotebookRepository
from app.services.sqlite_repository import SQLiteRepository


class RepositoryBackendUnavailableError(RuntimeError):
    """The selected formal backend has no installed repository adapter."""


def create_repository(settings: Settings) -> NotebookRepository:
    scheme = database_identity(settings.database_url).scheme
    if scheme == "sqlite":
        return SQLiteRepository(settings)
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

        return PostgresRepository(settings)
    raise AssertionError("validated settings returned an unsupported scheme")
