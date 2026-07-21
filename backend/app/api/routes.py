import asyncio
import io
import json
import queue
import zipfile
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import (
    admin_query_repository,
    notebook_access_repository, notebook_catalog_repository, repository,
    require_notebook_access, require_notebook_read, require_notebook_write,
    get_current_user, user_error,
)
from app.models.identity import UserProfile
from app.models.reports import (
    ReportCreate,
    ReportDetail,
    ReportExportRequest,
    ReportGenerateRequest,
    ReportOutlineUpdate,
    ReportSummary,
)
from app.models.admin import (
    AdminUserNotebook,
    AdminUserUsage,
    PromoteRequest,
    PromotionApproveResult,
    PromotionCandidate,
    PromotionRejectRequest,
)
from app.models.ask import (
    AskRequest,
    AskResponse,
    ConversationDetail,
    ConversationRenameRequest,
    ConversationSummary,
    FeedbackRequest,
    FeedbackResponse,
    KgSearchResponse,
    NotebookSearchResponse,
)
from app.models.kg import (
    ConceptWhitelistAdd,
    ConceptWhitelistEntry,
    MergeReviewJob,
    MergeReviewRequest,
    MergeReviewSummary,
    RebuildScaleIndexRequest,
    ScaleIndexStatus,
    UnifiedKgStatus,
)
from app.services import background_jobs
from app.services.ask_modes import resolve_mode, UnknownAskMode, ASK_MODES
from app.services.pending_bus import pending_bus
from app.repositories.ports import AskStreamPort
from app.api.knowhow_routes import router as knowhow_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import memory_router
from app.api.notebook_routes import router as notebook_router
from app.api.source_routes import router as source_router
from app.api.system_routes import router as system_router
from app.repositories.sqlite.kg_build_job_store import KgBuildAlreadyRunning

router = APIRouter()
router.include_router(memory_router)
router.include_router(system_router)
router.include_router(notebook_router)
router.include_router(source_router)
router.include_router(knowhow_router)
router.include_router(knowledge_router)




@router.get("/notebooks/{notebook_id}/search", response_model=NotebookSearchResponse, dependencies=[Depends(require_notebook_read)])
def search_notebook(
    notebook_id: str,
    q: str = Query(""),
) -> NotebookSearchResponse:
    try:
        return notebook_catalog_repository().search_notebook(notebook_id, q)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get(
    "/notebooks/{notebook_id}/kg/search",
    response_model=KgSearchResponse,
    dependencies=[Depends(require_notebook_read)],  # 只读 KG 搜索,成员可(与 /search、/graph 一致)
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
    repo: AskStreamPort,
    notebook_id: str,
    payload: AskRequest,
    spec,
    request: Request,
):
    # Task 23: 执行编排(begin→register→started→合成 start→copy_context worker→
    # trace 持久化 fail-open→finish→unregister→空会话清理→终态事件→哨兵)整体在
    # runtime-owned AskExecutionCoordinator;本函数保留冻结签名,只剩启动编排、
    # 交付队列消费与断连轮询。Task 24: 执行体 = runtime-owned AskService(三模式
    # 注册表派发在服务内),不再是 facade runner 回调。
    events = repo.start_ask_stream(
        notebook_id, payload, spec,
        user_id=repo.current_user().id,
    )
    # 客户端断连只停止本次流(break),**不** set cancel_event —— worker 脱离连接
    # 跑到完、答案照存。唯一取消入口是 POST …/ask/jobs/{job_id}/cancel。
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(events.get, True, 0.1)
            except queue.Empty:
                continue
        if event is None:
            break
        yield _ndjson_line(event)


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


@router.post("/notebooks/{notebook_id}/ask/jobs/{job_id}/cancel",
             dependencies=[Depends(require_notebook_read)])
