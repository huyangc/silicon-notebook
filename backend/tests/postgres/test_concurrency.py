from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from app.models.ask import AskRequest, AskResponse
from app.models.memory import MemoryWrite
from app.repositories.postgres.ask_state_store import AskStateStore
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.group_store import GroupStore as PostgresGroupStore
from app.repositories.postgres.knowhow_store import KnowhowStore
from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.notebook_store import NotebookStore
from app.repositories.postgres.query_store import QueryStore
from app.repositories.postgres.source_store import SourceStore
from app.services.repository_runtime import RepositoryCompatibilitySeams


def _wait_for_lock_wait(postgres_database, needle: str, future) -> None:
    import psycopg
    from psycopg.rows import dict_row

    deadline = time.monotonic() + 5
    with psycopg.connect(
        postgres_database._database_url,
        autocommit=True,
        row_factory=dict_row,
    ) as inspector:
        while time.monotonic() < deadline:
            if future.done():
                result = future.result()
                raise AssertionError(
                    f"operation finished before waiting for {needle!r}: {result!r}"
                )
            waiting = inspector.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                "AND wait_event_type='Lock' AND state='active' "
                "AND query ILIKE %s LIMIT 1",
                (f"%{needle}%",),
            ).fetchone()
            if waiting is not None:
                return
            time.sleep(0.01)
    raise AssertionError(f"operation never waited for {needle!r}")


@pytest.mark.postgres_integration
def test_notebook_delete_locks_parent_before_retention_snapshot(
    postgres_database,
):
    """A child write cannot commit after the deletion snapshot has begun.

    An ACCESS EXCLUSIVE table lock pauses delete immediately after it locks the
    notebook row but before retention maintenance/projection. The late ask must
    then wait on the FK's FOR KEY SHARE, and is rejected after deletion commits.
    Removing the aggregate-root FOR UPDATE lets that ask commit while delete is
    paused, reproducing the lost-activity window this test protects.
    """
    import psycopg
    from psycopg.rows import dict_row

    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T00:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES ('delete-owner','delete-owner@x','Delete Owner','user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks "
            "(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('delete-nb','Delete NB','delete-owner','ready',%s,%s)",
            (now, now),
        )

    store = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )

    def insert_late_ask() -> str:
        try:
            with postgres_database.write() as connection:
                connection.execute(
                    "INSERT INTO ask_jobs "
                    "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
                    "VALUES ('late-ask','delete-nb','delete-owner','chunk',"
                    "'late question','completed',%s,%s)",
                    (now, now),
                )
        except psycopg.errors.ForeignKeyViolation:
            return "foreign-key-rejected"
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        with psycopg.connect(
            postgres_database._database_url, row_factory=dict_row
        ) as blocker:
            blocker.execute(
                "LOCK TABLE retained_user_activity IN ACCESS EXCLUSIVE MODE"
            )
            delete_future = executor.submit(
                store.delete_row_and_orphan_embeddings, "delete-nb"
            )
            _wait_for_lock_wait(postgres_database, "retained_user_activity", delete_future)

            late_future = executor.submit(insert_late_ask)
            _wait_for_lock_wait(postgres_database, "INSERT INTO ask_jobs", late_future)

        assert delete_future.result(timeout=5) == []
        assert late_future.result(timeout=5) == "foreign-key-rejected"

    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM notebooks WHERE id='delete-nb'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM ask_jobs WHERE id='late-ask'"
        ).fetchone() is None


@pytest.mark.postgres_integration
def test_activity_delete_race_prefers_retained_lifecycle_postgres(
    postgres_database, postgres_settings, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('activity-race-owner','activity-race@x','Activity Race',"
            "'user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('activity-race-nb','Activity Race','activity-race-owner',"
            "'ready',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO ask_jobs(id,notebook_id,created_by,mode,question,status,"
            "created_at,updated_at) VALUES ('activity-race-ask','activity-race-nb',"
            "'activity-race-owner','chunk','race question','completed',%s,%s)",
            (now, now),
        )

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    queries = QueryStore(postgres_database, postgres_settings)
    reader_reached_retained = threading.Event()
    allow_retained_read = threading.Event()
    original_connect = postgres_database.connect

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, statement, *args, **kwargs):
            if "SELECT a.* FROM retained_user_activity" in statement:
                reader_reached_retained.set()
                assert allow_retained_read.wait(timeout=5)
            return self._connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def coordinated_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(postgres_database, "connect", coordinated_connect)

    with ThreadPoolExecutor(max_workers=2) as executor:
        activity_future = executor.submit(
            queries.list_user_activity,
            "activity-race-owner",
            activity_type="ask",
            include_inaccessible_questions=True,
            limit=50,
        )
        assert reader_reached_retained.wait(timeout=5)
        delete_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "activity-race-nb"
        )
        try:
            assert delete_future.result(timeout=5) == []
        finally:
            allow_retained_read.set()
        activity = activity_future.result(timeout=5)

    assert len(activity["items"]) == 1
    assert activity["items"][0]["id"] == "activity-race-ask"
    assert activity["items"][0]["notebook_deleted_at"]


@pytest.mark.postgres_integration
def test_live_ask_detail_delete_race_fails_closed_postgres(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('detail-race-owner','detail-race@x','Detail Race','user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('detail-race-nb','Detail Race','detail-race-owner','ready',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO ask_jobs(id,notebook_id,created_by,mode,question,status,"
            "created_at,updated_at) VALUES ('detail-race-ask','detail-race-nb',"
            "'detail-race-owner','chunk','detail race','completed',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) "
            "VALUES ('detail-race-ask',0,%s,%s)",
            (Jsonb({"kind": "retrieval"}), now),
        )

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    reader_reached_trace = threading.Event()
    allow_trace_read = threading.Event()
    original_connect = postgres_database.connect

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, statement, *args, **kwargs):
            if "SELECT step_json FROM ask_trace_steps" in statement:
                reader_reached_trace.set()
                assert allow_trace_read.wait(timeout=5)
            return self._connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def coordinated_connect(*args, **kwargs):
        with original_connect(*args, **kwargs) as connection:
            yield ConnectionProxy(connection)

    monkeypatch.setattr(postgres_database, "connect", coordinated_connect)

    with ThreadPoolExecutor(max_workers=2) as executor:
        detail_future = executor.submit(asks.ask_job_detail, "detail-race-ask")
        assert reader_reached_trace.wait(timeout=5)
        delete_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "detail-race-nb"
        )
        try:
            assert delete_future.result(timeout=5) == []
        finally:
            allow_trace_read.set()
        with pytest.raises(KeyError):
            detail_future.result(timeout=5)

    with asks.guarded_ask_detail(
        "detail-race-ask", actor_id="detail-race-owner", reader_id=None
    ) as snapshot:
        assert snapshot["job"]["notebook_deleted_at"]
        assert snapshot["job"]["retained_until"]
        assert snapshot["job"]["answer_id"] == ""
        assert snapshot["job"]["trace"] == []


@pytest.mark.postgres_integration
def test_guarded_ask_detail_holds_root_lease_through_projection_postgres(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('guard-detail-owner','guard-detail@x','Guard Detail',"
            "'user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('guard-detail-nb','Guard Detail','guard-detail-owner',"
            "'ready',%s,%s)",
            (now, now),
        )

    counter = iter(range(1, 20))
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-guard-detail-{next(counter)}",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    request = AskRequest(question="guarded detail")
    job_id, conversation_id = asks.begin_durable_job(
        "guard-detail-nb", request, "chunk", "guard-detail-owner"
    )
    answer_id = asks.save_answer_for_job(
        job_id,
        "guard-detail-nb",
        conversation_id,
        request.question,
        AskResponse(
            answer="protected answer",
            conclusion="protected answer",
            citations=[],
            anchors=[],
        ),
        "guard-detail-owner",
    )
    assert answer_id

    with ThreadPoolExecutor(max_workers=1) as executor:
        with asks.guarded_ask_detail(
            job_id, actor_id="guard-detail-owner", reader_id=None
        ) as snapshot:
            assert snapshot["answer_detail"]["payload"]["answer"] == (
                "protected answer"
            )
            delete_future = executor.submit(
                notebooks.delete_row_and_orphan_embeddings, "guard-detail-nb"
            )
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM notebooks", delete_future
            )
        assert delete_future.result(timeout=5) == []

    with pytest.raises(KeyError):
        asks.ask_job_detail(job_id)
    with asks.guarded_ask_detail(
        job_id, actor_id="guard-detail-owner", reader_id=None
    ) as snapshot:
        assert snapshot["job"]["notebook_deleted_at"]
        assert snapshot["job"]["answer_id"] == ""
        assert snapshot["job"]["trace"] == []


