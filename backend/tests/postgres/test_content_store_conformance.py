from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace

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
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.ask_execution import AskCancellationRegistry, AskExecutionCoordinator
from app.services.ask_service import AskService
from app.services.cancellation import AskCancelled, raise_if_cancelled


NOW = "2026-07-23T00:00:00+00:00"


pytestmark = pytest.mark.postgres_integration


def _knowhow_columns():
    return [
        {"name": "Topic", "role": "anchor"},
        {"name": "Procedure", "role": "procedure"},
    ]


def _cell_values(table: dict) -> list[list[str]]:
    column_ids = [column["id"] for column in table["columns"]]
    return [
        [row["cells"].get(column_id, "") for column_id in column_ids]
        for row in table["rows"]
    ]


def test_knowhow_atomic_create_rolls_back_table_for_missing_asset_and_row2_error(
    content_harness, monkeypatch
):
    store = content_harness.knowhow
    before = store.list_knowhow_tables("nb-content")
    missing = "asset-missing-import"
    with pytest.raises(ValueError, match=missing):
        store.create_knowhow_table_with_rows(
            "nb-content",
            "Atomic import",
            "",
            _knowhow_columns(),
            [["A", "ok"], ["B", f"![missing](asset://{missing})"]],
            "user-content",
        )
    assert store.list_knowhow_tables("nb-content") == before

    original_new_id = store.new_id
    cell_calls = 0

    def duplicate_cell_id(prefix: str) -> str:
        nonlocal cell_calls
        if prefix == "khcel":
            cell_calls += 1
            # Row 1's two cells succeed; row 2's first cell collides with row 1.
            return f"khcel-forced-{1 if cell_calls >= 3 else cell_calls}"
        return original_new_id(prefix)

    monkeypatch.setattr(store, "new_id", duplicate_cell_id)
    with pytest.raises(Exception):
        store.create_knowhow_table_with_rows(
            "nb-content",
            "Constraint rollback",
            "",
            _knowhow_columns(),
            [["A", "first"], ["B", "second"]],
            "user-content",
        )
    assert store.list_knowhow_tables("nb-content") == before


def test_knowhow_atomic_append_rolls_back_exact_state_for_missing_asset_and_row2_error(
    content_harness, monkeypatch
):
    store = content_harness.knowhow
    table_id = store.create_knowhow_table_with_rows(
        "nb-content",
        "Atomic append",
        "",
        _knowhow_columns(),
        [["seed", "old"]],
        "user-content",
    )
    table = store.get_knowhow_table(table_id)
    topic_id, procedure_id = [column["id"] for column in table["columns"]]
    before = store.get_knowhow_table(table_id)
    missing = "asset-missing-append"
    with pytest.raises(ValueError, match=missing):
        store.append_knowhow_rows(
            table_id,
            [
                {topic_id: "A", procedure_id: "first"},
                {topic_id: "B", procedure_id: f"![missing](asset://{missing})"},
            ],
        )
    assert store.get_knowhow_table(table_id) == before

    original_new_id = store.new_id
    cell_calls = 0

    def duplicate_cell_id(prefix: str) -> str:
        nonlocal cell_calls
        if prefix == "khcel":
            cell_calls += 1
            # Row 1's two cells succeed; row 2's first cell collides with row 1.
            return f"khcel-forced-append-{1 if cell_calls >= 3 else cell_calls}"
        return original_new_id(prefix)

    monkeypatch.setattr(store, "new_id", duplicate_cell_id)
    with pytest.raises(Exception):
        store.append_knowhow_rows(
            table_id,
            [
                {topic_id: "C", procedure_id: "third"},
                {topic_id: "D", procedure_id: "fourth"},
            ],
        )
    assert store.get_knowhow_table(table_id) == before


def test_knowhow_atomic_create_and_append_preserve_order_content_and_one_seq_bump(
    content_harness,
):
    store = content_harness.knowhow
    table_id = store.create_knowhow_table_with_rows(
        "nb-content",
        "Ordered batch",
        "",
        _knowhow_columns(),
        [["A", "first"], ["B", "second"]],
        "user-content",
    )
    imported = store.get_knowhow_table(table_id)
    assert [row["position"] for row in imported["rows"]] == [0, 1]
    assert _cell_values(imported) == [["A", "first"], ["B", "second"]]
    assert imported["mutation_seq"] == 1
    topic_id, procedure_id = [column["id"] for column in imported["columns"]]

    row_ids = store.append_knowhow_rows(
        table_id,
        [
            {topic_id: "C", procedure_id: "third"},
            {topic_id: "D", procedure_id: "fourth"},
        ],
    )
    final = store.get_knowhow_table(table_id)
    assert row_ids == [row["id"] for row in final["rows"][2:]]
    assert [row["position"] for row in final["rows"]] == [0, 1, 2, 3]
    assert _cell_values(final) == [
        ["A", "first"],
        ["B", "second"],
        ["C", "third"],
        ["D", "fourth"],
    ]
    assert final["mutation_seq"] == 2


def test_knowhow_asset_delta_allows_preserved_legacy_dead_ref_and_checks_each_target(
    content_harness,
):
    store = content_harness.knowhow
    table_id = store.create_knowhow_table_with_rows(
        "nb-content",
        "Legacy refs",
        "",
        _knowhow_columns(),
        [["A", "one"], ["B", "two"]],
        "user-content",
    )
    table = store.get_knowhow_table(table_id)
    procedure_id = table["columns"][1]["id"]
    row_a, row_b = [row["id"] for row in table["rows"]]
    mark = "%s"
    legacy = "asset-legacy-dead"
    missing = "asset-new-dead"
    old_ref = f"![legacy](asset://{legacy})"
    with content_harness.database.write() as connection:
        connection.execute(
            f"UPDATE knowhow_cells SET content_md={mark} "
            f"WHERE row_id={mark} AND column_id={mark}",
            (old_ref, row_a, procedure_id),
        )

    preserved = f"{old_ref} edited text"
    store.update_knowhow_cell(row_a, procedure_id, preserved)
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cell(
            row_a,
            procedure_id,
            f"{preserved} ![new](asset://{missing})",
        )
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cell(
            row_a, procedure_id, f"![replacement](asset://{missing})"
        )
    assert store.get_knowhow_table(table_id)["rows"][0]["cells"][procedure_id] == preserved

    # The shared write would preserve the ref on row A but add it to row B;
    # per-target delta union must therefore validate and reject it.
    with pytest.raises(ValueError, match=legacy):
        store.update_knowhow_cells(
            [row_a, row_b], procedure_id, f"{old_ref} merged"
        )
    with content_harness.database.write() as connection:
        connection.execute(
            f"UPDATE knowhow_cells SET content_md={mark} "
            f"WHERE row_id={mark} AND column_id={mark}",
            (old_ref, row_b, procedure_id),
        )
    store.update_knowhow_cells(
        [row_a, row_b], procedure_id, f"{old_ref} merged"
    )
    assert [
        row["cells"][procedure_id]
        for row in store.get_knowhow_table(table_id)["rows"]
    ] == [f"{old_ref} merged", f"{old_ref} merged"]

    bulk_before = f"{old_ref} merged"
    bulk_after = f"{old_ref} bulk"
    bulk_result = store.update_knowhow_cells_bulk_guarded(
        "nb-content",
        [
            (table_id, row_a, procedure_id, bulk_before, bulk_after),
            (table_id, row_b, procedure_id, bulk_before, bulk_after),
        ],
    )
    assert bulk_result["written"] == [(row_a, procedure_id), (row_b, procedure_id)]
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cells_bulk_guarded(
            "nb-content",
            [
                (
                    table_id,
                    row_a,
                    procedure_id,
                    bulk_after,
                    f"{bulk_after} ![new](asset://{missing})",
                )
            ],
        )

    atomic_after = f"{old_ref} atomic"
    atomic_result = store.update_knowhow_cells_guarded_atomic(
        "nb-content",
        [
            (table_id, row_a, procedure_id, bulk_after, atomic_after),
            (table_id, row_b, procedure_id, bulk_after, atomic_after),
        ],
    )
    assert atomic_result["conflict"] is False
    with pytest.raises(ValueError, match=missing):
        store.update_knowhow_cells_guarded_atomic(
            "nb-content",
            [
                (
                    table_id,
                    row_a,
                    procedure_id,
                    atomic_after,
                    f"![replacement](asset://{missing})",
                )
            ],
        )
    assert [
        row["cells"][procedure_id]
        for row in store.get_knowhow_table(table_id)["rows"]
    ] == [atomic_after, atomic_after]


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


def _seed_catalog(database) -> None:
    mark = "%s"
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
    database: object
    ask: object
    report: object
    memory: object
    knowhow: object
    transfer: object


