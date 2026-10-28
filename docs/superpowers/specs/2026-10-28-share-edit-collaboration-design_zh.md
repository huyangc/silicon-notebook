# 分享的编辑权限层与近实时协作 — 设计规格

- 日期：2026-10-28
- 状态：**用户已批准（2026-10-28）：D1–D18 全部按推荐，已立项（P1 先、P2 后）。**
  原状态为「待评审 → 待用户拍板」（用户裁决 16，2026-10-19：「长期方向四项都启动设计：先写规格、
  评审后给用户看」）。本文只是设计，不含实现；§12 的决策点拍板之前不进入计划（现已拍板）。
- 待办出处：`fangan_todo.md` 第三节「分享的 edit 权限层与近实时协作」——「写权限仍 owner-only；
  无 presence / revision 轮询……原 spec 的 D2 / D3 决策已被账号系统取代，动手前须重写」。
- 取代：`docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` 的 §4 Phase C
  （view/edit 分享）与 §5 Phase D（近实时协作）。该文的 D2（用户名正则）/ D3（无密码
  `X-User-Id` 头）已被账号系统与统一登录取代；D1（轮询 + presence、无 websocket/CRDT、
  last-write-wins）在本文 §6 重新论证后部分保留。
- 关联且**不被本文改动**的设计：群组知识共享 `2026-08-17-group-knowledge-sharing-design_zh.md`
  （其决策 4「不引入 editor」与本文 §4 的推荐方案直接相关，见 §12 决策 D1）；问答会话分享
  `2026-08-18-conversation-sharing-design_zh.md`；跨环境增量同步 `docs/incremental-sync-design.md`；
  删库宽限窗口 `2026-10-22-notebook-delete-grace-window_zh.md`；产品合同
  `docs/product-and-api_zh.md`「群组知识共享」一章。

规范定义以 PostgreSQL 一侧为准（SQLite 是镜像，所有谓词双后端同修）。下文「现状」的每条断言都
给了出处；行号以本文提交时的 `master`（`824615155`）为准。

---

## 1. 一句话结论

原 spec 设想的「edit 档」今天**已经以另一种形态部分存在**：群组共享的 `(group_admins, admin)`
授权边让组管理员在浏览器里拥有内容写权（来源、图谱、Knowhow、知识治理、命令目录、检索索引、
理解底座），但**没有任何入口能把写权授予一个具体的人**，也没有任何「别人改了什么 / 谁在看」
的信号。本设计补两件事：

1. **编辑者（editor）**：笔记本主人点名授予某个用户「可编辑」——内容写权，不含共享管理、
   画像修改与一切 owner 专属处置。复用既有授权边表与 `user` 主体臂，新增一个角色值与一档能力级别。
2. **协作脉搏（pulse）**：一个只在「确有他人可读」的笔记本上启用的轻量轮询端点，同时承担在场心跳
   与「哪一块内容变了」的版本信号。在场与版本状态全在进程内；成本如实记账：
   - owner 自己的写入：零新增数据库读写（变更只进进程内环形缓冲）；
   - 非 owner 的写入：若决策 D11 选推荐方案，每次 +1 条短事务 INSERT（「最近改动」持久日志），
     不增加读（owner id 由同一次授权查询顺带取回，§6.5）；
   - 每次脉搏请求：既有的会话鉴权 + 读权点查，各借一次连接池连接（§6.6）。

两期各自可独立交付（§11）。

---

## 2. 现状（以代码为准）

### 2.1 三条授权谓词与包含链

- 唯一定义点：`backend/app/repositories/postgres/access_sql.py`（SQLite 镜像
  `repositories/sqlite/access_sql.py`）。模块 docstring 第 9–11 行写明包含链
  **写权 ⊆ 管理权 ⊆ 读权**。
  - 写权 `NOTEBOOK_WRITE_SQL`（`access_sql.py:615`）= `notebooks.created_by = 用户`，owner-only。
  - 管理权 `NOTEBOOK_ADMIN_SQL`（`:627`）= owner ∪ `role='admin'` 的有效授权边；主体判定
    `_admin_principal_match_expr`（`:207`）= 受限三臂（user / group / group_admins）∧
    `role='admin'`，排除 `everyone`。
  - 读权 `NOTEBOOK_READ_SQL`（`:633`）= owner ∪ `notebook_members` 有行 ∪ 任意有效授权边
    （四类主体，**不看 role**，见 `_principal_match_expr` `:183`、`grant_access_expr` `:239`）。
- **`user` 主体臂已经在读权与管理权谓词里**：`_restricted_principal_arms`（`:139`）第一条臂就是
  `principal_type='user' AND principal_id=用户`。也就是说一条 `(user:U, admin)` 授权边今天如果
  存在，就会立刻让 U 获得全部 admin 档能力——缺的只是写入口。
- `notebook_grants.role` 无 CHECK 约束：`postgres/migrations/0027_group_sharing.sql` 第 5–8 行写明
  「本迁移新增的每一个枚举列都在应用层校验，不用 CHECK」，建表语句里 `role text COLLATE "C" NOT NULL`
  （第 72 行）确实没有约束。应用层的角色白名单是 `GRANT_ROLES`（`models/groups.py:30`）。新增角色值
  不需要改表结构，但要改应用层白名单所在的写入口。

### 2.2 能力表（浏览器 HTTP 面）

`backend/app/api/deps.py:485` `_CAPABILITY_LEVELS`，值域 `{owner, admin}`：

| 能力名 | 级别 | 端点数（`require_notebook_capability` 装饰器） | 主要内容 |
| --- | --- | --- | --- |
| `sources:write` | admin | `source_routes.py` 9 + 体内 2 | 上传/URL/导入/重解析/删除来源、附件 |
| `kg:write` | admin | `kg_routes.py` 17 + `notebook_routes.py` 1 | 图谱构建/重建/维护/审核/schema、索引管线切换 |
| `knowhow:write` | admin | `knowhow_routes.py` 27 | Knowhow 表增删改、批量改写、回退、转移 |
| `knowledge:write` | admin | `knowledge_routes.py` 6 + `admin_routes.py` 1 | 知识治理 |
| `catalog:write` | admin | `catalog_routes.py` 5 | 命令目录 |
| `scale_index:write` | admin | `kg_routes.py` 2 | 检索索引重建/取消 |
| `agent_profile:write` | admin | 体内（`agent_profile_routes.py:191/264/410/415/620`） | 共享「理解底座」、检索经验 |
| `notebook:manage` | admin | `notebook_routes.py` 2 | `PATCH /notebooks/{id}`（八字段画像）、`POST .../tier`（另有系统管理员门，`notebook_routes.py:277`） |
| `notebook:grant` | admin | `group_routes.py` 6 | 群组授权边、共享申请 |
| `notebook:configure` | owner | `notebook_routes.py` 4 | `share_token` 链接分享、只读挂载投影 |
| `notebook:mount` | owner | `notebook_routes.py` 4 | 挂载配置 |
| `notebook:delete` | owner | 独立守卫 `require_notebook_delete`（`deps.py:235`） | 删库 |
| `notebook:export` | owner | `notebook_transfer_routes.py` 1 | 整本导出 |
| `reports:write` | owner | 零消费点（报告改为行级 `created_by`，见 `deps.py` 能力表上方注释） | — |

- 守卫工厂 `require_notebook_capability`（`deps.py:633`）在 import 时查表，未知能力名 `KeyError`；
  纯函数版 `notebook_capability_allowed`（`deps.py:703`）供体内自查（来源 parse/delete
  `source_routes.py:507/886`、Knowhow 转移 `knowhow_routes.py:1485/1491`、插件路由
  `extension_routes.py:177`、知识治理 `knowledge_routes.py:133/225/283/327`）。
- 与权限正交的镜像围栏 `_CAPABILITY_MIRROR_FENCE`（`deps.py:561`），键集合与能力表相等（守卫测试
  `backend/tests/test_notebook_capability_guard.py` 钉死）。
- 能力表上方注释登记了「翻转能力级别时必须逐处核对的表外消费点」六处（`deps.py` 约 360–480 行）：
  `user_or_agent_scope` 会话分支、`knowledge_routes` 的 `can_edit`、Knowhow 转移 mode 门、
  source parse/delete 体内自查、`NotebookSummary.can_manage_content`、`agent_profile` 的
  `can_edit_base`。本设计新增一档时同样要逐处过（§4.3）。

### 2.3 读权从哪里来、写权从哪里来

- **分享链接**（`notebook_routes.py:391–509`）：owner 铸 `share_token`；持链接的**登录**用户
  （三个 `/shared/{token}` 端点都挂 `get_current_user`）对小库只能整本 copy、对大库只能
  `join` 成为 `notebook_members(role='reader')`（`join_shared_route` `:492`，小库 400
  「use copy, not join」）。链接分享恒 owner（`notebook:configure`），撤链接连带踢掉全部只读成员
  （`unshare_notebook_route` `:423`）。owner 没有逐人移除只读成员的入口（只读共享设计
  `2026-07-01-notebook-share-phase2-readonly-design.md` §2「不做逐个移除」）。
- **群组授权边**：`POST /notebooks/{id}/grants` 只收 `GRANTABLE_PRINCIPAL_TYPES = ("group",
  "group_admins")`（`backend/app/models/groups.py:27`），角色 `GRANT_ROLES = ("viewer", "admin")`
  （`:30`）。`group_routes.py` 模块 docstring 第 37–40 行明说 `user` 主体「继续走既有只读共享
  （share_token）流程」——即**没有任何写入口产生 `principal_type='user'` 的边**。
- 授权边的读与删也挂 `notebook:grant`（admin 档）：`GET /notebooks/{id}/grants`
  （`group_routes.py:581–596`）四类主体**全部**返回（docstring：「这个清单是库主『谁能看我的库』的
  完整答案」）；`DELETE /notebooks/{id}/grants/{grant_id}`（`:653–664`）只按 `id + notebook_id` 删
  （PG `group_store.py:810` `delete_grant`），不看主体类型。也就是说，一旦出现 `user` 主体边，组管理员
  就能看到并删掉它。
- **结论**：今天唯一的非 owner 写权来源是 `(group_admins:G, admin)` 边，且只在浏览器面生效。
  「让一个具体的同事能改我的库」现在只能绕道：建一个项目组、把同事设为组管理员、把库共享给组并勾
  「组管理员可管理这本笔记本」——这同时给了他 `notebook:grant`（能把库再共享给他管理的任何组）
  与 `notebook:manage`（改画像）。

### 2.4 列表与详情的权限投影

- `NotebookSummary.access` 值域 `"owner" | "reader"`（`backend/app/models/notebooks.py:199–203`，
  群组设计已定裁决 7：不加枚举值）；`can_manage_content`（`:238`）是「要不要画写入口」的 UI 信号。
- 列表 `NotebookSummaryQuery.list_for_user`（`services/notebook_catalog.py:850`）按 owner → 只读
  成员 → 群组授权三段拼装，`can_manage_content` 由 `notebook_grant_confers_admin`
  （`repositories/group_rows.py:104`）判边自己的 role。
- **群组授权那一段只查群组主体**：`postgres/query_store.py:818` `granted_notebook_rows` 的
  JOIN 条件是 `ng.principal_type IN ('group', 'group_admins')`（`:848`）。一条 `user` 主体边
  会让谓词放行、却**不会出现在列表里**——新增 `user` 边必须同步扩这条投影（§4.5）。
- 「已分享」徽标 `is_shared`（`notebooks.py:248–251`）= `notebooks.is_shared` 列 ∪ 指向群组的授权边
  （`notebook_catalog.py:466–479`），同样不认 `user` 主体边。
- 前端能力位的主判据是 `workspaceCapabilities`（`frontend/app/workspace-transitions.ts:184`），
  `canWrite = access !== "reader" || canManageContent`；它派生出的 `canGrantAccess`（群组授权边）与
  `canManageNotebook`（改名/画像/tier）**同样取 `canWrite`**（同文件约 232、248 行）。组管理员的改名
  控件由 `can_manage_content` 驱动（`frontend/app/notebook-reader-actions.tsx:60/112`）。
- 另有三处**不经** `workspaceCapabilities` 的手写判据，都按 `access !== "reader" || can_manage_content`：
  - `frontend/app/groups-page.tsx:418` `manageableNotebooks`：「共享给本组」的候选库列表（`notebook:grant`）；
  - `frontend/app/use-notebook-collection.ts:304–318` `rowCanManageContent` /
    `rowExplicitlyDeniesManageContent`，以及 `:484` `editorVisibleIds`（笔记本编辑弹窗还该不该留在屏上）；
    同文件 `:352–362` 的注释说明编辑弹窗同时承载改名/画像/tier 与**索引管线切换**，管线切换的保存
    在 `:858–868` 只看 `canManageContent`；
  - `frontend/app/transfer-model.ts:64` `destinationNotebooks`：Knowhow 转移目标在 `allowManaged` 时
    放行 `can_manage_content`，Memory 转移目标恒 owner。

### 2.5 永远按创建者私有的数据

- 问答会话：`postgres/ask_state_store.py:202–214` `LIST_CONVERSATIONS_SQL` 按
  `c.notebook_id=%s AND c.created_by=%s`；`GET /conversations/{id}` 按 `conversation_owner`
  比对（`ask_routes.py:1130–1135`）。群组设计决策 1「会话成员间不可见」。
- 深度报告：`report_routes.py` 头部两层授权（读守卫 + 行级 `created_by`，owner 也只看见自己的）；
  公开报告页实时复核创建者读权（`public_report_route` `:916`、`:953–960`）。
- Memory：`memory_items.created_by` + 带存活谓词的读权子句（`postgres/memory_store.py`
  `_read_access_clause`）。被撤权不删 Memory（`NotebookSharing.remove_member`
  `services/notebook_sharing.py:563`，docstring：保留、不可读、复权即回）；自助退出才按确认数删
  （`MemoryService.leave_notebook` `services/memory_service.py:1297`）。撤权同时清掉该人的理解覆盖层
  （`_clear_member_profiles` `notebook_sharing.py:616`）。
- 全局问答会话：按 `user_id` 隔离；全局会话分享每次打开以分享者身份实时复核所有涉及笔记本的读权
  （`GlobalAskService.public_conversation` `services/global_ask.py:2707`）。

### 2.6 Agent / MCP 面

- 五档 `("read", "ask", "contribute", "manage", "delete")`（`backend/app/domain/agent_tools.py:18`），
  `AGENT_OWNER_ONLY_TIERS = {"manage", "delete"}`（`:55`）；签发时要求持 manage/delete 的令牌至少
  有一本白名单库是属主自己的（`memory_service.py:627` `AgentOwnerOnlyTierError`）。
