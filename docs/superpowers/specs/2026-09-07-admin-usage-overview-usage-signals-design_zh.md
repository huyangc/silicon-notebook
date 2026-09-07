# 用户使用总览：使用强度信号与来源数口径修正 — 设计文档（spec）

状态：已拍板（2026-09-07）。决策记录见 §7。

规范后端：**PostgreSQL**。本文所有 SQL 以 PG 方言写出，是口径的唯一定义。SQLite 侧按仓库规则
（唯一 repository factory 按 `DATABASE_URL` 选择后端，两侧组合相同运行时边界）必须同修，但只要求
通过同一组 parity 用例；时间列混合 offset 的处理沿用 `_absolute_instant` 既有做法，不在本文重复。

## 0. 目标（一句话）

让 `/admin/usage` 从「谁产出了多少」升级为「谁在用、用得多重、现在还在不在用」，并修掉「来源」列与
同页其它列自相矛盾的归因口径。

## 1. 现状与问题

现有主表列：用户名（含在线点）/ 角色 / 注册时间 / 笔记本 / 来源 / 提问 / 报告 / 最近活跃 / 用户分析 /
文档上限 / 密码 / 权限管理。数据来自 `QueryStore.list_user_usage()`
（[postgres/query_store.py:563](../../../backend/app/repositories/postgres/query_store.py)）。

### 1.1 「来源」列口径与同页其它列矛盾（必须修）

| 列 | 归因 | 排除合成来源 | 排除 copying/deleting 笔记本 |
| --- | --- | --- | --- |
| 来源（用户总数） | 笔记本 owner `nb.created_by` | **否** | **否** |
| 展开明细的每库来源数 | — | 是 | 是 |
| 最近活跃的上传候选 | 实际上传者 `s.uploaded_by` | 是 | 是 |
| 提问 / 报告 | 提交者 `created_by` | — | — |

后果：
- 存过 Memory 或建过 Knowhow 表的用户，总来源数偏高，且与展开行合计对不上。
- 往别人共享库上传的人，上传量记在 owner 名下；同一屏「提问」「报告」却记在提交者名下。
- 深拷贝进行中的笔记本里的来源已被计入。

### 1.2 现有指标只有「产出量」

看不到：最近一次真正上线、磁盘占用、最近一段时间是否还在用、失败率、图谱构建这类重 LLM 动作、
协作参与。`llm.jsonl` 里有逐次模型调用与 token，但只在文件系统，未入库，本期不上表（§8）。

## 2. 用户故事（验收锚点）

1. 管理员展开某个用户，能看到「最近上线」，把「昨天登录只看了看」和「一周没来」分开；
   主表的「最近活跃」仍只反映上传/提问/报告三类产出动作，两者并列而不是替换。
2. 管理员展开某个用户，能看到「近 30 天提问」，判断此人当前是否还在用，而不是靠累计总数猜。
3. 管理员展开某个用户，能看到「存储」，并与主表「文档上限」对照。
4. 管理员展开某个用户，能直接看到提问/报告的失败次数，不用点进提问明细。
5. 主表「来源」总数等于该用户展开行各库来源数之和（当无共享库上传、无留存活动时）。
6. 主表列数、列顺序、排序键与现状完全一致。

## 3. 分期

### Phase A — 来源数口径修正（纯修正，零迁移，先做）

用户级来源总数改为与 `last_active` 的上传候选**同一谓词**：

```sql
SELECT k, SUM(c) AS c FROM (
  SELECT COALESCE(NULLIF(s.uploaded_by, ''), nb.created_by) AS k, COUNT(*) AS c
  FROM sources s JOIN notebooks nb ON nb.id = s.notebook_id
  WHERE nb.status NOT IN ('copying', 'deleting')
    AND s.source_type NOT IN ('memory', 'knowhow')
  GROUP BY 1
  UNION ALL
  SELECT COALESCE(NULLIF(a.actor_id, ''), a.notebook_owner_id) AS k, COUNT(*) AS c
  FROM retained_user_activity a
  WHERE a.activity_type = 'source' AND a.expires_at > CURRENT_TIMESTAMP
    AND NOT EXISTS (SELECT 1 FROM notebooks live WHERE live.id = a.notebook_id)
  GROUP BY 1
) t GROUP BY k
```

要点：
- `nb.status` 过滤用 `access_sql.NOTEBOOK_LIVE_SQL`，`source_type` 过滤用
  `VISIBLE_SOURCE_TYPES_PREDICATE`，不裸写。
