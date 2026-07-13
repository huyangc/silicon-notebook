#!/usr/bin/env python3
"""Offline official-client smoke for the scoped Memory MCP contract."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "get_memory",
    "ask_notebook",
    "propose_memory",
}


def payload(result):
    if result.isError:
        raise AssertionError(result)
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


class Client:
    def __init__(self, app, raw_token: str):
        self.app = app
        self.raw_token = raw_token
        self.stack = AsyncExitStack()

    async def __aenter__(self):
        http = await self.stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://127.0.0.1",
                headers={"Authorization": f"Bearer {self.raw_token}"},
                follow_redirects=True,
            )
        )
        read, write, _ = await self.stack.enter_async_context(
            streamable_http_client(
                "http://127.0.0.1/mcp",
                http_client=http,
                terminate_on_close=False,
            )
        )
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __aexit__(self, *exc_info):
        await self.stack.aclose()

    async def call(self, name: str, arguments: dict | None = None):
        return await self.session.call_tool(name, arguments or {})


async def run() -> None:
    with tempfile.TemporaryDirectory(prefix="silicon-notebook-mcp-") as tmp:
        root = Path(tmp)
        os.environ.update(
            {
                "DATABASE_URL": f"sqlite:///{root / 'smoke.db'}",
                "SILICON_NOTEBOOK_STORAGE_DIR": str(root / "storage"),
                "SILICON_NOTEBOOK_AUTH_OPTIONAL": "false",
                "EVENT_LOG_ENABLED": "false",
                "LLM_LOG_ENABLED": "false",
                "OPENAI_COMPAT_API_KEY": "",
                "EMBED_PROVIDER": "",
                "ALLOW_NO_ENV_FILE": "1",
            }
        )
        from app.api.deps import (
            identity_repository,
            mcp_memory_repository,
            notebook_catalog_repository,
            repository,
        )
        from app.core.config import get_settings
        from app.core.request_context import reset_request_user, set_request_user
        from app.main import create_app
        from app.models.schemas import NotebookCreate

        get_settings.cache_clear()
        repository.cache_clear()
        app = create_app()
        identity = identity_repository()
        catalog = notebook_catalog_repository()
        service = mcp_memory_repository()
        owner = identity.create_user("m00128001", "pw")
        marker = set_request_user(owner)
        try:
            notebook = catalog.create_notebook(
                NotebookCreate(name="MCP smoke")
            )
        finally:
            reset_request_user(marker)
        scopes = [
            "knowledge:read",
            "memory:read",
            "memory:read_candidates",
            "memory:propose",
            "ask:execute",
        ]
        first_profile = service.create_agent_profile(owner.id, "smoke-a", "")
        second_profile = service.create_agent_profile(owner.id, "smoke-b", "")
        first = service.issue_agent_token(
            owner.id, first_profile.id, scopes, notebook.id, [notebook.id], None
        )
        second = service.issue_agent_token(
            owner.id, second_profile.id, scopes, notebook.id, [notebook.id], None
        )

        async with app.router.lifespan_context(app):
            async with Client(app, first.token) as creator:
                listed = await creator.session.list_tools()
                assert {item.name for item in listed.tools} == TOOLS
                assert (await creator.call(
                    "search_agent_memory", {"query": "probe"}
                )).isError
                payload(await creator.call(
                    "select_notebook", {"notebook_id": notebook.id}
                ))
                created = payload(await creator.call(
                    "propose_memory",
                    {
                        "title": "Smoke probe",
                        "content_md": "Distinctive mcp-smoke-probe experience.",
                        "reason": "contract smoke",
                        "task_context": {"task": "smoke"},
                        "evidence_refs": [],
                        "client_request_id": "smoke-request-1",
                    },
                ))
                memory_id = created["memory_id"]
                formal = payload(await creator.call(
                    "search_notebook_context", {"query": "mcp-smoke-probe"}
                ))
                assert memory_id not in {
                    item.get("memory_id") for item in formal["items"]
                }

            async with Client(app, second.token) as peer:
                assert (await peer.call(
                    "search_agent_memory", {"query": "mcp-smoke-probe"}
                )).isError
                payload(await peer.call(
                    "select_notebook", {"notebook_id": notebook.id}
                ))
                recalled = payload(await peer.call(
                    "search_agent_memory", {"query": "mcp-smoke-probe"}
                ))
                assert memory_id in {item["memory_id"] for item in recalled["items"]}

    print("memory MCP smoke: OK (7 tools, session isolation, candidate plane isolation)")


if __name__ == "__main__":
    asyncio.run(run())
