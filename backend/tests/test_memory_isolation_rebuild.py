"""Post-readiness isolated rebuild for ruling M1 (plan 2026-09-29 §3.2), SQLite.

Real repository, real migration, real ``rebuild_unified_kg`` through the
ordinary maintenance slot -- nothing in the rebuild path is replaced. The only
seams used are the ones the service exposes for time (``sleep``) and the
readiness module itself; failures are produced for real (a table the rebuild
writes is missing) and a busy slot is produced for real (a held claim).

Pinned: marker 0 -> 1 only on success; completed notebooks are never queued
again; a failed notebook keeps 0, emits a content-free failure, and succeeds
at the "next start"; a busy slot defers and is retried in the same pass;
nothing runs before readiness; the startup hook sits after mark_ready;
checkup's read-only H9 follows the marker.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.core import readiness
from app.core.config import Settings
from app.repositories.sqlite.migrations import SqliteMigrator
from app.services import startup_warmup
from app.services.memory_isolation_rebuild import MemoryIsolationRebuild
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_isolation_cases as cases


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mkr.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    database = repository._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    with database.write() as db:
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN memory_isolation_version")
        db.execute("PRAGMA user_version = 86")
    assert SqliteMigrator(database, repository.settings).migrate() == [87, 88]
    return repository


@pytest.fixture
def events(repo, monkeypatch):
    """Record what the service emits while still emitting it (observation,
    not replacement)."""
    seen: list[dict] = []
    log = repo._runtime.event_log
    original = log.emit

    def recording(event, *args, **kwargs):
        seen.append(dict(event))
        return original(event, *args, **kwargs)

    monkeypatch.setattr(log, "emit", recording)
    return seen


def _isolation_events(events):
    return [e for e in events if str(e.get("kind", "")).startswith("memory_isolation_")]


def _marker(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT memory_isolation_version FROM unified_kg_state WHERE notebook_id=?",
            (notebook_id,),
        ).fetchone()
    return int(row[0])


def _state(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT cluster_input_version, dirty FROM unified_kg_state "
            "WHERE notebook_id=?", (notebook_id,),
        ).fetchone()
    return row[0], int(row[1])


def _published_clusters(repo, notebook_id):
    with repo._runtime.database.connect() as db:
        return db.execute(
            "SELECT COUNT(*) FROM concept_clusters WHERE notebook_id=? AND generation = "
            "(SELECT cluster_generation FROM unified_kg_state WHERE notebook_id=?)",
            (notebook_id, notebook_id),
        ).fetchone()[0]


def _kinds_for(events, notebook_id):
    return [e["kind"] for e in _isolation_events(events)
            if e.get("notebook_id") == notebook_id]


QUEUED = list(cases.QUEUED)
DONE = {"completed": len(QUEUED), "failed": 0, "deferred": 0, "skipped": 0}


def test_pass_rebuilds_the_marked_notebooks_and_sets_the_marker(repo, events):
    service = MemoryIsolationRebuild.for_repository(repo)
    # the migration queued F; every other notebook with clusters awaits the
    # post-readiness check
    assert service.pending_notebook_ids() == sorted(cases.F_NOTEBOOKS)
    assert service.seed_check_notebook_ids() == sorted(cases.SEED_CHECKED)
    assert _state(repo, cases.NB_F) == ("pre-isolation", 1)

    tally = service.run_pass()

    assert tally == DONE
    for notebook in (*QUEUED, *cases.SEED_CHECKED):
        assert _marker(repo, notebook) == 1, notebook
    assert service.pending_count() == 0
    # the rebuild really ran to its end-write: new input version, dirty
    # cleared, and a freshly published cluster map
    version, dirty = _state(repo, cases.NB_F)
    assert version not in ("", "pre-isolation") and dirty == 0
    assert _published_clusters(repo, cases.NB_F) > 0
    assert service.seed_check_count() == 0
    checks = {e["notebook_id"]: (e["outcome"], e["signal"])
              for e in _isolation_events(events)
              if e["kind"] == "memory_isolation_seed_checked"}
    assert checks == {
        notebook: ("queued", signal) if signal else ("clean", "")
        for notebook, signal in cases.SIGNALS.items()
    }
    for notebook in QUEUED:
        assert [k for k in _kinds_for(events, notebook)
                if k != "memory_isolation_seed_checked"] == [
            "memory_isolation_rebuild_started",
            "memory_isolation_rebuild_completed",
        ], notebook
    assert _kinds_for(events, cases.NB_C) == ["memory_isolation_seed_checked"]
    completed = [e for e in _isolation_events(events)
                 if e["kind"] == "memory_isolation_rebuild_completed"]
    assert [e["notebook_id"] for e in completed] == QUEUED
    assert all(isinstance(e["clusters"], int) for e in completed)


def test_a_dangling_seed_notebook_is_rebuilt_without_its_ghost_cluster(repo):
    """G holds no Memory any more; its cluster named after a since-deleted
    Memory object survived the migration (queued, not cleaned) and is gone
    from the PUBLISHED generation once the pass re-mints the clusters (every
    reader reads only the published generation; the superseded one is left
    to the generational swap's own lifecycle)."""
    def canonicals(notebook_id):
        with repo._runtime.database.connect() as db:
            return {r[0] for r in db.execute(
                "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? "
                "AND generation = (SELECT cluster_generation FROM unified_kg_state "
                "WHERE notebook_id=?)", (notebook_id, notebook_id)).fetchall()}

    assert "K-ghost memory" in canonicals(cases.NB_G)
    assert "K-~ko-h-gone" in canonicals(cases.NB_H)
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    assert "K-ghost memory" not in canonicals(cases.NB_G)
    assert "K-~ko-h-gone" not in canonicals(cases.NB_H)
    assert _marker(repo, cases.NB_G) == _marker(repo, cases.NB_H) == 1


def test_a_completed_notebook_is_never_queued_again(repo, events):
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    events.clear()

    # "next start": a fresh service instance, as startup would build it
    again = MemoryIsolationRebuild.for_repository(repo)
    assert again.pending_notebook_ids() == []
    assert again.schedule() is None
    assert again.run_pass() == {"completed": 0, "failed": 0, "deferred": 0, "skipped": 0}
    assert _isolation_events(events) == []


def test_failure_keeps_the_marker_and_the_next_start_retries(repo, events):
    database = repo._runtime.database
    with database.write() as db:
        db.execute("ALTER TABLE kg_cluster_scratch RENAME TO kg_cluster_scratch_away")

    tally = MemoryIsolationRebuild.for_repository(repo).run_pass()

    assert tally["failed"] == len(QUEUED) and tally["completed"] == 0
    for notebook in QUEUED:
        assert _marker(repo, notebook) == 0, notebook
    failed = [e for e in _isolation_events(events)
              if e["kind"] == "memory_isolation_rebuild_failed"]
    assert failed == [{
        "kind": "memory_isolation_rebuild_failed",
        "notebook_id": notebook,
        "stage": "rebuild",
        "error_class": "OperationalError",
    } for notebook in QUEUED]

    with database.write() as db:
        db.execute("ALTER TABLE kg_cluster_scratch_away RENAME TO kg_cluster_scratch")
    events.clear()
    retry = MemoryIsolationRebuild.for_repository(repo).run_pass()
    assert retry == DONE
    for notebook in QUEUED:
        assert _marker(repo, notebook) == 1, notebook


def test_busy_slot_defers_and_a_timer_rearms_a_fresh_pass(repo, events):
    """A busy notebook is deferred; the pass ENDS (its heavy-pool slot is
    free while waiting) and a timer re-submits a new pass after the delay."""
    held = repo.start_unified_kg_rebuild(cases.NB_F)
    armed: list[tuple[float, object]] = []
    service = MemoryIsolationRebuild.for_repository(
        repo, busy_retry_seconds=7.0, busy_retry_rounds=2,
        timer=lambda delay, callback: armed.append((delay, callback)),
    )

    first = service.run_pass()

    assert first == {"completed": len(QUEUED) - 1, "failed": 0, "deferred": 1,
                     "skipped": 0}
    assert _marker(repo, cases.NB_F) == 0
    assert [delay for delay, _cb in armed] == [7.0]
    # the user's rebuild ends (here: its submission is settled failed); the
    # timer fires: a fresh background pass finishes the notebook
    repo.fail_unified_kg_rebuild_submission(cases.NB_F, held["job_id"])
    handle = armed[0][1]()
    handle.join(timeout=60)
    assert not handle.is_alive()
    assert _marker(repo, cases.NB_F) == 1
    assert _kinds_for(events, cases.NB_F) == [
        "memory_isolation_rebuild_started",
        "memory_isolation_rebuild_deferred",
        "memory_isolation_rebuild_started",
        "memory_isolation_rebuild_completed",
    ]
    assert len(armed) == 1  # nothing deferred any more: no further re-arm


def test_rearming_stops_after_the_configured_rounds(repo):
    held = repo.start_unified_kg_rebuild(cases.NB_F)
    armed: list[float] = []
    service = MemoryIsolationRebuild.for_repository(
        repo, busy_retry_seconds=1.0, busy_retry_rounds=2,
        timer=lambda delay, callback: armed.append(delay),
    )
    for _ in range(4):
        assert service.run_pass()["deferred"] == 1
    assert armed == [1.0, 1.0]
    repo.fail_unified_kg_rebuild_submission(cases.NB_F, held["job_id"])


def test_the_pass_leaves_shared_only_totals_even_when_the_input_looks_unchanged(repo):
    """A queued notebook is really reclustered, never skipped on an unchanged
    input version: the skip path writes no end-state totals, so the
    pre-isolation ones -- Memory-derived rows counted -- would stay in
    ``unified_kg_status``.  Here the stored input version is made to match
    the current input exactly (the skip path would be taken) and the stored
    totals are the stale pre-isolation ones; after the pass they are the
    shared graph's."""
    lifecycle = repo._runtime.knowledge_lifecycle
    service = MemoryIsolationRebuild.for_repository(repo)
    # the pass's own pre-rebuild purge first, so it changes no input below
    service._purge_bridge_candidates(cases.NB_F)
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET cluster_input_version=?, object_count=999, "
            "relation_count=999, cluster_count=CASE WHEN cluster_count > 0 "
            "THEN cluster_count ELSE 1 END WHERE notebook_id=?",
            (lifecycle._cluster_input_version(cases.NB_F), cases.NB_F),
        )
    service.run_pass()
    with repo._runtime.database.connect() as db:
        shared_objects = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
            "WHERE ko.notebook_id=? AND ko.status!='deprecated' AND s.source_type<>'memory'",
            (cases.NB_F,)).fetchone()[0]
        shared_relations = db.execute(
            "SELECT COUNT(*) FROM knowledge_relations kr JOIN sources s ON s.id = kr.source_id "
            "WHERE kr.notebook_id=? AND s.source_type<>'memory'",
            (cases.NB_F,)).fetchone()[0]
    status = repo.unified_kg_status(cases.NB_F)
    assert _marker(repo, cases.NB_F) == 1
    assert (status["objects"], status["relations"]) == (shared_objects, shared_relations)


