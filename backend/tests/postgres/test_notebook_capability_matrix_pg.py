"""Capability x role matrix on PostgreSQL (share-edit P1-T3).

Runs the exact layers of ``tests/test_notebook_capability_matrix.py`` (defined
once in ``tests/notebook_capability_matrix_cases.py``) through the real routes
against the production PostgreSQL repository.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.notebook_capability_matrix_cases import CASES, build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_capability_matrix"),
]


@pytest.fixture
def client(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_capability_matrix_pg(client, monkeypatch, case):
    CASES[case](build_world(client, postgres=True), monkeypatch)
