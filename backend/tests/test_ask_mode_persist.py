from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest
from tests.model_testkit import bind_all_embedding_clients


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def test_chunk_response_carries_mode_and_round_trips(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    resp = repo.ask(nb.id, AskRequest(question="q", mode="chunk"))
    assert resp.mode == "chunk"
    detail = repo.get_conversation(resp.conversation_id)
    assert detail.turns[-1].response.mode == "chunk"   # 经 answers.payload JSON 回流


def test_browser_question_time_round_trips_without_becoming_answer_time(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    asked_at = "2026-07-28T09:05:12.345+08:00"

    resp = repo.ask(
        nb.id,
        AskRequest(question="q", mode="chunk", asked_at=asked_at),
    )
    turn = repo.get_conversation(resp.conversation_id).turns[-1]

    assert resp.asked_at == asked_at
    assert turn.asked_at == asked_at
    assert turn.created_at != asked_at
