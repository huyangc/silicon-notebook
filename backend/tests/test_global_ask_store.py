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


def test_create_persists_submitted_via_asked_at_and_updated_at(store):
    """记录平权(SQLite v81):``create`` 把 submitted_via/asked_at 写进自己的列,
    ``updated_at`` 起步等于 ``created_at``——与 ``ask_jobs`` 插入时两列同一个
    ``now`` 的写法一致。"""
    value = job()
    value.asked_at = "2026-09-20T00:00:00+00:00"
    store.create(value, "user-a", "request-a", "payload", "mcp", new_conversation=True)
    with store.database.connect() as db:
        row = db.execute(
            "SELECT submitted_via, asked_at, created_at, updated_at "
            "FROM global_ask_jobs WHERE id=?", (value.job_id,),
        ).fetchone()
    assert row["submitted_via"] == "mcp"
    assert row["asked_at"] == "2026-09-20T00:00:00+00:00"
    assert row["updated_at"] == row["created_at"]
    reread = store.job(value.job_id, "user-a")
    assert reread.asked_at == "2026-09-20T00:00:00+00:00"
    assert reread.updated_at == row["created_at"]


def test_save_writes_error_detail_and_moves_updated_at(store, monkeypatch):
    """``save`` 的失败原文进自己的列(owner-facing payload 保持固定文案),
    ``updated_at`` 挪到落终态的那一刻。"""
    import app.repositories.global_ask_store as store_module

    value = job(status="running")
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
            "SELECT error_detail FROM global_ask_jobs WHERE id=?", (value.job_id,),
        ).fetchone()
    assert row["error_detail"] == "RuntimeError: boom"


def test_save_progress_bumps_updated_at(store, monkeypatch):
    import app.repositories.global_ask_store as store_module

    value = job(status="running")
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    monkeypatch.setattr(
        store_module, "_transition_now", lambda: "2026-09-21T01:00:00+00:00"
    )
    value.searched_notebook_ids = ["nb-a"]
    assert store.save_progress(value, "user-a")
    reread = store.job(value.job_id, "user-a")
    assert reread.updated_at == "2026-09-21T01:00:00+00:00"


def test_set_feedback_writes_feedback_at_and_first_write_wins(store):
    value = job()
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    updated = store.set_feedback(value.job_id, "user-a", "useful")
    assert updated.feedback_at != ""
    first_feedback_at = updated.feedback_at
    again = store.set_feedback(value.job_id, "user-a", "not_useful")
    assert again.feedback_at == first_feedback_at
    assert again.feedback == "useful"


def test_recover_stamps_updated_at(store, monkeypatch):
    import app.repositories.global_ask_store as store_module

    value = job(status="running")
    store.create(value, "user-a", "request-a", "payload", "web", new_conversation=True)
    monkeypatch.setattr(
        store_module, "_transition_now", lambda: "2026-09-21T02:00:00+00:00"
    )
    store.recover()
    reread = store.job(value.job_id, "user-a")
    assert reread.status == "interrupted"
    assert reread.updated_at == "2026-09-21T02:00:00+00:00"


def test_admin_job_record_returns_record_and_none_for_foreign_or_missing_job(store):
    value = job(status="running")
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


def test_migration_81_backfills_submitted_via_and_is_rerunnable(tmp_path):
    """SQLite v81 的回填幂等:只补空的 submitted_via,已有值不被覆盖;再跑一次
    ``_migration_81`` 不重复加列(``add_column_if_missing`` 的 PRAGMA 检查)也
    不改动已经非空的行。"""
    settings = Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'backfill.db'}",
        storage_dir=str(tmp_path / "storage"),
    )
    repo = SQLiteRepository(settings)
    try:
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO global_ask_conversations "
                "(id,user_id,title,scope_json,submitted_via,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("gc-1", "user-a", "", '{"mode":"all","notebook_ids":[]}', "mcp",
                 "2026-09-20T00:00:00Z", "2026-09-20T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO global_ask_jobs "
                "(id,conversation_id,user_id,client_request_id,request_json,status,"
                "payload_json,created_at,submitted_via,asked_at,updated_at,error_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("gj-1", "gc-1", "user-a", None, "{}", "done", "{}",
                 "2026-09-20T00:00:00Z", "", "", "2026-09-20T00:00:00Z", ""),
            )
            db.execute(
                "INSERT INTO global_ask_jobs "
                "(id,conversation_id,user_id,client_request_id,request_json,status,"
                "payload_json,created_at,submitted_via,asked_at,updated_at,error_detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("gj-2", "gc-1", "user-a", None, "{}", "done", "{}",
                 "2026-09-20T00:00:01Z", "web", "", "2026-09-20T00:00:01Z", ""),
            )

        repo._migrator._migration_81()

        with repo._runtime.database.connect() as db:
            rows = {
                row["id"]: row["submitted_via"]
                for row in db.execute(
                    "SELECT id, submitted_via FROM global_ask_jobs"
                ).fetchall()
            }
            column_count = db.execute(
                "SELECT COUNT(*) AS c FROM pragma_table_info('global_ask_jobs') "
                "WHERE name='submitted_via'"
            ).fetchone()["c"]
        assert rows["gj-1"] == "mcp"
        # Already non-empty: the backfill's ``WHERE submitted_via=''`` must not
        # touch it, even though it differs from the conversation's own value.
        assert rows["gj-2"] == "web"
        assert column_count == 1
    finally:
        repo.close()


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
