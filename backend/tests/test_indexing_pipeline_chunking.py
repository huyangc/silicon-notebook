from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.domain.indexing_pipeline import (
    IndexingPipelineChunkResult,
    IndexingPipelineOption,
    IndexingPipelineRebuildFailedError,
    IndexingPipelineStalePlanError,
    IndexingPipelineUnavailableError,
)
from app.extension_sdk import IndexingChunkProposal
from app.models.notebooks import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


class _PipelineHost:
    option_row = IndexingPipelineOption(
        pipeline_id="test.pipeline",
        label="Test pipeline",
        description="one chunk per element",
        version="v1",
        overrides_chunking=True,
        overrides_kg_extraction=False,
        available=True,
    )

    def options(self):
        return (self.option_row,)

    def option(self, pipeline_id):
        return self.option_row if pipeline_id == self.option_row.pipeline_id else None

    def build_chunks(self, pipeline_id, elements, **_kwargs):
        assert pipeline_id == self.option_row.pipeline_id
        return IndexingPipelineChunkResult(
            tuple(
                IndexingChunkProposal(
                    text=f"plugin:{item['text']}",
                    element_ids=(str(item["id"]),),
                    section_path=str(item.get("section_path") or ""),
                )
                for item in elements
                if item.get("text")
            )
        )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pipeline.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    return SQLiteRepository(
        Settings(_env_file=None), indexing_pipeline_host=_PipelineHost()
    )


def _insert_source(repo, notebook_id: str, text: str, *, source_type="document") -> str:
    source_id = f"src-{uuid4().hex[:10]}"
    element_id = f"el-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,file_name,file_path,file_size,"
            "file_hash,summary,doc_type,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                notebook_id,
                source_id,
                source_type,
                f"{source_id}.md",
                "",
                0,
                source_id,
                "",
                "",
                "extracted",
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (element_id, source_id, "paragraph", "p1", text, "{}", now),
        )
    return source_id


def _chunk_texts(repo, notebook_id: str) -> dict[str, list[str]]:
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_id,text FROM chunks WHERE notebook_id=? "
            "ORDER BY source_id,id",
            (notebook_id,),
        ).fetchall()
    output: dict[str, list[str]] = {}
    for row in rows:
        output.setdefault(str(row["source_id"]), []).append(str(row["text"]))
    return output


def _intent(repo, notebook_id: str) -> tuple[str, str, str]:
    result = repo._runtime.indexing_pipeline.begin(notebook_id, "test.pipeline")
    return (
        str(result["_pipeline_id"]),
        str(result["_pipeline_version"]),
        str(result["_pipeline_generation"]),
    )


def _attach_job(repo, notebook_id: str, generation: str) -> str:
    job = repo._runtime.knowledge_lifecycle.prepare_notebook_kg_job(
        notebook_id, "rebuild", allow_without_model=True
    )
    assert repo._runtime.notebook_store.attach_indexing_pipeline_job(
        notebook_id, generation, job["id"]
    )
    return str(job["id"])


