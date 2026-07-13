from __future__ import annotations

import json
from contextlib import AsyncExitStack

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.api.deps import (
    identity_repository,
    mcp_memory_repository,
    notebook_catalog_repository,
    notebook_sharing_repository,
    repository,
)
from app.core.config import get_settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.schemas import NotebookCreate


PUBLIC_TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "get_memory",
    "ask_notebook",
    "propose_memory",
}


def _payload(result):
    assert not result.isError, result
    if result.structuredContent is not None:
        return result.structuredContent
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


class OfficialMcpClient:
    def __init__(self, app, token: str, *, manage_lifespan: bool = True):
        self.app = app
        self.token = token
        self.manage_lifespan = manage_lifespan
        self.stack = AsyncExitStack()
        self.session = None

    async def __aenter__(self):
        if self.manage_lifespan:
            await self.stack.enter_async_context(
                self.app.router.lifespan_context(self.app)
            )
        http = await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://127.0.0.1",
                headers={"Authorization": f"Bearer {self.token}"},
                follow_redirects=True,
            )
        )
        read, write, _ = await self.stack.enter_async_context(
            streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                # ASGITransport executes the server in this same task.  A
                # protocol DELETE would therefore cancel the test's own
                # AnyIO scope; real network clients run in a separate server
                # task/process and use the default terminate_on_close=True.
                terminate_on_close=False,
            )
        )
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc_info):
        await self.stack.aclose()

    async def call(self, name: str, arguments: dict | None = None):
        assert self.session is not None
        return await self.session.call_tool(name, arguments or {})


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mcp.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    get_settings.cache_clear()
    repository.cache_clear()

    from app.main import create_app

    app = create_app()
    identity = identity_repository()
    catalog = notebook_catalog_repository()
    sharing = notebook_sharing_repository()
    service = mcp_memory_repository()
    alice = identity.create_user("a00128001", "pw")
    bob = identity.create_user("b00128002", "pw")
    marker = set_request_user(alice)
    try:
        notebook = catalog.create_notebook(
            NotebookCreate(name="MCP notebook")
        )
        other = catalog.create_notebook(
            NotebookCreate(name="Other notebook")
        )
    finally:
        reset_request_user(marker)
    profile_a = service.create_agent_profile(alice.id, "Claude Code", "")
    profile_b = service.create_agent_profile(alice.id, "Codex", "")
    scopes = [
        "knowledge:read",
        "memory:read",
        "memory:read_candidates",
        "memory:propose",
        "ask:execute",
    ]
    token_a = service.issue_agent_token(
        alice.id, profile_a.id, scopes, notebook.id, [notebook.id], None
    )
    token_b = service.issue_agent_token(
        alice.id, profile_b.id, scopes, notebook.id, [notebook.id], None
    )
    restricted = service.issue_agent_token(
        alice.id, profile_b.id, ["memory:read"], notebook.id, [notebook.id], None
    )
    bob_profile = service.create_agent_profile(bob.id, "Foreign", "")
    sharing.add_member(notebook.id, bob.id)
    bob_token = service.issue_agent_token(
        bob.id, bob_profile.id, scopes, notebook.id, [notebook.id], None
    )
    return {
        "app": app,
        "sharing": sharing,
        "service": service,
        "alice": alice,
        "bob": bob,
        "notebook": notebook,
        "other": other,
        "profile_a": profile_a,
        "token_a": token_a,
        "token_b": token_b,
        "restricted": restricted,
        "bob_token": bob_token,
    }


