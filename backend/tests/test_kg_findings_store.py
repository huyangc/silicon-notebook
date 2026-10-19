"""KG analysis findings store primitives on SQLite (v97; design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §8).

The world and every expected value live in ``tests/kg_findings_store_cases.py``;
``tests/postgres/test_kg_findings_store_pg.py`` runs the same checks on
PostgreSQL against the same literals, which is what holds the two backends to
identical return values.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.repositories.sqlite import unified_kg_store as store_module
from app.services.sqlite_repository import SQLiteRepository
from tests import kg_findings_store_cases as cases


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close_local()


def test_findings_are_replaced_wholesale_with_the_ledger(repo):
    cases.check_findings_replace_wholesale(repo, postgres=False)


def test_findings_writes_refuse_malformed_rows_and_enforce_the_cap(repo, monkeypatch):
    cases.check_findings_write_guards(
        repo, postgres=False, monkeypatch=monkeypatch, store_module=store_module)


def test_marks_upsert_delete_prune_and_cap(repo, monkeypatch):
    cases.check_marks(
        repo, postgres=False, monkeypatch=monkeypatch, store_module=store_module)


def test_the_mark_cap_holds_under_concurrent_writers(repo, monkeypatch):
    cases.check_mark_cap_under_concurrency(
        repo, postgres=False, monkeypatch=monkeypatch, store_module=store_module)


def test_indexing_publication_clears_findings_and_the_code_version(repo):
    cases.check_indexing_publish_clears_findings(repo, postgres=False)


def test_source_states_read_the_latest_extraction_and_hide_synthetic_titles(repo):
    cases.check_source_states(repo, postgres=False)


def test_display_names_come_from_the_sources_own_visible_objects(repo):
    cases.check_display_names(repo, postgres=False)


def test_display_names_are_batch_invariant(repo):
    cases.check_display_names_across_batches(repo, postgres=False)


def test_merge_candidate_states_filter_the_notebook_slice(repo):
    cases.check_candidate_states(repo, postgres=False)


def test_code_version_stamp_is_guarded_by_the_input_version(repo):
    cases.check_code_version_stamp(repo, postgres=False)


def test_the_check_constraint_backs_the_mark_kind_vocabulary(repo):
    """The write entry refuses an unknown kind first; the table refuses it too."""
    world = cases.seed(repo, postgres=False)
    with pytest.raises(sqlite3.IntegrityError):
        with repo._runtime.database.write() as db:
            db.execute(
                "INSERT INTO kg_analysis_marks (notebook_id, rule_id, subject_key, "
                "kind, at_kg_seq, created_at) VALUES (?,?,?,?,?,?)",
                (world.nb, "F1", "src-a", "bogus", 1, cases.T1),
            )


# Production-shaped planner statistics for the tables the id-list statements
# touch (49k sources and objects; the shape tests/test_sqlite_id_binding.py
# installs), so the plan pins hold in both states production SQLite can be in:
# no statistics at all (the repository never runs ANALYZE) and statistics.
_PRODUCTION_STATS = [
    ("concept_clusters", "idx_clusters_member", "49000 1"),
    ("concept_clusters", "idx_clusters_nb_canonical_member_gen", "49000 49000 5 1 1"),
    ("concept_clusters", "idx_clusters_nb_canonical_name_lower", "49000 49000 5"),
    ("concept_clusters", "idx_clusters_nb_created_gen", "49000 49000 49000 49000"),
    ("concept_clusters", "sqlite_autoindex_concept_clusters_1", "49000 1"),
    ("concept_clusters", "uq_clusters_nb_type_member_generation", "49000 49000 49000 1 1"),
    ("knowledge_objects", "idx_knowledge_objects_nb_status", "49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_nb_type_created", "49008 49008 49008 49008 1"),
    ("knowledge_objects", "idx_knowledge_objects_nb_type_status", "49008 49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_nb_updated", "49008 49008 49008"),
    ("knowledge_objects", "idx_knowledge_objects_source", "49008 1"),
    ("knowledge_objects", "idx_knowledge_objects_source_id", "49008 1 1"),
    ("knowledge_objects", "sqlite_autoindex_knowledge_objects_1", "49008 1"),
    ("sources", "idx_sources_nb_hidden_type", "20 20 20"),
    ("sources", "idx_sources_nb_parse_status", "49078 24539 24539"),
    ("sources", "idx_sources_notebook_created", "49078 24539 24539"),
    ("sources", "idx_sources_notebook_status", "49078 24539 24539"),
    ("sources", "sqlite_autoindex_sources_1", "49078 1"),
    ("extraction_runs", "idx_extraction_runs_source_created", "98000 2 1"),
    ("extraction_runs", "idx_extraction_runs_notebook", "98000 49000"),
    ("extraction_runs", "sqlite_autoindex_extraction_runs_1", "98000 1"),
    ("concept_merge_candidates", "idx_candidates_nb_status", "20000 10000 2500"),
    ("concept_merge_candidates", "sqlite_autoindex_concept_merge_candidates_1", "20000 1"),
]


@pytest.mark.parametrize("statistics", ["none", "production"])
def test_id_list_statements_keep_their_key_plans(repo, monkeypatch, statistics):
    """The three statements that bind an id list (``id_binding``) drive by the
    keys they exist to read, with and without planner statistics:

    * source states: one primary-key seek per listed source (``drive_by``), the
      latest extraction run by ``idx_extraction_runs_source_created``;
    * display names: the clustered leg seeks ``(notebook_id, canonical_id)``
      per listed canonical, the unclustered leg seeks objects by primary key,
      and its "no published cluster row" probe goes by the member -- never by
      the notebook's whole cluster partition;
    * candidate states: the notebook's candidate slice by
      ``idx_candidates_nb_status``; the list only filters it.
    No step scans a table."""
    from app.repositories.sqlite import database as sqlite_database

    rt = repo._runtime
    world = cases.seed(repo, postgres=False)
    if statistics == "production":
        with rt.database.write() as db:
            db.execute("ANALYZE")
            tables = sorted({t for t, _i, _s in _PRODUCTION_STATS})
            marks = ",".join("?" * len(tables))
            db.execute(f"DELETE FROM sqlite_stat1 WHERE tbl IN ({marks})", tables)
            db.executemany(
                "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?,?,?)",
                _PRODUCTION_STATS)
        rt.database.close_local()
    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "json_each(?)" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    cases.source_states(rt, world.nb)
    cases.display_names(rt, world.nb)
    cases.candidate_states(rt, world.nb, ("confirmed", "rejected", "deferred"),
                           ["K-rmsnorm"])
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 3
    db = rt.database.connect()
    plans = [
        [row[3] for row in sqlite3.Connection.execute(db, "EXPLAIN QUERY PLAN " + sql, params)]
        for sql, params in captured
    ]
    states, names, candidates = plans
    assert "SEARCH s USING INDEX sqlite_autoindex_sources_1 (id=?)" in states, states
    assert any("idx_extraction_runs_source_created (source_id=?)" in step
               for step in states), states
    assert any("idx_clusters_nb_canonical_member_gen (notebook_id=? AND canonical_id=?)"
               in step for step in names), names
    assert names.count(
        "SEARCH o USING INDEX sqlite_autoindex_knowledge_objects_1 (id=?)") == 2, names
    assert "SEARCH cc USING INDEX idx_clusters_member (member_object_id=?)" in names, names
    assert any("idx_candidates_nb_status (notebook_id=? AND status=?)" in step
               for step in candidates), candidates
    for plan in plans:
        assert not [step for step in plan
                    if step.startswith("SCAN ") and "json_each" not in step], plan


@pytest.mark.parametrize("statistics", ["none", "production"])
def test_old_detail_read_stays_on_the_primary_key(repo, monkeypatch, statistics):
    """The publish transaction's one in-transaction read (old detail, for
    first-seen inheritance and the comparison) is bounded by the row cap and
    walks the primary key: a prefix SEARCH on its automatic index, no sort, no
    scan -- with no planner statistics (production SQLite databases are never
    ANALYZEd) and with production-shaped ones. The statement pinned is the one
    the store actually executes, captured on the way through, inside a write
    transaction: it carries no write-transaction guard."""
    from app.repositories.sqlite import database as sqlite_database

    rt = repo._runtime
    world = cases.seed(repo, postgres=False)
    cases.replace(rt, world.nb, cases.FINDINGS_V1)
    if statistics == "production":
        with rt.database.write() as db:
            db.execute("ANALYZE")
            db.execute("DELETE FROM sqlite_stat1 WHERE tbl='kg_analysis_findings'")
            db.execute(
                "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES "
                "('kg_analysis_findings', 'sqlite_autoindex_kg_analysis_findings_1', "
                "'200000 500 100 1')")
        rt.database.close_local()
    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "FROM kg_analysis_findings WHERE" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    with rt.database.write() as db:
        assert len(rt.unified_kg.kg_analysis_finding_rows(db, world.nb)) == 3
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 1
    sql, params = captured[0]
    assert params[-1] == store_module.KG_FINDINGS_ROWS_MAX + 1
    steps = [
        row[3] for row in sqlite3.Connection.execute(
            rt.database.connect(), "EXPLAIN QUERY PLAN " + sql, params)
    ]
    assert steps == [
        "SEARCH kg_analysis_findings USING INDEX "
        "sqlite_autoindex_kg_analysis_findings_1 (notebook_id=?)"
    ], steps