def test_a_notebook_rebuilt_meanwhile_is_skipped(repo, events):
    """The marker is read again right before each notebook: one cleared since
    the pending list was taken (a manual rebuild, another pass) is skipped
    without an event."""
    service = MemoryIsolationRebuild.for_repository(repo)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET memory_isolation_version = 1 "
                   "WHERE notebook_id = ?", (cases.NB_G,))
    service.pending_notebook_ids = lambda: list(QUEUED)
    tally = service.run_pass()
    assert tally["skipped"] == 1 and tally["completed"] == len(QUEUED) - 1
    assert _kinds_for(events, cases.NB_G) == []


def test_a_notebook_being_deleted_is_not_pending(repo):
    """Pending = marker 0 on a LIVE notebook (NOTEBOOK_LIVE_SQL)."""
    service = MemoryIsolationRebuild.for_repository(repo)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET status = 'deleting' WHERE id = ?",
                   (cases.NB_G,))
    assert cases.NB_G not in service.seed_check_notebook_ids()
    assert service.seed_check_count() == len(cases.SEED_CHECKED) - 1
    service.run_pass()
    assert _marker(repo, cases.NB_G) == 2  # untouched while being deleted


def test_bridge_merge_candidates_are_purged_before_the_rebuild(repo):
    """The migration's SQL cannot derive a bridge id (K- + the normalised
    name of a Memory concept); the pass purges the candidates naming one that
    no cluster carries, before the rebuild reads them."""
    def merge_ids():
        with repo._runtime.database.connect() as db:
            return {r[0] for r in db.execute(
                "SELECT id FROM concept_merge_candidates WHERE notebook_id=?",
                (cases.NB_F,)).fetchall()}

    assert cases.MERGE_CANDIDATES_BRIDGE <= merge_ids()
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    assert not (cases.MERGE_CANDIDATES_BRIDGE & merge_ids())
    assert "mc-2" in merge_ids()


