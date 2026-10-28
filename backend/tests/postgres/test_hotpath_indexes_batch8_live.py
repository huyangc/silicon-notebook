"""Batch 8 live: ``install_hotpath_indexes`` builds ``idx_sources_nb_summary_trgm``
and ``find_sources``' summary leg enters through it with no planner knobs.
Unit twin: ``tests/test_hotpath_indexes_batch8.py``."""
from __future__ import annotations

import psycopg
import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.hotpath_indexes import (
    inspect_hotpath_indexes,
    install_hotpath_indexes,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.source_store import SourceStore, _find_sources_term_legs

pytestmark = pytest.mark.postgres_integration

_NAME = "idx_sources_nb_summary_trgm"
_NB = "nb-batch8"
NOW = "2026-10-28T10:00:00+00:00"


def _schema_of(database) -> str:
    with database.connect() as connection:
        return connection.execute("SELECT current_schema() AS name").fetchone()["name"]


def _seed(database) -> None:
    now = normalize_timestamp(NOW)
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
            (_NB, _NB, now, now),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,summary,doc_type,created_at,updated_at) "
            "SELECT 'b7-'||lpad(g::text,6,'0'), %s, 'Design note '||g::text, 'pdf', "
            "'extracted','extracted','b7-'||g::text||'.pdf','u/x.pdf', "
            "'A study of reference circuits number '||g::text||' with measurements', "
            "'academic_paper', %s, %s FROM generate_series(0, 7999) g",
            (_NB, now, now),
        )
        db.execute(
            "UPDATE sources SET summary='We present a zqxjsummary method' "
            "WHERE id='b7-000042'"
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,summary,doc_type,created_at,updated_at) VALUES "
            "('b7-hidden',%s,'m','memory','extracted','extracted','m.md','u/m.md',"
            "'zqxjsummary private','',%s,%s)",
            (_NB, now, now),
        )


@pytest.mark.xdist_group(name="postgres_hotpath_indexes_batch8")
def test_the_summary_leg_is_served_by_its_own_gin(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    schema = _schema_of(postgres_database)
    database_url = postgres_database.settings.database_url
    before = inspect_hotpath_indexes(database_url, schema=schema)
    assert next(r for r in before["indexes"] if r["name"] == _NAME)["state"] == "缺失"
    state = install_hotpath_indexes(database_url, schema=schema)
    assert next(r for r in state["indexes"] if r["name"] == _NAME)["state"] == "存在"
    assert install_hotpath_indexes(database_url, schema=schema) == state

    _seed(postgres_database)
    # VACUUM merges the GIN pending list (0048's operational note) and
    # ANALYZE gives the planner the corpus; outside a transaction.
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("VACUUM ANALYZE sources")
    statement, params = _find_sources_term_legs(_NB, ["zqxjsummary"])
    legs = statement.split(" UNION ALL ")
    offset = 0
    for leg in legs:
        count = leg.count("%s")
        if "'summary'" in leg:
            summary, summary_params = leg, params[offset:offset + count]
            break
        offset += count
    with postgres_database.connect() as connection:
        plan = "\n".join(
            str(row["QUERY PLAN"]) for row in connection.execute(
                "EXPLAIN (COSTS OFF) " + summary, summary_params,
            ).fetchall()
        )
    assert f"Bitmap Index Scan on {_NAME}" in plan, plan
    assert "Seq Scan" not in plan, plan

    store = SourceStore(postgres_database, now=lambda: NOW)
    with postgres_database.connect() as connection:
        rows = store.find_sources_metadata_rows(
            connection, _NB, terms=["zqxjsummary"], kind="all")
    assert [row["source_id"] for row in rows if row["matches"]] == ["b7-000042"]
