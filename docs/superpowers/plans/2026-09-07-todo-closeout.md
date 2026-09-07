# 待办收尾计划（2026-09-07）

对账真源：根目录 `fangan_todo.md`（同日按代码重写）。本计划只覆盖**不需要产品拍板、规格已可由
代码与既有设计稿定死**的剩余项，按 PR 分批；每个任务落地后先做规格评审与代码质量评审，再进
下一项；每个 PR 走 codex 评审闭环后合入。数值契约只登记在 `docs/product-and-api*.md`。

## 不在本计划内（需要拍板或属多周特性，仍留在 `fangan_todo.md`）

Review Mode；企业能力（source 级 ACL / 审计日志 / SSO / Connectors / 全局搜索）；分享 edit 层与
近实时协作；自动用户记忆；Prompt per-notebook 定制与 self-evo；Agentic Memory 注入开闸；KG 节点
attrs 形态；gold 人工策展；推理分层（extends / Level 0–4 / Hypothesis）；schema 归纳新字段；refine
抽样比率（无测量依据）；BM25 / FTS5；结构化硬过滤；扫描件 OCR 与 OMML（引入新外部依赖，属部署
决策）；热路径各设计稿登记的残余债（「有真实需求再做」）；真模型合并质量 smoke 与 backfill-images
放量（用户侧）。

---

## PR-1 文档对账 + 小清理（本分支 `claude/codebase-pending-tasks-9e6532`）

已含提交 819eaabcd（fangan_todo 重写与三份设计稿状态头）。

- **T1 删除 `/notebook-templates` 死端点**：删 `system_routes.py` 路由、`notebook_templates.py`、
  `notebook_catalog.list_notebook_templates`、`repository_facade.list_notebook_templates`、
  `ports.py` 端口方法、`NotebookTemplate` 模型及 `schemas.py` 再导出；同步
  `ownership_manifest.py`、`test_route_domain_boundaries.py`、`test_notebook_summary_query.py`、
  `test_model_domain_boundaries.py`、`scripts/architecture_boundary_baseline.json`、
  `scripts/smoke_backend.py:545`；用 `scripts/generate_repository_contract_fixtures.py` 再生成
  `facade_surface.json` / `api_contract.json` / `legacy_schema_exports.json`；ports 棘轮基线同
  diff 下调（零松弛）。验收：全量 pytest 绿，OpenAPI 无 `/api/notebook-templates`。
- **T2 删 `repository_facade._citation()`** 零调用者死代码（先于 PR-2 的 A1 守卫，守卫基线不含它）。
- **T3 扩展 SDK `TODO(T7)`**：把 `deployment.py` docstring 里的 `configure` 成本规则（廉价、对
  settings 无副作用、可与 `settings_model` 一起省略）与 capability 命名规则（不得撞核心或其它插件
  的名字、每个声明名一探针）写进 `docs/deployment-extensions-sop.md` §3.2/§3.3 及 `_zh` 对照，
  删 TODO。验收：`test_architecture_documentation` 与 UI 词汇守卫绿。
- **T4 KG 抽取超时结案**：核实 `kg/run_control.py` 把 `kg_llm_timeout_seconds` 传给
  `TaskScopedKgClient` 后，流式请求下它是「总墙钟」还是「连接/读间隔」。若是总墙钟且流式已
  落地仍会截断长输出，则把默认值改为 150s 并在部署文档登记数值围栏；若已是读间隔语义，则在
  `fangan_todo.md` 以证据结案。
- **T5** `fangan_todo.md` 同步（本 PR 完成项移除；用户已完成的 T-0 / PG 调参 / 索引 apply 移除）。

## PR-2 多领域基准库合入后遗留 A1–A9

真源 `docs/superpowers/specs/2026-07-19-multi-domain-bases-followups.md`（含 2026-09-07 对账表）。

- **T1 A7 schema 上界守卫**：`sqlite/migrations.py::migrate()` 在 `user_version > SCHEMA_VERSION`
  时抛明确错误（含库版本、代码版本、提示回退代码或备份），PostgreSQL 迁移账本若无同款守卫一并
  补；`docs/development*.md` 写明 schema 单向性。测试：伪造更高 user_version 的库必须拒绝启动。
