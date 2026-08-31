"""W-CLI T-W1 — per-notebook build claim on the service side.

The cross-process half (real advisory locks, session death) lives in the
PostgreSQL lane: ``tests/postgres/test_scale_build_lock.py``. Everything here
is backend-neutral admission/handoff/swap discipline, exercised through the
SQLite repository with a stub lock standing in for the PostgreSQL adapter.
"""
from __future__ import annotations

import json
import threading

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.scale_build_lock import (
    UNSUPPORTED_SCALE_BUILD_LOCK,
    ScaleBuildBusy,
    ScaleBuildLock,
    ScaleBuildLockLost,
    UnsupportedScaleBuildLock,
    advisory_lock_key,
    advisory_lock_oid,
)
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repository = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _add_source(repo, notebook_id, *, source_id, object_id, chunk_id, day):
    now = f"2026-08-{day:02d}T00:00:00"
    text = f"concept {object_id}"
    vector = json.dumps(
        repo._runtime.models.embedding("retrieval_query_embedding")
        .embed_texts([text])[0]
    )
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, "md", "ready", now, now),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (chunk_id, notebook_id, source_id, text, "", "[]", now),
        )
        db.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (chunk_id, notebook_id, vector, now),
        )
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,"
            "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                object_id,
                notebook_id,
                "concept",
                "approved",
                "",
                json.dumps({"name": text}),
                "[]",
                source_id,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings "
            "(object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
            (object_id, notebook_id, vector, now),
        )


@pytest.fixture
def indexed_notebook(repo):
    notebook = repo.create_notebook(NotebookCreate(name="claimed"))
    _add_source(
        repo, notebook.id, source_id="s1", object_id="o1", chunk_id="c1", day=1
    )
    repo.rebuild_unified_kg(notebook.id)
    repo.build_scale_index(notebook.id)
    return notebook.id


class _StubLock:
    """A grantable/withdrawable stand-in for the PostgreSQL handle."""

    supported = True

    def __init__(self, held: bool = True) -> None:
        self.held = held
        self.releases = 0

    def verify_held(self) -> bool:
        return self.held

    def release(self) -> None:
        self.releases += 1
        self.held = False


def _scale(repo):
    return repo._runtime.scale_artifacts


# ---------------------------------------------------------------- sentinel --

def test_sqlite_reports_an_explicit_unsupported_sentinel(repo):
    """Not a nullcontext: the offline CLI refuses a SQLite deployment on the
    strength of ``supported`` being False, and a no-op would read as granted."""
    handle = repo._runtime.database.try_scale_build_lock("nb-anything")

    assert handle is UNSUPPORTED_SCALE_BUILD_LOCK
    assert isinstance(handle, UnsupportedScaleBuildLock)
    assert isinstance(handle, ScaleBuildLock)
    assert handle.supported is False
    # The sentinel is still a usable handle for the serving process, whose
    # mutual exclusion falls back to the in-process ``building`` claim.
    assert handle.verify_held() is True
    handle.release()


def test_the_sqlite_runtime_is_wired_to_that_sentinel(repo):
    assert (
        _scale(repo)._acquire_scale_build_lock("nb-x")
        is UNSUPPORTED_SCALE_BUILD_LOCK
    )


def test_advisory_keys_normalize_into_the_int32_range():
    keys = [advisory_lock_key(f"nb-{index}") for index in range(200)]

    assert all(-(2**31) <= key < 2**31 for key in keys)
    # Roughly half of all identifiers hash negative — the reason the two-argument
    # advisory form is used and the reason pg_locks needs the unsigned rendering.
    assert any(key < 0 for key in keys)
    assert all(0 <= advisory_lock_oid(key) < 2**32 for key in keys)
    assert advisory_lock_oid(-1) == 0xFFFFFFFF


# ------------------------------------------------- swap re-verification ----

def test_swap_is_refused_and_tmp_kept_when_the_claim_was_lost(
    repo, indexed_notebook
):
    """The swap is the only destructive step; a claim that evaporated during
    the build means somebody else may own the directory now."""
    store = repo._runtime.scale_artifact_store
    live = store.scale_dir(indexed_notebook)
    before = (live / "manifest.json").read_bytes()
    temporary = store.prepare_fold_directory(indexed_notebook)
    (temporary / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ScaleBuildLockLost):
        store.swap_fold_directory(
            indexed_notebook, temporary, verify_held=lambda: False
        )

    assert (live / "manifest.json").read_bytes() == before
    assert temporary.exists(), "the staged build must be left for the operator"


def test_a_held_claim_lets_the_swap_through(repo, indexed_notebook):
    store = repo._runtime.scale_artifact_store
    temporary = store.prepare_fold_directory(indexed_notebook)
    (temporary / "manifest.json").write_text('{"marker": 1}', encoding="utf-8")

    store.swap_fold_directory(
        indexed_notebook, temporary, verify_held=lambda: True
    )

    published = store.scale_dir(indexed_notebook) / "manifest.json"
    assert json.loads(published.read_text(encoding="utf-8")) == {"marker": 1}


