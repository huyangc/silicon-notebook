"""SQLite store-level contract for ``GlobalAskStore.set_feedback``.

Mirrors ``backend/tests/postgres/test_global_ask_store.py``'s structure.
Notably, the ownership and first-write-wins guarantees below sit BELOW
``GlobalAskService.submit_feedback``'s own authority check -- that service
method never reaches a foreign user's row (``get_job`` 404s first, before
``store.set_feedback`` is ever called), so those two SQL-level guarantees
can only be exercised by calling the store directly, the same way this file's
sibling exercises them against Postgres.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def store(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'global.db'}",
                         storage_dir=str(tmp_path / "storage"))
    repo = SQLiteRepository(settings)
    yield repo._runtime.global_ask_store
    repo.close()


def job(identifier="job-a", conversation_id="conversation-a", status="done"):
    return GlobalAskJob(
        job_id=identifier, conversation_id=conversation_id, status=status,
        question="比较两个笔记本的证据", created_at="2026-09-20T00:00:00Z",
        notebook_scope=GlobalNotebookScope(mode="include", notebook_ids=["nb-a", "nb-b"]),
        resolved_notebook_ids=["nb-a", "nb-b"],
    )


def test_set_feedback_writes_and_reads_back(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated.feedback == "useful"
    assert store.job(value.job_id, "user-a").feedback == "useful"


def test_set_feedback_first_write_wins(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    store.set_feedback(value.job_id, "user-a", "useful")
    again = store.set_feedback(value.job_id, "user-a", "not_useful")
    assert again.feedback == "useful"
    assert store.job(value.job_id, "user-a").feedback == "useful"


def test_set_feedback_rejects_foreign_user_and_leaves_row_untouched(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    assert store.set_feedback(value.job_id, "user-b", "useful") is None
    assert store.job(value.job_id, "user-a").feedback == ""


def test_set_feedback_rejects_unfinished_job(store):
    value = job(status="running")
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    assert store.set_feedback(value.job_id, "user-a", "useful") is None


def test_set_feedback_rejects_missing_job(store):
    assert store.set_feedback("no-such-job", "user-a", "useful") is None


def test_set_feedback_updates_legacy_payload_with_no_feedback_key_at_all(store):
    """A row persisted before this field existed has no ``feedback`` key in
    its stored JSON at all (the model default only applies on READ) -- the
    write-side empty check must treat a missing key the same as an explicit
    ``""``, not just the latter."""
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    with store.database.write() as db:
        row = db.execute("SELECT payload_json FROM global_ask_jobs WHERE id=?", (value.job_id,)).fetchone()
        payload = json.loads(row["payload_json"])
        del payload["feedback"]
        db.execute("UPDATE global_ask_jobs SET payload_json=? WHERE id=?",
                   (json.dumps(payload, ensure_ascii=False), value.job_id))
    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated is not None and updated.feedback == "useful"
