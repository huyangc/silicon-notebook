"""Which global conversations an Agent token may list and read, on PostgreSQL.

Runs the scenarios of ``tests/global_ask_agent_history_cases.py`` with
``create_app()`` on the production PostgreSQL repository.
"""
from __future__ import annotations

import pytest

from tests.global_ask_agent_history_cases import CASES
from tests.mcp_answer_link_cases import build_env, run_case

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_global_ask_agent_history"),
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
async def test_global_agent_history_scenario_pg(env, case):
    await run_case(env, CASES[case])
