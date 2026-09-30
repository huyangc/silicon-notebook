"""EXPLAIN pins (real PG) of the two statements behind the E5-3 orphan sweep.

* ``MemoryStore.orphan_memory_source_ids`` (the sweep's global keyset read) and
* ``MemoryStore.orphan_memory_source_count_on`` (checkup H12, one notebook).

Style follows ``test_memory_sql_explain_pins.py``: a non-trivial dataset (4,000
sources, 200 of them Memory sources, 8 of those orphans), real ``VACUUM (ANALYZE)``
statistics, and the plan the planner actually picks -- no ``enable_*`` switches for
the sweep read, because the point of that pin is the plan an untuned production
database chooses:

* neither statement may need a heap scan of either table: with ``enable_seqscan``
  off (the idiom of the neighbouring pins -- a few thousand test rows would let the
  planner legitimately prefer a Seq Scan on cost alone) an index path must exist;
* the sweep read has no ``source_type``-only index to seek (``sources`` is indexed by
  ``(notebook_id, source_type)``, not by type alone), so one pass over ``sources`` is
  inherent: exactly one scan node, bounded by ``LIMIT``, no join back into
  ``sources``. Its ``memory_items`` side is reached through an index only. Which
  index depends on table size, and both shapes are bounded: on this small table the
  planner hashes the confirmed ids once (``hashed SubPlan``); measured at 300k Memory
  items / 350k sources it uses one ``memory_items_pkey`` probe per Memory source
  (1.2 s for a full zero-orphan pass, 7 ms for the per-notebook count at 151 Memory
  sources);
* the count is seeked on ``idx_sources_nb_hidden_type (notebook_id, source_type)``.
"""
from __future__ import annotations

import pytest

from app.repositories.postgres import memory_store as postgres_memory_store
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_orphan_sweep_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"
_TABLES = ("sources", "memory_items")


def _seed(postgres_database) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','u-a@example.test','u-a','user','active',%s,%s,'u-a','','',0)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb','N','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "VALUES ('nb2','N2','','','ready','u-a',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        # 192 confirmed Memory items; sources 0..191 are their (healthy) sources
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||g,'nb','u-a','ask_answer','confirmed','t','x',%s,%s "
            "FROM generate_series(0,191) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||lpad(g::text,5,'0'),"
            "CASE WHEN g%%2=0 THEN 'nb' ELSE 'nb2' END,'t',"
            "CASE WHEN g<200 THEN 'memory' WHEN g<400 THEN 'knowhow' ELSE 'upload' END,"
            "CASE WHEN g<192 THEN 'mem-'||g WHEN g<196 THEN 'mem-gone-'||g "
            "WHEN g<198 THEN NULL WHEN g<200 THEN '' END,%s,%s "
            "FROM generate_series(0,3999) g",
            (_NOW, _NOW),
        )
        for table in _TABLES:
            db.execute(f"ANALYZE {table}")
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _plan(connection, sql: str, params: tuple, *, index_only: bool = False) -> str:
    if index_only:
        connection.execute("SET LOCAL enable_seqscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _plans(postgres_database) -> dict[str, str]:
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    where = postgres_memory_store._ORPHAN_MEMORY_SOURCE_WHERE
    with postgres_database.connect() as connection:
        # the statements really do what the pin is about
        ids = [
            row["id"]
            for row in connection.execute(
                "SELECT s.id FROM sources s WHERE " + where + " AND s.id > %s "
                "ORDER BY s.id LIMIT %s", ("", 100),
            ).fetchall()
        ]
        assert len(ids) == 8, ids
        plans = {
            "ids": _plan(
                connection,
                "SELECT s.id FROM sources s WHERE " + where + " AND s.id > %s "
                "ORDER BY s.id LIMIT %s",
                ("", 200),
                index_only=True,
            ),
        }
    with postgres_database.connect() as connection:
        plans["count"] = _plan(
            connection,
            "SELECT count(*) AS n FROM sources s WHERE s.notebook_id = %s AND " + where,
            ("nb",),
            index_only=True,
        )
    return plans


def test_orphan_statements_keep_their_plan_shapes(postgres_database):
    plans = _plans(postgres_database)

    for name, plan in plans.items():
        assert "Seq Scan" not in plan, (name, plan)
        # memory_items is only ever reached through an index (the primary key probe on
        # a large table, the owner index when the planner hashes the confirmed ids of
        # a small one) -- never scanned as a heap
        memory_lines = [ln for ln in plan.splitlines() if "on memory_items" in ln]
        assert memory_lines and all("Index" in ln for ln in memory_lines), (name, plan)
        assert plan.count("on sources") == 1, (name, plan)  # one pass, no join back

    ids = plans["ids"]
    assert ids.startswith("Limit"), ids  # bounded

    # the per-notebook count is seeked on the notebook prefix of the type index
    count = plans["count"]
    assert "Index Scan using idx_sources_nb_hidden_type" in count, count
    assert "notebook_id = 'nb'" in count and "source_type = 'memory'" in count, count
