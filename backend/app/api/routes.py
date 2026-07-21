import asyncio
import io
import json
import logging
import queue
import zipfile
from pathlib import Path
from typing import Any, List, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import (
    admin_query_repository, identity_repository,
    notebook_access_repository, notebook_catalog_repository,
    notebook_sharing_repository, repository, require_notebook_access,
    require_notebook_read, require_notebook_write, get_current_user, source_repository, user_error,
)
from app.core.config import get_settings
from app.services.sqlite_repository import KnowledgeGraphTooLargeError
from app.models.schemas import (
    AdminUserNotebook,
    AdminUserUsage,
    AskRequest,
    AskResponse,
    ConceptWhitelistAdd,
    ConceptWhitelistEntry,
    ConversationDetail,
    ConversationRenameRequest,
    ConversationSummary,
    DetectDocTypesRequest,
    DetectedDocType,
    DuplicateGroup,
    EdgeReviewItem,
    EdgeReviewRequest,
    FeedbackRequest,
    FeedbackResponse,
    KgSearchResponse,
    KnowhowCellPatch,
    KnowhowCellPatchResult,
    KnowhowCellsBatchPatch,
    KnowhowColumn,
    KnowhowColumnCreate,
    KnowhowColumnPatch,
    KnowhowImportPreview,
    KnowhowRow,
    KnowhowRowCreate,
    KnowhowTableCreate,
    KnowhowTableDetail,
    KnowhowTablePatch,
    KnowhowTableSummary,
    KnowledgeGraph,
    KnowledgeRecord,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    PaginatedKnowledge,
    MergeRequest,
    MergeReviewJob,
    MergeReviewRequest,
    MergeReviewSummary,
    ModelServiceView,
    ModelServiceStatusItem,
    ModelServicesStatus,
    ModelSettingsUpdate,
    ModelTestRequest,
    ModelTestResult,
    MountedBase,
    MountedByCount,
    NotebookAnalytics,
    NotebookCreate,
    NotebookRef,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    PromoteRequest,
    PromotionApproveResult,
    PromotionCandidate,
    PaginatedSources,
    PromotionRejectRequest,
    ReportCreate,
    ReportDetail,
    ReportExportRequest,
    ReportGenerateRequest,
    ReportOutlineUpdate,
    ReportSummary,
    SourceDetail,
    SourceElement,
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    SetTierRequest,
    ShareResponse,
    SharedByMeItem,
    SharedPreview,
    SourceImportRequest,
    SourceSummary,
    RebuildScaleIndexRequest,
    ScaleIndexStatus,
    SetBasesRequest,
    UnifiedKgStatus,
    UserProfile,
    KnowhowAppendPreview,
    KnowhowAppendResult,
    KnowhowCellOptimizeResult,
    KnowhowCellReformatResult,
)
from app.services import background_jobs
from app.services.ask_modes import resolve_mode, UnknownAskMode, ASK_MODES
from app.services.kg import scheduler as kg_scheduler
from app.services.knowhow import api as knowhow_api
from app.services.knowhow.assets import AssetService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.model_config import (
    ModelNotConfiguredError,
    STATUS_SERVICE_ROLES,
    model_config_fingerprint,
)
from app.services.model_status import ModelStatusService
from app.services.pending_bus import pending_bus
from app.repositories.ports import AskStreamPort, UploadedSourceFile
from app.api.memory_routes import memory_router
from app.repositories.sqlite.kg_build_job_store import KgBuildAlreadyRunning

logger = logging.getLogger("silicon_notebook.api.routes")

router = APIRouter()
router.include_router(memory_router)

SUPPORTED_SOURCE_SUFFIXES = {".pdf", ".md", ".markdown", ".docx", ".pptx", ".csv", ".xlsx", ".xlsm"}
MAX_SOURCE_UPLOAD_BYTES = 50 * 1024 * 1024


def _asset_service() -> AssetService:
    return AssetService(repository())


def _validate_source_file(file_name: str, content_size: int | None = None) -> None:
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source file type. Supported suffixes: {supported}",
        )
    if content_size is not None:
        if content_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded source file is empty")
        if content_size > MAX_SOURCE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded source file is too large")


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": settings.llm_configured,
        "reasoning_llm_configured": settings.reasoning_llm_configured,
        "embedding_configured": settings.embedder_configured,
    }


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user


_MODEL_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")


def _mask_key(key: str) -> str:
    # 只露尾 4 位且必须确有被截断的前缀(len>4)；≤4 位则整体隐去，绝不暴露完整短 key。
    key = key or ""
    return f"…{key[-4:]}" if len(key) > 4 else ("…" if key else "")


@router.get("/me/model-settings")
def get_model_settings(user: UserProfile = Depends(get_current_user)):
    repo = identity_repository()
    stored = repo.get_user_model_settings(user.id)
    out = {}
    for role in _MODEL_ROLES:
        svc = stored.get(role) or {}
        out[role] = ModelServiceView(
            base_url=svc.get("base_url", ""),
            model=svc.get("model", ""),
            has_key=bool(svc.get("api_key")),
            key_hint=_mask_key(svc.get("api_key", "")),
            source=repo.resolve_model_config(user, role).source,
        )
    return out


@router.put("/me/model-settings")
def put_model_settings(payload: ModelSettingsUpdate, user: UserProfile = Depends(get_current_user)):
    repo = identity_repository()
    stored = dict(repo.get_user_model_settings(user.id))
    before = {
        role: model_config_fingerprint(repo.resolve_model_config(user, role))
        for role in STATUS_SERVICE_ROLES
    }
    for role in _MODEL_ROLES:
        upd = getattr(payload, role)
        if upd is None:
            continue
        svc = dict(stored.get(role) or {})
        for field in ("base_url", "api_key", "model"):
            val = getattr(upd, field)
            if val is None:          # 不变
                continue
            if val == "":            # 清除
                svc.pop(field, None)
            else:                    # 设置
                svc[field] = val
        if svc:
            stored[role] = svc
        else:
            stored.pop(role, None)
    repo.set_user_model_settings(user.id, stored)
    invalidated = {
        role
        for role in STATUS_SERVICE_ROLES
        if model_config_fingerprint(repo.resolve_model_config(user, role)) != before[role]
    }
    repo.clear_model_service_statuses(user.id, sorted(invalidated))
    return get_model_settings(user)


@router.post("/me/model-settings/test", response_model=ModelTestResult)
def test_model_service(payload: ModelTestRequest, user: UserProfile = Depends(get_current_user)):
    import time
    if payload.service not in _MODEL_ROLES:
        return ModelTestResult(ok=False, code="unknown_service")
    repo = identity_repository()
    stored = repo.get_user_model_settings(user.id).get(payload.service) or {}
    api_key = payload.api_key if payload.api_key else stored.get("api_key", "")
    base_url, model = payload.base_url.strip(), payload.model.strip()
    if not (base_url and model and api_key):
        return ModelTestResult(ok=False, code="missing_config")
    started = time.perf_counter()
    settings = get_settings()
    try:
        if payload.service == "rerank":
            from app.services.rerank_client import RerankClient
            RerankClient(settings, model=model, base_url=base_url, api_key=api_key)._rerank_batch(
                "ping", ["a", "b"])
        else:
            from app.core.llm import OpenAICompatibleClient
            OpenAICompatibleClient(settings, base_url=base_url, api_key=api_key, model=model).chat_json(
                [{"role": "user", "content": "ping"}], "{}", timeout=10, max_retries=0)
        return ModelTestResult(ok=True, latency_ms=round((time.perf_counter() - started) * 1000))
    except Exception:
        logger.exception("draft model service test failed for %s", payload.service)
        return ModelTestResult(ok=False, latency_ms=round((time.perf_counter() - started) * 1000),
                               code="upstream_error")