- `COALESCE(uploaded_by, nb.created_by)`：`uploaded_by` 为 NULL 的行只有两类——深拷贝落到接收方
  库里的副本（`notebook_sharing.py` 显式置 NULL）和极早期未回填行。来源数是**资产**口径，副本归
  接收方 owner 是合理的；`last_active` 是**动作**口径，副本不刷新任何人，两者刻意不同（§7 决策 2）。
- retained 分支改按 `COALESCE(NULLIF(actor_id,''), notebook_owner_id)`（原只按 `notebook_owner_id`）：
  实际上传者优先，与 live 分支同一条资产口径；`actor_id` 为空（留存时 `uploaded_by` 为 NULL）的行回落到
  当时的笔记本 owner，这样删除笔记本不会让这类来源从所有人的计数里消失。`last_active` 的 retained
  候选仍只看 `actor_id`（动作口径，副本不刷新任何人）。
  留存快照写入侧（`notebook_store.py`）已用 `VISIBLE_SOURCE_TYPES_PREDICATE` 排除 memory/knowhow，读侧不重复。
- 文档 `docs/product-and-api*.md` 中「用户级来源总数」一句同步改写。

### Phase B — 使用强度信号（主表列数不变，全部呈现在展开区）

所有新字段加进 `list_user_usage()` 返回 dict 与 `AdminUserUsage` 模型（带默认值，旧客户端兼容），
仍是一次性全表聚合，不 per-user 开连接。**主表不加列、不改现有单元格**；新信号只在该行展开后的
「用户摘要」区呈现（B6）。

#### B1 最近上线 `last_seen`

```sql
SELECT user_id AS k, MAX(last_seen_at) AS m FROM auth_sessions GROUP BY user_id
```

`auth_sessions.last_seen_at` 由每次请求以 `AUTH_SESSION_TOUCH_INTERVAL_SECONDS`（默认 300s）节流刷新，
浏览、检索、看图谱都会刷。局限：登出或被吊销会删行，用户登出后此列变空；Agent 长期凭据不经过
`auth_sessions`，Agent 动作不刷新。

采纳做法（§7 决策 1）：给 `users` 加可空列 `last_seen_at`，在会话 touch 的同一写事务里一并更新，
同样受 300s 节流；`list_user_usage` 直接读该列。代价是一条 PG 迁移 + SQLite v72。上面的 `auth_sessions` 聚合仅作
口径参考，不采用。

#### B2 存储占用 `storage_bytes`

```sql
SELECT COALESCE(NULLIF(s.uploaded_by, ''), nb.created_by) AS k, SUM(s.file_size) AS b
FROM sources s JOIN notebooks nb ON nb.id = s.notebook_id
WHERE nb.status NOT IN ('copying', 'deleting')
  AND s.source_type NOT IN ('memory', 'knowhow')
GROUP BY 1
```

与 Phase A 同一归因、同一过滤；已删笔记本的文件已物理清理，不计。前端以 KB/MB/GB 显示。

#### B3 近 30 天提问 `questions_30d`

```sql
SELECT created_by AS k, COUNT(*) AS c FROM ask_jobs
WHERE created_at >= CURRENT_TIMESTAMP - INTERVAL '30 days' GROUP BY created_by
UNION ALL  -- retained 分支同 questions，追加 created_at 窗口条件
```

窗口固定 30 天，服务端算，不做可配置。实施时核实 `ask_jobs` 是否已有 `(created_by, created_at)` 索引
（活动流的 creator-wide keyset 索引可能已覆盖）；没有则不新增——本接口本来就是全表聚合。

#### B4 图谱构建 `kg_builds`

```sql
SELECT created_by AS k, COUNT(*) AS c FROM kg_build_jobs WHERE created_by <> '' GROUP BY created_by
```

所有状态都算（与 `questions` 含失败/取消同一口径）。`kg_build_jobs` 不在 `retained_user_activity`
覆盖范围内，删库后的构建记录随库消失，接受。

#### B5 失败数 `questions_failed` / `reports_failed`

```sql
SELECT created_by AS k, COUNT(*) AS c FROM ask_jobs WHERE status = 'failed' GROUP BY created_by
UNION ALL  -- retained: activity_type='ask' AND status='failed'
SELECT created_by AS k, COUNT(*) AS c FROM reports  WHERE status = 'failed' GROUP BY created_by
UNION ALL  -- retained: activity_type='report' AND status='failed'
```

`cancelled` 不算失败。在展开区摘要里与提问/报告总数并列显示（如「提问 12（失败 2）」；失败为 0 时省略括号）。

#### B6 呈现：展开区「用户摘要」

主表列保持现状。点开某行的展开按钮后，在现有笔记本明细表**之上**新增一块「用户摘要」，由
`list_user_usage` 已返回的字段直接渲染，不额外请求：

