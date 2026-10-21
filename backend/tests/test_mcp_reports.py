"""``list_reports`` / ``get_report`` on SQLite.

The scenarios live in ``tests/mcp_report_cases.py`` and also run on PostgreSQL
in ``tests/postgres/test_mcp_reports_pg.py``.
"""
from __future__ import annotations

import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.mcp_report_cases import CASES


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository

    built = build_env(f"sqlite:///{tmp_path / 'mcp.db'}", tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_report_scenario(env, case):
    await run_case(env, CASES[case])