@pytest.fixture
def content_harness(request) -> ContentHarness:
    seams = _seams()
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 42
    _seed_catalog(database)
    yield ContentHarness(
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


def test_knowhow_complete_enumeration_matches_persisted_contract(content_harness):
    """The PostgreSQL adapter retains physical rows across bounded complete-set pages."""
    store = content_harness.knowhow
    table_id = store.create_knowhow_table(
        "nb-content", "Enumeration", "", _knowhow_columns(), "user-content"
    )
    table = store.get_knowhow_table(table_id)
    topic_id, procedure_id = [column["id"] for column in table["columns"]]
    for index in range(100):
        store.add_knowhow_row(
            table_id,
            {
                topic_id: f"topic-{index}",
                procedure_id: f"method-{index}" if index != 50 else "  ",
            },
        )

    catalog = store.knowhow_enumeration_catalog(
        "nb-content", limit=8, query="Enumeration"
    )
    assert catalog["known_tables"] == 1
    assert catalog["known_total_rows"] == 100
    assert catalog["tables"] == [{
        "id": table_id,
        "title": "Enumeration",
        "mutation_seq": 0,
        "row_count": 100,
        "enumeration_seq": 101,
    }]
    assert len(catalog["fingerprint"]) == 4

    cursor = None
    seen: list[dict] = []
    while True:
        page = store.enumerate_knowhow_rows(
            "nb-content",
            table_ids=[table_id],
            column_ids=[procedure_id],
            cursor=cursor,
        )
        assert page["page_size"] == 25
        assert page["counts"] == {
            "scope_total_rows": 100,
            "scope_nonempty_rows": 100,
            "page_rows": len(page["rows"]),
        }
        assert page["tables"] == [
            {
                "id": table_id,
                "title": "Enumeration",
                "description": "",
                "mutation_seq": 0,
                "enumeration_seq": 101,
                "row_count": 100,
                "columns": [
                    {"id": topic_id, "name": "Topic", "role": "anchor", "position": 0},
                    {
                        "id": procedure_id,
                        "name": "Procedure",
                        "role": "procedure",
                        "position": 1,
                    },
                ],
            }
        ]
        seen.extend(page["rows"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]

    assert len(seen) == len({row["id"] for row in seen}) == 100
    assert [row["cells"][procedure_id] for row in seen] == [
        f"method-{index}" if index != 50 else "  " for index in range(100)
    ]


def test_ask_and_report_state_shapes_match_persisted_golden(content_harness):
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
    # A published report is terminal: a late cancellation must lose the CAS
    # instead of reversing ``done`` after completion observers were admitted.
    assert content_harness.report.cancel_report("nb-content", report_id) is False
    content_harness.report.update_report(
        "nb-content", report_id, status="done", content_md="# too late"
    )
    completed = content_harness.report.get_report("nb-content", report_id)
    assert completed["status"] == "done"
    assert completed["content_md"] == "# State"


def test_recent_user_ask_traces_scopes_to_the_reading_member(content_harness):
    """Agentic Memory P1 (T5): the overlay chain's ONE read, on PostgreSQL.

    ⚠ This is a privacy boundary, not an audit filter — the result feeds blocks
    only the reading member can see, so a foreign row would be summarised into
    them with no error anywhere. ``created_by = %s`` is written into BOTH
    statements (``test_agent_profile_isolation_guard.py`` pins that statically
    in both backends); this pins that PostgreSQL actually honours it, and that
    it projects the SAME narrow shape SQLite does — this backend hands the
    shared projection a ``jsonb``-decoded dict where SQLite hands it TEXT, and
    an understanding block whose contents depended on the database would be a
    long way from obvious.
    """
    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at,username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            ("user-other", "other@example.test", "Other", "user", "active",
             NOW, NOW, "o00123456", "", "", 0),
        )
    mine, _conv = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="mine?"), "reasoning", "user-content"
    )
    theirs, _conv2 = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="theirs?"), "reasoning", "user-other"
    )
    content_harness.ask.append_trace(
        "nb-content", mine,
        {"step_type": "retrieve", "summary": "mine step",
         "duration_ms": 11, "detail": {"count": 0, "error": "internal text"}},
        "user-content",
    )
    content_harness.ask.append_trace(
        "nb-content", theirs,
        {"step_type": "retrieve", "summary": "their step", "detail": {"count": 3}},
        "user-other",
    )

    rows = content_harness.ask.recent_user_ask_traces(
        "nb-content", "user-content", job_limit=40, step_limit=600
    )

    assert [row["question"] for row in rows] == ["mine?"]
    assert rows[0]["job_id"] == mine
    # Projected, not passed through: no ``error`` text, no other detail keys.
    assert rows[0]["steps"] == [
        {"step_type": "retrieve", "summary": "mine step",
         "duration_ms": 11, "count": 0}
    ]
    assert content_harness.ask.recent_user_ask_traces(
        "nb-content", "user-other", job_limit=40, step_limit=600
    )[0]["job_id"] == theirs
    assert content_harness.ask.recent_user_ask_traces(
        "nb-content", "user-nobody", job_limit=40, step_limit=600
    ) == []


def test_recent_user_ask_traces_step_cap_drops_the_oldest_job_first(content_harness):
    """Agentic Memory P1 (T5 repair round): the ``step_limit`` cap must bite on
    the OLDEST included job's tail, not on whichever job's opaque id happens to
    sort last lexicographically. Two jobs, four steps each, ``step_limit=3``:
    the newer job must keep all 3 of the steps the cap allows, and the older
    job must be truncated to zero — the reverse of what ``ORDER BY t.job_id
    ASC`` would have produced.
    """
    older, _conv = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="older?"), "reasoning", "user-content"
    )
    newer, _conv2 = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="newer?"), "reasoning", "user-content"
    )
    with content_harness.database.write() as db:
        db.execute(
            "UPDATE ask_jobs SET created_at=%s WHERE id=%s",
            ("2026-08-10T00:00:00+00:00", older),
        )
        db.execute(
            "UPDATE ask_jobs SET created_at=%s WHERE id=%s",
            ("2026-08-11T00:00:00+00:00", newer),
        )
    for job_id, label in ((older, "older"), (newer, "newer")):
        for n in range(4):
            content_harness.ask.append_trace(
                "nb-content", job_id,
                {"step_type": "retrieve", "summary": f"{label}-{n}",
                 "detail": {"count": n}},
                "user-content",
            )

    rows = content_harness.ask.recent_user_ask_traces(
        "nb-content", "user-content", job_limit=2, step_limit=3
    )

    assert [row["question"] for row in rows] == ["newer?", "older?"]
    assert [len(row["steps"]) for row in rows] == [3, 0]


def test_recent_user_report_traces_scopes_to_the_reading_member(content_harness):
    """Agentic Memory P2 (T4): the overlay chain's SECOND read, on PostgreSQL.

    ⚠ Same privacy-boundary contract as ``recent_user_ask_traces`` above —
    ``created_by = %s`` is written into BOTH statements (pinned statically by
    ``test_agent_profile_isolation_guard.py`` over the now two-element
    ``TRACE_READ_METHODS``); this pins that PostgreSQL actually honours it
    AND projects the SAME shape SQLite does, even though the two backends
    extract ``sections_json[i].attempted[j].{query,failed}`` through
    completely different SQL (``jsonb_array_elements``/``->>`` here, nested
    ``json_each``/``json_extract`` there) — an understanding block whose
    contents depended on which database ran it would be a long way from
    obvious.

    ⚠ The two MALFORMED shapes are pinned here too (P2-T4 fix round, item 7).
    They are the reason the SQLite side had to move off ``json_type()``: a
    string section element and a string attempt element both raise
    ``malformed JSON`` there, taking the member's WHOLE sample with them.
    ``jsonb`` is parsed at write time so PostgreSQL never raised — but that
    is exactly why it needs a test: without one, the two backends could
    diverge on corrupt data and nothing would say so.
    """
    from app.repositories.postgres._store_utils import jsonb

    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at,username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            ("user-other", "other@example.test", "Other", "user", "active",
             NOW, NOW, "o00123456", "", "", 0),
        )
        connection.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,status,"
            "created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("rep-mine", "nb-content", "mine?", jsonb([
                {"title": "s0", "attempted": [
                    {"query": "a", "new": 1, "tries": 1},
                    {"query": "b", "new": 0, "tries": 1},
                ]},
                {"title": "s1", "attempted": [
                    {"query": "c", "new": 2, "tries": 1, "failed": True},
                ]},
            ]), "done", "user-content", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,status,"
            "created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("rep-theirs", "nb-content", "theirs?", jsonb([
                {"title": "s0", "attempted": [{"query": "x", "new": 0, "tries": 1}]},
            ]), "done", "user-other", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,status,"
            "created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("rep-notdone", "nb-content", "not done?", jsonb([
                {"title": "s0", "attempted": [{"query": "y", "new": 0, "tries": 1}]},
            ]), "failed", "user-content", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,status,"
            "created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("rep-str-section", "nb-content", "string section?",
             jsonb(["hello"]), "done", "user-content",
             "2026-07-21T00:00:00+00:00", "2026-07-21T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO reports(id,notebook_id,question,sections_json,status,"
            "created_by,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            ("rep-str-attempt", "nb-content", "string attempt?",
             jsonb([{"attempted": ["oops"]}]), "done", "user-content",
             "2026-07-22T00:00:00+00:00", "2026-07-22T00:00:00+00:00"),
        )

    rows = content_harness.ask.recent_user_report_traces(
        "nb-content", "user-content", report_limit=10, attempt_limit=200
    )

    # codex #524 R15:排序按完成序(updated_at),两条畸形行的 updated_at
    # 因此与各自 created_at 对齐,预期顺序不变。
    assert [row["question"] for row in rows] == [
        "mine?", "string attempt?", "string section?"
    ]
    assert rows[0]["report_id"] == "rep-mine"
    # Projected, not passed through: only query/failed survive, in section-
    # then-attempt order, exactly the shape ``project_report_attempt``
    # produces. ``new`` is dropped at the projection — it measures additions
    # to the run's shared candidate pool, not this direction's results, and
    # the P2-T4 fix round removed every consumer of it.
    assert rows[0]["attempts"] == [
        {"query": "a", "failed": False},
        {"query": "b", "failed": False},
        {"query": "c", "failed": True},
    ]
    # Malformed shapes degrade, they do not poison: the string-attempt row
    # yields one attempt with no wording, the string-section row yields none,
    # and the healthy report above is untouched.
    assert rows[1]["attempts"] == [{"query": "", "failed": False}]
    assert rows[2]["attempts"] == []
    assert content_harness.ask.recent_user_report_traces(
        "nb-content", "user-other", report_limit=10, attempt_limit=200
    )[0]["report_id"] == "rep-theirs"
    assert content_harness.ask.recent_user_report_traces(
        "nb-content", "user-nobody", report_limit=10, attempt_limit=200
    ) == []


