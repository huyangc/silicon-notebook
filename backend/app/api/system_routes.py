import asyncio
import json
from typing import List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    admin_query_repository,
    get_current_user,
    notebook_catalog_repository,
    repository,
)
from app.core.config import get_settings
from app.models.identity import UserProfile
from app.models.notebooks import NotebookTemplate
from app.models.sources import DetectDocTypesRequest, DetectedDocType
from app.services.model_registry import SystemModelServiceRegistry
from app.services.pending_bus import pending_bus


router = APIRouter()


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    registry = SystemModelServiceRegistry.load(settings)
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": registry.service_for("ask_answer") is not None,
        "reasoning_llm_configured": (
            registry.service_for("reasoning_agent") is not None
        ),
        "embedding_configured": (
            registry.service_for("retrieval_query_embedding") is not None
        ),
    }


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user


@router.get("/doc-types")
def list_doc_types():
    """Document-type options for the upload picker ('' = auto-detect)."""
    from app.services.extraction_profiles import PROFILES

    return [{"id": "", "label": "自动检测"}] + [
        {"id": profile.id, "label": profile.label} for profile in PROFILES.values()
    ]


@router.post("/detect-doc-types", response_model=List[DetectedDocType])
def detect_doc_types(payload: DetectDocTypesRequest) -> List[DetectedDocType]:
    """Best-effort document-type detection from leading text samples, batched
    (one request for many files). Used by the upload picker to pre-fill each
    file's type; doc_type_id '' means undetected so the UI shows '自动检测'."""
    from app.services.extraction_profiles import detect_doc_type_from_sample

    return [
        DetectedDocType(
            name=item.name,
            doc_type_id=detect_doc_type_from_sample(item.sample) or "",
        )
        for item in payload.items
    ]


@router.get("/notebook-templates", response_model=List[NotebookTemplate])
def list_notebook_templates() -> List[NotebookTemplate]:
    return notebook_catalog_repository().list_notebook_templates()


# --- 待确认中心 (Pending Actions Center) ---------------------------------


@router.get("/me/pending-actions")
def me_pending_actions(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前用户的待办快照：深度报告待确认 + 治理三队列 + 索引状态，供铃铛使用。
    只读转调 repository().pending_actions；聚合逻辑与三源覆盖见该方法本身
    （Task 1 已有专门单测），此处只做 HTTP 薄包装。"""
    return repository().pending_actions(user.id)


@router.get("/me/pending-actions/stream")
async def me_pending_stream(
    request: Request, user: UserProfile = Depends(get_current_user)
) -> StreamingResponse:
    """待确认中心的实时推送通道（NDJSON）：先补发离线期间缓冲的瞬时事件，
    再发一帧初始 snapshot，再挂进 pending_bus 循环等待后续推送；15s 无消息发
    keepalive 注释帧（前端按 `:` 前缀跳过，非 JSON 行）。"""
    uid = user.id
    pending_bus.bind_loop()

    async def gen():
        # 1) 先补发离线期间缓冲的瞬时事件(跨会话)
        for ev in pending_bus.flush_buffer(uid):
            yield json.dumps({"kind": "event", **ev}, ensure_ascii=False) + "\n"
        # 2) 初始 snapshot —— DB 计算放线程池,勿阻塞 loop
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, repository().pending_actions, uid)
        yield json.dumps({"kind": "snapshot", "data": data}, ensure_ascii=False) + "\n"
        # 3) 注册连接,循环等待推送 + keepalive
        q = pending_bus.register(uid)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n"  # 注释帧,前端忽略(非 JSON 行)
                    continue
                yield json.dumps(msg, ensure_ascii=False) + "\n"
        finally:
            pending_bus.unregister(uid, q)

    return StreamingResponse(gen(), media_type="application/x-ndjson")
