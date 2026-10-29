"""A deep copy and a ``.snbk`` import keep every source's element order, on real
PostgreSQL: the twin of ``tests/test_copy_element_order.py``, running the same
checks on the same world (``tests/copy_element_order_cases.py``)."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.repository_factory import create_repository
from tests import copy_element_order_cases as cases
from tests import notebook_import_cases as import_cases
from tests.postgres.test_notebook_archive_export_pg import BACKEND

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_copy_element_order"),
]


@pytest.fixture
def repo(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    for key, value in import_cases.ENV.items():
        monkeypatch.setenv(key, value)
    repository = create_repository(Settings())
    try:
        cases.seed(repository, BACKEND)
        yield repository
    finally:
        repository.close()


def test_deep_copy_keeps_every_readers_element_order(repo):
    cases.check_order_kept(repo, cases.deep_copy(repo))


def test_import_keeps_every_readers_element_order(repo, tmp_path, monkeypatch):
    cases.check_order_kept(repo, cases.export_and_import(repo, tmp_path, monkeypatch))
