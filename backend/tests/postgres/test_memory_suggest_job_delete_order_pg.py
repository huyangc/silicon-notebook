"""Real-PostgreSQL plan pin for the ordered job-row delete (review P2-2).

Every multi-row ``DELETE FROM memory_suggest_jobs`` locks its target rows in a
MATERIALIZED CTE ``ORDER BY notebook_id, owner_id FOR UPDATE`` first
(``ordered_job_delete_sql``), so two such deletes over overlapping rows -- one
per notebook from a sharing change, one per owner from turning the switch off
-- cannot deadlock. The ordering lives in the plan, not in the text: this file
captures the statement each of the four call sites ACTUALLY executes (through
``psycopg.Connection.execute``, with its real parameters, on seeded rows),
runs ``EXPLAIN (FORMAT JSON)`` of it in a transaction it rolls back, and
asserts on the plan tree:

* the top node is a ``Delete`` on ``memory_suggest_jobs``;
* inside the ``doomed`` CTE a ``LockRows`` node sits directly above a ``Sort``
  whose keys are ``notebook_id`` then ``owner_id`` (qualified or not). When the
  statement pins one of the two to a constant (``owner_id = %s``) the planner
  drops that column from the sort keys -- every row carries the same value --
  and the pin accepts the single remaining key only if a scan below the Sort
  really holds that equality.

The planner is left alone (no ``enable_*`` settings); the ``Sort`` is there
because of the explicit ``ORDER BY`` whatever the primary-key index could
offer. Every pin runs in both ``bystander_snapshot`` states.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.repositories.memory_suggest_rows import ordered_job_delete_sql
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.group_store import _clear_suggest_jobs_without_read_on
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_suggest_job_delete_order"),
]

NOW = "2026-10-29T00:00:00+00:00"
CREATOR = "u-ord-creator"
NOTEBOOKS = 12
OWNERS = 12
MARKER = "DELETE FROM memory_suggest_jobs USING doomed"
SORT_KEYS = ["notebook_id", "owner_id"]


def _notebook(index: int) -> str:
    return f"nb-ord-{index:02d}"


def _owner(index: int) -> str:
    return f"u-ord-{index:02d}"


@pytest.fixture
def order_database(postgres_settings):
    database = PostgresDatabase(
        postgres_settings.model_copy(update={
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 2,
            "postgres_pool_acquire_timeout_seconds": 10,
            "postgres_statement_timeout_seconds": 60,
            "postgres_lock_timeout_seconds": 10,
        }),
        Path(__file__).resolve().parents[3],
    )
    try:
        assert PostgresMigrator(database).migrate() == 88
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """12 notebooks created by one user and 12 owners with a job row in every
    notebook (144 rows). None of the owners can read any notebook, so the
    access-loss cleanup has every row to delete."""
    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "SELECT CASE WHEN g = 0 THEN %s ELSE 'u-ord-'||lpad(g::text,2,'0') END, "
            "'ord'||g||'@x', 'o', 'user', %s, %s FROM generate_series(0, %s) g",
            (CREATOR, NOW, NOW, OWNERS),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "SELECT 'nb-ord-'||lpad(g::text,2,'0'), 'n', %s, %s, %s "
            "FROM generate_series(0, %s) g",
            (CREATOR, NOW, NOW, NOTEBOOKS - 1),
        )
        db.execute(
            "INSERT INTO memory_suggest_jobs(notebook_id,owner_id,status,pending_signal,"
            "cursor_created_at,cursor_id,cursor_inclusive,window_started_at,created_at,"
            "updated_at) "
            "SELECT 'nb-ord-'||lpad(n::text,2,'0'), 'u-ord-'||lpad(o::text,2,'0'), "
            "'idle', 1, %s::timestamptz, '', 1, %s::timestamptz, %s::timestamptz, "
            "%s::timestamptz FROM generate_series(0, %s) n, generate_series(1, %s) o",
            (NOW, NOW, NOW, NOW, NOTEBOOKS - 1, OWNERS),
        )
        db.execute(
            "INSERT INTO memory_suggest_users(owner_id,enabled,updated_at) "
            "SELECT 'u-ord-'||lpad(o::text,2,'0'), 1, %s::timestamptz "
            "FROM generate_series(1, %s) o",
            (NOW, OWNERS),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute(
            "VACUUM (ANALYZE) memory_suggest_jobs, memory_suggest_users, "
            "notebooks, users"
        )


def _captured(call) -> tuple[str, Any]:
    """The one statement carrying the ordered-delete text that ``call`` ran."""
    captured: list[tuple[str, Any]] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        if isinstance(query, str) and MARKER in query:
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording
    try:
        call()
    finally:
        psycopg.Connection.execute = original
    assert len(captured) == 1, f"expected one ordered delete, saw {len(captured)}"
    return captured[0]


def _explain(database: PostgresDatabase, sql: str, params: Any) -> dict[str, Any]:
    """The JSON plan of ``sql`` with its real parameters, in a transaction
    that is rolled back."""
    with psycopg.connect(database.settings.database_url) as raw:
        try:
            row = raw.execute(
                f"EXPLAIN (FORMAT JSON) {sql}", params, prepare=False
            ).fetchone()
        finally:
            raw.rollback()
    return row[0][0]["Plan"]


def _walk(node: dict[str, Any], ancestors: tuple[dict[str, Any], ...] = ()):
    yield node, ancestors
    for child in node.get("Plans", ()):
        yield from _walk(child, ancestors + (node,))


def _key_names(sort: dict[str, Any]) -> list[str]:
    """The sort keys as bare column names (qualifier and ``COLLATE`` dropped)."""
    return [
        re.sub(r"\s+COLLATE\s+\S+$", "", str(key).replace('"', "")).split(".")[-1]
        for key in sort.get("Sort Key", [])
    ]


def _fixed_by_equality(sort: dict[str, Any], column: str) -> bool:
    """Whether a scan below ``sort`` pins ``column`` to one constant, which
    lets the planner drop it from the sort keys (every row carries the same
    value, so the remaining keys still order the rows completely)."""
    pinned = re.compile(rf"(?<![\w.])(?:\w+\.)?{column} = (?!ANY\b)['\d$]")
    return any(
        pinned.search(str(node.get(field, "")))
        for node, _ in _walk(sort)
        for field in ("Filter", "Index Cond", "Recheck Cond")
    )


def _orders_by_primary_key(sort: dict[str, Any]) -> bool:
    """The sort yields ``(notebook_id, owner_id)`` ascending: both keys in that
    order, or one of them when the scan below pins the other to a constant."""
    keys = _key_names(sort)
    if keys == SORT_KEYS:
        return True
    if keys == ["notebook_id"]:
        return _fixed_by_equality(sort, "owner_id")
    if keys == ["owner_id"]:
        return _fixed_by_equality(sort, "notebook_id")
    return False


def _assert_ordered_lock_plan(plan: dict[str, Any]) -> None:
    assert plan["Node Type"] == "ModifyTable", plan["Node Type"]
    assert plan["Operation"] == "Delete", plan["Operation"]
    assert plan["Relation Name"] == "memory_suggest_jobs", plan["Relation Name"]
    locks = [
        (node, ancestors) for node, ancestors in _walk(plan)
        if node["Node Type"] == "LockRows"
    ]
    assert locks, "no LockRows node: the rows are not locked before the delete"
    ordered = [
        (node, ancestors) for node, ancestors in locks
        if any(
            child["Node Type"] == "Sort" and _orders_by_primary_key(child)
            for child in node.get("Plans", ())
        )
    ]
    assert ordered, (
        "no LockRows sits directly above a Sort by (notebook_id, owner_id); "
        f"LockRows children: {[[c['Node Type'] for c in n.get('Plans', ())] for n, _ in locks]}, "
        f"sort keys: {[_key_names(n) for n, _ in _walk(plan) if n['Node Type'] == 'Sort']}"
    )
    assert any(
        str(holder.get("Subplan Name", "")) == "CTE doomed"
        for node, ancestors in ordered
        for holder in (*ancestors, node)
    ), "the ordered LockRows is not inside the 'doomed' CTE"


def _store(database: PostgresDatabase) -> MemorySuggestStore:
    return MemorySuggestStore(database, new_id=lambda prefix: f"{prefix}-ord")


def _group_store_cleanup(database: PostgresDatabase) -> None:
    class _Rollback(Exception):
        pass

    with pytest.raises(_Rollback):
        with database.write() as connection:
            _clear_suggest_jobs_without_read_on(
                connection, [_notebook(0), _notebook(3), _notebook(7)]
            )
            raise _Rollback  # leave the seeded rows in place


def _account_merge_shape(database: PostgresDatabase) -> None:
    class _Rollback(Exception):
        pass

    with pytest.raises(_Rollback):
        with database.write() as connection:
            connection.execute(
                ordered_job_delete_sql("memory_suggest_jobs.owner_id = %s"), (_owner(3),)
            )
            raise _Rollback  # leave the seeded rows in place


CALL_SITES = {
    "set_memory_mode_manual": lambda db: _store(db).set_memory_mode(
        _owner(5), "manual", now=NOW),
    "clear_job_rows": lambda db: _store(db).clear_job_rows(
        _notebook(2), [_owner(1), _owner(4), _owner(9)]),
    # ``AuthStore.merge_sso_account`` deletes the removed account's rows with
    # exactly this call (the source guard pins that call site).
    "account_merge_per_owner": _account_merge_shape,
    "group_store_without_read_on": _group_store_cleanup,
}


@pytest.mark.parametrize("site", sorted(CALL_SITES))
def test_the_executed_delete_locks_rows_in_primary_key_order(
    order_database, bystander_snapshot, site,
):
    sql, params = _captured(lambda: CALL_SITES[site](order_database))
    _assert_ordered_lock_plan(_explain(order_database, sql, params))


def test_the_assertion_rejects_an_unordered_plan(order_database, bystander_snapshot):
    """The plan check is not vacuous: a bare per-owner delete has no LockRows
    and is rejected."""
    plan = _explain(
        order_database,
        "DELETE FROM memory_suggest_jobs WHERE owner_id = %s", (_owner(3),),
    )
    with pytest.raises(AssertionError):
        _assert_ordered_lock_plan(plan)
