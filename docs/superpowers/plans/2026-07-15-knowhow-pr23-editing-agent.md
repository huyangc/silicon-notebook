# Knowhow 表 PR-2+3（编辑维护 + Agent 面，合并交付）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 PR-1 骨架上交付编辑维护（格子浮窗/建表向导/列行管理/Excel 模板往返/LLM 表达优化/引用跳转）与 Agent 面（HTTP+MCP 四能力/代码附件/新 scope），并按定稿的**格子级节点模型**重构 KG 投影。

**Architecture:** 投影换血为格子级节点模型（每个非空格子→KO、`object_type=列名` 动态类型、行内边统一 `about` 指向行标题格、内容类型仅作解析提示、同列同值跨行归并）；行标题列 0..1 可选（无=纯检索投影）；编辑经统一调度器触发全表结构性重投影（单飞+防抖+按格文本哈希跳过嵌入）。Agent 面走「session 或 agent token」双通道依赖 + MCP 工具镜像。规格：`docs/superpowers/specs/2026-07-15-knowhow-tables-design.md`（**唯一需求源**，UI 文案以其 ① 节为准）。

**Tech Stack:** FastAPI+SQLite（backend/）、Next.js（frontend/）、openpyxl（已有）。

## Global Constraints

- **共享 worktree 协议**：提交一律 pathspec 形式 `git commit -m "..." -- <显式路径>`；提交后 `git show --stat HEAD` 核对；只改本任务 Files 清单（+守卫点名的登记行）；可能有兄弟任务，忽略无关脏文件；index.lock 忙等几秒重试。
- 架构守卫四件套每任务过：`test_repository_surface_manifest.py test_architecture_module_boundaries.py test_architecture_hardening.py test_architecture_documentation.py`（触 pin 文件加跑 `test_repository_callers_static.py`）；按守卫文件头说明登记，优先 EOF 追加/净零行差（先例见 `.superpowers/sdd/task-4-report.md` 修复节）。
- openapi golden：改路由的任务**限定重生成 openapi 键**（ensure_ascii=False、`serialization`/`source_commit` 字节不动并给程序化校验证据，流程见 task-4-report 修复节）；改路由的任务间不并行。
- 迁移走 `_migration_17` 追加 + `SCHEMA_VERSION` 16→17；必须有「已部署 v16 库补建」测试；快照 manifest/schema_contract/字面 pin 级联按 PR-1 先例（`scripts/verify_repository_snapshot.py` 加 hop、golden 全量重生成流程见 task-1-report）。
- LLM 只经 `repo._runtime.models`（`RuntimeModelProvider`）：优化用 `.rewrite_llm_client`＋`**cap_kwargs(client,"openai_compat_max_tokens")`（core/llm.py:49）；失败 `models.note_model_error(stage,model,exc)`；未配置查 `.configured`/`ModelNotConfiguredError`→友好中文 400。除 T8 优化端点外**零新增 LLM 调用**；投影零 LLM 不变。
- 后台任务只经 `background_jobs.submit(fn,*args,name=,notify_pending=)`（services/background_jobs.py:37，自带 copy_context）；严禁裸线程。
- Agent scope 两处同步：`AGENT_SCOPES`（memory_service.py:47）+ `AGENT_SCOPE_OPTIONS`（agent-token-model.ts:1）。
- 后端测试 backend/ 下 `${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python} -m pytest ... -q`；前端顶层 `*.test.mjs` + `npx tsc --noEmit`；已知外围失败=`test_repository_api_contract.py::test_serialization...`（master 级）。不启服务、不禁 sandbox。
- 文案中文友好无黑话（UI 字符串以规格①为准：行标题列/方法步骤/工具·事物/普通）；page.tsx 弯引号红线（diff 检查=0）；id 用 `new_id(prefix)`；效率一等（无轮询、嵌入仅增量）。

## 波次（编排者用；改路由任务加粗、互不同波）

A: T1 ∥ T4 · B: T2 ∥ **T3** ∥ T5 · C: **T6** ∥ T7 ∥ T14 · D: **T8** ∥ T9 ∥ T13 · E: **T10** · F: **T12** · G: T11 ∥ T15 ∥ T16 → 终审+浏览器 QA+PR

