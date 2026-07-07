# 管理员「用户使用总览」+ 界面显示用户名 — 设计

日期:2026-07-07
分支:`feat/admin-usage-overview`

## 背景与目标

当前系统已有按用户隔离的日志(`/dev/logs` + `/api/debug/logs`,channel = events/llm/requests,admin 可用 `?owner=<user_id>` 跨用户读),但:

1. 没有「列出所有用户」的入口,admin 无法一眼看到有哪些用户、各自用了多少(要手动去 `.local/logs/` 翻子目录、手拼 `?owner=`)。
2. 所有界面暴露的是内部 `user_id`(形如 `user-cec36ed943`),不是人类可读的**用户名**(形如 `h00932446`)。

**目标:**
- 给管理员一个**用户使用总览页**:列出所有用户 + 每人的用量统计,可下钻到该用户的日志。
- 页面入口链接**仅管理员可见**,普通用户看不到。
- 凡是展示用户的地方(总览页、`/dev/logs`)一律显示**用户名**而非内部 id。

## 非目标(明确排除)

- **不统一 `user_id` 与 `username`**、**不做任何 id 迁移**。内部 id 保持现状(`user-<hex>` / `user-local`),`is_safe_owner` / `owner_dir` 依赖的 id 格式不变。用户名只用于**显示**;下钻查日志时前端内部仍用 `user_id` 当 `owner` 传参。
- 不改注册逻辑(`create_user` 不变)。
- 不新增 LLM/embed/后台任务;总览统计只读 DB。
- 用户名唯一性**已由现有** `idx_users_username` 部分唯一索引保证,无需新增。

## 现状事实(实现依据)

- 用户表 `users(id, email, display_name, role, status, username, password_*, created_at, updated_at)`;`username` 有部分唯一索引。实测库 2 个用户:`user-local`(username=`admin`,role=admin)、`user-cec36ed943`(username=`h00932446`,role=user)。
- 归属列:`notebooks.created_by` / `conversations.created_by` → `users(id)`;`sources.notebook_id` / `reports.notebook_id` → `notebooks(id)`。
- admin 门控现有模式:路由内 `if user.role != "admin": raise HTTPException(403, ...)`。
- 前端顶栏已有 `currentUser.role === "admin"` 判断(page.tsx,渲染「(管理员)」徽章处)。
- `/dev/logs` 是独立 Next 路由;其 `api.ts` 调 `/debug/logs/{channel}`,当前**不传** `owner`(只看自己)。
- 原始 events/llm 日志受 `DEBUG_LOGS_ENABLED`(默认 false)门控;本设计的**统计接口不受此限**。

## Part 1 — 后端:`GET /api/admin/users`

新增只读端点,列出所有用户 + 用量统计。

**门控:** 依赖 `get_current_user`;`if user.role != "admin": raise HTTPException(403, "仅管理员可查看用户总览")`。不依赖 `DEBUG_LOGS_ENABLED`。

**响应模型 `AdminUserUsage`(list):**

| 字段 | 类型 | 含义 | 来源 |
|---|---|---|---|
| `id` | str | 内部 user_id(前端持有,用于下钻 `owner`,不展示) | `users.id` |
| `username` | str | 用户名(界面显示) | `users.username`(空则回退 `display_name`) |
| `role` | str | `admin` / `user` | `users.role` |
| `created_at` | str | 注册时间 | `users.created_at` |
| `notebooks` | int | 名下 notebook 数(排除 `status='copying'`) | `GROUP BY notebooks.created_by` |
| `sources` | int | 名下来源数 | `sources JOIN notebooks ON notebook_id GROUP BY created_by` |
| `conversations` | int | 名下对话数 | `GROUP BY conversations.created_by` |
| `reports` | int | 名下报告数 | `reports JOIN notebooks GROUP BY created_by` |
| `last_active` | str \| null | 最近活跃(取该用户 conversations 的 `max(updated_at)`) | `GROUP BY conversations.created_by` |

**实现要点(效率优先):**
- 仓库新增 `list_user_usage() -> list[dict]`:先 `SELECT * FROM users`,再用**固定数量**的 `GROUP BY` 聚合查询各指标(每指标一条,非按用户 N+1),在 Python 里按 `user_id` 合表。空缺计数补 0。
- 索引:确保 `notebooks.created_by`、`conversations.created_by` 有索引(缺则用 `_add_column_if_missing` 同款守卫式 `CREATE INDEX IF NOT EXISTS` 补)。`sources`/`reports` 经已索引的 `notebook_id` join。
- 只读、无写、无 LLM/embed。实测量级(用户个位数、notebook 个位数)开销可忽略;设计对大库同样是有界的 GROUP BY 扫描。

**路由挂载:** 复用现有 `routes.py`(已挂 `/api` + `get_current_user` 依赖)。路径 `GET /api/admin/users`。

## Part 2 — 前端

### 2a. 用户使用总览页(新路由 `/admin/usage`)