- 来源写/删/构建工具走 `_writable_notebook`（`api/mcp_tools/_shared.py:953`），失败抛
  `OwnerOnlyError`（`:1069`）——**owner-only，组管理员也不放行**。产品合同把这条分歧登记为刻意
  （`docs/product-and-api_zh.md`「角色」一节末段：长期 token 是独立凭据，签发后很久才获授管理权）。
- contribute 档（`propose_memory` / `put_knowhow_cell_code` / `add_observation`）刻意对只读读者也
  可用（`_shared.py` 约 1003–1040 行登记）。

### 2.7 匿名面与登录面

不需要登录的只有：注册/登录/SSO、Agent 接入说明、导出下载票据、公开报告
（`/api/public/reports/{token}`）与公开会话（`/api/public/conversations/{token}`，同一处理器服务
笔记本会话分享、全局会话分享与 MCP `alnk-` 回答链接，`ask_routes.py:1545–1566`）。这些全部是
**只读投影**；任何笔记本写端点都在挂了 `get_current_user` 的主 router 上（`backend/app/main.py`
第 576–612 行的挂载顺序）。分享链接 `/shared/{token}` 需要登录（§2.3）。

### 2.8 检索层负责权限

- 用户架构裁决（memory「检索层负责权限」）：权限与来源范围只在检索层审核，合成与终态不做权限兜底。
- 参与集解析唯一定义点 `postgres/mount_sql.py`（`MOUNT_VALID_EXPR` `:52`），**随查看者**解析
  （`architecture.md` Sharing 一行）。借入挂载的「未共享门」：受限读权借入的参考库，只在挂载方
  笔记本**没有任何** `notebook_members` 行、也没有任何 `notebook_grants` 行时有效
  （群组设计 §6.1、产品合同「读权 ⇒ 可挂载，以及借入挂载的『未共享门』」）。
- 全局问答选库：`readable_notebook_names` / `readable_notebook_ids`（`postgres/sharing_store.py`
  约 1086–1106 行）= 读权子句 ∧ 存活，经 `GlobalAskService._resolve_run_scope`（`global_ask.py:945`）。

- 「未共享门」是**挂载方笔记本这一级**的谓词：`_MOUNTER_NOT_SHARED_EXPR`（`postgres/mount_sql.py:47`）
  只看挂载方 `a.id` 有没有成员行/授权边，不区分查看者。门一关，借入边对**所有人**失效——包括挂载人
  （owner）自己在这本库里的问答。M3 的查看者片段 `_VIEWER_REACHES_MOUNT_EXPR`（`:125`）是在
  `MOUNT_VALID_EXPR` 之上**再**合取的一层（`:141`），收不回已被门关掉的边。今天 owner 开链接分享
  有人加入、或共享给群组时，已经是这个行为。

**推论**：编辑者是读权的子集里的一种授权边，读权谓词不看 role，所以本设计**不改**检索层、
全局问答、挂载参与集的任何判定；唯一的连带效应是「未共享门」——授予第一个编辑者会让 owner
自己在这本库里也检索不到借入的参考库（§4.6、决策 D13）。

### 2.9 并发与单飞现状

- **来源重解析无单飞**：`POST /sources/{id}/parse`（`source_routes.py:492`）同步调用
  `parse_source`；批量 `POST /notebooks/{id}/sources/reparse`（`:536`）逐个 `submit_job`。
  `sources.status` 的写入是无条件 `UPDATE`（`postgres/source_store.py` `set_status`）。唯一的串行化
  是进程内逐来源 `threading.Lock`（`services/source_ingestion.py` `_source_chunk_lock`），第二个
  重解析阻塞后**整份重做**。MCP 的 `reparse_source` 是唯一先探测 `source_parse_busy` 的调用方，
  注释直说「process_source 背后没有单飞守卫，浏览器的重解析路由无条件提交」
  （`api/mcp_tools/sources.py:557–570`）。
- **重解析 vs 删除已安全**：删除在写事务里 `SELECT ... FOR UPDATE` 锁来源行
  （`source_store.py` `source_exists_for_update_tx`），重解析的元素替换在同一行上复检，来源已删则
  `KeyError` 被 `process_source` 记失败吞掉。
- **图谱构建单飞三层**：持久层部分唯一索引 `idx_kg_build_jobs_one_running`
  （`postgres/migrations/0002_integrity_indexes.sql:22`）→ `KgBuildAlreadyRunning`；进程内
  `kg_building` 标记与维护槽 `KgMaintenanceJobs`（`services/kg/maintenance_jobs.py`）。HTTP 返回
  `user_error(409, "当前笔记本已有知识图谱分析任务正在运行")`（`kg_routes.py:64/105/145`）或按持有者
  分文案的 `_kg_maintenance_busy`（`:54`）。`kg_build_jobs.created_by` 记录了发起人。
- **Knowhow 单元格**：带 `expected_before` 时在一个写事务里比值，失配 409「内容已被其他人修改，
  请刷新后重试」（`knowhow_routes.py:540–561`）；**手动单元格编辑器不带它，显式 last-write-wins**
  （`:532–541` 注释）。回退带 `expected_head_seq`，失配 409 `knowhow_history_stale`。
- 生产**约定单进程**：`scripts/prod.sh:167–176` 以 `--workers 1` 启动，部署文档要求恰好一个后端进程。
  启动校验 `validate_process_local_scheduler_deployment`（`services/model_provider.py:106`）只看
  环境变量 `WEB_CONCURRENCY` / `UVICORN_WORKERS`（`_WORKER_ENVIRONMENT_VARIABLES`，`:73`），**挡不住**
  命令行 `--workers N`，也挡不住多实例部署。进程内单飞、进程内总线都以「约定单进程」为前提；违反约定时
  的失效模式见 §6.6。

### 2.10 审计现状

- `session_audit_principal`（`backend/app/core/audit_actor.py`）把身份拆成 `identity_id`（授权与归属）
  与 `audit_label`（不可变审计文本）。
- 记录「谁干的」的列只有：`sources.uploaded_by`（迁移 `0045_source_upload_actor.sql`，只记上传人，
  重解析不改）、`kg_build_jobs.created_by`、`catalog_jobs.created_by`、Knowhow 的
  `knowhow_changes.actor` / `knowhow_milestones.created_by` / `knowhow_cell_code.updated_by`、
  `notebook_grants.created_by`、`kg_analysis_marks.actor_user_id`。**来源的重解析与删除、图谱维护、
  知识治理动作没有落库的操作者**；只有按用户分目录的 JSONL 事件日志（`core/event_logging.py`）。

### 2.11 推送与轮询基础设施

- 没有 SSE / WebSocket，所有推送都是 NDJSON 流式 HTTP（`api/task_stream.py` `NDJSON_STREAM_HEADERS`）。
- 唯一的进程内发布订阅是 `pending_bus`（`services/pending_bus.py`，docstring 首行「进程内待办事件总线
  （单进程部署）」），喂 `GET /me/pending-actions/stream`（`api/system_routes.py:292`）；管理员页的
  「在线」= 持有该流的用户集合（`pending_bus.online_user_ids` `:102`）。
- 流式响应的每一帧都经 `AuthenticationStreamGuard`（`api/auth_stream.py`）在线程池里复核一次会话
  （`:44–46`），即每帧一次数据库会话查询。
- 前端对来源、图谱状态、报告全部是**按需轮询**（`use-source-library.ts` 1.5s 起退避到 15s；
  `page.tsx` / `use-kg-graph.ts` 若干 3–6s 的忙时轮询），只在「我自己发起了什么」时启动；
  **不存在「别人改了」的信号**。只有全局问答轮询与笔记本列表的访问复核看 `visibilitychange`
  （`use-notebook-collection.ts:533–549`，30s 节流）。

### 2.12 一条已登记的取舍（本设计提议推翻，决策 D6）

Knowhow 单元格「代码附件」的浏览器写路径走 `/agent/knowhow/rows/{row}/cells/{col}/code`
（`frontend/app/knowhow-model.ts:835/842`），后端是 `user_or_agent_scope(..., write=True)`
（`knowhow_agent_routes.py:151/181`），其会话分支写判定是 owner-only 的 `user_can_access_notebook`
（`deps.py:915`），而同一张表的单元格内容写是 admin 档 `knowhow:write`——今天组管理员能改单元格内容、
却改不了代码附件。

这**不是**代码与注释不符：`deps.py:924` 附近「口径取 `knowhow:write` 那一格」说的是**镜像围栏**取哪一格，
不是权限档；会话写分支的权限档在能力表上方「翻转时要核对的表外消费点」第 1 条（`deps.py:367–370`）
登记为 P2-T2 **刻意不翻**。所以决策 D6 是推翻一条已登记的裁决。另两点事实：

- 只读读者今天就能写代码附件——持 contribute 档 Agent 令牌经 Agent 分支（scope 决定，`write` 被忽略，
  `deps.py` `user_or_agent_scope` docstring）或 MCP `put_knowhow_cell_code`（contribute 档）。会话分支的
  owner-only 并没有把附件写收窄到 owner。
- 若推翻，能力 × 角色矩阵里「组管理员 × 代码附件会话写」这一格从 404 变为 2xx，是**预期变化**，
  矩阵测试要显式登记它，而不是当作回归。

---

## 3. 目标与非目标

### 目标

1. 笔记本主人能把**内容编辑权**授予一个点名的登录用户，并随时撤销；被授予者在浏览器里看到并能使用
   全部内容写入口，看不到 owner 专属入口。
2. 多个写者同时操作时：不出现 500、不出现静默覆盖别人刚写的 Knowhow 单元格、不重复跑整份重解析、
   忙碌拒绝的文案说清「谁在跑什么」。
3. 打开同一本多人可见笔记本的人，能在 ~15 秒内看到「来源 / 图谱 / Knowhow / 知识治理 / 命令目录 /
   画像」中哪一块被别人改了并自动刷新（编辑中的块只提示不打断），能看到当前有谁也在这本库里。
4. 对单人使用的笔记本**零额外成本**：不轮询、不新增查询、不改任何热路径。

### 非目标

- 不做 WebSocket / CRDT / OT / 字符级光标 / 编辑锁（沿用原 spec D1 的非目标）。
- 不改问答会话、深度报告、Memory 的「按创建者私有」（群组设计决策 1 不动）。
- 不改分享链接：链接永远只产生 copy 或只读加入，**不存在「可编辑链接」**（§8.3）。
- 不改检索层、参与集解析、全局问答选库的任何判定（§2.8 推论）。
- 不做多进程部署下的跨进程在场同步（生产约定单进程，违反约定时的降级见 §6.6）。
- 不改群组模型的两级角色（组侧是否开放 editor 见 §12 决策 D4，推荐不开）。
- 不改所有权模型（单 owner `notebooks.created_by`，编辑者不是共同所有人）；配额仍记 owner（群组设计决策 5）。

---

## 4. 权限模型

### 4.1 编辑者的精确语义（推荐方案，见决策 D1）

新增授权边角色 `editor`，**只**用于 `principal_type='user'` 的边（P1 不对群组主体开放）。
有效角色顺序 `viewer < editor < admin < owner`。

| 能力 | 只读（成员 / viewer 边） | **编辑者（editor 边）** | 组管理员（admin 边） | owner |
| --- | :-: | :-: | :-: | :-: |
| 打开、看来源/图谱、提问、存自己的 Memory、建自己的报告、挂载为参考库 | ✓ | ✓ | ✓ | ✓ |
| `sources:write` 新增/重解析来源、附件 | | ✓ | ✓ | ✓ |
| 删除来源 | | 仅本人上传的（决策 D14） | ✓ | ✓ |
| `kg:write` 图谱构建/维护/审核/schema、索引管线 | | ✓ | ✓ | ✓ |
| `scale_index:write` 检索索引重建 | | ✓ | ✓ | ✓ |
| `knowhow:write` Knowhow 表写（含代码附件，决策 D6） | | ✓ | ✓ | ✓ |
| Knowhow 表「移走」到别的库（`transfer` 的 `mode=move`） | | ✗，只能复制（决策 D14） | ✓ | ✓ |
| 删除整张 Knowhow 表、清理 Knowhow 变更历史 | | ✗（决策 D14） | ✓ | ✓ |
| `knowledge:write` 知识治理 | | ✓ | ✓ | ✓ |
| `catalog:write` 命令目录 | | ✓ | ✓ | ✓ |
| `agent_profile:write` 共享理解底座、检索经验 | | ✓ | ✓ | ✓ |
| `notebook:manage` 改画像（八字段）、tier 入口 | | | ✓ | ✓ |
| `notebook:grant` 群组授权边、审批共享申请 | | | ✓ | ✓ |
| `notebook:collaborate`（新）管理编辑者名单 | | | | ✓ |
| `notebook:configure` 链接分享 / `notebook:mount` 挂载配置 | | | | ✓ |
| `notebook:export` 整本导出 / `notebook:delete` 删库与撤销删除 | | | | ✓ |
| Agent/MCP 的 manage / delete 档 | | | | ✓ |

划线依据只有一条：**编辑者能改「库里有什么」，不能改「谁能看到这本库、这本库叫什么、库本身的存废与
去向」**。

- 不给 `notebook:grant`：组管理员今天能把库共享给自己管理的任何组，而项目组人人可建
  （`group_routes.py:241`），等于能把库给任何人看。点名邀请的编辑者如果也有这条，owner 的「我只
  邀请了张三」就不成立。
- 不给 `notebook:manage`：画像八字段不参与授权（`test_notebook_update_authorization_free.py`），给了
  也不提权；但「改名」是库的身份，原 spec 与群组设计都把它放在管理档，编辑者不需要它来协作。若用户
  认为编辑者应能改画像，可在决策 D1 选方案 A。
- `agent_profile:write` 给编辑者：共享理解底座是从库内容派生的描述，Agentic Memory 设计（`2026-08-18-
  agentic-memory-design.md` 第 133 行）把它定为「同 Knowhow 口径：owner/可写成员」。
