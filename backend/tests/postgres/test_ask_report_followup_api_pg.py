"""Asking about a deep report over HTTP, on PostgreSQL (plan 2026-10-21 T9).

Runs every scenario of ``tests/ask_report_followup_api_cases.py`` -- the report
gate in front of the Ask entries, the pre-send check and the follow-up scope --
through the real HTTP routes against the production PostgreSQL repository. The
verdict behind the check reads the owner maps and the participant set in the
same statements the run reads them in; the follow-up scope reads
``visible_source_scope_snapshot`` and the mount roster.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.ask_report_followup_api_cases import CASES
from tests.report_share_disclosure_cases import build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ask_report_followup_api"),
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


@pytest.fixture
def world(client, monkeypatch):
    return build_world(client, monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_ask_report_followup_api_scenario_pg(world, case):
    CASES[case](world)
