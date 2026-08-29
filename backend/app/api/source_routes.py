from pathlib import Path
from typing import Callable, List, Sequence

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.datastructures import FormData, UploadFile as StarletteUploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.api.deps import (
    get_current_user,
    notebook_access_repository,
    notebook_capability_allowed,
    notebook_store_port,
    repository,
    require_notebook_capability,
    require_notebook_read,
    source_repository,
    user_error,
)
from app.core.config import SOURCE_UPLOAD_MAX_FILES_PER_BATCH, get_settings
from app.models.identity import UserProfile
from app.models.sources import (
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    PaginatedSourceElements,
    PaginatedSources,
    ReparseSourcesRequest,
    RepairScheduledResult,
    ScopedSourceDetail,
    SourceDetail,
    SourceElement,
    SourceImportRequest,
    SourceSummary,
    UploadedSourceSummary,
)
from app.repositories.ports import (
    DocumentCapacityExceeded,
    SourceRepository,
    UploadedSourceFile,
)
from app.services import background_jobs
from app.services.kg import scheduler as kg_scheduler
from app.services.knowhow.assets import AssetService
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.parser_registry import SUPPORTED_SOURCE_SUFFIXES


router = APIRouter()

# 批量重解析一次最多受理的源数(去重后)。体检命中样本本就 ≤20;给宽松上限只为挡住
# 「重复 id/超大列表灌满无界执行器队列」(codex),不是产品限制。
_REPARSE_MAX = 200

_SOURCE_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["files"],
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                        },
                        "doc_types": {"type": "array", "items": {"type": "string"}},
                        "doc_type_explicit": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                }
            }
        },
    }
}

_SOURCE_UPLOAD_MAX_FORM_FIELDS = SOURCE_UPLOAD_MAX_FILES_PER_BATCH * 2
_SOURCE_UPLOAD_MAX_FIELD_BYTES = 4 * 1024
_SOURCE_UPLOAD_FORM_KEYS = frozenset({"files", "doc_types", "doc_type_explicit"})

# 「隐藏合成源」:memory/knowhow 投影出来的物理 source 行,不是用户导入的文档,由各自
# 投影服务维护。与存储层 VISIBLE_SOURCE_TYPES_PREDICATE 同一份口径(来源面板与文档数量
# 上限都按它排除)。本模块两处消费:reparse 不受理它们,参与集代理读取跨库时拒绝它们。
_HIDDEN_SOURCE_TYPES = frozenset({"memory", "knowhow"})


def _asset_service() -> AssetService:
    return AssetService(repository())


def _format_upload_size_limit(limit_bytes: int) -> str:
    """Compact, stable display for the configured byte limit in user errors."""
    mib = 1024 * 1024
    kib = 1024
    if limit_bytes % mib == 0:
        return f"{limit_bytes // mib} MB"
    if limit_bytes % kib == 0:
        return f"{limit_bytes // kib} KB"
    return f"{limit_bytes} bytes"


def _source_upload_too_large(max_bytes: int) -> HTTPException:
    return user_error(
        413,
        "上传的单个来源文件超过当前大小上限 "
        f"{_format_upload_size_limit(max_bytes)}（{max_bytes} 字节）。"
        "请选择更小的文件，或联系管理员调整部署配置。",
    )


class _SourceUploadTooLarge(MultiPartException):
    """Internal multipart abort used to close spool files before returning 413."""


