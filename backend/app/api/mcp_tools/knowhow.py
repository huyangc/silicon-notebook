"""Knowhow-table MCP tools."""

from typing import Any, Callable

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.services.knowhow import api as knowhow_api
from app.services.knowhow import audit as knowhow_audit

from ._shared import (
    RESULT_LIMIT,
    TEXT_LIMIT,
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
)


def register_knowhow_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description="List knowhow tables (structured tabular knowledge) in the selected notebook."
    )
    async def list_knowhow_tables(ctx: Context, limit: int = RESULT_LIMIT) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> list[dict[str, Any]]:
            with _owner_request_context(principal):
                return knowhow_api.list_tables_for_agent(repo, notebook_id)

        rows = await _run_with_progress(
            ctx, load, label="list_knowhow_tables"
        )
        cap = max(1, min(int(limit), RESULT_LIMIT))
        return _budget_response(
            {"notebook_id": notebook_id, "items": rows[:cap]},
            initial_omitted_items=max(0, len(rows) - cap),
            field_limits={"title": 300, "description": 500, "name": 200, "kind": 60},
        )

    @server.tool(
        description=(
            "Get the discrimination set for one knowhow table: every row's "
            "title plus its procedure/method columns' net text and "
            "code_status (implemented/stale/none), for picking which "
            "rows/methods still need generated code. The table must have a "
            "row-title (anchor) column configured."
        )
    )
    async def get_knowhow_discrimination(table_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                table = repo.get_knowhow_table(table_id)
                if table["notebook_id"] != notebook_id:
                    raise KeyError(table_id)
                wire_table = knowhow_api.to_wire_table(table)
                code_attachments = repo.list_knowhow_cell_code(table_id)
                return knowhow_api.build_discrimination_set(wire_table, code_attachments)

        return _budget_response(
            await _run_with_progress(
                ctx, load, label="get_knowhow_discrimination"
            ),
            field_limits={
                "title": 200, "column_name": 200, "text": TEXT_LIMIT,
                "code_status": 20,
            },
        )

    @server.tool(
        description=(
            "Get one knowhow row's full machine view: every column's "
            "kind/net-text (plus steps for a procedure column, items for an "
            "entity column), plus any existing code attachments for its "
            "columns."
        )
    )
    async def get_knowhow_row(row_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> dict[str, Any]:
            with _owner_request_context(principal):
                location = repo.get_knowhow_row_location(row_id)
                if location is None or location["notebook_id"] != notebook_id:
                    raise KeyError(row_id)
                table = knowhow_api.to_wire_table(
                    repo.get_knowhow_table(location["table_id"])
                )
                code_attachments = knowhow_audit.project_nested_updated_by(
                    repo, repo.list_knowhow_cell_code(location["table_id"])
                )
                return knowhow_api.build_row_detail(table, row_id, code_attachments)

        return _budget_response(
            await _run_with_progress(ctx, load, label="get_knowhow_row"),
            field_limits={
                "title": 200, "column_name": 200, "kind": 30, "text": TEXT_LIMIT,
                "language": 60, "code_text": TEXT_LIMIT, "status": 20,
                "updated_by": 200, "updated_at": 60,
            },
        )

    @server.tool(
        description=(
            "Save a code attachment for one knowhow cell (design doc §⑥-4): "
            "the code body itself, stored alongside the cell — never "
            "indexed, embedded, or retrievable as notebook knowledge. "
            "Requires the knowhow:code scope."
        )
    )
    async def put_knowhow_cell_code(
        row_id: str, column_id: str, code_text: str, ctx: Context, language: str = "",
    ) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowhow:code"
        )

        def run() -> dict[str, Any]:
            with _owner_request_context(principal):
                location = repo.get_knowhow_row_location(row_id)
                if location is None or location["notebook_id"] != notebook_id:
                    raise KeyError(row_id)
                # knowhow 表版本管理 Task 13 code review: this whole MCP server
                # is wrapped in AgentBearerMiddleware (see the bottom of
                # create_memory_mcp below) — every tool call, this one
                # included, is unconditionally an Agent principal, never a
                # session user — so origin="agent" is not a guess here, it is
                # the only value that can ever be true for this call site.
                return knowhow_api.put_cell_code(
                    repo, row_id, column_id, code_text, language,
                    principal.profile_name, origin="agent",
                )

        return _budget_response(
            await _run_with_progress(
                ctx, run, label="put_knowhow_cell_code"
            ),
            field_limits={"language": 60, "code_text": TEXT_LIMIT, "status": 20},
        )
