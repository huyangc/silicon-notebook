"""Relation ANN sidecar tests — mirror the chunk-ANN test shapes in
tests/test_scale_index_repo.py (build/save/load roundtrip, back-compat,
ANN-vs-full-matrix equivalence, core⊕delta merge, partial coverage, the
large+cold no-ANN guard, _open_scale_ann memoization, stage-name updates)."""
import json
import os

import numpy as np
import pytest
import scipy.sparse as sp
from scipy.sparse import load_npz

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg import scale_index as si
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("RELATION_RETRIEVAL_ENABLED", "true")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


# ── scale_index.py layer: save/load roundtrip + back-compat ────────────────


def test_save_scale_index_writes_relation_ann(tmp_path):
    rng = np.random.RandomState(1)
    n, dim = 12, 8
    vecs = rng.randn(n, dim).astype(np.float32)
    labels = [f"r{i}" for i in range(n)]
    out_dir = str(tmp_path / "idx")
    manifest = si.save_scale_index(
        out_dir,
        node_ids=["a", "b"],
        transition=sp.csr_matrix((2, 2)),
        idf=[1.0, 1.0],
        chunk_index=[],
        ann_vectors=np.empty((0, dim), dtype=np.float32),
        ann_labels=[],
        manifest={"version": "v1", "dim": dim},
        relation_ann_vectors=vecs,
        relation_ann_labels=labels,
    )
    assert manifest["has_relation_ann"] is True
    assert manifest["n_relation_ann"] == n
    assert os.path.exists(os.path.join(out_dir, "relation_ann.bin"))
    assert os.path.exists(os.path.join(out_dir, "relation_ann_labels.npy"))


def test_load_scale_index_reads_relation_ann_back(tmp_path):
    rng = np.random.RandomState(2)
    n, dim = 10, 8
    vecs = rng.randn(n, dim).astype(np.float32)
    labels = [f"r{i}" for i in range(n)]
    out_dir = str(tmp_path / "idx")
    si.save_scale_index(
        out_dir,
        node_ids=["a", "b"],
        transition=sp.csr_matrix((2, 2)),
        idf=[1.0, 1.0],
        chunk_index=[],
        ann_vectors=np.empty((0, dim), dtype=np.float32),
        ann_labels=[],
        manifest={"version": "v1", "dim": dim},
        relation_ann_vectors=vecs,
        relation_ann_labels=labels,
    )
    idx = si.load_scale_index(out_dir)
    assert idx is not None
    assert list(idx.relation_ann_labels) == labels
    assert idx.relation_ann_path.endswith("relation_ann.bin")


def test_load_scale_index_without_relation_ann_is_back_compat(tmp_path):
    """An older index built before this task (no relation_ann_* args) must
    still load fine — has_relation_ann absent in manifest, relation_ann_labels/
    path stay None. Mirrors has_chunk_ann's older-index-stays-valid property."""
    out_dir = str(tmp_path / "idx")
    si.save_scale_index(
        out_dir,
        node_ids=["a", "b"],
        transition=sp.csr_matrix((2, 2)),
        idf=[1.0, 1.0],
        chunk_index=[],
        ann_vectors=np.empty((0, 4), dtype=np.float32),
        ann_labels=[],
        manifest={"version": "v1", "dim": 4},
    )
    idx = si.load_scale_index(out_dir)
    assert idx is not None
    assert idx.relation_ann_labels is None
    assert idx.relation_ann_path is None
    assert not os.path.exists(os.path.join(out_dir, "relation_ann.bin"))


def test_save_scale_index_relation_ann_empty_labels_skips_artifact(tmp_path):
    """relation_ann_labels=None/[] (e.g. relation_retrieval disabled or no
    relations embedded yet) must not write relation_ann.bin at all — mirrors
    the chunk_ann `if chunk_ann_labels:` guard."""
    out_dir = str(tmp_path / "idx")
    manifest = si.save_scale_index(
        out_dir,
        node_ids=["a"],
        transition=sp.csr_matrix((1, 1)),
        idf=[1.0],
        chunk_index=[],
        ann_vectors=np.empty((0, 4), dtype=np.float32),
        ann_labels=[],
        manifest={"version": "v1", "dim": 4},
        relation_ann_vectors=None,
        relation_ann_labels=[],
    )
    assert "has_relation_ann" not in manifest
    assert not os.path.exists(os.path.join(out_dir, "relation_ann.bin"))


