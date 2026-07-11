# backend/tests/test_chunk_store_component.py
"""Task 10: ChunkStore component — chunks/chunks_fts row persistence extracted
off the facade's _build_chunks_for_source / _embed_chunks_for_source.

Row-level semantics under test (must stay identical to the former facade
bodies):
- replace_source_chunks swaps chunks AND their chunks_fts rows in ONE write
  transaction (a failed FTS insert rolls the chunk rows back too);
- old chunk rows cascade-delete their chunk_embeddings;
- source_elements_for_chunking keeps the zero-padded-id insertion order.
"""
import json

import pytest

from app.core.config import Settings
from app.repositories.sqlite.chunk_store import ChunkStore, ChunkWrite
from app.services import sqlite_repository


NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return sqlite_repository.SQLiteRepository(Settings())


@pytest.fixture
def chunk_store(repo):
    return repo._runtime.chunk_store


@pytest.fixture
def notebook_id(repo):
    from app.models.schemas import NotebookCreate

    return repo.create_notebook(NotebookCreate(name="nb")).id


@pytest.fixture
def source_id(repo, chunk_store, notebook_id):
    """A parsed source with elements plus one already-persisted chunk (with an
    embedding row) so replacement/rollback have prior state to preserve."""
    sid = "src-ck"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, notebook_id, "S", "markdown", "s.md", "/tmp/s.md",
             0, "h", "", "", "parsed", NOW, NOW),
        )
        for i in (2, 1):  # inserted out of order on purpose (ordering test)
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", f"text {i}", "{}", NOW),
            )
    chunk_store.replace_source_chunks(
        sid,
        notebook_id,
        [ChunkWrite("ck-old", "old chunk text", "S1", (f"el-{sid}-0001",))],
        created_at=NOW,
    )
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)",
            ("ck-old", notebook_id, "[1.0]", NOW),
        )
    return sid


def test_chunk_store_owns_chunk_persistence(repo):
    expected = {
        "source_elements_for_chunking",
        "replace_source_chunks",
        "source_chunks",
        "_insert_fts_rows",
    }
    assert expected <= ChunkStore.__dict__.keys()
    assert "__getattr__" not in ChunkStore.__dict__
    assert repo._runtime.chunk_store.database is repo._runtime.database


def test_replace_source_chunks_rolls_back_chunks_and_fts_together(
    chunk_store, source_id, notebook_id, monkeypatch
):
    before = chunk_store.source_chunks(source_id)
    assert before  # fixture seeded one chunk
    monkeypatch.setattr(
        chunk_store,
        "_insert_fts_rows",
        lambda connection, rows: (_ for _ in ()).throw(RuntimeError("fts")),
    )
    with pytest.raises(RuntimeError, match="fts"):
        chunk_store.replace_source_chunks(
            source_id,
            notebook_id,
            [ChunkWrite("c-new", "x", "", ())],
            created_at="t",
        )
    assert chunk_store.source_chunks(source_id) == before


def test_replace_source_chunks_swaps_rows_fts_and_cascades_embeddings(
    repo, chunk_store, source_id, notebook_id
):
    chunk_store.replace_source_chunks(
        source_id,
        notebook_id,
        [
            ChunkWrite("ck-new-1", "new one", "S1", (f"el-{source_id}-0001",)),
            ChunkWrite("ck-new-2", "new two", "S1 > sub", (f"el-{source_id}-0002",)),
        ],
        created_at="2026-01-02T00:00:00",
    )
    with repo._connect() as db:
        chunks = db.execute(
            "SELECT id, section_path, element_ids, created_at FROM chunks "
            "WHERE source_id=? ORDER BY id", (source_id,),
        ).fetchall()
        fts_ids = {
            r["chunk_id"]
            for r in db.execute(
                "SELECT chunk_id FROM chunks_fts WHERE notebook_id=?", (notebook_id,)
            ).fetchall()
        }
        emb = db.execute(
            "SELECT COUNT(*) c FROM chunk_embeddings WHERE chunk_id='ck-old'"
        ).fetchone()["c"]
    assert [r["id"] for r in chunks] == ["ck-new-1", "ck-new-2"]
    assert json.loads(chunks[0]["element_ids"]) == [f"el-{source_id}-0001"]
    assert chunks[0]["created_at"] == "2026-01-02T00:00:00"
    assert fts_ids == {"ck-new-1", "ck-new-2"}   # old FTS row gone, new in sync
    assert emb == 0                              # cascade wiped the old vector


def test_source_elements_for_chunking_orders_by_zero_padded_id(chunk_store, source_id):
    elements = chunk_store.source_elements_for_chunking(source_id)
    assert [e["id"] for e in elements] == [
        f"el-{source_id}-0001",
        f"el-{source_id}-0002",
    ]  # ORDER BY id == insertion order (zero-padded ids)
    assert elements[0] == {
        "id": f"el-{source_id}-0001",
        "element_type": "paragraph",
        "text": "text 1",
    }


def test_source_chunks_returns_id_and_text(chunk_store, source_id):
    rows = chunk_store.source_chunks(source_id)
    assert rows == [{"id": "ck-old", "text": "old chunk text"}]