def test_pending_blocks_writes_then_one_transaction_publishes_all_visible_sources(repo):
    notebook = repo.create_notebook(NotebookCreate(name="pipeline"))
    first = _insert_source(repo, notebook.id, "alpha")
    second = _insert_source(repo, notebook.id, "beta")
    hidden = _insert_source(repo, notebook.id, "private memory", source_type="memory")
    repo._build_chunks_for_source(first)
    repo._build_chunks_for_source(second)
    repo._build_chunks_for_source(hidden)
    before = _chunk_texts(repo, notebook.id)
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
            "object_type,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                "cluster-stale",
                notebook.id,
                "canonical-stale",
                "member-stale",
                "stale",
                "concept",
                _now(),
            ),
        )

    pipeline_id, version, generation = _intent(repo, notebook.id)
    job_id = _attach_job(repo, notebook.id, generation)

    with pytest.raises(IndexingPipelineUnavailableError):
        repo.require_indexing_pipeline_write(notebook.id)
    assert _chunk_texts(repo, notebook.id) == before

    repo._runtime.indexing_pipeline.rebuild(
        notebook.id,
        job_id=job_id,
        pipeline_id=pipeline_id,
        pipeline_version=version,
        pipeline_generation=generation,
    )
    # The bounded plan is durable but invisible until its success tail.
    assert _chunk_texts(repo, notebook.id) == before
    repo._runtime.knowledge_lifecycle.finish_indexing_pipeline_job(
        notebook.id,
        job_id,
        succeeded=True,
        pipeline_identity=(pipeline_id, version, generation),
    )
    after = _chunk_texts(repo, notebook.id)
    assert after[first] == ["plugin:alpha"]
    assert after[second] == ["plugin:beta"]
    # Hidden Memory/Knowhow products are core-owned and actor-independent:
    # the notebook switch neither enumerates nor deletes them.
    assert after[hidden] == before[hidden]
    with repo._connect() as db:
        assert db.execute(
            "SELECT 1 FROM concept_clusters WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone() is None
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert (state["published_pipeline_id"], state["published_pipeline_version"]) == (
        "test.pipeline",
        "v1",
    )
    assert state["pipeline_job_id"] == ""
    repo.require_indexing_pipeline_write(notebook.id)


def test_late_generation_rolls_back_without_mixing(repo):
    notebook = repo.create_notebook(NotebookCreate(name="stale"))
    first = _insert_source(repo, notebook.id, "old")
    repo._build_chunks_for_source(first)
    old_chunks = _chunk_texts(repo, notebook.id)
    pipeline_id, version, generation = _intent(repo, notebook.id)
    job_id = _attach_job(repo, notebook.id, generation)
    # A later revert owns a new opaque generation even if a worker from the
    # first request is still computing proposals (A→B / A→B→A safe).
    repo._runtime.indexing_pipeline.begin(notebook.id, None)

    with pytest.raises(IndexingPipelineStalePlanError):
        repo._runtime.indexing_pipeline.rebuild(
            notebook.id,
            job_id=job_id,
            pipeline_id=pipeline_id,
            pipeline_version=version,
            pipeline_generation=generation,
        )

    current = _chunk_texts(repo, notebook.id)
    assert current.get(first) == old_chunks[first]
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert state["published_pipeline_id"] == ""
    assert state["pipeline_id"] == ""
    repo._runtime.knowledge_lifecycle.finish_indexing_pipeline_job(
        notebook.id, job_id, succeeded=False
    )


def test_fully_staged_late_generation_is_discarded_without_live_mutation(repo):
    notebook = repo.create_notebook(NotebookCreate(name="late-stage"))
    source_id = _insert_source(repo, notebook.id, "old generation")
    repo._build_chunks_for_source(source_id)
    before = _chunk_texts(repo, notebook.id)
    pipeline_id, version, generation = _intent(repo, notebook.id)
    job_id = _attach_job(repo, notebook.id, generation)
    repo._runtime.indexing_pipeline.rebuild(
        notebook.id,
        job_id=job_id,
        pipeline_id=pipeline_id,
        pipeline_version=version,
        pipeline_generation=generation,
    )
    # A newer A→builtin selection takes authority after every payload is staged.
    repo._runtime.indexing_pipeline.begin(notebook.id, None)

    with pytest.raises(IndexingPipelineStalePlanError):
        repo._runtime.knowledge_lifecycle.finish_indexing_pipeline_job(
            notebook.id,
            job_id,
            succeeded=True,
            pipeline_identity=(pipeline_id, version, generation),
        )

    assert _chunk_texts(repo, notebook.id) == before
    assert repo._runtime.kg_build_jobs.get(job_id)["status"] == "failed"
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages WHERE job_id=?",
            (job_id,),
        ).fetchone()["c"] == 0


