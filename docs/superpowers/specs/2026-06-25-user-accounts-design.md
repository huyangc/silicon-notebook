# 用户系统（自助注册 + 密码登录 + 数据按 owner 隔离 + admin 管 base KG） — 设计文档（spec）

- 日期：2026-06-25
- 状态：待用户 review（先确认 spec，再写 plan）
- 范围：在当前单用户（硬编码 `user-local`）基础上，引入**多用户身份 + 密码认证 + 笔记本按所有者隔离**，并把现有 base/admin 概念落到账号体系上。

## 取代关系（重要）

本 spec **取代** `docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` 中的「用户身份/登录」部分（其 D2/D3/D4）。用户已于 2026-06-25 给出新的、相互冲突的决策，以本文档为准：

| 维度 | 旧 2026-06-04 | 本 spec（生效） |
|---|---|---|
| 用户名 | 恰好 1 字母 `^[A-Za-z]00[0-9]{6}$` | **1+字母** `^[A-Za-z]+00\d{6}$` |
| 认证 | 仅用户名、无密码（信任制 `X-User-Id`） | **用户名 + 密码**（Bearer session token） |
| 存量数据 | 归首登用户 / 种子 `a00000000` | 归 **admin**（`user-local` 原地升级，零搬运） |
| base/admin | 无 | **base 层由 admin 维护、对普通用户隐藏，仅问答后台检索** |
| 范围 | 会话 + 用户 + 分享 + 近实时协作（4 期） | 仅本期：注册 + 登录 + owner 隔离 + admin/base |

旧文档的「Phase A 会话 `created_by`」已实现并保留使用；其分享（Phase C）/协作（Phase D）**不在本期范围**（见 §9 YAGNI），如需要另立项。

## 0. 现状（已勘探，带锚点）

- `users(id, email NOT NULL UNIQUE, display_name, role, status, created_at, updated_at)`，当前只 seed 一个 `user-local`，`role='curator'`（`backend/app/services/sqlite_repository.py` `_seed()` ~700–715）。`user_profiles(user_id, memory_mode, domain_focus)` 同 seed。
- `current_user()` 永远查 `WHERE id='user-local'`（同文件 ~847–861）；`GET /api/me` 返回它（`backend/app/api/routes.py` ~107–109）。
- `notebooks.created_by REFERENCES users(id)`，建表时**硬编码** `'user-local'`（`create_notebook()` ~896）。`list_notebooks()` **无 WHERE**（返回全部，~863）。`notebooks.tier`（`base`|`personal`，默认 `personal`）经守卫式 `ALTER` 加入（~638）。
- `conversations.created_by` 已存在且 `list_conversations` / `bulk_delete_conversations` 已按 `(notebook_id, created_by)` 过滤（~6538–6692）——多用户会话隔离地基已就位。
- `ask()` 内部 federated 检索按 `tier='base'` **直查**（不经 owner 校验，~4722–4733）——base 库天然可被任何问答检索到。
- **无任何认证**：无密码、token、session、login/register；CORS 已设 `allow_credentials=True`（`backend/app/main.py` ~62–69）。`role` 字段**无任何鉴权门控**（仅 seed 值 + `reviewed_by='curator'` 审计串），改 `user-local` 角色为 `admin` 安全。
- 前端单页 `frontend/app/page.tsx`：`API_BASE`（line 35），通用 `api()` 包装器不带 auth header（~471–498），无登录态、无 localStorage；collection 视图已有「全部/我的笔记本/精选笔记本」过滤标签。

## 1. 已确认决策（2026-06-25）

- **U1 用户名** = `^[A-Za-z]+00\d{6}$`（1+ 个字母 + 字面 `00` + 6 位数字，如 `zhang00123456`）。大小写均允许；**存储与唯一性用小写归一化**（防 `Abc/abc` 撞键），登录大小写不敏感。
- **U2 认证** = 用户名 + 密码。密码**用户自定、不限长度**（仅非空）。会话用**不透明 token + `auth_sessions` 表**，前端存 localStorage，请求带 `Authorization: Bearer <token>`。
- **U3 admin** = 把现有 `user-local` 原地升级：`role='admin'`、`username='admin'`、密码哈希来自环境变量 `SILICON_NOTEBOOK_ADMIN_PASSWORD`（缺省 `admin`）。**不强制改密**。现有 notebook 已 `created_by='user-local'` → 自动归 admin，**零数据搬运**。
- **U4 base 可见性** = 对普通用户**完全隐藏**（不出现在其列表），但问答仍把 base 当权威参考检索（不动 `ask()` 的 federated 链路）。
- **U5 角色** = `admin` | `user` 两档。无组织/RBAC 细粒度。

