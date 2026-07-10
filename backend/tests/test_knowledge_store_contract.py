"""Task 13 contract: the knowledge / governance / unified-KG persistence
stores are runtime-owned components sharing the ONE SqliteDatabase boundary,
and the knowledge-domain compatibility exports stay the SAME objects after
their canonical definitions move to app.services.knowledge_contracts."""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_runtime_owns_three_knowledge_stores(repo):
    runtime = repo._runtime
    assert runtime.knowledge.database is runtime.database
    assert runtime.governance.database is runtime.database
    assert runtime.unified_kg.database is runtime.database


def test_knowledge_contract_exports_are_same_objects():
    from app.services import knowledge_contracts, sqlite_repository

    assert sqlite_repository.USABLE_STATUSES is knowledge_contracts.USABLE_STATUSES
    assert (
        sqlite_repository.KNOWLEDGE_STATUSES
        is knowledge_contracts.KNOWLEDGE_STATUSES
    )
    assert (
        sqlite_repository.KnowledgeGraphTooLargeError
        is knowledge_contracts.KnowledgeGraphTooLargeError
    )
    # The notebook-store compatibility name keeps pointing at the same tuple.
    from app.repositories.sqlite import notebook_store

    assert notebook_store.USABLE_STATUSES is knowledge_contracts.USABLE_STATUSES


def test_promotion_approval_contract_shape():
    from app.services.knowledge_contracts import PromotionApproval

    approval = PromotionApproval(
        candidate_id="promo-1",
        source_notebook_id="nb-p",
        source_object_id="ko-1",
        base_notebook_id="nb-b",
        base_object_id="ko-2",
        created_new_object=True,
    )
    assert approval.created_new_object
    with pytest.raises(Exception):
        approval.candidate_id = "other"  # frozen dataclass


def test_store_primitives_share_the_facade_transaction(repo):
    """Connection-taking primitives ride the caller's transaction: rows written
    through the store inside one facade _write() are atomic with it."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    knowledge = repo._runtime.knowledge
    now = _now()
    with repo._write() as db:
        knowledge.insert_object_chunk(
            db,
            [(
                "ko-contract-1", nb.id, "concept", "approved",
                json.dumps({"name": "Contract Concept"}), "[]", "", now, now,
            )],
        )
        knowledge.insert_kg_fts_rows(
            db, [("ko-contract-1", nb.id, "Contract Concept")]
        )
    row = knowledge.get_object_row(nb.id, "ko-contract-1")
    assert row is not None and row["object_type"] == "concept"
    with repo._connect() as db:
        hits = knowledge.fts_search(db, nb.id, "Contract", 10)
    assert [h["object_id"] for h in hits] == ["ko-contract-1"]


def test_replace_object_sources_maintains_reverse_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    knowledge = repo._runtime.knowledge
    oid = repo._test_insert_object(nb.id, "claim", {"name": "prov"})
    evidence = json.dumps([
        {"source_id": "src-a", "quoted_span": "x"},
        {"source_id": "src-b", "quoted_span": "y"},
    ])
    with repo._write() as db:
        knowledge.replace_object_sources(db, oid, nb.id, evidence)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
            (oid,),
        ).fetchall()
    assert {r["source_id"] for r in rows} == {"src-a", "src-b"}
    # Shrinking evidence deletes stale rows (delete-then-insert semantics).
    with repo._write() as db:
        knowledge.replace_object_sources(
            db, oid, nb.id, json.dumps([{"source_id": "src-a", "quoted_span": "x"}])
        )
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_id FROM knowledge_object_sources WHERE object_id=?",
            (oid,),
        ).fetchall()
    assert {r["source_id"] for r in rows} == {"src-a"}


def test_governance_store_owns_named_primitives(repo):
    governance = repo._runtime.governance
    for name in (
        "update_edge_review", "delete_clusters", "insert_clusters",
        "write_merge_candidate", "set_merge_decision", "pending_merges",
        "write_conflict_candidate", "set_conflict_status",
        "get_conflict_candidate", "decided_pairs", "decided_seed_pairs",
        "concept_whitelist_terms", "approve_promotion_in_transaction",
        "update_object_in_transaction", "merge_objects_in_transaction",
    ):
        assert callable(getattr(governance, name)), name


def test_unified_store_owns_state_and_checkpoints(repo):
    unified = repo._runtime.unified_kg
    for name in (
        "state_row", "mark_dirty", "bump_cluster_seq", "cluster_input_facts",
        "cluster_map_rows", "finish_rebuild_state", "replace_canonical_relations",
        "replace_mention_bridge", "replace_communities",
        "checkpoint_gc", "checkpoint_clear", "checkpoint_load", "checkpoint_put",
    ):
        assert callable(getattr(unified, name)), name


def test_unified_rebuild_state_update_preserves_mutation_seq(repo):
    """The final rebuild-state upsert must NOT alter kg_mutation_seq — bumping
    would make the version gate never skip; resetting would lose mutations that
    arrived mid-rebuild."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._mark_unified_kg_dirty(nb.id)
    repo._mark_unified_kg_dirty(nb.id)
    with repo._connect() as db:
        before = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["kg_mutation_seq"]
    with repo._write() as db:
        repo._runtime.unified_kg.finish_rebuild_state(db, nb.id, "v-test", 0, _now())
    with repo._connect() as db:
        row = db.execute(
            "SELECT kg_mutation_seq, dirty, cluster_input_version "
            "FROM unified_kg_state WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()
    assert row["kg_mutation_seq"] == before == 2
    assert row["dirty"] == 0
    assert row["cluster_input_version"] == "v-test"


def test_checkpoint_roundtrip_through_store(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    unified = repo._runtime.unified_kg
    unified.checkpoint_put(nb.id, "v1", "merge_review", [("k1", {"d": 1})], _now())
    assert unified.checkpoint_load(nb.id, "v1", "merge_review") == {"k1": {"d": 1}}
    unified.checkpoint_gc(nb.id, "v2")
    assert unified.checkpoint_load(nb.id, "v1", "merge_review") == {}
    unified.checkpoint_put(nb.id, "v2", "concept_desc", [("k2", {"d": 2})], _now())
    unified.checkpoint_clear(nb.id)
    assert unified.checkpoint_load(nb.id, "v2", "concept_desc") == {}
