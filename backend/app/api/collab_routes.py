from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    collaboration_service,
    get_current_user,
    notebook_access_repository,
    require_notebook_capability,
)
from app.domain.collaboration import PulseActor
from app.models.collab import ActivityResponse, PulseRequest, PulseResponse
from app.models.identity import UserProfile

router = APIRouter()


async def _pulse_access(notebook_id: str, user: UserProfile = Depends(get_current_user)):
    access = await run_in_threadpool(notebook_access_repository().notebook_access_level, notebook_id, user.id)
    if access is None:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return access


@router.post("/notebooks/{notebook_id}/pulse", response_model=PulseResponse)
def pulse(notebook_id: str, payload: PulseRequest, access=Depends(_pulse_access),
          user: UserProfile = Depends(get_current_user)):
    service = collaboration_service()
    actor = PulseActor(user.id, str(user.display_name or "").strip() or str(user.username or "").strip(),
                       {"edit": "editor", "read": "reader"}.get(access.level, access.level))
    return service.pulse.poll(notebook_id, actor, since=payload.since, visible=payload.visible,
                              pool_wait_ms=service.pool_wait.wait_ms())


@router.get("/notebooks/{notebook_id}/activity", response_model=ActivityResponse,
            dependencies=[Depends(require_notebook_capability("notebook:activity"))])
def activity(notebook_id: str, request: Request):
    # The guard already projected owner_id in its single authorization query.
    mark = next(mark for mark in request.state.collab_marks
                if mark.source == "guard" and mark.capability == "notebook:activity"
                and mark.notebook_id == notebook_id)
    return collaboration_service().recent_activity(notebook_id, mark.owner_id)
