"""Conflict detection's background job: its own slot, settled on every exit.

Mirrors test_kg_relink_job.py.  The properties are the same ones that matter for
relink, for the same reason: a claim stuck in `running` refuses every later
detection for the life of the process, and the post-build tail must go through
the same gates the endpoint does rather than around them.
"""
from __future__ import annotations

import threading

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import KgMaintenanceAlreadyRunning
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _jobs(repo):
    return repo._runtime.knowledge_lifecycle.kg_conflict_jobs


def _status(repo, notebook_id):
    return _jobs(repo).status(
        notebook_id, "conflict", dict(_jobs(repo).CONFLICT_COUNTERS)
    )


# ---------------------------------------------------------------------------
# Settlement on every exit path
# ---------------------------------------------------------------------------

def test_success_settles_succeeded_and_frees_the_slot(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    jobs = _jobs(repo)
    jobs._resolve_notebook_conflicts = lambda _nb: {
        "detected": 3, "auto_applied": 1, "queued": 2, "skipped_llm": False,
    }

    job = repo.start_conflict_resolution(nb)
    assert repo.run_conflict_resolution_job(nb, job["job_id"])["detected"] == 3

    status = _status(repo, nb)
    assert status["status"] == "succeeded"
    assert status["running"] is False
    assert (status["detected"], status["auto_applied"], status["queued"]) == (3, 1, 2)
    repo.start_conflict_resolution(nb)  # slot is free again


def test_exception_settles_failed_emits_and_frees_the_slot(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    jobs = _jobs(repo)
    emitted: list = []
    jobs.event_log.emit = lambda event: emitted.append(event)

    def _boom(_notebook_id):
        raise RuntimeError("adjudicator exploded")

    jobs._resolve_notebook_conflicts = _boom
    job = repo.start_conflict_resolution(nb)
    with pytest.raises(RuntimeError):
        repo.run_conflict_resolution_job(nb, job["job_id"])

    assert _status(repo, nb)["status"] == "failed"
    assert _status(repo, nb)["running"] is False
    assert emitted == [
        {"kind": "kg_conflict_resolution_failed", "notebook_id": nb}
    ]
    repo.start_conflict_resolution(nb)


def test_claim_is_released_on_base_exception(repo):
    """KeyboardInterrupt/SystemExit inherit BaseException; `except Exception`
    would leave the notebook permanently undetectable."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    jobs = _jobs(repo)

    def _interrupted(_notebook_id):
        raise KeyboardInterrupt

    jobs._resolve_notebook_conflicts = _interrupted
    job = repo.start_conflict_resolution(nb)
    with pytest.raises(KeyboardInterrupt):
        repo.run_conflict_resolution_job(nb, job["job_id"])

    assert _status(repo, nb)["status"] == "failed"
    repo.start_conflict_resolution(nb)


def test_concurrent_second_start_is_rejected_while_the_first_runs(repo):
    """Barrier, not sleep: the second caller runs while the first is provably
    still inside the resolution pass."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    jobs = _jobs(repo)
    entered = threading.Event()
    release = threading.Event()

    def _blocking(_notebook_id):
        entered.set()
        assert release.wait(timeout=10)
        return {"detected": 0, "auto_applied": 0, "queued": 0}

    jobs._resolve_notebook_conflicts = _blocking
    job = repo.start_conflict_resolution(nb)
    worker = threading.Thread(
        target=repo.run_conflict_resolution_job, args=(nb, job["job_id"]),
    )
    worker.start()
    try:
        assert entered.wait(timeout=10)
        assert _status(repo, nb)["running"] is True
        with pytest.raises(KgMaintenanceAlreadyRunning):
            repo.start_conflict_resolution(nb)
    finally:
        release.set()
        worker.join(timeout=10)

    assert _status(repo, nb)["status"] == "succeeded"
    repo.start_conflict_resolution(nb)


def test_relink_and_conflict_hold_independent_slots(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    repo.start_notebook_relink(nb)
    repo.start_conflict_resolution(nb)  # must not raise
    with pytest.raises(KgMaintenanceAlreadyRunning):
        repo.start_notebook_relink(nb)
    with pytest.raises(KgMaintenanceAlreadyRunning):
        repo.start_conflict_resolution(nb)


# ---------------------------------------------------------------------------
# The post-build tail shares the endpoint's gates
# ---------------------------------------------------------------------------

def test_build_tail_claims_the_slot_instead_of_bypassing_it(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(repo.settings, "kg_conflict_resolution_enabled", True)
    monkeypatch.setattr(repo.settings, "kg_relink_enabled", False)

    claimed: list = []
    original_claim = lifecycle.kg_conflict_jobs.start_conflict_resolution
    lifecycle.kg_conflict_jobs.start_conflict_resolution = (
        lambda notebook_id: claimed.append(notebook_id) or original_claim(notebook_id)
    )
    ran: list = []
    lifecycle.kg_conflict_jobs._resolve_notebook_conflicts = (
        lambda notebook_id: ran.append(notebook_id) or {}
    )

    lifecycle._run_success_side_effects(nb, {}, enqueue_fold=False)

    assert claimed == [nb] and ran == [nb]
    assert _status(repo, nb)["status"] == "succeeded"


def test_build_tail_skips_when_the_slot_is_already_held(repo, monkeypatch):
    """A user-triggered run in flight must not be joined by a second full pass."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(repo.settings, "kg_conflict_resolution_enabled", True)
    monkeypatch.setattr(repo.settings, "kg_relink_enabled", False)
    emitted: list = []
    monkeypatch.setattr(
        lifecycle.event_log, "emit", lambda event: emitted.append(event)
    )
    ran: list = []
    lifecycle.kg_conflict_jobs._resolve_notebook_conflicts = (
        lambda notebook_id: ran.append(notebook_id) or {}
    )

    repo.start_conflict_resolution(nb)  # somebody else holds the slot
    lifecycle._run_success_side_effects(nb, {}, enqueue_fold=False)

    assert ran == []
    assert {"kind": "kg_conflict_resolution_skipped", "notebook_id": nb,
            "reason": "already_running"} in emitted


def test_build_tail_honours_the_same_size_admission_as_the_endpoint(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(repo.settings, "kg_conflict_resolution_enabled", True)
    monkeypatch.setattr(repo.settings, "kg_relink_enabled", False)
    admissions: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "_conflict_resolution_admission_reason",
        lambda notebook_id: admissions.append(notebook_id) or "too_many_objects",
    )
    emitted: list = []
    monkeypatch.setattr(
        lifecycle.event_log, "emit", lambda event: emitted.append(event)
    )
    ran: list = []
    lifecycle.kg_conflict_jobs._resolve_notebook_conflicts = (
        lambda notebook_id: ran.append(notebook_id) or {}
    )

    lifecycle._run_success_side_effects(nb, {}, enqueue_fold=False)

    assert ran == []
    assert admissions == [nb], "超限路径只能执行一次有界准入计数"
    assert {"kind": "kg_conflict_resolution_skipped", "notebook_id": nb,
            "reason": "too_many_objects"} in emitted
    # The slot was never claimed, so a later admitted run still works.
    repo.start_conflict_resolution(nb)


def test_build_tail_stays_fail_open_when_resolution_raises(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(repo.settings, "kg_conflict_resolution_enabled", True)
    monkeypatch.setattr(repo.settings, "kg_relink_enabled", False)

    def _boom(_notebook_id):
        raise RuntimeError("adjudicator exploded")

    lifecycle.kg_conflict_jobs._resolve_notebook_conflicts = _boom

    lifecycle._run_success_side_effects(nb, {}, enqueue_fold=False)  # must not raise

    assert _status(repo, nb)["status"] == "failed"
    repo.start_conflict_resolution(nb)


def test_admission_predicate_reads_the_active_object_count(repo, monkeypatch):
    """One predicate for both entries — and it counts objects, not something else."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    repo.store_kg(nb, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim B"}, "evidence": []},
    ], [])

    monkeypatch.setattr(repo.settings, "kg_conflict_max_objects", 2)
    assert lifecycle.conflict_resolution_admitted(nb) is True
    assert repo.conflict_resolution_admitted(nb) is True
    monkeypatch.setattr(repo.settings, "kg_conflict_max_objects", 1)
    assert lifecycle.conflict_resolution_admitted(nb) is False
    assert repo.conflict_resolution_admitted(nb) is False


