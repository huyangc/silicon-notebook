from __future__ import annotations

import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from app.core.config import Settings
from app.models.ask import AskRequest, AskResponse
from app.models.memory import MemoryWrite
from app.repositories.postgres.ask_state_store import AskStateStore as PostgresAskStateStore
from app.repositories.postgres.knowhow_store import KnowhowStore as PostgresKnowhowStore
from app.repositories.postgres.knowhow_transfer_store import (
    KnowhowTransferStore as PostgresKnowhowTransferStore,
)
from app.repositories.postgres.memory_store import MemoryStore as PostgresMemoryStore
from app.repositories.postgres.report_store import ReportStore as PostgresReportStore
from app.repositories.sqlite.ask_state_store import AskStateStore as SqliteAskStateStore
from app.repositories.sqlite.knowhow_store import KnowhowStore as SqliteKnowhowStore
from app.repositories.sqlite.knowhow_transfer_store import (
    KnowhowTransferStore as SqliteKnowhowTransferStore,
)
from app.repositories.sqlite.memory_store import MemoryStore as SqliteMemoryStore
from app.repositories.sqlite.report_store import ReportStore as SqliteReportStore
from app.services.repository_runtime import RepositoryCompatibilitySeams


NOW = "2026-07-23T00:00:00+00:00"


def _public_callables(cls: type) -> dict[str, object]:
    return {
        name: inspect.getattr_static(cls, name)
        for name in cls.__dict__
        if not name.startswith("_") and callable(getattr(cls, name))
    }


def _signature_shape(method) -> tuple:
    return tuple(
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(method).parameters.values()
    )


@pytest.mark.parametrize(
    ("sqlite_cls", "postgres_cls"),
    (
        (SqliteAskStateStore, PostgresAskStateStore),
        (SqliteReportStore, PostgresReportStore),
        (SqliteMemoryStore, PostgresMemoryStore),
        (SqliteKnowhowStore, PostgresKnowhowStore),
        (SqliteKnowhowTransferStore, PostgresKnowhowTransferStore),
    ),
)
def test_postgres_content_store_surfaces_cover_sqlite(sqlite_cls, postgres_cls):
    sqlite_methods = _public_callables(sqlite_cls)
    postgres_methods = _public_callables(postgres_cls)
    assert sqlite_methods.keys() <= postgres_methods.keys()
    for name in sqlite_methods.keys() & postgres_methods.keys():
        assert type(sqlite_methods[name]) is type(postgres_methods[name])
        assert _signature_shape(getattr(sqlite_cls, name)) == _signature_shape(
            getattr(postgres_cls, name)
        )


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-content-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed_catalog(database, backend: str) -> None:
    mark = "%s" if backend == "postgres" else "?"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (
                "user-content",
                "content@example.test",
                "Content",
                "admin",
                "active",
                NOW,
                NOW,
                "c00123456",
                "",
                "",
                0,
            ),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            f"VALUES ({','.join([mark] * 9)})",
            (
                "nb-content",
                "Content",
                "",
                "engineering",
                "ready",
                "user-content",
                NOW,
                NOW,
                "personal",
            ),
        )


@dataclass
class ContentHarness:
    backend: str
    database: object
    ask: object
    report: object
    memory: object
    knowhow: object
    transfer: object


