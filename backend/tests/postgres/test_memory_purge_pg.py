"""Memory hard delete, the member's own exit and the export on PostgreSQL (E5-2).

Runs the exact scenarios of ``tests/test_memory_purge.py`` (defined once in
``tests/memory_purge_cases.py``) against the production PostgreSQL facade,
plus the real-concurrency cases only PostgreSQL can exercise.
"""
from __future__ import annotations

import threading

import pytest

from app.services.memory_service import (
    ExitDisclosureRequired,
    MemberExitIncomplete,
)
from tests.memory_purge_cases import (
    CASES,
    EMBED_DIM,
    MONKEYPATCH_CASES,
    build_world,
    make_memory,
    member_memory_count,
    self_exit,
)


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    # Two concurrent exits each hold a connection while waiting on the other.
    postgres_settings.postgres_pool_max_size = 6
    postgres_settings.postgres_lock_timeout_seconds = 5
    postgres_settings.postgres_statement_timeout_seconds = 10
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def world(repo):
    return build_world(repo, postgres=True)


@pytest.mark.parametrize("case", sorted(CASES))
def test_memory_purge_scenario_pg(world, case):
    CASES[case](world)


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_memory_purge_fault_scenario_pg(world, monkeypatch, case):
    MONKEYPATCH_CASES[case](world, monkeypatch)


def _race(calls):
    outcomes = [None] * len(calls)
    barrier = threading.Barrier(len(calls))

    def run(index, call):
        barrier.wait()
        try:
            call()
            outcomes[index] = "ok"
        except ExitDisclosureRequired as exc:
            outcomes[index] = f"disclosure:{exc.memory_count}"
        except MemberExitIncomplete as exc:
            outcomes[index] = (
                f"incomplete:{exc.deleted_memory_count}:{exc.memory_count}"
            )
        except BaseException as exc:  # noqa: BLE001 - reported below
            outcomes[index] = f"{type(exc).__name__}: {exc}"

    threads = [
        threading.Thread(target=run, args=(index, call))
        for index, call in enumerate(calls)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    return outcomes


@pytest.mark.parametrize("attempt", range(3))
def test_two_concurrent_exits_of_the_same_member_never_fail_on_gone_rows(
    world, attempt
):
    """A double-clicked exit: the second purge finds sources and rows the
    first already removed. That is "already deleted", never a KeyError/500;
    the only other honest outcome is a fresh disclosure when the count it
    acknowledged changed underneath it."""
    for number in range(6):
        make_memory(world, f"race{attempt}-{number}", world.shared, world.alice)
    count = member_memory_count(world, world.shared, world.alice)
    outcomes = _race([
        lambda: self_exit(world, world.alice, world.shared, count),
        lambda: self_exit(world, world.alice, world.shared, count),
    ])
    assert "ok" in outcomes, outcomes
    assert all(
        outcome == "ok" or outcome.startswith("disclosure:") for outcome in outcomes
    ), outcomes
    assert not world.repo.is_member(world.shared, world.alice.id)
    assert member_memory_count(world, world.shared, world.alice) == 0
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM sources s WHERE s.source_type='memory' "
        "AND NOT EXISTS (SELECT 1 FROM memory_items m WHERE m.id=s.memory_id)"
    ) == 0


@pytest.mark.parametrize("attempt", range(3))
def test_an_exit_racing_a_save_never_deletes_the_unacknowledged_memory(
    world, attempt
):
    """Saves hold the membership row FOR SHARE, the exit takes it FOR UPDATE
    at claim and at finish: a save either lands before the claim (the count
    changes -> disclosure), during the purge (the finish keeps the
    membership -> disclosure) or fails after the membership is gone."""
    svc = world.repo._runtime.memory_service
    saved: list[str] = []

    def save():
        for number in range(4):
            try:
                item = svc.create_candidate(
                    world.shared, world.alice.id, None, f"race-save-{attempt}-{number}",
                    f"Saved {number}", "Saved during the exit.", [], "reason", {}, [],
                )
                saved.append(item.id)
            except (KeyError, PermissionError):
                pass

    outcomes = _race([lambda: self_exit(world, world.alice, world.shared, 1), save])
    assert outcomes[1] == "ok", outcomes
    assert outcomes[0] == "ok" or outcomes[0].startswith(
        ("disclosure:", "incomplete:1:")
    ), outcomes
    surviving = {
        row["id"]
        for row in world.sql.rows(
            "SELECT id FROM memory_items WHERE notebook_id=? AND created_by=?",
            (world.shared, world.alice.id),
        )
    }
    # Every saved Memory still exists unless it was never created; the one
    # acknowledged Memory is either deleted (exit ran) or the exit refused.
    assert set(saved) <= surviving
    if outcomes[0] == "ok":
        assert not world.repo.is_member(world.shared, world.alice.id)
        assert surviving == set(saved) == set()
    else:
        assert world.repo.is_member(world.shared, world.alice.id)
