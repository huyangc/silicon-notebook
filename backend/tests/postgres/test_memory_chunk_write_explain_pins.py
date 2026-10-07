"""E4-1b: EXPLAIN pins for the Memory probes the chunk write paths run (real PG).

Every probe runs the production statement itself (the module constants and the
sync import's own builder ``_source_rows_statement``). Two kinds of pin, and what
each proves:

* capability pins (``enable_seqscan`` / ``enable_bitmapscan`` off):
  the statement AS WRITTEN has an index path through the named index. A rewrite
  into a form no index can answer (``lower(id)``, ``notebook_id || ''``, dropping
  the notebook scope) shows up as a ``Seq Scan`` or as the wrong index / index
  condition. They say nothing about what the planner prefers.
* real-planner pins (no planner setting touched): on ANALYSED data skewed like a
  large deployment (200k sources, a fifth of them Memory, one notebook with 10k
  ordinary sources) the planner actually CHOOSES that path. This is the pin the earlier whole-database publish probe would have
  failed: after ANALYZE it planned a Seq Scan of ``sources`` over every Memory
  source of the database.

The probes:

* ``ChunkStore`` probe (``replace_source_chunks`` / ``insert_rows``) and the
  Knowhow transfer probe: one primary-key lookup (``pk_sources``);
* KG build publish: the notebook's Memory sources through
  ``idx_sources_nb_hidden_type`` with ``notebook_id`` in the index condition, so
  its cost is bounded by the notebook, never by the Memory sources of the whole
  database; the result is intersected with the snapshot in Python;
* sync import: ``pk_sources`` for a full per-batch ``IN`` list (``_ROW_BATCH`` ids),
  the only statement both of its Memory refusals run.

The SQLite twins are ``tests/test_memory_chunk_write_sqlite_plans.py``.
"""
from __future__ import annotations

import re

import pytest

from app.migration.sync.import_ import _ROW_BATCH, _source_rows_statement
from app.repositories.postgres import (
    chunk_store,
    kg_build_job_store,
    knowhow_transfer_store,
)
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_chunk_write_explain"),
]

_NOW = "2026-01-01T00:00:00+00:00"


def _seed(postgres_database, *, other_notebooks: int, memory_elsewhere: int,
          upload_elsewhere: int, nb_uploads: int, nb_memory: int, analyze: bool) -> None:
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        # seeding only: the sync-capture row triggers would dominate the insert time
        db.execute("ALTER TABLE sources DISABLE TRIGGER USER")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('u-a','a@example.test','a','user','active',%s,%s,'u-a','','',0)",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            "SELECT CASE WHEN g=0 THEN 'nb' ELSE 'nb-'||g END,'N','','','ready','u-a',%s,%s,"
            "'personal' FROM generate_series(0,%s) g",
            (_NOW, _NOW, other_notebooks),
        )
        for prefix, notebook, source_type, count in (
            ("v-", "'nb'", "upload", nb_uploads),
            ("m-", "'nb'", "memory", nb_memory),
            ("k-", "'nb'", "knowhow", 20),
            ("mo-", f"'nb-'||(1+g%%{other_notebooks})", "memory", memory_elsewhere),
            ("o-", f"'nb-'||(1+g%%{other_notebooks})", "upload", upload_elsewhere),
        ):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                f"SELECT %s||g,{notebook},'t',%s,%s,%s FROM generate_series(1,%s) g",
                (prefix, source_type, _NOW, _NOW, count),
            )
        db.execute("ALTER TABLE sources ENABLE TRIGGER USER")
        if analyze:
            db.execute("ANALYZE sources")


def _queries() -> dict[str, tuple[str, tuple]]:
    ids = tuple(f"o-{i}" for i in range(1, _ROW_BATCH + 1))
    return {
        "chunk_store": (chunk_store.MEMORY_PROBE_SQL, ("v-1",)),
        "transfer": (knowhow_transfer_store.MEMORY_PROBE_SQL, ("v-1",)),
        "import": (_source_rows_statement(len(ids)).replace("?", "%s"), ids),
        "publish": (kg_build_job_store.NOTEBOOK_MEMORY_SOURCES_SQL, ("nb",)),
    }


def _plan(connection, sql: str, params: tuple, *, force_index: bool) -> str:
    if force_index:
        connection.execute("SET LOCAL enable_seqscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _assert_paths(plans: dict[str, str]) -> None:
    for name, plan in plans.items():
        assert "Seq Scan" not in plan, (name, plan)
        if name == "publish":
            assert "idx_sources_nb_hidden_type" in plan, (name, plan)
            assert re.search(r"Index Cond: \(\(?notebook_id = ", plan), (name, plan)
        else:
            assert "pk_sources" in plan, (name, plan)
            assert re.search(r"(Index Cond|Recheck Cond): \(id = ", plan), (name, plan)


def test_memory_probe_plans(postgres_database):
    """Both kinds of pin on one analysed database at the scale the quality review
    measured: 200k sources, 40k of them Memory across 100 notebooks, the published
    notebook with 10k ordinary sources and 300 Memory. (On a much smaller table the
    planner may prefer a Seq Scan for a full 1000-id batch, because reading the whole
    small table is cheaper; that is not what is pinned.)"""
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database, other_notebooks=99, memory_elsewhere=39_700,
          upload_elsewhere=150_000, nb_uploads=10_000, nb_memory=300, analyze=True)
    for force_index in (True, False):
        with postgres_database.connect() as connection:
            plans = {
                name: _plan(connection, sql, params, force_index=force_index)
                for name, (sql, params) in _queries().items()
            }
        _assert_paths(plans)
