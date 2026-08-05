# 用户日志页多维度改版设计

状态：设计稿，未实现。
范围：`frontend/app/dev/logs/`（页面）、`backend/app/api/debug_logs.py`（日志读取）、
`backend/app/api/admin_routes.py` + `backend/app/repositories/{sqlite,postgres}/query_store.py`
（新增 admin 只读查询）、`backend/app/core/event_logging.py`（P2 的上下文透传）。

---

## 1. 问题与定位变化

现在的 `/dev/logs` 是「某用户某天的 LLM 调用流水」：一条模型调用一行，左列表右详情，
按 `kind`/`status`/`model` 过滤。它回答的问题是**「模型调用出了什么问题」**。

要回答的新问题是**「这个用户在做什么」**——用了哪些笔记本、传了什么文件、问了什么、
系统怎么答的、其中哪一步慢或错了。所以页面定位从「调用流水」升级为「用户活动」，
调用流水降级为其中一个视图。

---

## 2. 数据现状（决定了什么能做、什么要先埋点）

以下为 2026-08-04 对本机 `.local/logs` 全量日志（3 个用户目录、4614 条 llm 记录、
26 种 events kind）的实测结论，不是推测：

**`llm.jsonl` 全部记录的字段只有**
`ts / id / kind / model / request / response / status / latency_ms / channel /
support_id / usage / error / attempt`。
**没有 `notebook_id`，没有 `conversation_id`，没有 `source_id`。**
全部 4614 条 `kind` 均为 `chat`，其中 2580 条带 `support_id`。

**`events.jsonl` 里带 `notebook_id` 的 kind**：`scale_index_build`(1090)、`ask_stage`(816)、
`communities_rebuilt`(226)、`scale_ppr_done`(108)、`pipeline`(76，另带 `source_id`)、
`paper_meta`(15) 等。**`status`(50) 带 `source_id` 但无 `notebook_id`。**
**全库任何 channel、任何 kind 都没有 `conversation_id`。**

**关键的断链**：llm 记录与 events 之间唯一的关联键是 `support_id`，而携带 `support_id` 的
`model_scheduler` 事件（2263 条）**恰恰没有 `notebook_id`**。所以今天从一条 LLM 调用出发，
无法确定它属于哪个笔记本、哪场对话。

### 2.1 真源分工（本设计的核心原则）

**名字与内容一律从数据库实时取，日志里只放 id。**

理由有二：名字会改（笔记本改名、来源重命名），日志里存一份副本就是第二个真源、必然陈旧；
而历史日志无法回填，数据库里的笔记本 / 来源 / 对话却是完整的、历史全在。

| 维度 | 真源 | 历史数据完整性 |
| --- | --- | --- |
| 笔记本名 | `notebooks.name` | 完整 |
| 来源名 | `sources.title` / `file_name`，经 `app/services/source_display.py::source_display_title` | 完整 |
| 对话 | `ask_jobs`（提问原文/模式/状态/`asked_at`）+ `answers`（正文/引用）+ `ask_trace_steps`（轨迹） | 完整 |
| 报告 | `reports` | 完整 |
| 模型调用 | `llm.jsonl` | 完整，但**无笔记本/对话归属** |
| 检索与解析阶段耗时 | `events.jsonl`（`ask_stage` 有 `notebook_id`，`pipeline` 有 `source_id`） | 部分有归属 |

因此**唯一需要新增写侧埋点的只有一件事**：LLM 调用 → 笔记本 / 对话 的归属。其余三个维度
（笔记本名、来源名、对话）今天就能从数据库完整取到，不依赖任何埋点。

---

## 3. 页面设计

顶部沿用现有的范围条（`当前查看: <用户> ▾`、`<日期> ▾`、`☑ 自动刷新`），下加视图 tab：
**活动**（默认）| **模型调用**。两个视图共享范围条状态。

### 3.1 「活动」视图（新增，默认）

三栏：左「范围」、中「活动流」、右「详情」。