def cancel_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        return repo.cancel_ask_job(job_id, repo.current_user().id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")


@router.get("/notebooks/{notebook_id}/ask/jobs/{job_id}",
            dependencies=[Depends(require_notebook_read)])
def get_ask_job(notebook_id: str, job_id: str) -> dict:
    repo = repository()
    try:
        detail = repo.ask_job_detail(job_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="ask job not found")
    if detail["created_by"] != repo.current_user().id:
        raise HTTPException(status_code=404, detail="ask job not found")
    return detail


@router.get("/notebooks/{notebook_id}/conversations", response_model=List[ConversationSummary], dependencies=[Depends(require_notebook_read)])
def list_conversations(notebook_id: str) -> List[ConversationSummary]:
    try:
        return repository().list_conversations(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)) -> ConversationDetail:
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        return repository().get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().rename_conversation(conversation_id, payload.title)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: UserProfile = Depends(get_current_user)):
    if notebook_access_repository().conversation_owner(conversation_id) != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        repository().delete_conversation(conversation_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.delete("/notebooks/{notebook_id}/conversations", dependencies=[Depends(require_notebook_read)])  # 仓库层按 created_by scope,成员删自己的旧会话
def bulk_delete_conversations(notebook_id: str, older_than_days: int = Query(..., ge=1)):
    try:
        deleted = repository().bulk_delete_conversations(notebook_id, older_than_days)
        return {"deleted": deleted}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/kg/build", dependencies=[Depends(require_notebook_access)])
def build_kg(notebook_id: str) -> dict:
    """按需触发该 notebook 的 KG 建图(后台线程,幂等)。
    已有 knowledge_objects 的 source 会跳过。需 LLM 已配置,否则 409。"""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not getattr(repo.kg_llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        job = repo.prepare_notebook_kg_job(notebook_id, "incremental")
    except KgBuildAlreadyRunning:
        raise user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    try:
        background_jobs.submit(
            repo.execute_notebook_kg_job,
            notebook_id,
            job["id"],
            "incremental",
            name=f"buildkg-{notebook_id}",
            notify_pending=True,
        )
    except Exception:
        repo.fail_notebook_kg_job_submission(job["id"])
        raise
    return {
        "status": "building",
        "notebook_id": notebook_id,
        "job_id": job["id"],
    }


@router.post("/notebooks/{notebook_id}/kg/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_kg(notebook_id: str) -> dict:
    """Full re-extract: clears all KG artefacts then re-extracts ALL sources
    (background thread). Requires LLM configured (409 if not), 404 if notebook
    missing — same guards as build_kg."""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not getattr(repo.kg_llm_client, "configured", False):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        job = repo.prepare_notebook_kg_job(notebook_id, "rebuild")
    except KgBuildAlreadyRunning:
        raise user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    try:
        background_jobs.submit(
            repo.execute_notebook_kg_job,
            notebook_id,
            job["id"],
            "rebuild",
            name=f"rebuildkg-{notebook_id}",
            notify_pending=True,
        )
    except Exception:
        repo.fail_notebook_kg_job_submission(job["id"])
        raise
    return {
        "status": "rebuilding",
        "notebook_id": notebook_id,
        "job_id": job["id"],
    }


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


# --- 深度报告(异步后台 job,轮询取状态) -------------------------------


def _report_llm_ready(repo) -> bool:
    return bool(getattr(repo.reasoning_llm_client, "configured", False))


def _launch_plan_job(repo, notebook_id: str, rid: str, question: str, history: str,
                     auto_generate: bool = False) -> None:
    """阶段1(规划)后台 job:跑 plan_outline → outline_ready;auto_generate 时
    在同一 worker 内接生成(一键直出)。depth 从 report 行读(创建时已落库)。
    Task 25:helper 名保留(测试 monkeypatch 位),编排移交运行时协调器——
    注册取消(submit 前)→ background_jobs.submit(copy_context)→ 收尾注销。"""
    repo.report_execution.start_plan(
        notebook_id, rid, question, history, auto_generate,
        user_id=repo.current_user().id)


def _launch_generate_job(repo, notebook_id: str, rid: str, question: str,
                         depth: int = 2) -> None:
    """阶段2(生成)后台 job:用已确认的 outline 跑 generate → done。"""
    repo.report_execution.start_generate(
        notebook_id, rid, question, depth,
        user_id=repo.current_user().id)


@router.post("/notebooks/{notebook_id}/reports",
             dependencies=[Depends(require_notebook_write)])
def create_report(notebook_id: str, payload: ReportCreate) -> dict:
    repo = repository()
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="question required")
    if not _report_llm_ready(repo):
        raise HTTPException(status_code=409, detail="LLM not configured")
    depth = max(1, min(16, int(payload.depth)))
    try:
        rid = repo.create_report(notebook_id, payload.question.strip(), depth=depth)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    _launch_plan_job(repo, notebook_id, rid, payload.question.strip(), payload.history,
                     payload.auto_generate)
    return {"report_id": rid, "status": "pending"}


@router.get("/notebooks/{notebook_id}/reports",
            dependencies=[Depends(require_notebook_read)])
def list_reports(notebook_id: str) -> List[ReportSummary]:
    # repo 行含 notebook_id/updated_at 等多余键 → 按模型字段过滤(仓库无 extra=ignore 风格)
    return [ReportSummary(**{k: v for k, v in r.items() if k in ReportSummary.model_fields})
            for r in repository().list_reports(notebook_id)]


@router.post("/notebooks/{notebook_id}/reports/export",
             dependencies=[Depends(require_notebook_read)])
def export_reports_endpoint(notebook_id: str, payload: ReportExportRequest) -> StreamingResponse:
    # owner∪成员可下(require_notebook_read);只导出该 notebook 下 status='done' 且
    # content_md 非空的报告,非 done/空/跨 notebook 的 id 静默跳过(repo 层已过滤)。
    rows = repository().export_reports(notebook_id, payload.report_ids)
    if not rows:                                 # 空 report_ids 或全部无效
        raise HTTPException(status_code=422, detail="no exportable reports")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, md in rows:
            z.writestr(name, md)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="reports.zip"'})


@router.get("/notebooks/{notebook_id}/reports/{report_id}",
            dependencies=[Depends(require_notebook_read)])
def get_report(notebook_id: str, report_id: str) -> ReportDetail:
    try:
        r = repository().get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    return ReportDetail(**{k: v for k, v in r.items() if k in ReportDetail.model_fields})


@router.patch("/notebooks/{notebook_id}/reports/{report_id}/outline",
              dependencies=[Depends(require_notebook_write)])
def update_report_outline(notebook_id: str, report_id: str, payload: ReportOutlineUpdate) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="outline editable only when outline_ready")
    secs = [s for s in payload.sections
            if str(s.get("title", "")).strip() and (s.get("sub_queries") or [])]
    if not secs:
        raise HTTPException(status_code=422, detail="at least one valid section required")
    repo.update_report(notebook_id, report_id, outline=secs)
    return {"status": "ok", "sections": len(secs)}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/generate",
             dependencies=[Depends(require_notebook_write)])
