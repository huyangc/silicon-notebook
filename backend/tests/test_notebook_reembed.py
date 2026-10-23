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

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.repositories.sqlite import maintenance as sqlite_maintenance
from app.services import background_jobs, notebook_reembed
from app.services.notebook_reembed import LEGS, NotebookReembedService, ReembedInFlight
from app.services.sqlite_repository import SQLiteRepository
from tests import notebook_reembed_cases as cases

# Runs share the process-wide heavy pool with whatever else a test queued; wait on the
# run's own completion event with room to spare rather than polling a short clock.
_WAIT_SECONDS = 120


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [
            json.dumps(values[c]) if isinstance(values[c], (dict, list)) else values[c]
            for c in columns
        ],
    )


BACKEND = cases.Backend(
    name="sqlite", insert=_insert, placeholder="?",
    maintenance_module=sqlite_maintenance, relation_page_attr="_RELATION_PAGE_SIZE",
    json_param=json.dumps,
)


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


def test_backfill_job_fills_knowhow_without_touching_the_graph(repo):
    cases.check_backfill_job_fills_knowhow_without_touching_the_graph(repo, BACKEND)


def test_knowhow_fill_skips_a_chunk_rewritten_mid_call(repo):
    cases.check_knowhow_fill_skips_a_chunk_rewritten_mid_call(repo, BACKEND)


def test_offline_missing_only_embed_keeps_knowhow_on_its_workload(repo):
    cases.check_offline_missing_only_embed_keeps_knowhow_on_its_workload(repo, BACKEND)


def test_object_vectors_mark_the_graph_dirty(repo):
    cases.check_object_vectors_mark_the_graph_dirty(repo, BACKEND)


def test_model_failure_is_visible_and_resumes(repo):
    cases.check_model_failure_is_visible_and_resumes(repo, BACKEND)


def test_unbound_workload_is_visible(repo):
    cases.check_unbound_workload_is_visible(repo, BACKEND)


def test_a_failed_discovery_fails_its_leg(repo, monkeypatch):
    cases.check_a_failed_discovery_fails_its_leg(repo, BACKEND, monkeypatch)


def test_a_pipeline_switch_aborts_the_pass(repo, monkeypatch):
    cases.check_a_pipeline_switch_aborts_the_pass(repo, BACKEND, monkeypatch)


def test_relation_backfill_pages(repo, monkeypatch):
    cases.check_relation_backfill_pages(repo, BACKEND, monkeypatch)


def test_object_leg_keeps_its_count_when_it_fails(repo, monkeypatch):
    cases.check_object_leg_keeps_its_count_when_it_fails(repo, BACKEND, monkeypatch)


def test_knowhow_fill_discovers_ids_then_reads_page_by_page(repo, monkeypatch):
    cases.check_knowhow_fill_discovers_ids_then_reads_page_by_page(repo, BACKEND, monkeypatch)


def test_conditional_chunk_vector_write(repo):
    cases.check_conditional_chunk_vector_write(repo, BACKEND)


def test_blank_chunks_are_never_a_gap(repo):
    cases.check_blank_chunks_are_never_a_gap(repo, BACKEND)


def test_orphan_knowhow_sources_are_not_counted(repo):
    cases.check_orphan_knowhow_sources_are_not_counted(repo, BACKEND)


def test_one_failing_knowhow_table_does_not_stop_the_others(repo, monkeypatch):
    cases.check_one_failing_knowhow_table_does_not_stop_the_others(repo, BACKEND, monkeypatch)


def test_offline_embed_messages(repo, capsys):
    cases.check_offline_embed_messages(repo, BACKEND, capsys)


def test_an_embedder_that_writes_nothing_is_never_done(repo, monkeypatch):
    cases.check_an_embedder_that_writes_nothing_is_never_done(repo, BACKEND, monkeypatch)


def test_object_backfill_never_overwrites_a_newer_vector(repo):
    cases.check_object_backfill_never_overwrites_a_newer_vector(repo, BACKEND)


def test_a_vector_another_writer_filled_is_not_remaining(repo):
    cases.check_a_vector_another_writer_filled_is_not_remaining(repo, BACKEND)


def test_relation_backfill_never_overwrites_or_mismatches(repo):
    cases.check_relation_backfill_never_overwrites_or_mismatches(repo, BACKEND)


def test_pipeline_switch_while_waiting_for_a_table_lock(repo, monkeypatch):
    cases.check_pipeline_switch_while_waiting_for_a_table_lock(repo, BACKEND, monkeypatch)


