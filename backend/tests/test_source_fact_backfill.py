import json
import uuid

import pytest

from app.core.config import Settings
from app.services.batch_ingest import run_backfill_source_facts
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'facts.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed(repo, *, objects=2):
    notebook_id = f"nb-{uuid.uuid4().hex}"
    source_id = f"src-{uuid.uuid4().hex}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (notebook_id, "facts", "", "", "ready", "user-local", now, now),
        )
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "source", "markdown", "ready", "parsed",
             "s.md", "s.md", 1, "hash", "", "", now, now),
        )
        for index in range(objects):
            element_id = f"el-{index}"
            object_id = f"ko-{index}"
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,'{}',?)",
                (element_id, source_id, "paragraph", "p", f"body {index}", now),
            )
            evidence = [{"source_id": source_id, "element_id": element_id}]
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (object_id, notebook_id, "concept", "approved", "",
                 json.dumps({"name": f"object {index}"}), json.dumps(evidence),
                 source_id, now, now),
            )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-history',?,?, 'kg','completed','kg objects=2 relations=0',?,?)",
            (notebook_id, source_id, now, now),
        )
    return notebook_id, source_id


def test_backfill_resumes_by_object_cursor_and_is_idempotent(repo):
    notebook_id, source_id = _seed(repo)
    # The production command builds this reverse index once before source pages.
    repo.maintenance.clear_source_index(notebook_id)
    cursor = ""
    while True:
        count, _written, cursor = repo.maintenance.backfill_source_index_batch(
            notebook_id, cursor, 1
        )
        if not count:
            break
    repo.maintenance.mark_source_index_backfilled(notebook_id)

    first = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert first["status"] == "running"
    assert first["objects_scanned"] == 1
    second = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert second["status"] == "running"
    final = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert final["status"] == "complete"
    assert final["facts_written"] == 2

    retry = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert retry["status"] == "complete"
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_source_facts WHERE source_id=?",
            (source_id,),
        ).fetchone()[0] == 2


def test_backfill_marks_ambiguous_payload_incomplete(repo):
    notebook_id, source_id = _seed(repo, objects=1)
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET evidence=? WHERE id='ko-0'",
            (json.dumps([
                {"source_id": source_id, "element_id": "el-0"},
                {"source_id": "src-other", "element_id": "el-other"},
            ]),),
        )
    result = run_backfill_source_facts(repo, notebook_id)
    assert result["failed"] == 0
    assert result["incomplete_objects"] == 1
    with repo._connect() as db:
        state = db.execute(
            "SELECT status,incomplete_objects,incomplete_reason,failure_code "
            "FROM knowledge_source_fact_backfills "
            "WHERE source_id=?", (source_id,)
        ).fetchone()
        fact_count = db.execute(
            "SELECT COUNT(*) FROM knowledge_source_facts WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    assert dict(state) == {
        "status": "incomplete",
        "incomplete_objects": 1,
        "incomplete_reason": "mixed_source_evidence",
        "failure_code": "",
    }
    assert fact_count == 0


def test_new_extraction_generation_clears_partial_backfill(repo):
    notebook_id, source_id = _seed(repo)
    repo.maintenance.clear_source_index(notebook_id)
    repo.maintenance.backfill_source_index_batch(notebook_id, "", 10)
    repo.maintenance.mark_source_index_backfilled(notebook_id)
    first = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert first["facts_written"] == 1

    with repo._write() as db:
        repo._runtime.knowledge.begin_extraction_run(
            db, source_id, notebook_id, "run-next", _now()
        )
    busy = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert busy["status"] == "busy"
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_source_facts WHERE source_id=?", (source_id,)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_id,),
        ).fetchone()[0] == 0


def test_failed_backfill_keeps_cursor_and_resumes_without_rewriting_prefix(repo):
    notebook_id, source_id = _seed(repo)
    repo.maintenance.clear_source_index(notebook_id)
    repo.maintenance.backfill_source_index_batch(notebook_id, "", 10)
    repo.maintenance.mark_source_index_backfilled(notebook_id)

    first = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert first["status"] == "running"
    repo.maintenance.mark_source_fact_backfill_failed(source_id, "backfill_error")
    with repo._connect() as db:
        failed = db.execute(
            "SELECT status,failure_code,after_object_id,facts_written "
            "FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_id,),
        ).fetchone()
    assert dict(failed) == {
        "status": "failed",
        "failure_code": "backfill_error",
        "after_object_id": "ko-0",
        "facts_written": 1,
    }

    resumed = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=10
    )
    assert resumed["status"] == "complete"
    assert resumed["facts_written"] == 2
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_source_facts WHERE source_id=?",
            (source_id,),
        ).fetchone()[0] == 2


def test_failure_after_incomplete_page_preserves_projection_reason(repo):
    notebook_id, source_id = _seed(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET evidence='[]' WHERE id='ko-0'")
    repo.maintenance.clear_source_index(notebook_id)
    repo.maintenance.backfill_source_index_batch(notebook_id, "", 10)
    repo.maintenance.mark_source_index_backfilled(notebook_id)

    first = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=1
    )
    assert first["status"] == "running"
    assert first["incomplete_objects"] == 1
    repo.maintenance.mark_source_fact_backfill_failed(source_id, "backfill_error")
    with repo._connect() as db:
        failed = db.execute(
            "SELECT status,incomplete_reason,failure_code "
            "FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_id,),
        ).fetchone()
    assert dict(failed) == {
        "status": "failed",
        "incomplete_reason": "missing_evidence",
        "failure_code": "backfill_error",
    }

    resumed = repo.maintenance.backfill_source_fact_batch(
        notebook_id, source_id, batch_size=10
    )
    assert resumed["status"] == "incomplete"
    with repo._connect() as db:
        terminal = db.execute(
            "SELECT incomplete_reason,failure_code "
            "FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_id,),
        ).fetchone()
    assert dict(terminal) == {
        "incomplete_reason": "missing_evidence",
        "failure_code": "",
    }