- **T2 A8 挂载谓词形状守卫**：仿 `test_access_sql_contract.py::test_owner_or_member_shape_lives_only_in_access_sql`，
  扫 `backend/app` 中 `tier = 'base'` 与 `created_by` 组合的 SQL 形状只允许出现在
  `mount_sql.py`；变异验证（复制一份谓词到别处守卫必须红）。
- **T3 A1 构造点守卫**：AST 扫 `backend/app` 里 `Citation(` / `AnswerAnchor(` 构造点，`notebook_id`
  实参必须来自登记的归一化 helper（现有的 `_normalize_notebook_id` 类函数，动手时核实真名）或
  在豁免清单（结构性安全的 memory 路径）逐条登记理由；基线 = 对账表列出的全部现有构造点。
- **T4 A2 graph BFS 锚点带来源库 id**：`build_rx_graph` 节点 payload 加 `notebook_id`，
  `render_subgraph_context` 写入 `evidence_by_id`；归一化走 T3 的 helper；删掉「暂未填」注释。
  测试：graph 模式跨库命中的引用带库名徽章，本库命中为空串。
- **T5 A6 深拷贝挂载边定性**：默认方案——深拷贝**携带**对接收者仍有效的挂载边
  （`MOUNT_VALID_EXPR` 以新 owner 求值：`tier='base'` 的公共库全部带走，私有库挂载只在同 owner
  拷贝时带走），无效边丢弃并计入拷贝结果；若实现期发现拷贝快照契约不允许（`_COPY_SNAPSHOT_QUERIES`
  的 shadow 停车约束），改为登记进「Deliberately absent」注释并加钉子测试。两种结果都要在
  `docs/product-and-api*.md` 登记。
- **T6 前端 A3 + A4 + A5**：晋升目标数据源统一到 `NotebookSummary.base_notebooks`，删
  `listBases` 拉取与假「需先挂载」门控；抽 `PromotionTargetModal` 共享组件（`none` 分支统一
  呈现策略，按 AGENTS.md Interactive feedback 在按钮紧邻处给结果）；`target_base_id` 为空的存量
  候选置灰并标注「需先用 `scripts/backfill_promotion_targets.py` 指定目标库」。组件测试覆盖三态。
- **T7 A9 收尾**：`docs/product-and-api*.md`（若 README 的 API 清单仍列 promote 端点则同步）补
  `target_base_id` 与 400 态；核对 `graph_retrieval.py` / `ask_service.py` / `knowledge_governance.py`
  里点名「已不存在的 ValueError 文案」的注释并清理。

## PR-3 待办中心露出「问答进行中」

真源 `docs/in-progress-action-resilience-design.md` §6.3（可选项）。`repository.pending_actions(user_id)`
聚合新增当前用户 `ask_jobs` 中 `running`/`queued` 项（notebook 名 + 提问摘要 + 开始时间），SSE
`pending_bus` 在 job 起止时推事件；`pending-center.tsx` 新分组「进行中的提问」，点击走原子
notebook opener 打开对应会话并复用既有接回逻辑。契约登记进 `docs/product-and-api*.md`；测试：
聚合查询按 owner 隔离、终态不再出现、前端分组渲染与跳转。

## PR-4 Deep Report 正文引用图片内联（第二期）

真源 `docs/superpowers/specs/2026-08-18-retrieval-image-citations-and-md-bundle-upload-design_zh.md`
§2 与 `fangan_done.md` 二期登记。后端 `report_engine` 已通过 `attach_reference_images` 给参考文献
挂图；本期把 Ask 的 `rehype-citation-images`（块级定位、跨引用去重、`CitationImageOrder`、页内
预览）接到 `report-view.tsx` 的正文渲染，并保留现有引用详情「本段附图」。公开分享报告页 `/r/{token}` **不**生效（实施期核实：`report_public_view` 的投影 allowlist 无 `asset_id`/`element_id`，报告也没有 token 别名的匿名资产端点，要做需同时改投影与新开匿名端点，属独立的披露面决定；已在双语文档与守卫反向钉住）。
组件测试对齐 Ask 既有用例（带图渲染、缺字段回退、未展开不发图片请求）。