@pytest.fixture(
    params=("sqlite", pytest.param("postgres", marks=pytest.mark.postgres_integration))
)
def content_harness(request, tmp_path) -> ContentHarness:
    seams = _seams()
    if request.param == "sqlite":
        from app.repositories.sqlite.database import SqliteDatabase
        from app.repositories.sqlite.migrations import SqliteMigrator

        settings = Settings(database_url=f"sqlite:///{tmp_path / 'content-golden.db'}")
        database = SqliteDatabase(settings, tmp_path)
        SqliteMigrator(database, settings).initialize()
        _seed_catalog(database, "sqlite")
        harness = ContentHarness(
            backend="sqlite",
            database=database,
            ask=SqliteAskStateStore(database, seams),
            report=SqliteReportStore(
                database,
                new_id=seams.new_id,
                now=seams.now,
                current_user_id=lambda: "user-content",
            ),
            memory=SqliteMemoryStore(database, new_id=seams.new_id, now=seams.now),
            knowhow=SqliteKnowhowStore(database, new_id=seams.new_id, now=seams.now),
            transfer=SqliteKnowhowTransferStore(database),
        )
        try:
            yield harness
        finally:
            database.close_local()
        return

    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 7
    _seed_catalog(database, "postgres")
    yield ContentHarness(
        backend="postgres",
        database=database,
        ask=PostgresAskStateStore(database, seams),
        report=PostgresReportStore(
            database,
            new_id=seams.new_id,
            now=seams.now,
            current_user_id=lambda: "user-content",
        ),
        memory=PostgresMemoryStore(database, new_id=seams.new_id, now=seams.now),
        knowhow=PostgresKnowhowStore(database, new_id=seams.new_id, now=seams.now),
        transfer=PostgresKnowhowTransferStore(database),
    )


