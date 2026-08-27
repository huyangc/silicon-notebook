"""Session-facing Knowhow table routes.

Agent Knowhow stays in app.api.knowhow_agent_routes.
"""

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, List, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    content_overview_service,
    get_current_user,
    notebook_access_repository as _kh_access,
    notebook_capability_allowed,
    repository,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.core.audit_actor import session_audit_principal
from app.models.identity import UserProfile
from app.models.knowhow import (
    VALID_ORIGINS,
    KnowhowAppendPreview,
    KnowhowAppendResult,
    KnowhowCellOptimizeResult,
    KnowhowCellPatch,
    KnowhowCellPatchResult,
    KnowhowCellReformatResult,
    KnowhowCellsBatchPatch,
    KnowhowColumn,
    KnowhowColumnCreate,
    KnowhowColumnPatch,
    KnowhowHistoryPruneRequest,
    KnowhowImportPreview,
    KnowhowMilestoneCreate,
    KnowhowRevertRequest,
    KnowhowRow,
    KnowhowRowCompleteRequest,
    KnowhowRowCompleteResult,
    KnowhowRowCreate,
    KnowhowTableCreate,
    KnowhowTableDetail,
    KnowhowTablePatch,
    KnowhowTableSummary,
    KnowhowTransferRequest,
)
from app.repositories.sqlite.knowhow_history_store import (
    HistoryInconsistent,
    HistoryStale,
    RevertVerifyFailed,
)
from app.services.knowhow import api as knowhow_api
from app.services.knowhow import audit as knowhow_audit
from app.services.knowhow import history as knowhow_history
from app.services.knowhow import transfer as _kh_transfer
from app.services.knowhow.grid_parser import GridParseError
from app.services.model_work import ModelNotConfiguredError
from app.api.task_stream import task_stream_response


router = APIRouter()


def _knowhow_import_user_error(
    exc: Union[GridParseError, knowhow_api.KnowhowImportValidationError],
) -> HTTPException:
    """Expose only messages carried by the import layer's safe exception types."""
    return user_error(400, exc.user_message)


# --- knowhow-tables PR-1 Task 6: table/import API. Guards mirror the asset
# routes above exactly: POST/DELETE need write access (owner-only via
# require_notebook_capability("knowhow:write"), 404 on denial), GET needs
# read access (owner ∪ read-only member via require_notebook_read, 404 on
# denial). Routes stay
# thin (param parsing / guard / dispatch + background-job launch); parsing,
# validation and row/table orchestration live in app.services.knowhow.api;
# GridParseError (a ValueError subclass) and the store's own ValueErrors
# (duplicate/empty column names, concept-count != 1) all flow through the
# same `except ValueError` 400 translation used throughout this file.
@router.post(
    "/notebooks/{notebook_id}/knowhow/import/preview",
    response_model=KnowhowImportPreview,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def preview_knowhow_import(
    notebook_id: str,
    file: UploadFile = File(...),
    anchor_index: Optional[int] = Form(None),
    orientation: str = Form("columns"),
) -> KnowhowImportPreview:
    """``anchor_index`` (optional, same wire shape as the import commit
    endpoint's) lets the wizard re-preview after the user changes the row-title
    (anchor) selection in step 2: when given, the preview skips THAT column from
    normalization instead of the guessed one, keeping "预览即所得" true for the
    user-confirmed anchor. Omitted on the initial load — preview then skips the
    guess (unchanged behavior). ``orientation`` selects whether the raw source
    presents attributes in columns (default) or rows."""
    data = await file.read()
    try:
        return knowhow_api.preview_import(
            file.filename or "import",
            data,
            orientation=orientation,
            anchor_index=anchor_index,
        )
    except (GridParseError, knowhow_api.KnowhowImportValidationError) as exc:
        raise _knowhow_import_user_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/knowhow/import",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def import_knowhow_table(
    notebook_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    columns_json: str = Form(...),
    anchor_index: Optional[int] = Form(None),
    orientation: str = Form("columns"),
    user: UserProfile = Depends(get_current_user),
) -> KnowhowTableDetail:
    """Parse -> validate -> create table -> insert every row -> schedule a
    debounced background full-table projection (ProjectionScheduler — PR-2+3
    Task 3; NEVER a raw background_jobs.submit call) -> return the FULL table
    detail immediately. Rows carry projection_status='pending'/'syncing'
    until the background job advances them to 'synced'/'failed'. The
    frontend does NOT poll the detail endpoint — a row's settled status is
    observed the next time the table is (re)opened or the caller explicitly
    refreshes it.

    ``orientation`` selects whether raw source attributes are in columns
    (default) or rows; preview and commit normalize through the same parser.
    ``columns_json``=``[{name,kind}]`` + the separate ``anchor_index`` form
    field (PR-2+3 Task 3 wire — kind is one of three non-anchor values; the
    row-title designation is named ONLY via anchor_index, never inline)."""
    # 稳定 creator id 与可读流水 label 分开传递；wire 形状不变。
    repo = repository()
    principal = session_audit_principal(user)
    data = await file.read()
    try:
        table_id = knowhow_api.import_table(
            repo, notebook_id, file.filename or "import", data, title,
            columns_json, anchor_index, orientation,
            created_by_id=principal.identity_id,
            actor_label=principal.audit_label,
        )
    except (GridParseError, knowhow_api.KnowhowImportValidationError) as exc:
        raise _knowhow_import_user_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return knowhow_api.to_wire_table(repo.get_knowhow_table(table_id))


@router.get(
    "/notebooks/{notebook_id}/knowhow",
    response_model=List[KnowhowTableSummary],
    dependencies=[Depends(require_notebook_read)],
)
def list_knowhow_tables(notebook_id: str) -> List[dict]:
    return content_overview_service().knowhow_tables(notebook_id)


