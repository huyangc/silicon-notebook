"""PostgreSQL ``MemorySuggestStore`` (0087). SQLite twin:
``tests/test_memory_suggest_store.py``; shared cases:
``tests/memory_suggest_store_cases.py``."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.domain.memory_suggest_contracts import NEIGHBOR_VECTOR_SCAN_MAX
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from tests import memory_suggest_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
    return database


def _new_id_factory():
    counter = iter(range(1, 1_000_000))
    return lambda prefix: f"{prefix}-{next(counter)}"


@pytest.fixture
def env(database) -> cases.Env:
    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    return cases.Env(
        store=MemorySuggestStore(database, new_id=_new_id_factory()),
        run=run,
        rows=rows,
        json=Jsonb,
    )


def test_set_memory_mode_single_transaction_clears_jobs(env):
    cases.assert_set_memory_mode_single_transaction_clears_jobs(env)


def test_quota_resets_on_utc_day_change(env):
    cases.assert_quota_resets_on_utc_day_change(env)


def test_quota_zero_rows_at_cap(env):
    cases.assert_quota_zero_rows_at_cap(env)


def test_quota_reservation_never_exceeds_cap_across_notebooks(env):
    """Concurrent reservations (jobs of different notebooks, separate
    connections released together by a barrier) never exceed the cap: the
    conditional UPDATE's row lock serialises them."""
    env.seed()
    cases.enable(env)
    workers = 8
    barrier = threading.Barrier(workers)
    granted: list[bool] = []
    lock = threading.Lock()

    def reserve(index: int) -> None:
        barrier.wait(timeout=10)
        result = env.store.reserve_daily_quota(
            cases.OWNER, today_utc="2026-10-01", cap=3, now=cases.at(index))
        with lock:
            granted.append(result)

    threads = [threading.Thread(target=reserve, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(granted) == workers
    assert granted.count(True) == 3
    assert env.store.user_state(cases.OWNER)["quota_used"] == 3


def test_note_signal_single_statement_takes_trigger_ask_created_at(env):
    cases.assert_note_signal_takes_trigger_ask_created_at(env)


def test_note_signal_respects_current_opt_in(env):
    cases.assert_note_signal_respects_current_opt_in(env)


def test_note_signal_foreign_ask_id_falls_back_to_now(env):
    cases.assert_note_signal_foreign_ask_id_falls_back_to_now(env)


def test_note_signal_increments_only_on_conflict(env):
    cases.assert_note_signal_increments_only_on_conflict(env)


def test_note_signal_counts_only_while_the_switch_is_on(env):
    cases.assert_note_signal_counts_only_while_the_switch_is_on(env)


def test_note_signal_without_read_access_writes_nothing(env):
    cases.assert_note_signal_without_read_access_writes_nothing(env)


def _grant_other(env) -> None:
    env.run(
        "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,role,"
        "created_by,created_at) VALUES (?,?,?,?,?,?,?)",
        ("g-other", cases.NB, "user", cases.OTHER, "viewer", cases.OWNER, cases.at(0)),
    )


def _other_job(env) -> list[dict]:
    return env.rows(
        "SELECT pending_signal FROM memory_suggest_jobs WHERE notebook_id = ? "
        "AND owner_id = ?",
        (cases.NB, cases.OTHER),
    )


def test_note_signal_cannot_recreate_a_job_row_across_a_concurrent_delete_user_grant(
    env, database,
):
    """Plan B9, both orders of ``GroupStore.delete_user_grant`` (grant row deleted,
    then the job rows of owners left without read access, one transaction)
    against a completed ask's ``note_signal`` for the grantee.

    1. ``delete_user_grant`` first, uncommitted: ``note_signal`` waits on the
       notebook root, sees the grant gone once the delete
       commits, and writes nothing -- the row that cleanup already deleted is
       not recreated. Without the locked probe it would read the old edge from
       its snapshot and re-insert the row.
    2. ``note_signal`` first, uncommitted: ``delete_user_grant`` waits on the
       share-locked root; once the signal commits, the delete's own cleanup
       sees the new row and deletes it.
    """
    cases.seed_enabled(env)
    _grant_other(env)
    env.store.note_signal(cases.NB, cases.OTHER, ask_job_id="", now=cases.at(1))
    assert _other_job(env) == [{"pending_signal": 1}]

    # -- order 1: the real ``delete_user_grant`` (grant deleted, job row cleared)
    # is paused before its commit when the signal arrives.
    deleted, counted = _run_blocked_pair(
        database,
        lambda db: _group_store(db).delete_user_grant(cases.NB, cases.OTHER),
        lambda db: MemorySuggestStore(db, new_id=_new_id_factory()).note_signal(
            cases.NB, cases.OTHER, ask_job_id="", now=cases.at(2)),
    )
    assert deleted is True and counted == 0
    assert _other_job(env) == []

    # -- order 2: the signal holds the edge when the revocation arrives. The
    # real ``note_signal`` runs against a database whose write transaction
    # pauses after the statements and before the commit.
    _grant_other(env)
    counted, deleted = _run_blocked_pair(
        database,
        lambda db: MemorySuggestStore(db, new_id=_new_id_factory()).note_signal(
            cases.NB, cases.OTHER, ask_job_id="", now=cases.at(3)),
        lambda db: _group_store(db).delete_user_grant(cases.NB, cases.OTHER),
    )
    assert counted == 1 and deleted is True
    assert _other_job(env) == []


class _ObservedDatabase:
    """Expose each worker's actual backend; optionally pause before commit."""

    def __init__(self, inner, release=None):
        self._inner = inner
        self._release = release
        self.connected = threading.Event()
        self.written = threading.Event()
        self.backend_pid = None

    @contextmanager
    def write(self, **kwargs):
        with self._inner.write(**kwargs) as db:
            self.backend_pid = db.info.backend_pid
            self.connected.set()
            yield db
            self.written.set()
            if self._release is not None:
                assert self._release.wait(timeout=30)


def _run_blocked_pair(database, first, second):
    """Release as soon as PG proves this exact waiter blocks on this holder.

    A fixed one-second join races the fixture's one-second lock timeout.
    Observe outside the two-connection pool and surface worker exceptions
    through futures; always release the paused transaction before joining.
    """
    release = threading.Event()
    holder = _ObservedDatabase(database, release)
    waiter = _ObservedDatabase(database)
    with psycopg.connect(database.settings.database_url, autocommit=True) as observer:
        with ThreadPoolExecutor(max_workers=2) as workers:
            held = workers.submit(first, holder)
            try:
                assert holder.written.wait(timeout=10), "holder never reached its commit"
                waiting = workers.submit(second, waiter)
                assert waiter.connected.wait(timeout=10), "waiter never opened its transaction"
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if waiting.done():
                        waiting.result()
                        pytest.fail("operation completed without waiting for the holder")
                    if observer.execute(
                        "SELECT %s = ANY(pg_blocking_pids(%s))",
                        (holder.backend_pid, waiter.backend_pid),
                    ).fetchone()[0]:
                        break
                    time.sleep(0.01)
                else:
                    pytest.fail("operation never blocked on the holder's transaction")
            finally:
                release.set()
            return held.result(timeout=30), waiting.result(timeout=30)


def _group_store(database):
    from datetime import datetime, timezone

    from app.repositories.postgres.group_store import GroupStore

    return GroupStore(
        database, new_id=_new_id_factory(), now=lambda: datetime.now(timezone.utc)
    )


def test_note_signal_cannot_recreate_a_job_row_across_a_concurrent_switch_off(
    env, database,
):
    """The switch-off transaction (switch row to 0, the owner's job rows
    deleted) is open when a completed ask's ``note_signal`` arrives: the
    signal waits on the switch row (``FOR SHARE``) and, once the switch-off
    commits, re-reads ``enabled = 0`` and writes nothing. Without the lock it
    would read the old ``enabled = 1`` and re-insert the deleted row."""
    cases.seed_enabled(env)
    env.store.note_signal(cases.NB, cases.OWNER, ask_job_id="", now=cases.at(1))
    assert env.job() is not None

    def switch_off(db):
        with db.write() as connection:
            connection.execute(
                "UPDATE memory_suggest_users SET enabled = 0 WHERE owner_id = %s",
                (cases.OWNER,),
            )
            connection.execute(
                "DELETE FROM memory_suggest_jobs WHERE owner_id = %s", (cases.OWNER,)
            )

    _, result = _run_blocked_pair(
        database, switch_off,
        lambda db: MemorySuggestStore(db, new_id=_new_id_factory()).note_signal(
            cases.NB, cases.OWNER, ask_job_id="", now=cases.at(2)),
    )
    assert result == 0
    assert env.job() is None


def test_claim_settle_cas_rejects_stale_token(env):
    cases.assert_claim_settle_cas_rejects_stale_token(env)


def test_sweep_resets_running(env):
    cases.assert_sweep_resets_running(env)


def test_clear_job_rows(env):
    cases.assert_clear_job_rows(env)


def test_clarified_scan_ascending_after_cursor_inclusive_start(env):
    cases.assert_clarified_scan_ascending_after_cursor_inclusive_start(env)


def test_repeated_window_descending_latest_40_within_lower_bound(env):
    cases.assert_repeated_window_descending_latest_within_lower_bound(env)


def test_sampling_predicate_excludes_mcp_evidence_report_unfinished_foreign(env):
    cases.assert_sampling_predicate_excludes_mcp_evidence_report_unfinished_foreign(env)


def test_answer_projection_owner_only(env):
    cases.assert_answer_projection_owner_only(env)


def test_latest_feedback_owner_and_latest(env):
    cases.assert_latest_feedback_owner_and_latest(env)


def test_answers_with_memory_any_status_owner_only(env):
    cases.assert_answers_with_memory_any_status_owner_only(env)


def test_unadopted_candidate_count(env):
    cases.assert_unadopted_candidate_count(env)


def test_fingerprint_declined_upgrades_to_proposed(env):
    cases.assert_fingerprint_declined_upgrades_to_proposed(env)


def test_fingerprint_proposed_never_downgrades(env):
    cases.assert_fingerprint_proposed_never_downgrades(env)


def test_fingerprint_declined_extends_to_later_expiry_only(env):
    cases.assert_fingerprint_declined_extends_to_later_expiry_only(env)


def test_fingerprint_presence_rules(env):
    cases.assert_fingerprint_presence_rules(env)


def test_neighbors_vector_side_latest_500_by_updated_at(env):
    cases.assert_neighbors_vector_side_latest_scan_by_updated_at(
        env, NEIGHBOR_VECTOR_SCAN_MAX + 1)


def test_neighbors_lexical_includes_rejected_and_deprecated(env):
    cases.assert_neighbors_lexical_includes_rejected_and_deprecated(env)


def test_neighbors_rejects_unknown_status(env):
    cases.assert_neighbors_rejects_unknown_status(env)


def test_expire_overdue_only_suggested_candidates(env):
    cases.assert_expire_overdue_only_suggested_candidates(env)


def test_expire_overdue_scoped_to_owner_notebook(env):
    cases.assert_expire_overdue_scoped_to_owner_notebook(env)


def test_expire_overdue_skips_a_locked_row_and_still_takes_exactly_limit(env, database):
    """Another transaction holds the oldest overdue suggestion: the sweep
    neither waits for it nor lets the skip loosen its LIMIT -- it moves exactly
    the next ``limit`` rows (the materialized pick runs once)."""
    env.seed()
    for minute in (1, 2, 3, 4):
        env.memory(f"s-old-{minute}", status="candidate", signal="clarified", minute=minute)
    import psycopg

    with psycopg.connect(database.settings.database_url) as holder:
        holder.execute("SELECT id FROM memory_items WHERE id = 's-old-1' FOR UPDATE")
        moved = env.store.expire_overdue(now=cases.at(200), cutoff=cases.at(50), limit=2)
        assert [row["id"] for row in moved] == ["s-old-2", "s-old-3"]
    statuses = {row["id"]: row["status"] for row in env.rows(
        "SELECT id, status FROM memory_items", ())}
    assert statuses == {
        "s-old-1": "candidate", "s-old-2": "rejected", "s-old-3": "rejected",
        "s-old-4": "candidate",
    }


def test_answer_projection_sources_owner_only(env):
    cases.assert_answer_projection_sources_owner_only(env)


def test_earliest_unfinished_ask_owner_and_predicate(env):
    cases.assert_earliest_unfinished_ask_owner_and_predicate(env)


def test_sampling_orders_offsets_and_microseconds_like_cursor(env):
    cases.assert_sampling_orders_offsets_and_microseconds_like_cursor(env)