def test_recent_user_ask_languages_scopes_to_the_asking_member(content_harness):
    """Agentic Memory P3 (B-Profile, T7): the THIRD trace read, on PostgreSQL.

    ⚠ Same privacy-boundary contract as the two reads above — ``created_by =
    %s`` is written into the statement (pinned statically by
    ``test_agent_profile_isolation_guard.py`` over the now three-element
    ``TRACE_READ_METHODS``) — but this method's projection is narrower still:
    every row is EXACTLY ``{"language": ...}``, never the question text
    itself, because it feeds a deterministic inference job that must never
    see anyone's question wording (see
    ``AskStateStorePort.recent_user_ask_languages``'s own docstring).

    Also pins the ``status = 'done'`` gate: a job that never reached the
    successful-delivery path (mirroring the ``note_ask_completed`` trigger's
    own gate) must not contribute a row, even though its question text is
    perfectly readable.
    """
    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at,username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            ("user-other", "other@example.test", "Other", "user", "active",
             NOW, NOW, "o00456789", "", "", 0),
        )
    mine, _conv = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="这是中文提问"), "reasoning", "user-content"
    )
    content_harness.ask.finish_job(mine, "done")
    unfinished, _conv2 = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="never finished"), "reasoning", "user-content"
    )
    content_harness.ask.finish_job(unfinished, "failed", error="boom")
    theirs, _conv3 = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="their english question"), "reasoning",
        "user-other",
    )
    content_harness.ask.finish_job(theirs, "done")

    rows = content_harness.ask.recent_user_ask_languages("user-content", limit=30)

    # Projected, not passed through: a "done" job for this member contributes
    # exactly one row shaped {"language": ...}, no question text anywhere —
    # the failed job and the other member's job are both excluded.
    assert rows == [{"language": "zh"}]
    assert set().union(*[set(row.keys()) for row in rows]) == {"language"}
    assert content_harness.ask.recent_user_ask_languages(
        "user-other", limit=30
    ) == [{"language": "en"}]
    assert content_harness.ask.recent_user_ask_languages(
        "user-nobody", limit=30
    ) == []


def test_ask_cleanup_preserves_other_running_job_and_terminal_states(
    content_harness,
):
    first = AskRequest(question="first")
    first_job, conversation_id = content_harness.ask.begin_durable_job(
        "nb-content", first, "chunk", "user-content"
    )
    second = AskRequest(question="second", conversation_id=conversation_id)
    second_job, continued_id = content_harness.ask.begin_durable_job(
        "nb-content", second, "chunk", "user-content"
    )
    assert continued_id == conversation_id
    assert content_harness.ask.cancel_running_job(
        first_job, "user-content"
    )["cancelled"] is True
    content_harness.ask.cleanup_empty_conversation(conversation_id)
    assert content_harness.ask.ask_job_status(second_job)["status"] == "running"
    assert content_harness.ask.get_conversation(conversation_id).id == conversation_id

    answer = AskResponse(
        answer="second answer", conclusion="second answer", citations=[], anchors=[]
    )
    answer_id = content_harness.ask.save_answer_for_job(
        second_job,
        "nb-content",
        conversation_id,
        "second",
        answer,
        "user-content",
    )
    assert answer_id
    content_harness.ask.finish_job(second_job, "cancelled")
    done = content_harness.ask.ask_job_status(second_job)
    assert done["status"] == "done"
    assert done["answer_id"] == answer_id

    failed_request = AskRequest(question="failed")
    failed_job, _ = content_harness.ask.begin_durable_job(
        "nb-content", failed_request, "chunk", "user-content"
    )
    content_harness.ask.finish_job(failed_job, "failed", error="original")
    content_harness.ask.finish_job(failed_job, "cancelled", error="replacement")
    content_harness.ask.finish_job(failed_job, "done", answer_id="not-real")
    failed = content_harness.ask.ask_job_status(failed_job)
    assert failed["status"] == "failed"
    assert failed["answer_id"] == ""
    assert failed["error"] == "original"

    cancelled_request = AskRequest(question="cancelled")
    cancelled_job, _ = content_harness.ask.begin_durable_job(
        "nb-content", cancelled_request, "chunk", "user-content"
    )
    content_harness.ask.cancel_running_job(cancelled_job, "user-content")
    content_harness.ask.finish_job(cancelled_job, "done", answer_id="not-real")
    cancelled_job_state = content_harness.ask.ask_job_status(cancelled_job)
    assert cancelled_job_state["status"] == "cancelled"
    assert cancelled_job_state["answer_id"] == ""


def _sync_ask_service(content_harness) -> AskService:
    service = AskService.__new__(AskService)
    service.ask_state = content_harness.ask
    service.current_user_id = lambda: "user-content"
    service.cancellations = AskCancellationRegistry()
    service.notebooks = SimpleNamespace(get_notebook=lambda notebook_id: notebook_id)
    return service


class _PausedJobPreparation:
    """Pause either the legacy or durable preparation entry at one barrier."""

    def __init__(self, delegate, entered: threading.Event, release: threading.Event):
        self.delegate = delegate
        self.entered = entered
        self.release = release

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def _wait(self) -> None:
        self.entered.set()
        assert self.release.wait(timeout=5)

    def prepare_turn(self, *args, **kwargs):
        self._wait()
        return self.delegate.prepare_turn(*args, **kwargs)

    def prepare_turn_for_job(self, *args, **kwargs):
        self._wait()
        return self.delegate.prepare_turn_for_job(*args, **kwargs)


class _ExecutorSubmitter:
    def __init__(self, executor: ThreadPoolExecutor):
        self.executor = executor
        self.future = None

    def submit(self, fn, *args, **kwargs):
        kwargs.pop("name", None)
        kwargs.pop("notify_pending", None)
        self.future = self.executor.submit(fn, *args, **kwargs)
        return self.future


@pytest.mark.parametrize("surface", ("sync", "stream"))
def test_cancel_before_job_bound_prepare_never_creates_a_replacement_conversation(
    content_harness, surface,
):
    """Cancellation can win after the engine's first in-memory check.

    Preparation must then validate the exact durable job and exact parent; it
    must not fall back to creating a second empty conversation after cancel
    cleanup removed the first one. The same engine path serves sync and stream.
    """
    entered = threading.Event()
    release = threading.Event()
    service = _sync_ask_service(content_harness)
    service.ask_state = _PausedJobPreparation(content_harness.ask, entered, release)
    request = AskRequest(question=f"cancel before prepare ({surface})", mode="chunk")

    with ThreadPoolExecutor(max_workers=1) as executor:
        if surface == "sync":
            future = executor.submit(service.ask_current, "nb-content", request)
            events = None
        else:
            submitter = _ExecutorSubmitter(executor)
            coordinator = AskExecutionCoordinator(
                ask_state=service.ask_state,
                cancellations=service.cancellations,
                job_submitter=submitter,
                event_log=SimpleNamespace(
                    logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None)
                ),
                ask=lambda: service,
            )
            events = coordinator.start(
                "nb-content",
                request,
                SimpleNamespace(id="chunk"),
                user_id="user-content",
            )
            future = submitter.future

        assert entered.wait(timeout=5)
        try:
            assert request.conversation_id
            active = content_harness.ask.get_conversation(
                request.conversation_id
            ).active_job
            assert active is not None
            job_id = active.job_id
            assert service.cancel_job(job_id, "user-content") == {
                "status": "cancelled",
                "job_id": job_id,
            }
            with pytest.raises(KeyError):
                content_harness.ask.get_conversation(request.conversation_id)
        finally:
            release.set()

        if surface == "sync":
            with pytest.raises(AskCancelled):
                future.result(timeout=5)
        else:
            delivered = []
            while True:
                event = events.get(timeout=5)
                if event is None:
                    break
                delivered.append(event)
            future.result(timeout=5)
            assert delivered[0] == {
                "event": "started",
                "job_id": job_id,
                "conversation_id": request.conversation_id,
            }
            assert any(event["event"] == "cancelled" for event in delivered)
            assert not any(event["event"] == "final" for event in delivered)

    assert content_harness.ask.ask_job_status(job_id)["status"] == "cancelled"
    assert content_harness.ask.list_conversations(
        "nb-content", "user-content"
    ) == []
    mark = "%s"
    with content_harness.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE notebook_id=" + mark,
            ("nb-content",),
        ).fetchone()["n"] == 0


