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
    # Auto user-memory suggestions: the signal class of a system suggestion
    # (``memory_suggest_contracts.SuggestedSignal``), None for every other
    # memory.
    suggested_signal: str | None = None


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
#: The list filter's origin domain: the two stored origins plus the virtual
#: ``suggested`` (rows with a ``suggested_signal``; their stored origin stays
#: ``ask_answer``, design D5(b)).
MemoryOriginFilter = Literal["ask_answer", "external_agent", "suggested"]
MemoryStatus = Literal["candidate", "confirmed", "rejected", "deprecated"]
MemoryPromotionState = Literal["none", "proposed", "approved", "rejected"]


@dataclass(frozen=True)
class MemberExitCompletion:
    """Atomic completion: remaining personal/draft counts mean no writes;
    an ended edge reports exact draft deletions and surviving read authority.
    A replaced or absent claim ends nothing and leaves its new edge untouched.
    """

    memory_count: int
    ended: bool
    keeps_access: bool = False
    deleted_suggestion_count: int = 0
    remaining_suggestion_count: int = 0


@dataclass(frozen=True)
class MemberExitSnapshot:
    """What leaving a notebook would do to the leaver's own Memory.

    ``memory_count`` is how many of "your memories" (every status except the
    unadopted system suggestions, design §6.3) the exit would delete, and
    ``suggestion_count`` how many unadopted system suggestions it would delete
    besides (G10): both zero when the user is not a member, or would keep
    reading the notebook through its ownership or a grant (``keeps_access``).
    The ids (the rows of "your memories" the exit purges -- so
    ``len(memory_ids) == memory_count``; the unadopted suggestions are NOT
    claimed by id: ``finish_member_exit`` deletes whatever is still unadopted
    under the membership lock, so one adopted after the claim is kept) and
    the membership token are filled only for a claiming read taken under the
    membership lock.
    """

    is_member: bool
    keeps_access: bool
    memory_count: int
    memory_ids: tuple[str, ...] = ()
    # What identifies the claimed membership row (its ``added_at``): the
    # finish ends only THAT membership, never one re-created meanwhile.
    membership_token: Any = None
    suggestion_count: int = 0


@dataclass(frozen=True)
class MemberExitCounts:
    """``MemoryService.exit_disclosure``: what leaving would delete now --
    ``memory_count`` of "your memories" (the number the leaver acknowledges)
    and ``suggestion_count`` unadopted system suggestions besides (G10)."""

    memory_count: int
    suggestion_count: int = 0


@dataclass(frozen=True)
class MemberExitOutcome:
    """``MemoryService.leave_notebook``: what THIS exit deleted, counted by
    the server -- ``deleted_memory_count`` of "your memories" and
    ``deleted_suggestion_count`` unadopted system suggestions (G10). Neither
    is ever negative."""

    deleted_memory_count: int
    deleted_suggestion_count: int = 0
    # Internal commit receipt, never part of the HTTP deletion-count shape.
    # A missing or replaced claimed edge can also return a successful no-op.
    ended: bool = False


class MemoryExitDisclosure(BaseModel):
    """``GET /notebooks/{id}/membership/exit-disclosure``: ``memory_count`` of
    "your memories" the exit deletes (the number to acknowledge) and
    ``suggestion_count`` unadopted system suggestions it deletes besides
    (G10; 0 for anyone who never turned suggestions on)."""

    memory_count: int
    suggestion_count: int = 0


class MemberExitResult(BaseModel):
    """``DELETE /notebooks/{id}/membership`` 200: the exit finished; this
    request deleted ``deleted_memory_count`` of "your memories" and
    ``deleted_suggestion_count`` unadopted system suggestions (both counted by
    the server)."""

    deleted_memory_count: int
    deleted_suggestion_count: int = 0


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
    # Set on a system suggestion (adopted or not): the signal class that
    # produced it. None for every memory a person or an agent created.
    suggested_signal: Optional[str] = None


@dataclass(frozen=True)
class SuggestionWriteResult:
    """``create_suggestions_with_initial_revision``: the suggestions this
    transaction inserted, in write order, and how many writes it skipped
    because the owner already holds a memory of that answer (the
    ``idx_memory_answer_once`` conflict of a repeated-class suggestion)."""

    created: tuple[MemoryRecord, ...]
    deduped: int


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


class AnswerMemoryState(BaseModel):
    """One answer's memory as ``answer-memory-links`` reports it in
    ``states``: drafts included (``suggested`` and ``status`` say which)."""

    memory_id: str
    status: MemoryStatus
    suggested: bool = False


class AnswerMemoryLinksResponse(BaseModel):
    """``links``: answer -> the caller's memory saved from it, unadopted
    system suggestions left out (the shape older clients read, unchanged).
    ``states``: answer -> every such memory, drafts included (design §5.3,
    G15) -- "has a suggestion" vs "saved"."""

    links: Dict[str, str] = Field(default_factory=dict)
    states: Dict[str, AnswerMemoryState] = Field(default_factory=dict)


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


class DeleteWindowTransferRequest(BaseModel):
    """``POST /notebooks/{id}/memories/transfer``: move the caller's own
    confirmed Memory out of a notebook inside its deletion window. The mode
    is always ``move`` and the server picks the items (ruling 2026-10-22);
    only the destination is the caller's choice."""

    model_config = ConfigDict(extra="forbid")

    target_notebook_id: str
    # The previous page's ``next_after``; omitted on the first page.
    after: str | None = None


@dataclass(frozen=True)
class MemoryExportSnapshot:
    """One consistent read of what the caller's export of a notebook holds.

    ``title`` is the notebook's name and ``delete_after`` the deadline when
    the notebook is inside its deletion window (``None`` while it is live) --
    both read in the same snapshot as ``items``."""

    title: str
    delete_after: Optional[str]
    items: tuple["MemoryRecord", ...]
    # Unadopted system suggestions in the same snapshot, left out of
    # ``items`` (drafts are not exported; the header says how many).
    omitted_suggestion_count: int = 0