- 四处收窄（推荐，决策 D14）：编辑者只能删除 `sources.uploaded_by` 等于本人的来源；不能把 Knowhow 表
  `move` 出本库（`copy` 照常）；不能删除整张 Knowhow 表；不能清理 Knowhow 变更历史。理由：四者对 owner 都不可撤销，而点名邀请的
  编辑者不像组管理员那样经过「组管理员」这层组织身份。实现上是体内判定，落在 `delete_source` 的体内自查
  （`source_routes.py:886` 之后，行级：比对 `uploaded_by`）、Knowhow 转移的 mode 门（`knowhow_routes.py:1485–1491`）
  与整表删除端点 `DELETE /notebooks/{id}/knowhow/{table_id}`（`knowhow_routes.py:191–196`）、历史清理端点（`:1426–1430`）（两者当前都挂
  `knowhow:write`，体内追加「调用者须有管理权」）；判据「调用者有编辑权但无管理权」由新的纯函数给出，不在
  调用点手拼。`uploaded_by` 列已存在（迁移 `0045_source_upload_actor.sql`），但**没有进任何对外投影**
  （`backend/app/models/` 与 `backend/app/api/` 里无此字段），P1 要给来源列表投影新增「上传人是否为本人」
  （或上传人 id）字段供前端判断删除按钮——这是 P1 范围的实现项。
  - **删除整张 Knowhow 表同样收窄**：它是硬删除——`DELETE FROM knowhow_tables WHERE id=%s`
    （`postgres/knowhow_store.py:1235`），`knowhow_changes` 与 `knowhow_milestones` 都以
    `REFERENCES knowhow_tables(id) ON DELETE CASCADE` 挂在表上（`postgres/migrations/0008_master_v28_features.sql`
    第 47、62 行），变更历史随表一起消失，不可恢复。按「对 owner 不可撤销的动作不给点名编辑者」这条规则，
    编辑者不能删整表（行、列、单元格的删除有变更历史可回退，照常允许）。
  - **图谱删除与重建不收窄，但这是一个取舍**：图谱可以从来源重新抽取，可人工做过的决定——合并审核的确认/
    驳回、冲突裁决、关系审核——重建后不会自动回来，得有人再做一遍。仍给编辑者的理由：这类维护本来就是
    「协作编辑」的主要内容（组管理员今天同样可以做），且每次删除/重建都会进「最近改动」（D11），owner 知道
    是谁做的；若用户认为人工审核结果也该受保护，D14 可选 (c′)，把图谱删除与全量重建也收为管理档。
  - **清理 Knowhow 变更历史同样收窄**：`POST .../knowhow/{table_id}/history/prune`（`knowhow_routes.py:1426–1430`，
    挂 `knowhow:write`）硬删除最老的连续前缀（`sqlite/knowhow_history_store.py:415` 起，PG 侧薄适配复用同一实现），
    `before_days` 最小可以是 1（`models/knowhow.py:433`，`Field(gt=0, le=36500)`）。编辑者用它能抹掉 owner
    一天以前的全部回退历史，正好击穿「行、列、单元格删除可由变更历史回退」这条前提。与整表删除同样做法：
    端点体内追加「调用者须有管理权」。

  **七个编辑档能力下破坏性端点的逐个处置**（按「对 owner 是否可撤销」这一条规则；只列会删东西或作出
  不可逆决定的端点）：

  | 端点 | 能力 | 可撤销吗 | 处置 |
  | --- | --- | --- | --- |
  | `DELETE /sources/{id}`（`source_routes.py:879`） | `sources:write` 体内 | 否 | (b)：仅本人上传 |
  | `DELETE .../knowhow/{table_id}`（`knowhow_routes.py:191`） | `knowhow:write` | 否（硬删除 + 历史级联） | (b)：管理档 |
  | `POST .../knowhow/{table_id}/history/prune`（`:1426`） | `knowhow:write` | 否（硬删除历史） | (b)：管理档 |
  | `POST .../knowhow/{table_id}/transfer` 的 `move`（`:1485–1491`） | `knowhow:write` 体内 | 否（表离开本库） | (b)：管理档 |
  | `DELETE .../knowhow/{table_id}/rows/{row_id}`、`.../columns/{column_id}`（`:480`、`:440`） | `knowhow:write` | 是（`row_delete` / `column_delete` 记入变更历史，`postgres/knowhow_store.py:678/1639`，可回退） | 不收窄 |
  | `POST .../knowhow/{table_id}/revert`（`:1333`） | `knowhow:write` | 是（回退本身也记历史） | 不收窄 |
  | `DELETE .../knowhow/{table_id}/milestones/{id}`（`:1410`） | `knowhow:write` | 里程碑只是命名标记，删了它变更流水还在，仍可按序号回退 | 不收窄 |
  | `POST .../kg/delete`、`.../kg/rebuild`、`.../unified-kg/rebuild`（`kg_routes.py:222/130/287`） | `kg:write` | 图谱可从来源重建，人工审核结果不会回来 | 不收窄；(c′) 收为管理档 |
  | 合并/冲突审核的确认、驳回、批量审核（`kg_routes.py:498/507/591/625/638`） | `kg:write` | 这些是协作编辑本身要做的决定 | 不收窄 |
  | `DELETE .../understanding/experiences`（`agent_profile_routes.py:490–498`，体内 `_require_experience_manager` 判 `agent_profile:write`） | `agent_profile:write` 体内 | 本库检索经验一次清空；经验来自所有读者的使用、可再蒸馏，但积累要时间 | 不收窄；(c′) 收为管理档 |
  | `DELETE .../understanding/{label}`、`POST .../understanding/rebuild`（`:561`、`:607`） | `agent_profile:write` 体内 | 共享理解底座可重建 | 不收窄 |
  | `DELETE .../object-schemas/{object_type}`（`knowledge_routes.py:184`） | `knowledge:write` | schema 覆盖是配置，可重新设置 | 不收窄 |
  | `scale-index/cancel`、`command-catalog/cancel`（`kg_routes.py:388`、`catalog_routes.py:273`） | — | 取消进行中的作业，不删数据 | 不收窄 |

  实现时要把这张表作为守卫测试的一部分：枚举七个编辑档能力下全部非安全方法路由，每条要么在表里登记
  「不收窄」及理由，要么带管理权体内判定。新增破坏性端点不登记即红。

### 4.2 永远 owner-only 的清单与理由

| 动作 | 守卫 | 理由 |
| --- | --- | --- |
| 删库、撤销删除 | `require_notebook_delete` / `require_notebook_cancel_delete` | 爆炸半径整本库；宽限窗口的撤销只认主人（删库宽限设计裁决 B1） |
| 导出整本 | `notebook:export` | 文件可在别的部署导入成导入者自有的新库 |
| 链接分享 | `notebook:configure` | bearer 凭证；撤链接连带踢全部只读成员 |
| 挂载配置 | `notebook:mount` | `mountable` 会枚举 owner 全部私有库名；挂私有库进共享库等于经代理端点公开全文（P2-T2 评审 P0） |
| 编辑者名单 | `notebook:collaborate`（新） | 授予他人写权，效力超出发起人自身权限（§4.4） |
| Agent 令牌 manage/delete 档 | `_writable_notebook` | 见 §4.7 |
| 模型配置 | 不是笔记本能力 | 能力表里没有任何笔记本级的模型配置格（§2.2），模型服务配置在笔记本授权模型之外，本设计不触及；笔记本内与模型相关的只有索引管线切换（`kg:write`，编辑者可用）。作业的模型调用按发起人打标（`model_artifact_scope(actor_id=...)`，`deps.py:214`） |

### 4.3 谓词与能力表改动

1. `access_sql.py`（双后端）新增**编辑权**谓词：
   `NOTEBOOK_EDIT_SQL` = owner ∪ `role IN ('admin','editor')` 的有效授权边（复用
   `_restricted_principal_arms`，排除 `everyone`），存活过滤同 `NOTEBOOK_ADMIN_SQL`。
   包含链变为 **写权 ⊆ 管理权 ⊆ 编辑权 ⊆ 读权**，由构造保证（管理权的主体判定是编辑权的子条件）。
   管理权谓词**一字不改**——`role='admin'` 仍然只认 admin，编辑者不会漏进管理档。
2. `_CAPABILITY_LEVELS` 值域扩为 `{owner, admin, edit}`；上表中七格（`sources:write`、`kg:write`、
   `scale_index:write`、`knowhow:write`、`knowledge:write`、`catalog:write`、`agent_profile:write`）从
   `admin` 改为 `edit`。因为编辑权 ⊇ 管理权，**组管理员的能力集合逐格不变**（回归防线：能力 × 角色
   矩阵测试，§10）。新增 `notebook:collaborate: owner`，围栏表同步加一格 `False`（编辑者名单是目标端
   自有可见性，同 `notebook:grant` 的理由）。
3. 工厂与纯函数版各加一个 `edit` 分支，复用新增的 `require_notebook_edit` /
   `user_can_edit_notebook`（`NotebookAccessRepository` 端口加一个方法，两后端 `sharing_store` 实现）。
4. 表外消费点逐处核对（§2.2 的六处）：
   - `user_or_agent_scope` 会话写分支 → 改为按 `knowhow:write` 判（决策 D6）；Agent 分支不动。
   - `knowledge_routes` 的 `can_edit`、Knowhow 转移、来源 parse/delete、`can_edit_base`：都走
     `notebook_capability_allowed`，随表自动跟随，补测试即可。
   - `NotebookSummary.can_manage_content` → 语义改为「编辑权」，新增 `can_manage_notebook`（§4.5）。
5. 插件路由按插件声明的能力名走同一张表（`extension_routes.py:177`），自动跟随；插件 SDK 文档补一句
   「内容写能力现在包含编辑者」。

### 4.4 发放与撤销

新增端点（写方法挂能力 `notebook:collaborate`，owner-only，非 owner 一律 404 不泄露存在性）：

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| `GET` | `/notebooks/{id}/collaborators` | 本库**全部** `principal_type='user'` 的授权边，每行带角色（`editor` / `viewer` / 其他历史值如实返回）、用户名、显示名、授予时间、授予人。owner 读全量；编辑者是否可读见决策 D17（推荐：编辑者只读 `editor` 行的名字与显示名）。其他人 404 |
| `PUT` | `/notebooks/{id}/collaborators/{user_id}` | 授予（幂等 upsert `(user:U, editor)` 边；已有同一用户的 `viewer` 边则改为 `editor`）。目标用户经既有 `GET /users/resolve?username=`（`group_routes.py:297`）解析 |
| `DELETE` | `/notebooks/{id}/collaborators/{user_id}` | 撤销该用户的 `user` 主体边，**不论角色**；`user_id` 等于调用者本人时是「退出协作」，此时守卫改为「本人持有这条边」 |

**完整性约束**：`GET /grants`（群组 / everyone 行）与 `GET /collaborators`（全部 `user` 行）合起来
**恰好**覆盖 `notebook_grants` 的四类主体，每一行都在其中一个端点可见、可撤。这样即使出现不是本设计
写入口产生的 `user` 行（跨环境同步的种子写入、历史数据、手工修复），owner 也能看见并撤销，不会留下
「看不见、撤不掉」的读权。守卫测试：对两个端点的并集与表内行做集合相等断言。

- 校验：目标 ≠ owner；目标账号存在且有效；笔记本存活；`tier='base'` 的公共库拒绝（公共库的写权模型
  是系统管理员发布，不做个人协作）。
- **PUT 的角色语义是「设为 editor」，不是「至少 editor」**：同一用户已有 `(user, viewer)` 边则升为 editor；已有
  遗留的 `(user, admin)` 边（本设计不产生它，但同步种子或手工数据可能有）则**降为** editor。降级是显式的：
  `GET /collaborators` 会把那条 admin 行如实列出（完整性约束），前端对它的「添加为编辑者」按钮旁写明「当前为
  管理权限，确认后将改为可编辑」。
- **库被升为公共库时已有编辑者**：与「授予时拒绝公共库」保持一致——`POST /notebooks/{id}/tier` 切到 `base`
  时若存在 `(user, editor)` 边，返回 409「请先移除这本笔记本的编辑者」，不静默删边、也不允许公共库带着
  点名编辑者存在。这条复核与 tier 写入在同一写事务里做。降回 `personal` 不受影响。
- **TOCTOU**：`deps.py` 能力表上方的规则——「凡写授予他人访问权的行，必须在同一写事务内复检并锁住
  发起人的笔记本侧权限」。授予（PUT）的写事务按 `create_grant` 的既有顺序：
  1. `_lock_notebook_on`（`postgres/group_store.py:414`，codex #519 R7）：以 `FOR KEY SHARE` 锁笔记本行并
     复核存在，不在则 `NotebookNotFoundError` → 404（防并发删库后 INSERT 撞外键成 500）；
  2. 同一事务里复核笔记本**存活**（`NOTEBOOK_LIVE_SQL`）：`pending_delete` / `deleting` / `copying` /
     `importing` 一律 404。**这一步只是尽力而为，不是互斥承诺**：第 1 步的 `FOR KEY SHARE` 刻意不与普通
     `UPDATE notebooks SET …` 冲突（`_lock_notebook_on` docstring 的理由：改成 `FOR SHARE` 会把本路径拖进
     与改名、容量闸等一整类无关的阻塞），所以删库请求把 `status` 改成 `pending_delete` 的那条 UPDATE 可以与
     本事务交错，留下一个「库刚进非存活态、编辑者边仍落库」的窗口。让库进入非存活态的不只是 owner 的删库：
     跨环境同步导入器在导入期间挂 `importing`、深拷贝写入方给**新副本**挂 `copying`；而镜像上 owner 同样能授予
     （`notebook:collaborate` 的围栏为 False）。本设计**接受**这个竞态，结论对每种来源都一样：授予只能由 owner
     发起，落下的边在非存活期间不生效（三条谓词都带存活过滤）；库恢复存活后（撤销删除、导入收尾翻成
     `draft`）它按 owner 授予时的意图生效；库被物理删除时随 `ON DELETE CASCADE` 消失。`copying` 只出现在尚不
     可见的新副本上，owner 无从对它授予；
  3. `_require_notebook_manage_on` 的 **owner 半**（`group_store.py:992`）比对 `notebooks.created_by`。该函数
     docstring 说明 owner 半刻意不加锁——`created_by` 只在建库与深拷贝时写入，事务期间不可能被撤销。
     授权边链那一半（admin 边的带锁探测）不涉及，因为发起人只能是 owner。