def test_conversation_activity_order_preserves_subsecond_recency(
    content_harness, monkeypatch,
):
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "NOW", "2026-07-24T10:05:00.100001+00:00")
    job_a, conversation_a = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="conversation A"), "chunk", "user-content"
    )
    monkeypatch.setattr(module, "NOW", "2026-07-24T10:05:00.100002+00:00")
    job_b, conversation_b = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="conversation B"), "chunk", "user-content"
    )
    assert [item.id for item in content_harness.ask.list_conversations(
        "nb-content", "user-content"
    )[:2]] == [conversation_b, conversation_a]

    monkeypatch.setattr(module, "NOW", "2026-07-24T10:05:00.100003+00:00")
    job_a2, continued = content_harness.ask.begin_durable_job(
        "nb-content",
        AskRequest(question="follow up A", conversation_id=conversation_a),
        "chunk",
        "user-content",
    )
    assert continued == conversation_a
    assert [item.id for item in content_harness.ask.list_conversations(
        "nb-content", "user-content"
    )[:2]] == [conversation_a, conversation_b]
    assert content_harness.ask.get_conversation(
        conversation_a
    ).active_job.job_id == job_a2

    for job_id in (job_a, job_b, job_a2):
        content_harness.ask.finish_job(job_id, "done")


def test_conversation_activity_order_compares_absolute_instants_across_offsets(
    content_harness, monkeypatch,
):
    module = sys.modules[__name__]
    # The second wall-clock label is lexically smaller, but it is 200 ms later
    # in UTC across a daylight-saving fallback boundary.
    monkeypatch.setattr(module, "NOW", "2026-11-01T01:59:59.900000-04:00")
    job_a, conversation_a = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="before fallback"), "chunk", "user-content"
    )
    monkeypatch.setattr(module, "NOW", "2026-11-01T01:00:00.100000-05:00")
    job_b, conversation_b = content_harness.ask.begin_durable_job(
        "nb-content", AskRequest(question="after fallback"), "chunk", "user-content"
    )

    assert [item.id for item in content_harness.ask.list_conversations(
        "nb-content", "user-content"
    )[:2]] == [conversation_b, conversation_a]
    for job_id in (job_a, job_b):
        content_harness.ask.finish_job(job_id, "done")


def test_bulk_delete_skips_an_old_conversation_with_a_running_job(content_harness):
    request = AskRequest(question="running old conversation", mode="chunk")
    job_id, conversation_id = content_harness.ask.begin_durable_job(
        "nb-content", request, "chunk", "user-content"
    )
    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            "UPDATE conversations SET updated_at=" + mark + " WHERE id=" + mark,
            ("2000-01-01T00:00:00+00:00", conversation_id),
        )

    result = content_harness.ask.bulk_delete_conversations(
        "nb-content", 1, "user-content"
    )
    assert result.deleted == 0
    assert result.deleted_ids == []
    assert content_harness.ask.get_conversation(conversation_id).id == conversation_id
    assert content_harness.ask.ask_job_status(job_id)["status"] == "running"


def test_explicit_conversation_delete_refuses_running_and_purges_terminal_job_state(
    content_harness,
):
    running_request = AskRequest(question="still running", mode="reasoning")
    running_job, running_conversation = content_harness.ask.begin_durable_job(
        "nb-content", running_request, "reasoning", "user-content"
    )
    content_harness.ask.append_trace(
        "nb-content",
        running_job,
        {"step_type": "retrieve", "summary": "private running trace"},
        "user-content",
    )
    with pytest.raises(RuntimeError, match="conversation has a running Ask job"):
        content_harness.ask.delete_conversation(running_conversation)
    assert content_harness.ask.get_conversation(running_conversation).active_job is not None
    assert content_harness.ask.ask_job_status(running_job)["status"] == "running"

    terminal_request = AskRequest(question="terminal private question", mode="reasoning")
    terminal_job, terminal_conversation = content_harness.ask.begin_durable_job(
        "nb-content", terminal_request, "reasoning", "user-content"
    )
    content_harness.ask.append_trace(
        "nb-content",
        terminal_job,
        {"step_type": "answer", "summary": "private terminal trace"},
        "user-content",
    )
    response = AskResponse(
        answer="terminal answer",
        conclusion="terminal answer",
        conversation_id=terminal_conversation,
        citations=[],
        anchors=[],
    )
    answer_id = content_harness.ask.save_answer_for_job(
        terminal_job,
        "nb-content",
        terminal_conversation,
        terminal_request.question,
        response,
        "user-content",
    )
    assert answer_id

    content_harness.ask.delete_conversation(terminal_conversation)
    with pytest.raises(KeyError):
        content_harness.ask.get_conversation(terminal_conversation)
    with pytest.raises(KeyError):
        content_harness.ask.ask_job_detail(terminal_job)
    mark = "%s"
    with content_harness.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE conversation_id=" + mark,
            (terminal_conversation,),
        ).fetchone()["n"] == 0
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM ask_trace_steps WHERE job_id=" + mark,
            (terminal_job,),
        ).fetchone()["n"] == 0


def test_bulk_delete_returns_only_actual_ids_and_purges_their_terminal_jobs(
    content_harness,
):
    def completed(question: str) -> tuple[str, str]:
        request = AskRequest(question=question, mode="chunk")
        job_id, conversation_id = content_harness.ask.begin_durable_job(
            "nb-content", request, "chunk", "user-content"
        )
        content_harness.ask.append_trace(
            "nb-content",
            job_id,
            {"step_type": "answer", "summary": question},
            "user-content",
        )
        response = AskResponse(
            answer=question,
            conclusion=question,
            conversation_id=conversation_id,
            citations=[],
            anchors=[],
        )
        assert content_harness.ask.save_answer_for_job(
            job_id,
            "nb-content",
            conversation_id,
            question,
            response,
            "user-content",
        )
        return job_id, conversation_id

    old_job, old_conversation = completed("old terminal")
    fresh_job, fresh_conversation = completed("fresh terminal")
    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            "UPDATE conversations SET updated_at=" + mark + " WHERE id=" + mark,
            ("2000-01-01T00:00:00+00:00", old_conversation),
        )

    result = content_harness.ask.bulk_delete_conversations(
        "nb-content", 1, "user-content"
    )
    assert result.deleted == 1
    assert result.deleted_ids == [old_conversation]
    with pytest.raises(KeyError):
        content_harness.ask.ask_job_detail(old_job)
    assert content_harness.ask.ask_job_detail(fresh_job)["conversation_id"] == (
        fresh_conversation
    )
    assert content_harness.ask.get_conversation(fresh_conversation).turn_count == 1


def test_sync_ask_running_job_protects_conversation_until_atomic_final_save(
    content_harness,
):
    """The non-streaming production wrapper must use the durable job lease.

    The engine is paused after the real prepare_turn. A cleanup from a
    cancelled/failed sibling runs to completion before final save; it must see
    this synchronous Ask's running job and retain the conversation.
    """
    service = _sync_ask_service(content_harness)
    prepared = threading.Event()
    release = threading.Event()
    captured: dict[str, str] = {}

    def engine(
        notebook_id, payload, *, user_id, job_id="", cancel_event=None,
        on_trace=None,
    ):
        del on_trace
        turn = content_harness.ask.prepare_turn(
            notebook_id, payload.conversation_id, payload.question, user_id
        )
        captured.update(job_id=job_id, conversation_id=turn.conversation_id)
        prepared.set()
        assert release.wait(timeout=5)
        response = AskResponse(
            answer="sync answer",
            conclusion="sync answer",
            conversation_id=turn.conversation_id,
            citations=[],
            anchors=[],
        )
        if job_id:
            answer_id = content_harness.ask.save_answer_for_job(
                job_id,
                notebook_id,
                turn.conversation_id,
                payload.question,
                response,
                user_id,
            )
            if answer_id is None:
                raise AskCancelled()
        else:
            answer_id = content_harness.ask.save_answer(
                notebook_id,
                turn.conversation_id,
                payload.question,
                response,
                user_id,
            )
        response.answer_id = answer_id
        return response

    service.ask = engine
    request = AskRequest(question="synchronous production path", mode="chunk")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.ask_current, "nb-content", request)
        assert prepared.wait(timeout=5)
        content_harness.ask.cleanup_empty_conversation(captured["conversation_id"])
        release.set()
        response = future.result(timeout=5)

    detail = content_harness.ask.get_conversation(captured["conversation_id"])
    assert response.conversation_id == captured["conversation_id"]
    assert detail.turn_count == 1
    assert detail.turns[0].answer_id == response.answer_id
    assert detail.active_job is None
    assert "job_id" not in response.model_dump()
    assert content_harness.ask.ask_job_status(captured["job_id"])["status"] == "done"
    mark = "%s"
    with content_harness.database.connect() as connection:
        orphan_count = connection.execute(
            "SELECT COUNT(*) AS n FROM answers a LEFT JOIN conversations c "
            "ON c.id=a.conversation_id WHERE a.id=" + mark + " AND c.id IS NULL",
            (response.answer_id,),
        ).fetchone()["n"]
    assert orphan_count == 0


