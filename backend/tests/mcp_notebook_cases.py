"""Notebook creation scenarios shared by SQLite and PostgreSQL MCP tests."""
from datetime import datetime, timedelta, timezone

import anyio

from tests.answer_link_service_cases import _notebook, _user, _write
from tests.mcp_answer_link_cases import _agent, _clients, _error, _payload


def _seed(env, scopes=("read", "manage")):
    headers, owner = _user(env)
    notebook = _notebook(env, headers, "原有笔记本")
    token, principal = _agent(env, owner, [notebook], scopes=scopes, allow_links=False)
    return headers, owner, notebook, token, principal


def _counts(env):
    with _write(env) as db:
        return tuple(db.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
                     for table in ("notebooks", "unified_kg_state", "agent_token_notebooks"))


async def create_and_manage(env):
    headers, owner, original, token, principal = _seed(env)
    # Even a second token of this same profile receives no automatic grant.
    sibling = env.repo.issue_agent_token(
        owner, principal.profile_id, ["read", "manage"], original, [original], None
    )
    before = env.repo.refresh_agent_principal(principal.token_id)
    async with _clients(env, token, sibling.token) as (client, other):
        tools = await client.session.list_tools()
        assert "create_notebook" in {tool.name for tool in tools.tools}
        created = _payload(await client.call("create_notebook", {
            "name": "  新建研究笔记本  ", "purpose": "  研究用途  ",
        }))
        nb = created["notebook_id"]
        assert created["name"] == "新建研究笔记本"
        assert created["purpose"] == "研究用途"
        assert created["tier"] == "personal"
        assert created["access"] == "owner"
        assert created["is_default"] is False
        live = env.repo.refresh_agent_principal(principal.token_id)
        assert set(live.notebook_ids) == {original, nb}
        assert live.default_notebook_id == before.default_notebook_id
        assert live.scopes == before.scopes
        assert live.allow_answer_links == before.allow_answer_links
        assert env.repo.refresh_agent_principal(sibling.id).notebook_ids == [original]
        listed = _payload(await client.call("list_notebooks"))["items"]
        assert {row["notebook_id"] for row in listed} == {original, nb}
        assert next(row for row in listed if row["notebook_id"] == original)["is_default"]
        overview = _payload(await client.call("get_notebook", {"notebook_id": nb}))
        assert overview["notebook_id"] == nb
        source = _payload(await client.call("add_source", {
            "notebook_id": nb, "content_md": "这是新笔记本的第一份研究资料。", "title": "资料",
        }))
        assert source
        assert _error(await other.call("get_notebook", {"notebook_id": nb}))[0] == "notebook_not_allowed"
        default = _payload(await client.call("get_notebook"))
        assert default["notebook_id"] == original
        # The browser sees the same notebook as owned by the token's real owner.
        response = env.client.get(f"/api/notebooks/{nb}", headers=headers)
        assert response.status_code == 200
        assert response.json()["access"] == "owner"


async def manage_without_read(env):
    _headers, _owner, original, token, principal = _seed(env, ("manage",))
    async with _clients(env, token) as (client,):
        tools = await client.session.list_tools()
        assert "create_notebook" in {tool.name for tool in tools.tools}
        created = _payload(await client.call("create_notebook", {"name": "只管理"}))
        assert set(env.repo.refresh_agent_principal(principal.token_id).notebook_ids) == {
            original, created["notebook_id"],
        }
        assert _error(await client.call("get_notebook", {
            "notebook_id": created["notebook_id"],
        }))[0] == "scope_missing"


async def concurrent_creations(env):
    _headers, _owner, original, token, principal = _seed(env)
    async with _clients(env, token) as (client,):
        created = []

        async def create(name):
            created.append(_payload(await client.call("create_notebook", {"name": name})))

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(create, "并发创建甲")
            tasks.start_soon(create, "并发创建乙")
        ids = {row["notebook_id"] for row in created}
        assert len(ids) == 2
        live = env.repo.refresh_agent_principal(principal.token_id)
        assert set(live.notebook_ids) == {original, *ids}
        assert live.default_notebook_id == original