@pytest.mark.anyio
async def test_official_client_exposes_exact_public_tool_contract(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        listed = await client.session.list_tools()
        notebooks = _payload(await client.call("list_notebooks"))
        assert [item["notebook_id"] for item in notebooks["items"]] == [
            mcp_env["notebook"].id
        ]
        assert (await client.call(
            "select_notebook", {"notebook_id": mcp_env["other"].id}
        )).isError
    assert {tool.name for tool in listed.tools} == PUBLIC_TOOLS


@pytest.mark.anyio
async def test_notebook_selection_is_required_and_session_scoped(mcp_env):
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as first:
            missing = await first.call("search_agent_memory", {"query": "anything"})
            assert missing.isError
            selected = _payload(await first.call(
                "select_notebook", {"notebook_id": mcp_env["notebook"].id}
            ))
            assert selected["notebook_id"] == mcp_env["notebook"].id
            assert not (await first.call(
                "search_agent_memory", {"query": "anything"}
            )).isError

        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as second:
            assert (await second.call(
                "search_agent_memory", {"query": "anything"}
            )).isError


@pytest.mark.anyio
async def test_candidate_is_agent_recallable_across_owner_profiles_but_not_notebook_context(mcp_env):
    notebook_id = mcp_env["notebook"].id
    content = "Use the distinctive kelvin guard-ring procedure before extraction."
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["token_a"].token, manage_lifespan=False
        ) as creator:
            _payload(await creator.call("select_notebook", {"notebook_id": notebook_id}))
            created = _payload(await creator.call("propose_memory", {
                "title": "Kelvin guard ring",
                "content_md": content,
                "tags": ["layout"],
                "reason": "Worked in the previous task",
                "task_context": {"task": "extraction"},
                "evidence_refs": [],
                "client_request_id": "request-1",
            }))
            memory_id = created["memory_id"]
            assert created["status"] == "candidate"
            detail = _payload(await creator.call(
                "get_memory", {"memory_id": memory_id}
            ))
            assert detail["status"] == "candidate"
            notebook_hits = _payload(await creator.call(
                "search_notebook_context", {"query": "kelvin guard-ring"}
            ))
            assert memory_id not in {item.get("memory_id") for item in notebook_hits["items"]}

        async with OfficialMcpClient(
            app, mcp_env["token_b"].token, manage_lifespan=False
        ) as peer:
            _payload(await peer.call("select_notebook", {"notebook_id": notebook_id}))
            recalled = _payload(await peer.call(
                "search_agent_memory", {"query": "kelvin guard-ring"}
            ))
            hit = next(item for item in recalled["items"] if item["memory_id"] == memory_id)
            assert hit["status"] == "candidate"
            assert hit["unconfirmed"] is True
            assert hit["created_by_agent"] == "Claude Code"
            mcp_env["service"].confirm_memory(memory_id, mcp_env["alice"].id)
            formal = _payload(await peer.call(
                "search_notebook_context", {"query": "kelvin guard-ring"}
            ))
            assert memory_id in {item.get("memory_id") for item in formal["items"]}

        async with OfficialMcpClient(
            app, mcp_env["bob_token"].token, manage_lifespan=False
        ) as foreign:
            _payload(await foreign.call("select_notebook", {"notebook_id": notebook_id}))
            hits = _payload(await foreign.call(
                "search_agent_memory", {"query": "kelvin guard-ring"}
            ))
            assert memory_id not in {item["memory_id"] for item in hits["items"]}