## PR-5 起：前端 workspace 状态拆分（架构阶段 5，分片推进）

`frontend/app/page.tsx` 约 8900 行。不做一次性大拆，每片一个 PR，行为零变化，组件测试跟随：
第一片抽晋升队列 + 分析弹窗（与 PR-2 T6 相邻，顺势），第二片抽来源栏（列表 / 分页 / 搜索状态），
第三片抽知识图谱视图。每片验收：`tsc`、组件测试、production build、真机对照截图。

## 执行纪律

- 每任务：实现子代理（规格已定死用 `impl-task`，需判断用 opus）→ `spec-review` → `code-quality-review`
  → 下一项。热函数天花板与 ports 棘轮零松弛，抬基线只允许同 diff 下调。
- 每 PR：`bash scripts/check.sh` 全绿 + PG lane（涉及 SQL 时）→ push → codex 闭环 → CI 全绿 →
  `gh pr merge --rebase`。下一 PR 从合入后的 master 起分支。
- 完成项从 `fangan_todo.md` 移除并补进 `fangan_done.md`。

---

## 附：PR-3 设计定稿（实现前写下，实现按此执行）

### D1 状态口径：在途集合恰好是 `status='running'`

`ask_jobs` 没有 `queued` 状态——`_insert_job_row` 直接插 `'running'`（两个后端逐字相同）。
终态四个：`done` / `failed` / `cancelled`，以及重启兜底
（`sqlite/migrations.py::_recover_interrupted_jobs` 把残留 `running` 改写成 `interrupted`）。
故「进行中的提问」= `status = 'running'` **精确匹配**（不写 `NOT IN (终态…)`：新增一个
中间状态时否定式会把它悄悄放进铃铛，而精确匹配 fail-safe）。计划正文写的
`running`/`queued` 按代码事实收窄为 `running`，并在契约文档里写明理由。

### D2 聚合走既有 projection 接缝，不新增端口方法

新分组在 `*/query_store.py::pending_actions_projection_rows` 内产出，与「报告待确认」
那一半同构：

- **不在 `if notebook_ids:` 闸内**——谓词只有 `created_by` + 读权，一个 notebook id 都不
  消费；闸内会让「没有自有库的成员」在共享库里提的问不进铃铛（与 P1-T3b 同一形态）。
- **属主隔离**：`j.created_by = <当前用户>`。别人在我库里跑的提问不进我的铃铛，我在别人
  库里跑的提问进我的铃铛。
- **可见性**：库名走 `LEFT JOIN notebooks`，再叠加规范读谓词
  `access_sql.read_access_exists_clause("j")` 与 `NOTEBOOK_LIVE_SQL`（复用片段，绝不重拼）。
  失权/删除中的库里的在途提问不进铃铛，与报告那一半同口径。
- **有界**：按与 `idx_ask_jobs_creator_activity` 一致的
  `(_absolute_instant(created_at) DESC, id DESC)` 取最新 `RUNNING_ASK_ROWS` 条。
- **字段**：`type:"ask"`、`state:"running"`、`job_id`、`notebook_id`、`notebook_name`、
  `conversation_id`、`title`（问题摘要，截断到 `ASK_QUESTION_PREVIEW_CHARS`，不出全文）、
  `asked_at`（浏览器提交时刻，空则回落到服务端 `created_at`）。
  复用既有 `title` / `state` 字段名而不新造，前端 `PendingItem` 只需多两个可选 id 字段。
- **两侧常量同一份**：`RUNNING_ASK_ROWS` / `ASK_QUESTION_PREVIEW_CHARS` 与行整形放
  `app/repositories/pending_action_rows.py`（中性层，仿 `group_rows.py`），杜绝双后端
  截断长度或上限分叉。数值围栏登记进 `docs/product-and-api*.md`。

