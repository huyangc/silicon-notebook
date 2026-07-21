from typing import List

from pydantic import BaseModel, Field

from app.models.memory import MemoryStatus


class MemoryOverviewItem(BaseModel):
    id: str
    title: str
    status: MemoryStatus
    updated_at: str


class MemoryOverviewSummary(BaseModel):
    total: int = 0
    confirmed: int = 0
    candidate: int = 0
    recent: List[MemoryOverviewItem] = Field(default_factory=list)


class KnowhowOverviewTable(BaseModel):
    id: str
    title: str
    row_count: int = 0
    last_activity_at: str = ""


class KnowhowOverviewSummary(BaseModel):
    table_count: int = 0
    row_count: int = 0
    projection_pending: int = 0
    projection_failed: int = 0
    stale_code_count: int = 0
    recent_tables: List[KnowhowOverviewTable] = Field(default_factory=list)


class NotebookContentOverview(BaseModel):
    memory: MemoryOverviewSummary = Field(default_factory=MemoryOverviewSummary)
    knowhow: KnowhowOverviewSummary = Field(default_factory=KnowhowOverviewSummary)
