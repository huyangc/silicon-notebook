"""EXPLAIN pin (real PostgreSQL): R4's focal pages walk a hub cluster through
the member index, bounded by the page size -- never a scan of the cluster's
whole membership or of ``knowledge_objects``.

R4 (``communities.CommunityQueryService._ceiling_focal``) checks that a focal
resolved by its stored name has an in-ceiling member spelled as the focal key,
reading the focal cluster's members a page at a time through
``UnifiedKgStore.comparison_peer_members`` (its LATERAL read: the cluster's rows
in ``(notebook_id, canonical_id, member_object_id)`` index order with ``LIMIT``,
each joined to ``knowledge_objects`` by primary key). An earlier shape put the
name comparison into SQL; that predicate sat on the joined table with no
statistics, so ``LIMIT`` bounded the rows returned, not the rows scanned, and
the planner joined the whole cluster first (review measurement on a 300k-object
library with an 8000-member hub: the first ``LIMIT 8`` page as a Parallel
Bitmap Heap Scan over all members plus a top-N sort, ~32k buffers; with
statistics a Parallel Seq Scan of ``knowledge_objects`` and a Merge Join on
every page).

The fixture is that hub: 8000 members, all spelled as the focal key and all
owned by a source outside the ceiling (so R4 walks the whole cluster and fails
closed: 21 pages of 8, 16, ..., 512), beside 50,000 other objects and their
clusters in the same library. The pages the service actually issues are
captured and EXPLAINed (ANALYZE, BUFFERS), both before and after the tables
have statistics: the first and the last page must show no Seq Scan, no Bitmap
scan and no Sort node, must walk the cluster index, and must touch a bounded
number of buffers.
"""
from __future__ import annotations

import contextlib
import re

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from app.services.communities import CommunityQueryService
from app.services.source_scope import source_scope_context

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_comparison_peer_plan_pin"),
]

NB = "nb-hub-pin"
HUB = "K-hubwidget"
KEY = "hub widget"
MEMBERS = 8000
NOISE = 50000
CLUSTER_INDEX = "idx_clusters_nb_canonical_member_gen"
PAGES = [8, 16, 32, 64, 128, 256] + [512] * 15
# A page walks at most ``limit`` cluster index entries plus one primary-key
# probe per member: a few buffers per row, never the ~5k-32k of a whole-cluster
# or whole-table scan.
MAX_BUFFERS_FIRST = 200
MAX_BUFFERS_LAST = 3000


def _seed(database) -> None:
    now = normalize_timestamp("2026-01-01T00:00:00+00:00")
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'hub',%s,%s)",
            (NB, now, now))
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, cluster_generation, updated_at) "
            "VALUES (%s, 0, %s)", (NB, now))
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) "
            "SELECT 'ko-hub-'||lpad(g::text,5,'0'), %s, 'concept', 'approved', "
            "jsonb_build_object('name', %s::text), '[]'::jsonb, 's-hidden', %s, %s "
            "FROM generate_series(0, %s) g", (NB, KEY, now, now, MEMBERS - 1))
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at) "
            "SELECT 'ko-noise-'||lpad(g::text,6,'0'), %s, 'concept', 'approved', "
            "jsonb_build_object('name', 'noise '||g), '[]'::jsonb, 's-open', %s, %s "
            "FROM generate_series(0, %s) g", (NB, now, now, NOISE - 1))
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "created_at,generation) "
            "SELECT 'cc-hub-'||g, %s, %s, 'ko-hub-'||lpad(g::text,5,'0'), %s, 'concept', %s, 0 "
            "FROM generate_series(0, %s) g", (NB, HUB, KEY, now, MEMBERS - 1))
        db.execute(
            "INSERT INTO concept_clusters "
            "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,"
            "created_at,generation) "
            "SELECT 'cc-noise-'||g, %s, 'K-noise-'||(g/10), "
            "'ko-noise-'||lpad(g::text,6,'0'), 'noise', 'concept', %s, 0 "
            "FROM generate_series(0, %s) g", (NB, now, NOISE - 1))


class _Recording:
    """The store's database, recording every statement and its parameters."""

    def __init__(self, database, log: list) -> None:
        self._database, self._log = database, log

    @contextlib.contextmanager
    def connect(self):
        log = self._log

        with self._database.connect() as db:
            class _Connection:
                def execute(self, sql, params=None, **kwargs):
                    log.append((str(sql), params))
                    return db.execute(sql, params, **kwargs)

                def __getattr__(self, name):
                    return getattr(db, name)

            yield _Connection()

    def __getattr__(self, name):
        return getattr(self._database, name)


class _NoEvents:
    def emit(self, event) -> None:
        raise AssertionError(f"no event expected: {event}")


def _explain(database, sql: str, params) -> str:
    with database.connect() as db:
        rows = db.execute(
            f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, TIMING OFF) {sql}", params,
            prepare=False,
        ).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _buffers(plan: str) -> int:
    """Shared buffers (hit + read) of the plan's top node."""
    match = re.search(r"Buffers: shared( hit=(\d+))?( read=(\d+))?", plan)
    assert match, plan
    return int(match.group(2) or 0) + int(match.group(4) or 0)


@pytest.mark.parametrize("statistics", ["fresh", "analyzed"])
def test_focal_pages_walk_the_cluster_index_bounded(postgres_database, statistics):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    if statistics == "analyzed":
        with postgres_database.write() as db:
            db.execute("SET LOCAL statement_timeout = '0'")
            db.execute("ANALYZE knowledge_objects")
            db.execute("ANALYZE concept_clusters")
    log: list = []
    service = CommunityQueryService(
        notebooks=None, unified_kg=UnifiedKgStore(_Recording(postgres_database, log)),
        event_log=_NoEvents())
    with source_scope_context(
        NB, None, None, notebook_source_ceilings={NB: frozenset({"s-open"})},
    ):
        # Every member is spelled as the key but owned outside the ceiling:
        # R4 walks the whole cluster and fails closed.
        assert service._ceiling_focal(NB, KEY, {"allowed_source_ids": ["s-open"]}) is None
    pages = [(sql, params) for sql, params in log if "member_name" in sql]
    assert [params[-1] for _sql, params in pages] == PAGES
    for label, (sql, params), budget in (
        ("first", pages[0], MAX_BUFFERS_FIRST), ("last", pages[-1], MAX_BUFFERS_LAST),
    ):
        plan = _explain(postgres_database, sql, params)
        print(f"--- {statistics} statistics, {label} page (limit {params[-1]}):\n{plan}")
        assert "Seq Scan" not in plan, f"{label} page:\n{plan}"
        assert "Bitmap" not in plan, f"{label} page:\n{plan}"
        assert not re.search(r"^\s*(->\s+)?(Incremental )?Sort\b", plan, re.M), (
            f"{label} page:\n{plan}")
        assert CLUSTER_INDEX in plan, f"{label} page:\n{plan}"
        assert _buffers(plan) <= budget, f"{label} page:\n{plan}"
