"""Navigation-page marker on SQLite (v101): build write, compare-and-set store
methods, the post-readiness background pass, the pipeline-switch publish and
the CLI. Shared cases in ``tests/navigation_backfill_cases.py``; PostgreSQL
twin ``tests/postgres/test_navigation_backfill_pg.py``."""
from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings
from app.domain.indexing_pipeline import (
    IndexingPipelineChunkResult,
    IndexingPipelineOption,
)
from app.extension_sdk import IndexingChunkProposal
from app.services import background_jobs, navigation_backfill, navigation_backfill_cli
from app.services.sqlite_repository import SQLiteRepository
from tests import navigation_backfill_cases as cases

Q = "?"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nav.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = SQLiteRepository(Settings(_env_file=None))
    try:
        yield repository
    finally:
        repository.close()


def test_build_writes_flags_and_stamp(repo):
    cases.case_build_writes_flags_and_stamp(repo, Q)


def test_build_with_detection_off_writes_zero_and_no_stamp(repo, monkeypatch):
    cases.case_build_with_detection_off_writes_zero_and_no_stamp(repo, Q, monkeypatch)


def test_replace_compare_and_set(repo):
    cases.case_replace_compare_and_set(repo, Q)


def test_build_redoes_under_the_lock_when_the_generation_moved(repo, monkeypatch):
    cases.case_build_redoes_under_the_lock_when_the_generation_moved(repo, Q, monkeypatch)


def test_backfill_judges_pending_sources_and_resumes(repo, monkeypatch):
    cases.case_backfill_judges_pending_sources_and_resumes(repo, Q, monkeypatch)


def test_backfill_abandons_a_source_whose_generation_moved(repo, monkeypatch):
    cases.case_backfill_abandons_a_source_whose_generation_moved(repo, Q, monkeypatch)


def test_backfill_retries_busy_sources_once(repo, monkeypatch):
    cases.case_backfill_retries_busy_sources_once(repo, Q, monkeypatch)


def test_backfill_runs_one_page_per_job(repo, monkeypatch):
    cases.case_backfill_runs_one_page_per_job(repo, Q, monkeypatch)


def test_backfill_reads_headings_only(repo, monkeypatch):
    cases.case_backfill_reads_headings_only(repo, Q, monkeypatch)


def test_backfill_switch_off_and_offline_batch(repo, monkeypatch):
    cases.case_backfill_switch_off_and_offline_batch(repo, Q, monkeypatch)


def test_clear_resets_flags_and_stamps(repo):
    cases.case_clear_resets_flags_and_stamps(repo, Q)


def test_mirrored_notebooks_are_left_alone(repo, monkeypatch):
    cases.case_mirrored_notebooks_are_left_alone(repo, Q, monkeypatch)


def test_the_service_pass_holds_the_source_chunk_lock(repo, monkeypatch):
    """The wired pass takes the ingestion service's per-source chunk lock
    without waiting: while a build of the source holds it, the source is left
    for that build (it stamps the source itself)."""
    _notebook_id, source_id = cases._pending_source(repo, Q, monkeypatch)
    ingestion = repo._runtime.source_ingestion
    service = cases.backfill(repo)
    # The source chunk lock is a plain (non-reentrant) Lock, so the pass's
    # no-wait acquire on this same thread fails exactly as it would elsewhere.
    with ingestion.hold_source_chunk_lock(source_id):
        result, _jobs = cases.drain(service)
    assert (result.busy, result.judged) == (1, 0)
    assert cases.drain(service)[0].judged == 1


def test_the_pass_is_a_light_maintenance_job():
    assert ("navigation-backfill", "navigation-backfill") in background_jobs._SAFE_JOB_PREFIXES
    assert "navigation-backfill" in background_jobs._LIGHT_MAINTENANCE_OPERATIONS
    assert background_jobs._maintenance_executor(navigation_backfill.JOB_NAME) is not None


def test_startup_wires_the_post_readiness_pass():
    import inspect

    from app.services import startup_warmup

    source = inspect.getsource(startup_warmup)
    assert "_backfill_navigation_chunks(repo)" in source
    assert "schedule_navigation_backfill" in inspect.getsource(
        startup_warmup._backfill_navigation_chunks
    )


def test_skipped_source_emits_a_content_free_event(repo, monkeypatch):
    from app.services import navigation_chunks

    monkeypatch.setattr(navigation_chunks, "NAV_MAX_TITLES", 3)
    events = []
    monkeypatch.setattr(repo._runtime.source_chunking, "event_log", type(
        "Log", (), {"emit": staticmethod(events.append)})())
    notebook_id = cases.create_notebook(repo)
    source_id = cases.seed_source(repo, notebook_id)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert events == [{
        "kind": "navigation_detection_skipped", "notebook_id": notebook_id,
        "source_id": source_id, "stage": "chunking",
        "reason": "too_many_titles", "title_count": len(cases.TITLES) + 1,
    }]
    assert set(cases.chunk_flags(repo, source_id, Q).values()) == {0}
    # judged (skipped is a decision): stamped, not pending
    chunked_at, scanned_at = cases.stamps(repo, source_id, Q)
    assert scanned_at == chunked_at


