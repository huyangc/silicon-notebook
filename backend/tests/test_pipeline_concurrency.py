import json
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
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    return SQLiteRepository(Settings())


class _FakeLLM:
    configured = True
    def __init__(self, payload):
        self._p = payload
    def chat_json(self, messages, response_schema_hint):
        return self._p
    def embed(self, text):
        return [0.0, 0.0]


class _ElementBlockingEmbedder:
    """Blocks ONLY the element-embedding worker threads (named 'emb-el*'), so KG-
    object embedding inside extraction proceeds while element embedding is held."""
    def __init__(self, dim=8):
        self.dim = dim
        self.entered = threading.Event()
        self.release = threading.Event()
    def embed_texts(self, texts):
        if threading.current_thread().name.startswith("emb-el"):
            self.entered.set()
            self.release.wait(15)
        return [[0.1] * self.dim for _ in texts]
    def embed_query(self, text):
        return [0.0] * self.dim
    def _ensure(self):
        pass


def test_extracted_set_before_element_embedding_finishes(repo, tmp_path):
    md = tmp_path / "doc.md"
    md.write_text("# Doc\n\nEngram is a memory architecture.\n", encoding="utf-8")
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
               VALUES (?,?,?, 'markdown','queued','queued', 'doc.md', ?, 0, '', '', 'academic_paper', ?, ?)""",
            (sid, nb.id, "Doc", str(md), now, now))

    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": []}))
    emb = _ElementBlockingEmbedder()
    repo.embedder = emb

    done = threading.Event()
    def run():
        try:
            repo.process_source(sid)
        finally:
            done.set()
    threading.Thread(target=run, daemon=True).start()

    assert emb.entered.wait(10), "background element-embedding never started"

    # While element embedding is held, parse_status must still reach 'extracted'.
    deadline = time.time() + 10
    reached = False
    while time.time() < deadline:
        if repo.get_source(sid).parse_status == "extracted":
            reached = True
            break
        time.sleep(0.05)
    assert not emb.release.is_set(), "precondition: embedding still blocked"
    assert reached, "status did not reach 'extracted' while element-embedding was blocked"

    emb.release.set()
    assert done.wait(15), "process_source did not finish after releasing embedder"
    assert repo.get_source(sid).parse_status == "extracted"
    with repo._connect() as db:
        (n,) = db.execute("SELECT COUNT(*) FROM element_embeddings WHERE source_id=?", (sid,)).fetchone()
    assert n >= 1, "element embeddings must be persisted once the pipeline completes"
