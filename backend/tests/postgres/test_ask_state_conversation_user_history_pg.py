"""PR-C (T2): ``AskStateStorePort.conversation_user_history`` on the real PG
backend — mirrors ``tests/test_ask_state_conversation_user_history.py``'s
sqlite coverage. See that file's module docstring for the full contract.
"""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.models.schemas import AskResponse


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ask_state_conversation_user_history"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _fake_answer(conv_id: str, conclusion: str) -> AskResponse:
    return AskResponse(
        answer_id="", conversation_id=conv_id, conclusion=conclusion, answer="a",
        grounded=True, anchors=[], related_knowledge=[], citations=[], llm_mode="x",
    )


def _seed_conversation(store, notebook_id: str, user_id: str, questions: list[str]) -> str:
    conv_id = None
    for index, question in enumerate(questions):
        with store.database.write() as db:
            conv_id = store.ensure_conversation(db, notebook_id, conv_id, question, user_id)
        store.save_answer(
            notebook_id, conv_id, question,
            _fake_answer(conv_id, f"内部结论第{index}轮，绝不该外泄"), user_id,
        )
    return conv_id


def test_three_rounds_returns_only_user_questions_in_order(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="t")).id
    questions = ["第一问：介绍这个库", "第二问：再深入一点", "第三问：还有什么"]
    conv_id = _seed_conversation(store, nb, uid, questions)

    result = store.conversation_user_history(nb, conv_id, uid)

    assert result == "\n".join(f"User: {q}" for q in questions)
    assert "内部结论" not in result
    assert "Assistant" not in result
    with store.database.write() as db:
        _, expected = store._conversation_histories(db, conv_id)
    assert result == expected


def test_limit_bounds_to_the_most_recent_rounds(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="t")).id
    questions = ["第一问", "第二问", "第三问"]
    conv_id = _seed_conversation(store, nb, uid, questions)

    result = store.conversation_user_history(nb, conv_id, uid, limit=2)

    assert result == "\n".join(f"User: {q}" for q in questions[-2:])


def test_other_users_conversation_returns_empty_not_raise(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="t")).id
    conv_id = _seed_conversation(store, nb, uid, ["属于我的会话"])

    assert store.conversation_user_history(nb, conv_id, "someone-else") == ""


def test_wrong_notebook_returns_empty_not_raise(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb1 = repo.create_notebook(NotebookCreate(name="nb1")).id
    nb2 = repo.create_notebook(NotebookCreate(name="nb2")).id
    conv_id = _seed_conversation(store, nb1, uid, ["属于 nb1 的会话"])

    assert store.conversation_user_history(nb2, conv_id, uid) == ""


def test_nonexistent_conversation_returns_empty_not_raise(postgres_repository):
    repo = postgres_repository
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = repo.create_notebook(NotebookCreate(name="t")).id

    assert store.conversation_user_history(nb, "conv-does-not-exist", uid) == ""
