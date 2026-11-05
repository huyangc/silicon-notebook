"""Real-PostgreSQL plan pins for the auto user-memory suggestion store reads
(0087), on a skewed corpus and with the planner left alone:

* ``user_state`` -- the hot-path read every completed web ask of an opted-in
  or default user makes -- is one primary-key probe of ``memory_suggest_users``;
* the backlog count (``unadopted_candidate_count``) reads the partial
  ``idx_memory_suggest_unadopted`` and never scans ``memory_items``;
* the two sampling reads (``clarified_scan`` / ``repeated_window``) never scan
  ``ask_jobs`` and reach it through an index keyed by ``created_by``
  through the G20 partial owner/notebook/time index (hot-path batch 10).

Each statement pinned is the one the store executes, captured on its way
through ``psycopg.Connection.execute`` and explained with its real
parameters; every pin runs in both ``bystander_snapshot`` states.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.memory_store import MemoryStore
from app.repositories.postgres.memory_suggest_store import MemorySuggestStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_suggest_plan_pins"),
]

OWNER = "u-pin-owner"
MEMBER = "u-pin-1"
GROUP = "g-pin"
NB = "nb-pin-00"
NOW = "2026-10-29T00:00:00+00:00"
NOTEBOOKS = 50
USERS = 30
ASK_JOBS = 60_000
MEMORIES = 40_000
SWITCH_ROWS = 20_000


@pytest.fixture
def pin_database(postgres_settings):
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
        assert PostgresMigrator(database).migrate() == POSTGRES_SCHEMA_MANIFEST.postgres_version
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """50 notebooks and 30 users. 60 000 ask jobs skewed onto the first
    notebook (half of them) and the owner (a third of those), mixed statuses
    and entry points; 40 000 memories, a few hundred of them open or dismissed
    suggestions; 20 000 switch rows."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "SELECT CASE WHEN g = 0 THEN %s ELSE 'u-pin-'||g END, 'pin'||g||'@x', 'p', "
            "'user', %s, %s FROM generate_series(0, %s) g",
            (OWNER, NOW, NOW, USERS - 1),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
            "SELECT 'nb-pin-'||lpad(g::text,2,'0'), 'n', %s, %s, %s "
            "FROM generate_series(0, %s) g",
            (OWNER, NOW, NOW, NOTEBOOKS - 1),
        )
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "SELECT 'job-'||g, CASE WHEN g %% 2 = 0 THEN %s "
            "ELSE 'nb-pin-'||lpad((g %% %s)::text,2,'0') END, 'conv', "
            "CASE WHEN g %% 6 = 0 THEN %s ELSE 'u-pin-'||(1 + g %% %s) END, 'reasoning', "
            "'q '||g, (ARRAY['done','done','done','failed'])[1 + g %% 4], 'ans-'||g, "
            "%s::timestamptz - (g || ' minutes')::interval, %s, "
            "(ARRAY['web','web','mcp'])[1 + g %% 3], 'answer', '' "
            "FROM generate_series(0, %s) g",
            (NB, NOTEBOOKS, OWNER, USERS - 1, NOW, NOW, ASK_JOBS - 1),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,suggested_signal,created_at,updated_at) "
            "SELECT 'mem-'||g, 'nb-pin-'||lpad((g %% %s)::text,2,'0'), "
            "CASE WHEN g %% 5 = 0 THEN %s ELSE 'u-pin-'||(1 + g %% %s) END, 'ask_answer', "
            "(ARRAY['confirmed','confirmed','candidate','rejected'])[1 + g %% 4], "
            "'t '||g, 'c', CASE WHEN g %% 97 = 0 THEN 'clarified' END, %s, %s "
            "FROM generate_series(0, %s) g",
            (NOTEBOOKS, OWNER, USERS - 1, NOW, NOW, MEMORIES - 1),
        )
        # A deployment-sized switch table: the hot-path read must stay a key
        # probe however many users have ever touched the switch.
        db.execute(
            "INSERT INTO memory_suggest_users(owner_id,enabled,updated_at) "
            "SELECT id, 1, %s::timestamptz FROM users UNION ALL "
            "SELECT 'u-switch-'||g, g %% 2, %s::timestamptz FROM generate_series(1, %s) g",
            (NOW, NOW, SWITCH_ROWS),
        )
        # One group reaching ten notebooks, MEMBER in it: the group-exit
        # preview's driver.
        db.execute(
            "INSERT INTO groups(id,name,created_at,updated_at) VALUES (%s,'g',%s,%s)",
            (GROUP, NOW, NOW),
        )
        db.execute(
            "INSERT INTO group_members(group_id,user_id,role,added_at) "
            "SELECT %s, CASE WHEN g = 0 THEN %s ELSE 'u-pin-'||(1 + g) END, "
            "'member', %s FROM generate_series(0, 9) g",
            (GROUP, MEMBER, NOW),
        )
        db.execute(
            "INSERT INTO notebook_grants(id,notebook_id,principal_type,principal_id,"
            "role,created_at) SELECT 'gr-'||g, 'nb-pin-'||lpad(g::text,2,'0'), "
            "'group', %s, 'reader', %s FROM generate_series(0, 9) g",
            (GROUP, NOW),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute(
            "VACUUM (ANALYZE) ask_jobs, memory_items, memory_suggest_users, users, "
            "groups, group_members, notebook_grants"
        )


def _captured(call, marker: str) -> list[tuple[str, object]]:
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        if isinstance(query, str) and marker in query:
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording
    try:
        call()
    finally:
        psycopg.Connection.execute = original
    assert captured, f"no statement carrying {marker!r} ran"
    return captured


def _plan(database: PostgresDatabase, sql: str, params) -> str:
    with database.connect() as db:
        rows = db.execute(f"EXPLAIN {sql}", params, prepare=False).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _store(database: PostgresDatabase) -> MemorySuggestStore:
    return MemorySuggestStore(database, new_id=lambda prefix: f"{prefix}-pin")


def test_user_state_is_one_primary_key_probe(pin_database, bystander_snapshot):
    store = _store(pin_database)
    ((sql, params),) = _captured(
        lambda: store.user_state(OWNER), "FROM memory_suggest_users")
    plan = _plan(pin_database, sql, params)
    assert "pk_memory_suggest_users" in plan, plan
    assert "Seq Scan" not in plan, plan


def test_backlog_count_reads_the_partial_unadopted_index(pin_database, bystander_snapshot):
    store = _store(pin_database)
    ((sql, params),) = _captured(
        lambda: store.unadopted_candidate_count(OWNER, NB), "FROM memory_items")
    plan = _plan(pin_database, sql, params)
    assert "idx_memory_suggest_unadopted" in plan, plan
    assert "Seq Scan on memory_items" not in plan, plan


def test_expiry_scan_reads_the_partial_expiry_index(pin_database, bystander_snapshot):
    counter = iter(range(1, MEMORIES + 1))
    store = MemorySuggestStore(
        pin_database, new_id=lambda prefix: f"{prefix}-expiry-pin-{next(counter)}"
    )
    ((sql, params),) = _captured(
        lambda: store.expire_overdue(now=NOW, cutoff="2026-10-30T00:00:00+00:00", limit=500),
        "WITH picked AS MATERIALIZED",
    )
    plan = _plan(pin_database, sql, params)
    assert "idx_memory_suggest_expiry" in plan, plan
    assert "Seq Scan on memory_items m" not in plan, plan


def test_group_exit_preview_subtrahend_reads_the_partial_unadopted_index(
    pin_database, bystander_snapshot,
):
    """``group_exit_memory_preview`` counts "your memories" as the original
    per-notebook count minus the per-notebook unadopted count: the
    subtrahend reads ``idx_memory_suggest_unadopted`` and neither half scans
    ``memory_items``."""
    store = MemoryStore(
        pin_database, new_id=lambda prefix: f"{prefix}-pin", now=lambda: NOW)
    ((sql, params),) = _captured(
        lambda: store.group_exit_memory_preview(GROUP, MEMBER, now=NOW),
        "FROM group_members gm")
    plan = _plan(pin_database, sql, params)
    assert "idx_memory_suggest_unadopted" in plan, plan
    assert "Seq Scan on memory_items" not in plan, plan


@pytest.mark.parametrize("read", ["clarified_scan", "repeated_window"])
def test_sampling_reads_never_scan_ask_jobs_and_key_on_the_owner(
    pin_database, bystander_snapshot, read,
):
    store = _store(pin_database)
    if read == "clarified_scan":
        def call():
            store.clarified_scan(
                OWNER, NB, cursor_created_at="2026-10-01T00:00:00+00:00",
                cursor_id="", inclusive=True, limit=40)
    else:
        def call():
            store.repeated_window(
                OWNER, NB, lower="2026-09-29T00:00:00+00:00", limit=40)
    ((sql, params),) = _captured(call, "FROM ask_jobs")
    plan = _plan(pin_database, sql, params)
    assert "Seq Scan on ask_jobs" not in plan, plan
    assert "idx_ask_jobs_memory_suggest_sample" in plan, plan
    assert "Sort" not in plan, plan