@pytest.mark.postgres_integration
def test_guarded_ask_detail_locks_group_read_authority_postgres(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        for user_id in ("guard-owner", "guard-reader"):
            connection.execute(
                "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
                "VALUES (%s,%s,%s,'user',%s,%s)",
                (user_id, f"{user_id}@x", user_id, now, now),
            )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('guard-access-nb','Guard Access','guard-owner','ready',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES ('guard-access-group','Guard Access','project','',"
            "'guard-owner',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            "VALUES ('guard-access-group','guard-reader','reader',%s,'guard-owner')",
            (now,),
        )
        connection.execute(
            "INSERT INTO notebook_grants"
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES ('guard-access-grant','guard-access-nb','group',"
            "'guard-access-group','reader','guard-owner',%s)",
            (now,),
        )

    counter = iter(range(1, 20))
    asks = AskStateStore(
        postgres_database,
        RepositoryCompatibilitySeams(
            new_id=lambda prefix: f"{prefix}-guard-access-{next(counter)}",
            now=lambda: now,
            copy_chunk_size=lambda: 100,
            remap_json_ids=lambda value, _mapping: value,
            in_chunk_size=lambda: 100,
        ),
    )
    request = AskRequest(question="group access")
    job_id, conversation_id = asks.begin_durable_job(
        "guard-access-nb", request, "chunk", "guard-reader"
    )
    assert asks.save_answer_for_job(
        job_id,
        "guard-access-nb",
        conversation_id,
        request.question,
        AskResponse(
            answer="group protected",
            conclusion="group protected",
            citations=[],
            anchors=[],
        ),
        "guard-reader",
    )

    def revoke_group_membership() -> None:
        with postgres_database.write() as connection:
            connection.execute(
                "DELETE FROM group_members "
                "WHERE group_id='guard-access-group' AND user_id='guard-reader'"
            )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with asks.guarded_ask_detail(
            job_id, actor_id="guard-reader", reader_id="guard-reader"
        ) as snapshot:
            assert snapshot["answer_detail"]["payload"]["answer"] == (
                "group protected"
            )
            revoke_future = executor.submit(revoke_group_membership)
            _wait_for_lock_wait(
                postgres_database, "DELETE FROM group_members", revoke_future
            )
        revoke_future.result(timeout=5)

    with pytest.raises(KeyError):
        with asks.guarded_ask_detail(
            job_id, actor_id="guard-reader", reader_id="guard-reader"
        ):
            pass


@pytest.mark.postgres_integration
def test_final_answer_and_notebook_delete_share_root_first_lock_order(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('answer-delete-owner','answer-delete@x','Answer Delete',"
            "'user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('answer-delete-nb','Answer Delete','answer-delete-owner',"
            "'ready',%s,%s)",
            (now, now),
        )

    counter = iter(range(1, 20))
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-answer-delete-{next(counter)}",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    job_id, conversation_id = asks.begin_durable_job(
        "answer-delete-nb",
        AskRequest(question="answer/delete race"),
        "chunk",
        "answer-delete-owner",
    )
    response = AskResponse(
        answer="completed answer",
        conclusion="completed answer",
        citations=[],
        anchors=[],
    )
    save_holds_child_locks = threading.Event()
    allow_save = threading.Event()
    original_conversation_lock = asks._lock_answer_conversation_on

    def pause_after_conversation_lock(*args, **kwargs):
        original_conversation_lock(*args, **kwargs)
        save_holds_child_locks.set()
        assert allow_save.wait(timeout=5)

    monkeypatch.setattr(
        asks, "_lock_answer_conversation_on", pause_after_conversation_lock
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            asks.save_answer_for_job,
            job_id,
            "answer-delete-nb",
            conversation_id,
            "answer/delete race",
            response,
            "answer-delete-owner",
        )
        assert save_holds_child_locks.wait(timeout=5)
        delete_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "answer-delete-nb"
        )
        try:
            _wait_for_lock_wait(postgres_database, "SELECT id FROM notebooks", delete_future)
        finally:
            allow_save.set()
        answer_id = save_future.result(timeout=5)
        assert delete_future.result(timeout=5) == []

    assert answer_id
    with pytest.raises(KeyError):
        asks.ask_job_detail(job_id)
    with asks.guarded_ask_detail(
        job_id, actor_id="answer-delete-owner", reader_id=None
    ) as snapshot:
        assert snapshot["job"]["status"] == "done"
        assert snapshot["job"]["answer_id"] == ""
        assert snapshot["job"]["notebook_deleted_at"]


@pytest.mark.postgres_integration
def test_new_job_and_final_answer_use_compatible_notebook_leases(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('begin-save-owner','begin-save@x','Begin Save','user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('begin-save-nb','Begin Save','begin-save-owner','ready',%s,%s)",
            (now, now),
        )

    counter = iter(range(1, 20))
    counter_lock = threading.Lock()

    def new_id(prefix: str) -> str:
        with counter_lock:
            return f"{prefix}-begin-save-{next(counter)}"

    seams = RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    old_request = AskRequest(question="old turn")
    old_job, conversation_id = asks.begin_durable_job(
        "begin-save-nb", old_request, "chunk", "begin-save-owner"
    )
    begin_holds_conversation = threading.Event()
    allow_begin_insert = threading.Event()
    original_ensure_conversation = asks.ensure_conversation

    def pause_after_conversation_lock(*args, **kwargs):
        result = original_ensure_conversation(*args, **kwargs)
        begin_holds_conversation.set()
        assert allow_begin_insert.wait(timeout=10)
        return result

    monkeypatch.setattr(asks, "ensure_conversation", pause_after_conversation_lock)
    new_request = AskRequest(question="new turn", conversation_id=conversation_id)
    response = AskResponse(
        answer="old answer",
        conclusion="old answer",
        citations=[],
        anchors=[],
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        begin_future = executor.submit(
            asks.begin_durable_job,
            "begin-save-nb",
            new_request,
            "chunk",
            "begin-save-owner",
        )
        assert begin_holds_conversation.wait(timeout=5)
        save_future = executor.submit(
            asks.save_answer_for_job,
            old_job,
            "begin-save-nb",
            conversation_id,
            old_request.question,
            response,
            "begin-save-owner",
        )
        try:
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM conversations", save_future
            )
        finally:
            allow_begin_insert.set()
        new_job, continued_conversation = begin_future.result(timeout=5)
        answer_id = save_future.result(timeout=5)

    assert continued_conversation == conversation_id
    assert answer_id
    assert asks.ask_job_status(old_job)["status"] == "done"
    assert asks.ask_job_status(new_job)["status"] == "running"


@pytest.mark.postgres_integration
def test_bulk_conversation_delete_holds_root_against_notebook_delete(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('bulk-delete-owner','bulk-delete@x','Bulk Delete','user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('bulk-delete-nb','Bulk Delete','bulk-delete-owner','ready',%s,%s)",
            (now, now),
        )

    counter = iter(range(1, 30))
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-bulk-delete-{next(counter)}",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    conversation_ids = []
    for ordinal in range(2):
        request = AskRequest(question=f"terminal {ordinal}")
        job_id, conversation_id = asks.begin_durable_job(
            "bulk-delete-nb", request, "chunk", "bulk-delete-owner"
        )
        response = AskResponse(
            answer=f"answer {ordinal}",
            conclusion=f"answer {ordinal}",
            citations=[],
            anchors=[],
        )
        assert asks.save_answer_for_job(
            job_id,
            "bulk-delete-nb",
            conversation_id,
            request.question,
            response,
            "bulk-delete-owner",
        )
        conversation_ids.append(conversation_id)
    with postgres_database.write() as connection:
        connection.execute(
            "UPDATE conversations SET updated_at='2000-01-01T00:00:00+00:00' "
            "WHERE notebook_id='bulk-delete-nb'"
        )

    bulk_reached_children = threading.Event()
    allow_bulk_delete = threading.Event()
    original_delete_idle = asks._delete_idle_conversation_on

    def pause_before_first_child_delete(*args, **kwargs):
        if not bulk_reached_children.is_set():
            bulk_reached_children.set()
            assert allow_bulk_delete.wait(timeout=10)
        return original_delete_idle(*args, **kwargs)

    monkeypatch.setattr(
        asks, "_delete_idle_conversation_on", pause_before_first_child_delete
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        bulk_future = executor.submit(
            asks.bulk_delete_conversations,
            "bulk-delete-nb",
            1,
            "bulk-delete-owner",
        )
        assert bulk_reached_children.wait(timeout=5)
        notebook_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "bulk-delete-nb"
        )
        try:
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM notebooks", notebook_future
            )
        finally:
            allow_bulk_delete.set()
        bulk_result = bulk_future.result(timeout=5)
        assert notebook_future.result(timeout=5) == []

    assert bulk_result.deleted == 2
    assert sorted(bulk_result.deleted_ids) == sorted(conversation_ids)


@pytest.mark.postgres_integration
def test_single_conversation_delete_holds_root_against_notebook_delete(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('single-delete-owner','single-delete@x','Single Delete',"
            "'user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('single-delete-nb','Single Delete','single-delete-owner',"
            "'ready',%s,%s)",
            (now, now),
        )

    counter = iter(range(1, 30))
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-single-delete-{next(counter)}",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    asks = AskStateStore(postgres_database, seams)
    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    first_request = AskRequest(question="completed turn")
    first_job, conversation_id = asks.begin_durable_job(
        "single-delete-nb", first_request, "chunk", "single-delete-owner"
    )
    response = AskResponse(
        answer="completed answer",
        conclusion="completed answer",
        citations=[],
        anchors=[],
    )
    assert asks.save_answer_for_job(
        first_job,
        "single-delete-nb",
        conversation_id,
        first_request.question,
        response,
        "single-delete-owner",
    )
    second_request = AskRequest(
        question="cancelled turn", conversation_id=conversation_id
    )
    second_job, continued_conversation = asks.begin_durable_job(
        "single-delete-nb", second_request, "chunk", "single-delete-owner"
    )
    assert continued_conversation == conversation_id
    assert asks.cancel_running_job(second_job, "single-delete-owner")["cancelled"]

    single_reached_children = threading.Event()
    allow_single_delete = threading.Event()
    original_delete_idle = asks._delete_idle_conversation_on

    def pause_before_child_delete(*args, **kwargs):
        single_reached_children.set()
        assert allow_single_delete.wait(timeout=10)
        return original_delete_idle(*args, **kwargs)

    monkeypatch.setattr(
        asks, "_delete_idle_conversation_on", pause_before_child_delete
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        conversation_future = executor.submit(
            asks.delete_conversation, conversation_id
        )
        assert single_reached_children.wait(timeout=5)
        notebook_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "single-delete-nb"
        )
        try:
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM notebooks", notebook_future
            )
        finally:
            allow_single_delete.set()
        assert conversation_future.result(timeout=5) is None
        assert notebook_future.result(timeout=5) == []


@pytest.mark.postgres_integration
def test_paper_meta_upsert_locks_parents_before_notebook_delete(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('paper-delete-owner','paper-delete@x','Paper Delete','user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('paper-delete-nb','Paper Delete','paper-delete-owner','ready',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,created_at,updated_at) VALUES ('paper-delete-source',"
            "'paper-delete-nb','Fallback Title','pdf','extracted','extracted',"
            "'paper.pdf','paper-delete/path.pdf',%s,%s)",
            (now, now),
        )

    sources = SourceStore(postgres_database, now=lambda: now)
    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    sources.upsert_paper_meta(
        "paper-delete-source",
        "paper-delete-nb",
        {
            "is_paper": True,
            "paper_title": "Original Paper",
            "authors": [{"position": 0, "name": "Original Author"}],
        },
    )
    upsert_reached_meta = threading.Event()
    allow_meta_upsert = threading.Event()
    thread_role = threading.local()
    original_write = postgres_database.write

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, statement, *args, **kwargs):
            if "INSERT INTO source_paper_meta" in statement:
                upsert_reached_meta.set()
                assert allow_meta_upsert.wait(timeout=10)
            return self._connection.execute(statement, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def coordinated_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            if getattr(thread_role, "value", "") == "upsert":
                yield ConnectionProxy(connection)
            else:
                yield connection

    monkeypatch.setattr(postgres_database, "write", coordinated_write)

    def update_paper_meta():
        thread_role.value = "upsert"
        return sources.upsert_paper_meta(
            "paper-delete-source",
            "paper-delete-nb",
            {
                "is_paper": True,
                "paper_title": "Updated Paper",
                "authors": [{"position": 0, "name": "Updated Author"}],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        upsert_future = executor.submit(update_paper_meta)
        assert upsert_reached_meta.wait(timeout=5)
        notebook_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "paper-delete-nb"
        )
        try:
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM notebooks", notebook_future
            )
        finally:
            allow_meta_upsert.set()
        assert upsert_future.result(timeout=5) is None
        assert notebook_future.result(timeout=5) == ["paper-delete/path.pdf"]

    with postgres_database.connect() as connection:
        retained = connection.execute(
            "SELECT display_title FROM retained_user_activity "
            "WHERE activity_type='source' AND record_id='paper-delete-source'"
        ).fetchone()
    assert retained["display_title"] == "Updated Paper"


@pytest.mark.postgres_integration
def test_notebook_delete_waits_for_existing_source_update_before_snapshot(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-31T12:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES ('source-update-owner','source-update@x','Source Update',"
            "'user',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
            "VALUES ('source-update-nb','Source Update','source-update-owner',"
            "'ready',%s,%s)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,created_at,updated_at) VALUES ('source-update-row',"
            "'source-update-nb','Source','pdf','processing','processing','source.pdf',"
            "'source.pdf',%s,%s)",
            (now, now),
        )

    notebooks = NotebookStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-unused",
        now=lambda: now,
        activity_retention_days=180,
    )
    update_uncommitted = threading.Event()
    allow_update_commit = threading.Event()

    def update_source() -> None:
        with postgres_database.write() as connection:
            connection.execute(
                "UPDATE sources SET status='parsed',parse_status='parsed' "
                "WHERE id='source-update-row'"
            )
            update_uncommitted.set()
            assert allow_update_commit.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        update_future = executor.submit(update_source)
        assert update_uncommitted.wait(timeout=5)
        delete_future = executor.submit(
            notebooks.delete_row_and_orphan_embeddings, "source-update-nb"
        )
        try:
            _wait_for_lock_wait(
                postgres_database, "SELECT id FROM sources", delete_future
            )
        finally:
            allow_update_commit.set()
        update_future.result(timeout=5)
        assert delete_future.result(timeout=5) == ["source.pdf"]

    with postgres_database.connect() as connection:
        retained = connection.execute(
            "SELECT status,parse_status FROM retained_user_activity "
            "WHERE activity_type='source' AND record_id='source-update-row'"
        ).fetchone()
    assert retained == {"status": "parsed", "parse_status": "parsed"}


@pytest.mark.postgres_integration
def test_legacy_merge_pair_decisions_lock_all_duplicates_in_one_order(
    postgres_database,
):
    """Different legacy ids for one displayed pair must not deadlock."""
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-20T00:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("merge-race-user", "merge-race@example.test", "Merge race", "admin",
             "active", now, now, "merge_race", "", "", 0),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("nb-merge-race", "Merge race", "", "", "ready", "merge-race-user",
             now, now, "personal"),
        )
        connection.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s,%s,%s)",
            ("merge-race-a", "nb-merge-race", "K-a", "K-b", 0.9, "pending", now, now,
             "merge-race-b", "nb-merge-race", "K-b", "K-a", 0.9, "pending", now, now),
        )

    store = GovernanceStore(
        postgres_database,
        type("Seams", (), {"now": lambda self: now})(),
    )
    barrier = threading.Barrier(2)

    def decide(candidate_id: str, status: str):
        barrier.wait(timeout=5)
        with postgres_database.write() as connection:
            return store.set_merge_decision(
                connection, "nb-merge-race", candidate_id, status, now
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=5) for future in (
            executor.submit(decide, "merge-race-a", "confirmed"),
            executor.submit(decide, "merge-race-b", "rejected"),
        )]

    assert "pending" in results
    with postgres_database.connect() as connection:
        rows = connection.execute(
            "SELECT status FROM concept_merge_candidates "
            "WHERE notebook_id='nb-merge-race' ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert len({row["status"] for row in rows}) == 1
    assert rows[0]["status"] in {"confirmed", "rejected"}


@pytest.mark.postgres_integration
@pytest.mark.parametrize("winner", ["cancel", "save"])
def test_ask_cancel_and_atomic_save_contend_on_the_real_job_row(
    postgres_database, monkeypatch, winner
):
    """Two PG connections serialize cancellation and answer insertion.

    The winner holds the real ask_jobs row before the losing store call gets
    its own pooled connection. This covers both legal terminal outcomes; the
    cancelled outcome must never leave an answer row behind.
    """
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-07-23T00:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("user-race", "race@example.test", "Race", "admin", "active", now, now,
             "r00123456", "", "", 0),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("nb-race", "Race", "", "", "ready", "user-race", now, now, "personal"),
        )

    counter = iter(range(1, 20))
    counter_lock = threading.Lock()

    def new_id(prefix: str) -> str:
        with counter_lock:
            return f"{prefix}-race-{next(counter)}"

    seams = RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    store = AskStateStore(postgres_database, seams)
    job_id, conversation_id = store.begin_durable_job(
        "nb-race", AskRequest(question="race"), "chunk", "user-race"
    )
    response = AskResponse(
        answer="race answer", conclusion="race answer", citations=[], anchors=[]
    )
    thread_role = threading.local()
    winner_locked = threading.Event()
    loser_connected = threading.Event()
    original_write = postgres_database.write

    @contextmanager
    def coordinated_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            role = getattr(thread_role, "value", "")
            if role == winner:
                connection.execute(
                    "SELECT id FROM ask_jobs WHERE id=%s FOR UPDATE", (job_id,)
                ).fetchone()
                winner_locked.set()
                assert loser_connected.wait(timeout=5)
            elif role:
                loser_connected.set()
            yield connection

    monkeypatch.setattr(postgres_database, "write", coordinated_write)

    def cancel():
        thread_role.value = "cancel"
        return store.cancel_running_job(job_id, "user-race")

    def save():
        thread_role.value = "save"
        return store.save_answer_for_job(
            job_id,
            "nb-race",
            conversation_id,
            "race",
            response,
            "user-race",
        )

    calls = {"cancel": cancel, "save": save}
    loser = "save" if winner == "cancel" else "cancel"
    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(calls[winner])
        assert winner_locked.wait(timeout=5)
        loser_future = executor.submit(calls[loser])
        winner_result = winner_future.result(timeout=10)
        loser_result = loser_future.result(timeout=10)

    results = {winner: winner_result, loser: loser_result}
    status = store.ask_job_status(job_id)
    with postgres_database.connect() as connection:
        answers = connection.execute(
            "SELECT id FROM answers WHERE conversation_id=%s", (conversation_id,)
        ).fetchall()
    if winner == "cancel":
        assert results["cancel"]["cancelled"] is True
        assert results["save"] is None
        assert status["status"] == "cancelled"
        assert answers == []
    else:
        assert results["save"]
        assert results["cancel"]["cancelled"] is False
        assert status["status"] == "done"
        assert [row["id"] for row in answers] == [results["save"]]