def test_sync_ask_failure_and_explicit_cancel_leave_no_empty_conversation(
    content_harness,
):
    service = _sync_ask_service(content_harness)
    failed: dict[str, str] = {}

    def failing_engine(
        notebook_id, payload, *, user_id, job_id="", cancel_event=None,
        on_trace=None,
    ):
        del cancel_event, on_trace
        turn = content_harness.ask.prepare_turn(
            notebook_id, payload.conversation_id, payload.question, user_id
        )
        failed.update(job_id=job_id, conversation_id=turn.conversation_id)
        raise RuntimeError("sync inference failed")

    service.ask = failing_engine
    with pytest.raises(RuntimeError, match="sync inference failed"):
        service.ask_current(
            "nb-content", AskRequest(question="failing sync", mode="chunk")
        )
    assert failed["job_id"]
    assert content_harness.ask.ask_job_status(failed["job_id"])["status"] == "failed"
    with pytest.raises(KeyError):
        content_harness.ask.get_conversation(failed["conversation_id"])

    prepared = threading.Event()
    release = threading.Event()
    cancelled: dict[str, str] = {}

    def cancellable_engine(
        notebook_id, payload, *, user_id, job_id="", cancel_event=None,
        on_trace=None,
    ):
        del on_trace
        turn = content_harness.ask.prepare_turn(
            notebook_id, payload.conversation_id, payload.question, user_id
        )
        cancelled.update(job_id=job_id, conversation_id=turn.conversation_id)
        prepared.set()
        assert release.wait(timeout=5)
        raise_if_cancelled(cancel_event)
        raise AssertionError("cancelled synchronous Ask continued")

    service.ask = cancellable_engine
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.ask_current,
            "nb-content",
            AskRequest(question="cancelled sync", mode="chunk"),
        )
        assert prepared.wait(timeout=5)
        assert cancelled["job_id"]
        state = service.cancel_job(cancelled["job_id"], "user-content")
        assert state == {"status": "cancelled", "job_id": cancelled["job_id"]}
        release.set()
        with pytest.raises(AskCancelled):
            future.result(timeout=5)

    assert content_harness.ask.ask_job_status(cancelled["job_id"])["status"] == "cancelled"
    with pytest.raises(KeyError):
        content_harness.ask.get_conversation(cancelled["conversation_id"])


def test_conversation_bound_answer_save_rejects_a_missing_parent(
    content_harness,
):
    """No FK exists on answers.conversation_id; the store must guard it."""
    response = AskResponse(
        answer="must not orphan",
        conclusion="must not orphan",
        conversation_id="conv-missing-parent",
        citations=[],
        anchors=[],
    )
    with pytest.raises(KeyError, match="conv-missing-parent"):
        content_harness.ask.save_answer(
            "nb-content",
            "conv-missing-parent",
            "orphan attempt",
            response,
            "user-content",
        )
    mark = "%s"
    with content_harness.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM answers WHERE conversation_id=" + mark,
            ("conv-missing-parent",),
        ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_postgres_bulk_delete_cannot_remove_a_concurrently_continued_conversation(
    postgres_database,
):
    """A real continuation holds the parent row while bulk deletion starts.

    The bulk store must block on that parent, re-evaluate its now-fresh
    ``updated_at`` after the continuation commits, and leave the job's final
    answer reachable. Coordination uses transaction/Event barriers and server
    lock introspection; there is no timing sleep.
    """
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    store = PostgresAskStateStore(postgres_database, seams)
    old_turn = store.prepare_turn(
        "nb-content", None, "old conversation", "user-content"
    )
    with postgres_database.write() as connection:
        connection.execute(
            "UPDATE conversations SET updated_at=%s WHERE id=%s",
            ("2000-01-01T00:00:00+00:00", old_turn.conversation_id),
        )

    continuation_database = PostgresDatabase(
        postgres_database.settings, postgres_database.root_dir
    )
    bulk_database = PostgresDatabase(
        postgres_database.settings, postgres_database.root_dir
    )
    continuation_store = PostgresAskStateStore(continuation_database, seams)
    bulk_store = PostgresAskStateStore(bulk_database, seams)
    continuation_uncommitted = threading.Event()
    release_continuation = threading.Event()
    bulk_connected = threading.Event()
    bulk_pid: list[int] = []
    original_continuation_write = continuation_database.write
    original_bulk_write = bulk_database.write

    @contextmanager
    def held_continuation_write(*args, **kwargs):
        with original_continuation_write(*args, **kwargs) as connection:
            yield connection
            continuation_uncommitted.set()
            assert release_continuation.wait(timeout=5)

    @contextmanager
    def observed_bulk_write(*args, **kwargs):
        with original_bulk_write(*args, **kwargs) as connection:
            bulk_pid.append(
                int(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            )
            bulk_connected.set()
            yield connection

    continuation_database.write = held_continuation_write
    bulk_database.write = observed_bulk_write
    request = AskRequest(
        question="continued while deletion starts",
        conversation_id=old_turn.conversation_id,
        mode="chunk",
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            continuation_future = executor.submit(
                continuation_store.begin_durable_job,
                "nb-content",
                request,
                "chunk",
                "user-content",
            )
            assert continuation_uncommitted.wait(timeout=5)
            try:
                bulk_future = executor.submit(
                    bulk_store.bulk_delete_conversations,
                    "nb-content",
                    1,
                    "user-content",
                )
                assert bulk_connected.wait(timeout=5)

                blocked = False
                deadline = time.monotonic() + 5
                with postgres_database.connect() as inspector:
                    while time.monotonic() < deadline:
                        blockers = inspector.execute(
                            "SELECT pg_blocking_pids(%s) AS pids", (bulk_pid[0],)
                        ).fetchone()["pids"]
                        if blockers:
                            blocked = True
                            break
                assert blocked, "bulk deletion never contended on the conversation parent"
            finally:
                release_continuation.set()
            job_id, continued_id = continuation_future.result(timeout=5)
            assert continued_id == old_turn.conversation_id
            bulk_result = bulk_future.result(timeout=5)
            assert bulk_result.deleted == 0
            assert bulk_result.deleted_ids == []
    finally:
        release_continuation.set()
        continuation_database.close()
        bulk_database.close()

    response = AskResponse(
        answer="reachable answer",
        conclusion="reachable answer",
        conversation_id=old_turn.conversation_id,
        citations=[],
        anchors=[],
    )
    answer_id = store.save_answer_for_job(
        job_id,
        "nb-content",
        old_turn.conversation_id,
        request.question,
        response,
        "user-content",
    )
    assert answer_id
    detail = store.get_conversation(old_turn.conversation_id)
    assert detail.turn_count == 1
    assert detail.turns[0].answer_id == answer_id
    assert store.ask_job_status(job_id)["status"] == "done"
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM answers a LEFT JOIN conversations c "
            "ON c.id=a.conversation_id WHERE a.conversation_id IS NOT NULL "
            "AND c.id IS NULL"
        ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_postgres_final_save_and_explicit_delete_do_not_deadlock_or_orphan(
    postgres_database,
):
    """Delete holds the conversation while final save holds the job.

    Delete must observe the still-running immutable view without trying to
    lock the job, reject, and release the parent so job→conversation final save
    completes. Event barriers plus ``pg_blocking_pids`` make the wait explicit.
    """
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    store = PostgresAskStateStore(postgres_database, seams)
    request = AskRequest(question="race final save", mode="chunk")
    job_id, conversation_id = store.begin_durable_job(
        "nb-content", request, "chunk", "user-content"
    )
    response = AskResponse(
        answer="saved",
        conclusion="saved",
        conversation_id=conversation_id,
        citations=[],
        anchors=[],
    )

    delete_database = PostgresDatabase(
        postgres_database.settings, postgres_database.root_dir
    )
    save_database = PostgresDatabase(
        postgres_database.settings, postgres_database.root_dir
    )
    delete_store = PostgresAskStateStore(delete_database, seams)
    save_store = PostgresAskStateStore(save_database, seams)
    delete_locked = threading.Event()
    release_delete = threading.Event()
    save_connected = threading.Event()
    save_pid: list[int] = []
    original_delete_write = delete_database.write
    original_save_write = save_database.write

    class _DeleteBarrierConnection:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def execute(self, query, params=None):
            cursor = self.delegate.execute(query, params)
            if query.startswith(
                "SELECT id FROM conversations WHERE id=%s FOR UPDATE"
            ):
                delete_locked.set()
                assert release_delete.wait(timeout=5)
            return cursor

    @contextmanager
    def observed_delete_write(*args, **kwargs):
        with original_delete_write(*args, **kwargs) as connection:
            yield _DeleteBarrierConnection(connection)

    @contextmanager
    def observed_save_write(*args, **kwargs):
        with original_save_write(*args, **kwargs) as connection:
            save_pid.append(
                int(connection.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            )
            save_connected.set()
            yield connection

    delete_database.write = observed_delete_write
    save_database.write = observed_save_write
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            delete_future = executor.submit(
                delete_store.delete_conversation, conversation_id
            )
            assert delete_locked.wait(timeout=5)
            try:
                save_future = executor.submit(
                    save_store.save_answer_for_job,
                    job_id,
                    "nb-content",
                    conversation_id,
                    request.question,
                    response,
                    "user-content",
                )
                assert save_connected.wait(timeout=5)
                blocked = False
                deadline = time.monotonic() + 5
                with postgres_database.connect() as inspector:
                    while time.monotonic() < deadline:
                        blockers = inspector.execute(
                            "SELECT pg_blocking_pids(%s) AS pids", (save_pid[0],)
                        ).fetchone()["pids"]
                        if blockers:
                            blocked = True
                            break
                assert blocked, "final save never waited on the conversation lease"
            finally:
                release_delete.set()
            with pytest.raises(
                RuntimeError, match="conversation has a running Ask job"
            ):
                delete_future.result(timeout=5)
            answer_id = save_future.result(timeout=5)
    finally:
        release_delete.set()
        delete_database.close()
        save_database.close()

    assert answer_id
    assert store.ask_job_status(job_id)["status"] == "done"
    detail = store.get_conversation(conversation_id)
    assert detail.turn_count == 1
    assert detail.turns[0].answer_id == answer_id
    with postgres_database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) AS n FROM answers a LEFT JOIN conversations c "
            "ON c.id=a.conversation_id WHERE a.id=%s AND c.id IS NULL",
            (answer_id,),
        ).fetchone()["n"] == 0


@pytest.mark.postgres_integration
def test_postgres_report_cancel_commit_beats_blocked_terminal_write(
    postgres_database,
):
    """Two real PG sessions contend on the report row; cancelled stays sticky."""
    import psycopg
    from psycopg.rows import dict_row

    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
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


def test_knowhow_mutation_and_snapshot_shapes_match_persisted_golden(content_harness):
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
        # A hidden projection source plus one chunk: copied chunk rows never
        # reach ChunkStore._insert_chunk_element_rows (the reprojection
        # scheduled after copy_table short-circuits on `old_specs ==
        # new_specs`), so insert_transfer must carry the chunk_elements reverse
        # rows itself. Without a chunk here that whole branch runs empty and
        # proves nothing.
        "source": {
            "id": "src-transfer-copy",
            "notebook_id": "nb-content",
            "title": "knowhow projection",
            "source_type": "knowhow",
            "status": "ready",
            "parse_status": "parsed",
            "created_at": NOW,
            "updated_at": NOW,
        },
        "elements": [],
        "chunks": [
            {
                "id": "chunk-transfer-copy-1",
                "notebook_id": "nb-content",
                "source_id": "src-transfer-copy",
                "text": "Visible knowledge",
                "section_path": "",
                # The duplicate is deliberate: the composite primary key would
                # reject a repeat mid-batch if the shaping helper did not
                # de-duplicate.
                "element_ids": ["el-transfer-a", "el-transfer-b", "el-transfer-a"],
                "created_at": NOW,
            }
        ],
        "chunk_embeddings": [],
    }
    content_harness.transfer.insert_transfer(
        payload,
        {"columns": 2, "rows": 1, "cells": 2, "cell_code": 1},
    )
    copied = content_harness.knowhow.get_knowhow_table("khtbl-transfer-copy")
    assert copied["rows"][0]["cells"]["khcol-transfer-procedure"] == "Visible knowledge"
    with content_harness.database.connect() as connection:
        reverse = connection.execute(
            "SELECT element_id,chunk_id FROM chunk_elements WHERE notebook_id=%s "
            "ORDER BY element_id COLLATE \"C\"",
            ("nb-content",),
        ).fetchall()
    assert [(row["element_id"], row["chunk_id"]) for row in reverse] == [
        ("el-transfer-a", "chunk-transfer-copy-1"),
        ("el-transfer-b", "chunk-transfer-copy-1"),
    ]
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

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
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


