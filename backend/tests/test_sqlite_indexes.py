import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _index_names(repo, table):
    with repo._connect() as db:
        return {row["name"] for row in db.execute(f"PRAGMA index_list({table})").fetchall()}


def test_notebook_scale_indexes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())

    assert "idx_sources_notebook_status" in _index_names(repo, "sources")
    assert "idx_sources_nb_parse_status" in _index_names(repo, "sources")
    assert "idx_source_elements_source" in _index_names(repo, "source_elements")
    assert "idx_knowledge_objects_nb_type_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_nb_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_source" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_relations_nb_source" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_relations_nb_target" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_embeddings_nb" in _index_names(repo, "knowledge_embeddings")
    assert "idx_element_embeddings_nb" in _index_names(repo, "element_embeddings")


# ---------------------------------------------------------------------------
# Version-probe composite indexes: (notebook_id, timestamp) on the four tables
# _scale_index_version aggregates over (COUNT + MAX). Without these, a cold
# cache miss on a GB-scale table forces a full per-row table fetch (no
# covering index) for both the COUNT and the MAX(timestamp) — measured
# 96-147s on a 490k-object deployment. See _scale_index_version's docstring.
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_version_probe_composite_indexes_exist(repo):
    assert "idx_knowledge_objects_nb_updated" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_relations_nb_created" in _index_names(repo, "knowledge_relations")
    assert "idx_chunks_nb_created" in _index_names(repo, "chunks")
    assert "idx_knowledge_embeddings_nb_created" in _index_names(repo, "knowledge_embeddings")


def _plan(db, sql, params):
    rows = db.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(r["detail"]) for r in rows)


def test_knowledge_objects_version_aggregate_uses_covering_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*), MAX(updated_at) FROM knowledge_objects WHERE notebook_id=?",
            ("nb-1",),
        )
    assert "idx_knowledge_objects_nb_updated" in plan, plan
    # A covering index scan must not also touch the base table (no "USING
    # INTEGER PRIMARY KEY" row lookups back to knowledge_objects).
    assert "TABLE knowledge_objects" not in plan or "COVERING INDEX" in plan, plan


def test_knowledge_relations_version_aggregate_uses_covering_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*), MAX(created_at) FROM knowledge_relations WHERE notebook_id=?",
            ("nb-1",),
        )
    assert "idx_knowledge_relations_nb_created" in plan, plan


def test_chunks_version_aggregate_uses_covering_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*), MAX(created_at) FROM chunks WHERE notebook_id=?",
            ("nb-1",),
        )
    assert "idx_chunks_nb_created" in plan, plan


def test_knowledge_embeddings_version_aggregate_uses_covering_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*), MAX(created_at) FROM knowledge_embeddings WHERE notebook_id=?",
            ("nb-1",),
        )
    assert "idx_knowledge_embeddings_nb_created" in plan, plan


# ---------------------------------------------------------------------------
# /analytics 看板 parse_status tally: SELECT parse_status, COUNT(*) FROM sources
# WHERE notebook_id=? GROUP BY parse_status ran a full sources table walk per
# board open (48,836 rows at scale) because idx_sources_notebook_status covers
# (notebook_id, status) not parse_status. idx_sources_nb_parse_status makes it
# index-only AND serves the GROUP BY from index order (no transient sort).
# ---------------------------------------------------------------------------

def test_sources_parse_status_group_by_uses_covering_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT parse_status, COUNT(*) FROM sources WHERE notebook_id=? "
            "GROUP BY parse_status",
            ("nb-1",),
        )
    assert "idx_sources_nb_parse_status" in plan, plan
    # Covering index scan: the tally never falls back to the sources base table.
    assert "COVERING INDEX" in plan, plan
    # (notebook_id, parse_status) order serves GROUP BY directly, so SQLite
    # builds no transient sort b-tree.
    assert "TEMP B-TREE" not in plan.upper(), plan


# ---------------------------------------------------------------------------
# C4 (hotpath cleanup, 4th index-bundle pass): answers/feedback/extraction_runs
# had ZERO indexes at all — notebook_analytics' three §16 COUNTs and the
# low_rated JOIN, and _extraction_warning's per-source-list-row ORDER BY +
# LIMIT 1, all forced a full table scan. See the CREATE INDEX comments in
# sqlite_repository.py's schema block for the per-index row-order audit.
# ---------------------------------------------------------------------------