def test_whole_notebook_bound_failure_keeps_old_publication_pending(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'bounded.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    monkeypatch.setenv("INDEXING_PIPELINE_REBUILD_MAX_PROPOSALS", "1")
    bounded = SQLiteRepository(
        Settings(_env_file=None), indexing_pipeline_host=_PipelineHost()
    )
    notebook = bounded.create_notebook(NotebookCreate(name="bounded"))
    first = _insert_source(bounded, notebook.id, "one")
    second = _insert_source(bounded, notebook.id, "two")
    bounded._build_chunks_for_source(first)
    bounded._build_chunks_for_source(second)
    old_chunks = _chunk_texts(bounded, notebook.id)
    pipeline_id, version, generation = _intent(bounded, notebook.id)
    job_id = _attach_job(bounded, notebook.id, generation)

    with pytest.raises(IndexingPipelineRebuildFailedError):
        bounded._runtime.indexing_pipeline.rebuild(
            notebook.id,
            job_id=job_id,
            pipeline_id=pipeline_id,
            pipeline_version=version,
            pipeline_generation=generation,
        )

    assert _chunk_texts(bounded, notebook.id) == old_chunks
    state = bounded._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert state["pipeline_id"] == "test.pipeline"
    assert state["published_pipeline_id"] == ""
    bounded._runtime.knowledge_lifecycle.finish_indexing_pipeline_job(
        notebook.id, job_id, succeeded=False
    )


def test_facade_reuses_durable_rebuild_job_and_returns_before_worker(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="durable"))
    source_id = _insert_source(repo, notebook.id, "durable body")
    repo._build_chunks_for_source(source_id)
    submitted = {}

    def capture(function, *args, **kwargs):
        submitted.update(function=function, args=args, kwargs=kwargs)
        return object()

    from app.services import background_jobs

    monkeypatch.setattr(background_jobs, "submit", capture)

    response = repo.set_indexing_pipeline(notebook.id, "test.pipeline")

    assert response["pending"] is True
    assert response["rebuild_status"] == "pending"
    assert response["job_id"].startswith("kgj-")
    latest = repo._runtime.kg_build_jobs.latest(notebook.id)
    assert latest["id"] == response["job_id"]
    assert latest["mode"] == "rebuild"
    assert latest["status"] == "running"
    assert _chunk_texts(repo, notebook.id)[source_id] != ["plugin:durable body"]

    submitted["function"](*submitted["args"])

    assert repo._runtime.kg_build_jobs.latest(notebook.id)["status"] == "succeeded"
    assert _chunk_texts(repo, notebook.id)[source_id] == ["plugin:durable body"]
    state = repo._runtime.notebook_store.indexing_pipeline_state(notebook.id)
    assert (state["published_pipeline_id"], state["published_pipeline_version"]) == (
        "test.pipeline",
        "v1",
    )
    projection = repo.indexing_pipeline_options(notebook.id)
    assert projection["pending"] is False
    assert projection["rebuild_status"] == "idle"


def test_unattached_or_version_drifted_desired_generation_is_retryable(repo):
    notebook = repo.create_notebook(NotebookCreate(name="retryable"))
    first = repo._runtime.indexing_pipeline.begin(notebook.id, "test.pipeline")
    assert first["pending"] is True
    assert first["rebuild_status"] == "failed"
    first_generation = first["_pipeline_generation"]

    retried = repo._runtime.indexing_pipeline.begin(notebook.id, "test.pipeline")
    assert retried["changed"] is True
    assert retried["_pipeline_generation"] != first_generation

    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_version='v0',"
            "indexing_pipeline_job_id='' WHERE id=?",
            (notebook.id,),
        )
    projection = repo.indexing_pipeline_options(notebook.id)
    assert projection["pending"] is True
    assert projection["rebuild_status"] == "failed"


def test_direct_parse_and_incremental_extraction_fail_before_mutation(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="write admission"))
    source_id = _insert_source(repo, notebook.id, "body")
    repo._runtime.indexing_pipeline.begin(notebook.id, "test.pipeline")
    calls = []
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "parse_source_compat",
        lambda *_: calls.append("parse"),
    )
    monkeypatch.setattr(
        repo._runtime.source_ingestion,
        "run_extraction",
        lambda *_: calls.append("extract"),
    )

    with pytest.raises(IndexingPipelineUnavailableError):
        repo.parse_source(source_id)
    with pytest.raises(IndexingPipelineUnavailableError):
        repo.extract_source(source_id)
    assert calls == []


def test_scale_build_and_idle_recovery_fail_before_writer_claim(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="scale admission"))
    repo._runtime.indexing_pipeline.begin(notebook.id, "test.pipeline")
    scale = repo._runtime.scale_artifacts
    calls = []
    monkeypatch.setattr(scale, "build", lambda *_args, **_kwargs: calls.append("build"))
    monkeypatch.setattr(scale, "fold", lambda *_args, **_kwargs: calls.append("fold"))
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda *_args, **_kwargs: calls.append("daemon"),
    )

    with pytest.raises(IndexingPipelineUnavailableError):
        repo.build_scale_index(notebook.id)
    with pytest.raises(IndexingPipelineUnavailableError):
        repo.fold_scale_index_delta(notebook.id)

    queued = ("full", "2026-01-01T00:00:00.000000+00:00")
    scale.idle_queue[notebook.id] = queued
    scale._process_idle_queue(force=True)

    assert scale.idle_queue[notebook.id] == queued
    assert notebook.id not in scale.building
    assert calls == []