`PendingActionsService.list_for_user` 的 `count` 不变：它按 type 白名单计数，`ask` 不在
白名单里 —— **在途提问不响铃**，与 index building / paper_meta building 同一裁决（那是
状态展示，不是「待你确认」）。

### D3 推送边界：只在起点与终态各一次

复用 `pending_bus.publish_snapshot(user_id)`（fail-open、无连接时零开销）。落点：

| 路径 | 起点 | 终态 |
|---|---|---|
| 流式（`AskExecutionCoordinator.start`） | worker 线程体第一句 | worker `finally`，**在 `events.put(None)` 之后**；外加 `job_submitter.submit` 失败那条 `_finish` 之后 |
| 同步（`AskService.ask_current`） | `begin_job_current` 之后 | 四个 `finish_job` 之后各一次 |

两条红线：

1. **绝不放进 `_finish` / `finish_job` 内部**，也绝不放在终态事件入队之前。
   `publish_snapshot` 会在调用线程做一次 `pending_actions` 的 DB 计算，夹在
   `_finish` 与浏览器等待的 `final` 事件之间就是白白拖慢答案投递——与
   `_note_ask_completed` 被移到 `events.put` 之后是同一条理由。
2. **绝不在进度点（`on_trace`）推**。轨迹步是高频点，`mark_dirty` 每次都要重算快照；
   `pending_bus` 的 docstring 已把「起始与完成必达、进度点走节流」定死，这里连节流版
   都不用——在途提问的呈现里没有任何随 trace 变化的字段。

`attach_existing`（幂等键重放）不建新 job，故不推送。

### D4 前端：新分组「进行中的提问」，未读语义与其它项不同

- `pending-center.tsx` 新分组排在「深度报告待确认」之前（它是用户此刻最可能在找的东西），
  行文案 `${notebook_name} · 提问中 · ${title}`；`itemSig` = `ask:${job_id}`。
- **不计入未读徽标**：`pendingView` 把 `type === "ask"` 排除在 `unread` 之外。理由——
  在途提问是用户**自己几秒前发起**的，不是消息；把它算进未读会让「每问一个问题铃铛就
  亮一次」变成常态噪音。它仍然可见、可关掉、终态即消失。这是与 index/paper_meta
  building（那两个仍计未读）的**刻意分歧**，因为那两个是用户没发起、可能忘了的后台活。
- 点击：`openPendingItem` 走既有原子 opener `openNotebook(nb, "push", …, {coalesce:false})`，
  再 `switchChatMode("ask")` + `openAskSession(conversation_id)`；接回由既有
  `applySessionDetail` → `ConversationDetail` 的在途 turn + 轮询逻辑承担，本 PR 不碰。

### D5 不做

不加新端点、不加端口方法、不写任何新的写路径；不做「在途提问」的取消入口（铃铛只导航，
取消仍在会话内的「停止」按钮）；不为在途提问做 toast（`doneMessage` 只服务完成事件）。

## PR-5 slice 1 设计定稿（晋升队列 + 关系审核队列）

**范围**：把 `frontend/app/page.tsx` 里两个同形的治理队列弹窗整体搬出去，行为零变化。
两者形状完全一致（`issue`→`fetch`→`publish` 的冻结票据开窗、`activeLease`+`owns` 守着的
决策写入、单飞 `operationRef`、close sink 只清载荷），所以一并抽，共用同一套接缝。

**接缝**：每个队列一对文件——`use-*.ts` 持有领域状态与协调器交互，`*-modal.tsx` 持有
`<section role="dialog">` 曲面。理由：`memory-panel.tsx` / `kg-analysis-view.tsx` /
`command-catalog-panel.tsx` 已经是「组件自持 section，page 只传 `interactive` / `zIndex` /
`onClose`」的既定形态（root-modal-boundary 守卫的 `componentBindings` 表就是为它们建的）；
而 `use-kg-workspace.ts` / `use-report-workspace.ts` 是「hook 收状态 + effects 注入」的既定
形态。两者叠加即本片接缝，不发明新范式。

