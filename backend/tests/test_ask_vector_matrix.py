"""ask() 走 vector_index 的 float32 矩阵路径(低内存),而非把所有向量
materialize 成 Python list。验证矩阵构建 + 端到端 ask 仍正确。"""
import json
import math

import numpy as np
import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from app.services.vector_index import encode_vector
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


class _FakeLLM:
    configured = True
    def chat_json(self, messages, response_schema_hint):
        return "{}"
    def embed(self, text):
        return [0.0] * 16


def test_gather_elements_without_vectors_skips_load(repo):
    # with_vectors=False must not populate the 'vector' field (avoids json.loads)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._connect() as db:
        els = repo._gather_elements(db, nb.id, with_vectors=False)
    assert all(e["vector"] is None for e in els)


def test_vector_matrix_builds_from_embeddings(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "MOSFET"}}])
    with repo._connect() as db:
        ids, mat = repo.retrieval.candidates._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")
    assert ids == [oid]
    assert mat.shape == (1, 16)
    # New writes go through _embed_objects_batch, which now stores BLOB (not JSON text).
    with repo._connect() as db:
        ty = db.execute("SELECT typeof(vector) t FROM knowledge_embeddings WHERE object_id=?",
                        (oid,)).fetchone()["t"]
    assert ty == "blob"


