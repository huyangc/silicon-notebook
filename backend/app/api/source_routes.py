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

# 批量重解析一次最多受理的源数(去重后)。体检命中样本本就 ≤20;给宽松上限只为挡住
# 「重复 id/超大列表灌满无界执行器队列」(codex),不是产品限制。
_REPARSE_MAX = 200

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
    notebook,不守 body 里带来的任意 source_id)。不属于本库/不存在的静默跳过,回执只含实际排入的。
    ⚠ **先去重 + 限量**再排(codex):重复 id 会把同一源的解析/嵌入/KG 昂贵管线在无界队列里并发
    排多次;超大列表同理会灌满执行器。dict.fromkeys 保序去重;超 _REPARSE_MAX 直接 400 拒绝。"""
    unique_ids = list(dict.fromkeys(payload.source_ids))
    if len(unique_ids) > _REPARSE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"too many sources to reparse at once (max {_REPARSE_MAX})",
        )
    repo = source_repository()
    scheduled: List[str] = []
    for source_id in unique_ids:
        try:
            src = repo.get_source(source_id)
        except KeyError:
            continue
        if src.notebook_id != notebook_id:
            continue
        # ⚠ 只重解析**导入型**用户源(codex):memory/knowhow 隐藏合成源无 file_path、由各自
        # 投影服务维护;把它们喂给文档解析 process_source 只会标失败/清派生态,不是修复。
        # 与 H2/H3 判据同口径(那两项本就排除 memory/knowhow),这里挡住 body 里带来的 id。
        if src.type in ("memory", "knowhow"):
            continue
        kg_scheduler.submit_job(repo.process_source, source_id)
        scheduled.append(source_id)
    return RepairScheduledResult(scheduled=scheduled)


def _backfill_vectors_job(repo, notebook_id: str) -> None:
    """后台补齐该 notebook 缺失的 chunk + element 向量(只补缺失、幂等)。EMBED 未配则跳过。

    ⚠ **逐源经 hold_source_chunk_lock 持 P1.5 的分块锁、锁内读→嵌**(codex P1):element/chunk id
    在 reparse 时复用,并发补齐若在锁外读到旧代行、embedding 后在替换之后才提交,会给新文本挂上
    **永久陈旧向量**(后续缺向量检查也发现不了——向量「在」,只是内容旧)。守卫在持锁期间同时登记
    活跃租约,使 process_source 不会在本方仍持锁时把锁 pop 掉(codex 第2轮 P1:否则后续 reparse 另建
    新锁、互斥失效;backfill-only 源还会锁泄漏)。锁内才读缺失行,故读到的行在补齐提交前不会被
    reparse 换掉——彻底关掉「快照后才开始 reparse」的残留窗口。best-effort:一源失败不拦其余。

    源发现只用**轻量 DISTINCT source_id 查询**、且只查已配的 workload(codex 第2轮 P1:大库上
    把每行全文物化进内存仅为收 source_id 会 GB 级/OOM,还会白扫未配的那侧)。"""
    mnt = repo.maintenance
    ingestion = repo._runtime.source_ingestion
    chunk_ok = repo.configured("chunk_embedding")
    elem_ok = repo.configured("source_element_embedding")
    if not (chunk_ok or elem_ok):
        return
    # 廉价定位「有缺失向量的源」:只取 DISTINCT source_id(不物化正文),且只查已配 workload。
    # 真正补齐在锁内按 only_source_id 重读(读到的才是待嵌的行)。
    sources: set[str] = set()
    if chunk_ok:
        sources |= set(mnt.missing_chunk_vector_source_ids(notebook_id))
    if elem_ok:
        sources |= set(mnt.missing_element_vector_source_ids(notebook_id))
    for source_id in sources:
        with ingestion.hold_source_chunk_lock(source_id):
            try:
                if chunk_ok:
                    rows = mnt.missing_chunk_embedding_rows(notebook_id, only_source_id=source_id)
                    if rows:
                        mnt.embed_chunks_batch(
                            notebook_id,
                            [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows],
                        )
            except Exception:  # noqa: BLE001 — 后台 job 自负错误,一源失败不拦其余
                pass
            try:
                if elem_ok:
                    rows = mnt.missing_element_embedding_rows(notebook_id, only_source_id=source_id)
                    if rows:
                        mnt.embed_elements_batch(
                            notebook_id,
                            [{"element_id": r["id"], "source_id": r["source_id"], "text": r["text"]} for r in rows],
                        )
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
    repo = repository()
    mnt = repo.maintenance
    # 按**损坏所属的 workload** 精判受理(codex P2):某类缺向量确实存在**且**其嵌入 workload 已配,
    # job 才补得动该类。粗判 `configured(chunk) or configured(element)` 会在「损坏是 element、却只配
    # 了 chunk」时假受理→前端「修复中」空转轮询、而 H5 永远清不掉。短路:未配某类就不跑那类 COUNT。
    # 计数不排活跃租约:看板 H4/H5 是排除活跃后的口径,本处是其超集,故看板显示有损坏时这里必也判
    # 有缺——不会误拒用户看到的损坏(在途嵌入的源即便被算进来,job 也在其分块锁内 no-op,无害)。
    chunk_fixable = (
        repo.configured("chunk_embedding")
        and mnt.count_missing_chunk_vectors(notebook_id) > 0
    )
    elem_fixable = (
        repo.configured("source_element_embedding")
        and mnt.count_missing_element_vectors(notebook_id) > 0
    )
    if not (chunk_fixable or elem_fixable):
        # 无「已配 + 有缺」的类 → job 必 no-op。accepted=false,前端据此提示而非「修复中」。
        return RepairScheduledResult(accepted=False)
    kg_scheduler.submit_job(_backfill_vectors_job, repo, notebook_id)
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
