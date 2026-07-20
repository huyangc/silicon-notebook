from fastapi import APIRouter, Depends

from app.api.deps import (
    content_overview_service,
    get_current_user,
    require_notebook_read,
)
from app.models.schemas import NotebookContentOverview, UserProfile


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
