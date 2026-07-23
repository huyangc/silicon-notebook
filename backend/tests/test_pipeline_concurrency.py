import json
import threading
import time
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
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

    bind_chat_client(repo, "kg_extract", _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram", "ev": 0}],
        "edges": []})))
    emb = _ElementBlockingEmbedder()
    bind_all_embedding_clients(repo, emb)

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


def test_background_embed_thread_inherits_request_context(repo, tmp_path, monkeypatch):
    """后台元素向量线程必须经 copy_context() 继承 _REQUEST_USER —— per-user
    模型解析/日志隔离的命脉;Task 12 摄取编排搬进 SourceIngestionService 后
    该传播不得丢失。"""
    from app.core.request_context import (
        get_request_user, set_request_user, reset_request_user,
    )

    md = tmp_path / "ctx.md"
    md.write_text("# Doc\n\nContext propagation body.\n", encoding="utf-8")
    user = repo.create_user("a00123456", "pw123456")
    nb = repo.create_notebook(NotebookCreate(name="ctx"))
    sid = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
               file_name, file_path, file_size, file_hash, summary, doc_type, created_at, updated_at)
               VALUES (?,?,?, 'markdown','queued','queued', 'ctx.md', ?, 0, '', '', 'academic_paper', ?, ?)""",
            (sid, nb.id, "Doc", str(md), now, now))

    seen = []
    embedding_service = repo._runtime.source_embedding
    original_embed_source = embedding_service.embed_source

    def spy(source_id):
        # Runs ON the background embed thread: it must carry the copied
        # request context AND the embed-<sid> thread identity.
        current = get_request_user()
        seen.append(
            (threading.current_thread().name.startswith("embed-"),
             getattr(current, "id", None))
        )
        return original_embed_source(source_id)

    monkeypatch.setattr(embedding_service, "embed_source", spy)

    class _CtxEmbedder:
        def embed_texts(self, texts):
            return [[0.1] * 8 for _ in texts]

        def embed_query(self, text):
            return [0.0] * 8

        def _ensure(self):
            pass

    bind_all_embedding_clients(repo, _CtxEmbedder())
    token = set_request_user(user)
    try:
        repo.process_source(sid)
    finally:
        reset_request_user(token)
    assert seen == [(True, user.id)]
