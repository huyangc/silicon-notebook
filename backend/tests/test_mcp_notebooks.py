import sqlite3

import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.mcp_notebook_cases import CASES


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings

    built = build_env(f"sqlite:///{tmp_path / 'notebooks.db'}", tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository().close()
        repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_notebook_scenario(env, case):
    await run_case(env, CASES[case])


@pytest.mark.anyio
async def test_creation_locks_authority_before_reading_it(env, monkeypatch):
    """A second SQLite connection cannot revoke manage between check and create."""
    from tests.mcp_answer_link_cases import _clients, _payload
    from tests.mcp_notebook_cases import _seed

    async def scenario(env):
        _headers, _owner, _original, token, principal = _seed(env)
        store = env.repo._runtime.memory_store
        create = store.create_agent_notebook
        probes = []

        def locked_creation(token_id, authorize, writer):
            def probe(row):
                # A raw connection bypasses the process-local write lock, as
                # another server process would. No thread or host service needed.
                with sqlite3.connect(env.db.db_path, timeout=0) as other:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        other.execute(
                            "UPDATE agent_access_tokens SET scopes_json='[]' WHERE id=?",
                            (token_id,),
                        )
                probes.append(token_id)
                authorize(row)
            return create(token_id, probe, writer)

        monkeypatch.setattr(store, "create_agent_notebook", locked_creation)
        async with _clients(env, token) as (client,):
            created = _payload(await client.call("create_notebook", {"name": "原子创建"}))
            assert created["notebook_id"] in env.repo.refresh_agent_principal(principal.token_id).notebook_ids
            assert probes == [principal.token_id]

    await run_case(env, scenario)
