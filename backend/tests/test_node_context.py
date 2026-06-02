import json, pytest, datetime
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.models.schemas import NotebookCreate

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

def _src_with_elements(repo, nb, texts):
    from uuid import uuid4
    sid = f"src-{uuid4().hex[:8]}"; now = datetime.datetime.now().isoformat()
    ids = []
    with repo._connect() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) VALUES (?,?,?,'markdown','extracted','parsed','d.md','',0,'','','academic_paper',?,?)", (sid, nb, "Doc", now, now))
        for i, t in enumerate(texts):
            eid = f"el-{uuid4().hex[:8]}"; ids.append(eid)
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) VALUES (?,?,?,?,?, '{}', ?)", (eid, sid, "paragraph", f"p{i}", t, f"{now}-{i:03d}"))
    return sid, ids

def test_node_context_concept_sentence_and_definition(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "As shown in Figure 1, Engram is a conditional memory module.",
        "Engram is defined as a structured store separating memory.",
    ])
    def ev(eid, span): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": span, "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev(eids[0],"Engram")]},
        {"local_id":"k","object_type":"claim","payload":{"name":"Engram is a structured store","section_path":"1"},"evidence":[ev(eids[1],"Engram is defined as")]},
    ], [{"source_local_id":"k","target_local_id":"c","edge_type":"defines","evidence":[]}])
    with repo._connect() as db:
        cid = next(r["id"] for r in db.execute("SELECT id,object_type FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchall() if r["object_type"]=="concept")
    ctx = repo.node_context(nb.id, cid)
    assert ctx["object_type"] == "concept"
    assert "conditional memory module" in ctx["occurrences"][0]["element_text"]
    assert "structured store" in (ctx["definition"] or "")

def test_node_context_procedure_steps_doc_order(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, [
        "First, we extract and compress suffix N-grams.",
        "Subsequently, embeddings are modulated by the hidden state.",
        "Finally, the result is refined via a lightweight convolution.",
    ])
    def ev(eid): return {"source_id": sid, "source_title": "Doc", "element_id": eid, "element_type": "paragraph", "location_label": "p", "quoted_span": "x", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [
        {"local_id":"p2","object_type":"procedure","payload":{"name":"modulate","section_path":"2.2"},"evidence":[ev(eids[1])]},
        {"local_id":"p1","object_type":"procedure","payload":{"name":"extract","section_path":"2.2"},"evidence":[ev(eids[0])]},
        {"local_id":"p3","object_type":"procedure","payload":{"name":"refine","section_path":"2.2"},"evidence":[ev(eids[2])]},
    ], [])
    with repo._connect() as db:
        pid = next(r["id"] for r in db.execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND json_extract(payload,'$.name')='extract'", (nb.id,)).fetchall())
    ctx = repo.node_context(nb.id, pid)
    names = [s["name"] for s in ctx["steps"]]
    assert names == ["extract", "modulate", "refine"]
    assert "suffix N-grams" in ctx["steps"][0]["element_text"]