**左栏 · 范围**
- 顶层是该用户的笔记本列表，复用既有的 `GET /admin/users/{id}/notebooks`
  （已返回 `name / status / sources / questions / reports / created_at / updated_at`）。
- 展开一个笔记本 → 该库的来源清单（显示名 + 解析状态徽章）。
  显示名**必须** import `source_display_title`（论文标题优先），不得另写一份——
  CLAUDE.md 红线：所有为用户命名来源的路径共用同一份实现。
- 点笔记本 = 中栏过滤到该库；点来源 = 中栏过滤到与该来源相关的活动。
- 计数只用界面词「来源 / 对话 / 报告」，不得出现 `chunk`/`KG`/`schema` 等内部词
  （`scripts/check_ui_vocabulary.py` 是硬门）。

**中栏 · 活动流**（时间倒序的混合流，四类条目）

| 类型 | 数据来源 | 行上显示 |
| --- | --- | --- |
| 提问 | `ask_jobs` + `answers` | 提问（截断）· 模式 · 状态 · 耗时 · 引用数 ·「N 次模型调用」 |
| 来源 | `sources` + `pipeline`/`status` 事件 | 显示名 · 类型 · 解析状态 · 元素数 · 异常小字 |
| 报告 | `reports` | 标题 · 深度 · 状态 · 总耗时 |
| 模型调用 | `llm.jsonl` | 默认折叠在触发它的提问下；无归属的单独成行、标「未关联」 |

约束：
- 来源行的异常小字**必须**走 `AnomalyBadge` + `sourceAnomalies()`，不得手搓内联样式或裸 `⚠`
  （回归门 `frontend/app/anomaly-guard.test.mjs`）。
- 报告行的耗时按红线规则：以 `generation_started_at → updated_at` 计算，
  旧报告缺开始戳时不编造耗时；未完成报告只显示创建时间。
- 时间一律显示浏览器本地时区，沿用 Ask 提问时间的既有格式规则
  （今天只显示时间；本周其他日期显示星期+时间；超出本周显示日期+时间；今年省略年份）。

**右栏 · 详情**（随选中项切换）
- 选中提问 → 提问原文 + 提问时间 + 答案正文（Markdown）+ 引用卡 + 推理轨迹步
  （复用既有 `ask-intent-trace` 渲染）+ 该次问答触发的模型调用列表。
- 选中来源 → 来源信息、解析状态、诊断、元素数、异常提示。来源详情**必须有界加载**
  （红线：一页 element，默认 40、单请求最多 100），不得一次性拉整篇大文档。
- 选中模型调用 → **原样复用现有的 `LogDetail`**（prompt/response transcript + 复制按钮）。

### 3.2 「模型调用」视图（现状保留）

现有页面一字不改地保留：列表 + 详情 + `kind`/`status`/`model` facet + 全文搜索 +
自动刷新 + 按天/按用户。P2 埋点上线后额外加一个笔记本过滤下拉（只对有归属的新日志生效）。
这样现有排障流程零回归。

---

## 4. 后端改动

### 4.1 新增 admin 只读查询（`query_store.py`，SQLite / PostgreSQL 各一份）

1. `list_user_notebook_sources(user_id, notebook_id, *, cursor, limit)`
   —— 来源清单，`C` collation 的 keyset 分页。大库红线：不得一次拉全。
2. 活动流：**不做三表 UNION**。`ask_jobs` / `sources` / `reports` 各自 keyset 分页、
   各自有界，前端按时间归并。理由：UNION ALL + 全局 ORDER BY 在大库上会退化成排序爆炸，
   而三份各自有界 + 内存归并的边界是可诚实回报的（「本页覆盖到 HH:MM 为止」）。
3. `get_user_ask_detail(job_id)` —— 提问 + 答案 + `ask_trace_steps`。

### 4.2 新增端点（`admin_routes.py`）

