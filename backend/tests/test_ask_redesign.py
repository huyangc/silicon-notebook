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
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
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

def test_parse_answer_anchors_keeps_only_cited(repo):
    # id_map: k1->ctx dict; only markers present in text become anchors
    id_map = {
        "k1": {"object_id": "o1", "object_type": "concept", "name": "Engram",
               "definition": "a memory module", "snippet": "Engram is ...",
               "source_title": "paper", "location_label": "2.1"},
        "k2": {"object_id": "o2", "object_type": "claim", "name": "unused",
               "definition": None, "snippet": None, "source_title": "", "location_label": ""},
    }
    anchors = repo._parse_answer_anchors("Engram is a module [k1]. Improving it is open.", id_map)
    keys = {a.key for a in anchors}
    assert keys == {"k1"}                    # k2 not cited -> excluded
    assert anchors[0].label == "Engram"

def test_ask_grounded_answer_has_anchors(repo):
    nb = _seed(repo)   # one concept "Engram"
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    assert resp.grounded is True
    assert resp.answer and "[k1]" in resp.answer
    assert any(a.object_type == "concept" for a in resp.anchors)
    assert resp.conclusion and "[k1]" not in resp.conclusion   # conclusion = markers stripped

def test_ask_ungrounded_when_no_hits(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    repo.llm_client.chat_json = lambda m, s: __import__("json").dumps(
        {"answer": "（推断）Engram is likely a memory mechanism.", "grounded": False})
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    assert resp.llm_mode == "ungrounded"
    assert "not yet contain approved knowledge" not in resp.conclusion   # no canned dead-end
    assert resp.answer

def test_concept_dedup_degrades_gracefully_without_clusters(repo):
    # No concept_clusters rows populated -> _concept_cluster_id returns object_id
    # (no dedup) and _answer_context must not crash; both concepts kept.
    nb = repo.create_notebook(NotebookCreate(name="nc"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "2"}, "evidence": []},
    ], [])
    assert repo.cluster_map(nb.id) == {}   # clustering not populated
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
            (nb.id,)).fetchall()
    oids = [r["id"] for r in rows]
    for oid in oids:
        assert repo._concept_cluster_id(nb.id, oid) == oid   # falls back to object_id
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    # no clusters -> no dedup -> both concept hits remain available as anchors targets
    assert resp.answer  # did not crash
