"""PostgreSQL twin of ``tests/test_mcp_memory_channel_e2e.py``.

The whole application (``create_app()``) runs on PostgreSQL, and the same
assertions go through the real MCP tool entry: without ``memory:read`` the
Memory channel is closed inside the worker (zero Memory store queries, no
Memory item, text or projection), with it Alice's Memory is retrieved as
before, and the other member of the shared notebook never sees it.
"""
from __future__ import annotations

import pytest

from tests.test_mcp_memory_channel_e2e import (
    assert_memory_channel_through_mcp,
    assert_search_channel_through_mcp,
    build_mcp_app,
    seed_shared_notebook,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mcp_memory_channel"),
]


@pytest.fixture
def postgres_env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository

    env = build_mcp_app(postgres_scope.url, tmp_path, monkeypatch)
    env["seeded"] = seed_shared_notebook(env, "%s")
    try:
        yield env
    finally:
        repository().close()
        repository.cache_clear()


@pytest.mark.anyio
async def test_ask_notebook_memory_channel_and_ceiling_on_postgres(
    postgres_env, monkeypatch
):
    await assert_memory_channel_through_mcp(postgres_env, monkeypatch)


@pytest.mark.anyio
async def test_search_notebook_context_memory_channel_on_postgres(
    postgres_env, monkeypatch
):
    await assert_search_channel_through_mcp(postgres_env, monkeypatch)
