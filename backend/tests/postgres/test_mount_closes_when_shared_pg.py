"""``closes_when_shared`` on the mount projection, on PostgreSQL (share-edit P1-T8).

Runs the case of ``tests/test_mount_closes_when_shared.py`` (defined once in
``tests/mount_closes_when_shared_cases.py``) against the production PostgreSQL
repository.
"""
from __future__ import annotations

import pytest

from tests.mount_closes_when_shared_cases import (
    closes_when_shared_truth_table,
    retrieval_path_does_not_compute_it,
)

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_closes_when_shared_truth_table(repo):
    closes_when_shared_truth_table(repo, postgres=True)


def test_retrieval_path_does_not_compute_closes_when_shared(repo, monkeypatch):
    retrieval_path_does_not_compute_it(repo, postgres=True, monkeypatch=monkeypatch)
