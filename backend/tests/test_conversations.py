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


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "C1", "object_type": "concept",
        "payload": {"name": "Engram", "section_path": "1"}, "evidence": []}], [])
    return nb

def test_ask_creates_then_appends_conversation(repo):
    nb = _seed(repo)
    r1 = repo.ask(nb.id, AskRequest(question="what is engram"))
    assert r1.conversation_id                      # new conversation created
    r2 = repo.ask(nb.id, AskRequest(question="and its drawbacks?", conversation_id=r1.conversation_id))
    assert r2.conversation_id == r1.conversation_id  # appended, not new
    detail = repo.get_conversation(r1.conversation_id)
    assert detail.turn_count == 2 and len(detail.turns) == 2

def test_ask_feeds_prior_turns_into_prompt(repo, monkeypatch):
    nb = _seed(repo)
    captured = {}
    def cap(messages, schema_hint):
        captured["p"] = messages[0]["content"]
        return json.dumps({"answer": "ok.", "grounded": False})
    repo.llm_client.chat_json = cap
    r1 = repo.ask(nb.id, AskRequest(question="ZZTOPIC question"))
    repo.ask(nb.id, AskRequest(question="follow up", conversation_id=r1.conversation_id))
    assert "ZZTOPIC question" in captured["p"]      # prior turn present in 2nd prompt