- `app/use-promotion-queue.ts`：`usePromotionQueue({ modals, effects })`，`modals` 是
  `Pick<RootModalCoordinator, "issue"|"publish"|"leaseIsCurrent"|"owns"|"activeLease"|"captureActorOwner">`
  的窄面（结构类型，page 直接传 `rootModals`，不是把整个 page state 灌进去），`effects` 只有
  `notify(message)` 与 `refreshCollection()`。返回 `{ view: { candidates, busy }, openPromoQueue,
  decidePromotion, clearQueue }`。函数名沿用 page.tsx 里的原名，守卫只需改「去哪个模块找」。
- `app/promotion-queue-modal.tsx`：`PromotionQueueModal`，props 全部显式——`candidates`、
  `busy`、`lookupNotebookName(id)`（目标库名的第二级回退，原本读 `notebookCollection.rows`）、
  `interactive`、`zIndex`、`onRequestClose(reason)`、`onApprove(id)`、`onReject(id)`。
  `PromotionCandidateActions` / `PromotionTargetModal` 原样不动。
- `app/use-edge-review-queue.ts` + `app/edge-review-modal.tsx`：同构，view 多一个 `total`。
- `promotion-target` slot 留在 page.tsx：它由 `submitPromotion`（知识条目 / Memory 的**提交**
  路径）驱动，与审核队列不是同一条流程，本片不动。

**协调器语义保持**：section 的 `aria-modal={interactive}` / `aria-hidden={!interactive}` /
`inert={interactive ? undefined : true}` / `style={{ zIndex }}` 与背景点击照搬；背景与「×」
关闭原本用的是**不同** reason（`"backdrop"` / `"button"`，`ROOT_MODAL_POLICIES` 按 reason 判
是否允许关闭），所以回调是 `onRequestClose(reason)` 而不是无参 `onClose`——沿用
`model-service-panel.tsx` 的 `onClose("escape")` 先例。

**守卫**：`tests/guards/root-modal-boundary.test.mjs` 有 4 处按「函数住在 page.tsx 的 Home 里」
认身份，本片只改「去哪找」，判据一条不减，并为两个新 hook 补上「clear 不许释放在飞操作」的
对称断言（原本只钉 page 的 close sink）。

## PR-5 slice 2 设计定稿（来源栏：搜索 / 列表 / 分页）

**范围**：`frontend/app/page.tsx` 里「本库来源」分组（标题 + 搜索表单）与 `.source-list`
（空态、来源行、`<Pagination>`）两块**相邻**的 JSX，整体搬进
`frontend/app/source-list-panel.tsx` 的 `SourceListPanel`，行为零变化。原文档顺序在
`.sources-body` 里是：扩展点 → `.source-scope-toolbar` → 参考库 `<section>` →
`.scope-group`(标题+搜索) → `.source-list`，本片只搬最后两块。

**不搬**：`.source-scope-toolbar`（检索范围计数 + 全选/清空）与参考库 `<section>`。它们由
`baseScopeSelection` 这份**留在 page 的** state 驱动（挂载参考库是另一条领域线），搬走会
把 page state 的 setter 灌进组件，与 slice 1 的窄面纪律相反；等参考库范围也有自己的 owner
hook 时再单独一片。上方的「添加来源 / 补全论文信息 / 整理知识图谱 / 检索索引 / 扩展点」同理
不动——它们分属 upload、paper-meta、kg、scale-index 四条领域线，不是来源列表。

**DOM 逐字不变**：`SourceListPanel` 返回 **Fragment**，两个 `div` 仍是 `.sources-body` 的
直接子节点。这是硬要求——`.source-list` 的滚动算术（`.sources-body` 上的
`flex:1 1 auto / min-height:0 / overflow:auto`）与 `extension-ui-boundary` 钉住的
「扩展点直接父节点 = `.sources-body`、且排在滚动列表之前」都靠这层直接父子关系。