def test_ask_and_report_state_shapes_match_sqlite_golden(content_harness):
    request = AskRequest(question="What is deterministic state?")
    job_id, conversation_id = content_harness.ask.begin_durable_job(
        "nb-content", request, "chunk", "user-content"
    )
    content_harness.ask.append_trace(
        "nb-content", job_id, {"step_type": "retrieve", "summary": "done"}, "user-content"
    )
    response = AskResponse(
        answer="Deterministic state.",
        conclusion="Deterministic state.",
        citations=[],
        anchors=[],
    )
    answer_id = content_harness.ask.save_answer(
        "nb-content", conversation_id, request.question, response, "user-content"
    )
    assert content_harness.ask.finish_job(job_id, "done", answer_id=answer_id) == conversation_id
    detail = content_harness.ask.ask_job_detail(job_id)
    assert detail["status"] == "done"
    assert detail["trace"] == [{"step_type": "retrieve", "summary": "done"}]
    assert content_harness.ask.answer_memory_source(answer_id)["answer"] == "Deterministic state."

    report_id = content_harness.report.create_report("nb-content", "State report", 3)
    content_harness.report.update_report(
        "nb-content",
        report_id,
        status="done",
        outline=[{"title": "State"}],
        sections=[{"title": "State", "citations": [answer_id]}],
        references=[{"answer_id": answer_id}],
        content_md="# State",
    )
    report = content_harness.report.get_report("nb-content", report_id)
    assert report["outline"] == [{"title": "State"}]
    assert report["references"] == [{"answer_id": answer_id}]
    assert report["created_at"].startswith("2026-07-23T00:00:00")
    assert content_harness.report.cancel_report("nb-content", report_id) is True
    content_harness.report.update_report(
        "nb-content", report_id, status="done", content_md="# too late"
    )
    cancelled = content_harness.report.get_report("nb-content", report_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["content_md"] == "# State"


@pytest.mark.postgres_integration
def test_postgres_report_cancel_commit_beats_blocked_terminal_write(
    postgres_database,
):
    """Two real PG sessions contend on the report row; cancelled stays sticky."""
    import psycopg
    from psycopg.rows import dict_row

    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()
    report = PostgresReportStore(
        postgres_database,
        new_id=seams.new_id,
        now=seams.now,
        current_user_id=lambda: "user-content",
    )
    report_id = report.create_report("nb-content", "race")
    terminal_database = PostgresDatabase(
        postgres_database.settings, postgres_database.root_dir
    )
    terminal_report = PostgresReportStore(
        terminal_database,
        new_id=seams.new_id,
        now=seams.now,
        current_user_id=lambda: "user-content",
    )
    terminal_pid: list[int] = []
    terminal_connected = threading.Event()
    original_write = terminal_database.write

    @contextmanager
    def observed_terminal_write(*args, **kwargs):
        with original_write(*args, **kwargs) as connection:
            terminal_pid.append(
                int(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            )
            terminal_connected.set()
            yield connection

    terminal_database.write = observed_terminal_write
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with postgres_database.write() as cancelling:
                cancelling.execute(
                    "UPDATE reports SET status='cancelled',progress=%s,updated_at=%s "
                    "WHERE id=%s AND notebook_id=%s",
                    ("已取消", NOW, report_id, "nb-content"),
                )
                terminal_future = pool.submit(
                    terminal_report.update_report,
                    "nb-content",
                    report_id,
                    status="done",
                    content_md="# too late",
                )
                assert terminal_connected.wait(timeout=5)
                lock_wait_seen = False
                deadline = time.monotonic() + 3
                with psycopg.connect(
                    postgres_database.settings.database_url,
                    row_factory=dict_row,
                ) as inspector:
                    while time.monotonic() < deadline:
                        state = inspector.execute(
                            "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                            (terminal_pid[0],),
                        ).fetchone()
                        if state and state["wait_event_type"] == "Lock":
                            lock_wait_seen = True
                            break
                        time.sleep(0.01)
                assert lock_wait_seen
            terminal_future.result(timeout=5)
    finally:
        terminal_database.close()

    final = report.get_report("nb-content", report_id)
    assert final["status"] == "cancelled"
    assert final["content_md"] == ""


def test_knowhow_mutation_and_snapshot_shapes_match_sqlite_golden(content_harness):
    table_id = content_harness.knowhow.create_knowhow_table(
        "nb-content",
        "Thermal table",
        "",
        [
            {"name": "Topic", "role": "anchor"},
            {"name": "Procedure", "role": "procedure"},
        ],
        "user-content",
    )
    table = content_harness.knowhow.get_knowhow_table(table_id)
    row_id = content_harness.knowhow.add_knowhow_row(table_id, {table["columns"][0]["id"]: "A"})
    content_harness.knowhow.update_knowhow_cell(
        row_id, table["columns"][1]["id"], "Step 1", []
    )
    updated = content_harness.knowhow.get_knowhow_table(table_id)
    assert updated["mutation_seq"] == 1
    assert updated["rows"][0]["cells"][table["columns"][1]["id"]] == "Step 1"
    snapshot = content_harness.transfer.snapshot_table(table_id)
    assert snapshot["table"]["id"] == table_id
    assert len(snapshot["rows"]) == 1
    assert len(snapshot["cells"]) == 2


def test_knowhow_transfer_fingerprint_and_code_isolation_match_golden(content_harness):
    table_id = content_harness.knowhow.create_knowhow_table(
        "nb-content",
        "Transfer",
        "description",
        [
            {"name": "Topic", "role": "anchor"},
            {"name": "Procedure", "role": "procedure"},
        ],
        "user-content",
    )
    table = content_harness.knowhow.get_knowhow_table(table_id)
    anchor_id, procedure_id = [column["id"] for column in table["columns"]]
    row_id = content_harness.knowhow.add_knowhow_row(
        table_id, {anchor_id: "A", procedure_id: "Visible knowledge"}
    )
    content_harness.knowhow.upsert_knowhow_cell_code(
        row_id,
        procedure_id,
        "print('isolated')",
        "python",
        "user-content",
        "hash",
    )
    source = content_harness.transfer.snapshot_table(table_id)
    assert source["chunks"] == []
    assert source["elements"] == []
    assert source["cell_code"][0]["code_text"] == "print('isolated')"

    table_map = {table_id: "khtbl-transfer-copy"}
    column_map = {
        source["columns"][0]["id"]: "khcol-transfer-anchor",
        source["columns"][1]["id"]: "khcol-transfer-procedure",
    }
    row_map = {source["rows"][0]["id"]: "khrow-transfer-copy"}
    payload = {
        "table": {
            **source["table"],
            "id": table_map[table_id],
            "title": "Transfer copy",
            "hidden_source_id": None,
        },
        "columns": [
            {**column, "id": column_map[column["id"]], "table_id": table_map[table_id]}
            for column in source["columns"]
        ],
        "rows": [
            {**row, "id": row_map[row["id"]], "table_id": table_map[table_id]}
            for row in source["rows"]
        ],
        "cells": [
            {
                **cell,
                "id": f"copy-{cell['id']}",
                "row_id": row_map[cell["row_id"]],
                "column_id": column_map[cell["column_id"]],
            }
            for cell in source["cells"]
        ],
        "cell_code": [
            {
                **code,
                "id": f"copy-{code['id']}",
                "row_id": row_map[code["row_id"]],
                "column_id": column_map[code["column_id"]],
            }
            for code in source["cell_code"]
        ],
        "assets": [],
        "source": None,
        "elements": [],
        "chunks": [],
        "chunk_embeddings": [],
    }
    content_harness.transfer.insert_transfer(
        payload,
        {"columns": 2, "rows": 1, "cells": 2, "cell_code": 1},
    )
    copied = content_harness.knowhow.get_knowhow_table("khtbl-transfer-copy")
    assert copied["rows"][0]["cells"]["khcol-transfer-procedure"] == "Visible knowledge"
    fingerprint = content_harness.transfer.table_fingerprint("khtbl-transfer-copy")
    assert fingerprint
    content_harness.knowhow.rename_knowhow_column(
        "khcol-transfer-procedure", "Renamed"
    )
    assert content_harness.transfer.delete_table_if_unchanged(
        "khtbl-transfer-copy", fingerprint
    ) is False


@pytest.mark.postgres_integration
@pytest.mark.parametrize("mutation", ("upsert", "delete"))
def test_postgres_code_mutation_wins_against_conditional_transfer_delete(
    postgres_database, mutation
):
    """Code is fingerprinted business state and locks the table aggregate."""
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    transfer = PostgresKnowhowTransferStore(postgres_database)
    table_id = knowhow.create_knowhow_table(
        "nb-content",
        "Code race",
        "",
        [{"name": "Topic", "role": "anchor"}],
        "user-content",
    )
    column_id = knowhow.get_knowhow_table(table_id)["columns"][0]["id"]
    row_id = knowhow.add_knowhow_row(table_id, {column_id: "A"})
    knowhow.upsert_knowhow_cell_code(
        row_id, column_id, "old", "python", "user-content", "hash-old"
    )
    fingerprint = transfer.table_fingerprint(table_id)

    mutation_locked = threading.Event()
    delete_started = threading.Event()
    original_lock = knowhow._lock_table_for_row

    def pause_after_aggregate_lock(connection, locked_row_id):
        table = original_lock(connection, locked_row_id)
        mutation_locked.set()
        assert delete_started.wait(timeout=10)
        return table

    knowhow._lock_table_for_row = pause_after_aggregate_lock

    def mutate_code():
        if mutation == "upsert":
            return knowhow.upsert_knowhow_cell_code(
                row_id,
                column_id,
                "new",
                "python",
                "user-content",
                "hash-new",
            )
        return knowhow.delete_knowhow_cell_code(row_id, column_id)

    def conditionally_delete():
        assert mutation_locked.wait(timeout=10)
        delete_started.set()
        return transfer.delete_table_if_unchanged(table_id, fingerprint)

    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation_future = pool.submit(mutate_code)
        delete_future = pool.submit(conditionally_delete)
        mutation_future.result(timeout=15)
        assert delete_future.result(timeout=15) is False

    assert knowhow.get_knowhow_table(table_id)["id"] == table_id
    code = knowhow.get_knowhow_cell_code(row_id, column_id)
    if mutation == "upsert":
        assert code["code_text"] == "new"
    else:
        assert code is None


def test_memory_revision_provenance_and_json_null_match_sqlite_golden(content_harness):
    write = MemoryWrite(
        id="mem-content",
        notebook_id="nb-content",
        created_by="user-content",
        origin="external_agent",
        status="candidate",
        title="Remember state",
        content_md="State is durable.",
        tags=["state", "状态"],
        created_at=NOW,
        updated_at=NOW,
        provenance={"client_request_id": "request-1", "optional": None},
    )
    item = content_harness.memory.create_candidate_with_initial_revision(
        write, "user-content", "created"
    )
    assert item.tags == ["state", "状态"]
    assert item.provenance["optional"] is None
    assert content_harness.memory.memory_by_agent_request(
        "user-content", "nb-content", None, "request-1"
    ).id == item.id
    confirmed = content_harness.memory.transition_with_revision(
        item.id,
        "user-content",
        {"candidate"},
        "confirmed",
        fields=None,
        changed_by="user-content",
        reason="confirmed",
    )
    assert confirmed.status == "confirmed"
    revisions = content_harness.memory.revisions_for_user(item.id, "user-content")
    assert [revision.revision for revision in revisions] == [1, 2]
    assert all(revision.created_at.startswith("2026-07-23T00:00:00") for revision in revisions)


def test_memory_rejects_nested_non_finite_json_without_partial_row(content_harness):
    write = MemoryWrite(
        id="mem-invalid-json",
        notebook_id="nb-content",
        created_by="user-content",
        origin="external_agent",
        status="candidate",
        title="Invalid",
        content_md="Invalid JSON must not persist.",
        tags=[],
        created_at=NOW,
        updated_at=NOW,
        provenance={"nested": [None, {"bad": float("nan")}]},
    )
    with pytest.raises(ValueError, match="non-finite"):
        content_harness.memory.create_candidate_with_initial_revision(
            write, "user-content", "created"
        )
    with content_harness.database.connect() as connection:
        mark = "%s" if content_harness.backend == "postgres" else "?"
        assert connection.execute(
            f"SELECT 1 FROM memory_items WHERE id={mark}", (write.id,)
        ).fetchone() is None


@pytest.mark.postgres_integration
def test_postgres_memory_search_filters_scope_before_candidate_limit(
    postgres_database,
):
    """Other owners cannot crowd a valid hit out of the bounded lexical pool."""
    from app.repositories.postgres.migrator import PostgresMigrator
    from psycopg.types.json import Jsonb

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()
    store = PostgresMemoryStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
            (
                "user-crowdout",
                "crowdout@example.test",
                "Crowdout",
                NOW,
                NOW,
                "z00123456",
            ),
        )
        connection.execute(
            "INSERT INTO notebook_members(notebook_id,user_id,role,added_at) "
            "VALUES (%s,%s,'reader',%s)",
            ("nb-content", "user-crowdout", NOW),
        )
        noise = [
            (
                f"aaa-crowdout-{index:03d}",
                "nb-content",
                "user-crowdout",
                "external_agent",
                "confirmed",
                "crowdout-token",
                "crowdout-token",
                Jsonb([]),
                NOW,
                NOW,
            )
            for index in range(220)
        ]
        connection.cursor().executemany(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,tags_json,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            noise,
        )
        connection.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,tags_json,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "zzz-valid-memory",
                "nb-content",
                "user-content",
                "external_agent",
                "confirmed",
                "crowdout-token",
                "crowdout-token",
                Jsonb([]),
                NOW,
                NOW,
            ),
        )

    result = store.list_memories(
        "user-content",
        notebook_id="nb-content",
        status="confirmed",
        origin="external_agent",
        query="crowdout-token",
        offset=0,
        limit=10,
    )
    assert result.total_count == 1
    assert [item.id for item in result.items] == ["zzz-valid-memory"]