- **群组授权边端点对 `user` 边的处置**（否则组管理员能绕过「名单只归 owner」）：
  - `GET /notebooks/{id}/grants` 排除 `principal_type='user'` 的行；它的 docstring「库主『谁能看我的库』
    的完整答案」改由两个端点合起来兑现（上面的完整性约束；前端是分享弹窗的群组授权区 + 「点名共享」区，
    后者只对 owner 渲染，列出编辑者与其他 `user` 行）。`everyone` 行照旧返回。
  - `DELETE /notebooks/{id}/grants/{grant_id}` 命中 `user` 主体行时返回与「不存在」相同的 404
    （store 侧 `delete_grant` 的 WHERE 加 `principal_type<>'user'`），不给可区分的提示——组管理员本来
    就看不到这些行，引导文案只会确认它们存在。
  - `GET /groups/{gid}/shared-notebooks` 已经只查群组主体（`group_store.py` `list_group_shared_notebooks`
    的 `principal_clause`），不受影响。
  - 前端群组授权区只渲染群组/everyone 行。矩阵测试加「组管理员删除编辑者边 → 404、边仍在」。
- 发放即生效，不设「对方接受」步骤（决策 D3）；对方笔记本列表里出现该库（§4.5），铃铛给一条瞬时
  事件（`pending_bus.emit`，进程内、无连接时进 TTL 缓冲）。
- 撤销：删边。若该用户因此**完全失去读权**（无成员行、无其他边），沿用 `remove_member` 的口径：
  清理理解覆盖层与观测（`_clear_member_profiles`），**不删 Memory**；若仍有读权（例如还是只读成员或
  群组成员），只降级，不清理。
- 退出协作：对外合同与只读成员的「退出共享」相同（`exit-disclosure` 告知 Memory 条数 → 带确认数
  调用 → 删），状态码与 `leave_notebook_route`（`DELETE /notebooks/{id}/membership`，路由声明在 `notebook_routes.py:560–561`）逐一对应；仍有其他读权则只删边、
  不删 Memory。**但不能直接复用现有实现**：`MemoryService.leave_notebook` 在**成员行**锁下领取要删的
  Memory，而编辑者的读权凭证是**授权边行**。需要一份锁授权边行（`SELECT ... FROM notebook_grants WHERE
  notebook_id=%s AND principal_type='user' AND principal_id=%s FOR UPDATE`）的版本：在同一把锁下判定
  「删掉这条边后是否仍有读权」、领取 Memory、删边，保证与并发的 owner 撤销/重新授予互斥。两份实现共享
  领取与分页删除的代码，只在「锁哪一行、删哪一行」上分叉。
- 已存在的只读成员可以被升级为编辑者（两条记录并存，列表按「成员行优先」的展示去重规则不变，
  `can_manage_content` 从授权边算，见 `notebook_catalog.py:574–620` 的交叉态说明）。
- **既是只读成员又是编辑者时的两个「退出」**：两条记录各有自己的出口，互不代删。
  - 「退出协作」（`DELETE .../collaborators/{me}`）只删 `user` 授权边。成员行还在，所以「仍有读权」恒成立，
    走「只删边、不删 Memory」那一支——结果是降回只读成员。
  - 「退出共享」（既有 `DELETE .../membership`）只删成员行。授权边还在，`leave_notebook` 既有的「调用者仍经
    所有权或授权边可读 → 只结束成员关系、不删 Memory」分支生效——结果是仍为编辑者。
  - 想彻底离开要两个都退；后退的那一个才会触发 Memory 披露与删除。
  - 只持有 `(user, viewer)` 点名边、没有成员行的用户（例如同步遗留或角色被调整过的人）：他的「退出」入口
    指向 `DELETE /collaborators/{me}`（文案「退出共享」），走同一套锁授权边行的退出实现与 Memory 披露——
    既有的 `DELETE .../membership` 只认成员行，对他是空操作。工作区在交叉态下两个按钮都显示，文案
    分别是「退出协作（保留只读）」与「退出共享」，后者在仍是编辑者时附一句「你仍是这本笔记本的编辑者」。

### 4.5 列表与详情投影

- `granted_notebook_rows`（PG `query_store.py:818`，SQLite 镜像）今天以 `group_members gm` 为驱动表
  JOIN 授权边，结构上取不到 `user` 主体边。扩法是追加一条 `UNION ALL` 分支：
  `FROM notebook_grants ng JOIN notebooks nb ... WHERE ng.principal_type='user' AND ng.principal_id=用户`，
  `_group_*` 列为 NULL；`fold_granted_notebook_groups` 跳过无群组的行。这条查询在**笔记本列表热路径**上，
  所以两个后端都要补 EXPLAIN 钉：PG 侧新分支必须走 `idx_notebook_grants_principal`
  `(principal_type, principal_id)` 索引扫描、原分支计划不变（钉法同 `test_mount_sql_viewer_pg.py`）；
  SQLite 侧钉 `EXPLAIN QUERY PLAN` 同一索引（它的原分支靠 `CROSS JOIN` 提示定驱动表，UNION 不得破坏
  这条提示）。排序键在 UNION 之后整体应用，保持现有 `ORDER BY` 语义。这条投影是列表展示，不是授权
  判定——授权仍只在 `access_sql`。
- `access` 仍是 `"owner" | "reader"`（不加枚举，已定裁决 7）。
- `can_manage_content`：语义精确化为「有编辑权」，由新判据 `notebook_grant_confers_edit`
  （`role IN ('admin','editor')`）计算。
- 新增 `can_manage_notebook: bool`：有管理权（owner 或 admin 边）。前端改名、授权边管理控件改看它；
  `workspaceCapabilities` 增一个参数，内容写入口仍看 `canManageContent`。
- 新增 `collaborator_role: "" | "editor"`：供徽章文案（「可编辑」 vs 「只读」 vs 「来自群组《X》」）。
- `is_shared` 并入「存在 `user` 主体边」，owner 卡片照常显示「已分享」徽标。
- 新增 `collab_pulse: bool | None`——这位查看者打开这本库时要不要启动脉搏（§6.6）。**三态**：`true` / `false`
  只由下面那一条路由给出，`None`（缺省）表示「本次响应没有计算」。原因：另有 6 个路由也返回
  `NotebookSummary`——建库 `POST /notebooks`（`notebook_routes.py:102`）、改画像 `PATCH /notebooks/{id}`（`:182`）、
  撤销删除 `POST /notebooks/{id}/cancel-delete`（`:237`）、tier `POST .../tier`（`:263`）、`/shared/{token}/copy`（`:453`）与 `/shared/{token}/join`（`:492`）——而
  前端会用响应**整对象替换**当前笔记本（`page.tsx:3088` `setCurrentNotebook(updated)`、`:4506`）。若字段是
  布尔且缺省为 `false`，改一次名就会把脉搏关掉。前端合并规则：新对象的 `collab_pulse` 为 `null` 时保留旧值。
  组件测试：改名（以及 tier 切换）之后脉搏不停。
  - **落点**：只在 HTTP 路由 `GET /notebooks/{id}`（`notebook_routes.py:107–120`）里，在
    `notebook_catalog_repository().get_notebook(...)` 返回之后单独算一次、填进响应。**不**放进
    `NotebookCatalogService.get_notebook`（`notebook_catalog.py:1034`）：那是详情投影的唯一实现，仓内有
    99 个 `.get_notebook(` 调用点（报告、全局问答、作业线程……），放进去等于给它们全部加一条查询。路由测试
    加查询计数断言：相对改动前恰好 +1 条语句；其他调用点 +0。
  - **判据**：`查看者有编辑权以上`（owner / 编辑者 / 组管理员）→ 当「除查看者外还有人可读」时为真；
    `查看者只有读权` → 当「除查看者外还有人可读」**且**查看者的读权不是只来自公共臂（`tier='base'` 或
    `everyone` 边）时为真。「除查看者外还有人可读」= owner（查看者非 owner 时）、任一成员行、任一 `user`
    边、任一群组边的组内另有成员、`everyone` 边、或本库 `tier='base'`。最后一项不能漏：公共库可能既无成员行
    也无授权边（公共可读来自 `tier` 列本身），漏掉它，公共库的 owner 永远不会启动脉搏，也就看不到编辑者的
    改动。
  - **为什么排除公共臂读者**：对非 owner 查看者，「除我之外还有人可读」按定义恒真（至少有 owner），而公共库
    的读者可能是全站所有人——若他们都每 15s 脉搏一次，负载与库的受众规模成正比。公共库读者的协作价值又
    最低（他们看不到写者在做什么之外的东西）。所以只经公共臂可读的读者**不启动脉搏**，打开时照常整体拉取；
    公共库的 owner 与有写权者照常脉搏。
  - `is_shared` 不能当闸：开了链接但没人加入、共享给只有自己的组都为真。
- owner 的「我分享的」列表（`GET /notebooks/shared-by-me`，`notebook_routes.py:97`）增列编辑者。

### 4.6 与既有共享机制的交互

| 机制 | 交互 | 处置 |
| --- | --- | --- |
| 分享链接 | 互不影响；链接只产生 copy / 只读加入 | 无改动 |
| 群组授权 | 同一人可同时是编辑者与组成员；有效角色取最大 | 谓词自然合并 |
| **借入挂载未共享门** | 第一次授予编辑者 = 本库出现一条 `notebook_grants` 行 → 本库挂载的**借入**参考库（经别人只读共享得来的）对**所有人**失效置灰——门是挂载方级谓词（§2.8），**owner 自己在这本库里的问答也不再检索它们**。这是 owner 侧的可见行为回退 | 处置方式由决策 D13 定。推荐 (a)：与今天开链接/共享给群组的行为一致；授予前的确认弹窗列出将失效的借入参考库名（owner 本来就能看 `GET .../bases`），并明说「包括你自己在这本库里的提问，也将不再检索这些参考库」；撤销最后一个编辑者（且无其他成员/授权边）后自动恢复 |
| 跨环境同步镜像 | 镜像上编辑者与 owner 一样被围栏挡 409 | 无改动；`notebook:collaborate` 围栏为 `False`（目标端自管可见性） |
| 跨环境同步导入 | `notebook_grants` 是种子写入（`migration/sync/manifest.py:403–416`）：`principal_id` 按行内 `principal_type=user/group` 选映射表，映射不到跳过该行并记日志；首次创建后不再更新/删除 | 不处理的话，源端的编辑者边会被**原样**种进镜像。两个问题：镜像上内容写全被围栏挡，`editor` 只剩 `scale_index:write` / `agent_profile:write` 两格可用、徽章误称「可编辑」；更重要的是 **seed-only 不传播撤销**——源端之后撤掉这个人，镜像上的边永远留着，既保持读权、又让镜像的未共享门永久关闭、还让这个账号的合并被 `blocking` 拦住。处置见决策 D16（推荐导入器跳过 `user` 主体边）；万一镜像上已有 `user` 行，按上面的完整性约束，owner 在「点名共享」区看得见、撤得掉 |
| 整本导出/导入 | 导入成独立新库 | 编辑者名单不随导出（与 Memory 不随导出同理） |
| 删库宽限窗口 | 编辑者与只读读者一样失去入口、能看到删除说明 | `require_notebook_read_or_delete_notice` 已覆盖有读权者 |
| 公开报告 / 会话链接 | 以创建者身份实时复核读权 | 编辑者被撤权后，他发布的公开链接随之失效（既有合同） |

### 4.7 Agent / MCP 面（推荐不动，决策 D5）

编辑者在 MCP 面**不获得** manage/delete 档能力：`_writable_notebook` 保持 owner-only。理由与组管理员
那条刻意分歧相同（长期令牌是独立凭据；来源写入影响每个读者的检索结果）。contribute 档本就对读者开放，
编辑者照旧可用。`docs/product-and-api*.md` 的 Agent 合同表补一句「编辑者与组管理员一样，写权只在浏览器
界面生效」。

---

## 5. 并发与冲突

原则：**每个可被两人同时触发的写，都要么由数据库/单飞机制拒绝并告诉对方是谁在做，要么是有意的
last-write-wins 且有历史可回退**。逐项：

| 写 | 现状 | 本设计 |
| --- | --- | --- |
| 同一来源重解析 ×2 | 无单飞，第二个阻塞后整份重做（§2.9） | 单篇 `POST /sources/{id}/parse` 先探测 `source_parse_busy`（MCP 已用的同一探针），忙则 `user_error(409, "这份来源正在解析，请等它完成")`；批量重解析跳过忙碌来源，响应新增 `skipped_busy: [source_id]`。探测与提交之间的窗口保留进程内分块锁兜底（最坏重复工作、不交错写，既有注释的结论）。决策 D8 |
| 重解析 vs 删除 | 已安全（行锁 + 失败记账） | 不改；补一条双写者测试 |
| 图谱构建/重建/维护 ×2 | 三层单飞，409 | 不改机制；409 文案在**发起人不是自己**时带上发起人显示名（`kg_build_jobs.created_by`；维护槽需在 `KgMaintenanceJobs.claim` 记下 actor，进程内字段，无迁移）：「张三发起的知识图谱分析正在运行」 |
| 检索索引重建 ×2 | `try_scale_build_lock` 会话级 advisory lock，`already_building` | 不改 |
| Knowhow 单元格（手动编辑器） | LWW（已登记取舍，`knowhow_routes.py:537–539`：正在编辑的人不该被自己更早的快照拒绝） | 乐观校验，决策 D7，见下方「D7 的三条硬规定」 |
| Knowhow 结构操作、回退 | 结构操作 LWW + 变更历史；回退带 `expected_head_seq` | 不改；历史可回退即兜底 |
| 知识治理审核（合并确认/驳回、冲突裁决） | 队列项 | 实现期逐端点核对「第二个人处理已处理项」返回 404/409 而不是 500（§13），统一文案「这一项已被处理」 |
| 笔记本画像 PATCH | LWW | 编辑者无此权；管理档内维持 LWW |
| 共享理解底座 | 单条标签 PUT | LWW；实现期核对是否已有版本比对（§13） |
| Memory | 按创建者私有 | 无跨人冲突 |
| 配额 | 文档数上限按库 owner 算：`_document_capacity_limit`（`source_routes.py:243–255`）取 `repo.notebook_owner(notebook_id)` 的有效上限，owner 是系统管理员则豁免 | 编辑者上传占 owner 额度，机制不改；超限文案改为「已达到这本笔记本的文档上限」而不是暗示「你的上限」 |

**D7 的覆盖面**：手动保存今天有**两条**落点，都要纳入（`frontend/app/knowhow-panel.tsx` 约 895–912 行的
端点选路）：

- 普通格子：单目标，走单格端点 `PATCH .../cells/{column_id}`，不带 `expected_before`；
- 合并共享格（锚定分组里共享列的一格）：扇写整组，`targets.length > 1` 走批量端点
  `batchPatchKnowhowCells`（`:908`），手动编辑时**不传** `expectedByRowId` 也不传 `anchorGuard`——同样是
  last-write-wins。批量端点本身已支持逐行 `expected_before` 与锚定守卫（规整保存在用）。

