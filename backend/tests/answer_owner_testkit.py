"""Seed an answer the way Ask does: inside a conversation its author created.

Since E7-4 an answer only "belongs" to a user when its conversation was created
by them (``NotebookSharingService.user_owns_answer``). ``save_answer(nb, None,
...)`` mints a creatorless answer that nobody owns, so tests that preview, save
or rate an answer as a real user seed it through here.

Backend-agnostic: it goes through the store's own ``ensure_conversation`` and
``save_answer`` on the runtime's database seat, so the same helper serves the
SQLite suites and the PostgreSQL twins.
"""

from __future__ import annotations

from app.models.schemas import AskResponse


def new_conversation(repo, notebook_id: str, user_id: str, title: str = "q") -> str:
    """A live conversation in ``notebook_id`` created by ``user_id``."""
    runtime = repo._runtime
    with runtime.database.write() as db:
        return runtime.ask_state.ensure_conversation(
            db, notebook_id, None, title, user_id
        )


def save_owned_answer(
    repo,
    notebook_id: str,
    user_id: str,
    question: str,
    response: AskResponse | None = None,
    *,
    conversation_id: str | None = None,
) -> str:
    """Save an answer inside a conversation created by ``user_id``."""
    if conversation_id is None:
        conversation_id = new_conversation(repo, notebook_id, user_id, question)
    if response is None:
        response = AskResponse(
            conclusion="The loop remains stable [1].",
            answer="The loop remains stable [1].",
            mode="chunk",
            llm_mode="test-model",
            evidence_level="grounded",
        )
    return repo._runtime.ask_state.save_answer(
        notebook_id, conversation_id, question, response, user_id
    )


def creatorless_conversation_answer(repo, notebook_id: str) -> str:
    """An answer whose conversation row has an empty creator (the schema
    default). Direct inserts: ``save_answer`` refuses a conversation that is
    not the caller's, which is exactly the state being seeded."""
    runtime = repo._runtime
    placeholder = (
        "%s"
        if type(runtime.database).__module__.startswith("app.repositories.postgres")
        else "?"
    )
    stamp = "'2026-01-01T00:00:00+00:00'"
    cid = f"conv-nocreator-{notebook_id[-8:]}"
    answer_id = f"ans-nocreator-{notebook_id[-8:]}"
    with runtime.database.write() as db:
        db.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            f"VALUES ({placeholder}, {placeholder}, 'x', '', {stamp}, {stamp})",
            (cid, notebook_id),
        )
        db.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            f"VALUES ({placeholder}, {placeholder}, {placeholder}, 'q', "
            f"'{{\"answer\": \"c\"}}', {stamp})",
            (answer_id, notebook_id, cid),
        )
    return answer_id


def seed_ownership_matrix(repo):
    """Alice's notebook, Bob a member, Carol outside. Returns a namespace with
    the users and one answer of each kind."""
    from types import SimpleNamespace

    from app.core.request_context import reset_request_user, set_request_user
    from app.models.notebooks import NotebookCreate

    alice = repo.create_user("a00777001", "pw123456")
    bob = repo.create_user("b00777002", "pw123456")
    carol = repo.create_user("c00777003", "pw123456")
    token = set_request_user(alice)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Ownership"))
    finally:
        reset_request_user(token)
    repo.add_member(notebook.id, bob.id)
    ns = SimpleNamespace(
        alice=alice, bob=bob, carol=carol, notebook_id=notebook.id
    )
    ns.alice_answer = save_owned_answer(repo, notebook.id, alice.id, "alice q")
    ns.bob_answer = save_owned_answer(repo, notebook.id, bob.id, "bob q")
    ns.creatorless_answer = repo._runtime.ask_state.save_answer(
        notebook.id,
        None,
        "no conversation",
        AskResponse(conclusion="c", answer="c"),
        alice.id,
    )
    ns.nocreator_answer = creatorless_conversation_answer(repo, notebook.id)
    return ns
