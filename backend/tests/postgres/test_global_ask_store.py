from __future__ import annotations

import pytest

from app.models.global_ask import GlobalAskJob, GlobalNotebookScope
from app.repositories.global_ask_store import GlobalAskStore
from app.repositories.postgres.migrator import PostgresMigrator


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def store(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 63
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


def test_set_feedback_first_write_wins_and_gates_owner_and_status(store):
    """PG twin of the SQLite store contract in
    ``backend/tests/test_global_ask_store.py``: ownership, completion, and
    first-write-wins are all expressed in the UPDATE's own WHERE clause
    (``payload_json::jsonb->>'feedback'``), not a read-then-write from
    Python."""
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)

    # Not yet done: rejected.
    assert store.set_feedback(value.job_id, "user-a", "useful") is None

    value.status = "done"
    assert store.save(value, "user-a")

    # Foreign user: rejected, row untouched.
    assert store.set_feedback(value.job_id, "user-b", "useful") is None
    assert store.job(value.job_id, "user-a").feedback == ""

    # Missing job: rejected.
    assert store.set_feedback("no-such-job", "user-a", "useful") is None

    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated.feedback == "useful"
    assert store.job(value.job_id, "user-a").feedback == "useful"

    # First write wins: a second, different rating does not overwrite it.
    again = store.set_feedback(value.job_id, "user-a", "not_useful")
    assert again.feedback == "useful"
    assert store.job(value.job_id, "user-a").feedback == "useful"


def test_set_feedback_updates_a_legacy_row_with_no_feedback_key_at_all(store):
    """A row persisted before this field existed has no ``feedback`` key in
    its JSONB at all -- the PG write-side empty check
    (``->>'feedback' IS NULL OR ... = ''``) must treat that the same as an
    explicit empty string."""
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.status = "done"
    assert store.save(value, "user-a")
    with store.database.write() as db:
        db.execute(
            "UPDATE global_ask_jobs SET payload_json = (payload_json::jsonb - 'feedback')::text WHERE id=%s",
            (value.job_id,),
        )
    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated is not None and updated.feedback == "useful"


def test_pg_create_persists_submitted_via_asked_at_and_updated_at(store):
    """记录平权(PostgreSQL 0061)的 PG 孪生:``create`` 把 submitted_via/
    asked_at 写进自己的列,``updated_at`` 起步等于 ``created_at``。"""
    value = job()
    value.asked_at = "2026-09-20T00:00:00+00:00"
    store.create(value, "user-a", "request-a", "payload", "mcp", new_conversation=True)
    with store.database.connect() as db:
        row = db.execute(
            "SELECT submitted_via, asked_at, created_at, updated_at "
            "FROM global_ask_jobs WHERE id=%s", (value.job_id,),
        ).fetchone()
    assert row["submitted_via"] == "mcp"
    assert row["asked_at"] == "2026-09-20T00:00:00+00:00"
    assert row["updated_at"] == row["created_at"]
    reread = store.job(value.job_id, "user-a")
    assert reread.asked_at == "2026-09-20T00:00:00+00:00"
    assert reread.updated_at == row["created_at"]


def test_pg_save_writes_error_detail_and_moves_updated_at(store, monkeypatch):
    import app.repositories.global_ask_store as store_module

    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    monkeypatch.setattr(
        store_module, "_transition_now", lambda: "2026-09-21T00:00:00+00:00"
    )
    value.status = "failed"
    assert store.save(value, "user-a", error_detail="RuntimeError: boom")
    reread = store.job(value.job_id, "user-a")
    assert reread.updated_at == "2026-09-21T00:00:00+00:00"
    with store.database.connect() as db:
        row = db.execute(
            "SELECT error_detail FROM global_ask_jobs WHERE id=%s", (value.job_id,),
        ).fetchone()
    assert row["error_detail"] == "RuntimeError: boom"


