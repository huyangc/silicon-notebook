"""MCP answer links through the real MCP entry (T7), on PostgreSQL.

Runs the scenarios of ``tests/mcp_answer_link_cases.py`` with ``create_app()``
on the production PostgreSQL repository: ``delivery="link"`` issues and reuses
through ``AnswerLinkStore`` only after ``get_ask``'s own checks, and every
refusal leaves no ``answer_links`` row.
"""
from __future__ import annotations

import pytest

from tests.mcp_answer_link_cases import CASES, build_env, run_case

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mcp_answer_links"),
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
async def test_mcp_answer_link_scenario_pg(env, case):
    await run_case(env, CASES[case])
