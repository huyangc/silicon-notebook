"""PostgreSQL: ``MemoryService``'s auto user-memory suggestion paths and the
switch service (plan T8). SQLite twin: ``tests/test_memory_suggest_adoption.py``;
shared cases: ``tests/memory_suggest_adoption_cases.py``."""
from __future__ import annotations

import threading
import time

import pytest
from psycopg.types.json import Jsonb

from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST
from tests import memory_suggest_adoption_cases as cases
from tests import memory_suggest_write_cases as writes

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
    return database


@pytest.fixture
def world(database) -> cases.World:
    counter = iter(range(1, 1_000_000))

    def new_id(prefix: str) -> str:
        return f"{prefix}-{next(counter)}"

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    return cases.build_world(writes.Env(
        store=MemoryStore(database, new_id=new_id, now=lambda: writes.NOW),
        suggest=MemorySuggestStore(database, new_id=new_id),
        run=run,
        rows=rows,
        json=Jsonb,
    ))


def test_create_suggestions_events_without_vector_or_kg(world):
    cases.assert_create_suggestions_events_without_vector_or_kg(world)


def test_two_adoption_entries_identical_side_effects(world):
    cases.assert_two_adoption_entries_identical_side_effects(world)


def test_save_from_answer_adopts_open_suggestion(world):
    cases.assert_save_from_answer_adopts_open_suggestion(world)


def test_save_from_answer_replaces_dismissed_suggestion(world):
    cases.assert_save_from_answer_replaces_dismissed_suggestion(world)


def test_save_from_answer_returns_other_rows_unchanged(world):
    cases.assert_save_from_answer_returns_other_rows_unchanged(world)


def test_save_from_answer_concurrent_insert_returns_existing(world):
    cases.assert_save_from_answer_concurrent_insert_returns_existing(world)


def test_clarified_suggestion_does_not_occupy_answer_index(world):
    cases.assert_clarified_suggestion_does_not_occupy_answer_index(world)


def test_answer_memory_link_view(world):
    cases.assert_answer_memory_link_view(world)


def test_origin_filters_keep_manual_answers_apart_from_all_suggestion_states(world):
    cases.assert_origin_filters_keep_manual_answers_apart_from_all_suggestion_states(world)


def test_exit_with_only_drafts_ends_membership(world):
    cases.assert_exit_with_only_drafts_ends_membership(world)


def test_exit_ack_compares_your_memories_only(world):
    cases.assert_exit_ack_compares_your_memories_only(world)


def test_exit_keeps_a_suggestion_adopted_after_the_claim(world):
    cases.assert_exit_keeps_a_suggestion_adopted_after_the_claim(world)


def test_exit_of_only_drafts_keeps_one_adopted_after_the_claim(world):
    cases.assert_exit_of_only_drafts_keeps_one_adopted_after_the_claim(world)


def test_exit_ack_mismatch_leaves_the_job_row(world):
    cases.assert_exit_ack_mismatch_leaves_the_job_row(world)


def test_export_header_and_type_labels(world):
    cases.assert_export_header_and_type_labels(world)


def test_transfer_rejects_unadopted(world):
    cases.assert_transfer_rejects_unadopted(world)


def test_enable_refused_when_wiring_off(world):
    cases.assert_enable_refused_when_wiring_off(world)


def test_disable_delete_unadopted_removes_only_candidates_keeps_rejected(world):
    cases.assert_disable_delete_unadopted_removes_only_candidates_keeps_rejected(world)


def test_disable_delete_keeps_a_suggestion_adopted_after_the_page_read(world):
    cases.assert_disable_delete_keeps_a_suggestion_adopted_after_the_page_read(world)


def test_expire_suggestions_ignores_deployment_switch_and_emits_events(world):
    cases.assert_expire_suggestions_ignores_deployment_switch_and_emits_events(world)