def test_pipeline_switch_between_the_halves(repo, monkeypatch):
    cases.check_pipeline_switch_between_the_halves(repo, BACKEND, monkeypatch)


def test_a_failing_later_page_still_counts_earlier_pages(repo, monkeypatch):
    cases.check_a_failing_later_page_still_counts_earlier_pages(repo, BACKEND, monkeypatch)


def test_fill_and_admission_never_read_cell_content(repo, monkeypatch):
    cases.check_fill_and_admission_never_read_cell_content(repo, BACKEND, monkeypatch)


def test_relation_discovery_page_is_one_index_range(repo):
    """Each relation-backfill page is one range of
    ``idx_knowledge_relations_nb_source_id``, read in index order: no scan of the
    notebook's relations and no temporary sort per page (what ``id > ? ORDER BY id``
    cost without a ``(notebook_id, id)`` index)."""
    with repo._runtime.database.connect() as db:
        plan = " | ".join(
            str(row["detail"]) for row in db.execute(
                "EXPLAIN QUERY PLAN " + sqlite_maintenance._MISSING_RELATION_PAGE_SQL,
                ("nb", "a", "b", 5),
            ).fetchall()
        )
    assert "idx_knowledge_relations_nb_source_id" in plan, plan
    assert "(source_object_id,id)>" in plan.replace(" ", ""), plan
    assert "TEMP B-TREE" not in plan, plan
    assert "SCAN r" not in plan, plan


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
                assert self.release.wait(timeout=_WAIT_SECONDS)
            return inner(texts)

        embedder.embed_texts = _blocking

    def __enter__(self) -> "_Gate":
        return self

    def __exit__(self, *_exc) -> None:
        # Always let the blocked worker go, so a failing assertion inside the block
        # cannot leave a heavy-pool worker parked until its timeout.
        self.release.set()


def _finished(run_id: str):
    status = NotebookReembedService.wait(run_id, timeout=_WAIT_SECONDS)
    assert status is not None and status.state != "running", status
    return status


def _add_document_chunk(repo, notebook_id: str, chunk_id: str, text: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,"
            "created_at) VALUES (?,?,?,?,'','[]',?)",
            (chunk_id, notebook_id, f"src-doc-{notebook_id}", text, cases._NOW),
        )


def test_a_leg_whose_discovery_already_ran_runs_again(repo):
    """A request joining after the current pass began discovering a leg is not covered
    by that pass: the leg runs once more, so a gap that appeared after discovery is
    filled before the run ends. ``embedded`` adds up over both passes."""
    world = cases.build_world(repo, BACKEND)
    nb = world.notebook_id
    cases.delete_vectors(repo, BACKEND, nb, tables=("chunk_embeddings",))
    service = NotebookReembedService(repo)

    with _Gate(world.embedders["chunk_embedding"]) as gate:
        first = service.submit(nb, legs=["chunks"])
        assert first.started
        assert gate.entered.wait(timeout=_WAIT_SECONDS)  # discovery done, embedding
        _add_document_chunk(repo, nb, f"ck-late-{nb}", "a chunk that arrived late")
        joined = service.submit(nb, legs=["chunks"])
        assert (joined.run_id, joined.started) == (first.run_id, False)
        with pytest.raises(ReembedInFlight):
            service.run(nb, legs=["elements"])

    status = _finished(first.run_id)
    assert status.state == "succeeded", status
    assert status.passes == 2
    assert f"ck-late-{nb}" in cases.vector_ids(repo, BACKEND, nb)["chunk_embeddings"]
    assert status.progress["chunks"].embedded == (
        cases.DOC_CHUNKS + 1 + cases.KNOWHOW_CHUNKS
    )
    assert world.embedders["source_element_embedding"].calls == []
    assert nb not in notebook_reembed._active_runs


