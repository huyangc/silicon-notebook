import asyncio
import json
import logging
import time
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    admin_query_repository,
    get_current_user,
    identity_repository,
    notebook_catalog_repository,
    repository,
)
from app.core.config import get_settings
from app.models.identity import UserProfile
from app.models.model_services import (
    ModelServiceStatusItem,
    ModelServiceView,
    ModelServicesStatus,
    ModelSettingsUpdate,
    ModelTestRequest,
    ModelTestResult,
)
from app.models.notebooks import NotebookTemplate
from app.models.sources import DetectDocTypesRequest, DetectedDocType
from app.services.model_config import STATUS_SERVICE_ROLES
from app.services.model_status import ModelStatusService
from app.services.pending_bus import pending_bus


router = APIRouter()
logger = logging.getLogger("silicon_notebook.model_settings")


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "environment": settings.environment,
        "llm_configured": settings.llm_configured,
        "reasoning_llm_configured": settings.reasoning_llm_configured,
        "embedding_configured": settings.embedder_configured,
    }


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user


_MODEL_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")


def _mask_key(key: str) -> str:
    # 只露尾 4 位且必须确有被截断的前缀(len>4)；≤4 位则整体隐去，绝不暴露完整短 key。
    key = key or ""
    return f"…{key[-4:]}" if len(key) > 4 else ("…" if key else "")


@router.get("/me/model-settings")
def get_model_settings(user: UserProfile = Depends(get_current_user)):
    repo = identity_repository()
    stored = repo.get_user_model_settings(user.id)
    out = {}
    for role in _MODEL_ROLES:
        svc = stored.get(role) or {}
        out[role] = ModelServiceView(
            base_url=svc.get("base_url", ""),
            model=svc.get("model", ""),
            has_key=bool(svc.get("api_key")),
            key_hint=_mask_key(svc.get("api_key", "")),
            source=repo.resolve_model_config(user, role).source,
        )
    return out


@router.put("/me/model-settings")
def put_model_settings(payload: ModelSettingsUpdate, user: UserProfile = Depends(get_current_user)):
    repo = identity_repository()
    repo.patch_user_model_settings_atomic(
        user.id,
        payload.model_dump(exclude_unset=True),
    )
    return get_model_settings(user)


@router.post("/me/model-settings/test", response_model=ModelTestResult)
def test_model_service(payload: ModelTestRequest, user: UserProfile = Depends(get_current_user)):
    if payload.service not in _MODEL_ROLES:
        return ModelTestResult(ok=False, code="unknown_service")
    repo = identity_repository()
    stored = repo.get_user_model_settings(user.id).get(payload.service) or {}
    api_key = payload.api_key if payload.api_key else stored.get("api_key", "")
    base_url, model = payload.base_url.strip(), payload.model.strip()
    if not (base_url and model and api_key):
        return ModelTestResult(ok=False, code="missing_config")
    started = time.perf_counter()
    settings = get_settings()
    try:
        if payload.service == "rerank":
            from app.services.rerank_client import RerankClient
            RerankClient(settings, model=model, base_url=base_url, api_key=api_key)._rerank_batch(
                "ping", ["a", "b"])
        else:
            from app.core.llm import OpenAICompatibleClient
            OpenAICompatibleClient(settings, base_url=base_url, api_key=api_key, model=model).chat_json(
                [{"role": "user", "content": "ping"}], "{}", timeout=10, max_retries=0)
        return ModelTestResult(ok=True, latency_ms=round((time.perf_counter() - started) * 1000))
    except Exception:
        logger.exception("model settings test failed for %s", payload.service)
        return ModelTestResult(
            ok=False,
            latency_ms=round((time.perf_counter() - started) * 1000),
            code="upstream_error",
        )


def _model_status_service() -> ModelStatusService:
    return ModelStatusService(identity_repository(), get_settings())


@router.get("/me/model-services/status", response_model=ModelServicesStatus)
def get_model_services_status(user: UserProfile = Depends(get_current_user)) -> ModelServicesStatus:
    return _model_status_service().snapshot(user)


@router.post("/me/model-services/test-all", response_model=ModelServicesStatus)
def test_all_model_services(user: UserProfile = Depends(get_current_user)) -> ModelServicesStatus:
    return _model_status_service().test_all(user)


@router.post("/me/model-services/{service}/test", response_model=ModelServiceStatusItem)
def test_current_model_service(
    service: str,
    user: UserProfile = Depends(get_current_user),
) -> ModelServiceStatusItem:
    if service not in STATUS_SERVICE_ROLES:
        raise HTTPException(status_code=404, detail="model service not found")
    return _model_status_service().test_one(user, service)


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