def test_answers_feedback_extraction_runs_indexes_exist(repo):
    assert "idx_answers_nb" in _index_names(repo, "answers")
    assert "idx_answers_conversation" in _index_names(repo, "answers")
    assert "idx_feedback_nb_rating" in _index_names(repo, "feedback")
    assert "idx_feedback_answer" in _index_names(repo, "feedback")
    assert "idx_extraction_runs_source_created" in _index_names(repo, "extraction_runs")


def test_answers_notebook_count_uses_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*) FROM answers WHERE notebook_id=?",
            ("nb-1",),
        )
    assert "idx_answers_nb" in plan, plan


def test_answers_conversation_lookup_uses_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT question, payload FROM answers WHERE conversation_id = ? "
            "ORDER BY created_at ASC",
            ("conv-1",),
        )
    assert "idx_answers_conversation" in plan, plan


def test_feedback_notebook_rating_count_uses_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT COUNT(*) AS c FROM feedback WHERE notebook_id = ? AND rating = 'useful'",
            ("nb-1",),
        )
    assert "idx_feedback_nb_rating" in plan, plan


def test_feedback_answer_fk_cascade_uses_index(repo):
    """feedback.answer_id is FK ... ON DELETE CASCADE with no SQLite auto-index
    on FK columns — idx_feedback_answer is what lets the cascade (fired by
    delete_conversation's per-answer DELETE FROM answers) find child feedback
    rows without a full table scan. Verified via EXPLAIN QUERY PLAN on the
    DELETE itself, which surfaces the FK-cascade sub-scan plan."""
    with repo._connect() as db:
        plan = _plan(db, "DELETE FROM answers WHERE id=?", ("a-1",))
    assert "idx_feedback_answer" in plan, plan


def test_extraction_runs_latest_by_source_uses_index(repo):
    with repo._connect() as db:
        plan = _plan(
            db,
            "SELECT error_message FROM extraction_runs WHERE source_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            ("src-1",),
        )
    assert "idx_extraction_runs_source_created" in plan, plan


# ---------------------------------------------------------------------------
# Row-order audit sanity: notebook_analytics + _extraction_warning must return
# byte-identical results with the new indexes in place (they always existed on
# a fresh DB in this test process, but the assertion below pins the actual
# ordering behavior — LIMIT 1 picks the most-recent row, not an arbitrary one).
# ---------------------------------------------------------------------------

def test_extraction_warning_picks_most_recent_run(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("s1", nb.id, "t", "md", "ready", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r1", nb.id, "s1", "kg", "completed", "windows_failed=1/3",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        db.execute(
            "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,status,error_message,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("r2", nb.id, "s1", "kg", "completed", "",
             "2026-01-02T00:00:00", "2026-01-02T00:00:00"),
        )
    with repo._connect() as db:
        warning = repo._extraction_warning(db, "s1")
    # The most recent run (r2, created_at later) has no windows_failed token,
    # so the warning must be None — proves the index-backed ORDER BY DESC
    # LIMIT 1 still returns the newest row, not r1 (which would produce a
    # warning string if incorrectly selected).
    assert warning is None, (
        f"expected None (most recent run r2 has no failure token), got {warning!r}")


def test_notebook_analytics_counts_match_manual_tally(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        for i in range(3):
            db.execute(
                "INSERT INTO answers (id,notebook_id,question,payload,created_at) "
                "VALUES (?,?,?,?,?)",
                (f"a{i}", nb.id, f"q{i}", "{}", now),
            )
        db.execute(
            "INSERT INTO feedback (id,answer_id,notebook_id,rating,comment,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("f0", "a0", nb.id, "useful", "", now),
        )
        db.execute(
            "INSERT INTO feedback (id,answer_id,notebook_id,rating,comment,created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("f1", "a1", nb.id, "not_useful", "", now),
        )
    analytics = repo.notebook_analytics(nb.id)
    assert analytics.answers_total == 3
    assert analytics.feedback_useful == 1
    assert analytics.feedback_not_useful == 1
    assert analytics.low_rated_questions == ["q1"]
