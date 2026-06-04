# 用户系统 + 会话管理 + 笔记本分享 + 近实时协作 — 设计文档（spec）

- 日期：2026-06-04
- 状态：待用户 review（用户要求：先写 spec，确认理解无误后再写 plan）
- 范围：在当前单用户基础上，加 4 件事。**这是多子系统需求，按 4 个独立可交付的 Phase 拆分，每个 Phase 单独出 plan + 实现。** 建设顺序 = 用户指定：先会话管理，再用户系统，再分享，再协作。

## 0. 现状（已勘探）
- `users(id, email UNIQUE, display_name, role, status, created_at, updated_at)`，当前硬编码本地用户 `user-local`；`current_user()` 读 `user-local`；`GET /me` 返回它；`user_profiles(user_id, memory_mode, domain_focus)` 已存在。
- `notebooks.created_by REFERENCES users(id)`（所有权脚手架已在）。
- `conversations(id, notebook_id, title, created_at, updated_at)` + `answers.conversation_id`（Phase 3 已落地，但**无 owner 列**）。
- 无鉴权中间件；前端无登录。

## 0.1 已确认决策（2026-06-04）
- **D1 协作实时性 = 近实时（轮询 + presence）**：同一笔记本两人可编辑，几秒内经轮询看到对方改动 + 在线状态；无 websocket/CRDT；冲突 last-write-wins。
- **D2 用户名格式 = 一个字母 + 00 + 6 位数字**：正则 `^[A-Za-z]00[0-9]{6}$`（如 `a00123456`）。不满足则前端报错、要求修改。字母不区分大小写，存储统一小写做唯一键。
- **D3 鉴权 = 仅用户名、无密码（信任制）**：输入合法用户名即代表该身份；身份存前端 localStorage，每个请求带 `X-User-Id` 头；后端据此解析 `current_user`。适合本机/团队 beta。
- **D4 存量数据 = 归属默认用户**：首次引入用户系统时，把现有 `user-local` 拥有的 notebooks/conversations/数据迁移给第一个登录的用户（或固定种子用户），保持可用、有主。

## 1. 跨切面：身份与访问控制（Phase B/C/D 的地基）
- **身份**：用户名是自然主键。`users` 增 `username TEXT UNIQUE`（小写）。登录 = `POST /api/login {username}` → 校验 D2 正则（不合法返回 400 + 提示）→ upsert 用户 → 返回 `{id, username}`。前端存 `id` 到 localStorage，之后每请求带 `X-User-Id`。
- **current_user 解析**：后端依赖项从 `X-User-Id` 头解析用户；缺失/未知 → 401（除 `/login`、`/health` 等白名单）。`current_user()` 不再硬编码 `user-local`。
- **访问控制（access tier）**：对某 notebook，当前用户的权限 = `owner`（created_by 本人） > `edit`（被分享 edit） > `view`（被分享 view） > 无。读操作需 ≥view，写操作需 ≥edit。封装为 `_require_access(notebook_id, user, min_tier)`，在所有 notebook 子资源路由统一校验。

## 2. Phase A — 聊天会话管理（先做，单用户即可用）
**目标**：同一 notebook 下，单用户可有多个会话（session=conversation），并在它们之间切换、看历史、开新会话。后端 conversations 已具备，主要补 UI + 会话归属。
- **数据**：`conversations` 增 `created_by TEXT`（owner）。列表/详情按 notebook + 当前用户过滤。（Phase B 前 `created_by` 用 `user-local` 占位。）
- **后端**：`list_conversations(notebook_id, user_id)` 按用户过滤；`ask` 写入 `created_by`；新增 `DELETE /conversations/{id}`（删会话）、可选 `PATCH /conversations/{id}` 改标题。
- **前端**：聊天面板加「会话列表」侧栏/下拉：显示本 notebook 当前用户的会话（标题 + 时间 + turn 数），点击切换 → 加载该会话 `GET /conversations/{id}` 的 turns 进线程；「新会话」清空线程 + 置 `conversationId=null`（下条消息自动建新会话）；删除会话。切 notebook 时按 notebook 取各自会话。
- **验收**：建多个会话、切换看到各自历史、刷新后历史还在（持久化）、新会话独立。

## 3. Phase B — 用户身份 + 单用户数据隔离
**目标**：实现 D2/D3 登录，把数据按用户隔离；存量数据迁移给默认用户（D4）。
- **数据**：`users.username`（唯一，小写）；所有「归属」对象以 `created_by`/owner 关联用户。需确保 owner 列齐全：`notebooks.created_by`（已有）、`conversations.created_by`（Phase A 加）。
- **后端**：
  - `POST /api/login {username}` 校验 + upsert + 返回 user。
  - `current_user` 依赖项读 `X-User-Id`；`GET /me` 返回当前用户。
  - `list_notebooks` 改为「我创建的 + 分享给我的」（Phase C 接入分享；Phase B 先只「我创建的」）。
  - 写路由记 `created_by = current_user`。
  - **迁移**：启动时若存在 `user-local` 拥有的数据且尚无真实用户 → 首个登录用户继承这些数据（把 `created_by='user-local'` 的行改为该用户），或建固定种子用户 `a00000000` 承接。迁移幂等、有日志。
