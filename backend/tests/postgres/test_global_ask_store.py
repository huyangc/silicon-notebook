from __future__ import annotations

import pytest

from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 56
    return GlobalAskStore(postgres_database, marker="%s")


def job(identifier="job-a", conversation_id="conversation-a"):
    return GlobalAskJob(
        job_id=identifier, conversation_id=conversation_id, status="running",
        question="比较两个笔记本的证据", created_at="2026-09-19T00:00:00Z",
        notebook_scope=GlobalNotebookScope(mode="include", notebook_ids=["nb-a", "nb-b"]),
        resolved_notebook_ids=["nb-a", "nb-b"],
    )


def test_global_job_roundtrip_owner_isolation_and_cascade(store):
    value = job()
    store.create(value, "user-a", "request-a", "request payload", "mcp", new_conversation=True)
    assert store.job(value.job_id, "user-a") == value
    assert store.request_job("user-a", "request-a") == (value, "request payload")
    assert store.conversation(value.conversation_id, "user-a").submitted_via == "mcp"
    assert store.list_conversations("user-a", 10, 0)[0].notebook_scope == value.notebook_scope
    assert store.jobs(value.conversation_id, "user-a", 10, 0) == [value]
    assert store.job(value.job_id, "user-b") is None
    assert store.request_job("user-b", "request-a") is None
    assert store.list_conversations("user-b", 10, 0) == []
    assert not store.rename(value.conversation_id, "user-b", "越权修改")
    assert not store.delete(value.conversation_id, "user-b")
    assert store.rename(value.conversation_id, "user-a", "新标题")
    assert store.conversation(value.conversation_id, "user-a").title == "新标题"
    assert store.delete(value.conversation_id, "user-a")
    assert store.job(value.job_id, "user-a") is None
    assert store.request_job("user-a", "request-a") is None


def test_global_running_and_request_uniqueness_roll_back_without_orphans(store):
    from psycopg.errors import UniqueViolation

    first = job()
    store.create(first, "user-a", "request-a", "payload", "web", new_conversation=True)
    with pytest.raises(UniqueViolation):
        store.create(job("job-b"), "user-a", "request-b", "payload", "web", new_conversation=False)
    duplicate = job("job-c", "conversation-c")
    with pytest.raises(UniqueViolation):
        store.create(duplicate, "user-a", "request-a", "payload", "web", new_conversation=True)
    assert store.conversation("conversation-c", "user-a") is None
    cancelled = first.model_copy(update={"status": "cancelled"})
    assert store.save(cancelled, "user-a")
    assert not store.save(first.model_copy(update={"status": "done"}), "user-a")
    store.create(job("job-b"), "user-a", "request-b", "payload", "web", new_conversation=False)
    store.recover()
    assert store.job("job-a", "user-a").status == "cancelled"
    assert store.job("job-b", "user-a").status == "interrupted"
    assert not store.save(job("job-b").model_copy(update={"status": "done"}), "user-a")
