"""MCP answer links, service layer (T5), on PostgreSQL.

Runs the scenarios of ``tests/answer_link_service_cases.py`` against the
production PostgreSQL repository: issuing through ``AnswerLinkStore`` and every
re-check the anonymous resolve runs (token liveness, tiers, allowlist, read
access, mounts, Memory replay, the global snapshot gate).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.answer_link_service_cases import CASES, make_env

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_answer_link_service"),
]


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
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
        yield make_env(TestClient(create_app()), deps.repository())
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_answer_link_service_scenario_pg(env, case):
    CASES[case](env)