@pytest.mark.anyio
async def test_access_is_rechecked_after_revoke_and_membership_loss(mcp_env):
    notebook_id = mcp_env["notebook"].id
    app = mcp_env["app"]
    async with app.router.lifespan_context(app):
        async with OfficialMcpClient(
            app, mcp_env["bob_token"].token, manage_lifespan=False
        ) as client:
            _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
            mcp_env["sharing"].remove_member(notebook_id, mcp_env["bob"].id)
            assert (await client.call("search_notebook_context", {"query": "x"})).isError

        mcp_env["service"].revoke_agent_token(
            mcp_env["alice"].id, mcp_env["token_a"].id
        )
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {mcp_env['token_a'].token}"},
            follow_redirects=True,
        )
        async with http:
            response = await http.post(
                "/mcp",
                headers={
                    "content-type": "application/json",
                    "accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        assert response.status_code == 401


@pytest.mark.anyio
async def test_ask_tool_reuses_formal_ask_and_rejects_experimental_graph(mcp_env):
    async with OfficialMcpClient(mcp_env["app"], mcp_env["token_a"].token) as client:
        _payload(await client.call(
            "select_notebook", {"notebook_id": mcp_env["notebook"].id}
        ))
        graph = await client.call(
            "ask_notebook", {"question": "What is here?", "mode": "graph"}
        )
        assert graph.isError
        answer = _payload(await client.call(
            "ask_notebook", {"question": "What evidence exists?", "mode": "chunk"}
        ))
        assert answer["mode"] == "chunk"
        assert "answer" in answer


@pytest.mark.anyio
async def test_each_data_tool_enforces_its_minimal_live_scope_and_output_budget(mcp_env):
    notebook_id = mcp_env["notebook"].id
    candidate = mcp_env["service"].create_memory_candidate(
        notebook_id,
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        "budget-request",
        "Budget marker",
        "budget-marker candidate-only",
        [],
        "test",
    )
    confirmed = mcp_env["service"].create_memory_candidate(
        notebook_id,
        mcp_env["alice"].id,
        mcp_env["profile_a"].id,
        "budget-request-confirmed",
        "Budget marker confirmed",
        "budget-marker " + ("private-data " * 1000),
        [],
        "test",
    )
    mcp_env["service"].confirm_memory(confirmed.id, mcp_env["alice"].id)
    async with OfficialMcpClient(
        mcp_env["app"], mcp_env["restricted"].token
    ) as client:
        _payload(await client.call("select_notebook", {"notebook_id": notebook_id}))
        recalled = _payload(await client.call(
            "search_agent_memory", {"query": "budget-marker", "limit": 50}
        ))
        assert candidate.id not in {item["memory_id"] for item in recalled["items"]}
        confirmed_hit = next(
            item for item in recalled["items"] if item["memory_id"] == confirmed.id
        )
        assert len(confirmed_hit["content"]) <= 2_000
        assert len(recalled["items"]) <= 20
        assert (await client.call(
            "search_notebook_context", {"query": "budget-marker"}
        )).isError
        assert (await client.call(
            "propose_memory",
            {
                "title": "No",
                "content_md": "No",
                "reason": "No",
                "task_context": {},
                "evidence_refs": [],
                "client_request_id": "no-scope",
            },
        )).isError
        assert (await client.call(
            "ask_notebook", {"question": "No scope", "mode": "chunk"}
        )).isError


def test_nonlocal_plain_http_mcp_configuration_is_rejected():
    from app.api.mcp_server import validate_mcp_deployment

    validate_mcp_deployment("127.0.0.1", "http://127.0.0.1:8000/mcp")
    validate_mcp_deployment("0.0.0.0", "https://memory.example.test/mcp")
    with pytest.raises(RuntimeError, match="HTTPS"):
        validate_mcp_deployment("0.0.0.0", "http://memory.example.test/mcp")


def test_bounded_mcp_collections_accept_a_smaller_per_response_budget():
    from app.api.mcp_server import _bounded

    rows = [{"text": "x" * 80}, {"text": "y" * 80}, {"text": "z" * 80}]
    bounded = _bounded(rows, 20, char_budget=180)
    assert len(bounded) == 1


@pytest.mark.anyio
async def test_transport_rejects_missing_token_and_untrusted_origin(mcp_env):
    app = mcp_env["app"]
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "security-test", "version": "1"},
        },
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            follow_redirects=True,
        ) as http:
            missing = await http.post(
                "/mcp",
                headers={"accept": "application/json, text/event-stream"},
                json=request,
            )
            hostile = await http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {mcp_env['token_a'].token}",
                    "Origin": "https://hostile.example",
                    "accept": "application/json, text/event-stream",
                },
                json=request,
            )
    assert missing.status_code == 401
    assert hostile.status_code == 403
