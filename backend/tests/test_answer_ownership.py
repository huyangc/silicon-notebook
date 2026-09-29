"""E7-4: endpoints that take an answer id require the CONVERSATION's author.

Reading the notebook is not enough: a member's answer may quote their private
Memory, so another member (or the notebook owner) must not be able to preview
it, turn it into a Memory or rate it. Answers without a conversation, answers in
a conversation without a creator, other members' answers and ids that do not
exist are one indistinguishable 404. A deployment admin reading answers through
the admin activity log is audit access and is not covered here (see
``test_admin_user_activity_api.py``).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.repositories.sqlite.ask_state_store import ANSWER_AUTHOR_PROBE_SQL
from tests.answer_owner_testkit import (
    capture_statements,
    refusal_cases,
    save_owned_answer,
    seed_ownership_matrix,
)

MISSING = "ans-does-not-exist"


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ownership.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def world(client):
    """Alice owns the notebook, Bob is a member, Carol is outside. Every user
    has a bearer token; ``ns`` carries one answer of each kind."""
    from app.api.deps import repository

    repo = repository()
    ns = seed_ownership_matrix(repo)
    ns.repo = repo
    ns.headers = {}
    for name in ("alice", "bob", "carol"):
        # seed_ownership_matrix registers users with this password
        login = client.post(
            "/api/auth/login",
            json={"username": getattr(ns, name).username, "password": "pw123456"},
        )
        assert login.status_code == 200, login.text
        ns.headers[name] = {"Authorization": f"Bearer {login.json()['token']}"}
    return ns


def _preview(client, headers, answer_id):
    return client.post(f"/api/answers/{answer_id}/memory-preview", headers=headers)


def _stream(client, headers, answer_id):
    return client.post(
        f"/api/answers/{answer_id}/memory-preview/stream", headers=headers
    )


def _save(client, headers, notebook_id, answer_id):
    return client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={
            "answer_id": answer_id,
            "title": "Saved",
            "content_md": "Body",
            "tags": [],
        },
    )


def _rate(client, headers, answer_id):
    return client.post(
        f"/api/answers/{answer_id}/feedback",
        headers=headers,
        json={"rating": "useful", "comment": ""},
    )


ENDPOINTS = {
    "preview": lambda c, h, w, a: _preview(c, h, a),
    "stream": lambda c, h, w, a: _stream(c, h, a),
    "from-answer": lambda c, h, w, a: _save(c, h, w.notebook_id, a),
    "feedback": lambda c, h, w, a: _rate(c, h, a),
}
OWN_STATUS = {"preview": 200, "stream": 200, "from-answer": 201, "feedback": 200}


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
def test_a_members_own_answer_still_works(client, world, endpoint):
    call = ENDPOINTS[endpoint]
    response = call(client, world.headers["bob"], world, world.bob_answer)
    assert response.status_code == OWN_STATUS[endpoint], response.text
    owner = call(client, world.headers["alice"], world, world.alice_answer)
    assert owner.status_code == OWN_STATUS[endpoint], owner.text


@pytest.mark.parametrize("endpoint", sorted(ENDPOINTS))
@pytest.mark.parametrize(
    "who,answer",
    [
        ("alice", "bob_answer"),  # the notebook OWNER is not the answer's author
        ("bob", "alice_answer"),  # a member reading the owner's answer
        ("alice", "creatorless_answer"),  # no conversation at all
        ("bob", "nocreator_answer"),  # conversation row without a creator
        ("carol", "alice_answer"),  # outside the notebook entirely
    ],
)
def test_anyone_elses_answer_is_the_same_404_as_an_unknown_id(
    client, world, endpoint, who, answer
):
    call = ENDPOINTS[endpoint]
    headers = world.headers[who]
    refused = call(client, headers, world, getattr(world, answer))
    unknown = call(client, headers, world, MISSING)
    assert refused.status_code == 404, refused.text
    assert (refused.status_code, refused.json()) == (
        unknown.status_code,
        unknown.json(),
    )
    # nothing was written on the way
    memories = client.get("/api/memories", headers=headers).json()
    assert memories["total_count"] == 0
    with world.repo._runtime.database.connect() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"] == 0


def test_all_four_endpoints_share_one_404_body(client, world):
    bodies = {
        name: call(client, world.headers["alice"], world, world.bob_answer).json()
        for name, call in ENDPOINTS.items()
    }
    assert set(map(str, bodies.values())) == {str({"detail": "Answer not found"})}


def test_losing_notebook_access_loses_the_answer(client, world):
    """Own conversation is not enough once the notebook can no longer be read."""
    assert _preview(client, world.headers["bob"], world.bob_answer).status_code == 200
    world.repo.remove_member(world.notebook_id, world.bob.id)
    for name, call in ENDPOINTS.items():
        response = call(client, world.headers["bob"], world, world.bob_answer)
        assert response.status_code == 404, (name, response.text)


def test_user_owns_answer_is_conversation_author_and_notebook_reader(world):
    sharing = world.repo._runtime.sharing
    assert sharing.user_owns_answer(world.alice_answer, world.alice.id) is True
    assert sharing.user_owns_answer(world.bob_answer, world.bob.id) is True
    # notebook read access alone, and being the notebook owner, are not enough
    assert sharing.user_owns_answer(world.bob_answer, world.alice.id) is False
    assert sharing.user_owns_answer(world.alice_answer, world.bob.id) is False
    assert sharing.user_owns_answer(world.alice_answer, world.carol.id) is False
    assert sharing.user_owns_answer(world.creatorless_answer, world.alice.id) is False
    assert sharing.user_owns_answer(world.nocreator_answer, world.bob.id) is False
    assert sharing.user_owns_answer(MISSING, world.alice.id) is False
    assert sharing.user_owns_answer(world.alice_answer, "") is False
    # answer_owner keeps meaning "the NOTEBOOK owner"; it is not authorisation
    assert sharing.answer_owner(world.bob_answer) == world.alice.id


def test_the_ownership_read_is_one_statement_for_every_refusal(world):
    store = world.repo._runtime.ask_state
    with capture_statements(store.database) as seen:
        assert store.answer_notebook_id(world.bob_answer, owned_by=world.bob.id) == (
            world.notebook_id
        )
    assert len(seen) == 1
    for kind, answer_id, user_id in refusal_cases(world):
        with capture_statements(store.database) as seen:
            assert store.answer_notebook_id(answer_id, owned_by=user_id) is None, kind
        # every refusal kind, and the unknown id, is the SAME single statement
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
    saved = service.create_from_answer(
        world.notebook_id, world.bob.id, world.bob_answer, "T", "B", [],
        extract_kg=False,
    )
    assert saved.created_by == world.bob.id


def test_an_answer_saved_after_the_conversation_is_reused_by_its_author(client, world):
    """A second turn in the author's conversation is theirs too."""
    from tests.answer_owner_testkit import new_conversation

    conversation = new_conversation(world.repo, world.notebook_id, world.bob.id)
    first = save_owned_answer(
        world.repo, world.notebook_id, world.bob.id, "one", conversation_id=conversation
    )
    second = save_owned_answer(
        world.repo, world.notebook_id, world.bob.id, "two", conversation_id=conversation
    )
    for answer_id in (first, second):
        assert _preview(client, world.headers["bob"], answer_id).status_code == 200
        assert _preview(client, world.headers["alice"], answer_id).status_code == 404


