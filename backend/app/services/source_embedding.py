from __future__ import annotations

import concurrent.futures as _cf
from typing import Any, Callable, List, Optional

from app.core.config import Settings
from app.core.event_logging import EventLogger
from app.repositories.ports import ChunkStorePort, EmbeddingStorePort, SourceStorePort
from app.services.retrieval import _payload_text


# Rows per keyset read page in ``embed_source``'s whole-source walk. A protocol
# boundary, not a tunable budget: it changes NOTHING about which elements are
# embedded, in what order, or in what batches (see the boundary note in
# ``embed_source``) — only how many element rows (text included) are resident at
# once while the walk advances. Deliberately larger than one embed batch so a
# page read is amortised across several provider calls.
_ELEMENT_READ_PAGE = 2000


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
        sources: SourceStorePort,
        chunks: ChunkStorePort,
        vectors: EmbeddingStorePort,
        embedder: Callable[[str], Any],
        parallelism: Callable[[str], int],
        event_log: EventLogger,
        now: Callable[[], str],
        invalidate_vector_matrix: "Callable[[str, str], None] | None" = None,
    ) -> None:
        self.settings = settings
        self.sources = sources
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.parallelism = parallelism
        self.event_log = event_log
        self.now = now
        # (notebook_id, embedding_table) -> drop retrieval's brute-force
        # _vector_matrix cache entry for that table. Paged (re-)embedding flushes
        # across several transactions; the cache is version-keyed on
        # (COUNT(*), MAX(created_at)), and a same-second re-embed of existing
        # rows changes neither, so a matrix built mid-pagination could be served
        # stale forever. Invalidating once the write completes is the
        # clock-independent guarantee (None = unwired: offline/test callers that
        # never build the cache). See the three batch embedders.
        self.invalidate_vector_matrix = invalidate_vector_matrix

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
            vector = embedder.embed_query(
                text[: self.settings.embed_truncate_chars]
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

    def _evict_matrix(self, notebook_id: str, table: str) -> None:
        """Drop retrieval's brute-force ``_vector_matrix`` cache entry for
        ``table`` after a paged (re-)embed — see ``invalidate_vector_matrix`` in
        __init__. Called from a ``finally`` so a page-flush failure still evicts
        the pages that already committed. No-op when unwired (offline/test)."""
        if self.invalidate_vector_matrix is not None:
            self.invalidate_vector_matrix(notebook_id, table)

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

        trunc = self.settings.embed_truncate_chars
        size = max(1, self.settings.embed_batch_size)

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

        # Page BEFORE mapping — the same fix the four sibling batch embedders
        # already carry (embed_elements_batch / embed_objects_batch /
        # embed_chunks_batch / embed_relations_batch), and the reason is identical:
        # _map_embedding_batches materialises a WHOLE call's results
        # (list(pool.map(...)) waits for every future), so handing it every batch
        # of the source at once holds every element vector of that source in
        # memory simultaneously — ~670MB for a 21k-element source at 1024 dims,
        # multiplied by the upload concurrency. Writing per page bounds live
        # vectors to embed_commit_batches × embed_batch_size (500 rows ≈ 16MB at
        # the defaults). Sources smaller than one page still take exactly one
        # write transaction, byte-identical to the pre-paging behaviour.
        #
        # ⚠ page size is counted in BATCHES, so every page except the last holds
        # a whole number of embed_batch_size batches — the embedder call
        # boundaries (and therefore the texts of each call) are unchanged.
        #
        # replace_element_vectors is a per-row upsert on both backends
        # (INSERT OR REPLACE / ON CONFLICT (element_id) DO UPDATE — no
        # delete-all-for-source), so flushing page by page can never drop a page
        # that already committed.
        #
        # ⚠ The READ is paged too (生产热点整改批 E). `source_elements()`
        # materialised the WHOLE source — every element row INCLUDING its full
        # text — before a single vector was computed, so the last unbounded
        # allocation of this path sat in front of all the bounding above. The
        # keyset walk (`source_elements_after`) hands back one
        # _ELEMENT_READ_PAGE-sized page at a time in the SAME global order
        # `source_elements()` used, and `carry` only ever holds the elements not
        # yet flushed.
        #
        # Batch and page boundaries stay BIT-IDENTICAL to the pre-paging read:
        # `carry` releases work only in whole `size * page_sz` blocks, so every
        # batch is exactly the `pending[i:i+size]` slice it always was and every
        # full page holds exactly `page_sz` of them; the tail (< one page) is
        # flushed once at the end, same as the old last page. The read page size
        # is therefore free to differ from either — it never moves a boundary.
        #
        # ⚠ What the paged read DOES give up is the single-statement snapshot:
        # the walk spans several reads, so a concurrent `replace_elements` could
        # in principle be observed half-old/half-new. That is bounded by the
        # SAME two things it always was, not by this loop: (a) a source's
        # elements are only ever replaced behind that source's write fence, and
        # (b) `replace_element_vectors` is a per-element upsert keyed on
        # element_id, and `replace_elements` cascades the old element rows away,
        # so a vector for a retired element cannot survive its element. The
        # authority for "this source's vectors are current" was never this
        # read's atomicity — it is the re-embed the ingest pipeline runs after
        # the replacement commits.
        page_sz = max(1, self.settings.embed_commit_batches)
        per_page = size * page_sz
        written = 0
        total_pending = 0
        saw_pending = False
        carry: list = []

        def _flush(page_batches: list) -> int:
            rows: list = []
            for part in self._map_embedding_batches(
                _embed_only,
                page_batches,
                task_prefix="emb-el",
                workload_id=workload_id,
            ):
                rows.extend(part)
            if rows:
                # Fresh timestamp per page (parity with the sibling batch
                # embedders); correctness against retrieval's
                # (COUNT(*), MAX(created_at)) matrix cache is guaranteed by
                # the _evict_matrix finally below, not by created_at moving.
                self.vectors.replace_element_vectors(
                    source_id, notebook_id, rows, created_at=self.now()
                )
            return len(rows)

        after: "tuple[Any, str] | None" = None
        try:
            while True:
                previous = after
                page, after = self.sources.source_elements_after(
                    source_id, after, _ELEMENT_READ_PAGE
                )
                # A cursor that does not strictly advance means the walk would
                # re-read the same page forever, re-embedding it and never
                # terminating — a silent hang inside a background ingest, the
                # worst possible failure mode. Fail loudly instead. (Not a
                # data-integrity check: `replace_element_vectors` is an upsert,
                # so a repeated page would be harmless in isolation; the
                # non-termination is the whole problem.)
                if after is not None and after == previous:
                    raise RuntimeError(
                        "source_elements_after cursor did not advance for "
                        f"source {source_id}"
                    )
                for element in page:
                    if element.text.strip():
                        carry.append(element)
                        total_pending += 1
                if carry and not saw_pending:
                    saw_pending = True
                    # Same single, single-threaded warm-up the unpaged read did
                    # — just deferred to the first element that actually needs
                    # embedding, so a source with nothing to embed still never
                    # touches the provider.
                    self._warm_up(embedder)
                while len(carry) >= per_page:
                    block, carry = carry[:per_page], carry[per_page:]
                    written += _flush(
                        [block[i:i + size] for i in range(0, per_page, size)]
                    )
                if after is None:
                    break
            if carry:
                written += _flush(
                    [carry[i:i + size] for i in range(0, len(carry), size)]
                )
                carry = []
        finally:
            # Now that the write spans several transactions, a matrix built
            # between two page flushes must not survive: re-embedding an already
            # embedded source leaves both COUNT(*) and MAX(created_at) unchanged
            # within the same second, so the version key cannot see it. Evict in
            # a finally so a mid-run failure still drops the pages that did
            # commit. No-op when unwired (offline/test callers).
            #
            # ⚠ Only THREE of the four siblings evict (objects / chunks /
            # relations) and embed_elements_batch deliberately does not — that
            # one only ever ADDS rows that were missing, so COUNT(*) moves and
            # the (COUNT, MAX(created_at)) version key invalidates by itself.
            # embed_source is the re-embed path: the rows already exist, COUNT
            # does not move, and within the same second neither does MAX — which
            # is exactly why paging its write forced this eviction.
            #
            # ⚠ `saw_pending` reproduces the pre-paging `if not pending: return`
            # early exit exactly: a source with no embeddable element never
            # warmed up, never evicted and never logged. Unlike then, that fact
            # is only known after the last read page, so it is a flag rather
            # than a return.
            if saw_pending:
                self._evict_matrix(notebook_id, "element_embeddings")
        if saw_pending:
            self.event_log.logger.info(
                "embedded %s/%s elements for source %s",
                written, total_pending, source_id,
            )

    def embed_elements_batch(self, notebook_id: str, items: List[dict]) -> int:
        """按 element 补向量(只嵌给定的行,不整源重嵌),返回真正落库的行数。

        ``items``: ``[{"element_id": str, "source_id": str, "text": str}, ...]``
        (缺向量的 element 由 maintenance 查出:交互式体检 backfill 走
        ``missing_element_embedding_ids`` + ``element_texts_by_ids``,离线 CLI 走
        ``missing_element_embedding_page``;判据三处逐字一致)。
        element_embeddings 主键是 element_id、写入走 INSERT OR REPLACE 纯 upsert,
        所以只补缺失是安全的——不会碰到同源已有的向量。

        与 embed_source(正常路径)**逐字节同构**:同样跳过 text.strip() 为空的元素,
        同样按 self.settings.embed_truncate_chars 截断原文。所有元素、对象、
        关系与 chunk 向量化路径共用这一真源,避免调整部署配置后不同
        回填路径产生不同口径的向量。

        replace_element_vectors 的签名是 per-source(source_id, notebook_id, rows),
        故计算按 embed_batch_size 平铺并发、落库时按 source_id 分组逐组写。
        计算失败按批隔离(记 warning 后跳过该批),不炸整轮。"""
        workload_id = "source_element_embedding"
        if not items:
            return 0
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
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
        # 并发度仍是 source_element_embedding 所绑定模型服务的
        # max_concurrency,只是活着的向量被限制在一页之内。
        page_size = max(1, self.parallelism(workload_id)) * size
        now = self.now()
        written = 0
        for start in range(0, len(pending), page_size):
            page = pending[start:start + page_size]
            batches = [page[i:i + size] for i in range(0, len(page), size)]
            for part in self._map_embedding_batches(
                _embed_only,
                batches,
                task_prefix="emb-el",
                workload_id=workload_id,
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
                            progress=None, commit_every: Optional[int] = None) -> int:
        """并发计算 payload 向量,**每 commit_every 批 flush 一次**(增量提交:中断可续跑、
        内存不攒全量)。每批计算失败照旧 log+跳过(best-effort)。"""
        workload_id = "knowledge_object_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
            return 0
        pending = []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if text:
                pending.append((
                    it["_oid"],
                    text[: self.settings.embed_truncate_chars],
                ))
        if not pending:
            if progress:
                progress(0, 0)
            return 0

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
        written = 0
        # Page the batches BEFORE mapping. _map_embedding_batches materialises a
        # whole call's results (list(pool.map(...))), so feeding it EVERY batch
        # at once holds every object vector in memory simultaneously (~32GB at
        # 8M objects) — an OOM in the very command meant to repair a big
        # library; `commit_every` used to bound only the write cadence, not the
        # live-vector peak. One commit_every-sized page bounds live vectors to
        # commit_every*embed_batch_size AND preserves the "≤commit_every batches
        # ⇒ a single write transaction" contract (one flush per page).
        try:
            for pstart in range(0, len(batches), commit_every):
                chunk = batches[pstart:pstart + commit_every]
                buf: list = []
                for part in self._map_embedding_batches(
                    _embed_only,
                    chunk,
                    task_prefix="emb-kg",
                    workload_id=workload_id,
                ):
                    buf.extend(part)
                done += sum(len(b) for b in chunk)
                if buf:
                    self.flush_object_vectors(notebook_id, buf)
                    written += len(buf)
                if progress:
                    progress(done, total)
            if progress:
                progress(total, total)
        finally:
            # Evict even if a page flush raised: earlier pages may already be
            # committed, and a same-second re-embed leaves (COUNT(*),
            # MAX(created_at)) unchanged — a stale matrix would otherwise survive
            # the failure.
            self._evict_matrix(notebook_id, "knowledge_embeddings")
        return written

    def _compute_staged_vectors(
        self,
        notebook_id: str,
        workload_id: str,
        pending: List[tuple[str, str]],
        *,
        task_prefix: str,
    ) -> List[dict]:
        """Compute JSON-safe vectors without writing a live product table.

        Notebook indexing rebuilds keep these rows in the durable stage until
        the final generation CAS.  Per-batch failures retain the established
        best-effort embedding contract; lexical/KG rows still publish.
        """
        embedder = self.embedder(workload_id)
        if not pending or not getattr(embedder, "configured", True):
            return []
        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        self._warm_up(embedder)

        def _embed_only(batch) -> list:
            try:
                vectors = embedder.embed_texts([text for _, text in batch])
            except Exception as exc:  # noqa: BLE001 - staged vectors are fail-open
                self.event_log.logger.warning(
                    "staged embedding batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [
                {
                    "id": row_id,
                    "vector": [float(value) for value in vector],
                    "created_at": self.now(),
                }
                for (row_id, _text), vector in zip(batch, vectors)
            ]

        output: List[dict] = []
        page_size = max(1, self.settings.embed_commit_batches)
        for offset in range(0, len(batches), page_size):
            for part in self._map_embedding_batches(
                _embed_only,
                batches[offset:offset + page_size],
                task_prefix=task_prefix,
                workload_id=workload_id,
            ):
                output.extend(part)
        return output

    def compute_staged_object_vectors(
        self, notebook_id: str, items: List[dict]
    ) -> List[dict]:
        pending = [
            (
                str(item["_oid"]),
                _payload_text(item["payload"])[
                    : self.settings.embed_truncate_chars
                ],
            )
            for item in items
            if _payload_text(item["payload"]).strip()
        ]
        return self._compute_staged_vectors(
            notebook_id,
            "knowledge_object_embedding",
            pending,
            task_prefix="stage-kg",
        )

    def compute_staged_relation_vectors(
        self, notebook_id: str, items: List[dict]
    ) -> List[dict]:
        pending = [
            (
                str(item["_rid"]),
                str(item.get("text") or "")[: self.settings.embed_truncate_chars],
            )
            for item in items
            if str(item.get("text") or "").strip()
        ]
        return self._compute_staged_vectors(
            notebook_id,
            "relation_embedding",
            pending,
            task_prefix="stage-rel",
        )

    def compute_staged_chunk_vectors(
        self, notebook_id: str, items: List[dict]
    ) -> List[dict]:
        pending = [
            (
                str(item["_oid"]),
                str((item.get("payload") or {}).get("text") or "")[
                    : self.settings.embed_truncate_chars
                ],
            )
            for item in items
            if str((item.get("payload") or {}).get("text") or "").strip()
        ]
        return self._compute_staged_vectors(
            notebook_id,
            "chunk_embedding",
            pending,
            task_prefix="stage-ck",
        )

    def embed_relations_batch(self, notebook_id: str, rel_items: List[dict]) -> None:
        """并发 COMPUTE 关系向量, 一次写事务持久化到 relation_embeddings。
        rel_items: [{"_rid": str, "text": str}]。best-effort,失败跳过。"""
        workload_id = "relation_embedding"
        embedder = self.embedder(workload_id)
        if not getattr(embedder, "configured", True):
            return
        pending = [
            (
                it["_rid"],
                it["text"][: self.settings.embed_truncate_chars],
            )
            for it in rel_items
            if it.get("text", "").strip()
        ]
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

        # Page before mapping (see embed_objects_batch): _map_embedding_batches
        # collects a whole call's vectors into memory, so mapping ALL batches at
        # once would hold every relation vector at once (~33GB at 8M relations).
        # replace_relation_vectors is a per-row INSERT OR REPLACE upsert, so
        # flushing per page is safe (no delete-all-for-notebook).
        page_sz = max(1, self.settings.embed_commit_batches)
        try:
            for pstart in range(0, len(batches), page_sz):
                rows: list = []
                for part in self._map_embedding_batches(
                    _embed_only,
                    batches[pstart:pstart + page_sz],
                    task_prefix="emb-rel",
                    workload_id=workload_id,
                ):
                    rows.extend(part)
                if rows:
                    # Fresh timestamp per page (parity with flush_object_vectors);
                    # correctness against retrieval's (count, max_created_at)
                    # matrix cache is GUARANTEED by the _evict_matrix finally
                    # below, which is clock-independent (a same-second re-embed
                    # leaves the version key unchanged) and abort-safe.
                    self.vectors.replace_relation_vectors(
                        notebook_id, rows, created_at=self.now()
                    )
        finally:
            self._evict_matrix(notebook_id, "relation_embeddings")

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
                pending.append((
                    it["_oid"],
                    text[: self.settings.embed_truncate_chars],
                ))
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

        # Page before mapping (see embed_objects_batch): mapping ALL batches at
        # once holds every chunk vector in memory (~7GB at 1.7M chunks).
        # replace_chunk_vectors is a per-row INSERT OR REPLACE upsert, so
        # flushing per page is safe (no delete-all-for-notebook).
        page_sz = max(1, self.settings.embed_commit_batches)
        try:
            for pstart in range(0, len(batches), page_sz):
                out: list = []
                for part in self._map_embedding_batches(
                    _emb,
                    batches[pstart:pstart + page_sz],
                    task_prefix="emb-ck",
                    workload_id=workload_id,
                ):
                    out.extend(part)
                if out:
                    # Fresh timestamp per page (see embed_relations_batch);
                    # correctness is guaranteed by the _evict_matrix finally, not
                    # by created_at advancing.
                    self.vectors.replace_chunk_vectors(
                        notebook_id, out, created_at=self.now()
                    )
        finally:
            self._evict_matrix(notebook_id, "chunk_embeddings")

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
        have = self.vectors.embedded_object_ids(db, notebook_id)
        missing = [
            {"_oid": obj["id"], "payload": obj.get("payload", {})}
            for obj in objects
            if obj["id"] not in have and _payload_text(obj.get("payload", {})).strip()
        ]
        if missing:
            self.embed_objects_batch(notebook_id, missing, progress=progress)
        elif progress:
            progress(0, 0)
