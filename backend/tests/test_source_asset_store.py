"""source-asset-linking Task 3: notebook_assets per-source write/query/delete.

Builds on Task 2's ``notebook_assets.source_id`` column (migration 19). This
task wires ``source_id`` through ``KnowhowStore.insert_notebook_asset`` and
adds ``source_asset_ids``/``delete_source_asset_rows`` so a MinerU-extracted
source's embedded images can be looked up and cleaned up by source, while
knowhow paste-in images (``source_id`` NULL) stay untouched. Exercised through
the facade's one-hop delegates, same construction pattern as
``test_knowhow_store.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "s"),
        )
    )


@pytest.fixture
def notebook_id(repo) -> str:
    return repo.create_notebook(NotebookCreate(name="n")).id


def test_insert_with_source_id_then_query_and_delete(repo, notebook_id):
    a1 = repo.insert_notebook_asset(
        notebook_id, "fig1.png", "image/png", 10, "u", source_id="src-1"
    )
    a2 = repo.insert_notebook_asset(
        notebook_id, "fig2.png", "image/png", 10, "u", source_id="src-1"
    )
    pasted = repo.insert_notebook_asset(
        notebook_id, "paste.png", "image/png", 10, "u"
    )  # source_id NULL (knowhow paste-in)

    assert set(repo.source_asset_ids("src-1")) == {a1, a2}

    deleted = repo.delete_source_asset_rows("src-1")

    assert set(deleted) == {a1, a2}
    assert repo.source_asset_ids("src-1") == []
    assert repo.get_notebook_asset(pasted) is not None  # untouched


def test_source_asset_ids_empty_for_unknown_source(repo, notebook_id):
    repo.insert_notebook_asset(notebook_id, "a.png", "image/png", 1, "u")
    assert repo.source_asset_ids("no-such-source") == []


def test_delete_source_asset_rows_missing_source_returns_empty(repo, notebook_id):
    assert repo.delete_source_asset_rows("no-such-source") == []


def test_insert_notebook_asset_without_source_id_still_persists_null(repo, notebook_id):
    asset_id = repo.insert_notebook_asset(notebook_id, "b.png", "image/png", 1, "u")
    asset = repo.get_notebook_asset(asset_id)
    assert asset["source_id"] is None