**D7 的硬规定**（防止单人自撞 409，兑现已登记取舍 `knowhow_routes.py:537–539` 的本意）：

1. **基线取服务端存储值**：编辑器打开时以表数据里的 `content_md` 为基线；**每次保存成功后**以响应回传的
   存储值替换基线——单格端点 `patch_knowhow_cell` 返回写后重读的 `content_md`；批量端点按行返回结果，
   逐行取存储值。不用本地输入框里的文本——服务端可能做过规范化（Knowhow 单元格 Markdown 规范化），用本地
   文本当基线会让第二次保存必然失配。
2. **合并共享格逐行基线**：手动保存合并共享格时，按 `targets` 逐行下发 `expected_before`（取各行当前基线）
   并带 `anchorGuard`（取打开编辑器时各行的锚定值），与规整保存同一套服务端三重校验（锚定未变 + 组成员
   相等 + 逐行比值）。
3. **比较口径**：`expected_before` 与存储的 `content_md` 做**逐字节相等**比较，在同一写事务里
   （`update_knowhow_cells_guarded_atomic`，`postgres/knowhow_store.py:2064`，既有实现，单格与批量共用）。
   不做空白归一、不做语义比较。
4. **同一浏览器的多个标签页共享单元格值**：任一标签页保存成功后，经
   `BroadcastChannel("knowhow-cell:<table_id>")` 广播 `[(row_id, column_id, stored_content_md), …]`（批量写一次
   广播整组）。其他标签页**不论是否打开了编辑器**，都用这些值更新本地缓存的表数据（`detail.rows` 里对应
   格子），从而也更新了下一次打开编辑器时取的基线；若该格正开着编辑器，只把基线前移、不动草稿。只更新
   编辑器而不更新缓存，未打开编辑器的标签页之后打开时会拿陈旧基线自撞 409。这样同一用户在同一浏览器里
   不会被自己另一个标签页的保存拒绝。`BroadcastChannel` 不是 Secure Context 限定 API，生产的 `http://<IP>`
   访问形态可用（实现期在该形态下实测确认，memory「Secure Context 限定的浏览器 API」）。不同浏览器/设备上的
   同一用户仍可能 409——这时确实存在另一份并发修改，按冲突处理是对的。

409 时在格子旁就地给出「对方的版本 / 我的版本」与「用我的覆盖」「放弃我的」两个按钮。合并共享格的 409
在共享格这一处展示（它在界面上就是一格），「对方的版本」取组内代表行的当前存储值；若 409 的原因是组成员
或锁定值变了（不是内容），不给覆盖按钮，提示「这一组已被重新分组，请刷新后再编辑」。

**区分 409 原因的工作量**：今天批量路径不区分原因——store 的 `update_knowhow_cells_guarded_atomic`
（`postgres/knowhow_store.py:2064`）只返回 `{"conflict": bool}`，路由（`knowhow_routes.py:697–698`）一律给
「内容已被其他人修改」。两种做法择一，计划阶段定：
- **store 给原因码**：两个后端的 guarded 写在 phase 1 校验处分别返回 `content_changed` / `group_changed`
  （成员不等或锚定值变了），路由映射成结构化 detail `{"code": "knowhow_cell_conflict", "reason": …}`。改动面：
  两后端 store 各一处返回值、路由一处、既有调用方（批量规整保存）的 409 处理要兼容新 detail 形状。
- **前端 409 后重新拉取比对**：后端不动；前端 409 后重新取该表，比对组成员与锚定值是否还等于打开编辑器时
  的快照，据此判断原因。改动只在前端，代价是 409 时多一次整表读取。
推荐前者：原因只有服务端在同一事务里知道得准确，前端比对存在「拉取时又变了」的二次竞态。

**「优化整行」的接受（`onAcceptCell`，`knowhow-panel.tsx:820/840/861`）纳入 D7**：它与手动格子编辑共用同一个
保存函数和同一条「不传 expected、last-write-wins」的分支，接受一条模型建议同样是在覆盖一格内容。纳入后：
基线取打开「优化整行」浮层时各格的存储值，接受时按格带 `expected_before`，409 就地展示同一套双版本 UI；
同浏览器标签页的单元格值广播同样适用。覆盖 = 以 409 时取回
的当前存储值（批量时逐行）为 `expected_before` 重发。

测试：单人在一个标签页连续保存 N 次永不 409（普通格与合并共享格各一组）；同一浏览器两个标签页交替编辑
同一格永不 409，含「标签页 B 在 A 保存之后才首次打开编辑器」；两个用户并发编辑同一格后保存者 409、覆盖后
成功；合并共享格分组变化时 409 且无覆盖按钮。

**审计 actor**：所有编辑者写入沿用 `session_audit_principal`（授权用 `identity_id`，落文本用
`audit_label`），既有列（`uploaded_by`、`kg_build_jobs.created_by`、Knowhow 历史 `actor`）自然记到编辑者。
来源重解析/删除、图谱维护、知识治理动作没有落库操作者，是否补「最近改动」日志见决策 D11；推荐方案见
§6.5。

**给别人看的操作者名字一律用显示名**：409 文案、脉搏 `changes[].actor_name`、在场名单、最近改动，都取
`users.display_name`，为空才回落到 `username`（与群组成员列表、`/users/resolve` 的 `UserRef` 同一披露面）。
**不用** `audit_label`——`session_audit_principal` 的回落链以 `username` 打头（`core/audit_actor.py:24`），
统一登录下 `username` 可能是工号。`audit_label` 只用于落库的不可变审计文本，与今天一致。

---

## 6. 在场与版本信号（协作脉搏）

### 6.1 方案比较

| | A. 轮询脉搏端点（推荐） | B. 挂到待办推送流 | C. 数据库指纹轮询 |
| --- | --- | --- | --- |
| 传输 | 新 `POST /notebooks/{id}/pulse`，15s 一次 | 复用 `/me/pending-actions/stream`，加 `notebook_pulse` 帧 | 同 A，但版本由 SQL 现算 |
| 延迟 | ≤15s | ~即时 | ≤15s |
| 新增连接 | 无（短请求） | 无 | 无 |
| 每次成本 | 会话鉴权 + 读权点查（均既有，各借一次池连接）+ 内存读 | 每帧一次会话复核（`AuthenticationStreamGuard`）；需服务端维护「谁在看哪本库」订阅表 | 鉴权 + 每次多条聚合查询（`MAX(sources.updated_at)`、`kg_mutation_seq`、Knowhow `mutation_seq` 求和） |
| 覆盖离线 CLI 写 | 否 | 否 | 是 |
| 进程重启 | epoch 变化 → 客户端整体刷新 | 同 A | 无影响 |
| 复杂度 | 低 | 中（流协议、订阅生命周期、标签页可见性与流的耦合） | 低，但大库聚合查询要 EXPLAIN 定索引 |

推荐 A：生产约定单进程（§2.9），进程内状态是既有先例（`pending_bus`、`kg_building`、维护槽）；脉搏
本身不往任何写路径加数据库读写（「最近改动」持久日志是 D11 的独立选项，成本单列在 §6.5）；15s 的延迟
对「看到同事加了来源」足够。B 的延迟优势只在两人同时盯着同一块
时有感，代价是把笔记本级订阅塞进一条按用户的流，留作 A 上线后的可选升级（决策 D9）。C 唯一的优势
是覆盖离线 CLI 写入，而离线批处理与协作编辑几乎不会同时落在同一本库上（批处理作用域见
`services/batch_ingest.py`，建检索索引的离线路径要求停服），收益不抵每 15s 一组聚合查询；漏掉的
CLI 写由「打开即整体拉取 + epoch 变化 reset」兜底。

### 6.2 服务端：`CollabPulse`（进程内）

- 状态（全部进程内、带锁、不落库）：
  - `epoch`：进程启动时生成的随机串；
  - **进程级全局单调 `rev`**（一个计数器，所有笔记本共用），每次 touch 递增；
  - 每本笔记本一个条目：`created_rev`（**建条目那次 touch 递增之前**的全局 rev，即「此后 rev 更大的改动
    都在本条目里」）、`dropped_rev`（本条目环形缓冲挤掉的最后一条的 rev，未挤掉为 0）、最近 64 条变更的
    环形缓冲 `{rev, kind, actor_id, actor_name, at}`、在场表 `{user_id: last_seen}`；
  - **全局回收水位 `evicted_rev`**：每回收一个条目，取 `max(evicted_rev, 该条目缓冲里最大的 rev)`。
  - 条目按 LRU 上限 2000 本回收（约 13 万条变更记录，量级十余 MB），无时间回收。**touch 无条件记录**
    （O(1) 的字典操作，不查库、不判在场），所以没人在线时发生的改动，之后打开的人仍能在「本次服务启动
    以来」的范围内看到。
- `kind` 枚举：`sources`、`kg`、`knowhow`、`knowledge`、`catalog`、`notebook`（画像/授权/编辑者名单）、
  `understanding`、`scale_index`，外加 **`none`**——「不触发刷新」，给那些挂在能力守卫下、但不改变其他
  人看到的内容的非安全请求：`notebook:export`（导出只写请求者自己的导出作业）、`reports:write`（报告按
  创建者私有）以及将来同类端点。新增能力名时必须显式选一个 kind（含 `none`），不许缺省。
- **触点 1 — 同步写端点**：能力表旁新增第三张同键表 `_CAPABILITY_PULSE_KIND`（键集合与能力表相等，
  守卫测试钉死，沿用「响亮失败」口径）。能力工厂产出的守卫在通过后把 `(notebook_id, capability)`
  记到 `request.state`；一个纯 ASGI 中间件在响应开始、状态码 < 400、方法非安全、kind ≠ `none` 时调用
  `pulse.touch(notebook_id, kind, actor)`。体内自查的写（来源 parse/delete、Knowhow 转移、理解底座、
  插件）在自查通过后调用同一个 `mark_pulse(request, notebook_id, capability)`。
  - **路由级覆盖**：能力名只是粗分类，同一能力下有不改内容的非安全方法——例如 `knowhow:write` 下的
    `POST .../knowhow/import/preview`（`knowhow_routes.py:91–95`，只解析不落库）、`POST .../{table_id}/append`
    （`:788–791`，`Union[KnowhowAppendPreview, KnowhowAppendResult]`，预览形态不落库），以及只产出模型建议、
    由用户接受后经单元格 PATCH 落库的 `.../cells/{column_id}/optimize`（`:845`）、`.../rows/{row_id}/complete`
    （`:976`）、`.../cells/{column_id}/reformat`（`:1123`）、`.../reformat/stream`（`:1181`），连同下面「流式端点」
    里的两条流。路由可以声明
    `pulse_kind="none"` 覆盖能力表的默认 kind（实现为路由依赖上的一个标注，中间件读它）；对「同一端点有时
    写有时不写」的联合响应，路由体在确实落库的分支里调 `mark_pulse`，端点级声明 `none`。
  - **守卫测试**：枚举基准是「应用里**所有带 `notebook_id` 路径参数的非安全方法路由**」，而不是「挂了能力守卫
    依赖的路由」——体内自查的路由（例如 `DELETE .../understanding/experiences`，`agent_profile_routes.py:490–498`，
    依赖只挂 `require_notebook_read`、体内才判 `agent_profile:write`）按依赖元数据枚举是看不到的。不带
    `notebook_id` 路径参数、由体内反查笔记本的写路由（如 `DELETE /sources/{source_id}`、`/agent/knowhow/rows/...`）
    另列一张显式清单一并归类。要求每一条**显式**归类——
    要么沿用能力表 kind 并登记在一张路由清单里，要么声明 `none`，要么声明「体内按分支 mark」。新增路由
    不归类即红。被覆盖为 `none` 的请求同样不进 D11 的持久日志。
  - **中间件与事件循环**：`touch` 是内存操作，可在事件循环上直接做；D11(b) 的 INSERT 是数据库写，在响应开始
    之后以 **fire-and-forget** 方式丢进线程池（提交给线程池执行器、不 `await` 其结果，失败在任务里记日志），
    既不在事件循环上同步执行，也不让中间件等它——响应时延与这次写入完全无关。
  - **流式端点**：NDJSON 流式响应在**响应开始**（发出状态码）时就满足「状态码 < 400」，此时流里的工作还没
    结束。挂在能力守卫下的流式端点今天有 Knowhow 的 `.../cells/{column_id}/optimize/stream`
    （`knowhow_routes.py:935–939`）与 `.../rows/{row_id}/complete/stream`（`:1075–1079`），它们经
    `task_event_stream`（`task_stream.py:103`，docstring：「model-backed previews」）产出的是**建议**，用户接受
    后才经单元格 PATCH 落库——按上面的路由级覆盖归为 `none`，由 PATCH 那一次 touch。今后若出现「流式且真的
    落库」的端点，规则是：中间件在响应开始时 touch（可能早于落定，只会让对方提前刷新一次、看到进行中状态），
    落库由后台作业完成的，作业落定时触点 2 再 touch 一次；守卫测试的路由清单里显式登记这类端点。
  - **与 `lru_cache` 身份断言的关系**：今天围栏为 False 的能力，工厂直接返回**档位守卫本身**——
    `notebook:configure` / `notebook:mount` / `notebook:delete` / `notebook:export` 返回同一个
    `require_notebook_write` 对象，`test_notebook_capability_guard.py:190` 断言
    `require_notebook_capability(capability) is expected[level]`。档位守卫本身不知道自己代表哪个能力名，
    记不了 `request.state`。所以**每个能力名都要有自己的包装**（像今天围栏为 True 的那几格一样），
    `:190` 的断言改为「同一能力名恒返回同一对象、且不是裸档位守卫」（即今天 `:244–248` 对围栏能力
    的断言形状），FastAPI 按 callable 身份的每请求去重仍然成立（同一能力名同一对象）。这是测试合同的
    显式变更，要在 PR 里登记。
- **触点 2 — 后台作业落定**：来源处理到达终态、图谱构建/维护作业结束、检索索引重建结束。这些位置今天
  已经在调 `pending_bus` 的发布函数（`background_jobs.py`、`knowledge_lifecycle.py`、
  `scale_artifact_runtime.py`），在旁边加一行 `pulse.touch`（同为 O(1) 内存操作）。
- **触点 3 — MCP 写工具**：`add_source` / `reparse_source` / `delete_source` / `build` 成功后 touch，
  actor 记为令牌属主 + profile 名。
- 不覆盖：离线 CLI（批量导入、跨环境同步导入器）直接写库的改动。客户端靠「打开笔记本时整体拉取」与
  epoch 变化兜底；文档登记为已知限制。