def test_memory_revision_provenance_and_json_null_match_persisted_golden(content_harness):
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


def _seed_group_grant(database, user_id: str, username: str) -> None:
    """给 `user_id` 一条指向 nb-content 的 `group` 授权边(经 `grp-pg`)。"""
    mark = "%s"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (user_id, f"{username}@example.test", "Grantee", "user", "active",
             NOW, NOW, username, "", "", 0),
        )
        connection.execute(
            "INSERT INTO groups(id,name,kind,description,created_by,created_at,updated_at) "
            f"VALUES ({','.join([mark] * 7)})",
            ("grp-pg", "PG 组", "project", "", "user-content", NOW, NOW),
        )
        connection.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at,added_by) "
            f"VALUES ({','.join([mark] * 5)})",
            ("grp-pg", user_id, "member", NOW, "user-content"),
        )
        connection.execute(
            "INSERT INTO notebook_grants"
            "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
            f"VALUES ({','.join([mark] * 7)})",
            ("gr-pg", "nb-content", "group", "grp-pg", "viewer", "user-content", NOW),
        )


def test_group_granted_reader_can_keep_private_memory_in_a_shared_notebook(
    content_harness,
):
    """PG 侧三段式带锁站点的**行为**面(P1-T2 质量评审 P1)。

    `test_access_sql_contract.py` 的两段式 allowlist 只数 token 不数站点——把某个
    站点的授权边探测搬走、在另一个站点复制一份,三个计数照样全绿。SQLite 侧有
    `test_memory_service.py` 的同名用例兜底,PG 侧此前没有,而 PG 恰恰是多挂了
    `FOR SHARE` 行锁、实现分叉可能性更高的那一侧。

    三个站点一次跑穿:`create_candidate_with_initial_revision`(写前判定)、
    `create_answer_with_initial_revision`(答案存 Memory 的范围校验)、
    `_lock_memory_aggregate_on`(`transition_with_revision` 的写事务内复核)。撤掉
    组成员之后三条路径必须**当场**全部拒绝——授权是实时判定,不是一次性授予。
    """
    reader = "user-pg-grantee"
    _seed_group_grant(content_harness.database, reader, "p00123456")
    store = content_harness.memory

    def _write(memory_id: str, **extra) -> MemoryWrite:
        return MemoryWrite(
            id=memory_id,
            notebook_id="nb-content",
            created_by=reader,
            # ck_memory_items_origin 只允许 ask_answer/external_agent 两值;
            # "user" 会当场 CheckViolation(修复复核实测)。
            origin="external_agent",
            status="candidate",
            title="组授权可写自己的 Memory",
            content_md="Body",
            tags=[],
            created_at=NOW,
            updated_at=NOW,
            provenance={},
            **extra,
        )

    # ① 写前判定:owner FOR SHARE → 成员 FOR SHARE(未命中)→ 授权边 FOR SHARE。
    item = store.create_candidate_with_initial_revision(
        _write("mem-pg-grant"), reader, "created"
    )
    assert item.created_by == reader

    # ② 答案存 Memory 的范围校验(同一三段式的第二处复刻)。
    request = AskRequest(question="组授权成员能提问吗?")
    _job_id, conversation_id = content_harness.ask.begin_durable_job(
        "nb-content", request, "chunk", reader
    )
    answer_id = content_harness.ask.save_answer(
        "nb-content",
        conversation_id,
        request.question,
        AskResponse(answer="能。", conclusion="能。", citations=[], anchors=[]),
        reader,
    )
    from_answer = store.create_answer_with_initial_revision(
        _write("mem-pg-answer", source_answer_id=answer_id), reader, "created"
    )
    assert from_answer.source_answer_id == answer_id

    # ③ 写事务内复核(_lock_memory_aggregate_on)。
    confirmed = store.transition_with_revision(
        item.id, reader, {"candidate"}, "confirmed",
        fields=None, changed_by=reader, reason="confirmed",
    )
    assert confirmed.status == "confirmed"
    assert store.memory_for_user(item.id, reader).id == item.id

    # 撤销组成员 ⇒ 读路径与两条写路径一起关闭。
    mark = "%s"
    with content_harness.database.write() as connection:
        connection.execute(
            f"DELETE FROM group_members WHERE group_id={mark} AND user_id={mark}",
            ("grp-pg", reader),
        )
    with pytest.raises(KeyError):
        store.memory_for_user(item.id, reader)
    with pytest.raises(KeyError):
        store.transition_with_revision(
            item.id, reader, {"confirmed"}, "archived",
            fields=None, changed_by=reader, reason="revoked",
        )
    with pytest.raises(PermissionError):
        store.create_candidate_with_initial_revision(
            _write("mem-pg-after-revoke"), reader, "created"
        )


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
        mark = "%s"
        assert connection.execute(
            f"SELECT 1 FROM memory_items WHERE id={mark}", (write.id,)
        ).fetchone() is None


