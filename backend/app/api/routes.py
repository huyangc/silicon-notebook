import asyncio
import contextvars
import json
import queue
import threading
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.api.deps import (
    repository, require_notebook_access, require_notebook_read,
    require_notebook_write, get_current_user,
)
from app.core.config import get_settings
from app.models.schemas import (
    AskRequest,
    AskResponse,
    ConceptWhitelistAdd,
    ConceptWhitelistEntry,
    ConversationDetail,
    ConversationRenameRequest,
    ConversationSummary,
    DetectDocTypesRequest,
    DetectedDocType,
    DuplicateGroup,
    EdgeReviewItem,
    EdgeReviewRequest,
    FeedbackRequest,
    FeedbackResponse,
    KgSearchResponse,
    KnowledgeGraph,
    KnowledgeRecord,
    KnowledgeTypeCount,
    KnowledgeUpdate,
    PaginatedKnowledge,
    MergeRequest,
    MergeReviewRequest,
    MergeReviewSummary,
    ModelServiceView,
    ModelSettingsUpdate,
    ModelTestRequest,
    ModelTestResult,
    NotebookAnalytics,
    NotebookCreate,
    NotebookSearchResponse,
    NotebookSummary,
    NotebookTemplate,
    NotebookUpdate,
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
    PromotionApproveResult,
    PromotionCandidate,
    PaginatedSources,
    PromotionRejectRequest,
    SourceDetail,
    SourceElement,
    AddUrlSourcesRequest,
    AddUrlSourcesResult,
    SetTierRequest,
    ShareResponse,
    SharedByMeItem,
    SharedPreview,
    SourceImportRequest,
    SourceSummary,
    ScaleIndexStatus,
    UnifiedKgStatus,
    UserProfile,
)
from app.services.ask_modes import resolve_mode, UnknownAskMode, ASK_MODES
from app.services.cancellation import AskCancelled
from app.services.kg import scheduler as kg_scheduler
from app.services.mineru_cloud_client import MinerUCloudNotConfigured
from app.services.repository import NotebookRepository, UploadedSourceFile

router = APIRouter()

SUPPORTED_SOURCE_SUFFIXES = {".pdf", ".md", ".markdown", ".docx", ".pptx", ".csv", ".xlsx", ".xlsm"}
MAX_SOURCE_UPLOAD_BYTES = 50 * 1024 * 1024


def _validate_source_file(file_name: str, content_size: int | None = None) -> None:
    suffix = Path(file_name).suffix.lower()
    if suffix not in SUPPORTED_SOURCE_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SOURCE_SUFFIXES))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported source file type. Supported suffixes: {supported}",
        )
    if content_size is not None:
        if content_size == 0:
            raise HTTPException(status_code=400, detail="Uploaded source file is empty")
        if content_size > MAX_SOURCE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Uploaded source file is too large")


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
    repo = repository()
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
    repo = repository()
    stored = dict(repo.get_user_model_settings(user.id))
    for role in _MODEL_ROLES:
        upd = getattr(payload, role)
        if upd is None:
            continue
        svc = dict(stored.get(role) or {})
        for field in ("base_url", "api_key", "model"):
            val = getattr(upd, field)
            if val is None:          # 不变
                continue
            if val == "":            # 清除
                svc.pop(field, None)
            else:                    # 设置
                svc[field] = val
        if svc:
            stored[role] = svc
        else:
            stored.pop(role, None)
    repo.set_user_model_settings(user.id, stored)
    return get_model_settings(user)


