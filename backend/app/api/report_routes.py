import io
import zipfile
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import (
    repository,
    require_notebook_read,
    user_error,
)
from app.models.reports import (
    PublicReport,
    ReportCreate,
    ReportIntentConfirm,
    ReportDetail,
    ReportExportRequest,
    ReportGenerateRequest,
    ReportOutlineUpdate,
    ReportShareResponse,
    ReportSummary,
)
from app.services.report_public_view import public_report_payload
from app.services.reports.intent_confirmation import (
    ReportIntentConfirmationError,
    confirmed_understanding,
)
from app.models.source_scope import (
    BaseNotebookScope,
    ResolvedSourceScope,
)
from app.api.ask_routes import (
    _require_non_empty_scope,
    _validate_base_scope,
    _validate_source_scope,
)


router = APIRouter()
# 群组共享 P1-T3b:深度报告是 P1 **唯一**放宽写面的地方。整个报告面的授权因此
# 是两层,缺一层就是越权或泄露:
#
#   ① notebook 层 —— 每个端点都挂 `Depends(require_notebook_read)`(owner ∪
#      只读成员 ∪ 有效授权边)。写端点此前挂的是
#      `require_notebook_capability("reports:write")`(owner-only);那张能力表
#      里 "reports:write" 保留给 P2 的组管理员管理动作,当前无端点消费它。
#   ② 行级 —— 已存在报告的每个操作都必须再过 `_own_report_or_404`
#      (`reports.created_by == 当前用户`),列表/导出按同一判据在 **SQL 里**
#      收窄。报告按创建者隔离:notebook owner 也只看得见自己建的,刻意不引入
#      「owner 看全部」这条新披露(设计决策 1 / 5:他人不可见,要给别人看就发
#      公开分享链接)。
#
# 唯一没有第二层的写端点是 `create_report`——它还没有行可归属,创建者身份由
# `ReportStore.create_report` 写进 `created_by`(取请求用户,不是 notebook
# owner),于是第二层从下一次请求起就成立。
#
# 公开分享面。**刻意与上面的 router 分开**：main.py 给 `router` 挂了 router 级
# `Depends(get_current_user)`（零逐路由遗漏），公开报告是全站唯一不需要 session
# 的读取，挂在那上面会被 401 拦掉。与 `auth_router` / `knowhow_agent_router`
# 同一模式：需要独立认证语义的面各用各的 router。
public_router = APIRouter()


# --- 深度报告(异步后台 job,轮询取状态) -------------------------------


def _report_llm_ready(repo) -> bool:
    return repo._runtime.models.configured("report_outline")


def _own_report_or_404(repo, notebook_id: str, report_id: str) -> dict:
    """Row-level gate for every operation on an EXISTING report (P1-T3b).

    One read answers both halves at once — the row proves notebook membership
    (``get_report`` already filters on ``notebook_id``) and carries
    ``created_by`` for the creator check — so no endpoint has to hand-assemble
    a second copy of the predicate.

    The returned row is the full report projection.  Six callers go on to use
    it (status checks, outline/understanding, question); ``cancel``, ``delete``
    and ``unshare`` discard it and pay for a full-row read they do not need.
    That is accepted rather than optimized with a second narrow read: all three
    are cold, user-clicked, one-per-report paths, and a separate
    "just the owner column" query would be a second place where the gate's
    predicate lives — exactly what this helper exists to prevent.

    "Exists but belongs to someone else" and "does not exist" must be the same
    404: a distinguishable 403 would turn this endpoint into an oracle for
    "how many reports has the owner got, and what are their ids" inside a
    notebook the caller can legitimately read.
    """
    try:
        row = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    if str(row.get("created_by") or "") != repo.current_user().id:
        raise HTTPException(status_code=404, detail="Report not found")
    return row