def generate_report(notebook_id: str, report_id: str, payload: ReportGenerateRequest) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="generate only from outline_ready")
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    _launch_generate_job(repo, notebook_id, report_id, cur["question"], depth)
    return {"status": "generating"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/cancel",
             dependencies=[Depends(require_notebook_write)])
def cancel_report_endpoint(notebook_id: str, report_id: str) -> dict:
    from app.services.report_engine import cancel_report as _cancel
    live = _cancel(report_id)
    if not live:                               # 线程已结束/不存在:直接落库标记
        try:
            repository().update_report(notebook_id, report_id, status="cancelled",
                                       progress="已取消")
        except Exception:
            raise HTTPException(status_code=404, detail="Report not found")
    return {"status": "cancelling" if live else "cancelled"}


@router.delete("/notebooks/{notebook_id}/reports/{report_id}",
               dependencies=[Depends(require_notebook_write)])
def delete_report(notebook_id: str, report_id: str) -> dict:
    repository().delete_report(notebook_id, report_id)
    return {"status": "deleted"}


@router.post("/answers/{answer_id}/feedback", response_model=FeedbackResponse)
def submit_feedback(answer_id: str, payload: FeedbackRequest, user: UserProfile = Depends(get_current_user)) -> FeedbackResponse:
    if not notebook_access_repository().user_can_read_answer(answer_id, user.id):  # owner ∪ 成员(spec §3.3)
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
        # 刷新图谱:走版本门控(force=False)——输入未变则跳过重聚类,只增量重建社区
        # (纯图/无 LLM/秒级);有新内容才重聚。强制全量重聚(如改了聚类设置)用
        # scripts/recluster_kg.py。这兑现「判断:只需重建社区就跳过其他动作」。
        clusters = repository().rebuild_unified_kg(notebook_id, force=False)
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
def rebuild_scale_index(notebook_id: str, body: RebuildScaleIndexRequest = RebuildScaleIndexRequest()) -> dict:
    """在线重建 scale 检索索引(base-tier / 已建过)。when=now 立即后台/idle 低峰调度;
    mode=auto(fold/full 自选)|fold|full。400 若参数非法,409 若不合格,404 若缺。"""
    if body.when not in ("now", "idle"):
        raise HTTPException(status_code=400, detail="when must be one of: now, idle")
    if body.mode not in ("auto", "fold", "full"):
        raise HTTPException(status_code=400, detail="mode must be one of: auto, fold, full")
    try:
        return repository().trigger_scale_index_rebuild(notebook_id, when=body.when, mode=body.mode)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/notebooks/{notebook_id}/scale-index/cancel", dependencies=[Depends(require_notebook_access)])