**Props 全部显式**（无 `sourceLibrary` 整体注入、无 page state setter）：
`sources`、`uiMode`、`sourceQuery`、`sourcesPage`、`sourcesTotal`、`sourcesPageLoading`、
`sourceScopeSelection`、`deletingSourceIds`、`askInFlight`、`readOnlyWorkspace`、
`kgReady`、`notebookId`，以及回调 `onQueryChange` / `onSubmitSearch` / `onToggleSource` /
`onOpenSource` / `onDeleteSource` / `onPage`。

- `uiMode` 传的是 `UiMode` 原值而不是算好的 `advanced` 布尔：来源行 className 模板里的
  `isAdvanced(uiMode)` 是 `ui-mode-wiring` 的判据文本，换成布尔就等于悄悄弱化守卫。
- `notebookId` 传原值（不是 `canSearch` 布尔），搜索按钮的 `!notebookId || sourcesPageLoading`
  与原文一字不差。翻页回调里的 `if (currentNotebookId)` 守卫**留在 page**。
- 打开详情 / 删除 / 上传入口的编排（`openSourceDetail(...).catch(reportError)`、
  `confirmDeleteSource`、`openSourceModal`）留在 page.tsx，只以回调下传；`reportError`
  不进组件。
- `compactSourceTitle` 与它依赖的 `SUPPORTED_SOURCE_EXT_GROUP` 迁到新模块
  `frontend/app/source-title.ts`（page.tsx 另有两处调用点：`sourceTopicLabel` 与另一处
  扩展名清洗），保持**单一定义**，不复制常量。

**状态与 effects 一律不动**：仍在 `use-source-library.ts`（在途请求 / AbortController /
owner-window 世代守卫、`sourcesPageLoading` 的开合时机、搜索与翻页的单飞纪律）。本片没有
向 hook 新增任何 view 字段或命令。

**守卫重指向（判据一条不减）**：

| 守卫 | 改动 | 理由 |
|---|---|---|
| `base-scope-wiring.test.mjs` 「搜索框归入本库来源分组」 | page 侧断言顺序改为 `source-scope-toolbar → base-scope-list → <SourceListPanel>`，`source-search` 在 `scope-group` 内的祖先链判据移到新模块的树上 | 元素跨模块了，「搜索框排在参考库之后、且装在分组里」两条判据原样保留 |
| `base-scope-wiring.test.mjs` 「列表没被包进新分组」 | `.source-list` 不在 `.scope-group` 内改在新模块树上判；「必须留在 `.sources-body` 内」改为在 page 侧断言 `<SourceListPanel>` 的直接父节点是 `.sources-body` 那个 div | 直接父子关系正是滚动算术的载荷，判得比原来的祖先链**更紧** |
| `ui-mode-wiring.test.mjs` 来源行 className 模板 | `visit(page)` → `visit(panel)` | 只换「去哪找」，`isAdvanced(uiMode)` 判据不变 |
| `source-agent-badge-guard.test.mjs` | 解析目标 page.tsx → source-list-panel.tsx（CSS 那半不动） | 同上 |
| `source-library-boundary.test.mjs` | 追加：page 不得再直接渲染 `.source-list` / `.source-search` | 防回填 |

**组件测试**：`tests/component/source-list-panel.component.test.tsx`——空态文案；行按注入
数据渲染；提交搜索调注入命令且按钮变「搜索中…」；`busy` 时分页上下页禁用；KG 徽章 +
解析状态点 + Agent 徽章一行；删除/打开回调交回 source。

## PR-5 slice 3 设计定稿（知识图谱视图）

**范围**：`frontend/app/page.tsx` 的 L7651–8033（`{kgGraph.open && (<section className="kg-view" …>`
到它的闭合，383 行）整体搬进 `frontend/app/kg-graph-view.tsx` 的 `KgGraphView`，行为零变化。
它是**一整块**三栏视图（`.kg-rail` 左栏 / `.kg-canvas` 画布 / `.kg-detail` 右栏），三栏之间
共享 `kgGraph` 这一个命名空间对象与同一套派生 memo，拆成三个组件只会让父组件变成 40 个 prop
的转发层——所以按 slice-2 的判据（「各自已是天然边界、且有各自的 props」）判定**不拆**，
一个组件一个文件。