def _report_scope_recheck(repo, notebook_id: str, understanding: dict):
    """Mirror ``confirm_report_intent``'s scope revalidation for the auto path.

    The automatic direct-run confirmation (engine ``_auto_confirm_intent``)
    cannot import these api-layer validators without inverting the layering,
    so the create endpoint hands it this check as a callable (codex R2 P2).
    Same three steps as the manual endpoint — re-freeze the persisted local
    scope, re-freeze the base-library scope, re-run the two-dimension
    emptiness gate.  The refreshed freeze must be ADOPTED, not just judged
    (codex R4 P2): sources/libraries added while intent understanding ran can
    flip ``narrowed`` on the re-freeze, and downstream whole-scope channel
    admission reads exactly that flag, so returning a bare "still fine" while
    claiming the stale contract would diverge from what manual confirmation
    persists.  Returns ``None`` to reject (caller stays at the manual gate,
    where the owner sees the endpoint's own 422/409 wording next attempt), or
    a dict with the updated ``understanding`` plus the resolved scope OBJECTS
    — the objects carry the hidden-participant snapshot/private attrs that a
    ``model_dump`` round-trip would lose, and the engine re-enters the scope
    context with them exactly like the manual endpoint's relaunch does."""
    persisted_scope = understanding.get("source_scope")
    persisted_base_scope = understanding.get("base_scope")
    if persisted_scope is None and persisted_base_scope is None:
        return {
            "understanding": understanding,
            "source_scope": None,
            "base_scope": None,
        }
    try:
        notebook = repo.get_notebook(notebook_id)
        updated = dict(understanding)
        resolved_scope = None
        resolved_base_scope = None
        if persisted_scope is not None:
            resolved_scope = _validate_source_scope(
                repo, notebook,
                ResolvedSourceScope.model_validate(persisted_scope),
            )
            updated["source_scope"] = resolved_scope.model_dump()
        if persisted_base_scope is not None:
            resolved_base_scope = _validate_base_scope(
                notebook,
                BaseNotebookScope.model_validate(persisted_base_scope),
            )
            updated["base_scope"] = resolved_base_scope.model_dump()
        _require_non_empty_scope(notebook, resolved_scope, resolved_base_scope)
    except Exception:  # noqa: BLE001 — any rejection means "stay at the gate"
        return None
    return {
        "understanding": updated,
        "source_scope": resolved_scope,
        "base_scope": resolved_base_scope,
    }


def _launch_plan_job(repo, notebook_id: str, rid: str, question: str, history: str,
                     auto_generate: bool = False, intent_contract=None,
                     source_scope=None, base_scope=None) -> None:
    """Run corpus-blind understanding, or resume planning after confirmation.

    The first call has no ``intent_contract`` and stops at ``intent_ready``.
    The confirmation endpoint supplies the reviewed contract; only that call may
    proceed into retrieval and outline planning.

    Task 25:helper 名保留(测试 monkeypatch 位),编排移交运行时协调器——
    注册取消(submit 前)→ background_jobs.submit(copy_context)→ 收尾注销。"""
    repo.report_execution.start_plan(
        notebook_id, rid, question, history, auto_generate,
        user_id=repo.current_user().id,
        intent_contract=intent_contract,
        source_scope=source_scope,
        base_scope=base_scope,
        scope_reconfirm=(
            lambda understanding: _report_scope_recheck(
                repo, notebook_id, understanding
            )
        ))


def _launch_generate_job(repo, notebook_id: str, rid: str, question: str,
                         depth: int = 2, *, source_scope=None,
                         base_scope=None) -> None:
    """阶段2(生成)后台 job:用已确认的 outline 跑 generate → done。"""
    repo.report_execution.start_generate(
        notebook_id, rid, question, depth,
        user_id=repo.current_user().id,
        source_scope=source_scope,
        base_scope=base_scope,
    )


@router.post("/notebooks/{notebook_id}/reports",
             dependencies=[Depends(require_notebook_read)])
def create_report(notebook_id: str, payload: ReportCreate) -> dict:
    repo = repository()
    if not payload.question.strip():
        raise HTTPException(status_code=422, detail="question required")
    if not _report_llm_ready(repo):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        notebook = repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    resolved_source_scope = _validate_source_scope(
        repo, notebook, payload.source_scope
    )
    resolved_base_scope = _validate_base_scope(notebook, payload.base_scope)
    # Before create_report(): an empty scope must not leave a report row behind,
    # and (the reason this check exists at all) must not let the plan job burn
    # model calls producing a zero-evidence report. Deliberately NOT the
    # ask_available gate -- only the scope-emptiness half.
    _require_non_empty_scope(notebook, resolved_source_scope, resolved_base_scope)
    depth = max(1, min(16, int(payload.depth)))
    try:
        rid = repo.create_report(notebook_id, payload.question.strip(), depth=depth)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    scope_payload = (
        resolved_source_scope.model_dump()
        if resolved_source_scope is not None else None
    )
    base_scope_payload = (
        resolved_base_scope.model_dump()
        if resolved_base_scope is not None else None
    )
    understanding_update: dict = {}
    if scope_payload is not None:
        understanding_update["source_scope"] = scope_payload
    if base_scope_payload is not None:
        understanding_update["base_scope"] = base_scope_payload
    if understanding_update:
        repo.update_report(
            notebook_id, rid, understanding=understanding_update
        )
    # Only pass the kwargs that are actually set (rather than always passing
    # both, even as None): a plain unscoped report must keep invoking
    # _launch_plan_job with the same bare positional signature as before either
    # scope dimension existed.
    launch_kwargs: dict = {}
    if resolved_source_scope is not None:
        launch_kwargs["source_scope"] = resolved_source_scope
    if resolved_base_scope is not None:
        launch_kwargs["base_scope"] = resolved_base_scope
    _launch_plan_job(
        repo, notebook_id, rid, payload.question.strip(), payload.history,
        payload.auto_generate, **launch_kwargs,
    )
    return {"report_id": rid, "status": "pending"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/intent",
             dependencies=[Depends(require_notebook_read)])