---

### Task 1: 迁移 17 与存储扩展

**Files:** Modify `backend/app/repositories/sqlite/migrations.py`、`backend/app/repositories/sqlite/knowhow_store.py`、`backend/app/services/knowhow/grid_parser.py`、facade 一跳委托与守卫登记；Test `backend/tests/test_knowhow_schema.py`（扩）、`backend/tests/test_knowhow_store.py`（扩）、`backend/tests/test_knowhow_grid_parser.py`（改）

**Interfaces (Produces):**
- `_migration_17`：建 `knowhow_cell_code(id TEXT PK, row_id TEXT NOT NULL, column_id TEXT NOT NULL, code_text TEXT NOT NULL, language TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '', cell_content_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(row_id, column_id))` + `idx_knowhow_cell_code_row`；`UPDATE knowhow_columns SET role= CASE role WHEN 'concept' THEN 'anchor' WHEN 'identify' THEN 'procedure' WHEN 'root_cause' THEN 'procedure' WHEN 'fix' THEN 'procedure' WHEN 'tool' THEN 'entity' ELSE 'attribute' END`；SCHEMA_VERSION=17。
- store 新/改方法（facade 同名一跳）：`create_knowhow_table(..., created_by)`（标题 strip 非空「表标题不能为空」；anchor **至多一**）、`set_knowhow_anchor_column(table_id, column_id|None)`（校验列属本表；返回旧值）、`add_knowhow_column(table_id,name,kind,position=None)->id`、`rename_knowhow_column(column_id,name)`、`set_knowhow_column_kind(column_id,kind)`、`delete_knowhow_column(column_id)`（级联 cells+cell_code）、`delete_knowhow_row(row_id)`（级联）、`update_knowhow_table_meta(table_id,title=None,description=None)`、`validate_cell_target(row_id,column_id)`（列属行所在表否则 ValueError「格子定位不合法」）、`upsert_knowhow_cell_code(row_id,column_id,code_text,language,updated_by,cell_content_hash)->id`、`get_knowhow_cell_code(row_id,column_id)->dict|None`、`delete_knowhow_cell_code(row_id,column_id)`、`list_knowhow_cell_code(table_id)->list[dict]`；kind 合法值 `{anchor,procedure,entity,attribute}`（anchor 仅经 set_anchor 写入）。
- `grid_parser.guess_roles` 重写为 `guess_kinds(columns)->tuple[list[str], int|None]`：kind 猜测（识别/方法/步骤/分析/修复→procedure；工具/命令/脚本→entity；其余 attribute）+ anchor 建议 index（列名命中 名称/概念/类型/violation/name/type/concept 才给，**无首列兜底**）。

- [ ] TDD：迁移双测（全新库+v16 补建+存量 role 值重映射断言）；store 各方法/校验/级联/code CRUD；guess_kinds 用例（时序修复五列→(attribute? 违例概念→anchor 建议 idx=0, 识别/根因/修复→procedure, 工具→entity)；旅行表九列→全 attribute + anchor=None）。
- [ ] 实现（迁移级联：manifest hop (16,17)+schema_contract+字面 pin 按 task-1-report 先例）→ 全绿+守卫。
- [ ] Commit（pathspec）：`feat(knowhow): migration 17 (cell_code + kind remap) + store editing/code primitives`

### Task 2: 投影重构——格子级节点模型

**Files:** Modify `backend/app/services/knowhow/projection.py`、`backend/app/services/knowhow/textops.py`（加 `compose_row_title`/`node_name`/`value_key`）；Test `backend/tests/test_knowhow_projection.py`（大改）、`backend/tests/test_knowhow_textops.py`（扩）

