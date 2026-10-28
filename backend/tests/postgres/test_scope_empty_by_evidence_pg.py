"""The empty-scope refusal judges ticked reference libraries by evidence, on
PostgreSQL: every scenario of ``tests/scope_empty_by_evidence_cases.py``
through the real HTTP routes against the production PostgreSQL repository --
the narrowed graph probe (``notebook_has_usable_base_kg(..., among=...)``) and
the passage probe run here in their canonical dialect.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.report_share_disclosure_cases import build_world
from tests.scope_empty_by_evidence_cases import CASES

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_scope_empty_by_evidence"),
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
def test_scope_empty_by_evidence_scenario_pg(world, case):
    CASES[case](world)