def confirm_report_intent(notebook_id: str, report_id: str,
                          payload: ReportIntentConfirm) -> dict:
    repo = repository()
    cur = _own_report_or_404(repo, notebook_id, report_id)
    if cur.get("status") != "intent_ready":
        raise user_error(409, "当前报告不在问题确认阶段")

    understanding = dict(cur.get("understanding") or {})
    persisted_scope = understanding.get("source_scope")
    persisted_base_scope = understanding.get("base_scope")
    resolved_scope = None
    resolved_base_scope = None
    if persisted_scope is not None or persisted_base_scope is not None:
        try:
            notebook = repo.get_notebook(notebook_id)
            if persisted_scope is not None:
                resolved_scope = _validate_source_scope(
                    repo, notebook,
                    ResolvedSourceScope.model_validate(persisted_scope),
                )
                understanding["source_scope"] = resolved_scope.model_dump()
            if persisted_base_scope is not None:
                resolved_base_scope = _validate_base_scope(
                    notebook,
                    BaseNotebookScope.model_validate(persisted_base_scope),
                )
                understanding["base_scope"] = resolved_base_scope.model_dump()
        except (TypeError, ValueError):
            raise user_error(409, "报告保存的来源范围无效，请重新创建报告")
        # Re-freezing against the CURRENT notebook can empty a scope that was
        # non-empty at create time (every selected source deleted, every
        # selected library unmounted), so the emptiness check must run again
        # here -- this is the last gate before planning is claimed and launched.
        _require_non_empty_scope(notebook, resolved_scope, resolved_base_scope)
    # Shared with the automatic direct-run confirmation: one implementation of
    # "freeze exactly what the owner reviewed", so both paths claim the same
    # contract.  Only the rejection transport differs (422 here, fail-open
    # there).
    try:
        understanding = confirmed_understanding(
            understanding,
            resolved_question=payload.resolved_question,
            answers=[
                {"id": row.id, "answer": row.answer} for row in payload.answers
            ],
        )
    except ReportIntentConfirmationError as exc:
        raise user_error(422, exc.message)
    if not repo.claim_report_intent(notebook_id, report_id, understanding):
        raise user_error(409, "当前报告不再处于问题确认阶段")
    launch_kwargs = {"intent_contract": understanding}
    if understanding.get("source_scope") is not None:
        launch_kwargs["source_scope"] = resolved_scope
    if understanding.get("base_scope") is not None:
        launch_kwargs["base_scope"] = resolved_base_scope
    _launch_plan_job(
        repo,
        notebook_id,
        report_id,
        cur["question"],
        "",
        bool(understanding.get("auto_generate_requested")),
        **launch_kwargs,
    )
    return {"status": "planning"}


@router.get("/notebooks/{notebook_id}/reports",
            dependencies=[Depends(require_notebook_read)])
def list_reports(notebook_id: str) -> List[ReportSummary]:
    # repo 行含 notebook_id/updated_at 等多余键 → 按模型字段过滤(仓库无 extra=ignore 风格)
    # 读权放行到列表,再按 created_by 在 SQL 里收窄(不是结果侧过滤):共享笔记本里
    # 每位成员只看得见自己建的报告,notebook owner 同样只看得见自己的。
    repo = repository()
    return [ReportSummary(**{k: v for k, v in r.items() if k in ReportSummary.model_fields})
            for r in repo.list_reports(
                notebook_id, created_by=repo.current_user().id
            )]