**Interfaces:**
- Consumes：T1 store；既有 chunk 差分/嵌入自愈/隐藏源机制（保留）。
- Produces（T3/T13 依赖）：`KnowhowProjector.project_table(table_id, *, embed=True) -> None`（**唯一投影入口**，project_row 移除）；`textops.compose_row_title(cells_by_position: list[str]) -> str`（前≤3 个非空格首行、每段≤16 字、「 · 」连接、兜底「行N」由调用方处理）；`textops.node_name(text)->str`（净文本首行≤40 字+…）；`textops.value_key(text)->str`（≤80 字：casefold+去空白标点归一；>80：`sha256` 前 32 hex）。派生 id：KO=`ko-kh-{sha1(table_id|column_name|value_key)[:32]}`、边=`kr-kh-{sha1(src|about|dst)[:32]}`；element/chunk id 方案**不变**（保嵌入）。

**存量自动重投影**：启动 warmup 阶段（镜像 startup_warmup 既有后台惰性模式）检测 knowhow 隐藏源下存在旧模型 KO（`object_type IN ('case','procedure','tool')` 且 id 前缀 `ko-kh-`）的表 → 逐表 `background_jobs.submit(project_table)`；结构性重建、chunk 未变零嵌入；只跑一次（重建后条件自然消失）。测试：造旧模型 KO → 启动钩子 → 动态类型 KO 替换到位。

**投影语义（规格④定稿）：** 有行标题列：每个非空格→KO（`object_type=列名`、name=node_name、payload={text 净文本, table_id,row_id(证据行列表),column_id,column_name, steps?(procedure 提示), }、evidence 累积各来源格 element）；entity 提示格按列表项/换行拆多 KO（每项独立 value_key）；行内边：非行标题格 KO --about--> 行标题格 KO。无行标题列：**零 KO/零边**（含从有到无的转换清理），chunk/element 照旧；section_path=`表名 › {行标题值|compose_row_title} › 列名`。全表单遍历：teardown 本表旧 KO/边（按隐藏源 ownership）→ 重建；chunk 仍按格哈希差分+向量自愈（既有机制原样保留）；行状态 syncing→synced/failed 逐行推进。

- [ ] TDD：格→KO 类型=列名；同列同值两行→1 KO 2 evidence 2 边；entity 拆分跨行归并（Innovus 两行一节点）；about 边方向/去重；无行标题→零 KO、chunk 在；行标题从有改无→KO 清零；长文 node_name/value_key；文本未变零嵌入（既有测试保持）；synthesized section_path。
- [ ] 实现 → 全绿+守卫（`test_knowhow_retrieval.py` 同步改断言：KO 类型断言换列名、`/knowledge-types` 计数含动态类型）。
- [ ] Commit：`feat(knowhow): cell-level node projection (dynamic types, about edges, merged identity)`

### Task 3: 编辑 API 与投影调度器

**Files:** Modify `backend/app/api/routes.py`、`backend/app/models/schemas.py`、`backend/app/services/knowhow/api.py`（加 `ProjectionScheduler`）+ golden；Test `backend/tests/test_knowhow_editing_api.py`（新）

**Interfaces:**
- Consumes：T1 store、T2 `project_table`。
- Produces（T4/T5/T7 wire 契约，snake_case）：`PATCH /notebooks/{nb}/knowhow/{t}` `{title?,description?,anchor_column_id?}`（显式 null 清除）；`POST .../{t}/columns` `{name,kind,position?}`；`PATCH .../columns/{col}` `{name?,kind?}`；`DELETE .../columns/{col}`；`POST .../{t}/rows` `{cells:{column_id:md},position?}`；`DELETE .../rows/{row}`；`PATCH .../rows/{row}/cells/{col}` `{content_md}`→`{row_id,column_id,content_md,projection_status}`；写=owner（镜像既有 require_notebook_access）、读者 404；全部经 `scheduler.schedule(table_id)`。
- **导入端点 wire 随 kinds 更新**（同属本任务）：preview 返回 `{columns:[{name, guessed_kind}], anchor_suggestion: int|null, ...}`；import 的 `columns_json`=`[{name,kind}]` + form `anchor_index`（可空）；`kind` 合法值三种（anchor 只经 anchor_index 表达）。golden 同轮限域重生成。
- `ProjectionScheduler`（api.py 内，进程级单例挂 runtime）：`schedule(table_id)`——per-table `pending/running/rerun` 三态：running 时置 rerun；否则 `background_jobs.submit(_run, table_id)`；`_run` finally 检查 rerun 再排一次；0.5s 防抖合并（`threading.Timer` 于 submit 前，计时器句柄表内）。**发现绝不 seq 门控**。

