import threading
import time

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    monkeypatch.setenv("EMBED_CONCURRENCY", "8")
    return SQLiteRepository(Settings())


class _ConcEmbedder:
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


def test_embed_objects_batch_concurrent_and_persists(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    items = []
    for i in range(35):
        oid = repo._test_insert_object(nb.id, "concept", {"name": f"Concept number {i}"})
        items.append({"_oid": oid, "payload": {"name": f"Concept number {i}"}})
    emb = _ConcEmbedder(dim=8)
    repo.embedder = emb
    repo._embed_objects_batch(nb.id, items)
    with repo._connect() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()
    assert n == 35
    assert emb.max_concurrent >= 2


def test_embed_objects_batch_failed_batch_isolated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    items = []
    for i in range(30):
        oid = repo._test_insert_object(nb.id, "concept", {"name": f"Concept number {i}"})
        items.append({"_oid": oid, "payload": {"name": f"Concept number {i}"}})
    emb = _ConcEmbedder(dim=8, fail_substr="number 15")
    repo.embedder = emb
    repo._embed_objects_batch(nb.id, items)
    with repo._connect() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()
    assert n == 20