def cancel_scale_index(notebook_id: str) -> dict:
    """取消检索索引:排队中→出队;构建中→拒绝(不可打断)。"""
    try:
        return repository().cancel_scale_index(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/scale-index/status", dependencies=[Depends(require_notebook_access)])
def scale_index_status(notebook_id: str) -> ScaleIndexStatus:
    try:
        return ScaleIndexStatus(**repository().scale_index_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/index-status", dependencies=[Depends(require_notebook_read)])
def index_status(notebook_id: str) -> dict:
    """三系统构建状态聚合(kg/unified_kg/scale_index)。纯读:与 /analytics、
    /unified-kg/status 一致用 read 守卫——只读成员也要能看面板里的状态位,
    写操作(build/rebuild/cancel)各自仍是 require_notebook_access 且前端按 !isReader 隐藏按钮。"""
    try:
        return repository().index_status(notebook_id)
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
    background_jobs.submit(repo.resolve_notebook_conflicts, notebook_id,
                           name=f"conflictresolve-{notebook_id}", notify_pending=True)
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
        raise user_error(403, "仅管理员可修改全局配置")
    try:
        return ConceptWhitelistEntry(**repository().concept_whitelist_add(payload.term, payload.note))
    except ValueError:
        raise HTTPException(status_code=400, detail="term must be non-empty")


@router.delete("/kg/concept-whitelist/{term}", status_code=204)
def delete_concept_whitelist(term: str, user: UserProfile = Depends(get_current_user)) -> None:
    if user.role != "admin":
        raise user_error(403, "仅管理员可修改全局配置")
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


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review-all", dependencies=[Depends(require_notebook_access)])
def review_all_unified_kg_merges(notebook_id: str) -> dict:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if repo.merge_review_job_status(notebook_id)["status"] == "running":
        return {"status": "running"}
    background_jobs.submit(repo.run_merge_review_job, notebook_id,
                           name=f"mergereview-{notebook_id}", notify_pending=True)
    return {"status": "started"}


@router.get("/notebooks/{notebook_id}/unified-kg/merges/review-job", dependencies=[Depends(require_notebook_access)])
def merge_review_job(notebook_id: str) -> MergeReviewJob:
    try:
        return MergeReviewJob(**repository().merge_review_job_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# --- Governance: promotion queue (Track F) ------------------------------


@router.post(
    "/notebooks/{notebook_id}/knowledge/{knowledge_id}/promote",
    response_model=PromotionCandidate,
    status_code=201,
    dependencies=[Depends(require_notebook_access)],
)
def propose_promotion(
    notebook_id: str, knowledge_id: str, payload: PromoteRequest = PromoteRequest()
) -> PromotionCandidate:
    try:
        return PromotionCandidate(**repository().propose_promotion(
            notebook_id, knowledge_id, target_base_id=payload.target_base_id
        ))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook or knowledge object not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/promotion-queue", response_model=List[PromotionCandidate])
def list_promotion_queue(status: str = Query(None), user: UserProfile = Depends(get_current_user)) -> List[PromotionCandidate]:
    if user.role != "admin":
        raise user_error(403, "仅管理员可管理内容审核队列")
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
        raise user_error(403, "仅管理员可管理内容审核队列")
    try:
        return PromotionApproveResult(
            **repository().approve_promotion_as_reviewer(candidate_id, user.id)
        )
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
        raise user_error(403, "仅管理员可管理内容审核队列")
    try:
        return PromotionCandidate(
            **repository().reject_promotion_as_reviewer(
                candidate_id, payload.reason, user.id
            )
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Promotion candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/admin/users", response_model=List[AdminUserUsage])
async def list_admin_users(user: UserProfile = Depends(get_current_user)) -> List[AdminUserUsage]:
    """管理员用户使用总览:所有用户 + 用量统计 + 当前在线。仅 admin。
    重的用量聚合放线程池,回 loop 线程读 pending_bus(免锁快照)。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看用户总览")
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, admin_query_repository().list_user_usage)
    online = pending_bus.online_user_ids()
    return [AdminUserUsage(**row, is_online=row["id"] in online) for row in rows]


@router.get("/admin/users/{user_id}/notebooks", response_model=List[AdminUserNotebook])
def list_admin_user_notebooks(user_id: str, user: UserProfile = Depends(get_current_user)) -> List[AdminUserNotebook]:
    """某用户名下笔记本详情。仅 admin。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看用户笔记本")
    return [
        AdminUserNotebook(**row)
        for row in admin_query_repository().list_user_notebooks(user_id)
    ]


@router.get("/admin/online")
async def list_online_users(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前在线用户 id 集合(持有实时流连接)。仅 admin,纯读内存零 DB。"""
    if user.role != "admin":
        raise user_error(403, "仅管理员可查看在线状态")
    return {"online_ids": sorted(pending_bus.online_user_ids())}




from app.api.content_overview_routes import router as content_overview_router  # noqa: E402
from app.api.deps import content_overview_service  # noqa: E402

router.include_router(content_overview_router)
