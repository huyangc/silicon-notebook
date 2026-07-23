import asyncio
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    admin_query_repository,
    get_current_user,
    model_status_service,
    identity_repository,
    repository,
    require_notebook_access,
    user_error,
)
from app.models.admin import (
    AdminUserNotebook,
    AdminUserRoleResult,
    AdminUserRoleUpdate,
    AdminUserUploadLimitResult,
    AdminUserUsage,
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
from app.services.model_status import ModelStatusService
from app.repositories.identity_errors import (
    BuiltinAdminDemotionError,
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
