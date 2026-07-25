from fastapi import APIRouter, Depends

from app.api.deps import (
    checkup_service,
    content_overview_service,
    get_current_user,
    require_notebook_read,
)
from app.models.identity import UserProfile
from app.models.content_overview import NotebookContentOverview
from app.models.checkup import CheckupItem, CheckupResponse


router = APIRouter()


@router.get(
    "/notebooks/{notebook_id}/analytics/content-overview",
    response_model=NotebookContentOverview,
    dependencies=[Depends(require_notebook_read)],
)
def notebook_content_overview(
    notebook_id: str,
    user: UserProfile = Depends(get_current_user),
) -> NotebookContentOverview:
    return content_overview_service().get(notebook_id, user.id)


@router.get(
    "/notebooks/{notebook_id}/checkup",
    response_model=CheckupResponse,
    dependencies=[Depends(require_notebook_read)],
)
def notebook_checkup(notebook_id: str) -> CheckupResponse:
    """流水线体检(P2):只读聚合 H2–H8 的损坏/待办信号。看板高频入口,`require_notebook_read`
    守卫(只读成员也能看)。与系统级 `/health`(API/LLM 状态)语义不同。响应用内部代号
    (H2/reparse 等),界面词由前端映射。"""
    result = checkup_service().run(notebook_id)
    return CheckupResponse(
        notebook_id=result.notebook_id,
        checked_at=result.checked_at,
        healthy=result.healthy,
        checks=[
            CheckupItem(code=c.code, count=c.count, sample=c.sample, fix=c.fix)
            for c in result.checks
        ],
    )
