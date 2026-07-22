from __future__ import annotations

import concurrent.futures as _cf
from typing import Any, Callable, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.repositories.sqlite.chunk_store import ChunkStore
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.model_concurrency import current_model_concurrency
from app.services.model_config import model_client_fingerprint
from app.services.retrieval import _payload_text


class EmbeddingProviderFailure(RuntimeError):
    """Marks failures raised by the remote embedding call, not local storage."""

    def __init__(self, message: str, failed_fingerprint: str) -> None:
        super().__init__(message)
        self.failed_fingerprint = failed_fingerprint


class SourceEmbeddingService:
    """Vector COMPUTE orchestration for elements / KG objects / KG relations /
    chunks: ThreadPoolExecutor batching (``embed_batch_size`` ×
    ``embed_concurrency``, pinned thread-name prefixes emb-el/emb-kg/emb-rel/
    emb-ck), best-effort per-batch failure isolation, single-threaded embedder
    HTTP-client warm-up. Persistence stays in EmbeddingStore (one write
    transaction per flush).

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
        embedder: Callable[[], Any],
        event_log: EventLogger,
        now: Callable[[], str],
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.event_log = event_log
        self.now = now

    def embed_knowledge(
        self, object_id: str, notebook_id: str, payload: dict
    ) -> None:
        if not self.settings.embedder_configured:
            return
        text = _payload_text(payload).strip()
        if not text:
            return
        try:
            vector = self._run_embedding_call(
                lambda: self.embedder().embed_query(text[:2000]),
                task_prefix="emb-kg",
            )
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
    ) -> list[list]:
        state = current_model_concurrency()
        if state is not None:
            futures = [
                state.embedding.submit(fn, batch, task_prefix=task_prefix)
                for batch in batches
            ]
            return [future.result() for future in futures]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        with _cf.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=task_prefix
        ) as pool:
            return list(pool.map(fn, batches))

    def _run_embedding_call(
        self, fn: Callable[[], Any], *, task_prefix: str
    ) -> Any:
        state = current_model_concurrency()
        if state is None:
            return fn()
        return state.embedding.run(fn, task_prefix=task_prefix)

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

        rows = []
        for part in self._map_embedding_batches(
            _embed_only, batches, task_prefix="emb-el"
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

    def embed_elements_batch(self, notebook_id: str, items: List[dict]) -> int:
        """按 element 补向量(只嵌给定的行,不整源重嵌),返回真正落库的行数。

        ``items``: ``[{"element_id": str, "source_id": str, "text": str}, ...]``
        (缺向量的 element 由 maintenance.missing_element_embedding_rows 查出)。
        element_embeddings 主键是 element_id、写入走 INSERT OR REPLACE 纯 upsert,
        所以只补缺失是安全的——不会碰到同源已有的向量。

        与 embed_source(正常路径)**逐字节同构**:同样跳过 text.strip() 为空的元素,
        同样按 self.settings.embed_truncate_chars 截断原文。刻意不复用
        embed_chunks_batch——它硬编码 text[:2000],默认值恰好相同所以现在看不出差别,
        但一旦调大 EMBED_TRUNCATE_CHARS,补出来的向量就和主路径不同构、检索质量静默劣化。

        replace_element_vectors 的签名是 per-source(source_id, notebook_id, rows),
        故计算按 embed_batch_size 平铺并发、落库时按 source_id 分组逐组写。
        计算失败按批隔离(记 warning 后跳过该批),不炸整轮。"""
        if not self.settings.embedder_configured or not items:
            return 0
        trunc = self.settings.embed_truncate_chars
        pending: List[tuple] = []
        for it in items:
            text = it.get("text") or ""
            if not text.strip():
                continue
            pending.append((it["element_id"], it["source_id"], text[:trunc]))
        if not pending:
            return 0

        size = max(1, self.settings.embed_batch_size)
        embedder = self.embedder()
        self._warm_up(embedder)

        def _embed_only(batch: list) -> list:
            try:
                vectors = embedder.embed_texts([t for _, _, t in batch])
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed elements batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [
                (eid, sid, vector)
                for (eid, sid, _), vector in zip(batch, vectors)
            ]

        # 按「页」推进,每页算完立刻落库。向量是这里最占内存的东西(几十万 element
        # × 1024 维 float ≈ GB 级),这条命令的用途恰恰是修大库,自己 OOM 说不过去。
        #
        # ⚠ 只把 for 循环写成「逐批处理」是**无效**的:_map_embedding_batches 返回
        # list[list](内部 [future.result() for ...] 会等全部 future 完成再收集),
        # 遍历它时所有向量早已同时在内存里。必须在**喂进去之前**就分页——一页的
        # 并发度仍是 embed_concurrency,只是活着的向量被限制在一页之内。
        page_size = max(1, self.settings.embed_concurrency) * size
        now = self.now()
        written = 0
        for start in range(0, len(pending), page_size):
            page = pending[start:start + page_size]
            batches = [page[i:i + size] for i in range(0, len(page), size)]
            for part in self._map_embedding_batches(
                _embed_only, batches, task_prefix="emb-el"
            ):
                by_source: dict[str, list] = {}
                for element_id, source_id, vector in part:
                    by_source.setdefault(source_id, []).append((element_id, vector))
                # 同页内仍按 source_id 分组——replace_element_vectors 是 per-source 签名。
                for source_id, rows in by_source.items():
                    self.vectors.replace_element_vectors(
                        source_id, notebook_id, rows, created_at=now
                    )
                    written += len(rows)
        if not written:
            return 0
        self.event_log.logger.info(
            "embedded %s/%s missing elements for notebook %s",
            written, len(pending), notebook_id,
        )
        return written

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

        total = len(pending)
        done = 0
        buf: list = []
        parts = self._map_embedding_batches(
            _embed_only, batches, task_prefix="emb-kg"
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

        rows = []
        for part in self._map_embedding_batches(
            _embed_only, batches, task_prefix="emb-rel"
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
        if not self.settings.embedder_configured or not rows:
            return
        trunc = self.settings.embed_truncate_chars
        texts = [r["text"][:trunc] for r in rows]
        embedder = self.embedder()
        self._warm_up(embedder)
        try:
            vectors = self._run_embedding_call(
                lambda: embedder.embed_texts(texts),
                task_prefix="emb-ck",
            )
        except Exception as exc:
            raise EmbeddingProviderFailure(
                str(exc), model_client_fingerprint(embedder)
            ) from exc
        pairs = [(r["id"], vector) for r, vector in zip(rows, vectors)]
        self.vectors.replace_chunk_vectors(notebook_id, pairs, created_at=self.now())

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

        out = []
        for part in self._map_embedding_batches(
            _emb, batches, task_prefix="emb-ck"
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
