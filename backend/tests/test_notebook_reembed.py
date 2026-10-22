"""Notebook re-embed job on SQLite.

The world and the backend-neutral checks live in ``tests/notebook_reembed_cases.py``;
PostgreSQL runs the same checks in ``tests/postgres/test_notebook_reembed_pg.py``. The
single-flight guard, the follow-up pass, the shared guard with ``backfill-vectors`` and
the background submission are process-local and backend-neutral, so they are pinned
here only.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services import background_jobs, notebook_reembed
from app.services.notebook_reembed import LEGS, NotebookReembedService, ReembedInFlight
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_reembed_cases as cases


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [
            json.dumps(values[c]) if isinstance(values[c], (dict, list)) else values[c]
            for c in columns
        ],
    )


BACKEND = cases.Backend(name="sqlite", insert=_insert, placeholder="?")


def _env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'reembed.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def test_every_vector_comes_back_once(repo):
    cases.check_every_vector_comes_back_once(repo, BACKEND)


def test_backfill_job_keeps_knowhow_chunks_on_their_workload(repo):
    cases.check_backfill_job_keeps_knowhow_chunks_on_their_workload(repo, BACKEND)


def test_object_vectors_mark_the_graph_dirty(repo):
    cases.check_object_vectors_mark_the_graph_dirty(repo, BACKEND)


def test_model_failure_is_visible_and_resumes(repo):
    cases.check_model_failure_is_visible_and_resumes(repo, BACKEND)


def test_unbound_workload_is_visible(repo):
    cases.check_unbound_workload_is_visible(repo, BACKEND)


# --- single flight, follow-up pass, shared guard ---------------------------------


class _Gate:
    """Blocks the first embed call of one workload until released."""

    def __init__(self, embedder) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        inner = embedder.embed_texts

        def _blocking(texts):
            if not self.entered.is_set():
                self.entered.set()
                assert self.release.wait(timeout=10)
            return inner(texts)

        embedder.embed_texts = _blocking


def _wait_finished(run_id: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = NotebookReembedService.status(run_id)
        if status is not None and status.state != "running":
            return status
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} never finished")


def test_a_second_request_joins_the_run_in_flight(repo):
    """While a run is in flight, a second request for the notebook starts nothing: legs
    the current pass runs are covered by it, the others run in one follow-up pass of the
    same run. Nothing embeds twice, and the guard is released only after that pass."""
    world = cases.build_world(repo, BACKEND)
    nb = world.notebook_id
    cases.delete_vectors(repo, BACKEND, nb)
    gate = _Gate(world.embedders["chunk_embedding"])
    service = NotebookReembedService(repo)

    first = service.submit(nb, legs=["chunks"])
    assert first.started
    assert gate.entered.wait(timeout=10)
    joined = service.submit(nb, legs=["chunks", "kg_objects"])
    with pytest.raises(ReembedInFlight):
        service.run(nb, legs=["elements"])
    assert (joined.run_id, joined.started) == (first.run_id, False)
    assert nb in notebook_reembed._inflight
    gate.release.set()

    status = _wait_finished(first.run_id)
    assert status.state == "succeeded", status
    assert status.passes == 2
    assert status.legs == ("chunks", "kg_objects")
    assert nb not in notebook_reembed._inflight
    chunk_calls = world.embedders["chunk_embedding"].calls
    assert len(chunk_calls) == len({tuple(call) for call in chunk_calls})  # no repeat
    assert world.embedders["knowledge_object_embedding"].calls
    assert world.embedders["source_element_embedding"].calls == []


def test_unknown_or_empty_legs_are_refused(repo):
    service = NotebookReembedService(repo)
    for legs in ([], ["chunks", "vectors"]):
        with pytest.raises(ValueError):
            service.submit("nb-x", legs=legs)
    assert "nb-x" not in notebook_reembed._inflight


def test_submission_runs_in_the_heavy_pool_and_failure_stays_inside(repo):
    """``submit`` hands the run to the heavy maintenance pool as ``notebook-reembed``; a
    run aborted before any leg (the indexing-pipeline gate) is ``failed``, releases the
    guard, and nothing escapes the worker."""
    assert background_jobs._maintenance_pool("notebook-reembed-nb-a") == (
        "maintenance", "notebook-reembed",
    )
    world = cases.build_world(repo, BACKEND)
    nb = world.notebook_id
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_job_id=? WHERE id=?",
            ("pending:generation", nb),
        )
    submission = NotebookReembedService(repo).submit(nb, legs=LEGS)
    status = _wait_finished(submission.run_id)
    assert status.state == "failed"
    assert status.error == "IndexingPipelineUnavailableError"
    assert nb not in notebook_reembed._inflight


def test_finished_runs_are_kept_up_to_the_history_bound(repo, monkeypatch):
    world = cases.build_world(repo, BACKEND)
    monkeypatch.setattr(notebook_reembed, "_RESULT_HISTORY", 2)
    service = NotebookReembedService(repo)
    runs = [service.run(world.notebook_id, legs=["elements"]).run_id for _ in range(4)]
    kept = [run_id for run_id in runs if NotebookReembedService.status(run_id) is not None]
    assert kept == runs[-2:]
    assert len(notebook_reembed._runs) == 2


# --- the backfill-vectors endpoint shares the guard ------------------------------


def _client(tmp_path, monkeypatch) -> TestClient:
    _env(monkeypatch, tmp_path)
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_backfill_vectors_endpoint_and_reembed_share_one_guard(tmp_path, monkeypatch):
    """A reembed run in flight makes ``POST .../backfill-vectors`` accept without
    scheduling a second job (its legs ride the run), and the endpoint's own run makes a
    reembed submission join it instead of starting a concurrent one."""
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/auth/register", json={"username": "r00400401", "password": "pw"})
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    created = client.post("/api/notebooks", headers=headers, json={"name": "shared-guard"})
    assert created.status_code == 200, created.text
    from app.api.deps import repository

    repo = repository()
    world = cases.build_world(repo, BACKEND, notebook_id=created.json()["id"])
    nb = world.notebook_id
    scheduled = []
    monkeypatch.setattr(
        "app.services.kg.scheduler.submit_job",
        lambda fn, /, *args, **kwargs: scheduled.append((fn, args)),
    )
    cases.delete_vectors(repo, BACKEND, nb)

    # 1) reembed in flight -> the endpoint joins it.
    gate = _Gate(world.embedders["knowledge_object_embedding"])
    service = NotebookReembedService(repo)
    running = service.submit(nb, legs=["kg_objects"])
    assert gate.entered.wait(timeout=10)
    response = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True
    assert scheduled == []
    gate.release.set()
    status = _wait_finished(running.run_id)
    assert status.legs == ("chunks", "elements", "kg_objects")
    assert status.state == "succeeded", status

    # 2) the endpoint's run in flight -> a reembed submission joins it.
    cases.delete_vectors(repo, BACKEND, nb)
    response = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert response.json()["accepted"] is True
    assert len(scheduled) == 1
    fn, args = scheduled[0]
    joined = service.submit(nb, legs=["kg_relations"])
    assert joined.started is False
    fn(*args)  # the scheduler job, run here
    status = NotebookReembedService.status(joined.run_id)
    assert status.state == "succeeded", status
    assert status.legs == ("chunks", "elements", "kg_relations")
    assert nb not in notebook_reembed._inflight