@pytest.mark.postgres_integration
def test_conversation_cleanup_cannot_split_continuation_job_creation(
    postgres_database, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-07-23T00:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("user-conv-race", "conv@example.test", "Conv", "admin", "active",
             now, now, "q00123456", "", "", 0),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("nb-conv-race", "Conv", "", "", "ready", "user-conv-race",
             now, now, "personal"),
        )
    seams = RepositoryCompatibilitySeams(
        new_id=(
            lambda _prefix, counter=iter(range(1, 20)): f"conv-race-{next(counter)}"
        ),
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    store = AskStateStore(postgres_database, seams)
    first_job, conversation_id = store.begin_durable_job(
        "nb-conv-race", AskRequest(question="first"), "chunk", "user-conv-race"
    )
    store.cancel_running_job(first_job, "user-conv-race")

    selected = threading.Event()
    cleanup_started = threading.Event()
    cleanup_finished = threading.Event()
    thread_role = threading.local()
    original_write = postgres_database.write

    class CursorProxy:
        def __init__(self, cursor):
            self._cursor = cursor

        def fetchone(self):
            row = self._cursor.fetchone()
            selected.set()
            assert cleanup_started.wait(timeout=5)
            cleanup_finished.wait(timeout=0.25)
            return row

        def __getattr__(self, name):
            return getattr(self._cursor, name)

    class ConnectionProxy:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, statement, params=None):
            cursor = self._connection.execute(statement, params)
            if (
                "SELECT id FROM conversations" in statement
                and getattr(thread_role, "value", "") == "begin"
            ):
                return CursorProxy(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

    @contextmanager
    def coordinated_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            if getattr(thread_role, "value", "") == "begin":
                yield ConnectionProxy(connection)
            else:
                yield connection

    monkeypatch.setattr(postgres_database, "write", coordinated_write)

    def begin_continuation():
        thread_role.value = "begin"
        return store.begin_durable_job(
            "nb-conv-race",
            AskRequest(question="second", conversation_id=conversation_id),
            "chunk",
            "user-conv-race",
        )

    def cleanup():
        cleanup_started.set()
        store.cleanup_empty_conversation(conversation_id)
        cleanup_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        begin_future = executor.submit(begin_continuation)
        assert selected.wait(timeout=5)
        cleanup_future = executor.submit(cleanup)
        second_job, continued_id = begin_future.result(timeout=10)
        cleanup_future.result(timeout=10)

    assert continued_id == conversation_id
    assert store.ask_job_status(second_job)["status"] == "running"
    assert store.get_conversation(conversation_id).id == conversation_id


def _seed_memory_race(postgres_database, *, member: bool = False) -> None:
    now = "2026-07-23T00:00:00+00:00"
    with postgres_database.write() as connection:
        for user_id, username in (("owner-race", "o00123456"), ("member-race", "m00123456")):
            connection.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user_id, f"{user_id}@example.test", user_id, now, now, username),
            )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,created_at,updated_at,tier) "
            "VALUES ('nb-memory-race','Race','','','ready','owner-race',%s,%s,'personal')",
            (now, now),
        )
        if member:
            connection.execute(
                "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
                "VALUES ('nb-memory-race','member-race','reader',%s)",
                (now,),
            )
        connection.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at,conversation_id) "
            "VALUES ('answer-memory-race','nb-memory-race','Q',%s,%s,NULL)",
            (Jsonb({"answer": "A", "citations": []}), now),
        )


def _memory_store(postgres_database) -> MemoryStore:
    lock = threading.Lock()
    counter = iter(range(1, 100))

    def new_id(prefix: str) -> str:
        with lock:
            return f"{prefix}-memory-race-{next(counter)}"

    return MemoryStore(
        postgres_database,
        new_id=new_id,
        now=lambda: "2026-07-23T00:00:00+00:00",
    )


def _confirmed_race_memory(store: MemoryStore, memory_id: str) -> MemoryWrite:
    write = MemoryWrite(
        id=memory_id,
        notebook_id="nb-memory-race",
        created_by="owner-race",
        origin="external_agent",
        status="confirmed",
        title="Race memory",
        content_md="revision one",
        tags=[],
        created_at="2026-07-23T00:00:00+00:00",
        updated_at="2026-07-23T00:00:00+00:00",
        confirmed_by="owner-race",
        confirmed_at="2026-07-23T00:00:00+00:00",
    )
    store.create_candidate_with_initial_revision(write, "owner-race", "created")
    return write


def _wait_for_memory_row_lock(postgres_database) -> None:
    import psycopg
    from psycopg.rows import dict_row

    deadline = time.monotonic() + 3
    with psycopg.connect(
        postgres_database._database_url, autocommit=True, row_factory=dict_row
    ) as inspector:
        while time.monotonic() < deadline:
            waiting = inspector.execute(
                "SELECT 1 FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                "AND wait_event_type='Lock' AND state='active' "
                "AND query ILIKE '%memory_items%' LIMIT 1"
            ).fetchone()
            if waiting is not None:
                return
            time.sleep(0.01)
    raise AssertionError("Memory operation never waited on the aggregate row lock")


