"""EXPLAIN pins (real PG, the REAL planner) of the statements behind the E5-3 orphan sweep.

* ``MemoryStore.has_orphan_memory_sources`` (startup probe, ``EXISTS``),
* ``MemoryStore.orphan_memory_source_ids`` (the sweep's global keyset read) and
* ``MemoryStore.orphan_memory_source_count_on`` (checkup H12, one notebook).

No ``enable_*`` switches anywhere: the point of these pins is the plan an untuned
production database chooses. The bed is big enough for the planner to have a real
opinion (100,000 sources with random ids like production's ``src-<uuid>``, 30,000 of
them Memory sources, 30,000 confirmed Memory items, 12 orphans of every shape) and
carries ``VACUUM (ANALYZE)`` statistics.

What is pinned, and why (measured on 1M sources / 300k Memory sources / random ids,
PostgreSQL 16, load average ~45 -- see docs/operations.md):

* the orphan predicate is ONE decorrelatable ``NOT EXISTS`` (no ``OR``): every plan
  contains an ``Anti Join`` and no per-row ``SubPlan``. The earlier ``OR`` spelling
  made the planner walk the whole of ``sources`` by primary key (id order for
  ``ORDER BY id LIMIT``) with a probe per Memory source: 59 s for the ``LIMIT 1``
  probe on the measurement machine, 8.4 s for a page;
* the paged read sorts the (small) anti-join result, never the primary key: the
  materialized CTE fences it, so ``pk_sources`` is not used as an ordered walk;
* the startup probe is an ``EXISTS`` (first hit stops it), no ``ORDER BY``;
* the per-notebook count is seeked on ``idx_sources_nb_hidden_type``
  ``(notebook_id, source_type)``.
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
            "SELECT 'nb'||g,'N','','','ready','u-a',%s,%s,'personal' "
            "FROM generate_series(0,49) g",
            (_NOW, _NOW),
        )
        # 30,000 confirmed Memory items (ids are md5s: random order, like production)
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||md5(g::text),'nb'||(g%%50),'u-a','ask_answer',"
            "CASE WHEN g<30000 THEN 'confirmed' ELSE 'deprecated' END,'t','x',%s,%s "
            "FROM generate_series(0,31999) g",
            (_NOW, _NOW),
        )
        # 30,000 healthy Memory sources, 70,000 uploads
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||md5('m'||g),'nb'||(g%%50),'t','memory',"
            "'mem-'||md5(g::text),%s,%s FROM generate_series(0,29999) g",
            (_NOW, _NOW),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-'||md5('u'||g),'nb'||(g%%50),'t','upload',NULL,%s,%s "
            "FROM generate_series(0,69999) g",
            (_NOW, _NOW),
        )
        # 12 orphans in notebook nb7: cleared link (''), NULL link, deprecated Memory,
        # a Memory that never existed
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'orph-'||g,'nb7','t','memory',"
            "CASE g%%4 WHEN 0 THEN '' WHEN 1 THEN NULL "
            "WHEN 2 THEN 'mem-'||md5((30000+g)::text) ELSE 'mem-gone-'||g END,%s,%s "
            "FROM generate_series(0,11) g",
            (_NOW, _NOW),
        )
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in _TABLES:
            raw.execute(f"VACUUM (ANALYZE) {table}")


def _plan(connection, sql: str, params: tuple = ()) -> str:
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


class _Recorder:
    """Wraps a connection so the exact statements a store method runs are captured."""

    def __init__(self, connection, seen):
        self._connection, self._seen = connection, seen

    def execute(self, sql, params=()):
        self._seen.append((sql, tuple(params)))
        return self._connection.execute(sql, params)


class _RecordingDatabase:
    def __init__(self, database, seen):
        self._database, self._seen = database, seen

    def connect(self):
        from contextlib import contextmanager

        @contextmanager
        def wrapped():
            with self._database.connect() as connection:
                yield _Recorder(connection, self._seen)

        return wrapped()


def test_orphan_statements_keep_their_plan_shapes(postgres_database):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    # the pinned statements are the ones the store methods really run
    seen: list[tuple[str, tuple]] = []
    store = postgres_memory_store.MemoryStore(
        _RecordingDatabase(postgres_database, seen), new_id=lambda prefix: prefix, now=lambda: _NOW
    )
    assert store.has_orphan_memory_sources() is True
    found = store.orphan_memory_source_ids(200)
    assert len(found) == 12 and found == sorted(found)
    with store.database.connect() as connection:
        assert postgres_memory_store.MemoryStore.orphan_memory_source_count_on(
            connection, "nb7"
        ) == 12
    assert len(seen) == 3, seen
    statements = dict(zip(("exists", "ids", "count"), seen))
    with postgres_database.connect() as connection:
        plans = {
            name: _plan(connection, sql, params)
            for name, (sql, params) in statements.items()
        }

    for name, plan in plans.items():
        # decorrelated: an anti join, never a per-row correlated SubPlan
        assert "Anti Join" in plan, (name, plan)
        assert "SubPlan" not in plan, (name, plan)
        assert plan.count("on sources") == 1, (name, plan)  # one pass, no join back
    for name in ("exists", "ids"):
        # the global statements never walk sources in primary-key order
        assert "pk_sources" not in plans[name], (name, plans[name])
    assert "ORDER BY" not in statements["exists"][0]  # the probe is an EXISTS, unordered

    ids = plans["ids"]
    assert "CTE o" in ids and "Sort" in ids and ids.startswith("Limit"), ids

    # sources side: the (notebook_id, source_type) seek. The memory_items side is size
    # dependent and both shapes are bounded: a Hash (Right) Anti Join over the confirmed
    # items here, one pk_memory_items probe per Memory source on a large table (measured
    # 28 ms for 1,525 Memory sources against 300k items).
    count = plans["count"]
    assert "idx_sources_nb_hidden_type" in count, count
