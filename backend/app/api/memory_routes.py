"""Authenticated, owner-private Memory page and Ask-capture endpoints."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import (
    ask_state_repository,
    get_current_user,
    memory_preview_client,
    memory_service,
    notebook_access_repository,
    require_notebook_read,
)
from app.models.schemas import (
    AnswerMemoryLinksRequest,
    AnswerMemoryLinksResponse,
    MemoryCreateFromAnswer,
    MemoryOrigin,
    MemoryPreview,
    MemoryRecord,
    MemoryReviewRequest,
    MemoryStatus,
    MemoryUpdate,
    PaginatedMemories,
    UserProfile,
)
from app.repositories.ports import AskStateStorePort, MemoryRepository
from app.services.prompts import MEMORY_PREVIEW_SCHEMA_HINT, memory_preview_prompt


memory_router = APIRouter()
_PROTECTED_MARKDOWN_RE = re.compile(
    r"```[^\n]*\n.*?```|~~~[^\n]*\n.*?~~~|`[^`\n]*`|"
    r"\\\(.*?\\\)|\\\[.*?\\\]|\$\$.*?\$\$|\$(?:\\.|[^$\n])+\$",
    re.DOTALL,
)


def _clean_plain_answer(text: str) -> str:
    cleaned = re.sub(
        r"\[k\d+\]|\[\d+(?:\s*,\s*\d+)+\]|(?<!\w)\[\d+\]",
        "",
        text,
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


def _not_found(detail: str = "Memory not found") -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


async def _memory_call(call, *args):
    try:
        return await run_in_threadpool(call, *args)
    except (KeyError, PermissionError):
        raise _not_found()
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


@memory_router.post("/answers/{answer_id}/memory-preview", response_model=MemoryPreview)
async def preview_answer_memory(
    answer_id: str,
    user: UserProfile = Depends(get_current_user),
    ask_state: AskStateStorePort = Depends(ask_state_repository),
    llm_client=Depends(memory_preview_client),
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
    )
    if not getattr(llm_client, "configured", False):
        return fallback
    try:
        raw = await run_in_threadpool(
            llm_client.chat_json,
            [{"role": "user", "content": memory_preview_prompt(source["question"], source["answer"])}],
            MEMORY_PREVIEW_SCHEMA_HINT,
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
        )
    except Exception:
        return fallback


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
        )
    except PermissionError:
        raise _not_found()
    except KeyError:
        raise HTTPException(status_code=409, detail="Answer is no longer available")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
