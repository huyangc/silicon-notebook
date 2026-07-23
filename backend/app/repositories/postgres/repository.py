"""PostgreSQL repository facade composed from the PostgreSQL persistence bundle."""
from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
from app.services.repository_facade import RepositoryFacade


class PostgresRepository(RepositoryFacade):
    def __init__(self, settings: Settings, *, model_provider: Any | None = None) -> None:
        factory = PostgresPersistenceBundleFactory()
        try:
            super().__init__(settings, factory, model_provider=model_provider)
        except BaseException:
            # Covers failures after bundle creation but before the facade has a
            # usable runtime (service/cache/file composition included).
            factory.close()
            raise

    def close(self) -> None:
        self._runtime.close()

    def _recover_interrupted_jobs(self) -> None:
        self.maintenance.recover_interrupted_jobs()

    @property
    def maintenance(self):
        adapter = self.__dict__.get("_maintenance")
        if adapter is None:
            from app.repositories.postgres.maintenance import (
                PostgresMaintenanceAdapter,
            )

            adapter = PostgresMaintenanceAdapter(self._runtime)
            self.__dict__["_maintenance"] = adapter
        return adapter
