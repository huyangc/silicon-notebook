"""Named-share (editor) storage layer on SQLite (share-edit P1-T5).

The scenarios live in ``tests/notebook_collaborator_store_cases.py`` and run
unchanged on PostgreSQL through
``tests/postgres/test_notebook_collaborator_store_pg.py`` (which adds the
row-lock races). This file adds the SQLite-only regression on the shared
``_require_notebook_on`` helper.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.ports import NotebookNotFoundError
from app.repositories.sqlite.group_store import GroupStore
from app.services.sqlite_repository import SQLiteRepository
from tests.memory_purge_cases import EMBED_DIM
from tests.notebook_collaborator_store_cases import (
    CASES,
    MONKEYPATCH_CASES,
    build_world,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'collab.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_collaborator_store(repo, case):
    CASES[case](build_world(repo, postgres=False))


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_notebook_collaborator_store_recorded(repo, monkeypatch, case):
    MONKEYPATCH_CASES[case](build_world(repo, postgres=False), monkeypatch)


def test_sqlite_create_grant_and_share_request_unchanged(repo):
    """The named share got a helper of its own (``_require_owned_live_notebook_on``:
    live and owned). The shared ``_require_notebook_on`` -- used by
    ``create_grant`` and the share request -- still answers existence only: a
    notebook being copied passes it, a missing one does not, and a group grant
    on a live notebook is created exactly as before."""
    world = build_world(repo, postgres=False)
    groups = repo._runtime.groups
    world.write("UPDATE notebooks SET status='copying' WHERE id=?", (world.notebook,))
    with repo._runtime.database.write() as db:
        GroupStore._require_notebook_on(db, world.notebook)  # existence only
        with pytest.raises(NotebookNotFoundError):
            GroupStore._require_notebook_on(db, world.notebook, require_live=True)
        with pytest.raises(NotebookNotFoundError):
            GroupStore._require_notebook_on(db, "nb-missing")
        with pytest.raises(NotebookNotFoundError):
            GroupStore._require_owned_live_notebook_on(
                db, world.notebook, world.uid("owner")
            )
    world.write("UPDATE notebooks SET status='draft' WHERE id=?", (world.notebook,))
    group = groups.create_group(
        name="g", kind="project", description="", created_by=world.uid("owner")
    )
    grant = groups.create_grant(
        world.notebook, principal_type="group", principal_id=group["id"],
        role="viewer", created_by=world.uid("owner"), admin_user_id=world.uid("owner"),
    )
    assert grant["principal_type"] == "group"
    assert [row["id"] for row in groups.list_grants(world.notebook)] == [grant["id"]]