@pytest.mark.postgres_integration
def test_stale_conditional_delete_rechecks_revision_after_row_lock(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    write = _confirmed_race_memory(store, "memory-stale-delete")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_database.write() as editor:
            editor.execute(
                "SELECT id FROM memory_items WHERE id=%s FOR UPDATE", (write.id,)
            ).fetchone()
            delete_future = executor.submit(
                store.delete_memory_if_unchanged, write.id, "owner-race", 1
            )
            _wait_for_memory_row_lock(postgres_database)
            editor.execute(
                "UPDATE memory_items SET content_md=%s,embedding_status='pending' "
                "WHERE id=%s",
                ("revision two", write.id),
            )
            store._append_revision_on(
                editor,
                write.id,
                {
                    "title": write.title,
                    "content_md": "revision two",
                    "tags": [],
                    "status": "confirmed",
                    "promotion_state": "none",
                },
                "owner-race",
                "edited",
            )
        assert delete_future.result(timeout=5) is False

    current = store.memory_for_user(write.id, "owner-race")
    assert current.content_md == "revision two"
    assert [r.revision for r in store.revisions_for_user(write.id, "owner-race")] == [1, 2]


@pytest.mark.postgres_integration
def test_stale_embedding_failure_rechecks_revision_after_row_lock(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    write = _confirmed_race_memory(store, "memory-stale-embedding-failure")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with postgres_database.write() as editor:
            editor.execute(
                "SELECT id FROM memory_items WHERE id=%s FOR UPDATE", (write.id,)
            ).fetchone()
            failure_future = executor.submit(
                store.mark_embedding_failed, write.id, 1, "stale provider error"
            )
            _wait_for_memory_row_lock(postgres_database)
            editor.execute(
                "UPDATE memory_items SET content_md=%s,embedding_status='pending',"
                "embedding_error='' WHERE id=%s",
                ("revision two", write.id),
            )
            store._append_revision_on(
                editor,
                write.id,
                {
                    "title": write.title,
                    "content_md": "revision two",
                    "tags": [],
                    "status": "confirmed",
                    "promotion_state": "none",
                },
                "owner-race",
                "edited",
            )
        assert failure_future.result(timeout=5) is False

    current = store.memory_for_user(write.id, "owner-race")
    assert current.embedding_status == "pending"
    assert current.embedding_error == ""


@pytest.mark.postgres_integration
def test_revoked_member_cannot_commit_save_answer_memory(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database, member=True)
    store = _memory_store(postgres_database)
    revoked_uncommitted = threading.Event()
    saver_started = threading.Event()
    allow_revoke_commit = threading.Event()

    def revoke() -> None:
        with postgres_database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id='nb-memory-race' "
                "AND user_id='member-race'"
            )
            revoked_uncommitted.set()
            assert saver_started.wait(timeout=5)
            assert allow_revoke_commit.wait(timeout=5)

    def save() -> str:
        assert revoked_uncommitted.wait(timeout=5)
        saver_started.set()
        allow_revoke_commit.set()
        write = MemoryWrite(
            id="memory-race-save",
            notebook_id="nb-memory-race",
            created_by="member-race",
            origin="ask_answer",
            status="confirmed",
            title="Saved",
            content_md="Answer",
            tags=[],
            created_at="2026-07-23T00:00:00+00:00",
            updated_at="2026-07-23T00:00:00+00:00",
            source_answer_id="answer-memory-race",
            confirmed_by="member-race",
            confirmed_at="2026-07-23T00:00:00+00:00",
        )
        with pytest.raises(KeyError):
            store.create_answer_with_initial_revision(write, "member-race", "saved")
        return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        revoke_future = executor.submit(revoke)
        save_future = executor.submit(save)
        revoke_future.result()
        assert save_future.result() == "rejected"
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM memory_items WHERE id='memory-race-save'"
        ).fetchone() is None


@pytest.mark.postgres_integration
def test_save_answer_holds_access_lock_until_atomic_commit(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database, member=True)
    store = _memory_store(postgres_database)
    save_scope_locked = threading.Event()
    revoke_started = threading.Event()
    original_scope_hook = store._answer_save_scope_locked_on

    def pause_after_scope_locks(connection, write, row):
        locked_row = original_scope_hook(connection, write, row)
        save_scope_locked.set()
        assert revoke_started.wait(timeout=5)
        return locked_row

    store._answer_save_scope_locked_on = pause_after_scope_locks
    write = MemoryWrite(
        id="memory-race-save-first",
        notebook_id="nb-memory-race",
        created_by="member-race",
        origin="ask_answer",
        status="confirmed",
        title="Saved",
        content_md="Answer",
        tags=[],
        created_at="2026-07-23T00:00:00+00:00",
        updated_at="2026-07-23T00:00:00+00:00",
        source_answer_id="answer-memory-race",
        confirmed_by="member-race",
        confirmed_at="2026-07-23T00:00:00+00:00",
    )

    def revoke():
        assert save_scope_locked.wait(timeout=5)
        revoke_started.set()
        with postgres_database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id='nb-memory-race' "
                "AND user_id='member-race'"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        save_future = executor.submit(
            store.create_answer_with_initial_revision,
            write,
            "member-race",
            "saved",
        )
        revoke_future = executor.submit(revoke)
        assert save_future.result(timeout=10).id == write.id
        revoke_future.result(timeout=10)
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM memory_items WHERE id=%s", (write.id,)
        ).fetchone() is not None


@pytest.mark.postgres_integration
def test_competing_memory_promotion_approval_is_idempotent(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    write = MemoryWrite(
        id="memory-promotion-race",
        notebook_id="nb-memory-race",
        created_by="owner-race",
        origin="external_agent",
        status="confirmed",
        title="Promote",
        content_md="Promote once.",
        tags=[],
        created_at="2026-07-23T00:00:00+00:00",
        updated_at="2026-07-23T00:00:00+00:00",
        confirmed_by="owner-race",
        confirmed_at="2026-07-23T00:00:00+00:00",
    )
    item = store.create_candidate_with_initial_revision(write, "owner-race", "created")
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id) VALUES "
            "('promo-race','nb-memory-race',%s,'memory','proposed',%s,%s,'')",
            (item.id, write.created_at, write.updated_at),
        )
        store.propose_promotion_on(
            connection, item.id, "owner-race", "promo-race", [], [], item, write.created_at
        )

    governance = GovernanceStore(postgres_database, type("Seams", (), {"now": lambda self: write.created_at})())
    barrier = threading.Barrier(2)

    def approve(reviewer: str) -> bool:
        barrier.wait()
        with postgres_database.write() as connection:
            candidate = governance.promotion_candidate_row(connection, "promo-race")
            if candidate["status"] == "approved":
                return False
            connection.execute(
                "UPDATE promotion_candidates SET status='approved',reviewed_by=%s,updated_at=%s "
                "WHERE id='promo-race' AND status='proposed'",
                (reviewer, write.updated_at),
            )
            store.record_promotion_decision_on(
                connection,
                item.id,
                "approved",
                reviewer,
                write.updated_at,
                base_object_ids=("base-object",),
            )
            return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, ("owner-race", "member-race")))
    assert sorted(results) == [False, True]
    final = store.memory_for_user(item.id, "owner-race")
    assert final.promotion_state == "approved"
    assert final.provenance["kg_promotion"]["base_object_ids"] == ["base-object"]
    assert len(store.revisions_for_user(item.id, "owner-race")) == 3


def _knowhow_race_store(postgres_database):
    lock = threading.Lock()
    counter = iter(range(1, 200))

    def new_id(prefix: str) -> str:
        with lock:
            return f"{prefix}-knowhow-race-{next(counter)}"

    store = KnowhowStore(
        postgres_database,
        new_id=new_id,
        now=lambda: "2026-07-23T00:00:00+00:00",
    )
    table_id = store.create_knowhow_table(
        "nb-memory-race",
        "Race table",
        "",
        [
            {"name": "Topic", "role": "anchor"},
            {"name": "Procedure", "role": "procedure"},
        ],
        "owner-race",
    )
    table = store.get_knowhow_table(table_id)
    anchor_id = table["columns"][0]["id"]
    procedure_id = table["columns"][1]["id"]
    row_ids = [
        store.add_knowhow_row(table_id, {anchor_id: "A", procedure_id: "old"})
        for _ in range(2)
    ]
    return store, table_id, anchor_id, procedure_id, row_ids


def _insert_gc_asset(store, tmp_path, suffix: str):
    asset_id = store.insert_notebook_asset(
        "nb-memory-race", f"{suffix}.png", "image/png", 3, "owner-race"
    )
    asset_dir = tmp_path / "assets" / "nb-memory-race"
    asset_dir.mkdir(parents=True, exist_ok=True)
    asset_path = asset_dir / f"{asset_id}.png"
    asset_path.write_bytes(b"png")
    return asset_id, asset_path


@pytest.mark.postgres_integration
def test_asset_writer_first_blocks_gc_then_gc_rechecks_and_retains_reference(
    postgres_database, tmp_path, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, _table_id, _anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    asset_id, asset_path = _insert_gc_asset(store, tmp_path, "writer-first")
    maintenance = PostgresMaintenanceAdapter(
        SimpleNamespace(database=postgres_database, storage_dir=tmp_path)
    )
    writer_locked = threading.Event()
    allow_writer_commit = threading.Event()
    gc_lock_attempted = threading.Event()
    gc_lock_acquired = threading.Event()
    original_require = store._lock_required_assets
    original_gc_lock = maintenance._lock_candidate_assets

    def hold_writer_lock(db, notebook_id, asset_ids):
        result = original_require(db, notebook_id, asset_ids)
        writer_locked.set()
        assert allow_writer_commit.wait(timeout=5)
        return result

    def observe_gc_lock(db, notebook_id, asset_ids):
        gc_lock_attempted.set()
        result = original_gc_lock(db, notebook_id, asset_ids)
        gc_lock_acquired.set()
        return result

    monkeypatch.setattr(store, "_lock_required_assets", hold_writer_lock)
    monkeypatch.setattr(maintenance, "_lock_candidate_assets", observe_gc_lock)
    content = f"![kept](asset://{asset_id})"
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.update_knowhow_cell,
            row_ids[0],
            procedure_id,
            content,
            [asset_id],
        )
        assert writer_locked.wait(timeout=5)
        sweep = executor.submit(
            maintenance.sweep_orphan_assets, "nb-memory-race"
        )
        assert gc_lock_attempted.wait(timeout=5)
        assert not gc_lock_acquired.wait(timeout=0.1)
        allow_writer_commit.set()
        writer.result(timeout=10)
        assert sweep.result(timeout=10) == {"removed": 0}

    assert store.get_notebook_asset(asset_id) is not None
    assert asset_path.is_file()
    assert store.get_knowhow_table(_table_id)["rows"][0]["cells"][procedure_id] == content