def test_admission_predicate_also_rejects_dense_relation_graph(repo, monkeypatch):
    """对象数不过线但关系数过线时，入口同样拒绝整个检测任务。"""
    nb = repo.create_notebook(NotebookCreate(name="dense-relations")).id
    repo.store_kg(nb, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "B"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "supports", "evidence": []},
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "contradicts", "evidence": []},
    ])

    monkeypatch.setattr(repo.settings, "kg_conflict_max_objects", 10)
    monkeypatch.setattr(repo.settings, "kg_conflict_max_relations", 1)
    assert repo.conflict_resolution_admitted(nb) is False
    monkeypatch.setattr(repo.settings, "kg_conflict_max_relations", 2)
    assert repo.conflict_resolution_admitted(nb) is True


def test_worker_relation_limit_race_skips_whole_pass(repo, monkeypatch):
    """入口预检后的新增边也不会触发无界读取或部分图检测。"""
    nb = repo.create_notebook(NotebookCreate(name="relation-race")).id
    repo.store_kg(nb, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "B"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "supports", "evidence": []},
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "contradicts", "evidence": []},
    ])
    service = repo._runtime.knowledge_governance
    monkeypatch.setattr(repo.settings, "kg_conflict_max_relations", 1)
    monkeypatch.setattr(
        service.model_clients, "configured", lambda _purpose: True
    )

    def _must_not_load_objects(*_args, **_kwargs):
        raise AssertionError("over-limit relation graph must skip before object hydration")

    monkeypatch.setattr(
        service.governance_store,
        "conflict_resolution_rows",
        _must_not_load_objects,
    )
    emitted: list[dict] = []
    monkeypatch.setattr(service.event_log, "emit", emitted.append)

    result = service.resolve_notebook_conflicts(nb)

    assert result["skipped_relation_limit"] is True
    assert result["detected"] == result["queued"] == result["auto_applied"] == 0
    assert emitted == [{
        "kind": "kg_conflict_resolution_skipped",
        "notebook_id": nb,
        "reason": "too_many_relations",
    }]


