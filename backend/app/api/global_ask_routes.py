"""Owner-scoped global Ask HTTP entry point."""
import asyncio
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user, global_ask_service, user_error
from app.api.task_stream import (
    NDJSON_STREAM_HEADERS,
    deliver_ask_events,
    task_stream_response,
)
from app.models.identity import UserProfile
from app.models.ask import (
    ConversationShareRequest,
    ConversationShareResponse,
    QueryIntentContract,
)
from app.repositories.ports import (
    ConversationHasNoShareableAnswer,
    ConversationShareWatermarkStale,
)
from app.models.global_ask import (
    GlobalAskFeedbackRequest, GlobalAskIntentPreviewRequest, GlobalAskRequest, GlobalAskJob,
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
    service = global_ask_service()
    prepared = await asyncio.to_thread(
        _call, service.prepare_intent_preview, payload, user_id=user.id,
    )

    def run_preview() -> QueryIntentContract:
        return _call(
            service.run_intent_preview, prepared, cancel_event=cancel_event,
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

    TWO PHASES, exactly like ``ask_routes.preview_ask_intent_stream``: scope
    resolution and the authority re-check run BEFORE the stream opens, so a
    foreign conversation, a revoked share or an over-wide scope keeps its real
    404/422 and its real Chinese sentence. Only the model call -- the slow
    part the heartbeat exists for -- runs inside the stream. Running both
    inside the stream is what the first cut did, and it turned every one of
    those refusals into a 200 plus a content-free
    ``{"event": "error", "error": "global_ask_intent_failed"}`` frame, which a
    client cannot tell apart from "the model service is down".
    """
    cancel_event = threading.Event()
    service = global_ask_service()
    # Before the first frame: the response status is still negotiable here.
    prepared = await asyncio.to_thread(
        _call, service.prepare_intent_preview, payload, user_id=user.id,
    )

    def run_preview() -> QueryIntentContract:
        return _call(
            service.run_intent_preview, prepared, cancel_event=cancel_event,
        )

    return task_stream_response(
        request, run_preview,
        stage="global_ask_intent", error_code="global_ask_intent_failed",
        cancel_event=cancel_event,
    )


@router.get("/jobs/{job_id}", response_model=GlobalAskJob)
def get_global_ask_job(job_id: str, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().get_job, job_id, user_id=user.id)


@router.get("/jobs/{job_id}/stream")
async def stream_global_ask_job(
    job_id: str, request: Request, user: UserProfile = Depends(get_current_user),
) -> StreamingResponse:
    """Push this job's progress to an ATTACHED client, as NDJSON.

    An accelerator on top of the durable job, not a second way to run one:
    `POST /ask` still creates it, `GET /jobs/{id}` still reads it, and a client
    that never opens this endpoint — or loses it — sees the same run by
    polling. Nothing here starts, finishes or cancels anything.

    ⛔ THE CLIENT DISCONNECTING DOES NOT CANCEL THE JOB, exactly as on the
    notebook Ask stream: the shared delivery loop stops delivering and closes
    the queue, which stops a follower polling on this connection's behalf and
    drops this subscriber from the live feed. The only cancel entry is
    ``POST /jobs/{job_id}/cancel``.

    Authority is checked BEFORE the first frame, through the very call
    ``GET /jobs/{id}`` makes (``GlobalAskService.get_job``, inside ``attach``),
    so a foreign or missing job keeps its real 404 instead of degrading into an
    error frame inside a 200. That call is blocking, hence ``to_thread`` — the
    same shape the two intent endpoints above use for their preflight.
    """
    events = await asyncio.to_thread(
        _call, global_ask_service().attach, job_id, user_id=user.id,
    )
    return StreamingResponse(
        deliver_ask_events(events, request),
        media_type="application/x-ndjson",
        headers=NDJSON_STREAM_HEADERS,
    )


@router.post("/jobs/{job_id}/cancel", response_model=GlobalAskJob)
def cancel_global_ask_job(
    job_id: str, discard: bool = Query(False), user: UserProfile = Depends(get_current_user),
):
    return _call(global_ask_service().cancel, job_id, user_id=user.id, discard=discard)


@router.post("/jobs/{job_id}/feedback", response_model=GlobalAskJob)
def submit_global_ask_feedback(job_id: str, payload: GlobalAskFeedbackRequest, user: UserProfile = Depends(get_current_user)):
    return _call(global_ask_service().submit_feedback, job_id, payload.rating, user_id=user.id)


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


# --- 全局会话公开分享(发放/回读/撤销)---------------------------------------
#
# The notebook-scoped trio under `/notebooks/{nb}/conversations/{cid}/share`,
# transposed onto a conversation that belongs to a USER rather than to a
# library: there is no `require_notebook_read` to sit above it, because there is
# no notebook the conversation lives in -- ownership IS the whole gate, and the
# libraries a snapshot drew on are re-authorized on every anonymous open instead
# (`GlobalAskService.public_conversation`). Request/response models are the
# notebook-scoped ones, imported rather than copied, so one contract change can
# never land on only one of the two features -- which is also what lets the
# browser drive both through the same `ConversationShareApi` object.
#
# The anonymous read stays on `ask_routes.public_router`'s existing
# `/public/conversations/{token}` (dispatched by token prefix), so `/c/{token}`
# is one page for both features.


def _share_call(method, *args, **kwargs):
    """The share trio's error mapping, byte for byte the notebook-scoped one.

    ``KeyError`` (missing OR not this user's) is the same indistinguishable 404
    ``_own_conversation_or_404`` answers; the two 409s carry the store's own
    refusals with the same Chinese sentences the notebook-scoped route uses --
    the same modal renders both, so a divergent sentence would read as a
    different failure for what is the same one.
    """
    try:
        return method(*args, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    except ConversationShareWatermarkStale:
        raise user_error(409, "这条会话已有变化，请刷新后重新分享。")
    except ConversationHasNoShareableAnswer:
        raise user_error(409, "这条会话还没有已完成的回答，暂时无法分享。")
    except GlobalAskError as exc:
        raise user_error(exc.status_code, exc.message) from exc


@router.post("/conversations/{conversation_id}/share", response_model=ConversationShareResponse)
def share_global_conversation_route(
    conversation_id: str,
    payload: ConversationShareRequest | None = Body(default=None),
    user: UserProfile = Depends(get_current_user),
) -> ConversationShareResponse:
    """Publish this conversation behind a link AND advance the watermark.

    ``expected_through_id`` is a JOB id on this surface (the notebook-scoped
    twin pins an answer id): the newest completed turn the client saw in what
    it disclosed. It pins the published snapshot to exactly that turn, closing
    the window where a turn finishing between the client's disclosure read and
    this POST would otherwise be published without the user ever reviewing it.
    """
    state = _share_call(
        global_ask_service().share_conversation, conversation_id, user_id=user.id,
        expected_through_id=payload.expected_through_id if payload else "",
    )
    return ConversationShareResponse(**state)


@router.get("/conversations/{conversation_id}/share", response_model=ConversationShareResponse)
def get_global_conversation_share_route(
    conversation_id: str, user: UserProfile = Depends(get_current_user),
) -> ConversationShareResponse:
    """Read back the existing link + watermark, owner only -- the token IS the
    grant. Not shared is a 404, the same answer an unknown conversation gets;
    the browser's share dialog reads that 404 as "not shared yet" rather than
    as an error."""
    state = _share_call(
        global_ask_service().conversation_share, conversation_id, user_id=user.id,
    )
    if not state.get("share_token"):
        raise HTTPException(status_code=404, detail="conversation is not shared")
    return ConversationShareResponse(**state)


@router.delete("/conversations/{conversation_id}/share", status_code=204)
def unshare_global_conversation_route(
    conversation_id: str, user: UserProfile = Depends(get_current_user),
) -> Response:
    """Revoke the link; the next public request 404s like any unknown token.

    The 404 for a foreign or missing conversation comes from the service's
    ownership gate, NOT from the store: ``unshare_conversation`` is a bare
    idempotent UPDATE that reports success whether or not it matched a row, so
    without that gate this endpoint would answer 204 for someone else's
    conversation id. Un-sharing an ALREADY-unshared conversation of one's own
    stays a 204, matching the notebook-scoped endpoint.
    """
    _share_call(global_ask_service().unshare_conversation, conversation_id, user_id=user.id)
    return Response(status_code=204)