def _recording_log(repo, monkeypatch) -> list:
    events: list = []
    monkeypatch.setattr(repo._runtime.source_chunking, "event_log", type(
        "Log", (), {"emit": staticmethod(events.append)})())
    return events


def test_a_redo_after_a_moved_generation_reports_the_skip_once(repo, monkeypatch):
    from app.services import navigation_chunks

    monkeypatch.setattr(navigation_chunks, "NAV_MAX_TITLES", 3)
    events = _recording_log(repo, monkeypatch)
    notebook_id = cases.create_notebook(repo)
    source_id = cases.seed_source(repo, notebook_id)
    store = repo._runtime.chunk_store
    real_replace = store.replace_source_chunks
    writes = []

    def first_write_loses(*args, **kwargs):
        if not writes:
            cases.bump_chunked_at(repo, source_id, Q)
        writes.append(1)
        return real_replace(*args, **kwargs)

    monkeypatch.setattr(store, "replace_source_chunks", first_write_loses)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert len(writes) == 2  # refused, then redone under the lock
    assert [event["kind"] for event in events] == ["navigation_detection_skipped"]


def test_a_detector_failure_never_fails_the_build(repo, monkeypatch):
    """Fail-open: the chunks are written unflagged and unjudged (the
    background pass retries), and the event names only the exception class."""
    from app.services import source_chunking

    def broken(*_args, **_kwargs):
        raise RuntimeError("detector exploded: secret source text")

    monkeypatch.setattr(source_chunking, "detect_navigation_chunks", broken)
    events = _recording_log(repo, monkeypatch)
    notebook_id = cases.create_notebook(repo)
    source_id = cases.seed_source(repo, notebook_id)
    repo._runtime.source_chunking.build_chunks_for_source(source_id)
    assert cases.chunk_count(repo, source_id, Q) == len(cases.expected_flags())
    assert set(cases.chunk_flags(repo, source_id, Q).values()) == {0}
    chunked_at, scanned_at = cases.stamps(repo, source_id, Q)
    assert chunked_at is not None and scanned_at is None
    assert events == [{
        "kind": "navigation_detection_failed", "notebook_id": notebook_id,
        "source_id": source_id, "stage": "chunking", "error_class": "RuntimeError",
    }]
    assert repo._runtime.chunk_store.navigation_pending_count() == 1


# ------------------------------------------------- pipeline-switch publish
class _PipelineHost:
    option_row = IndexingPipelineOption(
        pipeline_id="test.pipeline", label="Test pipeline",
        description="one chunk per element", version="v1",
        overrides_chunking=True, overrides_kg_extraction=False, available=True,
    )

    def options(self):
        return (self.option_row,)

    def option(self, pipeline_id):
        return self.option_row if pipeline_id == self.option_row.pipeline_id else None

    def build_chunks(self, pipeline_id, elements, **_kwargs):
        return IndexingPipelineChunkResult(tuple(
            IndexingChunkProposal(
                text=f"plugin:{item['text']}", element_ids=(str(item["id"]),),
                section_path=str(item.get("section_path") or ""),
            )
            for item in elements if item.get("text")
        ))


def test_pipeline_switch_stages_and_publishes_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pipe.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repo = SQLiteRepository(Settings(_env_file=None), indexing_pipeline_host=_PipelineHost())
    try:
        notebook_id = cases.create_notebook(repo)
        source_id = cases.seed_source(repo, notebook_id)
        repo._runtime.source_chunking.build_chunks_for_source(source_id)
        staged = []
        stage_store = repo._runtime.kg_build_jobs
        real_stage = stage_store.stage_indexing_pipeline_chunks

        def spy_stage(job_id, source, payload):
            staged.append(payload)
            return real_stage(job_id, source, payload)

        monkeypatch.setattr(stage_store, "stage_indexing_pipeline_chunks", spy_stage)
        result = repo._runtime.indexing_pipeline.begin(notebook_id, "test.pipeline")
        identity = (str(result["_pipeline_id"]), str(result["_pipeline_version"]),
                    str(result["_pipeline_generation"]))
        job = repo._runtime.knowledge_lifecycle.prepare_notebook_kg_job(
            notebook_id, "rebuild", allow_without_model=True)
        assert repo._runtime.notebook_store.attach_indexing_pipeline_job(
            notebook_id, identity[2], job["id"])
        repo._runtime.indexing_pipeline.rebuild(
            notebook_id, job_id=job["id"], pipeline_id=identity[0],
            pipeline_version=identity[1], pipeline_generation=identity[2])
        assert staged and staged[0]["navigation_scanned"] is True
        flagged = [row for row in staged[0]["rows"] if row["navigation"]]
        assert len(flagged) == 1 and flagged[0]["text"].startswith("plugin:1 Introduction")
        repo._runtime.knowledge_lifecycle.finish_indexing_pipeline_job(
            notebook_id, job["id"], succeeded=True, pipeline_identity=identity)
        with repo._connect() as db:
            rows = db.execute(
                "SELECT text, navigation FROM chunks WHERE source_id=?", (source_id,)
            ).fetchall()
        assert {r["text"].startswith("plugin:") for r in rows} == {True}
        assert [r["text"][:22] for r in rows if r["navigation"]] == ["plugin:1 Introduction "]
        chunked_at, scanned_at = cases.stamps(repo, source_id, Q)
        assert scanned_at == chunked_at
    finally:
        repo.close()


