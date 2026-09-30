"""Member exit (contract v2) under real concurrency on PostgreSQL.

The single-connection outcomes of every contract row run on both backends in
``tests/memory_purge_cases.py``. This file drives the interleavings only
PostgreSQL can produce (READ COMMITTED, row locks, a second connection that
commits between the exit's steps), starting from the quality review's
harness, and asserts the state each one must leave:

* S1  a save in flight at the claim (holding the membership row FOR SHARE):
      the claim waits for it and refuses with the new count — nothing is
      deleted. This is what makes the claim's row lock load-bearing.
* S2  a candidate confirmed (inline KG ingest) during the purge;
  S2b its ingest running only after the exit finished;
  S3  a candidate rejected during the purge;
* S4  the owner removing the member / unsharing / kicking everyone /
      deleting the notebook / removing and re-adding the member during the
      purge;
* S8  the process dying between pages, then the retry from the disclosure;
* S9  a transfer-move out of the notebook during the purge;
* two members leaving at once whose Memory objects were merged into the
  same shared objects in opposite source orders (no deadlock).
"""
from __future__ import annotations

import threading
import time

import pytest

from app.repositories.postgres import governance_store as governance_module
from app.repositories.postgres import memory_store as memory_store_module
from app.services.memory_service import (
    ExitDisclosureRequired,
    MemberExitFailed,
    MemberExitIncomplete,
)
from tests.memory_purge_cases import (
    EMBED_DIM,
    _fused_doc_object,
    build_world,
    disclosure,
    legacy_merge,
    make_memory,
    member_memory_count,
    plain_memory,
    self_exit,
    service,
)

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    postgres_settings.postgres_pool_max_size = 8
    postgres_settings.postgres_lock_timeout_seconds = 10
    postgres_settings.postgres_statement_timeout_seconds = 20
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def world(repo):
    return build_world(repo, postgres=True)


def _orphans(world) -> int:
    return world.sql.count(
        "SELECT COUNT(*) AS c FROM sources s WHERE s.source_type='memory' "
        "AND NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id=s.memory_id)"
    )


def _outcome(fn) -> str:
    """The HTTP outcome the route would answer for this exit call."""
    try:
        deleted = fn()
    except ExitDisclosureRequired as exc:
        return f"409 exit_disclosure_required {exc.memory_count}"
    except MemberExitIncomplete as exc:
        return f"409 exit_incomplete {exc.deleted_memory_count} {exc.memory_count}"
    except MemberExitFailed as exc:
        return f"503 exit_incomplete {exc.deleted_memory_count} {exc.memory_count}"
    return f"200 {deleted}"


def _after_page(monkeypatch, world, hook, page: int = 1) -> None:
    svc = service(world)
    original = svc._purge_page
    calls = [0]

    def wrapped(user_id, refs):
        deleted = original(user_id, refs)
        calls[0] += 1
        if calls[0] == page:
            hook()
        return deleted

    monkeypatch.setattr(svc, "_purge_page", wrapped)


def test_s1_a_save_in_flight_at_the_claim_is_waited_for_and_refused(world, monkeypatch):
    """Item 4 / quality P2-5: the save holds the membership row FOR SHARE
    until it commits; the claim's FOR UPDATE waits for it and then counts 2,
    so the acknowledgement of 1 is refused and NOTHING is deleted. Without
    the claim lock the exit would delete 1 and then stop half-done."""
    inside, release = threading.Event(), threading.Event()
    original = memory_store_module.MemoryStore._ensure_initial_revision_on

    def parked(self, db, item, created, changed_by, reason):
        original(self, db, item, created, changed_by, reason)
        if item.title == "Inflight":
            inside.set()
            release.wait(10)

    monkeypatch.setattr(
        memory_store_module.MemoryStore, "_ensure_initial_revision_on", parked
    )
    results: dict[str, str] = {}

    def save() -> None:
        service(world).create_candidate(
            world.shared, world.alice.id, None, "rq-inflight", "Inflight", "x",
            [], "r", {}, [],
        )
        results["save"] = "ok"

    saver = threading.Thread(target=save)
    saver.start()
    assert inside.wait(10)
    leaver = threading.Thread(target=lambda: results.__setitem__(
        "exit", _outcome(lambda: self_exit(world, world.alice, world.shared, 1))
    ))
    leaver.start()
    time.sleep(1.0)  # the exit is now blocked on the membership row lock
    assert "exit" not in results
    release.set()
    saver.join(20)
    leaver.join(30)
    assert results == {"save": "ok", "exit": "409 exit_disclosure_required 2"}
    assert member_memory_count(world, world.shared, world.alice) == 2
    assert world.repo.is_member(world.shared, world.alice.id)


