"""Task 26 — the consolidated explicit compatibility facade contract.

``SQLiteRepository`` resolves root/storage, constructs ONE
:class:`RepositoryRuntime` and publishes every frozen Task-1 surface member as
an explicit delegate/property over that runtime.  The class body holds no SQL
and no dynamic dispatch (``__getattr__`` / dispatch tables) — every member is
a statically visible delegate, and the module keeps re-exporting the frozen
Task-1 compatibility imports as the SAME objects.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services import repository, sqlite_repository
from app.services.sqlite_repository import SQLiteRepository

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "facade_surface.json"
)


def frozen_surface() -> dict[str, dict]:
    assert FIXTURE.is_file(), f"missing frozen facade surface: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("facade-contract")
    return SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path / 't.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        auth_optional=True,
    ))


def test_facade_matches_frozen_surface_manifest(repo):
    for name, contract in frozen_surface().items():
        if contract.get("scope") == "module":
            module = (
                repository
                if contract["modules"] == ["app.services.repository"]
                else sqlite_repository
            )
            assert hasattr(module, name), name
            continue
        assert hasattr(SQLiteRepository, name) or hasattr(repo, name), name
        if contract["kind"] == "method":
            assert str(inspect.signature(getattr(SQLiteRepository, name))) == (
                contract["signature"]
            ), name


def test_facade_has_no_getattr_or_sql():
    source = inspect.getsource(SQLiteRepository)
    assert "def __getattr__" not in source
    assert ".execute(" not in source
    assert ".executemany(" not in source
    assert ".executescript(" not in source


def test_module_reexports_the_frozen_task1_imports():
    from app.repositories.sqlite import migrations
    from app.services import (
        knowledge_contracts,
        knowledge_lifecycle,
        sqlite_identity,
        sqlite_notebook_sharing,
    )
    from app.services.retrieval import RetrievedKnowledge

    assert sqlite_repository.SQLiteRepository is SQLiteRepository
    assert sqlite_repository.SCHEMA_VERSION == migrations.SCHEMA_VERSION
    assert sqlite_repository.UploadedSourceFile is repository.UploadedSourceFile
    assert callable(sqlite_repository._now)
    assert callable(sqlite_repository._new_id)
    assert sqlite_repository._fast_loads is knowledge_lifecycle._fast_loads
    assert sqlite_repository._REQUEST_USER is sqlite_identity._REQUEST_USER
    assert sqlite_repository.set_request_user is sqlite_identity.set_request_user
    assert sqlite_repository.reset_request_user is sqlite_identity.reset_request_user
    assert sqlite_repository.USABLE_STATUSES is knowledge_contracts.USABLE_STATUSES
    assert sqlite_repository.KNOWLEDGE_STATUSES is knowledge_contracts.KNOWLEDGE_STATUSES
    assert sqlite_repository.KnowledgeGraphTooLargeError is (
        knowledge_contracts.KnowledgeGraphTooLargeError
    )
    assert sqlite_repository._COPY_CHUNK == 1000
    assert sqlite_repository._remap_json_ids is (
        sqlite_notebook_sharing._remap_json_ids
    )
    assert sqlite_repository.RetrievedKnowledge is RetrievedKnowledge
