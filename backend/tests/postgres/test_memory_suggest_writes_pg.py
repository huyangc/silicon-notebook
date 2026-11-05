"""PostgreSQL Memory store write side of auto memory suggestions (plan T4b).
SQLite twin: ``tests/test_memory_suggest_writes.py``; shared cases:
``tests/memory_suggest_write_cases.py``."""
from __future__ import annotations

import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain.memory_suggest_contracts import SKIP_NO_READ_ACCESS, SuggestionRevoked
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from tests import memory_suggest_write_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
    return database


@pytest.fixture
def env(database) -> cases.Env:
    counter = iter(range(1, 1_000_000))

    def new_id(prefix: str) -> str:
        return f"{prefix}-{next(counter)}"

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    return cases.Env(
        store=MemoryStore(database, new_id=new_id, now=lambda: cases.NOW),
        suggest=MemorySuggestStore(database, new_id=new_id),
        run=run,
        rows=rows,
        json=Jsonb,
    )


def test_member_exit_snapshot_counts_memories_and_suggestions_apart(env):
    cases.assert_member_exit_snapshot_counts_memories_and_suggestions_apart(env)


def test_exit_claim_writes_nothing_finish_deletes_job_row(env):
    cases.assert_exit_claim_writes_nothing_finish_deletes_job_row(env)


def test_finish_keeps_a_suggestion_adopted_after_the_claim(env):
    cases.assert_finish_keeps_a_suggestion_adopted_after_the_claim(env)


def test_exit_race_suggestion_between_purge_and_finish(env):
    cases.assert_exit_race_suggestion_between_purge_and_finish(env)


def test_finish_member_exit_still_counts_all_states(env):
    cases.assert_finish_member_exit_still_counts_all_states(env)


def test_create_suggestions_revoked_when_disabled(env):
    cases.assert_create_suggestions_revoked_when_disabled(env)


def test_create_suggestions_revoked_when_job_row_deleted(env):
    cases.assert_create_suggestions_revoked_when_job_row_deleted(env)


def test_create_suggestions_revoked_when_token_mismatch(env):
    cases.assert_create_suggestions_revoked_when_token_mismatch(env)


def test_create_suggestions_revoked_without_read_access(env):
    cases.assert_create_suggestions_revoked_without_read_access(env)


def test_repeated_suggestion_answer_once_conflict_is_skipped(env):
    cases.assert_repeated_suggestion_answer_once_conflict_is_skipped(env)


def test_create_suggestions_rejects_malformed_writes(env):
    cases.assert_create_suggestions_rejects_malformed_writes(env)


def test_adopt_from_answer_cas(env):
    cases.assert_adopt_from_answer_cas(env)


def test_replace_rejected_suggestion(env):
    cases.assert_replace_rejected_suggestion(env)


def test_concurrent_manual_save_noop(env):
    cases.assert_concurrent_manual_save_noop(env)


def test_resync_children_touch_only_when_capture_enabled(env):
    cases.assert_resync_children_touch_only_when_capture_enabled(env)


def _lock_waiters(probe) -> int:
    """Other backends of this database blocked on a lock (the
    tests/postgres/test_concurrency.py probe)."""
    return int(probe.execute(
        "SELECT count(*) AS n FROM pg_stat_activity "
        "WHERE pid <> pg_backend_pid() AND datname = current_database() "
        "AND wait_event_type = 'Lock' AND state = 'active'"
    ).fetchone()["n"])


def test_create_suggestions_serializes_with_member_removal(env, database):
    """The suggestion write holds the membership row ``FOR SHARE``: a removal
    that has deleted the row but not committed makes it wait, and once the
    removal commits the write finds no read access — ``SuggestionRevoked``,
    nothing written. Without the row lock the write would read the
    not-yet-deleted row from its snapshot and land a draft the removed
    member can no longer see."""
    env.seed()
    env.start_run()
    outcome: dict[str, object] = {}

    def write_suggestion() -> None:
        try:
            outcome["result"] = env.create([cases.suggestion("contested")])
        except SuggestionRevoked as exc:
            outcome["error"] = exc

    url = database.settings.database_url  # carries the test schema
    # Raw connections outside the (two-connection) test pool: the removal
    # held open, and a probe that watches the write block.
    with psycopg.connect(url, row_factory=dict_row) as removal, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        removal.execute(
            "DELETE FROM notebook_members WHERE notebook_id = %s AND user_id = %s",
            (cases.NB, cases.MEMBER),
        )
        writer = threading.Thread(target=write_suggestion)
        writer.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and writer.is_alive() and not _lock_waiters(probe):
            time.sleep(0.01)
        # Commit at once: the pool's lock_timeout (1 s) would cancel the wait.
        blocked = writer.is_alive()
        removal.commit()
        writer.join(timeout=20)
    assert not writer.is_alive()
    assert blocked, f"the suggestion write did not wait for the removal: {outcome}"
    error = outcome.get("error")
    assert isinstance(error, SuggestionRevoked), outcome
    assert error.reason == SKIP_NO_READ_ACCESS
    assert env.memory_ids() == set()
    assert env.fingerprints() == {}


def test_replace_rejected_waits_behind_an_exit_finish_without_deadlock(env, database):
    """Review P3-3: ``replace_rejected_suggestion_with_answer`` DELETEs a
    Memory row, so it locks in the canonical order notebook → membership →
    memory row, like ``finish_member_exit`` (membership ``FOR UPDATE``, then
    the draft DELETE). An exit finish holding the membership row and a replace
    of the same dismissed suggestion therefore queue on the membership row:
    the finish deletes the draft unhindered and the replace then finds no
    read access (404). In the old order the replace took the memory row
    first and the two waited on each other."""
    env.seed()
    env.answer("ans-1")
    env.start_run()
    env.create([cases.suggestion("old", signal="repeated", answer_id="ans-1")])
    env.store.transition_with_revision(
        "old", cases.MEMBER, {"candidate"}, "rejected", fields=None,
        changed_by=cases.MEMBER, reason="rejected")
    outcome: dict[str, object] = {}

    def replace() -> None:
        try:
            outcome["result"] = env.store.replace_rejected_suggestion_with_answer(
                cases.manual_save("m-new", "ans-1"), cases.MEMBER, "created_from_answer")
        except Exception as exc:  # judged below
            outcome["error"] = exc

    url = database.settings.database_url
    with psycopg.connect(url, row_factory=dict_row) as finish, \
            psycopg.connect(url, autocommit=True, row_factory=dict_row) as probe:
        # The finish's own statements, in its order.
        finish.execute(
            "SELECT added_at FROM notebook_members WHERE notebook_id=%s AND user_id=%s "
            "FOR UPDATE",
            (cases.NB, cases.MEMBER),
        )
        replacer = threading.Thread(target=replace)
        replacer.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and replacer.is_alive() and not _lock_waiters(probe):
            time.sleep(0.01)
        blocked = replacer.is_alive()
        deleted = finish.execute(
            "DELETE FROM memory_items m WHERE m.notebook_id=%s AND m.created_by=%s "
            "AND m.suggested_signal IS NOT NULL AND m.status IN ('candidate','rejected')",
            (cases.NB, cases.MEMBER),
        ).rowcount
        finish.execute(
            "DELETE FROM notebook_members WHERE notebook_id=%s AND user_id=%s",
            (cases.NB, cases.MEMBER),
        )
        finish.commit()
        replacer.join(timeout=20)
    assert not replacer.is_alive()
    assert blocked, f"the replace did not queue behind the finish: {outcome}"
    assert deleted == 1
    assert isinstance(outcome.get("error"), KeyError), outcome
    assert env.memory_ids() == set()