def test_concurrent_save_from_answer_conflict_returns_existing(world):
    """Two "save to Memory" requests on the same answer, on two connections:
    the first reads no row and holds its serializable save open past its
    locks; the second saves and commits. The first's insert then meets the
    committed row -- PostgreSQL aborts it (a ``ValueError``, 409, from the
    store) and the service answers with the row that landed: no error, no
    second row, no second event (design §5.3 last row)."""
    world.seed()
    world.env.answer("ans-1")
    store = world.env.store
    paused = threading.Event()
    release = threading.Event()
    original_hook = store._answer_save_scope_locked_on
    original_scope_exists = store._answer_save_scope_exists
    conflicts: list[bool] = []

    def pause_first_save(connection, write, row):
        locked = original_hook(connection, write, row)
        if not paused.is_set():
            paused.set()
            assert release.wait(timeout=10)
        return locked

    def note_conflict(write):
        exists = original_scope_exists(write)
        conflicts.append(exists)
        return exists

    store._answer_save_scope_locked_on = pause_first_save
    store._answer_save_scope_exists = note_conflict
    outcome: dict[str, object] = {}

    def first_save() -> None:
        try:
            outcome["first"] = world.service.create_from_answer(
                writes.NB, writes.MEMBER, "ans-1", "first", "body", [])
        except Exception as exc:  # reported by the assertion below
            outcome["error"] = exc

    worker = threading.Thread(target=first_save)
    worker.start()
    try:
        assert paused.wait(timeout=10)
        second = world.service.create_from_answer(
            writes.NB, writes.MEMBER, "ans-1", "second", "body", [])
    finally:
        release.set()
        worker.join(timeout=20)
    assert not worker.is_alive()
    assert "error" not in outcome, outcome
    first = outcome["first"]
    assert first.id == second.id
    assert first.title == "second"
    assert conflicts == [True]  # the first save really hit the conflict path
    assert world.env.memory_ids() == {second.id}
    assert [event["action"] for event in world.events.items] == ["answer_confirmed"]


def test_exit_finish_waits_out_a_concurrent_adoption(world, database):
    """Review P2-1, concurrent: right after the exit's claim, another
    connection adopts a draft while holding the membership row ``FOR SHARE``
    (what every member Memory write holds). The exit's finish waits for that
    lock, then counts the adopted row as "your memory": nothing deleted, the
    membership stays, the server's numbers come back."""
    world.seed()
    world.memory("draft-a", title="a", status="candidate", signal="clarified")
    world.memory("draft-b", title="b", status="rejected", signal="repeated")
    store = world.env.store
    locked = threading.Event()
    adopter: dict[str, object] = {}

    def adopt_holding_the_member_row() -> None:
        try:
            with database.write() as db:
                db.execute(
                    "SELECT 1 FROM notebook_members WHERE notebook_id=%s AND user_id=%s "
                    "FOR SHARE",
                    (writes.NB, writes.MEMBER),
                )
                db.execute(
                    "UPDATE memory_items SET status='confirmed' WHERE id=%s", ("draft-a",))
                locked.set()
                time.sleep(0.5)
        except Exception as exc:  # reported by the assertion below
            adopter["error"] = exc

    class ClaimThenAdopt:
        def __getattr__(self, name):
            return getattr(store, name)

        def member_exit_snapshot(self, notebook_id, user_id, *, claim):
            snapshot = store.member_exit_snapshot(notebook_id, user_id, claim=claim)
            if claim:
                adopter["thread"] = threading.Thread(target=adopt_holding_the_member_row)
                adopter["thread"].start()
                assert locked.wait(timeout=10)
            return snapshot

    world.service.store = ClaimThenAdopt()
    started = time.monotonic()
    try:
        with pytest.raises(cases.ExitDisclosureRequired) as required:
            world.service.leave_notebook(writes.NB, writes.MEMBER, 0)
    finally:
        thread = adopter.get("thread")
        if thread is not None:
            thread.join(timeout=20)
    assert "error" not in adopter
    assert time.monotonic() - started >= 0.4  # the finish waited for the lock
    assert (required.value.memory_count, required.value.suggestion_count) == (1, 1)
    assert world.env.is_member()
    assert world.env.memory_ids() == {"draft-a", "draft-b"}