- **前端**：未登录 → 登录页（输入用户名，前端先按 D2 正则校验，错误就地提示；通过则 `POST /login`）。登录后存 user 到 localStorage，所有 `api()` 调用带 `X-User-Id`；右上角显示用户名 + 退出（清 localStorage）。
- **验收**：合法/非法用户名校验；登录后只看到自己的 notebooks；存量数据归属首登用户可用；退出再换用户名隔离。

## 4. Phase C — 笔记本分享 + 权限（view / edit）
**目标**：owner 把 notebook 分享给其他用户名，选 view 或 edit。
- **数据**：`notebook_shares(id, notebook_id, user_id, permission CHECK in('view','edit'), created_by, created_at, UNIQUE(notebook_id,user_id))`。
- **后端**：
  - `POST /notebooks/{id}/shares {username, permission}`（仅 owner）→ 校验目标用户名存在（或允许分享给尚未登录过的合法用户名，登录即生效）→ upsert 分享。
  - `GET /notebooks/{id}/shares`（owner）、`DELETE /notebooks/{id}/shares/{user_id}`（撤销）、`PATCH` 改权限。
  - `list_notebooks` = 我创建的 ∪ 分享给我的；每条带 `access_tier`（owner/edit/view）。
  - 所有 notebook 子资源路由套 `_require_access`（读≥view、写≥edit）。view 用户看不到写操作入口（前端按 tier 隐藏）+ 后端硬拦。
- **前端**：notebook 卡片/工作区「分享」按钮（仅 owner）：输入用户名 + 选 view/edit + 现有分享列表（可改/撤销）。notebook 列表区分「我的 / 分享给我的」+ 角标显示 view/edit。view 模式隐藏上传/编辑/审核等写入口。
- **验收**：分享后对方能看到；view 不能写（前后端双拦）；edit 能写；撤销后失访。

## 5. Phase D — 近实时协作（轮询 + presence）
**目标**：edit 共享下两人同一 notebook 操作，几秒内互见 + 在线状态（D1）。
- **数据**：`notebook_presence(notebook_id, user_id, last_seen)`（心跳）；notebook 增 `revision INTEGER`（或复用 `updated_at` + 子资源变更计数）作为「变了没」信号；可选 `notebook_activity(id, notebook_id, user_id, action, target, created_at)` 记录近期操作。
- **后端**：
  - `POST /notebooks/{id}/presence`（心跳，更新 last_seen，需 ≥view）→ 返回当前在线用户（last_seen 在 ~15s 内）。
  - 任何写操作 bump `notebook.revision` + 写一条 activity。
  - `GET /notebooks/{id}/state?since=rev`：返回当前 revision + （可选）自 since 以来的 activity；客户端据此决定是否重拉 sources/KG/articles。
- **前端**：进入 notebook 后定时（~3–5s）心跳 + 拉 state；revision 变化 → 刷新受影响区（来源列表 / KG / 文章）；顶部显示在线协作者（用户名/头像）；可选「最近操作」浮条。冲突按 last-write-wins，刷新即对齐。
- **非目标**：不做 websocket、不做字符级实时光标、不做 CRDT/OT、不做编辑锁（后续若需要再单独立项）。
- **验收**：两用户（两浏览器）同一 edit 笔记本，一方加来源/审核，另一方数秒内看到；presence 显示双方在线；view 用户只读且也能看到更新。

## 6. 数据/接口变更汇总
- 新表：`notebook_shares`、`notebook_presence`、可选 `notebook_activity`。改列：`users.username`、`conversations.created_by`、`notebooks.revision`。均 `IF NOT EXISTS` / 守卫式 `ALTER`。
- 新端点：`POST /login`；`/notebooks/{id}/shares` CRUD；`/notebooks/{id}/presence`；`/notebooks/{id}/state`；`DELETE/PATCH /conversations/{id}`。
- 头：所有受保护请求带 `X-User-Id`。

## 7. 测试策略（每 Phase 各自的 plan 内细化）
- 后端 pytest：用户名校验（合法/非法）、登录 upsert、X-User-Id 解析、access tier 拦截（view 写被拒）、分享 CRUD、会话按用户过滤、presence 心跳/在线集合、revision bump、迁移幂等。全 mock，无网络。
- 前端 `tsc --noEmit` + 人工 eyeball：登录校验、会话切换、分享入口按 tier 显隐、协作刷新 + presence。
- 真机 smoke（主会话）：两用户两浏览器走完分享 + 协作。

## 8. 全局非目标（YAGNI）
- 不做密码/SSO/邮箱验证（D3 信任制）。
- 不做 websocket / CRDT / 实时光标 / 编辑锁。
- 不做组织/团队/RBAC 细粒度角色（owner/edit/view 三档够用）。
- 不改 KG 抽取/检索/问答内核（已收敛）。
- 模型仍一律走 URL（不引本地模型）。

## 9. 给用户 review 的确认点
1. 4 个 Phase 的拆分与建设顺序（A→B→C→D）对吗？
2. 会话（session）= 每用户每 notebook 的多个 conversation，是否符合你说的「session 切换 / 历史保留」？
3. 协作只刷新「来源/KG/文章」这类共享态，**聊天会话保持每人独立**（不共享对方的提问），对吗？
4. 存量数据归属「首个登录用户」可接受吗（还是要固定种子用户名）？