**留在 page.tsx 的**：所有 state、ref、useMemo 派生、effects、命令函数一律不动（use-kg-graph.ts /
use-kg-workspace.ts 的边界也一律不动，本片没有向 hook 增删任何 view 字段或命令）。`{kgGraph.open && …}`
开合门也留在 page（root-modal-boundary 的注释把「kg-view 由 kgGraph.open 直接控制、不是
RootModalSlot」写成既有设计，门留在 page 才看得见这条）。

**KgAnalysisView 仍由 page 渲染**：它现在是 `.kg-view` section 的**最后一个子节点**（
`position:fixed` 的 section 自建层叠上下文，挪出去会改变 z-index 归属）。所以 `KgGraphView`
接 `children` 并原位渲染，page 侧 `<KgAnalysisView interactive={rootModals.view("kg-analysis").topmost} …/>`
的 JSX 一字不动——root-modal-boundary 那条 `pageText` 断言因此**完全不用改**，rootModals
的开关编排也一条不外泄进组件。

**props：扁平、显式、且与原局部变量同名**。同名是硬要求而非偷懒：搬过去的 JSX 因此**逐字节
不变**（`disabled={kgGraph.relinking || kgGraph.rebuilding || kgGraph.buildingKg}` 这类正是
kg-relink/kg-rebuild 两个守卫的判据文本），等价性可以用 diff 直接看。分三组：

- 命名空间：`kgGraph: Pick<KgWorkspace["graph"], …24 个字段>`。按 slice-2 存疑 #1 用 `Pick<>`
  收窄——JSX 只读这 24 个字段，hook 的命令面一个都不进组件。`kgGraph` 本身是 page 里既有的
  合法命名空间别名（`const { …, graph: kgGraph } = kgWorkspace`），不构成 owner-view 再摊平。
- 派生只读：`fgData` `kgCanvas` `kgSearching` `kgDenseView` `kgSize` `kgTypeCounts`
  `kgNodeGroups` `selectedKgNode` `selectedKgEdges` `relatedNodeGroups`；上下文
  `readOnlyWorkspace` `currentNotebookId` `baseKgAvailable` `scaleIndexStatus`；
  ref `kgCanvasRef` `kgGraphRef` `kgDetailRef`（三者都还被 page 的 effect / 命令读，必须留在
  page 声明，只把 ref 对象下传）。
- 回调：`openKgAnalysis` `openKgSchemas` `closeKgView` `relinkFromKgView`
  `confirmRefreshUnifiedKg` `startKgRebuild` `handleKgSearchChange` `changeKgRange`
  `toggleKgType` `reviewPendingMerges` `reviewAllMerges` `decideMerge` `fitKgGraphView`
  `runScaleIndexOp`，加三个改名的：`onClearTypes`（原 `kgWorkspace.clearTypes`）、
  `onLoadMoreConceptMembers`、`onSelectOverviewNode`。

  - `baseKgAvailable` 传布尔而不是整个 `currentNotebook`：JSX 只读
    `currentNotebook?.base_kg_available` 一处，且没有任何守卫钉这段文本；传整条笔记本才是
    把 page state 灌进组件。
  - `reportError` 不进组件（slice-2 纪律）：`onLoadMoreConceptMembers` /
    `onSelectOverviewNode` 由 page 侧包好 `.catch(reportError)`。
  - **`selectKgNode` 刻意保留两个入口**：画布 `onNodeClick` 现在是不带 catch 的
    `selectKgNode(n.id)`（浮空 promise），总览列表是 `selectKgNode(node.id).catch(reportError)`。
    统一成一个回调会把画布那条的失败从「unhandledrejection」变成 toast——那是行为变化，
    不在零变化片里做。故 `onSelectCanvasNode`（不 catch）与 `onSelectOverviewNode`（catch）
    分开传，并在组件里注明这处不对称是登记在案的既有缺口。

