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


def test_completed_history_projects_only_successful_dialogue(store):
    from app.models.global_ask import GlobalAskAnswer

    first = job()
    store.create(first, "user-a", "request-a", "payload", "web", new_conversation=True)
    assert store.running_job_ids(first.conversation_id, "user-a") == [first.job_id]
    first.status = "done"
    first.response = GlobalAskAnswer(
        answer_id="answer", question=first.question, answer="上下文回答", created_at=first.created_at,
        notebook_scope=first.notebook_scope, resolved_notebook_ids=first.resolved_notebook_ids,
        searched_notebook_ids=[], cited_notebook_ids=[],
    )
    assert store.save(first, "user-a")
    failed = job("job-failed")
    store.create(failed, "user-a", "request-failed", "payload", "web", new_conversation=False)
    failed.status = "failed"
    assert store.save(failed, "user-a")
    assert store.completed_history(first.conversation_id, "user-a", 10) == [{
        "question": first.question, "answer": "上下文回答", "notebook_ids": ["nb-a", "nb-b"],
    }]
    assert store.completed_history(first.conversation_id, "user-b", 10) == []
    assert store.running_job_ids(first.conversation_id, "user-a") == []


def test_postgres_global_batch_authority_sources_and_evidence_snapshot(store):
    from app.repositories.postgres.sharing_store import SharingStore
    from app.repositories.postgres.source_store import SourceStore

    database = store.database
    now = "2026-09-19T00:00:00Z"
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("global-owner", "global@example.test", "Global", "user", "active", now, now),
        )
        for index in range(24):
            db.execute(
                "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)",
                (f"nb-{index}", f"Library {index}", "global-owner", now, now),
            )
            for kind in ("markdown", "memory"):
                db.execute(
                    "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    (f"source-{index}-{kind}", f"nb-{index}", "Original", kind, now, now),
                )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
            ("element", "source-0-markdown", "paragraph", "p1", "original evidence", now),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) VALUES(%s,%s,%s,%s,%s::jsonb,%s)",
            ("chunk", "nb-0", "source-0-markdown", "original evidence", '["element","missing"]', now),
        )
    sharing = object.__new__(SharingStore)
    sharing.database = database
    sources = object.__new__(SourceStore)
    sources.database = database
    names = sharing.readable_notebook_names("global-owner")
    assert len(names) == 24
    assert sharing.readable_notebook_ids(list(names), "global-owner") == set(names)
    assert sharing.readable_notebook_ids(list(names), "other-user") == set()
    ceilings = sources.visible_source_ids_by_notebook(list(names))
    assert all(values == [f"source-{nb.removeprefix('nb-')}-markdown"] for nb, values in ceilings.items())
    fingerprint = sources.evidence_fingerprints(["element"])
    with database.connect() as db:
        snapshot = sources.global_candidate_evidence(db, ["chunk", "missing-chunk"])
    assert snapshot["chunk"]["text"] == "original evidence"
    assert snapshot["chunk"]["element_ids"] == ["element", "missing"]
    assert snapshot["chunk"]["element_fingerprints"] == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET metadata=%s::jsonb WHERE id=%s", ('{"image":"enhanced"}', "element"))
    assert sources.evidence_fingerprints(["element"]) == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET text=%s WHERE id=%s", ("changed evidence", "element"))
    assert sources.evidence_fingerprints(["element"]) != fingerprint
