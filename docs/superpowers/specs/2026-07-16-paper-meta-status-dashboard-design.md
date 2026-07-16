# 论文元数据补抽状态化 + 看板总览 — 设计

日期：2026-07-16
分支：`claude/paper-meta-status-dashboard`
状态：定稿（待实现）
承接：PR#271（论文元数据抽取，见 spec `2026-07-15-paper-metadata-extraction-design.md`）

## 1. 背景与目标

PR#271 交付了论文元数据抽取 + 三通道补抽。真机使用暴露两个体验缺口：

1. **补抽按钮「点了没反应」**：`POST /paper-meta/backfill` 是 fire-and-forget——前端只在 HTTP 往返期间禁用按钮、弹一次 toast，之后无任何持久状态。补抽不改 `parse_status`，来源轮询（parse_status-only）不刷新，用户除非手动打开某篇详情，否则完全感知不到 job 做了事。诊断实据：08:50 的点击 POST 200、3 篇 8 秒全部入库、事件日志 `kind=paper_meta` 齐全——功能正常，纯反馈缺失。
2. **看板看不到整体状态**：无法一眼知道一个 notebook 里多少来源有论文元数据、多少缺失、多少非论文。

目标：把补抽做成**和「索引构建/KG 构建」一样的有状态长任务**（进行中/篇数/完成可见，且在头像旁铃铛「待确认中心」里出现），并在知识分析看板加一个论文元数据总览区块。

## 2. 非目标

- **不做「过时」检测**（用户 2026-07-16 拍板）。现状无零成本的诚实过时信号：`sources.updated_at > meta.updated_at` 近似法在本项目「上传→必跑 KG 抽取」流程下几乎全误报（KG 抽取在 meta 写入后继续 bump `updated_at`）；精确法需给 `source_paper_meta` 加 `source_file_hash` 快照列（+1 迁移）。两者都不做——看板只显示 有/缺/非论文 三态。将来确需再加。
- **不改机构展示**（用户 2026-07-16 拍板）：作者机构继续以详情里的 hover title 呈现，本次不动。机构数据 PR#271 已抽取入库（`source_authors.affiliation`）。
- **不改抽取/接地逻辑本身**：`ensure_paper_metadata`/`verify_paper_meta`/`backfill_paper_metadata` 的抽取与校验行为不变，只加状态可观测性。
- **不加新表、不加迁移**：全部走已有表的裸 GROUP BY + 进程内内存标志 + 响应模型加字段。
- **不做 backfill 篇数进 notebook 列表路径**：避免 `GET /notebooks` 每卡一次 COUNT 的规模回归（见 §5）。
- **铃铛不常驻 missing 待办**：缺失篇数在看板看（§4），铃铛只显示"补抽进行中"项，不对每个有缺失的 notebook 常驻提醒（避免噪音）。

## 3. 状态化设计（对齐 KG 构建 + 铃铛）

房内长任务（KG 构建、索引构建）的状态都不用 DB flag，而是**进程内内存结构标注"正在跑"，读取 notebook 摘要时暴露为布尔**——重启天然清空（未构建），无需 DB flag、无需 reconcile。补抽照抄，并接入铃铛「待确认中心」。

### 3.1 后端

- **进程内 building + 进度**：`SourceIngestionService` 加 `_paper_meta_backfilling: dict[str, dict]`（notebook_id → `{"total": N, "done": k}`）+ 锁，镜像 `knowledge_lifecycle.kg_building` 但携带进度。`backfill_paper_metadata(notebook_id, ...)` 方法体开头 `with lock: dict[nb]={"total":queued,"done":0}`；每源完成 `with lock: dict[nb]["done"]+=1`（复用 PR#271 已有的 counts 锁与内部进度点——那个 `progress` 回调此前埋了没消费，现接上供铃铛用，CLI 仍走同一回调打印）；`finally: with lock: dict.pop(nb, None)` + 发完成事件（见 §3.3）。单源随摄取的 `ensure_paper_metadata`（process_source 路径）**不进** dict——dict 只标注"批量补抽 job 在跑"。
  - 竞态：前端点击后乐观置 `backfillingMeta=true`；job 秒级完成时首个 tick 读到不在跑=判完成（正确）。与 `kg_building` 同款语义，无竞态缺陷。
- **暴露到摘要**：`NotebookSummary` 加 `paper_meta_backfilling: bool`（默认 False），在 notebook 读取路径按 `notebook_id in self._paper_meta_backfilling` 求值（O(1) dict membership，与 `kg_building` 同源零成本，安全带进列表路径）。**不加** `paper_meta_pending_sources` 到摘要（那是 DB COUNT，进列表路径即 N 卡回归）。

### 3.2 前端（`frontend/app/page.tsx`）

- 复用既有 `backfillingMeta` state（PR#271 已加）。点击「补全论文信息」→ `POST` 返回 `{queued}`：
  - `queued == 0` → toast「论文信息已是最新，无需补全」，不置 building。
  - `queued > 0` → `setBackfillingMeta(true)` + toast「已提交 N 篇论文的信息补全」，启动完成轮询。