# Read-only: memories made from an answer whose conversation belongs to someone
# else (or to no conversation). Answers that no longer exist cannot be judged.
LEGACY_FOREIGN_ANSWER_MEMORIES_SQLITE = (
    "SELECT COUNT(*) FROM memory_items m "
    "JOIN answers a ON a.id = m.source_answer_id "
    "LEFT JOIN conversations c ON c.id = a.conversation_id "
    "WHERE m.origin = 'ask_answer' "
    "AND (c.id IS NULL OR c.created_by IS NOT m.created_by)"
)


def _legacy_count(repo) -> int:
    with repo._runtime.database.connect() as db:
        return db.execute(LEGACY_FOREIGN_ANSWER_MEMORIES_SQLITE).fetchone()[0]


def test_a_legacy_memory_from_someone_elses_answer_is_returned_to_its_creator_only(
    client, world
):
    """A Memory saved before answers were author-checked keeps working for the
    person who made it, is never handed to anyone else, and is countable."""
    world.repo.add_member(world.notebook_id, world.carol.id)
    assert _legacy_count(world.repo) == 0
    # Alice saves her own answer, then the answer's conversation "moves" to Bob:
    # the state a pre-fix save from Bob's answer left behind.
    saved = _save(client, world.headers["alice"], world.notebook_id, world.alice_answer)
    assert saved.status_code == 201, saved.text
    with world.repo._write() as db:
        db.execute(
            "UPDATE conversations SET created_by=? WHERE id="
            "(SELECT conversation_id FROM answers WHERE id=?)",
            (world.bob.id, world.alice_answer),
        )
    assert _legacy_count(world.repo) == 1
    again = _save(client, world.headers["alice"], world.notebook_id, world.alice_answer)
    assert again.status_code == 201 and again.json()["id"] == saved.json()["id"]
    memory_id = saved.json()["id"]
    # Carol (a member, not the author, no Memory of her own): refused.
    assert _save(
        client, world.headers["carol"], world.notebook_id, world.alice_answer
    ).status_code == 404
    # Bob is the author now: he gets his OWN new Memory, never Alice's.
    bobs = _save(client, world.headers["bob"], world.notebook_id, world.alice_answer)
    assert bobs.status_code == 201 and bobs.json()["id"] != memory_id
    assert bobs.json()["created_by"] == world.bob.id
    for who in ("bob", "carol"):
        assert client.get(
            f"/api/memories/{memory_id}", headers=world.headers[who]
        ).status_code == 404


def test_the_write_lock_rechecks_the_author(world):
    """The author is re-checked in the write transaction, not only before it:
    if the conversation's creator differs by then, nothing is written."""
    service = world.repo._runtime.memory_service
    store = world.repo._runtime.memory_store
    original = store.create_answer_with_initial_revision

    def creator_changes_before_the_write(write, changed_by, reason):
        with world.repo._write() as db:
            db.execute(
                "UPDATE conversations SET created_by=? WHERE id="
                "(SELECT conversation_id FROM answers WHERE id=?)",
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
        assert db.execute("SELECT COUNT(*) FROM memory_items").fetchone()[0] == 0
