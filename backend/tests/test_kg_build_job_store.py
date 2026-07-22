import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite.kg_build_job_store import (
    KgBuildAlreadyRunning,
    KgBuildJobStore,
)
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'jobs.db'}",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture
def notebook(repo):
    return repo.create_notebook(NotebookCreate(name="KG jobs"))


@pytest.fixture
def store(repo):
    return KgBuildJobStore(
        repo._runtime.database,
        new_id=repo._runtime.seams.new_id,
        now=repo._runtime.seams.now,
    )


def test_create_job_is_single_flight(store, notebook):
    first = store.create_job(
        notebook.id,
        "user-local",
        "incremental",
        8,
    )
    assert first["status"] == "running"
    assert first["stage"] == "probing"
    with pytest.raises(KgBuildAlreadyRunning):
        store.create_job(
            notebook.id,
            "user-local",
            "rebuild",
            8,
        )


def test_progress_and_terminal_update_require_running_job(store, notebook):
    job = store.create_job(
        notebook.id,
        "user-local",
        "incremental",
        3,
    )
    assert store.record_source_result(job["id"], succeeded=True) is True
    assert store.record_source_result(job["id"], succeeded=False) is True
    assert store.finish(job["id"], "succeeded") is True
    assert store.record_source_result(job["id"], succeeded=True) is False
    assert store.set_stage(job["id"], "extracting") is False
    assert store.finish(job["id"], "failed") is False
    saved = store.get(job["id"])
    assert (saved["completed_sources"], saved["failed_sources"]) == (1, 1)
    assert saved["status"] == "succeeded"
    assert saved["stage"] == "finished"
    assert saved["finished_at"]


def test_failed_terminal_job_allows_a_later_job(store, notebook):
    first = store.create_job(
        notebook.id,
        "user-local",
        "incremental",
        2,
    )
    assert store.finish(
        first["id"],
        "failed",
        error_code="model_unavailable",
        error_message="safe message",
    )
    second = store.create_job(
        notebook.id,
        "user-local",
        "incremental",
        1,
    )
    assert second["id"] != first["id"]
    assert store.latest(notebook.id)["id"] == second["id"]


def test_stage_failure_and_submission_failure_are_guarded(store, notebook):
    job = store.create_job(
        notebook.id,
        "user-local",
        "rebuild",
        4,
    )
    assert store.set_stage(
        job["id"],
        "stopping",
        error_code="model_unavailable",
        error_message="safe message",
    )
    stopping = store.get(job["id"])
    assert stopping["error_code"] == "model_unavailable"
    assert stopping["error_message"] == "safe message"
    assert store.fail_submission(job["id"]) is True
    failed = store.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "job_submission_failed"
    assert store.fail_submission(job["id"]) is False


def test_finish_rejects_unknown_terminal_status(store, notebook):
    job = store.create_job(
        notebook.id,
        "user-local",
        "incremental",
        1,
    )
    with pytest.raises(ValueError, match="terminal"):
        store.finish(job["id"], "cancelled")
    assert store.get(job["id"])["status"] == "running"


def test_get_missing_job_raises_key_error(store):
    with pytest.raises(KeyError):
        store.get("kgj-missing")


def test_restart_marks_running_kg_job_failed(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'restart.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    notebook = repo.create_notebook(
        NotebookCreate(name="Restart recovery")
    )
    store = KgBuildJobStore(
        repo._runtime.database,
        new_id=repo._runtime.seams.new_id,
        now=repo._runtime.seams.now,
    )
    job = store.create_job(
        notebook.id,
        repo.current_user().id,
        "incremental",
        4,
    )
    now = repo._runtime.seams.now()
    with repo._write() as db:
        db.execute(
            """
            INSERT INTO sources
            (id, notebook_id, title, source_type, status, parse_status,
             file_name, file_path, file_size, file_hash, summary, doc_type,
             created_at, updated_at)
            VALUES ('source-restart', ?, 'Interrupted', 'markdown',
                    'extracting', 'extracting', 'source.md', '', 0, '',
                    '', 'academic_paper', ?, ?)
            """,
            (notebook.id, now, now),
        )
        db.execute(
            """
            INSERT INTO sources
            (id, notebook_id, title, source_type, status, parse_status,
             file_name, file_path, file_size, file_hash, summary, doc_type,
             created_at, updated_at)
            VALUES ('source-before-run', ?, 'Interrupted before run',
                    'markdown', 'extracting', 'extracting', 'before.md', '',
                    0, '', '', 'academic_paper', ?, ?)
            """,
            (notebook.id, now, now),
        )
        db.execute(
            """
            INSERT INTO extraction_runs
            (id, notebook_id, source_id, run_type, status, error_message,
             created_at, updated_at)
            VALUES ('run-restart', ?, 'source-restart', 'kg', 'running',
                    '', ?, ?)
            """,
            (notebook.id, now, now),
        )

    # 服务端重启 = 构造(migrate + seed) + 显式清算。清算不再是构造的副作用
    # (离线 CLI 也构造仓储,没资格判定服务端的进行中行);服务端 lifespan 在
    # mark_ready() 之前调用它——见 tests/test_startup_recovery_ownership.py。
    restarted = SQLiteRepository(settings)
    restarted._recover_interrupted_jobs()
    restarted_store = KgBuildJobStore(
        restarted._runtime.database,
        new_id=restarted._runtime.seams.new_id,
        now=restarted._runtime.seams.now,
    )
    recovered = restarted_store.get(job["id"])
    assert recovered["status"] == "failed"
    assert recovered["stage"] == "finished"
    assert recovered["error_code"] == "worker_interrupted"
    assert recovered["finished_at"]
    with restarted._connect() as db:
        source = db.execute(
            "SELECT status, parse_status FROM sources "
            "WHERE id='source-restart'"
        ).fetchone()
        before_run = db.execute(
            "SELECT status, parse_status FROM sources "
            "WHERE id='source-before-run'"
        ).fetchone()
        run = db.execute(
            "SELECT status, error_message FROM extraction_runs "
            "WHERE id='run-restart'"
        ).fetchone()
    assert (source["status"], source["parse_status"]) == ("parsed", "parsed")
    assert (before_run["status"], before_run["parse_status"]) == (
        "parsed",
        "parsed",
    )
    assert run["status"] == "failed"
    assert "worker_interrupted" in run["error_message"]
