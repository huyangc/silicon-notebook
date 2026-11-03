"""Terminal publication is independent of model execution and hint failures."""
import threading
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.request_context import reset_request_user, set_request_user
from app.domain.collaboration import PulseActor
from app.models.identity import UserProfile
from app.services.agent_profile_job import AgentProfileConsolidationService
from app.services.catalog_job import CommandCatalogService
from app.services.collab_jobs import current_job_actor, job_queue_entry
from app.services.collab_runtime import CollaborationRuntime
from app.services.kg.maintenance_jobs import KgMaintenanceJobs
from app.services.knowhow.api import ProjectionScheduler
from app.services.knowledge_lifecycle import KnowledgeLifecycleService
from app.services.source_ingestion import SourceIngestionService


def test_source_terminal_counter_settles_even_when_hint_fails():
    service = object.__new__(SourceIngestionService)
    service._active_notebooks_cond = threading.Condition()
    service._active_notebooks = {"nb": 1}
    service._collab_touch = lambda *args: (_ for _ in ()).throw(RuntimeError("hint unavailable"))
    service._finish_notebook_ingestion("nb")
    assert service._active_notebooks == {}


def test_paper_metadata_terminal_is_generation_checked_and_covers_failure_cleanup():
    service = object.__new__(SourceIngestionService)
    service._paper_meta_backfilling_lock = threading.Lock()
    service._paper_meta_backfilling = {"nb": {"_gen": 2}}
    calls = []
    service._collab_touch = lambda *args: calls.append(args)
    assert service._pop_backfilling("nb", 1) is False
    assert not calls
    assert service._pop_backfilling("nb", 2) is True
    assert calls == [("nb", "sources", None, None)]
    assert service._pop_backfilling("nb", 2) is False
    assert len(calls) == 1


def test_catalog_terminal_reuses_its_existing_job_read_and_is_fail_open():
    service = object.__new__(CommandCatalogService)
    reads, events, touches = [], [], []
    service.catalog = SimpleNamespace(get_job=lambda id_: reads.append(id_) or {"notebook_id": "nb", "created_by": "actor"})
    service._emit = lambda *args, **kwargs: events.append(args)
    service._collab_touch = lambda *args: touches.append(args)
    service._emit_finished("job")
    assert reads == ["job"] and touches == [("nb", "catalog", "actor", None)]
    service._collab_touch = lambda *args: (_ for _ in ()).throw(RuntimeError("hint unavailable"))
    service._emit_finished("job")
    assert len(events) == 2


@pytest.mark.parametrize("kind,expected", [("kg_build_started", 0), ("kg_build_succeeded", 1), ("kg_build_failed", 1)])
def test_durable_kg_terminal_events_publish_actor_without_new_reads(kind, expected):
    service = object.__new__(KnowledgeLifecycleService)
    calls, events = [], []
    runtime = CollaborationRuntime(Settings(), SimpleNamespace(insert_activity=lambda _: pytest.fail("owner activity must stay ephemeral")),
                                   SimpleNamespace(find_user_by_id=lambda _: pytest.fail("captured actor must not be reread")),
                                   SimpleNamespace(pool_wait_stats=lambda: {}))

    def terminal(nb, kinds, actor_id, actor_name):
        calls.append((nb, kinds, actor_id, actor_name))
        runtime.touch_for_job(nb, kinds, actor_id, "提交时")

    service._collab_touch = terminal
    service.event_log = SimpleNamespace(emit=events.append)
    job = {"id": "job", "notebook_id": "nb", "created_by": "actor", "mode": "incremental", "status": "failed", "stage": "done", "total_sources": 1, "completed_sources": 0, "failed_sources": 1, "error_code": "unavailable"}
    try:
        cursor = runtime.pulse.cursor()
        service._emit_kg_build_event(kind, job)
        assert [row["kind"] for row in runtime.pulse.recent_owner_changes("nb", "actor")] == ["kg"] * expected
        hints = runtime.pulse.poll("nb", PulseActor("peer", "peer", "reader"), since=cursor, visible=True)["changes"]
        assert {hint["kind"] for hint in hints} == ({"kg", "knowledge", "notebook", "sources"} if expected else set())
    finally:
        runtime.close()
    assert len(calls) == expected
    if calls:
        assert calls[0] == ("nb", ("kg", "knowledge", "notebook", "sources"), "actor", None)
    assert events[0]["kind"] == kind


