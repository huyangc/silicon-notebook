"""E7-4 on the real PostgreSQL backend: the answer-owner read and the service
predicate built on it (twin of ``tests/test_answer_ownership.py``'s store and
service cases), the in-transaction author re-check of the Memory write, and the
EXPLAIN pin of the ownership statement. The pin EXPLAINs the statement the store
actually sends (captured from ``answer_notebook_id``), not a copy: both sides
must stay primary-key probes, never a scan."""

from __future__ import annotations

import pytest

from app.repositories.postgres.ask_state_store import ANSWER_AUTHOR_PROBE_SQL
from tests.answer_owner_testkit import (
    capture_statements,
    refusal_cases,
    seed_ownership_matrix,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_answer_ownership"),
]

MISSING = "ans-does-not-exist"


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def world(postgres_repository):
    ns = seed_ownership_matrix(postgres_repository)
    ns.repo = postgres_repository
    return ns


def test_user_owns_answer_is_conversation_author_and_notebook_reader(world):
    sharing = world.repo._runtime.sharing
    assert sharing.user_owns_answer(world.alice_answer, world.alice.id) is True
    assert sharing.user_owns_answer(world.bob_answer, world.bob.id) is True
    assert sharing.user_owns_answer(world.bob_answer, world.alice.id) is False
    assert sharing.user_owns_answer(world.alice_answer, world.bob.id) is False
    assert sharing.user_owns_answer(world.alice_answer, world.carol.id) is False
    assert sharing.user_owns_answer(world.creatorless_answer, world.alice.id) is False
    assert sharing.user_owns_answer(world.nocreator_answer, world.bob.id) is False
    assert sharing.user_owns_answer(MISSING, world.alice.id) is False
    assert sharing.user_owns_answer(world.alice_answer, "") is False
    # answer_owner is still "the NOTEBOOK owner", never authorisation
    assert sharing.answer_owner(world.bob_answer) == world.alice.id


def test_removing_the_member_removes_the_answer_from_them(world):
    sharing = world.repo._runtime.sharing
    assert sharing.user_owns_answer(world.bob_answer, world.bob.id) is True
    world.repo.remove_member(world.notebook_id, world.bob.id)
    assert sharing.user_owns_answer(world.bob_answer, world.bob.id) is False


def test_every_refusal_is_the_same_none_from_one_statement(world):
    store = world.repo._runtime.ask_state
    with capture_statements(store.database) as seen:
        assert store.answer_notebook_id(world.bob_answer, owned_by=world.bob.id) == (
            world.notebook_id
        )
    assert len(seen) == 1
    for kind, answer_id, user_id in refusal_cases(world):
        with capture_statements(store.database) as seen:
            assert store.answer_notebook_id(answer_id, owned_by=user_id) is None, kind
        assert len(seen) == 1, kind
        assert seen[0][0] == ANSWER_AUTHOR_PROBE_SQL, kind


def test_a_refusal_costs_the_service_one_statement_whatever_the_kind(world):
    sharing = world.repo._runtime.sharing
    counts = {}
    for kind, answer_id, user_id in refusal_cases(world):
        with capture_statements(world.repo._runtime.ask_state.database) as seen:
            assert sharing.user_owns_answer(answer_id, user_id) is False, kind
        counts[kind] = len(seen)
    assert set(counts.values()) == {1}, counts


def test_service_create_from_answer_refuses_a_foreign_answer(world):
    service = world.repo._runtime.memory_service
    with pytest.raises(PermissionError):
        service.create_from_answer(
            world.notebook_id, world.alice.id, world.bob_answer, "T", "B", []
        )
    with pytest.raises(PermissionError):
        service.create_from_answer(
            world.notebook_id, world.bob.id, world.creatorless_answer, "T", "B", []
        )


def test_the_write_lock_rechecks_the_author(world):
    service = world.repo._runtime.memory_service
    store = world.repo._runtime.memory_store
    original = store.create_answer_with_initial_revision

    def creator_changes_before_the_write(write, changed_by, reason):
        with world.repo._runtime.database.write() as db:
            db.execute(
                "UPDATE conversations SET created_by=%s WHERE id="
                "(SELECT conversation_id FROM answers WHERE id=%s)",
                (world.alice.id, world.bob_answer),
            )
        return original(write, changed_by, reason)

    store.create_answer_with_initial_revision = creator_changes_before_the_write
    try:
        with pytest.raises(KeyError):
            service.create_from_answer(
                world.notebook_id, world.bob.id, world.bob_answer, "T", "B", [],
                extract_kg=False,
            )
    finally:
        del store.create_answer_with_initial_revision
    with world.repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()["n"] == 0


LEGACY_FOREIGN_ANSWER_MEMORIES_PG = (
    "SELECT COUNT(*) AS n FROM memory_items m "
    "JOIN answers a ON a.id = m.source_answer_id "
    "LEFT JOIN conversations c ON c.id = a.conversation_id "
    "WHERE m.origin = 'ask_answer' "
    "AND (c.id IS NULL OR c.created_by IS DISTINCT FROM m.created_by)"
)


def test_the_legacy_memory_count_query_finds_a_memory_from_someone_elses_answer(world):
    service = world.repo._runtime.memory_service
    service.kg_ingest_scheduler = lambda fn, key: None
    service.embedding_scheduler = lambda fn, job: None

    def count() -> int:
        with world.repo._runtime.database.connect() as db:
            return db.execute(LEGACY_FOREIGN_ANSWER_MEMORIES_PG).fetchone()["n"]

    assert count() == 0
    service.create_from_answer(
        world.notebook_id, world.alice.id, world.alice_answer, "T", "B", [],
        extract_kg=False,
    )
    assert count() == 0
    with world.repo._runtime.database.write() as db:
        db.execute(
            "UPDATE conversations SET created_by=%s WHERE id="
            "(SELECT conversation_id FROM answers WHERE id=%s)",
            (world.bob.id, world.alice_answer),
        )
    assert count() == 1


def test_the_ownership_statement_is_two_primary_key_probes(world):
    """EXPLAIN the statement production really sends (captured), scale-free:
    scans are forbidden, so the plan must be servable by the primary keys."""
    store = world.repo._runtime.ask_state
    with capture_statements(store.database) as seen:
        store.answer_notebook_id(world.bob_answer, owned_by=world.bob.id)
    assert len(seen) == 1
    sql, params = seen[0]
    with store.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        rows = db.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    plan = "\n".join(str(row["QUERY PLAN"]) for row in rows)
    assert "pk_answers" in plan and "pk_conversations" in plan, plan
    assert "Seq Scan" not in plan, plan
    # the creator is compared in Python: it must not become an index condition
    assert "idx_conversations_created_by" not in plan, plan
