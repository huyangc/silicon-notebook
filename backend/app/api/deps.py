"""请求级依赖：单例仓库 + 当前用户解析 + notebook 访问守卫。"""
from functools import lru_cache
from typing import AsyncIterator, cast

from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.audit_actor import session_audit_principal
from app.core.request_context import set_request_user, reset_request_user
from app.services.model_work import model_artifact_scope
from app.models.identity import UserProfile
from app.bootstrap import application_extension_runtime, create_application_repository
from app.domain.report_export import ReportExporterHostPort
from app.repositories.ports import AdminQueryRepository, ExtensionToggleStorePort, GroupStorePort, NotebookRepository, IdentityRepository, NotebookAccessRepository, NotebookCatalogRepository, NotebookSharingRepository, NotebookStorePort, RetrievalExperienceStorePort, SourceRepository, AskStreamPort, AskStateStorePort, McpMemoryRepository, MemoryRepository, WishStorePort


@lru_cache
def repository() -> NotebookRepository:
    return create_application_repository(get_settings())


def report_exporter_host() -> ReportExporterHostPort:
    return application_extension_runtime().report_exporter


def identity_repository() -> IdentityRepository:
    return repository()._runtime.identity  # type: ignore[attr-defined]

def extension_toggle_repository() -> ExtensionToggleStorePort:
    return repository()._runtime.extension_toggles  # type: ignore[attr-defined]

def wish_repository() -> WishStorePort:
    return repository()._runtime.wishes  # type: ignore[attr-defined]

def admin_query_repository() -> AdminQueryRepository:
    return repository()._runtime.queries  # type: ignore[attr-defined]

def analysis_issue_repository():
    """Filesystem issue projection; deliberately not a facade mutation seat."""
    return repository()._runtime.analysis_artifacts  # type: ignore[attr-defined]

def notebook_catalog_repository() -> NotebookCatalogRepository:
    return repository()._runtime.catalog  # type: ignore[attr-defined]

def notebook_question_suggestions_service():
    return repository()._runtime.question_suggestions  # type: ignore[attr-defined]

def notebook_delete_repository():
    """批 3·W1 PR-3 §T-2 的删除 tombstone + 后台作业入口；不进
    `NotebookCatalogRepository` Protocol——`delete_notebook`（同步、
    全量、供测试/eval 直调）与新的 `request_delete`（202、tombstone、
    后台作业）是两个不同的方法名，刻意不合并（见
    `services/notebook_delete.py` 模块 docstring）。"""
    return repository()._runtime.notebook_delete  # type: ignore[attr-defined]

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


def global_ask_service():
    return repository()._runtime.global_ask_service()  # type: ignore[attr-defined]

def retrieval_experience_store() -> RetrievalExperienceStorePort:
    """检索经验库的行存储席位(Agentic Memory P2,按 notebook 分区)。

    走 facade **已有的** ``retrieval_experiences`` 属性,不用 ``_runtime``——
    公开面上已经有这一格,绕过它去取同一个对象只会让 facade 的消费点账目对不上。
    座位在 bundle 里是必填的(``RetrievalExperienceStorePort``,不是
    ``| None``),所以调用点不需要判空;``distillation_wiring_active`` 里那条
    ``store is not None`` 是给窄测试替身留的,不是这条路径的形态。

    ⚠ 取行之前先读该端口的 docstring:这里的 ``notebook_id`` 是**分区键**,
    不是本文件其它 store 给你的那种租户谓词。
    """
    return repository().retrieval_experiences


def retrieval_experience_jobs_service():
    """蒸馏 service 席位:界面「立即整理」按钮唯一的入口(``distill_now``)。

    这一格 facade 上**没有**,所以走 ``_runtime``——facade 的公开面只许收缩,
    为一个消费点加一格属性是反方向的。

    与 store 分两个席位取,而不是从这个 service 上摘 ``experiences``:读列表
    / 清空只需要行,和「有没有一条链路在跑」无关;合成一个入口会让读路径拿到
    一个它不该调用的 ``start()``。
    """
    return repository()._runtime.retrieval_experience_jobs  # type: ignore[attr-defined]


def global_ask_repository():
    """The global Ask store itself, for the administrator activity detail: it
    reads one owner's job with its admin-only record columns and applies its
    own reader rule, which the owner-facing service (``get_job``) does not
    model -- an administrator keeps the audit history after the owner has lost
    a participant's read access, exactly as ``guarded_ask_detail`` does."""
    return repository()._runtime.global_ask_store  # type: ignore[attr-defined]

