"""PostgreSQL repository facade composed from the PostgreSQL persistence bundle."""
from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.domain.extensions import (
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory
from app.services.repository_facade import RepositoryFacade


class PostgresRepository(RepositoryFacade):
    def __init__(
        self,
        settings: Settings,
        *,
        model_provider: Any | None = None,
        retrieval_contributor_host: RetrievalContributorHostPort | None = None,
        parser_provider_chain_host: ParserProviderChainHostPort | None = None,
    ) -> None:
        factory = PostgresPersistenceBundleFactory()
        try:
            super().__init__(
                settings,
                factory,
                model_provider=model_provider,
                retrieval_contributor_host=retrieval_contributor_host,
                parser_provider_chain_host=parser_provider_chain_host,
            )
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

    @property
    def checkup(self):
        """P2 体检聚合(H2–H8)——与 SQLiteRepository.checkup 同构(镜像 ``maintenance`` 的每后端各
        一份模式),只是 database/queries/maintenance 落到 postgres。checkup 本身后端中性(注入
        queries + count seam,不 import 任何后端),故两后端共用同一 service;H7/H8 走 scale artifacts
        (文件系统层、后端无关)。facade 是 lru_cache 单例 → checkup 单例,H7/H8 进程内缓存跨请求存活。"""
        c = self.__dict__.get("_checkup")
        if c is None:
            from app.services.checkup import CheckupService, probe_scale_index_integrity

            rt = self._runtime
            c = CheckupService(
                database=rt.database,
                queries=rt.queries,
                count_missing_chunk_vectors=(
                    lambda nb, exclude: self.maintenance.count_missing_chunk_vectors(nb, exclude)
                ),
                count_missing_element_vectors=(
                    lambda nb, exclude: self.maintenance.count_missing_element_vectors(nb, exclude)
                ),
                scale_index_state=(
                    lambda nb: str(rt.scale_artifacts.status(nb).get("state", ""))
                ),
                index_state_signature=(lambda nb: rt.scale_artifacts.state_signature(nb)),
                index_manifest_identity=(
                    lambda nb: rt.scale_artifact_store.scale_manifest_identity(nb)
                ),
                probe_index_integrity=(
                    lambda nb: probe_scale_index_integrity(
                        rt.scale_artifact_store.scale_dir(nb),
                        logger=rt.event_log.logger,
                    )
                ),
                active_source_ids=rt._active_source_ids_snapshot,
                now=rt.seams.now,
                event_log=rt.event_log,
            )
            self.__dict__["_checkup"] = c
        return c
