import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest

class FakeLLM:
    configured = True
    def chat_json(self, messages, schema_hint):
        return json.dumps({"answer": "ok.", "grounded": False})

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope"); monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); r.llm_client = FakeLLM()
    return r

def test_schema_has_conversations_and_fk(repo):
    with repo._connect() as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(answers)").fetchall()}
        assert "conversation_id" in cols
        tbls = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "conversations" in tbls


def test_conversation_schemas_exist():
    from app.models.schemas import AskRequest, AskResponse, ConversationSummary, ConversationDetail
    req = AskRequest(question="q", conversation_id="c1")
    assert req.conversation_id == "c1"
    resp = AskResponse(conclusion="x", conversation_id="c1")
    assert resp.conversation_id == "c1"
    s = ConversationSummary(id="c1", notebook_id="n", title="t", updated_at="now", turn_count=2)
    assert s.turn_count == 2
