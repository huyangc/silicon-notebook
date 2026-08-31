import asyncio
import threading
from typing import Any, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.deps import (
    _bearer_token,
    admin_query_repository,
    extension_toggle_repository,
    get_current_user,
    model_status_service,
    identity_repository,
    repository,
    require_notebook_capability,
    user_error,
)
from app.core.cache import CacheAdmin, make_cache_backend
from app.core.config import get_settings
from app.models.admin import (
    ActivityAsk,
    ActivityReport,
    ActivityResponse,
    ActivitySource,
    AdminExtension,
    AdminExtensionContribution,
    AdminExtensionRuntimeResult,
    AdminExtensionRuntimeUpdate,
    AdminExtensionUiContribution,
    AdminExtensionsResponse,
    AdminPasswordResetRequest,
    AdminPasswordResetResult,
    AdminUserNotebook,
    AdminUserRoleResult,
    AdminUserRoleUpdate,
    AdminUserUploadLimitResult,
    AdminUserUsage,
    AskDetail,
    CacheEvictRequest,
    CacheEvictResult,
    CacheStats,
    PromoteRequest,
    PromotionApproveResult,
    PromotionCandidate,
    PromotionRejectRequest,
    UploadLimitDefaultResult,
    UploadLimitDefaultUpdate,
    UploadLimitUpdate,
)
from app.models.identity import UserProfile
from app.models.model_services import ModelServiceStatusItem, ModelServicesStatus
from app.models.sources import PaginatedSources
from app.services.extension_toggles import (
    apply_admission_override,
    logger as extension_admission_logger,
    refresh_extension_admission,
)
from app.services.model_status import ModelStatusService
from app.services.source_display import source_display_title
from app.repositories.identity_errors import (
    BuiltinAdminDemotionError,
    BuiltinAdminPasswordError,
    SelfDemotionError,
)
from app.services.pending_bus import pending_bus


router = APIRouter()


