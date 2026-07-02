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