def test_memory_edit_supersedes_pinned_promotion_atomically(content_harness):
    write = MemoryWrite(
        id="mem-promote",
        notebook_id="nb-content",
        created_by="user-content",
        origin="external_agent",
        status="confirmed",
        title="Pinned",
        content_md="Pinned content.",
        tags=["pin"],
        created_at=NOW,
        updated_at=NOW,
        confirmed_by="user-content",
        confirmed_at=NOW,
        provenance={"optional": None},
    )
    item = content_harness.memory.create_candidate_with_initial_revision(
        write, "user-content", "created"
    )
    mark = "%s" if content_harness.backend == "postgres" else "?"
    with content_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,status,"
            "created_at,updated_at,target_base_id) "
            f"VALUES ({','.join([mark] * 8)})",
            ("promo-memory", "nb-content", item.id, "memory", "proposed", NOW, NOW, ""),
        )
        promoted = content_harness.memory.propose_promotion_on(
            connection,
            item.id,
            "user-content",
            "promo-memory",
            [{"object_type": "claim", "payload": {"name": "Pinned"}}],
            [{"quoted_span": "Pinned", "optional": None}],
            item,
            NOW,
        )
    assert promoted.promotion_state == "proposed"

    edited = content_harness.memory.update_with_revision(
        item.id,
        "user-content",
        {"content_md": "Edited content."},
        expected={"confirmed"},
        changed_by="user-content",
        reason="edited",
    )
    assert edited.promotion_state == "none"
    assert edited.provenance["kg_promotion"]["state"] == "superseded"
    assert "proposal_id" not in edited.provenance["kg_promotion"]
    with content_harness.database.connect() as connection:
        candidate = connection.execute(
            f"SELECT status FROM promotion_candidates WHERE id={mark}",
            ("promo-memory",),
        ).fetchone()
    assert candidate["status"] == "rejected"


