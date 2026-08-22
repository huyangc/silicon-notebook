import asyncio
import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    _bearer_token,
    admin_query_repository,
    get_current_user,
    identity_repository,
    model_service_binding_summary,
    model_status_service,
    notebook_catalog_repository,
    repository,
    user_error,
)
from app.core.config import SOURCE_UPLOAD_MAX_FILES_PER_BATCH, get_settings
from app.models.identity import (
    PasswordChangeRequest,
    SearchProfileUpdate,
    UiModeUpdate,
    UserProfile,
)
from app.repositories.identity_errors import (
    BuiltinAdminPasswordError,
    PasswordMismatchError,
)
from app.models.model_services import ModelServicesStatus
from app.models.notebooks import NotebookTemplate
from app.models.sources import DetectDocTypesRequest, DetectedDocType
from app.models.system import (
    SystemConfiguration,
    SystemExtensionContribution,
    SystemExtensionsResponse,
)
from app.services.model_status import ModelStatusService
from app.services.parser_registry import (
    SUPPORTED_SOURCE_EXTENSIONS,
    parser_engine_capabilities,
)
from app.services.pending_bus import pending_bus
from app.services.reasoning_retrieval import search_profile_wiring_active

#: 与 agent_profile_routes.py 的 ``_DISABLED_MESSAGE`` 同一措辞——两处都是
#: 「功能被总开关关闭」的同一类用户文案，保持逐字一致而非各写一份。
_SEARCH_PROFILE_DISABLED_MESSAGE = "这项功能当前未开启，暂时无法编辑"


router = APIRouter()


@router.get("/health")
def health(
    bindings: dict[str, bool] = Depends(model_service_binding_summary),
) -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        **bindings,
    }


@router.get("/model-services/status", response_model=ModelServicesStatus)
def get_system_model_services_status(
    _user: UserProfile = Depends(get_current_user),
    service: ModelStatusService = Depends(model_status_service),
) -> ModelServicesStatus:
    """Return local sanitized state only; opening the panel never probes upstream."""
    return service.snapshot()


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user


@router.patch("/me/ui-mode", response_model=UserProfile)
def update_my_ui_mode(
    payload: UiModeUpdate,
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """自助切换界面模式偏好("auto"|"advanced")；只写调用者自己的 user_profiles
    行，不做 admin 校验。合法值由 pydantic Literal 在到达这里之前已经拒绝。"""
    return identity_repository().set_user_ui_mode(user.id, payload.ui_mode)


@router.patch("/me/search-profile", response_model=UserProfile)
def update_my_search_profile(
    payload: SearchProfileUpdate,
    user: UserProfile = Depends(get_current_user),
) -> UserProfile:
    """自助编辑调用者自己的检索/回答风格偏好(Agentic Memory P3, T6)。只写调用者
    自己的 user_profiles 行,不做 admin 校验——与 ``update_my_ui_mode`` 同格。

    ``payload.model_fields_set`` 区分「请求体里没出现这个字段」(维持原值)与
    「出现且值为 null」(=清空该字段,交还给 T7 归纳 job 重新填);未出现的字段
    一律不进 ``fields`` 字典,不会被 ``set_user_search_profile`` 触碰。合法值域
    由 pydantic 的 Literal/自定义校验器在到达这里之前已经拒绝(422)。总闸关闭
    时拒绝写入(409)——与 ``GET /me`` 仍照常返回上次归纳/编辑的旧值不同,写入
    在关闭期间不该继续积累数据。"""
    if not search_profile_wiring_active(get_settings(), identity_repository()):
        raise user_error(409, _SEARCH_PROFILE_DISABLED_MESSAGE)
    fields = {name: getattr(payload, name) for name in payload.model_fields_set}
    if not fields:
        return user
    return identity_repository().set_user_search_profile(user.id, fields, origin="user")


@router.patch("/me/password", status_code=204)
def update_my_password(
    payload: PasswordChangeRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> None:
    """自助修改密码：校验当前密码后更新，并吊销本用户其他会话（保留当前会话）。
    空白新密码在这里就地拒绝，store 里的 ValueError 只是防御——不再宽 catch
    ValueError，避免把 verify 阶段的意外错误(如损坏的 salt)误报成「新密码不能为空」。"""
    if not payload.new_password.strip():
        raise user_error(400, "新密码不能为空")
    token = _bearer_token(request)
    try:
        identity_repository().change_user_password(
            user.id, payload.old_password, payload.new_password, keep_token=token or None
        )
    except BuiltinAdminPasswordError:
        raise user_error(409, "内置管理员密码由部署配置决定，请修改环境变量后重启生效")
    except PasswordMismatchError:
        raise user_error(400, "当前密码不正确")
    except KeyError:
        raise HTTPException(status_code=404, detail="User not found")


@router.get("/system/config", response_model=SystemConfiguration)
def system_configuration(
    _user: UserProfile = Depends(get_current_user),
) -> SystemConfiguration:
    """Small authenticated browser configuration surface.

    Keep this deliberately limited to non-sensitive values that need matching
    client behavior. Deployment environment names, paths, credentials, and
    unrelated Settings fields must never be reflected here.
    """
    settings = get_settings()
    return SystemConfiguration(
        source_upload_max_bytes=settings.source_upload_max_bytes,
        source_upload_max_files_per_batch=SOURCE_UPLOAD_MAX_FILES_PER_BATCH,
        supported_source_extensions=list(SUPPORTED_SOURCE_EXTENSIONS),
        parser_engines=parser_engine_capabilities(settings),
        report_max_sections=settings.report_max_sections,
        report_max_subqueries_per_section=(
            settings.report_max_subqueries_per_section
        ),
        user_activity_view_enabled=settings.user_activity_view_enabled,
        source_image_max_bytes=settings.mineru_max_image_bytes,
        source_image_max_per_source=settings.mineru_max_images_per_source,
        source_images_enabled=settings.mineru_return_images,
        agent_profile_enabled=settings.agent_profile_enabled,
        user_search_profile_enabled=settings.user_search_profile_enabled,
    )


@router.get("/system/extensions", response_model=SystemExtensionsResponse)
def system_extensions(
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> SystemExtensionsResponse:
    """Return live, metadata-only UI capability availability.

    The registry topology is startup-frozen, while each capability decision is
    evaluated for this request.  Internal capability names, reasons, paths,
    endpoints, credentials, and exception text never cross this boundary.
    """

    projection = request.app.state.extension_ui_projection
    if not callable(projection):
        raise RuntimeError("application extension UI projection is unavailable")
    return SystemExtensionsResponse(
        extensions=[
            SystemExtensionContribution(**row.__dict__)
            for row in projection(user)
        ]
    )


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