def test_worker_object_limit_race_skips_whole_pass(repo, monkeypatch):
    """提交后排队期间新增对象，worker 不得再无界 hydrate 对象/向量。"""
    nb = repo.create_notebook(NotebookCreate(name="object-race")).id
    repo.store_kg(nb, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "A"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "B"}, "evidence": []},
    ], [])
    service = repo._runtime.knowledge_governance
    monkeypatch.setattr(repo.settings, "kg_conflict_max_objects", 1)
    monkeypatch.setattr(
        service.model_clients, "configured", lambda _purpose: True
    )

    def _must_not_hydrate(*_args, **_kwargs):
        raise AssertionError("over-limit object graph must skip before hydration")

    monkeypatch.setattr(
        service.governance_store,
        "conflict_resolution_rows",
        _must_not_hydrate,
    )
    monkeypatch.setattr(
        service.governance_store,
        "conflict_relation_rows",
        _must_not_hydrate,
    )
    emitted: list[dict] = []
    monkeypatch.setattr(service.event_log, "emit", emitted.append)

    result = service.resolve_notebook_conflicts(nb)

    assert result["skipped_object_limit"] is True
    assert result["detected"] == result["queued"] == result["auto_applied"] == 0
    assert emitted == [{
        "kind": "kg_conflict_resolution_skipped",
        "notebook_id": nb,
        "reason": "too_many_objects",
    }]
