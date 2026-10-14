"""F2 on PostgreSQL: the notebook list's batched summary read.

* the batched list serialises byte for byte like the per-notebook path in
  every cell of ``tests/test_notebook_list_prefetch.py`` (the oracle);
* warm, its statement count does not depend on how many notebooks it lists;
* the count cache's prefetched key is a HIT check only: a hit keeps the LRU
  recency and reads nothing, a miss re-reads the notebook's own key and
  stores the value under it (never under the key the list read earlier);
* EXPLAIN pin of the one statement on a seeded library: every source count,
  ``kg_ready`` and mount is a per-notebook index probe, no sequential scan,
  in the custom and the cached generic plan, at 1, 50 and 600 notebooks.
  The mount half's pin at realistic access-control scale is in
  ``test_mount_sql_viewer_pg.py``.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from contextlib import contextmanager

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres import knowledge_counts_cache as pg_counts_cache
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from tests.postgres.test_kg_viewer_scope_pg import repo  # noqa: F401  (fixture)
from tests.postgres.conftest import scans_with_own_conditions
from tests.postgres.test_mount_sql_viewer_pg import (
    analyze_and_settle,
    assert_the_mounts_are_probed_per_notebook,
    facts_plan as _facts_plan,
    held_bystander,
    seed_realistic_mount_fixture,
)
from tests.test_notebook_list_prefetch import (
    as_user,
    assert_list_matches_the_per_notebook_path,
    build_list_world,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_list_prefetch"),
]


def pg_q(statement: str) -> str:
    return statement.replace("?", "%s")


def test_pg_list_matches_the_per_notebook_path(repo, monkeypatch):  # noqa: F811
    w = build_list_world(repo, pg_q)
    assert_list_matches_the_per_notebook_path(repo, w, pg_counts_cache, pg_q, monkeypatch)


class _Counting:
    def __init__(self, connection, statements):
        self._connection = connection
        self._statements = statements

    def execute(self, query, params=None, *args, **kwargs):
        self._statements.append(" ".join(str(query).split()))
        return self._connection.execute(query, params, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _listed_statements(repo, user, monkeypatch):  # noqa: F811
    summaries = repo._runtime.notebook_summaries
    database = summaries.database
    real_connect = database.connect
    statements: list[str] = []

    @contextmanager
    def connect(*args, **kwargs):
        with real_connect(*args, **kwargs) as connection:
            yield _Counting(connection, statements)

    with monkeypatch.context() as patch:
        patch.setattr(database, "connect", connect)
        listed = as_user(user, summaries.list_for_user, user.id)
    return listed, statements


def test_pg_warm_list_statement_count_does_not_grow_with_the_list(repo, monkeypatch):  # noqa: F811
    """Warm, a list of 5 (the small-list gate's first batched length) and a
    list of 12 notebooks cost the same statements: the four list reads, the
    Memory-holders probe and ONE facts statement.  (Before F2 every notebook
    added five: source count, three count-cache keys, mounts.)  Every
    notebook has its own non-zero ``(kg_reset_epoch, kg_mutation_seq)`` and
    one in each list has no ``unified_kg_state`` row at all (a legacy import
    or copy): the prefetched key must still be the key the cache stores."""
    runtime = repo._runtime
    small = runtime.identity.create_user("s00000021", "pw123456")
    large = runtime.identity.create_user("l00000022", "pw123456")
    for user, n in ((small, 5), (large, 12)):
        ids = [
            as_user(user, runtime.catalog.create_notebook, NotebookCreate(name=f"n{i}")).id
            for i in range(n)
        ]
        with runtime.database.write() as db:
            for i, nb in enumerate(ids[1:], start=1):
                db.execute(
                    "UPDATE unified_kg_state SET kg_reset_epoch=%s, kg_mutation_seq=%s "
                    "WHERE notebook_id=%s", (i, 100 + 7 * i, nb))
            db.execute("DELETE FROM unified_kg_state WHERE notebook_id=%s", (ids[0],))
    shapes = {}
    for user in (small, large):
        _listed_statements(repo, user, monkeypatch)            # warm the memos
        listed, statements = _listed_statements(repo, user, monkeypatch)
        shapes[user.username] = (len(listed), statements)
    (n_small, small_sql), (n_large, large_sql) = shapes.values()
    assert (n_small, n_large) == (5, 12)
    assert small_sql == large_sql
    assert len(large_sql) == 6, large_sql
    assert sum("jsonb_array_elements_text" in s for s in large_sql) == 1


def test_a_prefetched_key_only_decides_a_hit(repo):  # noqa: F811
    """``_seq_gated(version=...)``: a hit on the prefetched key returns the
    memo with no read and moves it to the LRU end; a miss re-reads the
    notebook's key and stores under THAT key, so a value computed after the
    list's read is never filed under the older key the list read."""
    database = repo._runtime.database
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with database.write() as db:
        for nb, seq in (("nb-k1", 5), ("nb-k2", 7)):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,created_at,updated_at) "
                "VALUES (%s,%s,NULL,%s,%s)", (nb, nb, now, now))
            db.execute(
                "INSERT INTO unified_kg_state (notebook_id,kg_mutation_seq,updated_at) "
                "VALUES (%s,%s,%s)", (nb, seq, now))
    memo: OrderedDict = OrderedDict()
    computed: list[str] = []

    def compute(nb):
        def run():
            computed.append(nb)
            return f"value-{nb}-{len(computed)}"
        return run

    with database.connect() as db:
        # Miss on a stale prefetched key: the fresh key is read and used.
        value = pg_counts_cache._seq_gated(memo, db, "nb-k1", compute("nb-k1"),
                                           version=(0, 4))
        assert computed == ["nb-k1"] and memo["nb-k1"] == ((0, 5), value)
        pg_counts_cache._seq_gated(memo, db, "nb-k2", compute("nb-k2"))
        assert list(memo) == ["nb-k1", "nb-k2"]

    class NoReads:
        def execute(self, *_a, **_k):
            raise AssertionError("a prefetched hit reads nothing")

    hit = pg_counts_cache._seq_gated(memo, NoReads(), "nb-k1", compute("nb-k1"),
                                     version=(0, 5))
    assert hit == value and computed == ["nb-k1", "nb-k2"]
    assert list(memo) == ["nb-k2", "nb-k1"]                     # LRU recency kept

    # The memo holds an OLDER key than the one the list prefetched (the
    # notebook changed since the memo was filled): a miss, re-read, written
    # back under the new key.
    with database.write() as db:
        db.execute("UPDATE unified_kg_state SET kg_mutation_seq=9 WHERE notebook_id='nb-k1'")
    with database.connect() as db:
        fresh = pg_counts_cache._seq_gated(memo, db, "nb-k1", compute("nb-k1"),
                                           version=(0, 9))
    assert computed == ["nb-k1", "nb-k2", "nb-k1"] and fresh != value
    assert memo["nb-k1"] == ((0, 9), fresh)


