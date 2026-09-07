from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
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


def test_summary_query_keeps_list_kg_building_false(repo):
    notebook = repo.create_notebook(NotebookCreate(name="summary"))
    repo._kg_building.add(notebook.id)
    listed = {item.id: item for item in repo.list_notebooks()}
    assert listed[notebook.id].kg_building is False
    assert repo.get_notebook(notebook.id).kg_building is True


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
    repo.replace_notebook_bases(other.id, [base.id], "user-local")
    summary = repo.get_notebook(other.id)
    assert [b.name for b in summary.base_notebooks] == ["基准库"]
    assert summary.base_kg_available is False
    assert summary.tier == "personal"
    assert repo.get_notebook(base.id).tier == "base"


def test_summary_source_and_pending_counts_exclude_derived_content(repo):
    notebook = repo.create_notebook(NotebookCreate(name="mixed sources"))
    now = "2026-07-20T00:00:00"
    with repo._write() as db:
        for source_id, source_type in (
            ("s-uploaded", "document"),
            ("s-memory", "memory"),
            ("s-knowhow", "knowhow"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    source_id,
                    notebook.id,
                    source_id,
                    source_type,
                    "ready",
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"e-{source_id}", source_id, "paragraph", "p1", "text", now),
            )
    from app.repositories.sqlite import knowledge_counts_cache

    knowledge_counts_cache.invalidate(notebook.id)
    summary = repo.get_notebook(notebook.id)
    assert summary.counts["sources"] == 1
    assert summary.kg_pending_sources == 1


# ---------------------------------------------------------------------------
# Task 4 delegation tests: NotebookSummaryQuery / NotebookCatalogService now
# hold ZERO SQL — every projection primitive is a QueryStore method reached
# through the SAME `repo._runtime.queries` instance the summary query was
# constructed with.  These spies prove each public op (get_notebook /
# list_notebooks) still routes to the store method, so nobody can quietly
# re-inline the SQL (or point a projection at the wrong store method) without
# a red test.  Reached-via / arg assertions are tagged `# MUT` (mutation
# harness inverts them to prove they are load-bearing, not vacuous).
# ---------------------------------------------------------------------------
