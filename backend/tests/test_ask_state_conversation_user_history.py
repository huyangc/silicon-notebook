"""PR-C (T2): ``AskStateStorePort.conversation_user_history`` — the
reasoning-followup rewrite gate's ONE read of prior turns. Language-blind by
construction (question lines only, never an answer ``conclusion`` and never a
citation) and scoped to the requesting member's own conversation in one
notebook, byte-for-byte the same ownership predicate as
``ensure_conversation``'s continuation check. sqlite backend + the facade's
one-hop forward.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _fake_answer(conv_id: str, conclusion: str) -> AskResponse:
    return AskResponse(
        answer_id="", conversation_id=conv_id, conclusion=conclusion, answer="a",
        grounded=True, anchors=[], related_knowledge=[], citations=[], llm_mode="x",
    )


def _seed_conversation(store, notebook_id: str, user_id: str, questions: list[str]) -> str:
    """Create one conversation and append one answer turn per question, the
    same ensure_conversation -> save_answer sequence every ask handler uses."""
    conv_id = None
    for index, question in enumerate(questions):
        with store.database.write() as db:
            store.database.begin_guarded_write(db)
            conv_id = store.ensure_conversation(db, notebook_id, conv_id, question, user_id)
        store.save_answer(
            notebook_id, conv_id, question,
            _fake_answer(conv_id, f"内部结论第{index}轮，绝不该外泄"), user_id,
        )
    return conv_id


def _nb(repo, name="t"):
    return repo.create_notebook(NotebookCreate(name=name)).id


def test_three_rounds_returns_only_user_questions_in_order(repo):
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = _nb(repo)
    questions = ["第一问：介绍这个库", "第二问：再深入一点", "第三问：还有什么"]
    conv_id = _seed_conversation(store, nb, uid, questions)

    result = store.conversation_user_history(nb, conv_id, uid)

    assert result == "\n".join(f"User: {q}" for q in questions)
    assert "内部结论" not in result
    assert "Assistant" not in result
    # Byte-identical to the existing user-history projection this reuses.
    with store.database.write() as db:
        store.database.begin_guarded_write(db)
        _, expected = store._conversation_histories(db, conv_id)
    assert result == expected


def test_limit_bounds_to_the_most_recent_rounds(repo):
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = _nb(repo)
    questions = ["第一问", "第二问", "第三问"]
    conv_id = _seed_conversation(store, nb, uid, questions)

    result = store.conversation_user_history(nb, conv_id, uid, limit=2)

    assert result == "\n".join(f"User: {q}" for q in questions[-2:])


def test_other_users_conversation_returns_empty_not_raise(repo):
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = _nb(repo)
    conv_id = _seed_conversation(store, nb, uid, ["属于我的会话"])

    assert store.conversation_user_history(nb, conv_id, "someone-else") == ""


def test_wrong_notebook_returns_empty_not_raise(repo):
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb1 = _nb(repo, "nb1")
    nb2 = _nb(repo, "nb2")
    conv_id = _seed_conversation(store, nb1, uid, ["属于 nb1 的会话"])

    assert store.conversation_user_history(nb2, conv_id, uid) == ""


def test_nonexistent_conversation_returns_empty_not_raise(repo):
    store = repo._runtime.ask_state
    uid = repo.current_user().id
    nb = _nb(repo)

    assert store.conversation_user_history(nb, "conv-does-not-exist", uid) == ""