# ---------------------------------------------------------------- EXPLAIN pin
_PIN_NOTEBOOKS = 3000
_PIN_SEEDED = 600
_LIBRARY_TABLES = ("notebooks", "notebook_bases", "notebook_members", "notebook_grants",
                   "group_members", "sources", "knowledge_objects", "unified_kg_state")


@pytest.fixture
def pg_seeded_library(request, postgres_database):
    """The realistic access-control fixture (3000 notebooks) plus sources and
    KG objects on the first 600: per notebook 3-40 sources (some hidden), 0-80
    objects. A test that also asks for ``bystander_snapshot`` gets the
    bystander open before the migration; without one every table is VACUUMed
    until its visibility map is full (``analyze_and_settle``)."""
    held = held_bystander(request)
    PostgresMigrator(postgres_database).migrate()
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        seed_realistic_mount_fixture(db.execute, _PIN_NOTEBOOKS, now)
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            "SELECT 'nb'||g||'-s'||j, 'nb'||g, 's', "
            "CASE WHEN j %% 9 = 0 THEN 'knowhow' ELSE 'pdf' END, 'extracted', 'parsed', %s, %s "
            "FROM generate_series(1, %s) g CROSS JOIN LATERAL "
            "generate_series(1, 3 + abs(hashtext('s'||g)) %% 38) j",
            (now, now, _PIN_SEEDED))
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,source_id,"
            "created_at,updated_at) "
            "SELECT 'nb'||g||'-o'||j, 'nb'||g, 'rule', 'approved', 'nb'||g||'-s1', %s, %s "
            "FROM generate_series(1, %s) g CROSS JOIN LATERAL "
            "generate_series(1, abs(hashtext('o'||g)) %% 81) j",
            (now, now, _PIN_SEEDED))
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id,kg_mutation_seq,updated_at) "
            "SELECT 'nb'||g, g, %s FROM generate_series(1, %s) g", (now, _PIN_SEEDED))
    analyze_and_settle(postgres_database.settings.database_url, _LIBRARY_TABLES, held=held)
    return postgres_database