- [ ] TDD：每端点行为+校验（kind 非法/anchor 列不属本表/`validate_cell_target`/标题空）+读者矩阵+调度断言（连续三次 PATCH 合并为≤2 次 project_table——fake scheduler 计数）+anchor 变更触发重投影+无行标题表编辑后仍零 KO。
- [ ] 实现（路由薄，编排进 api.py）→ 绿+守卫+golden 限域重生成（校验证据入报告）。
- [ ] Commit：`feat(knowhow): editing API (table/column/row/cell) + projection scheduler`

### Task 4: 前端模型层扩展

**Files:** Modify `frontend/app/knowhow-model.ts`、`frontend/app/knowhow-model.test.mjs`；`frontend/app/agent-token-model.ts`（+knowhow:code 选项，与 T10 后端同步字符串）

**Interfaces (Produces，T5/T7/T9/T11/T12 逐字 import)：** `CellKind = "anchor"|"procedure"|"entity"|"attribute"`；`KIND_LABELS`（方法步骤/工具·事物/普通——anchor 不在列下拉，UI 走表级 anchorColumnId）；`KnowhowTableDetail.anchorColumnId: string|null`；fetchers：`patchKnowhowTable(nb,t,{title?,description?,anchorColumnId?})`、`addKnowhowColumn/patchKnowhowColumn/deleteKnowhowColumn`、`addKnowhowRow/deleteKnowhowRow`、`patchKnowhowCell(nb,t,row,col,md)`、`knowhowTemplateUrl(nb,t)`、`appendKnowhowPreview/appendKnowhowCommit`、`optimizeKnowhowCell(nb,t,row,col)->{suggestionMd}`、`getCellCode/putCellCode/deleteCellCode`（status:"implemented"|"stale"|"none"）、`CitationKnowhowRef {tableId,rowId}`；`composeRowTitle(cells,columns)`（与后端同规则，网格/抽屉标题用）。wire 映射 snake_case↔camelCase 照旧收敛在 mapper。

- [ ] TDD（纯逻辑：composeRowTitle 规则、payload 组装、KIND_LABELS 覆盖、code status 映射）→ 实现 → `node --test` 全绿+tsc。
- [ ] Commit：`feat(knowhow): frontend model layer for editing/template/optimize/code/citation`

### Task 5: 管理与建表向导 UI

**Files:** Create `frontend/app/knowhow-manage.tsx`（建表向导+列/行/表管理菜单）；Modify `frontend/app/knowhow-panel.tsx`（工具栏「新建表」「管理」入口、canEdit gating、行标题列选择器入设置）、`frontend/app/knowhow-import.tsx`（kind 下拉 3 项+行标题列选择器+猜测预选）、`frontend/app/page.tsx`（传 `canEdit={!isReader}`，page.tsx:3147 的 isReader）；Test `frontend/app/knowhow-manage.test.mjs`

**要点：** 建表向导=定表头（列名+类型下拉+行标题列选择器「[列 ▾/不设置]」带规格①提示语）→ create → 打开网格+「添加行」引导；管理菜单收拢（加列/改列/删列/删行/表信息）；破坏性操作确认层；读者隐藏全部写入口；UI 精致标准（对齐/徽章一致）。

- [ ] TDD 纯逻辑（向导 payload、anchor 选择器状态、猜测预选映射）→ 实现 → 测试+tsc+弯引号检查=0。
- [ ] Commit：`feat(knowhow): create-table wizard + table/column/row management UI`

### Task 6: Excel 模板往返（后端）

**Files:** Modify `backend/app/services/knowhow/api.py`、`backend/app/api/routes.py` + golden；Test `backend/tests/test_knowhow_template.py`

