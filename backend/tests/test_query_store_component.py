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


def _seed_source(repo, notebook_id, sid, *, source_type="document",
                 chunked_at=None, with_elements=True):
    NOW = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, status, "
            "parse_status, created_at, updated_at, chunked_at) "
            "VALUES (?, ?, ?, ?, 'extracted', 'extracted', ?, ?, ?)",
            (sid, notebook_id, sid, source_type, NOW, NOW, chunked_at),
        )
        if with_elements:
            db.execute(
                "INSERT INTO source_elements (id, source_id, element_type, "
                "location_label, text, metadata, created_at) "
                "VALUES (?, ?, 'paragraph', 'p1', 'body', '{}', ?)",
                (f"el-{sid}", sid, NOW),
            )


def test_sources_missing_chunks_flags_only_user_sources_with_elements_and_null_marker(repo):
    """H3(缺分块)候选集 = 有 elements 且 chunked_at IS NULL 的**用户**源。
    排除:①已置 marker(分块成功过)②无 elements(那是 H2 空源)③隐藏合成源
    (memory/knowhow 不走置位路径、chunked_at 恒 NULL,含进来会误报)。"""
    nb = repo.create_notebook(NotebookCreate(name="h3"))
    NOW = "2026-01-01T00:00:00"
    _seed_source(repo, nb.id, "damaged", chunked_at=None, with_elements=True)     # H3 命中
    _seed_source(repo, nb.id, "chunked", chunked_at=NOW, with_elements=True)      # 已分块→排除
    _seed_source(repo, nb.id, "empty", chunked_at=None, with_elements=False)      # 无 elements(H2)→排除
    _seed_source(repo, nb.id, "mem", source_type="memory", chunked_at=None)       # 隐藏源→排除
    _seed_source(repo, nb.id, "kh", source_type="knowhow", chunked_at=None)       # 隐藏源→排除

    with repo._connect() as db:
        result = QueryStore.sources_missing_chunks(db, nb.id)

    assert result == {"damaged"}


def test_sources_missing_chunks_scopes_to_the_notebook(repo):
    """跨 notebook 隔离:只报本 notebook 的候选,别的 notebook 的损坏源不串。"""
    nb1 = repo.create_notebook(NotebookCreate(name="nb1"))
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    _seed_source(repo, nb1.id, "d1", chunked_at=None, with_elements=True)
    _seed_source(repo, nb2.id, "d2", chunked_at=None, with_elements=True)

    with repo._connect() as db:
        assert QueryStore.sources_missing_chunks(db, nb1.id) == {"d1"}
        assert QueryStore.sources_missing_chunks(db, nb2.id) == {"d2"}