@router.post(
    "/admin/model-services/{service_id}/test",
    response_model=ModelServiceStatusItem,
)
def test_system_model_service(
    service_id: str,
    user: UserProfile = Depends(get_current_user),
    status_service: ModelStatusService = Depends(model_status_service),
) -> ModelServiceStatusItem:
    if user.role != "admin":
        raise user_error(403, "仅管理员可测试模型服务")
    try:
        return status_service.test_one(service_id, actor_id=user.id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Model service not found")


@router.post(
    "/admin/model-services/test-all",
    response_model=ModelServicesStatus,
)
def test_all_system_model_services(
    user: UserProfile = Depends(get_current_user),
    status_service: ModelStatusService = Depends(model_status_service),
) -> ModelServicesStatus:
    if user.role != "admin":
        raise user_error(403, "仅管理员可测试模型服务")
    return status_service.test_all(actor_id=user.id)


# --- Governance: promotion queue (Track F) ------------------------------


@router.post(
    "/notebooks/{notebook_id}/knowledge/{knowledge_id}/promote",
    response_model=PromotionCandidate,
    status_code=201,
    dependencies=[Depends(require_notebook_capability("knowledge:write"))],
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
    except (KeyError, PermissionError):
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
    except (KeyError, PermissionError):
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
    return [
        AdminUserUsage(
            **row,
            is_online=row["id"] in online,
            role_mutable=row["id"] not in {"user-local", user.id},
        )
        for row in rows
    ]


@router.patch("/admin/users/{user_id}/role", response_model=AdminUserRoleResult)
def update_admin_user_role(
    user_id: str,
    payload: AdminUserRoleUpdate,
    user: UserProfile = Depends(get_current_user),
) -> AdminUserRoleResult:
    """Grant or revoke administrator access. Existing sessions observe it next request."""
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理用户权限")
    try:
        result = identity_repository().set_user_role(user.id, user_id, payload.role)
    except PermissionError:
        raise user_error(403, "仅管理员可管理用户权限")
    except KeyError:
        raise HTTPException(status_code=404, detail="User not found")
    except BuiltinAdminDemotionError:
        raise user_error(409, "内置管理员权限不可撤销")
    except SelfDemotionError:
        raise user_error(409, "不能撤销当前账户的管理员权限")
    return AdminUserRoleResult(**result)


@router.post(
    "/admin/users/{user_id}/reset-password", response_model=AdminPasswordResetResult
)
def reset_admin_user_password(
    user_id: str,
    payload: AdminPasswordResetRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> AdminPasswordResetResult:
    """管理员重置某用户密码；目标用户的浏览器会话全部吊销，需用新密码重新登录。
    auth_optional 部署的匿名回退(无 token 即视作种子管理员)不放行——改密是
    锁死型操作，当事人无法自救，必须由真实登录的管理员会话发起。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可重置用户密码")
    if not _bearer_token(request):
        raise user_error(403, "请先登录管理员账户再重置用户密码")
    if not payload.new_password.strip():
        raise user_error(400, "新密码不能为空")
    try:
        result = identity_repository().admin_reset_user_password(
            user.id, user_id, payload.new_password
        )
    except BuiltinAdminPasswordError:
        raise user_error(409, "内置管理员密码由部署配置决定，请修改环境变量后重启生效")
    except PermissionError:
        raise user_error(403, "仅管理员可重置用户密码")
    except KeyError:
        raise HTTPException(status_code=404, detail="User not found")
    return AdminPasswordResetResult(**result)


@router.get(
    "/admin/settings/upload-limit-default", response_model=UploadLimitDefaultResult
)
def get_upload_limit_default(
    user: UserProfile = Depends(get_current_user),
) -> UploadLimitDefaultResult:
    """当前全局默认「每笔记本文档数量上限」。仅 admin。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看文档上限设置")
    return UploadLimitDefaultResult(
        limit=identity_repository().global_document_limit_default()
    )


@router.patch(
    "/admin/settings/upload-limit-default", response_model=UploadLimitDefaultResult
)
def update_upload_limit_default(
    payload: UploadLimitDefaultUpdate,
    user: UserProfile = Depends(get_current_user),
) -> UploadLimitDefaultResult:
    """设置全局默认「每笔记本文档数量上限」。仅 admin。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改文档上限设置")
    try:
        result = identity_repository().set_global_document_limit_default(
            user.id, payload.limit
        )
    except PermissionError:
        raise user_error(403, "仅管理员可修改文档上限设置")
    except ValueError:
        raise user_error(400, "文档上限需在 1 到 100000 之间")
    return UploadLimitDefaultResult(**result)


@router.patch(
    "/admin/users/{user_id}/upload-limit", response_model=AdminUserUploadLimitResult
)
def update_admin_user_upload_limit(
    user_id: str,
    payload: UploadLimitUpdate,
    user: UserProfile = Depends(get_current_user),
) -> AdminUserUploadLimitResult:
    """给某用户设/清「每笔记本文档数量上限」覆盖值(limit=null 清除、回落全局默认)。
    仅 admin。返回改动后该用户的生效上限与是否仍为覆盖值。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理用户文档上限")
    try:
        result = identity_repository().set_user_document_limit_override(
            user.id, user_id, payload.limit
        )
    except PermissionError:
        raise user_error(403, "仅管理员可管理用户文档上限")
    except KeyError:
        raise HTTPException(status_code=404, detail="User not found")
    except ValueError:
        raise user_error(400, "文档上限需在 1 到 100000 之间")
    return AdminUserUploadLimitResult(**result)


@router.get("/admin/users/{user_id}/notebooks", response_model=List[AdminUserNotebook])
def list_admin_user_notebooks(user_id: str, user: UserProfile = Depends(get_current_user)) -> List[AdminUserNotebook]:
    """某用户名下笔记本详情。自己或 admin 可查——该端点只返回
    `notebooks WHERE created_by = user_id`,`user_id` 等于调用者自己时读的
    就是自己的笔记本,本来就该放行；活动视图左栏正是复用它展开自己的
    笔记本清单,「仅 admin」是历史遗留(该端点最初只服务 admin 总览页)。"""
    _require_self_or_admin(user, user_id)
    return [
        AdminUserNotebook(**row)
        for row in admin_query_repository().list_user_notebooks(user_id)
    ]


# --- 用户日志页多维度改版 · 「活动」视图(P1,仅 API 层) ----------------------
#
# 权限口径统一镜像 debug_logs._resolve_owner:user_id == 当前用户.id 放行
# (普通用户看自己),否则要求 user.role == "admin"。三个端点共用这一条。
# 真源见 docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md
# §4.2。


def _require_activity_enabled() -> None:
    """活动视图有自己独立的部署开关 `USER_ACTIVITY_VIEW_ENABLED`(默认开),
    与管 `/api/debug/logs/...`(JSONL 日志文件读取)的 `DEBUG_LOGS_ENABLED`
    是两件互不相关的事,不能共用一个判据。

    这三个端点管的是「活动视图能不能读到别人的提问原文与答案正文」——
    `/dev/logs` 的「活动」是**默认** tab,若门控用默认关闭的
    `DEBUG_LOGS_ENABLED`,普通部署一打开页面就 404,新特性等于不可用。
    因此这里改用默认开启的独立开关;关闭的口子留给介意跨用户内容可见性
    (自己或 admin 能读到别人的提问/答案原文)的部署。
    """
    if not getattr(get_settings(), "user_activity_view_enabled", True):
        raise HTTPException(status_code=404, detail="user activity view disabled")


def _require_self_or_admin(user: UserProfile, user_id: str) -> None:
    """自己或 admin 可查。现由 list_admin_user_notebooks 与三个活动端点共用,
    文案因此保持通用而非只提「活动记录」。"""
    if user.id == user_id or user.role == "admin":
        return
    raise user_error(403, "无权查看该用户的信息")


def _activity_source_item(row: dict) -> ActivitySource:
    """把 list_user_activity 的原始 source 行(带 title/file_name/is_paper/
    paper_title 四个原始列)合成出前端契约要的 display_title —— 论文标题优先,
    唯一实现是 source_display_title（`docs/product-and-api.md` 契约:所有为用户命名来源的路径
    共用同一份实现)。刻意保留 source_display_title 可能返回的空串,不在这里
    造占位符,前端已按空串处理。"""
    # Retained rows already carry the display title frozen immediately before
    # notebook deletion. Live rows continue through the canonical synthesizer.
    display_title = (
        row.get("display_title")
        if "display_title" in row
        else source_display_title(row)
    )
    return ActivitySource(
        id=row["id"],
        notebook_id=row["notebook_id"],
        created_at=row["created_at"],
        display_title=display_title or "",
        file_name=row.get("file_name") or "",
        source_type=row.get("source_type") or "",
        parse_status=row.get("parse_status") or "",
        status=row.get("status") or "",
        parse_failed=bool(row.get("parse_failed")),
        extraction_warning=row.get("extraction_warning") or "",
        parse_quality_warning=bool(row.get("parse_quality_warning")),
        paper_meta_status=row.get("paper_meta_status") or "",
        notebook_name=row.get("notebook_name") or "",
        notebook_deleted_at=row.get("notebook_deleted_at") or "",
        retained_until=row.get("retained_until") or "",
    )


def _activity_item_from_row(row: dict):
    kind = row["type"]
    if kind == "ask":
        return ActivityAsk(**row)
    if kind == "source":
        return _activity_source_item(row)
    if kind == "report":
        return ActivityReport(**row)
    raise AssertionError(f"unknown activity item type: {kind!r}")  # pragma: no cover


@router.get("/admin/users/{user_id}/activity", response_model=ActivityResponse)
def get_admin_user_activity(
    user_id: str,
    activity_type: Optional[Literal["ask", "source", "report"]] = Query(
        None, alias="activity_type"
    ),
    notebook_id: Optional[str] = Query(None),
    since: Optional[str] = Query(None),
    until: Optional[str] = Query(None),
    before_ts: Optional[str] = Query(None),
    before_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: UserProfile = Depends(get_current_user),
) -> ActivityResponse:
    """用户活动流:提问 / 来源 / 报告三类,时间倒序归并。自己或 admin 可查。"""
    _require_activity_enabled()
    _require_self_or_admin(user, user_id)
    try:
        raw = admin_query_repository().list_user_activity(
            user_id,
            activity_type=activity_type,
            include_inaccessible_questions=user.role == "admin",
            notebook_id=notebook_id,
            since=since,
            until=until,
            before_ts=before_ts,
            before_id=before_id,
            limit=limit,
        )
    except ValueError as exc:
        # 时间参数畸形。两个 store 对此**都**抛 ValueError(见 app.core.activity_time
        # 的契约:不静默忽略,悄悄丢掉一个 since 会返回比请求更宽的窗口)。这里必须接住
        # 它:不接的话 `?since=not-a-date` 是一条 500(实测,且不带 X-User-Message),
        # 前端只能显示「请重试」,然后无限重试一个永远不会成功的请求。
        #
        # 400 而不是 422:这是**查询参数**的取值不合法,FastAPI 的 422 留给它自己的
        # 签名级校验,两者混在一起会让前端分不清「我发错了」和「服务端校验器改了」。
        # 走 user_error 打 X-User-Message 头(红线:后端中文用户文案的唯一出口)。
        raise user_error(400, "时间范围或翻页位置不是有效的时间，请重新选择日期") from exc
    return ActivityResponse(
        items=[_activity_item_from_row(row) for row in raw["items"]],
        has_more=raw["has_more"],
        next_cursor=raw["next_cursor"],
    )


def _notebook_owned_by_user(user_id: str, notebook_id: str) -> bool:
    """口径:notebooks.created_by = user_id AND status != 'copying'。

    ⚠ 刻意**不**走 list_user_notebooks:那是一次全量用量聚合(1 条 notebooks 查询 +
    4 条覆盖该用户**全部**笔记本的 GROUP BY COUNT),只为判断一行的归属;而左栏每展开
    一个笔记本就发一次。判据本身是一行的事,用 notebook_exists_for_owner 一条带主键的
    SELECT 1 回答——那正是 list_user_activity 内部 owned 分支已经在用的同一条谓词
    (见两个 query_store 的 `SELECT 1 FROM notebooks WHERE id=? AND created_by=?
    AND status != 'copying'`),口径逐字相同,没有第二份拼写。
    """
    return admin_query_repository().notebook_exists_for_owner(notebook_id, user_id)


@router.get(
    "/admin/users/{user_id}/notebooks/{notebook_id}/sources",
    response_model=PaginatedSources,
)
def get_admin_user_notebook_sources(
    user_id: str,
    notebook_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: UserProfile = Depends(get_current_user),
) -> PaginatedSources:
    """某用户某笔记本的来源清单(活动视图左栏展开用)。自己或 admin 可查;
    notebook_id 不属于该用户时 404(不泄露存在性,不用 403)。"""
    _require_activity_enabled()
    _require_self_or_admin(user, user_id)
    if not _notebook_owned_by_user(user_id, notebook_id):
        raise HTTPException(status_code=404, detail="notebook not found")
    return repository().list_sources_page(notebook_id, offset, limit)


@router.get("/admin/users/{user_id}/asks/{job_id}", response_model=AskDetail)
def get_admin_user_ask_detail(
    user_id: str,
    job_id: str,
    user: UserProfile = Depends(get_current_user),
) -> AskDetail:
    """右栏「选中提问」详情。存活笔记本返回完整问答与推理轨迹；删除后的
    未到期活动只返回最小摘要。自己或 admin 可查；job_id 不属于该用户时 404。

    首次 ``ask_job_detail(job_id)`` 只完成目标用户与实时 notebook 访问校验。
    随后的 ``guarded_ask_detail`` 在 adapter 的删除协调事务内一次投影 job、
    trace 与至多一条 answer，并把生命周期锁保持到本响应对象组装完成；因此不会
    经 ``get_conversation`` 线性加载整段会话，也不会把删除前读到的答案带过删除
    提交边界。answered_at 仍以 answers.created_at 回填；没有 answer_id 或答案行
    时保持空，不编造时间或正文。
    """
    _require_activity_enabled()
    _require_self_or_admin(user, user_id)
    repo = repository()
    try:
        job = repo.ask_job_detail(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")

    def require_job_access(candidate: dict) -> None:
        if candidate["created_by"] != user_id:
            raise HTTPException(status_code=404, detail="ask job not found")
        if user.role != "admin" and not repo.user_can_read_notebook(
            candidate["notebook_id"], user.id
        ):
            raise HTTPException(status_code=404, detail="ask job not found")

    require_job_access(job)

    try:
        with repo.guarded_ask_detail(job_id) as snapshot:
            job = snapshot["job"]
            # Initial access validation ran before acquiring the guard so a
            # max-size=1 PostgreSQL pool never needs a nested connection while
            # the guarded transaction is held. Identity is immutable; the only
            # lifecycle transition relevant here is live→retained, which is
            # admin-only and must fail closed for self-service readers.
            if job["created_by"] != user_id or (
                user.role != "admin" and job.get("notebook_deleted_at")
            ):
                raise HTTPException(status_code=404, detail="ask job not found")
            answer_detail: "dict[str, Any] | None" = snapshot["answer_detail"]
            answer = answer_detail["payload"] if answer_detail is not None else None
            answered_at = (
                str(answer_detail["payload"].get("answered_at") or "")
                if answer_detail is not None
                else ""
            )
            return AskDetail(
                job_id=job["job_id"],
                notebook_id=job["notebook_id"],
                conversation_id=job.get("conversation_id") or "",
                question=job["question"],
                mode=job["mode"],
                status=job["status"],
                asked_at=job.get("asked_at") or "",
                answered_at=answered_at,
                error=job["error"],
                trace=job["trace"],
                answer=answer,
                notebook_name=job.get("notebook_name") or "",
                notebook_deleted_at=job.get("notebook_deleted_at") or "",
                retained_until=job.get("retained_until") or "",
            )
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")


@router.get("/admin/online")
async def list_online_users(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前在线用户 id 集合(持有实时流连接)。仅 admin,纯读内存零 DB。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看在线状态")
    return {"online_ids": sorted(pending_bus.online_user_ids())}


# --- 内容寻址缓存：只读现状 + 失效逃生口 ------------------------------------
#
# 缓存默认开、TTL 90 天、跨用户全局共享。TTL 之所以敢设这么长，正是因为有一个
# 显式的失效入口兜底：**同名模型的权重被替换**是唯一需要主动清理的场景（模型名
# 没变，缓存键因此不变，但同一段 prompt 的正确答案已经变了）。没有这两个端点时，
# evict_tag/clear/stats 全仓零调用方——能力存在但不可达，运维只能去删缓存文件，
# 而没人知道要去删。
#
# 用 isinstance(backend, CacheAdmin) 探测后再调用：只实现 get/put 的后端
# （Redis 把 TTL/LRU 交给服务端配置）是合法形态，不能假设运维能力一定在。


def _cache_admin(user: UserProfile):
    """取一个具备运维能力的缓存后端；不具备时如实拒绝。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理响应缓存")
    try:
        backend = make_cache_backend(get_settings())
    except Exception:
        # 缓存故障永不影响主流程——这里也一样，只是如实告诉管理员它起不来。
        raise user_error(503, "响应缓存当前不可用，请稍后再试")
    if not isinstance(backend, CacheAdmin):
        raise user_error(501, "当前响应缓存的存储后端不支持查看与清理")
    return backend


@router.get("/admin/cache", response_model=CacheStats)
def get_cache_stats(user: UserProfile = Depends(get_current_user)) -> CacheStats:
    """响应缓存现状：总量、命中率、按模型分布。仅 admin，纯读。

    by_tag 按模型名分组——决定「换了哪个模型服务、该清哪一份」时看的就是它。
    命中/未命中是**进程内**计数（重启归零）：缓存实例按路径记忆化，所以这是全进程
    的口径，而不是某一个 LLM client 自己的那一份。
    """
    backend = _cache_admin(user)
    stats = backend.stats()
    return CacheStats(
        enabled=get_settings().llm_cache_enabled,
        admin_supported=True,
        **stats,
    )


@router.post("/admin/cache/evict", response_model=CacheEvictResult)
def evict_cache(
    payload: CacheEvictRequest, user: UserProfile = Depends(get_current_user)
) -> CacheEvictResult:
    """按模型名清理缓存，或整库清空。仅 admin。

    换模型服务（尤其是同名模型换了权重）后应当按 tag 清掉那一份：模型名没变，
    缓存键就没变，旧答案会一直被回放到 TTL 到期为止。
    tag 与 clear_all 必须显式二选一，避免一次手滑清空整库。
    """
    backend = _cache_admin(user)
    tag = payload.tag.strip()
    # tag 与 clear_all 契约上二选一。两个都给是自相矛盾的请求——一个本想按 model 清
    # 单份、却带上了全清标志的格式错误请求，绝不能被静默解读成「走 clear_all 清空整库」
    # 并触发一次昂贵的全量重填。先拒绝、不执行任何清理。
    if payload.clear_all and tag:
        raise user_error(400, "tag 与 clear_all 只能二选一：指定模型名清单份，或只传 clear_all 清空全部")
    if payload.clear_all:
        return CacheEvictResult(evicted=backend.clear(), scope="all")
    if not tag:
        raise user_error(400, "请指定要清理的模型名，或明确选择清空全部缓存")
    return CacheEvictResult(evicted=backend.evict_tag(tag), scope=tag)


# --- 扩展拓扑（装载态只读）+ 运行时开关（写 DB、本进程立即生效）--------------


def _extension_admin_projection(request: Request):
    # getattr with a default, not attribute access: Starlette's ``State``
    # raises AttributeError for an unset key, so ``state.x`` would bypass the
    # guard below entirely and surface as a 500 with a Starlette-worded message
    # instead of this module's own. An app built without the extension wiring
    # must fail here, deliberately.
    projection = getattr(request.app.state, "extension_admin_projection", None)
    if not callable(projection):
        raise RuntimeError("application extension admin projection is unavailable")
    return projection()


@router.get("/admin/extensions", response_model=AdminExtensionsResponse)
def list_admin_extensions(
    request: Request, user: UserProfile = Depends(get_current_user)
) -> AdminExtensionsResponse:
    """已加载的扩展拓扑 + 运行时开关现状。仅 admin。

    两层语义，不同源，按行合成：

    * 装载拓扑（id/version/trust/display_name/contributions/ui_contributions）
      仍是启动冻结的白名单投影——绝不返回模块路径/文件路径/settings 值/
      reason/异常文本；TOML `enabled = false` 或未点名的插件从未进入
      registry，因此不出现在这里，这一层本身依旧没有 `enabled` 字段（真源见
      ``app/extensions/admin_projection.py``）。
    * 运行时开关（runtime_enabled/runtime_updated_by/runtime_updated_at）是
      另一层：从 `extension_runtime_toggles` 表**现读**（不是进程内 admission
      快照——管理页要看到的是 DB 里的真值，不是这个进程恰好收敛到的那一刻），
      按 plugin_id 与上面的装载行合成。builtin 恒为
      `None, None, None`（只读，不可开关）；已装载的 deployment 插件无开关行
      时是 `True, None, None`（无行 = 启用），有行则如实返回。

    先取投影、只在其中确实出现过 `trust == "deployment"` 行时才查
    ``list_extension_runtime_toggles()``：出厂默认零 deployment 插件是最常见
    的部署形态，这条路由不该为它凭空多一次 DB 查询（同一教义见
    ``app.services.startup_warmup._deployment_plugins_are_loaded`` 与
    ``app.extensions.http_router.collect_plugin_router_specs`` 的零贡献者
    早退——"零插件是出厂默认，必须不花一分钱"）。
    """
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看已加载的扩展")
    rows = tuple(_extension_admin_projection(request))
    toggles: dict[str, dict] = {}
    if any(row.trust == "deployment" for row in rows):
        toggles = {
            row["plugin_id"]: row
            for row in extension_toggle_repository().list_extension_runtime_toggles()
        }
    extensions: list[AdminExtension] = []
    for row in rows:
        runtime_enabled: Optional[bool] = None
        runtime_updated_by: Optional[str] = None
        runtime_updated_at: Optional[str] = None
        if row.trust == "deployment":
            toggle = toggles.get(row.id)
            if toggle is None:
                runtime_enabled = True  # 无行 = 启用
            else:
                runtime_enabled = toggle["enabled"]
                runtime_updated_by = toggle["updated_by"]
                runtime_updated_at = toggle["updated_at"]
        extensions.append(
            AdminExtension(
                id=row.id,
                version=row.version,
                trust=row.trust,
                display_name=row.display_name,
                contributions=[
                    AdminExtensionContribution(
                        id=item.id, point=item.point, kind=item.kind
                    )
                    for item in row.contributions
                ],
                ui_contributions=[
                    AdminExtensionUiContribution(
                        id=item.id, slot=item.slot, capability=item.capability
                    )
                    for item in row.ui_contributions
                ],
                runtime_enabled=runtime_enabled,
                runtime_updated_by=runtime_updated_by,
                runtime_updated_at=runtime_updated_at,
            )
        )
    return AdminExtensionsResponse(extensions=extensions)


# 进程内「写库 + 发布」的串行闸（用途与取舍见 handler 内注释）。
_RUNTIME_TOGGLE_WRITE_LOCK = threading.Lock()


@router.patch(
    "/admin/extensions/{plugin_id}", response_model=AdminExtensionRuntimeResult
)
def update_admin_extension_runtime(
    plugin_id: str,
    payload: AdminExtensionRuntimeUpdate,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> AdminExtensionRuntimeResult:
    """开/关一个已装载的 deployment 插件。仅 admin，装载拓扑不受影响。

    ``plugin_id`` 必须落在「已装载 deployment 插件 id 集合」内——从这次请求
    自己现读的装载投影取（与 GET 同一份真源），不是 store 认识的任何东西。
    这一步同时挡掉 builtin、TOML 里未点名/已停用的插件、以及任何拼错的 id，
    store 的 ValueError（空/纯空白 plugin_id）与无界写入面因此在这条路由下
    不可达——不会有人对着一个从未装载过的 id 在表里堆出行来。

    成功写行后立即 ``refresh_extension_admission``，让本进程的 registry 闸
    与插件路由闸当次生效，调用方（多半就是刚点了这个开关的管理员）不必等
    低频轮询收敛。两个管理员并发各改一次（甚至改同一个 plugin_id）不会互相
    回滚彼此的发布——较晚落地的那次读到的已经是较新的库内状态，刷新按序发布，
    本进程最终停在与 DB 一致的那个值上，不会因为两次刷新的先后交错而倒退回
    更早的快照。刷新失败是防御性的：写已经落库，只是这一个进程的内存快照要
    等下一轮轮询才追上——见下面的 ``except`` 分支。
    """
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理扩展运行时开关")
    loaded_deployment_ids = {
        row.id
        for row in _extension_admin_projection(request)
        if row.trust == "deployment"
    }
    if plugin_id not in loaded_deployment_ids:
        raise user_error(404, "该扩展不存在或不支持运行时开关")
    store = extension_toggle_repository()
    # 「写库 + 发布」在本进程内串行化（codex #635 R5 P2）：两个管理员并发改同
    # 一个插件、且较早那次的回读恰好失败时，若不加锁，较早请求的本地回退会
    # 拿到更新的令牌、把较新裁决盖回旧值一个刷新周期。锁内提交序 = 发布序，
    # 这一类倒置在进程内就不可能发生；跨进程的并发写本就按轮询收敛（成文
    # 契约）。管理员开关是低频操作，这把锁没有吞吐代价。
    with _RUNTIME_TOGGLE_WRITE_LOCK:
        try:
            row = store.set_extension_runtime_enabled(
                plugin_id, payload.enabled, user.id
            )
        except PermissionError:
            # 理论上不可达——上面已经检过角色——但绝不能把它泄漏成一条 500：一个
            # 管理员会话在两次检查之间被别的会话降权，是可以想象的竞态，答案必须
            # 和上面同一句话，而不是一条陌生的堆栈。
            raise user_error(403, "仅管理员可管理扩展运行时开关")
        try:
            refresh_extension_admission(store)
        except Exception as exc:
            # 写路径的取舍：这次写已经落库，只是发布前的整表回读失败了。不能
            # 就此返回成功却让本进程继续放行该插件（那会违背「处理这次写的
            # 进程立即生效」的成文保证），也不该把一次数据库抖动变成管理员
            # 眼里的「操作失败」。所以退化为零 I/O 的本地快照运算：这次写的
            # 那一位是确定的持久事实，直接按它翻转进程快照；其它插件的位由
            # 下一次成功刷新收敛。plugin_id 不进日志——复用
            # app.services.extension_toggles 的既有口径，那是运营者的数据。
            # 异常同理只记类名不记文本/traceback：store 故障的异常文本可能
            # 内嵌 DSN 或私有路径，扩展面日志家族的纪律是 content-free
            # （AGENTS.md）。
            apply_admission_override(plugin_id, payload.enabled)
            extension_admission_logger.warning(
                "extension admission refresh read failed after an admin "
                "runtime-toggle write (%s); the write is saved and this "
                "process applied it locally, other rows converge on the "
                "background refresher's next tick",
                type(exc).__name__,
            )
    return AdminExtensionRuntimeResult(
        plugin_id=row["plugin_id"],
        runtime_enabled=row["enabled"],
        runtime_updated_by=row["updated_by"],
        runtime_updated_at=row["updated_at"],
    )
