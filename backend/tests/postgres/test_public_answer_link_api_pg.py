"""MCP answer links on the anonymous public routes (T6), on PostgreSQL.

Runs the scenarios of ``tests/public_answer_link_api_cases.py`` against the
production PostgreSQL repository: the ``alnk-`` page and image branches, the
410 for an expired row, the issue-path sweep, and the alias keyed by the link.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.public_answer_link_api_cases import CASES, make_env

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_public_answer_link_api"),
]


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield make_env(TestClient(create_app()), deps.repository())
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_public_answer_link_scenario_pg(env, case):
    CASES[case](env)