**Interfaces (Produces)：** `GET /notebooks/{nb}/knowhow/{t}/template` → xlsx `StreamingResponse`（附件名 `{title}-template.xlsx`；首行=列名（锁定）、次行=类型+行标题标注说明、冻结前两行；生成用 openpyxl，下载习语照 routes.py:1090 报告导出）；`POST .../{t}/append`（multipart file + form `mode=preview|commit`）：按**列名**匹配（缺列=空、多列忽略并报告）、纯文本、preview→`{rows_preview(前5),total_rows,unmatched_columns,duplicate_titles:[{row_index,title}]}`（仅设行标题列时按其值对比现有行标）、commit→`{added}` 且追加行后 `scheduler.schedule`。

- [ ] TDD（模板往返：生成→用 grid_parser 读回列名一致；append 预览重名标记/缺列容错/commit 追加+调度）→ 实现 → 绿+守卫+golden 限域。
- [ ] Commit：`feat(knowhow): xlsx template download + append import`

### Task 7: 格子编辑浮窗

**Files:** Create `frontend/app/knowhow-cell-editor.tsx`（编辑态组件：textarea+预览分栏、轻工具栏、粘贴/拖拽图片→`POST /assets`→插 `asset://`、localStorage 草稿(键=cell id，恢复提示)、保存/保存并下一格(行主序)/取消、`Cmd+Enter`/`Cmd+Shift+Enter`/Esc 未存提醒、行上下文条(同行其他格摘要可展开)、procedure 提示行「用有序列表写步骤…」）；Modify `frontend/app/knowhow-panel.tsx`（格子浮窗预览态加「编辑」（canEdit）、抽屉分节编辑按钮、空格子点击直入编辑）；Test `frontend/app/knowhow-cell-editor.test.mjs`（草稿键、下一格序、图片插入文本操作等纯逻辑）

- [ ] TDD 纯逻辑 → 实现（编辑器风格照 memory-panel textarea+预览先例，样式命名空间 kh-*）→ 测试+tsc+quote=0。
- [ ] Commit：`feat(knowhow): cell editor modal (dual-state, image paste, drafts, save-and-next)`

### Task 8: LLM 表达优化（后端）

**Files:** Modify `backend/app/services/knowhow/api.py`、`backend/app/api/routes.py` + golden；Test `backend/tests/test_knowhow_optimize.py`

**Interfaces (Produces)：** `POST /notebooks/{nb}/knowhow/{t}/rows/{row}/cells/{col}/optimize` → `{suggestion_md}`；服务：`optimize_cell(...)` 用 `models.rewrite_llm_client`（`.configured` 否→400「尚未配置模型，无法优化表达」）、prompt 固定模板（保持原意与语言/规整结构/procedure 列要求有序列表/`asset://` 引用原样保留/只回 markdown 正文）、`**cap_kwargs(client,"openai_compat_max_tokens")`、异常→`models.note_model_error("knowhow_optimize",...)`+502 友好；**不写库**（回填走既有 PATCH cell）。

- [ ] TDD（fake client：prompt 含格文本与约束、成功回建议、未配置 400、异常 502+model_error 计数、asset 引用保留断言）→ 实现 → 绿+守卫+golden 限域。
- [ ] Commit：`feat(knowhow): LLM cell rewrite endpoint (explicit, suggestion-only)`

### Task 9: 模板+优化 UI

**Files:** Modify `frontend/app/knowhow-panel.tsx`（工具栏「下载模板」「追加导入」）、`frontend/app/knowhow-import.tsx`（append 模式：重名标黄/缺列提示）、`frontend/app/knowhow-cell-editor.tsx`（「优化表达」→建议对照分栏（原文/建议）→接受(填入编辑框)/放弃）、`frontend/app/knowhow-panel.tsx` 抽屉「优化整行」（逐格顺序调用、每格接受/跳过、进度与错误逐格显示）；Test `frontend/app/knowhow-optimize.test.mjs`（对照状态机、逐格队列纯逻辑）

