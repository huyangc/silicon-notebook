"""Storage-neutral Memory write and revision value types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.memory_inputs import (
    normalize_content,
    normalize_reason,
    normalize_tags,
    normalize_title,
)


@dataclass(frozen=True)
class MemoryWrite:
    id: str
    notebook_id: str
    created_by: str
    origin: str
    status: str
    title: str
    content_md: str
    tags: Sequence[str]
    created_at: str
    updated_at: str
    agent_profile_id: str | None = None
    source_answer_id: str | None = None
    confirmed_by: str | None = None
    confirmed_at: str | None = None
    provenance: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class MemoryRevision:
    revision: int
    title: str
    content_md: str
    tags: list[str]
    status: str
    promotion_state: str
    changed_by: str
    change_reason: str
    created_at: str


MemoryOrigin = Literal["ask_answer", "external_agent"]
MemoryStatus = Literal["candidate", "confirmed", "rejected", "deprecated"]
MemoryPromotionState = Literal["none", "proposed", "approved", "rejected"]


@dataclass(frozen=True)
class MemberExitSnapshot:
    """What leaving a notebook would do to the leaver's own Memory.

    ``memory_count`` is the number of Memory rows (every status) the exit
    would delete: zero when the user is not a member, or would keep reading
    the notebook through its ownership or a grant (``keeps_access``). The
    ids and the membership token are filled only for a claiming read taken
    under the membership lock.
    """

    is_member: bool
    keeps_access: bool
    memory_count: int
    memory_ids: tuple[str, ...] = ()
    # What identifies the claimed membership row (its ``added_at``): the
    # finish ends only THAT membership, never one re-created meanwhile.
    membership_token: Any = None


class MemoryExitDisclosure(BaseModel):
    """``GET /notebooks/{id}/membership/exit-disclosure``."""

    memory_count: int


class MemberExitResult(BaseModel):
    """``DELETE /notebooks/{id}/membership`` 200: the exit finished; this
    request deleted ``deleted_memory_count`` Memories (counted by the
    server)."""

    deleted_memory_count: int


class MemoryRecord(BaseModel):
    id: str
    notebook_id: str
    created_by: str
    agent_profile_id: Optional[str] = None
    source_answer_id: Optional[str] = None
    origin: MemoryOrigin
    status: MemoryStatus
    promotion_state: MemoryPromotionState = "none"
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    confirmed_by: Optional[str] = None
    confirmed_at: Optional[str] = None
    embedding_status: str = "pending"
    embedding_error: str = ""
    created_at: str
    updated_at: str
    provenance: Dict[str, Any] = Field(default_factory=dict)


class MemoryHit(BaseModel):
    """A retrieval projection with relevance and authority kept separate."""

    memory_id: str
    title: str
    text: str
    status: Literal["candidate", "confirmed"]
    authority: int
    score: float
    provenance: Dict[str, Any] = Field(default_factory=dict)

    @property
    def relevance(self) -> float:
        return self.score

    @property
    def object_id(self) -> str:
        return self.memory_id


class MemoryNotebookOption(BaseModel):
    notebook_id: str
    name: str
    memory_count: int
    pending_count: int


class PaginatedMemories(BaseModel):
    items: List[MemoryRecord]
    total_count: int
    offset: int
    limit: int
    owner_total_count: int = 0
    owner_pending_count: int = 0
    notebook_options: List[MemoryNotebookOption] = Field(default_factory=list)
    # Notebook-scoped listing only (the gate needs a single notebook to
    # evaluate); the cross-notebook user-level listing leaves this None.
    kg_extract_eligible: Optional[bool] = None


class MemoryPreview(BaseModel):
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    provenance_summary: Dict[str, Any] = Field(default_factory=dict)
    kg_extract_eligible: bool = False


class MemoryCreateFromAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_id: str
    title: str
    content_md: str
    tags: List[str] = Field(default_factory=list)
    extract_kg: bool = True

    _normalize_title = field_validator("title")(normalize_title)
    _normalize_content = field_validator("content_md")(normalize_content)
    _normalize_tags = field_validator("tags")(normalize_tags)


class AnswerMemoryLinksRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_ids: List[str] = Field(default_factory=list, max_length=200)


class AnswerMemoryLinksResponse(BaseModel):
    links: Dict[str, str] = Field(default_factory=dict)


class MemoryBulkDeleteRequest(BaseModel):
    memory_ids: List[str] = Field(default_factory=list, max_length=200)


class MemoryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    content_md: Optional[str] = None
    tags: Optional[List[str]] = None

    @field_validator("title")
    @classmethod
    def _normalize_optional_title(cls, value):
        return normalize_title(value) if value is not None else None

    @field_validator("content_md")
    @classmethod
    def _normalize_optional_content(cls, value):
        return normalize_content(value) if value is not None else None

    @field_validator("tags")
    @classmethod
    def _normalize_optional_tags(cls, value):
        return normalize_tags(value) if value is not None else None


class MemoryReviewRequest(MemoryUpdate):
    reason: Optional[str] = None
    extract_kg: Optional[bool] = None

    @field_validator("reason")
    @classmethod
    def _normalize_optional_reason(cls, value):
        return normalize_reason(value) if value is not None else None


class MemoryTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_ids: List[str] = Field(..., min_length=1, max_length=200)
    target_notebook_id: str
    mode: Literal["copy", "move"]
    extract_kg: bool = True