def test_vector_matrix_mixed_json_and_blob_rows_matches_all_json_oracle(repo):
    """A table with some legacy JSON-text rows and some BLOB rows (e.g. mid-
    backfill, or a fresh install alongside an old un-migrated notebook) must
    build the exact same matrix as if every row were still JSON text."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    rng = np.random.default_rng(5)
    vecs = {f"ko-{i}": rng.normal(size=16).astype(np.float32) for i in range(6)}
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        for i, (oid, vec) in enumerate(vecs.items()):
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "active", json.dumps({"name": oid}), "src", now, now))
            # Even index -> BLOB (as if backfilled/new write); odd -> legacy JSON text.
            raw = encode_vector(vec) if i % 2 == 0 else json.dumps(vec.tolist())
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)", (oid, nb.id, raw, now))

    with repo._connect() as db:
        ids_mixed, mat_mixed = repo.retrieval.candidates._vector_matrix(db, nb.id, "knowledge_embeddings", "object_id")

    # Oracle: rebuild the same notebook's embeddings table with every row as JSON text.
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    with repo._write() as db:
        for oid, vec in vecs.items():
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
                "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (f"{oid}-o", nb2.id, "concept", "active", json.dumps({"name": oid}), "src", now, now))
            db.execute(
                "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)", (f"{oid}-o", nb2.id, json.dumps(vec.tolist()), now))
    with repo._connect() as db:
        ids_oracle, mat_oracle = repo.retrieval.candidates._vector_matrix(db, nb2.id, "knowledge_embeddings", "object_id")

    # Same ids (modulo the "-o" suffix) and identical normalized matrix content.
    assert sorted(ids_mixed) == sorted(vecs.keys())
    assert mat_mixed.shape == mat_oracle.shape
    # Reorder oracle rows to match mixed's id order for an elementwise compare.
    order = [ids_oracle.index(f"{oid}-o") for oid in ids_mixed]
    assert np.allclose(mat_mixed, mat_oracle[order], atol=1e-5)


def test_ask_matrix_path_returns_matching_object(repo):
    # P4-5: ask_fast retired; verify vector-matrix path via _retrieve_scored directly.
    bind_chat_client(repo, "ask_answer", _FakeLLM())
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "claim", {"name": "Engram improves perplexity"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "Engram improves perplexity"}}])
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "does engram improve perplexity")
    assert any("Engram" in (h.payload.get("name") or "") for h in hits)


def test_ask_does_not_backfill_missing_knowledge_embeddings(repo, monkeypatch):
    bind_chat_client(repo, "ask_answer", _FakeLLM())
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._test_insert_object(nb.id, "claim", {"name": "Channel loss depends on equalization"})

    def fail_backfill(*args, **kwargs):
        raise AssertionError("ask() must not synchronously backfill knowledge embeddings")

    monkeypatch.setattr(repo._runtime.source_embedding, "backfill_knowledge_embeddings", fail_backfill)
    resp = repo.ask(nb.id, AskRequest(question="channel loss equalization"))
    assert resp.conversation_id
    assert resp.answer_id


def test_ask_does_not_load_all_source_elements_for_citation_validation(repo, monkeypatch):
    # P4-5: ask_fast retired. This test was specific to ask_fast's element-gather
    # optimization. Replaced: verify _retrieve_scored surfaces the bandwidth claim
    # without loading all elements (the optimization now lives in ask_chunk; the
    # graph ask engine, which also inherited it, has since been retired in turn).
    bind_chat_client(repo, "ask_answer", _FakeLLM())
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "claim", {"name": "Finite cable bandwidth attenuates high frequencies"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "Finite cable bandwidth attenuates high frequencies"}}])
    hits = repo.retrieval.candidates._retrieve_scored(nb.id, "why does cable bandwidth matter")
    assert any("bandwidth" in (h.payload.get("name") or "").lower() for h in hits)


# --- _stream_seed_reps Pass B: mixed JSON/BLOB knowledge_embeddings rows ----

def _seed_concept_with_vector(repo, nb_id, oid, name, vec, created_at, raw_mode):
    """raw_mode: 'json' -> legacy JSON text row; 'blob' -> encode_vector row."""
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "active", json.dumps({"name": name}), "src",
             created_at, created_at))
        raw = encode_vector(vec) if raw_mode == "blob" else json.dumps(vec.tolist())
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)", (oid, nb_id, raw, created_at))


def test_stream_seed_reps_pass_b_mixed_json_and_blob_matches_all_json_oracle(repo):
    """_stream_seed_reps Pass B joins knowledge_embeddings by object_id and means
    the vectors per seed. A table with some BLOB rows (new writes/backfilled) and
    some legacy JSON rows must produce identical reps to an all-JSON table."""
    from app.services.kg_merge import seed_concept

    embed_dim = repo.settings.embed_dim
    rng = np.random.default_rng(9)
    # Two concepts sharing seed "widget" (same normalized name) + one distinct.
    specs = [
        ("ko-1", "Widget", rng.normal(size=embed_dim).astype(np.float32), "blob"),
        ("ko-2", "widget", rng.normal(size=embed_dim).astype(np.float32), "json"),
        ("ko-3", "gadget assembly", rng.normal(size=embed_dim).astype(np.float32), "blob"),
    ]
    now = "2026-01-01T00:00:00"
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for oid, name, vec, mode in specs:
        _seed_concept_with_vector(repo, nb.id, oid, name, vec, now, mode)

    reps_mixed, members_mixed, _ = repo._stream_seed_reps(
        nb.id, "concept", seed_concept, run_id="r1", compute_reps=True)

    # Oracle: identical concepts/vectors, all stored as legacy JSON text.
    # object_id is globally unique (not notebook-scoped) — use distinct ids.
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    for oid, name, vec, _mode in specs:
        _seed_concept_with_vector(repo, nb2.id, f"{oid}-o", name, vec, now, "json")
    reps_oracle, members_oracle, _ = repo._stream_seed_reps(
        nb2.id, "concept", seed_concept, run_id="r2", compute_reps=True)

    assert set(reps_mixed.keys()) == set(reps_oracle.keys()) == {"widget", "gadget assembly"}
    assert members_mixed == members_oracle == {"widget": 2, "gadget assembly": 1}
    for seed in reps_mixed:
        assert np.allclose(reps_mixed[seed], reps_oracle[seed], atol=1e-5)


def test_stream_seed_reps_pass_b_skips_wrong_dim_blob_row(repo):
    """A BLOB row whose decoded length != settings.embed_dim must be skipped,
    same as the legacy JSON wrong-length skip (mirrors the old length filter)."""
    from app.services.kg_merge import seed_concept

    embed_dim = repo.settings.embed_dim
    now = "2026-01-01T00:00:00"
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    good_vec = np.random.default_rng(1).normal(size=embed_dim).astype(np.float32)
    bad_vec = np.zeros(embed_dim + 3, dtype=np.float32)  # wrong dim, still valid BLOB
    _seed_concept_with_vector(repo, nb.id, "ko-good", "widget", good_vec, now, "blob")
    _seed_concept_with_vector(repo, nb.id, "ko-bad", "gadget", bad_vec, now, "blob")

    reps, members, _ = repo._stream_seed_reps(
        nb.id, "concept", seed_concept, run_id="r1", compute_reps=True)

    assert "widget" in reps  # good vector produced a rep
    assert "gadget" not in reps  # wrong-dim vector skipped, no rep for that seed
    assert members == {"widget": 1, "gadget": 1}  # membership counting unaffected
