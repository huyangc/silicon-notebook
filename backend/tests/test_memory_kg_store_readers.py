"""E4-4 (ruling M1): the KG store readers see another member's Memory-derived
rows for nobody but internal callers.

Scenario and assertions: ``memory_kg_reader_cases`` (shared with the
PostgreSQL twin ``postgres/test_memory_kg_store_readers_pg.py``).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite.id_binding import bind_ids, drive_by
from app.repositories.sqlite import knowledge_counts_cache
from app.services.sqlite_repository import SQLiteRepository

from tests import memory_kg_reader_cases as cases


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    knowledge_counts_cache.invalidate()
    return cases.build_world(repo, postgres=False)


def test_list_page_rows_and_total_follow_the_viewer(world):
    cases.check_list_page_and_total(world)


def test_counts_are_shared_plus_own_memory(world):
    cases.check_counts(world)


def test_kg_ready_is_shared_or_own_memory(world):
    cases.check_has_kg(world)


def test_no_viewer_counts_are_one_cached_statement_and_zero_live_reads(world):
    cases.check_none_counts_zero_live_reads(world)


def test_fts_filters_before_the_limit(world):
    cases.check_fts_filters_before_the_limit(world)


def test_fts_ceiling_and_viewer_combine(world):
    cases.check_fts_ceiling_and_viewer(world)


def test_object_meta_rows_viewer_form_is_notebook_bound(world):
    cases.check_object_meta_rows_binding(world)


def test_edge_centrality_rows_hold_no_memory(world):
    cases.check_edge_centrality_is_shared(world)


def test_object_evidence_rows_can_skip_the_evidence(world):
    cases.check_object_evidence_rows(world)


def test_enrich_evidence_can_resolve_sources_only(world):
    cases.check_enrich_evidence_sources_only(world)


def test_enrich_evidence_sources_only_narrows_the_owner_form(world):
    cases.check_enrich_evidence_sources_only_with_owner(world)


def test_enrich_evidence_sources_only_is_primary_key_driven(world):
    with cases.connect(world) as db:
        plan = [
            row["detail"] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT id, source_id FROM source_elements "
                "WHERE id IN (?,?)", ("el-src-s", "el-src-mb"),
            ).fetchall()
        ]
    assert len(plan) == 1 and plan[0].startswith("SEARCH source_elements USING ") and (
        "(id=?)" in plan[0]), plan


def test_object_evidence_rows_without_evidence_is_primary_key_driven(world):
    ids = sorted(world.names)[:3]
    with cases.connect(world) as db:
        plan = [
            row["detail"] for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT id, source_id FROM knowledge_objects "
                "WHERE id IN (?,?,?)", ids,
            ).fetchall()
        ]
    assert plan == ["SEARCH knowledge_objects USING INDEX "
                    "sqlite_autoindex_knowledge_objects_1 (id=?)"], plan


def test_the_counts_memo_carries_no_viewer(world):
    cases.check_counts_cache_holds_no_viewer(world)


def test_graph_readers_follow_the_viewer(world):
    cases.check_graph_readers(world)


def test_own_memory_overlay_reads_only_the_viewers_memory(world):
    cases.check_own_memory_overlay(world)


def test_search_legs_follow_the_viewer(world):
    cases.check_search_legs(world)


def test_neighbour_relations_follow_the_viewer(world):
    cases.check_neighbour_relations(world)


def test_shared_tooling_leaves_every_memory_row_out(world):
    cases.check_shared_tooling(world)


@pytest.mark.parametrize("viewer_kind", ["member", "closed"])
def test_viewer_statements_are_index_driven_without_analyze(world, viewer_kind):
    """SQLite plans (no ANALYZE, as in production): every changed statement
    reaches ``sources`` / ``memory_items`` by primary key or by the notebook's
    Memory sources, and never walks a whole table; the reads DRIVEN by the
    Memory sources start from ``idx_sources_nb_hidden_type``."""
    viewer = world.a if viewer_kind == "member" else ""
    ids = sorted(world.names)
    store = cases.store(world)
    with cases.connect(world) as db:
        recorder = cases.Recorder(db)
        cases.invalidate_counts(world)
        store.list_knowledge_page(recorder, world.nb, "concept", None, 0, 5, viewer_id=viewer)
        store.graph_node_rows(recorder, world.nb, viewer_id=viewer)
        store.unified_graph_rows(recorder, world.nb, viewer_id=viewer)
        store.relations_for_notebook(recorder, world.nb, viewer_id=viewer)
        store.neighbor_relation_rows(recorder, world.nb, ids, viewer_id=viewer)
        store.object_meta_rows_for_notebook(recorder, world.nb, ids, viewer_id=viewer)
        store.object_meta_rows(recorder, ids, notebook_id=world.nb, viewer_id=viewer)
        store.fts_search(recorder, world.nb, "kgtoken", 5, viewer_id=viewer)
        store.community_context_rows(recorder, world.nb, ids)
        store.duplicate_member_rows(recorder, world.nb, ids)
        cases.queries(world).notebook_has_kg(recorder, world.nb2, viewer_id=viewer)
        if viewer:
            store.unified_graph_rows(recorder, world.nb, viewer_id=viewer, own_memory_only=True)
            store.relations_for_notebook(
                recorder, world.nb, viewer_id=viewer, own_memory_only=True)
        statements = list(recorder.statements)
    checked = driven = 0
    with cases.connect(world) as db:
        for sql in statements:
            if "kg_mutation_seq" in sql:
                continue
            # The plan depends on the text only; bind a string per placeholder.
            plan = [
                row["detail"] for row in db.execute(
                    "EXPLAIN QUERY PLAN " + sql, tuple("x" for _ in range(sql.count("?")))
                ).fetchall()
            ]
            for line in plan:
                if line == "SCAN t":
                    continue  # the count statement's grouped ``total`` CTE
                if line.startswith("SCAN "):
                    # Only the id-list JSON and the FTS5 match may be scanned.
                    assert line.startswith(
                        ("SCAN json_each", "SCAN kg_objects_fts", "SCAN CONSTANT ROW")
                    ), (sql, plan)
            if " FROM sources s " in sql:
                # driven BY the notebook's Memory sources (join order pinned,
                # or an uncorrelated id list): ``s`` is read through the
                # partial hidden-type index, never by primary key per object
                # nor by another notebook index
                source_steps = [line for line in plan if line.startswith("SEARCH s ")]
                assert source_steps and all(
                    "idx_sources_nb_hidden_type" in line for line in source_steps
                ), (sql, plan)
                driven += 1
            checked += 1
    assert checked >= 12
    # member: the cold two-half count of nb and of nb2 (kg_ready's shared half
    # comes from the count cache), the own-Memory count half (list total),
    # kg_ready's own-Memory probe on the Memory-only nb2, the two overlay reads
    # and the FTS exclusion list; closed channel: the two cold counts and the
    # FTS exclusion list only
    assert driven == (7 if viewer else 3)


def test_no_viewer_issues_the_pre_isolation_statements(world):
    members = bind_ids(sorted(world.names)[:1])
    cases.check_none_statements_unchanged(world, {
        "graph_node_rows": [
            "SELECT id, object_type, status, payload FROM knowledge_objects "
            "WHERE notebook_id = ? AND status != 'deprecated'"],
        "unified_graph_rows": [
            "SELECT id, object_type, payload, status FROM knowledge_objects "
            "WHERE notebook_id=? AND status!='deprecated'"],
        "relations_for_notebook": ["SELECT * FROM knowledge_relations WHERE notebook_id = ?"],
        "neighbor_relation_rows": [
            "SELECT source_object_id, target_object_id, edge_type FROM knowledge_relations "
            f"WHERE notebook_id=? AND ({drive_by('source_object_id', members)} "
            f"OR {drive_by('target_object_id', members)})"],
        "object_meta_rows_for_notebook": [
            "SELECT id, object_type, payload FROM knowledge_objects "
            "WHERE notebook_id=? AND id IN (?)"],
        "object_meta_rows": [
            "SELECT id, object_type, status, payload FROM knowledge_objects WHERE id IN (?)"],
        "list_knowledge_page_rows": [
            "SELECT * FROM knowledge_objects WHERE notebook_id = ? AND object_type = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ? OFFSET ?"],
        "fts_search": [
            "SELECT object_id, name, bm25(kg_objects_fts) AS rank "
            "FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? "
            "ORDER BY rank LIMIT ?"],
        "notebook_has_kg": [
            "SELECT EXISTS(SELECT 1 FROM knowledge_objects WHERE notebook_id = ?)"],
    })
