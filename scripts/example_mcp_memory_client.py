#!/usr/bin/env python3
"""Connect to a live silicon-notebook MCP server with the official client.

The script never prints the bearer token. Read-only retrieval is the default;
pass ``--propose`` to create one idempotent candidate Memory for UI review.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


# The mounted route: `POST /mcp` is a 307 to `/mcp/`. The official client
# always follows redirects, so both work here — the slash keeps this in
# step with the runbook, which tells readers to configure the form that
# does not depend on a client preserving method/body/Authorization.
DEFAULT_URL = "http://127.0.0.1:8000/mcp/"
REQUIRED_TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "propose_memory",
    "add_source_file",
}


def _payload(result: Any) -> dict[str, Any]:
    if result.isError:
        detail = "\n".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        raise RuntimeError(detail or "MCP tool call failed")
    if result.structuredContent is not None:
        return dict(result.structuredContent)
    if not result.content or not getattr(result.content[0], "text", None):
        raise RuntimeError("MCP tool returned no JSON payload")
    return json.loads(result.content[0].text)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise silicon-notebook retrieval and candidate Memory over MCP."
    )
    parser.add_argument(
        "--url",
        default=os.getenv("SILICON_NOTEBOOK_MCP_URL", DEFAULT_URL),
        help=f"Streamable HTTP endpoint (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--notebook-id",
        default=os.getenv("SILICON_NOTEBOOK_NOTEBOOK_ID", ""),
        help="Allowlisted notebook id; otherwise the token default is selected.",
    )
    parser.add_argument(
        "--query",
        default="What reusable engineering guidance is available in this notebook?",
        help="Query sent to both formal notebook context and Agent Memory.",
    )
    parser.add_argument(
        "--propose",
        action="store_true",
        help="Create an idempotent candidate Memory that must be reviewed in the UI.",
    )
    parser.add_argument(
        "--memory-title",
        default="MCP quickstart connectivity verified",
    )
    parser.add_argument(
        "--memory-content",
        default=(
            "The silicon-notebook MCP quickstart completed an authenticated session, "
            "selected the intended notebook, and exercised the Memory tools."
        ),
    )
    parser.add_argument(
        "--client-request-id",
        default="silicon-notebook-mcp-memory-sop-v1",
        help="Stable idempotency key for the candidate proposal.",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help=(
            "Also call get_notebook_profile (Agentic Memory P3, requires "
            "agent_profile:read) and print only block counts and character "
            "counts -- never the block text itself, since this script's "
            "output is meant to be pasted into chat/logs."
        ),
    )
    parser.add_argument(
        "--source-file",
        default="",
        help=(
            "Upload one local PDF/PPTX/DOCX/XLSX/Markdown/ZIP source through "
            "add_source_file (requires sources:write)."
        ),
    )
    parser.add_argument(
        "--source-title",
        default="",
        help="Optional display title for --source-file; defaults to its file name.",
    )
    return parser.parse_args()


def _choose_notebook(items: list[dict[str, Any]], requested_id: str) -> dict[str, Any]:
    if requested_id:
        match = next(
            (item for item in items if item.get("notebook_id") == requested_id),
            None,
        )
        if match is None:
            raise RuntimeError(
                f"notebook {requested_id!r} is not in this token's live allowlist"
            )
        return match
    default = next((item for item in items if item.get("is_default")), None)
    if default is not None:
        return default
    if not items:
        raise RuntimeError("the Agent token has no readable allowlisted notebooks")
    return items[0]


def _print_result(label: str, payload: dict[str, Any]) -> None:
    print(f"\n{label}")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _print_profile_summary(payload: dict[str, Any]) -> None:
    """Print only shape (block counts, character counts) for
    ``get_notebook_profile`` -- never the block text itself. Unlike
    ``_print_result``, this deliberately does not dump the payload verbatim:
    the blocks are prompt scaffolding about how this notebook has been used,
    and a quickstart script's output is the kind of thing that gets pasted
    into chat or committed to a log."""
    print("\nNotebook understanding (get_notebook_profile)")
    if not payload.get("enabled", False):
        print("  enabled: false (feature is off, or nothing consolidated yet)")
        return
    for group in ("shared", "mine"):
        blocks = payload.get(group, [])
        chars = sum(len(str(block.get("value", ""))) for block in blocks)
        print(f"  {group}: {len(blocks)} block(s), {chars} character(s) total")


async def _run(args: argparse.Namespace) -> None:
    token = os.getenv("SILICON_NOTEBOOK_AGENT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "set SILICON_NOTEBOOK_AGENT_TOKEN to the one-time token issued in "
            "私有记忆 → Agent 接入"
        )

    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
                timeout=60,
            )
        )
        read, write, _ = await stack.enter_async_context(
            streamable_http_client(
                args.url,
                http_client=http,
                terminate_on_close=False,
            )
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()

        tools = {tool.name for tool in (await session.list_tools()).tools}
        missing = sorted(REQUIRED_TOOLS - tools)
        if missing:
            raise RuntimeError(f"server is missing required tools: {', '.join(missing)}")
        print(f"Connected to {args.url}; server exposes {len(tools)} tools.")

        listed = _payload(await session.call_tool("list_notebooks", {"limit": 20}))
        notebook = _choose_notebook(listed.get("items", []), args.notebook_id)
        notebook_id = str(notebook["notebook_id"])
        selected = _payload(
            await session.call_tool(
                "select_notebook", {"notebook_id": notebook_id}
            )
        )
        print(f"Selected {selected.get('name', notebook_id)} ({notebook_id}).")
        if args.source_file:
            source_path = Path(args.source_file).expanduser()
            if not source_path.is_file():
                raise RuntimeError(f"source file does not exist: {source_path}")
            uploaded = _payload(
                await session.call_tool(
                    "add_source_file",
                    {
                        "file_name": source_path.name,
                        "content_base64": base64.b64encode(
                            source_path.read_bytes()
                        ).decode("ascii"),
                        "title": args.source_title,
                    },
                )
            )
            _print_result("Uploaded source (background parsing queued)", uploaded)

        formal = _payload(
            await session.call_tool(
                "search_notebook_context", {"query": args.query, "limit": 5}
            )
        )
        memories = _payload(
            await session.call_tool(
                "search_agent_memory", {"query": args.query, "limit": 5}
            )
        )
        _print_result("Formal notebook context (confirmed plane)", formal)
        _print_result("Agent Memory (candidate + confirmed when scoped)", memories)

        if args.profile:
            profile = _payload(await session.call_tool("get_notebook_profile", {}))
            _print_profile_summary(profile)

        if args.propose:
            request_id = f"{args.client_request_id}:{notebook_id}"[:200]
            proposal = _payload(
                await session.call_tool(
                    "propose_memory",
                    {
                        "title": args.memory_title,
                        "content_md": args.memory_content,
                        "tags": ["mcp", "sop-example"],
                        "reason": "Verify the documented MCP/Memory onboarding SOP.",
                        "task_context": {
                            "example": "mcp_memory_quickstart",
                            "query": args.query,
                        },
                        "evidence_refs": [],
                        "client_request_id": request_id,
                    },
                )
            )
            _print_result("Candidate Memory proposed for UI review", proposal)
            formal_after = _payload(
                await session.call_tool(
                    "search_notebook_context",
                    {"query": args.memory_title, "limit": 5},
                )
            )
            proposed_id = proposal.get("memory_id")
            if proposed_id in {
                item.get("memory_id") for item in formal_after.get("items", [])
            }:
                raise RuntimeError(
                    "candidate Memory unexpectedly appeared in formal notebook context"
                )
            _print_result(
                "Formal notebook context remains candidate-free", formal_after
            )
            recalled = _payload(
                await session.call_tool(
                    "search_agent_memory",
                    {"query": args.memory_title, "limit": 5},
                )
            )
            _print_result("Candidate recalled from Agent Memory", recalled)
            print(
                "\nNext: open 私有记忆, filter 来源=Agent 提议 and 状态=待确认, "
                "then confirm or reject the candidate."
            )


def main() -> None:
    try:
        asyncio.run(_run(_arguments()))
    except (httpx.HTTPError, RuntimeError, OSError, ValueError) as exc:
        raise SystemExit(f"MCP quickstart failed: {exc}") from exc


if __name__ == "__main__":
    main()