@pytest.mark.postgres_integration
def test_atomic_append_holds_asset_lock_until_every_row_and_sequence_commit(
    postgres_database, tmp_path, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, anchor_id, procedure_id, _row_ids = _knowhow_race_store(
        postgres_database
    )
    before_seq = store.get_knowhow_table(table_id)["mutation_seq"]
    asset_id, asset_path = _insert_gc_asset(store, tmp_path, "append-writer-first")
    maintenance = PostgresMaintenanceAdapter(
        SimpleNamespace(database=postgres_database, storage_dir=tmp_path)
    )
    writer_locked = threading.Event()
    allow_writer_commit = threading.Event()
    gc_lock_attempted = threading.Event()
    gc_lock_acquired = threading.Event()
    original_require = store._lock_required_assets
    original_gc_lock = maintenance._lock_candidate_assets

    def hold_writer_lock(db, notebook_id, asset_ids):
        result = original_require(db, notebook_id, asset_ids)
        writer_locked.set()
        assert allow_writer_commit.wait(timeout=5)
        return result

    def observe_gc_lock(db, notebook_id, asset_ids):
        gc_lock_attempted.set()
        result = original_gc_lock(db, notebook_id, asset_ids)
        gc_lock_acquired.set()
        return result

    monkeypatch.setattr(store, "_lock_required_assets", hold_writer_lock)
    monkeypatch.setattr(maintenance, "_lock_candidate_assets", observe_gc_lock)
    content = f"![kept](asset://{asset_id})"
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(
            store.append_knowhow_rows,
            table_id,
            [
                {anchor_id: "B", procedure_id: content},
                {anchor_id: "C", procedure_id: content},
            ],
        )
        assert writer_locked.wait(timeout=5)
        sweep = executor.submit(
            maintenance.sweep_orphan_assets, "nb-memory-race"
        )
        assert gc_lock_attempted.wait(timeout=5)
        assert not gc_lock_acquired.wait(timeout=0.1)
        allow_writer_commit.set()
        appended_ids = writer.result(timeout=10)
        assert sweep.result(timeout=10) == {"removed": 0}

    final = store.get_knowhow_table(table_id)
    assert appended_ids == [row["id"] for row in final["rows"][-2:]]
    assert [row["cells"][procedure_id] for row in final["rows"][-2:]] == [
        content,
        content,
    ]
    assert final["mutation_seq"] == before_seq + 1
    assert store.get_notebook_asset(asset_id) is not None
    assert asset_path.is_file()


@pytest.mark.postgres_integration
def test_asset_gc_first_blocks_writer_then_writer_rolls_back_missing_reference(
    postgres_database, tmp_path, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, _anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    asset_id, asset_path = _insert_gc_asset(store, tmp_path, "gc-first")
    maintenance = PostgresMaintenanceAdapter(
        SimpleNamespace(database=postgres_database, storage_dir=tmp_path)
    )
    gc_locked = threading.Event()
    allow_gc_finish = threading.Event()
    writer_lock_attempted = threading.Event()
    writer_lock_acquired = threading.Event()
    original_require = store._lock_required_assets
    original_gc_lock = maintenance._lock_candidate_assets

    def hold_gc_lock(db, notebook_id, asset_ids):
        result = original_gc_lock(db, notebook_id, asset_ids)
        gc_locked.set()
        assert allow_gc_finish.wait(timeout=5)
        return result

    def observe_writer_lock(db, notebook_id, asset_ids):
        writer_lock_attempted.set()
        result = original_require(db, notebook_id, asset_ids)
        writer_lock_acquired.set()
        return result

    monkeypatch.setattr(maintenance, "_lock_candidate_assets", hold_gc_lock)
    monkeypatch.setattr(store, "_lock_required_assets", observe_writer_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        sweep = executor.submit(
            maintenance.sweep_orphan_assets, "nb-memory-race"
        )
        assert gc_locked.wait(timeout=5)
        writer = executor.submit(
            store.update_knowhow_cell,
            row_ids[0],
            procedure_id,
            f"![gone](asset://{asset_id})",
            [asset_id],
        )
        assert writer_lock_attempted.wait(timeout=5)
        assert not writer_lock_acquired.wait(timeout=0.1)
        allow_gc_finish.set()
        assert sweep.result(timeout=10) == {"removed": 1}
        with pytest.raises(ValueError, match=asset_id):
            writer.result(timeout=10)

    assert store.get_notebook_asset(asset_id) is None
    assert not asset_path.exists()
    assert store.get_knowhow_table(table_id)["rows"][0]["cells"][procedure_id] == "old"


@pytest.mark.postgres_integration
def test_multi_asset_writers_canonicalize_opposite_orders_without_deadlock_and_validate_all(
    postgres_database, tmp_path, monkeypatch
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_a, _anchor_a, procedure_a, rows_a = _knowhow_race_store(
        postgres_database
    )
    table_b = store.create_knowhow_table(
        "nb-memory-race",
        "Other table",
        "",
        [{"name": "Procedure", "role": "procedure"}],
        "owner-race",
    )
    procedure_b = store.get_knowhow_table(table_b)["columns"][0]["id"]
    row_b = store.add_knowhow_row(table_b, {procedure_b: "old"})
    asset_a, _path_a = _insert_gc_asset(store, tmp_path, "multi-a")
    asset_b, _path_b = _insert_gc_asset(store, tmp_path, "multi-b")
    original_lock = store._lock_required_assets
    barriers = [threading.Barrier(2)]
    observed_orders: list[tuple[str, ...]] = []
    observed_lock = threading.Lock()

    def synchronized_lock(db, notebook_id, asset_ids):
        ordered = tuple(sorted(set(asset_ids)))
        with observed_lock:
            observed_orders.append(ordered)
        barriers[0].wait(timeout=5)
        return original_lock(db, notebook_id, asset_ids)

    monkeypatch.setattr(store, "_lock_required_assets", synchronized_lock)

    def save(row_id, column_id, requested):
        content = f"![a](asset://{asset_a}) ![b](asset://{asset_b})"
        store.update_knowhow_cell(row_id, column_id, content, requested)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            save, rows_a[0], procedure_a, [asset_b, asset_a, asset_b]
        )
        second = executor.submit(
            save, row_b, procedure_b, [asset_a, asset_b]
        )
        first.result(timeout=10)
        second.result(timeout=10)
    assert observed_orders == [tuple(sorted((asset_a, asset_b)))] * 2

    with postgres_database.write() as connection:
        connection.execute(
            "UPDATE knowhow_cells SET content_md='old' "
            "WHERE (row_id=%s AND column_id=%s) OR (row_id=%s AND column_id=%s)",
            (rows_a[0], procedure_a, row_b, procedure_b),
        )
        connection.execute("DELETE FROM notebook_assets WHERE id=%s", (asset_b,))

    barriers[0] = threading.Barrier(2)
    observed_orders.clear()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            save, rows_a[0], procedure_a, [asset_b, asset_a]
        )
        second = executor.submit(
            save, row_b, procedure_b, [asset_a, asset_b]
        )
        for future in (first, second):
            with pytest.raises(ValueError, match=asset_b):
                future.result(timeout=10)
    assert observed_orders == [tuple(sorted((asset_a, asset_b)))] * 2
    assert store.get_knowhow_table(table_a)["rows"][0]["cells"][procedure_a] == "old"
    assert store.get_knowhow_table(table_b)["rows"][0]["cells"][procedure_b] == "old"


@pytest.mark.postgres_integration
def test_every_postgres_cell_insert_path_rejects_a_missing_rendered_asset(
    postgres_database,
):
    """Add/import, merged, CLI-guarded, and interactive-guarded share one guard."""
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, _anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    missing = "asset-missing-race"
    content = f"![missing](asset://{missing})"

    with pytest.raises(ValueError, match=missing):
        # Fresh import and append both converge on add_knowhow_row.
        store.add_knowhow_row(table_id, {procedure_id: content})
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cells(row_ids, procedure_id, content)
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cells_bulk_guarded(
            "nb-memory-race",
            [(table_id, row_ids[0], procedure_id, "old", content)],
        )
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cells_guarded_atomic(
            "nb-memory-race",
            [(table_id, row_ids[0], procedure_id, "old", content)],
        )

    final = store.get_knowhow_table(table_id)
    assert len(final["rows"]) == 2
    assert [row["cells"][procedure_id] for row in final["rows"]] == ["old", "old"]


@pytest.mark.postgres_integration
def test_asset_file_unlink_failure_cannot_leave_a_validatable_broken_row(
    postgres_database, tmp_path, monkeypatch
):
    """A filesystem failure may leak a file, never a live row without a file."""
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, _anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    asset_id, asset_path = _insert_gc_asset(store, tmp_path, "unlink-failure")
    maintenance = PostgresMaintenanceAdapter(
        SimpleNamespace(database=postgres_database, storage_dir=tmp_path)
    )
    path_type = type(asset_path)
    original_unlink = path_type.unlink

    def fail_target_unlink(path, *args, **kwargs):
        if path == asset_path:
            raise OSError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "unlink", fail_target_unlink)
    assert maintenance.sweep_orphan_assets("nb-memory-race") == {"removed": 1}
    assert store.get_notebook_asset(asset_id) is None
    assert asset_path.is_file()  # safe unreachable leak, not a broken live row
    with pytest.raises(ValueError, match=asset_id):
        store.update_knowhow_cell(
            row_ids[0],
            procedure_id,
            f"![missing](asset://{asset_id})",
        )
    assert store.get_knowhow_table(table_id)["rows"][0]["cells"][procedure_id] == "old"


@pytest.mark.postgres_integration
def test_stale_projection_pass_cannot_overwrite_newer_pending_edit(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, _anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    stale_seq = store.get_knowhow_table(table_id)["mutation_seq"]
    edit_uncommitted = threading.Event()
    projection_started = threading.Event()
    allow_edit_commit = threading.Event()

    def edit() -> None:
        with postgres_database.write() as connection:
            connection.execute(
                "SELECT id FROM knowhow_tables WHERE id=%s FOR UPDATE", (table_id,)
            )
            connection.execute(
                "UPDATE knowhow_cells SET content_md='new',updated_at=%s "
                "WHERE row_id=%s AND column_id=%s",
                ("2026-07-23T00:00:01+00:00", row_ids[0], procedure_id),
            )
            connection.execute(
                "UPDATE knowhow_rows SET projection_status='pending' WHERE id=%s",
                (row_ids[0],),
            )
            connection.execute(
                "UPDATE knowhow_tables SET mutation_seq=mutation_seq+1 WHERE id=%s",
                (table_id,),
            )
            edit_uncommitted.set()
            assert projection_started.wait(timeout=5)
            assert allow_edit_commit.wait(timeout=5)

    def publish() -> bool:
        assert edit_uncommitted.wait(timeout=5)
        projection_started.set()
        allow_edit_commit.set()
        return store.set_knowhow_row_projection_if_table_seq(
            row_ids[0], "synced", stale_seq
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        edit_future = executor.submit(edit)
        publish_future = executor.submit(publish)
        edit_future.result()
        assert publish_future.result() is False
    final = store.get_knowhow_table(table_id)
    assert final["rows"][0]["projection_status"] == "pending"
    assert final["rows"][0]["cells"][procedure_id] == "new"


@pytest.mark.postgres_integration
def test_batch_reformat_membership_drift_is_zero_write_conflict(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store, table_id, anchor_id, procedure_id, row_ids = _knowhow_race_store(
        postgres_database
    )
    join_uncommitted = threading.Event()
    save_started = threading.Event()
    allow_join_commit = threading.Event()

    def join_group() -> None:
        with postgres_database.write() as connection:
            connection.execute(
                "SELECT id FROM knowhow_tables WHERE id=%s FOR UPDATE", (table_id,)
            )
            connection.execute(
                "INSERT INTO knowhow_rows(id,table_id,position,created_at,updated_at) "
                "VALUES ('khrow-joiner',%s,2,%s,%s)",
                (table_id, "2026-07-23T00:00:01+00:00", "2026-07-23T00:00:01+00:00"),
            )
            connection.execute(
                "INSERT INTO knowhow_cells(id,row_id,column_id,content_md,updated_at) VALUES "
                "('khcel-join-anchor','khrow-joiner',%s,'A',%s),"
                "('khcel-join-proc','khrow-joiner',%s,'old',%s)",
                (
                    anchor_id,
                    "2026-07-23T00:00:01+00:00",
                    procedure_id,
                    "2026-07-23T00:00:01+00:00",
                ),
            )
            join_uncommitted.set()
            assert save_started.wait(timeout=5)
            assert allow_join_commit.wait(timeout=5)

    def guarded_save():
        assert join_uncommitted.wait(timeout=5)
        save_started.set()
        allow_join_commit.set()
        return store.update_knowhow_cells_guarded_atomic(
            "nb-memory-race",
            [
                (table_id, row_id, procedure_id, "old", "formatted")
                for row_id in row_ids
            ],
            anchor_column_id=anchor_id,
            expected_anchor=["A", "A"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        join_future = executor.submit(join_group)
        save_future = executor.submit(guarded_save)
        join_future.result()
        result = save_future.result()
    assert result == {"written": [], "conflict": True}
    final = store.get_knowhow_table(table_id)
    assert [row["cells"][procedure_id] for row in final["rows"]] == ["old", "old", "old"]


def _confirmed_memory_write(memory_id: str, content: str = "Before") -> MemoryWrite:
    now = "2026-07-23T00:00:00+00:00"
    return MemoryWrite(
        id=memory_id,
        notebook_id="nb-memory-race",
        created_by="owner-race",
        origin="external_agent",
        status="confirmed",
        title="Memory",
        content_md=content,
        tags=[],
        created_at=now,
        updated_at=now,
        confirmed_by="owner-race",
        confirmed_at=now,
    )


@pytest.mark.postgres_integration
def test_memory_edit_and_promotion_decision_share_one_lock_order(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    write = _confirmed_memory_write("memory-lock-order")
    item = store.create_candidate_with_initial_revision(write, "owner-race", "created")
    governance = GovernanceStore(
        postgres_database,
        type("Seams", (), {"now": lambda self: write.created_at})(),
    )
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id) VALUES "
            "('promo-lock-order','nb-memory-race',%s,'memory','proposed',%s,%s,'')",
            (item.id, write.created_at, write.updated_at),
        )
        store.propose_promotion_on(
            connection,
            item.id,
            "owner-race",
            "promo-lock-order",
            [{"object_type": "claim", "payload": {"name": "Before"}}],
            [],
            item,
            write.created_at,
        )

    edit_holds_memory = threading.Event()
    decision_started = threading.Event()
    editing = threading.local()
    original_candidate_lock = store._lock_active_promotion_candidate_on

    def pause_before_edit_candidate_lock(connection, proposal_id, memory_id):
        if getattr(editing, "active", False):
            edit_holds_memory.set()
            assert decision_started.wait(timeout=5)
        return original_candidate_lock(connection, proposal_id, memory_id)

    store._lock_active_promotion_candidate_on = pause_before_edit_candidate_lock

    def edit():
        editing.active = True
        try:
            return store.update_with_revision(
                item.id,
                "owner-race",
                {"content_md": "After"},
                expected={"confirmed"},
                changed_by="owner-race",
                reason="edited",
            )
        finally:
            editing.active = False

    def decide():
        assert edit_holds_memory.wait(timeout=5)
        decision_started.set()
        with postgres_database.write() as connection:
            identity = governance.promotion_candidate_identity(
                connection, "promo-lock-order"
            )
            locked = store.lock_promotion_memory_on(
                connection, identity["object_id"], identity["notebook_id"]
            )
            candidate = governance.promotion_candidate_row(
                connection, "promo-lock-order"
            )
            return locked.promotion_state, candidate["status"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        edit_future = executor.submit(edit)
        decision_future = executor.submit(decide)
        assert edit_future.result(timeout=10).content_md == "After"
        assert decision_future.result(timeout=10) == ("none", "rejected")


@pytest.mark.postgres_integration
def test_memory_embedding_replace_and_edit_preserve_revision_freshness(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    item = store.create_candidate_with_initial_revision(
        _confirmed_memory_write("memory-embedding-race", "Old text"),
        "owner-race",
        "created",
    )
    revision = store.embedding_revision(item.id, item)
    embedding_holds_memory = threading.Event()
    edit_started = threading.Event()
    original_lock = store._lock_memory_revision_on

    def pause_after_embedding_lock(connection, memory_id):
        locked = original_lock(connection, memory_id)
        embedding_holds_memory.set()
        assert edit_started.wait(timeout=5)
        return locked

    store._lock_memory_revision_on = pause_after_embedding_lock

    def edit():
        assert embedding_holds_memory.wait(timeout=5)
        edit_started.set()
        return store.update_with_revision(
            item.id,
            "owner-race",
            {"content_md": "New text"},
            expected={"confirmed"},
            changed_by="owner-race",
            reason="edited",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        embed_future = executor.submit(
            store.replace_embedding, item.id, revision, "test", [1.0, 0.0]
        )
        edit_future = executor.submit(edit)
        assert embed_future.result(timeout=10) is True
        edited = edit_future.result(timeout=10)
    assert edited.embedding_status == "pending"
    assert store.embedding_revision(item.id, edited) == revision + 1


@pytest.mark.postgres_integration
def test_memory_copy_holds_source_through_vector_snapshot(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = _memory_store(postgres_database)
    source_write = _confirmed_memory_write("memory-copy-source-race", "Old text")
    source = store.create_candidate_with_initial_revision(
        source_write, "owner-race", "created"
    )
    revision = store.embedding_revision(source.id, source)
    assert store.replace_embedding(source.id, revision, "test", [1.0, 0.0])
    copy_write = replace(
        source_write,
        id="memory-copy-target-race",
        source_answer_id=None,
    )
    copy_holds_source = threading.Event()
    edit_started = threading.Event()
    original_copy_lock = store._lock_copy_source_on

    def pause_copy_source(connection, memory_id):
        locked = original_copy_lock(connection, memory_id)
        copy_holds_source.set()
        assert edit_started.wait(timeout=5)
        return locked

    store._lock_copy_source_on = pause_copy_source

    def edit_and_reembed():
        assert copy_holds_source.wait(timeout=5)
        edit_started.set()
        edited = store.update_with_revision(
            source.id,
            "owner-race",
            {"content_md": "New text"},
            expected={"confirmed"},
            changed_by="owner-race",
            reason="edited",
        )
        new_revision = store.embedding_revision(source.id, edited)
        assert store.replace_embedding(source.id, new_revision, "test", [0.0, 1.0])

    with ThreadPoolExecutor(max_workers=2) as executor:
        copy_future = executor.submit(
            store.create_copy_with_initial_revision,
            copy_write,
            source.id,
            "owner-race",
            "copied",
            revision,
        )
        edit_future = executor.submit(edit_and_reembed)
        copied = copy_future.result(timeout=10)
        edit_future.result(timeout=10)
    assert copied.content_md == "Old text"
    assert copied.embedding_status == "ready"
    with postgres_database.connect() as connection:
        vectors = {
            row["memory_id"]: bytes(row["vector"])
            for row in connection.execute(
                "SELECT memory_id,vector FROM memory_embeddings WHERE memory_id=ANY(%s)",
                ([source.id, copied.id],),
            ).fetchall()
        }
    assert vectors[source.id] != vectors[copied.id]


@pytest.mark.postgres_integration
def test_revoked_member_cannot_complete_full_memory_approval(postgres_database):
    from app.core.config import Settings
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.services.knowledge_governance import KnowledgeGovernanceService
    from app.services.review_queue_memo import ReviewQueueMemo
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database, member=True)
    now = "2026-07-23T00:00:00+00:00"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES "
            "('nb-base-race','Base','','','ready','owner-race',%s,%s,'base')",
            (now, now),
        )
    store = _memory_store(postgres_database)
    write = _confirmed_memory_write("memory-member-approval")
    write = replace(write, created_by="member-race")
    item = store.create_candidate_with_initial_revision(
        write, "member-race", "created"
    )
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id) VALUES "
            "('promo-member-approval','nb-memory-race',%s,'memory','proposed',%s,%s,"
            "'nb-base-race')",
            (item.id, now, now),
        )
        store.propose_promotion_on(
            connection,
            item.id,
            "member-race",
            "promo-member-approval",
            [{"object_type": "claim", "payload": {"name": "Private claim"}}],
            [],
            item,
            now,
        )

    counter = iter(range(1000, 1100))
    seams = RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-approval-{next(counter)}",
        now=lambda: now,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )
    governance_store = GovernanceStore(postgres_database, seams)
    service = KnowledgeGovernanceService(
        settings=Settings(database_url="postgresql://unused/unused"),
        event_log=None,
        governance_store=governance_store,
        knowledge=KnowledgeStore(postgres_database, seams),
        new_id=seams.new_id,
        now=seams.now,
        connect=postgres_database.connect,
        write=postgres_database.write,
        get_notebook=lambda _notebook_id: None,
        invalidate_unified_cache=lambda _notebook_id: None,
        mark_unified_kg_dirty=lambda _notebook_id: None,
        model_clients=None,
        edge_centrality_map=lambda _notebook_id: {},
        embed_knowledge=lambda *_args: None,
        knowledge_objects=lambda *_args, **_kwargs: [],
        as_retrieved=lambda row, _tier: row,
        rule_card=lambda row: row,
        set_conflict_status=lambda *_args: None,
        memory_store=store,
        kg_mutation_seq=lambda _notebook_id: 0,
        # Not exercised by this test (it drives promotion approval, not
        # set_edge_review) — a trivial stand-in like the other collaborators
        # above, added because the R2 P2 fix (codex #638 R2) widened
        # KnowledgeGovernanceService's constructor with this new seat.
        mark_unified_kg_dirty_in_tx=lambda _connection, _notebook_id: 0,
        review_queue_memo=ReviewQueueMemo(),
    )
    revoked_uncommitted = threading.Event()
    approval_started = threading.Event()
    allow_revoke_commit = threading.Event()

    def revoke():
        with postgres_database.write() as connection:
            connection.execute(
                "DELETE FROM notebook_members WHERE notebook_id='nb-memory-race' "
                "AND user_id='member-race'"
            )
            revoked_uncommitted.set()
            assert approval_started.wait(timeout=5)
            assert allow_revoke_commit.wait(timeout=5)

    def approve():
        assert revoked_uncommitted.wait(timeout=5)
        approval_started.set()
        allow_revoke_commit.set()
        with pytest.raises(PermissionError):
            service.approve_promotion(
                "promo-member-approval", reviewer_id="owner-race"
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        revoke_future = executor.submit(revoke)
        approve_future = executor.submit(approve)
        revoke_future.result(timeout=10)
        approve_future.result(timeout=10)

    with postgres_database.connect() as connection:
        candidate = connection.execute(
            "SELECT status FROM promotion_candidates WHERE id='promo-member-approval'"
        ).fetchone()
        memory = connection.execute(
            "SELECT promotion_state FROM memory_items WHERE id=%s", (item.id,)
        ).fetchone()
        base_count = connection.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects "
            "WHERE source_candidate_id='promo-member-approval'"
        ).fetchone()["n"]
    assert candidate["status"] == "proposed"
    assert memory["promotion_state"] == "proposed"
    assert base_count == 0


# ---------------------------------------------------------------------------
# 群组成员变更的并发不变量(群组知识共享 P1-T3)
# ---------------------------------------------------------------------------
#
# 与本文件其余用例同一目的:证明**行锁真的承重**,而不是「代码里写了一句 FOR
# UPDATE」。静态那一半(检查必须落在写事务体内)由
# `backend/tests/test_group_store_transaction_guard.py` 承担,两者互补——静态守卫
# 看不见「锁根本没生效」,而这两条用例删掉 `_lock_group_on` 就会红。


def _seed_group_world(postgres_database, now: str, users: tuple[str, ...]) -> None:
    with postgres_database.write() as connection:
        for index, user_id in enumerate(users):
            connection.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,"
                "updated_at,username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (user_id, f"{user_id}@example.test", user_id, "user", "active",
                 now, now, f"g{index:08d}", "", "", 0),
            )
        connection.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,updated_at) "
            "VALUES (%s,%s,'project','',%s,%s,%s)",
            ("grp-race", "Race", users[0], now, now),
        )
        for user_id in users:
            connection.execute(
                "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
                "VALUES (%s,%s,'admin',%s,%s)",
                ("grp-race", user_id, now, users[0]),
            )


