"""Owner-scoped global Ask HTTP entry point."""
from fastapi import APIRouter, Depends, Query, Response
from app.api.deps import get_current_user, global_ask_service, user_error
from app.models.identity import UserProfile
from app.models.global_ask import (
    GlobalAskRequest, GlobalAskJob, GlobalConversationSummary,
    GlobalConversationDetail, GlobalConversationRename,
    GLOBAL_ASK_PAGE_SIZE, GLOBAL_ASK_PAGE_MAX,
)
from app.models.sources import SourceElement
from app.services.global_ask import GlobalAskError

router = APIRouter(prefix="/global-ask", tags=["global-ask"])


def _call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except GlobalAskError as exc:
        raise user_error(exc.status_code, exc.message) from exc


@router.post("/ask", response_model=GlobalAskJob, status_code=202)
def ask_global(payload: GlobalAskRequest, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().start, payload, user_id=user.id)


@router.get("/jobs/{job_id}", response_model=GlobalAskJob)
def get_global_ask_job(job_id: str, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().get_job, job_id, user_id=user.id)


@router.post("/jobs/{job_id}/cancel", response_model=GlobalAskJob)
def cancel_global_ask_job(job_id: str, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().cancel, job_id, user_id=user.id)


@router.get("/jobs/{job_id}/citations/{element_id}", response_model=SourceElement)
def global_cited_element(job_id: str, element_id: str, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().cited_element, job_id, element_id, user_id=user.id)


@router.get("/conversations", response_model=list[GlobalConversationSummary])
def global_conversations(
    limit: int = Query(GLOBAL_ASK_PAGE_SIZE, ge=1, le=GLOBAL_ASK_PAGE_MAX),
    offset: int = Query(0, ge=0), user: UserProfile = Depends(get_current_user),
):
    return _call(global_ask_service().list_conversations, user_id=user.id, limit=limit, offset=offset)


@router.get("/conversations/{conversation_id}", response_model=GlobalConversationDetail)
def global_conversation(
    conversation_id: str,
    limit: int = Query(GLOBAL_ASK_PAGE_SIZE, ge=1, le=GLOBAL_ASK_PAGE_MAX),
    offset: int = Query(0, ge=0), user: UserProfile = Depends(get_current_user),
):
    return _call(global_ask_service().conversation, conversation_id, user_id=user.id, limit=limit, offset=offset)


@router.patch("/conversations/{conversation_id}", response_model=GlobalConversationSummary)
def rename_global_conversation(conversation_id: str, payload: GlobalConversationRename, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().rename_conversation, conversation_id, payload.title, user_id=user.id)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_global_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)):
    _call(global_ask_service().delete_conversation, conversation_id, user_id=user.id)
    return Response(status_code=204)