def test_memory_agent_token_round_trip_uses_connection_cursor_batch(content_harness):
    """Token allowlists work on psycopg connections without ``executemany``."""
    store = content_harness.memory
    profile = store.create_agent_profile(
        "user-content", "MCP conformance", "PostgreSQL token issuance"
    )

    issued = store.create_agent_token(
        "agent-token-content",
        "user-content",
        profile.id,
        "sha256-token-hash",
        ["knowledge:read", "memory:read"],
        "nb-content",
        ["nb-content"],
        "2026-08-01T00:00:00+00:00",
    )

    assert issued.agent_profile_id == profile.id
    assert issued.default_notebook_id == "nb-content"
    assert issued.notebook_ids == ["nb-content"]
    assert issued.scopes == ["knowledge:read", "memory:read"]
    assert store.list_agent_tokens("user-content") == [issued]


@pytest.mark.postgres_integration
def test_postgres_memory_search_filters_scope_before_candidate_limit(
    postgres_database,
):
    """Other owners cannot crowd a valid hit out of the bounded lexical pool."""
    from app.repositories.postgres.migrator import PostgresMigrator
    from psycopg.types.json import Jsonb

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
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


@pytest.mark.postgres_integration
def test_postgres_memory_search_total_is_exact_beyond_candidate_page(
    postgres_database,
):
    """Filtered totals are exact while each result page remains bounded."""
    from app.repositories.postgres.migrator import PostgresMigrator
    from psycopg.types.json import Jsonb

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    store = PostgresMemoryStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    rows = [
        (
            f"mem-exact-{index:03d}",
            "nb-content",
            "user-content",
            "external_agent",
            "confirmed",
            f"exact-total-token {index:03d}",
            "exact-total-token",
            Jsonb([]),
            NOW,
            NOW,
        )
        for index in range(225)
    ]
    with postgres_database.write() as connection:
        connection.cursor().executemany(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,tags_json,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            rows,
        )
        connection.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,tags_json,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "mem-exact-excluded",
                "nb-content",
                "user-content",
                "ask_answer",
                "confirmed",
                "exact-total-token excluded",
                "exact-total-token",
                Jsonb([]),
                NOW,
                NOW,
            ),
        )

    first = store.list_memories(
        "user-content",
        notebook_id="nb-content",
        status="confirmed",
        origin="external_agent",
        query="exact-total-token",
        offset=0,
        limit=50,
    )
    last = store.list_memories(
        "user-content",
        notebook_id="nb-content",
        status="confirmed",
        origin="external_agent",
        query="exact-total-token",
        offset=200,
        limit=50,
    )
    assert first.total_count == last.total_count == 225
    assert len(first.items) == 50
    assert len(last.items) == 25
    assert {item.id for item in first.items}.isdisjoint(item.id for item in last.items)


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
    mark = "%s"
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

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    sources = SourceStore(postgres_database, now=seams.now)
    chunks = ChunkStore(postgres_database)
    knowledge = KnowledgeStore(postgres_database, seams)
    vectors = EmbeddingStore(write=postgres_database.write)
    projector = KnowhowProjector(
        settings=Settings(database_url="postgresql://unused/unused"),
        database=postgres_database,
        knowhow=knowhow,
        sources=sources,
        chunks=chunks,
        knowledge=knowledge,
        embedding=SimpleNamespace(vectors=vectors),
        note_model_error=lambda *_args, **_kwargs: None,
        invalidate_unified_cache=lambda _notebook_id: None,
        mark_unified_dirty_in_tx=lambda _db, _notebook_id: 0,
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
    # synchronously.
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

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    sources = SourceStore(postgres_database, now=seams.now)
    chunks = ChunkStore(postgres_database)
    knowledge = KnowledgeStore(postgres_database, seams)
    projector = KnowhowProjector(
        settings=Settings(database_url="postgresql://unused/unused"),
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
        mark_unified_dirty_in_tx=lambda _db, _notebook_id: 0,
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
def test_postgres_delete_route_cleans_source_created_after_initial_snapshot(
    postgres_database, monkeypatch
):
    """First projection between route snapshot and table DELETE leaves nothing."""
    from types import SimpleNamespace

    from app.api import knowhow_routes
    from app.repositories.postgres.chunk_store import ChunkStore
    from app.repositories.postgres.embedding_store import EmbeddingStore
    from app.repositories.postgres.knowledge_store import KnowledgeStore
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import SourceStore
    from app.services.knowhow.projection import KnowhowProjector

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    knowhow = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    projector = KnowhowProjector(
        settings=Settings(database_url="postgresql://unused/unused"),
        database=postgres_database,
        knowhow=knowhow,
        sources=SourceStore(postgres_database, now=seams.now),
        chunks=ChunkStore(postgres_database),
        knowledge=KnowledgeStore(postgres_database, seams),
        embedding=SimpleNamespace(
            vectors=EmbeddingStore(write=postgres_database.write)
        ),
        note_model_error=lambda *_args, **_kwargs: None,
        invalidate_unified_cache=lambda _notebook_id: None,
        mark_unified_dirty_in_tx=lambda _db, _notebook_id: 0,
        new_id=seams.new_id,
        now=seams.now,
    )
    table_id = knowhow.create_knowhow_table(
        "nb-content",
        "首次投影删除竞态",
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

    deleted: dict[str, str | None] = {}

    class RouteRepository:
        def get_knowhow_table(self, selected_table_id):
            return knowhow.get_knowhow_table(selected_table_id)

        def delete_knowhow_table(self, selected_table_id):
            result = knowhow.delete_knowhow_table(selected_table_id)
            deleted.update(result)
            return result

    first_cleanup = True

    class RacingProjector:
        def delete_table_projection(self, hidden_source_id):
            nonlocal first_cleanup
            if first_cleanup and hidden_source_id is None:
                first_cleanup = False
                error: list[BaseException] = []

                def run_projection():
                    try:
                        projector.project_table(table_id, embed=False)
                    except BaseException as exc:  # pragma: no cover - asserted below
                        error.append(exc)

                worker = threading.Thread(target=run_projection)
                worker.start()
                worker.join(timeout=10)
                assert not worker.is_alive(), "projection/delete interleave deadlocked"
                assert error == []
                return
            projector.delete_table_projection(hidden_source_id)

    monkeypatch.setattr(knowhow_routes, "repository", lambda: RouteRepository())
    monkeypatch.setattr(
        knowhow_routes.knowhow_api, "build_projector", lambda _repo: RacingProjector()
    )
    monkeypatch.setattr(
        knowhow_routes.knowhow_api,
        "maybe_sweep_orphan_assets",
        lambda *_args, **_kwargs: None,
    )

    knowhow_routes.delete_knowhow_table("nb-content", table_id)
    source_id = deleted["hidden_source_id"]
    assert source_id
    with postgres_database.connect() as connection:
        for table_name, predicate, value in (
            ("knowhow_tables", "id=%s", table_id),
            ("sources", "id=%s", source_id),
            ("source_elements", "source_id=%s", source_id),
            ("chunks", "source_id=%s", source_id),
            ("knowledge_objects", "source_id=%s", source_id),
            ("knowledge_relations", "source_id=%s", source_id),
        ):
            assert connection.execute(
                f"SELECT COUNT(*) AS n FROM {table_name} WHERE {predicate}",
                (value,),
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

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()

    def make_projector():
        knowhow_store = PostgresKnowhowStore(
            postgres_database, new_id=seams.new_id, now=seams.now
        )
        source_store = SourceStore(postgres_database, now=seams.now)
        return knowhow_store, KnowhowProjector(
            settings=Settings(database_url="postgresql://unused/unused"),
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
            mark_unified_dirty_in_tx=lambda _db, _notebook_id: 0,
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


@pytest.mark.postgres_integration
def test_postgres_source_elements_for_chunking_extracts_metadata_keys(
    postgres_database,
):
    """PG 侧 source_elements_for_chunking 的 metadata 提取(caption + description
    + section_path)
    与 SQLite 侧语义对等。历史上这半边零覆盖:把提取写成 metadata.get("section_paths")
    这类笔误只会让 PG 部署静默退回旧标签,本地门与 CI 都不报红——SQLite 半边有
    集成回归门,这条钉住 PG 半边(质量评审 P2-2)。"""
    from app.repositories.postgres._store_utils import jsonb
    from app.repositories.postgres.chunk_store import ChunkStore
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    mark = "%s"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            f"VALUES ('src-crumb',{mark},'source','markdown','extracted','parsed',"
            f"'a.md','',0,'hash-crumb','',{mark},{mark},'textbook')",
            ("nb-content", NOW, NOW),
        )
        rows = [
            ("el-src-crumb-0001", "heading", "set_db",
             {"section_path": "Manual > Commands > set_db"}),
            ("el-src-crumb-0002", "paragraph", "body text", {}),
            ("el-src-crumb-0003", "image", "", {"caption": "flow diagram"}),
            ("el-src-crumb-0004", "image", "三级流水线示意",
             {"description": "三级流水线示意"}),
        ]
        for element_id, element_type, text, metadata in rows:
            connection.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                f"VALUES ({mark},'src-crumb',{mark},'L1',{mark},{mark},{mark})",
                (element_id, element_type, text, jsonb(metadata), NOW),
            )

    elements = ChunkStore(postgres_database).source_elements_for_chunking("src-crumb")

    by_id = {element["id"]: element for element in elements}
    assert by_id["el-src-crumb-0001"]["section_path"] == "Manual > Commands > set_db"
    assert by_id["el-src-crumb-0002"]["section_path"] == ""
    assert by_id["el-src-crumb-0003"]["caption"] == "flow diagram"
    assert by_id["el-src-crumb-0003"]["description"] == ""
    # markdown 的 `> **图片描述**` 引用块：没有 alt 图注的图片凭它进检索
    # （build_chunks 对 image/figure 仅在 caption 与 description 皆空时跳过）。
    assert by_id["el-src-crumb-0004"]["description"] == "三级流水线示意"
    assert [element["id"] for element in elements] == sorted(by_id)


# ---------------------------------------------------------------------------
# 生产热点整改批 E:orphan asset 单遍扫描 + PG 侧补上 knowhow_changes 历史扫描。
# 后者是**正确性修复**:这个适配器此前只扫 live cell,最后一处引用落在历史里
# 的资产在 PG 上会被删掉、在 SQLite 上会被保留 —— 回退到那个版本就指向一个
# 已删文件。两侧现在同一份判定原语、同一份语料。
# ---------------------------------------------------------------------------


def _asset_gc_maintenance(postgres_database, tmp_path):
    from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter

    return PostgresMaintenanceAdapter(
        SimpleNamespace(database=postgres_database, storage_dir=tmp_path)
    )


def _asset_gc_fixture(postgres_database, tmp_path):
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    store = PostgresKnowhowStore(
        postgres_database, new_id=seams.new_id, now=seams.now
    )
    table_id = store.create_knowhow_table(
        "nb-content", "资产", "", _knowhow_columns(), "user-content"
    )
    table = store.get_knowhow_table(table_id)
    anchor_id, plain_id = table["columns"][0]["id"], table["columns"][1]["id"]
    row_id = store.add_knowhow_row(table_id, {anchor_id: "A"})

    def upload(suffix: str) -> str:
        asset_id = store.insert_notebook_asset(
            "nb-content", f"{suffix}.png", "image/png", 3, "user-content"
        )
        asset_dir = tmp_path / "assets" / "nb-content"
        asset_dir.mkdir(parents=True, exist_ok=True)
        (asset_dir / f"{asset_id}.png").write_bytes(b"png")
        return asset_id

    return store, row_id, plain_id, upload


def test_postgres_sweep_keeps_an_asset_referenced_only_by_history(
    postgres_database, tmp_path
):
    """PG 侧的历史扫描:格子把图片改掉之后,``knowhow_changes`` 里那条
    ``before`` 仍然引用着它 —— 回收了就没法回退回带图的版本。

    变异锚点:把 ``_history_ref_tokens`` 整个删掉(或让 ``_surviving_asset_refs`` 不再调它)
    → 这条红(资产被删),SQLite 侧同名用例仍绿 —— 那正是修复前的两侧分叉。"""
    store, row_id, plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    asset_id = upload("history-only")

    store.update_knowhow_cell(row_id, plain_id, f"![图](asset://{asset_id})")
    store.update_knowhow_cell(row_id, plain_id, "图没了")

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 0}
    assert store.get_notebook_asset(asset_id) is not None


def test_postgres_sweep_still_reclaims_an_asset_nothing_mentions(
    postgres_database, tmp_path
):
    """删除面只缩不扩:从未被任何格子或历史提过的资产照旧回收。"""
    store, _row_id, _plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    asset_id = upload("never-referenced")

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 1}
    assert store.get_notebook_asset(asset_id) is None


def test_postgres_sweep_ignores_code_attachment_only_references(
    postgres_database, tmp_path
):
    """代码附件里的 ``asset://`` 不是 keeper 引用 —— 与 SQLite 同口径
    (``content_strings_in_payload`` 从不读 ``code``/``code_text``)。"""
    store, row_id, plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    asset_id = upload("code-only")

    store.update_knowhow_cell(row_id, plain_id, "纯文本,没有图片")
    store.upsert_knowhow_cell_code(
        row_id, plain_id, f"# see asset://{asset_id}", "python", "user-content", "h"
    )

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 1}
    assert store.get_notebook_asset(asset_id) is None


