import io
import zipfile
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import (
    repository,
    require_notebook_read,
    require_notebook_write,
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
# 公开分享面。**刻意与上面的 router 分开**：main.py 给 `router` 挂了 router 级
# `Depends(get_current_user)`（零逐路由遗漏），公开报告是全站唯一不需要 session
# 的读取，挂在那上面会被 401 拦掉。与 `auth_router` / `knowhow_agent_router`
# 同一模式：需要独立认证语义的面各用各的 router。
public_router = APIRouter()


# --- 深度报告(异步后台 job,轮询取状态) -------------------------------


def _report_llm_ready(repo) -> bool:
    return repo._runtime.models.configured("report_outline")


def _report_scope_recheck(repo, notebook_id: str, understanding: dict) -> bool:
    """Mirror ``confirm_report_intent``'s scope revalidation as a boolean gate.

    The automatic direct-run confirmation (engine ``_auto_confirm_intent``)
    cannot import these api-layer validators without inverting the layering,
    so the create endpoint hands it this check as a callable (codex R2 P2).
    Same three steps as the manual endpoint — re-freeze the persisted local
    scope, re-freeze the base-library scope, re-run the two-dimension
    emptiness gate — but the rejection transport is a plain ``False``: the
    caller leaves the report at the manual confirmation gate, where the owner
    then sees the endpoint's own 422/409 wording on their next attempt."""
    persisted_scope = understanding.get("source_scope")
    persisted_base_scope = understanding.get("base_scope")
    if persisted_scope is None and persisted_base_scope is None:
        return True
    try:
        notebook = repo.get_notebook(notebook_id)
        resolved_scope = None
        resolved_base_scope = None
        if persisted_scope is not None:
            resolved_scope = _validate_source_scope(
                repo, notebook,
                ResolvedSourceScope.model_validate(persisted_scope),
            )
        if persisted_base_scope is not None:
            resolved_base_scope = _validate_base_scope(
                notebook,
                BaseNotebookScope.model_validate(persisted_base_scope),
            )
        _require_non_empty_scope(notebook, resolved_scope, resolved_base_scope)
    except Exception:  # noqa: BLE001 — any rejection means "stay at the gate"
        return False
    return True


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
             dependencies=[Depends(require_notebook_write)])
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
             dependencies=[Depends(require_notebook_write)])
def confirm_report_intent(notebook_id: str, report_id: str,
                          payload: ReportIntentConfirm) -> dict:
    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
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
    from app.services.report_synthesis import normalize_report_frame

    repo = repository()
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
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
             dependencies=[Depends(require_notebook_write)])
def generate_report(notebook_id: str, report_id: str, payload: ReportGenerateRequest) -> dict:
    repo = repository()
    if not repo._runtime.models.configured("report_section"):
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        cur = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
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
        if understanding_changed:
            repo.update_report(
                notebook_id, report_id, understanding=understanding
            )
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    if not repo.claim_report_generation(notebook_id, report_id):
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
             dependencies=[Depends(require_notebook_write)])
def cancel_report_endpoint(notebook_id: str, report_id: str) -> dict:
    from app.services.report_engine import cancel_report as _cancel
    repo = repository()
    try:
        repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Report not found")
    repo.update_report(
        notebook_id, report_id, status="cancelled", progress="已取消"
    )
    # Durable state wins first; the event only stops an active worker promptly.
    _cancel(report_id)
    return {"status": "cancelled"}


@router.delete("/notebooks/{notebook_id}/reports/{report_id}",
               dependencies=[Depends(require_notebook_write)])
def delete_report(notebook_id: str, report_id: str) -> dict:
    repository().delete_report(notebook_id, report_id)
    return {"status": "deleted"}


@router.post("/notebooks/{notebook_id}/reports/{report_id}/share",
             response_model=ReportShareResponse,
             dependencies=[Depends(require_notebook_write)])
def share_report_route(notebook_id: str, report_id: str) -> ReportShareResponse:
    """Publish one finished report behind an unguessable link.

    Only `done` reports can be shared: a link to a running or failed report
    would show an empty or half-written body to whoever it was sent to.
    """
    repo = repository()
    try:
        report = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="report not found")
    if str(report.get("status") or "") != "done":
        raise user_error(409, "只能分享已完成的报告。")
    return ReportShareResponse(share_token=repo.share_report(notebook_id, report_id))


@router.get("/notebooks/{notebook_id}/reports/{report_id}/share",
            response_model=ReportShareResponse,
            dependencies=[Depends(require_notebook_write)])
def get_report_share_route(notebook_id: str, report_id: str) -> ReportShareResponse:
    """Read back the existing link. Write-guarded: the token *is* the grant.

    The report detail endpoint only reports whether a report is shared, because
    it is reachable with read permission — handing readers the bearer credential
    would let them grant anonymous access without write access.
    """
    repo = repository()
    try:
        report = repo.get_report(notebook_id, report_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="report not found")
    if not report.get("shared"):
        raise HTTPException(status_code=404, detail="report is not shared")
    return ReportShareResponse(
        share_token=repo.report_share_token(notebook_id, report_id)
    )


@router.delete("/notebooks/{notebook_id}/reports/{report_id}/share",
               status_code=204,
               dependencies=[Depends(require_notebook_write)])
def unshare_report_route(notebook_id: str, report_id: str) -> None:
    """Revoke the link. The next public request 404s like any unknown token."""
    repository().unshare_report(notebook_id, report_id)


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
    row = repository().public_report_by_token(token)
    if row is None:
        raise HTTPException(status_code=404, detail="shared report not found")
    return PublicReport(**public_report_payload(row, row.get("references") or []))
