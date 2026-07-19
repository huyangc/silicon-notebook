from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate, NotebookUpdate
from app.repositories.sqlite.notebook_store import NotebookStore
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'notebooks.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_notebook_store_owns_row_level_sql():
    expected = {
        "create_row",
        "get_row",
        "update_row",
        "set_tier",
        "delete_row_and_orphan_embeddings",
    }
    assert expected <= NotebookStore.__dict__.keys()
    assert "__getattr__" not in NotebookStore.__dict__


def test_notebook_store_shares_runtime_database(repo):
    assert repo._runtime.notebook_store.database is repo._runtime.database


def test_create_row_persists_owner_and_auto_purpose_flag(repo):
    store = repo._runtime.notebook_store
    notebook_id = store.create_row(NotebookCreate(name="row", purpose="  "), "user-local")
    row = store.get_row(notebook_id)
    assert row["name"] == "row"
    assert row["created_by"] == "user-local"
    assert row["status"] == "draft"
    assert row["purpose"] == ""
    assert row["purpose_auto"] == 1

    manual_id = store.create_row(NotebookCreate(name="manual", purpose="手写"), "user-local")
    assert store.get_row(manual_id)["purpose_auto"] == 0


def test_get_row_hides_copying_rows_unless_asked(repo):
    notebook = repo.create_notebook(NotebookCreate(name="copying"))
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET status='copying' WHERE id=?", (notebook.id,))
    store = repo._runtime.notebook_store
    with pytest.raises(KeyError):
        store.get_row(notebook.id)
    assert store.get_row(notebook.id, include_copying=True)["status"] == "copying"
    with pytest.raises(KeyError):
        store.get_row("nb-missing", include_copying=True)


def test_update_row_applies_only_provided_fields(repo):
    notebook = repo.create_notebook(NotebookCreate(name="before", purpose="p"))
    store = repo._runtime.notebook_store
    store.update_row(notebook.id, NotebookUpdate(name="  ", purpose="用户改写"))
    row = store.get_row(notebook.id)
    assert row["name"] == "Untitled notebook"
    assert row["purpose"] == "用户改写"
    assert row["purpose_auto"] == 0

    before = dict(row)
    store.update_row(notebook.id, NotebookUpdate())
    after = store.get_row(notebook.id)
    assert dict(after) == before  # empty payload → no write at all


def test_set_tier_base_no_longer_globally_unique_and_personal_is_local(repo):
    """多领域基准库:set_tier(tier='base') 不再全局唯一 —— 把 B 设为 base 不应
    降级 A(各领域各有自己的公共知识库)。tier='personal' 仍是纯本地重置,
    幂等,且不影响其它笔记本的 tier。"""
    first = repo.create_notebook(NotebookCreate(name="A"))
    second = repo.create_notebook(NotebookCreate(name="B"))
    store = repo._runtime.notebook_store
    store.set_tier(first.id, "base")
    assert store.get_row(first.id)["tier"] == "base"
    store.set_tier(second.id, "base")
    assert store.get_row(second.id)["tier"] == "base"
    assert store.get_row(first.id)["tier"] == "base"
    store.set_tier(second.id, "personal")
    assert store.get_row(second.id)["tier"] == "personal"
    assert store.get_row(first.id)["tier"] == "base"


def test_delete_row_returns_file_paths_and_clears_orphan_embeddings(repo, tmp_path):
    notebook = repo.create_notebook(NotebookCreate(name="doomed"))
    keeper = repo.create_notebook(NotebookCreate(name="keeper"))
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, file_path, created_at, updated_at) "
            "VALUES ('src-doomed', ?, 't', 'pdf', ?, '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
            (notebook.id, str(tmp_path / "doomed.pdf")),
        )
        # knowledge_embeddings has no FK to notebooks → must be deleted explicitly.
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
            "VALUES ('ko-doomed', ?, '[]', '2026-01-01T00:00:00')",
            (notebook.id,),
        )
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
            "VALUES ('ko-keeper', ?, '[]', '2026-01-01T00:00:00')",
            (keeper.id,),
        )

    paths = repo._runtime.notebook_store.delete_row_and_orphan_embeddings(notebook.id)

    assert paths == [str(tmp_path / "doomed.pdf")]
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM notebooks WHERE id=?", (notebook.id,)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM sources WHERE notebook_id=?", (notebook.id,)
        ).fetchone()[0] == 0
        remaining = [
            row["object_id"]
            for row in db.execute("SELECT object_id FROM knowledge_embeddings").fetchall()
        ]
    assert remaining == ["ko-keeper"]