独立 Next 路由(**不写进 page.tsx**,沿用 `/dev/logs` 的独立工具页模式,便于隔离与单测)。

- 拉 `/api/admin/users`,渲染一张表:**用户名** · 角色 · 注册时间 · notebook 数 · 来源数 · 对话数 · 报告数 · 最近活跃。
- 每行可点 → 跳 `/dev/logs?owner=<id>`(带内部 id 当 owner;显示用用户名)。
- **403 兜底**:非 admin 直接访问该 URL → 接口 403 → 页面显示「无权限」,不渲染数据(纵深防御,不只靠隐藏入口)。
- 样式与 `/dev/logs` 保持一致(复用其 css 变量/表格风格)。

### 2b. 顶栏入口链接(page.tsx)

- 在顶栏账户区(`currentUser.role === "admin"` 已有判断处)增加一个链接「用户总览」→ `/admin/usage`。
- **仅 `currentUser.role === "admin"` 时渲染**;普通用户不渲染(与页面 403 兜底双保险)。

### 2c. `/dev/logs` 显示用户名 + admin 用户选择

- 顶部显示「当前查看:**<用户名>**」。
- **admin**:额外一个**按用户名**的下拉(选项来自 `/api/admin/users` 的 `{id, username}`),切换即以 `?owner=<id>` 重新加载;URL 带 `?owner=<id>` 进入时自动选中并解析出用户名显示。`api.ts` 的 `RecordQuery` 增加可选 `owner`,`fetchChannels`/`fetchRecords`/`fetchRecord` 透传。
- **普通用户**:无下拉、无 owner 参数,维持「只看自己」。顶部用户名取自 `/me`(现有 auth)。
- **DEBUG_LOGS_ENABLED 关闭时**:统计页正常;`/dev/logs` 原始日志区显示「原始日志未开启(设置 DEBUG_LOGS_ENABLED=true 并重启后端)」而非报错。

### 2d. 实现注意:认证头

现有 `frontend/app/dev/logs/api.ts` 用**裸 `fetch` 不带认证头**(本地未认证时后端回退 `user-local`,恰为 admin,所以「看起来能用」)。但:
- 新的 admin-only 接口 `/api/admin/users` 与跨用户 `?owner=` 读取,**必须携带 `authHeaders()`**(与主应用 `auth.ts` 一致),否则真实部署下 `get_current_user` 认不出管理员 → 403 或错读 user-local。
- 因此:总览页与 `/dev/logs` 的所有请求统一走带 `authHeaders()` 的 fetch(复用 `auth.ts` 的 `API_BASE`/`authHeaders`)。这也顺带修正 `/dev/logs` 原先的裸 fetch。

## 数据流

```
顶栏「用户总览」(仅 admin 渲染)
   → /admin/usage  ──GET /api/admin/users(403 if not admin)──► 用户统计表(显示 username)
        └─点某行─► /dev/logs?owner=<user_id>
                       ├─ 显示「当前查看: <username>」(admin 下拉可切)
                       └─GET /api/debug/logs/{events,llm,requests}?owner=<user_id>
                            (原始日志需 DEBUG_LOGS_ENABLED)
```

## 安全 / 权限

- `/api/admin/users`:admin-only(403),纵深防御的第一层。
- `/admin/usage` 前端页:接口 403 → 显示无权限;入口链接对非 admin 不渲染。
- `/dev/logs` 的跨用户读:`?owner=` 的越权判定沿用现有 `_resolve_owner`(非 admin 传别人 owner → 403;admin 才可读任意 owner)。本设计不放宽该逻辑。
- 用户名展示不引入注入面(纯文本渲染,React 默认转义)。

## 错误处理

- 接口层:非 admin → 403;DB 异常 → 500(FastAPI 默认)。
- 前端:统计接口失败(403/网络)→ 页面显示对应态(无权限 / 加载失败重试),不崩。
- `last_active` 无对话 → null → 显示「—」。

## user-local 特例

`user-local`(username=`admin`)的 id 保持不变——它是全系统回退哨兵(无认证兜底、后台 job 默认模型解析)。总览与日志页对它显示用户名 `admin`。不迁移、不改哨兵语义。

## 测试(TDD)

**后端:**
- admin 调 `/api/admin/users` → 200 + 每用户统计正确(播种 2+ 用户、各挂不同数量 notebook/source/conversation/report,断言计数)。
- 非 admin 调 → 403。
- 空缺计数补 0(某用户无任何内容 → 全 0、`last_active=null`)。
- `username` 空串 → 回退 `display_name`。

**前端:**
- helper 单测(`.test.mjs`):按 `role` 计算入口链接是否渲染;`owner` 参数拼接进 query。
- 总览页:mock `/api/admin/users` → 渲染用户名与计数;403 → 无权限态。
- `/dev/logs`:owner→用户名解析显示;admin 下拉切换触发带 owner 的请求。

## 交付

前后端同一 PR 交付(遵循前后端同步设计原则)。分支 `feat/admin-usage-overview`,最终 rebase 到 master 走线性 PR。