- **完成轮询**：克隆 `buildingKg` 的轮询块（6s `setInterval`，`GET /notebooks/{nb}`，20 分钟安全上限）。判据 `backfillingMeta && !refreshed.paper_meta_backfilling` → `setBackfillingMeta(false)` + 刷新来源列表（`reloadSources`，使详情里的作者/机构立刻带出）。**完成 toast 不在这里弹**——统一交给铃铛的 done 事件（§3.3），避免同一完成弹两次；本轮询只负责复位按钮状态与刷新来源。
- **按钮**：`disabled={backfillingMeta}` + 标签 `补全中…` / `补全论文信息`（PR#271 已具备，完成检测从"仅 POST 往返"升级为"轮询到 job 结束"）。
- **跨会话 resume**：打开 notebook 时（select / 摘要加载）若 `paper_meta_backfilling` 为真，`setBackfillingMeta(true)`——与 `kg_building`、`scale_index` 的 resume 对称，刷新页面/换设备仍显示进行中。

### 3.3 铃铛集成（待确认中心，对齐索引构建）

补抽与索引构建对齐——铃铛里既有进行中待办项、完成又弹 done toast。房内两条既有机制直接复用（`pending_bus`）：

- **进行中待办项（snapshot item）**：`pending_actions_service.list_for_user(uid)` 新增一个来源——遍历该 user 拥有的 notebook，凡 `notebook_id in _paper_meta_backfilling` 者产出 `{"type":"paper_meta","state":"building","notebook_id","notebook_name","progress":{"done":k,"total":N}}`（进度取自 §3.1 内存 dict，零 DB）。per-user 过滤按 notebook owner。job 结束项自然消失（dict 已 pop）。**不常驻 missing 待办**（§2）——这是相对 index `suggested/stale` 的克制取舍，符合"铃铛聚合真正进行中/待办"的意图。
- **完成事件（done toast + 响铃）**：`backfill_paper_metadata` 的 `finally` 里，成功完成调 `pending_bus.emit(uid, {"event":"paper_meta_done","notebook_id","notebook_name","stored":N})` + `pending_bus.mark_dirty(uid)`——完全对照 `scale_artifact_runtime.notify_index_done` 的模板。同时 `POST /paper-meta/backfill` 的 `background_jobs.submit(...)` 补 `notify_pending=True`（快照刷新兜底）。
- **前端（`pending-center.tsx`）**：
  - `PendingItem.type` 封闭 union 加 `"paper_meta"`；加分组标题「论文元数据」与 item 渲染（`论文信息补全中 · k/N`，点击跳到该 notebook 的来源面板）。
  - `msg.event === "paper_meta_done"` 分支 → 复用/泛化既有 `DoneToast`，文案「论文信息补全完成 ✓ · 已补全 N 篇」。既有 `index_done` 的 DoneToast/PendingToast 文案是硬编码 index 专属——本次将其泛化为**按事件类型取文案的分派表**（`index_done`→索引、`paper_meta_done`→论文信息），index 分支行为逐字保持，而非再复制一份。
  - `uid` 归属：emit 用 job 所属 user（`background_jobs.submit` 已 `copy_context` 传播 ContextVar，与 buildkg 同款）。

## 4. 看板「论文元数据」总览

### 4.1 后端

- `NotebookAnalytics`（`schemas.py`）加 `paper_meta_counts: Dict[str, int]`，键 = `has_meta`（is_paper=1 的行数）、`marker`（is_paper=0，即已判定非论文）、`missing`（合规但无 meta 行）。
- 在 `QueryStore.notebook_analytics` 里算，紧挨现有 `source_status_counts`。**刻意不走 `kg_mutation_seq` 记忆化缓存**：论文元数据写入不 bump 该 seq，seq-keyed 缓存会读脏值（`knowledge_counts_cache` 文档已明确排除同类"不 bump seq"的计数）。改走裸的带索引 GROUP BY，与 `source_status_counts` 同款纪律（未缓存、覆盖索引、实测万级来源毫秒级）：
  - 有元数据 / 非论文标记：`SELECT is_paper, COUNT(*) FROM source_paper_meta WHERE notebook_id=? GROUP BY is_paper`（走 `idx_source_paper_meta_nb`）。
  - 缺失：`sources_missing_paper_meta` 的 COUNT 镜像——谓词 `source_type NOT IN ('memory','knowhow') AND doc_type IN ('','academic_paper') AND parse_status IN ('parsed','extracting','extracted') AND NOT EXISTS(meta 行)`（走 `idx_sources_nb_parse_status_type`）。
- 该计数只在 analytics 端点（用户打开看板才请求）计算，不进 `GET /notebooks` 列表路径。

### 4.2 前端

- `NotebookAnalytics` TS 类型（`workspace-model.ts`）加 `paper_meta_counts`。
- 知识分析看板弹窗「来源状态」区块后新增「论文元数据」区块，复用现有 `.tag` + 色调语法：
  - `有元数据 N`（ok 调）· `缺失 M`（M>0 warn 调，否则不显示或 muted）· `非论文 K`（muted 调）。
  - 全 0（无合规来源）时区块可整体隐藏或显示「暂无论文来源」。