def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def stream_credential_is_valid(token: str) -> bool:
    return identity_repository().resolve_session(token) is not None


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
        if await run_in_threadpool(lambda: repo.auth.get_policy()["mode"]) != "local":
            raise HTTPException(status_code=401, detail="authentication required")
        user = await run_in_threadpool(repo.current_user)  # ContextVar 未设 → seeded admin
    else:
        raise HTTPException(status_code=401, detail="authentication required")

    ctx_token = set_request_user(user)
    try:
        with model_artifact_scope(
            actor_id=user.id,
            notebook_id=str(request.path_params.get("notebook_id") or ""),
        ):
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


async def require_notebook_delete(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """`DELETE /api/notebooks/{id}` 专属守卫（codex #659 R6 P2）:owner-only,
    但**不**要求 notebook 处于 live 状态——`require_notebook_write` 的
    NOTEBOOK_WRITE_SQL 会把一次重复/重试的 DELETE 挡成 404（这一行已经是
    `status='deleting'`，对生命周期过滤而言"不存在"），而路由体内
    `notebook_delete_repository().request(...)` 本该把它分流成 409
    （`NotebookAlreadyDeletingError`，已在路由体内正确映射）——只是依赖层
    先一步挡下了请求，那个分支从未被走到。

    非 owner（或 notebook 从未存在过/已经被相位 5 彻底物理删除）仍然 404，
    与其它三道守卫同一条"不泄露存在性"口径——放宽的**唯一**一点是 owner 对
    自己正在删除中（或拷贝中）的库仍然"看得见"这一行。**不要**把这道守卫
    挂到除 DELETE 端点以外的任何路由上；其它写端点必须继续用
    `require_notebook_write`/`require_notebook_capability`。

    ⚠ 镜像写入围栏在这里**手工应用**（`_CAPABILITY_MIRROR_FENCE["notebook:delete"]`
    是 True，但这道守卫不经能力工厂，拿不到工厂产出的包装）。顺序与工厂那边逐字相同：
    先 owner 判定（非 owner 仍 404，不泄露存在性），通过之后才谈镜像。镜像只能由
    导入器退役——目标端删掉它只会让下一次导入把整本库重新造出来。"""
    allowed = await run_in_threadpool(
        notebook_access_repository().user_owns_notebook_regardless_of_lifecycle,
        notebook_id, user.id,
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    await _raise_if_mirrored(notebook_id)
    return notebook_id


async def require_notebook_admin(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """管理守卫:owner ∪ `role='admin'` 的有效授权边(P2 能力翻转,裁决 P2-1)。

    非授权 → 404(不泄露存在性),与另外两道守卫同口径。谓词的唯一定义点是两个后端的
    `repositories/*/access_sql.py::NOTEBOOK_ADMIN_SQL`,这里只是一跳委托。

    ⚠ 它**不是** `require_notebook_write` 的替代品:`notebook:delete` 仍解析到后者
    (删库恒 owner),Agent/MCP 面也仍走自己那条 owner-only 判定
    (`mcp_server._writable_notebook`)。哪几格翻、哪几格不翻,见下方
    `_CAPABILITY_LEVELS` 的注释。
    """
    allowed = await run_in_threadpool(
        notebook_access_repository().user_can_admin_notebook, notebook_id, user.id
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
    自动跟随。**三道守卫仍不对称**:`require_notebook_capability` 的六个内容管理能力
    在 P2 之后是 owner ∪ 管理边(`require_notebook_admin`),`notebook:delete` 仍是
    owner-only。
    """
    allowed = await run_in_threadpool(
        notebook_access_repository().user_can_read_notebook, notebook_id, user.id
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# --------------------------------------------------------------------------
# 按能力命名的 notebook 写守卫(P0-T2 建表,P2-T2 首次翻格)。
#
# P0 阶段:每个能力都解析到与 require_notebook_write 完全相同的 owner-only 判定
# ——那一步只是把「一个裸守卫」拆成「能力名 → 判定级别」的一张表,给后续群组
# 授权留一个接缝。**P2-T2 兑现了这个接缝**:改的只有这张表的值与工厂本体的判定
# 分支,73 个端点声明里 ``Depends(require_notebook_capability("kg:write"))`` 这类
# 调用点一个字都没动。
#
# 值域现在是 {"owner", "admin"}(见 test_notebook_capability_guard.py 的
# value-domain 断言):
#   * "owner" —— owner-only,`require_notebook_write` / `user_can_access_notebook`;
#   * "admin" —— owner ∪ `role='admin'` 的有效授权边,`require_notebook_admin` /
#     `user_can_admin_notebook`(谓词唯一定义点:`access_sql.NOTEBOOK_ADMIN_SQL`)。
#
# ⚠ 翻转这张表时,还有四个**表外**的写谓词/写投影消费点必须逐处核对(它们不经这张
# 表,漏掉就是「API 收写而 UI 只读」或「Agent 面与浏览器面口径分叉」)。P2-T2 的
# 逐项处置记录在此,后续任务照此清单继续核对:
#   1. `user_or_agent_scope` 的 session 分支(本文件下方)——Agent/MCP 面,
#      **刻意不翻**:`architecture.md` 的 Sharing 边界登记了它独立的 owner-only
#      取向(`mcp_server._writable_notebook`),浏览器面放宽不传导过去;
#   2. `knowledge_routes.py` 的 `can_edit` 响应投影——驱动前端编辑控件显隐,
#      **P2-T2 已改用 `notebook_capability_allowed("knowledge:write", ...)`**,
#      从此随本表自动跟随,不再是第二份手写判定;
#   3. `knowhow_routes.py::transfer_knowhow_table` 的 mode 门(copy=读/move=写,
#      写半已走 notebook_capability_allowed,读半沿用 user_can_read_notebook)
#      —— 随表自动翻转,已验证;
#   4. `source_routes.py` 的 parse/delete 体内自查(已走
#      notebook_capability_allowed("sources:write"))—— 随表自动翻转,已验证。
#
# ⚠ **能力守卫的 TOCTOU 窗口:哪些写端点必须在事务内再复检一次**(裁决,codex #519 R6)。
#
# 这道守卫与真正落库的写事务之间永远隔着一个窗口:守卫读到「你有管理权」之后、写事务
# 开始之前,库主完全可以撤掉发起人的管理边。**这个窗口不是每个写端点都要堵**——逐个端点
# 打补丁既做不完也没必要,判据是「这次写入产生的是什么」:
#
#   * **内容写入**(来源上传/删除/重解析、knowhow 写、图谱与检索索引构建、知识治理……)
#     在窗口内落库只是一次普通竞态。那些内容本就在库主掌控之下:他撤权之后照样能删掉、
#     改掉、重建它们,失权者多写进去的一行不会超出他的处置范围。**不加**事务内复检。
#   * **创建持久授权状态**的写入不同:它把访问权授予**他人**,而且效力**超出发起人自身
#     权限的存续**——发起人失权之后,那条边还在替他继续放行别人。它不是「库主事后能收拾
#     的一行内容」,是一条独立于他之外持续生效的授权。
#
# 所以规则是:**凡是写 `notebook_grants`(或未来任何授予他人访问权的行)的路径,必须在
# 同一写事务内复检并锁住发起人的笔记本侧权限**;其余写端点不加。
#
# 落地形态见 `repositories/*/group_store.py::_require_notebook_manage_on`(两段式:owner
# 半普通查 + 授权边半锁住**整条生效链**——行锁够不到 `EXISTS` 子查询里的行,所以既不能
# 直接给 `NOTEBOOK_ADMIN_SQL` 加锁,也不能只锁那条 `notebook_grants` 边:让 `group` /
# `group_admins` 边生效的那行 `group_members` 同样要锁,否则并发的移出组/降级照样能在
# 探测与 INSERT 之间提交(codex #519 R5 立、R8 P1 收口)。链的两环由
# `ADMIN_GRANT_USER_ARM_FOR_SHARE_SQL` + `ADMIN_GRANT_GROUP_CHAIN_FOR_SHARE_SQL` 覆盖。
# 当前的两个消费点:`create_grant`(发起人)与 `approve_share_request`(申请人)。
# 新增授权类写端点时照此办理。
#
# ⚠ 第五个消费点是**前端投影**:`NotebookSummary.can_manage_content`
# (`services/notebook_catalog.py`)。它不是授权判定(权威永远是这里的守卫),而是
# 「要不要把写入口画出来」的 UI 信号;判定放宽而它不动,组管理员会看到一个 API 允许
# 但界面藏起来的只读工作区。
#
# ⚠ `reports:write` 现在**没有任何端点消费**(P1-T3b),条目刻意保留:
# 报告的授权已经不是「一个 notebook 级能力」能表达的形状——9 个 report 写端点
# 改成了 `require_notebook_read` + 体内行级 `reports.created_by == 当前用户`
# (见 report_routes.py 顶部的两层授权说明),因为设计文档(docs/superpowers/
# specs/2026-08-17-group-knowledge-sharing-design_zh.md §4)把「在共享库内创建
# 自己的深度报告」放在成员(viewer)档,而报告按创建者隔离。能力名留给 P2 的
# 组管理员**管理**动作(例如批量清理本组库里的报告),届时新端点直接挂它;
# 现在删掉只会让 P2 重新想一遍这个名字。
# ⚠ 因此:翻转这一格**不会**让成员能动别人的报告(没有消费点),也**不是**
# 收回成员自建报告的开关——那条路径由 report_routes.py 的行级判定负责。
#
# P2-T2 翻的**恰好是这六格**(裁决 P2-1):sources/kg/knowhow/knowledge/catalog
# 五个内容写 + notebook:manage。⚠ manage 的 `PATCH /notebooks/{id}` 编辑的是**整份
# 描述性画像**(`NotebookUpdate` 的八个字段)而不只是改名——「改名」是端点的简写,别读成
# 字段清单(codex #519 R10)。安全性靠两条:八个字段没有一个参与授权判定(授权只在
# `access_sql.py`),且 `extra="forbid"` 挡住了生命周期列。反向护栏
# `backend/tests/test_notebook_update_authorization_free.py`。
# 留在 "owner" 的**恰好是这三格**:
#   * `notebook:delete` —— 删库的爆炸半径是整本库且 owner 无法撤销,不随组管理员走;
#   * `notebook:configure` —— **链接分享,P2-T2 评审 P0 拆出来的新格**(GET/POST/DELETE
#     `/notebooks/{id}/share`)。组管理员能替 Alice 铸公开链接、组外任意人整本 copy,
#     正是那次评审复现的 P0;撤链接还连带踢掉全部只读成员。设计 §4 的组管理员矩阵是
#     「改名 + 管理授权边」,**share_token 链接分享不在其中**——它是 owner 对本库对外
#     处置的配置,不随内容管理权转移。故单列一格恒 owner,不与 notebook:manage 合并
#     (合并会让这类端点跟着 manage 一起翻 admin)。
#   * `reports:write` —— P1-T3b 起已无端点消费(报告转成行级 created_by 判定),
#     它现在只是一个**留给 P2/P3 组管理员批量管理动作**的名字。翻它既不会放开也不会
#     收回任何东西(没有消费点),所以刻意保持 "owner":让这个名字在真正长出消费点
#     那天,由那次改动显式决定它属于哪一档,而不是被这次批量翻格顺手带走。
#
# ⚠ notebook:configure 与 notebook:mount 都解析到 **owner 档**(与 notebook:delete 同,
# 复用 require_notebook_write / user_can_access_notebook),所以能力值域仍是
# {owner, admin}——它们不新增第三档,只是把 owner-only 端点从 notebook:manage 拆出来
# 单独命名,好让「哪些端点恒 owner」在能力表上一眼可见、且不会被下一次批量翻格顺手带走。
#
# ⚠ **跨环境同步 §5 的两格再拆分**(docs/incremental-sync-design.md)。下方的
# `_CAPABILITY_MIRROR_FENCE` 要按能力名回答「这个端点会不会改写同步层内容」,而两个
# 旧能力名各自混着两类端点,一格答不了两件事,所以按那条轴再拆一次(**级别一个字
# 没变**,拆前拆后同一批端点解析到同一道级别守卫):
#   * `notebook:grant`(admin,从 notebook:manage 拆出)—— `group_routes.py` 的授权边
#     端点(GET/POST /notebooks/{id}/grants、DELETE .../grants/{grant_id}、POST 与 GET
#     .../share-requests)。它们写的是**目标端自己的可见性**,镜像上照常放行:设计 §5
#     明确「目标端自己的可见性由目标端管理;导入只在首次创建笔记本时写入源端授权,
#     之后不覆盖」。`notebook:manage` 留给 `PATCH /notebooks/{id}` 与
#     `POST /notebooks/{id}/tier`——它们改的是**同步来的**描述性字段与 tier,是同步层
#     内容,镜像上要挡。
#   * `notebook:mount`(owner,从 notebook:configure 拆出)—— `PUT /notebooks/{id}/bases`、
#     `GET .../mountable`、`GET .../mounted-by-count`。挂载配置写 `notebook_bases`,
#     属同步层,镜像上要挡(那两条 GET 只为「改挂载」服务,归同一格;它们本身是安全
#     方法,因而仍然照常返回——见围栏表上方的安全方法豁免)。`notebook:configure`
#     留给链接分享(`GET`/`POST`/`DELETE /notebooks/{id}/share`)与只读的挂载投影
#     `GET /notebooks/{id}/bases`——share_token 是**目标端自有列**,不随同步走;
#     `GET bases` 答的是「这本库此刻挂了什么」,镜像上照样要显示得出来。
#     ⚠ 两格拆开**不是**放宽:mount 与 configure 同为 owner 档,P2-T2 评审 P0 的那套
#     论证(mountable 枚举库主全部私有库名、PUT bases 把私有库挂进共享库经代理端点读
#     全文)逐字仍然成立,只是现在写在 notebook:mount 这一格上。
#   * `scale_index:write`(admin,从 kg:write 拆出)—— `POST .../scale-index/rebuild`
#     与 `POST .../scale-index/cancel`。检索索引(`kg_index/` `kg_viz/` 工件)是**目标端
#     自有的派生产物**,不在同步闭包里(设计 §6),所以它必须能在镜像上重建——否则一本
#     镜像库会永远停在导入那一刻的索引上,而重建又恰恰是目标端唯一的修复手段。
#     `kg:write` 留给真正写 KG 行的端点(构建、候选审核、schema 编辑)。
#
# ⚠ 第六个消费点同样是**响应投影**:Agentic Memory P1(T6)的
# `agent_profile_routes.py::GET .../understanding` 里的 `can_edit_base` 字段,由
# `notebook_capability_allowed("agent_profile:write", ...)` 算出,驱动前端「共享底座」
# 块是只读渲染还是可编辑。它随本表自动跟随(不是第二份手写判定),但翻转这一格时
# 仍要记得:判定放宽而投影不动,新获授权的成员会在这个面板上只看到只读的共享块
# ——与上面 `can_manage_content` 同一类失配。
_CAPABILITY_LEVELS: dict[str, str] = {
    "sources:write": "admin",
    "kg:write": "admin",
    "knowhow:write": "admin",
    "knowledge:write": "admin",
    "catalog:write": "admin",
    "scale_index:write": "admin",
    "reports:write": "owner",
    "notebook:manage": "admin",
    "notebook:grant": "admin",
    "notebook:configure": "owner",
    "notebook:mount": "owner",
    "notebook:delete": "owner",
    "agent_profile:write": "admin",
}


# --------------------------------------------------------------------------
# 目标端写入围栏(跨环境增量同步,docs/incremental-sync-design.md §5)。
#
# `notebooks.sync_origin` 非空 ⇒ 这本笔记本是从别的环境**同步来的镜像**。镜像上任何
# 会改写同步层内容的操作都必须被拒绝:目标端改了也留不住,下一次导入会原样覆盖回去,
# 而用户得到的是「我明明改过」的静默数据丢失。
#
# 这张表与 `_CAPABILITY_LEVELS` **键集合相同**(守卫测试钉死),值的含义只有一句:
# **这个能力的端点会不会改写同步层内容**。它是一条与「谁有权」正交的轴,所以单开一张
# 表而不是给级别值域加第三档——级别答「你是不是有权改」,围栏答「这本库的内容还允不允许
# 被改」,两个问题的答案互不蕴含(镜像的 owner 权限一点没少,他仍然能分享、能授权、能
# 提问,只是不能改同步来的内容)。
#
# 判定顺序是**先级别后围栏**,不可交换:未授权的人对镜像必须仍然拿 404(与对本地库
# 逐字相同,不泄露存在性),只有已经越过级别守卫的人才有资格看到 409 与 `sync_origin`。
#
# ⚠ **安全方法一律豁免**(HTTP `GET` / `HEAD` / `OPTIONS`)。这张表的谓词是「该能力的
# **端点**会不会改写同步层内容」,而一个 GET 永远不会——所以包装依赖先看
# `request.method`,是安全方法就根本不查 `sync_origin`。
#
# 这不是权宜之计,而是让归属与豁免各管各的那一条缝:**按能力归类**天然是粗粒度的
# (一个能力名底下既有写端点也有只为那个写服务的读端点——`GET .../scale-index/status`、
# `GET .../unified-kg/merges/review-job` 在 `kg:write` 下,`GET .../mountable`、
# `GET .../mounted-by-count` 在 `notebook:mount` 下)。没有这条豁免,唯一的出路是为每
# 一条这样的 GET 再劈一个能力名,能力表会因为一条与权限无关的轴而不断分裂,而每一格
# 都还得重新论证一次自己的级别。有了它,**归属只按写来定**,读端点跟着它服务的那个写
# 走,一格都不用挪。
#
# 反方向也成立:一条会改状态的 `POST`/`PUT`/`PATCH`/`DELETE` 永远拿不到豁免,所以
# 「把写端点伪装成 GET」这种事做不到——FastAPI 的方法来自路由声明,不来自请求体。
#
# 挡(True)的八格,逐格的理由(下面每一句说的都是那一格里的**非安全方法**):
#   * `sources:write` / `kg:write` / `knowhow:write` / `knowledge:write` /
#     `catalog:write` —— 材料、向量、KG、Knowhow 表、知识治理产物,全部在同步闭包里;
#   * `notebook:manage` —— `PATCH /notebooks/{id}` 与 `POST .../tier` 改的是同步来的
#     描述性画像与 tier(§5 点名的「笔记本改名与元数据编辑、tier 切换」);
#   * `notebook:mount` —— 挂载配置写 `notebook_bases`,属同步层;
#   * `notebook:delete` —— §5 点名:镜像只能由导入器退役,目标端删掉它只会让下一次
#     导入把整本库重新造出来。⚠ 这一格的**实际消费点不是能力工厂**:DELETE 端点挂的
#     是独立守卫 `require_notebook_delete`(理由见它自己的 docstring),围栏在那道守卫
#     体内单独应用。这一格留在表里是为了让「删库在镜像上也被挡」这件事在表上可见,
#     并让键集合保持与 `_CAPABILITY_LEVELS` 相等。
#
# 放行(False)的五格:
#   * `notebook:grant` —— 目标端自己的可见性由目标端管理(§5 明文放行);
#   * `notebook:configure` —— 链接分享的 `share_token` 是目标端自有列,不随同步走;
#   * `scale_index:write` —— 检索索引是目标端自有的派生产物,不在同步闭包里(设计 §6),
#     镜像必须能重建它,否则索引会永远停在导入那一刻且无从修复;
#   * `reports:write` —— 报告是交互数据,不同步(§1 的非同步清单);
#   * `agent_profile:write` —— 理解底座是目标端用户用出来的,同样不在同步闭包里。
#
# ⚠ 新增能力名时这张表**必须同时加一格**,否则 `require_notebook_capability` 当场
# KeyError——与能力表同一条「响亮失败,不许落到宽松默认值」的口径。
# ⚠ 这里的 True 只对**非安全方法**生效(见上面的安全方法豁免):一格取 True 说的是
# 「这个能力底下的写端点会改同步层内容」,不是「这个能力底下的一切请求都挡」。
_CAPABILITY_MIRROR_FENCE: dict[str, bool] = {
    "sources:write": True,
    "kg:write": True,
    "knowhow:write": True,
    "knowledge:write": True,
    "catalog:write": True,
    "scale_index:write": False,
    "reports:write": False,
    "notebook:manage": True,
    "notebook:grant": False,
    "notebook:configure": False,
    "notebook:mount": True,
    "notebook:delete": True,
    "agent_profile:write": False,
}


#: HTTP 方法里语义上「不改变服务端状态」的那一批(RFC 9110 §9.2.1)。围栏对它们
#: 一律豁免——理由写在 `_CAPABILITY_MIRROR_FENCE` 上方。刻意只列这三个:`TRACE` 虽然
#: 也安全但本应用根本不提供,写进来只会是一条无主条目。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: 镜像围栏拒绝时的 HTTP 码与 detail 形状(设计文档 §5)。409 而不是 403:请求者的
#: 权限没有问题,是**目标资源此刻的状态**不接受这次写入——与「正在删除中」「已有一个
#: 构建在跑」同一族。
#:
#: detail 形状与 `knowhow_routes.py` 的 `knowhow_history_stale` 那一族对齐:
#: `{"code", "message", "sync_origin"}`。`code` 给前端分支,`message` 是可直接展示的
#: 中文文案,`sync_origin` 让「镜像自 <源环境>」这句话不必再发一次请求去问。
#:
#: ⚠ **刻意不带 `X-User-Message`**:那个头是 `user_error()` 的标记,而 `user_error()`
#: 的 detail 是**裸字符串**(见本文件末尾「用户可见文案的出处标记」一节——前端靠这个
#: 头判断「4xx 的 detail 能不能原样显示」)。结构化 detail 是另一族:前端按 `code` 分
#: 支、拿 `message` 显示,本来就不会去原样打印整个 detail 对象,所以那个头在这里没有
#: 消费者。与 `knowhow_history_stale` / `knowhow_history_inconsistent` 逐字同款。
_MIRRORED_STATUS = 409
_MIRRORED_CODE = "notebook_mirrored"


def _mirror_message(origin: str) -> str:
    return f"此笔记本是从 {origin} 同步的镜像，内容只能在源环境修改"


def _mirror_detail(origin: str) -> dict[str, str]:
    return {
        "code": _MIRRORED_CODE,
        "message": _mirror_message(origin),
        "sync_origin": origin,
    }


async def _raise_if_mirrored(notebook_id: str) -> None:
    """已越过级别守卫之后的第二道判定:镜像 ⇒ 409。

    单独成函数而不是内联,是因为它有**三个**调用形态:能力工厂产出的包装依赖、
    `require_notebook_delete` 这个不经能力工厂的独立守卫,以及 `user_or_agent_scope`
    的会话写分支(第三条鉴权通道,承载 `knowhow_agent_routes` 的格子代码写)。三处
    必须逐字同义。

    ⚠ 调用方负责**只在非安全方法上调它**(包装依赖按 `request.method` 判,另外两处
    的消费端点本来就只有写方法)。这个函数自己不看方法:它拿不到 `Request`,而把
    `Request` 传进来只为了在三个调用点里的两个恒真的分支上再判一次,不值当。
    """
    origin = await run_in_threadpool(
        notebook_access_repository().notebook_sync_origin, notebook_id
    )
    if origin:
        raise HTTPException(status_code=_MIRRORED_STATUS, detail=_mirror_detail(origin))


@lru_cache
def require_notebook_capability(capability: str):
    """按能力命名的 notebook 写守卫工厂。

    在**模块 import 时**(而不是第一次请求到达时)按 ``capability`` 查
    ``_CAPABILITY_LEVELS``——路由文件里的调用点都写成
    ``dependencies=[Depends(require_notebook_capability("sources:write"))]``,
    这个字符串实参在路由装饰器求值那一刻(也就是模块 import 时)就被传进来,
    未登记的能力名当场 ``KeyError``,逼着新端点显式登记,而不是漏迁移后
    静默落到某个宽松默认值上、直到线上才暴露。

    每一档都**直接复用**对应守卫的函数本体——同一个函数对象,行为逐字相同
    (未授权 → 404,不泄露存在性)。刻意不在这里重写判定逻辑:两档各只有一份实现,
    这个工厂只做「能力名 → 哪一份」的查表。

    ⚠ 被 ``_CAPABILITY_MIRROR_FENCE`` 标记的能力**不**直接返回裸档位守卫,而是返回一个
    按能力名缓存的包装依赖:先 await 那一份档位守卫(级别判定、404 口径一个字不改),
    再看 ``request.method``——安全方法(GET/HEAD/OPTIONS)到此为止,非安全方法才多查
    一次 ``sync_origin``,非空则 409。包装是**加法**——判定逻辑仍然只有档位守卫那一份,
    这里只是在它之后串一条与权限正交的状态判定。
    """
    level = _CAPABILITY_LEVELS[capability]
    if level == "owner":
        level_guard = require_notebook_write
    elif level == "admin":
        level_guard = require_notebook_admin
    else:
        raise AssertionError(  # pragma: no cover
            f"unknown notebook capability level: {level!r}"
        )
    if not _CAPABILITY_MIRROR_FENCE[capability]:
        return level_guard

    async def guard(
        request: Request,
        notebook_id: str,
        user: UserProfile = Depends(get_current_user),
    ) -> str:
        await level_guard(notebook_id, user)
        # 安全方法豁免:围栏表答的是「这个能力的端点会不会改同步层内容」,而一个 GET
        # 永远不会。完整理由(为什么豁免放在这里而不是把每条只读端点劈成新能力名)
        # 写在 `_CAPABILITY_MIRROR_FENCE` 上方。
        if request.method.upper() in _SAFE_METHODS:
            return notebook_id
        await _raise_if_mirrored(notebook_id)
        return notebook_id

    guard.__doc__ = (
        f"档位守卫 + 镜像写入围栏(能力名 {capability!r},级别 {level!r})。\n\n"
        "顺序:级别判定(未授权 → 404,不泄露存在性)→ 安全方法直接放行 → "
        "非安全方法查 sync_origin,非空 → 409 notebook_mirrored。"
        "两张表见 api/deps.py 的 _CAPABILITY_LEVELS 与 _CAPABILITY_MIRROR_FENCE。"
    )
    # FastAPI 按 callable 身份做每请求依赖去重,而外层的 @lru_cache 保证同一能力名
    # 恒返回这**同一个** ``guard`` 对象——两条性质缺一不可,由
    # test_notebook_capability_guard 的身份断言钉住。名字带上能力名,好让
    # /openapi.json 与异常回溯里分得清是哪一格拒绝的。
    guard.__name__ = f"require_{capability.replace(':', '_')}_unmirrored"
    guard.__qualname__ = guard.__name__
    return guard


# 工厂挂 @lru_cache 的理由写在这里而不是工厂 docstring:P0 阶段每个能力都返回
# 同一个 require_notebook_write 对象,缓存是 no-op;**P2-T2 起它真的在按档返回不同
# 对象**(owner 档 → require_notebook_write,admin 档 → require_notebook_admin),
# 而 FastAPI 的每请求依赖缓存按 callable **身份**去重——不缓存的话,同一路由声明两个
# 能力依赖就会各拿一个新函数对象、多跑一次判定查询。P0 预埋的这一行现在开始兑现。
# ⚠ 兑现的前提是**同一个能力名恒返回同一个对象**(不只是「同一档恒返回同一个」),
# 那正是 lru_cache 的语义,由 test_notebook_capability_guard 的身份断言钉住。


def notebook_capability_allowed(capability: str, notebook_id: str, user_id: str) -> bool:
    """能力判定的纯函数版本(非 ``Depends``),与工厂共用同一张能力表。

    供路由体内**必须先从别的 id 反查出 notebook_id、再自查**的场景复用
    (source_routes.py 的 parse_source/delete_source:notebook_id 不是这两个端点
    URL 上的路径参数,守卫没法挂在静态的 ``Depends(...)`` 上)。带 ``capability``
    形参是为了让这些体内调用点与 73 个装饰器点吃**同一张** ``_CAPABILITY_LEVELS``
    表:P1/P2 翻转某个能力的级别时,体内点自动跟随,不会出现「组管理员能整库
    重解析、却删不掉单篇来源」的半翻转。未知能力名同样当场 ``KeyError``。
    P2-T2 正是靠这一条让 source_routes 的 parse/delete 体内自查、以及
    knowledge_routes 的 ``can_edit`` 投影**零改动地**跟着翻。

    别在调用点手拼 ``source_owner(source_id) == user.id`` 这类判据的第二份拷贝。
    注意「翻转能力级别时要核对的表外消费点」清单在 ``_CAPABILITY_LEVELS`` 上方
    的注释里——本函数只覆盖「进表」的那部分。
    """
    level = _CAPABILITY_LEVELS[capability]
    if level == "owner":
        return notebook_access_repository().user_can_access_notebook(
            notebook_id, user_id
        )
    if level == "admin":
        return notebook_access_repository().user_can_admin_notebook(
            notebook_id, user_id
        )
    raise AssertionError(f"unknown notebook capability level: {level!r}")  # pragma: no cover


def notebook_mirror_fence(capability: str, notebook_id: str) -> "str | None":
    """镜像写入围栏的纯函数版本(非 ``Depends``),与工厂共用同一张
    ``_CAPABILITY_MIRROR_FENCE``。

    返回 ``sync_origin``(即「这次写入该被挡」)或 ``None``(放行)。
    **刻意不返回 bool**:调用点要把源环境标识放进 409 的 detail 里,返回布尔会逼它
    再查一次库,而那次查询与本次之间又是一个窗口。

    为什么与 ``notebook_capability_allowed`` 并列而不是合进去:后者的返回值语义是
    「这个用户有没有这项能力」,它同时驱动**只读投影**(knowledge_routes 的
    ``can_edit``、agent_profile 的 ``can_edit_base``)——那些投影答的是权限,不该被
    镜像状态改写(镜像上用户的权限一点没少)。把围栏塞进那个 bool 会让两件事再也
    分不开。所以:凡是走 ``notebook_capability_allowed`` 的**写**路径,在它之后**再
    调一次**本函数;只读投影不调。

    ⚠ 它**刻意不带 method 形参**,因而没有包装依赖那条安全方法豁免:本函数的调用点
    全部是体内自查的**写**路径(source 的 parse/delete、knowhow 的表转移、插件 URL
    导入口、理解底座写),豁免在那里恒不命中。加一个恒不命中的形参只会让调用点每次
    都要想一遍「我该传什么」,并给「把它用在读路径上」开一个口子——而读路径本来就不
    该调它。

    未知能力名同样当场 ``KeyError``,与另外两条同口径。
    """
    if not _CAPABILITY_MIRROR_FENCE[capability]:
        return None
    origin = notebook_access_repository().notebook_sync_origin(notebook_id)
    return origin or None


def mirrored_notebook_error(origin: str) -> HTTPException:
    """把 ``notebook_mirror_fence`` 的返回值变成路由层要抛的那个异常。

    存在的理由是**形状只有一份**:409 与 ``{"code": ..., "sync_origin": ...}`` 这个
    detail 是前端解析的契约(设计文档 §5),散在各个体内自查点手拼迟早分叉。
    """
    return HTTPException(status_code=_MIRRORED_STATUS, detail=_mirror_detail(origin))


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
    decision (docs/product-and-api*.md's Agent source-management contract), pinned on both
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
        if write:
            # 镜像写入围栏也盖 Agent 令牌通道(codex #770 r1 P1)。`write` 对 Agent
            # 主体的**权限**判定确实被忽略(scope 说了算,见 docstring),但围栏问的
            # 不是权限而是「这次写碰不碰同步层的表」,与主体是谁无关;MCP 面的
            # `refuse_if_mirrored` 管不到这条 HTTP 路径(它直接进 knowhow_api)。
            await _raise_if_mirrored(notebook_id)
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
        if write:
            # 镜像写入围栏,第三条鉴权通道(跨环境同步 §5)。这条分支承载的是
            # `knowhow_agent_routes` 的 PUT/DELETE 格子代码,口径取 `knowhow:write`
            # 那一格——`knowhow_cell_code` 在笔记本内容闭包里(见 sharing_store 的
            # 深拷贝表清单),镜像上写进去的附件活不过下一次导入。
            #
            # 只挂在 `write=True` 上:`write=False` 的读分支拿的是读权谓词,围栏与它
            # 无关(安全方法豁免的同一条理由)。
            #
            # Agent 令牌分支在上面同样按 `write` 挡(两条通道同一道围栏);MCP 面的
            # `refuse_if_mirrored` 只盖 MCP 工具,盖不到这两条 HTTP 路由。
            await _raise_if_mirrored(notebook_id)
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
