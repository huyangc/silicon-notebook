# 进行中动作的刷新韧性（In-Progress Action Resilience）

**日期**：2026-07-08
**状态**（2026-09-07 对账）：三个工作流均已合入 master。WS1 的 KG 在跑标志
（`NotebookSummary.kg_building`）、WS2a 的 `ask_jobs` 持久化 + `started` 事件 + 取消端点、
WS2b 的 `GET …/ask/jobs/{job_id}` 重连 + append-only 轨迹 + 前端接回均已上线；离开 / 刷新后的
接回契约由 PR #661 / #662 / #664 / #665（2026-09-02～03）收口。行为契约以
`docs/product-and-api*.md`、`architecture.md` 与 `fangan_done.md` 为准，下文保留为设计历史。
唯一未做的是 §6.3 标为可选的「待办中心露出问答进行中」，登记在 `fangan_todo.md`。
**范围**：三个独立 PR —— WS1（后台 job 刷新重连）、WS2a（ask 脱离连接跑到完成）、WS2b（重开会话实时接回）

---

## 1. 问题

当前 UI 上「持续进行中」的动作，其进行状态只活在前端本地 state 里。用户**刷新页面 / 切走 / 离开**后，前端把状态忘了；有的动作后端还在跑（前端却看不到），有的动作干脆被后端主动掐掉、成果丢失。

用户举的两个实例，恰好分属两个**本质不同**的类别：

- **「全部预审」** —— 后端 daemon 线程还在跑，只是前端刷新后不再显示进度。
- **「深挖推理」** —— 后端在客户端断连时**主动中止**计算，成果丢失，还在历史里留下空壳会话。

用户诉求（原话）：**「我想让用户离开后，仍然能够继续做完会话，在问答的所有场景中都应该要保证这个。」** 且确认要做到「回来能实时接着看它跑」（2b）。

## 2. 现状勘查（代码事实）

四条探查线的结论，均带 `file_path:line`。

### 2.1 两个类别

**A 类 —— 后台 job：离开后还在跑，前端失忆。** 后端是 `background_jobs.submit()` 起的 daemon 线程（`backend/app/services/background_jobs.py:38-66`，用 `contextvars.copy_context()` 传播 per-user 上下文），前端刷新它照跑；只有**后端重启**才会死。已有的重启兜底：`_reconcile_crashed_jobs`（`backend/app/services/sqlite_repository.py:1240-1247`）把残留 `running` 标成 `failed`（"中断:服务重启"）。

| 动作 | 后端「在跑」信号 | 前端失忆点 |
|---|---|---|
| 全部预审 | ✅ `merge_review_jobs` 表 + `GET …/merges/review-job` | 切库把 `reviewAllJob` 清成 null（`page.tsx:1160`），mount 不补查 |
| 检索索引构建 | ✅ `GET …/scale-index/status` 的 `building` 字段 | 切库拉了状态但不重新起轮询（`page.tsx:1077-1105`） |
| 图谱 Viz 索引 | ✅ `unified-graph` 的 `viz_building` 字段 | （规划期复核：`openKgView@2382` 每次已 `setVizBuilding(g.viz_building)`，刷新后重开面板即恢复——**实为已符合，WS1 不改**） |
| KG 重抽 | ❌ 只有 `kg_ready`，**没有干净的「正在重抽」标志** | `openNotebook` 无条件 `setBuildingKg(false)`（`page.tsx:1786`）；且轮询错用 `kg_ready` 停止（重抽已建库时恒真） |

**已经做对的参照模板**（B 类，刷新可恢复）：
- 深度报告：`report-view.tsx:507-575` —— mount 即 `listReports()`，有活跃报告就自动起 6s 轮询。
- 待办中心：`pending-center.tsx:32-100` —— REST 秒开 + SSE 流 + 退避重连。

WS1 就是把 A 类四项补齐成 B 类的「mount 补查 → 若在跑就续上」。

### 2.2 ask 全模式共享一个 choke point

三种模式全走**同一条**路径：