def test_pg_save_progress_bumps_updated_at(store, monkeypatch):
    import app.repositories.global_ask_store as store_module

    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    monkeypatch.setattr(
        store_module, "_transition_now", lambda: "2026-09-21T01:00:00+00:00"
    )
    value.searched_notebook_ids = ["nb-a"]
    assert store.save_progress(value, "user-a")
    reread = store.job(value.job_id, "user-a")
    assert reread.updated_at == "2026-09-21T01:00:00+00:00"


def test_pg_set_feedback_writes_feedback_at_and_first_write_wins(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.status = "done"
    assert store.save(value, "user-a")
    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated.feedback_at != ""
    first_feedback_at = updated.feedback_at
    again = store.set_feedback(value.job_id, "user-a", "not_useful")
    assert again.feedback_at == first_feedback_at
    assert again.feedback == "useful"


def test_pg_recover_stamps_updated_at(store, monkeypatch):
    import app.repositories.global_ask_store as store_module

    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    monkeypatch.setattr(
        store_module, "_transition_now", lambda: "2026-09-21T02:00:00+00:00"
    )
    store.recover()
    reread = store.job(value.job_id, "user-a")
    assert reread.status == "interrupted"
    assert reread.updated_at == "2026-09-21T02:00:00+00:00"


def test_pg_admin_job_record_returns_record_and_none_for_foreign_or_missing_job(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "mcp", new_conversation=True)
    value.status = "failed"
    assert store.save(value, "user-a", error_detail="RuntimeError: boom")

    record = store.admin_job_record(value.job_id, "user-a")
    assert record is not None
    assert record["error_detail"] == "RuntimeError: boom"
    assert record["submitted_via"] == "mcp"
    assert record["job"].job_id == value.job_id

    assert store.admin_job_record(value.job_id, "user-b") is None
    assert store.admin_job_record("no-such-job", "user-a") is None


def test_postgres_global_batch_authority_and_source_ceiling(store, monkeypatch):
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
    with database.write() as db:
        db.execute("UPDATE source_elements SET metadata=%s::jsonb WHERE id=%s", ('{"image":"enhanced"}', "element"))
    assert sources.evidence_fingerprints(["element"]) == fingerprint
    with database.write() as db:
        db.execute("UPDATE source_elements SET text=%s WHERE id=%s", ("changed evidence", "element"))
    assert sources.evidence_fingerprints(["element"]) != fingerprint


def test_postgres_passage_snapshot_pairs_passage_text_with_its_element_prints(store):
    """PG 孪生:段落摘要与元素指纹同一条语句、同一个快照,元素正文不过线。

    与 SQLite 侧同一份合同(``GlobalAskSourceStorePort``):元素 id 确定性复用,
    所以只按 id 读指纹无法回答「这是不是这次检索读到的那段原文」。
    """
    import hashlib

    from app.repositories.postgres.source_store import SourceStore

    database = store.database
    now = "2026-09-20T00:00:00Z"
    halves = ("首段 · α 数据", "次段 · β 数据 🔬")
    passage_text = " ".join(halves)
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("passage-owner", "passage@example.test", "Passage", "user", "active", now, now),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)",
            ("nb-passage", "Passages", "passage-owner", now, now),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
            ("src-passage", "nb-passage", "Original", "markdown", now, now),
        )
        for index, text in enumerate(halves):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                (f"el-src-passage-{index:04d}", "src-passage", "paragraph", f"p{index}", text, now),
            )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)",
            ("chunk-wide", "nb-passage", "src-passage", passage_text, "",
             '["el-src-passage-0000","el-src-passage-0001"]', now),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)",
            ("chunk-bare", "nb-passage", "src-passage", "独立段落", "", "[]", now),
        )
    sources = object.__new__(SourceStore)
    sources.database = database

    def digest(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    snapshot = sources.passage_evidence_snapshot(
        ["chunk-wide", "chunk-bare", "chunk-vanished"]
    )
    assert snapshot["chunk-wide"] == {
        "text_sha": digest(passage_text),
        "elements": {
            "el-src-passage-0000": ("src-passage", digest(halves[0])),
            "el-src-passage-0001": ("src-passage", digest(halves[1])),
        },
    }
    # 「段落没有元素」与「段落已经没了」是两件事。
    assert snapshot["chunk-bare"] == {"text_sha": digest("独立段落"), "elements": {}}
    assert "chunk-vanished" not in snapshot

    with database.write() as db:
        db.execute("UPDATE source_elements SET text=%s WHERE id=%s",
                   ("首段 · α 数据(修订)", "el-src-passage-0000"))
    after = sources.passage_evidence_snapshot(["chunk-wide"])
    assert after["chunk-wide"]["text_sha"] == snapshot["chunk-wide"]["text_sha"]
    assert after["chunk-wide"]["elements"]["el-src-passage-0000"] == (
        "src-passage", digest("首段 · α 数据(修订)"),
    )
    with database.write() as db:
        db.execute("UPDATE chunks SET text=%s WHERE id=%s", ("整段换掉", "chunk-wide"))
    assert sources.passage_evidence_snapshot(["chunk-wide"])["chunk-wide"][
        "text_sha"
    ] == digest("整段换掉")


def test_postgres_passage_snapshot_batches_past_the_parameter_limit(store):
    """上千个段落 id 分批读,每个段落仍在自己的那一个快照里。"""
    import hashlib

    from app.repositories.postgres.source_store import SourceStore

    database = store.database
    now = "2026-09-20T00:00:00Z"
    wanted = [f"chunk-{index:05d}" for index in range(1500)]
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            ("bulk-owner", "bulk@example.test", "Bulk", "user", "active", now, now),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) VALUES(%s,%s,%s,%s,%s)",
            ("nb-bulk", "Bulk", "bulk-owner", now, now),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s)",
            ("src-bulk", "nb-bulk", "Original", "markdown", now, now),
        )
        for chunk_id in wanted:
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (chunk_id, "nb-bulk", "src-bulk", f"text of {chunk_id}", "", "[]", now),
            )
    sources = object.__new__(SourceStore)
    sources.database = database

    snapshot = sources.passage_evidence_snapshot(wanted)
    assert len(snapshot) == len(wanted)
    assert snapshot[wanted[-1]]["text_sha"] == hashlib.sha256(
        f"text of {wanted[-1]}".encode("utf-8")
    ).hexdigest()


