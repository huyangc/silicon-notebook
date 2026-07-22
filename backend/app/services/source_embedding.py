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
    chunks: ThreadPoolExecutor batching.  The producer pool is derived from
    the bound model service's ``parallelism(workload_id)``; the scheduled
    adapters remain the process-wide admission authority. Persistence stays in
    EmbeddingStore (one write transaction per flush).

    The ``embedder`` collaborator is late-bound so tests and runtime settings
    may replace the provider after construction. Object-vector flushes are
    owned directly by this service; flush errors propagate so an interrupted
    backfill keeps its already-committed groups. Batch compute failures remain
    isolated per batch and never abort the whole run."""

    def __init__(
        self,
        *,
        settings: Settings,
        sources: SourceStore,
        chunks: ChunkStore,
        vectors: EmbeddingStore,
        embedder: Callable[[str], Any],
        parallelism: Callable[[str], int],
        event_log: EventLogger,
        now: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.parallelism = parallelism
        self.event_log = event_log
        self.now = now

    def embed_knowledge(
        self, object_id: str, notebook_id: str, payload: dict
    ) -> None:
        workload_id = "knowledge_object_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
            return
        text = _payload_text(payload).strip()
        if not text:
            return
        try:
            vector = embedder.embed_query(text[:2000])
        except Exception:
            return
        self.vectors.replace_knowledge_vectors(
            notebook_id, [(object_id, vector)], created_at=self.now()
        )

    def flush_object_vectors(self, notebook_id: str, rows: list) -> None:
        if rows:
            self.vectors.replace_knowledge_vectors(
                notebook_id, rows, created_at=self.now()
            )

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

    def _map_embedding_batches(
        self,
        fn: Callable[[Any], list],
        batches: list,
        *,
        task_prefix: str,
        workload_id: str,
    ) -> list[list]:
        if not batches:
            return []
        workers = max(1, min(self.parallelism(workload_id), len(batches)))
        with _cf.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=task_prefix
        ) as pool:
            return list(pool.map(fn, batches))

    def embed_source(self, source_id: str) -> None:
        workload_id = "source_element_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
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

        rows = []
        for part in self._map_embedding_batches(
            _embed_only,
            batches,
            task_prefix="emb-el",
            workload_id=workload_id,
        ):
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
        workload_id = "knowledge_object_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
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

        total = len(pending)
        done = 0
        buf: list = []
        parts = self._map_embedding_batches(
            _embed_only,
            batches,
            task_prefix="emb-kg",
            workload_id=workload_id,
        )
        for bi, part in enumerate(parts, 1):
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
        workload_id = "relation_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
            return
        pending = [(it["_rid"], it["text"][:2000]) for it in rel_items if it.get("text", "").strip()]
        if not pending:
            return
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
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

        rows = []
        for part in self._map_embedding_batches(
            _embed_only,
            batches,
            task_prefix="emb-rel",
            workload_id=workload_id,
        ):
            rows.extend(part)
        if not rows:
            return
        self.vectors.replace_relation_vectors(
            notebook_id, rows, created_at=self.now()
        )

    def embed_chunk_ids(self, notebook_id: str, rows: List[dict]) -> None:
        """Incremental embed for an EXPLICIT list of chunks — knowhow-tables
        PR-1 Task 5's projector calls this with ONLY the row/cell's chunk(s)
        it just (re)wrote, never a whole source (embed_chunks_for_source
        would recompute vectors for every chunk under the source, undoing
        the "only the changed cell re-embeds" invariant project_row needs).

        Reuses the SAME single vector write path as the batch methods above
        (``vectors.replace_chunk_vectors``), but unlike embed_chunks_batch/
        embed_source (best-effort: log a warning and swallow per batch), this
        lets an embedder exception PROPAGATE to the caller. Knowhow
        projection needs to know SYNCHRONOUSLY whether embedding succeeded so
        it can flip the row's projection_status to 'failed' and emit a
        model_error event at the exact point of failure — best-effort
        silence would hide that from the row-status UI entirely.

        ``rows``: ``[{"id": chunk_id, "text": chunk_text}, ...]``. No-op (and
        no exception) if no embedder is configured or ``rows`` is empty —
        "embedder not set up" is a normal, non-failure state for this app,
        distinct from "embedder configured but the call failed"."""
        workload_id = "knowhow_embedding"
        embedder = self.embedder(workload_id)
        if not rows or not getattr(embedder, "configured", True):
            return
        trunc = self.settings.embed_truncate_chars
        texts = [r["text"][:trunc] for r in rows]
        self._warm_up(embedder)
        vectors = embedder.embed_texts(texts)
        pairs = [(r["id"], vector) for r, vector in zip(rows, vectors)]
        self.vectors.replace_chunk_vectors(notebook_id, pairs, created_at=self.now())

    def embed_chunks_for_source(self, source_id: str) -> None:
        """给一个 source 已写入的 chunk 补向量(并发+429退避)。无网络则 no-op。"""
        if not getattr(self.embedder("chunk_embedding"), "configured", True):
            return
        notebook_id = self.sources.get_source(source_id).notebook_id
        rows = self.chunks.source_chunks(source_id)
        items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
        self.embed_chunks_batch(notebook_id, items)

    def embed_chunks_batch(self, notebook_id: str, items: List[dict]) -> None:
        """并发调用 embedder(resilience 由 DashscopeEmbedder 层负责), 落 chunk_embeddings。
        每批失败时 log + 跳过(best-effort)。"""
        workload_id = "chunk_embedding"
        embedder = self.embedder(workload_id)
        if not items or not getattr(embedder, "configured", True):
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
        self._warm_up(embedder)

        def _emb(batch):
            try:
                vecs = embedder.embed_texts([t for _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning("embed chunks batch failed (%d) for %s: %s",
                                              len(batch), notebook_id, exc)
                return []
            return [(cid, v) for (cid, _), v in zip(batch, vecs)]

        out = []
        for part in self._map_embedding_batches(
            _emb,
            batches,
            task_prefix="emb-ck",
            workload_id=workload_id,
        ):
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
        if not getattr(
            self.embedder("knowledge_object_embedding"), "configured", True
        ):
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
