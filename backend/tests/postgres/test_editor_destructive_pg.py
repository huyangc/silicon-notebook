"""What a named editor may not destroy, on PostgreSQL (share-edit P1-T4).

Runs every scenario of ``tests/test_editor_destructive.py`` (defined once in
``tests/editor_destructive_cases.py``) through the real routes against the
production PostgreSQL repository: the uploader check runs on the row the
teardown locks ``FOR UPDATE``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.editor_destructive_cases import CASES, build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_editor_destructive"),
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
def test_editor_destructive_pg(client, case):
    CASES[case](build_world(client, postgres=True))