def test_s2_a_candidate_confirmed_during_the_purge_is_deleted_with_its_source(
    world, monkeypatch
):
    svc = service(world)
    candidate = svc.create_candidate(
        world.shared, world.alice.id, None, "rq-c", "Cand", "cand body", [], "r", {}, []
    )
    original = svc.store.bulk_delete_memories
    done: list[int] = []

    def confirm_then_delete(user_id, ids):
        if not done:
            done.append(1)
            svc.confirm(candidate.id, world.alice.id)
            assert svc.memory_kg.memory_source_id(candidate.id) is not None
        return original(user_id, ids)

    monkeypatch.setattr(svc.store, "bulk_delete_memories", confirm_then_delete)
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, 2)) == "200 2"
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert _orphans(world) == 0


def test_s2b_an_ingest_that_runs_after_the_exit_leaves_no_orphan(world, monkeypatch):
    svc = service(world)
    candidate = svc.create_candidate(
        world.shared, world.alice.id, None, "rq-c2", "Cand2", "body 2", [], "r", {}, []
    )
    deferred: list = []
    original = svc.store.bulk_delete_memories

    def confirm_then_delete(user_id, ids):
        if not deferred:
            svc.kg_ingest_scheduler = lambda fn, item: deferred.append((fn, item))
            svc.confirm(candidate.id, world.alice.id)
            svc.kg_ingest_scheduler = lambda fn, item: fn(item)
        return original(user_id, ids)

    monkeypatch.setattr(svc.store, "bulk_delete_memories", confirm_then_delete)
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, 2)) == "200 2"
    for fn, item in deferred:
        fn(item)
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert _orphans(world) == 0


def test_s3_a_candidate_rejected_during_the_purge(world, monkeypatch):
    svc = service(world)
    candidate = svc.create_candidate(
        world.shared, world.alice.id, None, "rq-r", "CandR", "b", [], "r", {}, []
    )
    rejected: list[str] = []

    def reject() -> None:
        try:
            svc.reject(candidate.id, world.alice.id)
            rejected.append("ok")
        except KeyError:
            rejected.append("gone")

    _after_page(monkeypatch, world, reject)
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, 2)) == "200 2"
    assert rejected == ["gone"]  # one page: the candidate was already deleted
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert not world.repo.is_member(world.shared, world.alice.id)


@pytest.mark.parametrize(
    "action",
    ["remove_member", "unshare", "kick_all", "delete_notebook", "remove_and_readd"],
)
def test_s4_other_actions_during_the_purge(world, monkeypatch, action):
    """The exit deletes exactly what was acknowledged and reports it; the
    owner's actions delete nothing of anyone's; a member removed and added
    back meanwhile keeps the NEW membership (item 5)."""
    for number in range(250):
        plain_memory(world, world.shared, world.alice, f"s4-{number}", "confirmed")
    count = member_memory_count(world, world.shared, world.alice)
    bob_before = member_memory_count(world, world.shared, world.bob)
    repo = world.repo

    def hook() -> None:
        if action == "remove_member":
            repo.remove_member(world.shared, world.alice.id)
        elif action == "unshare":
            repo.unshare_notebook(world.shared)
        elif action == "kick_all":
            repo.kick_all_members(world.shared)
        elif action == "delete_notebook":
            repo.delete_notebook(world.shared)
        else:
            repo.remove_member(world.shared, world.alice.id)
            repo.add_member(world.shared, world.alice.id)

    _after_page(monkeypatch, world, hook)
    result = _outcome(lambda: self_exit(world, world.alice, world.shared, count))
    assert _orphans(world) == 0
    if action == "delete_notebook":
        # The notebook delete took the rest with it; this request reports
        # only what IT deleted (the first page).
        assert result == "200 200"
        return
    assert result == f"200 {count}"
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert member_memory_count(world, world.shared, world.bob) == bob_before
    assert repo.is_member(world.shared, world.alice.id) is (action == "remove_and_readd")