def test_a_leg_not_started_yet_covers_a_joining_request(repo):
    """A request for a leg the current pass has not reached yet is covered by that pass:
    no second pass, and the object leg runs exactly once. (A leg the run does not have
    at all goes to a follow-up pass: pinned by the shared-guard endpoint test below.)"""
    world = cases.build_world(repo, BACKEND)
    nb = world.notebook_id
    objects = cases.vector_ids(repo, BACKEND, nb)["knowledge_embeddings"]
    cases.delete_vectors(repo, BACKEND, nb, tables=("chunk_embeddings", "knowledge_embeddings"))
    service = NotebookReembedService(repo)

    with _Gate(world.embedders["chunk_embedding"]) as gate:
        first = service.submit(nb, legs=["chunks", "kg_objects"])
        assert gate.entered.wait(timeout=_WAIT_SECONDS)  # in the chunk leg
        assert service.join_running(nb, ["kg_objects"])

    status = _finished(first.run_id)
    assert status.state == "succeeded", status
    assert status.passes == 1
    # One object pass: every missing object embedded once, in ceil(n / batch) calls.
    recorder = world.embedders["knowledge_object_embedding"]
    assert len(recorder.texts) == len(objects)
    assert len(recorder.calls) == -(-len(objects) // cases.BATCH)


def test_run_claimed_has_exactly_one_executor(repo):
    """The job body executes only a claimed run, and only once at a time."""
    world = cases.build_world(repo, BACKEND)
    nb = world.notebook_id
    service = NotebookReembedService(repo)
    with pytest.raises(ValueError):
        service.run_claimed(nb)
    cases.delete_vectors(repo, BACKEND, nb, tables=("chunk_embeddings",))
    with _Gate(world.embedders["chunk_embedding"]) as gate:
        running = service.submit(nb, legs=["chunks"])
        assert gate.entered.wait(timeout=_WAIT_SECONDS)
        with pytest.raises(RuntimeError):
            service.run_claimed(nb)
    assert _finished(running.run_id).state == "succeeded"


def test_unknown_or_empty_legs_are_refused(repo):
    service = NotebookReembedService(repo)
    for legs in ([], ["chunks", "vectors"]):
        with pytest.raises(ValueError):
            service.submit("nb-x", legs=legs)
    assert "nb-x" not in notebook_reembed._active_runs


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
    status = _finished(submission.run_id)
    assert status.state == "failed"
    assert status.error == "IndexingPipelineUnavailableError"
    assert nb not in notebook_reembed._active_runs


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

    # 1) reembed in flight -> the endpoint joins it; legs the run did not have run in
    #    a follow-up pass of the same run.
    service = NotebookReembedService(repo)
    with _Gate(world.embedders["knowledge_object_embedding"]) as gate:
        running = service.submit(nb, legs=["kg_objects"])
        assert gate.entered.wait(timeout=_WAIT_SECONDS)
        response = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is True
        assert scheduled == []
    status = _finished(running.run_id)
    assert status.legs == ("chunks", "elements", "kg_objects")
    assert status.passes == 2
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
    assert nb not in notebook_reembed._active_runs


@pytest.mark.parametrize(
    "gap,unbound,accepted",
    [
        ("knowhow", "chunk_embedding", True),
        ("knowhow", "knowhow_embedding", False),
        ("document", "knowhow_embedding", True),
        ("document", "chunk_embedding", False),
    ],
)
def test_backfill_vectors_accepts_a_chunk_gap_only_when_its_workload_is_bound(
    tmp_path, monkeypatch, gap, unbound, accepted
):
    """H4 counts document and Knowhow chunks together; the endpoint accepts a chunk gap
    only when the workload that fills that kind of chunk is bound (a document gap needs
    chunk_embedding, a Knowhow gap knowhow_embedding), and refuses otherwise in the
    existing ``accepted=false`` shape."""
    client = _client(tmp_path, monkeypatch)
    r = client.post("/api/auth/register", json={"username": "r00400402", "password": "pw"})
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    created = client.post("/api/notebooks", headers=headers, json={"name": "gap-kind"})
    from app.api.deps import repository

    repo = repository()
    world = cases.build_world(repo, BACKEND, notebook_id=created.json()["id"])
    nb = world.notebook_id
    if gap == "knowhow":
        missing = sorted(cases.knowhow_chunk_ids(repo, BACKEND, world))[:1]
    else:
        missing = [f"ck-src-doc-{nb}-000"]
    cases.delete_chunk_vectors(repo, BACKEND, missing)
    repo._runtime.source_embedding.note_source_vectors_written(nb)  # fresh H4/H5
    original = repo.configured
    monkeypatch.setattr(repo, "configured", lambda w: w != unbound and original(w))
    scheduled = []
    monkeypatch.setattr(
        "app.services.kg.scheduler.submit_job",
        lambda fn, /, *args, **kwargs: scheduled.append((fn, args)),
    )
    try:
        response = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["accepted"] is accepted
        assert len(scheduled) == (1 if accepted else 0)
    finally:
        NotebookReembedService.release(nb)
