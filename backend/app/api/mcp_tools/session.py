"""Session and notebook-selection MCP tools."""

from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from ._shared import (
    RESULT_LIMIT,
    _SELECTED_ATTR,
    _budget_response,
    _live_principal,
    _owner_request_context,
    _run_with_progress,
)


def register_session_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(description="List live notebooks in this Agent token's allowlist.")
    async def list_notebooks(ctx: Context, limit: int = RESULT_LIMIT) -> dict[str, Any]:
        repo = repository_provider()
        principal = await anyio.to_thread.run_sync(_live_principal, repo)

        def load() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            with _owner_request_context(principal):
                for notebook_id in principal.notebook_ids[:RESULT_LIMIT]:
                    if not repo.user_can_read_notebook(
                        notebook_id, principal.owner_id
                    ):
                        continue
                    try:
                        item = repo.get_notebook(notebook_id)
                    except KeyError:
                        continue
                    rows.append(
                        {
                            "notebook_id": item.id,
                            "name": item.name,
                            "purpose": item.purpose,
                            "tier": item.tier,
                            "access": item.access,
                            "counts": dict(item.counts),
                            "is_default": item.id == principal.default_notebook_id,
                        }
                    )
            return rows

        rows = await _run_with_progress(ctx, load, label="list_notebooks")
        cap = max(1, min(int(limit), RESULT_LIMIT))
        return _budget_response(
            {"items": rows[:cap], "selected_notebook_id": ""},
            initial_omitted_items=max(0, len(rows) - cap),
            field_limits={"name": 200, "purpose": 500},
        )

    @server.tool(description="Select one allowlisted notebook for this MCP session.")
    async def select_notebook(notebook_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal = await anyio.to_thread.run_sync(_live_principal, repo)
        if notebook_id not in principal.notebook_ids:
            raise PermissionError("notebook is outside the token allowlist")

        def load() -> tuple[Any, Any]:
            if not repo.user_can_read_notebook(
                notebook_id, principal.owner_id
            ):
                raise PermissionError("notebook access denied")
            with _owner_request_context(principal):
                summary = repo.get_notebook(notebook_id)
                kg_status = repo.unified_kg_status(notebook_id)
            return summary, kg_status

        summary, kg_status = await _run_with_progress(
            ctx, load, label="select_notebook"
        )
        setattr(ctx.session, _SELECTED_ATTR, notebook_id)
        return _budget_response({
            "notebook_id": summary.id,
            "name": summary.name,
            "purpose": summary.purpose,
            "tier": summary.tier,
            "counts": dict(summary.counts),
            "kg_status": (
                kg_status.model_dump()
                if hasattr(kg_status, "model_dump")
                else dict(kg_status)
            ),
            "retrieval": {
                "agent_memory": "candidate+confirmed when scoped",
                "notebook_context": "confirmed only",
            },
        }, field_limits={"name": 200, "purpose": 500})