- 文案友好、对齐精致（UI 质量基线）。

## 5. 效率账（一等约束）

- **零迁移、零新表、零新端点。**
- **列表路径零新增查询**：`paper_meta_backfilling` 是 O(1) 内存 dict membership；篇数计数只在按需的 analytics 端点。
- **看板计数**：两条覆盖索引 GROUP BY，仅打开看板时执行，与既有 `source_status_counts` 同量级。
- **完成轮询**：复用 KG 构建已有的 `GET /notebooks/{nb}` 6s 轮询路径，无新轮询端点；仅当前 notebook、仅补抽进行中期间。
- **铃铛**：待办项与进度全部读自 §3.1 内存 dict（零 DB）；完成事件复用既有 `pending_bus.emit`/`mark_dirty`，无新基建。

## 6. 前端触点清单（同 PR 交付）

1. `workspace-model.ts`：`NotebookSummary.paper_meta_backfilling?: boolean`；`NotebookAnalytics.paper_meta_counts`；`PendingItem` union 加 `paper_meta`。
2. `page.tsx`：完成轮询块（克隆 buildingKg，不弹 toast）；resume 钩子；看板「论文元数据」区块；按钮完成语义升级。
3. `pending-center.tsx`：`paper_meta` 待办项渲染 + 分组「论文元数据」+ 跳转；`paper_meta_done` done toast 分支；泛化既有 index 专属 DoneToast/PendingToast 文案为按事件类型取。
4. 中文文案沿用弯引号风格，不批量替换直引号（`git diff | grep -c '^-.*[“”]'` = 0）。
5. API 路径不带 `/api` 前缀（双前缀 404 坑）。

## 7. 测试计划

- **后端**：
  - `NotebookSummary.paper_meta_backfilling`：dict 有/无成员时布尔正确；`backfill_paper_metadata` 期间为真、结束（含异常路径）pop 为假。
  - `paper_meta_counts`：构造 has_meta/marker/missing/非合规（memory/knowhow）混合来源，断言三键计数精确；空库全 0。
  - 计数不误用 seq 缓存：meta 写入后立即查得新计数（无 stale）。
  - 铃铛待办项：补抽进行中 `list_for_user(owner)` 含 `type=paper_meta,state=building` 且 progress 反映内存 dict；per-user 过滤正确（非 owner 看不到）；job 结束项消失。
  - 完成事件：`backfill_paper_metadata` finally 成功路径 emit `paper_meta_done`（带 stored 计数）；异常/零队列路径的事件语义明确（零队列不 emit done）。
- **前端**：既有 tsc + 组件测试保持绿；新逻辑若可单测则补（轮询判据、resume、pending-center 的 paper_meta 分支与文案分派）。
- **契约/golden**：`NotebookSummary` + `NotebookAnalytics` 加字段 → api_contract.json regen（按先例流程，`source_commit` 不动）；surface-manifest / facade / callers-static 按测试输出对齐（新消费点）。
- **端到端冒烟**：扩展 PR#271 的 scratchpad 冒烟——补抽 job 期间 summary.paper_meta_backfilling=true、结束=false；analytics.paper_meta_counts 三态正确；铃铛 list_for_user 进行中含 paper_meta 项、完成 emit paper_meta_done。

## 8. 契约与迁移

- **无迁移**（SCHEMA_VERSION 不变，仍 17）。规避与提审中 #272（已 rebase 占 `_migration_18`/SCHEMA=18）的任何编号协调。
- api_contract.json / schema 无关（无表变更）；仅响应模型加字段 → api_contract openapi/serialization 两键按测试自身计算重算。

## 9. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 补抽 job 秒级完成，前端首个 tick 前已 pop | 前端乐观置 building；首 tick 读到 false = 判完成（正确）；与 kg_building 同款，无缺陷 |
| 进程重启期间有补抽在跑 → dict 丢失显示"未在跑" | 与 KG/索引同款权衡（进程内标志重启即清）；job 本身幂等，重启后用户可再点，已完成的源被 `missing` 排除不重抽 |
| analytics 计数在超大库变慢 | 两条覆盖索引 GROUP BY，与既有 source_status_counts 同款；若真机 profiling 要求再引入专用缓存（届时需自带 invalidate，不能用 kg_mutation_seq gate） |
| 响应模型加字段打破 golden | api_contract/surface-manifest 按既定 regen/allowlist 流程对齐，不弱化断言 |
| 进度 dict 并发写（backfill ≤8 worker） | 复用 PR#271 已有 counts 锁；`done+=1` 与 pop 均在锁内；读侧（摘要/铃铛）快照读一致即可 |
| 泛化 index 专属 done toast 文案破坏索引现状 | 泛化为按 `event` 取文案的分派表，index 分支行为逐字保持；测试覆盖 index_done 文案不变 |
| 铃铛 emit 的 uid 归属错（多用户） | 复用 buildkg 的 ContextVar 传播；测试断言非 owner 不收到该 notebook 的 paper_meta 项/事件 |