### 6.3 协议

`POST /notebooks/{id}/pulse`，守卫 `require_notebook_read`（读者也能收更新、也在场）。

请求 `{since: "<epoch>:<rev>" | "", visible: bool}`；响应：

```json
{
  "cursor": "<epoch>:<rev>",
  "reset": false,
  "changes": [{"kind": "sources", "actor_name": "张三", "at": "..."}],
  "viewers": [{"user_id": "...", "display_name": "张三", "role": "editor"}],
  "other_reader_count": 3,
  "next_interval_s": 15
}
```

- 游标是 `<epoch>:<全局 rev>`。判为 `reset: true`（客户端刷新全部已挂载面板）的情形：
  1. `since` 为空或无法解析；
  2. `epoch` 与当前不同（进程重启，或多进程/多实例下请求落到了另一个进程，见 §6.6）；
  3. `since_rev > 当前全局 rev`（同一进程里不会出现，出现即说明游标来自别处）；
  4. **可能有回收丢失**：`since_rev < evicted_rev`，**且**（本笔记本此刻没有条目，**或**条目的
     `created_rev > since_rev`）。前半句说「游标之后发生过回收」，后半句说「本笔记本的条目不是从游标之前
     一直连续存在的」——两者同时成立时，游标之后、条目重建之前的改动可能随旧条目一起丢了；
  5. **缓冲挤掉**：条目存在且 `since_rev < dropped_rev`（游标之后的改动有一部分被挤出 64 条缓冲）。

  其余情形返回本条目里 `rev > since_rev` 的变更（条目不存在则为空）。逐一核对两个典型情形：
  - 标签页隐藏期间该条目被回收、之后再无 touch：回来时条目不存在、`since_rev < evicted_rev` → 规则 4 reset，
    不会报「无变化」；
  - 冷库在游标之后第一次被改动：建条目时 `created_rev`（递增前的值）≥ `since_rev`，若这段时间没有发生过
    回收（`since_rev ≥ evicted_rev`）就不 reset，正常返回这条改动——只有真的可能丢了才整体刷新。
  规则 4 是保守的：水位是全局的，任一笔记本的回收都会让持旧游标、且条目晚于游标建立的客户端多 reset
  一次；这是用一次多余刷新换「永不漏报」。
- `changes` 按 `kind` 去重合并（客户端只关心「哪几块要刷」），附最近一个 actor 供提示文案。
- 在场：请求本身即心跳；`visible=false` 时不记在场（标签页在后台不算「在这里」）。在场 TTL 45s。
- `viewers` 的可见范围见 §6.4。
- 响应不带任何内容数据，只带「哪块变了」；刷新仍走各面板既有的读端点与既有守卫——脉搏不是第二条
  读通道。

### 6.4 在场的披露范围（决策 D10）

推荐：**有写权者（owner / 编辑者 / 组管理员）互相看见名字；协作读者只以人数出现（`other_reader_count`），
读者自己看到的 `viewers` 只含有写权者**。「协作读者」指此刻在发脉搏的只读读者——只经公共臂（`tier='base'` /
`everyone` 边）可读的读者不发脉搏（§4.5），所以不在这个计数里；`other_reader_count` 不是「这本库有多少读者」。理由：只读读者今天彼此不可见（分享链接加入者看不到别的
加入者；群组成员名单只在群组页按组角色可见），把「谁也在读这本库」暴露给所有读者是新的披露面；而
写者互见正是协作的需要。管理员用户页的「在线」是另一特性，不受影响。

**一处刻意的不对称**：组管理员调 `GET /collaborators` 得 404（编辑者名单只归 owner，D17 只放开给编辑者），
但他打开这本库时，在场名单里能看到此刻在线的编辑者。两者答的问题不同：名单是「谁被授予了写权」——授权
管理信息，组管理员管不了它，也就不该拿到它；在场是「此刻谁在这里改东西」——协作信息，所有有写权者都需要。
在场只暴露当下在线的人、不含授予人与时间，泄露面小于名单。若用户认为这不可接受，可在 D10 里把组管理员
排除出写者互见。

### 6.5 最近改动（决策 D11）

「最近改动」面板在 P2 交付，数据来源有三个选项，成本各自如实记账：

- **(a) 只用进程内环形缓冲**：零数据库读写。展示「本次服务启动以来、最近 64 条」；重启或 LRU 回收即丢。
- **(b) 只持久化非 owner 的写（推荐）**：新表 `notebook_activity(id, notebook_id, kind, capability,
  actor_id, actor_name, target, created_at)`。只在**操作者 ≠ owner** 时写一行——这正是 owner 最需要知道
  的那部分（编辑者/组管理员动了什么）。判断「操作者是不是 owner」**不新增读**，也**不改**既有端口契约：
  - `user_can_admin_notebook -> bool`（端口 `repositories/ports.py:723`、服务 `notebook_sharing.py:500`，
    消费者含 `deps.py` 的 `require_notebook_admin` 与 `notebook_capability_allowed`、所有权清单）和
    `notebook_capability_allowed -> bool`（约 15 个体内调用点，§2.2）**保持原样**；
  - 新增兄弟方法 `owner_if_can_edit(notebook_id, user_id) -> str | None` / `owner_if_can_admin(...)`：执行与
    对应谓词**同一条** SQL，只把 `SELECT 1` 换成 `SELECT nb.created_by`（同一行、同一计划，§13 待核对第 5 条
    取 EXPLAIN 证据），有权返回 owner id、无权返回 `None`；
  - 只有一份 allowlist 里的调用方用兄弟方法：能力工厂产出的 edit / admin 档守卫（拿到 owner id 后记进
    `request.state` 再转成布尔），以及 `mark_pulse`。守卫测试钉住 allowlist，其余调用方继续用布尔版。
  成本：owner 自己的写 **+0**；非 owner 的写 **+1 条独立短事务 INSERT**（响应开始之后 fire-and-forget 丢进线程池、中间件不等待，失败只
  记日志、不影响请求）。保留 90 天，复用既有保留期清扫的挂点。owner 与有写权者在协作面板看最近 50 条持久
  记录，叠加环形缓冲里 owner 自己的近期改动。

  其他会新增读的地方一并记账（都不在 owner 单人使用的热路径上）：
  - 作业落定触点（§6.2 触点 2）要给 `changes[].actor_name` 取发起人显示名：每个作业终态 **+1 次按主键读
    `users`**（每个作业一次，不是每个请求）；
  - 忙碌 409 带发起人显示名（§5）：只在真的返回 409 时 **+1 读**；
  - D14 的收窄：非 owner 删除来源、`move` Knowhow 表、删除整张 Knowhow 表、清理 Knowhow 变更历史时，要区分
    「组管理员（不受限）」与「编辑者（受限）」，各 **+1 次管理权探测**（删除来源另读 `sources.uploaded_by`，
    与删除前既有的来源行读取合并）；owner 的这些动作 +0（守卫已知调用者是 owner）。
- **(c) 每次写都持久化**：要么每次写 +1 写（单人笔记本也付），要么先 +1 读判「这本库是否有他人可写」
  再决定写不写——两种都给所有笔记本的写路径加成本，不推荐。

(b) 不是合规审计：离线 CLI 写、事务失败、owner 自己的改动重启后都可能缺失，文档如实写明。
`actor_id` 在 `repositories/user_references.py` 登记为 `audit_kept`（决策 D15）。

### 6.6 成本与上限

- **启动闸**：前端只在 `GET /notebooks/{id}` 返回 `collab_pulse = true`（§4.5）时启动脉搏。单人笔记本零
  脉搏请求；`collab_pulse` 只在这一条 HTTP 路由里多一条语句，不进列表、不进 `get_notebook` 的 99 个内部
  调用点。只经公共臂（`tier='base'` / `everyone` 边）可读的读者不启动脉搏，所以公共库的读者规模不传导为
  脉搏负载。owner 授予第一个编辑者或有人加入后，前端在下一次详情刷新时启动脉搏。
- 每个可见标签页按服务端给的 `next_interval_s`（默认 15s）一次；隐藏标签页停止（`visibilitychange`），
  回到前台立即补一次。
- **连接池**：每次脉搏顺序借两次池连接——会话鉴权（`get_current_user` → `resolve_session`）与读权点查
  （`require_notebook_read`），各毫秒级、不并发持有。PG 池默认上限 10（`POSTGRES_POOL_MAX_SIZE`，
  `core/config.py:1923`）。几百人同时打开同一本群组库（群组设计决策 11 的规模上限）→ 约 20 次/秒、
  约 40 次借出/秒。P2 交付前用压测脚本跑两个场景，量池等待 p99 与其他接口的延迟变化，结果写进 PR：
  (1) 300 个在场标签页打开同一本群组库；(2) 一本公共库（`tier='base'`）与一本 `everyone` 授权库各被 300 个
  只读读者打开——断言这些读者零脉搏请求，只有 owner 与有写权者在脉搏；**降级策略**：服务端以近 10s 的池等待时间为信号，超过阈值时把 `next_interval_s` 逐级放大到
  30s / 60s，恢复后回落——在场 TTL 随间隔同步放大（取 3 倍间隔），避免放慢后在场名单闪烁。
- **刷新风暴防护**：同一 `kind` 的刷新在客户端 2s 去抖；作业落定类 touch 在服务端对同一笔记本同一 kind
  做**后沿合并**：第一次 touch 只登记「待递增」并起 1s 定时，窗口内的后续 touch 只更新 actor 与时间，
  1s 到点才分配新 rev 写入缓冲。**不用前沿合并**（第一次就递增、窗口内后续丢弃）——那样窗口内最后一次
  改动不会产生比客户端游标更大的 rev，客户端在第一次之后拉到的是中间态、此后再无信号。等价的另一种写法
  是：窗口内的后续 touch 把已有记录移到缓冲尾部并赋予新的 rev。两者择一，测试钉住「窗口内最后一次
  改动之后，持旧游标的客户端一定能看到一条 rev 更大的变更」。客户端对 `reset` 做退避：连续 reset 时第 2 次起不再自动整体刷新，改为
  面板顶部内联提示「有新的改动 · 刷新」，直到用户点击或出现一次非 reset 响应。
- **多进程 / 多实例下的失效模式**：违反单进程约定时（§2.9 的启动校验挡不住命令行 `--workers N` 与多实例），
  相邻两次脉搏大概率落到 epoch 不同的进程，每次都是 reset，在场名单也只含落在同一进程的人。客户端识别
  这一形态——在两个 epoch 之间来回切换（同一标签页内见过 ≥ 2 个不同 epoch 且出现回切）——就停止脉搏并
  在协作条上显示「协作同步暂不可用」，同时上报一条前端事件日志，不做刷新风暴。P2 **不**补 `--workers`
  启动校验：应用进程内无法可靠得知 uvicorn 的命令行 worker 数，更看不到负载均衡后面的其他实例；单进程
  仍由部署文档与 `scripts/prod.sh` 约定，P2 只保证违反约定时降级而不是放大负载。
- **撤权后的脉搏**：脉搏返回 404 时，客户端立即停止脉搏，并调用既有的访问复核
  `refreshAfterAccessChange`（`use-notebook-collection.ts`，`visibilitychange` 复核的同一入口），走与其他
  404 相同的「这本库已不可访问」退出路径；不重试脉搏。
- 性能回归防线：§10 的查询计数断言（脉搏端点除鉴权与读权点查外零 SQL）与「单人笔记本零请求」的
  前端测试。

---

## 7. 迁移与兼容

- **数据库**：P1 无表结构变更（`notebook_grants.role` 无 CHECK，新值 `editor` 直接可写；`principal_type
  ='user'` 早在四值白名单里）。P2 若决策 D11 选 (b)，新增 `notebook_activity`（PG 新迁移号 + SQLite
  镜像版本；加迁移前先看 `origin/master` 最新号，memory「并行分支迁移号会撞」）。
- **账号合并**：`repositories/user_references.py:97` 把 `notebook_grants.principal_id`（限
  `principal_type='user'`）登记为 `blocking`——被合并（删除）的账号身上只要有一条 `user` 边，合并就被拒，
  管理员看到「笔记本共享与成员」类计数。编辑者边因此自动落在既有合并闸里；是否改为迁移见决策 D18。
  新增的 `notebook_activity.actor_id` 必须在同一文件登记（决策 D15，推荐 `audit_kept`，与
  `notebooks.delete_requested_by` 同类）。
- **API**：`NotebookSummary` 新增字段向后兼容；`can_manage_content` 的语义从「管理权」精确化为「编辑权」，
  对今天唯一的非 owner 持有者（组管理员）取值不变。
- **旧前端标签页**：发布后，用户已打开的标签页仍在跑旧构建，直到刷新。旧构建把 `can_manage_content`
  当作「改名/授权边」的判据（§2.4），所以旧标签页里的编辑者会看到改名与群组授权入口，点了得到 404/
  「已不可访问」类提示（后端始终是权威，不会写成功）。缓解：发布说明标为用户可见变更；系统更新通知
  （memory「系统更新通知」）会提示刷新。不做旧构建兼容层。
- **能力表**：值域新增 `edit`，`test_notebook_capability_guard.py` 的值域断言、身份断言（§6.2）与
  `architecture.md` Sharing 一行、`docs/product-and-api*.md` 角色表同步改（中英成对）。
- **回滚语义**（P1 发布后又回滚到旧版本）：
  - 遗留的 `(user, editor)` 边：旧读权谓词不看 role → 仍有读权；旧管理权谓词 `role='admin'` 精确匹配
    → 无写权。即自动降级为只读，**不会提权**（测试钉住旧谓词对 editor 行的判定）。
  - 但旧列表投影只认群组主体（§2.4），这些人**在列表里看不到**那本库，只能经已有链接打开；旧
    `GET /grants` 会把 `user` 行如实返回给 owner 与组管理员，旧前端对它没有专门渲染。
  - 未共享门：只要 `user` 边还在，挂载方仍算「已共享」，借入参考库继续失效。
  - 因此回滚手册给两条路由让运维二选一：保留边（编辑者降为不可见的只读读者，借入挂载保持失效），或
    执行一次性清理 `DELETE FROM notebook_grants WHERE principal_type='user' AND role='editor'`（编辑者
    失去访问，借入挂载在无其他共享时自动恢复，被撤者 Memory 按 §4.4 保留不可见）。推荐清理——回滚
    意味着该功能下线，留下不可见的读权与旧前端看不懂的行都不干净。
  - P2 是纯增量：`notebook_activity` 表留在库里无消费者，回滚无数据后果。
