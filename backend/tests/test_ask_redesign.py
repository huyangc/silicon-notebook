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

def test_ask_global_topn_not_fixed_quota(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"M{i}", "object_type": "claim",
             "payload": {"name": f"engram claim number {i}", "section_path": "1"}, "evidence": []}
            for i in range(8)]
    repo.store_kg(nb.id, None, objs, [])
    resp = repo.ask(nb.id, AskRequest(question="engram claim", scenario={}))
    claim_hits = [r for r in resp.related_knowledge if r.object_type == "claim"]
    assert len(claim_hits) > 5   # old code capped claims at _TOP_PER_TYPE=5

def test_askresponse_has_answer_and_anchors():
    from app.models.schemas import AskResponse, AnswerAnchor
    a = AnswerAnchor(key="k1", object_id="o1", object_type="concept", label="Engram", name="Engram")
    r = AskResponse(conclusion="x", answer="Engram [k1].", grounded=True, anchors=[a])
    assert r.answer == "Engram [k1]." and r.grounded and r.anchors[0].key == "k1"
