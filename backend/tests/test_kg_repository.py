import json
import pytest
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now
from app.core.config import Settings
from uuid import uuid4


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def test_store_kg_writes_objects_and_relations(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "Engram", "confidence": 1.0}]},
        {"local_id": "K1", "object_type": "claim",
         "payload": {"name": "Engram improves perplexity", "section_path": "Abstract"},
         "evidence": [{"source_id": "s1", "source_title": "Doc", "element_id": "e1",
                       "element_type": "paragraph", "location_label": "p1",
                       "quoted_span": "improves perplexity", "confidence": 1.0}]},
    ]
    relations = [{"source_local_id": "K1", "target_local_id": "C1",
                  "edge_type": "about", "evidence": [{"quote": "Engram improves perplexity"}]}]
    n_obj, n_rel = repo.store_kg(nb.id, None, objects, relations)
    assert (n_obj, n_rel) == (2, 1)
    # raw object rows
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, object_type, status, payload FROM knowledge_objects WHERE notebook_id=? ORDER BY object_type",
            (nb.id,)).fetchall()
    assert [r["object_type"] for r in rows] == ["claim", "concept"]
    assert all(r["status"] == "approved" for r in rows)
    ids = {r["id"] for r in rows}
    # relation endpoints are real knowledge_object ids, not the local ids
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1 and rels[0]["edge_type"] == "about"
    assert rels[0]["source_object_id"] in ids and rels[0]["target_object_id"] in ids


def test_store_kg_skips_unresolved_relations(repo):
    """Relations that reference a local_id not present in the objects list are
    silently skipped: they are excluded from the returned count and from
    relations_for_notebook."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objects = [
        {"local_id": "A1", "object_type": "concept",
         "payload": {"name": "Alpha", "section_path": "S1"},
         "evidence": []},
    ]
    relations = [
        # Valid: both ends exist in objects.
        # There is only one object so no valid self-referential edge either —
        # use two objects to ensure at least one valid rel in a different test.
        # Here: target "MISSING" is not in objects → must be skipped.
        {"source_local_id": "A1", "target_local_id": "MISSING",
         "edge_type": "related", "evidence": []},
    ]
    n_obj, n_rel = repo.store_kg(nb.id, None, objects, relations)
    assert n_obj == 1
    assert n_rel == 0                                  # skipped relation not counted
    rels = repo.relations_for_notebook(nb.id)
    assert rels == []                                  # nothing written to DB


def test_add_and_read_relations(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    a = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    b = repo._test_insert_object(nb.id, "claim", {"name": "MOSFET has threshold voltage"})
    repo.add_relations(nb.id, None, [
        {"source_object_id": b, "target_object_id": a, "edge_type": "about",
         "evidence": [{"quote": "threshold voltage of the MOSFET"}]},
    ])
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 1
    assert rels[0]["source_object_id"] == b
    assert rels[0]["target_object_id"] == a
    assert rels[0]["edge_type"] == "about"
    assert rels[0]["evidence"] == [{"quote": "threshold voltage of the MOSFET"}]


# ---------------------------------------------------------------------------
# Helpers + KG extraction path tests
# ---------------------------------------------------------------------------

def _test_insert_source(repo, notebook_id, title, file_name, doc_type, text):
    """Insert a minimal source row + one source_elements row. Returns SourceDetail."""
    source_id = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed',
                       ?, '', 0, '', '', ?, ?, ?)""",
            (source_id, notebook_id, title, file_name, doc_type, now, now),
        )
        elem_id = f"el-{uuid4().hex[:10]}"
        db.execute(
            """INSERT INTO source_elements
               (id, source_id, element_type, location_label, text, metadata, created_at)
               VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)""",
            (elem_id, source_id, text, now),
        )
    return repo.get_source(source_id)


class _FakeLLM:
    configured = True

    def __init__(self, payload):
        self._p = payload

    def chat_json(self, prompt, retries=4):
        return self._p

    def embed(self, text):
        return [0.0, 0.0]


def test_run_extraction_kg_path(repo):
    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []}))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    repo._run_extraction(src.id)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT object_type, payload, status FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,)).fetchall()
    assert any(
        r["object_type"] == "concept"
        and r["status"] == "approved"
        and json.loads(r["payload"])["name"] == "Engram"
        for r in rows
    )


def test_reextraction_is_idempotent(repo):
    repo.llm_client = _FakeLLM(json.dumps({
        "nodes": [{"local_id": "a", "type": "Concept", "name": "Engram",
                   "evidence": "Engram is a memory architecture"}],
        "edges": []}))
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    src = _test_insert_source(repo, nb.id, "Doc", "doc.md", "academic_paper",
                              "Engram is a memory architecture.")
    sid = src.id
    repo._run_extraction(sid)
    repo._run_extraction(sid)
    with repo._connect() as db:
        (count,) = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?",
            (nb.id,)).fetchone()
    assert count == 1   # not doubled


def test_knowledge_graph_from_kg_tables(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    c = repo._test_insert_object(nb.id, "concept", {"name": "Engram"})
    k = repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    repo.add_relations(nb.id, None, [{"source_object_id": k, "target_object_id": c,
                                      "edge_type": "about", "evidence": []}])
    g = repo.knowledge_graph(nb.id)
    assert {n.object_type for n in g.nodes} == {"concept", "claim"}
    assert any(n.headline == "Engram" for n in g.nodes)
    assert len(g.edges) == 1
    e = g.edges[0]
    assert e.from_id == k and e.to_id == c and e.relation == "about"