- 不设功能开关（实验开关已登记为技术债，`fangan_todo.md`「实验开关评估出口」）。

---

## 8. 安全

1. **越权写**：编辑权谓词是管理权谓词的超集，能力表是唯一入口；新增「能力 × 角色」全矩阵测试
   （6 个角色形态 × 15 个能力 × 安全/非安全方法），对每个写端点断言 404 / 409 / 2xx；外加三条表外格：
   组管理员删除编辑者边 → 404 且边仍在（§4.4）；编辑者删除他人上传的来源 → 404、`move` Knowhow 表 → 404
   （D14）；组管理员 × 代码附件会话写 404 → 2xx 是登记的预期变化（D6）。漏挂守卫的端点仍默认
   owner-only（只读共享设计 §3.1「默认最严」原则未变）。
2. **撤销后的在途写入**：沿用 `deps.py` 能力表上方的已定裁决（codex #519 R6）——内容写不做事务内复检，
   窗口内落库的一行仍在 owner 掌控之内；**授予他人访问权的写**（编辑者名单 PUT）必须事务内复检。撤销
   **不中止**该编辑者已发起的后台作业（图谱构建等），owner 可在作业结束后照常处置（决策 D12）。
3. **分享链接与匿名面不可写**：链接是 bearer 凭证，永远不能携带写权——一条被转发的链接不能变成写入口。
   编辑权只能由 owner 点名授予已登录账号。公开报告 / 公开会话 / 回答链接是只读投影，无变化。
   `auth_optional` 的种子管理员回退（`deps.py:192–215`）不因本设计扩大。
4. **重新共享**：编辑者无 `notebook:grant` / `notebook:configure` / `notebook:mount`，不能把库再给别人；
   借入挂载未共享门在授予编辑者时自动收紧（§4.6）。
5. **存在性不泄露**：所有新端点对无权者 404；`/users/resolve` 的可探测性是既有已登记取舍，不扩大。
6. **脉搏不是读通道**：只回 `kind` 与操作者显示名，不回任何内容；刷新走各面板既有读端点与守卫。
   `changes[].actor_name`（显示名，§5）与在场名单同一披露规则（§6.4）：按推荐方案，写者名对所有读者
   可见，故 actor 随之可见；若决策 D10 选「仅 owner 看在场」或「不做在场」，非 owner 收到的 actor 一律置空。
7. **Agent 令牌**：不改，§4.7。

---

## 9. 前端交互

- **分享弹窗**（owner）：在「分享链接」与「共享给群组」之间新增「点名共享」一节——用户名输入 → 解析出
  显示名 → 「添加为编辑者」。添加前若存在将失效的借入参考库（D13 选 (a) 时；判据是挂载投影新增的 `closes_when_shared`
  字段，§13），按钮下方列出它们，并写明「添加后，这些参考库对所有人失效，包括你自己在这本库里的提问」，
  要求二次确认（D13 选 (c) 时改为按钮禁用并说明需先移除这些参考库）。名单列出 `GET /collaborators` 返回的
  **全部**点名授权，每行标角色（「可编辑」/「只读」），编辑者以外的行（例如历史遗留或同步来的只读点名
  授权）同样可移除。群组授权区只渲染群组/everyone 行。名单每行「移除」按钮；结果（「已添加」「已移除」「没有找到该用户名对应的用户」）落在按钮本身或紧邻处，
  自带计时还原（`AGENTS.md`「Interactive feedback」；不用页面顶部横幅）。请求进行中按钮禁用防重复提交。
- **非 owner 不渲染「点名共享」这一节**：组管理员打开分享弹窗仍只看到群组授权区（今天跳过 `GET .../share` 的同一
  分支，`page.tsx` `openShareModal`）。
- **编辑者的工作区**：所有内容写入口按 `workspaceCapabilities(...).canWriteNotebook` 显示（与组管理员
  同一判据）；改名、授权边、挂载配置、链接分享、导出、删除不渲染。徽章「可编辑 · 来自 张三」。
  「退出协作」放在与只读成员「退出共享」相同的位置，走同一套 Memory 披露确认。来源列表里，编辑者对
  他人上传的来源不渲染删除按钮（D14；判据 = `can_manage_content && !can_manage_notebook` 且来源行的
  上传人不是自己——来源列表投影今天不带上传人，P1 新增该字段，§4.1）。
- **前端能力判据逐处处置**（§2.4 列出的全部消费点；新增参数 `canManageNotebook`）：

  | 消费点 | 今天的判据 | 处置 |
  | --- | --- | --- |
  | `workspaceCapabilities` 的 `canWriteNotebook` / `canGovernKnowledge` / `canRebuildIndexes` | `canWrite` | 不变（编辑权） |
  | `workspaceCapabilities` 的 `canGrantAccess` | `canWrite` | 改看 `can_manage_notebook`（`notebook:grant` 是管理档） |
  | `workspaceCapabilities` 的 `canManageNotebook` | `canWrite && !mirrored` | 改看 `can_manage_notebook && !mirrored` |
  | `notebook-reader-actions.tsx:112` 改名控件 | `can_manage_content` | 改看 `can_manage_notebook` |
  | `groups-page.tsx:418` 「共享给本组」候选库 | `access !== "reader" \|\| can_manage_content` | 改看 `access !== "reader" \|\| can_manage_notebook`（否则编辑者会看到一个提交必 404 的候选） |
  | `use-notebook-collection.ts:304–318` `rowCanManageContent` 等 | 同上 | 不变（内容管理权 = 编辑权） |
  | `use-notebook-collection.ts:484` `editorVisibleIds` 与编辑弹窗入口 `rowCanRename`（`:352–362`） | 弹窗同时承载画像/tier 与索引管线切换 | 拆入口：弹窗对「编辑权或管理权」可打开；画像/tier 字段只在 `canManageNotebook` 时可编辑、否则只读展示；索引管线切换（`:858–868`，`kg:write`）在 `canManageContent` 时可用。`editorVisibleIds` 随入口改为「编辑权或管理权」。不把管线切换搬出弹窗——它的二次确认与续跑检查点都依附弹窗，搬家的改动面大于收益 |
  | `transfer-model.ts:64` `destinationNotebooks` | `allowManaged && can_manage_content` | 不变：Knowhow 转移目标要的是目标库 `knowhow:write`（编辑权）；Memory 转移目标仍恒 owner |
- **忙碌冲突**：重解析 409 与图谱忙 409 的文案出现在被点的按钮旁（「张三发起的知识图谱分析正在运行」），
  不是横幅。
- **Knowhow 冲突**：§5 的就地双版本对比，在单元格旁；两个按钮都有按下反馈与结果态。
- **协作条**（P2）：工作区顶栏右侧，名字缩写头像（有写权者）+「另有 N 位读者」；检测到多进程形态时
  显示「协作同步暂不可用」（§6.6）。别人改动到达时：
  未在编辑中的面板静默刷新，并在面板标题旁短暂显示「张三刚更新了来源」（3s 自动消失）；正在编辑中的
  面板（Knowhow 单元格编辑器打开、来源详情窗口有未保存输入）不刷新，改为在面板顶部内联提示「有新的
  改动 · 刷新」，点「刷新」才拉取。
- 所有新增中文文案过 `scripts/check_ui_vocabulary.py`（不出现「授权边」「主体」等内部词）；新增用语
  登记到 `docs/ui-vocabulary.md`（「编辑者」「可编辑」「退出协作」）。

---

## 10. 测试与验收

**后端（每期独立）**

- `test_access_sql_contract.py`：编辑权谓词双后端对等；包含链 写 ⊆ 管理 ⊆ 编辑 ⊆ 读 的性质测试
  （随机生成 owner / 成员 / 四类主体 × 三角色的边集，逐对比较四条谓词）；`everyone`+`editor` 不给编辑权。
- `test_notebook_capability_guard.py`：值域 `{owner, admin, edit}`；新三张表键集合相等；工厂身份断言。
- 能力 × 角色矩阵测试（§8.1），PG 泳道 + SQLite 各跑一遍；组管理员一列与改动前逐格相等（回归防线）。
- 旧谓词对 `editor` 行降级为只读的回滚测试（§7）。
- 编辑者名单端点：owner-only 404、幂等、自我退出 + Memory 披露、撤权后理解覆盖层清理而 Memory 保留、
  仍有其他读权时只降级、owner 判定与 INSERT 在同一写事务内（钉住调用顺序）。
- 列表投影：`user` 边进列表、`granted_via` 为空、交叉态 `can_manage_content` / `can_manage_notebook`
  取值、`is_shared` 并集。
- 借入挂载：授予第一个编辑者后借入边失效、撤销后恢复；挂载投影的 `closes_when_shared` 只标「此刻仅靠借入臂有效」的边——经 everyone 边可读的他人库为假、本库已关门时全部为假。
- 完整性：`GET /grants` 与 `GET /collaborators` 的并集与 `notebook_grants` 表内行集合相等（含手工插入的
  `(user, viewer)` 行），且每一行都能经对应端点撤销。
- 同步导入（D16）：源端的 `user` 主体边不进镜像，群组边照旧映射种入。
- D14：编辑者删他人上传的来源 404、删本人上传的成功；`move` Knowhow 表 404、`copy` 成功；整表删除 404；
  `history/prune` 404（含 `before_days=1`）；组管理员四者均成功；§4.1 破坏性端点清单与路由枚举的一致性守卫；
  来源列表投影带上传人字段。
- `closes_when_shared`：同 owner 库、公共库、经 `everyone` 边可读的他人库为假；仅经只读共享/受限授权边借入
  的有效边为真；本库已有成员行时全部为假。
- 点名共享语义：PUT 把遗留 `(user, admin)` 降为 editor、把 `(user, viewer)` 升为 editor；`POST .../tier` 切到
  `base` 时存在编辑者 → 409 且 tier 不变；只持 viewer 点名边者经 `DELETE /collaborators/{me}` 退出。
- Knowhow 批量 409 原因码（若计划选 store 给原因码）：两后端在内容失配与分组变化时分别返回对应原因；
  「优化整行」接受带 `expected_before`。
- 交叉态退出：既是只读成员又是编辑者时，「退出协作」只删授权边、不删 Memory；「退出共享」只删成员行、
  不删 Memory；两者都退后才触发披露与删除。
- 并发：重解析单飞 409 / 批量 `skipped_busy`；图谱忙 409 带发起人名；Knowhow 手动编辑器 409 与覆盖重发。
- MCP：编辑者令牌的 `add_source` / `build` 仍 `OwnerOnlyError`（既有 `test_memory_mcp.py` 口径扩一列）。
- Knowhow D7：单人单标签页连续保存 N 次永不 409；同一浏览器两个标签页交替编辑同一格永不 409；两用户
  并发 409 后覆盖成功；基线取 PATCH 响应的存储值（构造一个服务端会规范化的输入验证第二次保存不 409）。
- 授权边端点：组管理员 `GET /grants` 不含 `user` 行、`DELETE` 编辑者边 404 且边仍在；授予在
  `pending_delete` 库上 404；授予与并发删库 → 404 而非 500。
- 列表投影 EXPLAIN 钉（两后端，§4.5）。
- 脉搏（P2）：全局 rev 游标的五种 reset 情形逐一覆盖，外加两条反例——「隐藏期间条目被回收且之后无 touch
  → 必须 reset」与「冷库在游标后首次 touch、期间无回收 → 不 reset 且返回该改动」；
  中间件只在非安全方法 + <400 + kind≠`none` 时 touch；路由级 `none` 覆盖（import/preview、append 预览形态、
  optimize/complete 流）不 touch、不落持久日志；守卫测试枚举全部挂能力守卫的非安全方法路由，每条显式归类；
  D11(b) 的 INSERT 在线程池里 fire-and-forget 执行、响应不等待它（注入一个慢 INSERT 断言响应时延不变）；每个能力名的包装对象身份；读者可见性过滤；
  TTL 随 `next_interval_s` 放大；查询计数断言（鉴权与读权点查之外零 SQL）；D11(b) 下 owner 写零新增
  语句、非 owner 写恰好 +1 INSERT 且授权查询条数不变。

**前端**

- `workspaceCapabilities` 单测：编辑者 / 组管理员 / owner / 只读 / 镜像 各形态的每个能力位。
- 分享弹窗组件测试：非 owner 不渲染「点名共享」一节；名单含只读点名授权且可移除；借入挂载警告；按钮结果态与计时还原。
- 脉搏 hook：`collab_pulse=false` 零请求（含只经公共臂可读的读者）；响应里 `collab_pulse=null` 时保留旧值——改名、tier 切换后脉搏不停；公共库 owner 启动脉搏；后沿合并下窗口内最后一次改动必有更大 rev；隐藏标签页停发；编辑中面板不刷新只提示；2s 去抖；
  连续 reset 退避为内联提示；两个 epoch 来回切换时停止并显示「协作同步暂不可用」；404 停止并走访问复核。
- 前端能力判据表（§9）里每一格的单测，尤其编辑者看不到「共享给本组」候选、编辑弹窗里画像只读而
  管线切换可用。

**真机验收（主会话）**

两个账号两个浏览器：owner 授予编辑者 → 编辑者 15s 内在列表看到库 → 编辑者上传来源，owner 端 15s 内
来源面板出现并提示「张三刚更新了来源」→ 两人同时改同一 Knowhow 单元格，后保存者看到双版本对比 →
编辑者发起图谱分析，owner 点构建得到带名字的 409 → owner 撤销 → 编辑者下一次操作 404、工作区回到
无权状态、Memory 仍在（复权后可见）。只读读者全程看不到任何写入口、在场里只看到写者名与人数。

---

## 11. 分期（每期可独立交付、不留半截）

**P1 编辑者权限层**——**一个 PR 交付**（后端 + 前端 + 文档）。不拆成多个 PR：谓词与能力表单独合入
后没有任何入口能产生 editor 边，是一段无消费者的代码；端点先于前端合入则 owner 只能用 API 授予、
编辑者在界面上看到错误的入口——都是半截特性。PR 内按提交分组（谓词/能力表 → 端点与投影 → 前端 → 文档），
每个提交单独过测试（memory「rebase 合入落每个提交」）。

- `access_sql` 编辑权谓词（双后端）、能力表 `edit` 档与七格翻转、`notebook:collaborate`、同键表守卫。
- 点名共享三端点（GET 列全部 `user` 边、DELETE 不论角色）与完整性守卫、TOCTOU 复检（含 `_lock_notebook_on`
  与尽力而为的存活判定）、群组授权边端点排除 `user` 边、撤销与退出（锁授权边行的退出实现、交叉态两个
  退出）的 Memory 与覆盖层处置。
