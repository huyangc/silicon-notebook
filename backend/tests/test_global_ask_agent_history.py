"""Which global conversations an Agent token may list and read (plan D4/D5).

The scenarios live in ``tests/global_ask_agent_history_cases.py`` and run here
on SQLite (and on PostgreSQL in
``tests/postgres/test_global_ask_agent_history_pg.py``).
"""
from __future__ import annotations

import pytest

from tests.global_ask_agent_history_cases import CASES
from tests.mcp_answer_link_cases import build_env, run_case


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository

    built = build_env(f"sqlite:///{tmp_path / 'history.db'}", tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_global_agent_history_scenario(env, case):
    await run_case(env, CASES[case])