- 前端唯一提交：`runAsk`（`page.tsx:2082-2124`）→ `readAskStream(…, "/notebooks/{id}/ask/stream", …)`（硬编码流式端点 `page.tsx:2102`），`mode` 参数区分 chunk/reasoning/graph。`readAskStream` 在 `page.tsx:542-620`。中止靠 `askAbortRef`（AbortController）关闭连接。
- 后端唯一入口：`POST …/ask/stream` → `_stream_ask_events()`（`backend/app/api/routes.py:592-668`）。worker 线程跑 handler（`:609-633`）；主循环 `await request.is_disconnected()` → `cancel_event.set()` → `break`（`:639-641`），`finally` 再 `set()`（`:650`）。
- 三个 handler 同一时序：`_ensure_conversation`（开头，`sqlite_repository.py:11310 / 12096 / 12237`）→ 计算 → `raise_if_cancelled(cancel_event)`（`:11488 / 12205 / 12598`）→ `_save_answer`（结尾，`:11489 / 12206 / 12599`）。
- 模式表：`ASK_MODES`（`backend/app/services/ask_modes.py:34-38`）。`_ensure_conversation` 在 `:12668-12694`，`_save_answer` 在 `:12638-12666`。

**后果**：断连 → `raise_if_cancelled` 抛 `AskCancelled` → `_save_answer` 不执行 → 答案丢失，但 conversation 已建 → 历史里留空壳。三模式皆然。

**红利**：一个 choke point（`_stream_ask_events` 的取消逻辑 + 答案落库时机）覆盖全部模式，正合「所有场景都要保证」。现有非流式 `POST …/ask`（`routes.py:566-574`）前端未用，可作 dispatcher 参照但不改行为。

## 3. 设计原则

**前端永远如实反映「服务端此刻真在干什么」，刷新/离开不改变这一点。** 具体到两条工作流：

- A 类动作：后端本就有真相，前端 mount 时**补查并接回**。
- ask：把它从「绑在连接上、断连即掐」升级成「像深度报告那样的持久化后台任务」，离开照跑到完、重开可实时接回。

三个 PR 顺序交付：**WS1 → WS2a → WS2b**，各自独立可验证。

---

## 4. WS1 —— 后台 job 刷新重连（四项）

一套统一的「mount 补查 → 若在跑就续上进度」。**纯读现有轮询/状态端点，不新增 LLM/embed/DB 重活**（符合 [[efficiency-first-mandate]]）。

### 4.1 三项零后端改动

对每项，把「切库/mount 无条件复位」改成「拉后端状态，若在跑则置 running 标志 → 现有轮询 effect 自动重新起转」：

- **全部预审**：`openNotebook` / 打开 KG 视图时 `fetchMergeReviewJob(nb)`；`status==="running"` → `setReviewAllJob(job) + setReviewAllRunning(true)`。删掉 `page.tsx:1160` 的无脑清空，改为补查。
- **检索索引**：已有 `page.tsx:1077-1083` 在切库时 `fetchScaleIndexStatus()`；补一步：若 `building` → `setBuildingScaleIndex(true)`，让 `:1084-1105` 轮询重新起转。
- **Viz 索引**：打开 KG 视图时若 `unified-graph.viz_building` → `setVizBuilding(true)`，让 `:1110-1128` 轮询起转。

### 4.2 KG 重抽 —— 补一个轻量后端「在跑」标志（内存 set，非表）

后端**没有**KG 重抽的 running 真相源。规划期发现已有**内存 set** 先例 `self._viz_building`（`sqlite_repository.py:383-384`，`set()` + `Lock`，`unified_kg_status` 读它得 `viz_building`）——照抄它加 `self._kg_building`，**免建表、免迁移**：

- `__init__` 加 `self._kg_building: set = set()` + `self._kg_building_lock`（紧邻 `_viz_building`）。
- `build_notebook_kg`（`:2919`）入口 `add`、`finally` `discard`（`rebuild`=`delete+build`，长耗时在 build，一处埋点即覆盖重抽）。
- 暴露：`get_notebook`（`:1837`）回填 `NotebookSummary.kg_building = notebook_id in self._kg_building`——前端 KG 构建轮询本就每 6s 打 `/notebooks/{nb}`（`page.tsx:1054-1074`），读这个字段**零新增请求**。
- 前端：`openNotebook`（`:1786`）不再无条件 `setBuildingKg(false)`，改按 `notebook.kg_building` 置位；轮询**停止条件从 `kg_ready` 改看 `kg_building`**（重抽已建库时 `kg_ready` 恒真、会一进来就误停）。