**连带搬迁（单一定义，不复制）**：

| 去处 | 内容 | 理由 |
|---|---|---|
| `kg-graph-view.tsx`（只有这个视图用） | `ForceGraph2D` 的 `dynamic(…, { ssr:false })`、`RELATION_LABELS`+`relationLabel`、`truncateKgLabel`、`kgPayloadValue`、`drawKgNode`、`paintKgPointerArea`、`drawKgLinkLabel` | 全部只被这块 JSX 与它自己的 canvas 绘制函数消费 |
| 新 `kg-object-cards.tsx`（page 与视图**共用**） | `kgNodeName`、`KgOccurrenceCard`、`KgProcedureStepCard`、`FIELD_LABELS`+`fieldLabel` | page 侧 `fgData`/`selectedKgEdges` 两个 memo 与 `KnowledgeBrowser`/`genericBody` 仍在用；从 page.tsx 反向 import 会成环，只能提取共享模块 |
| 新 `relative-time.ts`（共用） | `formatRelativeTime` | 同上，page 另有 3 个调用点 |

`kgTypeBandForce`（d3 力）留在 page.tsx——它只被 page 的 `useEffect` 用，不进 JSX。
`kg-focus.ts` / `focusKgGraphNode` 同理不动（它是注入给 hook 的 effect，不在这块 JSX 里）。

**守卫重指向（判据一条不减）**：

| 守卫 | 改动 | 理由 |
|---|---|---|
| `kg-relink-wiring-guard.test.mjs` 最后一条 | `disabled={kgGraph.relinking \|\| …}` 的匹配目标 page.tsx → kg-graph-view.tsx；`relinkFromKgView` 委派仍判 page | 按钮跨模块了，两条判据（委派给 hook、三个忙碌位或起来）逐字保留 |
| `kg-rebuild-wiring-guard.test.mjs` 「presentation disables both…」 | 同上，`disabled={kgGraph.rebuilding \|\| …}` 判在 kg-graph-view.tsx；早退 `if (kgGraph.rebuilding \|\| …) return` 仍判 page | 同上 |
| `long-task-button-guard.test.mjs` | `LONG_TASK_BUTTONS` 每项加 `module`（缺省 `page.tsx`），`relinkFromKgView` / `decideMerge(` / `reviewAllMerges` 三项指向 kg-graph-view.tsx；按 module 分组解析、分组内仍「匹配 0 个即失败」 | 入口整体搬走后原表会静默变成 0 匹配——现表的响亮失败语义按模块保留，`requires` 在飞标志判据不变 |
| `root-modal-boundary.test.mjs` 「no legacy modal booleans」 | 删掉 kg-view 的挖洞 `replace(...)`，page 侧直接断言**不存在**任何静态 `aria-modal`；把 `<section className="kg-view" role="dialog" aria-modal="true">` 的存在断言移到 kg-graph-view.tsx | 判得比原来**更紧**（page 从「除 kg-view 外无静态 aria-modal」变成「无静态 aria-modal」），kg-view 自身的形状判据在新模块原样保留 |

新增防回填断言：page.tsx 不得再出现 `className="kg-view"` / `<ForceGraph2D`。

**组件测试** `tests/component/kg-graph-view.component.test.tsx`（`react-force-graph-2d`
经 `next/dynamic` 桩化，并断言 `ssr:false` 确实传下去）：画布四态（graph / 「图谱索引构建中」/
「库规模较大…」/ 空态）各渲染各自的曲面；`kgCanvas === "graph"` 时 ForceGraph2D 拿到注入的
`graphData` / `width` / `height`；待确认合并的「合并」「拒绝」把候选与 confirm 位交回注入命令，
且 `decidingMerge`/`rebuilding` 时两颗都禁用；概念详情经既有 `KgEvidenceList` 渲染出处，
`next_cursor` 存在时「加载更多成员」调注入命令；搜索框改字调 `handleKgSearchChange`；
只读工作区隐藏「图谱处理」栏与合并决定按钮。
