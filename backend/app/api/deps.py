"""请求级依赖：单例仓库 + 当前用户解析 + notebook 访问守卫。"""
from functools import lru_cache
from typing import AsyncIterator, cast

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.audit_actor import session_audit_principal
from app.core.request_context import set_request_user, reset_request_user
from app.models.identity import UserProfile
from app.repositories.factory import create_repository
from app.repositories.ports import AdminQueryRepository, GroupStorePort, NotebookRepository, IdentityRepository, NotebookAccessRepository, NotebookCatalogRepository, NotebookSharingRepository, NotebookStorePort, SourceRepository, AskStreamPort, AskStateStorePort, McpMemoryRepository, MemoryRepository


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

def group_repository() -> GroupStorePort:
    # 群组 / 组成员 / 授权边的行持久化(群组知识共享 P1-T3)。刻意直取 store 端口
    # 而不经一层 service:策略(谁能建哪一类组、双重条件的授权边创建、最后一名组
    # 管理员的 409)全在 group_routes.py,store 只管行,中间那层会是纯转发。
    # ⚠ 这个端口**不含**授权判定——「谁能读这个 notebook」仍只由
    # notebook_access_repository()/access_sql.py 回答。
    return repository()._runtime.groups  # type: ignore[attr-defined]

def source_repository() -> SourceRepository:
    return repository()

