"""Agent-facing HTTP surface for knowhow tables (knowhow-tables PR-2+3 Task
10, design doc §⑥): table list / discrimination set / row detail / cell-level
code attachments — reachable by EITHER a signed-in session or an Agent Bearer
token (``app.api.deps.require_user_or_agent`` / ``user_or_agent_scope``).

Mounted at bare ``/agent/...`` paths under the API's own ``/api`` prefix (see
``app.main.create_app``'s ``app.include_router(agent_router, prefix="/api")``
call) — deliberately NOT added to the composition-only aggregate router in
``app.api.routes``, which is mounted with a blanket
``dependencies=[Depends(get_current_user)]`` (session-Bearer only). That
would reject every Agent token outright (a ``snm_...`` value is not a valid
session token) before this file's own dual-mode guard ever got a chance to
run.

Row/cell-scoped routes below carry ONLY ``row_id``/``column_id`` in their
URL — no ``notebook_id`` or ``table_id`` segment at all (unlike every
session-facing knowhow route in ``app.api.knowhow_routes``) — so each one resolves
row -> table -> notebook via ``repository().get_knowhow_row_location``
FIRST, then enforces access with the resolved notebook_id, exactly mirroring
the table-scoped discrimination route's own table -> notebook resolution.
Every read/write funnels through ``app.services.knowhow.api`` — the SAME
functions ``app.api.mcp_server``'s four mirror tools call, so HTTP and MCP
can never drift on response shape or business rules (only auth transport
differs).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import repository, require_user_or_agent, user_or_agent_scope
from app.models.knowhow import (
    KnowhowAgentTable,
    KnowhowCellCodePut,
    KnowhowCellCodeResult,
    KnowhowDiscriminationSet,
    KnowhowRowDetail,
)
from app.services.knowhow import api as knowhow_api
from app.services.knowhow import audit as knowhow_audit

agent_router = APIRouter()


async def _resolve_row_notebook(repo: Any, row_id: str, not_found_detail: str) -> dict:
    """Shared row -> {table_id, notebook_id} resolution + 404 gate for every
    row/cell-scoped route below."""
    location = await run_in_threadpool(repo.get_knowhow_row_location, row_id)
    if location is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return location


@agent_router.get(
    "/agent/knowhow/tables",
    response_model=list[KnowhowAgentTable],
)
def list_agent_tables(
    notebook_id: str,
    _actor: Any = Depends(require_user_or_agent("knowledge:read")),
) -> list[dict]:
    return knowhow_api.list_tables_for_agent(repository(), notebook_id)


@agent_router.get(
    "/agent/knowhow/tables/{table_id}/discrimination",
    response_model=KnowhowDiscriminationSet,
)
async def get_agent_discrimination(table_id: str, request: Request) -> dict:
    repo = repository()
    try:
        table = await run_in_threadpool(repo.get_knowhow_table, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    async with user_or_agent_scope(
        request, table["notebook_id"], "knowledge:read",
        not_found_detail="Table not found",
    ):
        wire_table = knowhow_api.to_wire_table(table)
        code_attachments = await run_in_threadpool(
            repo.list_knowhow_cell_code, table_id
        )
        try:
            return knowhow_api.build_discrimination_set(wire_table, code_attachments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@agent_router.get(
    "/agent/knowhow/rows/{row_id}",
    response_model=KnowhowRowDetail,
)
async def get_agent_row(row_id: str, request: Request) -> dict:
    repo = repository()
    location = await _resolve_row_notebook(repo, row_id, "Row not found")
    async with user_or_agent_scope(
        request, location["notebook_id"], "knowledge:read",
        not_found_detail="Row not found",
    ):
        table = knowhow_api.to_wire_table(
            await run_in_threadpool(repo.get_knowhow_table, location["table_id"])
        )
        code_attachments = await run_in_threadpool(
            repo.list_knowhow_cell_code, location["table_id"]
        )
        code_attachments = await run_in_threadpool(
            knowhow_audit.project_nested_updated_by, repo, code_attachments
        )
        try:
            return knowhow_api.build_row_detail(table, row_id, code_attachments)
        except KeyError:
            raise HTTPException(status_code=404, detail="Row not found")


@agent_router.get(
    "/agent/knowhow/rows/{row_id}/cells/{column_id}/code",
    response_model=KnowhowCellCodeResult,
)
async def get_agent_cell_code(row_id: str, column_id: str, request: Request) -> dict:
    repo = repository()
    location = await _resolve_row_notebook(repo, row_id, "Row not found")
    async with user_or_agent_scope(
        request, location["notebook_id"], "knowledge:read",
        not_found_detail="Row not found",
    ):
        try:
            result = await run_in_threadpool(
                knowhow_api.get_cell_code, repo, row_id, column_id
            )
            return await run_in_threadpool(
                knowhow_audit.project_nested_updated_by, repo, result
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Row not found")


@agent_router.put(
    "/agent/knowhow/rows/{row_id}/cells/{column_id}/code",
    response_model=KnowhowCellCodeResult,
)
async def put_agent_cell_code(
    row_id: str, column_id: str, body: KnowhowCellCodePut, request: Request,
) -> dict:
    repo = repository()
    location = await _resolve_row_notebook(repo, row_id, "Row not found")
    async with user_or_agent_scope(
        request, location["notebook_id"], "knowhow:code", write=True,
        not_found_detail="Row not found",
    ) as actor:
        try:
            # knowhow 表版本管理 Task 13 code review: this route is reachable
            # by EITHER a session user or an Agent Bearer token (see the
            # module docstring / user_or_agent_scope) — origin must reflect
            # WHICH one actually wrote this, not silently default to "user"
            # for both (put_cell_code's own default), or an agent-authored
            # code attachment would be indistinguishable from a human's in
            # the history timeline.
            return await run_in_threadpool(
                knowhow_api.put_cell_code, repo, row_id, column_id,
                body.code_text, body.language, actor.actor_label,
                origin="agent" if actor.is_agent else "user",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Row not found")


@agent_router.delete(
    "/agent/knowhow/rows/{row_id}/cells/{column_id}/code",
    status_code=204,
)
async def delete_agent_cell_code(row_id: str, column_id: str, request: Request) -> None:
    repo = repository()
    location = await _resolve_row_notebook(repo, row_id, "Row not found")
    async with user_or_agent_scope(
        request, location["notebook_id"], "knowhow:code", write=True,
        not_found_detail="Row not found",
    ) as actor:
        try:
            # Same origin-attribution fix as put_agent_cell_code above.
            await run_in_threadpool(
                knowhow_api.delete_cell_code, repo, row_id, column_id,
                actor=actor.actor_label,
                origin="agent" if actor.is_agent else "user",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail="Row not found")


__all__ = ["agent_router"]