class _BoundedSourceMultiPartParser(MultiPartParser):
    """Stop a source file part while it is streaming into Starlette's spool.

    Starlette's ``max_part_size`` applies only to ordinary form fields, not file
    parts. Tracking the active file here prevents an oversized upload from first
    consuming arbitrary temporary-disk space. The route parses manually so this
    check and authentication both happen before FastAPI's usual eager form parse.
    """

    def __init__(self, *args, max_source_bytes: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_source_bytes = max_source_bytes
        self._current_source_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_source_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_source_bytes += end - start
            if self._current_source_bytes > self._max_source_bytes:
                raise _SourceUploadTooLarge("source file exceeded configured limit")
        super().on_part_data(data, start, end)


async def _parse_source_upload(
    request: Request,
    max_bytes: int,
) -> tuple[FormData, list[StarletteUploadFile], list[str], list[str]]:
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise user_error(400, "来源文件上传必须使用 multipart/form-data 请求。")
    parser = _BoundedSourceMultiPartParser(
        request.headers,
        request.stream(),
        max_source_bytes=max_bytes,
        max_files=SOURCE_UPLOAD_MAX_FILES_PER_BATCH,
        max_fields=_SOURCE_UPLOAD_MAX_FORM_FIELDS,
        max_part_size=_SOURCE_UPLOAD_MAX_FIELD_BYTES,
    )
    try:
        form = await parser.parse()
    except _SourceUploadTooLarge:
        raise _source_upload_too_large(max_bytes)
    except MultiPartException as exc:
        if exc.message.startswith("Too many files."):
            raise user_error(
                413,
                f"单次最多上传 {SOURCE_UPLOAD_MAX_FILES_PER_BATCH} 个来源文件。"
                "请减少本批文件数量后重试。",
            )
        if exc.message.startswith("Too many fields.") or "maximum size" in exc.message:
            raise user_error(413, "上传请求中的文件类型元数据过多或过大，请减少本批文件后重试。")
        raise HTTPException(status_code=400, detail=f"Invalid multipart upload: {exc.message}")

    unexpected_keys = set(form.keys()) - _SOURCE_UPLOAD_FORM_KEYS
    if unexpected_keys:
        await form.close()
        raise HTTPException(status_code=422, detail="Unexpected source upload form field")

    raw_files = form.getlist("files")
    files = [item for item in raw_files if isinstance(item, StarletteUploadFile)]
    if not files or len(files) != len(raw_files):
        await form.close()
        raise HTTPException(status_code=422, detail="At least one source file is required")

    def _string_fields(name: str) -> list[str] | None:
        values = form.getlist(name)
        if any(not isinstance(item, str) for item in values):
            return None
        return [str(item) for item in values]

    doc_types = _string_fields("doc_types")
    doc_type_explicit = _string_fields("doc_type_explicit")
    if (
        doc_types is None
        or doc_type_explicit is None
        or len(doc_types) > len(files)
        or len(doc_type_explicit) > len(files)
    ):
        await form.close()
        raise HTTPException(status_code=422, detail="Invalid source upload metadata")
    return form, files, doc_types, doc_type_explicit


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
        max_bytes = get_settings().source_upload_max_bytes
        if content_size > max_bytes:
            raise _source_upload_too_large(max_bytes)


def _document_capacity(
    notebook_id: str, repo: "SourceRepository | None" = None
) -> "tuple[int, int] | None":
    """(当前可见文档数, owner 有效上限) —— 「每笔记本文档数量上限」的单一计算点,供
    上传/导入的批量预检与 URL 导入的逐条预算共用。owner 为 admin 的笔记本豁免 → None
    (不限)。分享拷贝、离线批量摄取、Memory 派生源都走各自路径,不经这些 HTTP 端点。

    ``repo`` 是**注入取数口**,给不经 FastAPI 依赖的调用方用:MCP 的 Agent 来源工具
    拿的是 ``create_memory_mcp`` 注入的 repository,不走 ``source_repository()``
    (同 ``in_participant_scope`` 的 ``participant_notebook_ids`` 参数,理由逐字相同)。
    默认 None 保持浏览器侧三个调用点逐字不变。这个参数**不是**允许调用方另写一份上限
    规则的口子——上限判据只写在这里一次,Agent 侧上传/加链接也必须过这一道。"""
    repo = repo if repo is not None else source_repository()
    limit = _document_capacity_limit(notebook_id, repo)
    if limit is None:
        return None
    return repo.visible_document_count(notebook_id), limit


def _document_capacity_limit(
    notebook_id: str, repo: "SourceRepository | None" = None
) -> "int | None":
    """只取 owner 有效上限(admin 豁免 → None),**不数当前文档数**。

    给只需要把上限穿进建源事务、不做预检的调用方(MCP add_source_text 的去重
    命中分支——那条路预期复用既有行,COUNT 是白算的,而它是 Agent 幂等重试的
    热路径)。admin 豁免判据只写在这一处,_document_capacity 在它之上补 COUNT。"""
    repo = repo if repo is not None else source_repository()
    owner_id, owner_role = repo.notebook_owner(notebook_id)
    if owner_role == "admin":
        return None
    return repo.effective_document_limit(owner_id)


def document_capacity_message(current: int, limit: int, adding: int) -> str:
    """「文档数量上限」超限时给用户看的**唯一**一句话。

    抽出来是因为它现在有两个出处:浏览器路由的 409(下面),和 MCP 的 Agent 建源工具
    (它抛的是 ValueError,`user_error` 的 HTTP 载体在那条路上没有意义)。同一个条件在
    两个界面上说两种话——尤其一中一英——用户和 Agent 就会以为遇到了两个不同的限制。
    纯中文模板,只插三个整数,无内部黑话(界面词只说「文档」)。"""
    return (
        f"该笔记本最多可添加 {limit} 篇文档，当前已有 {current} 篇，"
        f"无法再添加 {adding} 篇。"
    )


def _enforce_document_capacity(notebook_id: str, adding: int) -> "int | None":
    """建源前批量预检(上传/导入:提交的每个文件/条目都会入库,故按总数一次性判)。超限
    整批 409。URL 导入不用它——URL 天然部分成功(空白/不可达/非 PDF 跳过),由
    import_url_sources 把上限穿给逐条建源,详见该端点。

    返回 owner 有效上限(admin 豁免 → None),供上传路径把它穿进
    ``upload_sources(capacity_limit=...)``:预检只是便宜的先手 409,权威强制在
    store 的建源写事务内(COUNT+INSERT 同事务;SQLite BEGIN IMMEDIATE / PG
    notebook 行锁)。此前这里登记过的 check-then-insert TOCTOU(两个并发请求都
    快照到剩 1 个名额就都能入库)已由那道事务内闸关闭(PR #584 codex R6);预检
    输给并发时,路由把 store 抛的 ``DocumentCapacityExceeded`` 翻成同一句 409。
    唯一仍只走预检的是 ``/sources/import``(纯元数据登记、无前端调用方)——这是
    **范围取舍**不是技术障碍:它的批量插入本就共享一个自有写事务,在里面补一次
    COUNT 即可闭合,只是需要另一种穿参形状(批量 all-or-nothing 而非逐条),而该
    端点没有并发双击的真实入口,故保留原竞态窗口、如实登记。

    **上限值是请求入口的快照,刻意不在事务内重读**(codex #590 R2 P2):事务内闸
    关的是 COUNT-vs-INSERT 的完整性竞态(两个并发建源同抢最后名额);而 admin 在
    预检与事务之间改 override/全局默认属于配置变更竞态——按新限重读要在每次建源
    事务里多付 user_profiles/app_settings 两次读、还得与管理员写串行,买到的只是
    一个自愈边界(下一次请求即按新限执行,偏差有界于在飞请求数)。"""
    cap = _document_capacity(notebook_id)
    if cap is None:
        return None
    current, limit = cap
    if current + adding > limit:
        raise user_error(409, document_capacity_message(current, limit, adding))
    return limit


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


@router.post("/notebooks/{notebook_id}/sources/import", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_capability("sources:write"))])
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


