from __future__ import annotations

import concurrent.futures as _cf
from typing import Any, Callable, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.retrieval import _payload_text


class SourceEmbeddingService:
    """Vector COMPUTE orchestration for elements / KG objects / KG relations /
    chunks: ThreadPoolExecutor batching (``embed_batch_size`` ×
    ``embed_concurrency``, pinned thread-name prefixes emb-el/emb-kg/emb-rel/
    emb-ck), best-effort per-batch failure isolation, single-threaded embedder
    HTTP-client warm-up. Persistence stays in EmbeddingStore (one write
    transaction per flush).

    Two collaborators are LATE-BOUND facade seams resolved at call time:

    - ``embedder`` — the facade's mutable ``self.embedder`` attribute (tests
      swap in fakes after construction);
    - ``flush_object_vectors`` — the facade's ``_flush_object_vectors``
      MASTER_V10 seat (kept on the facade until Gate 5): per-instance
      monkeypatches must observe every incremental object-vector commit, and
      flush errors must PROPAGATE so an interrupted backfill keeps its
      already-committed groups (test_node_embed_incremental's resume
      contract). Batch COMPUTE failures, by contrast, are isolated per batch
      and never abort the whole run."""

    def __init__(
        self,
        *,
        settings: Settings,
        sources: SourceStore,
        chunks: ChunkStore,
        vectors: EmbeddingStore,
        embedder: Callable[[], Any],
        event_log: EventLogger,
        flush_object_vectors: Callable[[str, list], None],
        now: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.event_log = event_log
        self.flush_object_vectors = flush_object_vectors
        self.now = now

    @staticmethod
    def _warm_up(embedder: Any) -> None:
        # Pre-create the embedder's HTTP client single-threaded to avoid a lazy-init
        # race when many worker threads first touch it (no-op for fakes).
        ensure = getattr(embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

    def embed_source(self, source_id: str) -> None:
        if not self.settings.embedder_configured:
            return
        source = self.sources.get_source(source_id)
        notebook_id = source.notebook_id
        elements = self.sources.source_elements(source_id)
        pending = [el for el in elements if el.text.strip()]
        if not pending:
            return

        trunc = self.settings.embed_truncate_chars
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]

        embedder = self.embedder()
        self._warm_up(embedder)

        def _embed_only(els: list) -> list:
            texts = [el.text[:trunc] for el in els]
            try:
                vectors = embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed batch failed (%d elements) for source %s: %s",
                    len(els), source_id, exc,
                )
                return []
            return [(el.id, vector) for el, vector in zip(els, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-el") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        now = self.now()
        if rows:
            self.vectors.replace_element_vectors(
                source_id, notebook_id, rows, created_at=now
            )
        self.event_log.logger.info(
            "embedded %s/%s elements for source %s", len(rows), len(pending), source_id
        )

    def embed_objects_batch(self, notebook_id: str, items: List[dict],
                            progress=None, commit_every: Optional[int] = None) -> None:
        """并发计算 payload 向量,**每 commit_every 批 flush 一次**(增量提交:中断可续跑、
        内存不攒全量)。每批计算失败照旧 log+跳过(best-effort)。"""
        if not self.settings.embedder_configured:
            return
        pending = []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            if progress:
                progress(0, 0)
            return

        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        commit_every = commit_every or max(1, self.settings.embed_commit_batches)
        embedder = self.embedder()
        self._warm_up(embedder)

        def _embed_only(batch) -> list:
            texts = [t for _, t in batch]
            try:
                vectors = embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed kg-objects batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [(oid, vec) for (oid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        total = len(pending)
        done = 0
        buf: list = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-kg") as pool:
            for bi, part in enumerate(pool.map(_embed_only, batches), 1):
                buf.extend(part)
                done += len(batches[bi - 1])
                if bi % commit_every == 0:
                    self.flush_object_vectors(notebook_id, buf)
                    buf = []
                    if progress:
                        progress(done, total)
        if buf:
            self.flush_object_vectors(notebook_id, buf)
        if progress:
            progress(total, total)

    def embed_relations_batch(self, notebook_id: str, rel_items: List[dict]) -> None:
        """并发 COMPUTE 关系向量, 一次写事务持久化到 relation_embeddings。
        rel_items: [{"_rid": str, "text": str}]。best-effort,失败跳过。"""
        if not self.settings.embedder_configured:
            return
        pending = [(it["_rid"], it["text"][:2000]) for it in rel_items if it.get("text", "").strip()]
        if not pending:
            return
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        embedder = self.embedder()
        self._warm_up(embedder)

        def _embed_only(batch) -> list:
            try:
                vectors = embedder.embed_texts([t for _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort per batch
                self.event_log.logger.warning(
                    "embed kg-relations batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc)
                return []
            return [(rid, vec) for (rid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        rows = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-rel") as pool:
            for part in pool.map(_embed_only, batches):
                rows.extend(part)
        if not rows:
            return
        self.vectors.replace_relation_vectors(
            notebook_id, rows, created_at=self.now()
        )

    def embed_chunks_for_source(self, source_id: str) -> None:
        """给一个 source 已写入的 chunk 补向量(并发+429退避)。无网络则 no-op。"""
        if not self.settings.embedder_configured:
            return
        notebook_id = self.sources.get_source(source_id).notebook_id
        rows = self.chunks.source_chunks(source_id)
        items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
        self.embed_chunks_batch(notebook_id, items)

    def embed_chunks_batch(self, notebook_id: str, items: List[dict]) -> None:
        """并发调用 embedder(resilience 由 DashscopeEmbedder 层负责), 落 chunk_embeddings。
        每批失败时 log + 跳过(best-effort)。"""
        if not self.settings.embedder_configured or not items:
            return
        pending = []
        for it in items:
            text = (it["payload"].get("text") or "").strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            return
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i+size] for i in range(0, len(pending), size)]
        embedder = self.embedder()
        self._warm_up(embedder)

        def _emb(batch):
            try:
                vecs = embedder.embed_texts([t for _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning("embed chunks batch failed (%d) for %s: %s",
                                              len(batch), notebook_id, exc)
                return []
            return [(cid, v) for (cid, _), v in zip(batch, vecs)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        out = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-ck") as pool:
            for part in pool.map(_emb, batches):
                out.extend(part)
        if not out:
            return
        self.vectors.replace_chunk_vectors(
            notebook_id, out, created_at=self.now()
        )

    def backfill_knowledge_embeddings(self, db, notebook_id: str,
                                      objects: List[dict], progress=None) -> None:
        """Embed + persist any knowledge objects missing a vector, concurrently
        (rides embed_objects_batch). No-op when all are embedded or no
        embedder. Task 26: orchestration moved from the facade; the "have"
        probe reads through the caller's connection so the facade `_connect`
        boundary stays observable."""
        if not self.settings.embedder_configured:
            return
        have = EmbeddingStore.embedded_object_ids(db, notebook_id)
        missing = [
            {"_oid": obj["id"], "payload": obj.get("payload", {})}
            for obj in objects
            if obj["id"] not in have and _payload_text(obj.get("payload", {})).strip()
        ]
        if missing:
            self.embed_objects_batch(notebook_id, missing, progress=progress)
        elif progress:
            progress(0, 0)
