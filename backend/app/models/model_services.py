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


class ModelUnboundWorkloadView(BaseModel):
    id: str
    label: str
    # The workload's own feature switch is on, so a feature the operator
    # turned on is silently doing nothing (startup: 「特性已开但模型未绑定」).
    feature_enabled: bool = False


class ModelServicesStatus(BaseModel):
    services: List[ModelServiceStatusItem] = Field(default_factory=list)
    # Binding gaps of the live registry, the same computation as the startup
    # ``model-bindings:`` warnings. Admin-only; always empty for other users,
    # in the offline mode (no service at all), and under the default strict
    # binding gate.
    unbound_workloads: List[ModelUnboundWorkloadView] = Field(default_factory=list)
    # Unknown/retired ids the registry ignored, sanitised exactly like the log
    # line: ids not shaped like a workload id only add to the hidden count.
    stale_binding_ids: List[str] = Field(default_factory=list)
    stale_binding_hidden_count: int = 0
