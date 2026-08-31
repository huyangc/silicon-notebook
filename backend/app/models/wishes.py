"""Public wish-wall API models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


WISH_TITLE_MAX_CHARS = 120
WISH_CONTENT_MAX_CHARS = 4000
WISH_PAGE_DEFAULT = 50
WISH_PAGE_MAX = 100

WishKind = Literal["bug", "feature", "plan"]


class WishCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: WishKind
    title: str
    content: str


class WishItem(BaseModel):
    id: str
    kind: WishKind
    title: str
    content: str
    author_id: str
    author_name: str
    vote_count: int = 0
    voted_by_me: bool = False
    created_at: str
    updated_at: str


class PaginatedWishes(BaseModel):
    items: list[WishItem] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = WISH_PAGE_DEFAULT


class WishVoteResult(BaseModel):
    wish_id: str
    voted: bool
    vote_count: int
