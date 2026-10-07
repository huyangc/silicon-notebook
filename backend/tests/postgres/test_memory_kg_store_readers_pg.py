"""PostgreSQL twin of tests/test_memory_kg_store_readers.py (E4-4, ruling M1).

Same scenario and expectations (``memory_kg_reader_cases``), seeded through
the same repository facade.  The EXPLAIN pins of the changed statements live
in ``test_memory_kg_readers_explain_pins.py``.
"""
from __future__ import annotations

import pytest

from app.repositories.postgres import knowledge_counts_cache

from tests import memory_kg_reader_cases as cases


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_store_readers"),
]


@pytest.fixture
def world(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    knowledge_counts_cache.invalidate()
    try:
        yield cases.build_world(repository, postgres=True)
    finally:
        repository.close()


def test_list_page_rows_and_total_follow_the_viewer_pg(world):
    cases.check_list_page_and_total(world)


def test_counts_are_shared_plus_own_memory_pg(world):
    cases.check_counts(world)


def test_kg_ready_is_shared_or_own_memory_pg(world):
    cases.check_has_kg(world)


def test_no_viewer_counts_are_one_cached_statement_and_zero_live_reads_pg(world):
    cases.check_none_counts_zero_live_reads(world)


def test_fts_filters_before_the_limit_pg(world):
    cases.check_fts_filters_before_the_limit(world)


def test_fts_ceiling_and_viewer_combine_pg(world):
    cases.check_fts_ceiling_and_viewer(world)


def test_object_meta_rows_viewer_form_is_notebook_bound_pg(world):
    cases.check_object_meta_rows_binding(world)


def test_edge_centrality_rows_hold_no_memory_pg(world):
    cases.check_edge_centrality_is_shared(world)


def test_object_evidence_rows_can_skip_the_evidence_pg(world):
    cases.check_object_evidence_rows(world)


def test_enrich_evidence_can_resolve_sources_only_pg(world):
    cases.check_enrich_evidence_sources_only(world)


def test_a_viewer_read_never_takes_the_knn_shape_pg(world):
    """``allow_knn`` is a hint: with a conforming GiST name index the unscoped
    probe takes the KNN statement (control), a viewer read never does — its
    Memory filter rides the legacy arms — and still leaves B's objects out."""
    from app.repositories.postgres.search import reset_knn_index_cache

    with world.repo._runtime.database.write() as db:
        db.execute(
            "CREATE INDEX idx_e44_knn_gist ON knowledge_objects "
            "USING gist ((((payload ->> 'name') COLLATE \"C\")) "
            "public.gist_trgm_ops(siglen=128)) WHERE status != 'deprecated'"
        )
    reset_knn_index_cache()
    try:
        with cases.connect(world) as db:
            control = cases.Recorder(db)
            cases.store(world).fts_search(control, world.nb, "kgtoken", 30, allow_knn=True)
            viewer = cases.Recorder(db)
            hits = cases.store(world).fts_search(
                viewer, world.nb, "kgtoken", 30, allow_knn=True, viewer_id=world.a)
        assert any("<->" in s for s in control.statements), control.statements
        assert not any("<->" in s for s in viewer.statements), viewer.statements
        assert {h["name"] for h in hits} == cases.SHARED_NAMES | cases.A_NAMES
    finally:
        reset_knn_index_cache()


def test_the_counts_memo_carries_no_viewer_pg(world):
    cases.check_counts_cache_holds_no_viewer(world)


def test_graph_readers_follow_the_viewer_pg(world):
    cases.check_graph_readers(world)


def test_own_memory_overlay_reads_only_the_viewers_memory_pg(world):
    cases.check_own_memory_overlay(world)


def test_search_legs_follow_the_viewer_pg(world):
    cases.check_search_legs(world)


def test_neighbour_relations_follow_the_viewer_pg(world):
    cases.check_neighbour_relations(world)


def test_shared_tooling_leaves_every_memory_row_out_pg(world):
    cases.check_shared_tooling(world)


def _no_memory_predicate(statements):
    return statements and not any(
        "memory_items" in s or "FROM sources" in s for s in statements)


def test_no_viewer_issues_the_pre_isolation_statements_pg(world):
    cases.check_none_statements_unchanged(world, {
        "graph_node_rows": [
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = %s AND status != 'deprecated'"],
        "unified_graph_rows": [
            "SELECT id, object_type, payload, status FROM knowledge_objects "
            "WHERE notebook_id=%s AND status!='deprecated' ORDER BY ordinal"],
        "relations_for_notebook": ["SELECT * FROM knowledge_relations WHERE notebook_id = %s"],
        "neighbor_relation_rows": [
            "SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            "WHERE notebook_id=%s "
            "AND (source_object_id=ANY(%s) OR target_object_id=ANY(%s))"],
        "object_meta_rows_for_notebook": [
            "SELECT id, object_type, payload FROM knowledge_objects "
            "WHERE notebook_id=%s AND id IN (%s)"],
        "object_meta_rows": [
            "SELECT id, object_type, status, payload FROM knowledge_objects WHERE id IN (%s)"],
        "list_knowledge_page_rows": [
            "SELECT * FROM knowledge_objects WHERE notebook_id = %s AND object_type = %s "
            "ORDER BY created_at ASC, id ASC LIMIT %s OFFSET %s"],
        # The lexical candidate union is long and shared with the chunk leg;
        # its viewer-free text is pinned by never carrying the Memory probe.
        "fts_search": _no_memory_predicate,
        "notebook_has_kg": [
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects "
            "WHERE notebook_id=%s) AS exists"],
    })