def test_s8_a_process_dying_between_pages_then_the_retry(world, monkeypatch):
    """The exit dies after page 1: still a member, the rest remains, the next
    disclosure says how many; the stale acknowledgement is refused and the
    retry with the new count finishes."""
    for number in range(450):
        plain_memory(world, world.shared, world.alice, f"s8-{number}", "confirmed")
    count = member_memory_count(world, world.shared, world.alice)

    class Killed(BaseException):
        pass

    def die() -> None:
        raise Killed()

    _after_page(monkeypatch, world, die)
    with pytest.raises(Killed):
        self_exit(world, world.alice, world.shared, count)
    monkeypatch.undo()
    assert world.repo.is_member(world.shared, world.alice.id)
    remaining = disclosure(world, world.alice, world.shared)
    assert remaining == count - 200
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, count)) == (
        f"409 exit_disclosure_required {remaining}"
    )
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, remaining)) == (
        f"200 {remaining}"
    )
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert _orphans(world) == 0


def test_s9_a_transfer_move_during_the_purge(world, monkeypatch):
    """The Memory the move targets was already deleted by the exit's page:
    the move fails for it, nothing is copied, the exit reports both."""
    svc = service(world)
    extra = plain_memory(world, world.shared, world.alice, "s9", "confirmed")
    home_before = member_memory_count(world, world.alice_home, world.alice)
    moved: dict = {}

    def hook() -> None:
        moved["result"] = svc.transfer(
            world.alice.id, [extra], world.alice_home, "move", extract_kg=False
        )

    _after_page(monkeypatch, world, hook)
    assert _outcome(lambda: self_exit(world, world.alice, world.shared, 2)) == "200 2"
    assert [item["status"] for item in moved["result"]] == ["failed"]
    assert member_memory_count(world, world.alice_home, world.alice) == home_before
    assert _orphans(world) == 0


def test_two_leavers_merged_into_the_same_shared_objects_do_not_deadlock(
    world, monkeypatch
):
    """Item 6: Carol's two Memory sources were merged into shared objects Y
    and X, Dave's into X and Y — opposite orders by source. Each exit takes
    the shared rows' locks in ONE id-ordered statement, so the two queue
    instead of deadlocking; a barrier right after the strip makes the two
    transactions overlap on every run. (Locking source by source, in source
    order, deadlocks here: Carol holds Y and waits for X, Dave the reverse.)"""
    shared_x = _fused_doc_object(world, "Shared x notes")
    shared_y = _fused_doc_object(world, "Shared y notes")
    shared_x, shared_y = sorted([shared_x, shared_y])
    users = {
        "carol": world.repo.create_user("c00100005", "pw123456"),
        "dave": world.repo.create_user("d00100006", "pw123456"),
    }
    for user in users.values():
        world.repo.add_member(world.shared, user.id)
    projections = {
        name: sorted(
            (make_memory(world, f"dl-{name}-{n}", world.shared, user) for n in range(2)),
            key=lambda projection: projection.source_id,
        )
        for name, user in users.items()
    }
    targets = {"carol": (shared_y, shared_x), "dave": (shared_x, shared_y)}
    for name, (first, second) in targets.items():
        for projection, target in zip(projections[name], (first, second)):
            legacy_merge(world, world.shared, projection.object_ids[0], target)
    counts = {
        name: disclosure(world, user, world.shared) for name, user in users.items()
    }
    assert counts == {"carol": 2, "dave": 2}
    barrier = threading.Barrier(2)
    original = governance_module.GovernanceStore.strip_sources_evidence_on
    stripped_by: dict[str, set[str]] = {}

    def strip_then_meet(connection, notebook_id, source_ids, now):
        stripped = original(connection, notebook_id, source_ids, now)
        stripped_by.setdefault(threading.current_thread().name, set()).update(stripped)
        try:
            barrier.wait(timeout=3)
        except threading.BrokenBarrierError:
            pass
        return stripped

    monkeypatch.setattr(
        governance_module.GovernanceStore, "strip_sources_evidence_on",
        staticmethod(strip_then_meet),
    )
    results: dict[str, str] = {}
    threads = [
        threading.Thread(target=lambda name=name: results.__setitem__(
            name,
            _outcome(lambda: self_exit(world, users[name], world.shared, counts[name])),
        ))
        for name in users
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert results == {"carol": "200 2", "dave": "200 2"}
    # Both exits really took the shared rows' locks (each stripped X and Y).
    assert [sorted(ids) for ids in stripped_by.values()] == [
        sorted([shared_x, shared_y]), sorted([shared_x, shared_y]),
    ], stripped_by
    for target in (shared_x, shared_y):
        assert world.sql.count(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE id=?", (target,)
        ) == 1
    assert _orphans(world) == 0
