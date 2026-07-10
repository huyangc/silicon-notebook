from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate, NotebookSearchResponse
from app.repositories.sqlite.query_store import QueryStore
from app.services.notebook_scale import NotebookScaleFacts
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'queries.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_query_store_owns_declared_query_implementations():
    expected = {
        "list_user_usage",
        "list_user_notebooks",
        "notebook_analytics",
        "pending_actions_projection_rows",
        "search_notebook",
        "load_notebook_scale_facts",
    }
    assert expected <= QueryStore.__dict__.keys()
    assert "__getattr__" not in QueryStore.__dict__


def test_facade_query_delegates_to_runtime(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="query delegation"))
    expected = NotebookSearchResponse(query="needle", hits=[])
    monkeypatch.setattr(
        repo._runtime.queries,
        "search_notebook",
        lambda notebook_id, query: expected,
    )
    assert repo.search_notebook(notebook.id, "needle") is expected


def test_scale_facts_delegate_preserves_object_identity(repo, monkeypatch):
    expected = NotebookScaleFacts(
        bytes=1,
        sources=2,
        chunks=3,
        nodes=4,
        edges=5,
    )
    monkeypatch.setattr(
        repo._runtime.queries,
        "load_notebook_scale_facts",
        lambda notebook_id: expected,
    )
    assert repo.load_notebook_scale_facts("nb-id") is expected