@router.post("/me/model-settings/test", response_model=ModelTestResult)
def test_model_service(payload: ModelTestRequest, user: UserProfile = Depends(get_current_user)):
    import time
    if payload.service not in _MODEL_ROLES:
        return ModelTestResult(ok=False, error="未知服务")
    repo = repository()
    stored = repo.get_user_model_settings(user.id).get(payload.service) or {}
    api_key = payload.api_key if payload.api_key else stored.get("api_key", "")
    base_url, model = payload.base_url.strip(), payload.model.strip()
    if not (base_url and model and api_key):
        return ModelTestResult(ok=False, error="缺少 base_url / model / api_key")
    started = time.perf_counter()
    try:
        if payload.service == "rerank":
            from app.services.rerank_client import RerankClient
            RerankClient(repo.settings, model=model, base_url=base_url, api_key=api_key)._rerank_batch(
                "ping", ["a", "b"])
        else:
            from app.core.llm import OpenAICompatibleClient
            OpenAICompatibleClient(repo.settings, base_url=base_url, api_key=api_key, model=model).chat_json(
                [{"role": "user", "content": "ping"}], "{}", timeout=10, max_retries=0)
        return ModelTestResult(ok=True, latency_ms=round((time.perf_counter() - started) * 1000))
    except Exception as exc:
        return ModelTestResult(ok=False, latency_ms=round((time.perf_counter() - started) * 1000),
                               error=f"{type(exc).__name__}: {exc}"[:200])


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
    return repository().list_notebook_templates()


@router.get("/notebooks", response_model=List[NotebookSummary])
def list_notebooks() -> List[NotebookSummary]:
    return repository().list_notebooks()


# 注意:静态段路由必须在 /notebooks/{notebook_id} 之前注册,否则 "shared-by-me" 被当作 {notebook_id}。
@router.get("/notebooks/shared-by-me", response_model=List[SharedByMeItem])
def shared_by_me_route(user: UserProfile = Depends(get_current_user)) -> List[SharedByMeItem]:
    return [SharedByMeItem(**it) for it in repository().shared_by_me(user.id)]


@router.post("/notebooks", response_model=NotebookSummary)
def create_notebook(payload: NotebookCreate) -> NotebookSummary:
    return repository().create_notebook(payload)