def import_url_sources(
    notebook_id: str,
    urls: List[str],
    *,
    trusted_proxy_origins: "frozenset[str] | None" = None,
) -> AddUrlSourcesResult:
    """按 URL 建源的**唯一**实现,浏览器端点与部署插件路由宿主共用。

    抽成模块级函数只为让第二个调用方(``app.api.extension_routes`` 的
    ``PluginUrlSourceImportPort`` 适配器)复用**同一份**语义,而不是让插件自己
    再拼一遍——它拿不到 repository,也就不可能另写一份:

    * **容量逐条核算**:URL 导入天然部分成功(空白/不可达/非 PDF 会被跳过),故容量按
      **成功探测逐条**核算:超出的有效 URL 进 rejected(不消耗配额、不整批 409),与
      「一个无效链接拖累整批」相反。这里穿下去的是**绝对上限** ``capacity_limit``
      而不是请求开始时算好的剩余额度——当前数由 store 在每条 INSERT 自己的写事务内
      重新 COUNT(冻结的剩余额度正是并发双请求都能花掉同一个名额的 TOCTOU)。
    * **admin 豁免**:owner 是 admin 的笔记本 ``capacity_limit=None``(不限)。
    * **未配置解析服务 → 400**,且 ``detail`` 是服务层原文(``str(exc)``)而**不是**
      ``user_error()``:它带的是「本地 MINERU_MODE 或云端 MINERU_API_TOKEN」这类部署
      配置措辞,不是给终端用户看的中文文案,所以刻意不打 ``X-User-Message``。这条
      行为在抽取前后逐字不变——插件路由得到的映射与浏览器端点完全一致。

    ``notebook_id`` 的授权由**调用方**负责:这个函数只做容量与调度,不判权限。浏览器
    端点靠 ``sources:write`` 装饰器守卫;插件路由靠
    ``extension_routes._UrlSourceImportAdapter`` 在委托进来**之前**用
    ``notebook_capability_allowed("sources:write", …)`` 对**本请求已认证的用户**判一次
    ——不是靠插件自己挂了哪道门(它可以只挂读门、把 id 藏在 body 里,或者给路径参数换
    个名字)。

    ``trusted_proxy_origins`` 是部署配置的受信代理 origin 白名单(SSRF 公网地址
    检查的豁免面,详见 ``source_ingestion.add_url_sources``),**只由插件端口适配器
    注入**——浏览器端点(下面的 ``add_url_sources`` 路由)刻意不传,恒 ``None``,
    故导入探测半程的豁免只对插件端口可达;请求级输入在任何路径上都改不了名单
    本身(解析下载半程按部署 settings 判定,见
    ``source_ingestion._parser_trusted_proxy_origins``)。
    """
    repo = source_repository()
    cap = _document_capacity(notebook_id)
    capacity_limit = None if cap is None else cap[1]
    try:
        return repo.add_url_sources(
            notebook_id,
            urls,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
            capacity_limit=capacity_limit,
            trusted_proxy_origins=trusted_proxy_origins,
        )
    except MinerUCloudNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources/url", response_model=AddUrlSourcesResult, dependencies=[Depends(require_notebook_capability("sources:write"))])
