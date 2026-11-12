"""Rank member probes on production-shaped data, in both visibility states."""
import re

import psycopg
import pytest
from psycopg.rows import dict_row

from app.repositories.postgres.unified_kg_store import UnifiedKgStore
from tests.postgres.conftest import vacuum_until_all_visible
from tests.postgres.test_kg_findings_plan_pins import (
    NB,
    OTHER_NOTEBOOKS,
    PAGE_PAIRS,
    _captured,
    _plan,
)
from tests.postgres.test_kg_findings_plan_pins import (
    pin_database as pin_database,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_concept_rank_plan_pins"),
]


@pytest.fixture
def rank_pin_database(bystander_snapshot, pin_database):
    # The bystander must begin before the corpus is written.
    with pin_database.write() as db:
        db.execute(
            "INSERT INTO kg_concept_ranks(notebook_id,rank,canonical_id,"
            "representative_object_id,source_count,object_count,built_at_kg_seq,"
            "built_at_cluster_seq) "
            "SELECT n.id,g,CASE WHEN g=0 THEN '' ELSE 'can-'||lpad((g-1)::text,6,'0') END,"
            "CASE WHEN g=0 THEN '' ELSE 'ko-'||lpad(((g-1)*5)::text,6,'0') END,"
            "CASE WHEN g=0 THEN 0 ELSE 4 END,CASE WHEN g=0 THEN 0 ELSE 4 END,1,1 "
            "FROM generate_series(0,80) g CROSS JOIN "
            "(SELECT %s AS id UNION ALL SELECT 'nb-other-'||lpad(o::text,2,'0') "
            "FROM generate_series(1,%s) o) n",
            (NB, OTHER_NOTEBOOKS))
    with psycopg.connect(
        pin_database.settings.database_url, autocommit=True, row_factory=dict_row,
    ) as raw:
        raw.execute("VACUUM (ANALYZE) kg_concept_ranks")
        if bystander_snapshot.held:
            bystander_snapshot.assert_visibility_map_blocked(raw, "concept_clusters")
    if not bystander_snapshot.held:
        for table in ("concept_clusters", "knowledge_objects", "kg_concept_ranks"):
            vacuum_until_all_visible(pin_database.settings.database_url, table)
    return pin_database


def test_rank_reads_seek_bounded_keys(rank_pin_database):
    database = rank_pin_database
    store = UnifiedKgStore(database)
    # Mix published clusters with unclustered singletons, as the Q path does.
    canonicals = list(dict.fromkeys(can for _, can in PAGE_PAIRS))[:19]
    canonicals.append("ko-000003")
    for method, kw in (
        ("concept_recount_rows", {"ceiling": None}),
        ("concept_rank_names", {"ceiling": None, "scan": 50}),
        ("concept_rank_citation_rows", {"ceiling": None, "scan": 50}),
        ("concept_rank_representatives", {}),
    ):
        def call():
            with store.read_snapshot() as db:
                assert getattr(store, method)(db, NB, canonicals, **kw)

        [(sql, params)] = _captured(call, "UNION ALL")
        plan = _plan(database, sql, params)
        assert re.search(
            r"Index (?:Only )?Scan using idx_clusters_nb_canonical_member_gen", plan), plan
        # The schema names its knowledge-object primary key pk_knowledge_objects.
        assert plan.count("pk_knowledge_objects") == 2, plan
        assert "Seq Scan on knowledge_objects" not in plan, plan
        if method == "concept_rank_representatives":
            # An ordered index seek must feed the per-canonical LIMIT.
            # Sorting all members below that LIMIT defeats early stopping.
            with database.connect() as db:
                row = db.execute("EXPLAIN (FORMAT JSON) " + sql, params, prepare=False).fetchone()
            nodes = list(_plan_nodes(row["QUERY PLAN"][0]["Plan"]))
            limited_clusters = [
                node for node in nodes
                if node["Node Type"] == "Limit" and any(
                    child.get("Index Name") == "idx_clusters_nb_canonical_member_gen"
                    for child in _plan_nodes(node)
                )
            ]
            assert limited_clusters, plan
            for limit in limited_clusters:
                descendants = list(_plan_nodes(limit))
                assert not any(child["Node Type"] in {"Sort", "Incremental Sort"}
                               for child in descendants), plan
                assert any(child.get("Index Name") == "idx_clusters_nb_canonical_member_gen"
                           and child.get("Scan Direction") == "Forward"
                           for child in descendants), plan

    def read_precomputed():
        with store.read_snapshot() as db:
            assert len(store.concept_rank_precomputed_rows(db, NB)) == 80

    captured = _captured(read_precomputed, "SELECT")
    rank_queries = [(sql, params) for sql, params in captured if "FROM kg_concept_ranks" in sql]
    assert len(rank_queries) == 1
    for sql, params in captured:
        plan = _plan(database, sql, params)
        if "FROM kg_concept_ranks" in sql:
            assert "pk_kg_concept_ranks" in plan, plan
        assert "Seq Scan on knowledge_objects" not in plan, plan


def _plan_nodes(node):
    yield node
    for child in node.get("Plans", ()):
        yield from _plan_nodes(child)
