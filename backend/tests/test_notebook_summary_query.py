from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookAnalytics, NotebookCreate, NotebookSearchResponse
from app.services.notebook_catalog import NotebookCatalogService, NotebookSummaryQuery
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'summaries.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_summary_query_owns_projection_implementations():
    expected = {"get", "list_for_user", "from_row"}
    assert expected <= NotebookSummaryQuery.__dict__.keys()
    assert "__getattr__" not in NotebookSummaryQuery.__dict__


def test_catalog_owns_declared_orchestration():
    expected = {
        "list_notebook_templates",
        "list_notebooks",
        "create_notebook",
        "get_notebook",
        "update_notebook",
        "delete_notebook",
        "mark_notebook_base",
        "set_notebook_personal",
        "notebook_analytics",
        "search_notebook",
    }
    assert expected <= NotebookCatalogService.__dict__.keys()
    assert "__getattr__" not in NotebookCatalogService.__dict__


def test_facade_kg_building_set_is_the_catalog_set(repo):
    assert repo._kg_building is repo._runtime.catalog.kg_building


def test_summary_query_keeps_list_kg_building_false(repo):
    notebook = repo.create_notebook(NotebookCreate(name="summary"))
    repo._kg_building.add(notebook.id)
    listed = {item.id: item for item in repo.list_notebooks()}
    assert listed[notebook.id].kg_building is False
    assert repo.get_notebook(notebook.id).kg_building is True


def test_facade_get_notebook_delegates_to_catalog(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="delegate"))
    expected = repo.get_notebook(notebook.id)
    monkeypatch.setattr(
        repo._runtime.catalog, "get_notebook", lambda notebook_id: expected
    )
    assert repo.get_notebook(notebook.id) is expected


def test_facade_list_notebooks_delegates_to_catalog(repo, monkeypatch):
    sentinel: list = []
    monkeypatch.setattr(repo._runtime.catalog, "list_notebooks", lambda: sentinel)
    assert repo.list_notebooks() is sentinel


def test_catalog_search_notebook_is_a_query_store_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="search"))
    expected = NotebookSearchResponse(query="needle", hits=[])
    monkeypatch.setattr(
        repo._runtime.queries,
        "search_notebook",
        lambda notebook_id, query: expected,
    )
    assert repo._runtime.catalog.search_notebook(notebook.id, "needle") is expected
    assert repo.search_notebook(notebook.id, "needle") is expected


def test_catalog_notebook_analytics_is_a_query_store_delegate(repo, monkeypatch):
    expected = NotebookAnalytics(
        answers_total=0,
        feedback_useful=0,
        feedback_not_useful=0,
        usefulness_rate=0.0,
        low_rated_questions=[],
        knowledge_counts={},
        source_status_counts={},
    )
    monkeypatch.setattr(
        repo._runtime.queries, "notebook_analytics", lambda notebook_id: expected
    )
    assert repo.notebook_analytics("nb-any") is expected


def test_get_raises_keyerror_for_missing_and_copying_rows(repo):
    summaries = repo._runtime.notebook_summaries
    with pytest.raises(KeyError):
        summaries.get("nb-missing")
    notebook = repo.create_notebook(NotebookCreate(name="half copied"))
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET status='copying' WHERE id=?", (notebook.id,))
    with pytest.raises(KeyError):
        summaries.get(notebook.id)


def test_list_for_user_scopes_to_owner(repo):
    mine = repo.create_notebook(NotebookCreate(name="mine"))
    summaries = repo._runtime.notebook_summaries
    owner_id = repo.current_user().id
    listed = summaries.list_for_user(owner_id)
    assert [item.id for item in listed] == [mine.id]
    assert listed[0].access == "owner"
    assert summaries.list_for_user("user-somebody-else") == []


def test_base_notebook_projection_survives_the_move(repo):
    base = repo.create_notebook(NotebookCreate(name="基准库"))
    other = repo.create_notebook(NotebookCreate(name="notes"))
    repo.mark_notebook_base(base.id)
    summary = repo.get_notebook(other.id)
    assert summary.base_notebook_name == "基准库"
    assert summary.base_kg_available is False
    assert summary.tier == "personal"
    assert repo.get_notebook(base.id).tier == "base"
