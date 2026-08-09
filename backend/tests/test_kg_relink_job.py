"""PR-B: relink runs in the background under a per-notebook single flight.

Two properties beyond the API smoke tests in test_kg_rebuild_relink_api.py:
  · the claim is published BEFORE the work starts and released on EVERY exit path
    (including BaseException — a claim stuck in `running` would refuse every later
    relink for the life of the process);
  · the post-build auto relink stays fail-open but is no longer SILENT.
"""
from __future__ import annotations

from contextvars import ContextVar
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


def test_concurrent_second_start_is_rejected_while_the_first_runs(repo):
    """Barrier, not sleep: the second caller runs while the first is provably
    still inside `relink_notebook_kg`."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    entered = threading.Event()
    release = threading.Event()
    original = lifecycle.relink_notebook_kg

    def _blocking(notebook_id):
        entered.set()
        assert release.wait(timeout=10)
        return original(notebook_id)

    lifecycle.relink_notebook_kg = _blocking
    job = repo.start_notebook_relink(nb)
    worker = threading.Thread(
        target=repo.run_notebook_relink_job, args=(nb, job["job_id"]),
    )
    worker.start()
    try:
        assert entered.wait(timeout=10)
        assert repo.notebook_relink_status(nb)["running"] is True
        with pytest.raises(KgMaintenanceAlreadyRunning):
            repo.start_notebook_relink(nb)
    finally:
        release.set()
        worker.join(timeout=10)

    # Once settled the slot is free again.
    assert repo.notebook_relink_status(nb)["status"] == "succeeded"
    assert repo.notebook_relink_status(nb)["running"] is False
    repo.start_notebook_relink(nb)


def test_claim_is_released_on_base_exception(repo):
    """KeyboardInterrupt/SystemExit inherit BaseException; `except Exception`
    would leave the notebook permanently un-relinkable."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle

    def _interrupted(_notebook_id):
        raise KeyboardInterrupt

    lifecycle.relink_notebook_kg = _interrupted
    job = repo.start_notebook_relink(nb)
    with pytest.raises(KeyboardInterrupt):
        repo.run_notebook_relink_job(nb, job["job_id"])

    status = repo.notebook_relink_status(nb)
    assert status["running"] is False
    assert status["status"] == "failed"
    repo.start_notebook_relink(nb)     # slot reusable


def test_job_collaborator_preserves_the_callers_context(repo, monkeypatch):
    """The background scheduler supplies a copied Context; the collaborator must
    execute the late-bound algorithm callback inside that same Context rather
    than introducing a context-losing thread or construction-time binding."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    marker: ContextVar[str] = ContextVar("kg_maintenance_marker")
    token = marker.set("request-owner")
    try:
        monkeypatch.setattr(
            lifecycle,
            "relink_notebook_kg",
            lambda _notebook_id: {
                "isolated_before": 0,
                "edges_added": 0,
                "isolated_after": 0,
                "marker": marker.get(),
            },
        )
        job = repo.start_notebook_relink(nb)
        result = repo.run_notebook_relink_job(nb, job["job_id"])
    finally:
        marker.reset(token)

    assert result["marker"] == "request-owner"


def test_a_stale_worker_does_not_clobber_a_newer_claim(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    first = repo.start_notebook_relink(nb)
    repo.fail_notebook_relink_submission(nb, first["job_id"])
    second = repo.start_notebook_relink(nb)

    # The older worker finally settles — it must not touch the newer claim.
    repo._runtime.knowledge_lifecycle._settle_notebook_relink(
        nb, first["job_id"], "succeeded", {"edges_added": 99}
    )
    status = repo.notebook_relink_status(nb)
    assert status["job_id"] == second["job_id"]
    assert status["status"] == "running"
    assert status["edges_added"] == 0


def test_relink_single_flight_is_per_notebook(repo):
    first = repo.create_notebook(NotebookCreate(name="a")).id
    second = repo.create_notebook(NotebookCreate(name="b")).id
    repo.start_notebook_relink(first)
    repo.start_notebook_relink(second)      # must not raise
    with pytest.raises(KgMaintenanceAlreadyRunning):
        repo.start_notebook_relink(first)


def test_start_notebook_relink_404s_on_unknown_notebook(repo):
    with pytest.raises(KeyError):
        repo.start_notebook_relink("nb-missing")
    with pytest.raises(KeyError):
        repo.notebook_relink_status("nb-missing")


# ---------------------------------------------------------------------------
# auto relink after a successful build: fail-open, but observable
# ---------------------------------------------------------------------------

def test_auto_relink_failure_keeps_the_build_and_emits_an_event(repo, monkeypatch):
    """A MemoryError here used to be swallowed whole: the build reported success
    while the process was already dying, and nothing said so."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))

    def _oom(_notebook_id):
        raise MemoryError("relink blew up")

    monkeypatch.setattr(lifecycle, "relink_notebook_kg", _oom)
    result = {"built": ["s1"], "failed": [], "skipped": []}
    lifecycle._run_success_side_effects(nb, result, enqueue_fold=False)

    assert "relink" not in result            # fail-open: build still succeeded
    assert result["built"] == ["s1"]
    failures = [e for e in emitted if e.get("kind") == "kg_relink_failed"]
    assert len(failures) == 1
    # Body-free by contract: kind + notebook id, no message and no traceback.
    assert failures[0] == {"kind": "kg_relink_failed", "notebook_id": nb}


def test_auto_relink_success_emits_no_failure_event(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))
    monkeypatch.setattr(
        lifecycle, "relink_notebook_kg",
        lambda _nb: {"isolated_before": 0, "edges_added": 0, "isolated_after": 0},
    )
    result: dict = {}
    lifecycle._run_success_side_effects(nb, result, enqueue_fold=False)

    assert result["relink"]["edges_added"] == 0
    assert [e for e in emitted if e.get("kind") == "kg_relink_failed"] == []


