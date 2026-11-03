"""Named sharing ("点名共享", share-edit P1-T6): the owner names editors.

* ``GET /notebooks/{id}/collaborators`` -- read guard. The owner gets every
  named grant (``principal_type='user'``: editors and legacy named
  viewer/admin rows); a caller holding a named editor grant gets the editors'
  names only (decision D17, whatever else they hold); anyone else 404.
* ``PUT /notebooks/{id}/collaborators/{user_id}`` -- ``notebook:collaborate``
  (owner-only, decision D2). Makes the user an editor; a real change rings
  their bell once.
* ``DELETE /notebooks/{id}/collaborators/{user_id}`` -- no route guard,
  decided in the body by WHO is removed: someone else needs
  ``notebook:collaborate`` (the same 404 a missing notebook gets otherwise);
  the caller themself leaves through the acknowledged exit with exactly the
  membership exit's contract (``_EXIT_RESPONSES``) -- idempotent like
  ``DELETE .../membership``: a caller holding no named grant there (already
  left, never had one, the notebook is gone) gets 204 with nothing touched, or
  409 ``exit_disclosure_required`` (count 0) when they acknowledged Memory.
  That answer is the same for every notebook id, so it discloses nothing.
* ``GET /notebooks/{id}/collaborators/{user_id}/exit-disclosure`` -- the
  caller themself only (read guard): personal Memory and unadopted system
  suggestions counted apart; both 0 without a named grant.

Together with ``/grants`` (group and everyone grants, which never touch a
named row) every grant row of a notebook is listed and removable exactly once.
"""
from __future__ import annotations

from typing import Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import (
    get_current_user,
    group_repository,
    identity_repository,
    mark_pulse,
    memory_membership_service,
    notebook_access_repository,
    notebook_capability_owner,
    notebook_sharing_repository,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.api.notebook_routes import _EXIT_RESPONSES, _exit_incomplete
from app.models.collaborators import (
    CollaboratorItem,
    CollaboratorList,
    CollaboratorPutResult,
    EditorName,
)
from app.models.identity import UserProfile
from app.models.memory import MemberExitResult, MemoryExitDisclosure
from app.repositories.ports import (
    CollaboratorTargetInvalid,
    IdentityStorePort,
    NotebookIsPublicLibrary,
    NotebookNotFoundError,
)
from app.services.memory_service import (
    ExitDisclosureRequired,
    MemberExitFailed,
    MemberExitIncomplete,
)
from app.services.notebook_collaborators import NotebookCollaboratorService
from app.services.pending_bus import pending_bus

router = APIRouter()

_NOT_FOUND = "Notebook not found"


def _service() -> NotebookCollaboratorService:
    return NotebookCollaboratorService(
        groups=group_repository(),
        sharing=notebook_sharing_repository(),
        memory=memory_membership_service(),
        emit=pending_bus.emit,
    )


@router.get(
    "/notebooks/{notebook_id}/collaborators",
    response_model=CollaboratorList,
    dependencies=[Depends(require_notebook_read)],
)
def list_collaborators(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> CollaboratorList:
    access = notebook_access_repository().notebook_access_level(notebook_id, user.id)
    if access is not None and access.level == "owner":
        return CollaboratorList(view="owner", items=[
            CollaboratorItem(**{key: row[key] for key in CollaboratorItem.model_fields})
            for row in group_repository().list_user_grants(notebook_id)
        ])
    if group_repository().user_grant_role(notebook_id, user.id) == "editor":
        return CollaboratorList(view="editor", editors=[
            EditorName(**row) for row in group_repository().list_editor_names(notebook_id)
        ])
    raise HTTPException(status_code=404, detail=_NOT_FOUND)


@router.put(
    "/notebooks/{notebook_id}/collaborators/{user_id}",
    response_model=CollaboratorPutResult,
    dependencies=[Depends(require_notebook_capability("notebook:collaborate"))],
)
def put_collaborator(
    notebook_id: str, user_id: str, user: UserProfile = Depends(get_current_user)
) -> CollaboratorPutResult:
    try:
        result = _service().grant_editor(
            notebook_id, owner_id=user.id, target_user_id=user_id
        )
    except NotebookNotFoundError:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    except NotebookIsPublicLibrary:
        raise user_error(409, "公共知识库对所有人只读，不能添加编辑者")
    except CollaboratorTargetInvalid:
        raise user_error(400, "找不到这个用户，或不能把他设为编辑者")
    grant = result["grant"]
    return CollaboratorPutResult(
        grant=CollaboratorItem(**{key: grant[key] for key in CollaboratorItem.model_fields}),
        changed=result["changed"],
        previous_role=result["previous_role"],
    )


@router.delete(
    "/notebooks/{notebook_id}/collaborators/{user_id}",
    response_class=Response,
    responses=_EXIT_RESPONSES,
)
def delete_collaborator(
    notebook_id: str,
    user_id: str,
    acknowledged_memory_count: Optional[int] = Query(default=None, ge=0),
    user: UserProfile = Depends(get_current_user),
    request: Request = None,
) -> Response:
    service = _service()
    if user_id != user.id:
        owner_id = notebook_capability_owner("notebook:collaborate", notebook_id, user.id)
        if owner_id is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND)
        if not service.revoke(notebook_id, user_id):
            raise user_error(404, "这个人不是这本笔记本的点名协作者")
        mark_pulse(request, notebook_id, "notebook:collaborate", owner_id, user)
        return Response(status_code=204)
    # Hold the owner identity before the acknowledged exit can remove the
    # caller's final read edge. A successful no-op is not a committed exit.
    access = notebook_access_repository().notebook_access_level(notebook_id, user.id)
    try:
        outcome = service.leave(notebook_id, user.id, acknowledged_memory_count)
    except ExitDisclosureRequired as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "exit_disclosure_required", "memory_count": exc.memory_count,
                    "suggestion_count": exc.suggestion_count},
        )
    except MemberExitIncomplete as exc:
        raise _exit_incomplete(409, exc)
    except MemberExitFailed as exc:
        raise _exit_incomplete(503, exc)
    if outcome.ended:
        try:
            # Access can be granted between the earlier read and the claim.
            # Only a committed exit permits recovering the owner after losing
            # that edge; no-op/missing-notebook exits reveal nothing.
            owner_id = access.owner_id if access is not None else cast(IdentityStorePort, identity_repository()).notebook_owner(notebook_id)[0] or ""
            mark_pulse(request, notebook_id, "notebook:collaborate", owner_id, user)
        except Exception:
            # Observer metadata failure cannot reverse an already committed exit.
            pass
    if acknowledged_memory_count or outcome.deleted_suggestion_count:
        return JSONResponse(
            status_code=200,
            content=MemberExitResult(
                deleted_memory_count=outcome.deleted_memory_count,
                deleted_suggestion_count=outcome.deleted_suggestion_count,
            ).model_dump(),
        )
    return Response(status_code=204)


@router.get(
    "/notebooks/{notebook_id}/collaborators/{user_id}/exit-disclosure",
    response_model=MemoryExitDisclosure,
    dependencies=[Depends(require_notebook_read)],
)
def collaborator_exit_disclosure(
    notebook_id: str, user_id: str, user: UserProfile = Depends(get_current_user)
) -> MemoryExitDisclosure:
    """The caller themself only (PD-11): personal Memory and unadopted
    suggestions counted apart. Both zero when another read path survives or
    the caller holds no named grant here."""
    if user_id != user.id:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    counts = _service().exit_disclosure(notebook_id, user.id)
    return MemoryExitDisclosure(memory_count=counts.memory_count, suggestion_count=counts.suggestion_count)
