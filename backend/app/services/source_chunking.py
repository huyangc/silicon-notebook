from __future__ import annotations

from typing import Callable

from app.core.config import Settings
from app.repositories.sqlite.chunk_store import ChunkStore, ChunkWrite
from app.repositories.sqlite.source_store import SourceStore
from app.services.chunking import build_chunks
from app.services.source_embedding import SourceEmbeddingService


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
        sources: SourceStore,
        chunks: ChunkStore,
        embedding: SourceEmbeddingService,
        new_id: Callable[[str], str],
        now: Callable[[], str],
        mark_unified_dirty: Callable[[str], None],
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.embedding = embedding
        self.new_id = new_id
        self.now = now
        self.mark_unified_dirty = mark_unified_dirty

    def build_chunks_for_source(self, source_id: str) -> None:
        """合并一个 source 的 source_elements 成检索 chunk(纯写库, 无网络)。
        幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。"""
        src = self.sources.get_source(source_id)
        notebook_id = src.notebook_id
        elements = self.chunks.source_elements_for_chunking(source_id)
        chunk_dicts = build_chunks(elements,
                                   target_chars=self.settings.chunk_target_chars,
                                   overlap_chars=self.settings.chunk_overlap_chars)
        rows = [ChunkWrite(id=self.new_id("ck"), text=c["text"],
                           section_path=c["section_path"],
                           element_ids=tuple(c["element_ids"])) for c in chunk_dicts]
        self.chunks.replace_source_chunks(
            source_id, notebook_id, rows, created_at=self.now()
        )
        # Unconditionally bump kg_mutation_seq: chunk rows just changed, and every
        # chunk-derived cache (:elemchunk/:entchunk/:ppr_graph/scale-index version)
        # keys off the seq. Without this, kg_auto_extract=False (default) + no
        # existing KG never reaches the extract-path _mark_unified_kg_dirty, so a
        # freshly uploaded source's chunks stay invisible to warm caches forever
        # (no TTL, no other invalidation). Dirty=1 is also semantically right for
        # a pure chunk upload: a new source means the unified KG is stale anyway.
        self.mark_unified_dirty(notebook_id)
        # 分块成功(build_chunks 正常返回,**含产 0 chunk 的纯标题 md**)才走到这里:
        # 置完成标记 chunked_at。放在 build_chunks_for_source 末尾而非 process_source,
        # 是为了让 chunk_and_embed_source / scripts/build_chunks.py 等所有分块路径统一
        # 打标。本函数是直线代码、无 early-return——replace_source_chunks 对空 rows
        # 也照常提交(0 行),故 0-chunk 路径同样走到这里置值。失败会在上面某步抛出、
        # 到不了这里 → chunked_at 留 NULL,正是 H3 的损坏信号。
        self.sources.mark_chunked(source_id, self.now())

    def chunk_and_embed_source(self, source_id: str) -> None:
        """build + embed(供回填脚本/测试同步调用)。"""
        self.build_chunks_for_source(source_id)
        self.embedding.embed_chunks_for_source(source_id)