@router.get("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_read)])
def get_notebook(notebook_id: str) -> NotebookSummary:
    try:
        return repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/analytics", response_model=NotebookAnalytics, dependencies=[Depends(require_notebook_read)])
def notebook_analytics(notebook_id: str) -> NotebookAnalytics:
    try:
        return repository().notebook_analytics(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}", response_model=NotebookSummary, dependencies=[Depends(require_notebook_access)])
def update_notebook(
    notebook_id: str,
    payload: NotebookUpdate,
) -> NotebookSummary:
    try:
        return repository().update_notebook(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}", status_code=204, dependencies=[Depends(require_notebook_access)])
def delete_notebook(notebook_id: str) -> None:
    try:
        repository().delete_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/sources", response_model=PaginatedSources, dependencies=[Depends(require_notebook_read)])
def list_sources(
    notebook_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str = Query(""),
) -> PaginatedSources:
    return repository().list_sources_page(notebook_id, offset=offset, limit=limit, q=q)


@router.post("/notebooks/{notebook_id}/sources/import", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_access)])
def import_sources(
    notebook_id: str,
    payload: SourceImportRequest,
) -> List[SourceSummary]:
    try:
        for file in payload.files:
            _validate_source_file(file.file_name)
        return repository().import_sources(notebook_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources/url", response_model=AddUrlSourcesResult, dependencies=[Depends(require_notebook_access)])
def add_url_sources(
    notebook_id: str,
    payload: AddUrlSourcesRequest,
) -> AddUrlSourcesResult:
    repo = repository()
    try:
        return repo.add_url_sources(
            notebook_id,
            payload.urls,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
        )
    except MinerUCloudNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/sources", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_access)])
async def upload_sources(
    notebook_id: str,
    files: List[UploadFile] = File(...),
    doc_types: List[str] = Form(default=[]),
) -> List[SourceSummary]:
    try:
        repo = repository()
        uploaded_files = []
        for index, file in enumerate(files):
            file_name = file.filename or "source.bin"
            _validate_source_file(file_name)
            content = await file.read()
            _validate_source_file(file_name, len(content))
            # doc_types is aligned with files by position; missing/extra are tolerated.
            doc_type = doc_types[index] if index < len(doc_types) else ""
            uploaded_files.append(
                UploadedSourceFile(
                    file_name=file_name,
                    content_type=file.content_type or "",
                    content=content,
                    doc_type=doc_type,
                )
            )
        return repo.upload_sources(
            notebook_id,
            uploaded_files,
            scheduler=lambda source_id: kg_scheduler.submit_job(repo.process_source, source_id),
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceDetail:
    if not repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.post("/sources/{source_id}/parse", response_model=SourceSummary)
def parse_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceSummary:
    if repository().source_owner(source_id) != user.id:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return repository().parse_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.get("/sources/{source_id}/elements", response_model=List[SourceElement])
def source_elements(source_id: str, user: UserProfile = Depends(get_current_user)) -> List[SourceElement]:
    if not repository().user_can_read_source(source_id, user.id):  # 读:owner ∪ 只读成员
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return repository().source_elements(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    if repository().source_owner(source_id) != user.id:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        repository().delete_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")



@router.get(
    "/notebooks/{notebook_id}/knowledge-types",
    response_model=List[KnowledgeTypeCount],
    dependencies=[Depends(require_notebook_read)],
)
def knowledge_types(notebook_id: str) -> List[KnowledgeTypeCount]:
    try:
        return repository().knowledge_types(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/knowledge", response_model=PaginatedKnowledge, dependencies=[Depends(require_notebook_read)])
def list_knowledge(
    notebook_id: str,
    type: str = Query(...),
    status: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> PaginatedKnowledge:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().list_knowledge(notebook_id, object_type, status=status, offset=offset, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Editable extraction-schema registry ---------------------------------
@router.get("/object-schemas", response_model=List[ObjectSchemaModel])
def list_object_schemas() -> List[ObjectSchemaModel]:
    return repository().list_object_schemas()


@router.post("/object-schemas", response_model=ObjectSchemaModel)
def create_object_schema(payload: ObjectSchemaCreate, user: UserProfile = Depends(get_current_user)) -> ObjectSchemaModel:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改全局配置")
    try:
        return repository().create_object_schema(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/object-schemas/{object_type}", response_model=ObjectSchemaModel)
def update_object_schema(
    object_type: str, payload: ObjectSchemaUpdate, user: UserProfile = Depends(get_current_user)
) -> ObjectSchemaModel:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改全局配置")
    try:
        return repository().update_object_schema(object_type, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/object-schemas/{object_type}")
def delete_object_schema(object_type: str, user: UserProfile = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改全局配置")
    try:
        repository().delete_object_schema(object_type)
        return {"status": "deleted", "object_type": object_type}
    except KeyError:
        raise HTTPException(status_code=404, detail="Schema not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/notebooks/{notebook_id}/schema-proposals",
    response_model=List[ObjectSchemaModel],
    dependencies=[Depends(require_notebook_access)],
)
def propose_schemas(notebook_id: str) -> List[ObjectSchemaModel]:
    try:
        return repository().propose_schemas(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.patch("/notebooks/{notebook_id}/knowledge/{knowledge_id}", dependencies=[Depends(require_notebook_access)])
def update_knowledge(notebook_id: str, knowledge_id: str, payload: KnowledgeUpdate):
    try:
        return repository().update_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


_KNOWLEDGE_TYPE_MAP = {
    "rules": "rule",
    "methods": "method",
    "risks": "risk",
    "cases": "case",
    "checklist": "checklist",
    "glossary": "glossary",
}


@router.get("/notebooks/{notebook_id}/duplicates", response_model=List[DuplicateGroup], dependencies=[Depends(require_notebook_read)])
def find_duplicates(notebook_id: str, type: str = Query("rules")) -> List[DuplicateGroup]:
    object_type = _KNOWLEDGE_TYPE_MAP.get(type, type)
    try:
        return repository().find_duplicates(notebook_id, object_type)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")



@router.get("/notebooks/{notebook_id}/graph", response_model=KnowledgeGraph, dependencies=[Depends(require_notebook_read)])
def knowledge_graph(notebook_id: str) -> KnowledgeGraph:
    try:
        return repository().knowledge_graph(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/knowledge/{knowledge_id}/merge", dependencies=[Depends(require_notebook_access)])
def merge_knowledge(notebook_id: str, knowledge_id: str, payload: MergeRequest):
    try:
        return repository().merge_knowledge(notebook_id, knowledge_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/notebooks/{notebook_id}/search", response_model=NotebookSearchResponse, dependencies=[Depends(require_notebook_read)])
def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    try:
        return repository().search_notebook(notebook_id, q)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get(
    "/notebooks/{notebook_id}/kg/search",
    response_model=KgSearchResponse,
    dependencies=[Depends(require_notebook_access)],
)
def kg_search(
    notebook_id: str,
    q: str = Query(...),
    k: int = Query(30, ge=1, le=200),
) -> KgSearchResponse:
    """词法(FTS5)∪语义(ANN)搜索 KG 节点,按 score 降序返回 k 条。"""
    try:
        hits = repository().kg_search(notebook_id, q, k)
        return KgSearchResponse(query=q, hits=hits)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/ask", response_model=AskResponse, dependencies=[Depends(require_notebook_read)])
def ask(notebook_id: str, payload: AskRequest) -> AskResponse:
    try:
        return repository().ask(notebook_id, payload)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/ask-modes")
def ask_modes() -> list[dict[str, Any]]:
    """User-facing ask modes (single source: app/services/ask_modes.py).
    Copy/labels live in the frontend; this exposes ids + behavioural flags."""
    return [
        {"id": m.id, "group": m.group,
         "requires_kg": m.requires_kg, "streaming": m.streaming}
        for m in ASK_MODES.values() if m.user_facing
    ]


def _ndjson_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


async def _stream_ask_events(
    repo: NotebookRepository,
    notebook_id: str,
    payload: AskRequest,
    spec,
    request: Request,
):
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()
    cancel_event = threading.Event()
    events.put({"event": "progress", "step": {
        "step_type": "start", "summary": "启动检索",
        "detail": {"mode": spec.id}}})

    def on_trace(step) -> None:
        if not cancel_event.is_set():
            events.put({"event": "progress", "step": step.model_dump()})

    def worker() -> None:
        try:
            handler = getattr(repo, spec.handler)
            response = handler(
                notebook_id,
                payload,
                on_trace=on_trace,
                cancel_event=cancel_event,
            ) if spec.streaming else handler(
                notebook_id,
                payload,
                cancel_event=cancel_event,
            )
            if not cancel_event.is_set():
                events.put({"event": "final", "response": response.model_dump()})
        except AskCancelled:
            pass
        except Exception as exc:  # noqa: BLE001
            if not cancel_event.is_set():
                events.put({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            events.put(None)

    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(worker), daemon=True).start()
    try:
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                try:
                    event = await asyncio.to_thread(events.get, True, 0.1)
                except queue.Empty:
                    continue
            if event is None:
                break
            yield _ndjson_line(event)
    finally:
        cancel_event.set()


@router.post("/notebooks/{notebook_id}/ask/stream", dependencies=[Depends(require_notebook_read)])
async def ask_stream(notebook_id: str, request: Request, payload: AskRequest) -> StreamingResponse:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        spec = resolve_mode(payload.mode)
    except UnknownAskMode as exc:
        raise HTTPException(status_code=422, detail={
            "error": "unknown ask mode", "mode": exc.mode, "valid": list(ASK_MODES)})
    return StreamingResponse(
        _stream_ask_events(repo, notebook_id, payload, spec, request),
        media_type="application/x-ndjson",
    )


@router.get("/notebooks/{notebook_id}/conversations", response_model=List[ConversationSummary], dependencies=[Depends(require_notebook_read)])
def list_conversations(notebook_id: str) -> List[ConversationSummary]:
    try:
        return repository().list_conversations(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)) -> ConversationDetail:
    if repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return repository().get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest, user: UserProfile = Depends(get_current_user)):
    if repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().rename_conversation(conversation_id, payload.title)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)):
    if repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().delete_conversation(conversation_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/notebooks/{notebook_id}/conversations", dependencies=[Depends(require_notebook_access)])
def bulk_delete_conversations(notebook_id: str, older_than_days: int = Query(..., ge=1)):
    try:
        deleted = repository().bulk_delete_conversations(notebook_id, older_than_days)
        return {"deleted": deleted}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# ---------------------------------------------------------------------------
# Edge trust & curation (Track E)
# ---------------------------------------------------------------------------

@router.get("/notebooks/{notebook_id}/edge-review-queue", response_model=List[EdgeReviewItem], dependencies=[Depends(require_notebook_read)])
def edge_review_queue(notebook_id: str, limit: int = 100) -> List[EdgeReviewItem]:
    """Return edges ranked by review priority (high centrality × low trust) desc.
    Excludes already-rejected edges.
    """
    try:
        return repository().review_queue(notebook_id, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/relations/{rel_id}/review", status_code=200, dependencies=[Depends(require_notebook_access)])
def review_relation(notebook_id: str, rel_id: str,
                    payload: EdgeReviewRequest) -> dict:
    """Mark an edge as 'verified', 'rejected', or 'pending'.
    Rejected edges are excluded from all future graph-reasoning traversals.
    """
    try:
        repository().set_edge_review(notebook_id, rel_id, payload.status)
        return {"rel_id": rel_id, "review_status": payload.status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/notebooks/{notebook_id}/tier", response_model=NotebookSummary, dependencies=[Depends(require_notebook_access)])
def set_notebook_tier(notebook_id: str, payload: SetTierRequest, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """Set a notebook's federation tier: 'base' (authoritative reference KG)
    or 'personal' (default user notes). Drives tier-weighted relevance and
    conflict precedence in ask()."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可设置基准库")
    tier = payload.tier.strip().lower()
    if tier not in {"base", "personal"}:
        raise HTTPException(status_code=400, detail="tier must be 'base' or 'personal'")
    try:
        if tier == "base":
            repository().mark_notebook_base(notebook_id)
        else:
            repository().set_notebook_personal(notebook_id)
        return repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/share", response_model=ShareResponse,
             dependencies=[Depends(require_notebook_access)])
def share_notebook_route(notebook_id: str) -> ShareResponse:
    try:
        return ShareResponse(**repository().share_notebook(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}/share", status_code=204,
               dependencies=[Depends(require_notebook_access)])
def unshare_notebook_route(notebook_id: str) -> None:
    try:
        repository().unshare_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/shared/{token}", response_model=SharedPreview)
def shared_preview_route(token: str, user: UserProfile = Depends(get_current_user)) -> SharedPreview:
    nb_id = repository().find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    return SharedPreview(**repository().shared_preview(nb_id))


@router.post("/shared/{token}/copy", response_model=NotebookSummary)
def copy_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    repo = repository()
    nb_id = repo.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if not repo.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=409, detail="notebook too large to copy")
    return repo.copy_notebook(nb_id, new_owner_id=user.id)


@router.post("/shared/{token}/join", response_model=NotebookSummary)
def join_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    """大库只读加入:凭 share_token 成为只读成员。小库应走 copy 而非 join。"""
    repo = repository()
    nb_id = repo.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if repo.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=400, detail="small notebook — use copy, not join")
    return repo.join_shared(nb_id, user.id)


@router.delete("/notebooks/{notebook_id}/membership", status_code=204)
def leave_notebook_route(notebook_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    """退出只读共享:只删自己的成员记录(幂等,不影响他人)。"""
    repository().leave_notebook(notebook_id, user.id)


@router.post("/notebooks/{notebook_id}/kg/build", dependencies=[Depends(require_notebook_access)])
def build_kg(notebook_id: str) -> dict:
    """按需触发该 notebook 的 KG 建图(后台线程,幂等)。
    已有 knowledge_objects 的 source 会跳过。需 LLM 已配置,否则 409。"""
    repo = repository()
    if not getattr(repo.llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    threading.Thread(target=repo.build_notebook_kg, args=(notebook_id,),
                     name=f"buildkg-{notebook_id}", daemon=True).start()
    return {"status": "building", "notebook_id": notebook_id}


@router.post("/notebooks/{notebook_id}/kg/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_kg(notebook_id: str) -> dict:
    """Full re-extract: clears all KG artefacts then re-extracts ALL sources
    (background thread). Requires LLM configured (409 if not), 404 if notebook
    missing — same guards as build_kg."""
    repo = repository()
    if not getattr(repo.llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    threading.Thread(target=repo.rebuild_notebook_kg, args=(notebook_id,),
                     name=f"rebuildkg-{notebook_id}", daemon=True).start()
    return {"status": "rebuilding", "notebook_id": notebook_id}


@router.post("/notebooks/{notebook_id}/kg/relink", dependencies=[Depends(require_notebook_access)])
def relink_kg(notebook_id: str) -> dict:
    """Deterministic reconnection of isolated KG nodes (synchronous, no LLM).
    Returns {"isolated_before", "edges_added", "isolated_after"}."""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return repo.relink_notebook_kg(notebook_id)


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(answer_id: str, payload: FeedbackRequest, user: UserProfile = Depends(get_current_user)) -> FeedbackResponse:
    if repository().answer_owner(answer_id) != user.id:
        raise HTTPException(status_code=404, detail="Answer not found")
    try:
        return repository().submit_feedback(answer_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Answer not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# Unified Knowledge Graph endpoints
# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/unified-kg/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_unified_kg(notebook_id: str) -> dict:
    try:
        # Explicit user action (刷新图谱): force a full recompute so it also picks
        # up any clustering-settings change the data-version gate can't observe.
        clusters = repository().rebuild_unified_kg(notebook_id, force=True)
        return {"clusters": clusters}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg/status", dependencies=[Depends(require_notebook_read)])
def unified_kg_status(notebook_id: str) -> UnifiedKgStatus:
    try:
        return UnifiedKgStatus(**repository().unified_kg_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/scale-index/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_scale_index(notebook_id: str) -> dict:
    """在线重建 scale 检索索引(base-tier / 已建过;后台任务)。409 若不合格,404 若缺。"""
    try:
        return repository().trigger_scale_index_rebuild(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/notebooks/{notebook_id}/scale-index/status", dependencies=[Depends(require_notebook_access)])
def scale_index_status(notebook_id: str) -> ScaleIndexStatus:
    try:
        return ScaleIndexStatus(**repository().scale_index_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg", dependencies=[Depends(require_notebook_read)])
def get_unified_kg(
    notebook_id: str,
    level: str = Query("concept"),
    limit: Optional[int] = Query(None, ge=1, description="只返回连接度最高的前 N 个节点(核心子图);省略=全量"),
) -> dict:
    try:
        return repository().unified_graph(notebook_id, level=level, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg/pending-merges", dependencies=[Depends(require_notebook_read)])
def get_pending_merges(notebook_id: str) -> list:
    try:
        return repository().pending_merges(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/concepts/{canonical_id}/detail", dependencies=[Depends(require_notebook_read)])
def get_concept_detail(notebook_id: str, canonical_id: str) -> dict:
    try:
        return repository().concept_detail(notebook_id, canonical_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Concept not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/context", dependencies=[Depends(require_notebook_read)])
def object_context(notebook_id: str, object_id: str):
    try:
        return repository().node_context(notebook_id, object_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/neighbors", dependencies=[Depends(require_notebook_read)])
def object_neighbors(
    notebook_id: str,
    object_id: str,
    cap: int = Query(50, ge=1, description="最多返回的 1-hop 邻居数"),
) -> dict:
    """折叠图中某节点的 1-hop 邻域(有界);与 unified-kg 同形(nodes/edges)。"""
    try:
        return repository().kg_neighbors(notebook_id, object_id, cap=cap)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/confirm", dependencies=[Depends(require_notebook_access)])
def confirm_merge(notebook_id: str, candidate_id: str) -> dict:
    try:
        repository().confirm_merge(notebook_id, candidate_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Merge candidate not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/reject", dependencies=[Depends(require_notebook_access)])
def reject_merge(notebook_id: str, candidate_id: str) -> dict:
    try:
        repository().reject_merge(notebook_id, candidate_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Merge candidate not found")


# ---------------------------------------------------------------------------
# KG conflict resolve/review endpoints (Task T6)
# Mirrors the kg/build + concept-merge review patterns above.
# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/kg/conflicts/resolve", dependencies=[Depends(require_notebook_access)])
def resolve_conflicts(notebook_id: str) -> dict:
    """Trigger background conflict resolution for a notebook's KG.

    Mirrors kg/build: 409 if LLM not configured, 404 if notebook missing,
    otherwise starts a daemon thread and returns immediately.
    """
    repo = repository()
    if not getattr(repo.llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    threading.Thread(
        target=repo.resolve_notebook_conflicts,
        args=(notebook_id,),
        name=f"conflictresolve-{notebook_id}",
        daemon=True,
    ).start()
    return {"status": "resolving", "notebook_id": notebook_id}


@router.get("/notebooks/{notebook_id}/kg/conflicts/pending", dependencies=[Depends(require_notebook_read)])
def get_pending_conflicts(notebook_id: str) -> list:
    """Return all pending conflict candidates for a notebook."""
    try:
        return repository().pending_conflicts(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/confirm", dependencies=[Depends(require_notebook_access)])
def confirm_conflict(notebook_id: str, candidate_id: str) -> dict:
    """Apply a pending conflict candidate and mark it as 'applied'."""
    try:
        return repository().confirm_conflict(notebook_id, candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conflict candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/reject", dependencies=[Depends(require_notebook_access)])
def reject_conflict(notebook_id: str, candidate_id: str) -> dict:
    """Reject a pending conflict candidate (no KG mutation)."""
    try:
        repository().reject_conflict(notebook_id, candidate_id)
        return {"status": "rejected", "candidate_id": candidate_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conflict candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/kg/concept-whitelist", response_model=List[ConceptWhitelistEntry])
def list_concept_whitelist() -> List[ConceptWhitelistEntry]:
    return [ConceptWhitelistEntry(**e) for e in repository().concept_whitelist_list()]


@router.post("/kg/concept-whitelist", response_model=ConceptWhitelistEntry)
def add_concept_whitelist(payload: ConceptWhitelistAdd, user: UserProfile = Depends(get_current_user)) -> ConceptWhitelistEntry:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改全局配置")
    try:
        return ConceptWhitelistEntry(**repository().concept_whitelist_add(payload.term, payload.note))
    except ValueError:
        raise HTTPException(status_code=400, detail="term must be non-empty")


@router.delete("/kg/concept-whitelist/{term}", status_code=204)
def delete_concept_whitelist(term: str, user: UserProfile = Depends(get_current_user)) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修改全局配置")
    repository().concept_whitelist_remove(term)


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review", dependencies=[Depends(require_notebook_access)])
def review_unified_kg_merges(notebook_id: str, payload: MergeReviewRequest) -> MergeReviewSummary:
    try:
        return MergeReviewSummary(**repository().review_pending_merges(
            notebook_id,
            limit=payload.limit,
            confirm_threshold=payload.confirm_threshold,
            separate_threshold=payload.separate_threshold,
        ))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Governance: promotion queue (Track F) ------------------------------


@router.post(
    "/notebooks/{notebook_id}/knowledge/{knowledge_id}/promote",
    response_model=PromotionCandidate,
    status_code=201,
    dependencies=[Depends(require_notebook_access)],
)
def propose_promotion(notebook_id: str, knowledge_id: str) -> PromotionCandidate:
    try:
        return PromotionCandidate(**repository().propose_promotion(notebook_id, knowledge_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook or knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/promotion-queue", response_model=List[PromotionCandidate])
def list_promotion_queue(status: str = Query(None), user: UserProfile = Depends(get_current_user)) -> List[PromotionCandidate]:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理晋升队列")
    return [
        PromotionCandidate(**c)
        for c in repository().list_promotion_queue(status_filter=status)
    ]


@router.post(
    "/promotion-queue/{candidate_id}/approve",
    response_model=PromotionApproveResult,
)
def approve_promotion(candidate_id: str, user: UserProfile = Depends(get_current_user)) -> PromotionApproveResult:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理晋升队列")
    try:
        return PromotionApproveResult(**repository().approve_promotion(candidate_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Promotion candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/promotion-queue/{candidate_id}/reject",
    response_model=PromotionCandidate,
)
def reject_promotion(candidate_id: str, payload: PromotionRejectRequest, user: UserProfile = Depends(get_current_user)) -> PromotionCandidate:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可管理晋升队列")
    try:
        return PromotionCandidate(
            **repository().reject_promotion(candidate_id, reason=payload.reason)
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Promotion candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