**为何内存 set 优于表**：重启后 daemon 线程已死、集合天然为空=「未构建」——这本就是诚实态，无需 `_recover_interrupted_jobs`、无需 schema 迁移、无 `SCHEMA_VERSION` bump，风险与代价都更低。代价仅是「不显示 KG 专属的『中断:服务重启』」——但重启后它确实已不在构建，显示「未构建」即诚实。（`merge_review_jobs` 用表是因为它另需解锁单飞守卫并显式报中断；KG 无此需求。）

### 4.3 交付

WS1 = 一个 PR（三个 commit，见 `docs/superpowers/plans/2026-07-08-ws1-background-job-resume.md`）。改动：前端三处（reviewAll 再查询 / scaleIndex mount 接回 / KG 停止条件+openNotebook 置位）+ 一个纯判定模块 + 后端一个内存 set/一个字段/一处回填。**无 schema 迁移**。viz 已符合、不改。

---

## 5. WS2a —— ask 脱离连接、跑到完成

核心：**worker 生命周期与请求连接解耦**。就一个 choke point。

### 5.1 行为改动

1. **断连不再取消 worker**：`_stream_ask_events` 主循环里，客户端断连时**停止 yield（结束本次流）但不 `cancel_event.set()`**（改 `routes.py:639-641`）。⚠**同时**要处理 `finally: cancel_event.set()`（`routes.py:650`）——它当前在生成器退出（含断连退出）时也会取消 worker，必须改成**仅在显式取消路径下才 set**（否则断连经 finally 仍把 worker 掐掉，前面白改）。worker 变成真正脱离连接：继续跑到完，`_save_answer` 照常执行 → **答案必存、不再留空壳**。断连后 worker 仍会 `events.put()` 到无人消费的 queue，无害（有界，且 WS2b 起 worker 另落 `ask_jobs`）。daemon 线程（`routes.py:633`）本就随进程存活、不随请求结束，故离开后照跑。
2. **显式取消要有独立通道**（必须配套，否则「停止」失效）：新增 `POST …/ask/jobs/{jobId}/cancel` → 置该 worker 的 cancel_event → 抛 `AskCancelled` → `status='cancelled'`。前端「停止」按钮改打这个端点，**不再**靠关闭 fetch 实现取消（离开≠停止）。
   - 需要一个**进程内 registry** `{jobId: cancel_event}`（仅活跃 worker；重启即失，故也需 reconcile）。
3. **答案落库保底**：`_save_answer` 时机维持在末尾即可（不取消 → 必达）。为稳妥，可在 worker `finally` 中对「已算出 response 但未存」的情形补存。
4. **重启 reconcile**：新表 `ask_jobs` 的残留 `running` 启动时标 `interrupted`("中断:服务重启")，前端如实显示 + 提供「重试」。

### 5.2 ask_jobs 表

一行一个 ask turn（也是 2b 的持久化载体）：

```
ask_jobs(
  id TEXT PRIMARY KEY,            -- job/turn id，前端用它 cancel/reconnect
  notebook_id TEXT REFERENCES notebooks(id) ON DELETE CASCADE,
  conversation_id TEXT,
  created_by TEXT,               -- 严格按 user.id 归属（见 [[per-user-logs-state]] 的 id≠username 坑）
  mode TEXT,                     -- chunk | reasoning | graph
  question TEXT,
  status TEXT,                   -- running | done | failed | cancelled | interrupted
  trace_json TEXT DEFAULT '',    -- 累积的进行中推理轨迹（2b 用；chunk/graph 可空）
  answer_id TEXT DEFAULT '',     -- done 时回填，指向 answers 表
  error TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT
)
```

- ask 提交：insert `running`。
- 完成：`status='done'` + `answer_id`。
- 显式取消：`status='cancelled'`。
- 同样追加 `_migration_N` + bump `SCHEMA_VERSION`。

### 5.3 jobId 下发

保持单一 `POST …/ask/stream`，但流的**第一行事件**改为 `{"event":"started","job_id":"…","conversation_id":"…"}`，前端据此拿到 jobId（供「停止」与后续重连）与已持久化的 conversationId（供首轮回答前即时入历史和重新打开）。其余 `progress` / `final` / `error` 事件不变。