# ── repository layer: build_scale_index writes relation_ann ────────────────


def _seed_relation(repo, nb_id=None):
    nb = repo.create_notebook(NotebookCreate(name="base")) if nb_id is None else repo.get_notebook(nb_id)
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a",
         "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    return nb


def _backfill_relation_vector(repo, nb_id):
    with repo._connect() as db:
        rels = db.execute(
            "SELECT id, edge_type FROM knowledge_relations WHERE notebook_id=?", (nb_id,)).fetchall()
    with repo._write() as db:
        for r in rels:
            existing = db.execute(
                "SELECT 1 FROM relation_embeddings WHERE relation_id=?", (r["id"],)).fetchone()
            if existing:
                continue
            v = repo.embedder.embed_texts([r["edge_type"]])[0]
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)",
                (r["id"], nb_id, json.dumps(v), "2026-07-01T00:00:00"))
    return [r["id"] for r in rels]


def test_build_scale_index_writes_relation_ann(repo):
    nb = _seed_relation(repo)
    rel_ids = _backfill_relation_vector(repo, nb.id)
    manifest = repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    assert os.path.exists(os.path.join(d, "relation_ann.bin"))
    assert os.path.exists(os.path.join(d, "relation_ann_labels.npy"))
    assert manifest.get("has_relation_ann") is True
    assert manifest.get("n_relation_ann") == len(rel_ids)
    idx = repo._scale_index(nb.id)
    assert idx is not None
    assert set(idx.relation_ann_labels) == set(rel_ids)
    assert idx.relation_ann_path.endswith("relation_ann.bin")


def test_build_scale_index_no_relation_vectors_no_relation_ann(repo):
    """Relations exist but none embedded (no embedder configured / not yet
    backfilled) → build_scale_index must not write relation_ann.bin, and
    older-index-stays-valid: has_relation_ann simply absent.

    _seed_relation's store_kg auto-backfills relation_embeddings (via
    RELATION_RETRIEVAL_ENABLED=true in the fixture), so explicitly clear the
    table afterwards to simulate the "no embedder configured yet" case."""
    nb = _seed_relation(repo)
    with repo._write() as db:
        db.execute("DELETE FROM relation_embeddings WHERE notebook_id=?", (nb.id,))
    manifest = repo.build_scale_index(nb.id)
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    assert not os.path.exists(os.path.join(d, "relation_ann.bin"))
    assert not manifest.get("has_relation_ann")


def test_build_scale_index_emits_relation_matrix_stage(repo, monkeypatch):
    """New relation_matrix stage must be timed/emitted alongside the existing
    8 stages — mirrors chunk_matrix's stage-name test."""
    nb = _seed_relation(repo)
    _backfill_relation_vector(repo, nb.id)

    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)
    manifest = repo.build_scale_index(nb.id)

    expected_stages = {"kg_matrix", "ann_build", "synonym", "gather", "transition",
                       "chunk_matrix", "relation_matrix", "viz_arrays", "persist"}
    returned_build_ms = manifest["build_ms"]
    assert set(returned_build_ms.keys()) == expected_stages | {"total"}

    scale_events = [e for e in events if e.get("kind") == "scale_index_build"]
    stages_seen = {e["stage"] for e in scale_events}
    assert stages_seen == expected_stages | {"total"}


def test_open_scale_ann_relation_kind_memoizes(repo, monkeypatch):
    """_open_scale_ann(idx, 'relation') must memoize the hnswlib handle on the
    ScaleIndex instance — mirrors test_scale_ann_handle_cached for 'kg'."""
    nb = _seed_relation(repo)
    _backfill_relation_vector(repo, nb.id)
    repo.build_scale_index(nb.id)
    idx = repo._scale_index(nb.id)
    assert idx is not None and idx.relation_ann_labels

    import hnswlib
    calls = {"n": 0}
    real = hnswlib.Index.load_index

    def spy(self, *a, **k):
        calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(hnswlib.Index, "load_index", spy)
    h1 = repo._open_scale_ann(idx, "relation")
    h2 = repo._open_scale_ann(idx, "relation")
    assert h1 is not None and h1 is h2
    assert calls["n"] == 1


