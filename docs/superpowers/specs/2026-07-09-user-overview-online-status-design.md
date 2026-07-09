# 用户总览「当前在线」指示 — 设计

- 日期:2026-07-09
- 分支:`claude/user-overview-online-status-7e39bb`
- 目标:admin 的「用户使用总览」表格中,显示哪些用户**当前在线**。

## 背景与现状

现有 admin「用户使用总览」:

- 后端 `GET /api/admin/users`(admin-only,[backend/app/api/routes.py:1371](backend/app/api/routes.py:1371))→ `SQLiteRepository.list_user_usage()`([backend/app/services/sqlite_repository.py:1877](backend/app/services/sqlite_repository.py:1877)),返回每用户:`id / username / role / created_at / notebooks / sources / conversations / reports / last_active`。其中 `last_active = MAX(conversations.updated_at)`,只在建/改对话时跳动,较粗,**不是**登录/会话时间。
- 前端页面 `/admin/usage`([frontend/app/admin/usage/page.tsx](frontend/app/admin/usage/page.tsx)),表格列:用户名 / 角色 / 注册时间 / 笔记本 / 来源 / 对话 / 报告 / 最近活跃 / 日志。类型与 fetch 在 [frontend/app/admin/usage/api.ts](frontend/app/admin/usage/api.ts)。
- schema `AdminUserUsage` 在 [backend/app/models/schemas.py:698](backend/app/models/schemas.py:698)。

### 判断在线的现成信号(评估结论)

| 信号 | 位置 | 语义 | 成本 |
|---|---|---|---|
| **`pending_bus._conns`** | 内存 `dict[str, set[Queue]]` | 谁**此刻**开着 App(持有通知实时流) | O(1) 内存,零 DB |
| `auth_sessions.last_seen_at` | SQLite | 最近 N 分钟内有过请求 | 1 次索引 SQL |
| `MAX(conversations.updated_at)`(现有 `last_active`) | SQLite | 仅建/改对话时跳动 | GROUP BY |

**决策:采用 `pending_bus._conns`(实时连接口径)。** 它最贴合「当前在线」、零 DB 开销、秒级上下线。`auth_sessions.last_seen_at`(「近期活跃」)与两者结合方案已评估并否决(前者语义偏「活跃过」、后者 UI 复杂度不划算)。

## 在线口径与前提

- **在线** = 该 `user_id` 此刻存在于 `pending_bus._conns` 键集合中,即持有一条打开的通知中心实时流(NDJSON/SSE,[backend/app/api/routes.py:1398](backend/app/api/routes.py:1398) `GET /api/me/pending-actions/stream`;连接时 `register(uid)`,断开时 `finally: unregister(uid, q)`,15s keepalive 探活)。
- **前提(已核实)**:前端主应用 [frontend/app/page.tsx:1061](frontend/app/page.tsx:1061) 在登录后挂载 `usePendingActions(...)`(内部 `usePendingStream` 自动重连)。普通用户日常都在 `/` 页,故「App 开着 = 持流 = 在线」成立。
- **已知 caveat**:
  - `/admin/usage` 页本身不挂载该流,故 admin 停在此页时**自己那行可能显示离线**——不影响观察其他用户,接受为已知小瑕疵。
  - `pending_bus` 是单进程内存单例([backend/app/services/pending_bus.py](backend/app/services/pending_bus.py)),多 worker/多机部署下每进程各有各的 `_conns`。**当前部署为单进程**,故 `_conns` 对全局权威;文档注明此约束。
  - 后端重启后 `_conns` 清空 → 短暂全体显示离线,前端指数退避自动重连(秒级)补回。可接受。

## 后端改动

1. **`PendingBus.online_user_ids() -> set[str]`**(新增,[backend/app/services/pending_bus.py](backend/app/services/pending_bus.py)):
   - 实现 `return set(self._conns.keys())`。
   - **仅在事件循环线程调用**(见下),保持 PendingBus 现有「loop-only、免锁」不变量,**不引入锁**。