def _keyed_on_the_listed_notebook(plan: str, table: str, key: str) -> bool:
    """Every scan node of ``table`` carries an Index/Recheck Cond on ``key``:
    a per-notebook index probe, whatever access method the planner picked."""
    scans = scans_with_own_conditions(plan, table)
    return bool(scans) and all(
        any(line.split(":", 1)[0] in ("Index Cond", "Recheck Cond") and key in line
            for line in own)
        for _node, own in scans
    )


def test_the_list_facts_statement_probes_each_notebook_by_index(
    bystander_snapshot, pg_seeded_library,
):
    """Each per-notebook read is an index probe correlated with the listed
    notebook -- the source count on a ``sources`` index, ``kg_ready`` on a
    ``knowledge_objects`` index, the mounts on ``pk_notebook_bases`` -- the
    per-notebook statements' own access paths, in the custom plan and the
    cached generic plan, for lists of 1, 50 and 600 notebooks (owned,
    everyone-mounted and seeded mixed).

    Two states (``bystander_snapshot``). Which ACCESS METHOD those probes and
    the list's drive use moves with the visibility map: with no page
    all-visible the source count becomes a Bitmap Heap Scan and the drive
    becomes ``Seq Scan on notebooks n`` + Hash Join (the jsonb list is always
    estimated at 100 rows). So:

    * both states: no sequential scan of any per-notebook table, every scan of
      ``sources`` / ``knowledge_objects`` / ``notebook_bases`` keyed on the
      listed (or mounted) notebook, and with ``enable_seqscan=off`` the drive
      has its ``pk_notebooks`` index path;
    * no bystander and a verified-full visibility map: the exact plan -- no
      ``Seq Scan`` at all, Index Only Scans on ``sources`` and
      ``pk_notebook_bases``, the drive on ``pk_notebooks``."""
    with pg_seeded_library.connect() as connection:
        bystander_snapshot.assert_visibility_map_blocked(connection, *_LIBRARY_TABLES)
        for n in (1, 50, _PIN_SEEDED):
            ids = ["nbP0"] + [f"nb{g}" for g in range(1, n)]
            for viewer in ("u7", None):
                for generic in (False, True):
                    plan = _facts_plan(connection, ids, viewer, generic=generic)
                    where = f"{n} / {viewer} / {'generic' if generic else 'custom'}:\n{plan}"
                    assert not re.search(
                        r"Seq Scan on (sources|knowledge_objects|unified_kg_state)\b", plan
                    ), where
                    assert _keyed_on_the_listed_notebook(
                        plan, "sources", "notebook_id = n.id"), where
                    assert _keyed_on_the_listed_notebook(
                        plan, "knowledge_objects", "notebook_id = "), where
                    assert re.search(
                        r"Index (Only )?Scan using \w+ on knowledge_objects ko\s+"
                        r"Index Cond: \(notebook_id = n\.id\)", plan), where
                    assert_the_mounts_are_probed_per_notebook(plan, where)
                    if bystander_snapshot.held:
                        continue
                    assert "Seq Scan" not in plan, where
                    assert re.search(
                        r"Index Only Scan using \w+ on sources s\s+Index Cond: "
                        r"\(notebook_id = n\.id\)", plan), where
                    assert re.search(
                        r"Index Only Scan using pk_notebook_bases on notebook_bases e\s+"
                        r"Index Cond: \(notebook_id = n\.id\)", plan), where
                    assert re.search(r"Scan using pk_notebooks on notebooks n\b", plan), where
                capability = _facts_plan(connection, ids, viewer, generic=False, seqscan=False)
                assert "Seq Scan" not in capability, capability
                assert re.search(
                    r"Scan using pk_notebooks on notebooks n\b", capability), capability
