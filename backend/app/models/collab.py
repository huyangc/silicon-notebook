from pydantic import BaseModel, ConfigDict, Field

from app.domain.collaboration import PulseKind


class PulseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    since: str = ""
    visible: bool = True


class PulseChange(BaseModel):
    kind: PulseKind
    actor_name: str
    at: str


class PulseViewer(BaseModel):
    user_id: str
    display_name: str
    role: str


class PulseResponse(BaseModel):
    cursor: str
    reset: bool
    changes: list[PulseChange] = Field(default_factory=list)
    viewers: list[PulseViewer] = Field(default_factory=list)
    other_reader_count: int
    next_interval_s: int


class ActivityItem(BaseModel):
    id: str
    notebook_id: str
    kind: PulseKind
    capability: str
    actor_id: str
    actor_name: str
    target: str
    created_at: str


class ActivityResponse(BaseModel):
    items: list[ActivityItem] = Field(default_factory=list)
