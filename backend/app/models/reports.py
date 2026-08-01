from typing import List

from pydantic import BaseModel, Field


class ReportCreate(BaseModel):
    question: str
    history: str = ""
    depth: int = 2
    auto_generate: bool = False


class ReportOutlineUpdate(BaseModel):
    sections: List[dict] = Field(default_factory=list)


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
