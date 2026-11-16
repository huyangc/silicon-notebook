"""Incremental relink preserves committed work and rejects stale source inputs."""
import json

import pytest

from app.models.schemas import KnowledgeUpdate, NotebookCreate
from app.services.kg.relink_work import RELINK_ALGORITHM_VERSION
from tests.relink_staged_cleanup_cases import assert_staged_cleanup_receipts
from tests.test_kg_relink_repository import _insert_object, _insert_source
from tests.test_kg_relink_repository import repo as repo


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("staged", [False, True])
def test_replacement_requeues_surviving_partitions_atomically(repo, rollback, staged):
    assert_staged_cleanup_receipts(repo, postgres=False, rollback=rollback, staged=staged)


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="relink resume")).id
    for sid in ("a", "b"):
        _insert_source(repo, nb, sid)
        _insert_object(repo, nb, sid, "concept", "Engram", [sid])
        _insert_object(repo, nb, sid, "claim", "Engram improves memory", [sid])
    return nb, repo._runtime.knowledge_lifecycle


@pytest.mark.parametrize("analyzed", [False, True])
def test_receipt_invalidation_seeks_changed_ids_instead_of_scanning_notebook(repo, analyzed):
    nb, service = _seed(repo)
    plans = []
    with repo._write() as db:
        now = "2026-01-01T00:00:00+00:00"
        # A wide same-notebook bystander population makes an accidental
        # notebook scan materially different from the handful of changed IDs.
        db.executemany(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,status,"
            "payload,evidence,created_at,updated_at) VALUES (?,?,'b','concept','approved','{}','[]',?,?)",
            [(f"bystander-{i}", nb, now, now) for i in range(2000)],
        )
        db.executemany(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) VALUES (?,?,'b',?,?,'about','[]',?)",
            [(f"bystander-edge-{i}", nb, f"bystander-{i}", f"bystander-{i + 1}", now)
             for i in range(1999)],
        )
        if analyzed:
            db.execute("ANALYZE")
        oid = db.execute("SELECT id FROM knowledge_objects WHERE source_id='a'").fetchone()["id"]

        class ObservePlans:
            def execute(self, statement, params=()):
                if "knowledge_objects" in statement:
                    plans.extend(row["detail"] for row in db.execute(
                        "EXPLAIN QUERY PLAN " + statement, params,
                    ))
                return db.execute(statement, params)

        work = service.knowledge.relink_work
        work.enqueue_for_objects(ObservePlans(), nb, [oid], RELINK_ALGORITHM_VERSION, adjacent=True)
        work.enqueue_for_source_relations(ObservePlans(), nb, ["a"], RELINK_ALGORITHM_VERSION)
    assert plans
    assert not any("SCAN o" in step or "SCAN r" in step for step in plans), "\n".join(plans)
    relation_seeks = [step for step in plans if "SEARCH r " in step]
    assert relation_seeks and all(any(key in step for key in (
        "source_id=?", "source_id>? AND source_id<?", "source_object_id=?", "target_object_id=?",
    )) for step in relation_seeks), plans


def _observe_reads(monkeypatch, service):
    observed = []
    original = service.knowledge.relink_node_rows_for_source

    def read(db, notebook_id, source_id):
        observed.append(source_id)
        return original(db, notebook_id, source_id)

    monkeypatch.setattr(service.knowledge, "relink_node_rows_for_source", read)
    return observed


def _receipts(repo, nb):
    with repo._connect() as db:
        return {row["source_id"]: row["status"] for row in db.execute(
            "SELECT source_id,status FROM kg_relink_work WHERE notebook_id=?", (nb,),
        )}


def test_automatic_bootstrap_then_noop_but_explicit_full_revisits(repo, monkeypatch):
    nb, service = _seed(repo)
    observed = _observe_reads(monkeypatch, service)
    result = service.relink_notebook_kg(nb, incremental=True)
    assert observed == ["a", "b"]
    assert result["edges_added"] == 2
    assert _receipts(repo, nb) == {"a": "done", "b": "done"}
    observed.clear()
    assert service.relink_notebook_kg(nb, incremental=True)["edges_added"] == 0
    assert observed == []
    service.relink_notebook_kg(nb)
    assert observed == ["a", "b"]


def test_store_kg_only_invalidates_changed_source_and_partial_run_has_no_full_mark(repo, monkeypatch):
    nb, service = _seed(repo)
    service.relink_notebook_kg(nb, incremental=True)
    observed = _observe_reads(monkeypatch, service)
    marked = []
    monkeypatch.setattr(service, "_mark_relink_attempted", lambda *args: marked.append(args))
    service.store_kg(nb, "b", [{
        "local_id": "new", "object_type": "claim",
        "payload": {"name": "Engram supports retrieval"},
        "evidence": [{"element_id": "b", "source_id": "b"}],
    }], [])
    assert _receipts(repo, nb) == {"a": "done", "b": "pending"}
    service.relink_notebook_kg(nb, incremental=True)
    assert observed == ["b"]
    assert marked == []
    assert _receipts(repo, nb) == {"a": "done", "b": "done"}


