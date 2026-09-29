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

from tests.answer_owner_testkit import save_owned_answer, seed_ownership_matrix

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
    # without the keyword it is still the plain notebook lookup
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
