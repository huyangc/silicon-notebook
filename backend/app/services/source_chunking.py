from __future__ import annotations

import threading
from typing import Callable, Iterable

from app.core.config import Settings
from app.repositories.ports import ChunkStorePort, ChunkWrite, SourceStorePort
from app.services.chunking import build_chunks
from app.services.source_embedding import SourceEmbeddingService
from app.domain.indexing_pipeline import (
    BUILTIN_INDEXING_PIPELINE_VERSION,
    IndexingChunkProposal,
    IndexingPipelineHostPort,
    IndexingPipelineStalePlanError,
    IndexingPipelineUnavailableError,
)


class SourceChunkingService:
    """Chunk construction for the chunk-native retrieval layer: merge one
    source's elements into retrieval chunks (pure DB write, no network) and
    optionally backfill their vectors. Chunk boundaries stay a pure function
    of (elements, chunk_target_chars, chunk_overlap_chars); ids keep the
    ``ck-`` surrogate prefix minted through the module ``_new_id`` seam so
    deterministic-fixture replay keeps observing module patches.

    ``mark_unified_dirty`` is the facade's ``_mark_unified_kg_dirty`` seat
    (KG domain, stays on the facade until Gate 5), resolved at call time so
    per-instance monkeypatches keep observing the dirty bump."""

    def __init__(
        self,
        *,
        settings: Settings,
        sources: SourceStorePort,
        chunks: ChunkStorePort,
        embedding: SourceEmbeddingService,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        mark_unified_dirty: Callable[[str], None],
        notebooks=None,
        indexing_stage_store=None,
        indexing_pipelines: IndexingPipelineHostPort | None = None,
        event_log=None,
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.embedding = embedding
        self.new_id = new_id
        self.now = now
        self.mark_unified_dirty = mark_unified_dirty
        self.notebooks = notebooks
        self.indexing_stage_store = indexing_stage_store
        self.indexing_pipelines = indexing_pipelines
        self.event_log = event_log
        self._notebook_locks: dict[str, threading.RLock] = {}
        self._notebook_locks_guard = threading.Lock()

    def _notebook_lock(self, notebook_id: str) -> threading.RLock:
        with self._notebook_locks_guard:
            lock = self._notebook_locks.get(notebook_id)
            if lock is None:
                lock = threading.RLock()
                self._notebook_locks[notebook_id] = lock
            return lock

    def _pipeline_identity(self, notebook_id: str) -> tuple[str, str]:
        if self.notebooks is None:
            return "", BUILTIN_INDEXING_PIPELINE_VERSION
        state = self.notebooks.indexing_pipeline_state(notebook_id)
        pipeline_id = str(state["pipeline_id"] or "")
        if not pipeline_id:
            return "", BUILTIN_INDEXING_PIPELINE_VERSION
        if self.indexing_pipelines is None:
            raise IndexingPipelineUnavailableError(pipeline_id)
        option = self.indexing_pipelines.option(pipeline_id)
        if option is None or not option.available:
            raise IndexingPipelineUnavailableError(pipeline_id)
        desired_version = str(
            state["pipeline_version"] or BUILTIN_INDEXING_PIPELINE_VERSION
        )
        published_id = str(state["published_pipeline_id"] or "")
        published_version = str(
            state["published_pipeline_version"]
            or BUILTIN_INDEXING_PIPELINE_VERSION
        )
        if (
            pipeline_id != published_id
            or desired_version != option.version
            or option.version != published_version
        ):
            raise IndexingPipelineUnavailableError(pipeline_id)
        return pipeline_id, option.version

    def _emit_warning(self, pipeline_id: str, warning_code: str) -> None:
        if self.event_log is None:
            return
        try:
            self.event_log.emit(
                {
                    "kind": "indexing_pipeline_chunk_fallback",
                    "pipeline_id": pipeline_id,
                    "stage": "chunking",
                    "status": "fallback",
                    "warning_code": warning_code,
                }
            )
        except Exception:  # noqa: BLE001 - observability is fail-open
            pass

    def _validate_plugin_proposals(
        self,
        raw: Iterable[object],
        elements: list[dict],
    ) -> list[dict]:
        allowed_ids = {str(item["id"]) for item in elements}
        output: list[dict] = []
        for proposal in raw:
            if len(output) >= self.settings.indexing_pipeline_max_proposals_per_source:
                raise ValueError("too many indexing proposals")
            if type(proposal) is not IndexingChunkProposal:
                raise TypeError("invalid indexing proposal")
            if (
                type(proposal.text) is not str
                or not proposal.text.strip()
                or len(proposal.text)
                > self.settings.indexing_pipeline_max_text_chars
                or type(proposal.section_path) is not str
                or len(proposal.section_path)
                > self.settings.indexing_pipeline_max_text_chars
                or type(proposal.element_ids) is not tuple
                or not proposal.element_ids
                or len(proposal.element_ids)
                > self.settings.indexing_pipeline_max_element_refs
                or any(type(value) is not str for value in proposal.element_ids)
                or len(set(proposal.element_ids)) != len(proposal.element_ids)
                or any(value not in allowed_ids for value in proposal.element_ids)
            ):
                raise ValueError("invalid indexing proposal")
            output.append(
                {
                    "text": proposal.text,
                    "section_path": proposal.section_path,
                    "element_ids": list(proposal.element_ids),
                }
            )
        return output

    def _chunk_dicts(
        self,
        elements: list[dict],
        pipeline_id: str,
        *,
        allow_unpublished: bool = False,
    ) -> tuple[list[dict], str]:
        if not pipeline_id:
            return (
                build_chunks(
                    elements,
                    target_chars=self.settings.chunk_target_chars,
                    overlap_chars=self.settings.chunk_overlap_chars,
                ),
                "",
            )
        if self.indexing_pipelines is None:
            raise IndexingPipelineUnavailableError(pipeline_id)
        option = self.indexing_pipelines.option(pipeline_id)
        if option is None or not option.available:
            raise IndexingPipelineUnavailableError(pipeline_id)
        if not option.overrides_chunking:
            return (
                build_chunks(
                    elements,
                    target_chars=self.settings.chunk_target_chars,
                    overlap_chars=self.settings.chunk_overlap_chars,
                ),
                "",
            )
        outcome = self.indexing_pipelines.build_chunks(
            pipeline_id,
            elements,
            target_chars=self.settings.chunk_target_chars,
            overlap_chars=self.settings.chunk_overlap_chars,
        )
        warning = outcome.warning_code
        if outcome.proposals is not None:
            try:
                return self._validate_plugin_proposals(outcome.proposals, elements), ""
            except Exception:  # noqa: BLE001 - untrusted iterable degrades per source
                warning = "indexing_pipeline_invalid_chunk_proposal"
        self._emit_warning(pipeline_id, warning or "indexing_pipeline_chunk_failed")
        return (
            build_chunks(
                elements,
                target_chars=self.settings.chunk_target_chars,
                overlap_chars=self.settings.chunk_overlap_chars,
            ),
            warning or "indexing_pipeline_chunk_failed",
        )

    def build_chunks_for_source(self, source_id: str) -> None:
        """合并一个 source 的 source_elements 成检索 chunk(纯写库, 无网络)。
        幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。"""
        src = self.sources.get_source(source_id)
        notebook_id = src.notebook_id
        with self._notebook_lock(notebook_id):
            pipeline_id, _pipeline_version = self._pipeline_identity(notebook_id)
            elements = self.chunks.source_elements_for_chunking(source_id)
            chunk_dicts, _warning = self._chunk_dicts(elements, pipeline_id)
            rows = [ChunkWrite(id=self.new_id("ck"), text=c["text"],
                               section_path=c["section_path"],
                               element_ids=tuple(c["element_ids"])) for c in chunk_dicts]
            now = self.now()
            # 分块成功(build_chunks 正常返回,**含产 0 chunk 的纯标题 md**)才走到这里:
            # 把完成标记 chunked_at 与它认证的 chunk 数据在**同一事务**里提交。
            self.chunks.replace_source_chunks(
                source_id, notebook_id, rows, created_at=now, mark_chunked_at=now
            )
            # Every chunk-derived cache keys off this mutation sequence.
            self.mark_unified_dirty(notebook_id)

    def rebuild_notebook_chunks(
        self,
        notebook_id: str,
        *,
        job_id: str,
        pipeline_id: str,
        pipeline_version: str,
        pipeline_generation: str,
    ) -> dict[str, int]:
        """Compute a bounded complete chunk generation into durable staging."""
        if self.notebooks is None or self.indexing_stage_store is None:
            raise RuntimeError("indexing pipeline notebook store is not wired")
        with self._notebook_lock(notebook_id):
            source_ids = sorted(self.sources.all_visible_source_ids(notebook_id))
            self.indexing_stage_store.begin_indexing_pipeline_stage(
                job_id,
                notebook_id,
                pipeline_id,
                pipeline_version,
                pipeline_generation,
                source_ids,
            )
            rows_by_source: dict[str, list[ChunkWrite]] = {}
            total_proposals = 0
            total_chars = 0
            warning_count = 0
            for source_id in source_ids:
                elements = self.chunks.source_elements_for_chunking(source_id)
                chunk_dicts, warning = self._chunk_dicts(
                    elements, pipeline_id, allow_unpublished=True
                )
                if warning:
                    warning_count += 1
                total_proposals += len(chunk_dicts)
                total_chars += sum(len(item["text"]) for item in chunk_dicts)
                if (
                    total_proposals
                    > self.settings.indexing_pipeline_rebuild_max_proposals
                    or total_chars
                    > self.settings.indexing_pipeline_rebuild_max_text_chars
                ):
                    raise ValueError("indexing pipeline rebuild exceeds deployment bounds")
                rows_by_source[source_id] = [
                    ChunkWrite(
                        id=self.new_id("ck"),
                        text=item["text"],
                        section_path=item["section_path"],
                        element_ids=tuple(item["element_ids"]),
                    )
                    for item in chunk_dicts
                ]
            # Embedding/model I/O happens before and outside the final publish
            # transaction. Each source payload is durable but not readable.
            for source_id in source_ids:
                created_at = self.now()
                rows = rows_by_source[source_id]
                try:
                    vectors = self.embedding.compute_staged_chunk_vectors(
                        notebook_id,
                        [
                            {"_oid": row.id, "payload": {"text": row.text}}
                            for row in rows
                        ],
                    )
                except Exception:  # noqa: BLE001 - embeddings are fail-open
                    vectors = []
                    self._emit_warning(
                        pipeline_id, "indexing_pipeline_embedding_deferred"
                    )
                staged = self.indexing_stage_store.stage_indexing_pipeline_chunks(
                    job_id,
                    source_id,
                    {
                        "created_at": created_at,
                        "rows": [
                            {
                                "id": row.id,
                                "source_id": source_id,
                                "text": row.text,
                                "section_path": row.section_path,
                                "element_ids": list(row.element_ids),
                            }
                            for row in rows
                        ],
                        "vectors": vectors,
                    },
                )
                if not staged:
                    raise IndexingPipelineStalePlanError(notebook_id)
            return {
                "source_count": len(source_ids),
                "chunk_count": total_proposals,
                "warning_count": warning_count,
            }

    def chunk_and_embed_source(self, source_id: str) -> None:
        """build + embed(供回填脚本/测试同步调用)。"""
        self.build_chunks_for_source(source_id)
        self.embedding.embed_chunks_for_source(source_id)