def test_a_stage_written_before_the_marker_publishes_unflagged_and_unstamped(repo):
    """A payload without ``navigation`` / ``navigation_scanned`` (staged by the
    previous build, published after the upgrade) reads as unflagged and leaves
    the source for the background pass."""
    store = repo._runtime.kg_build_jobs
    validate = getattr(type(store), "_validate_stage_payloads")
    source_id = "src-old"
    payload = {
        "chunks": {
            "created_at": "2026-10-21T00:00:00+00:00",
            "rows": [{"id": "ck-old", "source_id": source_id, "text": "t",
                      "section_path": "", "element_ids": []}],
            "vectors": [],
        },
        "kg": {"mode": "preserve"},
    }
    with repo._connect() as db:
        validate(db, [(source_id, payload)])
    payload["chunks"]["rows"][0]["navigation"] = 1  # not a bool
    with repo._connect() as db, pytest.raises(ValueError):
        validate(db, [(source_id, payload)])


# ---------------------------------------------------------------- the CLI
@pytest.fixture
def cli_env(repo, tmp_path, monkeypatch):
    # The CLI composes its own repository from the environment (same file).
    get_settings.cache_clear()
    yield repo
    get_settings.cache_clear()


def test_cli_dry_run_writes_nothing_and_lists_sections(cli_env, monkeypatch, capsys):
    repo = cli_env
    _notebook_id, source_id = cases._pending_source(repo, Q, monkeypatch)
    before = cases.chunk_flags(repo, source_id, Q), cases.stamps(repo, source_id, Q)
    code = navigation_backfill_cli.main(["--all-notebooks", "--dry-run", "--show-sections"])
    output = capsys.readouterr().out
    assert code == 0
    assert f"source={source_id}" in output and "flagged=1 changed=1" in output
    assert "  section: Contents" in output
    assert '"judged": 1' in output
    assert (cases.chunk_flags(repo, source_id, Q), cases.stamps(repo, source_id, Q)) == before


def test_cli_rerun_writes_and_clear_rolls_back(cli_env, monkeypatch, capsys):
    repo = cli_env
    notebook_id, source_id = cases._pending_source(repo, Q, monkeypatch)
    assert navigation_backfill_cli.main(["--notebook-id", notebook_id]) == 0
    assert cases.chunk_flags(repo, source_id, Q) == cases.expected_flags()
    assert cases.stamps(repo, source_id, Q)[1] is not None
    # nothing pending: a re-run judges nothing; --rescan judges it again
    assert navigation_backfill_cli.main(["--notebook-id", notebook_id]) == 0
    assert '"judged": 0' in capsys.readouterr().out
    assert navigation_backfill_cli.main(["--notebook-id", notebook_id, "--rescan"]) == 0
    assert '"judged": 1' in capsys.readouterr().out
    assert navigation_backfill_cli.main(["--all-notebooks", "--clear"]) == 0
    assert '"chunks_cleared": 1' in capsys.readouterr().out
    assert set(cases.chunk_flags(repo, source_id, Q).values()) == {0}
    assert cases.stamps(repo, source_id, Q)[1] is None


def test_cli_exit_codes(cli_env, monkeypatch, capsys):
    repo = cli_env
    _notebook_id, source_id = cases._pending_source(repo, Q, monkeypatch)
    with pytest.raises(SystemExit) as usage:
        navigation_backfill_cli.main(["--dry-run"])  # no scope
    assert usage.value.code == 2
    with pytest.raises(SystemExit) as usage:
        navigation_backfill_cli.main(["--all-notebooks", "--show-sections"])
    assert usage.value.code == 2
    # a write with detection switched off is refused (a dry run is not)
    monkeypatch.setenv("NAVIGATION_DETECTION_ENABLED", "false")
    assert navigation_backfill_cli.main(["--all-notebooks"]) == 2
    assert navigation_backfill_cli.main(["--all-notebooks", "--dry-run"]) == 0
    monkeypatch.delenv("NAVIGATION_DETECTION_ENABLED")
    # a source abandoned under the pass: exit 1, still pending
    from app.repositories.sqlite.chunk_store import ChunkStore

    monkeypatch.setattr(ChunkStore, "apply_navigation_flags", lambda self, *a, **k: False)
    assert navigation_backfill_cli.main(["--all-notebooks"]) == 1
    assert cases.stamps(repo, source_id, Q)[1] is None
