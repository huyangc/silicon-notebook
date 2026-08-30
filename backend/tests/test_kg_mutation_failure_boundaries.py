"""Task 14 — KgMutationCoordinator failure boundaries.

Failure-injection companions to test_kg_mutation_phase_matrix: each test
breaks ONE phase and asserts exactly what the frozen matrix says survives.

    store_kg second object chunk   whole source graph rolls back;
                                   no relations, no embeds, no hooks
    write_clusters insert          replace transaction rolls back atomically
                                   (old rows survive, no cluster-seq bump,
                                   no invalidate)
    approve_promotion candidate    base mutation and candidate approval roll
                                   back together; hooks never run
    confirm_conflict status        candidate-status failure leaves the already
                                   committed resolution mutation applied
                                   (hooks for the mutation already ran)
    update_knowledge embed         best-effort embed failure still
                                   invalidates and marks dirty — and since
                                   codex #638 R5 the bump already committed
                                   with the row BEFORE the embed ran, so it
                                   no longer depends on that failure being
                                   swallowed at all

Injection rides component seams (runtime.knowledge / runtime.governance /
runtime.embedding_store), mirroring how the frozen phase-contract suite
injects through SQL-substring failing connections — never fresh facade
patch targets.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import KnowledgeUpdate, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'boundary.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _spy_coordinator(repo, monkeypatch, events):
    coordinator = repo._runtime.kg_mutations
    monkeypatch.setattr(
        coordinator,
        "invalidate_unified_cache",
        lambda notebook_id: events.append("invalidate"),
    )
    monkeypatch.setattr(
        coordinator,
        "mark_unified_kg_dirty",
        lambda notebook_id: events.append("dirty"),
    )
    # codex #638 R5: most graph-row writers now bump INSIDE their own write
    # transaction, so a spy on the post-commit entry alone would silently stop
    # observing them. Wrap-and-delegate (callers may need the real seq back);
    # note this records the CALL, so a bump that is later rolled back with its
    # transaction still shows up — every failure injected in this file lands
    # before its operation's bump, so the traces stay unambiguous.
    original_in_tx = coordinator.mark_unified_kg_dirty_in_tx

    def _in_tx(db, notebook_id):
        events.append("dirty")
        return original_in_tx(db, notebook_id)

    monkeypatch.setattr(coordinator, "mark_unified_kg_dirty_in_tx", _in_tx)


def _claim(local_id, name):
    return {
        "local_id": local_id,
        "object_type": "claim",
        "payload": {"name": name},
        "evidence": [],
    }


def _count(repo, sql, *params):
    with repo._connect() as db:
        return db.execute(sql, params).fetchone()["c"]


def test_store_kg_second_chunk_failure_rolls_back_whole_source_graph(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="chunk failure"))
    objects = [_claim(f"L{i}", f"claim {i}") for i in range(1500)]
    relations = [
        {"source_local_id": "L0", "target_local_id": "L1",
         "edge_type": "about", "evidence": []}
    ]
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.source_embedding,
        "embed_objects_batch",
        lambda notebook_id, items, progress=None, commit_every=None: (
            events.append("embed_objects")
        ),
    )
    monkeypatch.setattr(
        repo._runtime.source_embedding,
        "embed_relations_batch",
        lambda notebook_id, rel_items: events.append("embed_relations"),
    )
    knowledge = repo._runtime.knowledge
    original = knowledge.insert_object_chunk
    calls = {"n": 0}

    def failing_second_chunk(db, rows):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second chunk failed")
        return original(db, rows)

    monkeypatch.setattr(knowledge, "insert_object_chunk", failing_second_chunk)

    with pytest.raises(RuntimeError, match="second chunk failed"):
        repo.store_kg(notebook.id, None, objects, relations)

    # A source is the KG persistence boundary: a later chunk failure rolls back
    # the earlier chunk too, and no later phase runs.
    assert _count(
        repo, "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
        notebook.id,
    ) == 0
    assert _count(
        repo, "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=?",
        notebook.id,
    ) == 0
    assert events == []


def test_write_clusters_insert_failure_rolls_back_the_whole_replace(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="cluster rollback"))
    object_id = repo._test_insert_object(notebook.id, "concept", {"name": "seed"})
    repo.write_clusters(notebook.id, [{
        "canonical_id": "K-old",
        "member_object_id": object_id,
        "canonical_name": "seed",
    }])
    with repo._connect() as db:
        seq_before = int(repo._runtime.unified_kg.state_row(
            db, notebook.id)["cluster_mutation_seq"])
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.governance,
        "insert_clusters",
        lambda db, notebook_id, object_type, rows, now: (
            (_ for _ in ()).throw(RuntimeError("cluster insert failed"))
        ),
    )

    with pytest.raises(RuntimeError, match="cluster insert failed"):
        repo.write_clusters(notebook.id, [{
            "canonical_id": "K-new",
            "member_object_id": object_id,
            "canonical_name": "seed",
        }])

    # The DELETE from the same transaction rolled back with the failed INSERT:
    # the pre-existing cluster row survives, the cluster-seq bump never
    # happened, and no invalidation hook ran.
    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_id FROM concept_clusters WHERE notebook_id=?",
            (notebook.id,),
        ).fetchall()
        seq_after = int(repo._runtime.unified_kg.state_row(
            db, notebook.id)["cluster_mutation_seq"])
    assert [r["canonical_id"] for r in rows] == ["K-old"]
    assert seq_after == seq_before
    assert events == []


def test_approve_promotion_candidate_failure_rolls_back_base_and_candidate(
    repo, monkeypatch
):
    personal = repo.create_notebook(NotebookCreate(name="personal rollback"))
    base = repo.create_notebook(NotebookCreate(name="base rollback"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(personal.id, [base.id], "user-local")
    object_id = repo._test_insert_object(
        personal.id, "claim", {"name": "rollback promotion"}
    )
    candidate = repo.propose_promotion(personal.id, object_id)
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    governance = repo._runtime.governance
    original = governance.approve_promotion_in_transaction

    class FailingConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, *args, **kwargs):
            if "UPDATE promotion_candidates" in sql and "status='approved'" in sql:
                raise RuntimeError("candidate update failed")
            return self._connection.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    monkeypatch.setattr(
        governance,
        "approve_promotion_in_transaction",
        lambda db, candidate_id, now: original(
            FailingConnection(db), candidate_id, now
        ),
    )

    with pytest.raises(RuntimeError, match="candidate update failed"):
        repo.approve_promotion(candidate["id"])

    with repo._connect() as db:
        candidate_status = db.execute(
            "SELECT status FROM promotion_candidates WHERE id=?",
            (candidate["id"],),
        ).fetchone()["status"]
    assert candidate_status == "proposed"
    assert _count(
        repo,
        "SELECT COUNT(*) AS c FROM knowledge_objects "
        "WHERE notebook_id=? AND source_candidate_id=?",
        base.id, candidate["id"],
    ) == 0
    assert events == []


def test_confirm_conflict_status_failure_leaves_mutation_committed(
    repo, monkeypatch
):
    notebook = repo.create_notebook(NotebookCreate(name="conflict boundary"))
    left = repo._test_insert_object(notebook.id, "claim", {"name": "before"})
    right = repo._test_insert_object(notebook.id, "claim", {"name": "other"})
    candidate_id = repo.write_conflict_candidate(
        notebook.id,
        "node",
        left,
        right,
        resolution="modify",
        winner_ref=left,
        resolved_payload=json.dumps({"name": "after"}),
    )
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.governance,
        "set_conflict_status",
        lambda db, notebook_id, cid, status, now: (
            (_ for _ in ()).throw(RuntimeError("status failed"))
        ),
    )

    with pytest.raises(RuntimeError, match="status failed"):
        repo.confirm_conflict(notebook.id, candidate_id)

    with repo._connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (left,)
        ).fetchone()["payload"])
        candidate_status = db.execute(
            "SELECT status FROM kg_conflict_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()["status"]
    assert payload["name"] == "after"          # resolution mutation committed
    assert candidate_status == "pending"       # status transaction failed
    # The mutation's own hooks ran BEFORE the status failure: update_knowledge's
    # bump (in its own transaction since codex #638 R5, hence first) and its
    # invalidate, plus the conflict path's second, post-commit bump.
    assert events == ["dirty", "invalidate", "dirty"]


def test_update_knowledge_embed_failure_still_invalidates_and_marks_dirty(
    repo, monkeypatch
):
    """codex #638 R5 strengthened this boundary rather than weakening it: the
    bump no longer merely *survives* a best-effort embed failure, it committed
    with the row before the embed was ever attempted, so it cannot depend on
    that failure being swallowed."""
    notebook = repo.create_notebook(NotebookCreate(name="embed boundary"))
    object_id = repo._test_insert_object(
        notebook.id, "claim", {"name": "embed me"}
    )
    events = []
    _spy_coordinator(repo, monkeypatch, events)
    monkeypatch.setattr(
        repo._runtime.embedding_store,
        "replace_knowledge_vectors",
        lambda notebook_id, rows, created_at="": (
            (_ for _ in ()).throw(RuntimeError("embed store down"))
        ),
    )

    card = repo.update_knowledge(
        notebook.id, object_id, KnowledgeUpdate(payload={"name": "renamed"})
    )

    assert card is not None
    with repo._connect() as db:
        payload = json.loads(db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()["payload"])
    assert payload["name"] == "renamed"
    assert events == ["dirty", "invalidate"]
