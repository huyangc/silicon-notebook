"""Two people writing one notebook at the same time, on PostgreSQL (share-edit
P1-T10).

Runs every scenario of ``tests/test_editor_concurrency.py`` (defined once in
``tests/editor_concurrency_cases.py``) through the real routes against the
production PostgreSQL repository, including the running-analysis initiator
lookup (``running_initiator``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.editor_concurrency_cases import CASES, build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_editor_concurrency"),
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
def test_editor_concurrency_pg(client, case):
    CASES[case](build_world(client, postgres=True))
