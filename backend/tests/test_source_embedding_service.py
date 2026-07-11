# backend/tests/test_source_embedding_service.py
"""Task 11: SourceEmbeddingService — element/object/relation/chunk vector
COMPUTE orchestration extracted off the facade.

Invariants under test:
- the embedder is a LATE-BOUND facade seam: ``repo.embedder = fake`` after
  construction is what every embed path calls;
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


def _make_repo(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")   # embedder_configured=True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBED_CONCURRENCY", "8")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return sqlite_repository.SQLiteRepository(Settings())


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
    repo.embedder = emb                                    # post-construction seam

    repo._embed_source(sid)

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
    repo.embedder = emb

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
    trunc_repo.embedder = emb

    trunc_repo._embed_source(sid)

    assert emb.texts_seen and all(len(t) <= 40 for t in emb.texts_seen)


# ------------------------------------------------ object flush facade seat
def test_embed_objects_batch_flush_rides_facade_seat(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.embedder = _RecordingEmbedder(dim=8)
    flushes = []
    real_flush = repo._runtime.source_embedding.flush_object_vectors

    def spy_flush(notebook_id, rows):
        flushes.append(len(rows))
        return real_flush(notebook_id, rows)

    monkeypatch.setattr(repo._runtime.source_embedding, "flush_object_vectors", spy_flush)
    items = [{"_oid": f"ko-{i}", "payload": {"name": f"concept number {i}"}}
             for i in range(35)]

    repo._embed_objects_batch(nb.id, items)

    assert sum(flushes) == 35                              # every vector flushed via the seat
    assert _count(
        repo,
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?",
        (nb.id,),
    ) == 35


def test_embed_objects_batch_flush_errors_propagate(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.embedder = _RecordingEmbedder(dim=8)

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
    repo.embedder = emb
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
    repo.embedder = emb

    repo._embed_chunks_for_source(sid)

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
    bare_repo = sqlite_repository.SQLiteRepository(Settings())
    nb = bare_repo.create_notebook(NotebookCreate(name="nb"))

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("embedder must not be touched when unconfigured")

    bare_repo.embedder = _Boom()

    bare_repo._embed_source("src-missing")                 # returns before get_source
    bare_repo._embed_objects_batch(nb.id, [{"_oid": "ko-1", "payload": {"name": "x"}}])
    bare_repo._embed_relations_batch(nb.id, [{"_rid": "rel-1", "text": "x"}])
    bare_repo._embed_chunks_batch(nb.id, [{"_oid": "ck-1", "payload": {"text": "x"}}])

    assert _count(
        bare_repo,
        "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?",
        (nb.id,),
    ) == 0
