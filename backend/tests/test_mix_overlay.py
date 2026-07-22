import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings()); bind_embedding_client(r, FakeEmbedder(dim=16)); return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"source_id": "src-x", "source_title": "test", "element_id": "el-x-0001",
                        "element_type": "paragraph", "location_label": "1", "quoted_span": "cascode",
                        "confidence": 1.0}]},
         {"local_id": "b", "object_type": "claim", "payload": {"name": "Cascode raises output resistance"}, "evidence": []}],
        [{"source_local_id": "b", "target_local_id": "a", "edge_type": "about", "evidence": []}])
    return nb


def test_overlay_block_idmap_hits(repo):
    nb = _seed(repo)
    block, id_map, hits = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)
    assert isinstance(block, str) and id_map
    assert any(int(k[1:]) >= 6 for k in id_map)          # id_offset=5 → key 从 k6
    assert all(hasattr(h, "relevance") for h in hits)


def test_overlay_empty_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="e"))
    assert repo._chunk_kg_overlay(nb.id, "x", "", 0) == ("", {}, [])


def test_kg_source_chunks_maps_evidence_to_chunk(repo):
    import json as _j
    from app.services.sqlite_repository import _now
    nb = _seed(repo)   # concept "Cascode" with evidence element_id el-x-0001
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-x", nb.id, "test-src", "markdown", "extracted", "parsed",
             "test.md", "test.md", 0, "", "", "textbook", _now(), _now()))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("ck-mix1", nb.id, "src-x", "cascode raises Rout via stacking",
                    "1", _j.dumps(["el-x-0001"]), _now()))
    with repo._connect() as db:
        oid = db.execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
                         (nb.id,)).fetchone()["id"]
    chunks = repo._kg_source_chunks(nb.id, [oid])
    assert any(c.chunk_id == "ck-mix1" for c in chunks)


def test_kg_source_chunks_empty_inputs(repo):
    nb = repo.create_notebook(__import__("app.models.schemas", fromlist=["NotebookCreate"]).NotebookCreate(name="e"))
    assert repo._kg_source_chunks(nb.id, []) == []
