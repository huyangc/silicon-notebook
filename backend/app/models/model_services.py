from typing import List, Literal

from pydantic import BaseModel, Field


class ModelWorkloadView(BaseModel):
    id: str
    label: str


class ModelServiceStatusItem(BaseModel):
    service_id: str
    display_name: str
    kind: Literal["chat", "embedding", "rerank"]
    model: str = ""
    workloads: List[ModelWorkloadView] = Field(default_factory=list)
    status: Literal[
        "untested", "ok", "busy", "error", "circuit_open", "half_open"
    ] = "untested"
    active: int = 0
    maximum: int = 0
    queued: int = 0
    oldest_wait_ms: int = 0
    latency_ms: int = 0
    checked_at: str = ""
    trigger: str = ""
    code: str = ""
    support_id: str = ""


class ModelServicesStatus(BaseModel):
    services: List[ModelServiceStatusItem] = Field(default_factory=list)