def add_url_sources(
    notebook_id: str,
    payload: AddUrlSourcesRequest,
) -> AddUrlSourcesResult:
    return import_url_sources(notebook_id, payload.urls)


# response_model 是 SourceSummary 的子类：字段只增不减（多一个 reused），旧客户端
# 原样可用。上传路径会做同 notebook 内容去重，返回值里可能夹着**没有新建**的既有
# 源——前端据 reused 分别计数/措辞，别再拿 len(response) 当「新增了几个」。
@router.post(
    "/notebooks/{notebook_id}/sources",
    response_model=List[UploadedSourceSummary],
    dependencies=[Depends(require_notebook_capability("sources:write"))],
    openapi_extra=_SOURCE_UPLOAD_OPENAPI,
)
async def upload_sources(
    notebook_id: str,
    request: Request,
) -> List[UploadedSourceSummary]:
    max_bytes = get_settings().source_upload_max_bytes
    # A full notebook must fail before any multipart bytes are consumed. Other
    # requests remain bounded by SOURCE_UPLOAD_MAX_FILES_PER_BATCH in the parser.
    cap = _document_capacity(notebook_id)
    if cap is not None and cap[0] >= cap[1]:
        _enforce_document_capacity(notebook_id, 1)
    form, files, doc_types, doc_type_explicit = await _parse_source_upload(
        request, max_bytes
    )
    try:
        repo = source_repository()
        # 先用 multipart parser 已核算的真实 part 大小验证整批，再做任何持久化。
        for file in files:
            file_name = file.filename or "source.bin"
            _validate_source_file(file_name, file.size)
        # 预检整批(便宜的先手 409),并拿到 owner 有效上限穿给每条建源——真正的
        # 强制在 store 的写事务内(见 _enforce_document_capacity docstring)。
        capacity_limit = _enforce_document_capacity(notebook_id, len(files))

        uploaded: list[UploadedSourceSummary] = []
        for index, file in enumerate(files):
            file_name = file.filename or "source.bin"
            # Keep the read bounded even if a future parser or test double reports
            # an incorrect size. Process one file at a time so a valid batch does
            # not retain N × max_bytes in memory before persistence starts.
            content = await file.read(max_bytes + 1)
            _validate_source_file(file_name, len(content))
            # doc_types / doc_type_explicit are aligned with files by position;
            # missing/extra are tolerated (老前端不发 doc_type_explicit → 一律非显式)。
            doc_type = doc_types[index] if index < len(doc_types) else ""
            explicit = (
                _truthy_form_flag(doc_type_explicit[index])
                if index < len(doc_type_explicit)
                else False
            )
            result = repo.upload_sources(
                notebook_id,
                [
                    UploadedSourceFile(
                        file_name=file_name,
                        content_type=file.content_type or "",
                        content=content,
                        doc_type=doc_type,
                        doc_type_explicit=explicit,
                    )
                ],
                scheduler=lambda source_id: kg_scheduler.submit_job(
                    repo.process_source, source_id
                ),
                capacity_limit=capacity_limit,
            )
            uploaded.extend(result)
        return uploaded
    except DocumentCapacityExceeded as exc:
        # 竞态兜底:预检放行后、这个文件插入前,并发请求占走了名额,store 在建源
        # 写事务内拒绝(行没插、孤儿文件已清)。同一句 409 文案;adding 报本次还没
        # 建成的文件数(len(files) - len(uploaded):含被拒的这一个;更早的文件已各自
        # 提交并保留,与拆成多次请求提交同形,来源列表随轮询显现)。
        raise user_error(
            409,
            document_capacity_message(exc.current, exc.limit, len(files) - len(uploaded)),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    finally:
        await form.close()


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceDetail:
    if not notebook_access_repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员 ∪ 有效授权边
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.post("/sources/{source_id}/parse", response_model=SourceSummary)
def parse_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceSummary:
    # notebook_id 不是这个端点 URL 上的路径参数(URL 只带 source_id),守卫没法
    # 挂在静态的 Depends(require_notebook_capability(...)) 上——先反查
    # notebook_id,再按同一张能力表判 "sources:write"(见
    # notebook_capability_allowed 的 docstring)。语义与旧的
    # `source_owner(source_id) != user.id` 逐字等价:source 不存在 → 404;
    # 非 owner → 404。
    notebook_id = notebook_access_repository().source_notebook_id(source_id)
    if notebook_id is None or not notebook_capability_allowed(
        "sources:write", notebook_id, user.id
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().parse_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.post(
    "/notebooks/{notebook_id}/sources/reparse",
    response_model=RepairScheduledResult,
    dependencies=[Depends(require_notebook_capability("sources:write"))],
)
def reparse_sources(
    notebook_id: str, payload: ReparseSourcesRequest
) -> RepairScheduledResult:
    """体检修复(H2 空源 / H3 缺分块):批量重新解析。逐个后台 submit_job(process_source)
    ——复用既有摄取管线(含 P1.5 的活跃租约 + 分块串行锁),**不**另造摄取路径。
    ⚠ 每个 source_id 必须真属于本 notebook 才排入(防越权:
    ``require_notebook_capability("sources:write")`` 只守 notebook,不守 body 里
    带来的任意 source_id)。不属于本库/不存在的静默跳过,回执只含实际排入的。
    ⚠ **先去重 + 限量**再排(codex):重复 id 会把同一源的解析/嵌入/KG 昂贵管线在无界队列里并发
    排多次;超大列表同理会灌满执行器。dict.fromkeys 保序去重;超 _REPARSE_MAX 直接 400 拒绝。"""
    unique_ids = list(dict.fromkeys(payload.source_ids))
    if len(unique_ids) > _REPARSE_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"too many sources to reparse at once (max {_REPARSE_MAX})",
        )
    repo = source_repository()
    # Reject before any background submission; process_source repeats the
    # admission inside the worker to close the queue-to-execution race.
    repo.require_indexing_pipeline_write(notebook_id)
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
        if src.type in _HIDDEN_SOURCE_TYPES:
            continue
        kg_scheduler.submit_job(repo.process_source, source_id)
        scheduled.append(source_id)
    return RepairScheduledResult(scheduled=scheduled)


_BACKFILL_PAGE_ROWS = 500
"""每源分页读取的目标行数(会向下取整到 embed_batch_size 的整数倍,见 _backfill_page_rows)。"""


def _backfill_page_rows(repo) -> int:
    """把 ``_BACKFILL_PAGE_ROWS`` 向下取整到 ``embed_batch_size`` 的整数倍(至少一整批)。

    ⚠ 取整不是美学:``embed_chunks_batch`` / ``embed_elements_batch`` 都把收到的行按
    ``embed_batch_size`` 切成一次次 embedder 调用。页大小若不是它的整数倍,每页末尾就会
    多出一个不足整批的调用,**调用次数**就跟着变。取整之后 embedder 的调用次数与
    「一次性喂全部行」逐字相同(``ceil(N / embed_batch_size)``)。

    ⚠ 只说到这里为止:**每次调用的文本分组可能不同**。无界 rows 版按 planner 返回序切批,
    分页/发现版按 id 升序切批,同一批里装的行因此可能不是同一撮。落库的向量集合与
    embedder 的调用次数不受影响(每行的文本是它自己的,与同批邻居无关),这是刻意接受的
    差异——把它写成「每次调用的文本逐字相同」是不对的。"""
    size = max(1, int(repo._runtime.settings.embed_batch_size))
    return max(size, (_BACKFILL_PAGE_ROWS // size) * size)


def _hydrate_in_id_order(rows: list) -> list:
    """把主键 hydrate 回来的行按 ``id`` 升序重排。

    ``*_texts_by_ids`` 走 ``id IN (...)`` / ``id=ANY(...)``,SQL 对这类查询**不保证行序**
    (SQLite 按主键 b-tree 序、PG 按它选中的访问路径),而发现查询是 ``ORDER BY id``。
    不重排的话「哪几行进同一次 embedder 调用」会随后端和执行计划漂;重排之后页边界与
    批内容确定、可复现,与 keyset 分页版逐字同序。"""
    return sorted(rows, key=lambda r: str(r["id"]))


def _backfill_vectors_job(repo, notebook_id: str) -> None:
    """后台补齐该 notebook 缺失的 chunk + element 向量(只补缺失、幂等)。EMBED 未配则跳过。

    ⚠ **逐源经 hold_source_chunk_lock 持 P1.5 的分块锁、锁内读→嵌**(codex P1):element/chunk id
    在 reparse 时复用,并发补齐若在锁外读到旧代行、embedding 后在替换之后才提交,会给新文本挂上
    **永久陈旧向量**(后续缺向量检查也发现不了——向量「在」,只是内容旧)。守卫在持锁期间同时登记
    活跃租约,使 process_source 不会在本方仍持锁时把锁 pop 掉(codex 第2轮 P1:否则后续 reparse 另建
    新锁、互斥失效;backfill-only 源还会锁泄漏)。锁内才读缺失行,故读到的行在补齐提交前不会被
    reparse 换掉——彻底关掉「快照后才开始 reparse」的残留窗口。best-effort:一源失败不拦其余。

    源发现只用**轻量 DISTINCT source_id 查询**、且只查已配的 workload(codex 第2轮 P1:大库上
    把每行全文物化进内存仅为收 source_id 会 GB 级/OOM,还会白扫未配的那侧)。

    ⚠ 锁内是 **一次发现 + 按页主键 hydrate**(审计批4 + 其评审修订):
      - 无界的 ``missing_*_embedding_rows`` 会把该源每一行的**全文**一次性读进内存(2 万
        元素的手册就是几百 MB,而这条命令的用途恰恰是修大库)——所以正文必须按页取;
      - 但按页**重跑发现查询**(``missing_*_embedding_page`` 的 keyset)更糟:本仓库从不对
        生产库跑 ``ANALYZE``,``id > ?`` 在无统计信息时被选成整表主键区间扫、``source_id``
        降级成残余过滤(EXPLAIN 实测),21k 行的源 = 43 页 × 43 次全扫;
      - 故发现只跑**一次**、只投影 id(``missing_*_embedding_ids``,判据与 rows 版逐字一致,
        几十字节 × 21k 的内存可忽略),正文再按页经 ``*_texts_by_ids`` 主键取回。扫描回到
        一次,正文驻留仍按页有界。
    写入的向量集合逐位不变:每行**恰好被尝试一次**(id 列表内不重复、按序消费),嵌入失败
    的行仍是本轮跳过、下轮再补;页大小是 ``embed_batch_size`` 的整数倍(见
    ``_backfill_page_rows``),故 embedder 调用次数与不分页时相同——**分组**可能不同,理由
    见该函数 docstring。
    hydrate 回来的行按发现序重排(``IN`` 不保证行序),保证页边界与批内容可复现。
    发现查询 ``missing_*_vector_source_ids`` 保持原样:它只投影 DISTINCT source_id、不物化正文,
    其 btrim/TRIM 全表评估是**判据本身**(与 rows/count/page/ids 四处逐字一致),没有索引可
    依托,也省不掉这一次扫描——登记为已知代价,不在本次改动范围。"""
    repo.require_indexing_pipeline_write(notebook_id)
    mnt = repo.maintenance
    ingestion = repo._runtime.source_ingestion
    chunk_ok = repo.configured("chunk_embedding")
    elem_ok = repo.configured("source_element_embedding")
    if not (chunk_ok or elem_ok):
        return
    # 廉价定位「有缺失向量的源」:只取 DISTINCT source_id(不物化正文),且只查已配 workload。
    # 真正补齐在锁内按 only_source_id 重新发现(发现到的才是待嵌的行)。
    sources: set[str] = set()
    if chunk_ok:
        sources |= set(mnt.missing_chunk_vector_source_ids(notebook_id))
    if elem_ok:
        sources |= set(mnt.missing_element_vector_source_ids(notebook_id))
    page = _backfill_page_rows(repo)
    for source_id in sources:
        with ingestion.hold_source_chunk_lock(source_id):
            # Recheck after queueing and again at each source-generation lock;
            # a pipeline switch aborts the job rather than being swallowed as
            # one source's best-effort embedding failure.
            repo.require_indexing_pipeline_write(notebook_id)
            try:
                if chunk_ok:
                    ids = mnt.missing_chunk_embedding_ids(
                        notebook_id, only_source_id=source_id
                    )
                    for start in range(0, len(ids), page):
                        rows = _hydrate_in_id_order(
                            mnt.chunk_texts_by_ids(ids[start:start + page])
                        )
                        if not rows:
                            continue
                        mnt.embed_chunks_batch(
                            notebook_id,
                            [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows],
                        )
            except Exception:  # noqa: BLE001 — 后台 job 自负错误,一源失败不拦其余
                pass
            try:
                if elem_ok:
                    ids = mnt.missing_element_embedding_ids(
                        notebook_id, only_source_id=source_id
                    )
                    for start in range(0, len(ids), page):
                        rows = _hydrate_in_id_order(
                            mnt.element_texts_by_ids(ids[start:start + page])
                        )
                        if not rows:
                            continue
                        mnt.embed_elements_batch(
                            notebook_id,
                            [{"element_id": r["id"], "source_id": r["source_id"], "text": r["text"]} for r in rows],
                        )
            except Exception:  # noqa: BLE001
                pass


@router.post(
    "/notebooks/{notebook_id}/backfill-vectors",
    response_model=RepairScheduledResult,
    dependencies=[Depends(require_notebook_capability("sources:write"))],
)
def backfill_vectors(notebook_id: str) -> RepairScheduledResult:
    """体检修复(H4 缺 chunk 向量 / H5 缺 element 向量):后台补齐该 notebook 的缺失向量
    (只补缺失、幂等,仅 embedding、不动解析/KG)。用户点触发、非自动(承 efficiency-first:
    凡调 embedding 的修复不自动)。补完后 H4/H5 计数下降,前端重拉 checkup 反映。
    用完整 facade(``repository()``)——backfill 需要 maintenance/configured(BatchIngestRepository)。"""
    repo = repository()
    repo.require_indexing_pipeline_write(notebook_id)
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
    if not notebook_access_repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员 ∪ 有效授权边
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().source_elements(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get(
    "/sources/{source_id}/elements-page", response_model=PaginatedSourceElements
)
def source_elements_page(
    source_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=100),
    anchor_element_id: str = Query("", max_length=200),
    user: UserProfile = Depends(get_current_user),
) -> PaginatedSourceElements:
    if not notebook_access_repository().user_can_read_source(source_id, user.id):
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return source_repository().source_elements_page(
            source_id, offset, limit, anchor_element_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


# --- 参与集内的代理读取(挂载参考库的来源详情 / 元素 / 图片资产)-------------
#
# 「挂载公共参考库 ≠ 获得该库的直接成员权限」是红线:上面那对 `/sources/{id}` 端点
# 是 owner∪member 口径,挂载方对被挂库通常两者都不是,直连必 404。但 Ask 的清单结果卡
# 与引用会**合法地**列出参与库(active + 有效挂载的参考库)里的条目——用户看得到条目,
# 却点不开来源、看不到图,是个死角。
#
# 解法与「问答引用定位图谱节点」红线同构(kg_routes 的 objects/{id}/neighbors|context):
#   ① 浏览器**始终**只用当前 active notebook 过权限(require_notebook_read);
#   ② 后端只在该 active notebook 的**有效** participant 集内解析目标资源,并内部代理读取。
# 于是权限判定完全不依赖「用户是不是被挂库的成员」,也绝不因为挂了一个库就把该库的直接
# 成员权限发出去:被挂库易主/降级/正在深拷贝时挂载边即刻失效(mount_sql 的实时判定),
# 这两个端点当场回到 404。
#
# deny by default:目标资源自己声明它属于哪个 notebook(sources.notebook_id /
# notebook_assets.notebook_id),不在 participant 集内一律 404(与本仓库「不泄露存在性」
# 的惯例一致,不区分「不存在」与「无权」)。参与集查询是一次有界的挂载边 join,不随库大小增长。
#
# 判据本体(下面两个不带下划线的函数)刻意不依赖 FastAPI,也不自己去取参与集——它们
# 收一个 `participant_notebook_ids` 可调用对象。理由是这条合同现在有**第二个**消费方:
# MCP 的 `get_cited_element`(外部 Agent 把 Ask 回执里的 source_id/element_id 解引用回
# 原文),它没有 Request、没有 HTTPException,拿到的是注入的 repository 而不是 deps 里的
# 全局 port。让两处各写一遍谓词就是这份红线最典型的失效方式,所以判据只写一次,
# 「怎么取参与集」和「不满足时抛什么」留给各自的调用方。
# ⚠ 取参与集的入口仍然只有 `participant_notebook_ids`(唯一定义点 mount_sql.py);
# 这个参数是**注入取数口**,不是允许调用方另写一份挂载谓词的口子。
def in_participant_scope(
    notebook_id: str,
    owner_notebook_id: str,
    participant_notebook_ids: Callable[[str], Sequence[str]],
) -> bool:
    """``owner_notebook_id`` 是否在 ``notebook_id`` 的有效参与集内。

    同库先短路:participant 集首项恒为 active 自身,所以「资源就属于当前库」这个
    绝对主流的路径(本库来源、本库图片,一张图一次请求)一次挂载边 join 都不用付。
    """
    return owner_notebook_id == notebook_id or owner_notebook_id in (
        participant_notebook_ids(notebook_id)
    )


def source_readable_in_participant_scope(
    notebook_id: str,
    detail: SourceDetail,
    participant_notebook_ids: Callable[[str], Sequence[str]],
) -> bool:
    """``detail`` 这份来源可否作为 ``notebook_id`` 的参与集资源被代理读取。

    两道判据,顺序固定:先参与集,再跨库隐藏合成源。
    """
    if not in_participant_scope(
        notebook_id, detail.notebook_id, participant_notebook_ids
    ):
        return False
    # ⚠ 跨库时再挡一道隐藏合成源。集合地图/枚举**刻意**把 memory/knowhow 的物理 source
    # 行算进作用域(`source_change_signal_rows` 的原话:数的是检索能够到的东西,不是来源
    # 面板显示的东西),所以一个清单条目原则上可以带着这类 source_id 出现在跨库结果里,
    # 用户一点「查看来源」,`/elements` 就会把整条合成源摊开——包括没被枚举到的部分,而
    # Memory 是**按创建者私有**的。当前这条路径实际走不通(knowhow 只写 `knowhow_cell`
    # 元素、不在可枚举白名单里,Memory 投影根本不写 source_elements),但那是别处的事实,
    # 不该被这里的授权判断依赖 —— deny by default 更便宜也更稳。
    # 同库刻意不挡:那是既有 `/sources/{id}` owner∪member 路径逐字不变的行为,收紧它属于
    # 另一件事。图片资产端点同样不挡:knowhow 单元格图片是普通内容资产,本就随挂载库的
    # 内容参与检索,与「文档视图对合成源没有意义」不是一回事。
    return not (
        detail.notebook_id != notebook_id and detail.type in _HIDDEN_SOURCE_TYPES
    )


def _participant_ids(active_notebook_id: str) -> list[str]:
    """本模块(HTTP 侧)的参与集取数口 —— deps 的全局 port。"""
    return notebook_store_port().participant_notebook_ids(active_notebook_id)


def _in_participant_scope(notebook_id: str, owner_notebook_id: str) -> bool:
    return in_participant_scope(notebook_id, owner_notebook_id, _participant_ids)


def _participant_scoped_source(notebook_id: str, source_id: str) -> SourceDetail:
    """返回 ``source_id`` 的详情,前提是它属于 ``notebook_id`` 的有效参与集;否则 404。

    participant 集首项恒为 active notebook 自身,所以同库来源走的是同一条路径——
    调用方不需要先判断「跨不跨库」再选端点。
    """
    try:
        detail = source_repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")
    if not source_readable_in_participant_scope(
        notebook_id, detail, _participant_ids
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    return detail


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}",
    response_model=ScopedSourceDetail,
    dependencies=[Depends(require_notebook_read)],
)
def get_source_in_scope(notebook_id: str, source_id: str) -> ScopedSourceDetail:
    # ⚠ 响应模型不是 SourceDetail:代理读取会把参考库的来源交给一个对该库既非 owner
    # 也非成员的用户,`file_path`(后端绝对路径)和 `error_message`(原始异常串,同样
    # 可能带绝对路径)不能跟着出去。理由与取舍见 ScopedSourceDetail 的 docstring。
    return ScopedSourceDetail.of(_participant_scoped_source(notebook_id, source_id))


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}/elements",
    response_model=List[SourceElement],
    dependencies=[Depends(require_notebook_read)],
)
def source_elements_in_scope(notebook_id: str, source_id: str) -> List[SourceElement]:
    # 先做范围校验再读元素:元素是整源的行集合,越权请求不应该先把它捞出来。
    _participant_scoped_source(notebook_id, source_id)
    try:
        return source_repository().source_elements(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get(
    "/notebooks/{notebook_id}/sources/{source_id}/elements-page",
    response_model=PaginatedSourceElements,
    dependencies=[Depends(require_notebook_read)],
)
def source_elements_page_in_scope(
    notebook_id: str,
    source_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(40, ge=1, le=100),
    anchor_element_id: str = Query("", max_length=200),
) -> PaginatedSourceElements:
    _participant_scoped_source(notebook_id, source_id)
    try:
        return source_repository().source_elements_page(
            source_id, offset, limit, anchor_element_id
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    # 同 parse_source 上面的注释:notebook_id 不在 URL 上,守卫在函数体内先反查
    # 再按同一张能力表判 "sources:write"。
    notebook_id = notebook_access_repository().source_notebook_id(source_id)
    if notebook_id is None or not notebook_capability_allowed(
        "sources:write", notebook_id, user.id
    ):
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
@router.post("/notebooks/{notebook_id}/assets", dependencies=[Depends(require_notebook_capability("sources:write"))])
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
    # 路径里的 notebook_id 是**请求方当前的 active notebook**(权限就按它判,见
    # require_notebook_read),不是「资产必须正好属于这个库」。资产自己声明所属库,
    # 只要那个库在 active notebook 的有效参与集内就代理读取——参与集首项恒为 active
    # 自身,所以本库资产(knowhow 单元格图片、来源插图)的既有行为逐字不变,只是额外
    # 放行了「有效挂载的参考库」这一档,与上面来源详情/元素的代理读取同一条口径。
    asset = repository().get_notebook_asset(asset_id)
    if asset is None or not _in_participant_scope(notebook_id, asset["notebook_id"]):
        raise HTTPException(status_code=404, detail="Asset not found")
    path = _asset_service().path_for(asset)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")
    # ⚠ 代理来的跨库资产不进浏览器缓存。挂载有效期内取回的图片若按 max-age=86400
    # 缓存,取消挂载/降级/易主之后重新打开会由缓存直接命中、根本到不了上面那次参与集
    # 判定——「挂载边一失效就当场 404」这条合同会被静默架空整整一天。本端点不做条件
    # 请求(没有 If-None-Match 处理),`no-cache` 的重验同样要回源整份字节,与 `no-store`
    # 没有带宽差别,故直接取语义最不含糊的那个。本库资产不涉及挂载生命周期,保持原有
    # 长缓存不动——它才是「一张图一次请求」的高频路径。
    cacheable = asset["notebook_id"] == notebook_id
    return FileResponse(
        path,
        media_type=asset["mime"],
        headers={"Cache-Control": "private, max-age=86400" if cacheable else "no-store"},
    )


@router.post(
    "/notebooks/{notebook_id}/paper-meta/backfill",
    dependencies=[Depends(require_notebook_capability("sources:write"))],
)
def backfill_paper_metadata(notebook_id: str) -> dict:
    """补抽该 notebook 缺论文元数据的源(后台线程,幂等可续跑)。返回排队数;
    LLM 未配置 409。owner 门控由 require_notebook_capability("sources:write")
    承担(P0 阶段解析到 owner-only,非 owner 404)。"""
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