@router.post("/notebooks/{notebook_id}/reports/export",
             dependencies=[Depends(require_notebook_read)])
def export_reports_endpoint(notebook_id: str, payload: ReportExportRequest) -> StreamingResponse:
    # owner∪成员可下(require_notebook_read);只导出该 notebook 下 status='done' 且
    # content_md 非空**且本人创建**的报告,非 done/空/跨 notebook/别人的 id 一律
    # 静默跳过(repo 层已过滤——跳过而不是 404,免得导出成为「这个 id 存不存在」
    # 的探针)。全部被跳过时仍走下面既有的 422。
    repo = repository()
    rows = repo.export_reports(
        notebook_id, payload.report_ids, created_by=repo.current_user().id
    )
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
    repo = repository()
    r = _own_report_or_404(repo, notebook_id, report_id)
    return ReportDetail(**{k: v for k, v in r.items() if k in ReportDetail.model_fields})


@router.patch("/notebooks/{notebook_id}/reports/{report_id}/outline",
              dependencies=[Depends(require_notebook_read)])
def update_report_outline(notebook_id: str, report_id: str, payload: ReportOutlineUpdate) -> dict:
    from app.services.report_synthesis import normalize_report_frame

    repo = repository()
    cur = _own_report_or_404(repo, notebook_id, report_id)
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="outline editable only when outline_ready")
    intent_catalog = next(
        (list(section.get("intent_catalog") or []) for section in (cur.get("outline") or [])
         if isinstance(section, dict) and section.get("intent_catalog")),
        [],
    )
    intent_contract = next(
        (dict(section.get("intent_contract") or {}) for section in (cur.get("outline") or [])
         if isinstance(section, dict) and section.get("intent_contract")),
        {},
    )
    understanding = dict(cur.get("understanding") or {})
    report_frame = understanding.get("report_frame")
    if payload.frame is not None:
        try:
            report_frame = normalize_report_frame(payload.frame, strict=True)
        except (TypeError, ValueError):
            raise user_error(422, "分析框架格式无效，请检查分类维度和比较条件")
    known_intents = {
        str(item.get("id") or ""): item for item in intent_catalog
        if isinstance(item, dict) and item.get("id")
    }
    secs = []
    for raw in payload.sections:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        raw_queries = raw.get("sub_queries") or []
        if not isinstance(raw_queries, list):
            continue
        queries = [str(item).strip() for item in raw_queries if str(item).strip()]
        if not title or not queries:
            continue
        if len(queries) > repo.settings.report_max_subqueries_per_section:
            raise user_error(
                422,
                "每个章节最多可保留 "
                f"{repo.settings.report_max_subqueries_per_section} 条检索方向",
            )
        section = dict(raw)
        section.update(
            title=title,
            scope=str(raw.get("scope") or "").strip(),
            sub_queries=queries,
        )
        raw_intent_ids = raw.get("intent_ids") or []
        if not isinstance(raw_intent_ids, list):
            raw_intent_ids = []
        intent_ids = list(dict.fromkeys(
            str(item).strip() for item in raw_intent_ids
            if str(item).strip() in known_intents
        ))
        section["intent_ids"] = intent_ids
        section["intent_questions"] = [
            str(known_intents[item].get("question") or "") for item in intent_ids
        ]
        if intent_catalog:
            section["intent_catalog"] = intent_catalog
        if intent_contract:
            # The outline PATCH is the user's frame-confirmation boundary.
            # Keep the compatibility copy synchronized so an older prompt path
            # cannot resurrect the planner's pre-confirmation frame.
            section_intent_contract = dict(intent_contract)
            if report_frame:
                section_intent_contract["report_frame"] = report_frame
            else:
                section_intent_contract.pop("report_frame", None)
            section["intent_contract"] = section_intent_contract
        if report_frame:
            section["report_frame"] = report_frame
        else:
            # ``section`` started as a copy of the submitted row, which may
            # still carry the previously confirmed frame.  Clearing the frame
            # is authoritative too: do not let that stale section-level copy
            # outrank the synchronized compatibility contract during assembly.
            section.pop("report_frame", None)
        secs.append(section)
    if len(secs) > repo.settings.report_max_sections:
        raise user_error(
            422,
            f"报告大纲最多可保留 {repo.settings.report_max_sections} 个章节",
        )
    if not secs:
        raise HTTPException(status_code=422, detail="at least one valid section required")
    covered_intents = {
        intent_id for section in secs for intent_id in section["intent_ids"]
    }
    missing_intents = [
        str(item.get("title") or item.get("question") or intent_id)
        for intent_id, item in known_intents.items()
        if intent_id not in covered_intents
    ]
    if missing_intents:
        raise user_error(
            422,
            "大纲必须保留每个必答主题，请恢复被删除的主题后再试",
        )
    if report_frame:
        understanding["report_frame"] = report_frame
    else:
        understanding.pop("report_frame", None)
    repo.update_report(
        notebook_id, report_id, outline=secs, understanding=understanding
    )
    return {"status": "ok", "sections": len(secs)}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/generate",
             dependencies=[Depends(require_notebook_read)])
