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
    ReportCreate,
    ReportIntentConfirm,
    ReportDetail,
    ReportExportRequest,
    ReportGenerateRequest,
    ReportOutlineUpdate,
    ReportSummary,
)


router = APIRouter()


# --- 深度报告(异步后台 job,轮询取状态) -------------------------------


def _report_llm_ready(repo) -> bool:
    return repo._runtime.models.configured("report_outline")


def _launch_plan_job(repo, notebook_id: str, rid: str, question: str, history: str,
                     auto_generate: bool = False, intent_contract=None) -> None:
    """Run corpus-blind understanding, or resume planning after confirmation.

    The first call has no ``intent_contract`` and stops at ``intent_ready``.
    The confirmation endpoint supplies the reviewed contract; only that call may
    proceed into retrieval and outline planning.

    Task 25:helper 名保留(测试 monkeypatch 位),编排移交运行时协调器——
    注册取消(submit 前)→ background_jobs.submit(copy_context)→ 收尾注销。"""
    repo.report_execution.start_plan(
        notebook_id, rid, question, history, auto_generate,
        user_id=repo.current_user().id,
        intent_contract=intent_contract)


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
    ambiguities = {
        str(row.get("id") or ""): row
        for row in (understanding.get("ambiguities") or [])
        if isinstance(row, dict) and row.get("id")
    }
    submitted = {
        row.id.strip(): row.answer.strip()
        for row in payload.answers
        if row.id.strip() and row.answer.strip() and row.id.strip() in ambiguities
    }
    missing = [
        row for ambiguity_id, row in ambiguities.items()
        if row.get("required") is not False and not submitted.get(ambiguity_id)
    ]
    if missing:
        raise user_error(422, "请先回答所有必填澄清问题")

    resolved_question = payload.resolved_question.strip()
    if not resolved_question:
        raise user_error(422, "确认后的研究问题不能为空")
    answer_rows = [
        {
            "id": ambiguity_id,
            "question": str(ambiguities[ambiguity_id].get("question") or ""),
            "answer": answer,
        }
        for ambiguity_id, answer in submitted.items()
    ]
    understanding["confirmed_input"] = {
        "resolved_question": resolved_question,
        "answers": answer_rows,
    }
    understanding["confirmed"] = True
    if not repo.claim_report_intent(notebook_id, report_id, understanding):
        raise user_error(409, "当前报告不再处于问题确认阶段")
    _launch_plan_job(
        repo,
        notebook_id,
        report_id,
        cur["question"],
        "",
        bool(understanding.get("auto_generate_requested")),
        intent_contract=understanding,
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
    for raw in payload.sections[: repo.settings.report_max_sections]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        raw_queries = raw.get("sub_queries") or []
        if not isinstance(raw_queries, list):
            continue
        queries = [str(item).strip() for item in raw_queries if str(item).strip()][:4]
        if not title or not queries:
            continue
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
        secs.append(section)
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
    if cur.get("status") != "outline_ready":
        raise HTTPException(status_code=409, detail="generate only from outline_ready")
    depth = max(1, min(16, int(payload.depth or cur.get("depth", 2))))
    if not repo.claim_report_generation(notebook_id, report_id):
        raise user_error(409, "当前报告不再处于大纲确认阶段")
    _launch_generate_job(repo, notebook_id, report_id, cur["question"], depth)
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