async def old_default_unreadable(env):
    _headers, _owner, original, token, principal = _seed(env)
    _other_headers, other_owner = _user(env)
    with _write(env) as db:
        db.execute("UPDATE notebooks SET created_by=? WHERE id=?", (other_owner, original))
    async with _clients(env, token) as (client,):
        assert _error(await client.call("get_notebook"))[0] == "notebook_unreadable"
        created = _payload(await client.call("create_notebook", {"name": "新起点"}))
        assert created["notebook_id"] in env.repo.refresh_agent_principal(principal.token_id).notebook_ids
        assert _payload(await client.call("get_notebook", {
            "notebook_id": created["notebook_id"],
        }))["access"] == "owner"


async def refuses_without_manage(env):
    _headers, _owner, _original, token, _principal = _seed(env, ("read", "delete"))
    before = _counts(env)
    async with _clients(env, token) as (client,):
        tools = await client.session.list_tools()
        assert "create_notebook" not in {tool.name for tool in tools.tools}
        code, text = _error(await client.call("create_notebook", {"name": "不能创建"}))
        assert code == "scope_missing" and "管理" in text
        assert _counts(env) == before


async def invalid_name(env):
    _headers, _owner, _original, token, _principal = _seed(env)
    before = _counts(env)
    async with _clients(env, token) as (client,):
        for args in ({}, {"name": " \n\t"}):
            assert _error(await client.call("create_notebook", args))[0] == "invalid_argument"
        assert _counts(env) == before


async def rolls_back_failed_grant(env):
    _headers, _owner, _original, token, _principal = _seed(env)
    store = env.repo._runtime.notebook_store
    create = store.create_row

    def invalid_grant(*args, **kwargs):
        create(*args, **kwargs)
        return "nb-does-not-exist"

    env.monkeypatch.setattr(store, "create_row", invalid_grant)
    before = _counts(env)
    async with _clients(env, token) as (client,):
        assert _error(await client.call("create_notebook", {"name": "应回滚"}))[0] == "internal"
        assert _counts(env) == before


async def authority_changed(env, change):
    _headers, owner, original, token, principal = _seed(env)
    store = env.repo._runtime.memory_store
    create = store.create_agent_notebook

    def change_before_commit(*args, **kwargs):
        if change == "scope":
            env.repo.update_agent_token_access(
                owner, principal.token_id, ["read"], original, [original], None,
                allow_answer_links=False,
            )
        elif change == "revoked":
            env.repo.revoke_agent_token(owner, principal.token_id)
        elif change == "profile":
            env.repo.update_agent_profile(principal.profile_id, owner, {"status": "revoked"})
        else:
            with _write(env) as db:
                if change == "owner":
                    db.execute("UPDATE users SET status='disabled' WHERE id=?", (owner,))
                else:
                    db.execute("UPDATE agent_access_tokens SET expires_at=? WHERE id=?", (
                        (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                        principal.token_id,
                    ))
        return create(*args, **kwargs)

    env.monkeypatch.setattr(store, "create_agent_notebook", change_before_commit)
    before = _counts(env)
    async with _clients(env, token) as (client,):
        code, _text = _error(await client.call("create_notebook", {"name": "权限已收回"}))
        assert code == ("scope_missing" if change == "scope" else "token_inactive")
        assert _counts(env) == before


CASES = {
    "create_and_manage": create_and_manage,
    "manage_without_read": manage_without_read,
    "concurrent_creations": concurrent_creations,
    "old_default_unreadable": old_default_unreadable,
    "refuses_without_manage": refuses_without_manage,
    "invalid_name": invalid_name,
    "rolls_back_failed_grant": rolls_back_failed_grant,
}


def _authority_case(change):
    async def case(env):
        await authority_changed(env, change)
    return case


CASES.update({f"authority_{change}": _authority_case(change)
              for change in ("scope", "revoked", "profile", "owner", "expired")})
