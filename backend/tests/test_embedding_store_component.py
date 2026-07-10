# backend/tests/test_embedding_store_component.py
"""Task 10: EmbeddingStore component — the four vector tables' persistence
(element/knowledge/relation/chunk) extracted off the facade.

Invariants under test:
- every replace_* flush is ONE write transaction on the facade's ``_write``
  seat (late-bound: per-instance monkeypatches keep observing every flush —
  the same seat test_sqlite_write_optimization pins for _embed_objects_batch);
- rows are encoded with encode_vector (float32 BLOB) and overwrite legacy
  JSON-TEXT / bytes / memoryview rows in place (INSERT OR REPLACE);
- empty row batches never open a transaction.
"""
import contextlib
import json

import numpy as np
import pytest

from app.core.config import Settings
from app.repositories.sqlite.embedding_store import EmbeddingStore
from app.services import sqlite_repository
from app.services.vector_index import decode_vector, encode_vector


NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return sqlite_repository.SQLiteRepository(Settings())


@pytest.fixture
def store(repo):
    return repo._runtime.embedding_store


@pytest.fixture
def notebook_id(repo):
    from app.models.schemas import NotebookCreate

    return repo.create_notebook(NotebookCreate(name="nb")).id


def _counting_write(repo, monkeypatch):
    counts = {"n": 0}
    real_write = repo._write

    @contextlib.contextmanager
    def counting():
        counts["n"] += 1
        with real_write() as db:
            yield db

    monkeypatch.setattr(repo, "_write", counting)
    return counts


def test_embedding_store_owns_vector_persistence(repo):
    expected = {
        "replace_element_vectors",
        "replace_knowledge_vectors",
        "replace_relation_vectors",
        "replace_chunk_vectors",
    }
    assert expected <= EmbeddingStore.__dict__.keys()
    assert "__getattr__" not in EmbeddingStore.__dict__
    assert repo._runtime.embedding_store is not None


def test_replace_knowledge_vectors_overwrites_mixed_format_rows(repo, store, notebook_id):
    # Seed all three historical on-disk formats: legacy JSON TEXT, raw bytes
    # BLOB, and memoryview BLOB (what sqlite3 hands back when copying rows).
    seeded = {
        "ko-json": json.dumps([1.0, 0.0]),
        "ko-bytes": encode_vector([0.0, 1.0]),
        "ko-mv": memoryview(encode_vector([0.5, 0.5])),
    }
    with repo._write() as db:
        db.executemany(
            "INSERT INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
            "VALUES (?,?,?,?)",
            [(oid, notebook_id, raw, NOW) for oid, raw in seeded.items()],
        )
    store.replace_knowledge_vectors(
        notebook_id,
        [("ko-json", [0.25, 0.75]), ("ko-mv", [0.75, 0.25]), ("ko-new", [1.0, 1.0])],
        created_at="2026-01-02T00:00:00",
    )
    with repo._connect() as db:
        rows = {
            r["object_id"]: r["vector"]
            for r in db.execute(
                "SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
        }
    assert set(rows) == {"ko-json", "ko-bytes", "ko-mv", "ko-new"}
    # Replaced rows are float32 BLOBs now; untouched legacy rows still decode.
    assert isinstance(rows["ko-json"], bytes)
    np.testing.assert_allclose(decode_vector(rows["ko-json"]), [0.25, 0.75])
    np.testing.assert_allclose(decode_vector(rows["ko-mv"]), [0.75, 0.25])
    np.testing.assert_allclose(decode_vector(rows["ko-bytes"]), [0.0, 1.0])
    np.testing.assert_allclose(decode_vector(rows["ko-new"]), [1.0, 1.0])


def test_flushes_use_one_write_transaction_on_the_facade_seat(repo, store, notebook_id, monkeypatch):
    counts = _counting_write(repo, monkeypatch)
    store.replace_knowledge_vectors(
        notebook_id,
        [(f"ko-{i}", [float(i), 1.0]) for i in range(25)],
        created_at=NOW,
    )
    assert counts["n"] == 1                 # one flush == one transaction
    store.replace_knowledge_vectors(notebook_id, [], created_at=NOW)
    assert counts["n"] == 1                 # empty batch never opens one


def test_write_seat_late_binding_observes_post_construction_patch(repo, store, notebook_id, monkeypatch):
    """The store must resolve the facade ``_write`` at CALL time (compatibility
    seat), not capture it at construction — mirror of the Task 9 proofs."""

    class Boom(RuntimeError):
        pass

    @contextlib.contextmanager
    def failing_write():
        raise Boom("late-bound")
        yield  # pragma: no cover

    monkeypatch.setattr(repo, "_write", failing_write)
    with pytest.raises(Boom):
        store.replace_knowledge_vectors(notebook_id, [("ko-x", [1.0])], created_at=NOW)


def test_replace_element_relation_and_chunk_vectors_roundtrip(repo, store, notebook_id):
    # Real parents so the FK-bearing tables accept rows.
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("src-emb", notebook_id, "S", "markdown", "s.md", "/tmp/s.md",
             0, "h", "", "", "parsed", NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            ("el-src-emb-0001", "src-emb", "paragraph", "p1", "t", "{}", NOW),
        )
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
            ("ck-emb-1", notebook_id, "src-emb", "t", "", "[]", NOW),
        )
    repo.store_kg(
        notebook_id,
        None,
        [
            {"local_id": "a", "object_type": "concept", "payload": {"name": "A"}, "evidence": []},
            {"local_id": "b", "object_type": "concept", "payload": {"name": "B"}, "evidence": []},
        ],
        [{"source_local_id": "a", "target_local_id": "b", "edge_type": "about", "evidence": []}],
    )
    with repo._connect() as db:
        rel_id = db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=?", (notebook_id,)
        ).fetchone()["id"]

    store.replace_element_vectors(
        "src-emb", notebook_id, [("el-src-emb-0001", [0.1, 0.9])], created_at=NOW
    )
    store.replace_relation_vectors(notebook_id, [(rel_id, [0.2, 0.8])], created_at=NOW)
    store.replace_chunk_vectors(notebook_id, [("ck-emb-1", [0.3, 0.7])], created_at=NOW)

    with repo._connect() as db:
        el = db.execute(
            "SELECT source_id, vector FROM element_embeddings WHERE element_id=?",
            ("el-src-emb-0001",),
        ).fetchone()
        rel = db.execute(
            "SELECT vector FROM relation_embeddings WHERE relation_id=?", (rel_id,)
        ).fetchone()
        ck = db.execute(
            "SELECT vector FROM chunk_embeddings WHERE chunk_id=?", ("ck-emb-1",)
        ).fetchone()
    assert el["source_id"] == "src-emb"
    np.testing.assert_allclose(decode_vector(el["vector"]), [0.1, 0.9])
    np.testing.assert_allclose(decode_vector(rel["vector"]), [0.2, 0.8])
    np.testing.assert_allclose(decode_vector(ck["vector"]), [0.3, 0.7])


def test_replace_is_idempotent_per_id(repo, store, notebook_id):
    store.replace_knowledge_vectors(notebook_id, [("ko-1", [1.0, 0.0])], created_at=NOW)
    store.replace_knowledge_vectors(notebook_id, [("ko-1", [0.0, 1.0])], created_at=NOW)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT vector FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)
        ).fetchall()
    assert len(rows) == 1
    np.testing.assert_allclose(decode_vector(rows[0]["vector"]), [0.0, 1.0])