@pytest.mark.postgres_integration
def test_postgres_projector_commits_terminal_knowhow_graph(
    postgres_database, monkeypatch
):
    """The real projector reaches its terminal graph transaction on PG."""
    from types import SimpleNamespace

    from app.repositories.postgres.chunk_store import ChunkStore
    from app.repositories.postgres.embedding_store import EmbeddingStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import SourceStore
    from app.services.knowhow.projection import KnowhowProjector

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    sources = SourceStore(postgres_database, now=seams.now)
    chunks = ChunkStore(postgres_database)
    knowledge = KnowledgeStore(postgres_database, seams)
    vectors = EmbeddingStore(write=postgres_database.write)
    projector = KnowhowProjector(
        settings=Settings(database_url="sqlite:///unused.db"),
        database=postgres_database,
        knowhow=knowhow,
        sources=sources,
        chunks=chunks,
        knowledge=knowledge,
        embedding=SimpleNamespace(vectors=vectors),
        note_model_error=lambda *_args, **_kwargs: None,
        invalidate_unified_cache=lambda _notebook_id: None,
        mark_unified_dirty=lambda _notebook_id: None,
        new_id=seams.new_id,
        now=seams.now,
    )
    table_id = knowhow.create_knowhow_table(
        "nb-content",
        "稳定性",
        "",
        [
            {"name": "问题", "role": "anchor"},
            {"name": "方法", "role": "procedure"},
        ],
        "user-content",
    )
    columns = knowhow.get_knowhow_table(table_id)["columns"]
    by_name = {column["name"]: column["id"] for column in columns}
    row_id = knowhow.add_knowhow_row(
        table_id,
        {by_name["问题"]: "振荡", by_name["方法"]: "增加阻尼"},
    )

    assert projector.project_table(table_id, embed=False) == "nb-content"

    table = knowhow.get_knowhow_table(table_id)
    assert table["rows"][0]["id"] == row_id
    assert table["rows"][0]["projection_status"] == "synced"
    source_id = table["hidden_source_id"]
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM source_elements WHERE source_id=%s",
            (source_id,),
        ).fetchone()["n"] == 2
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id=%s",
            (source_id,),
        ).fetchone()["n"] == 2
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects WHERE source_id=%s",
            (source_id,),
        ).fetchone()["n"] == 2
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM knowledge_relations WHERE source_id=%s",
            (source_id,),
        ).fetchone()["n"] == 1

    # Pin the PG JSONB legacy discovery path and run the scheduled replacement
    # synchronously. This must never fall back to SQLite's json_extract SQL.
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,"
            "source_candidate_id,source_id,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,'',%s::jsonb,%s::jsonb,NULL,%s,%s,%s)",
            (
                "ko-kh-legacy-content",
                "nb-content",
                "procedure",
                "approved",
                json.dumps({"table_id": table_id, "name": "legacy"}),
                "[]",
                source_id,
                NOW,
                NOW,
            ),
        )
    monkeypatch.setattr(
        "app.services.knowhow.projection.background_jobs.submit",
        lambda fn, *args, **_kwargs: fn(*args, embed=False),
    )
    assert projector.reproject_legacy_tables() == [table_id]
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM knowledge_objects "
            "WHERE id=%s OR (object_type=%s AND id LIKE %s)",
            ("ko-kh-legacy-content", "procedure", "ko-kh-%"),
        ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_postgres_projector_and_delete_leave_no_projection_orphans(
    postgres_database,
):
    """A delete queued behind terminal projection removes the whole aggregate.

    The barriers put teardown exactly at its source-row lock while projection
    holds the table/source key-share locks.  This pins the lock order without
    timing sleeps and catches both deadlocks and mixed old/new graph state.
    """
    from types import SimpleNamespace

    from app.repositories.postgres.chunk_store import ChunkStore
    from app.repositories.postgres.embedding_store import EmbeddingStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import SourceStore
    from app.services.knowhow.projection import KnowhowProjector

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    sources = SourceStore(postgres_database, now=seams.now)
    chunks = ChunkStore(postgres_database)
    knowledge = KnowledgeStore(postgres_database, seams)
    projector = KnowhowProjector(
        settings=Settings(database_url="sqlite:///unused.db"),
        database=postgres_database,
        knowhow=knowhow,
        sources=sources,
        chunks=chunks,
        knowledge=knowledge,
        embedding=SimpleNamespace(
            vectors=EmbeddingStore(write=postgres_database.write)
        ),
        note_model_error=lambda *_args, **_kwargs: None,
        invalidate_unified_cache=lambda _notebook_id: None,
        mark_unified_dirty=lambda _notebook_id: None,
        new_id=seams.new_id,
        now=seams.now,
    )
    table_id = knowhow.create_knowhow_table(
        "nb-content",
        "删除竞态",
        "",
        [
            {"name": "问题", "role": "anchor"},
            {"name": "方法", "role": "procedure"},
        ],
        "user-content",
    )
    columns = knowhow.get_knowhow_table(table_id)["columns"]
    by_name = {column["name"]: column["id"] for column in columns}
    knowhow.add_knowhow_row(
        table_id,
        {by_name["问题"]: "振荡", by_name["方法"]: "增加阻尼"},
    )
    assert projector.project_table(table_id, embed=False) == "nb-content"
    source_id = knowhow.get_knowhow_table(table_id)["hidden_source_id"]

    # Make the next pass materially different so a mixed terminal write would
    # be observable, then park it after both aggregate locks are held.
    knowhow.add_knowhow_row(
        table_id,
        {by_name["问题"]: "噪声", by_name["方法"]: "增加滤波"},
    )
    projection_locked = threading.Event()
    delete_at_source_lock = threading.Event()
    projecting = threading.local()
    original_delete_relations = knowledge.delete_relations_by_source
    original_source_lock = sources.source_exists_for_update_tx

    def pause_terminal_write(connection, locked_source_id):
        if getattr(projecting, "active", False):
            projection_locked.set()
            assert delete_at_source_lock.wait(timeout=10)
        return original_delete_relations(connection, locked_source_id)

    def observe_delete_lock(connection, locked_source_id):
        delete_at_source_lock.set()
        return original_source_lock(connection, locked_source_id)

    knowledge.delete_relations_by_source = pause_terminal_write
    sources.source_exists_for_update_tx = observe_delete_lock

    def run_projection():
        projecting.active = True
        try:
            return projector.project_table(table_id, embed=False)
        finally:
            projecting.active = False

    def run_delete():
        assert projection_locked.wait(timeout=10)
        projector.delete_table_projection(source_id)
        return knowhow.delete_knowhow_table(table_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        projection_future = pool.submit(run_projection)
        delete_future = pool.submit(run_delete)
        assert projection_future.result(timeout=15) == "nb-content"
        assert delete_future.result(timeout=15)["hidden_source_id"] == source_id

    with postgres_database.connect() as connection:
        for table_name, predicate in (
            ("knowhow_tables", "id=%s"),
            ("sources", "id=%s"),
            ("source_elements", "source_id=%s"),
            ("chunks", "source_id=%s"),
            ("knowledge_objects", "source_id=%s"),
            ("knowledge_relations", "source_id=%s"),
        ):
            assert connection.execute(
                f"SELECT COUNT(*) AS n FROM {table_name} WHERE {predicate}",
                (table_id if table_name == "knowhow_tables" else source_id,),
            ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_postgres_two_projectors_serialize_whole_pass_and_newest_wins(
    postgres_database,
):
    """A stale pass cannot finish after a newer pass from another worker."""
    from types import SimpleNamespace

    from app.repositories.postgres.chunk_store import ChunkStore
    from app.repositories.postgres.embedding_store import EmbeddingStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import SourceStore
    from app.services.knowhow.projection import KnowhowProjector

    assert PostgresMigrator(postgres_database).migrate() == 7
    _seed_catalog(postgres_database, "postgres")
    seams = _seams()

    def make_projector():
        knowhow_store = PostgresKnowhowStore(
            postgres_database, new_id=seams.new_id, now=seams.now
        )
        source_store = SourceStore(postgres_database, now=seams.now)
        return knowhow_store, KnowhowProjector(
            settings=Settings(database_url="sqlite:///unused.db"),
            database=postgres_database,
            knowhow=knowhow_store,
            sources=source_store,
            chunks=ChunkStore(postgres_database),
            knowledge=KnowledgeStore(postgres_database, seams),
            embedding=SimpleNamespace(
                vectors=EmbeddingStore(write=postgres_database.write)
            ),
            note_model_error=lambda *_args, **_kwargs: None,
            invalidate_unified_cache=lambda _notebook_id: None,
            mark_unified_dirty=lambda _notebook_id: None,
            new_id=seams.new_id,
            now=seams.now,
        )

    knowhow_a, projector_a = make_projector()
    knowhow_b, projector_b = make_projector()
    table_id = knowhow_a.create_knowhow_table(
        "nb-content",
        "跨进程投影",
        "",
        [
            {"name": "问题", "role": "anchor"},
            {"name": "方法", "role": "procedure"},
        ],
        "user-content",
    )
    columns = knowhow_a.get_knowhow_table(table_id)["columns"]
    by_name = {column["name"]: column["id"] for column in columns}
    row_id = knowhow_a.add_knowhow_row(
        table_id,
        {by_name["问题"]: "振荡", by_name["方法"]: "旧方法"},
    )

    stale_snapshot_loaded = threading.Event()
    release_stale_pass = threading.Event()
    stale_terminal_done = threading.Event()
    newer_started = threading.Event()
    original_get_a = knowhow_a.get_knowhow_table
    original_locked_a = projector_a._project_table_locked
    original_get_b = knowhow_b.get_knowhow_table

    def pause_after_stale_snapshot(locked_table_id):
        table = original_get_a(locked_table_id)
        stale_snapshot_loaded.set()
        assert release_stale_pass.wait(timeout=10)
        return table

    def mark_stale_terminal(locked_table_id, *, embed):
        result = original_locked_a(locked_table_id, embed=embed)
        stale_terminal_done.set()
        return result

    def assert_newer_enters_after_stale(locked_table_id):
        assert stale_terminal_done.is_set()
        return original_get_b(locked_table_id)

    knowhow_a.get_knowhow_table = pause_after_stale_snapshot
    projector_a._project_table_locked = mark_stale_terminal
    knowhow_b.get_knowhow_table = assert_newer_enters_after_stale

    def run_newer():
        newer_started.set()
        return projector_b.project_table(table_id, embed=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_future = pool.submit(projector_a.project_table, table_id, embed=False)
        assert stale_snapshot_loaded.wait(timeout=10)
        knowhow_b.update_knowhow_cell(row_id, by_name["方法"], "新方法")
        newer_future = pool.submit(run_newer)
        assert newer_started.wait(timeout=10)
        release_stale_pass.set()
        assert stale_future.result(timeout=15) == "nb-content"
        assert newer_future.result(timeout=15) == "nb-content"

    table = knowhow_b.get_knowhow_table(table_id)
    source_id = table["hidden_source_id"]
    assert table["rows"][0]["projection_status"] == "synced"
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM sources "
            "WHERE notebook_id=%s AND source_type='knowhow'",
            ("nb-content",),
        ).fetchone()["n"] == 1
        texts = {
            row["text"]
            for row in connection.execute(
                "SELECT payload->>'text' AS text FROM knowledge_objects "
                "WHERE source_id=%s",
                (source_id,),
            ).fetchall()
        }
    assert texts == {"振荡", "新方法"}