def test_maintenance_terminal_keeps_captured_name_and_rejects_stale_job():
    jobs = KgMaintenanceJobs(event_log=SimpleNamespace(emit=lambda _: None), get_notebook=lambda _: None, new_id=lambda _: "job")
    jobs.jobs["nb"] = {"job_id": "job", "status": "running", "actor_id": "actor", "actor_name": "提交时姓名"}
    calls = []
    jobs._collab_touch = lambda *args: calls.append(args)
    jobs.settle("nb", "stale", "failed")
    assert not calls
    jobs.settle("nb", "job", "failed")
    assert calls == [("nb", ("kg", "knowledge", "notebook"), "actor", "提交时姓名")]
    assert jobs.jobs["nb"]["status"] == "failed"


def test_conflict_review_settlement_keeps_the_kg_only_dependency():
    jobs = KgMaintenanceJobs(event_log=SimpleNamespace(emit=lambda _: None), get_notebook=lambda _: None, new_id=lambda _: "job")
    jobs.jobs["nb"] = {"job_id": "job", "kind": "conflict", "status": "running", "actor_id": "actor", "actor_name": "name"}
    calls = []
    jobs._collab_touch = lambda *args: calls.append(args)
    jobs.settle("nb", "job", "idle")
    assert calls == [("nb", "kg", "actor", "name")]


def test_only_a_settled_shared_understanding_chain_publishes():
    from app.repositories.ports import AGENT_PROFILE_SETTLE_SUPERSEDED, AGENT_PROFILE_SETTLED
    from app.services.agent_profile_job import BASE_CHAIN_OWNER

    service = object.__new__(AgentProfileConsolidationService)
    calls = []
    outcome = [AGENT_PROFILE_SETTLED]
    service.profiles = SimpleNamespace(settle=lambda *args, **kwargs: outcome[0])
    service._collab_touch = lambda *args: calls.append(args)
    service._safe_settle("nb", "private-owner", "idle", claim_token="claim")
    assert not calls
    service._safe_settle("nb", BASE_CHAIN_OWNER, "idle", claim_token="claim")
    assert calls == [("nb", "understanding", None, None)]
    outcome[0] = AGENT_PROFILE_SETTLE_SUPERSEDED
    service._safe_settle("nb", BASE_CHAIN_OWNER, "idle", claim_token="claim")
    assert len(calls) == 1


def test_queue_metadata_preserves_two_tuple_contract_and_frozen_actor():
    user = UserProfile(id="actor", username="login", display_name="提交时", email="", role="user")
    token = set_request_user(user)
    try:
        queued = job_queue_entry("full", 42)
    finally:
        reset_request_user(token)
    assert queued == ("full", 42)
    assert len(queued) == 2
    restored = job_queue_entry("incremental", queued[1], previous=queued)
    assert restored.actor == PulseActor("actor", "提交时", "editor")


def test_debounce_and_rerun_use_each_submitting_actor(monkeypatch):
    timers, pending, observed = [], [], []

    class Timer:
        def __init__(self, *args, **kwargs):
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr("app.services.knowhow.api.threading.Timer", Timer)
    scheduler = ProjectionScheduler(lambda table: observed.append(current_job_actor()), submit=lambda fn, *args, **kwargs: pending.append((fn, args)))
    for actor in ("first", "latest"):
        token = set_request_user(UserProfile(id=actor, username=actor, display_name=actor, email="", role="user"))
        try:
            scheduler.schedule("table")
        finally:
            reset_request_user(token)
    scheduler._fire("table")
    token = set_request_user(UserProfile(id="rerun", username="rerun", display_name="rerun", email="", role="user"))
    try:
        scheduler.schedule("table")
    finally:
        reset_request_user(token)
    scheduler._fire("table")
    while pending:
        fn, args = pending.pop(0)
        fn(*args)
    assert [actor.user_id for actor in observed] == ["latest", "rerun"]
    assert not scheduler._contexts and not scheduler._running and not scheduler._rerun