`started` 的历史发布按“当前是否仍在同一 notebook”判断，不按旧 run 是否仍占有回答区判断；因此用户在首行抵达前切到同库旧会话，也不会遗失新会话。切走后的 success/failure/cancel 终态仍独立刷新当前 notebook 的摘要；列表请求 generation 也是 notebook 级，同库会话 epoch 变化不丢弃有效响应，其他 notebook 的延迟调用则在发请求/递增 generation 前被拒绝。若用户在首行抵达前点击「停止」，界面立即恢复草稿，但客户端不能立刻 abort transport（那会永远丢掉 jobId）；它按 run 记录 controller，读到 `started` 后先调用取消端点，再停止该 run 的本地流。重连会话只持有 job id、不复用旧 stream controller，避免多在途会话取消串台。

### 5.4 空壳会话策略

- 断连不再取消 → 正常完成 → 无空壳（问题自然消失）。
- **显式取消** 且该会话此前无任何答案（即新开会话首轮被取消）→ 连同空 conversation 一并清理，避免留下 0-turn 空壳。否则仅把该 turn 记为 cancelled。

### 5.5 成本与逃生口（[[efficiency-first-mandate]]）

不新增任何 LLM 调用；多花的仅是「原本会被中途掐掉的 ask 现在跑到完」——正是用户要的那部分。逃生口 = 显式「停止」。**可选**再加 per-user 在途 ask 并发上限（默认不设硬顶，先观察）。此点在 spec 评审时定。

### 5.6 交付

WS2a = 一个 PR。改 `_stream_ask_events` 取消语义 + cancel 端点 + registry + `ask_jobs` 表 + reconcile + started 事件 + 前端「停止」改造。

---

## 6. WS2b —— 重开会话实时接回

让「重开一个还在跑的会话」能**回放已发生的轨迹 + 继续看它实时跑到完**。

### 6.1 持久化轨迹

worker 每产出一个 trace step（reasoning 的 `on_trace` 回调，`sqlite_repository.py:12124-12125）` 就**追加写入** `ask_jobs.trace_json`（或独立 `ask_trace_steps` 表，二选一，spec 评审定；倾向 trace_json 单列，简单）。chunk/graph 无逐步轨迹，仅 status。

### 6.2 双路径流

- **首连（用户在场）**：维持现状——worker 内存 queue 的 push 式低延迟流。仅去掉「断连即取消」。
- **重连（用户回来）**：新的**poll 式**视图（照抄深度报告详情轮询 `report-view.tsx:556-575` 的成熟形制）：
  - `GET …/ask/jobs/{jobId}`：返回 `status` + 已持久化的 `trace`（回放）+（done 时）answer。
  - 前端对 `running` 的 job 每 1–2s 轮询该端点，increment 渲染新 step，直到终态。终态即拉 `answers` 里的最终答案。
  - poll 式天然多观察者安全（只读），多标签页/多次重开都 OK。

### 6.3 前端重连 UX

- `openSession`（`page.tsx:2143-2154`）拉 `ConversationDetail` 时，若某 turn 有在途 `ask_jobs`（`status='running'`），把它渲染成「生成中」并挂上 6.2 的重连轮询 → 就地完成。
- 会话详情读取（`list_conversations` / `ConversationDetail` 构建，`sqlite_repository.py:12716-12786`）需 LEFT JOIN `ask_jobs`，让在途 turn 也出现在 turns 里（当前只从 `answers` 读，在途 turn 无 answer 会漏）。
- **可选**：待办中心铃铛露出「问答进行中」（`pending-center` + `/me/pending-actions` 聚合，`routes.py:1339 / repo:12934-13031`），呼应用户「想看到正在进行的动作」的心智。此项 spec 评审定去留。

### 6.4 交付

WS2b = 一个 PR。轨迹持久化 + `GET …/ask/jobs/{jobId}` + 会话详情 JOIN + 前端重连轮询（+ 可选铃铛）。

---

## 7. API 变更汇总

