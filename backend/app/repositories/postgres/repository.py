"""PostgreSQL repository facade composed from the PostgreSQL persistence bundle."""
from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.domain.extensions import (
    AskCompletedObserverHostPort,
    ReportCompletedObserverHostPort,
    ParserProviderChainHostPort,
    RetrievalContributorHostPort,
)
from app.domain.gap_consult import GapConsultHostPort
from app.domain.ask_engine import AskEngineHostPort
from app.domain.indexing_pipeline import IndexingPipelineHostPort
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
        ask_completed_observer_host: AskCompletedObserverHostPort | None = None,
        report_completed_observer_host: ReportCompletedObserverHostPort | None = None,
        ask_engine_host: AskEngineHostPort | None = None,
        indexing_pipeline_host: IndexingPipelineHostPort | None = None,
        gap_consult_host: GapConsultHostPort | None = None,
        migrate: bool = True,
        seed: bool = True,
    ) -> None:
        """``migrate``/``seed`` forward the bundle's schema-ownership seam.

        Only a process that does NOT own the schema passes ``False`` — today
        that is the offline scale-build CLI, which runs beside a live service
        and must neither apply DDL nor re-seed the admin credential. Every
        other caller keeps the defaults and the current behaviour exactly.
        """
        factory = PostgresPersistenceBundleFactory(migrate=migrate, seed=seed)
        try:
            super().__init__(
                settings,
                factory,
                model_provider=model_provider,
                retrieval_contributor_host=retrieval_contributor_host,
                parser_provider_chain_host=parser_provider_chain_host,
                ask_completed_observer_host=ask_completed_observer_host,
                report_completed_observer_host=report_completed_observer_host,
                ask_engine_host=ask_engine_host,
                indexing_pipeline_host=indexing_pipeline_host,
                gap_consult_host=gap_consult_host,
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
            from app.services.checkup import (
                CheckupService,
                h45_version_key,
                probe_scale_index_integrity,
            )

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
                # H4/H5 memo 键的版本分量:(kg_reset_epoch, kg_mutation_seq) 二元组
                # (batch-3-W1 PR-2)。R1 (P1-1, post-review): 曾经这里独立接了一份
                # 裸 int(graph_seq_row(db,nb)[0]) 的旧线,与 SQLite 侧改用的
                # h45_version_key 脱节——生产 PostgreSQL 后端的 H4/H5 memo 因此仍在
                # 对 delete+reingest 别名。现在两后端共用同一份实现,结构上不可能
                # 再分叉。见 checkup.h45_version_key 的完整论证。
                kg_version=h45_version_key(rt.unified_kg),
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
            # 事件失效插槽已在 facade 构造期指向 __dict__ 晚解析的转发器(见
            # RepositoryFacade.__init__),这里不再绑实例(与 sqlite 侧同构,理由见那边)。
            self.__dict__["_checkup"] = c
        return c
