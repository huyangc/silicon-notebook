import pytest
from unittest.mock import patch
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")  # embedder_configured=True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)               # inject; no real model loads (lazy)
    return r


def test_rebuild_streaming_clusters_same_name(repo):
    """Two concepts MOSFET/mosfet across two store_kg calls collapse to one
    canonical via the streamed (scratch-table) rebuild path."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
                                 "payload": {"name": "MOSFET", "section_path": ""},
                                 "evidence": []}], [])
    repo.store_kg(nb.id, None, [{"local_id": "b", "object_type": "concept",
                                 "payload": {"name": "mosfet", "section_path": ""},
                                 "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2


def test_rebuild_scratch_table_cleaned(repo):
    """After rebuild, the transient kg_cluster_scratch holds no rows for the nb."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "claim",
         "payload": {"name": "gain is high", "section_path": ""}, "evidence": []},
    ], [])
    repo.rebuild_unified_kg(nb.id)
    with repo._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert n == 0


def test_concurrent_rebuild_scratch_isolated(repo):
    """run_id isolation: a stray scratch row left by a different run is untouched
    by a subsequent rebuild, and both rebuilds produce correct clusters + leave
    zero own-run scratch rows behind.

    Simulates interleaving by inserting a row with a fake 'other_run' run_id
    BEFORE the second rebuild. Asserts:
      1. After rebuild 1: no scratch rows for nb (own run fully cleaned).
      2. After inserting the stray row: it is visible in the table.
      3. After rebuild 2: stray row is STILL there (different run_id untouched),
         AND the rebuild's own rows are fully cleaned.
      4. Both rebuilds produce the same stable cluster result.
    """
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept",
         "payload": {"name": "NMOS", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept",
         "payload": {"name": "nmos", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "claim",
         "payload": {"name": "gain is high", "section_path": ""}, "evidence": []},
    ], [])

    # --- Rebuild 1 ---
    count1 = repo.rebuild_unified_kg(nb.id)
    cmap1 = repo.cluster_map(nb.id)
    with repo._connect() as db:
        after1 = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]
    assert after1 == 0, "rebuild 1 must leave zero scratch rows"

    # Insert a stray row as if a different run_id's rebuild were still in flight.
    stray_run_id = "other_run_000"
    with repo._write() as db:
        db.execute(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) VALUES (?,?,?,?)",
            (nb.id, stray_run_id, "fake-obj-id", "fake-seed"))
    with repo._connect() as db:
        stray_count = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, stray_run_id)).fetchone()["c"]
    assert stray_count == 1, "stray row must be present before rebuild 2"

    # --- Rebuild 2 ---
    count2 = repo.rebuild_unified_kg(nb.id)
    cmap2 = repo.cluster_map(nb.id)

    # Stray row from the other run must be untouched.
    with repo._connect() as db:
        stray_after = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=? AND run_id=?",
            (nb.id, stray_run_id)).fetchone()["c"]
        total_after = db.execute(
            "SELECT COUNT(*) AS c FROM kg_cluster_scratch WHERE notebook_id=?",
            (nb.id,)).fetchone()["c"]

    assert stray_after == 1, "stray row from other run_id must survive rebuild 2"
    assert total_after == 1, "rebuild 2's own rows must all be cleaned (only stray remains)"

    # Both rebuilds should produce the same stable cluster result.
    assert count1 == count2, "cluster count must be stable across rebuilds"
    # NMOS/nmos (concepts) → one canonical; two member objects.
    # cluster_map returns all types (concepts + claims), so filter to concept entries.
    concept_canonicals1 = {cid for cid in cmap1.values() if cid.startswith("K-")}
    assert len(concept_canonicals1) == 1, "NMOS/nmos must collapse to one concept canonical"
    concept_members1 = [oid for oid, cid in cmap1.items() if cid.startswith("K-")]
    assert len(concept_members1) == 2, "both NMOS objects must be in the concept cluster"
    assert cmap1 == cmap2, "cluster map must be identical across sequential rebuilds"
