"""E7-4 on the real PostgreSQL backend: the answer-owner read and the service
predicate built on it (twin of ``tests/test_answer_ownership.py``'s store and
service cases), plus the EXPLAIN pin of the new ``answers JOIN conversations``
statement: both sides must stay primary-key probes, never a scan."""

from __future__ import annotations

import pytest

from tests.answer_owner_testkit import seed_ownership_matrix

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
    assert store.answer_notebook_id(world.bob_answer, owned_by=world.bob.id) == (
        world.notebook_id
    )
    for answer_id, user_id in (
        (world.bob_answer, world.alice.id),
        (world.creatorless_answer, world.alice.id),
        (world.nocreator_answer, world.bob.id),
        (MISSING, world.alice.id),
        (world.bob_answer, ""),
    ):
        assert store.answer_notebook_id(answer_id, owned_by=user_id) is None
    assert store.answer_notebook_id(world.creatorless_answer) == world.notebook_id


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


def test_the_ownership_join_is_two_primary_key_probes(world):
    store = world.repo._runtime.ask_state
    with store.database.connect() as db:
        # scale-free criterion (same as the other EXPLAIN pins): the fixture is
        # tiny, so forbid scans and assert the statement CAN be served by the
        # primary keys on both sides.
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        rows = db.execute(
            "EXPLAIN (COSTS OFF) SELECT a.notebook_id AS notebook_id, "
            "c.created_by AS creator FROM answers a "
            "LEFT JOIN conversations c ON c.id=a.conversation_id WHERE a.id=%s",
            (world.bob_answer,),
        ).fetchall()
    plan = "\n".join(str(row["QUERY PLAN"]) for row in rows)
    assert "pk_answers" in plan and "pk_conversations" in plan, plan
    assert "Seq Scan" not in plan, plan
