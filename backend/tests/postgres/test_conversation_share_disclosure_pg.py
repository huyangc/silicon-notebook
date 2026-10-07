"""Conversation share disclosure and the public Memory marker on PostgreSQL
(E7-5, ruling M4).

Runs every scenario of ``tests/conversation_share_disclosure_cases.py`` --
notebook-scoped conversations, global conversations and the public report
marker -- through the real HTTP routes against the production PostgreSQL
repository.  The SQL behind them is not new: the count reads the
EXPLAIN-pinned ``memory_sources_for_source_ids`` statement, and the global
share window is the same keyset statement the public snapshot runs.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conversation_share_disclosure_cases import (
    GLOBAL_CASES,
    NOTEBOOK_CASES,
    REPORT_CASES,
)
from tests.report_share_disclosure_cases import build_world

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_conversation_share_disclosure"),
]

ALL_CASES = {**NOTEBOOK_CASES, **GLOBAL_CASES, **REPORT_CASES}


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


@pytest.mark.parametrize("case", sorted(ALL_CASES))
def test_conversation_share_disclosure_scenario_pg(world, case):
    ALL_CASES[case](world)