# ── query: ANN-path result equivalence vs full-matrix oracle ───────────────


def test_retrieve_relations_ann_matches_full_matrix_oracle(repo):
    """Small n, ef high: the ANN branch's top hit must agree with brute-force
    top_k_sims over the same relation matrix (oracle)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    objs = [{"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": f"entity {i}", "section_path": ""}, "evidence": []}
            for i in range(6)]
    rels = [{"source_local_id": f"o{i}", "target_local_id": f"o{i+1}",
             "edge_type": f"relates_{i}", "evidence": []} for i in range(5)]
    repo.store_kg(nb.id, None, objs, rels)
    repo.rebuild_unified_kg(nb.id)
    rel_ids = _backfill_relation_vector(repo, nb.id)
    repo.build_scale_index(nb.id)

    from app.services.vector_index import top_k_sims
    with repo._connect() as db:
        ids, mat = repo._vector_matrix(db, nb.id, "relation_embeddings", "relation_id")
    qv = repo._embed_query("relates_2")
    oracle = dict(top_k_sims(qv, ids, mat, len(ids)))
    oracle_top = max(oracle.items(), key=lambda kv: kv[1])[0]

    hits = repo._retrieve_relations_scored(nb.id, "relates_2")
    assert hits
    top_hit_ids = {h.relation_id for h in hits[:3]}
    assert oracle_top in top_hit_ids or len(rel_ids) <= 3


def test_retrieve_relations_scored_returns_valid_scores(repo):
    nb = _seed_relation(repo)
    _backfill_relation_vector(repo, nb.id)
    repo.build_scale_index(nb.id)
    hits = repo._retrieve_relations_scored(nb.id, "MOSFET current mirror")
    assert hits
    assert all(0.0 <= h.score for h in hits)


# ── core ⊕ delta merge: post-watermark relation retrievable ────────────────


def test_retrieve_relations_ann_plus_delta_finds_post_watermark_relation(repo, monkeypatch):
    """opt-in delta brute-force: with scale_search_include_delta=True, a NEW
    source's relation (post-watermark, not in the ANN) is still retrievable
    via the small id-scoped delta brute-force path. (Default is now OFF —
    see test_indexed_only_principle.py — so this test explicitly enables the
    opt-in to exercise the brute-force branch.)"""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a",
         "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    _backfill_relation_vector(repo, nb.id)
    repo.build_scale_index(nb.id)

    # New source added AFTER the index watermark — its relation is delta.
    with repo._write() as db:
        now = "2026-07-05T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s2", nb.id, "t2", "md", "ready", now, now))
        for oid, name in [("c", "bandgap"), ("d", "reference voltage")]:
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", "s2", now, now))
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_object_id,target_object_id,"
            "edge_type,evidence,source_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("rel-delta", nb.id, "c", "d", "bandgap_uses_reference", "[]", "s2", now))
        v = repo.embedder.embed_texts(["bandgap_uses_reference"])[0]
        db.execute(
            "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            ("rel-delta", nb.id, json.dumps(v), now))

    hits = repo._retrieve_relations_scored(nb.id, "bandgap_uses_reference")
    assert any(h.relation_id == "rel-delta" for h in hits)


# ── partial coverage: ANN only has embedded relations ───────────────────────


def test_relation_ann_partial_coverage_matches_matrix_semantics(repo):
    """Only embedded relations enter the ANN — a relation with no
    relation_embeddings row is absent from relation_ann_labels, same as
    today's matrix path (rel_ids from _vector_matrix never included it either)."""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "X", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "Y", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept",
         "payload": {"name": "Z", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "rel_ab", "evidence": []},
        {"source_local_id": "b", "target_local_id": "c", "edge_type": "rel_bc", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    with repo._connect() as db:
        all_rel_ids = [r["id"] for r in db.execute(
            "SELECT id FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchall()]
    assert len(all_rel_ids) == 2
    # store_kg auto-embeds both relations (RELATION_RETRIEVAL_ENABLED=true in
    # the fixture) — delete one relation_embeddings row to simulate partial
    # coverage (e.g. embedder was reconfigured mid-stream / backfill gap).
    only_id = all_rel_ids[0]
    drop_id = all_rel_ids[1]
    with repo._write() as db:
        db.execute("DELETE FROM relation_embeddings WHERE relation_id=?", (drop_id,))
    manifest = repo.build_scale_index(nb.id)
    assert manifest.get("n_relation_ann") == 1
    idx = repo._scale_index(nb.id)
    assert set(idx.relation_ann_labels) == {only_id}


# ── #171 guard (master's large+cold cold-matrix guard) vs the ANN branch ───
# 守卫本体的行为矩阵由 tests/test_relation_scoring_cold_matrix_guard.py(随
# #171 合入 master)覆盖;这里只测「守卫与 ANN 分支的相互次序」——ANN 在前、
# 守卫退位为无 ANN 大库的最后兜底。


def test_relation_scoring_ann_bypasses_large_cold_guard(repo, monkeypatch):
    """Large (copyable=False) + cold matrix + persisted relation ANN → the
    ANN branch (②) runs BEFORE the #171 guard (③): semantic hits come back,
    no relation_scoring_skipped event — the guard is demoted from the common
    case to the no-ANN last resort."""
    nb = _seed_relation(repo)
    _backfill_relation_vector(repo, nb.id)
    repo.build_scale_index(nb.id)
    # Evict any matrix warmth so ONLY the ANN branch can explain a non-skip.
    repo._vector_cache.invalidate(f"{nb.id}:matrix:relation_embeddings")
    monkeypatch.setattr(repo.retrieval.candidates, "notebook_copy_stats",
                        lambda notebook_id: {"copyable": False, "size": {}})

    events = []
    orig_emit = repo.event_log.emit

    def spy_emit(event, **kw):
        events.append(event)
        return orig_emit(event, **kw)

    monkeypatch.setattr(repo.event_log, "emit", spy_emit)

    hits = repo._retrieve_relations_scored(nb.id, "MOSFET current mirror")
    skip_events = [e for e in events if e.get("kind") == "relation_scoring_skipped"]
    assert not skip_events, "ANN branch must pre-empt the #171 guard"
    assert hits


# ── fold: relation ANN gets add_items (mirrors chunk_ann fold behavior) ────


def test_fold_scale_index_delta_extends_relation_ann(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))

    def add_source_with_relation(sid, o1, o2, rid, n1, n2, et, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute(
                "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for oid, name in [(o1, n1), (o2, n2)]:
                db.execute(
                    "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
                    "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (oid, nb.id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, now, now))
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_object_id,target_object_id,"
                "edge_type,evidence,source_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (rid, nb.id, o1, o2, et, "[]", sid, now))
            for oid, name in [(o1, n1), (o2, n2)]:
                v = repo.embedder.embed_texts([name])[0]
                db.execute(
                    "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                    (oid, nb.id, json.dumps(v), now))
            v = repo.embedder.embed_texts([et])[0]
            db.execute(
                "INSERT INTO relation_embeddings (relation_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                (rid, nb.id, json.dumps(v), now))

    add_source_with_relation("s1", "o1", "o2", "rel1", "MOSFET", "current mirror", "depends_on", 1)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    idx0 = repo._scale_index(nb.id)
    assert idx0.relation_ann_labels and "rel1" in set(idx0.relation_ann_labels)

    add_source_with_relation("s2", "o3", "o4", "rel2", "bandgap", "voltage ref", "uses", 2)
    repo.fold_scale_index_delta(nb.id)

    idx1 = repo._scale_index(nb.id)
    assert idx1 is not None
    assert idx1.relation_ann_labels is not None
    assert "rel1" in set(idx1.relation_ann_labels)
    assert "rel2" in set(idx1.relation_ann_labels), (
        "fold must add_items delta relation vectors into relation ANN, mirroring chunk_ann fold")


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
