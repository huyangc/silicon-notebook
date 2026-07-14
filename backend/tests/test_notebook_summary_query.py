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


def test_t4deleg_visible_source_count_delegate(repo, monkeypatch):
    """memory-kg-extract Task 5 retargets the counts["sources"] projection
    from the generic count_rows(table, column, value) primitive to the
    dedicated visible_source_count(notebook_id) — it excludes Memory-derived
    synthetic sources (source_type='memory'), which count_rows cannot express
    since it is a table-agnostic helper still used elsewhere (the facade's
    _count re-export). The delegation-proof spy moves with it."""
    notebook = repo.create_notebook(NotebookCreate(name="count-rows"))
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return 0

    monkeypatch.setattr(repo._runtime.queries, "visible_source_count", spy)
    repo.get_notebook(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_knowledge_type_count_rows_delegate(repo, monkeypatch):
    # Local import keeps the module-level import block (and its manifest-frozen
    # SQLiteRepository consumer line) unshifted.
    from app.repositories.sqlite.notebook_store import USABLE_STATUSES

    notebook = repo.create_notebook(NotebookCreate(name="ktype"))
    calls = []

    def spy(db, notebook_id, statuses):
        calls.append((notebook_id, statuses))
        return []

    monkeypatch.setattr(repo._runtime.queries, "knowledge_type_count_rows", spy)
    repo.get_notebook(notebook.id)
    assert calls and calls[0] == (notebook.id, USABLE_STATUSES)  # MUT


def test_t4deleg_notebook_has_kg_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="haskg"))
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return False

    monkeypatch.setattr(repo._runtime.queries, "notebook_has_kg", spy)
    repo.get_notebook(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_pending_kg_source_count_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="pending"))
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return 0

    monkeypatch.setattr(repo._runtime.queries, "pending_kg_source_count", spy)
    repo.get_notebook(notebook.id)
    assert calls and calls[0] == notebook.id  # MUT


def test_t4deleg_base_notebook_info_row_delegate(repo, monkeypatch):
    # base_notebook_info_row takes only the connection (no notebook_id), so the
    # strong delegation proof is that the store's returned tuple reaches the
    # NotebookSummary unaltered (name + kg-availability projection).
    notebook = repo.create_notebook(NotebookCreate(name="baseinfo"))
    calls = []

    def spy(db):
        calls.append(True)
        return ("SENTINEL-BASE", 1)

    monkeypatch.setattr(repo._runtime.queries, "base_notebook_info_row", spy)
    summary = repo.get_notebook(notebook.id)
    assert calls and summary.base_notebook_name == "SENTINEL-BASE" and summary.base_kg_available is True  # MUT


def test_t4deleg_summary_notebook_row_delegate(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="summaryrow"))
    store = repo._runtime.queries
    original = store.summary_notebook_row  # staticmethod -> plain function
    calls = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return original(db, notebook_id)

    monkeypatch.setattr(store, "summary_notebook_row", spy)
    summary = repo.get_notebook(notebook.id)
    assert calls and calls[0] == notebook.id and summary.id == notebook.id  # MUT


def test_t4deleg_owned_notebook_rows_delegate(repo, monkeypatch):
    repo.create_notebook(NotebookCreate(name="owned"))
    owner_id = repo.current_user().id
    calls = []

    def spy(db, user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr(repo._runtime.queries, "owned_notebook_rows", spy)
    repo.list_notebooks()
    assert calls and calls[0] == owner_id  # MUT


def test_t4deleg_joined_notebook_rows_delegate(repo, monkeypatch):
    # owned_notebook_rows runs for real (real notebook -> real from_row); only
    # the joined leg is spied, proving list_for_user reaches BOTH store reads.
    repo.create_notebook(NotebookCreate(name="joined"))
    owner_id = repo.current_user().id
    calls = []

    def spy(db, user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr(repo._runtime.queries, "joined_notebook_rows", spy)
    repo.list_notebooks()
    assert calls and calls[0] == owner_id  # MUT
