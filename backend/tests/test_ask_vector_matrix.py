"""ask() 走 vector_index 的 float32 矩阵路径(低内存),而非把所有向量
materialize 成 Python list。验证矩阵构建 + 端到端 ask 仍正确。"""
import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")   # embedder_configured == True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


class _FakeLLM:
    configured = True
    def chat_json(self, messages, response_schema_hint):
        return "{}"
    def embed(self, text):
        return [0.0] * 16


def test_gather_elements_without_vectors_skips_load(repo):
    # with_vectors=False must not populate the 'vector' field (avoids json.loads)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._connect() as db:
        els = repo._gather_elements(db, nb.id, with_vectors=False)
    assert all(e["vector"] is None for e in els)


def test_vector_matrix_builds_from_embeddings(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "MOSFET"}}])
    with repo._connect() as db:
        ids, mat = repo._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
    assert ids == [oid]
    assert mat.shape == (1, 16)


def test_ask_matrix_path_returns_matching_object(repo):
    repo.llm_client = _FakeLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "Engram improves perplexity"}}])
    resp = repo.ask(nb.id, AskRequest(question="does engram improve perplexity"))
    assert any("Engram" in r.headline for r in resp.related_knowledge)


def test_ask_does_not_backfill_missing_knowledge_embeddings(repo, monkeypatch):
    repo.llm_client = _FakeLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._test_insert_object(nb.id, "claim", {"name": "Channel loss depends on equalization"})

    def fail_backfill(*args, **kwargs):
        raise AssertionError("ask() must not synchronously backfill knowledge embeddings")

    monkeypatch.setattr(repo, "_backfill_knowledge_embeddings", fail_backfill)
    resp = repo.ask(nb.id, AskRequest(question="channel loss equalization"))
    assert resp.conversation_id
    assert resp.answer_id


def test_ask_does_not_load_all_source_elements_for_citation_validation(repo, monkeypatch):
    repo.llm_client = _FakeLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "claim", {"name": "Finite cable bandwidth attenuates high frequencies"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "Finite cable bandwidth attenuates high frequencies"}}])

    original = repo._gather_elements

    def guard(db, notebook_id, with_vectors=True):
        if with_vectors is False:
            raise AssertionError("ask() must not gather every element only to build a valid id set")
        return original(db, notebook_id, with_vectors=with_vectors)

    monkeypatch.setattr(repo, "_gather_elements", guard)
    resp = repo.ask(nb.id, AskRequest(question="why does cable bandwidth matter"))
    assert any("bandwidth" in r.headline.lower() for r in resp.related_knowledge)
