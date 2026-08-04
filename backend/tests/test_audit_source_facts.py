import importlib.util
import pathlib
import sys

import pytest

from app.core.config import Settings
from app.models.notebooks import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "audit_source_facts.py"
SPEC = importlib.util.spec_from_file_location("audit_source_facts", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["audit_source_facts"] = module
SPEC.loader.exec_module(module)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'audit.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_audit_reports_only_counts_and_ids(repo):
    notebook = repo.create_notebook(NotebookCreate(name="audit"))
    now = repo._runtime.seams.now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('src-audit',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (notebook.id, "SECRET TITLE", "markdown", "ready", "parsed", "s.md",
             "s.md", 1, "hash", "", "", now, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES "
            "('ko-audit',?,'concept','approved','','{}','[]','src-audit',?,?)",
            (notebook.id, now, now),
        )
        db.execute(
            "INSERT INTO knowledge_source_fact_backfills "
            "(source_id,notebook_id,source_generation,projection_version,status,"
            "after_object_id,objects_scanned,facts_written,incomplete_objects,"
            "failure_code,created_at,updated_at) "
            "VALUES ('src-audit',?,'run-1',1,'incomplete','ko-audit',1,0,1,"
            "'incomplete:missing_evidence',?,?)",
            (notebook.id, now, now),
        )
        db.execute(
            "INSERT INTO extraction_runs "
            "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES ('run-1',?, 'src-audit','kg','completed',"
            "'kg objects=1 relations=0',?,?)",
            (notebook.id, now, now),
        )
    with repo._connect() as db:
        report = module.audit(db, "?", notebook.id, 10)
    assert report["status_counts"] == {"incomplete": 1}
    assert report["sources"] == 1
    assert report["integrity_mismatches"] == 0
    assert report["sample_source_ids"] == {"incomplete": ["src-audit"]}
    assert "SECRET TITLE" not in str(report)
    assert "evidence" not in str(report).lower()


def test_audit_detects_count_and_generation_drift(repo):
    notebook = repo.create_notebook(NotebookCreate(name="drift"))
    now = repo._runtime.seams.now()
    with repo._write() as db:
        for source_id, generation in (
            ("src-count", "run-count"),
            ("src-gen", "run-old"),
            ("src-projection", "run-projection"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,created_at,updated_at) "
                "VALUES (?,?,?,'markdown','ready','parsed','s.md','s.md',1,?,'','',?,?)",
                (source_id, notebook.id, source_id, source_id, now, now),
            )
            current_generation = "run-new" if source_id == "src-gen" else generation
            db.execute(
                "INSERT INTO extraction_runs "
                "(id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
                "VALUES (?,?,?,'kg','completed','kg objects=1 relations=0',?,?)",
                (current_generation, notebook.id, source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_source_fact_backfills "
                "(source_id,notebook_id,source_generation,projection_version,status,"
                "after_object_id,objects_scanned,facts_written,incomplete_objects,"
                "failure_code,created_at,updated_at) "
                "VALUES (?,?,?,?, 'complete','',?,?,0,'',?,?)",
                (
                    source_id,
                    notebook.id,
                    generation,
                    2 if source_id == "src-projection" else 1,
                    0 if source_id == "src-projection" else 1,
                    0 if source_id == "src-projection" else 1,
                    now,
                    now,
                ),
            )
    with repo._connect() as db:
        report = module.audit(db, "?", notebook.id, 10)
    assert report["status_counts"] == {
        "count_drift": 1,
        "generation_drift": 1,
        "projection_version_drift": 1,
    }
    assert report["sources"] == 3
    assert report["integrity_mismatches"] == 3
