"""Authenticated, owner-private Memory page and Ask-capture endpoints."""
from __future__ import annotations

import json
import re
import threading

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    ask_state_repository,
    get_current_user,
    memory_preview_client,
    memory_service,
    notebook_access_repository,
    require_notebook_read,
)
from app.models.identity import (
    AgentProfile,
    AgentProfileCreate,
    AgentProfileUpdate,
    AgentTokenCreate,
    AgentTokenIssued,
    AgentTokenSummary,
    UserProfile,
)
from app.models.memory import (
    AnswerMemoryLinksRequest,
    AnswerMemoryLinksResponse,
    MemoryBulkDeleteRequest,
    MemoryCreateFromAnswer,
    MemoryOrigin,
    MemoryPreview,
    MemoryRecord,
    MemoryReviewRequest,
    MemoryStatus,
    MemoryTransferRequest,
    MemoryUpdate,
    PaginatedMemories,
)
from app.models.admin import PromoteRequest, PromotionCandidate
from app.repositories.ports import AskStateStorePort, MemoryRepository
from app.services.prompts import MEMORY_PREVIEW_SCHEMA_HINT, memory_preview_prompt
from app.services.knowledge_governance import PromotionTargetError
from app.services.citation_markers import LOOSE_MARKER_RE
from app.core.memory_inputs import MemoryInputError
from app.api.task_stream import task_stream_response
from app.services.cancellation import AskCancelled


memory_router = APIRouter()
_PROTECTED_MARKDOWN_RE = re.compile(
    r"```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~|`[^`\n]*`|"
    r"\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$|\$(?:\\.|[^$\n])+\$",
    re.DOTALL,
)


def _clean_plain_answer(text: str) -> str:
    cleaned = LOOSE_MARKER_RE.sub("", text)
    cleaned = re.sub(
        r"(?:\[\d+(?:\s*[,，]\s*\d+)+\]|【\d+(?:\s*[,，]\s*\d+)+】)|"
        r"(?<!\w)(?:\[\d+\]|【\d+】)",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"[ \t]+([,.;:!?，。；：！？])", r"\1", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned)


