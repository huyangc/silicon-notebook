"""Global wish-wall HTTP surface."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user, user_error, wish_repository
from app.models.identity import UserProfile
from app.models.wishes import (
    PaginatedWishes,
    WishCreate,
    WishItem,
    WishVoteResult,
    WISH_CONTENT_MAX_CHARS,
    WISH_PAGE_DEFAULT,
    WISH_PAGE_MAX,
    WISH_TITLE_MAX_CHARS,
)


router = APIRouter(prefix="/wishes")


def _clean_title(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise user_error(400, "标题不能为空")
    if len(cleaned) > WISH_TITLE_MAX_CHARS:
        raise user_error(400, "标题过长，请精简后重试")
    return cleaned


def _clean_content(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise user_error(400, "详细说明不能为空")
    if len(cleaned) > WISH_CONTENT_MAX_CHARS:
        raise user_error(400, "详细说明过长，请精简后重试")
    return cleaned


@router.get("", response_model=PaginatedWishes)
def list_wishes(
    kind: Literal["bug", "feature", "plan"] | None = Query(None),
    sort: Literal["priority", "latest"] = Query("priority"),
    offset: int = Query(0, ge=0),
    limit: int = Query(WISH_PAGE_DEFAULT, ge=1, le=WISH_PAGE_MAX),
    user: UserProfile = Depends(get_current_user),
) -> PaginatedWishes:
    result = wish_repository().list_wishes(
        actor_id=user.id, kind=kind, sort=sort, offset=offset, limit=limit
    )
    return PaginatedWishes(
        items=[WishItem(**item) for item in result["items"]],
        total=result["total"],
        offset=offset,
        limit=limit,
    )


@router.post("", response_model=WishItem, status_code=201)
def create_wish(
    payload: WishCreate,
    user: UserProfile = Depends(get_current_user),
) -> WishItem:
    if payload.kind == "plan" and user.role != "admin":
        raise user_error(403, "仅管理员可发布更新计划")
    title = _clean_title(payload.title)
    content = _clean_content(payload.content)
    try:
        item = wish_repository().create_wish(
            kind=payload.kind,
            title=title,
            content=content,
            actor_id=user.id,
        )
    except PermissionError:
        raise user_error(403, "仅管理员可发布更新计划")
    return WishItem(**item)


@router.post("/{wish_id}/vote", response_model=WishVoteResult)
def toggle_wish_vote(
    wish_id: str,
    user: UserProfile = Depends(get_current_user),
) -> WishVoteResult:
    try:
        return WishVoteResult(**wish_repository().toggle_wish_vote(wish_id, user.id))
    except KeyError:
        raise user_error(404, "这条许愿墙内容不存在或已被删除")
    except ValueError:
        raise user_error(409, "更新计划不参与点赞排序")
