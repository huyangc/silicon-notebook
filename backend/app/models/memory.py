"""Storage-neutral Memory write and revision value types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


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
