from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from app.api.deps import (
    get_current_user,
    notebook_access_repository,
    repository,
    require_notebook_access,
    require_notebook_read,
    source_repository,
)
from app.models.identity import UserProfile
from app.models.sources import (
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    PaginatedSources,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
)
from app.repositories.ports import UploadedSourceFile
from app.services import background_jobs
from app.services.kg import scheduler as kg_scheduler
from app.services.knowhow.assets import AssetService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured


router = APIRouter()

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


@router.post(
    "/notebooks/{notebook_id}/paper-meta/backfill",
    dependencies=[Depends(require_notebook_access)],
)
def backfill_paper_metadata(notebook_id: str) -> dict:
    """补抽该 notebook 缺论文元数据的源(后台线程,幂等可续跑)。返回排队数;
    LLM 未配置 409。owner 门控由 require_notebook_access 承担(非 owner 404)。"""
    repo = repository()
    llm_ready = repo._runtime.models.configured("paper_metadata")
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
