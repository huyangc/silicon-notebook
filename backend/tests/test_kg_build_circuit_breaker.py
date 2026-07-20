import json
import threading

import httpx
import pytest
from openai import APIConnectionError

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.kg_build_job_store import KgBuildAlreadyRunning
from app.services.embedding import FakeEmbedder
from app.services.kg import scheduler as kg_scheduler
from app.services.kg.run_control import KgBuildAborted
from app.services.sqlite_repository import SQLiteRepository


class _ControlledKgClient:
    configured = True
    model = "test-kg"

    def __init__(self, *, fail_after_successful_sources=None, fail_probe=False):
        self.fail_after_successful_sources = fail_after_successful_sources
        self.fail_probe = fail_probe
        self.lock = threading.Lock()
        self.probes = 0
        self.source_calls = 0

    @staticmethod
    def _connection_error():
        return APIConnectionError(
            request=httpx.Request(
                "POST", "https://model.example/chat/completions"
            )
        )

    def chat_json(self, messages, response_schema_hint, **kwargs):
        prompt = messages[0]["content"]
        if prompt.startswith('Return {"ok":true}'):
            with self.lock:
                self.probes += 1
            if self.fail_probe:
                raise self._connection_error()
            return '{"ok":true}'

        with self.lock:
            self.source_calls += 1
            source_call = self.source_calls
        if (
            self.fail_after_successful_sources is not None
            and source_call > self.fail_after_successful_sources
        ):
            raise self._connection_error()
        return json.dumps(
            {
                "nodes": [
                    {
                        "local_id": "engram",
                        "type": "Concept",
                        "name": "Engram",
                        "ev": 0,
                    }
                ],
                "edges": [],
            }
        )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'kg.db'}")
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage")
    )
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings(_env_file=None)
    settings.kg_llm_max_retries = 0
    settings.paper_meta_enabled = False
    settings.kg_refine_enabled = False
    settings.kg_gleaning_enabled = False
    settings.kg_conflict_resolution_enabled = False
    settings.kg_relink_enabled = False
    result = SQLiteRepository(settings)
    result.embedder = FakeEmbedder(dim=settings.embed_dim)
    kg_scheduler.configure(window_workers=1, job_workers=1)
    try:
        yield result
    finally:
        kg_scheduler.reset()


def _seed_three_parsed_sources(repo):
    notebook = repo.create_notebook(NotebookCreate(name="KG circuit"))
    now = "2026-07-20T00:00:00"
    source_ids = [f"source-{index}" for index in range(3)]
    with repo._write() as db:
        for index, source_id in enumerate(source_ids):
            db.execute(
                """
                INSERT INTO sources
                (id, notebook_id, title, source_type, status, parse_status,
                 file_name, file_path, file_size, file_hash, summary, doc_type,
                 created_at, updated_at)
                VALUES (?, ?, ?, 'markdown', 'parsed', 'parsed',
                        ?, '', 0, ?, '', 'academic_paper', ?, ?)
                """,
                (
                    source_id,
                    notebook.id,
                    f"Source {index}",
                    f"source-{index}.md",
                    f"hash-{index}",
                    now,
                    now,
                ),
            )
            db.execute(
                """
                INSERT INTO source_elements
                (id, source_id, element_type, location_label, text, metadata,
                 created_at)
                VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)
                """,
                (
                    f"element-{index}",
                    source_id,
                    f"Engram is a technical memory architecture for source {index}.",
                    now,
                ),
            )
    return notebook, source_ids


def _source_statuses(repo, source_ids):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, status FROM sources WHERE id IN (?, ?, ?)",
            tuple(source_ids),
        ).fetchall()
    return {row["id"]: row["status"] for row in rows}


def _kg_source_ids(repo, notebook_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT source_id FROM knowledge_objects "
            "WHERE notebook_id=? AND source_id!=''",
            (notebook_id,),
        ).fetchall()
    return {row["source_id"] for row in rows}


def test_model_outage_preserves_completed_source_and_stops_remaining(repo):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    client = _ControlledKgClient(fail_after_successful_sources=1)
    repo._kg_llm_client = client
    job = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "incremental"
        )

    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["stage"] == "finished"
    assert saved["error_code"] == "model_unavailable"
    assert saved["completed_sources"] == 1
    assert saved["failed_sources"] == 0
    assert repo.get_notebook(notebook.id).kg_pending_sources == 2
    statuses = _source_statuses(repo, source_ids)
    assert "extracting" not in statuses.values()
    assert statuses[source_ids[0]] == "extracted"
    assert statuses[source_ids[1]] == "parsed"
    assert statuses[source_ids[2]] == "parsed"
    assert _kg_source_ids(repo, notebook.id) == {source_ids[0]}


def test_failed_rebuild_continues_incrementally_without_second_delete(
    repo, monkeypatch
):
    notebook, source_ids = _seed_three_parsed_sources(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    real_delete = lifecycle.delete_notebook_kg
    delete_calls = []

    def tracked_delete(notebook_id):
        delete_calls.append(notebook_id)
        return real_delete(notebook_id)

    monkeypatch.setattr(lifecycle, "delete_notebook_kg", tracked_delete)
    repo._kg_llm_client = _ControlledKgClient(
        fail_after_successful_sources=1
    )
    rebuild = repo.prepare_notebook_kg_job(notebook.id, "rebuild")
    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, rebuild["id"], "rebuild"
        )
    assert delete_calls == [notebook.id]
    assert _kg_source_ids(repo, notebook.id) == {source_ids[0]}

    delete_calls.clear()
    repo._kg_llm_client = _ControlledKgClient()
    continuation = repo.prepare_notebook_kg_job(notebook.id, "incremental")
    result = repo.execute_notebook_kg_job(
        notebook.id, continuation["id"], "incremental"
    )

    assert delete_calls == []
    assert result["job_id"] == continuation["id"]
    assert sorted(result["built"]) == sorted(source_ids[1:])
    assert _kg_source_ids(repo, notebook.id) == set(source_ids)


def test_rebuild_probe_failure_happens_before_delete(repo, monkeypatch):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    repo._kg_llm_client = _ControlledKgClient(fail_probe=True)
    lifecycle = repo._runtime.knowledge_lifecycle
    delete_calls = []
    monkeypatch.setattr(
        lifecycle,
        "delete_notebook_kg",
        lambda notebook_id: delete_calls.append(notebook_id),
    )
    job = repo.prepare_notebook_kg_job(notebook.id, "rebuild")

    with pytest.raises(KgBuildAborted):
        repo.execute_notebook_kg_job(
            notebook.id, job["id"], "rebuild"
        )

    assert delete_calls == []
    saved = repo._runtime.kg_build_jobs.get(job["id"])
    assert saved["status"] == "failed"
    assert saved["error_code"] == "model_unavailable"


def test_duplicate_preparation_never_enters_executor(repo):
    notebook, _source_ids = _seed_three_parsed_sources(repo)
    repo._kg_llm_client = _ControlledKgClient()
    first = repo.prepare_notebook_kg_job(notebook.id, "incremental")

    with pytest.raises(KgBuildAlreadyRunning):
        repo.prepare_notebook_kg_job(notebook.id, "incremental")

    assert first["status"] == "running"
    assert repo._runtime.kg_build_jobs.get(first["id"])["status"] == "running"