- [ ] TDD → 实现（模板下载=带 token 的 blob fetch 触发下载，复用 KnowhowImage 的认证 fetch 习语）→ 测试+tsc。
- [ ] Commit：`feat(knowhow): template round-trip + optimize UI (diff, per-cell accept)`

### Task 10: Agent HTTP + MCP

**Files:** Create `backend/app/api/knowhow_agent_routes.py`（router，main.py 挂 `/api`）；Modify `backend/app/api/deps.py`（新依赖 `require_user_or_agent(scope)`：Bearer `snm_` 前缀→`resolve_agent_token`+`require_agent_access(principal,scope,notebook_id)`+set_request_user(owner)（习语照 mcp_server._owner_request_context）；否则走既有 session 解析+notebook 读写守卫）、`backend/app/services/memory_service.py:47`（AGENT_SCOPES += `knowhow:code`）、`backend/app/api/mcp_server.py`（PUBLIC_TOOLS+4 工具）、`backend/app/services/knowhow/api.py`（服务核心共用）+ golden；Test `backend/tests/test_knowhow_agent_api.py`

**Interfaces (Produces)：**
- HTTP（session 或 agent 皆可）：`GET /agent/knowhow/tables?notebook_id=` → 概要+列(kind)+行数+anchor_column_id（scope knowledge:read）；`GET /agent/knowhow/tables/{t}/discrimination` → `{rows:[{row_id,title,methods:[{column_id,column_name,text}]}]}`（methods=procedure 列净文本；无行标题表→400「该表未设置行标题列，不支持判别集」）；`GET /agent/knowhow/rows/{row}` → `{title,cells:[{column_id,column_name,kind,text,steps?,items?}],code:[{column_id,language,code_text,status,updated_at}]}`；`PUT/GET/DELETE /agent/knowhow/rows/{row}/cells/{col}/code`（PUT body `{code_text,language}`，写 scope `knowhow:code`，读 knowledge:read；status 推导=`sha256(strip_images(content_md))` vs 存储 hash → implemented/stale/none）。
- MCP 工具（同服务核心，`_budget_response` 包裹）：`list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row`、`put_knowhow_cell_code`；注册模式照 search_notebook_context（mcp_server.py:797 习语，`_selected_notebook(ctx,repo,scope)`）。

- [ ] TDD：鉴权矩阵（session owner/读者；agent token scope 命中/缺失/notebook 不在白名单→404）、判别集形状与无行标题 400、code 三态推导（改格后 stale）、MCP 工具冒烟（镜像既有 MCP 测试 harness）。
- [ ] 实现（scope 字符串与 T4 前端选项一致）→ 绿+守卫+golden 限域。
- [ ] Commit：`feat(knowhow): agent surface (HTTP+MCP, discrimination/row/code, knowhow:code scope)`

### Task 11: 代码附件 UI

**Files:** Create `frontend/app/knowhow-code.tsx`（查看/编辑浮层：等宽+复制+language 标签+新鲜度 chip（已实现/知识已更新/未实现）+编辑保存/删除确认）；Modify `frontend/app/knowhow-panel.tsx`（网格行聚合徽章(最差态)、抽屉分节 code chip 入口）；Test `frontend/app/knowhow-code.test.mjs`（状态聚合/展示映射）

- [ ] TDD → 实现（canEdit gating；agent 写入的代码用户可看改删）→ 测试+tsc。
- [ ] Commit：`feat(knowhow): cell code attachment UI (badges, viewer/editor, freshness)`

### Task 12: 引用跳转（后端富化 + 前端）

**Files:** Modify `backend/app/models/schemas.py`（`Citation.knowhow: Optional[{table_id,row_id}]`）、`backend/app/services/evidence_context.py:202`（citations_from：批量按 element_id 查 `source_elements.metadata.knowhow`，命中则填充）+ golden；`frontend/app/answer-formatting.ts`（CitationLike + knowhow 透传）、`frontend/app/answer-panel.tsx`（弹层加「在表格中查看」按钮）、`frontend/app/page.tsx`（`openKnowhowAt(tableId,rowId)`：打开面板→表→行抽屉；KnowhowPanel 加 `initialTableId/initialRowId` 受控入参）、`frontend/app/knowhow-panel.tsx`；Test `backend/tests/test_knowhow_citation.py` + `frontend/app/knowhow-citation.test.mjs`