```
最近上线 2026-09-07 10:32 · 存储 1.8 GB · 近 30 天提问 37 · 提问 120（失败 3）· 报告 8（失败 1）· 图谱整理 5
记忆 14 · Knowhow 表 2 · 加入的共享库 3 · 群组 1
```

- 一行放使用强度（B1–B5），一行放采纳度与协作（Phase C）。
- 界面用词以 `docs/ui-vocabulary.md` 为准：Memory 显示「记忆」，KG 构建显示「图谱整理」（与主界面「整理知识图谱」同词），Knowhow 保留原词。
- `last_seen` 为空显示「—」；字节用 KB/MB/GB，1024 进制，保留一位小数，进位在四舍五入之后判断（不得出现「1024.0 KB」）；小于 1 KB 显示整数字节。
- 主表排序键不新增；不要求按新信号排序（已拍板：不加主表列即不加排序入口）。

### Phase C — 采纳度与协作（与 B 同批，展开区第二行）

- `memory_count`：`memory_items` 按 `created_by`，`status <> 'rejected'`。
- `knowhow_tables`：`knowhow_tables` 按 `created_by`。
- `joined_notebooks`：`notebook_members` 按 `user_id`（他人库的成员身份）。
- `groups`：`group_members` 按 `user_id`。

呈现见 B6 第二行；不进主表。

## 4. 数据 / 接口变更（汇总）

| 层 | 变更 |
| --- | --- |
| PG `query_store.list_user_usage` | 改 sources 聚合；新增 last_seen / storage_bytes / questions_30d / kg_builds / questions_failed / reports_failed（Phase C 四项按决策） |
| SQLite `query_store.list_user_usage` | 同上镜像；时间窗口与 MAX 用 `_absolute_instant` |
| 迁移（B1） | PG `00xx_users_last_seen_at.sql`；SQLite v72；`identity_store` touch 路径双写 |
| `models/admin.py` `AdminUserUsage` | 新字段全部带默认值（`None` / `0`） |
| `frontend/app/admin/usage/api.ts` | 类型同步 |
| `frontend/app/admin/usage/format.ts` | 新增 `formatBytes`；不新增 sort key |
| `frontend/app/admin/usage/page.tsx` | 展开区新增「用户摘要」两行；主表不变 |
| `docs/product-and-api.md` / `_zh.md` | 用户总览段落：来源数新口径、展开区各信号定义、`last_seen` 与 `last_active` 的区别 |
| `docs/development*.md` | schema 版本提升到 72 的说明 |

## 5. 测试

后端（两侧都跑，PG 为准）：
- `backend/tests/test_admin_users.py`：改 `sources` 断言（Memory/Knowhow 行不计、共享库上传记提交者、
  copying 库不计、deep-copy 副本记接收方）；新增各新字段用例，含 retained 分支和 30 天窗口边界
  （窗口内外各一条、恰在边界一条）。
- `backend/tests/postgres/`：新增或扩展 conformance 用例，按 `test_query_store_activity_conformance.py`
  的 `postgres_integration` 形态，与 SQLite 共用 `tests/activity_parity_cases.py` 风格的 seed。
- B1：`identity_store` touch 节流下 `users.last_seen_at` 与 `auth_sessions.last_seen_at`
  同步；登出后该列保留。
- 本机 PG 跑法见 `docs/development_zh.md`（一次性库、`-n 1`）。

前端：
- `frontend/tests/component/admin-usage-page.component.test.tsx`：展开后摘要两行渲染、失败数括号显隐、
  `last_seen` 空值显示「—」、字节格式化；主表列数与排序键不变。

## 6. 非目标

- 不复活 deprecated 的 `conversations`。
- 不给 KG 审核、概念白名单、知识合并这类低频编辑动作计数。
- 不改 `last_active` 口径（已有产品文档与测试锁定）。
- 不做时间窗口可配置、不做图表。

## 7. 决策记录（2026-09-07）

1. **`last_seen` 落点**：采纳推荐——加 `users.last_seen_at`，与会话 touch 同事务、同 300s 节流写入，
   登出后保留。备选（只聚合 `auth_sessions`）不采用。
2. **来源数 / 存储的 NULL 归因**：采纳推荐——`COALESCE(uploaded_by, nb.created_by)`，深拷贝副本记接收方
   owner；`last_active` 保持副本不刷新任何人。
3. **呈现位置**：用户明确主表**不加列**；Phase B 与 Phase C 全部放展开区「用户摘要」两行（§3 B6）。

## 8. 延后单独立项

模型调用次数与 token 消耗：`llm_logging` 已按用户分目录写 `llm.jsonl`（kind / model / latency_ms /
usage），但未入库。需新增按用户按日聚合表并在写日志时同步落库，涉及写路径热点，独立 spec。
