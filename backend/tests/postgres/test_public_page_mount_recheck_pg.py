"""Public pages re-check the libraries they draw on, on PostgreSQL (E7-5: D-3,
D-2).

Runs the scenarios of ``tests/public_page_mount_recheck_cases.py`` through the
real HTTP routes against the production PostgreSQL repository.  The reads are
existing statements: the participant resolution (``mount_sql``) and the
EXPLAIN-pinned ``visible_source_owners``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.public_page_mount_recheck_cases import CASES
from tests.report_share_disclosure_cases import build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_public_page_mount_recheck"),
]


@pytest.fixture
def client(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # The image case needs the deployment to serve answer images.
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true")
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
def test_public_page_mount_recheck_scenario_pg(world, case):
    CASES[case](world)
