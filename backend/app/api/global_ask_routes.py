"""Owner-scoped global Ask HTTP entry point."""
import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, global_ask_service, user_error
from app.api.task_stream import task_stream_response
from app.models.identity import UserProfile
from app.models.ask import QueryIntentContract
from app.models.global_ask import (
    GlobalAskIntentPreviewRequest, GlobalAskRequest, GlobalAskJob,
    GlobalConversationSummary, GlobalConversationDetail,
    GlobalConversationRename, GLOBAL_ASK_PAGE_SIZE, GLOBAL_ASK_PAGE_MAX,
)
from app.models.sources import SourceElement
from app.services.cancellation import AskCancelled
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


@router.post("/intent", response_model=QueryIntentContract)
async def preview_global_ask_intent(
    payload: GlobalAskIntentPreviewRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> QueryIntentContract:
    """Understand a global reasoning question before creating a job/conversation.

    Byte-for-byte the request-local cancellation dance ``ask_routes.
    preview_ask_intent`` runs above the single-library engine: the potentially
    slow model call moves onto a worker thread, and a client that disconnects
    before it returns sets ``cancel_event`` rather than leaving the call to run
    to completion for nobody. ``GlobalAskService.preview_intent`` re-resolves
    scope and re-checks authority on the SAME path ``start()`` uses, so a
    library the caller can no longer read here is refused exactly as it would
    be on submission.
    """
    cancel_event = threading.Event()

    def run_preview() -> QueryIntentContract:
        return _call(
            global_ask_service().preview_intent, payload,
            user_id=user.id, cancel_event=cancel_event,
        )

    task = asyncio.create_task(asyncio.to_thread(run_preview))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.05)
            if not task.done() and await request.is_disconnected():
                cancel_event.set()
                # The provider observes cancel_event. Consume its terminal
                # exception without delaying a client that has already left.
                task.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                raise HTTPException(status_code=499, detail="Client Closed Request")
        return task.result()
    except asyncio.CancelledError:
        cancel_event.set()
        task.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        raise
    except AskCancelled:
        raise HTTPException(status_code=499, detail="Client Closed Request")


@router.post("/intent/stream")
async def preview_global_ask_intent_stream(
    payload: GlobalAskIntentPreviewRequest,
    request: Request,
    user: UserProfile = Depends(get_current_user),
) -> StreamingResponse:
    """Heartbeat-capable browser transport for the global intent preview.

    The blocking JSON endpoint above remains available for compatibility
    clients; this is the same NDJSON heartbeat wrapper
    ``ask_routes.preview_ask_intent_stream`` uses so a slow understanding
    call does not sit past a reverse proxy's idle timeout. A distinct
    stage/error_code from the single-library endpoint (``global_ask_intent``
    rather than ``ask_intent``) keeps the two features' telemetry apart, the
    same way ``knowhow_routes``/``memory_routes`` each carry their own stage
    name rather than sharing one across features.

    ONE deliberate difference from ``ask_routes.preview_ask_intent_stream``:
    that endpoint resolves scope/history in a plain pre-stream step so a
    404/422 keeps its real HTTP status, and only the model call itself runs
    inside the stream. This endpoint runs ``GlobalAskService.preview_intent``
    -- scope resolution, authority re-check AND the model call -- entirely
    inside the stream worker, because there is no second public entry point
    on ``GlobalAskService`` to split those two phases (the surface this
    change may add is deliberately kept to the one ``preview_intent``
    method). The practical effect: a scope/authority failure on THIS
    endpoint surfaces as the stream's generic content-free
    ``{"event": "error", "error": "global_ask_intent_failed"}`` frame rather
    than a distinct pre-stream status code. The blocking endpoint above does
    not have this gap -- ``task.result()`` re-raises the real
    ``GlobalAskError`` there -- so a client that needs the specific reason
    (foreign conversation, revoked access, empty question) can call it
    instead of the stream.
    """
    cancel_event = threading.Event()

    def run_preview() -> QueryIntentContract:
        return _call(
            global_ask_service().preview_intent, payload,
            user_id=user.id, cancel_event=cancel_event,
        )

    return task_stream_response(
        request, run_preview,
        stage="global_ask_intent", error_code="global_ask_intent_failed",
        cancel_event=cancel_event,
    )


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