## 2. 关键技术选型

- **认证机制 = DB session token**（而非无状态 JWT / cookie）：登录签发 `secrets.token_urlsafe(32)`，存 `auth_sessions`，可吊销、可滑动过期、无密钥管理、零新依赖。契合现有 `api()`（header 注入）。
- **「当前用户」传递 = ContextVar**（仓库已有 model_errors 的 ContextVar 先例）：auth 依赖把已认证 `UserProfile` 写入 `ContextVar`；`current_user()` 改为读它，未设时**回退 `user-local`（admin）**——保证离线脚本/eval/直接测 repository 的用例不破。安全关键的 notebook 归属再加一道**显式 owner 校验**（§4），不单靠 ContextVar。
- **密码哈希 = 标准库 `hashlib.pbkdf2_hmac`**（sha256 + 每用户随机 salt + 固定高迭代，如 200k），不引新依赖，符合项目「离线/少依赖」。存 `password_hash` / `password_salt` / `password_iterations` 三列，便于日后调参。

## 3. 数据模型变更（SQLite，走现有守卫式 `_migrate()` 增量列）

`users`（已存在）新增列（`ALTER … ADD COLUMN`，PRAGMA 守卫幂等）：
- `username TEXT`（+ `CREATE UNIQUE INDEX IF NOT EXISTS` on 小写 username）
- `password_hash TEXT NOT NULL DEFAULT ''`、`password_salt TEXT NOT NULL DEFAULT ''`、`password_iterations INTEGER NOT NULL DEFAULT 0`
- `role` 复用现有列，取值改用 `admin`|`user`

email 约束处理：现有 `email NOT NULL UNIQUE` 不动；注册时**合成占位** `email = f"{username}@users.silicon-notebook.local"` 满足约束（登录只认 username）。

新增表：
```
auth_sessions(
  token TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
)
```
token 30 天滑动过期（每次命中刷新 `last_seen_at`，临近到期顺延 `expires_at`）；logout 删行；过期行惰性清理。

`notebooks` / `conversations` **不改表**（`created_by`、会话隔离已就位）。

## 4. 归属与可见性（核心隔离规则）

- `list_notebooks()` → `WHERE created_by = 当前用户.id`。
  - 普通用户：只见自己创建的 personal 本 → **base 自动隐藏**（满足 U4）。
  - admin：见自己创建的（= 全部 base 本 + admin 个人本）。
- `create_notebook()` → `created_by = 当前用户.id`（替换硬编码 `'user-local'`，line ~896）。
- 新增守卫 `_assert_can_access_notebook(notebook_id)`：通过当且仅当 `created_by == 当前用户.id`，**无 admin 全局越权**（admin 能进 base 本纯粹因为 base 本的 owner 就是 admin；admin 同样看不到其他普通用户的私人本，保护隐私）。失败返回 **404**（不泄露存在性）。在 `get/update/delete_notebook` 及所有 notebook 子路由（sources / knowledge / ask / conversations / graph / analytics …）入口统一调用。
- **base 仍被问答检索**：不改 `ask()` 内部按 `tier='base'` 直查的 federated 逻辑——普通用户问答照常吃到 admin 的基础 KG。这是「隐藏但后台检索」的关键。
- 「设为基准库 / 取消基准库」动作仅 admin 可见可用（前端按 role 隐藏 + 后端在 `POST /notebooks/{id}/tier` 校验 role=='admin'）。

## 5. 后端 API

**免认证**（白名单：`/`、`/health`、`/ask-modes`、`/doc-types`、`/auth/*`）：
- `POST /api/auth/register {username, password}` → 校验 U1 正则 + 唯一 + 密码非空 → 建 `user(role='user')` + `user_profiles` 行 → 自动登录，返回 `{token, user}`。用户名非法/已存在/密码空 → `400`（结构化 detail）。
- `POST /api/auth/login {username, password}` → 校验 → 签发 session → `{token, user}`。失败 `401`。
- `POST /api/auth/logout`（带 Bearer）→ 删当前 session，`204`。

**需认证**：
- `GET /api/me`（已存在）→ 返回已认证用户；`UserProfile` 增 `username` / `role`。

**依赖与中间件**：
- 新增 FastAPI 依赖 `get_current_user`：
  1. 读 `Authorization: Bearer <token>`；命中有效 session → 解析 user，写 ContextVar，返回。
  2. token 存在但无效/过期 → `401`。
  3. 无 token：若 `settings.auth_optional`（默认 **False**）→ 回退 seeded admin（写 ContextVar）；否则 `401`。
