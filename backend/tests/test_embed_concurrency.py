import threading
import time
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")   # embedder_configured == True
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBED_CONCURRENCY", "8")
    return SQLiteRepository(Settings())


def _insert_source_with_elements(repo, notebook_id, n):
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
               VALUES (?,?,?, 'markdown','parsed','parsed', ?, '', 0, '', '', '', ?, ?)""",
            (sid, notebook_id, "Doc", "doc.md", now, now))
        for i in range(n):
            db.execute(
                """INSERT INTO source_elements (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, 'paragraph', ?, ?, '{}', ?)""",
                (f"el-{i:04d}-{sid}", sid, f"p{i}", f"element text number {i}", now))
    return sid


class _ConcEmbedder:
    """Records max concurrent embed_texts calls; can fail batches containing a sentinel."""
    def __init__(self, dim=8, fail_substr=None):
        self.dim = dim
        self._fail = fail_substr
        self._cur = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def embed_texts(self, texts):
        with self._lock:
            self._cur += 1
            self.max_concurrent = max(self.max_concurrent, self._cur)
        try:
            time.sleep(0.05)
            if self._fail and any(self._fail in t for t in texts):
                raise RuntimeError("boom")
            return [[float(len(t) % 7)] * self.dim for t in texts]
        finally:
            with self._lock:
                self._cur -= 1

    def embed_query(self, text):
        return [0.0] * self.dim


def test_embed_source_is_concurrent_and_persists_all(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(repo, nb.id, 35)   # 4 batches (10,10,10,5)
    emb = _ConcEmbedder(dim=8)
    repo.embedder = emb
    repo._embed_source(sid)
    with repo._connect() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)).fetchone()
    assert n == 35                    # every element persisted
    assert emb.max_concurrent >= 2    # batches ran concurrently (not serial)


def test_embed_source_failed_batch_isolated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = _insert_source_with_elements(repo, nb.id, 30)   # batches [0-9],[10-19],[20-29]
    emb = _ConcEmbedder(dim=8, fail_substr="number 15")    # only element 15 -> kills batch [10-19]
    repo.embedder = emb
    repo._embed_source(sid)
    with repo._connect() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)).fetchone()
    assert n == 20                    # the failed batch's 10 dropped; other 20 persisted
