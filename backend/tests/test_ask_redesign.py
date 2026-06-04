import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest

class FakeLLM:
    configured = True
    def __init__(self): self.last_prompt = None
    def chat_json(self, messages, schema_hint):
        self.last_prompt = messages[0]["content"]
        return json.dumps({"answer": "Engram is a memory module [k1].", "grounded": True})

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    r.llm_client = FakeLLM()
    return r

def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"}, "evidence": []},
    ], [])
    return nb

def test_ask_query_excludes_scenario(repo):
    nb = _seed(repo)
    repo.ask(nb.id, AskRequest(question="what is engram", scenario={"domain": "ZZZUNIQUE"}))
    # scenario value must NOT leak into the retrieval/answer prompt
    assert "ZZZUNIQUE" not in (repo.llm_client.last_prompt or "")
