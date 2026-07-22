from __future__ import annotations

import inspect
import json
import threading
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