def notebook_store_port() -> NotebookStorePort:
    # 参与集(active 本身 + 有效挂载的参考库)的唯一解析点,供「按 active notebook
    # 代理读取参与库资源」的路由做 deny-by-default 的范围校验。有效性判定见
    # repositories/*/mount_sql.py —— 挂载边不是授权凭证,库易主/降级后边仍在但不生效。
    return repository()._runtime.notebook_store  # type: ignore[attr-defined]

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
    """读守卫:owner ∪ 只读成员 ∪ 有效授权边(user/group/group_admins/everyone)。

    非授权 → 404(不泄露存在性)。谓词的唯一定义点是两个后端的
    `repositories/*/access_sql.py`,这里只是一跳委托——读权扩了什么,这道守卫
    自动跟随。**写权不对称**:`require_notebook_capability` 那侧仍是 owner-only。
    """
    allowed = await run_in_threadpool(
        notebook_access_repository().user_can_read_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# --------------------------------------------------------------------------
# 按能力命名的 notebook 写守卫(P0-T2)。
#
# P0 阶段:每个能力都解析到与 require_notebook_write 完全相同的 owner-only 判定
# ——这一步只是把「一个裸守卫」拆成「能力名 → 判定级别」的一张表,给后续群组
# 授权(P1/P2:群组管理员可写某些能力)留一个接缝。届时改的只是这张表(以及
# require_notebook_capability 本体新增的判定分支),端点声明里
# ``Depends(require_notebook_capability("kg:write"))`` 这类调用点一个字都不用
# 再动一次。
#
# 值域目前只有 "owner" 一档(P0 冻结,见 test_notebook_capability_guard.py 的
# value-domain 断言)。
#
# ⚠ 群组授权(P1/P2)动这张表时,还有四个**表外**的写谓词/写投影消费点,翻转
# 能力级别时必须逐处核对(它们不经这张表,漏掉就是「API 收写而 UI 只读」或
# 「Agent 面与浏览器面口径分叉」):
#   1. `user_or_agent_scope` 的 session 分支(本文件下方)——Agent/MCP 面,
#      CLAUDE.md「MCP 工具面」红线已登记其独立的 owner-only 取向;
#   2. `knowledge_routes.py` 的 `can_edit` 响应投影——驱动前端编辑控件显隐,
#      判定放宽时它必须同步,否则新获授权的成员看到的是只读界面;
#   3. `knowhow_routes.py::transfer_knowhow_table` 的 mode 门(copy=读/move=写,
#      写半已走 notebook_capability_allowed,读半沿用 user_can_read_notebook);
#   4. `source_routes.py` 的 parse/delete 体内自查(已走
#      notebook_capability_allowed("sources:write"),随表自动翻转)。
#
# ⚠ `reports:write` 的已知前瞻:设计文档(docs/superpowers/specs/
# 2026-08-17-group-knowledge-sharing-design_zh.md §4)把「在共享库内创建自己的
# 深度报告」放在成员(viewer)档,而这里的 9 个 report 写端点挂的是同一个
# notebook 级能力——P1 落地成员自有报告时,这批端点需要行级 `created_by` 判定
# (谁建的谁能 generate/cancel/delete),不是翻一格表就完事。现在登记,免得
# P1 才发现接缝形状不够。
_CAPABILITY_LEVELS: dict[str, str] = {
    "sources:write": "owner",
    "kg:write": "owner",
    "knowhow:write": "owner",
    "knowledge:write": "owner",
    "catalog:write": "owner",
    "reports:write": "owner",
    "notebook:manage": "owner",
    "notebook:delete": "owner",
}


@lru_cache
def require_notebook_capability(capability: str):
    """按能力命名的 notebook 写守卫工厂。

    在**模块 import 时**(而不是第一次请求到达时)按 ``capability`` 查
    ``_CAPABILITY_LEVELS``——路由文件里的调用点都写成
    ``dependencies=[Depends(require_notebook_capability("sources:write"))]``,
    这个字符串实参在路由装饰器求值那一刻(也就是模块 import 时)就被传进来,
    未登记的能力名当场 ``KeyError``,逼着新端点显式登记,而不是漏迁移后
    静默落到某个宽松默认值上、直到线上才暴露。

    P0 阶段值域只有 ``"owner"`` 一档,直接复用 ``require_notebook_write`` 本体
    ——同一个函数对象,行为逐字相同(非 owner → 404,不泄露存在性)。这一步
    刻意不重写判定逻辑。
    """
    level = _CAPABILITY_LEVELS[capability]
    if level == "owner":
        return require_notebook_write
    raise AssertionError(f"unknown notebook capability level: {level!r}")  # pragma: no cover


# 工厂挂 @lru_cache 的理由写在这里而不是工厂 docstring:P0 阶段每个能力都返回
# 同一个 require_notebook_write 对象,缓存是 no-op;但 P2 按能力返回不同闭包时,
# FastAPI 的每请求依赖缓存按 callable 身份去重——不缓存的话,同一路由声明两个
# 能力依赖就会各拿一个新函数对象、多跑一次判定查询。现在加是零成本消掉那个坑。


def notebook_capability_allowed(capability: str, notebook_id: str, user_id: str) -> bool:
    """能力判定的纯函数版本(非 ``Depends``),与工厂共用同一张能力表。

    供路由体内**必须先从别的 id 反查出 notebook_id、再自查**的场景复用
    (source_routes.py 的 parse_source/delete_source:notebook_id 不是这两个端点
    URL 上的路径参数,守卫没法挂在静态的 ``Depends(...)`` 上)。带 ``capability``
    形参是为了让这些体内调用点与 73 个装饰器点吃**同一张** ``_CAPABILITY_LEVELS``
    表:P1/P2 翻转某个能力的级别时,体内点自动跟随,不会出现「组管理员能整库
    重解析、却删不掉单篇来源」的半翻转。未知能力名同样当场 ``KeyError``。

    别在调用点手拼 ``source_owner(source_id) == user.id`` 这类判据的第二份拷贝。
    注意「翻转能力级别时要核对的表外消费点」清单在 ``_CAPABILITY_LEVELS`` 上方
    的注释里——本函数只覆盖「进表」的那部分。
    """
    level = _CAPABILITY_LEVELS[capability]
    if level == "owner":
        return notebook_access_repository().user_can_access_notebook(
            notebook_id, user_id
        )
    raise AssertionError(f"unknown notebook capability level: {level!r}")  # pragma: no cover


# 向后兼容别名——**刻意删除**(P0-T2)。所有路由消费点已迁移到
# ``require_notebook_capability(...)``;留着这个别名会让漏迁移的新端点继续
# 安静地拿到 owner-only 守卫,而不是在 import 时就因为拼错能力名而报错。
# 结构扫描守卫(test_notebook_capability_guard.py)钉死它不再出现。


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
# get_current_user, which it otherwise reads a lot like) purely for
# readability — it is not required for correctness.
# (The original note here claimed every line above this point was
# individually pinned by a `test_repository_surface_manifest.py` and a
# `test_repository_callers_static.py`, and that inserting anything above them
# would shift those pins. That was already wrong: no such tests exist, the
# architecture guards are semantic — {path, scope, kind, target}, no line
# numbers — and P0-T2 inserted ~80 lines above this point (the capability
# guard factory) with every guard still green. See the identical correction
# in app/api/mcp_server.py above its own Task 10 section.)
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
    caller (an Agent's profile_name, or the canonical session audit label).
    ``identity_id`` remains the stable authorization/ownership identity."""
    user: UserProfile
    is_agent: bool
    identity_id: str
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
    never a second owner/reader axis layered on top. That is THIS surface's
    contract (design doc §⑥-4: a cell code attachment is inert — never
    executed, indexed, embedded, or projected), not a codebase-wide rule:
    the MCP source-management/build tools DO layer an owner-only gate on
    top of their scopes (mcp_server._writable_notebook) because a document
    write reaches every member's retrieval. The divergence is a recorded
    decision (AGENTS.md's Agent source-management bullet), pinned on both
    sides by backend/tests/test_memory_mcp.py.

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
                user=owner, is_agent=True, identity_id=principal.owner_id,
                actor_label=principal.profile_name,
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
            principal = session_audit_principal(user)
            yield RequestActor(
                user=user, is_agent=False, identity_id=principal.identity_id,
                actor_label=principal.audit_label,
            )
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
# 追加在文件末尾纯粹是可读性考量，不是正确性要求。
# （这里原先的注释声称本模块顶部那几个 `_runtime` 取值行被一份
# `tests/test_repository_callers_static.py` 的 INDEPENDENT_PRIVATE_SITES 按
# **行号**精确登记，在上方插入代码会整体移位、打破那份账本。那条说法本来就是
# 错的：不存在这样的测试，架构守卫是语义化的——{path, scope, kind, target}，
# 不含行号——P0-T2 在这个位置之上插入了约 90 行（能力守卫工厂 + 值域表），
# 全部守卫照样绿。与 app/api/mcp_server.py 里同一处更正完全一致。）
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
from app.services.kg_analysis import KgAnalysisService  # noqa: E402


def content_overview_service() -> ContentOverviewService:
    runtime = repository()._runtime  # type: ignore[attr-defined]
    return ContentOverviewService(runtime.memory_store, runtime.knowhow_store)


def checkup_service() -> CheckupService:
    """体检聚合 service(P2)。**由后端相关的 facade(SQLiteRepository)懒构造**——checkup 依赖
    maintenance 的 COUNT + sqlite QueryStore,不能落在中性 repository_runtime(neutrality 守卫禁其
    import sqlite/postgres)。facade 是 lru_cache 单例 → checkup 也是单例,H7/H8 进程内缓存跨请求存活。"""
    return repository().checkup  # type: ignore[attr-defined]


def kg_analysis_service() -> KgAnalysisService:
    """KG 质量分析报告 service(T3)。构造在**中性 runtime** 里(它只吃 database +
    unified_kg 两个 seam,不 import 任何后端),facade 只是一跳委托。facade 是 lru_cache
    单例 → 这个 service 也是单例,按 seq 记忆化的板块列表缓存因此跨请求存活。"""
    return repository().kg_analysis  # type: ignore[attr-defined]


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


from app.services.catalog_job import CommandCatalogService  # noqa: E402


def command_catalog_service() -> CommandCatalogService:
    """命令目录抽取 service(方案 C·C1b)。构造在**中性 runtime** 里,facade 只是一跳
    委托;facade 是 lru_cache 单例 → 这个 service 也是单例,取消事件注册表因此跨请求
    与后台线程存活(发起 job 的请求线程与跑 job 的线程必须看到同一份)。"""
    return repository().command_catalog  # type: ignore[attr-defined]


def model_service_binding_summary() -> dict[str, bool]:
    """Read-only readiness summary with no service identity or live diagnostics."""
    models = repository()._runtime.models  # type: ignore[attr-defined]
    return {
        "llm_configured": models.configured("ask_answer"),
        "reasoning_llm_configured": models.configured("reasoning_agent"),
        "embedding_configured": models.configured("retrieval_query_embedding"),
    }