@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_read)],
)
def get_knowhow_table(notebook_id: str, table_id: str) -> dict:
    try:
        return knowhow_api.get_table_in_notebook(repository(), notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def delete_knowhow_table(notebook_id: str, table_id: str) -> None:
    """Cascade-delete (task brief: "连投影产物+隐藏源"). Ordering: fetch+
    cross-notebook-check first (404 without touching anything), THEN the
    projector's cleanup of everything ever derived from the hidden source
    (relations/objects/chunks/elements/the source row itself — safe no-op when
    hidden_source_id is None, e.g. a table created but never projected), and
    ONLY THEN the store's own cascade delete (columns/rows/cells via ON DELETE
    CASCADE). Projection-teardown-first so a crash BETWEEN the two phases
    leaves a still-deletable table whose projection is already clean, rather
    than an orphaned hidden source with no table left to trigger its cleanup."""
    repo = repository()
    try:
        detail = knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    projector = knowhow_api.build_projector(repo)
    snapshot_hidden_source_id = detail.get("hidden_source_id")
    projector.delete_table_projection(snapshot_hidden_source_id)
    deleted = repo.delete_knowhow_table(table_id)
    # A first projection can attach its hidden source after the detail snapshot
    # above but before the table DELETE obtains its row lock. The store returns
    # the id observed by that atomic DELETE; clean that authoritative id too.
    # This second call is deliberately idempotent when both ids are equal.
    projector.delete_table_projection(deleted.get("hidden_source_id"))
    # Deleting a table bulk-orphans every asset its cells referenced, and it is
    # the one mutation that deliberately never schedules a projection (the table
    # is gone), so the projection-completion trigger would never see it — if this
    # was the notebook's only knowhow table, no later projection can ever fire.
    # min_interval=0 deliberately BYPASSES the throttle: the throttle exists to
    # bound how often the scan runs under continuous editing, but a delete is
    # rare and is the last chance this notebook may get, so it must not be
    # swallowed by a sweep that happened to run moments earlier. Backgrounded so
    # a DELETE never waits on an O(assets x cells) scan.
    #
    # This pass normally only MARKS the freshly orphaned assets (the grace runs
    # from first-seen-unreferenced), leaving removal to a later sweep. That is
    # fine while other tables remain — their next projection reclaims them — but
    # if this was the notebook's LAST knowhow table, no later projection can ever
    # fire and the marks would never mature: the files would survive until the
    # notebook itself is deleted, i.e. exactly the "GC never runs" state this
    # trigger exists to end.
    #
    # waive_grace_if_no_tables is this route's INTENT, not its observation: the
    # sweep re-reads "does this notebook still have a table" under the write
    # lock before acting on it, because another request can create one (and
    # upload a draft image into it) between here and the background worker's
    # scan. Both must hold — asked for by a last-table delete, and still true at
    # deletion time — or the grace stays in force. See sweep_orphan_assets.
    knowhow_api.maybe_sweep_orphan_assets(
        repo,
        notebook_id,
        min_interval=0,
        background=True,
        waive_grace_if_no_tables=True,
    )


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/reproject",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def reproject_knowhow_table(notebook_id: str, table_id: str) -> dict:
    """Full-rebuild escape hatch (task brief: "全量重投影逃生口，后台执行") —
    schedules KnowhowProjector.project_table (NEVER the seq-gated incremental
    path, and NEVER a raw background_jobs.submit — always through
    ProjectionScheduler, PR-2+3 Task 3) and responds immediately with a
    status body, mirroring build_kg/rebuild_kg's
    synchronous-response-then-background-work shape exactly."""
    repo = repository()
    try:
        knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return {"status": "reprojecting", "table_id": table_id}


# --- knowhow-tables PR-2+3 Task 3: editing API (table/column/row/cell) +
# create-table wizard backend. Guards mirror the import/table routes above
# exactly (owner-only write via require_notebook_capability("knowhow:write"),
# 404 on denial). Every mutation schedules a debounced full-table reprojection via
# knowhow_api.get_scheduler(repo).schedule(table_id) — never a raw
# background_jobs.submit call, and never seq-gated (ProjectionScheduler
# always runs the full deterministic project_table pass). Routes stay thin;
# the only logic here is param parsing, the table/column/row 404-scoping
# helpers below, and dispatch — validation/orchestration lives in
# app.services.knowhow.api, whose ValueErrors flow through this file's
# existing `except ValueError` 400 idiom.


def _require_table(repo: Any, notebook_id: str, table_id: str) -> dict:
    """Shared 404 gate: table must exist AND belong to notebook_id. Returns
    the wire-shaped table detail (columns already renamed to kind) since
    every caller either returns it directly or uses it to scope a row/column
    id that must belong to THIS table (not merely notebook_id) — see
    _require_column_in_table/_require_row_in_table below."""
    try:
        return knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")


def _require_column_in_table(table: dict, column_id: str) -> None:
    """A column_id from a DIFFERENT table (even one in the same notebook, or
    one the caller has no access to at all) 404s exactly like a
    cross-notebook table_id does — the URL's table_id is the caller's proven
    scope, not merely notebook_id (mirrors get_table_in_notebook's own
    cross-notebook check one level deeper)."""
    if not any(column["id"] == column_id for column in table["columns"]):
        raise HTTPException(status_code=404, detail="Column not found")


def _require_row_in_table(table: dict, row_id: str) -> None:
    if not any(row["id"] == row_id for row in table["rows"]):
        raise HTTPException(status_code=404, detail="Row not found")


@router.post(
    "/notebooks/{notebook_id}/knowhow",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def create_knowhow_table(
    notebook_id: str, body: KnowhowTableCreate,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """Create-table wizard backend: an EMPTY table (title + column
    definitions only, no grid/rows — mirrors import_table's create step
    minus the file). Does not schedule a reprojection (nothing to project
    yet on a zero-row table); the first row/cell mutation schedules the
    table's first real run."""
    # 稳定 creator id 与可读创世流水 label 分开传递。
    repo = repository()
    principal = session_audit_principal(user)
    try:
        table_id = knowhow_api.create_table(
            repo, notebook_id, body.title,
            [{"name": column.name, "kind": column.kind} for column in body.columns],
            body.anchor_index,
            created_by_id=principal.identity_id,
            actor_label=principal.audit_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def patch_knowhow_table(
    notebook_id: str, table_id: str, patch: KnowhowTablePatch,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """title/description/anchor_column_id are all optional; anchor_column_id
    additionally distinguishes "omitted" (leave alone) from "explicit null"
    (clear the row-title designation) via ``model_fields_set`` — omission and
    None both parse to the same Python value, so only the set of keys the
    client actually sent tells them apart. A patch that only renames the
    table still schedules reprojection: the table title feeds every row's
    chunk section_path label (design doc §④), so renaming it is itself a
    content change from the projector's point of view."""
    # 所有流水写入统一使用 session audit label。
    repo = repository()
    principal = session_audit_principal(user)
    _require_table(repo, notebook_id, table_id)
    fields_set = patch.model_fields_set
    try:
        if "anchor_column_id" in fields_set:
            repo.set_knowhow_anchor_column(
                table_id, patch.anchor_column_id, actor=principal.audit_label
            )
        title = patch.title if "title" in fields_set else None
        description = patch.description if "description" in fields_set else None
        if title is not None or description is not None:
            repo.update_knowhow_table_meta(
                table_id, title=title, description=description,
                actor=principal.audit_label,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns",
    response_model=KnowhowColumn,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def add_knowhow_column(
    notebook_id: str, table_id: str, body: KnowhowColumnCreate,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    repo = repository()
    principal = session_audit_principal(user)
    _require_table(repo, notebook_id, table_id)
    try:
        column_id = repo.add_knowhow_column(
            table_id, body.name, body.kind, body.position,
            actor=principal.audit_label,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    table = repo.get_knowhow_table(table_id)
    column = next(c for c in table["columns"] if c["id"] == column_id)
    return knowhow_api.to_wire_column(column)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns/{column_id}",
    response_model=KnowhowColumn,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def patch_knowhow_column(
    notebook_id: str, table_id: str, column_id: str, body: KnowhowColumnPatch,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    _require_column_in_table(table, column_id)
    fields_set = body.model_fields_set
    try:
        if "name" in fields_set and body.name is not None:
            repo.rename_knowhow_column(
                column_id, body.name, actor=principal.audit_label
            )
        if "kind" in fields_set and body.kind is not None:
            repo.set_knowhow_column_kind(
                column_id, body.kind, actor=principal.audit_label
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    column = next(c for c in updated["columns"] if c["id"] == column_id)
    return knowhow_api.to_wire_column(column)


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns/{column_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def delete_knowhow_column(
    notebook_id: str, table_id: str, column_id: str,
    user: UserProfile = Depends(get_current_user),
) -> None:
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    _require_column_in_table(table, column_id)
    repo.delete_knowhow_column(column_id, actor=principal.audit_label)
    knowhow_api.get_scheduler(repo).schedule(table_id)


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows",
    response_model=KnowhowRow,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def add_knowhow_row(
    notebook_id: str, table_id: str, body: KnowhowRowCreate,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    valid_column_ids = {column["id"] for column in table["columns"]}
    if set(body.cells) - valid_column_ids:
        raise user_error(400, "格子对应的列不属于本表")
    row_id = repo.add_knowhow_row(
        table_id, body.cells, body.position, actor=principal.audit_label
    )
    knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    return next(r for r in updated["rows"] if r["id"] == row_id)


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def delete_knowhow_row(
    notebook_id: str, table_id: str, row_id: str,
    user: UserProfile = Depends(get_current_user),
) -> None:
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    _require_row_in_table(table, row_id)
    repo.delete_knowhow_row(row_id, actor=principal.audit_label)
    knowhow_api.get_scheduler(repo).schedule(table_id)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}",
    response_model=KnowhowCellPatchResult,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def patch_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str, body: KnowhowCellPatch,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """Returns {row_id,column_id,content_md,projection_status} so the caller
    can refresh just this row's sync badge without re-fetching the whole
    table. row_id/column_id validity is checked twice, deliberately: the
    table-scoped membership check (belongs to THIS table_id, not merely SOME
    table) and the store's own validate_cell_target (same-table pairing) —
    both funnel into the same 400 "格子定位不合法" so a client sees one
    consistent error regardless of which check actually caught it.

    knowhow 表版本管理 Task 12: ``body.origin`` is validated FIRST — a pure
    in-memory check with no DB lookups, and a hard 400 on anything outside
    VALID_ORIGINS (never a lenient default; see KnowhowCellPatch.origin's own
    docstring for why)."""
    # 流水使用 session audit label，权限仍由稳定 user id 守卫。
    if body.origin not in VALID_ORIGINS:
        raise user_error(400, "未知的变更来源")
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    if (
        not any(row["id"] == row_id for row in table["rows"])
        or not any(column["id"] == column_id for column in table["columns"])
    ):
        raise user_error(400, "格子定位不合法")
    try:
        repo.validate_cell_target(row_id, column_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Concurrency P1 (fix b): when the caller supplies expected_before (the
    # batch-reformat save path), the write must be a SERVER-SIDE atomic
    # compare-and-write — the whole point is to close the client-side TOCTOU the
    # old preflight-fetch-then-PATCH left open. Route it through the guarded store
    # method (one entry); a stale baseline → 409 with a friendly message, nothing
    # written. When expected_before is None (the manual cell editor), keep the
    # existing last-write-wins path unchanged — a human actively editing this
    # cell should not have their save refused by their own earlier snapshot.
    if body.expected_before is not None:
        # F2: the guarded (compare-and-write) branch must run the SAME newly-
        # dangling-asset guard the legacy branch below runs — otherwise a guarded
        # save can commit content referencing a missing/foreign asset. Compute the
        # newly added refs against the CURRENT cell and hand them to the store,
        # which re-checks them INSIDE its write transaction (a sweep running now
        # cannot slip between us); a miss raises ValueError -> legacy 400.
        current_row = next(r for r in table["rows"] if r["id"] == row_id)
        added_assets = knowhow_api.newly_added_asset_refs(
            current_row.get("cells", {}).get(column_id, ""), body.content_md
        )
        try:
            result = repo.update_knowhow_cells_guarded_atomic(
                notebook_id,
                [(table_id, row_id, column_id, body.expected_before, body.content_md)],
                require_assets=added_assets,
                actor=principal.audit_label, origin=body.origin,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=knowhow_api.CELL_ASSET_MISSING_MESSAGE)
        if result["conflict"]:
            raise user_error(409, "内容已被其他人修改，请刷新后重试")
    else:
        # Refuse to persist a NEWLY dangling asset:// link (the image was reclaimed
        # while it lived only in this browser draft). We cannot restore the file, but
        # silently writing a dead reference would hand the user a broken image with
        # no explanation. The store re-checks these ids inside its own write
        # transaction, so a sweep running right now cannot slip between us.
        current_row = next(r for r in table["rows"] if r["id"] == row_id)
        added_assets = knowhow_api.newly_added_asset_refs(
            current_row.get("cells", {}).get(column_id, ""), body.content_md
        )
        try:
            repo.update_knowhow_cell(
                row_id, column_id, body.content_md,
                require_assets=added_assets, actor=principal.audit_label,
                origin=body.origin,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=knowhow_api.CELL_ASSET_MISSING_MESSAGE)
    knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    row = next(r for r in updated["rows"] if r["id"] == row_id)
    return {
        "row_id": row_id,
        "column_id": column_id,
        "content_md": row["cells"].get(column_id, ""),
        "projection_status": row["projection_status"],
    }


# --- followup A: batch cell write, ONE DB transaction (anchor-grouping spec
# §6「整组批量写单事务，不半改」). A merged/shared cell (one attribute, same
# value across an entire concept group's branch rows) edited through the
# frontend used to fire N independent patch_knowhow_cell PATCHes
# (Promise.all) — a mid-batch failure could leave the group half-written.
# This endpoint writes the whole batch through repo.update_knowhow_cells,
# whose ONE `with self.database.write() as db:` block makes it all-or-
# nothing at the SQL layer. Validation still happens BEFORE any write, same
# as patch_knowhow_cell above (belt-and-suspenders: table-scoped membership
# here, then validate_cell_target's same-table pairing) — so a single bad
# row_id 400s the ENTIRE request and update_knowhow_cells is never called at
# all, meaning the other, valid rows in the batch don't get written either.
@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}/cells",
    response_model=List[KnowhowCellPatchResult],
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def patch_knowhow_cells_batch(
    notebook_id: str, table_id: str, body: KnowhowCellsBatchPatch,
    user: UserProfile = Depends(get_current_user),
) -> list:
    """Returns one {row_id,column_id,content_md,projection_status} entry per
    row in body.row_ids, in the same order — same shape patch_knowhow_cell
    returns for a single row, just as a list.

    knowhow 表版本管理 Task 12: ``body.origin`` validated first, same as
    patch_knowhow_cell — this endpoint is the merged-cell fan-out's save path
    (see the module docstring above), so it needs the identical whitelist
    check or a batch reformat-accept could never be attributed as
    'llm_reformat' in the history timeline."""
    # 流水使用 session audit label，权限仍由稳定 user id 守卫。
    if body.origin not in VALID_ORIGINS:
        raise user_error(400, "未知的变更来源")
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    row_ids = body.row_ids
    valid_row_ids = {row["id"] for row in table["rows"]}
    if (
        not all(row_id in valid_row_ids for row_id in row_ids)
        or not any(column["id"] == body.column_id for column in table["columns"])
    ):
        raise user_error(400, "格子定位不合法")
    try:
        for row_id in row_ids:
            repo.validate_cell_target(row_id, body.column_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Concurrency P1 (fix b): with per-row expected_before (the batch-reformat
    # fan-out save), write the WHOLE group through the guarded atomic store method
    # — one all-or-nothing compare-and-write, so a stale baseline on ANY branch
    # refuses the entire concept group (409, nothing written) rather than a
    # half-written group. expected_before is positionally parallel to row_ids.
    if body.expected_before is not None:
        if len(body.expected_before) != len(row_ids):
            # Client contract error (the UI always sends matching lengths); an
            # API/MCP client that gets it wrong sees the generic 400 Chinese copy.
            raise HTTPException(
                status_code=400, detail="expected_before length must match row_ids"
            )
        updates = [
            (table_id, row_id, body.column_id, expected, body.content_md)
            for row_id, expected in zip(row_ids, body.expected_before)
        ]
        # Round-4 P1: OPTIONAL anchor baseline guard (see KnowhowCellsBatchPatch),
        # sent alongside expected_before by the fan-out. Validated for the SAME
        # positional-parallelism as expected_before, then handed to the store so a
        # sibling row whose anchor left the shared group refuses the whole batch
        # (edited cell unchanged → expected_before alone can't catch it). Either
        # field None → the store never re-reads the anchor (byte-identical to the
        # expected_before-only guard); the single-cell route deliberately omits it.
        anchor_column_id = None
        expected_anchor = None
        if body.anchor_column_id is not None and body.expected_anchor is not None:
            if len(body.expected_anchor) != len(row_ids):
                raise HTTPException(
                    status_code=400, detail="expected_anchor length must match row_ids"
                )
            anchor_column_id = body.anchor_column_id
            expected_anchor = body.expected_anchor
        # F2: same newly-dangling-asset guard as the single-cell guarded branch —
        # cover EVERY fan-out target's new content (an asset is guarded if it is
        # new to at least one target row), identical to the legacy batch else-branch
        # below. The store re-checks inside its write transaction; a miss -> 400.
        current_rows_by_id = {row["id"]: row for row in table["rows"]}
        added_assets = sorted(
            {
                asset_id
                for row_id in row_ids
                for asset_id in knowhow_api.newly_added_asset_refs(
                    current_rows_by_id[row_id].get("cells", {}).get(body.column_id, ""),
                    body.content_md,
                )
            }
        )
        try:
            result = repo.update_knowhow_cells_guarded_atomic(
                notebook_id,
                updates,
                require_assets=added_assets,
                anchor_column_id=anchor_column_id,
                expected_anchor=expected_anchor,
                actor=principal.audit_label, origin=body.origin,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=knowhow_api.CELL_ASSET_MISSING_MESSAGE)
        if result["conflict"]:
            raise user_error(409, "内容已被其他人修改，请刷新后重试")
    else:
        # Same newly-dangling-asset guard as the single-cell route above. It cannot
        # be skipped here just because this path is "internal": the editor saves
        # every MERGED/shared cell through this endpoint, so a draft whose image was
        # reclaimed reaches the database via THIS call, not the single-cell one. An
        # asset is guarded if it is new to at least one target row.
        current_rows_by_id = {row["id"]: row for row in table["rows"]}
        added_assets = sorted(
            {
                asset_id
                for row_id in row_ids
                for asset_id in knowhow_api.newly_added_asset_refs(
                    current_rows_by_id[row_id].get("cells", {}).get(body.column_id, ""),
                    body.content_md,
                )
            }
        )
        try:
            repo.update_knowhow_cells(
                row_ids, body.column_id, body.content_md,
                require_assets=added_assets, actor=principal.audit_label,
                origin=body.origin,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail=knowhow_api.CELL_ASSET_MISSING_MESSAGE)
    if row_ids:
        knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    rows_by_id = {r["id"]: r for r in updated["rows"]}
    return [
        {
            "row_id": row_id,
            "column_id": body.column_id,
            "content_md": rows_by_id[row_id]["cells"].get(body.column_id, ""),
            "projection_status": rows_by_id[row_id]["projection_status"],
        }
        for row_id in row_ids
    ]


# --- knowhow-tables PR-2+3 Task 6: Excel template round-trip (design doc §②
# 路B). GET needs read access (mirrors every other knowhow GET); POST needs
# write access (mirrors every other knowhow mutation) and — like every other
# mutating knowhow endpoint — schedules a debounced reprojection via
# knowhow_api.get_scheduler(repo).schedule(table_id) after a successful
# commit, never a raw background_jobs.submit call. mode=preview never
# writes; mode=commit is the only branch that does, using the identical
# parse+align step preview already ran once for the user to review.


def _template_content_disposition(filename: str) -> str:
    """RFC 5987/6266 Content-Disposition value for a (possibly non-ASCII)
    download filename: an ASCII-sanitized `filename=` fallback for clients
    that don't understand the extended form, plus the real UTF-8 name via
    `filename*=` (what every modern browser actually uses). Needed because
    Starlette encodes header VALUES as latin-1 (Response.init_headers) — a
    raw Chinese table title in a bare `filename="..."` alone would 500
    (UnicodeEncodeError) the instant a real (non-ASCII) table title reached
    this response. export_reports_endpoint's sibling zip download above
    never needed this: "reports.zip" is a fixed ASCII literal, never a
    user-supplied title."""
    import re
    from urllib.parse import quote

    ascii_fallback = re.sub(
        r'[\\"/\r\n\x00-\x1f]', "_", filename.encode("ascii", "ignore").decode("ascii")
    ).strip() or "template.xlsx"
    # safe="" so a literal "/" in a table title is %2F-escaped too — quote's
    # default (safe="/") would leave it raw, which violates RFC 5987's
    # attr-char grammar and is inconsistent with the fallback branch above,
    # which DOES strip "/".
    encoded = quote(filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}/template",
    dependencies=[Depends(require_notebook_read)],
)
def download_knowhow_template(notebook_id: str, table_id: str) -> StreamingResponse:
    """Excel template download (design doc §② 路B): the table's CURRENT
    column header (+ per-column kind hint / row-title annotation), generated
    fresh on every request — never cached, so it always reflects whatever
    columns exist right now. Mirrors export_reports_endpoint's
    StreamingResponse+BytesIO(via knowhow_api.build_template_xlsx)+
    Content-Disposition idiom above; see _template_content_disposition for
    why the filename needs the RFC 5987 `filename*=` form."""
    repo = repository()
    try:
        table = knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    data = knowhow_api.build_template_xlsx(table)
    filename = f"{table['title']}-template.xlsx"
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": _template_content_disposition(filename)},
    )


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/append",
    response_model=Union[KnowhowAppendPreview, KnowhowAppendResult],
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def append_knowhow_rows(
    notebook_id: str,
    table_id: str,
    file: UploadFile = File(...),
    mode: str = Form("preview"),
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """Excel append import (design doc §② 路B): matches the uploaded file's
    header BY COLUMN NAME against the table's existing columns (never by
    position — see knowhow_api._align_rows_to_table_columns). mode=preview
    parses+aligns without writing anything, reporting what commit WOULD do
    (rows_preview/total_rows/unmatched_columns/duplicate_titles); mode=commit
    performs the identical alignment, actually appends the rows, and
    schedules a debounced reprojection exactly like every other mutating
    knowhow endpoint."""
    # 追加导入流水使用 session audit label。
    if mode not in ("preview", "commit"):
        # 分类:这不是用户文案。mode 是 Form 参数,UI 永远不会发错值,只有 API/MCP
        # 客户端会踩;「mode 必须是 preview 或 commit」对真人毫无意义。故降级成普通
        # HTTPException(英文 detail = API 契约),真人看到的是 400 的通用中文文案。
        raise HTTPException(status_code=400, detail="mode must be 'preview' or 'commit'")
    repo = repository()
    principal = session_audit_principal(user)
    table = _require_table(repo, notebook_id, table_id)
    data = await file.read()
    try:
        if mode == "preview":
            return knowhow_api.preview_append(table, file.filename or "append", data)
        added = knowhow_api.commit_append(
            repo, table_id, table, file.filename or "append", data,
            actor_label=principal.audit_label,
        )
    except GridParseError as exc:
        raise _knowhow_import_user_error(exc)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return {"added": added}


# --- knowhow-tables PR-2+3 Task 8: LLM cell rewrite (design doc §③, explicit
# trigger only, suggestion-only). Write-gated like every other knowhow
# mutation (require_notebook_capability("knowhow:write")) even though it
# never writes the store itself — this is an authoring affordance (the cell-editor's "优化表达"
# button), not a read. Never schedules a reprojection either: the user's own
# explicit accept goes through the EXISTING PATCH cell endpoint, which is
# what writes + schedules. ValueError/ModelNotConfiguredError -> 400 (both
# validation-shaped, friendly Chinese message as-is); KnowhowOptimizeUnavailable
# (the LLM call itself failed after being reached, already logged via
# note_model_error inside optimize_cell) -> 502.


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/optimize",
    response_model=KnowhowCellOptimizeResult,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def optimize_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str
) -> dict:
    return _optimize_knowhow_cell(
        notebook_id, table_id, row_id, column_id
    )


def _optimize_knowhow_cell(
    notebook_id: str,
    table_id: str,
    row_id: str,
    column_id: str,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    repo, content_md, column_name, column_kind = _prepare_knowhow_cell_task(
        notebook_id,
        table_id,
        row_id,
        column_id,
    )
    if not content_md.strip():
        raise user_error(400, "格子为空，无需优化")
    if not repo._runtime.models.chat("knowhow_optimize").configured:
        raise user_error(400, "尚未配置模型，无法优化表达")
    return _run_optimize_knowhow_cell(
        repo,
        content_md,
        column_name,
        column_kind,
        cancel_event=cancel_event,
    )


def _prepare_knowhow_cell_task(
    notebook_id: str,
    table_id: str,
    row_id: str,
    column_id: str,
) -> tuple[Any, str, str, str]:
    """Resolve cheap authoritative cell state before a stream starts."""

    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    if (
        not any(row["id"] == row_id for row in table["rows"])
        or not any(column["id"] == column_id for column in table["columns"])
    ):
        raise user_error(400, "格子定位不合法")
    try:
        repo.validate_cell_target(row_id, column_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    row = next(r for r in table["rows"] if r["id"] == row_id)
    column = next(c for c in table["columns"] if c["id"] == column_id)
    content_md = row["cells"].get(column_id, "")
    return repo, content_md, column["name"], column["kind"]


def _run_optimize_knowhow_cell(
    repo: Any,
    content_md: str,
    column_name: str,
    column_kind: str,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    try:
        suggestion_md = knowhow_api.optimize_cell(
            repo,
            content_md,
            column_name,
            column_kind,
            cancel_event=cancel_event,
        )
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except knowhow_api.KnowhowOptimizeUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"suggestion_md": suggestion_md}


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/optimize/stream",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def optimize_knowhow_cell_stream(
    notebook_id: str,
    table_id: str,
    row_id: str,
    column_id: str,
    request: Request,
) -> StreamingResponse:
    repo, content_md, column_name, column_kind = await run_in_threadpool(
        _prepare_knowhow_cell_task,
        notebook_id,
        table_id,
        row_id,
        column_id,
    )
    if not content_md.strip():
        raise user_error(400, "格子为空，无需优化")
    if not repo._runtime.models.chat("knowhow_optimize").configured:
        raise user_error(400, "尚未配置模型，无法优化表达")
    cancel_event = threading.Event()
    return task_stream_response(
        request,
        lambda: _run_optimize_knowhow_cell(
            repo,
            content_md,
            column_name,
            column_kind,
            cancel_event=cancel_event,
        ),
        stage="knowhow_optimize",
        error_code="knowhow_optimize_failed",
        cancel_event=cancel_event,
    )


# Suggestion-only row completion. This is write/owner gated because it is an
# authoring action, but it neither mutates the table nor schedules projection;
# accepted suggestions return through the existing guarded PATCH cell route.
@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/complete",
    response_model=KnowhowRowCompleteResult,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def complete_knowhow_row(
    notebook_id: str,
    table_id: str,
    row_id: str,
    body: KnowhowRowCompleteRequest,
) -> dict:
    return _complete_knowhow_row(
        notebook_id, table_id, row_id, body
    )


def _complete_knowhow_row(
    notebook_id: str,
    table_id: str,
    row_id: str,
    body: KnowhowRowCompleteRequest,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    repo, table = _prepare_complete_knowhow_row(
        notebook_id, table_id, row_id, body
    )
    return _run_complete_knowhow_row(
        repo,
        notebook_id,
        table,
        row_id,
        body,
        cancel_event=cancel_event,
    )


def _prepare_complete_knowhow_row(
    notebook_id: str,
    table_id: str,
    row_id: str,
    body: KnowhowRowCompleteRequest,
) -> tuple[Any, dict]:
    """Validate row, targets and required workloads before streaming."""

    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    if not any(row["id"] == row_id for row in table["rows"]):
        raise user_error(400, "行定位不合法")
    models = repo._runtime.models
    workload_id = "reasoning_agent"
    try:
        knowhow_api.resolve_completion_request(
            table, row_id, body.target_column_ids
        )
        for workload_id in ("reasoning_agent", "knowhow_complete"):
            if not models.chat(workload_id).configured:
                raise ModelNotConfiguredError
    except ModelNotConfiguredError:
        raise user_error(400, "尚未配置模型，无法智能补全")
    except ValueError:
        raise user_error(400, "无法补全当前行，请检查目标列和已知内容")
    except Exception as exc:
        models.note_model_error(
            workload_id, exc, workload_id=workload_id
        )
        raise HTTPException(
            status_code=502,
            detail="智能补全服务暂时不可用，请稍后再试",
        )
    return repo, table


def _run_complete_knowhow_row(
    repo: Any,
    notebook_id: str,
    table: dict,
    row_id: str,
    body: KnowhowRowCompleteRequest,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    try:
        return knowhow_api.complete_row(
            repo,
            notebook_id,
            table,
            row_id,
            body.target_column_ids,
            cancel_event=cancel_event,
        )
    except ModelNotConfiguredError:
        raise user_error(400, "尚未配置模型，无法智能补全")
    except ValueError:
        raise user_error(400, "无法补全当前行，请检查目标列和已知内容")
    except knowhow_api.KnowhowCompletionUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/complete/stream",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def complete_knowhow_row_stream(
    notebook_id: str,
    table_id: str,
    row_id: str,
    body: KnowhowRowCompleteRequest,
    request: Request,
) -> StreamingResponse:
    repo, table = await run_in_threadpool(
        _prepare_complete_knowhow_row,
        notebook_id, table_id, row_id, body
    )
    cancel_event = threading.Event()
    return task_stream_response(
        request,
        lambda: _run_complete_knowhow_row(
            repo,
            notebook_id,
            table,
            row_id,
            body,
            cancel_event=cancel_event,
        ),
        stage="knowhow_complete",
        error_code="knowhow_complete_failed",
        cancel_event=cancel_event,
    )


# --- knowhow-md-normalize Task 4: /reformat HTTP endpoint (design doc §③-
# adjacent, same suggestion-only contract as /optimize just above) — exposes
# Task 3's knowhow_api.reformat_cell over HTTP for a later editor "格式化
# 排版" button (and a later-still batch action) to call. Structure mirrors
# optimize_knowhow_cell exactly (repo resolve -> _require_table 404 gate ->
# row/column table-membership 400 -> validate_cell_target 400 -> read the
# SAVED content_md). The one deliberate divergence: reformat_cell already
# handles an unconfigured LLM gracefully inside itself (falls back to
# rule_normalize, returns source="rule/no-llm") and NEVER raises
# ModelNotConfiguredError — see that function's own docstring — so this
# endpoint does NOT mirror optimize's ModelNotConfiguredError/
# KnowhowOptimizeUnavailable -> 400/502 mapping. It always 200s with
# {candidate_md, source, changed}; the caller reads `source` to decide how to
# present the candidate. Suggestion-only like optimize: never writes the
# cell, never schedules a reprojection — the user's own accept goes through
# the existing PATCH cell endpoint.
@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/reformat",
    response_model=KnowhowCellReformatResult,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def reformat_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str
) -> dict:
    return _reformat_knowhow_cell(
        notebook_id, table_id, row_id, column_id
    )


def _reformat_knowhow_cell(
    notebook_id: str,
    table_id: str,
    row_id: str,
    column_id: str,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    repo, content_md, column_name, column_kind = _prepare_knowhow_cell_task(
        notebook_id, table_id, row_id, column_id
    )
    return _run_reformat_knowhow_cell(
        repo,
        content_md,
        column_name,
        column_kind,
        cancel_event=cancel_event,
    )


def _run_reformat_knowhow_cell(
    repo: Any,
    content_md: str,
    column_name: str,
    column_kind: str,
    *,
    cancel_event: threading.Event | None = None,
) -> dict:
    result = knowhow_api.reformat_cell(
        repo,
        content_md,
        column_name,
        column_kind,
        cancel_event=cancel_event,
    )
    # Concurrency P1 (fix a): report the EXACT content_md we just read and fed to
    # reformat_cell so the batch client can tell "this candidate derives from the
    # cell I snapshotted" from "the live cell was edited under me". content_md is
    # the same string reformat_cell consumed (it does `raw = content_md or ""`,
    # and content_md is never None here — `.get(column_id, "")`), so source_md ==
    # the snapshot iff nobody edited the cell between modal-open and now.
    result["source_md"] = content_md
    return result


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/reformat/stream",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
async def reformat_knowhow_cell_stream(
    notebook_id: str,
    table_id: str,
    row_id: str,
    column_id: str,
    request: Request,
) -> StreamingResponse:
    repo, content_md, column_name, column_kind = await run_in_threadpool(
        _prepare_knowhow_cell_task,
        notebook_id, table_id, row_id, column_id
    )
    cancel_event = threading.Event()
    return task_stream_response(
        request,
        lambda: _run_reformat_knowhow_cell(
            repo,
            content_md,
            column_name,
            column_kind,
            cancel_event=cancel_event,
        ),
        stage="knowhow_reformat",
        error_code="knowhow_reformat_failed",
        cancel_event=cancel_event,
    )


# --- knowhow 表版本管理 Task 12: 历史时间线 / 单条详情 / 两版对比 / 单格历史 /
# 回退 / 里程碑增删 / 清理的 HTTP 端点 ---------------------------------------
# design doc `2026-07-22-knowhow-table-version-control-design.md` §8.1。存储层
# （Task 8-9）与服务层（Task 11：aggregate_diff 纯函数 + revert_table 编排）都
# 已就绪；这里只是把 facade 的一跳委托方法接到 HTTP 上。四个读端点直接透传
# store 的 dict 形状（无 response_model，同 reproject_knowhow_table 等既有薄
# 端点一致的写法）；写端点做校验 + 调度重投影 + （仅 revert）专属错误码映射。
#
# 路由注册顺序有一处硬约束：GET .../history/diff 必须排在 GET .../history/{seq}
# 前面。原因：两者都是 GET，Starlette 按注册顺序找第一个路径匹配的路由，一旦
# 命中就不会因为该路由自身的参数校验失败而回退去试下一条。本文件用
# `{seq:int}` 把"必须是数字"这条约束前移进路由正则本身（FastAPI/Starlette 的
# IntegerConvertor 只匹配数字片段），所以字面量子路径如 "diff"/"prune" 在路由
# 匹配阶段就已经与这条路由无关，不再依赖注册顺序——但顺序仍然保持"diff 在
# 前"，作为纵深防御，也是给未来在 {seq} 后面再加字面量子路径的人一个明显的
# 排列范例。
@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}/history",
    dependencies=[Depends(require_notebook_read)],
)
def get_knowhow_history(
    notebook_id: str,
    table_id: str,
    # 纵深防御（codex P2）：limit 必须正且有界。SQLite 把 LIMIT -1 当作"无限"，
    # 不挡的话任何读者都能一次拉回整表历史（反序列化 + 传输放大）。上限 500。
    limit: int = Query(50, gt=0, le=500),
    before_seq: Optional[int] = None,
    milestones_only: bool = False,
) -> dict:
    """时间线分页（spec §8.1 行 1）。响应体固定 3 个键：``head_seq``（当前
    最新 seq——前端拿它做"陈旧"比对，revert 请求体里的 expected_head_seq 就是
    这个值的回声）、``changes``（seq 倒序，``before_seq`` 严格小于用于向更旧
    翻页）、``milestones``（本表全部命名里程碑，含 stale 标记，§4.2）。

    ``milestones_only=true`` 时把本页 ``changes`` 按里程碑 seq 集合过滤——这是
    一次性的单页内过滤，不会为了凑够 ``limit`` 条而多轮翻页：里程碑本来就稀疏，
    这只是前端"只看里程碑"筛选的展示层需求，不值得为它引入内部多轮翻页的
    复杂度与延迟。

    ``head_seq``/``changes``/``milestones`` 三者由 ``knowhow_history_page`` 在
    **同一读快照**里一次取出（codex 第 4 轮 P1）：早前分三次 store 调用取，
    一条夹在中间提交的并发编辑会让 ``head_seq`` 领先于 ``changes`` 里最新一条，
    用户没看见它却能确认回退、陈旧守卫还照放，把这条未见编辑抹掉。
    ``milestones_only`` 的过滤是纯展示层，落在快照之后、对一致性无影响。"""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    page = knowhow_audit.project_history_page(
        repo, repo.knowhow_history_page(table_id, limit, before_seq)
    )
    changes = page["changes"]
    if milestones_only:
        milestone_seqs = {milestone["seq"] for milestone in page["milestones"]}
        changes = [change for change in changes if change["seq"] in milestone_seqs]
    return {
        "head_seq": page["head_seq"],
        "changes": changes,
        "milestones": page["milestones"],
    }


@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}/history/diff",
    dependencies=[Depends(require_notebook_read)],
)
def get_knowhow_history_diff(
    notebook_id: str,
    table_id: str,
    from_seq: int = Query(..., alias="from"),
    to_seq: int = Query(..., alias="to"),
) -> dict:
    """两版净变化（spec §8.1 行 3 / §6.5）——纯计算：只读区间 ``(from, to]``
    的流水再折叠，不碰任何写路径，也不校验 from/to 是否真实存在（区间落空
    时 aggregate_diff 对空列表的输出就是全 5 个键的"无变化"形状，天然合理）。
    必须注册在 .../history/{seq} 之前，见本节顶部的路由顺序说明。"""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    changes = repo.knowhow_changes_between(table_id, from_seq, to_seq)
    return knowhow_audit.project_nested_updated_by(
        repo, knowhow_history.aggregate_diff(changes)
    )


@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}/history/{seq:int}",
    dependencies=[Depends(require_notebook_read)],
)
def get_knowhow_change(notebook_id: str, table_id: str, seq: int) -> dict:
    """单条变更详情（spec §8.1 行 2）——payload 原样返回，前端用它渲染这一次
    改动的红绿 diff。"""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    change = repo.get_knowhow_change(table_id, seq)
    if change is None:
        raise HTTPException(status_code=404, detail="Change not found")
    return knowhow_audit.project_change(repo, change)


@router.get(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/history",
    dependencies=[Depends(require_notebook_read)],
)
def get_knowhow_cell_history(
    notebook_id: str, table_id: str, row_id: str, column_id: str,
    limit: int = Query(50, gt=0, le=500),  # 纵深防御（codex P2）：同 get_knowhow_history
) -> list:
    """单格历史时间线（spec §8.1 行 4）——最新在前。row_id/column_id 的
    table-scoped 校验与 patch_knowhow_cell 同款（同一 URL 形状的姊妹端点，见
    该函数文档字符串）。"""
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    if (
        not any(row["id"] == row_id for row in table["rows"])
        or not any(column["id"] == column_id for column in table["columns"])
    ):
        raise user_error(400, "格子定位不合法")
    return knowhow_audit.project_cell_history(
        repo, repo.knowhow_cell_history(table_id, row_id, column_id, limit)
    )


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/revert",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def revert_knowhow_table(
    notebook_id: str, table_id: str, body: KnowhowRevertRequest,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """整表回退到 ``target_seq``（spec §8.1 行 5 / §6.1）。错误映射（spec
    §8.1 表）：head 已变→409 knowhow_history_stale；前置指纹不一致→400
    knowhow_history_inconsistent；后置校验失败（已整事务回滚，表未被改动）→
    500 knowhow_revert_verify_failed；target_seq 不存在/不属于本表→404（无
    oracle，同本文件"不存在"与"越权"统一 404 的既定约定）。重放本体 + 提交后
    的重投影调度都在 knowhow_history.revert_table 里，这里只做错误码翻译。"""
    repo = repository()
    principal = session_audit_principal(user)
    try:
        knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    try:
        return knowhow_history.revert_table(
            repo, notebook_id, table_id,
            body.target_seq, body.expected_head_seq, principal.audit_label,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Change not found")
    except HistoryStale:
        raise HTTPException(status_code=409, detail={
            "code": "knowhow_history_stale",
            "message": "这张表刚被其他人改过，请刷新后重试",
        })
    except HistoryInconsistent:
        raise HTTPException(status_code=400, detail={
            "code": "knowhow_history_inconsistent",
            "message": "表的当前内容与变更历史对不上，回退已中止",
        })
    except RevertVerifyFailed:
        raise HTTPException(status_code=500, detail={
            "code": "knowhow_revert_verify_failed",
            "message": "回退结果校验失败，已放弃本次回退，表未被改动",
        })


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/milestones",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def create_knowhow_milestone(
    notebook_id: str, table_id: str, body: KnowhowMilestoneCreate,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    """给一个 ``seq`` 命名（spec §8.1 行 6 / §4.2）——零快照，只是一条指针
    记录。``seq`` 必须已经是这张表的一条真实流水，否则 404（同 revert 的
    "目标不存在无 oracle"约定，防止一个里程碑从创建那一刻起就指向空气；
    get_knowhow_change 按 table_id+seq 联合查询，"seq 不存在"与"seq 属于别的
    表"天然统一成同一个 None）。重名：这张表的 ``UNIQUE(table_id, name)``
    让 store 直接抛 ``sqlite3.IntegrityError``（store 自己的文档字符串明确
    "这里不做预检，省一次往返"，把翻译成用户文案的职责留给了这一层）——接住
    它转成 400，而不是让用户看见一坨裸的 500 异常堆栈。"""
    repo = repository()
    principal = session_audit_principal(user)
    _require_table(repo, notebook_id, table_id)
    # 事务外预检 = 快速失败给出明确 404；真正堵 TOCTOU 的是 create_milestone
    # 写事务内的复检（codex P2），那里 seq 不存在会 raise KeyError，下面接住转
    # 同一个 404（"目标不存在无 oracle"约定）。
    if repo.get_knowhow_change(table_id, body.seq) is None:
        raise HTTPException(status_code=404, detail="Change not found")
    try:
        return repo.create_knowhow_milestone(
            table_id, body.seq, body.name, body.note, principal.audit_label
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Change not found")
    except sqlite3.IntegrityError:
        raise user_error(400, "这个里程碑名字已经用过了，换一个试试")


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}/milestones/{milestone_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def delete_knowhow_milestone(notebook_id: str, table_id: str, milestone_id: str) -> None:
    """spec §8.1 行 7。store 的 ``delete_milestone`` 本身按 ``table_id``
    限定作用域，且对"已经不存在"静默 no-op（同本文件其余 delete 端点的既定
    约定），所以这里除了确认表本身存在/属于本 notebook 之外不需要再做额外的
    归属校验——一个属于别的表的 milestone_id 传进来，WHERE 子句天然不命中
    任何行，等同于"什么都没删"，不会误删。"""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    repo.delete_knowhow_milestone(table_id, milestone_id)


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/history/prune",
    dependencies=[Depends(require_notebook_capability("knowhow:write"))],
)
def prune_knowhow_history(
    notebook_id: str, table_id: str, body: KnowhowHistoryPruneRequest,
) -> dict:
    """删掉最老的连续前缀（spec §8.1 行 8 / §7.7）。``before_days`` 换算成
    ISO 截止时间：用与本仓库各 store 模块的 ``_now()`` 相同的"去掉微秒的
    naive 本地时间 isoformat"形状，才能和 ``knowhow_changes.created_at``
    做字面量字符串比较——两者格式必须一致，否则 SQL 里的字符串 ``<``
    比较会失真。

    §7.1 的已知代价在这里兑现：被清理掉的历史很可能是某些图片在库里唯一还
    "活着"的引用来源，删完之后这些图片才终于可能被判定为孤儿——所以清理
    完成后主动触发一次绕过节流的资产清扫（``min_interval=0``），而不是干等
    下一次自然节流窗口才释放磁盘；后台执行，不拖慢这次清理请求本身的响应。"""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    cutoff_iso = (
        (datetime.now() - timedelta(days=body.before_days))
        .replace(microsecond=0)
        .isoformat()
    )
    result = repo.prune_knowhow_history(table_id, cutoff_iso)
    knowhow_api.maybe_sweep_orphan_assets(repo, notebook_id, min_interval=0, background=True)
    return result


# --- knowhow 表跨 notebook 传输（复制/移动） -------------------------------
# 追加在文件尾：mode 决定源守卫（copy=read / move=write），无法用静态
# Depends(require_notebook_*)，故在处理器内手动核权（复用 deps 的访问仓库）。
# 用同步 def（同 create_report/create_object_schema）——FastAPI 自动放线程池跑。


@router.post("/notebooks/{notebook_id}/knowhow/{table_id}/transfer")
def transfer_knowhow_table(
    notebook_id: str,
    table_id: str,
    payload: KnowhowTransferRequest,
    user: UserProfile = Depends(get_current_user),
) -> dict:
    if payload.target_notebook_id == notebook_id:
        raise user_error(400, "源与目标不能是同一个笔记本")
    repo = repository()
    principal = session_audit_principal(user)
    access = _kh_access()
    # 只有 copy 能放宽到「只读成员」——写成 `read if copy else 写判定` 而不是
    # `写判定 if move else read`，是为了失败关闭（默认最严兜底）：将来若多出
    # 第三种 mode，它继承的是写级别的强守卫，而不是悄悄拿到只读成员权限。
    # 今天两者等价（mode 是 Literal["copy","move"]），差别只在未来。
    # 写半走 notebook_capability_allowed("knowhow:write", ...) 而不是手拼
    # user_can_access_notebook：与本文件 21 个装饰器端点吃同一张能力表，
    # P1/P2 翻转 knowhow:write 时 move/目标库判定自动跟随——否则会出现
    # 「组管理员能改每个格子、却复制/移动不了整张表」的半翻转，且报的还是
    # 与「库不存在」同码的 404。读半（copy 的源库）沿用 user_can_read_notebook。
    source_ok = (
        access.user_can_read_notebook(notebook_id, principal.identity_id)
        if payload.mode == "copy"
        else notebook_capability_allowed(
            "knowhow:write", notebook_id, principal.identity_id
        )
    )
    if not source_ok:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not notebook_capability_allowed(
        "knowhow:write", payload.target_notebook_id, principal.identity_id
    ):
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        # 复用既有 helper（同 reproject 路由 routes.py:583-586 的「只做存在性+
        # 归属校验、丢弃返回值」用法）：它自己的 docstring 持有「'从未存在' 与
        # '存在但属于别的 notebook' 统一 KeyError → 路由统一 404，不泄露是哪种」
        # 这条策略。手写同款检查会把该策略复刻到第二处，日后必然静默分叉。
        knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    try:
        new_table_id = _kh_transfer.transfer_table(
            repo, table_id, payload.target_notebook_id,
            principal.identity_id, payload.mode,
            actor_label=principal.audit_label,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Table not found")
    except _kh_transfer.SourceCleanupFailed as exc:
        # 复制已提交、清理源(拆投影/删源表)失败：副本已在目标存在，源仍在——
        # 结构化 409 让前端能诚实地告诉用户"重复不丢失"，而不是裸 500 诱导用户
        # 盲目重试(会在目标侧越堆越多重复副本)。见 A3 评审附加需求。
        #
        # round 10 P1-A：exc.reason 区分两种截然不同的成因，绝不能共用同一条
        # "请手动删除源表"的指引——"source_changed" 时源是被有意保留下来保护
        # 一份并发编辑的（指纹复核未命中），照做会连带丢掉那份编辑；只有
        # "cleanup_error"（拆投影/原子删除本身抛出异常，源确实原封未动）才
        # 适用"手动删源"这条建议。code 因此分裂成两个：source_cleanup_failed
        # 原样保留给 cleanup_error（不破坏既有契约/前端已有的分支判断），新增
        # source_changed_kept 给 source_changed。
        if exc.reason == "source_changed":
            code = "source_changed_kept"
            message = (
                "源表在复制完成后有更新的修改，为避免丢失该修改，源表已被保留——"
                "目标笔记本里的副本是这次修改之前的旧版本。请核对后删除目标里的"
                "旧副本，或重新发起一次搬迁；请勿删除源表。"
            )
        else:
            code = "source_cleanup_failed"
            message = "已复制到目标，但源表未删除；请手动删除源表，或先删掉多余副本再重试"
        raise HTTPException(
            status_code=409,
            detail={
                "code": code,
                "new_table_id": exc.new_table_id,
                "message": message,
            },
        )
    return {"new_table_id": new_table_id}
