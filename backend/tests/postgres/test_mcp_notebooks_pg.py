import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.mcp_notebook_cases import CASES

pytestmark = [pytest.mark.postgres_integration]


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
async def test_mcp_notebook_scenario_pg(env, case):
    await run_case(env, CASES[case])


@pytest.mark.anyio
async def test_creation_locks_postgres_authority(env, postgres_scope, monkeypatch):
    import psycopg
    from psycopg.errors import LockNotAvailable

    from tests.mcp_answer_link_cases import _clients, _payload
    from tests.mcp_notebook_cases import _seed

    async def scenario(env):
        _headers, _owner, _original, token, principal = _seed(env)
        store = env.repo._runtime.memory_store
        create = store.create_agent_notebook
        probes = []

        def locked_creation(token_id, authorize, writer):
            def probe(row):
                # Independent sessions stand in for concurrent permission edits.
                # A bounded lock timeout keeps the test hermetic and deterministic.
                for statement, identity in (
                    ("UPDATE agent_access_tokens SET scopes_json='[]' WHERE id=%s", token_id),
                    ("UPDATE agent_profiles SET status='revoked' WHERE id=%s", principal.profile_id),
                    ("UPDATE users SET status='disabled' WHERE id=%s", principal.owner_id),
                ):
                    with psycopg.connect(postgres_scope.url) as other:
                        other.execute("SET LOCAL lock_timeout = '50ms'")
                        with pytest.raises(LockNotAvailable):
                            other.execute(statement, (identity,))
                        other.rollback()
                    probes.append(identity)
                authorize(row)
            return create(token_id, probe, writer)

        monkeypatch.setattr(store, "create_agent_notebook", locked_creation)
        async with _clients(env, token) as (client,):
            created = _payload(await client.call("create_notebook", {"name": "原子创建"}))
            assert created["notebook_id"] in env.repo.refresh_agent_principal(principal.token_id).notebook_ids
            assert probes == [principal.token_id, principal.profile_id, principal.owner_id]

    await run_case(env, scenario)
