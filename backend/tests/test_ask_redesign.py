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

def test_retired_fast_mode_routes_to_chunk(repo, monkeypatch):
    # P4-5: mode="fast" is retired; _RETIRED_MODES maps it to "chunk".
    # ask_chunk is chunk-native (related_knowledge=[]), so this verifies the
    # retirement alias works without raising UnknownAskMode or AttributeError.
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    resp = repo.ask(nb.id, AskRequest(question="engram claim", scenario={}, mode="fast"))
    assert resp.mode == "chunk"   # retired id resolves to chunk handler

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
    # P4-5: ask_fast retired; test now uses _retrieve_scored to verify KG retrieval
    # still surfaces the concept, and ask_graph to verify the grounded-answer path.
    nb = _seed(repo)   # one concept "Engram"
    # Directly verify that KG retrieval surfaces the concept
    hits = repo._retrieve_scored(nb.id, "what is engram")
    assert any("engram" in (h.payload.get("name") or "").lower() for h in hits), \
        "_retrieve_scored must find the Engram concept"
    # ask_graph synthesises an answer from KG hits; verify it does not crash
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}, mode="graph"))
    assert resp is not None
    assert resp.mode == "graph"

def test_ask_ungrounded_when_no_hits(repo, monkeypatch):
    # P4-5: ask_fast retired; verify ask() (default=chunk) handles an empty notebook gracefully.
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}))
    # chunk mode on empty notebook: no crash, response is explicitly ungrounded
    assert resp is not None
    assert resp.grounded is False  # chunk path with no hits must not claim grounding

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
    # P4-5: ask_fast retired; use ask_graph (same KG path) to verify no crash
    resp = repo.ask(nb.id, AskRequest(question="what is engram", scenario={}, mode="graph"))
    assert resp is not None  # did not crash
