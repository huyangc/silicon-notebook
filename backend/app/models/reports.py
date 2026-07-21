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


class ReportSummary(BaseModel):
    id: str
    question: str
    status: str
    progress: str = ""
    section_count: int = 0
    created_at: str = ""
    created_by: str = ""


class ReportExportRequest(BaseModel):
    report_ids: List[str] = Field(default_factory=list)


class ReportDetail(ReportSummary):
    outline: List[dict] = Field(default_factory=list)
    sections: List[dict] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    references: List[dict] = Field(default_factory=list)
    depth: int = 2
    section_status: List[dict] = Field(default_factory=list)
    content_md: str = ""
    error: str = ""
