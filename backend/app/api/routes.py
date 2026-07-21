import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    admin_query_repository,
    repository,
    require_notebook_access, require_notebook_read,
    get_current_user, user_error,
)
from app.api.ask_routes import router as ask_router
from app.models.identity import UserProfile
from app.models.admin import (
    AdminUserNotebook,
    AdminUserUsage,
    PromoteRequest,
    PromotionApproveResult,
    PromotionCandidate,
    PromotionRejectRequest,
)
from app.models.ask import KgSearchResponse
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
from app.services.pending_bus import pending_bus
from app.api.knowhow_routes import router as knowhow_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import memory_router
from app.api.notebook_routes import router as notebook_router
from app.api.source_routes import router as source_router
from app.api.system_routes import router as system_router
from app.api.report_routes import router as report_router
from app.repositories.sqlite.kg_build_job_store import KgBuildAlreadyRunning

router = APIRouter()
router.include_router(memory_router)
router.include_router(system_router)
router.include_router(notebook_router)
router.include_router(source_router)
router.include_router(knowhow_router)
router.include_router(knowledge_router)
router.include_router(ask_router)
router.include_router(report_router)




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