def generate_report(notebook_id: str, report_id: str, payload: ReportGenerateRequest) -> dict:
    repo = repository()
    if not repo._runtime.models.configured("report_section"):
        raise HTTPException(status_code=409, detail="LLM not configured")
    cur = _own_report_or_404(repo, notebook_id, report_id)
    retrying = cur.get("status") == "failed"
    if cur.get("status") not in {"outline_ready", "failed"}:
        raise HTTPException(
            status_code=409,
            detail="generate only from outline_ready or failed",
        )
    if retrying and not (cur.get("outline") or []):
        raise user_error(409, "该失败报告没有可复用大纲，请重新创建报告")
    understanding = dict(cur.get("understanding") or {})
    persisted_scope = understanding.get("source_scope")
    persisted_base_scope = understanding.get("base_scope")
    resolved_scope = None
    resolved_base_scope = None
    understanding_changed = False
    if persisted_scope is not None or persisted_base_scope is not None:
        try:
            notebook = repo.get_notebook(notebook_id)
            if persisted_scope is not None:
                resolved_scope = _validate_source_scope(
                    repo, notebook,
                    ResolvedSourceScope.model_validate(persisted_scope),
                )
                if resolved_scope.model_dump() != persisted_scope:
                    understanding["source_scope"] = resolved_scope.model_dump()
                    understanding_changed = True
            if persisted_base_scope is not None:
                resolved_base_scope = _validate_base_scope(
                    notebook,
                    BaseNotebookScope.model_validate(persisted_base_scope),
                )
                if resolved_base_scope.model_dump() != persisted_base_scope:
                    understanding["base_scope"] = resolved_base_scope.model_dump()
                    understanding_changed = True
        except (TypeError, ValueError):
            raise user_error(409, "报告保存的来源范围无效，请重新创建报告")
        # Same emptiness gate as create/confirm: sources or libraries may have
        # gone away since the outline was approved, and generation is by far
        # the most expensive phase to run against an empty universe.
        _require_non_empty_scope(notebook, resolved_scope, resolved_base_scope)
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    if not repo.claim_report_generation(
        notebook_id,
        report_id,
        understanding if understanding_changed else None,
    ):
        raise user_error(409, "当前报告已进入其他处理阶段")
    launch_kwargs: dict = {}
    if resolved_scope is not None:
        launch_kwargs["source_scope"] = resolved_scope
    if resolved_base_scope is not None:
        launch_kwargs["base_scope"] = resolved_base_scope
    _launch_generate_job(
        repo, notebook_id, report_id, cur["question"], depth, **launch_kwargs
    )
    return {"status": "generating"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/cancel",
             dependencies=[Depends(require_notebook_read)])
def cancel_report_endpoint(notebook_id: str, report_id: str) -> dict:
    from app.services.report_engine import cancel_report as _cancel
    repo = repository()
    _own_report_or_404(repo, notebook_id, report_id)
    repo.update_report(
        notebook_id, report_id, status="cancelled", progress="已取消"
    )
    terminal = repo.get_report(notebook_id, report_id)
    if terminal.get("status") != "cancelled":
        # Preserve the endpoint's historical idempotent 200 while reporting
        # the actual sticky terminal state. A completed report is not relabelled
        # or signalled as cancelled merely because this request arrived late.
        return {"status": terminal.get("status")}
    # Durable state wins first; the event only stops an active worker promptly.
    _cancel(report_id)
    return {"status": "cancelled"}


@router.delete("/notebooks/{notebook_id}/reports/{report_id}",
               dependencies=[Depends(require_notebook_read)])
