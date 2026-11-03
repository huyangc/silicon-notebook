"""``closes_when_shared`` on the mount projection, on SQLite (share-edit P1-T8).

The case lives in ``tests/mount_closes_when_shared_cases.py`` and runs
unchanged on PostgreSQL through
``tests/postgres/test_mount_closes_when_shared_pg.py``. This file also pins
the field on the HTTP projection (``GET /notebooks/{id}/bases``).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.notebooks import MountedBase
from app.services.sqlite_repository import SQLiteRepository
from tests.mount_closes_when_shared_cases import (
    closes_when_shared_truth_table,
    retrieval_path_does_not_compute_it,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mounts.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def test_closes_when_shared_truth_table(repo):
    closes_when_shared_truth_table(repo, postgres=False)


def test_retrieval_path_does_not_compute_closes_when_shared(repo, monkeypatch):
    retrieval_path_does_not_compute_it(repo, postgres=False, monkeypatch=monkeypatch)


def test_mounted_base_carries_closes_when_shared():
    """The response model keeps the field (default False for an edge built
    without it) -- the warning before granting an editor reads it from
    ``GET /notebooks/{id}/bases``."""
    edge = MountedBase(id="nb", name="n", closes_when_shared=True)
    assert edge.model_dump()["closes_when_shared"] is True
    assert MountedBase(id="nb", name="n").closes_when_shared is False