- 用 `Depends(get_current_user)` 挂到除白名单外的全部用户面路由（routes.py 通过 router 级 `dependencies=` 或逐路由注入，具体在 plan 定）。

`change-password`：**本期不做**（原仅为「首登强制改密」而设，U3 已去掉强制；见 §9）。

## 6. 前端（单页 `page.tsx` 加登录门，不新增路由）

- 新增 `currentUser` 状态 + `token` 存 localStorage。
- 启动：无 token → 渲染**登录/注册门**（同页切换登录↔注册，贴合现有 SPA，不建 `/login` 路由）；有 token → 调 `/me`，401 则清 token 回登录门。
- `api()` 包装器统一注入 `Authorization: Bearer <token>`；任一响应 401 → 清 token + 回登录门（集中处理，覆盖所有调用点，含 `readAskStream`）。
- **注册表单**：用户名输入实时正则提示（占位示例 `zhang00123456`，不符即时报错）、密码 + 确认密码。
- **登录表单**：用户名 + 密码。
- 顶栏右侧加用户菜单：显示 username、「退出」（调 logout + 清 localStorage + 回登录门）。
- 普通用户隐藏「设为基准库」入口（按 `currentUser.role` 判断）。

## 7. 测试策略

- **多数测试不受影响**：~120 个测试里仅 **11 个走 HTTP(TestClient)**；其余测 service/repository，走 `current_user()` 的 ContextVar 回退（=admin），无需改。
- **HTTP 测试**：新建顶层 `backend/tests/conftest.py`，autouse fixture 把测试进程 `settings.auth_optional=True`（无 token 即 admin）——那 11 个文件**无需逐一登录**即可继续以 admin 跑。
- **新增专项测试**：
  - 用户名校验（合法/各类非法：少字母、缺 `00`、位数错、含非法字符）。
  - 注册→自动登录→`/me`；重复用户名 400；密码空 400；大小写归一化唯一性。
  - 登录成功/失败（错密码 401）、logout 后 token 失效 401。
  - **隔离**：用户 A、B 各注册登录（带各自 token），A 建 notebook，B 列表看不到、直接 GET A 的 notebook → 404；admin 能见自己的（含 base）；普通用户列表不含 base 本。
  - base 仍被检索：普通用户问答命中 admin 的 base KG（沿用既有 federated 测试骨架）。
- 前端 `tsc --noEmit` + `npm run build` 绿；人工 eyeball 登录/注册/退出/正则提示/用户菜单。
- 全流程 `scripts/check.sh` 绿。

## 8. 数据/接口变更汇总

- 改列：`users.username`（+唯一索引）、`users.password_hash/password_salt/password_iterations`；`users.role` 语义改 `admin|user`。
- 新表：`auth_sessions`。
- seed/migrate：`user-local` → `role='admin'`、`username='admin'`、密码哈希自 `SILICON_NOTEBOOK_ADMIN_PASSWORD`（缺省 `admin`）；幂等、有日志。
- 配置：`SILICON_NOTEBOOK_ADMIN_PASSWORD`（admin 初始密码）、`auth_optional`（默认 False；测试/本地可开）。
- 新端点：`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`；`GET /api/me` 扩字段。
- 前端：登录门组件、`api()` 注入 token + 401 处理、用户菜单、按 role 显隐 base 动作。
- 文档：README / README_zh / AGENTS.md 增「用户系统」段；`.env.example` 增两个变量；`fangan_done.md` 记录本特性。

## 9. 非目标（YAGNI）

- 不做：用户间分享 / 协作 / presence（旧 Phase C/D 另立项）。
- 不做：邮箱验证、找回密码、**改密**、限流、第三方登录 / SSO。
- 不做：组织 / 团队 / 细粒度 RBAC（admin/user 两档够用）。
- 不改 KG 抽取 / 检索 / 问答内核；模型仍一律走 URL。

## 10. 给用户 review 的确认点

1. 用户名小写归一化（`Zhang00…` 与 `zhang00…` 视为同一账号）可接受吗？
2. admin 初始密码缺省 `admin`、可经 `SILICON_NOTEBOOK_ADMIN_PASSWORD` 覆盖、**不强制改密**——OK 吗？
3. 普通用户**完全看不到** base 本（连只读都不行），但问答能用到——符合预期吗？
4. 登录/注册做成**同页门**（不新增 `/login` 路由）可接受吗？
5. 本期**不做改密功能**（用户注册时定下的密码即固定，需要可后续补）——可接受吗？
