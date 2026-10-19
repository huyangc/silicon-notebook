"""PostgreSQL twin of tests/test_pending_merge_names.py (P4-T6): the same
worlds answer the same rows, byte for byte (the random candidate ids aside),
and the name read is pinned on a production-shaped corpus.

The plan pin EXPLAINs the statement the store actually issues (a recording
wrapper around the connection captures it with its parameters), with ANALYZE
and the planner's full choice -- sequential scans NOT disabled -- on a corpus
shaped like production: hub clusters of 50 000 members, a retired and an
in-flight generation interleaved with the published one on every hub member,
50 000 small clusters, and ten other notebooks' rows, VACUUM ANALYZEd.
"""
from __future__ import annotations

import json

import pytest

from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.postgres.migrator import PostgresMigrator
from tests.postgres.test_kg_viewer_scope_pg import (  # noqa: F401  (``repo`` is a fixture)
    as_user,
    build_scenario,
    repo,
)
from tests.test_pending_merge_names import (
    assert_hidden_members_never_name_a_side,
    assert_names_cost_one_statement_per_batch,
    assert_rows_carry_both_sides_names,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_pending_merge_names"),
]


def test_pg_rows_carry_both_sides_names(repo):
    assert_rows_carry_both_sides_names(repo)


def test_pg_hidden_members_never_name_a_side(repo):
    assert_hidden_members_never_name_a_side(repo, build_scenario, as_user)


def test_pg_names_cost_one_statement_per_batch(repo, monkeypatch):
    assert_names_cost_one_statement_per_batch(repo, monkeypatch)


# ------------------------------------------------------------------ plan pin

_HUB_MEMBERS = 50_000
_SMALL_CLUSTERS = 50_000


def _seed_plan_corpus(postgres_database) -> None:
    now = normalize_timestamp("2026-10-19T00:00:00+00:00")
    notebooks = ["nb-target"] + [f"nb-o{i}" for i in range(10)]
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for nb in notebooks:
            db.execute(
                "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'n',%s,%s)",
                (nb, now, now))
            db.execute(
                "INSERT INTO unified_kg_state (notebook_id, cluster_generation, updated_at) "
                "VALUES (%s, 2, %s)", (nb, now))
        # two hubs: every member has a retired (1), published (2) and an
        # in-flight (3) row, so the published row of the first member in
        # member order sits between the others under the same index key
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-h'||h||'-'||m||'-'||g, 'nb-target', 'K-hub'||h, "
            "'ko-'||md5(h||'-'||m), 'HUB '||h||' g'||g, 'concept', %s, g "
            "FROM generate_series(0,1) h, generate_series(0,%s) m, generate_series(1,3) g",
            (now, _HUB_MEMBERS - 1))
        # small clusters of 1-4 members, retired + published rows
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-s'||c||'-'||m||'-'||g, 'nb-target', 'K-c'||lpad(c::text,6,'0'), "
            "'ko-'||md5('s'||c||'-'||m), 'C'||c||' g'||g, 'concept', %s, g "
            "FROM generate_series(0,%s) c, generate_series(0,3) m, generate_series(1,2) g "
            "WHERE m <= c %% 4",
            (now, _SMALL_CLUSTERS - 1))
        # other notebooks reuse the same canonical ids
        db.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-o'||n||'-'||r, 'nb-o'||n, 'K-c'||lpad((r/4)::text,6,'0'), "
            "'ko-'||md5('o'||n||'-'||r), 'O', 'concept', %s, 2 "
            "FROM generate_series(0,9) n, generate_series(0,19999) r",
            (now,))
    import psycopg

    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) concept_clusters")
        raw.execute("VACUUM (ANALYZE) unified_kg_state")


class _Recording:
    """Connection wrapper that logs ``(statement, params)`` of every execute."""

    def __init__(self, inner, log):
        self._inner = inner
        self._log = log

    def execute(self, statement, params=(), **kwargs):
        self._log.append((statement, params))
        return self._inner.execute(statement, params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from _nodes(child)


def test_pg_name_lookup_is_one_bounded_seek_per_id(postgres_database):
    from app.repositories.postgres.governance_store import GovernanceStore

    assert PostgresMigrator(postgres_database).migrate()
    _seed_plan_corpus(postgres_database)
    ids = sorted(["K-hub0", "K-hub1"] + [f"K-c{i:06d}" for i in range(0, 50_000, 50)][:898])
    log: list = []
    with postgres_database.connect() as db:
        names = GovernanceStore.pending_merge_names(_Recording(db, log), "nb-target", ids)
        assert len(log) == 1, log
        assert len(names) == 900
        # the published generation answers, never the retired or in-flight row
        assert names["K-hub0"] == "HUB 0 g2" and names["K-c000000"] == "C0 g2"
        statement, params = log[0]
        row = db.execute(
            f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {statement}", params).fetchone()
    plan = row["QUERY PLAN"]
    plan = json.loads(plan) if isinstance(plan, str) else plan
    root = plan[0]["Plan"]
    nodes = list(_nodes(root))
    text = json.dumps(plan)
    scans = [n for n in nodes if n.get("Relation Name") == "concept_clusters"]
    assert len(scans) == 1, text
    scan = scans[0]
    assert scan["Node Type"] == "Index Scan", text
    assert scan["Index Name"] == "idx_clusters_nb_canonical_member_gen", text
    # one seek per id, at most one row each, and the rows of other
    # generations it skips before the published one bounded by the two
    # non-published generations -- a 50 000-member hub costs one seek.
    # EXPLAIN reports both figures as per-loop averages.
    assert scan["Actual Loops"] == 900, text
    assert scan["Actual Rows"] <= 1, text
    assert scan.get("Rows Removed by Filter", 0) <= 2, text
    assert not any(n["Node Type"] == "Sort" for n in nodes), text
    assert not any(n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "concept_clusters"
                   for n in nodes), text
    # the published generation is a bound-parameter InitPlan, evaluated once
    init = [n for n in nodes if n.get("Parent Relationship") == "InitPlan"]
    assert init and all(
        n.get("Relation Name") in (None, "unified_kg_state") for n in init), text
    assert not any(n.get("Parent Relationship") == "SubPlan" for n in nodes), text
    assert any(n["Node Type"] == "Function Scan" and n.get("Function Name") == "unnest"
               for n in nodes), text
    # for the record (not asserted; shown with -s): the plan and its time
    for n in nodes:
        print(n["Node Type"], n.get("Parent Relationship", ""), n.get("Relation Name", ""),
              n.get("Index Name", ""), "rows", n.get("Actual Rows"), "loops",
              n.get("Actual Loops"), "removed", n.get("Rows Removed by Filter", 0))
    print("pending_merge_names 900 ids:", plan[0].get("Execution Time"), "ms")