def _model_status_service() -> ModelStatusService:
    return ModelStatusService(identity_repository(), get_settings())


@router.get("/me/model-services/status", response_model=ModelServicesStatus)
def get_model_services_status(user: UserProfile = Depends(get_current_user)):
    return _model_status_service().snapshot(user)


@router.post("/me/model-services/test-all", response_model=ModelServicesStatus)
def test_all_model_services(user: UserProfile = Depends(get_current_user)):
    return _model_status_service().test_all(user)


@router.post("/me/model-services/{service}/test", response_model=ModelServiceStatusItem)
def test_current_model_service(service: str, user: UserProfile = Depends(get_current_user)):
    if service not in STATUS_SERVICE_ROLES:
        raise HTTPException(status_code=404, detail="unknown model service")
    return _model_status_service().test_one(user, service)


@router.get("/doc-types")
def list_doc_types():
    """Document-type options for the upload picker ('' = auto-detect)."""
    from app.services.extraction_profiles import PROFILES

    return [{"id": "", "label": "自动检测"}] + [
        {"id": profile.id, "label": profile.label} for profile in PROFILES.values()
    ]


@router.post("/detect-doc-types", response_model=List[DetectedDocType])
def detect_doc_types(payload: DetectDocTypesRequest) -> List[DetectedDocType]:
    """Best-effort document-type detection from leading text samples, batched
    (one request for many files). Used by the upload picker to pre-fill each
    file's type; doc_type_id '' means undetected so the UI shows '自动检测'."""
    from app.services.extraction_profiles import detect_doc_type_from_sample

    return [
        DetectedDocType(
            name=item.name,
            doc_type_id=detect_doc_type_from_sample(item.sample) or "",
        )
        for item in payload.items
    ]


@router.get("/notebook-templates", response_model=List[NotebookTemplate])
def list_notebook_templates() -> List[NotebookTemplate]:
    return notebook_catalog_repository().list_notebook_templates()


@router.get("/notebooks", response_model=List[NotebookSummary])
def list_notebooks() -> List[NotebookSummary]:
    return notebook_catalog_repository().list_notebooks()


# 注意:静态段路由必须在 /notebooks/{notebook_id} 之前注册,否则 "shared-by-me" 被当作 {notebook_id}。
@router.get("/notebooks/shared-by-me", response_model=List[SharedByMeItem])
def shared_by_me_route(user: UserProfile = Depends(get_current_user)) -> List[SharedByMeItem]:
    return [SharedByMeItem(**it) for it in notebook_sharing_repository().shared_by_me(user.id)]


@router.post("/notebooks", response_model=NotebookSummary)
def create_notebook(payload: NotebookCreate) -> NotebookSummary:
    return notebook_catalog_repository().create_notebook(payload)


@router.get("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_read)])
def get_notebook(notebook_id: str) -> NotebookSummary:
    try:
        return notebook_catalog_repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/analytics", response_model=NotebookAnalytics, dependencies=[Depends(require_notebook_read)])
def notebook_analytics(notebook_id: str) -> NotebookAnalytics:
    try:
        return notebook_catalog_repository().notebook_analytics(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_access)])
def update_notebook(
    notebook_id: str,
    payload: NotebookUpdate,
) -> NotebookSummary:
    try:
        return notebook_catalog_repository().update_notebook(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}", status_code=204, dependencies=[Depends(require_notebook_access)])
def delete_notebook(notebook_id: str) -> None:
    try:
        notebook_catalog_repository().delete_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/sources", response_model=PaginatedSources, dependencies=[Depends(require_notebook_read)])
def list_sources(
    notebook_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str = Query(""),
) -> PaginatedSources:
    return source_repository().list_sources_page(notebook_id, offset=offset, limit=limit, q=q)


@router.post("/notebooks/{notebook_id}/sources/import", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_access)])
def import_sources(
    notebook_id: str,
    payload: SourceImportRequest,
) -> List[SourceSummary]:
    try:
        for file in payload.files:
            _validate_source_file(file.file_name)
        return source_repository().import_sources(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources/url", response_model=AddUrlSourcesResult, dependencies=[Depends(require_notebook_access)])
def add_url_sources(
    notebook_id: str,
    payload: AddUrlSourcesRequest,
) -> AddUrlSourcesResult:
    repo = source_repository()
    try:
        return repo.add_url_sources(
            notebook_id,
            payload.urls,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
        )
    except MinerUCloudNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_access)])