def test_nothing_runs_before_readiness(repo, events):
    readiness.reset()
    try:
        service = MemoryIsolationRebuild.for_repository(repo)
        assert service.schedule() is None
        assert service.run_pass()["completed"] == 0
        assert _marker(repo, cases.NB_F) == 0
        assert _isolation_events(events) == []
    finally:
        readiness.mark_ready()


def test_schedule_runs_one_background_pass_after_readiness(repo, events):
    handle = MemoryIsolationRebuild.for_repository(repo).schedule()
    assert handle is not None
    handle.join(timeout=60)
    assert not handle.is_alive()
    assert _marker(repo, cases.NB_F) == 1


def test_startup_schedules_the_rebuild_only_after_mark_ready():
    """``run_startup`` reaches the rebuild step strictly after the lifecycle
    was marked ready (statement order in the function body)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(startup_warmup.run_startup)))
    calls = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.setdefault(node.func.id, node.lineno)
    assert "_rebuild_memory_isolated_notebooks" in calls
    assert calls["_mark_lifecycle_ready"] < calls["_rebuild_memory_isolated_notebooks"]


def test_startup_hook_never_raises(repo, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("scheduling exploded")

    monkeypatch.setattr(MemoryIsolationRebuild, "schedule", boom)
    startup_warmup._rebuild_memory_isolated_notebooks(repo)  # must not raise


def test_checkup_reports_the_read_only_item_until_the_rebuild_finished(repo):
    def h9(notebook_id):
        return next(c for c in repo.checkup.run(notebook_id).checks if c.code == "H9")

    pending = h9(cases.NB_F)
    assert (pending.count, pending.fix, pending.sample) == (1, "none", [])
    # marker 2 (awaiting the dangling-seed check) is not isolated either
    assert h9(cases.NB_C).count == 1
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    assert h9(cases.NB_F).count == 0
    assert h9(cases.NB_C).count == 0


def test_checkup_counts_approved_generic_promotions_of_memory_objects(repo):
    """H10 is read-only and never touched by the migration or the rebuild:
    the approved generic proposal of a Memory-derived object counts; the
    rejected/open ones, the creator-only Memory path and shared objects'
    proposals do not."""
    def h10(notebook_id):
        return next(c for c in repo.checkup.run(notebook_id).checks if c.code == "H10")

    approved = [p for p in cases.PROMOTIONS
                if p[4] == "approved" and p[3] != "memory"
                and p[2] in cases.MEMORY_OBJECTS]
    item = h10(cases.NB_F)
    assert (item.count, item.fix, item.sample) == (len(approved), "none", []) == (1, "none", [])
    assert h10(cases.NB_C).count == 0
    # a second approved generic proposal, of a SHARED object: not counted
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,"
            "status,created_at,updated_at) VALUES "
            "('pr-shared-approved',?,'ko-ldo-2','concept','approved',?,?)",
            (cases.NB_F, cases.NOW, cases.NOW),
        )
    assert h10(cases.NB_F).count == 1
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    assert h10(cases.NB_F).count == 1


# ------------------------------------------------ end-to-end isolation
# The isolated rebuild is isolated because the graph-build inputs leave out
# Memory-derived objects (E4-2, unified_kg_store seed readers), and a manual
# rebuild clears the marker through E4-2's line in finish_rebuild_state.


def _memory_object_ids(db):
    return {r[0] for r in db.execute(
        "SELECT ko.id FROM knowledge_objects ko JOIN sources s ON s.id = ko.source_id "
        "WHERE s.source_type = 'memory'").fetchall()}


def test_after_the_pass_no_derived_layer_holds_memory(repo):
    """End to end: after the pass, no Memory-derived object is a member of
    any cluster (of any generation), nor named by a mention edge, a
    co-mention pair or a community member. (Canonical ids and names are not
    compared with the Memory objects' names: the shared documents' own
    "Bandgap" / "secret project" legitimately re-mint K-bandgap and
    K-secret project, with only shared members.)"""
    MemoryIsolationRebuild.for_repository(repo).run_pass()
    with repo._runtime.database.connect() as db:
        memory = _memory_object_ids(db)
        clusters = db.execute(
            "SELECT canonical_id, member_object_id, canonical_name FROM concept_clusters "
            "WHERE notebook_id=?", (cases.NB_F,)).fetchall()
        mentions = db.execute(
            "SELECT claim_object_id, concept_canonical_id FROM mention_edges "
            "WHERE notebook_id=?", (cases.NB_F,)).fetchall()
        pairs = db.execute(
            "SELECT canonical_a, canonical_b FROM concept_comentions WHERE notebook_id=?",
            (cases.NB_F,)).fetchall()
        members = db.execute(
            "SELECT canonical_id FROM community_members WHERE notebook_id=?",
            (cases.NB_F,)).fetchall()

    def isolated():
        assert not {r[1] for r in clusters} & memory
        assert not {x for r in mentions for x in r} & memory
        assert not {x for r in pairs for x in r} & memory
        assert not {r[0] for r in members} & memory

    isolated()


def test_a_manual_rebuild_sets_the_marker(repo):
    """The ordinary 刷新图谱 path (no isolation worker involved) clears the
    marker through unified_kg_store.finish_rebuild_state (E4-2's line)."""
    job = repo.start_unified_kg_rebuild(cases.NB_F)
    repo.run_unified_kg_rebuild_job(cases.NB_F, job["job_id"])

    assert _marker(repo, cases.NB_F) == 1


def test_the_dangling_seed_check_pages_through_every_cluster(repo):
    """Every signal is read in bounded pages and still sees every row: with
    one key per page, each G notebook gets exactly its own signal (G: a name
    no member carries; H: an encoded seed object gone; GM: a mention row of a
    gone claim after a live one; GC: a community member naming nothing after
    a live one; GX / GD: dirty), and C -- whose BUILDING generation holds a
    name no member carries -- the public library and the copy are clean."""
    from app.repositories.sqlite.memory_isolation_store import MemoryIsolationStore

    with repo._runtime.database.connect() as db:
        for page in (1, 2, 5000):
            for notebook, signal in cases.SIGNALS.items():
                assert MemoryIsolationStore.seed_check_signal(
                    db, notebook, page_size=page) == signal, (notebook, page)
                assert MemoryIsolationStore.has_dangling_seed(
                    db, notebook, page_size=page) is (signal == "seed"), (notebook, page)
            for notebook, stale in ((cases.NB_GM, True), (cases.NB_GC, True),
                                    (cases.NB_GX, True), (cases.NB_C, False),
                                    (cases.NB_COPY, False), (cases.NB_GD, False)):
                assert MemoryIsolationStore.has_stale_reference(
                    db, notebook, page_size=page) is stale, (notebook, page)


def _gx_rows(repo):
    """GX's PUBLISHED derived rows (every reader reads only the published
    generation of clusters and communities; a superseded one is left to the
    generational swap's own lifecycle)."""
    with repo._runtime.database.connect() as db:
        texts = [r[0] for r in db.execute(
            "SELECT canonical_name || ' ' || COALESCE(canonical_description, '') "
            "FROM concept_clusters WHERE notebook_id = ? AND generation = "
            "(SELECT cluster_generation FROM unified_kg_state WHERE notebook_id = ?)",
            (cases.NB_GX, cases.NB_GX))]
        texts += [r[0] for r in db.execute(
            "SELECT COALESCE(title, '') || ' ' || COALESCE(summary, '') "
            "FROM communities WHERE notebook_id = ? AND generation = "
            "(SELECT community_generation FROM unified_kg_state WHERE notebook_id = ?)",
            (cases.NB_GX, cases.NB_GX))]
        mentions = {r[0] for r in db.execute(
            "SELECT claim_object_id FROM mention_edges WHERE notebook_id = ?",
            (cases.NB_GX,))}
        merge = {r[0] for r in db.execute(
            "SELECT id FROM concept_merge_candidates WHERE notebook_id = ?",
            (cases.NB_GX,))}
    return texts, mentions, merge


def test_the_deleted_memorys_text_does_not_survive_the_pass(repo):
    """The review's GX notebook: its cluster name is a surviving member's
    own ("Alpha"), so neither name nor seed dangles -- but it is dirty, and
    its cluster description and community were written while a since-deleted
    Memory was a member, with that Memory claim's mention row and merge
    candidates left behind. It must not be passed as clean: it is queued,
    the merge candidates naming nothing that exists go when it is queued,
    and after the pass none of the deleted Memory's text is left."""
    texts, mentions, merge = _gx_rows(repo)
    assert any(cases.GX_SECRET in t for t in texts)
    assert "ko-gx-memclaim-gone" in mentions
    assert cases.MERGE_CANDIDATES_STALE <= merge

    MemoryIsolationRebuild.for_repository(repo).run_pass()

    texts, mentions, merge = _gx_rows(repo)
    assert not any(cases.GX_SECRET in t for t in texts), texts
    assert "ko-gx-memclaim-gone" not in mentions
    assert not (cases.MERGE_CANDIDATES_STALE & merge)
    assert "mc-gx-keep" in merge  # names a live cluster and a live object
    assert "mc-gx-bridge" in merge  # bridge-shaped: a decision, kept
    assert _marker(repo, cases.NB_GX) == 1


def test_a_failing_rearm_callback_is_reported_and_rearms(repo, events):
    """The timer's callback runs on its own thread: an exception there (here:
    the pending count cannot be read) is reported as a failure event and the
    chain re-arms while rounds are left, then stops -- never a silent break."""
    armed: list = []
    service = MemoryIsolationRebuild.for_repository(
        repo, busy_retry_seconds=3.0, busy_retry_rounds=2,
        timer=lambda delay, callback: armed.append(callback),
    )

    def broken():
        raise RuntimeError("database gone")

    service.pending_count = broken
    service._rearm()
    assert len(armed) == 1
    assert armed[0]() is None  # does not raise
    assert len(armed) == 2     # re-armed, one round left -> used
    assert armed[1]() is None
    assert len(armed) == 2     # rounds exhausted: stopped
    failed = [e for e in _isolation_events(events)
              if e["kind"] == "memory_isolation_rebuild_failed"]
    assert failed == [{"kind": "memory_isolation_rebuild_failed", "notebook_id": "",
                       "stage": "rearm", "error_class": "RuntimeError"}] * 2


# ------------------------------- pre-isolation scale indexes (E4-6 ruling)
@pytest.fixture
def scale_repo(tmp_path, monkeypatch):
    """The shared world with a copy bound of ONE row, so every notebook but
    the one-object copy is non-copyable (the automatic index path owns it)."""
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    return _make_repo(tmp_path, monkeypatch)


def _make_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mkr.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    database = repository._runtime.database
    with database.write() as db:
        cases.seed(db, postgres=False)
    with database.write() as db:
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN memory_isolation_version")
        db.execute("PRAGMA user_version = 86")
    assert SqliteMigrator(database, repository.settings).migrate() == [87, 88]
    return repository


def _publish_manifests(scale, manifests):
    import json as _json

    for notebook_id, manifest in manifests.items():
        directory = scale.artifacts.scale_dir(notebook_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(_json.dumps(manifest))


#: published scale manifests: pre-isolation (no field) on a personal notebook
#: without Memory (C), a public library without Memory (PUB) and the copyable
#: copy (COPY); an isolated one (field present) on G
SCALE_MANIFESTS = {
    cases.NB_C: {"built_at": cases.NOW},
    cases.NB_PUB: {"built_at": cases.NOW},
    cases.NB_COPY: {"built_at": cases.NOW},
    cases.NB_G: {"built_at": cases.NOW, "memory_isolation": 1},
    # GX's KG rebuild is deferred in the test: its index must wait for it
    cases.NB_GX: {"built_at": cases.NOW},
}


def test_pre_isolation_scale_indexes_are_queued_for_a_full_build(scale_repo, monkeypatch):
    """Every non-copyable notebook whose KG is isolated and whose published
    scale index predates the isolation -- with or without Memory, any tier --
    gets a FULL build queued through the automatic path after the pass's
    rebuilds; an isolated index and a copyable notebook's are not queued; a
    notebook still awaiting its check or its rebuild (marker 2 / 0: here GX,
    whose rebuild is deferred) is not queued until that rebuild set its
    marker; a later start re-queues only what is still unstamped;
    SCALE_INDEX_AUTO_ENABLED off queues nothing."""
    seen: list = []
    log = scale_repo._runtime.event_log
    original = log.emit
    monkeypatch.setattr(log, "emit", lambda e, *a, **k: (seen.append(dict(e)), original(e, *a, **k))[1])
    scale = scale_repo._runtime.scale_artifacts
    _publish_manifests(scale, SCALE_MANIFESTS)
    assert scale.notebook_copy_stats(cases.NB_COPY)["copyable"]
    assert not scale.notebook_copy_stats(cases.NB_C)["copyable"]

    off = MemoryIsolationRebuild.for_repository(scale_repo, scale_auto_enabled=False)
    assert off.pre_isolation_scale_notebook_ids() == []

    service = MemoryIsolationRebuild.for_repository(
        scale_repo, scale_when="idle", timer=lambda delay, callback: None)
    expected = sorted([cases.NB_C, cases.NB_PUB])
    # before the pass every one of them is still marked 2: nothing to build
    assert service.pre_isolation_scale_notebook_ids() == []
    held = scale_repo.start_unified_kg_rebuild(cases.NB_GX)
    assert service.run_pass()["deferred"] == 1
    try:
        assert _marker(scale_repo, cases.NB_GX) == 0
        assert service.pre_isolation_scale_notebook_ids() == expected
        assert cases.NB_GX not in [e.get("notebook_id") for e in seen
                                   if e.get("kind") == "memory_isolation_scale_queued"]
        scale_repo.fail_unified_kg_rebuild_submission(cases.NB_GX, held["job_id"])
        service.run_pass()
        assert _marker(scale_repo, cases.NB_GX) == 1
        assert sorted(e["notebook_id"] for e in seen
                      if e.get("kind") == "memory_isolation_scale_queued"
                      and e["notebook_id"] == cases.NB_GX) == [cases.NB_GX]
        expected = sorted(expected + [cases.NB_GX])
        # (the pass's own KG rebuilds of large notebooks queue their usual
        # automatic builds too; ours are the FULL ones, and nothing for the
        # stamped G index or the copyable copy)
        queued = {nb: mode for nb, (mode, _ts) in scale.idle_queue.items()}
        assert {nb: queued.get(nb) for nb in expected} == {nb: "full" for nb in expected}
        assert cases.NB_COPY not in queued
        assert sorted({e["notebook_id"] for e in seen
                       if e.get("kind") == "memory_isolation_scale_queued"}) == expected
        # every marker settled; a fresh start still has scale work while the
        # manifests are unstamped, and none once an isolating build stamped them
        again = MemoryIsolationRebuild.for_repository(scale_repo)
        assert again.pending_count() == 0 and again.seed_check_count() == 0
        assert again.pre_isolation_scale_notebook_ids() == expected
        # scale work alone is reason enough to schedule a pass at a start
        handle = again.schedule()
        assert handle is not None
        handle.join(timeout=60)
        assert not handle.is_alive()
        _publish_manifests(scale, {nb: {"memory_isolation": 1} for nb in expected})
        assert again.pre_isolation_scale_notebook_ids() == []
        assert again.schedule() is None
    finally:
        for notebook_id in list(scale.idle_queue):
            scale.dequeue_idle(notebook_id)


def _gk_state(repo):
    with repo._runtime.database.connect() as db:
        decided = {r[0]: r[1] for r in db.execute(
            "SELECT id, status FROM concept_merge_candidates WHERE notebook_id = ?",
            (cases.NB_GK,))}
        members = {r[0]: r[1] for r in db.execute(
            "SELECT member_object_id, canonical_id FROM concept_clusters "
            "WHERE notebook_id = ? AND generation = (SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id = ?)", (cases.NB_GK, cases.NB_GK))}
    return decided, members


def test_curator_merge_decisions_survive_queueing_and_the_rebuild(repo):
    """A curator's decisions key on seeds, and canonical ids drift after a
    merge: GK's confirmed 'Alpha' + 'Alpha Beta' pair names K-alpha beta,
    which no cluster row carries -- the normal state of a decided pair, not a
    stale one. Queueing the dirty notebook keeps both decisions (only a
    sentinel side whose object is gone is purged), and the rebuild keeps the
    confirmed merge."""
    from app.repositories.sqlite.memory_isolation_store import MemoryIsolationStore

    decided, members = _gk_state(repo)
    assert decided == {d[0]: d[4] for d in cases.DECIDED}
    assert members["ko-gk-1"] == members["ko-gk-2"]
    with repo._runtime.database.write() as db:
        # direct: nothing of GK is purgeable; of GX only the sentinel
        assert MemoryIsolationStore.purge_stale_merge_candidates(db, cases.NB_GK) == 0
        db.execute("SAVEPOINT probe")
        assert MemoryIsolationStore.purge_stale_merge_candidates(
            db, cases.NB_GX) == len(cases.MERGE_CANDIDATES_STALE)
        db.execute("ROLLBACK TO probe")
        db.execute("RELEASE probe")

    MemoryIsolationRebuild.for_repository(repo).run_pass()

    decided, members = _gk_state(repo)
    assert _marker(repo, cases.NB_GK) == 1
    assert {k: v for k, v in decided.items() if k in dict.fromkeys(
        d[0] for d in cases.DECIDED)} == {d[0]: d[4] for d in cases.DECIDED}
    assert members["ko-gk-1"] == members["ko-gk-2"], members


@pytest.fixture
def viz_repo(tmp_path, monkeypatch):
    """Copy bound of one row (only the copy is copyable) and a synchronous
    viz budget of three objects."""
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    monkeypatch.setenv("VIZ_SYNC_BUILD_MAX_OBJECTS", "3")
    return _make_repo(tmp_path, monkeypatch)


def _publish_viz_manifests(scale, manifests):
    import json as _json

    for notebook_id, manifest in manifests.items():
        directory = scale.artifacts.viz_dir(notebook_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.json").write_text(_json.dumps(manifest))


def test_a_large_standalone_viz_without_a_scale_root_is_queued_too(viz_repo):
    """A notebook with only a standalone visualisation (no scale root) built
    before the isolation: queued for the full build when it has more objects
    than VIZ_SYNC_BUILD_MAX_OBJECTS (GK, 4) -- it would otherwise show no
    preview until a manual build; not when within the budget (G, 3: rebuilt
    on its first read), not when stamped (GM), not when copyable (COPY), not
    while its KG still awaits the isolated rebuild; a notebook with both
    roots, over the budget, is queued once, from its scale root (F)."""
    scale = viz_repo._runtime.scale_artifacts
    _publish_manifests(scale, {cases.NB_C: {"built_at": cases.NOW},
                               cases.NB_F: {"built_at": cases.NOW}})
    _publish_viz_manifests(scale, {
        cases.NB_F: {}, cases.NB_GK: {}, cases.NB_G: {}, cases.NB_COPY: {},
        cases.NB_GM: {"memory_isolation": 1},
    })
    service = MemoryIsolationRebuild.for_repository(viz_repo, scale_when="idle")
    assert service.pre_isolation_scale_notebook_ids() == []  # all still marked 2
    service.run_pass()
    try:
        assert service.pre_isolation_scale_notebook_ids() == sorted(
            [cases.NB_C, cases.NB_F, cases.NB_GK])
        queued = {nb: mode for nb, (mode, _ts) in scale.idle_queue.items()}
        assert queued.get(cases.NB_GK) == "full" and queued.get(cases.NB_C) == "full"
        assert cases.NB_COPY not in queued
    finally:
        for notebook_id in list(scale.idle_queue):
            scale.dequeue_idle(notebook_id)