| 端点 | 工作流 | 说明 |
|---|---|---|
| `POST …/ask/stream` | WS2a | 断连不取消；首行发 `started` + `job_id` + `conversation_id` |
| `POST …/ask/jobs/{jobId}/cancel` | WS2a | 显式取消（新） |
| `GET …/ask/jobs/{jobId}` | WS2b | 状态 + 回放轨迹 + 终态答案（新） |
| `GET …/notebooks/{id}` | WS1 | 增 `kg_building` 字段（`get_notebook` 从进程内 `_kg_building` set 回填；无新端点） |
| `GET …/conversations/{id}` | WS2b | 详情含在途 turn（JOIN ask_jobs） |

## 8. 数据模型变更

- **WS1**：无表变更、无迁移——KG 用进程内 `self._kg_building` set（仿 `self._viz_building`）。
- **WS2a**：新表 `ask_jobs`（WS2b 复用其 `trace_json`），追加 `_migration_N` + bump `SCHEMA_VERSION`（[[schema-migration-convention]]），并入 `_recover_interrupted_jobs` 重启兜底（残留 `running` → `interrupted`）。

## 9. 错误处理与边界

- **后端重启**：WS1 无表——`_kg_building` 内存 set 天然清空=未构建（诚实态，无需 KG 专属 reconcile；经 `background_jobs.submit` 提交的那次 job 另由现有 `_recover_interrupted_jobs` 标记）。WS2a 的 `ask_jobs` 残留 running → `interrupted`/"中断:服务重启"；前端如实显示 + 重试。
- **会话被删**：`ON DELETE CASCADE` 清 `ask_jobs`；worker 落库遇会话不存在需 fail-open 不崩。
- **显式取消竞态**：worker 已 done 后再取消 → no-op。
- **多标签/多次重开**：重连走只读 poll，天然安全。
- **per-user 归属**：`ask_jobs.created_by`、reconnect/cancel 端点、会话 JOIN 一律按 `user.id` scope（重申 [[per-user-logs-state]] 的 id≠username 坑）。
- **contextvars**：worker 继续沿用 `background_jobs.submit` 的 `copy_context`，保 per-user 模型不回退（[[per-user-model-config-state]]）。

## 10. 测试计划（TDD，红→绿）

**后端 pytest**：
- `test_ask_stream_cancel.py` 反转断连语义：断连后 worker 跑完、答案入库（现测断言需改）。
- 新增：显式 cancel → 答案不存、`status='cancelled'`、空首轮会话被清理。
- `ask_jobs` 生命周期：running→done / running→cancelled / 重启 reconcile running→interrupted。
- 重连：`GET …/ask/jobs/{id}` 回放已持久化轨迹；轮询至 final。
- WS1：`_kg_building` set 在 `build_notebook_kg` **与** `rebuild_notebook_kg` 全程置位/清位（覆盖重抽的 delete 阶段，防大库 delete>6s 时前端轮询过早停）；`get_notebook.kg_building` 回填。

**前端**（纯逻辑 `.ts` + `.test.mjs`，`node --test`，见 [[frontend-backend-co-design]] 前后端同步）：
- WS1 纯模块 `in-progress-resume.ts` 的四个谓词（`shouldResumeReviewAll`/`shouldResumeScaleIndex`/`shouldResumeKgBuild` + `kgBuildFinished`）→ 是否接回轮询 + KG 轮询停止条件（改看 `kg_building` 而非 `kg_ready`）。
- WS2b 一个纯函数：给定 `ConversationDetail`（含在途 turn）→ 产出「哪些 turn 需重连、从第几步续」的 resume 描述子。
- DOM 耦合部分保持薄。

## 11. 分期与非目标

- **PR 顺序**：WS1（快赢、零成本）→ WS2a（兑现「离开仍做完」的保证，成本行为变更集中在此）→ WS2b（实时接回）。
- **非目标**：**不**做通用 `background_jobs` 一级任务表的大一统重构（诱人但风险大、YAGNI）。本设计沿用「feature-local 状态表」既有形制（`merge_review_jobs` 样板），把统一留给将来。
- **不**触碰非流式 `POST …/ask` 的现有行为（前端未用）。

## 12. 开放问题（spec 评审时定）

1. WS2a 是否加 per-user 在途 ask 并发上限？默认不加。
2. WS2b 轨迹持久化用 `ask_jobs.trace_json` 单列，还是独立 `ask_trace_steps` 表？倾向单列。
3. WS2b 是否顺带在铃铛露出「问答进行中」？倾向做，可拆到后续。