def _group_store(postgres_database, now: str):
    from app.repositories.postgres.group_store import GroupStore

    counter = iter(range(1, 50))
    counter_lock = threading.Lock()

    def new_id(prefix: str) -> str:
        with counter_lock:
            return f"{prefix}-race-{next(counter)}"

    return GroupStore(postgres_database, new_id=new_id, now=lambda: now)


@pytest.mark.postgres_integration
def test_concurrent_demotions_cannot_both_strip_the_last_group_admin(
    postgres_database, monkeypatch
):
    """两名组管理员被并发降级:`FOR UPDATE` 必须让第二个看到第一个的结果。

    没有那把锁,两条 `read committed` 事务各自读到 ``admin_count == 2``、各自判「还有
    别人」、然后**都提交**——组里一个管理员都不剩,而它的每个管理端点都要求组管理员
    身份。这个终态在单线程测试里造不出来,所以只有真并发用例证得了锁在承重。

    编排:winner 线程先在自己的写事务里拿到 `groups` 行的 `FOR UPDATE`,放行 loser
    线程进来;loser 的 store 体内那次 `FOR UPDATE` 必然阻塞到 winner 提交为止。
    """
    from app.repositories.ports import LastGroupAdminError

    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-18T00:00:00+00:00"
    _seed_group_world(postgres_database, now, ("user-race-a", "user-race-b"))
    store = _group_store(postgres_database, now)

    thread_role = threading.local()
    winner_locked = threading.Event()
    loser_connected = threading.Event()
    original_write = postgres_database.write

    @contextmanager
    def coordinated_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            role = getattr(thread_role, "value", "")
            if role == "winner":
                connection.execute(
                    "SELECT id FROM groups WHERE id='grp-race' FOR UPDATE"
                ).fetchone()
                winner_locked.set()
                assert loser_connected.wait(timeout=5)
            elif role:
                loser_connected.set()
            yield connection

    monkeypatch.setattr(postgres_database, "write", coordinated_write)

    def demote(role_name: str, user_id: str):
        thread_role.value = role_name
        try:
            return store.upsert_member(
                "grp-race", user_id, role="member", added_by="user-race-a"
            )
        except LastGroupAdminError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as executor:
        winner_future = executor.submit(demote, "winner", "user-race-a")
        assert winner_locked.wait(timeout=5)
        loser_future = executor.submit(demote, "loser", "user-race-b")
        outcomes = [winner_future.result(timeout=15), loser_future.result(timeout=15)]

    assert sorted(outcomes) == ["refused", "updated"], outcomes
    with postgres_database.connect() as connection:
        admins = connection.execute(
            "SELECT COUNT(*) AS n FROM group_members "
            "WHERE group_id='grp-race' AND role='admin'"
        ).fetchone()["n"]
    assert admins == 1, "并发降级把组的管理员降到了 0 —— 行锁没有承重"


