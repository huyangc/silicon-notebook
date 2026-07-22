from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_embedding_client


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def test_chunk_response_carries_mode_and_round_trips(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    resp = repo.ask(nb.id, AskRequest(question="q", mode="chunk"))
    assert resp.mode == "chunk"
    detail = repo.get_conversation(resp.conversation_id)
    assert detail.turns[-1].response.mode == "chunk"   # 经 answers.payload JSON 回流