def test_post_build_relink_claims_the_same_slot_and_releases_it(repo, monkeypatch):
    """The build tail must go through the SAME per-notebook claim the endpoint
    uses — otherwise a click landing while a build finishes starts a second full
    read of every source."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    seen_claims = []
    monkeypatch.setattr(
        lifecycle, "relink_notebook_kg",
        lambda _nb: seen_claims.append(repo.notebook_relink_status(_nb)["status"])
        or {"isolated_before": 0, "edges_added": 0, "isolated_after": 0},
    )
    result: dict = {}
    lifecycle._run_success_side_effects(nb, result, enqueue_fold=False)

    # The claim was held WHILE the pass ran (a claim taken after the fact would
    # protect nothing) and released afterwards.
    assert seen_claims == ["running"]
    status = repo.notebook_relink_status(nb)
    assert (status["status"], status["running"]) == ("succeeded", False)
    repo.start_notebook_relink(nb)          # slot reusable


def test_post_build_relink_skips_when_a_concurrent_relink_holds_the_slot(
    repo, monkeypatch,
):
    """Losing the claim to a CONCURRENT RELINK is a true SKIP: whoever holds it
    is doing exactly this work, nothing is lost. Fail-open, and observable
    through a body-free event that names the holder."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    repo.start_notebook_relink(nb)          # e.g. the user clicked 「补上关联」
    ran = []
    monkeypatch.setattr(
        lifecycle, "relink_notebook_kg",
        lambda _nb: ran.append(_nb) or {"isolated_before": 0, "edges_added": 0,
                                        "isolated_after": 0},
    )
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))
    result = {"built": ["s1"]}
    lifecycle._run_success_side_effects(nb, result, enqueue_fold=False)

    assert ran == [], "the build tail started a duplicate pass over the notebook"
    assert "relink" not in result           # nothing to report, and no lie either
    assert result["built"] == ["s1"]        # fail-open: the build still succeeded
    assert {"kind": "kg_relink_skipped", "notebook_id": nb, "holder": "relink"} in emitted
    assert [e for e in emitted if e.get("kind") == "kg_relink_failed"] == []
    # The other party's claim is untouched.
    assert repo.notebook_relink_status(nb)["running"] is True


def test_post_build_relink_skips_when_a_concurrent_rebuild_holds_the_slot(
    repo, monkeypatch,
):
    """Losing the claim to a CONCURRENT 「重新合并」 REBUILD is a DIFFERENT case
    from the relink-vs-relink one above: a rebuild does not append the edges
    this tail exists to add, so the drop is a deliberately accepted fail-open
    loss (this build's isolated-node connections simply do not happen this
    round), not "someone else is already doing this for me". The event's
    `holder` field is what lets an operator tell the two skip reasons apart."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    repo.start_unified_kg_rebuild(nb)       # e.g. the user clicked 「重新合并」
    ran = []
    monkeypatch.setattr(
        lifecycle, "relink_notebook_kg",
        lambda _nb: ran.append(_nb) or {"isolated_before": 0, "edges_added": 0,
                                        "isolated_after": 0},
    )
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))
    result = {"built": ["s1"]}
    lifecycle._run_success_side_effects(nb, result, enqueue_fold=False)

    assert ran == [], "the build tail started a duplicate pass over the notebook"
    assert "relink" not in result           # nothing to report, and no lie either
    assert result["built"] == ["s1"]        # fail-open: the build still succeeded
    assert {"kind": "kg_relink_skipped", "notebook_id": nb, "holder": "rebuild"} in emitted
    assert [e for e in emitted if e.get("kind") == "kg_relink_failed"] == []
    # The other party's claim is untouched.
    assert repo.unified_kg_rebuild_status(nb)["running"] is True


# ---------------------------------------------------------------------------
# failure reporting: ONE place, both entry points
# ---------------------------------------------------------------------------

def test_endpoint_job_failure_reports_through_the_same_place(repo, monkeypatch):
    """The endpoint's background job fails silently unless it reports here — the
    build tail's failure event would otherwise have no counterpart, and a click
    that blew up would leave nothing in the event stream at all."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))

    def _boom(_notebook_id):
        raise RuntimeError("relink blew up")

    monkeypatch.setattr(lifecycle, "relink_notebook_kg", _boom)
    job = repo.start_notebook_relink(nb)
    with pytest.raises(RuntimeError):
        repo.run_notebook_relink_job(nb, job["job_id"])

    assert [e for e in emitted if e.get("kind") == "kg_relink_failed"] == [
        {"kind": "kg_relink_failed", "notebook_id": nb}
    ]
    assert repo.notebook_relink_status(nb)["status"] == "failed"


def test_an_interrupt_releases_the_slot_without_reporting_a_failed_pass(
    repo, monkeypatch,
):
    """Ctrl-C is not a failure of the pass — the same distinction the durable
    KG-build guard makes about never opening a circuit on a human interrupt. The
    claim still has to be released."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    emitted = []
    monkeypatch.setattr(lifecycle.event_log, "emit", lambda payload: emitted.append(payload))

    def _interrupted(_notebook_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "relink_notebook_kg", _interrupted)
    job = repo.start_notebook_relink(nb)
    with pytest.raises(KeyboardInterrupt):
        repo.run_notebook_relink_job(nb, job["job_id"])

    assert [e for e in emitted if e.get("kind") == "kg_relink_failed"] == []
    assert repo.notebook_relink_status(nb)["running"] is False
    repo.start_notebook_relink(nb)          # slot reusable