def test_a_full_build_re_verifies_its_claim_before_publishing(
    repo, indexed_notebook, monkeypatch
):
    """Mutation anchor: drop ``verify_held=`` from the builder's ``save_full``
    call (or the check inside ``swap_fold_directory``) and this goes green
    while a lost claim silently republishes over another writer."""
    scale = _scale(repo)
    store = repo._runtime.scale_artifact_store
    before = (store.scale_dir(indexed_notebook) / "manifest.json").read_bytes()
    lost = _StubLock(held=False)
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: lost)

    with pytest.raises(ScaleBuildLockLost):
        scale.build(indexed_notebook)

    assert (
        store.scale_dir(indexed_notebook) / "manifest.json"
    ).read_bytes() == before
    # A failed build still surrenders its claim and its in-process slot.
    assert lost.releases == 1
    assert indexed_notebook not in scale.building
    assert scale._scale_build_lock_handles == {}


def test_verification_reads_the_claim_registered_for_that_notebook(repo):
    scale = _scale(repo)
    handle = _StubLock()
    scale._register_scale_build_lock("nb-a", handle)

    assert scale.verify_scale_build_lock("nb-a") is True
    handle.held = False
    assert scale.verify_scale_build_lock("nb-a") is False
    # An unrelated notebook holds no claim of its own and loses nothing.
    assert scale.verify_scale_build_lock("nb-b") is True

    scale._discard_scale_build_lock("nb-a")
    assert handle.releases == 1
    assert scale.verify_scale_build_lock("nb-a") is True


def test_an_unverifiable_claim_counts_as_lost(repo):
    class _Exploding:
        supported = True

        def verify_held(self):
            raise RuntimeError("session gone")

        def release(self):
            return None

    scale = _scale(repo)
    scale._register_scale_build_lock("nb-a", _Exploding())

    assert scale.verify_scale_build_lock("nb-a") is False


# -------------------------------------------------- synchronous claiming ---

def test_facade_build_scale_index_now_excludes_a_concurrent_builder(
    repo, indexed_notebook
):
    """``build()`` used to claim nothing at all, so two callers could stage and
    swap the same directory. The facade path inherits the claim now."""
    scale = _scale(repo)
    with scale.building_lock:
        scale.building.add(indexed_notebook)
    try:
        with pytest.raises(ScaleBuildBusy):
            repo.build_scale_index(indexed_notebook)
    finally:
        with scale.building_lock:
            scale.building.discard(indexed_notebook)


def test_a_busy_cross_process_claim_refuses_the_synchronous_build(
    repo, indexed_notebook, monkeypatch
):
    scale = _scale(repo)
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)

    with pytest.raises(ScaleBuildBusy):
        scale.build(indexed_notebook)

    assert indexed_notebook not in scale.building


def test_a_probe_that_raises_fails_closed(repo, indexed_notebook, monkeypatch):
    def explode(_notebook_id):
        raise RuntimeError("database unreachable")

    scale = _scale(repo)
    monkeypatch.setattr(scale, "_scale_build_lock", explode)

    with pytest.raises(ScaleBuildBusy):
        scale.build(indexed_notebook)


def test_a_synchronous_build_releases_its_claim_on_both_outcomes(
    repo, indexed_notebook, monkeypatch
):
    scale = _scale(repo)
    handles: list[_StubLock] = []

    def issue(_notebook_id):
        handle = _StubLock()
        handles.append(handle)
        return handle

    monkeypatch.setattr(scale, "_scale_build_lock", issue)
    scale.build(indexed_notebook)
    monkeypatch.setattr(
        scale.builder,
        "build",
        lambda notebook_id, on_stage=None: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
    )
    with pytest.raises(RuntimeError, match="boom"):
        scale.build(indexed_notebook)

    assert [handle.releases for handle in handles] == [1, 1]
    assert scale._scale_build_lock_handles == {}
    assert indexed_notebook not in scale.building


# ------------------------------------------------------------- admission ---