- `GET /admin/users/{user_id}/notebooks/{notebook_id}/sources`
- `GET /admin/users/{user_id}/activity?notebook_id=&since=&until=&cursor=&limit=`
- `GET /admin/users/{user_id}/asks/{job_id}`

权限口径与现有 `debug_logs._resolve_owner` 一致：`user_id == user.id` 放行（普通用户看自己），
否则要求 `user.role == "admin"`，失败走 `user_error(403, "...")`（红线：中文用户文案必须经
`user_error()` 打 `X-User-Message` 头，前端翻译只在 `frontend/app/errors.ts`）。
新端点必须跑架构守卫的默认模式刷 `api_contract`。

### 4.3 日志上下文透传（P2，唯一的写侧改动）

照 `_log_owner` 的模式，在 `app/core/event_logging.py` 加：

```python
_log_context: ContextVar[dict | None] = ContextVar("log_context", default=None)
```

在三处入口设置，由 `EventLogger.emit` 与 `LLMInteractionLogger.log` 落进记录：

| 入口 | 写入字段 |
| --- | --- |
| `/ask/stream`、`/ask` job 执行 | `notebook_id`、`conversation_id`、`ask_job_id` |
| 报告生成 | `notebook_id`、`report_id` |
| 来源摄取 | `notebook_id`、`source_id` |

后台 job 经 `contextvars.copy_context()` 天然带上（与 `_log_owner` 同一机制）。
写入是 best-effort 的，日志失败绝不能影响主流程（`EventLogger` 既有语义）。

**明确记账**：历史 4614 条 llm 记录归不到笔记本上。它们在活动流里进「未关联」分组。
这不是缺陷，是上线前的数据本就没有这个字段——页面必须如实这么标，不得用启发式猜测归属。

---

## 5. 隐私与权限边界

- admin **今天已经**能读别人的 `llm.jsonl`，其中就是完整的 prompt 与答案原文。因此在活动流里
  显示对话正文**没有扩大**暴露面，只是把同样的内容组织得可读。
- 私有 Memory **不进**这个页面。红线：Memory 按创建者私有，共享笔记本里别人的 Memory
  在任何通道都不得可见。
- `list_user_notebooks` 用 `created_by`，只列该用户自己建的笔记本。他在别人共享库里的提问
  计入 admin 总览的「用户总数」但不在这里分解——这与现有 admin 总览的口径**完全一致**
  （红线明写「展开清单刻意沿用 owner-only」），本设计不改动它。
- 参与库（挂载的参考库）的来源不在左栏列出；左栏只列该笔记本自己的来源。

---

## 6. 分期

**P1 — 不动写侧，历史数据立刻全部可见**
左栏范围树（笔记本 + 来源清单）+ 中栏活动流（提问 / 来源 / 报告三类，全部来自数据库）+
右栏详情（完整问答 + 推理轨迹）+ 视图 tab。零埋点依赖。

**P2 — 写侧埋点**
`_log_context` 透传 → 模型调用挂到触发它的提问下面；「模型调用」视图加笔记本过滤下拉。

**P3 — 可选**
把 `events.jsonl` 的 `ask_stage` / `pipeline` 阶段耗时挂进提问 / 来源详情。
`notebook_id` / `source_id` 现在就有，不需要埋点。

---

## 7. 前端硬约束清单（实现时逐条核对）

- 视图 tab、笔记本下拉等控件与现有 `logview-*` 样式体系对齐，不新造一套。
- 长任务按钮（若引入「重新解析」等入口）必须立刻置忙并换进行态文案，
  回归门 `frontend/app/long-task-button-guard.test.mjs`。
- 所有面向用户的文案只用界面词，硬门 `scripts/check_ui_vocabulary.py`。
- 若新增居中浮动弹窗，复用 `frontend/app/use-floating-window.ts`，不另造拖动实现。
- 请求竞态：沿用现有页面已有的 generation ref + scope key 模式（切用户 / 切日期 / 切笔记本
  时旧响应不得接管），新加的笔记本维度要一并进 scope key。
