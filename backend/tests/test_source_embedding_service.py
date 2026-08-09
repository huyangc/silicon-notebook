# backend/tests/test_source_embedding_service.py
"""Task 11: SourceEmbeddingService — element/object/relation/chunk vector
COMPUTE orchestration extracted off the facade.

Invariants under test:
- every method resolves its explicit workload through the system provider;
- the embedder's HTTP-client warm-up (``_ensure``) runs once, single-threaded,
  before the worker pool — and a warm-up failure never aborts embedding;
- element texts are truncated to ``embed_truncate_chars`` before compute;
- object-vector flushes are owned by ``SourceEmbeddingService``
  (per-instance monkeypatches keep observing them; flush errors PROPAGATE —
  the incremental-commit/resume contract of test_node_embed_incremental);
- a failed embed batch is isolated (other batches persist / reach the store);
- with no embedder configured every path is a silent no-op.
"""
import threading

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import sqlite_repository
from app.services.source_embedding import SourceEmbeddingService
from tests.model_testkit import RecordingModelProvider


def _make_repo(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    provider = RecordingModelProvider(
        parallelism_by_workload={
            "source_element_embedding": 8,
            "knowledge_object_embedding": 8,
            "relation_embedding": 8,
            "chunk_embedding": 8,
            "knowhow_embedding": 8,
        }
    )
    repository = sqlite_repository.SQLiteRepository(
        Settings(model_services_config=""), model_provider=provider
    )
    repository.recording_model_provider = provider
    return repository


def _bind(repo, workload_id, embedder):
    repo.recording_model_provider.embedding_clients = {
        **repo.recording_model_provider.embedding_clients,
        workload_id: embedder,
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    return _make_repo(tmp_path, monkeypatch)


def _insert_source_with_elements(repo, notebook_id, n, text="element text number {i}"):
    from uuid import uuid4

    sid = f"src-{uuid4().hex[:10]}"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
               VALUES (?,?,?, 'markdown','parsed','parsed', ?, '', 0, '', '', '', ?, ?)""",
            (sid, notebook_id, "Doc", "doc.md", now, now))
        for i in range(n):
            db.execute(
                """INSERT INTO source_elements (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, 'paragraph', ?, ?, '{}', ?)""",
                (f"el-{sid}-{i:04d}", sid, f"p{i}", text.format(i=i), now))
    return sid


class _RecordingEmbedder:
    """Records warm-up ordering, thread names and received texts; can fail
    batches containing a sentinel; can fail warm-up."""

    def __init__(self, dim=8, fail_substr=None, ensure_raises=False):
        self.dim = dim
        self._fail = fail_substr
        self._ensure_raises = ensure_raises
        self.events = []            # "ensure" / "embed" in call order
        self.ensure_threads = []
        self.thread_names = set()
        self.texts_seen = []
        self._lock = threading.Lock()

    def _ensure(self):
        with self._lock:
            self.events.append("ensure")
            self.ensure_threads.append(threading.current_thread().name)
        if self._ensure_raises:
            raise RuntimeError("warm-up boom")

    def embed_texts(self, texts):
        with self._lock:
            self.events.append("embed")
            self.thread_names.add(threading.current_thread().name)
            self.texts_seen.extend(texts)
        if self._fail and any(self._fail in t for t in texts):
            raise RuntimeError("boom")
        return [[float(len(t) % 7)] * self.dim for t in texts]

    def embed_query(self, text):
        return [0.0] * self.dim


def _count(repo, sql, args):
    with repo._connect() as db:
        (n,) = db.execute(sql, args).fetchone()
    return n


# ------------------------------------------------------------- composition
def test_runtime_composes_source_embedding_service(repo):
    service = repo._runtime.source_embedding
    assert isinstance(service, SourceEmbeddingService)
    expected = {
        "embed_source",
        "embed_elements_batch",
        "embed_objects_batch",
        "embed_relations_batch",
        "embed_chunks_for_source",
        "embed_chunks_batch",
    }
    assert expected <= set(SourceEmbeddingService.__dict__)
    assert "__getattr__" not in SourceEmbeddingService.__dict__


# -------------------------------------------------- embedder seam + warm-up
def test_embed_source_warms_up_late_bound_embedder_once(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(repo, nb.id, 25)   # 3 batches (10,10,5)
    emb = _RecordingEmbedder(dim=8)
    _bind(repo, "source_element_embedding", emb)

    repo._embed_source(sid)
    assert ("embedding", "source_element_embedding") in repo.recording_model_provider.calls

    assert emb.events.count("ensure") == 1                 # warm-up exactly once
    assert emb.events[0] == "ensure"                       # ... and before compute
    assert not emb.ensure_threads[0].startswith("emb-")    # single-threaded warm-up
    assert _count(
        repo,
        "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?",
        (sid,),
    ) == 25


def test_embed_source_warmup_failure_is_swallowed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(repo, nb.id, 12)
    emb = _RecordingEmbedder(dim=8, ensure_raises=True)
    _bind(repo, "source_element_embedding", emb)

    repo._embed_source(sid)                                # must not raise

    assert _count(
        repo,
        "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?",
        (sid,),
    ) == 12


def test_embed_source_truncates_element_texts(tmp_path, monkeypatch):
    trunc_repo = _make_repo(tmp_path, monkeypatch, EMBED_TRUNCATE_CHARS="40")
    nb = trunc_repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(
        trunc_repo, nb.id, 3, text="x" * 500 + "-{i}"
    )
    emb = _RecordingEmbedder(dim=8)
    _bind(trunc_repo, "source_element_embedding", emb)

    trunc_repo._embed_source(sid)

    assert emb.texts_seen and all(len(t) <= 40 for t in emb.texts_seen)


# ------------------------------------------------ object flush facade seat
def test_embed_objects_batch_flush_rides_facade_seat(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _bind(repo, "knowledge_object_embedding", _RecordingEmbedder(dim=8))
    flushes = []
    real_flush = repo._runtime.source_embedding.flush_object_vectors

    def spy_flush(notebook_id, rows):
        flushes.append(len(rows))
        return real_flush(notebook_id, rows)

    monkeypatch.setattr(repo._runtime.source_embedding, "flush_object_vectors", spy_flush)
    items = [{"_oid": f"ko-{i}", "payload": {"name": f"concept number {i}"}}
             for i in range(35)]

    repo._embed_objects_batch(nb.id, items)
    assert ("embedding", "knowledge_object_embedding") in repo.recording_model_provider.calls

    assert sum(flushes) == 35                              # every vector flushed via the seat
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?",
        (nb.id,),
    ) == 35


def test_embed_objects_batch_flush_errors_propagate(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _bind(repo, "knowledge_object_embedding", _RecordingEmbedder(dim=8))

    def broken_flush(notebook_id, rows):
        raise RuntimeError("simulated flush interrupt")

    monkeypatch.setattr(repo._runtime.source_embedding, "flush_object_vectors", broken_flush)
    items = [{"_oid": f"ko-{i}", "payload": {"name": f"widget {i}"}} for i in range(5)]

    with pytest.raises(RuntimeError, match="simulated flush interrupt"):
        repo._embed_objects_batch(nb.id, items)


# ----------------------------------------------- relation batch isolation
def test_embed_relations_batch_isolates_failed_batches(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    emb = _RecordingEmbedder(dim=8, fail_substr="relation number 15")
    _bind(repo, "relation_embedding", emb)
    persisted = []
    monkeypatch.setattr(
        repo._runtime.embedding_store,
        "replace_relation_vectors",
        lambda notebook_id, rows, *, created_at: persisted.extend(
            rid for rid, _ in rows
        ),
    )
    items = [{"_rid": f"rel-{i}", "text": f"relation number {i}"} for i in range(30)]

    repo._embed_relations_batch(nb.id, items)
    assert ("embedding", "relation_embedding") in repo.recording_model_provider.calls

    # batch [10..19] contains the sentinel and is dropped; the rest reach the store
    assert len(persisted) == 20
    assert "rel-15" not in persisted and "rel-5" in persisted and "rel-25" in persisted
    assert any(name.startswith("emb-rel") for name in emb.thread_names)


# --------------------------------------------------- chunk batch isolation
def test_embed_chunks_for_source_isolates_failed_batches(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(
        repo, nb.id, 30, text="chunk body marker {i} " + "filler " * 80
    )
    repo._build_chunks_for_source(sid)
    total = _count(repo, "SELECT COUNT(*) FROM chunks WHERE source_id=?", (sid,))
    assert total >= 2

    with repo._connect() as db:
        victim = db.execute(
            "SELECT id, text FROM chunks WHERE source_id=? LIMIT 1", (sid,)
        ).fetchone()
    emb = _RecordingEmbedder(dim=8, fail_substr=victim["text"][:60])
    _bind(repo, "chunk_embedding", emb)

    repo._embed_chunks_for_source(sid)
    assert ("embedding", "chunk_embedding") in repo.recording_model_provider.calls

    embedded = _count(
        repo,
        "SELECT COUNT(*) FROM chunk_embeddings WHERE notebook_id=?",
        (nb.id,),
    )
    assert 0 < embedded < total                            # failed batch dropped, rest persisted
    assert any(name.startswith("emb-ck") for name in emb.thread_names)


# --------------------------------------------------------- unconfigured no-op
def test_unconfigured_embedder_paths_are_noops(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'n.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "sn"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    bare_repo = sqlite_repository.SQLiteRepository(
        Settings(model_services_config=""),
        model_provider=RecordingModelProvider(),
    )
    nb = bare_repo.create_notebook(NotebookCreate(name="nb"))

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("embedder must not be touched when unconfigured")

    bare_repo._embed_source("src-missing")                 # returns before get_source
    bare_repo._embed_objects_batch(nb.id, [{"_oid": "ko-1", "payload": {"name": "x"}}])
    bare_repo._embed_relations_batch(nb.id, [{"_rid": "rel-1", "text": "x"}])
    bare_repo._embed_chunks_batch(nb.id, [{"_oid": "ck-1", "payload": {"text": "x"}}])

    assert _count(
        bare_repo,
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?",
        (nb.id,),
    ) == 0


# ----------------------------------------------- OOM: page before mapping
# _map_embedding_batches collects a whole call's results into memory
# (list(pool.map(...))), so handing it EVERY batch at once holds every vector
# simultaneously — ~32GB at 8M objects / ~33GB at 8M relations. The batch
# embedders must page (commit_every-sized) and map ONE page at a time. These
# guards spy on _map_embedding_batches and assert no single call ever sees more
# than one page of batches, while every vector still lands. Reverting any of the
# three to a single all-batches map call fails here.

def _spy_map_batches(service, monkeypatch):
    calls = []
    real = service._map_embedding_batches

    def spy(fn, batches, *, task_prefix, workload_id):
        calls.append(len(batches))
        return real(fn, batches, task_prefix=task_prefix, workload_id=workload_id)

    monkeypatch.setattr(service, "_map_embedding_batches", spy)
    return calls


def _spy_invalidate(service, monkeypatch):
    invalidated = []
    monkeypatch.setattr(
        service, "invalidate_vector_matrix",
        lambda notebook_id, table: invalidated.append((notebook_id, table)),
    )
    return invalidated


def test_embed_source_pages_before_mapping(tmp_path, monkeypatch):
    """审计批4:``embed_source`` 是四个 embedder 里最后一个不分页的——它把整来源的
    batch 一次性喂给 ``_map_embedding_batches``,21k 元素的来源 ≈670MB 向量同时驻留,
    再乘上传并发。与三个 sibling 同款:页在**喂进去之前**切,每次 map 调用至多一页。

    变异锚点(两个,删除式与移动式各一):
      - 把分页循环改回 ``self._map_embedding_batches(_embed_only, batches, ...)`` 的一次性
        形态 → ``max(calls) <= 2`` 红;
      - 把 ``replace_element_vectors`` **移出**页循环(攒满再一次写)→ 映射仍是分页的、
        上面那条照旧绿,但每页落库那条(``len(flushes) >= 3``)红——向量还是全量驻留了。"""
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES="2")   # page = 2 batches
    nb = r.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(r, nb.id, 55)                  # 6 batches / page 2
    _bind(r, "source_element_embedding", _RecordingEmbedder(dim=8))
    calls = _spy_map_batches(r._runtime.source_embedding, monkeypatch)
    invalidated = _spy_invalidate(r._runtime.source_embedding, monkeypatch)
    flushes: list = []
    real_replace = r._runtime.embedding_store.replace_element_vectors

    def spy_replace(source_id, notebook_id, rows, *, created_at):
        flushes.append(len(rows))
        return real_replace(source_id, notebook_id, rows, created_at=created_at)

    monkeypatch.setattr(
        r._runtime.embedding_store, "replace_element_vectors", spy_replace
    )

    r._embed_source(sid)

    assert len(calls) >= 3                                            # paged, not one shot
    assert max(calls) <= 2                                            # each map call ≤ one page
    # 边算边写:每页落一次库,活着的向量被限制在一页之内(不是攒满整来源再写一次)。
    assert len(flushes) >= 3 and max(flushes) <= 2 * 10               # ≤ 一页(2 批 × 10)
    assert sum(flushes) == 55
    assert _count(
        r, "SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)
    ) == 55                                                          # every vector still lands
    assert (nb.id, "element_embeddings") in invalidated               # matrix cache dropped after paged re-embed


def _embed_source_run(tmp_path, monkeypatch, *, commit_batches, n=55):
    """跑一次 embed_source,回 (每次 embedder 调用收到的 texts, 落库的 (id, 向量字节))。"""
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES=commit_batches)
    nb = r.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(r, nb.id, n)
    per_call: list = []

    class _CallRecordingEmbedder(_RecordingEmbedder):
        def embed_texts(self, texts):
            per_call.append(list(texts))
            return super().embed_texts(texts)

    _bind(r, "source_element_embedding", _CallRecordingEmbedder(dim=8))
    r._embed_source(sid)
    with r._connect() as db:
        stored = [
            # source id 是随机的,只留 el-<sid>-<idx> 的序号部分,让两次跑法可比。
            (row["element_id"].rsplit("-", 1)[-1], bytes(row["vector"]))
            for row in db.execute(
                "SELECT element_id, vector FROM element_embeddings "
                "WHERE source_id=? ORDER BY element_id", (sid,)
            ).fetchall()
        ]
    return per_call, stored


def test_embed_source_paging_keeps_calls_and_vectors_bit_identical(tmp_path, monkeypatch):
    """差分钉:分页只改 I/O 形状。一页装得下全部批(=分页前的行为)与切成很多页两种跑法,
    **embedder 的调用序列(次数 + 每次的文本)** 与 **落库的向量字节** 必须逐位相同。

    页大小以「批」计,所以页边界永远落在 ``embed_batch_size`` 的批边界上——这是调用内容
    不变的机制性理由,不是巧合。

    调用**之间**的先后按 batch 内容排序后比较:两种跑法都把一页内的批并发交给线程池,
    到达顺序本来就由调度决定(分页前也是),不属于被钉的契约;被钉的是「哪些文本被切进
    了哪一次调用」。"""
    one_page, one_rows = _embed_source_run(
        tmp_path / "a", monkeypatch, commit_batches="1000"
    )
    many_pages, many_rows = _embed_source_run(
        tmp_path / "b", monkeypatch, commit_batches="1"
    )
    assert len(one_page) == 6 and len(many_pages) == 6      # 55 元素 / 每批 10 → 6 次调用,两种跑法一致
    assert sorted(one_page) == sorted(many_pages)            # 每次调用的文本切分逐位相同
    assert [eid for eid, _ in one_rows] == [eid for eid, _ in many_rows]
    assert [vec for _, vec in one_rows] == [vec for _, vec in many_rows]


def test_embed_objects_batch_pages_before_mapping(tmp_path, monkeypatch):
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES="2")   # page = 2 batches
    nb = r.create_notebook(NotebookCreate(name="nb"))
    _bind(r, "knowledge_object_embedding", _RecordingEmbedder(dim=8))
    calls = _spy_map_batches(r._runtime.source_embedding, monkeypatch)
    invalidated = _spy_invalidate(r._runtime.source_embedding, monkeypatch)

    items = [{"_oid": f"ko-{i}", "payload": {"name": f"concept number {i}"}}
             for i in range(55)]                                      # 6 batches / page 2
    r._embed_objects_batch(nb.id, items)

    assert len(calls) >= 3                                            # paged, not one shot
    assert max(calls) <= 2                                            # each map call ≤ one page
    assert _count(
        r, "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)
    ) == 55                                                          # every vector still lands
    assert (nb.id, "knowledge_embeddings") in invalidated            # matrix cache dropped after re-embed


def test_embed_relations_batch_pages_before_mapping(tmp_path, monkeypatch):
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES="2")
    nb = r.create_notebook(NotebookCreate(name="nb"))
    _bind(r, "relation_embedding", _RecordingEmbedder(dim=8))
    calls = _spy_map_batches(r._runtime.source_embedding, monkeypatch)
    invalidated = _spy_invalidate(r._runtime.source_embedding, monkeypatch)
    # record through the store seat (relation_embeddings FKs knowledge_relations;
    # the paging shape, not the FK, is what's under test — mirrors
    # test_embed_relations_batch_isolates_failed_batches)
    persisted: list = []
    monkeypatch.setattr(
        r._runtime.embedding_store, "replace_relation_vectors",
        lambda notebook_id, rows, *, created_at: persisted.extend(rid for rid, _ in rows),
    )

    items = [{"_rid": f"rel-{i}", "text": f"relation number {i}"} for i in range(55)]
    r._embed_relations_batch(nb.id, items)

    assert len(calls) >= 3                                            # paged, not one shot
    assert max(calls) <= 2                                            # each map call ≤ one page
    assert len(persisted) == 55                                      # every vector flushed across pages
    assert (nb.id, "relation_embeddings") in invalidated             # matrix cache dropped after re-embed
                                         # (clock-independent: same-second paged re-embed can't strand a partial)


def test_embed_chunks_batch_pages_before_mapping(tmp_path, monkeypatch):
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES="2")
    nb = r.create_notebook(NotebookCreate(name="nb"))
    _bind(r, "chunk_embedding", _RecordingEmbedder(dim=8))
    calls = _spy_map_batches(r._runtime.source_embedding, monkeypatch)
    invalidated = _spy_invalidate(r._runtime.source_embedding, monkeypatch)
    persisted: list = []
    monkeypatch.setattr(
        r._runtime.embedding_store, "replace_chunk_vectors",
        lambda notebook_id, rows, *, created_at: persisted.extend(cid for cid, _ in rows),
    )

    items = [{"_oid": f"ck-{i}", "payload": {"text": f"chunk number {i}"}}
             for i in range(55)]
    r._embed_chunks_batch(nb.id, items)

    assert len(calls) >= 3
    assert max(calls) <= 2
    assert len(persisted) == 55
    assert (nb.id, "chunk_embeddings") in invalidated                # matrix cache dropped after re-embed


def test_invalidate_vector_matrix_wiring_evicts_the_real_cache(repo):
    """End-to-end: the runtime-wired invalidate_vector_matrix actually evicts
    retrieval's brute-force _vector_matrix cache entry (the guards above only
    prove the embedders CALL it; this proves the wiring reaches the same
    VectorCache retrieval reads)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    cache = repo._runtime.retrieval_snapshots.vector_cache
    key = f"{nb.id}:matrix:knowledge_embeddings"
    cache.get(key, ("v0",), lambda: {"warm": True})
    assert cache.peek(key, ("v0",))                                  # warmed
    repo._runtime.source_embedding.invalidate_vector_matrix(
        nb.id, "knowledge_embeddings"
    )
    assert not cache.peek(key, ("v0",))                             # evicted by the wired callback


def test_paged_embed_evicts_matrix_even_when_a_page_flush_raises(tmp_path, monkeypatch):
    """Guard (codex PR#342 round 3 P2): if a LATER page's flush raises, the
    matrix cache must still be evicted — earlier pages already committed, and a
    same-second re-embed leaves (COUNT(*), MAX(created_at)) unchanged, so a
    stale matrix would otherwise survive the failure. Eviction runs in a
    finally; the exception still propagates. Dropping the finally fails here."""
    r = _make_repo(tmp_path, monkeypatch, EMBED_COMMIT_BATCHES="1")   # 1 batch/page → many pages
    nb = r.create_notebook(NotebookCreate(name="nb"))
    _bind(r, "knowledge_object_embedding", _RecordingEmbedder(dim=8))
    invalidated = _spy_invalidate(r._runtime.source_embedding, monkeypatch)

    flushes = {"n": 0}

    def flaky_flush(notebook_id, rows):
        flushes["n"] += 1
        if flushes["n"] == 2:                                        # blow up on the 2nd page
            raise RuntimeError("page 2 flush boom")

    monkeypatch.setattr(r._runtime.source_embedding, "flush_object_vectors", flaky_flush)

    items = [{"_oid": f"ko-{i}", "payload": {"name": f"c{i}"}} for i in range(30)]  # 3 pages
    with pytest.raises(RuntimeError, match="page 2 flush boom"):
        r._embed_objects_batch(nb.id, items)

    assert flushes["n"] == 2                                         # aborted mid-pagination
    assert (nb.id, "knowledge_embeddings") in invalidated           # ...yet the matrix was still evicted
