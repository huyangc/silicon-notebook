"""请求级依赖：单例仓库 + 当前用户解析 + notebook 访问守卫。"""
from functools import lru_cache
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.models.schemas import UserProfile
from app.repositories.ports import NotebookRepository, IdentityRepository, NotebookAccessRepository, SourceRepository, AskStreamPort
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)


@lru_cache
def repository() -> NotebookRepository:
    return SQLiteRepository(get_settings())

def identity_repository() -> IdentityRepository:
    return repository()

def notebook_access_repository() -> NotebookAccessRepository:
    return repository()

def source_repository() -> SourceRepository:
    return repository()

def ask_stream_repository() -> AskStreamPort:
    return repository()


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


async def get_current_user(request: Request) -> AsyncIterator[UserProfile]:
    """解析 Bearer token → session → user，写入 ContextVar（请求结束复位）。
    无 token 且 settings.auth_optional → 回退 seeded admin；否则 401。
    注意：必须是 async 依赖——其 ContextVar.set 在请求 task 上下文生效，
    随后被 Starlette 复制进同步路由的 threadpool；同步依赖里 set 不会传播。"""
    settings = get_settings()
    repo = repository()
    token = _bearer_token(request)
    user: "UserProfile | None" = None
    if token:
        user = await run_in_threadpool(repo.resolve_session, token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
    elif settings.auth_optional:
        user = repo.current_user()  # ContextVar 未设 → seeded admin
    else:
        raise HTTPException(status_code=401, detail="authentication required")

    ctx_token = set_request_user(user)
    try:
        yield user
    finally:
        reset_request_user(ctx_token)


async def require_notebook_write(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """写守卫:仅 owner。非 owner → 404(不泄露存在性)。"""
    allowed = await run_in_threadpool(
        repository().user_can_access_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


async def require_notebook_read(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """读守卫:owner ∪ 只读成员。非授权 → 404(不泄露存在性)。"""
    allowed = await run_in_threadpool(
        repository().user_can_read_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# 向后兼容别名:老代码/未分类路由默认仍是 owner-only(默认最严兜底)。
require_notebook_access = require_notebook_write