def test_admission_hands_its_claim_to_the_worker_and_the_worker_releases_it(
    repo, indexed_notebook, monkeypatch
):
    scale = _scale(repo)
    handles: list[_StubLock] = []

    def issue(_notebook_id):
        # A fresh handle per probe, exactly as the adapter behaves: the worker
        # tail's own follow-up probe must not be able to release ours.
        handle = _StubLock()
        handles.append(handle)
        return handle

    monkeypatch.setattr(scale, "_scale_build_lock", issue)
    seen: dict[str, object] = {}
    finished = threading.Event()

    def fake_build(notebook_id, on_stage=None):
        seen["registered"] = dict(scale._scale_build_lock_handles)
        seen["verified"] = scale.verify_scale_build_lock(notebook_id)
        return {}

    monkeypatch.setattr(scale.builder, "build", fake_build)
    monkeypatch.setattr(
        scale, "notify_index_done", lambda _nb: finished.set()
    )

    assert scale._run_scale_op(indexed_notebook, "full") is True
    assert finished.wait(timeout=10)
    admitted = handles[0]
    for _ in range(500):
        if admitted.releases:
            break
        threading.Event().wait(0.02)

    # The worker — not the admitting thread — saw the claim and released it.
    assert seen["registered"] == {indexed_notebook: admitted}
    assert seen["verified"] is True
    assert admitted.releases == 1
    # Every later probe (the coalesced follow-up, the slot handoff) started and
    # ended nothing, so none of those handles leaked either.
    assert all(handle.releases == 1 for handle in handles)
    assert scale._scale_build_lock_handles == {}


def test_admission_releases_its_claim_when_nothing_starts(
    repo, indexed_notebook, monkeypatch
):
    """Handoff discipline: every non-started exit of the claimed half must
    release. Mutation anchor — delete the ``finally`` in
    ``_admit_claimed_scale_op`` and the leaked handle shows up here."""
    scale = _scale(repo)
    handle = _StubLock()
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: handle)
    # Nothing to claim: the idle entry a drain thinks it is consuming is gone.
    assert scale._admit_scale_op(indexed_notebook, "auto", claim_idle=True) == (
        "refused"
    )

    assert handle.releases == 1
    assert scale._scale_build_lock_handles == {}


def test_admission_releases_its_claim_when_the_request_is_only_parked(
    repo, indexed_notebook, monkeypatch
):
    scale = _scale(repo)
    handle = _StubLock()
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: handle)
    # Occupy every concurrency slot so admission can only park the request.
    taken = []
    while scale._scale_build_semaphore.acquire(blocking=False):
        taken.append(1)
    try:
        assert scale._admit_scale_op(indexed_notebook, "full") == "queued"
    finally:
        for _ in taken:
            scale._scale_build_semaphore.release()

    assert indexed_notebook in scale._scale_pending
    assert handle.releases == 1


def test_a_busy_claim_queues_the_followup_and_records_no_backoff(
    repo, indexed_notebook, monkeypatch
):
    """An offline build legitimately runs for 40 minutes. Charging that to this
    notebook's failure backoff would push automatic retries to the 30-minute
    ceiling for a build that never even started."""
    scale = _scale(repo)
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)

    outcome = scale._admit_scale_op(
        indexed_notebook, "full", supersede_idle=True, queue_full_if_busy=True
    )

    assert outcome == "queued"
    assert scale.idle_queue[indexed_notebook][0] == "full"
    assert scale._scale_failure_state == {}
    assert indexed_notebook not in scale.building
    # Nothing was consumed and no worker exists, so no slot was spent either.
    assert scale._slot_available() is True


def test_a_busy_claim_leaves_a_drained_queue_entry_where_it_was(
    repo, indexed_notebook, monkeypatch
):
    scale = _scale(repo)
    scale.idle_queue[indexed_notebook] = ("fold", "2026-08-31T00:00:00+00:00")
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)

    assert scale._admit_scale_op(
        indexed_notebook, "auto", claim_idle=True
    ) == "refused"

    assert scale.idle_queue[indexed_notebook][0] == "fold"
    assert scale._scale_failure_state == {}


def test_a_failed_worker_start_rolls_back_everything_it_took(
    repo, indexed_notebook, monkeypatch
):
    """Mutation anchor for the rollback branch: drop any one of the four
    restorations (claim, ticket, ``building``, queue entries) and exactly one
    assertion below turns red. Recording a failure here would be a fifth bug —
    the build was never attempted, so the backoff window must not move."""
    scale = _scale(repo)
    handle = _StubLock()
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: handle)
    monkeypatch.setattr(
        scale,
        "_start_daemon",
        lambda name, target: (_ for _ in ()).throw(RuntimeError("no thread")),
    )
    stamp = "2026-08-31T00:00:00+00:00"
    scale.idle_queue[indexed_notebook] = ("fold", stamp)
    scale._scale_pending[indexed_notebook] = ("full", stamp)

    with pytest.raises(RuntimeError, match="no thread"):
        scale._admit_scale_op(indexed_notebook, "auto", claim_idle=True)

    assert handle.releases == 1
    assert scale._scale_build_lock_handles == {}
    assert scale._slot_available() is True
    assert indexed_notebook not in scale.building
    assert scale.idle_queue[indexed_notebook] == ("fold", stamp)
    assert scale._scale_pending[indexed_notebook] == ("full", stamp)
    assert scale._scale_failure_state == {}
