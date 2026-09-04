"""PR-E: 「重新合并」 runs in the background under the SHARED KG-maintenance slot.

Four properties beyond the API smoke tests in test_kg_rebuild_relink_api.py:
  · the claim is published BEFORE the work starts and released on EVERY exit path
    (including BaseException — a claim stuck in `running` would refuse every later
    rebuild AND every later relink for the life of the process);
  · the slot is SHARED with relink in both directions — the two passes rewrite the
    same derived tables (`concept_clusters` + the community partition), so letting
    them overlap lets one publish over inputs the other is still reading;
  · each status view reports only its own kind and answers `idle` for the other's
    job, so neither bounded poll can be parked on a task it did not start;
  · the pass itself is `rebuild_unified_kg` called verbatim — this PR moved WHERE
    it runs, not what it does.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

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
    still inside `rebuild_unified_kg`."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    entered = threading.Event()
    release = threading.Event()

    def _blocking(notebook_id, force=False):
        entered.set()
        assert release.wait(timeout=10)
        return 7

    lifecycle.rebuild_unified_kg = _blocking
    job = repo.start_unified_kg_rebuild(nb)
    worker = threading.Thread(
        target=repo.run_unified_kg_rebuild_job, args=(nb, job["job_id"]),
    )
    worker.start()
    try:
        assert entered.wait(timeout=10)
        assert repo.unified_kg_rebuild_status(nb)["running"] is True
        with pytest.raises(KgMaintenanceAlreadyRunning):
            repo.start_unified_kg_rebuild(nb)
    finally:
        release.set()
        worker.join(timeout=10)

    settled = repo.unified_kg_rebuild_status(nb)
    assert settled["status"] == "succeeded"
    assert settled["running"] is False
    # The count reaches the browser through the status view, not the POST.
    assert settled["clusters"] == 7
    repo.start_unified_kg_rebuild(nb)


def test_claim_is_released_on_base_exception(repo):
    """KeyboardInterrupt/SystemExit inherit BaseException; `except Exception`
    would leave the notebook permanently un-rebuildable AND un-relinkable."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle

    def _interrupted(_notebook_id, force=False):
        raise KeyboardInterrupt

    lifecycle.rebuild_unified_kg = _interrupted
    job = repo.start_unified_kg_rebuild(nb)
    with pytest.raises(KeyboardInterrupt):
        repo.run_unified_kg_rebuild_job(nb, job["job_id"])

    status = repo.unified_kg_rebuild_status(nb)
    assert status["running"] is False
    assert status["status"] == "failed"
    repo.start_unified_kg_rebuild(nb)          # slot reusable
    # ...and so is relink: they share the slot, so a stuck claim would take both.
    repo._runtime.knowledge_lifecycle._settle_kg_maintenance(
        nb, repo.unified_kg_rebuild_status(nb)["job_id"], "failed"
    )
    repo.start_notebook_relink(nb)


