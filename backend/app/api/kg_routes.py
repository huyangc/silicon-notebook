from dataclasses import asdict
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import (
    get_current_user,
    kg_analysis_service,
    repository,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.models.ask import KgSearchResponse
from app.models.identity import UserProfile
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
from app.models.kg_analysis import KgAnalysisResponse, SourceProfilePageResponse
from app.repositories.ports import KgBuildAlreadyRunning, KgMaintenanceAlreadyRunning
from app.services import background_jobs
from app.services.knowledge_contracts import (
    COMMUNITY_OVERVIEW_MAX,
    COMMUNITY_TOP_MEMBERS_MAX,
    CONCEPT_DETAIL_PAGE_MAX,
    KG_COMMUNITY_EDGES_MAX,
    KG_SOURCE_PAGE_MAX,
)


router = APIRouter()


# 「补上关联」与「重新合并」共用一个 per-notebook 单飞槽(两者都重写派生的聚类/板块
# 产物,见 KgMaintenanceAlreadyRunning)。409 必须点名**真正占着槽**的那个动作,否则
# 用户会盯着自己没点过的按钮等——所以文案按 holder 分支,而不是按被拒的那个端点写死。
# 用词与按钮上的界面词逐字一致;三条都是中文字面量而不是查一张表,好让
# test_user_error 的 AST 守卫能直接看见它们(动态实参那条路要登记进 allowlist,
# 为省三行重复而换来一条豁免不划算)。
def _kg_maintenance_busy(exc: KgMaintenanceAlreadyRunning) -> HTTPException:
    if exc.holder == "rebuild":
        return user_error(409, "当前笔记本正在重新合并，请等它完成")
    if exc.holder == "relink":
        return user_error(409, "当前笔记本正在补上关联，请等它完成")
    if exc.holder == "buildkg":
        # 批 3·W2 §2.1:维护动作被在飞的分析作业闸住——文案与 build 侧
        # 409 逐字同款,用户等的是同一件事。
        return user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    return user_error(409, "当前笔记本正在整理知识图谱，请等它完成")


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


@router.post("/notebooks/{notebook_id}/kg/build", dependencies=[Depends(require_notebook_capability("kg:write"))])
def build_kg(notebook_id: str) -> dict:
    """按需触发该 notebook 的 KG 建图(后台线程,幂等)。
    已有 knowledge_objects 的 source 会跳过。需 LLM 已配置,否则 409。"""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not repo._runtime.models.configured("kg_extract"):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        job = repo.prepare_notebook_kg_job(
            notebook_id, "incremental", retry_partial=True
        )
    except KgBuildAlreadyRunning:
        raise user_error(409, "当前笔记本已有知识图谱分析任务正在运行")
    try:
        background_jobs.submit(
            repo.execute_notebook_kg_job,
            notebook_id,
            job["id"],
            "incremental",
            retry_partial=True,
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


@router.post("/notebooks/{notebook_id}/kg/rebuild", dependencies=[Depends(require_notebook_capability("kg:write"))])
def rebuild_kg(notebook_id: str) -> dict:
    """Full re-extract: clears all KG artefacts then re-extracts ALL sources
    (background thread). Requires LLM configured (409 if not), 404 if notebook
    missing — same guards as build_kg."""
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not repo._runtime.models.configured("kg_extract"):
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


@router.post("/notebooks/{notebook_id}/kg/relink", dependencies=[Depends(require_notebook_capability("kg:write"))])
def relink_kg(notebook_id: str) -> dict:
    """Deterministic reconnection of isolated KG nodes (background thread, no LLM).

    Same shape as build/rebuild above. It runs off the request thread because the
    work is proportional to the notebook, not to the click: on a large graph the
    synchronous version outlived PostgreSQL's statement timeout and pinned a
    request worker for the whole run. Single flight is per notebook (409), so a
    second click cannot queue a duplicate pass. No LLM gate — relink is
    deterministic and must keep working on deployments with no model configured.
    """
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        job = repo.start_notebook_relink(notebook_id)
    except KgMaintenanceAlreadyRunning as exc:
        raise _kg_maintenance_busy(exc)
    try:
        background_jobs.submit(
            repo.run_notebook_relink_job,
            notebook_id,
            job["job_id"],
            name=f"relinkkg-{notebook_id}",
        )
    except Exception:
        repo.fail_notebook_relink_submission(notebook_id, job["job_id"])
        raise
    return {
        "status": "relinking",
        "notebook_id": notebook_id,
        "job_id": job["job_id"],
    }


@router.get(
    "/notebooks/{notebook_id}/kg/relink/status",
    dependencies=[Depends(require_notebook_read)],
)
def relink_kg_status(notebook_id: str) -> dict:
    """Latest relink state for this notebook — the browser's completion signal.

    ``status`` is one of running / succeeded / failed / idle; ``idle`` covers both
    "never ran here" and "the process that ran it restarted", so a bounded poll
    always terminates. Counters are zero until a run finishes. No error text
    crosses this boundary — diagnostics stay in the event log.
    """
    try:
        return repository().notebook_relink_status(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# ---------------------------------------------------------------------------
# Unified Knowledge Graph endpoints
# ---------------------------------------------------------------------------


@router.post("/notebooks/{notebook_id}/unified-kg/rebuild", dependencies=[Depends(require_notebook_capability("kg:write"))])
def rebuild_unified_kg(notebook_id: str) -> dict:
    """「重新合并」(background thread). Same shape as kg/relink above.

    It runs off the request thread for the same reason relink does: the work is
    proportional to the notebook, not to the click. The version gate (force=False,
    applied inside `run_unified_kg_rebuild_job`) still makes an unchanged notebook
    answer in milliseconds, but a real recluster streams seed representatives over
    the whole graph — minutes to hours on a base-tier library, well past
    PostgreSQL's statement timeout, with a request worker pinned for the duration.
    No LLM PRECONDITION gate — unlike kg/build, this endpoint does not require a
    model to be configured and keeps working without one. That is not the same as
    the pass being purely deterministic: when `kg_merge_review` and/or
    `kg_concept_description` ARE configured, `rebuild_unified_kg` calls them as a
    fail-open enhancement (candidate-pair adjudication, canonical description
    generation) — an unexpected failure there is swallowed and the rebuild still
    proceeds, but a configured model genuinely gets called. Single flight is
    shared with relink (409, see `_kg_maintenance_busy`), so neither pass can
    publish over the other's inputs.
    The old synchronous `{"clusters": N}` is gone — the count now arrives from
    `unified-kg/rebuild/status` once the pass actually finishes.
    """
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    try:
        job = repo.start_unified_kg_rebuild(notebook_id)
    except KgMaintenanceAlreadyRunning as exc:
        raise _kg_maintenance_busy(exc)
    try:
        background_jobs.submit(
            repo.run_unified_kg_rebuild_job,
            notebook_id,
            job["job_id"],
            name=f"unifiedkg-{notebook_id}",
        )
    except Exception:
        repo.fail_unified_kg_rebuild_submission(notebook_id, job["job_id"])
        raise
    return {
        "status": "rebuilding",
        "notebook_id": notebook_id,
        "job_id": job["job_id"],
    }


@router.get(
    "/notebooks/{notebook_id}/unified-kg/rebuild/status",
    dependencies=[Depends(require_notebook_read)],
)
def unified_kg_rebuild_status(notebook_id: str) -> dict:
    """Latest 「重新合并」 state for this notebook — the browser's completion signal.

    Mirrors kg/relink/status: ``status`` is running / succeeded / failed / idle,
    where ``idle`` covers "never ran here", "the process that ran it restarted"
    AND "the shared slot is held by a relink pass", so a bounded poll always
    terminates. ``clusters`` is zero until a run finishes. No error text crosses
    this boundary — diagnostics stay in the event log.

    Deliberately separate from `unified-kg/status`: that one reports the derived
    state of the graph (dirty / last_rebuild_at / viz artefacts) and its
    ``building`` flag is about the visualization artefact, not this pass.
    """
    try:
        return repository().unified_kg_rebuild_status(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/unified-kg/status", dependencies=[Depends(require_notebook_read)])
def unified_kg_status(notebook_id: str) -> UnifiedKgStatus:
    try:
        return UnifiedKgStatus(**repository().unified_kg_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/scale-index/rebuild", dependencies=[Depends(require_notebook_capability("kg:write"))])
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


@router.post("/notebooks/{notebook_id}/scale-index/cancel", dependencies=[Depends(require_notebook_capability("kg:write"))])
def cancel_scale_index(notebook_id: str) -> dict:
    """取消检索索引:排队中→出队;构建中→拒绝(不可打断)。"""
    try:
        return repository().cancel_scale_index(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/scale-index/status", dependencies=[Depends(require_notebook_capability("kg:write"))])
def scale_index_status(notebook_id: str) -> ScaleIndexStatus:
    try:
        return ScaleIndexStatus(**repository().scale_index_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/notebooks/{notebook_id}/index-status", dependencies=[Depends(require_notebook_read)])
def index_status(notebook_id: str) -> dict:
    """三系统构建状态聚合(kg/unified_kg/scale_index)。纯读:与 /analytics、
    /unified-kg/status 一致用 read 守卫——只读成员也要能看面板里的状态位,
    写操作(build/rebuild/cancel)各自仍是 require_notebook_capability("kg:write")
    (P0 阶段解析到 owner-only)且前端按 !isReader 隐藏按钮。"""
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
def get_concept_detail(
    notebook_id: str,
    canonical_id: str,
    source_notebook_id: str = Query(""),
    limit: int = Query(
        CONCEPT_DETAIL_PAGE_MAX, ge=1, le=CONCEPT_DETAIL_PAGE_MAX,
        description="hub 簇成员分页大小(keyset);默认与上限都是一页",
    ),
    after: str = Query("", description="上一页最后一个成员 id(keyset 游标)"),
) -> dict:
    try:
        return repository().concept_detail(
            notebook_id,
            canonical_id,
            source_notebook_id=source_notebook_id or notebook_id,
            limit=limit,
            after=after,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Concept not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/context", dependencies=[Depends(require_notebook_read)])
def object_context(
    notebook_id: str,
    object_id: str,
    source_notebook_id: str = Query(""),
):
    try:
        return repository().node_context(
            notebook_id,
            object_id,
            source_notebook_id=source_notebook_id or notebook_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Object not found")


@router.get("/notebooks/{notebook_id}/objects/{object_id}/neighbors", dependencies=[Depends(require_notebook_read)])
def object_neighbors(
    notebook_id: str,
    object_id: str,
    cap: int = Query(50, ge=1, description="最多返回的 1-hop 邻居数"),
    source_notebook_id: str = Query(""),
) -> dict:
    """折叠图中某节点的 1-hop 邻域(有界);与 unified-kg 同形(nodes/edges)。"""
    try:
        return repository().kg_neighbors(
            notebook_id,
            object_id,
            cap=cap,
            source_notebook_id=source_notebook_id or notebook_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/confirm", dependencies=[Depends(require_notebook_capability("kg:write"))])
def confirm_merge(notebook_id: str, candidate_id: str) -> dict:
    try:
        repository().confirm_merge(notebook_id, candidate_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Merge candidate not found")


@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/reject", dependencies=[Depends(require_notebook_capability("kg:write"))])
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


@router.post("/notebooks/{notebook_id}/kg/conflicts/resolve", dependencies=[Depends(require_notebook_capability("kg:write"))])
def resolve_conflicts(notebook_id: str) -> dict:
    """Trigger background conflict resolution for a notebook's KG.

    Mirrors kg/build: 409 if LLM not configured, 404 if notebook missing,
    otherwise queues the job on the fixed heavy-maintenance workers and returns
    immediately.

    Two admission gates sit in front of that thread. Single flight is per
    notebook (its own slot, not relink/rebuild's), so a second click cannot
    enqueue a duplicate pass over the same graph. Size admission refuses
    notebooks whose active object or relation count exceeds its configured rail:
    detection is superlinear in the graph and every surviving candidate costs an
    LLM call, so accepting such a notebook would only mean a background job that
    never finishes and a bill that keeps growing. The post-build conflict tail
    shares both gates through the same methods, so neither can be reached around.
    """
    repo = repository()
    if not repo._runtime.models.configured("kg_conflict_review"):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
        admitted = repo.conflict_resolution_admitted(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not admitted:
        raise user_error(409, "当前笔记本知识图谱规模过大，暂不支持自动冲突检测")
    try:
        job = repo.start_conflict_resolution(notebook_id)
    except KgMaintenanceAlreadyRunning:
        raise user_error(409, "当前笔记本正在检测知识冲突，请等它完成")
    try:
        background_jobs.submit(
            repo.run_conflict_resolution_job,
            notebook_id,
            job["job_id"],
            name=f"conflictresolve-{notebook_id}",
            notify_pending=True,
        )
    except Exception:
        repo.fail_conflict_resolution_submission(notebook_id, job["job_id"])
        raise
    return {
        "status": "resolving",
        "notebook_id": notebook_id,
        "job_id": job["job_id"],
    }


@router.get("/notebooks/{notebook_id}/kg/conflicts/pending", dependencies=[Depends(require_notebook_read)])
def get_pending_conflicts(notebook_id: str) -> list:
    """Return all pending conflict candidates for a notebook."""
    try:
        return repository().pending_conflicts(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/confirm", dependencies=[Depends(require_notebook_capability("kg:write"))])
def confirm_conflict(notebook_id: str, candidate_id: str) -> dict:
    """Apply a pending conflict candidate and mark it as 'applied'."""
    try:
        return repository().confirm_conflict(notebook_id, candidate_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conflict candidate not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/notebooks/{notebook_id}/kg/conflicts/{candidate_id}/reject", dependencies=[Depends(require_notebook_capability("kg:write"))])
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


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review", dependencies=[Depends(require_notebook_capability("kg:write"))])
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


@router.post("/notebooks/{notebook_id}/unified-kg/merges/review-all", dependencies=[Depends(require_notebook_capability("kg:write"))])
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


@router.get("/notebooks/{notebook_id}/unified-kg/merges/review-job", dependencies=[Depends(require_notebook_capability("kg:write"))])
def merge_review_job(notebook_id: str) -> MergeReviewJob:
    try:
        return MergeReviewJob(**repository().merge_review_job_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


# ---------------------------------------------------------------------------
# KG 质量分析视图(T3)。两个端点都是**纯只读**:只读 T2 落库的预计算产物 + 板块表,
# 一次都不跑那三条全表重活(生产库 200 万簇行 / 836 万边)。`require_notebook_read`
# 守卫(只读成员也能看,与 /checkup、/analytics 一致);响应里全是内部代号,界面词
# 由 T4 的前端映射。


@router.get(
    "/notebooks/{notebook_id}/kg-analysis",
    response_model=KgAnalysisResponse,
    dependencies=[Depends(require_notebook_read)],
)
def kg_analysis_overview(
    notebook_id: str,
    boards: int = Query(50, ge=1, le=COMMUNITY_OVERVIEW_MAX),
    top_members: int = Query(5, ge=1, le=COMMUNITY_TOP_MEMBERS_MAX),
    edges: int = Query(200, ge=1, le=KG_COMMUNITY_EDGES_MAX),
) -> KgAnalysisResponse:
    """KG 质量分析总览:状态 + 五份预计算产物 + 主题板块列表 + 跨板块边 top-N。

    三个上限都在这里声明(FastAPI 直接 422 掉越界值),service 与 store 各自还会再
    clamp 一次 —— 纵深而不是重复:越界在这一层是**用户输入错误**(响亮拒绝),在下面
    两层是**不变式**(任何调用方都不可能拿到无界返回)。

    ``level`` **刻意不是参数**:三张产物表都没有 level 维度,让调用方指定只会造出
    「板块列表是 level 1、跨板块边是 level 0」这种自相矛盾的报告。service 从账本里
    读出产物描述的那个 level 并回报在 ``level`` 字段上。
    """
    return KgAnalysisResponse(
        **asdict(
            kg_analysis_service().overview(
                notebook_id,
                board_limit=boards,
                top_members=top_members,
                edge_limit=edges,
            )
        )
    )


@router.get(
    "/notebooks/{notebook_id}/kg-analysis/sources",
    response_model=SourceProfilePageResponse,
    dependencies=[Depends(require_notebook_read)],
)
def kg_analysis_sources(
    notebook_id: str,
    limit: int = Query(50, ge=1, le=KG_SOURCE_PAGE_MAX),
    offset: int = Query(0, ge=0),
    order: Literal["sparse", "connected"] = Query("sparse"),
) -> SourceProfilePageResponse:
    """来源画像的一页(默认「与主体板块最不连通」在前)。

    **必须分页**:生产 base 库有 48 836 个来源,一次全返回既是几 MB payload 也没人
    读得完。排序在库内走 ``idx_kg_source_profiles_nb_mainstream``,并列按 source_id
    消歧(没有它,两页之间会重复/漏行,而且两个后端各给一种顺序)。
    """
    return SourceProfilePageResponse(
        **asdict(
            kg_analysis_service().source_profiles(
                notebook_id, limit=limit, offset=offset, order=order
            )
        )
    )