def test_interruption_preserves_completed_source_for_resume(repo, monkeypatch):
    nb, service = _seed(repo)
    original = service._relink_one_source

    def interrupted(notebook_id, source_id, written, **kwargs):
        if source_id == "b":
            raise KeyboardInterrupt()
        return original(notebook_id, source_id, written, **kwargs)

    monkeypatch.setattr(service, "_relink_one_source", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.relink_notebook_kg(nb, incremental=True)
    assert _receipts(repo, nb) == {"a": "done", "b": "pending"}
    assert len(repo.relations_for_notebook(nb)) == 1
    monkeypatch.setattr(service, "_relink_one_source", original)
    observed = _observe_reads(monkeypatch, service)
    result = service.relink_notebook_kg(nb, incremental=True)
    assert observed == ["b"]
    assert result["edges_added"] == 2
    assert len(repo.relations_for_notebook(nb)) == 2


def test_receipt_failure_rolls_back_edges_and_leaves_source_pending(repo, monkeypatch):
    nb, service = _seed(repo)

    def failed_receipt(*args):
        raise RuntimeError("receipt unavailable")

    monkeypatch.setattr(service.knowledge.relink_work, "finish", failed_receipt)
    with pytest.raises(RuntimeError, match="receipt unavailable"):
        service.relink_notebook_kg(nb, incremental=True)
    assert repo.relations_for_notebook(nb) == []
    assert _receipts(repo, nb) == {"a": "pending", "b": "pending"}


def test_source_republished_during_read_cannot_publish_stale_edges(repo, monkeypatch):
    nb, service = _seed(repo)
    work = service.knowledge.relink_work
    with repo._write() as db:
        work.prepare(db, nb, RELINK_ALGORITHM_VERSION)
    with repo._connect() as db:
        revision = next(row["revision"] for row in work.page(db, nb, None, 10)
                        if row["source_id"] == "a")
    original = service.knowledge.relink_node_rows_for_source

    def republished(db, notebook_id, source_id):
        rows = original(db, notebook_id, source_id)
        with repo._write() as writer:
            work.enqueue(writer, notebook_id, source_id, RELINK_ALGORITHM_VERSION)
        return rows

    monkeypatch.setattr(service.knowledge, "relink_node_rows_for_source", republished)
    written = {"edges": 0, "seq_bumps": 0}
    assert service._relink_one_source(nb, "a", written, revision=revision) is None
    assert written == {"edges": 0, "seq_bumps": 0}
    assert repo.relations_for_notebook(nb) == []
    assert _receipts(repo, nb)["a"] == "pending"


def test_graph_reset_discards_receipts_and_next_run_bootstraps_again(repo, monkeypatch):
    nb, service = _seed(repo)
    service.relink_notebook_kg(nb, incremental=True)
    service.delete_notebook_kg(nb)
    assert _receipts(repo, nb) == {}
    with repo._connect() as db:
        assert db.execute("SELECT 1 FROM kg_relink_state WHERE notebook_id=?", (nb,)).fetchone() is None
    observed = _observe_reads(monkeypatch, service)
    service.relink_notebook_kg(nb, incremental=True)
    assert observed == ["a", "b"]


def test_edit_requeues_completed_source_and_automatic_pass_links_new_match(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="edited source")).id
    _insert_source(repo, nb, "edited")
    concept = _insert_object(repo, nb, "edited", "concept", "Unrelated", [])
    _insert_object(repo, nb, "edited", "claim", "Engram improves memory", [])
    service = repo._runtime.knowledge_lifecycle
    assert service.relink_notebook_kg(nb, incremental=True)["edges_added"] == 0
    assert _receipts(repo, nb) == {"edited": "done"}
    repo.update_knowledge(nb, concept, KnowledgeUpdate(payload={"name": "Engram"}))
    assert _receipts(repo, nb) == {"edited": "pending"}
    observed = _observe_reads(monkeypatch, service)
    assert service.relink_notebook_kg(nb, incremental=True)["edges_added"] == 1
    assert observed == ["edited"]


def test_cross_source_cleanup_requeues_deleted_owner_and_surviving_neighbour(repo):
    nb, service = _seed(repo)
    _insert_source(repo, nb, "c")
    neighbour = _insert_object(repo, nb, "c", "concept", "Other", [])
    _insert_source(repo, nb, "d")
    direct_neighbour = _insert_object(repo, nb, "d", "concept", "Direct", [])
    service.relink_notebook_kg(nb, incremental=True)
    with repo._write() as db:
        victim = db.execute("SELECT id FROM knowledge_objects WHERE source_id='b' "
                            "AND object_type='claim'").fetchone()["id"]
        evidence = json.dumps([{"source_id": "a", "element_id": "a"}])
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?", (evidence, victim))
        service.knowledge.replace_object_sources(db, victim, nb, evidence)
        db.execute("UPDATE unified_kg_state SET source_index_backfilled=1 WHERE notebook_id=?", (nb,))
        db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                   "target_object_id,edge_type,evidence,created_at) "
                   "VALUES ('cross',?,'a',?,?,'about','[]','2026-01-01T00:00:00Z')",
                   (nb, victim, neighbour))
        direct = db.execute("SELECT id FROM knowledge_objects WHERE source_id='a' "
                            "AND object_type='claim'").fetchone()["id"]
        db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
                   "target_object_id,edge_type,evidence,created_at) "
                   "VALUES ('direct-cross',?,'a',?,?,'about','[]','2026-01-01T00:00:00Z')",
                   (nb, direct, direct_neighbour))
        service.knowledge.clear_source_graph_state(db, "a", nb)
    assert _receipts(repo, nb) == {"a": "pending", "b": "pending", "c": "pending", "d": "pending"}
    with repo._connect() as db:
        assert db.execute("SELECT id FROM knowledge_objects WHERE id=?", (victim,)).fetchone() is None
        assert db.execute("SELECT id FROM knowledge_objects WHERE id=?", (neighbour,)).fetchone() is not None