def _clean_answer(text: str) -> str:
    value = text or ""
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_MARKDOWN_RE.finditer(value):
        parts.append(_clean_plain_answer(value[cursor:match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_clean_plain_answer(value[cursor:]))
    return "".join(parts).strip()


@memory_router.get("/agent-profiles", response_model=list[AgentProfile])
async def list_agent_profiles(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> list[AgentProfile]:
    return await run_in_threadpool(service.list_agent_profiles, user.id, offset, limit)


@memory_router.post(
    "/agent-profiles",
    response_model=AgentProfile,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_profile(
    payload: AgentProfileCreate,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> AgentProfile:
    try:
        return await run_in_threadpool(
            service.create_agent_profile, user.id, payload.name, payload.description
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@memory_router.patch("/agent-profiles/{profile_id}", response_model=AgentProfile)
async def update_agent_profile(
    profile_id: str,
    payload: AgentProfileUpdate,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> AgentProfile:
    try:
        return await run_in_threadpool(
            service.update_agent_profile, profile_id, user.id, payload
        )
    except KeyError:
        raise _not_found("Agent profile not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@memory_router.post(
    "/agent-profiles/{profile_id}/tokens",
    response_model=AgentTokenIssued,
    status_code=status.HTTP_201_CREATED,
)
async def issue_agent_token(
    profile_id: str,
    payload: AgentTokenCreate,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> AgentTokenIssued:
    if payload.agent_profile_id != profile_id:
        raise HTTPException(status_code=422, detail="profile path and payload differ")
    try:
        return await run_in_threadpool(
            service.issue_agent_token,
            user.id,
            profile_id,
            payload.scopes,
            payload.default_notebook_id,
            payload.notebook_ids,
            payload.expires_at,
        )
    except KeyError:
        raise _not_found("Agent profile not found")
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@memory_router.get("/agent-tokens", response_model=list[AgentTokenSummary])
async def list_agent_tokens(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> list[AgentTokenSummary]:
    return await run_in_threadpool(service.list_agent_tokens, user.id, offset, limit)


@memory_router.delete("/agent-tokens/{token_id}", response_model=AgentTokenSummary)
async def revoke_agent_token(
    token_id: str,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> AgentTokenSummary:
    try:
        return await run_in_threadpool(service.revoke_agent_token, user.id, token_id)
    except KeyError:
        raise _not_found("Agent token not found")


def _not_found(detail: str = "Memory not found") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


async def _memory_call(call, *args, **kwargs):
    try:
        return await run_in_threadpool(call, *args, **kwargs)
    except (KeyError, PermissionError):
        raise _not_found()
    except PromotionTargetError as exc:
        # 目标(target_base_id)校验失败是「输入不合法」而非「状态冲突」——
        # 与其它晋升相关 ValueError(如「已在晋升中」,409)语义不同,单独识别
        # 出来映射 400。子类关系保证这条 except 必须排在下面的 ValueError 之前。
        raise HTTPException(status_code=400, detail=str(exc))
    except MemoryInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@memory_router.get("/memories", response_model=PaginatedMemories)
async def list_all_memories(
    notebook_id: str | None = None,
    status_filter: MemoryStatus | None = Query(None, alias="status"),
    origin: MemoryOrigin | None = None,
    query: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> PaginatedMemories:
    return await _memory_call(
        service.list_memories,
        user.id,
        notebook_id,
        status_filter,
        origin,
        query,
        offset,
        limit,
    )


@memory_router.get(
    "/notebooks/{notebook_id}/memories", response_model=PaginatedMemories
)
async def list_notebook_memories(
    notebook_id: str = Depends(require_notebook_read),
    status_filter: MemoryStatus | None = Query(None, alias="status"),
    origin: MemoryOrigin | None = None,
    query: str = "",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> PaginatedMemories:
    page = await _memory_call(
        service.list_memories,
        user.id,
        notebook_id,
        status_filter,
        origin,
        query,
        offset,
        limit,
    )
    page.kg_extract_eligible = await run_in_threadpool(
        service.memory_kg_eligible, notebook_id
    )
    return page


@memory_router.post(
    "/notebooks/{notebook_id}/answer-memory-links",
    response_model=AnswerMemoryLinksResponse,
)
async def answer_memory_links(
    payload: AnswerMemoryLinksRequest,
    notebook_id: str = Depends(require_notebook_read),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> AnswerMemoryLinksResponse:
    links = await _memory_call(
        service.answer_memory_links, notebook_id, user.id, payload.answer_ids
    )
    return AnswerMemoryLinksResponse(links=links)


@memory_router.post("/memories/bulk-delete")
async def bulk_delete_memories(
    payload: MemoryBulkDeleteRequest,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> dict[str, int]:
    deleted = await _memory_call(
        service.bulk_delete_memories, user.id, payload.memory_ids
    )
    return {"deleted": deleted}


@memory_router.delete(
    "/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_memory(
    memory_id: str,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> None:
    await _memory_call(service.delete_memory, memory_id, user.id)


@memory_router.get("/memories/{memory_id}", response_model=MemoryRecord)
async def get_memory(
    memory_id: str,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    return await _memory_call(service.get_memory, memory_id, user.id)


@memory_router.patch("/memories/{memory_id}", response_model=MemoryRecord)
async def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    return await _memory_call(service.update_memory, memory_id, user.id, payload)


@memory_router.post("/memories/{memory_id}/confirm", response_model=MemoryRecord)
async def confirm_memory(
    memory_id: str,
    payload: MemoryReviewRequest | None = None,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    return await _memory_call(service.confirm_memory, memory_id, user.id, payload)


@memory_router.post("/memories/{memory_id}/reject", response_model=MemoryRecord)
async def reject_memory(
    memory_id: str,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    return await _memory_call(service.reject_memory, memory_id, user.id)


@memory_router.post("/memories/{memory_id}/deprecate", response_model=MemoryRecord)
async def deprecate_memory(
    memory_id: str,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    return await _memory_call(service.deprecate_memory, memory_id, user.id)


@memory_router.post(
    "/memories/{memory_id}/promote",
    response_model=PromotionCandidate,
    status_code=status.HTTP_201_CREATED,
)
async def promote_memory(
    memory_id: str,
    payload: PromoteRequest = PromoteRequest(),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> PromotionCandidate:
    return await _memory_call(
        service.propose_memory_promotion, memory_id, user.id,
        target_base_id=payload.target_base_id,
    )


@memory_router.post("/answers/{answer_id}/memory-preview", response_model=MemoryPreview)
async def preview_answer_memory(
    answer_id: str,
    user: UserProfile = Depends(get_current_user),
    ask_state: AskStateStorePort = Depends(ask_state_repository),
    llm_client=Depends(memory_preview_client),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryPreview:
    can_read = await run_in_threadpool(
        notebook_access_repository().user_can_read_answer, answer_id, user.id
    )
    if not can_read:
        raise _not_found("Answer not found")
    try:
        source = await run_in_threadpool(ask_state.answer_memory_source, answer_id)
    except KeyError:
        raise _not_found("Answer not found")

    kg_extract_eligible = await run_in_threadpool(
        service.memory_kg_eligible, source["notebook_id"]
    )
    return await run_in_threadpool(
        _answer_memory_preview_result,
        answer_id,
        source,
        kg_extract_eligible,
        llm_client,
    )


def _answer_memory_preview_result(
    answer_id: str,
    source: dict,
    kg_extract_eligible: bool,
    llm_client,
    cancel_event: threading.Event | None = None,
) -> MemoryPreview:
    fallback = MemoryPreview(
        title=source["question"][:80],
        content_md=_clean_answer(source["answer"]),
        tags=[],
        provenance_summary={
            "answer_id": answer_id,
            "notebook_id": source["notebook_id"],
            "evidence_level": source["evidence_level"],
            "citation_count": len(source["citations"]),
        },
        kg_extract_eligible=kg_extract_eligible,
    )
    if not getattr(llm_client, "configured", False):
        return fallback
    try:
        control = {"cancel_event": cancel_event} if cancel_event is not None else {}
        raw = llm_client.chat_json(
            [{"role": "user", "content": memory_preview_prompt(source["question"], source["answer"])}],
            MEMORY_PREVIEW_SCHEMA_HINT,
            **control,
        )
        data = json.loads(raw)
        title = str(data.get("title") or "").strip()[:80]
        content_md = str(data.get("content_md") or "").strip()
        raw_tags = data.get("tags")
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()] if isinstance(raw_tags, list) else []
        if not title or not content_md:
            return fallback
        return MemoryPreview(
            title=title,
            content_md=content_md,
            tags=tags,
            provenance_summary=fallback.provenance_summary,
            kg_extract_eligible=kg_extract_eligible,
        )
    except AskCancelled:
        raise
    except Exception:
        return fallback


@memory_router.post("/answers/{answer_id}/memory-preview/stream")
async def preview_answer_memory_stream(
    answer_id: str,
    request: Request,
    user: UserProfile = Depends(get_current_user),
    ask_state: AskStateStorePort = Depends(ask_state_repository),
    llm_client=Depends(memory_preview_client),
    service: MemoryRepository = Depends(memory_service),
) -> StreamingResponse:
    """Stream the slow model half while retaining the deterministic fallback."""
    can_read = await run_in_threadpool(
        notebook_access_repository().user_can_read_answer, answer_id, user.id
    )
    if not can_read:
        raise _not_found("Answer not found")
    try:
        source = await run_in_threadpool(ask_state.answer_memory_source, answer_id)
    except KeyError:
        raise _not_found("Answer not found")
    kg_extract_eligible = await run_in_threadpool(
        service.memory_kg_eligible, source["notebook_id"]
    )
    cancel_event = threading.Event()
    return task_stream_response(
        request,
        lambda: _answer_memory_preview_result(
            answer_id,
            source,
            kg_extract_eligible,
            llm_client,
            cancel_event,
        ),
        stage="memory_preview",
        error_code="memory_preview_failed",
        cancel_event=cancel_event,
    )


@memory_router.post(
    "/notebooks/{notebook_id}/memories/from-answer",
    response_model=MemoryRecord,
    status_code=status.HTTP_201_CREATED,
)
async def create_memory_from_answer(
    payload: MemoryCreateFromAnswer,
    notebook_id: str = Depends(require_notebook_read),
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> MemoryRecord:
    try:
        return await run_in_threadpool(
            service.create_memory_from_answer,
            notebook_id,
            user.id,
            payload.answer_id,
            payload.title,
            payload.content_md,
            payload.tags,
            payload.extract_kg,
        )
    except PermissionError:
        raise _not_found()
    except KeyError:
        raise HTTPException(status_code=409, detail="Answer is no longer available")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@memory_router.post("/memories/transfer")
async def transfer_memories(
    payload: MemoryTransferRequest,
    user: UserProfile = Depends(get_current_user),
    service: MemoryRepository = Depends(memory_service),
) -> dict:
    results = await _memory_call(
        service.transfer_memories,
        user.id,
        payload.memory_ids,
        payload.target_notebook_id,
        payload.mode,
        payload.extract_kg,
    )
    return {"results": results}