async def upload_sources(
    notebook_id: str,
    files: List[UploadFile] = File(...),
    doc_types: List[str] = Form(default=[]),
) -> List[SourceSummary]:
    try:
        repo = source_repository()
        uploaded_files = []
        for index, file in enumerate(files):
            file_name = file.filename or "source.bin"
            _validate_source_file(file_name)
            content = await file.read()
            _validate_source_file(file_name, len(content))
            # doc_types is aligned with files by position; missing/extra are tolerated.
            doc_type = doc_types[index] if index < len(doc_types) else ""
            uploaded_files.append(
                UploadedSourceFile(
                    file_name=file_name,
                    content_type=file.content_type or "",
                    content=content,
                    doc_type=doc_type,
                )
            )
        return repo.upload_sources(
            notebook_id,
            uploaded_files,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceDetail:
    if not notebook_access_repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.post("/sources/{source_id}/parse", response_model=SourceSummary)
def parse_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceSummary:
    if notebook_access_repository().source_owner(source_id) != user.id:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().parse_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get("/sources/{source_id}/elements", response_model=List[SourceElement])
def source_elements(source_id: str, user: UserProfile = Depends(get_current_user)) -> List[SourceElement]:
    if not notebook_access_repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().source_elements(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    if notebook_access_repository().source_owner(source_id) != user.id:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        source_repository().delete_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


# --- knowhow-tables PR-1 Task 4: notebook image assets (pasted table-cell
# images). Guards mirror the source endpoints above exactly: POST needs
# write access (like upload_sources), GET needs read access (like
# get_source/source_elements) — both 404 on denial, never 403, matching this
# codebase's "don't leak existence" convention. Route bodies stay thin
# (param parsing / guard / orchestration only); validation + disk I/O live
# in AssetService.
@router.post("/notebooks/{notebook_id}/assets", dependencies=[Depends(require_notebook_access)])
async def upload_notebook_asset(notebook_id: str, file: UploadFile = File(...)) -> dict:
    repo = repository()
    content = await file.read()
    try:
        asset = _asset_service().save(
            notebook_id,
            file.filename or "asset",
            file.content_type or "",
            content,
            repo.current_user().id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"id": asset["id"], "url": f"/api/notebooks/{notebook_id}/assets/{asset['id']}"}


@router.get("/notebooks/{notebook_id}/assets/{asset_id}", dependencies=[Depends(require_notebook_read)])
def get_notebook_asset_file(notebook_id: str, asset_id: str) -> FileResponse:
    asset = repository().get_notebook_asset(asset_id)
    if asset is None or asset["notebook_id"] != notebook_id:
        raise HTTPException(status_code=404, detail="Asset not found")
    path = _asset_service().path_for(asset)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    return FileResponse(
        path, media_type=asset["mime"], headers={"Cache-Control": "private, max-age=86400"}
    )


# --- knowhow-tables PR-1 Task 6: table/import API. Guards mirror the asset
# routes above exactly: POST/DELETE need write access (owner-only via
# require_notebook_access, 404 on denial), GET needs read access (owner ∪
# read-only member via require_notebook_read, 404 on denial). Routes stay
# thin (param parsing / guard / dispatch + background-job launch); parsing,
# validation and row/table orchestration live in app.services.knowhow.api;
# GridParseError (a ValueError subclass) and the store's own ValueErrors
# (duplicate/empty column names, concept-count != 1) all flow through the
# same `except ValueError` 400 translation used throughout this file.
@router.post(
    "/notebooks/{notebook_id}/knowhow/import/preview",
    response_model=KnowhowImportPreview,
    dependencies=[Depends(require_notebook_access)],
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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/knowhow/import",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_access)],
)
async def import_knowhow_table(
    notebook_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    columns_json: str = Form(...),
    anchor_index: Optional[int] = Form(None),
    orientation: str = Form("columns"),
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
    repo = repository()
    data = await file.read()
    try:
        table_id = knowhow_api.import_table(
            repo, notebook_id, file.filename or "import", data, title,
            columns_json, anchor_index, orientation,
        )
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
    dependencies=[Depends(require_notebook_access)],
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
    knowhow_api.build_projector(repo).delete_table_projection(detail.get("hidden_source_id"))
    repo.delete_knowhow_table(table_id)
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
    dependencies=[Depends(require_notebook_access)],
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
# exactly (owner-only write via require_notebook_access, 404 on denial).
# Every mutation schedules a debounced full-table reprojection via
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
    dependencies=[Depends(require_notebook_access)],
)
def create_knowhow_table(notebook_id: str, body: KnowhowTableCreate) -> dict:
    """Create-table wizard backend: an EMPTY table (title + column
    definitions only, no grid/rows — mirrors import_table's create step
    minus the file). Does not schedule a reprojection (nothing to project
    yet on a zero-row table); the first row/cell mutation schedules the
    table's first real run."""
    repo = repository()
    try:
        table_id = knowhow_api.create_table(
            repo, notebook_id, body.title,
            [{"name": column.name, "kind": column.kind} for column in body.columns],
            body.anchor_index,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}",
    response_model=KnowhowTableDetail,
    dependencies=[Depends(require_notebook_access)],
)
def patch_knowhow_table(notebook_id: str, table_id: str, patch: KnowhowTablePatch) -> dict:
    """title/description/anchor_column_id are all optional; anchor_column_id
    additionally distinguishes "omitted" (leave alone) from "explicit null"
    (clear the row-title designation) via ``model_fields_set`` — omission and
    None both parse to the same Python value, so only the set of keys the
    client actually sent tells them apart. A patch that only renames the
    table still schedules reprojection: the table title feeds every row's
    chunk section_path label (design doc §④), so renaming it is itself a
    content change from the projector's point of view."""
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    fields_set = patch.model_fields_set
    try:
        if "anchor_column_id" in fields_set:
            repo.set_knowhow_anchor_column(table_id, patch.anchor_column_id)
        title = patch.title if "title" in fields_set else None
        description = patch.description if "description" in fields_set else None
        if title is not None or description is not None:
            repo.update_knowhow_table_meta(table_id, title=title, description=description)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return knowhow_api.get_table_in_notebook(repo, notebook_id, table_id)


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns",
    response_model=KnowhowColumn,
    dependencies=[Depends(require_notebook_access)],
)
def add_knowhow_column(notebook_id: str, table_id: str, body: KnowhowColumnCreate) -> dict:
    repo = repository()
    _require_table(repo, notebook_id, table_id)
    try:
        column_id = repo.add_knowhow_column(table_id, body.name, body.kind, body.position)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    table = repo.get_knowhow_table(table_id)
    column = next(c for c in table["columns"] if c["id"] == column_id)
    return knowhow_api.to_wire_column(column)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns/{column_id}",
    response_model=KnowhowColumn,
    dependencies=[Depends(require_notebook_access)],
)
def patch_knowhow_column(
    notebook_id: str, table_id: str, column_id: str, body: KnowhowColumnPatch
) -> dict:
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    _require_column_in_table(table, column_id)
    fields_set = body.model_fields_set
    try:
        if "name" in fields_set and body.name is not None:
            repo.rename_knowhow_column(column_id, body.name)
        if "kind" in fields_set and body.kind is not None:
            repo.set_knowhow_column_kind(column_id, body.kind)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    column = next(c for c in updated["columns"] if c["id"] == column_id)
    return knowhow_api.to_wire_column(column)


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}/columns/{column_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_access)],
)
def delete_knowhow_column(notebook_id: str, table_id: str, column_id: str) -> None:
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    _require_column_in_table(table, column_id)
    repo.delete_knowhow_column(column_id)
    knowhow_api.get_scheduler(repo).schedule(table_id)


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows",
    response_model=KnowhowRow,
    dependencies=[Depends(require_notebook_access)],
)
def add_knowhow_row(notebook_id: str, table_id: str, body: KnowhowRowCreate) -> dict:
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    valid_column_ids = {column["id"] for column in table["columns"]}
    if set(body.cells) - valid_column_ids:
        raise user_error(400, "格子对应的列不属于本表")
    row_id = repo.add_knowhow_row(table_id, body.cells, body.position)
    knowhow_api.get_scheduler(repo).schedule(table_id)
    updated = repo.get_knowhow_table(table_id)
    return next(r for r in updated["rows"] if r["id"] == row_id)


@router.delete(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}",
    status_code=204,
    dependencies=[Depends(require_notebook_access)],
)
def delete_knowhow_row(notebook_id: str, table_id: str, row_id: str) -> None:
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    _require_row_in_table(table, row_id)
    repo.delete_knowhow_row(row_id)
    knowhow_api.get_scheduler(repo).schedule(table_id)


@router.patch(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}",
    response_model=KnowhowCellPatchResult,
    dependencies=[Depends(require_notebook_access)],
)
def patch_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str, body: KnowhowCellPatch
) -> dict:
    """Returns {row_id,column_id,content_md,projection_status} so the caller
    can refresh just this row's sync badge without re-fetching the whole
    table. row_id/column_id validity is checked twice, deliberately: the
    table-scoped membership check (belongs to THIS table_id, not merely SOME
    table) and the store's own validate_cell_target (same-table pairing) —
    both funnel into the same 400 "格子定位不合法" so a client sees one
    consistent error regardless of which check actually caught it."""
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
                row_id, column_id, body.content_md, require_assets=added_assets
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
    dependencies=[Depends(require_notebook_access)],
)
def patch_knowhow_cells_batch(
    notebook_id: str, table_id: str, body: KnowhowCellsBatchPatch
) -> list:
    """Returns one {row_id,column_id,content_md,projection_status} entry per
    row in body.row_ids, in the same order — same shape patch_knowhow_cell
    returns for a single row, just as a list."""
    repo = repository()
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
                row_ids, body.column_id, body.content_md, require_assets=added_assets
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
    dependencies=[Depends(require_notebook_access)],
)
async def append_knowhow_rows(
    notebook_id: str,
    table_id: str,
    file: UploadFile = File(...),
    mode: str = Form("preview"),
) -> dict:
    """Excel append import (design doc §② 路B): matches the uploaded file's
    header BY COLUMN NAME against the table's existing columns (never by
    position — see knowhow_api._align_rows_to_table_columns). mode=preview
    parses+aligns without writing anything, reporting what commit WOULD do
    (rows_preview/total_rows/unmatched_columns/duplicate_titles); mode=commit
    performs the identical alignment, actually appends the rows, and
    schedules a debounced reprojection exactly like every other mutating
    knowhow endpoint."""
    if mode not in ("preview", "commit"):
        # 分类:这不是用户文案。mode 是 Form 参数,UI 永远不会发错值,只有 API/MCP
        # 客户端会踩;「mode 必须是 preview 或 commit」对真人毫无意义。故降级成普通
        # HTTPException(英文 detail = API 契约),真人看到的是 400 的通用中文文案。
        raise HTTPException(status_code=400, detail="mode must be 'preview' or 'commit'")
    repo = repository()
    table = _require_table(repo, notebook_id, table_id)
    data = await file.read()
    try:
        if mode == "preview":
            return knowhow_api.preview_append(table, file.filename or "append", data)
        added = knowhow_api.commit_append(repo, table_id, table, file.filename or "append", data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    knowhow_api.get_scheduler(repo).schedule(table_id)
    return {"added": added}


# --- knowhow-tables PR-2+3 Task 8: LLM cell rewrite (design doc §③, explicit
# trigger only, suggestion-only). Write-gated like every other knowhow
# mutation (require_notebook_access) even though it never writes the store
# itself — this is an authoring affordance (the cell-editor's "优化表达"
# button), not a read. Never schedules a reprojection either: the user's own
# explicit accept goes through the EXISTING PATCH cell endpoint, which is
# what writes + schedules. ValueError/ModelNotConfiguredError -> 400 (both
# validation-shaped, friendly Chinese message as-is); KnowhowOptimizeUnavailable
# (the LLM call itself failed after being reached, already logged via
# note_model_error inside optimize_cell) -> 502.


@router.post(
    "/notebooks/{notebook_id}/knowhow/{table_id}/rows/{row_id}/cells/{column_id}/optimize",
    response_model=KnowhowCellOptimizeResult,
    dependencies=[Depends(require_notebook_access)],
)
def optimize_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str
) -> dict:
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
    try:
        suggestion_md = knowhow_api.optimize_cell(repo, content_md, column["name"], column["kind"])
    except ModelNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except knowhow_api.KnowhowOptimizeUnavailable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"suggestion_md": suggestion_md}


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
    dependencies=[Depends(require_notebook_access)],
)
def reformat_knowhow_cell(
    notebook_id: str, table_id: str, row_id: str, column_id: str
) -> dict:
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
    result = knowhow_api.reformat_cell(repo, content_md, column["name"], column["kind"])
    # Concurrency P1 (fix a): report the EXACT content_md we just read and fed to
    # reformat_cell so the batch client can tell "this candidate derives from the
    # cell I snapshotted" from "the live cell was edited under me". content_md is
    # the same string reformat_cell consumed (it does `raw = content_md or ""`,
    # and content_md is never None here — `.get(column_id, "")`), so source_md ==
    # the snapshot iff nobody edited the cell between modal-open and now.
    result["source_md"] = content_md
    return result