def test_failure_settles_the_claim_and_reports_once(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    emitted = []
    lifecycle.event_log.emit = lambda payload: emitted.append(payload)

    def _boom(_notebook_id, force=False):
        raise RuntimeError("boom")

    lifecycle.rebuild_unified_kg = _boom
    job = repo.start_unified_kg_rebuild(nb)
    with pytest.raises(RuntimeError):
        repo.run_unified_kg_rebuild_job(nb, job["job_id"])

    assert repo.unified_kg_rebuild_status(nb)["status"] == "failed"
    # Body-free by contract: kind + notebook id only, no traceback text.
    assert emitted == [{"kind": "unified_kg_rebuild_failed", "notebook_id": nb}]


def test_relink_and_rebuild_share_one_slot_in_both_directions(repo):
    """They rewrite the same derived tables — concurrency is not just wasteful,
    it lets one pass publish over inputs the other is still consuming."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id

    relink = repo.start_notebook_relink(nb)
    with pytest.raises(KgMaintenanceAlreadyRunning) as busy:
        repo.start_unified_kg_rebuild(nb)
    # The 409 must name the action actually holding the slot, not the one refused.
    assert busy.value.holder == "relink"

    repo.fail_notebook_relink_submission(nb, relink["job_id"])
    rebuild = repo.start_unified_kg_rebuild(nb)
    with pytest.raises(KgMaintenanceAlreadyRunning) as busy:
        repo.start_notebook_relink(nb)
    assert busy.value.holder == "rebuild"
    repo.fail_unified_kg_rebuild_submission(nb, rebuild["job_id"])


def test_each_status_view_ignores_the_other_kind_of_job(repo):
    """A poll must never be parked on a job it did not start: while a rebuild holds
    the shared slot, the relink view answers `idle` (a terminal state for the
    browser) rather than `running`, and neither view leaks the other's counters."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    repo.start_unified_kg_rebuild(nb)

    assert repo.unified_kg_rebuild_status(nb)["running"] is True
    relink_view = repo.notebook_relink_status(nb)
    assert relink_view == {
        "job_id": "", "notebook_id": nb, "status": "idle", "running": False,
        "isolated_before": 0, "edges_added": 0, "isolated_after": 0,
    }
    # `kind` is an internal registry field and must not cross the wire.
    assert set(repo.unified_kg_rebuild_status(nb)) == {
        "job_id", "notebook_id", "status", "running", "clusters",
    }


def test_a_stale_worker_does_not_clobber_a_newer_claim(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    first = repo.start_unified_kg_rebuild(nb)
    repo.fail_unified_kg_rebuild_submission(nb, first["job_id"])
    second = repo.start_unified_kg_rebuild(nb)

    repo._runtime.knowledge_lifecycle._settle_kg_maintenance(
        nb, first["job_id"], "succeeded", {"clusters": 99}
    )
    status = repo.unified_kg_rebuild_status(nb)
    assert status["job_id"] == second["job_id"]
    assert status["status"] == "running"
    assert status["clusters"] == 0


def test_rebuild_single_flight_is_per_notebook(repo):
    first = repo.create_notebook(NotebookCreate(name="a")).id
    second = repo.create_notebook(NotebookCreate(name="b")).id
    repo.start_unified_kg_rebuild(first)
    repo.start_unified_kg_rebuild(second)      # must not raise
    with pytest.raises(KgMaintenanceAlreadyRunning):
        repo.start_unified_kg_rebuild(first)


def test_start_unified_kg_rebuild_404s_on_unknown_notebook(repo):
    with pytest.raises(KeyError):
        repo.start_unified_kg_rebuild("nb-missing")
    with pytest.raises(KeyError):
        repo.unified_kg_rebuild_status("nb-missing")


def test_background_job_runs_the_pass_under_the_version_gate(repo):
    """`force=False` is the whole reason an unchanged notebook still answers in
    milliseconds. Backgrounding must not quietly turn every click into a full
    recluster (that stays with scripts/recluster_kg.py)."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    calls = []

    def _record(notebook_id, force=False):
        calls.append((notebook_id, force))
        return 3

    repo._runtime.knowledge_lifecycle.rebuild_unified_kg = _record
    job = repo.start_unified_kg_rebuild(nb)
    assert repo.run_unified_kg_rebuild_job(nb, job["job_id"]) == 3
    assert calls == [(nb, False)]


# ---------------------------------------------------------------------------
# The pass itself did not move INTO the job plumbing.
# ---------------------------------------------------------------------------

_JOB_PLUMBING = (
    "_claim_kg_maintenance",
    "_settle_kg_maintenance",
    "kg_maintenance_jobs",
    "kg_maintenance_lock",
    "kg_maintenance",
    "start_unified_kg_rebuild",
    "background_jobs",
)


def _lifecycle_function_source(name: str) -> str:
    path = (
        Path(__file__).resolve().parents[1]
        / "app" / "services" / "knowledge_lifecycle.py"
    )
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found in knowledge_lifecycle.py")


def test_rebuild_unified_kg_body_knows_nothing_about_the_job_slot():
    """This PR moved WHERE the pass runs, not what it does.

    The clustering pass stays a plain synchronous function of a notebook: the
    offline CLIs (`scripts/recluster_kg.py`, `batch_ingest`) call it directly from
    their own processes, where an in-process registry means nothing. Growing slot
    handling inside it would both break those callers and make "the algorithm is
    untouched" unverifiable — the exact regression this guard is aimed at.
    """
    body = _lifecycle_function_source("rebuild_unified_kg")
    offenders = [name for name in _JOB_PLUMBING if name in body]
    assert offenders == [], (
        f"rebuild_unified_kg 的函数体里出现了任务槽管道 {offenders} —— "
        "后台化只该改『在哪儿跑』,不该改这一遍怎么跑"
    )
    # The compatibility wrapper is now a one-hop delegate to the dedicated job
    # collaborator; the algorithm itself remains outside that plumbing.
    wrapper = _lifecycle_function_source("run_unified_kg_rebuild_job")
    assert "self.kg_maintenance.run_unified_kg_rebuild_job" in wrapper
    assert "self.rebuild_unified_kg" not in wrapper


def test_lifecycle_public_maintenance_surface_is_one_hop(repo, monkeypatch):
    """Facade/API compatibility stays on lifecycle while orchestration ownership
    lives in KgMaintenanceJobs. Each public wrapper must preserve arguments and
    return values without adding a second policy path."""
    lifecycle = repo._runtime.knowledge_lifecycle
    collaborator = lifecycle.kg_maintenance
    calls = []

    cases = (
        ("start_notebook_relink", ("nb",), {"started": "relink"}),
        ("notebook_relink_status", ("nb",), {"status": "idle"}),
        ("run_notebook_relink_job", ("nb", "rlj-1"), {"edges_added": 2}),
        ("fail_notebook_relink_submission", ("nb", "rlj-1"), None),
        ("start_unified_kg_rebuild", ("nb",), {"started": "rebuild"}),
        ("unified_kg_rebuild_status", ("nb",), {"status": "idle"}),
        ("run_unified_kg_rebuild_job", ("nb", "ukj-1"), 3),
        ("fail_unified_kg_rebuild_submission", ("nb", "ukj-1"), None),
    )
    for name, args, expected in cases:
        def _delegate(*actual, _name=name, _expected=expected, **kw):
            # 批 3·W2 PR-3:start_notebook_relink 多了 exempt_build_marker
            # 关键字直传(默认 False)——单跳委托语义不变,记账带上它。
            calls.append((_name, actual, tuple(sorted(kw.items()))))
            return _expected

        monkeypatch.setattr(collaborator, name, _delegate)
        assert getattr(lifecycle, name)(*args) == expected

    expected_kw = {"start_notebook_relink": (("exempt_build_marker", False),)}
    assert calls == [
        (name, args, expected_kw.get(name, ()))
        for name, args, _expected in cases
    ]


def test_lifecycle_private_registry_aliases_keep_identity(repo):
    lifecycle = repo._runtime.knowledge_lifecycle

    assert lifecycle.kg_maintenance_jobs is lifecycle.kg_maintenance.jobs
    assert lifecycle.kg_maintenance_lock is lifecycle.kg_maintenance.lock
