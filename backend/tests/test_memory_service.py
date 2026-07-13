from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.models.schemas import AskResponse, MemoryUpdate, NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def users(repo):
    return SimpleNamespace(
        alice=repo.create_user("a00123456", "pw"),
        bob=repo.create_user("b00654321", "pw"),
    )


@pytest.fixture
def notebook(repo, users):
    token = set_request_user(users.alice)
    try:
        item = repo.create_notebook(NotebookCreate(name="Memory notebook"))
    finally:
        reset_request_user(token)
    with repo._write() as db:
        db.executemany(
            "INSERT INTO agent_profiles "
            "(id,owner_id,name,description,status,created_at,updated_at) "
            "VALUES (?,?,?,'','active','t','t')",
            [
                ("agent-a", users.alice.id, "Agent A"),
                ("agent-b", users.alice.id, "Agent B"),
                ("agent-bob", users.bob.id, "Agent Bob"),
            ],
        )
    return item


@pytest.fixture
def memory_service(repo):
    return repo._runtime.memory_service


@pytest.fixture
def saved_answer(repo, notebook, users):
    answer_id = repo._runtime.ask_state.save_answer(
        notebook.id,
        None,
        "What is stable?",
        AskResponse(conclusion="Grounded answer", answer="Grounded answer"),
        users.alice.id,
    )
    return SimpleNamespace(id=answer_id, notebook_id=notebook.id)


def _candidate(service, notebook, user, agent, request, title="Title", body="Body"):
    return service.create_candidate(
        notebook.id,
        user.id,
        agent,
        request,
        title,
        body,
        ["analog"],
        "task",
        {"goal": "review"},
        [{"source_id": "source-1"}],
    )


def test_agent_candidate_is_private_to_owner_but_shared_across_owner_profiles(
    memory_service, users, notebook
):
    first = _candidate(memory_service, notebook, users.alice, "agent-a", "req-1")
    second = _candidate(memory_service, notebook, users.alice, "agent-b", "req-2")

    assert memory_service.get(first.id, users.alice.id).id == first.id
    assert {item.id for item in memory_service.list_memories(users.alice.id).items} == {
        first.id,
        second.id,
    }
    with pytest.raises(KeyError):
        memory_service.get(first.id, users.bob.id)
    with pytest.raises(PermissionError):
        _candidate(memory_service, notebook, users.alice, "agent-bob", "req-3")


def test_duplicate_agent_request_is_idempotent(
    memory_service, users, notebook, monkeypatch
):
    first = _candidate(memory_service, notebook, users.alice, "agent-a", "req-same")
    # Simulate two callers whose service-level preflight both missed.  The
    # serialized store write is the final idempotency boundary.
    monkeypatch.setattr(
        memory_service.store, "memory_by_agent_request", lambda *args: None
    )
    second = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-same",
        title="Changed",
        body="Changed body",
    )

    assert second.id == first.id
    assert second.title == "Title"


def test_confirm_writes_revision_and_duplicate_answer_is_idempotent(
    memory_service, saved_answer, users, repo
):
    first = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "T",
        "B",
        [],
    )
    second = memory_service.create_from_answer(
        saved_answer.notebook_id,
        users.alice.id,
        saved_answer.id,
        "T2",
        "B2",
        [],
    )

    assert second.id == first.id
    assert second.title == "T"
    assert memory_service.revisions(first.id, users.alice.id)[0].status == "confirmed"
    assert memory_service.get(first.id, users.alice.id).embedding_status == "ready"
    with repo._connect() as db:
        embedded = db.execute(
            "SELECT model, dimension, length(vector) AS size FROM memory_embeddings "
            "WHERE memory_id=?",
            (first.id,),
        ).fetchone()
    assert embedded is not None
    assert embedded["dimension"] == repo.embedder.dim
    assert embedded["size"] == repo.embedder.dim * 4


def test_candidate_lifecycle_updates_snapshots_and_rejects_invalid_transition(
    memory_service, users, notebook
):
    item = _candidate(memory_service, notebook, users.alice, "agent-a", "req-life")
    updated = memory_service.update(
        item.id,
        users.alice.id,
        MemoryUpdate(title="Edited", tags=["checked"]),
    )
    confirmed = memory_service.confirm(
        item.id,
        users.alice.id,
        MemoryUpdate(content_md="Confirmed body"),
    )
    deprecated = memory_service.deprecate(item.id, users.alice.id)

    assert updated.title == "Edited"
    assert confirmed.status == "confirmed"
    assert confirmed.confirmed_by == users.alice.id
    assert deprecated.status == "deprecated"
    assert [revision.status for revision in memory_service.revisions(item.id, users.alice.id)] == [
        "candidate",
        "candidate",
        "confirmed",
        "deprecated",
    ]
    with pytest.raises(ValueError):
        memory_service.confirm(
            item.id, users.alice.id, MemoryUpdate(title="must not be saved")
        )
    assert memory_service.get(item.id, users.alice.id).title == "Edited"


def test_reject_is_owner_scoped_and_list_filters_search_and_paginates(
    memory_service, users, notebook
):
    first = _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-a",
        "req-filter-1",
        title="环路稳定性",
        body="phase margin",
    )
    _candidate(
        memory_service,
        notebook,
        users.alice,
        "agent-b",
        "req-filter-2",
        title="Noise",
        body="thermal noise",
    )
    rejected = memory_service.reject(first.id, users.alice.id)

    page = memory_service.list_memories(
        users.alice.id,
        notebook_id=notebook.id,
        status="rejected",
        origin="external_agent",
        query="稳定性",
        offset=0,
        limit=1,
    )
    assert rejected.status == "rejected"
    assert page.total_count == 1
    assert [item.id for item in page.items] == [first.id]
    assert memory_service.list_memories(users.bob.id).total_count == 0
    with pytest.raises(KeyError):
        memory_service.reject(first.id, users.bob.id)


def test_notebook_summary_memory_counts_are_grouped_once(
    repo, users, notebook, memory_service
):
    token = set_request_user(users.alice)
    try:
        other = repo.create_notebook(NotebookCreate(name="Other"))
    finally:
        reset_request_user(token)
    _candidate(memory_service, notebook, users.alice, "agent-a", "req-count-1")
    memory_service.create_candidate(
        other.id,
        users.alice.id,
        None,
        "req-count-2",
        "Manual agent item",
        "Body",
        [],
        "task",
        {},
        [],
    )

    statements = []
    with repo._connect() as db:
        db.set_trace_callback(statements.append)
        counts = repo._runtime.notebook_summaries.list_for_user(users.alice.id)
        db.set_trace_callback(None)
    # The list call owns its own connection, so trace the shared database via a
    # temporary connect wrapper instead of relying on this probe connection.
    real_connect = repo._runtime.database.connect

    @contextmanager
    def traced_connect():
        with real_connect() as db:
            db.set_trace_callback(statements.append)
            yield db
            db.set_trace_callback(None)

    repo._runtime.database.connect = traced_connect
    try:
        counts = repo._runtime.notebook_summaries.list_for_user(users.alice.id)
    finally:
        repo._runtime.database.connect = real_connect

    by_id = {item.id: item.counts["memories"] for item in counts}
    grouped = [
        sql
        for sql in statements
        if "FROM memory_items" in sql and "GROUP BY created_by, notebook_id" in sql
    ]
    assert by_id[notebook.id] == 1
    assert by_id[other.id] == 1
    assert len(grouped) == 1