def _guarded_store(postgres_database):
    from app.repositories.postgres import access_sql
    from app.repositories.postgres.read_authority_lock import lock_reader_access_on

    assert PostgresMigrator(postgres_database).migrate() == 63
    return GlobalAskStore(
        postgres_database, marker="%s", access_sql=access_sql,
        read_authority_lock=lock_reader_access_on,
    )


def _seed_participants(store):
    now = "2026-09-20T00:00:00Z"
    with store.database.write() as db:
        for user in ("user-a", "user-b"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (user, f"{user}@x", user, "user", "active", now, now))
        for nb, owner in (("nb-a", "user-a"), ("nb-b", "user-b")):
            db.execute(
                "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (nb, f"NB {nb}", owner, "ready", now, now))


def test_guarded_admin_job_record_applies_the_owner_rule_and_names_readable_participants(postgres_database):
    """PG 孪生:FOR SHARE / FOR KEY SHARE 下同一套判定(见 SQLite 侧同名用例)。"""
    store = _guarded_store(postgres_database)
    _seed_participants(store)
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    value.status = "done"
    assert store.save(value, "user-a")

    with store.guarded_admin_job_record(value.job_id, "user-a", reader_id=None) as record:
        assert record is not None and record["notebook_names"] == {"nb-a": "NB nb-a"}
    with store.guarded_admin_job_record(value.job_id, "user-a", reader_id="user-a") as record:
        assert record is None
    with store.guarded_admin_job_record(value.job_id, "user-b", reader_id=None) as record:
        assert record is None

    with store.database.write() as db:
        db.execute("UPDATE notebooks SET created_by=%s WHERE id=%s", ("user-a", "nb-b"))
    with store.guarded_admin_job_record(value.job_id, "user-a", reader_id="user-a") as record:
        assert record is not None
        assert record["notebook_names"] == {"nb-a": "NB nb-a", "nb-b": "NB nb-b"}