def test_source_owned_object_without_evidence_is_audited_as_incomplete(repo):
    notebook_id, source_id = _seed(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET evidence='[]' WHERE id='ko-1'")
        now = _now()
        repo._runtime.knowledge.insert_source_fact_rows(
            db,
            [
                (
                    "ksf-live", notebook_id, source_id, "run-history", "local-0",
                    "ko-0", "concept", json.dumps({"name": "object 0"}),
                    json.dumps([{"source_id": source_id, "element_id": "el-0"}]),
                    1, now, now,
                ),
                (
                    "ksf-live-folded", notebook_id, source_id, "run-history", "local-1",
                    "ko-0", "concept", json.dumps({"name": "object 0 alias"}),
                    json.dumps([{"source_id": source_id, "element_id": "el-0"}]),
                    1, now, now,
                ),
            ],
            [
                ("ksf-live", notebook_id, source_id, "run-history", "el-0", now),
                (
                    "ksf-live-folded", notebook_id, source_id,
                    "run-history", "el-0", now,
                ),
            ],
        )
    result = run_backfill_source_facts(repo, notebook_id)
    assert result["facts_written"] == 2
    assert result["incomplete_objects"] == 1
    with repo._connect() as db:
        state = db.execute(
            "SELECT status,objects_scanned,facts_written,incomplete_objects,"
            "incomplete_reason,failure_code "
            "FROM knowledge_source_fact_backfills WHERE source_id=?",
            (source_id,),
        ).fetchone()
    assert dict(state) == {
        "status": "incomplete",
        "objects_scanned": 2,
        "facts_written": 2,
        "incomplete_objects": 1,
        "incomplete_reason": "missing_evidence",
        "failure_code": "",
    }


def test_orphaned_live_fact_with_legacy_prefix_is_preserved_and_counted(repo):
    notebook_id, source_id = _seed(repo, objects=1)
    now = _now()
    with repo._write() as db:
        repo._runtime.knowledge.insert_source_fact_rows(
            db,
            [(
                "ksf-orphan-live", notebook_id, source_id, "run-history",
                "legacy:legitimate-live-id", "ko-deleted", "concept",
                json.dumps({"name": "orphan live"}),
                json.dumps([{"source_id": source_id, "element_id": "el-0"}]),
                1, now, now,
            )],
            [(
                "ksf-orphan-live", notebook_id, source_id,
                "run-history", "el-0", now,
            )],
        )

    result = run_backfill_source_facts(repo, notebook_id)
    assert result["facts_written"] == 2
    assert result["incomplete_objects"] == 0
    with repo._connect() as db:
        facts = db.execute(
            "SELECT local_object_id,projection_origin FROM knowledge_source_facts "
            "WHERE source_id=? ORDER BY projection_origin,local_object_id",
            (source_id,),
        ).fetchall()
    assert [(row["local_object_id"], row["projection_origin"]) for row in facts] == [
        ("legacy:ko-0", "historical"),
        ("legacy:legitimate-live-id", "live"),
    ]


def test_historical_local_id_collision_fails_closed_without_deleting_live(repo):
    notebook_id, source_id = _seed(repo, objects=1)
    now = _now()
    with repo._write() as db:
        repo._runtime.knowledge.insert_source_fact_rows(
            db,
            [(
                "ksf-colliding-live", notebook_id, source_id, "run-history",
                "legacy:ko-0", "ko-deleted", "concept",
                json.dumps({"name": "colliding live"}),
                json.dumps([{"source_id": source_id, "element_id": "el-0"}]),
                1, now, now,
            )],
            [(
                "ksf-colliding-live", notebook_id, source_id,
                "run-history", "el-0", now,
            )],
        )

    result = run_backfill_source_facts(repo, notebook_id)
    assert result["facts_written"] == 1
    assert result["incomplete_objects"] == 1
    with repo._connect() as db:
        state = db.execute(
            "SELECT incomplete_reason FROM knowledge_source_fact_backfills "
            "WHERE source_id=?", (source_id,),
        ).fetchone()
        facts = db.execute(
            "SELECT id,projection_origin FROM knowledge_source_facts WHERE source_id=?",
            (source_id,),
        ).fetchall()
    assert state["incomplete_reason"] == "local_id_collision"
    assert [(row["id"], row["projection_origin"]) for row in facts] == [
        ("ksf-colliding-live", "live")
    ]


def test_later_non_kg_run_does_not_hide_valid_kg_generation(repo):
    notebook_id, source_id = _seed(repo, objects=1)
    with repo._write() as db:
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-paper',?,?,'paper_meta','running','',"
            "'9999-12-31T23:59:59Z','9999-12-31T23:59:59Z')",
            (notebook_id, source_id),
        )
    result = run_backfill_source_facts(repo, notebook_id)
    assert result["facts_written"] == 1
    assert result["failed"] == 0


def test_completed_reverse_index_is_reused_on_fact_retry(repo, monkeypatch):
    notebook_id, _source_id = _seed(repo, objects=1)
    first = run_backfill_source_facts(repo, notebook_id)
    assert first["facts_written"] == 1

    def fail_if_rebuilt(_notebook_id):
        raise AssertionError("completed reverse index must be reused")

    monkeypatch.setattr(repo.maintenance, "clear_source_index", fail_if_rebuilt)
    second = run_backfill_source_facts(repo, notebook_id)
    assert second["facts_written"] == 1