def test_postgres_sweep_scan_count_is_independent_of_asset_count(
    postgres_database, tmp_path, monkeypatch
):
    """判定扫描按阶段发生(分类一次 + 写事务复核一次),不随资产数增长。"""
    store, row_id, plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    for index in range(9):
        upload(f"bulk-{index}")
    store.update_knowhow_cell(row_id, plain_id, "没有图片")

    scans = _count_pg_keeper_scans(maintenance, monkeypatch)

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 9}
    assert len(scans["decide"]) == 2, scans


def test_postgres_source_elements_after_walk_equals_the_whole_source_read(
    postgres_database,
):
    """批 E 的 keyset 整源走查:PG 侧与 ``source_elements()`` 同序、无跳无重。"""
    from app.repositories.postgres._store_utils import jsonb
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import SourceStore

    assert PostgresMigrator(postgres_database).migrate() == 42
    _seed_catalog(postgres_database)
    seams = _seams()
    mark = "%s"
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,created_at,updated_at,doc_type) "
            f"VALUES ('src-keyset',{mark},'source','markdown','extracted','parsed',"
            f"'a.md','',0,'hash-keyset','',{mark},{mark},'textbook')",
            ("nb-content", NOW, NOW),
        )
        for index in range(37):
            connection.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                f"VALUES ({mark},'src-keyset','paragraph','L1',{mark},{mark},{mark})",
                (
                    f"el-keyset-{index:04d}",
                    f"text {index}",
                    jsonb({}),
                    # 多个 created_at,且同一时刻下有多行 —— 次键必须参与。
                    f"2026-07-2{1 + index // 13}T00:00:0{index % 7}+00:00",
                ),
            )

    sources = SourceStore(postgres_database, now=seams.now)
    whole = sources.source_elements("src-keyset")
    assert len(whole) == 37
    for page_size in (1, 5, 36, 37, 100):
        walked, after, pages = [], None, 0
        while True:
            items, after = sources.source_elements_after("src-keyset", after, page_size)
            pages += 1
            assert len(items) <= page_size
            walked.extend(items)
            if after is None:
                break
            assert pages <= 60, "游标没有推进"
        assert walked == whole, page_size


def _count_pg_keeper_scans(maintenance, monkeypatch):
    """PG 侧的判定计数 spy —— 与 SQLite 侧 ``_count_keeper_scans`` 同形状。"""
    scans: dict[str, list] = {"decide": [], "history": []}
    real_decide = maintenance._surviving_asset_refs
    real_history = maintenance._history_ref_tokens

    def counting_decide(db, notebook_id, asset_ids):
        scans["decide"].append(notebook_id)
        return real_decide(db, notebook_id, asset_ids)

    def counting_history(db, notebook_id):
        scans["history"].append(notebook_id)
        return real_history(db, notebook_id)

    monkeypatch.setattr(maintenance, "_surviving_asset_refs", counting_decide)
    monkeypatch.setattr(maintenance, "_history_ref_tokens", counting_history)
    return scans


def test_postgres_sweep_skips_history_when_live_cells_account_for_every_asset(
    postgres_database, tmp_path, monkeypatch
):
    """活格子已把全部候选判为保留时,PG 侧同样跳过历史扫描(与 SQLite 同形状)。"""
    store, row_id, plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    assets = [upload(f"live-{index}") for index in range(3)]
    body = "\n".join(f"![图{i}](asset://{a})" for i, a in enumerate(assets))
    store.update_knowhow_cell(row_id, plain_id, body)
    store.update_knowhow_cell(row_id, plain_id, body + "\n补一句")
    scans = _count_pg_keeper_scans(maintenance, monkeypatch)

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 0}

    assert len(scans["decide"]) == 1
    assert scans["history"] == [], scans


def test_postgres_sweep_still_consults_history_when_a_candidate_is_not_live(
    postgres_database, tmp_path, monkeypatch
):
    """反向护栏:短路不得退化成「永不扫历史」——那正是本轮修掉的 PG 缺陷。"""
    store, row_id, plain_id, upload = _asset_gc_fixture(postgres_database, tmp_path)
    maintenance = _asset_gc_maintenance(postgres_database, tmp_path)
    asset_id = upload("history-fallthrough")
    store.update_knowhow_cell(row_id, plain_id, f"![图](asset://{asset_id})")
    store.update_knowhow_cell(row_id, plain_id, "图没了")
    scans = _count_pg_keeper_scans(maintenance, monkeypatch)

    assert maintenance.sweep_orphan_assets("nb-content") == {"removed": 0}

    assert scans["history"], "活格子判不下来时必须继续扫历史"
    assert store.get_notebook_asset(asset_id) is not None
