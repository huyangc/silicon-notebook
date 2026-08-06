from typing import List

from pydantic import BaseModel, Field, field_validator

from app.core.internal_observability import public_report_sections
from app.models.source_scope import BaseNotebookScope, SourceScope


class ReportCreate(BaseModel):
    question: str
    history: str = ""
    depth: int = 2
    auto_generate: bool = False
    source_scope: SourceScope | None = None
    # None preserves the historical behavior of every mounted base notebook
    # participating unconditionally. Independent dimension from source_scope
    # -- see BaseNotebookScope's docstring.
    base_scope: BaseNotebookScope | None = None


class ReportOutlineUpdate(BaseModel):
    sections: List[dict] = Field(default_factory=list)
    # Optional comparison/classification frame confirmed with the outline.  The
    # service layer applies the bounded semantic validator; keeping this field
    # additive preserves old clients and persisted reports.
    frame: dict | None = None


class ReportGenerateRequest(BaseModel):
    depth: int | None = None


class ReportClarificationAnswer(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    answer: str = Field(min_length=1, max_length=4000)


class ReportIntentConfirm(BaseModel):
    resolved_question: str = Field(min_length=1, max_length=4000)
    answers: List[ReportClarificationAnswer] = Field(default_factory=list, max_length=8)


class ReportSummary(BaseModel):
    id: str
    question: str
    status: str
    progress: str = ""
    section_count: int = 0
    created_at: str = ""
    generation_started_at: str = ""
    updated_at: str = ""
    created_by: str = ""


class ReportExportRequest(BaseModel):
    report_ids: List[str] = Field(default_factory=list)


class ReportDetail(ReportSummary):
    outline: List[dict] = Field(default_factory=list)
    sections: List[dict] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    references: List[dict] = Field(default_factory=list)
    understanding: dict = Field(default_factory=dict)
    depth: int = 2
    section_status: List[dict] = Field(default_factory=list)
    content_md: str = ""
    error: str = ""
    # 只说「有没有在分享」，**不给 token**。报告详情端点只要
    # `require_notebook_read`,而 token 本身就是匿名访问凭据——放在这里等于让只读
    # 成员绕过写权限把公开访问分发出去。凭据只从写权限的 `GET .../share` 出。
    shared: bool = False

    @field_validator("sections", mode="before")
    @classmethod
    def _hide_internal_rollout_receipts(cls, value: object) -> object:
        return public_report_sections(value)


class PublicReportReference(BaseModel):
    """One citation as an anonymous reader sees it.

    No `source_id` / `element_id` / `object_id`: a public link must not hand out
    handles into the authenticated API. See `services/report_public_view.py`.
    """

    key: str = ""
    title: str = ""
    file_name: str = ""
    location: str = ""
    snippet: str = ""


class PublicReport(BaseModel):
    """A shared report, readable without a session."""

    question: str = ""
    content_md: str = ""
    created_at: str = ""
    updated_at: str = ""
    references: List[PublicReportReference] = Field(default_factory=list)
    reference_count: int = 0
    truncated_references: bool = False


class ReportShareResponse(BaseModel):
    share_token: str