@pytest.mark.postgres_integration
def test_adding_a_member_to_a_concurrently_deleted_group_fails_closed(
    postgres_database, monkeypatch
):
    """并发删组 + 加成员:必须是 `GroupNotFoundError`(→ 404),不是外键炸出来的 500。

    钉的是 `_lock_group_on` **消费了自己的返回值**这一半:等锁等完之后 `groups` 行
    真的没了,必须当场抛出;忽略它继续 INSERT,撞的是 `group_members.group_id` 的外键,
    用户拿到的是「服务器出错」而不是「这个群组不存在」。

    ⚠ 它**不是**行锁本身的承重证明——实测把 `FOR UPDATE` 摘掉这条仍然绿(两条事务的
    可见性在这个编排下已经足够让加成员那侧看到组没了)。锁的承重由上面那条
    `test_concurrent_demotions_cannot_both_strip_the_last_group_admin` 证明:摘锁即红。
    两条各钉一半,别把它们读成同一件事。
    """
    from app.repositories.ports import GroupNotFoundError

    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-08-18T00:00:00+00:00"
    _seed_group_world(postgres_database, now, ("user-del-a", "user-del-b"))
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at,username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("user-del-c", "c@example.test", "C", "user", "active", now, now,
             "g00000099", "", "", 0),
        )
    store = _group_store(postgres_database, now)

    thread_role = threading.local()
    deleter_locked = threading.Event()
    adder_connected = threading.Event()
    original_write = postgres_database.write

    @contextmanager
    def coordinated_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            role = getattr(thread_role, "value", "")
            if role == "delete":
                yield connection
                # 删除语句已经发出(行锁在手),放行加成员线程去撞这把锁,
                # 然后本事务才提交。
                deleter_locked.set()
                assert adder_connected.wait(timeout=5)
                return
            if role == "add":
                adder_connected.set()
            yield connection

    monkeypatch.setattr(postgres_database, "write", coordinated_write)

    def delete_group():
        thread_role.value = "delete"
        return store.delete_group("grp-race")

    def add_member():
        thread_role.value = "add"
        assert deleter_locked.wait(timeout=5)
        with pytest.raises(GroupNotFoundError):
            store.upsert_member(
                "grp-race", "user-del-c", role="member", added_by="user-del-a"
            )
        return "fail-closed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        delete_future = executor.submit(delete_group)
        add_future = executor.submit(add_member)
        assert delete_future.result(timeout=15) is True
        assert add_future.result(timeout=15) == "fail-closed"

    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM group_members WHERE user_id='user-del-c'"
        ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_delete_group_locks_the_group_row_before_sweeping_its_grants(
    postgres_database, monkeypatch
):
    """删组必须**先**锁住 `groups` 行,再清 `notebook_grants`。

    反过来会留下**孤儿授权边**:`create_grant` 持的是同一行的 `FOR SHARE`,它可以在
    「清边」与「删组」之间提交一条新边——清理已经走过去了,而 `principal_id` 没有外键,
    `DELETE FROM groups` 带不走它。组没了、边还在,只有 `merge_dbs` 的孤儿清扫或库主在
    共享清单里看见 `principal_kind="missing"` 才发现得了。

    本用例把那个窗口撑开:让 `create_grant` 拿到 `FOR SHARE` 之后停住,再发删组。锁在
    最前面时,删组会**阻塞**在取锁那一步(而不是先把清理跑完);放行之后它清掉的就是
    那条刚提交的新边。把锁挪回清理之后,这条用例会看到删组照常跑完清理、最后留下一条
    指向已删群组的边。
    """
    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    store = PostgresGroupStore(
        postgres_database,
        new_id=lambda prefix: f"{prefix}-delete-race",
        now=lambda: "2026-07-23T00:00:00+00:00",
    )
    group = store.create_group(
        name="删组竞态", kind="project", description="", created_by="owner-race"
    )

    share_locked = threading.Event()
    allow_grant_commit = threading.Event()
    delete_lock_attempted = threading.Event()
    delete_lock_acquired = threading.Event()
    allow_delete_continue = threading.Event()
    original_lock = store._lock_group_on

    def hooked_lock(connection, group_id, *, mode="UPDATE"):
        if mode == "SHARE":                       # create_grant 那一侧:拿到锁后停住
            original_lock(connection, group_id, mode=mode)
            share_locked.set()
            assert allow_grant_commit.wait(timeout=5)
            return None
        delete_lock_attempted.set()               # delete_group 那一侧:观察它等不等
        original_lock(connection, group_id, mode=mode)
        delete_lock_acquired.set()
        # ⚠ 拿到锁之后再停一次,是为了拆掉**测试自己**的一处竞态,与被测的锁序无关:
        # `create_grant` 提交之后还要在**新事务**里回读那一行做投影,而删组的锁恰好
        # 在那次提交的瞬间解开——不挡一下,删组会赶在回读之前把边删掉,granter 拿到
        # None 崩在投影上(整套件并行跑时稳定复现,单跑靠运气过)。
        assert allow_delete_continue.wait(timeout=5)
        return None

    monkeypatch.setattr(store, "_lock_group_on", hooked_lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        granter = executor.submit(
            store.create_grant,
            "nb-memory-race",
            principal_type="group",
            principal_id=group["id"],
            role="viewer",
            created_by="owner-race",
            admin_user_id="owner-race",
        )
        assert share_locked.wait(timeout=5)
        deleter = executor.submit(store.delete_group, group["id"])
        assert delete_lock_attempted.wait(timeout=5)
        # 锁在最前面 ⇒ 删组此刻**还没有**开始清理,它卡在取锁上。
        assert not delete_lock_acquired.wait(timeout=0.3)
        allow_grant_commit.set()
        granter.result(timeout=10)
        allow_delete_continue.set()
        assert deleter.result(timeout=10) is True

    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT 1 FROM groups WHERE id=%s", (group["id"],)
        ).fetchone() is None
        orphans = connection.execute(
            "SELECT COUNT(*) AS c FROM notebook_grants WHERE principal_id=%s",
            (group["id"],),
        ).fetchone()["c"]
    assert int(orphans) == 0, "删组之后留下了指向它的孤儿授权边"


