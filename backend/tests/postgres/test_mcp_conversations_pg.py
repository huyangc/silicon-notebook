"""``list_conversations`` / ``get_conversation`` through the real MCP entry, on
PostgreSQL.

Runs the scenarios of ``tests/mcp_conversation_cases.py`` with ``create_app()``
on the production PostgreSQL repository, so conversations, answers, Ask jobs and
global turns are read back through the PostgreSQL stores.
"""
from __future__ import annotations

import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.mcp_conversation_cases import CASES

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mcp_conversations"),
]


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings

    built = build_env(postgres_scope.url, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository().close()
        repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_conversation_scenario_pg(env, case):
    await run_case(env, CASES[case])