- [ ] TDD：BE 命中格引用带 {table_id,row_id}、非 knowhow 引用无该字段、批量查询一次；FE 透传与跳转参数纯逻辑。
- [ ] 实现 → 绿+守卫+golden 限域+quote=0。
- [ ] Commit：`feat(knowhow): ask citations jump to table row drawer`

### Task 13: 完整深拷贝（id 重映射）

**Files:** Modify `backend/app/repositories/sqlite/sharing_store.py`（业务表五张+notebook_assets 入 `_COPY_SNAPSHOT_QUERIES`/`_COPY_VALIDATED_TABLES`；**chunks/source_elements/chunk_embeddings 的 knowhow 排除改为随拷贝重映射**；KO/关系保持排除）、`backend/app/services/notebook_sharing.py`（新增 khtbl/khcol/khrow/khcel/khcode/asset 重映射；隐藏源行拷贝重映射、`hidden_source_id` 更新；element/chunk id 用投影稳定函数重算（从 projection 导出纯 id 助手）；`source_elements.metadata.knowhow` 与 cell md `asset://` 重写；资产文件 `storage_dir/assets/<src>→<new>` 拷贝）；拷贝发布后逐表 `scheduler.schedule`（结构重建 KO/边；chunk 未变+向量已在→**零嵌入**）；Test `backend/tests/test_knowhow_copy.py`

- [ ] TDD：拷贝后表/行/格/code/资产文件/md 引用全重映射、KO 经投影重建且类型=列名、fake embedder 零调用、副本检索命中、原本不受影响、validate_copy 平衡。
- [ ] 实现 → 绿+守卫。
- [ ] Commit：`feat(knowhow): full deep-copy with id remap (zero re-embedding)`

### Task 14: 资产 GC

**Files:** Modify notebook 删除路径（grep 现有 notebook 删除服务清理源文件处，加 `assets/<nb>` 目录与行清理）、`backend/app/repositories/sqlite/maintenance.py`（`sweep_orphan_assets(notebook_id)->{removed}`：资产 id 未出现于本库任何 cell content_md 即删行+文件）；Test `backend/tests/test_knowhow_asset_gc.py`

- [ ] TDD（删库清目录；孤儿清、被引用留）→ 实现 → 绿+守卫。
- [ ] Commit：`feat(knowhow): asset GC (notebook delete + orphan sweep)`

### Task 15: 守护与集成测试

**Files:** Test `backend/tests/test_knowhow_code_isolation.py`（代码隔离不变量：cell_code 文本注入后遍历 elements/chunks/chunks_fts/KO payload/ask 上下文组装均无泄漏）、`backend/tests/test_knowhow_pr23_integration.py`（编辑→调度→检索反映新文本；改格→code 变 stale；动态类型进 /graph 与 /knowledge-types；判别集 e2e；引用富化 e2e）

- [ ] 编写并跑绿（暴露真 bug 按小修在对应模块+独立 commit 说明，跨界 STOP 上报）。
- [ ] Commit：`test(knowhow): code isolation guard + PR-2/3 integration`

### Task 16: README 双语

**Files:** Modify `README.md`（新 `## Knowhow tables` 节临近 Product Flow；APIs 列表加 knowhow 端点组；Memory and Agent MCP 节加 4 工具与 `knowhow:code` scope）、`README_zh.md`（镜像）

- [ ] 通用口径撰写（无机器路径）→ `test_architecture_documentation.py` 过（README 措辞守卫！）。
- [ ] Commit：`docs(knowhow): bilingual feature + agent API documentation`

---

## 收尾（编排者）：全量后端+前端测试 → fable 全分支终审（含 Minor 台账 triage）→ 浏览器 QA（编辑/向导/模板/优化/代码/跳转全流程 + 迁移后旧表自动重投影验证）→ #270 已合则 rebase --onto 掉已并提交 → push → PR（正文含模型五轮演进说明）。
