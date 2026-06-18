import json as _j
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("CHUNK_KG_OVERLAY_ENABLED", "true")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); return r


def _seed_chunks_and_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"quoted_span": "x", "element_id": "el-x-1", "source_id": "s",
                        "source_title": "D", "element_type": "paragraph",
                        "location_label": "1", "confidence": 1.0}]}], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("s", nb.id, "D", "markdown", "extracted", "parsed",
             "D", "/d", 0, "", "", "textbook", _now(), _now()))
        for cid, els in [("ck-vec", ["el-y-1"]), ("ck-kg", ["el-x-1"])]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s", "cascode " + cid, "1", _j.dumps(els), _now()))
    return nb


def test_mix_retrieve_merges_vector_and_kg_source_chunks(repo):
    nb = _seed_chunks_and_kg(repo)
    cand, block, id_map, kg_hits = repo._mix_retrieve(nb.id, "cascode", "", ["cascode"])
    ids = {c.chunk_id for c in cand}
    assert "ck-kg" in ids                       # KG 源 chunk 进了候选池
    assert isinstance(block, str) and isinstance(id_map, dict)
    # KG key 用高 base(≥1001),不与 chunk key 撞
    assert all(int(k[1:]) >= 1001 for k in id_map) if id_map else True
