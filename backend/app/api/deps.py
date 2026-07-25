"""请求级依赖：单例仓库 + 当前用户解析 + notebook 访问守卫。"""
from functools import lru_cache
from typing import AsyncIterator, cast

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.request_context import set_request_user, reset_request_user
from app.models.identity import UserProfile
from app.repositories.factory import create_repository
from app.repositories.ports import AdminQueryRepository, NotebookRepository, IdentityRepository, NotebookAccessRepository, NotebookCatalogRepository, NotebookSharingRepository, SourceRepository, AskStreamPort, AskStateStorePort, McpMemoryRepository, MemoryRepository


@lru_cache
def repository() -> NotebookRepository:
    return create_repository(get_settings())

def identity_repository() -> IdentityRepository:
    return repository()._runtime.identity  # type: ignore[attr-defined]

def admin_query_repository() -> AdminQueryRepository:
    return repository()._runtime.queries  # type: ignore[attr-defined]

def notebook_catalog_repository() -> NotebookCatalogRepository:
    return repository()._runtime.catalog  # type: ignore[attr-defined]

def notebook_access_repository() -> NotebookAccessRepository:
    return repository()._runtime.sharing  # type: ignore[attr-defined]

def notebook_sharing_repository() -> NotebookSharingRepository:
    return repository()._runtime.sharing  # type: ignore[attr-defined]

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
    repo = identity_repository()
    token = _bearer_token(request)
    user: "UserProfile | None" = None
    if token:
        user = await run_in_threadpool(repo.resolve_session, token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
    elif settings.auth_optional:
        user = await run_in_threadpool(repo.current_user)  # ContextVar 未设 → seeded admin
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
        notebook_access_repository().user_can_access_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


async def require_notebook_read(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """读守卫:owner ∪ 只读成员。非授权 → 404(不泄露存在性)。"""
    allowed = await run_in_threadpool(
        notebook_access_repository().user_can_read_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# 向后兼容别名:老代码/未分类路由默认仍是 owner-only(默认最严兜底)。
require_notebook_access = require_notebook_write


def memory_service() -> MemoryRepository:
    return cast(MemoryRepository, repository())


def ask_state_repository() -> AskStateStorePort:
    return cast(AskStateStorePort, repository())


def memory_preview_client():
    return repository()._runtime.models.chat("memory_preview")  # type: ignore[attr-defined]


def mcp_memory_repository() -> McpMemoryRepository:
    return cast(McpMemoryRepository, repository())


# --- knowhow-tables PR-2+3 Task 10: "session OR Agent token" dependency -----
# Appended at EOF rather than interleaved above (e.g. right after
# get_current_user, which it otherwise reads a lot like): every line above
# this point is individually pinned by test_repository_surface_manifest.py
# (_runtime at :20/:23/:26/:29/:32, resolve_session at :57, current_user
# at :61, llm_client at :109, ...) and test_repository_callers_static.py —
# inserting anything above them shifts every one of those line numbers.
# Appending here keeps every existing pin exactly where it already is (same
# zero-line-shift discipline documented in services/knowhow/api.py above its
# own Task 10 section).
from contextlib import asynccontextmanager  # noqa: E402
from dataclasses import dataclass  # noqa: E402


async def _resolve_session_user(request: Request) -> UserProfile:
    """Session-Bearer -> UserProfile: the SAME resolve-or-auth_optional-
    fallback-or-401 behavior as get_current_user's own body, DUPLICATED
    rather than extracted/shared — get_current_user's body has individual
    lines pinned by both architecture guards (see this section's own header
    comment), so refactoring it to share this helper would shift those pins
    for zero behavioral gain. Used only by user_or_agent_scope's session
    branch below; get_current_user itself is untouched."""
    settings = get_settings()
    repo = identity_repository()
    token = _bearer_token(request)
    if token:
        user = await run_in_threadpool(repo.resolve_session, token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return user
    if settings.auth_optional:
        return await run_in_threadpool(repo.current_user)  # ContextVar 未设 → seeded admin
    raise HTTPException(status_code=401, detail="authentication required")


@dataclass(frozen=True)
class RequestActor:
    """Who is making this request — a signed-in user or an authenticated
    Agent token — reduced to what knowhow_agent_routes.py/mcp_server.py need:
    the resolved UserProfile (already set as the request's current user via
    set_request_user), and an actor_label for a write's audit trail (e.g.
    knowhow_cell_code.updated_by) that reads naturally for either kind of
    caller (an Agent's profile_name, or a session user's own id)."""
    user: UserProfile
    is_agent: bool
    actor_label: str


@asynccontextmanager
async def user_or_agent_scope(
    request: Request,
    notebook_id: str,
    scope: str,
    *,
    write: bool = False,
    not_found_detail: str = "Notebook not found",
) -> AsyncIterator[RequestActor]:
    """The auth CORE behind require_user_or_agent (see that factory for the
    Depends()-shaped wrapper used by a route whose notebook_id is a plain
    path/query parameter). A row/table-scoped agent route that must resolve
    notebook_id via a store lookup FIRST — no notebook_id in its URL at all,
    e.g. /agent/knowhow/rows/{row_id}/... — calls this directly as an async
    context manager instead, after that resolution; both paths share 100% of
    the security logic below, never duplicated.

    Bearer starting with ``snm_`` -> Agent token: resolve_agent_token (401 on
    a bad/expired/unknown token, mirrors AgentBearerMiddleware's own wording
    in app.api.mcp_server), then require_agent_access(principal, scope,
    notebook_id) — a LIVE re-check of scope/allowlist/revocation/expiry;
    PermissionError -> 404 (this codebase's "never confirm-or-deny a
    resource's mere existence via 403" convention: unauthorized and
    nonexistent get the identical response — see require_notebook_write/
    require_notebook_read above). ``write`` is IGNORED for an Agent
    principal — its read/write capability is entirely scope-driven (the
    CALLER picks knowledge:read for a read, knowhow:code for a code write),
    never a second owner/reader axis layered on top.

    Otherwise -> session Bearer (or the auth_optional seeded-admin
    fallback): resolves the user like get_current_user, then applies
    notebook_access_repository()'s existing owner-only (write=True) or
    owner-or-reader (write=False) guard — ``scope`` is IGNORED for a session
    user (design doc §⑥-4: "写入走新增 scope knowhow:code（用户界面走会话鉴
    权）" — a session's authority is notebook membership, never a scope).

    Either branch sets the request's current-user ContextVar (mirrors
    mcp_server._owner_request_context for the Agent branch, get_current_user
    for the session branch) for the duration of the ``yield`` and resets it
    in ``finally`` — every downstream repository call this feature makes
    depends on that boundary being set correctly."""
    token = _bearer_token(request)
    if token.startswith("snm_"):
        service = memory_service()
        principal = await run_in_threadpool(service.resolve_agent_token, token)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid or expired Agent token")
        try:
            await run_in_threadpool(
                service.require_agent_access, principal, scope, notebook_id
            )
        except PermissionError:
            raise HTTPException(status_code=404, detail=not_found_detail)
        owner = UserProfile(
            id=principal.owner_id, email="", display_name=principal.profile_name,
            role="user",
        )
        marker = set_request_user(owner)
        try:
            yield RequestActor(
                user=owner, is_agent=True, actor_label=principal.profile_name
            )
        finally:
            reset_request_user(marker)
    else:
        user = await _resolve_session_user(request)
        guard = (
            notebook_access_repository().user_can_access_notebook
            if write
            else notebook_access_repository().user_can_read_notebook
        )
        allowed = await run_in_threadpool(guard, notebook_id, user.id)
        if not allowed:
            raise HTTPException(status_code=404, detail=not_found_detail)
        marker = set_request_user(user)
        try:
            yield RequestActor(user=user, is_agent=False, actor_label=user.id)
        finally:
            reset_request_user(marker)


def require_user_or_agent(scope: str, *, write: bool = False):
    """Dependency FACTORY for a route whose notebook_id is directly a
    path/query parameter — FastAPI binds the inner dependency's own
    ``notebook_id: str`` argument the same way it would for any route
    function (path segment if the route has one by that name, else a query
    parameter — e.g. ``GET /agent/knowhow/tables?notebook_id=``). A
    row/table-scoped route that must resolve notebook_id via a store lookup
    does NOT use this factory; it calls ``user_or_agent_scope`` directly
    (see its own docstring)."""
    async def _dependency(
        request: Request, notebook_id: str,
    ) -> AsyncIterator[RequestActor]:
        async with user_or_agent_scope(
            request, notebook_id, scope, write=write
        ) as actor:
            yield actor
    return _dependency


# --------------------------------------------------------------------------
# 用户可见文案的「出处标记」
#
# ⚠追加在文件末尾是刻意的：本模块顶部那几个 `_runtime` 取值行被
# tests/test_repository_callers_static.py 的 INDEPENDENT_PRIVATE_SITES 按
# **行号**精确登记，在上方插入代码会整体移位、打破那份账本（见
# AGENTS.md 的 line-exact 约定）。新增模块级内容一律往这后面加。
#
# 后端的 4xx `detail` 有两类，结构上完全一样：一类是刻意为终端用户写的中文文案
# （「仅管理员可设为公共知识库」），另一类是 `detail=str(exc)` 直接甩出来的异常文本
# （含内网地址的上游错误、`field required`……）。前端没法靠形态区分它们——
# 「4xx 且含中文就原样显示」会把网关正文和异常串一起放行。
#
# 所以出处必须由这一侧显式声明：只有经 user_error() 抛出的 detail 才带
# `X-User-Message: 1`，前端也只信这一个标记。裸 HTTPException 一律不带标记，
# 前端按状态码给通用中文文案、原文只进 console。
#
# 用响应头而不是改 detail 的 JSON 形状：detail 是 MCP agent / 日志 / 排查的
# 契约，它的类型不能动。
# ⚠这个头必须同时登记进 main.py 的 CORS `expose_headers`，否则跨源部署时
# 浏览器读不到它（同源开发和前后端单测都察觉不到，见 tests/test_user_error.py）。
# --------------------------------------------------------------------------
USER_MESSAGE_HEADER = "X-User-Message"


def user_error(status_code: int, message: str) -> HTTPException:
    """构造一个「detail 可以原样展示给终端用户」的 HTTPException。

    message 必须是完整的中文用户文案：能读懂、可操作、不含异常类名 / 堆栈 /
    上游地址 / 字段名。凡是拼了 `str(exc)` 的一律**不要**用这个函数——它们
    本来就该被前端拦掉，只按状态码给通用文案。
    """
    return HTTPException(
        status_code=status_code,
        detail=message,
        headers={USER_MESSAGE_HEADER: "1"},
    )


from app.services.content_overview import ContentOverviewService  # noqa: E402
from app.services.checkup import CheckupService  # noqa: E402


def content_overview_service() -> ContentOverviewService:
    runtime = repository()._runtime  # type: ignore[attr-defined]
    return ContentOverviewService(runtime.memory_store, runtime.knowhow_store)


def checkup_service() -> CheckupService:
    """体检聚合 service(P2)。**由后端相关的 facade(SQLiteRepository)懒构造**——checkup 依赖
    maintenance 的 COUNT + sqlite QueryStore,不能落在中性 repository_runtime(neutrality 守卫禁其
    import sqlite/postgres)。facade 是 lru_cache 单例 → checkup 也是单例,H7/H8 进程内缓存跨请求存活。"""
    return repository().checkup  # type: ignore[attr-defined]


def shutdown_repository_if_initialized() -> None:
    """Close the cached runtime without constructing it during shutdown."""
    if repository.cache_info().currsize:
        repository().close()  # type: ignore[attr-defined]


# System model-service wiring is deliberately appended after the line-pinned
# repository compatibility sites above. Routes receive the one process-owned
# status service; they never reconstruct registries, providers, or schedulers.
from app.services.model_status import ModelStatusService  # noqa: E402


def model_status_service() -> ModelStatusService:
    return repository()._runtime.model_status  # type: ignore[attr-defined]


def model_provider_if_initialized():
    """Return the process-owned provider without constructing a repository."""
    if not repository.cache_info().currsize:
        return None
    return repository()._runtime.models  # type: ignore[attr-defined]


def model_service_binding_summary() -> dict[str, bool]:
    """Read-only readiness summary with no service identity or live diagnostics."""
    models = repository()._runtime.models  # type: ignore[attr-defined]
    return {
        "llm_configured": models.configured("ask_answer"),
        "reasoning_llm_configured": models.configured("reasoning_agent"),
        "embedding_configured": models.configured("retrieval_query_embedding"),
    }
