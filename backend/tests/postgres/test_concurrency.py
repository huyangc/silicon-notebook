from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.types.json import Jsonb

from app.models.ask import AskRequest
from app.models.memory import MemoryWrite
from app.repositories.postgres.ask_state_store import AskStateStore
from app.repositories.postgres.governance_store import GovernanceStore
from app.repositories.postgres.knowhow_store import KnowhowStore
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams


@pytest.mark.postgres_integration
def test_competing_ask_terminal_writes_keep_explicit_cancel(postgres_database):
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
    job_id, _ = store.begin_durable_job(
        "nb-race", AskRequest(question="race"), "chunk", "user-race"
    )
    barrier = threading.Barrier(2)

    def finish(status: str):
        barrier.wait()
        return store.finish_job(job_id, status, answer_id="ans-race" if status == "done" else "")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(finish, "cancelled"), executor.submit(finish, "done")]
        for future in futures:
            future.result()

    assert store.ask_job_status(job_id)["status"] == "cancelled"


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
