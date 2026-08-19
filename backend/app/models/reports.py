from typing import List

from pydantic import BaseModel, Field, field_validator

from app.core.internal_observability import public_report_sections
from app.models.source_scope import BaseNotebookScope, SourceScope

# The one rail on a research question's length, shared by the create request and
# the confirmed question the intent gate hands back.  It is a protocol boundary,
# not a tunable, so it lives as a named constant rather than a literal repeated
# per field; `frontend/app/report-api.ts::REPORT_INPUT_LIMITS` mirrors it.
#
# It is load-bearing for the *public* share page: `report_public_view` serves
# `reports.question` WHOLE (truncating a user's own artifact with no disclosure
# violates AGENTS.md 用户编辑的数据不得静默截断), and `reports.question` is the
# create-time value -- confirmation writes `resolved_question` into
# `understanding` and never rewrites the column.  So "serve it whole" is only
# safe while creation refuses an over-length question in the first place: that
# is the other half of the same red line (前端显示同一护栏, API 超限明确拒绝),
# and without it an anonymous response would be unbounded by client input
# (codex #525 R1 P2).
REPORT_QUESTION_MAX_CHARS = 4000


class ReportCreate(BaseModel):
    # No `min_length`: the create route already refuses an empty/whitespace
    # question with its own 422, and adding one here would only change which
    # layer produces the same status for the same input.
    question: str = Field(max_length=REPORT_QUESTION_MAX_CHARS)
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
    resolved_question: str = Field(min_length=1, max_length=REPORT_QUESTION_MAX_CHARS)
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

    `title_truncated`/`snippet_truncated`/`file_name_truncated` disclose that an
    over-length value was clipped to its bounded prefix, so the page can mark it
    rather than drop the tail silently (AGENTS.md 用户编辑的数据不得静默截断;
    same three flags `PublicReference` carries for a shared conversation).
    """

    key: str = ""
    title: str = ""
    file_name: str = ""
    location: str = ""
    snippet: str = ""
    title_truncated: bool = False
    snippet_truncated: bool = False
    file_name_truncated: bool = False


class PublicReport(BaseModel):
    """A shared report, readable without a session.

    `content_md` is served whole. `question` is served whole up to
    `REPORT_QUESTION_MAX_CHARS` — which creation now refuses to exceed, so for
    any report created since that rail it *is* the whole question — and past it
    (only reachable for a pre-rail row) it is bounded with `question_truncated`
    set rather than clipped silently or returned unbounded.
    """

    question: str = ""
    # Only ever True for a report created before `REPORT_QUESTION_MAX_CHARS`
    # existed: creation now refuses a longer question, so for every new report
    # the question is served whole and this stays False.
    question_truncated: bool = False
    content_md: str = ""
    created_at: str = ""
    updated_at: str = ""
    references: List[PublicReportReference] = Field(default_factory=list)
    reference_count: int = 0
    truncated_references: bool = False


class ReportShareResponse(BaseModel):
    share_token: str