@pytest.mark.postgres_integration
def test_share_request_blocks_on_the_notebook_row_and_fails_closed(
    postgres_database, monkeypatch
):
    """并发删库 + 提交共享申请:必须是 `NotebookNotFoundError`(→ 404),不是外键 500。

    `notebook_share_requests` 引用**两个**父表。`groups` 那一半早就有复核了,`notebooks`
    那一半没有(codex #519 R7 P2):能力守卫放行之后、写事务开始之前库被删掉,
    `INSERT` 撞 `notebook_id` 外键抛 `ForeignKeyViolation` —— `create_share_request` 只
    catch `UniqueViolation`,于是它一路冒到路由外面变成 500。

    编排把那个窗口撑开:deleter 线程发出 `DELETE FROM notebooks` 后**不提交**(行锁在手),
    requester 线程再进来提交申请。

    ⚠ 这条用例同时钉住**锁本身**和**锁模式**,两种退化各自都会让它红:

    * 把 `_lock_notebook_on` 整个删掉 → `lock_attempted` 永远不 set,第一条断言红
      (真实后果就是那次 `ForeignKeyViolation`);
    * 把 `FOR KEY SHARE` 摘成一条普通 `SELECT` → 未提交的 DELETE 在 `read committed` 下
      看不见,存在性检查**照常通过**、当场返回,`lock_returned` 立刻 set,第二条断言红
      (真实后果同样是随后 INSERT 的外键违例,只是窗口更窄)。

    与 `test_adding_a_member_to_a_concurrently_deleted_group_fails_closed` 分工:那条钉
    `groups` 那一半、且**不**证明锁在承重;这条两件都证。
    """
    from app.repositories.ports import NotebookNotFoundError

    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    counter = iter(range(1, 50))
    counter_lock = threading.Lock()

    def new_id(prefix: str) -> str:
        with counter_lock:
            return f"{prefix}-nb-race-{next(counter)}"

    store = PostgresGroupStore(
        postgres_database, new_id=new_id, now=lambda: "2026-07-23T00:00:00+00:00"
    )
    # 建组时创建者就是组管理员,所以事务内的成员资格复核会通过——本用例要证的是**它后面**
    # 那条笔记本存在性复核,别让申请在更早的一步就被挡下。
    group = store.create_group(
        name="删库竞态", kind="project", description="", created_by="owner-race"
    )

    delete_issued = threading.Event()
    allow_delete_commit = threading.Event()
    lock_attempted = threading.Event()
    lock_returned = threading.Event()
    original_lock = store._lock_notebook_on

    def hooked_lock(connection, notebook_id):
        lock_attempted.set()
        try:
            return original_lock(connection, notebook_id)
        finally:
            # 「这次取锁结束了」——正常返回与抛 NotebookNotFoundError 都算,要观察的是
            # 它有没有**阻塞**,不是它的结论。
            lock_returned.set()

    monkeypatch.setattr(store, "_lock_notebook_on", hooked_lock)

    def delete_notebook():
        with postgres_database.write() as connection:
            connection.execute("DELETE FROM notebooks WHERE id='nb-memory-race'")
            delete_issued.set()          # 行锁在手,尚未提交
            assert allow_delete_commit.wait(timeout=10)
        return "deleted"

    def file_request():
        assert delete_issued.wait(timeout=10)
        with pytest.raises(NotebookNotFoundError):
            store.create_share_request(
                "nb-memory-race", group_id=group["id"], requested_by="owner-race"
            )
        return "fail-closed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleter = executor.submit(delete_notebook)
        assert delete_issued.wait(timeout=10)
        requester = executor.submit(file_request)
        try:
            assert lock_attempted.wait(timeout=10), (
                "申请压根没去复核笔记本行 —— 那次 INSERT 会直接撞 notebook_id 外键"
            )
            # 锁模式承重的证明:`FOR KEY SHARE` 与 deleter 那条未提交 DELETE 的
            # `FOR UPDATE` 冲突,所以这一刻它必须还卡着。裸 SELECT 会当场通过。
            assert not lock_returned.wait(timeout=0.5), (
                "复核没有阻塞在未提交的删库上 —— 锁模式被摘掉了,存在性检查看到的是"
                "一行马上就要消失的记录"
            )
        finally:
            allow_delete_commit.set()
        assert deleter.result(timeout=15) == "deleted"
        assert requester.result(timeout=15) == "fail-closed"

    with postgres_database.connect() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM notebook_share_requests"
            ).fetchone()["c"]
        ) == 0, "指向已删笔记本的共享申请落库了"


@pytest.mark.postgres_integration
def test_create_grant_owner_branch_blocks_on_the_notebook_row_and_fails_closed(
    postgres_database, monkeypatch
):
    """`create_grant` 的 **owner 分支**同款外键竞态(codex #519 R7 存疑项收口)。

    `_require_notebook_manage_on` 拆成两半,只有非 owner 那半带锁:`FOR SHARE OF ng` 锁住
    授权边行,删库要 CASCADE 掉它就得先拿同一把锁,于是删不进来。**owner 半是一条无锁
    SELECT 且当场短路** —— 库主自己发边时,那条 SELECT 与随后的 INSERT 之间可以插进一次
    已提交的删库,`notebook_grants.notebook_id` 外键当场违例;而 `create_grant` 只 catch
    `UniqueViolation`,`ForeignKeyViolation` 一路冒成 500。

    编排与 `test_share_request_blocks_on_the_notebook_row_and_fails_closed` 相同,断言也同样
    **两件都钉**(删掉锁 → 第一条红;摘掉 `FOR KEY SHARE` → 第二条红)。

    ⚠ 发起人**就是库主**是本用例的要件:换成非 owner 会走进那条自带 `FOR SHARE OF ng` 的
    分支,竞态本来就不存在,用例会变成一条对着 bug 也全绿的空转。
    """
    from app.repositories.ports import NotebookNotFoundError

    assert PostgresMigrator(postgres_database).migrate() == 44
    _seed_memory_race(postgres_database)
    counter = iter(range(1, 50))
    counter_lock = threading.Lock()

    def new_id(prefix: str) -> str:
        with counter_lock:
            return f"{prefix}-grant-nb-race-{next(counter)}"

    store = PostgresGroupStore(
        postgres_database, new_id=new_id, now=lambda: "2026-07-23T00:00:00+00:00"
    )
    # owner-race 既是 nb-memory-race 的库主(走无锁的 owner 短路),又是本组的组管理员
    # (群组那一半的复核会通过)——两个前置条件都满足,才轮得到笔记本存在性这一关。
    group = store.create_group(
        name="发边删库竞态", kind="project", description="", created_by="owner-race"
    )

    delete_issued = threading.Event()
    allow_delete_commit = threading.Event()
    lock_attempted = threading.Event()
    lock_returned = threading.Event()
    original_lock = store._lock_notebook_on

    def hooked_lock(connection, notebook_id):
        lock_attempted.set()
        try:
            return original_lock(connection, notebook_id)
        finally:
            lock_returned.set()   # 正常返回与抛 NotebookNotFoundError 都算「结束了」

    monkeypatch.setattr(store, "_lock_notebook_on", hooked_lock)

    def delete_notebook():
        with postgres_database.write() as connection:
            connection.execute("DELETE FROM notebooks WHERE id='nb-memory-race'")
            delete_issued.set()          # 行锁在手,尚未提交
            assert allow_delete_commit.wait(timeout=10)
        return "deleted"

    def hand_out_grant():
        assert delete_issued.wait(timeout=10)
        with pytest.raises(NotebookNotFoundError):
            store.create_grant(
                "nb-memory-race",
                principal_type="group",
                principal_id=group["id"],
                role="viewer",
                created_by="owner-race",
                admin_user_id="owner-race",
            )
        return "fail-closed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        deleter = executor.submit(delete_notebook)
        assert delete_issued.wait(timeout=10)
        granter = executor.submit(hand_out_grant)
        try:
            assert lock_attempted.wait(timeout=10), (
                "发边压根没去复核笔记本行 —— owner 短路之后那次 INSERT 会直接撞外键"
            )
            assert not lock_returned.wait(timeout=0.5), (
                "复核没有阻塞在未提交的删库上 —— 锁模式被摘掉了"
            )
        finally:
            allow_delete_commit.set()
        assert deleter.result(timeout=15) == "deleted"
        assert granter.result(timeout=15) == "fail-closed"

    with postgres_database.connect() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) AS c FROM notebook_grants"
            ).fetchone()["c"]
        ) == 0, "指向已删笔记本的授权边落库了"


@pytest.mark.postgres_integration
def test_manage_recheck_locks_the_group_membership_behind_the_edge(
    postgres_database, monkeypatch
):
    """管理权复检必须锁住**整条生效链**,不只是授权边行(codex #519 R8 P1)。

    管理权来自 `group_admins` 边时,让那条边生效的是一行 `group_members`。R5 只锁了边行,
    而成员行藏在 `EXISTS (...)` 里锁不着——于是并发的移出组/降级可以提交在探测快照之后、
    `create_grant` 插入持久边之前,一个管理权**刚刚被撤销**的人照样把访问权散了出去。

    编排:发起人的 `create_grant` 在复检结束后停住(锁在手),另一线程去移出他的组成员
    资格。锁真的锁住整条链时,那次移除**拿不到锁**(连接上配了 `lock_timeout`,于是抛
    `LockNotAvailable`);序列化顺序因此是「发边在前、移出组在后」,语义正确。

    ⚠ 把 `FOR SHARE OF ng, ngm` 摘成 `FOR SHARE OF ng`,移除会**当场成功**——这条用例
    因此钉的是**锁模式本身**,不是「代码里有没有那句 SELECT」。
    """
    from psycopg import errors

    assert PostgresMigrator(postgres_database).migrate() == 44
    now = "2026-07-23T00:00:00+00:00"
    _seed_group_world(postgres_database, now, ("user-chain-a", "user-chain-b"))
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb-chain-race','Chain','','','ready','user-chain-a',%s,%s,'personal')",
            (now, now),
        )
        # user-chain-b 经 group_admins 边拿到 nb-chain-race 的管理权。grp-race 里两名
        # 管理员都在(`_seed_group_world` 建的),所以移除他不会先撞「最后一名组管理员」。
        connection.execute(
            "INSERT INTO notebook_grants"
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            "VALUES ('gnt-chain','nb-chain-race','group_admins','grp-race','admin',"
            "'user-chain-a',%s)",
            (now,),
        )
    store = _group_store(postgres_database, now)
    target = store.create_group(
        name="收边组", kind="project", description="", created_by="user-chain-b"
    )

    chain_locked = threading.Event()
    allow_release = threading.Event()
    original_recheck = store._require_notebook_manage_on

    def hooked_recheck(connection, notebook_id, user_id):
        result = original_recheck(connection, notebook_id, user_id)
        chain_locked.set()          # 复检通过,整条链的锁此刻应当都在手上
        assert allow_release.wait(timeout=10)
        return result

    monkeypatch.setattr(store, "_require_notebook_manage_on", hooked_recheck)

    def hand_out_grant():
        return store.create_grant(
            "nb-chain-race",
            principal_type="group",
            principal_id=target["id"],
            role="viewer",
            created_by="user-chain-b",
            admin_user_id="user-chain-b",
        )

    def evict_the_manager():
        assert chain_locked.wait(timeout=10)
        try:
            store.remove_member("grp-race", "user-chain-b")
            return "removed"
        except errors.LockNotAvailable:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as executor:
        granter = executor.submit(hand_out_grant)
        evictor = executor.submit(evict_the_manager)
        try:
            outcome = evictor.result(timeout=20)
        finally:
            allow_release.set()
        assert granter.result(timeout=20)["principal_id"] == target["id"]

    assert outcome == "blocked", (
        "移出组在发边事务持锁期间就完成了 —— 生效链的成员资格那一环没被锁住,"
        "一个管理权刚被撤销的人照样把访问权散了出去(codex #519 R8 P1)"
    )
