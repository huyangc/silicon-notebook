"""PostgreSQL twin of ``tests/test_default_ceiling_probe.py``, plus the EXPLAIN
pin of the drift probe's one-row fingerprint statement.

The fingerprint is computed in SQL here (``md5(string_agg(id, E'\\x1e'
ORDER BY id))`` over COLLATE "C" ids), so the equality with Python's
``universe_digest`` -- ids that differ in case and byte order, empty halves --
is the property this file exists for; the drift matrix, the one-read budget,
the push-down verdict and verify-on-read run unchanged.

EXPLAIN pin (style of ``test_memory_sql_explain_pins.py``: seqscan and
bitmapscan off, assert an index path exists): both halves start from a
``notebook_id`` index condition -- never a walk of the primary key across
every notebook -- the hidden half from ``idx_sources_nb_hidden_type``; the
owner test is a hashed SubPlan on ``idx_memory_owner_notebook_status``, not a
per-row probe.
"""
from __future__ import annotations

import re

import pytest

from tests.test_default_ceiling_probe import (
    assert_an_unbound_read_is_verified_and_rerun_bound,
    assert_one_probe_is_one_fingerprint_read_and_never_cached,
    assert_the_ceiling_is_pushed_down_only_when_it_excludes_nothing,
    assert_the_probe_sees_every_change,
    assert_the_store_digest_is_the_python_digest,
    build_probe_fixture,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_default_ceiling_probe"),
]


@pytest.fixture
def pg_ids(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.embedding import FakeEmbedder
    from tests.model_testkit import bind_all_embedding_clients

    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield build_probe_fixture(repository, "%s")
    finally:
        repository.close()


def test_store_digest_equals_python_digest_on_postgres(pg_ids):
    assert_the_store_digest_is_the_python_digest(pg_ids)


def test_probe_reports_every_change_and_only_the_askers_on_postgres(pg_ids):
    assert_the_probe_sees_every_change(pg_ids)


def test_probe_is_one_fingerprint_read_and_never_cached_on_postgres(pg_ids, monkeypatch):
    assert_one_probe_is_one_fingerprint_read_and_never_cached(pg_ids, monkeypatch)


def test_ceiling_is_pushed_down_only_when_it_excludes_nothing_on_postgres(pg_ids):
    assert_the_ceiling_is_pushed_down_only_when_it_excludes_nothing(pg_ids)


def test_unbound_read_is_verified_and_rerun_bound_on_postgres(pg_ids):
    assert_an_unbound_read_is_verified_and_rerun_bound(pg_ids)


def test_digest_statement_keeps_its_index_paths(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.source_store import _UNIVERSE_DIGEST_SQL

    now = "2026-01-01T00:00:00+00:00"
    assert PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for user in ("u-a", "u-b"):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@example.test", user, now, now, user),
            )
        for notebook in ("nb", "nb-other"):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) "
                "VALUES (%s,'N','','','ready','u-a',%s,%s,'personal')",
                (notebook, now, now),
            )
        # Another notebook's sources, so "this notebook" is selective, as in
        # production (a single-notebook table makes a primary-key walk as
        # cheap as the notebook index and hides the plan being pinned).
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'other-'||g,'nb-other','t','upload',%s,%s "
            "FROM generate_series(0,39999) g",
            (now, now),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||g,'nb',CASE WHEN g%%2=0 THEN 'u-a' ELSE 'u-b' END,"
            "'ask_answer','confirmed','t','x',%s,%s FROM generate_series(0,199) g",
            (now, now),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||g,'nb','t',"
            "CASE WHEN g<200 THEN 'memory' WHEN g<400 THEN 'knowhow' ELSE 'upload' END,"
            "CASE WHEN g<200 THEN 'mem-'||g END,%s,%s FROM generate_series(0,3999) g",
            (now, now),
        )
        db.execute("ANALYZE sources")
        db.execute("ANALYZE memory_items")
    with postgres_database.connect() as connection:
        connection.execute("SET LOCAL enable_seqscan=off")
        connection.execute("SET LOCAL enable_bitmapscan=off")
        rows = connection.execute(
            "EXPLAIN (COSTS OFF) " + _UNIVERSE_DIGEST_SQL, ("nb", "nb", "u-a"),
        ).fetchall()
        # The statement's value on this data: both halves non-empty.
        digests = connection.execute(_UNIVERSE_DIGEST_SQL, ("nb", "nb", "u-a")).fetchone()
    plan = "\n".join(str(row["QUERY PLAN"]) for row in rows)
    assert "Seq Scan" not in plan, plan
    # Both halves start from a notebook_id index condition on ``sources``
    # (the visible half from whichever (notebook_id, ...) index the planner
    # prefers, the hidden half from the hidden-type partial index).
    assert re.search(r"on sources$", plan, re.M) and "on sources s" in plan, plan
    assert plan.count("Index Cond: (notebook_id = 'nb'::text)") >= 2, plan
    assert "idx_sources_nb_hidden_type" in plan, plan
    assert "idx_memory_owner_notebook_status" in plan and "hashed SubPlan" in plan, plan
    assert digests["visible_digest"] and digests["hidden_digest"]
