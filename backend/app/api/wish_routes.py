"""Global wish-wall HTTP surface."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from app.api.deps import get_current_user, user_error, wish_repository
from app.models.identity import UserProfile
from app.models.wishes import (
    PaginatedWishes,
    WishCreate,
    WishItem,
    WishStatus,
    WishStatusUpdate,
    WishUpdate,
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
    status: WishStatus | None = Query(None),
    sort: Literal["priority", "latest"] = Query("priority"),
    offset: int = Query(0, ge=0),
    limit: int = Query(WISH_PAGE_DEFAULT, ge=1, le=WISH_PAGE_MAX),
    user: UserProfile = Depends(get_current_user),
) -> PaginatedWishes:
    result = wish_repository().list_wishes(
        actor_id=user.id, kind=kind, status=status, sort=sort, offset=offset,
        limit=limit,
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


@router.patch("/{wish_id}", response_model=WishItem)
def update_wish(
    wish_id: str,
    payload: WishUpdate,
    user: UserProfile = Depends(get_current_user),
) -> WishItem:
    """Author or administrator edit of kind/title/content.

    Ownership and the plan rule are both decided inside the store's write
    transaction (author_id and the current kind are read under the row lock):
    only an administrator may turn a non-plan into a plan, while an author may
    keep editing a plan an administrator already promoted, even when the
    payload repeats the unchanged kind.
    """
    if payload.kind is None and payload.title is None and payload.content is None:
        raise user_error(400, "没有需要修改的内容")
    title = _clean_title(payload.title) if payload.title is not None else None
    content = _clean_content(payload.content) if payload.content is not None else None
    try:
        item = wish_repository().update_wish(
            wish_id, actor_id=user.id, kind=payload.kind, title=title, content=content,
        )
    except KeyError:
        raise user_error(404, "这条许愿墙内容不存在或已被删除")
    except PermissionError as exc:
        # The store distinguishes "not yours" from "promoting to a plan needs
        # admin"; an unchanged kind="plan" on an already-promoted plan is fine.
        if str(exc) == "admin role required":
            raise user_error(403, "仅管理员可发布更新计划")
        raise user_error(403, "只能修改自己发布的内容")
    return WishItem(**item)


@router.delete("/{wish_id}", status_code=204, response_class=Response)
def delete_wish(
    wish_id: str,
    user: UserProfile = Depends(get_current_user),
) -> Response:
    try:
        wish_repository().delete_wish(wish_id, actor_id=user.id)
    except KeyError:
        raise user_error(404, "这条许愿墙内容不存在或已被删除")
    except PermissionError:
        raise user_error(403, "只能删除自己发布的内容")
    return Response(status_code=204)


@router.put("/{wish_id}/status", response_model=WishItem)
def set_wish_status(
    wish_id: str,
    payload: WishStatusUpdate,
    user: UserProfile = Depends(get_current_user),
) -> WishItem:
    if user.role != "admin":
        raise user_error(403, "仅管理员可标记处理状态")
    try:
        item = wish_repository().set_wish_status(
            wish_id, status=payload.status, actor_id=user.id
        )
    except KeyError:
        raise user_error(404, "这条许愿墙内容不存在或已被删除")
    except PermissionError:
        raise user_error(403, "仅管理员可标记处理状态")
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
