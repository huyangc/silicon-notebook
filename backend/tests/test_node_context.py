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

def test_element_texts_does_not_scan_entire_notebook(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["A target sentence.", "Another sentence."])

    executed = []
    original_connect = repo._connect

    class TrackingConnection:
        def __init__(self, inner):
            self.inner = inner
        def __enter__(self):
            self.conn = self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def execute(self, sql, params=()):
            executed.append(" ".join(sql.split()))
            return self.conn.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: TrackingConnection(original_connect()))
    with repo._connect() as db:
        texts, ordinal = repo._element_texts(db, [eids[0]])

    assert texts[eids[0]] == "A target sentence."
    assert ordinal == {}
    assert not any("ORDER BY se.created_at ASC, se.id ASC" in sql for sql in executed)


def test_concept_detail_includes_element_text(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["As shown, Engram is a conditional memory module."])
    ev = {"source_id": sid, "source_title": "Doc", "element_id": eids[0], "element_type": "paragraph", "location_label": "p", "quoted_span": "Engram", "confidence": 1.0}
    repo.store_kg(nb.id, sid, [{"local_id":"c","object_type":"concept","payload":{"name":"Engram","section_path":"1"},"evidence":[ev]}], [])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    d = repo.concept_detail(nb.id, cid)
    assert any("conditional memory module" in (e.get("element_text") or "") for e in d["evidence"])


def test_formula_evidence_metadata_survives_context_enrichment(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    formula = r"C _ {l} = 2 \sigma (\tilde {C} _ {l}).\tag{7}"
    sid, eids = _src_with_elements(repo, nb.id, [formula])
    with repo._connect() as db:
        db.execute(
            "UPDATE source_elements SET element_type='formula', location_label='eq. 7' WHERE id=?",
            (eids[0],),
        )

    evidence = {
        "source_id": sid,
        "source_title": "2606.19348v1.pdf",
        "element_id": eids[0],
        # Persisted metadata can be stale; SourceElement is authoritative.
        "element_type": "paragraph",
        "location_label": "old location",
        "quoted_span": formula,
        "confidence": 0.98,
    }
    repo.store_kg(
        nb.id,
        sid,
        [{
            "local_id": "f",
            "object_type": "formula",
            "payload": {"name": formula, "section_path": "2"},
            "evidence": [evidence],
        }],
        [],
    )
    with repo._connect() as db:
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["id"]

    occurrence = repo.node_context(nb.id, object_id)["occurrences"][0]
    assert occurrence == {
        **evidence,
        "element_type": "formula",
        "location_label": "eq. 7",
        "element_text": formula,
    }

    repo.rebuild_unified_kg(nb.id)
    canonical_id = repo.cluster_map(nb.id)[object_id]
    detail_evidence = repo.concept_detail(nb.id, canonical_id)["evidence"][0]
    assert detail_evidence["element_type"] == "formula"
    assert detail_evidence["element_text"] == formula
