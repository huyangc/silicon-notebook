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


def test_legacy_response_row_still_reads_pg(store):
    """D1-1: a row persisted before the answer/trace/mode fields existed --
    pure ``response``, nothing new on the payload -- still round-trips and
    still projects through the Postgres COALESCE variant of
    ``_history_projection``."""
    from app.models.global_ask import GlobalAskAnswer

    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.status = "done"
    value.response = GlobalAskAnswer(
        answer_id="answer", question=value.question, answer="旧答案文本",
        created_at=value.created_at, notebook_scope=value.notebook_scope,
        resolved_notebook_ids=value.resolved_notebook_ids,
        searched_notebook_ids=[], cited_notebook_ids=[],
    )
    assert store.save(value, "user-a")
    reread = store.job(value.job_id, "user-a")
    assert reread.answer is None
    assert reread.mode == "chunk" and reread.trace == []
    assert reread.response.answer == "旧答案文本"
    assert store.completed_history(value.conversation_id, "user-a", 10) == [{
        "question": value.question, "answer": "旧答案文本",
        "notebook_ids": value.resolved_notebook_ids,
    }]


def test_new_answer_row_reads_pg(store):
    """A row whose only populated answer shape is the new ``answer`` field
    (``response`` left at its default ``None``) reads back through both the
    model round-trip and the COALESCE projection."""
    from app.models.ask import AskResponse, Citation

    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.status = "done"
    value.mode = "chunk"
    value.answer = AskResponse(
        conclusion="done", answer="新答案文本",
        citations=[Citation(
            label="[1]", source_id="s1", element_id="e1",
            location_label="L1", quoted_span="span1",
        )],
    )
    assert store.save(value, "user-a")
    reread = store.job(value.job_id, "user-a")
    assert reread.response is None
    assert reread.answer.answer == "新答案文本"
    assert store.completed_history(value.conversation_id, "user-a", 10) == [{
        "question": value.question, "answer": "新答案文本",
        "notebook_ids": value.resolved_notebook_ids,
    }]


def test_both_shapes_in_one_conversation_history_pg(store):
    """A legacy-shaped turn and a new-shaped turn in the SAME conversation
    both surface in ``completed_history``, newest first."""
    from app.models.ask import AskResponse
    from app.models.global_ask import GlobalAskAnswer

    legacy = job("job-legacy", "conversation-mixed")
    legacy.created_at = "2026-09-19T00:00:00Z"
    store.create(legacy, "user-a", "request-legacy", "payload", "web", new_conversation=True)
    legacy.status = "done"
    legacy.response = GlobalAskAnswer(
        answer_id="answer-legacy", question=legacy.question, answer="第一答",
        created_at=legacy.created_at, notebook_scope=legacy.notebook_scope,
        resolved_notebook_ids=legacy.resolved_notebook_ids,
        searched_notebook_ids=[], cited_notebook_ids=[],
    )
    assert store.save(legacy, "user-a")

    new = job("job-new", "conversation-mixed")
    new.created_at = "2026-09-19T00:00:01Z"
    store.create(new, "user-a", "request-new", "payload", "web", new_conversation=False)
    new.status = "done"
    new.answer = AskResponse(conclusion="done", answer="第二答")
    assert store.save(new, "user-a")

    history = store.completed_history("conversation-mixed", "user-a", 10)
    assert [turn["answer"] for turn in history] == ["第二答", "第一答"]


def test_progress_patch_preserves_payload_and_cannot_overwrite_terminal_state(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.searched_notebook_ids = ["nb-a"]
    value.degraded_notebook_ids = ["nb-a"]
    assert store.save_progress(value, "user-a")
    assert store.job(value.job_id, "user-a").question == value.question
    assert store.job(value.job_id, "user-a").degraded_notebook_ids == ["nb-a"]
    assert not store.save_progress(value, "user-b")
    value.status = "cancelled"
    assert store.save(value, "user-a")
    value.searched_notebook_ids.append("nb-b")
    assert not store.save_progress(value, "user-a")
    assert store.job(value.job_id, "user-a").searched_notebook_ids == ["nb-a"]


def test_postgres_global_batch_authority_sources_and_evidence_snapshot(store, monkeypatch):
    from contextlib import contextmanager
    import hashlib

    from app.repositories.postgres.sharing_store import SharingStore
    from app.repositories.postgres.source_store import SourceStore

    database = store.database
    now = "2026-09-19T00:00:00Z"
    original_text = "原始证据 · β 低温性能 🔋"
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
            ("element", "source-0-markdown", "paragraph", "p1", original_text, now),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) VALUES(%s,%s,%s,%s,%s::jsonb,%s)",
            ("chunk", "nb-0", "source-0-markdown", original_text, '["element","missing"]', now),
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
    projections = []
    connect = database.connect

    class ProjectionProbe:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query, parameters):
            cursor = self.connection.execute(query, parameters)
            projections.append({column.name for column in cursor.description})
            return cursor

    @contextmanager
    def probe_connection():
        with connect() as connection:
            yield ProjectionProbe(connection)

    monkeypatch.setattr(database, "connect", probe_connection)
    fingerprint = sources.evidence_fingerprints(["element"])
    assert fingerprint == {"element": ("source-0-markdown", hashlib.sha256(original_text.encode("utf-8")).hexdigest())}
    assert projections.pop() == {"id", "source_id", "evidence_hash"}
    with database.connect() as db:
        snapshot = sources.global_candidate_evidence(db, ["chunk", "missing-chunk"])
    assert projections.pop() == {
        "id", "source_id", "text", "section_path", "element_ids", "source_title",
        "evidence_id", "evidence_source_id", "evidence_hash",
    }
    assert snapshot["chunk"]["text"] == original_text
    assert snapshot["chunk"]["element_ids"] == ["element", "missing"]
    assert snapshot["chunk"]["element_fingerprints"] == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET metadata=%s::jsonb WHERE id=%s", ('{"image":"enhanced"}', "element"))
    assert sources.evidence_fingerprints(["element"]) == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET text=%s WHERE id=%s", ("changed evidence", "element"))
    assert sources.evidence_fingerprints(["element"]) != fingerprint
