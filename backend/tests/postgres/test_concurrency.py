from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace

import pytest
from psycopg.types.json import Jsonb

from app.models.ask import AskRequest, AskResponse
from app.models.memory import MemoryWrite
from app.repositories.postgres.ask_state_store import AskStateStore
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.knowhow_store import KnowhowStore
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams


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
    assert PostgresMigrator(postgres_database).migrate() == 7
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


@pytest.mark.postgres_integration
def test_revoked_member_cannot_commit_save_answer_memory(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    assert PostgresMigrator(postgres_database).migrate() == 7
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


@pytest.mark.postgres_integration
def test_stale_projection_pass_cannot_overwrite_newer_pending_edit(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    original_lock = store._lock_embedding_memory_on

    def pause_after_embedding_lock(connection, memory_id):
        locked = original_lock(connection, memory_id)
        embedding_holds_memory.set()
        assert edit_started.wait(timeout=5)
        return locked

    store._lock_embedding_memory_on = pause_after_embedding_lock

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
    assert PostgresMigrator(postgres_database).migrate() == 7
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
    from app.services.repository_runtime import RepositoryCompatibilitySeams

    assert PostgresMigrator(postgres_database).migrate() == 7
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
        settings=Settings(database_url="sqlite:///unused.db"),
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
        llm=lambda: None,
        kg_llm=lambda: None,
        relations_for_notebook=lambda _notebook_id: [],
        edge_centrality_map=lambda _notebook_id: {},
        embed_knowledge=lambda *_args: None,
        knowledge_objects=lambda *_args, **_kwargs: [],
        as_retrieved=lambda row, _tier: row,
        rule_card=lambda row: row,
        set_conflict_status=lambda *_args: None,
        memory_store=store,
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