@router.get(
    "/notebooks/{notebook_id}/knowledge-types",
    response_model=List[KnowledgeTypeCount],
    dependencies=[Depends(require_notebook_read)],
)
def knowledge_types(notebook_id: str) -> List[KnowledgeTypeCount]:
    try:
        return repository().knowledge_types(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/knowledge", response_model=PaginatedKnowledge, dependencies=[Depends(require_notebook_read)])
def list_knowledge(
    notebook_id: str,
    type: str = Query(...),
    status: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedKnowledge:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().list_knowledge(notebook_id, object_type, status=status, offset=offset, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Editable extraction-schema registry ---------------------------------
@router.get("/object-schemas", response_model=List[ObjectSchemaModel])
def list_object_schemas() -> List[ObjectSchemaModel]:
    return repository().list_object_schemas()


@router.post("/object-schemas", response_model=ObjectSchemaModel)
def create_object_schema(payload: ObjectSchemaCreate, user: UserProfile = Depends(get_current_user)) -> ObjectSchemaModel:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return repository().create_object_schema(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/object-schemas/{object_type}", response_model=ObjectSchemaModel)
def update_object_schema(
    object_type: str, payload: ObjectSchemaUpdate, user: UserProfile = Depends(get_current_user)
) -> ObjectSchemaModel:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return repository().update_object_schema(object_type, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/object-schemas/{object_type}")
def delete_object_schema(object_type: str, user: UserProfile = Depends(get_current_user)):
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        repository().delete_object_schema(object_type)
        return {"status": "deleted", "object_type": object_type}
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/schema-proposals",
    response_model=List[ObjectSchemaModel],
    dependencies=[Depends(require_notebook_access)],
)
def propose_schemas(notebook_id: str) -> List[ObjectSchemaModel]:
    try:
        return repository().propose_schemas(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}/knowledge/{knowledge_id}", dependencies=[Depends(require_notebook_access)])
def update_knowledge(notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate):
    try:
        return repository().update_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_KNOWLEDGE_TYPE_MAP = {
    "rules": "rule",
    "methods": "method",
    "risks": "risk",
    "cases": "case",
    "checklist": "checklist",
    "glossary": "glossary",
}


@router.get("/notebooks/{notebook_id}/duplicates", response_model=List[DuplicateGroup], dependencies=[Depends(require_notebook_read)])
def find_duplicates(notebook_id: str, type: str = Query("rules")) -> List[DuplicateGroup]:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().find_duplicates(notebook_id, object_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")



@router.get("/notebooks/{notebook_id}/graph", response_model=KnowledgeGraph, dependencies=[Depends(require_notebook_read)])
def knowledge_graph(notebook_id: str) -> KnowledgeGraph:
    try:
        return repository().knowledge_graph(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except KnowledgeGraphTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc))


@router.post("/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge", dependencies=[Depends(require_notebook_access)])
def merge_knowledge(notebook_id: str, knowledge_id: str, payload: MergeRequest):
    try:
        return repository().merge_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/notebooks/{notebook_id}/search", response_model=NotebookSearchResponse, dependencies=[Depends(require_notebook_read)])
def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    try:
        return notebook_catalog_repository().search_notebook(notebook_id, q)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get(
    "/notebooks/{notebook_id}/kg/search",
    response_model=KgSearchResponse,
    dependencies=[Depends(require_notebook_read)],  # 只读 KG 搜索,成员可(与 /search、/graph 一致)
)
def kg_search(
    notebook_id: str,
    q: str = Query(...),
    k: int = Query(30, ge=1, le=200),
) -> KgSearchResponse:
    """词法(FTS5)∪语义(ANN)搜索 KG 节点,按 score 降序返回 k 条。"""
    try:
        hits = repository().kg_search(notebook_id, q, k)
        return KgSearchResponse(query=q, hits=hits)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse, dependencies=[Depends(require_notebook_read)])
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    try:
        return repository().ask(notebook_id, payload)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/ask-modes")
def ask_modes() -> list[dict[str, Any]]:
    """User-facing ask modes (single source: app/services/ask_modes.py).
    Copy/labels live in the frontend; this exposes ids + behavioural flags."""
    return [
        {"id": m.id, "group": m.group,
         "requires_kg": m.requires_kg, "streaming": m.streaming}
        for m in ASK_MODES.values() if m.user_facing
    ]


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


async def _stream_ask_events(
    repo: AskStreamPort,
    notebook_id: str,
    payload: AskRequest,
    spec,
    request: Request,
):
    # Task 23: 执行编排(begin→register→started→合成 start→copy_context worker→
    # trace 持久化 fail-open→finish→unregister→空会话清理→终态事件→哨兵)整体在
    # runtime-owned AskExecutionCoordinator;本函数保留冻结签名,只剩启动编排、
    # 交付队列消费与断连轮询。Task 24: 执行体 = runtime-owned AskService(三模式
    # 注册表派发在服务内),不再是 facade runner 回调。
    events = repo.start_ask_stream(
        notebook_id, payload, spec,
        user_id=repo.current_user().id,
    )
    # 客户端断连只停止本次流(break),**不** set cancel_event —— worker 脱离连接
    # 跑到完、答案照存。唯一取消入口是 POST …/ask/jobs/{job_id}/cancel。
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(events.get, True, 0.1)
            except queue.Empty:
                continue
        if event is None:
            break
        yield _ndjson_line(event)


@router.post("/notebooks/{notebook_id}/ask/stream", dependencies=[Depends(require_notebook_read)])
async def ask_stream(notebook_id: str, request: Request, payload: AskRequest) -> StreamingResponse:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        spec = resolve_mode(payload.mode)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    return StreamingResponse(
        _stream_ask_events(repo, notebook_id, payload, spec, request),
        media_type="application/x-ndjson",
    )


@router.post("/notebooks/{notebook_id}/ask/jobs/{job_id}/cancel",
             dependencies=[Depends(require_notebook_read)])
def cancel_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        return repo.cancel_ask_job(job_id, repo.current_user().id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")


@router.get("/notebooks/{notebook_id}/ask/jobs/{job_id}",
            dependencies=[Depends(require_notebook_read)])
def get_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        detail = repo.ask_job_detail(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")
    if detail["created_by"] != repo.current_user().id:
        raise HTTPException(status_code=404, detail="ask job not found")
    return detail


@router.get("/notebooks/{notebook_id}/conversations", response_model=List[ConversationSummary], dependencies=[Depends(require_notebook_read)])
def list_conversations(notebook_id: str) -> List[ConversationSummary]:
    try:
        return repository().list_conversations(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)) -> ConversationDetail:
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return repository().get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().rename_conversation(conversation_id, payload.title)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().delete_conversation(conversation_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/notebooks/{notebook_id}/conversations", dependencies=[Depends(require_notebook_read)])  # 仓库层按 created_by scope,成员删自己的旧会话
def bulk_delete_conversations(notebook_id: str, older_than_days: int = Query(..., ge=1)):
    try:
        deleted = repository().bulk_delete_conversations(notebook_id, older_than_days)
        return {"deleted": deleted}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# ---------------------------------------------------------------------------
# Edge trust & curation (Track E)
# ---------------------------------------------------------------------------

@router.get("/notebooks/{notebook_id}/edge-review-queue", response_model=List[EdgeReviewItem], dependencies=[Depends(require_notebook_read)])
def edge_review_queue(notebook_id: str, limit: int = 100) -> List[EdgeReviewItem]:
    """Return edges ranked by review priority (high centrality × low trust) desc.
    Excludes already-rejected edges.
    """
    try:
        return repository().review_queue(notebook_id, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/relations/{rel_id}/review", status_code=200, dependencies=[Depends(require_notebook_access)])
def review_relation(notebook_id: str, rel_id: str,
                    payload: EdgeReviewRequest) -> dict:
    """Mark an edge as 'verified', 'rejected', or 'pending'.
    Rejected edges are excluded from all future graph-reasoning traversals.
    """
    try:
        repository().set_edge_review(notebook_id, rel_id, payload.status)
        return {"rel_id": rel_id, "review_status": payload.status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/notebooks/{notebook_id}/tier", response_model=NotebookSummary, dependencies=[Depends(require_notebook_access)])
def set_notebook_tier(notebook_id: str, payload: SetTierRequest, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """Set a notebook's federation tier: 'base'(发布为公共知识库,可被任何笔记本
    挂载为参考库) 或 'personal'(撤回发布)。**不再全局唯一** —— 每个领域可以有自己
    的公共知识库。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可设为公共知识库")
    tier = payload.tier.strip().lower()
    if tier not in {"base", "personal"}:
        raise HTTPException(status_code=400, detail="tier must be 'base' or 'personal'")
    try:
        catalog = notebook_catalog_repository()
        if tier == "base":
            catalog.mark_notebook_base(notebook_id)
        else:
            catalog.set_notebook_personal(notebook_id)
        return catalog.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_access)])
def list_notebook_bases_route(notebook_id: str) -> List[MountedBase]:
    """本 notebook 挂载的参考库。含 active=False 的失效边(被挂库易主 / 公共库被
    降级),前端置灰展示——边保留是为了对方恢复后自动生效。"""
    return [MountedBase(**edge) for edge in repository().list_notebook_bases(notebook_id)]


@router.put("/notebooks/{notebook_id}/bases", response_model=List[MountedBase],
            dependencies=[Depends(require_notebook_access)])
def set_notebook_bases_route(
    notebook_id: str, payload: SetBasesRequest,
    user: UserProfile = Depends(get_current_user),
) -> List[MountedBase]:
    """全量替换挂载集合。只接受本 notebook 的可挂候选(公共知识库 ∪ 同 owner 的库)
    ∪ 当前已挂载的 id(含失效边),其余一律 400 —— 挂载边不是授权凭证,写入侧也要挡。
    写权限本身由 require_notebook_access(owner-only,404 on denial)在依赖层挡;
    这里只做候选集校验,不重复手工判断写权限。

    并入"当前已挂载的 id"是刻意的:mountable_notebooks 与失效边的判定谓词
    (MOUNT_VALID_EXPR)是同一个表达式,所以一条失效边(被挂库降级/易主后)永远不会
    出现在 mountable 里。前端编辑表单原样重新提交"保留这条失效边不变"的挂载集合
    (不做任何前端过滤 —— 那会静默删掉设计上刻意保留、等对方重新发布后自动恢复的边)
    时,若只拿 mountable 当白名单会把这个合法保留动作也 400 掉,导致表单永久存不了。
    并入的是"已挂载"而非"任意 id",所以仍然拒绝新挂一个从未属于本笔记本的无效 id。"""
    repo = repository()
    allowed = {n["id"] for n in repo.mountable_notebooks(notebook_id)}
    allowed |= {edge["id"] for edge in repo.list_notebook_bases(notebook_id)}
    wanted = [nb_id for nb_id in dict.fromkeys(payload.base_notebook_ids) if nb_id]
    if any(nb_id not in allowed for nb_id in wanted):
        raise user_error(400, "选择里包含不能作为参考库的知识库")
    repo.replace_notebook_bases(notebook_id, wanted, user.id)
    return [MountedBase(**edge) for edge in repo.list_notebook_bases(notebook_id)]


@router.get("/notebooks/{notebook_id}/mountable", response_model=List[NotebookRef],
            dependencies=[Depends(require_notebook_access)])
def mountable_notebooks_route(notebook_id: str) -> List[NotebookRef]:
    """可挂候选 = 所有公共知识库 ∪ 与本库同 owner 的库。

    刻意挂在 {notebook_id} 下而非 /notebooks/mountable —— 后者会与既有的
    /notebooks/{notebook_id} 争路由匹配(FastAPI 按声明序,静态段必须先注册)。"""
    return [NotebookRef(**n) for n in repository().mountable_notebooks(notebook_id)]


@router.get("/notebooks/{notebook_id}/mounted-by-count", response_model=MountedByCount,
            dependencies=[Depends(require_notebook_access)])
def mounted_by_count_route(notebook_id: str) -> MountedByCount:
    """删除确认弹窗专用(spec §6):有多少笔记本正在把本 notebook 挂为参考库——
    ON DELETE CASCADE 会连同这些边一起清空且不可撤销,用户点删除前必须看到影响面。
    与 DELETE 端点用同一个 owner-only 依赖(不新开一套权限判断)。"""
    return MountedByCount(count=repository().mounted_by_count(notebook_id))


@router.post("/notebooks/{notebook_id}/share", response_model=ShareResponse,
             dependencies=[Depends(require_notebook_access)])
def share_notebook_route(notebook_id: str) -> ShareResponse:
    try:
        return ShareResponse(**notebook_sharing_repository().share_notebook(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}/share", status_code=204,
               dependencies=[Depends(require_notebook_access)])
def unshare_notebook_route(notebook_id: str) -> None:
    try:
        notebook_sharing_repository().unshare_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/shared/{token}", response_model=SharedPreview)
def shared_preview_route(token: str, user: UserProfile = Depends(get_current_user)) -> SharedPreview:
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    return SharedPreview(**sharing.shared_preview(nb_id))


@router.post("/shared/{token}/copy", response_model=NotebookSummary)
def copy_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if not sharing.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=409, detail="notebook too large to copy")
    return sharing.copy_notebook(nb_id, new_owner_id=user.id)


@router.post("/shared/{token}/join", response_model=NotebookSummary)
def join_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """大库只读加入:凭 share_token 成为只读成员。小库应走 copy 而非 join。"""
    sharing = notebook_sharing_repository()
    nb_id = sharing.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if sharing.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=400, detail="small notebook — use copy, not join")
    return sharing.join_shared(nb_id, user.id)


@router.delete("/notebooks/{notebook_id}/membership", status_code=204)
def leave_notebook_route(notebook_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    """退出只读共享:只删自己的成员记录(幂等,不影响他人)。"""
    notebook_sharing_repository().leave_notebook(notebook_id, user.id)


@router.post("/notebooks/{notebook_id}/kg/build", dependencies=[Depends(require_notebook_access)])
def build_kg(notebook_id: str) -> dict:
    """按需触发该 notebook 的 KG 建图(后台线程,幂等)。
    已有 knowledge_objects 的 source 会跳过。需 LLM 已配置,否则 409。"""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not getattr(repo.kg_llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        job = repo.prepare_notebook_kg_job(notebook_id, "incremental")
    except KgBuildAlreadyRunning:
        raise user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    try:
        background_jobs.submit(
            repo.execute_notebook_kg_job,
            notebook_id,
            job["id"],
            "incremental",
            name=f"buildkg-{notebook_id}",
            notify_pending=True,
        )
    except Exception:
        repo.fail_notebook_kg_job_submission(job["id"])
        raise
    return {
        "status": "building",
        "notebook_id": notebook_id,
        "job_id": job["id"],
    }


@router.post("/notebooks/{notebook_id}/kg/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_kg(notebook_id: str) -> dict:
    """Full re-extract: clears all KG artefacts then re-extracts ALL sources
    (background thread). Requires LLM configured (409 if not), 404 if notebook
    missing — same guards as build_kg."""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not getattr(repo.kg_llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        job = repo.prepare_notebook_kg_job(notebook_id, "rebuild")
    except KgBuildAlreadyRunning:
        raise user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    try:
        background_jobs.submit(
            repo.execute_notebook_kg_job,
            notebook_id,
            job["id"],
            "rebuild",
            name=f"rebuildkg-{notebook_id}",
            notify_pending=True,
        )
    except Exception:
        repo.fail_notebook_kg_job_submission(job["id"])
        raise
    return {
        "status": "rebuilding",
        "notebook_id": notebook_id,
        "job_id": job["id"],
    }


@router.post("/notebooks/{notebook_id}/kg/relink", dependencies=[Depends(require_notebook_access)])
def relink_kg(notebook_id: str) -> dict:
    """Deterministic reconnection of isolated KG nodes (synchronous, no LLM).
    Returns {"isolated_before", "edges_added", "isolated_after"}."""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return repo.relink_notebook_kg(notebook_id)


# --- 深度报告(异步后台 job,轮询取状态) -------------------------------


def _report_llm_ready(repo) -> bool:
    return bool(getattr(repo.reasoning_llm_client, "configured", False))


def _launch_plan_job(repo, notebook_id: str, rid: str, question: str, history: str,
                     auto_generate: bool = False) -> None:
    """阶段1(规划)后台 job:跑 plan_outline → outline_ready;auto_generate 时
    在同一 worker 内接生成(一键直出)。depth 从 report 行读(创建时已落库)。
    Task 25:helper 名保留(测试 monkeypatch 位),编排移交运行时协调器——
    注册取消(submit 前)→ background_jobs.submit(copy_context)→ 收尾注销。"""
    repo.report_execution.start_plan(
        notebook_id, rid, question, history, auto_generate,
        user_id=repo.current_user().id)


def _launch_generate_job(repo, notebook_id: str, rid: str, question: str,
                         depth: int = 2) -> None:
    """阶段2(生成)后台 job:用已确认的 outline 跑 generate → done。"""
    repo.report_execution.start_generate(
        notebook_id, rid, question, depth,
        user_id=repo.current_user().id)


@router.post("/notebooks/{notebook_id}/reports",
             dependencies=[Depends(require_notebook_write)])
def create_report(notebook_id: str, payload: ReportCreate) -> dict:
    repo = repository()
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="question required")
    if not _report_llm_ready(repo):
        raise HTTPException(status_code=409, detail="LLM not configured")
    depth = max(1, min(16, int(payload.depth)))
    try:
        rid = repo.create_report(notebook_id, payload.question.strip(), depth=depth)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    _launch_plan_job(repo, notebook_id, rid, payload.question.strip(), payload.history,
                     payload.auto_generate)
    return {"report_id": rid, "status": "pending"}


@router.get("/notebooks/{notebook_id}/reports",
            dependencies=[Depends(require_notebook_read)])
def list_reports(notebook_id: str) -> List[ReportSummary]:
    # repo 行含 notebook_id/updated_at 等多余键 → 按模型字段过滤(仓库无 extra=ignore 风格)
    return [ReportSummary(**{k: v for k, v in r.items() if k in ReportSummary.model_fields})
            for r in repository().list_reports(notebook_id)]


@router.post("/notebooks/{notebook_id}/reports/export",
             dependencies=[Depends(require_notebook_read)])
def export_reports_endpoint(notebook_id: str, payload: ReportExportRequest) -> StreamingResponse:
    # owner∪成员可下(require_notebook_read);只导出该 notebook 下 status='done' 且
    # content_md 非空的报告,非 done/空/跨 notebook 的 id 静默跳过(repo 层已过滤)。
    rows = repository().export_reports(notebook_id, payload.report_ids)
    if not rows:                                 # 空 report_ids 或全部无效
        raise HTTPException(status_code=422, detail="no exportable reports")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, md in rows:
            z.writestr(name, md)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="reports.zip"'})


@router.get("/notebooks/{notebook_id}/reports/{report_id}",
            dependencies=[Depends(require_notebook_read)])
def get_report(notebook_id: str, report_id: str) -> ReportDetail:
    try:
        r = repository().get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    return ReportDetail(**{k: v for k, v in r.items() if k in ReportDetail.model_fields})


@router.patch("/notebooks/{notebook_id}/reports/{report_id}/outline",
              dependencies=[Depends(require_notebook_write)])
def update_report_outline(notebook_id: str, report_id: str, payload: ReportOutlineUpdate) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="outline editable only when outline_ready")
    secs = [s for s in payload.sections
            if str(s.get("title", "")).strip() and (s.get("sub_queries") or [])]
    if not secs:
        raise HTTPException(status_code=422, detail="at least one valid section required")
    repo.update_report(notebook_id, report_id, outline=secs)
    return {"status": "ok", "sections": len(secs)}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/generate",
             dependencies=[Depends(require_notebook_write)])
def generate_report(notebook_id: str, report_id: str, payload: ReportGenerateRequest) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="generate only from outline_ready")
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    _launch_generate_job(repo, notebook_id, report_id, cur["question"], depth)
    return {"status": "generating"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/cancel",
             dependencies=[Depends(require_notebook_write)])
def cancel_report_endpoint(notebook_id: str, report_id: str) -> dict:
    from app.services.report_engine import cancel_report as _cancel
    live = _cancel(report_id)
    if not live:                               # 线程已结束/不存在:直接落库标记
        try:
            repository().update_report(notebook_id, report_id, status="cancelled",
                                       progress="已取消")
        except Exception:
            raise HTTPException(status_code=404, detail="Report not found")
    return {"status": "cancelling" if live else "cancelled"}


@router.delete("/notebooks/{notebook_id}/reports/{report_id}",
               dependencies=[Depends(require_notebook_write)])
def delete_report(notebook_id: str, report_id: str) -> dict:
    repository().delete_report(notebook_id, report_id)
    return {"status": "deleted"}


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(answer_id: str, payload: FeedbackRequest, user: UserProfile = Depends(get_current_user)) -> FeedbackResponse:
    if not notebook_access_repository().user_can_read_answer(answer_id, user.id):  # owner ∪ 成员(spec §3.3)
        raise HTTPException(status_code=404, detail="Answer not found")
    try:
        return repository().submit_feedback(answer_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Answer not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Unified Knowledge Graph endpoints
# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/unified-kg/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_unified_kg(notebook_id: str) -> dict:
    try:
        # 刷新图谱:走版本门控(force=False)——输入未变则跳过重聚类,只增量重建社区
        # (纯图/无 LLM/秒级);有新内容才重聚。强制全量重聚(如改了聚类设置)用
        # scripts/recluster_kg.py。这兑现「判断:只需重建社区就跳过其他动作」。
        clusters = repository().rebuild_unified_kg(notebook_id, force=False)
        return {"clusters": clusters}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg/status", dependencies=[Depends(require_notebook_read)])
def unified_kg_status(notebook_id: str) -> UnifiedKgStatus:
    try:
        return UnifiedKgStatus(**repository().unified_kg_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/scale-index/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_scale_index(notebook_id: str, body: RebuildScaleIndexRequest = RebuildScaleIndexRequest()) -> dict:
    """在线重建 scale 检索索引(base-tier / 已建过)。when=now 立即后台/idle 低峰调度;
    mode=auto(fold/full 自选)|fold|full。400 若参数非法,409 若不合格,404 若缺。"""
    if body.when not in ("now", "idle"):
        raise HTTPException(status_code=400, detail="when must be one of: now, idle")
    if body.mode not in ("auto", "fold", "full"):
        raise HTTPException(status_code=400, detail="mode must be one of: auto, fold, full")
    try:
        return repository().trigger_scale_index_rebuild(notebook_id, when=body.when, mode=body.mode)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/notebooks/{notebook_id}/scale-index/cancel", dependencies=[Depends(require_notebook_access)])
def cancel_scale_index(notebook_id: str) -> dict:
    """取消检索索引:排队中→出队;构建中→拒绝(不可打断)。"""
    try:
        return repository().cancel_scale_index(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/scale-index/status", dependencies=[Depends(require_notebook_access)])
def scale_index_status(notebook_id: str) -> ScaleIndexStatus:
    try:
        return ScaleIndexStatus(**repository().scale_index_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/index-status", dependencies=[Depends(require_notebook_read)])
def index_status(notebook_id: str) -> dict:
    """三系统构建状态聚合(kg/unified_kg/scale_index)。纯读:与 /analytics、
    /unified-kg/status 一致用 read 守卫——只读成员也要能看面板里的状态位,
    写操作(build/rebuild/cancel)各自仍是 require_notebook_access 且前端按 !isReader 隐藏按钮。"""
    try:
        return repository().index_status(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg", dependencies=[Depends(require_notebook_read)])
def get_unified_kg(
    notebook_id: str,
    level: str = Query("concept"),
    limit: Optional[int] = Query(None, ge=1, description="只返回连接度最高的前 N 个节点(核心子图);省略=全量"),
) -> dict:
    try:
        return repository().unified_graph(notebook_id, level=level, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg/pending-merges", dependencies=[Depends(require_notebook_read)])
def get_pending_merges(notebook_id: str) -> list:
    try:
        return repository().pending_merges(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/concepts/{canonical_id}/detail", dependencies=[Depends(require_notebook_read)])
def get_concept_detail(notebook_id: str, canonical_id: str) -> dict:
    try:
        return repository().concept_detail(notebook_id, canonical_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Concept not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/context", dependencies=[Depends(require_notebook_read)])
def object_context(notebook_id: str, object_id: str):
    try:
        return repository().node_context(notebook_id, object_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/neighbors", dependencies=[Depends(require_notebook_read)])
def object_neighbors(
    notebook_id: str,
    object_id: str,
    cap: int = Query(50, ge=1, description="最多返回的 1-hop 邻居数"),
) -> dict:
    """折叠图中某节点的 1-hop 邻域(有界);与 unified-kg 同形(nodes/edges)。"""
    try:
        return repository().kg_neighbors(notebook_id, object_id, cap=cap)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/confirm", dependencies=[Depends(require_notebook_access)])
def confirm_merge(notebook_id: str, candidate_id: str) -> dict:
    try:
        repository().confirm_merge(notebook_id, candidate_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Merge candidate not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/reject", dependencies=[Depends(require_notebook_access)])
def reject_merge(notebook_id: str, candidate_id: str) -> dict:
    try:
        repository().reject_merge(notebook_id, candidate_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Merge candidate not found")


# ---------------------------------------------------------------------------
# KG conflict resolve/review endpoints (Task T6)
# Mirrors the kg/build + concept-merge review patterns above.
# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/kg/conflicts/resolve", dependencies=[Depends(require_notebook_access)])
def resolve_conflicts(notebook_id: str) -> dict:
    """Trigger background conflict resolution for a notebook's KG.

    Mirrors kg/build: 409 if LLM not configured, 404 if notebook missing,
    otherwise starts a daemon thread and returns immediately.
    """
    repo = repository()
    if not getattr(repo.llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    background_jobs.submit(repo.resolve_notebook_conflicts, notebook_id,
                           name=f"conflictresolve-{notebook_id}", notify_pending=True)
    return {"status": "resolving", "notebook_id": notebook_id}


@router.get("/notebooks/{notebook_id}/kg/conflicts/pending", dependencies=[Depends(require_notebook_read)])
def get_pending_conflicts(notebook_id: str) -> list:
    """Return all pending conflict candidates for a notebook."""
    try:
        return repository().pending_conflicts(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/confirm", dependencies=[Depends(require_notebook_access)])
def confirm_conflict(notebook_id: str, candidate_id: str) -> dict:
    """Apply a pending conflict candidate and mark it as 'applied'."""
    try:
        return repository().confirm_conflict(notebook_id, candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conflict candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/reject", dependencies=[Depends(require_notebook_access)])
def reject_conflict(notebook_id: str, candidate_id: str) -> dict:
    """Reject a pending conflict candidate (no KG mutation)."""
    try:
        repository().reject_conflict(notebook_id, candidate_id)
        return {"status": "rejected", "candidate_id": candidate_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conflict candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/kg/concept-whitelist", response_model=List[ConceptWhitelistEntry])
def list_concept_whitelist() -> List[ConceptWhitelistEntry]:
    return [ConceptWhitelistEntry(**e) for e in repository().concept_whitelist_list()]


@router.post("/kg/concept-whitelist", response_model=ConceptWhitelistEntry)
def add_concept_whitelist(payload: ConceptWhitelistAdd, user: UserProfile = Depends(get_current_user)) -> ConceptWhitelistEntry:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return ConceptWhitelistEntry(**repository().concept_whitelist_add(payload.term, payload.note))
    except ValueError:
        raise HTTPException(status_code=400, detail="term must be non-empty")


@router.delete("/kg/concept-whitelist/{term}", status_code=204)
def delete_concept_whitelist(term: str, user: UserProfile = Depends(get_current_user)) -> None:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
    repository().concept_whitelist_remove(term)


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review", dependencies=[Depends(require_notebook_access)])
def review_unified_kg_merges(notebook_id: str, payload: MergeReviewRequest) -> MergeReviewSummary:
    try:
        return MergeReviewSummary(**repository().review_pending_merges(
            notebook_id,
            limit=payload.limit,
            confirm_threshold=payload.confirm_threshold,
            separate_threshold=payload.separate_threshold,
        ))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review-all", dependencies=[Depends(require_notebook_access)])
def review_all_unified_kg_merges(notebook_id: str) -> dict:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if repo.merge_review_job_status(notebook_id)["status"] == "running":
        return {"status": "running"}
    background_jobs.submit(repo.run_merge_review_job, notebook_id,
                           name=f"mergereview-{notebook_id}", notify_pending=True)
    return {"status": "started"}


@router.get("/notebooks/{notebook_id}/unified-kg/merges/review-job", dependencies=[Depends(require_notebook_access)])
def merge_review_job(notebook_id: str) -> MergeReviewJob:
    try:
        return MergeReviewJob(**repository().merge_review_job_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Governance: promotion queue (Track F) ------------------------------


@router.post(
    "/notebooks/{notebook_id}/knowledge/{knowledge_id}/promote",
    response_model=PromotionCandidate,
    status_code=201,
    dependencies=[Depends(require_notebook_access)],
)
def propose_promotion(
    notebook_id: str, knowledge_id: str, payload: PromoteRequest = PromoteRequest()
) -> PromotionCandidate:
    try:
        return PromotionCandidate(**repository().propose_promotion(
            notebook_id, knowledge_id, target_base_id=payload.target_base_id
        ))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook or knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/promotion-queue", response_model=List[PromotionCandidate])
def list_promotion_queue(status: str = Query(None), user: UserProfile = Depends(get_current_user)) -> List[PromotionCandidate]:
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理内容审核队列")
    return [
        PromotionCandidate(**c)
        for c in repository().list_promotion_queue(status_filter=status)
    ]


@router.post(
    "/promotion-queue/{candidate_id}/approve",
    response_model=PromotionApproveResult,
)
def approve_promotion(candidate_id: str, user: UserProfile = Depends(get_current_user)) -> PromotionApproveResult:
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理内容审核队列")
    try:
        return PromotionApproveResult(
            **repository().approve_promotion_as_reviewer(candidate_id, user.id)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Promotion candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/promotion-queue/{candidate_id}/reject",
    response_model=PromotionCandidate,
)
def reject_promotion(candidate_id: str, payload: PromotionRejectRequest, user: UserProfile = Depends(get_current_user)) -> PromotionCandidate:
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理内容审核队列")
    try:
        return PromotionCandidate(
            **repository().reject_promotion_as_reviewer(
                candidate_id, payload.reason, user.id
            )
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Promotion candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/admin/users", response_model=List[AdminUserUsage])
async def list_admin_users(user: UserProfile = Depends(get_current_user)) -> List[AdminUserUsage]:
    """管理员用户使用总览:所有用户 + 用量统计 + 当前在线。仅 admin。
    重的用量聚合放线程池,回 loop 线程读 pending_bus(免锁快照)。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看用户总览")
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, admin_query_repository().list_user_usage)
    online = pending_bus.online_user_ids()
    return [AdminUserUsage(**row, is_online=row["id"] in online) for row in rows]


@router.get("/admin/users/{user_id}/notebooks", response_model=List[AdminUserNotebook])
def list_admin_user_notebooks(user_id: str, user: UserProfile = Depends(get_current_user)) -> List[AdminUserNotebook]:
    """某用户名下笔记本详情。仅 admin。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看用户笔记本")
    return [
        AdminUserNotebook(**row)
        for row in admin_query_repository().list_user_notebooks(user_id)
    ]


@router.get("/admin/online")
async def list_online_users(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前在线用户 id 集合(持有实时流连接)。仅 admin,纯读内存零 DB。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看在线状态")
    return {"online_ids": sorted(pending_bus.online_user_ids())}


# --- 待确认中心 (Pending Actions Center) ---------------------------------


@router.get("/me/pending-actions")
def me_pending_actions(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前用户的待办快照：深度报告待确认 + 治理三队列 + 索引状态，供铃铛使用。
    只读转调 repository().pending_actions；聚合逻辑与三源覆盖见该方法本身
    （Task 1 已有专门单测），此处只做 HTTP 薄包装。"""
    return repository().pending_actions(user.id)


@router.get("/me/pending-actions/stream")
async def me_pending_stream(
    request: Request, user: UserProfile = Depends(get_current_user)
) -> StreamingResponse:
    """待确认中心的实时推送通道（NDJSON）：先补发离线期间缓冲的瞬时事件，
    再发一帧初始 snapshot，再挂进 pending_bus 循环等待后续推送；15s 无消息发
    keepalive 注释帧（前端按 `:` 前缀跳过，非 JSON 行）。"""
    uid = user.id
    pending_bus.bind_loop()

    async def gen():
        # 1) 先补发离线期间缓冲的瞬时事件(跨会话)
        for ev in pending_bus.flush_buffer(uid):
            yield json.dumps({"kind": "event", **ev}, ensure_ascii=False) + "\n"
        # 2) 初始 snapshot —— DB 计算放线程池,勿阻塞 loop
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, repository().pending_actions, uid)
        yield json.dumps({"kind": "snapshot", "data": data}, ensure_ascii=False) + "\n"
        # 3) 注册连接,循环等待推送 + keepalive
        q = pending_bus.register(uid)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n"  # 注释帧,前端忽略(非 JSON 行)
                    continue
                yield json.dumps(msg, ensure_ascii=False) + "\n"
        finally:
            pending_bus.unregister(uid, q)

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@router.post(
    "/notebooks/{notebook_id}/paper-meta/backfill",
    dependencies=[Depends(require_notebook_access)],
)
def backfill_paper_metadata(notebook_id: str) -> dict:
    """补抽该 notebook 缺论文元数据的源(后台线程,幂等可续跑)。返回排队数;
    LLM 未配置 409。owner 门控由 require_notebook_access 承担(非 owner 404)。"""
    repo = repository()
    llm_ready = getattr(repo.kg_llm_client, "configured", False) or getattr(
        repo.llm_client, "configured", False
    )
    if not llm_ready:
        raise HTTPException(status_code=409, detail="LLM not configured")
    queued = len(repo.sources_missing_paper_meta(notebook_id))
    if queued:
        background_jobs.submit(
            repo.backfill_paper_metadata, notebook_id,
            name=f"papermeta-{notebook_id}",
            notify_pending=True,   # 兜底刷新 pending 快照
        )
    return {"queued": queued}


# --- knowhow 表跨 notebook 传输（复制/移动） -------------------------------
# 追加在文件尾：mode 决定源守卫（copy=read / move=write），无法用静态
# Depends(require_notebook_*)，故在处理器内手动核权（复用 deps 的访问仓库）。
# 用同步 def（同 create_report/create_object_schema）——FastAPI 自动放线程池跑，
# 无需 run_in_threadpool、无需在文件顶部加 import（避免打断行号 pin）。
from app.api.deps import notebook_access_repository as _kh_access  # noqa: E402
from app.models.schemas import KnowhowTransferRequest  # noqa: E402
from app.services.knowhow import transfer as _kh_transfer  # noqa: E402


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
    access = _kh_access()
    # 只有 copy 能放宽到「只读成员」——写成 `read if copy else access` 而不是
    # `access if move else read`，是为了失败关闭（默认最严兜底，同 deps.py 末尾
    # require_notebook_access = require_notebook_write 的取向）：将来若多出第三
    # 种 mode，它继承的是 owner-only 的强守卫，而不是悄悄拿到只读成员权限。
    # 今天两者等价（mode 是 Literal["copy","move"]），差别只在未来。
    source_check = (
        access.user_can_read_notebook
        if payload.mode == "copy"
        else access.user_can_access_notebook
    )
    if not source_check(notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not access.user_can_access_notebook(payload.target_notebook_id, user.id):
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
            repo, table_id, payload.target_notebook_id, user.id, payload.mode
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


from app.api.content_overview_routes import router as content_overview_router  # noqa: E402
from app.api.deps import content_overview_service  # noqa: E402

router.include_router(content_overview_router)