2. **`AdminUserUsage` 增字段**([backend/app/models/schemas.py:698](backend/app/models/schemas.py:698)):`is_online: bool = False`。

3. **`GET /api/admin/users` 改为 `async`**([backend/app/api/routes.py:1371](backend/app/api/routes.py:1371)):
   - 重的聚合仍在线程池:`rows = await run_in_threadpool(repository().list_user_usage)`(`from fastapi.concurrency import run_in_threadpool`)。
   - 回到 loop 线程读在线集合:`online = pending_bus.online_user_ids()`(同步读、无 `await` 交错 → 与 loop 内的 register/unregister 无竞态,安全)。
   - 逐行置 `is_online = row["id"] in online`,构造 `AdminUserUsage`。**首屏即正确**。
   - `list_user_usage` 保持纯净,不感知 bus。

4. **新增 `GET /api/admin/online`**(admin-only,`async`,[backend/app/api/routes.py](backend/app/api/routes.py)):
   - 返回 `{"online_ids": [...]}`(`list(pending_bus.online_user_ids())`)。
   - **纯读内存、零 DB**,供前端周期刷新,避免重跑 `list_user_usage` 的 5 个 GROUP BY。
   - 复用现有 admin 门控(`user.role != "admin"` → 403)。

## 前端改动

文件:[frontend/app/admin/usage/api.ts](frontend/app/admin/usage/api.ts)、[frontend/app/admin/usage/page.tsx](frontend/app/admin/usage/page.tsx)、[frontend/app/admin/usage/usage.css](frontend/app/admin/usage/usage.css)。

1. **类型**:`AdminUserUsage` 加 `is_online: boolean`。
2. **fetch**:新增 `fetchOnlineIds(): Promise<string[]>` 打 `GET /api/admin/online`。
3. **显示**:**用户名单元格前置一个小圆点**(绿 = 在线 / 灰 = 离线)+ `aria-label`/`title`(「在线」/「离线」)。紧凑、与现列对齐,不新增列(表已 9 列偏宽)。样式进 `usage.css`。
4. **刷新**:
   - 首屏 `fetchAdminUsers()` 自带 `is_online` → 初次渲染即正确。
   - 之后 `setInterval` 每 **15s** 调 `fetchOnlineIds()`,只把在线集合并入现有行(`is_online = ids.has(u.id)`),**不重拉重表**。
   - `useEffect` 清理:卸载时 `clearInterval`。
   - 表格角落可选加「截至 HH:MM:SS」小字(低优先,可省)。

## 效率(符合「运行效率一等约束」)

- 在线读:O(连接数) 内存操作,零 DB。
- 主表 `GET /api/admin/users`:成本与现状持平(仅每行多一次集合成员判断)。
- 周期轮询只打零 DB 的 `/api/admin/online`;不重跑聚合。
- 无新增写入、无新增表、无 schema 迁移。

## 测试

后端([backend/tests/test_admin_users.py](backend/tests/test_admin_users.py) 附近):

- 向 `pending_bus._conns` 塞入某注册用户的 `user_id`(或经 `register`)→ `GET /api/admin/users` 中该行 `is_online == True`,未连接用户 `is_online == False`。
- `GET /api/admin/online`:admin 返回含该 id 的 `online_ids`;非 admin → 403。
- 测后清理 `_conns`(避免污染其他测试)。

前端:类型编译通过;必要时对圆点渲染做轻量断言(视现有测试规模而定)。

## 交付

- 遵循仓库流程:分支 rebase 到 `master` 保持线性 → push → `gh pr create --base master`(合并按钮为 Rebase and merge)。
- 视觉验证:改完给出 `/admin/usage` 的真机/预览截图(在线绿点 + 离线灰点对齐)。

## 明确不做(YAGNI)

- 不改「最近活跃」列(仍为 conversations 口径)。
- 不引入 `auth_sessions.last_seen_at` 的「近期活跃」口径,不做「最后活跃 X 分钟前」文案。
- 不加 presence 表/心跳/WebSocket。
- 不为多进程/多机做跨进程 presence 汇聚(当前单进程)。
