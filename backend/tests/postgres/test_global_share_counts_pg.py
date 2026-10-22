"""The share dialog's exact numbers for a global conversation, on PostgreSQL.

Runs every scenario of ``tests/share_counts_cases.py`` -- the shared golden,
the differential against the public page over a conversation longer than the
page, and the authority cases -- through the real HTTP routes against the
production PostgreSQL repository. The statements' plans are pinned in
``test_share_window_explain_pins.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.share_counts_cases import CASES

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_global_share_counts"),
]


@pytest.fixture
def world(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app
    from tests.report_share_disclosure_cases import build_world

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield build_world(TestClient(create_app()), monkeypatch)
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_global_share_counts_scenario_pg(world, case):
    CASES[case](world)
