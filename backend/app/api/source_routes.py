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
    user_error,
)
from app.models.identity import UserProfile
from app.models.sources import (
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    PaginatedSources,
    ReparseSourcesRequest,
    RepairScheduledResult,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
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


def _document_capacity(notebook_id: str) -> "tuple[int, int] | None":
    """(当前可见文档数, owner 有效上限) —— 「每笔记本文档数量上限」的单一计算点,供
    上传/导入的批量预检与 URL 导入的逐条预算共用。owner 为 admin 的笔记本豁免 → None
    (不限)。分享拷贝、离线批量摄取、Memory 派生源都走各自路径,不经这些 HTTP 端点。"""
    repo = source_repository()
    owner_id, owner_role = repo.notebook_owner(notebook_id)
    if owner_role == "admin":
        return None
    return repo.visible_document_count(notebook_id), repo.effective_document_limit(owner_id)


def _enforce_document_capacity(notebook_id: str, adding: int) -> None:
    """建源前批量预检(上传/导入:提交的每个文件/条目都会入库,故按总数一次性判)。超限
    整批 409。URL 导入不用它——URL 天然部分成功(空白/不可达/非 PDF 跳过),改由
    add_url_sources 按成功探测逐条扣减 capacity,详见该端点。

    check-then-insert 非原子:并发双提交存在极小 TOCTOU 窗口(可能略微超限)。这是刻意
    取舍——为此加写锁/唯一约束不值当,偶尔多一两篇文档无害,下一次提交即被挡住。"""
    cap = _document_capacity(notebook_id)
    if cap is None:
        return
    current, limit = cap
    if current + adding > limit:
        raise user_error(
            409,
            f"该笔记本最多可添加 {limit} 篇文档，当前已有 {current} 篇，"
            f"无法再添加 {adding} 篇。",
        )


def _truthy_form_flag(raw: str) -> bool:
    """把上传表单里的 doc_type_explicit 项（前端发 "1"/"0"）解析成 bool。缺省/空/其它
    值一律当 False——「没显式表态」是安全默认（reuse 路径据此保留既有源的类型）。"""
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


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
        _enforce_document_capacity(notebook_id, len(payload.files))
        return source_repository().import_sources(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources/url", response_model=AddUrlSourcesResult, dependencies=[Depends(require_notebook_access)])
def add_url_sources(
    notebook_id: str,
    payload: AddUrlSourcesRequest,
) -> AddUrlSourcesResult:
    repo = source_repository()
    # URL 导入天然部分成功(空白/不可达/非 PDF 会被跳过),故容量按**成功探测逐条**核算:
    # 剩余额度 = 有效上限 − 当前数;超出的有效 URL 进 rejected(不消耗配额、不整批 409),
    # 与「一个无效链接拖累整批」相反。admin 豁免 → capacity=None(不限)。
    cap = _document_capacity(notebook_id)
    capacity = None if cap is None else max(0, cap[1] - cap[0])
    try:
        return repo.add_url_sources(
            notebook_id,
            payload.urls,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
            capacity=capacity,
        )
    except MinerUCloudNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# response_model 是 SourceSummary 的子类：字段只增不减（多一个 reused），旧客户端
# 原样可用。上传路径会做同 notebook 内容去重，返回值里可能夹着**没有新建**的既有
# 源——前端据 reused 分别计数/措辞，别再拿 len(response) 当「新增了几个」。
@router.post("/notebooks/{notebook_id}/sources", response_model=List[UploadedSourceSummary], dependencies=[Depends(require_notebook_access)])
async def upload_sources(
    notebook_id: str,
    files: List[UploadFile] = File(...),
    doc_types: List[str] = Form(default=[]),
    doc_type_explicit: List[str] = Form(default=[]),
) -> List[UploadedSourceSummary]:
    try:
        repo = source_repository()
        # 建源前先挡容量(在读取任何文件内容之前 fail-fast,不为超限的批次白读进内存)。
        _enforce_document_capacity(notebook_id, len(files))
        uploaded_files = []
        for index, file in enumerate(files):
            file_name = file.filename or "source.bin"
            _validate_source_file(file_name)
            content = await file.read()
            _validate_source_file(file_name, len(content))
            # doc_types / doc_type_explicit are aligned with files by position;
            # missing/extra are tolerated (老前端不发 doc_type_explicit → 一律非显式)。
            doc_type = doc_types[index] if index < len(doc_types) else ""
            explicit = (
                _truthy_form_flag(doc_type_explicit[index])
                if index < len(doc_type_explicit)
                else False
            )
            uploaded_files.append(
                UploadedSourceFile(
                    file_name=file_name,
                    content_type=file.content_type or "",
                    content=content,
                    doc_type=doc_type,
                    doc_type_explicit=explicit,
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


@router.post(
    "/notebooks/{notebook_id}/sources/reparse",
    response_model=RepairScheduledResult,
    dependencies=[Depends(require_notebook_access)],
)
def reparse_sources(
    notebook_id: str, payload: ReparseSourcesRequest
) -> RepairScheduledResult:
    """体检修复(H2 空源 / H3 缺分块):批量重新解析。逐个后台 submit_job(process_source)
    ——复用既有摄取管线(含 P1.5 的活跃租约 + 分块串行锁),**不**另造摄取路径。
    ⚠ 每个 source_id 必须真属于本 notebook 才排入(防越权:``require_notebook_access`` 只守
    notebook,不守 body 里带来的任意 source_id)。不属于本库/不存在的静默跳过,回执只含实际排入的。"""
    repo = source_repository()
    scheduled: List[str] = []
    for source_id in payload.source_ids:
        try:
            if repo.get_source(source_id).notebook_id != notebook_id:
                continue
        except KeyError:
            continue
        kg_scheduler.submit_job(repo.process_source, source_id)
        scheduled.append(source_id)
    return RepairScheduledResult(scheduled=scheduled)


def _backfill_vectors_job(repo, notebook_id: str) -> None:
    """后台补齐该 notebook 缺失的 chunk + element 向量(只补缺失、幂等)。复用 batch_ingest
    的既有 backfill,EMBED 未配则各自跳过。best-effort:一侧失败不拦另一侧。"""
    from app.services import batch_ingest

    try:
        batch_ingest.backfill_chunk_embeddings(repo, notebook_id, missing_only=True)
    except Exception:  # noqa: BLE001 — 后台 job 自负错误,一侧失败不拦另一侧
        pass
    try:
        batch_ingest.backfill_element_embeddings(repo, notebook_id)
    except Exception:  # noqa: BLE001
        pass


@router.post(
    "/notebooks/{notebook_id}/backfill-vectors",
    response_model=RepairScheduledResult,
    dependencies=[Depends(require_notebook_access)],
)
def backfill_vectors(notebook_id: str) -> RepairScheduledResult:
    """体检修复(H4 缺 chunk 向量 / H5 缺 element 向量):后台补齐该 notebook 的缺失向量
    (只补缺失、幂等,仅 embedding、不动解析/KG)。用户点触发、非自动(承 efficiency-first:
    凡调 embedding 的修复不自动)。补完后 H4/H5 计数下降,前端重拉 checkup 反映。
    用完整 facade(``repository()``)——backfill 需要 maintenance/configured(BatchIngestRepository)。"""
    kg_scheduler.submit_job(_backfill_vectors_job, repository(), notebook_id)
    return RepairScheduledResult(accepted=True)


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