- 同步导入器跳过 `user` 主体边（D16）。
- 来源列表投影新增上传人字段、挂载投影新增 `closes_when_shared`（§13 转入的两项）。
- 列表/详情投影（`user` 边 UNION ALL 分支 + 两后端 EXPLAIN 钉、`can_manage_notebook`、`collaborator_role`、
  `is_shared`；`collab_pulse` 只在 `GET /notebooks/{id}` 路由里算）。
- 决策 D6、D7（含三条硬规定）、D8、D13、D14、D16、D17、D18 的落地，图谱忙 409 带发起人显示名。
- 前端：分享弹窗「点名共享」一节、§9 能力判据表逐格处置、徽章、退出协作、冲突就地 UI。
- 文档：`docs/product-and-api*.md` 角色表与新端点、`architecture.md` Sharing 一行、`docs/user-manual_zh.md`
  共享一章、`docs/ui-vocabulary.md`；发布说明（用户可见）。
- 交付后状态：多人可以协作编辑，彼此的改动靠手动刷新/重新打开可见——这是一个完整可用的功能。

**P2 协作脉搏**（依赖 P1 只在于「多人可写」让它有意义；对只读共享与群组库同样生效，可独立评审）

- `CollabPulse` 服务（全局 rev 游标、无条件 touch、LRU）、`_CAPABILITY_PULSE_KIND` 表（含 `none`）、
  逐能力包装与测试合同变更、中间件、作业落定与 MCP 触点。
- `POST /notebooks/{id}/pulse`、在场披露规则（D10）、`next_interval_s` 降级与压测报告。
- 「最近改动」面板及 D11 所选的数据来源（推荐 (b) 时含 `notebook_activity` 表与 `user_references` 登记）。
- 前端协作条、按 kind 刷新、编辑中不打断、可见性暂停、reset 退避、多进程形态降级、404 退出。
- 文档与发布说明。同样一个 PR 交付。

**不在本设计内的后续（只有用户另行要求才立项）**：推送流升级（决策 D9 的 B）、群组 editor 角色（D4）、
Agent 面放开（D5）。

---

## 12. 需要用户拍板的决策点

**裁决（用户，2026-10-28）：D1–D18 全部按下表「推荐」列执行，已立项；实施顺序 P1 先、P2 后。**

| # | 问题 | 选项 | 推荐 |
| --- | --- | --- | --- |
| **D1** | 「可编辑」到底是什么 | **A** 允许 owner 给个人发 `(user, admin)` 边：与组管理员同权（含改画像、把库共享给自己管理的组），零谓词改动。**B** 新增 `editor` 角色：只拿内容写，不拿画像/授权/名单，新增一档谓词与能力级别（推翻群组设计决策 4 在「个人」维度的适用，组维度不变）。**C** 不做个人编辑权，只在分享弹窗里引导「建项目组 + 组管理员」现有路径 | **B**。A 让被点名的协作者能经自建项目组把库给任何人看（§4.1）；C 把「邀请一个同事一起改」变成建组、加人、勾选三步，且同样带出 A 的再共享问题 |
| **D2** | 谁能授予编辑者 | 仅 owner / owner + 组管理员 | **仅 owner**（授予写权是对库的处置，与链接分享同理） |
| **D3** | 授予形态 | 点名即生效 / 发邀请需对方接受 / 分享链接上选「可编辑」 | **点名即生效**（与群组加人一致）；链接方式**否决**（bearer 凭证不能带写权，§8.3） |
| **D4** | 群组授权边是否也开放 `editor`（「组成员都可编辑」） | 开 / 不开 | **不开**（维持群组设计决策 4 的两级；谓词天然支持，将来要开只需放开 `GRANT_ROLES`） |
| **D5** | Agent/MCP 面是否给编辑者 manage/delete 档 | 维持 owner-only / 放开 | **维持**（与组管理员同一刻意分歧） |
| **D6** | 推翻已登记取舍：Knowhow 代码附件的浏览器会话写从 owner-only 改为 `knowhow:write` | 推翻（组管理员与编辑者都能写） / 维持 P2-T2「刻意不翻」 | **推翻**。理由：那条裁决的依据是「Agent/MCP 面独立 owner-only」，而附件写今天已对持 contribute 令牌的只读读者开放（§2.12），会话分支的 owner-only 并没有收窄任何人；附件惰性、不执行不入索引。矩阵里「组管理员 × 附件会话写」404→2xx 是登记的预期变化 |
| **D7** | Knowhow 手动编辑（普通格 + 合并共享格两条落点）并发 | 乐观校验 + 就地双版本（含 §5 的硬规定：基线取存储值、合并格逐行基线 + 锚定守卫、逐字节比较、同浏览器标签页共享单元格值） / 维持 LWW（`knowhow_routes.py:537–539` 已登记取舍） | **乐观校验**。硬规定保住那条取舍的本意——同一人在同一浏览器里不被自己的旧快照拒绝 |
| **D8** | 同一来源重复重解析 | 409 就地提示 / 静默幂等接受 | **409 就地提示**（对 owner 单人使用也是改进：双击不再重跑整份） |
| **D9** | 版本信号传输 | A 轮询脉搏（15s） / B 挂到待办推送流 / C 数据库指纹轮询 | **A**，B 作为日后可选升级 |
| **D10** | 在场的披露范围 | 有写权者互见 + 协作读者只计人数 / 有写权者互见但组管理员不见编辑者（与 `/collaborators` 对组管理员 404 对齐）/ 所有协作读者互见 / 仅 owner 看 / 不做在场 | **有写权者互见 + 协作读者计数**（「协作读者」= 正在发脉搏的只读读者，不含只经公共臂可读者；组管理员能在在场里看到编辑者的不对称见 §6.4） |
| **D11** | 「最近改动」的数据来源（§6.5） | (a) 只用进程内环形缓冲，零数据库读写，重启即丢 / (b) 只持久化非 owner 的写：owner 写 +0，非 owner 写 +1 INSERT、不增读 / (c) 每次写都持久化：所有笔记本写路径 +1 写或 +1 读 / 不做 | **(b)**。owner 最需要知道的是别人动了什么，而这部分的成本只落在非 owner 的写上 |
| **D12** | 撤销编辑者时他在途的后台作业 | 不中止 / 中止他发起的作业 | **不中止**（沿用内容写的既有 TOCTOU 裁决；中止需在每类作业上记账） |
| **D13** | 授予编辑者会关上借入挂载的「未共享门」，owner 自己在这本库里也检索不到借入参考库（§2.8、§4.6） | (a) 接受，确认弹窗按 `closes_when_shared`（仅靠借入臂有效的边，不含经 everyone 边可读的库、不含本库早已关门时就失效的边）精确列出将失效的参考库，并明说影响含 owner 自己 / (b) 把门收窄到查看者级（借 M3 的 `_VIEWER_REACHES_MOUNT_EXPR` 思路，只对读不到被挂库的查看者关门）；前提是另做一次安全审查，确认 owner 视角产出的共享派生物——共享理解底座、统一图谱、知识治理结果——不会把借入库内容带给编辑者 / (c) 本库有借入挂载时拒绝授予编辑者，要求先移除 | **(a)**。与今天开链接、共享给群组时的行为一致，不引入新的安全面；(b) 的审查面是三类派生物各自的写入来源，值得做但应单独立项；(c) 比 (a) 更生硬，且同样的情形在链接/群组共享上并不拒绝 |
| **D14** | 编辑者的破坏性动作范围（逐端点清单见 §4.1）：删除他人（含 owner）上传的来源、把 Knowhow 表移出本库、删除整张 Knowhow 表（硬删除、变更历史级联删除）、清理 Knowhow 变更历史（`history/prune`，硬删除，最短 1 天）、删除/重建图谱（人工审核结果不随重建回来）、清空本库检索经验（`DELETE .../understanding/experiences`） | (a) 与组管理员同权，全部可以 / (b) 删除来源仅限本人上传；Knowhow 表只能复制出库、不能移走、不能整表删除、不能清理变更历史；图谱删除/重建与清空检索经验照常可做 / (c′) 在 (b) 基础上把图谱删除与全量重建、清空检索经验也收为管理档 / (c) 编辑者不能删除任何来源 | **(b)**。收窄的四项对 owner 都不可撤销，其中清理历史还会抹掉其余 Knowhow 删除赖以回退的流水；图谱与检索经验都能重新生成，损失的是人工审核结果与积累时间，由「最近改动」可追责，且图谱维护本是协作的主要内容；(c) 让编辑者连自己误传的文件都删不掉 |
| **D15** | `notebook_activity.actor_id` 在账号合并登记表里的类别（仅 D11 选 (b) 时） | `audit_kept`（保留悬空 id，不阻塞合并） / `blocking` | **`audit_kept`**，与 `notebooks.delete_requested_by`、`auth_identity_audit.actor_id` 同类 |
| **D16** | 跨环境同步：源端的 `user` 主体边（含编辑者边）是否种进镜像（§4.6） | (a) 原样种入 / (b) 导入时降为 `viewer` / (c) 导入器跳过 `user` 主体边。注意 (a)(b) 都是 seed-only：源端之后的撤销**不会**传播到镜像，边会一直留着读权、关着未共享门、拦着账号合并 | **(c) 不种**。镜像的可见性本来就由目标端自己管理（同步设计 §5），点名到个人的授权在目标端由 owner 重新决定；群组边照旧按映射种入。完整性约束保证即使有遗留 `user` 行，owner 也能看见并撤销 |
| **D17** | 编辑者能否看到其他编辑者名单 | 只有 owner 看 / 编辑者可看其他编辑者的名字与显示名（只含 `editor` 行，不含授予人、时间、只读点名授权） / 编辑者可看全部字段 | **编辑者可看其他编辑者的名字与显示名**。协作需要知道还有谁能改；在场（D10 推荐）本来就让写者互见 |
| **D18** | 被合并的账号身上有编辑者边时 | 维持现状：边属 `blocking`，合并被拒，管理员请 owner 先撤边、合并后对保留账号重新授予 / 合并时把边迁到保留账号 | **维持现状**。合并只删「空账号」是既有原则；自动迁移授权会在 owner 不知情时改变谁能写他的库 |

---

## 13. 实现期核对项（不阻塞设计，计划阶段逐条落证据）

初稿里的三项已由代码给出答案，写入正文，不再是待核对项：

- 文档数上限按库 owner 算（`source_routes.py:243–255` `_document_capacity_limit`），见 §5「配额」；
- 同步导入按 `principal_type=user/group` 映射 `principal_id`，映射不到跳过并记日志，种子写入
  （`migration/sync/manifest.py:403–416`），见 §4.6 与 D16；
- 账号合并时 `user` 主体边归 `blocking`（`repositories/user_references.py:97`），见 §7 与 D18。

第二轮又有三项已由代码回答，转为 **P1 范围的实现项**：

- 整张 Knowhow 表删除是硬删除、变更历史级联删除、不可恢复（`postgres/knowhow_store.py:1235`；
  `0008_master_v28_features.sql` 第 47、62 行）→ 并入 D14，编辑者不能整表删除（§4.1）；
- `sources.uploaded_by` 已存在（迁移 0045）但没有进任何对外投影 → P1 给来源列表投影新增上传人字段（§4.1、§9）；
- `MountedBase` 只有 `active` / `inactive_reason`（`models/notebooks.py:80–84`），区分不出「借入」→ P1 给挂载
  投影新增布尔字段 `closes_when_shared`，按**臂**精确判定「此刻有效、且仅靠借入臂有效」：
  `MOUNT_VALID_EXPR ∧ ¬(b.tier='base' ∨ b.created_by=a.created_by ∨ everyone 授权)`，其中借入那一臂与
  `MOUNT_GATE_CLOSED_EXPR` 共用同一个 `_BORROWED_READ_EXPR`（`postgres/mount_sql.py:37–62`，两后端同修），
  不在调用点手拼。**不能**用 `MOUNT_ORIGIN_COLUMN` 的 `shared` 类别代替：`shared` 也包含经 `everyone` 边可读的
  他人库，而那一臂不受未共享门约束（`MOUNT_VALID_EXPR` 第 52–56 行里 `everyone_grant_expr` 与带门的借入臂
  并列），授予后照样有效，列为「将失效」就是误报。授予前警告只列 `closes_when_shared = true` 的边。
  本库**原本就已关门**（已有成员行或授权边）时，借入边此刻已经无效（`MOUNT_VALID_EXPR` 为假），
  `closes_when_shared` 恒为假——早已失效的挂载不会被当作「将失效」重复列出（它们在挂载面板上本来就以
  失效状态置灰）。

仍待核对：

1. 知识治理队列各审核端点对「已被处理」项的返回码。
2. 共享理解底座单条标签 PUT 是否已有版本比对。
3. `KgMaintenanceJobs.claim` 记录发起人所需的最小改动面，以及 SQLite 侧是否同构。
4. 作业落定触点的精确位置清单（与 `pending_bus` 发布点逐一对照），确保每个 `kind` 至少有一个终态触点。
5. 兄弟方法 `owner_if_can_edit` / `owner_if_can_admin` 的 `SELECT nb.created_by` 写法（§6.5），PG / SQLite 两侧
   计划与布尔版逐一相同的 EXPLAIN 证据。

---

## 附：与原 spec 的对照

| 原 spec（2026-06-04） | 本设计 |
| --- | --- |
| D2 用户名正则、D3 `X-User-Id` 信任制 | 作废：账号系统 + 会话 Bearer + 统一登录 |
| `notebook_shares(view/edit)` 新表 | 不建：复用 `notebook_grants` 的 `user` 主体臂，新增 `editor` 角色 |
| `_require_access(min_tier)` | 已由能力表 + 三条谓词实现，本设计加第四档 `edit` |
| view 分享 | 已由分享链接只读加入与群组 viewer 边实现 |
| `notebook_presence` 表 + `notebooks.revision` 列 | 在场与版本不落库：进程内 `CollabPulse`（约定单进程，§6.6 写明违反时的降级），避免在每个写事务里更新 `notebooks` 热行（`memory_store` 等写事务对该行持 `FOR SHARE`，见 `access_sql.py` docstring） |
| `notebook_activity` 可选 | 决策 D11，推荐只持久化非 owner 的写 |
| 3–5s 轮询 | 默认 15s（池压力下放大）、仅 `collab_pulse` 为真的查看者（公共臂读者不脉搏）、隐藏标签页暂停 |
| last-write-wins | Knowhow 单元格改乐观校验；其余保留 LWW + 历史 |
