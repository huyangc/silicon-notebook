import io
import zipfile
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.api.deps import repository, require_notebook_read, require_notebook_write
from app.models.reports import (
    ReportCreate,
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
    if not repo._runtime.models.configured("report_section"):
        raise HTTPException(status_code=409, detail="LLM not configured")
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