def delete_report(notebook_id: str, report_id: str) -> dict:
    # 这个端点此前不读行(仓库层的 DELETE 自带 notebook 谓词,零行删除是静默
    # no-op)。行级判定要求先读一次:少了它,共享笔记本里任何成员都能删掉别人的
    # 报告,而且因为 no-op 静默,越权和「本来就不存在」在响应上无法区分。
    repo = repository()
    _own_report_or_404(repo, notebook_id, report_id)
    repo.delete_report(notebook_id, report_id)
    return {"status": "deleted"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/share",
             response_model=ReportShareResponse,
             dependencies=[Depends(require_notebook_read)])
def share_report_route(notebook_id: str, report_id: str) -> ReportShareResponse:
    """Publish one finished report behind an unguessable link.

    Only `done` reports can be shared: a link to a running or failed report
    would show an empty or half-written body to whoever it was sent to.
    """
    repo = repository()
    report = _own_report_or_404(repo, notebook_id, report_id)
    if str(report.get("status") or "") != "done":
        raise user_error(409, "只能分享已完成的报告。")
    return ReportShareResponse(share_token=repo.share_report(notebook_id, report_id))


@router.get("/notebooks/{notebook_id}/reports/{report_id}/share",
            response_model=ReportShareResponse,
            dependencies=[Depends(require_notebook_read)])
def get_report_share_route(notebook_id: str, report_id: str) -> ReportShareResponse:
    """Read back the existing link. Write-guarded: the token *is* the grant.

    The report detail endpoint only reports whether a report is shared, because
    it is reachable with read permission — handing readers the bearer credential
    would let them grant anonymous access without write access.
    """
    repo = repository()
    report = _own_report_or_404(repo, notebook_id, report_id)
    if not report.get("shared"):
        raise HTTPException(status_code=404, detail="report is not shared")
    return ReportShareResponse(
        share_token=repo.report_share_token(notebook_id, report_id)
    )


@router.delete("/notebooks/{notebook_id}/reports/{report_id}/share",
               status_code=204,
               dependencies=[Depends(require_notebook_read)])
def unshare_report_route(notebook_id: str, report_id: str) -> None:
    """Revoke the link. The next public request 404s like any unknown token.

    Row-level gated like every other operation on an existing report: revoking
    someone else's link is a destructive act on their report, and the silent
    zero-row UPDATE underneath would report success either way.
    """
    repo = repository()
    _own_report_or_404(repo, notebook_id, report_id)
    repo.unshare_report(notebook_id, report_id)


@public_router.get("/public/reports/{token}", response_model=PublicReport)
def public_report_route(token: str) -> PublicReport:
    """The one report read that needs no session — the token is the whole grant.

    Deliberately has NO `Depends(get_current_user)`: this is the anonymous
    surface. That also means no request user is bound, so nothing here may call
    an owner-scoped repository method — `current_user` falls back to the seeded
    admin when the ContextVar is unset, which would silently run as an
    administrator. `public_report_by_token` takes the token alone for exactly
    that reason, and the payload is an explicit allowlist rather than the stored
    row.
    """
    repo = repository()
    row = repo.public_report_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="shared report not found")
    # Live re-authorization of the CREATOR (P1-T3b ruling).  A member may
    # publish a report built from a shared notebook's corpus, but that is a
    # grant that lasts exactly as long as their read access does: revoke the
    # group grant / remove them from the group / delete the group, and every
    # link they issued in that notebook must die immediately.
    #
    # It has to be here rather than a cascade at each revocation point: there
    # are several ways to lose read access (grant deleted, group membership
    # removed, group deleted, member row dropped, notebook re-tiered), and a
    # cascade would have to be re-derived at every one of them — the same
    # argument that made mount validity a live predicate instead of stored
    # state.  Once neither the member (blocked by the read guard) nor the owner
    # (blocked by the row-level creator check) can reach `unshare`, a stale
    # token would otherwise serve the owner's corpus forever.
    #
    # `user_can_read_notebook` takes both ids EXPLICITLY, so this stays legal on
    # the anonymous router: it binds no request user and never consults the
    # ContextVar (which falls back to the seeded admin when unset).
    # Same 404 as an unknown token — a distinguishable response would report on
    # somebody's group membership to an anonymous caller.  Restoring access
    # revives the link, which is the same idempotent-token semantics `share`
    # already promises.
    if not repo.user_can_read_notebook(
        str(row.get("notebook_id") or ""), str(row.get("created_by") or "")
    ):
        raise HTTPException(status_code=404, detail="shared report not found")
    return PublicReport(**public_report_payload(row, row.get("references") or []))
