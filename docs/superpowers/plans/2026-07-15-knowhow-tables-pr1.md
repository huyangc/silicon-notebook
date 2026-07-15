# Knowhow 表 PR-1（骨架）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地 knowhow 表骨架：数据模型+迁移、一次性整表导入（xlsx/csv/md+角色映射）、只读阅读视图（总览网格+行详情抽屉）、资产存储与鉴权路由、确定性投影（零 LLM 构建 KO/边/chunk）+ 检索接通。

**Architecture:** 表/列/行/格四表为编辑真相源；每表挂一个隐藏合成源（`source_type="knowhow"`），行级确定性投影产出 element→chunk（格子粒度、带定位 metadata）与 case/procedure/tool KO + 结构化边；现有检索/ask 经 chunk/KO 自动覆盖。规格：`docs/superpowers/specs/2026-07-15-knowhow-tables-design.md`（本计划的唯一需求来源，冲突以规格为准）。

**Tech Stack:** FastAPI + SQLite（backend/），Next.js（frontend/），openpyxl（已有依赖）。

## Global Constraints

- 遵守既有架构守卫：facade 新成员走 allowlist + 一跳委托；改动后必须跑 `test_repository_surface_manifest.py`、`test_architecture_module_boundaries.py`、`test_architecture_hardening.py`、`test_architecture_documentation.py`。
- 新表走 `_migration_16` 追加 + `SCHEMA_VERSION` 15→16（严禁塞进已封版迁移）；必须有「已部署 v15 库补建」测试。
- 所有新 id 用仓储现有 `new_id(prefix)`（全 128 位）；派生对象 id 用稳定哈希（见 Task 5），不得用 uuid4 截断。
- 提交纪律：`git add <显式文件路径>`，**严禁 `git add -A/.`**（同 worktree 有并行任务）；每任务一个 commit，消息带 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。
- 只改本任务 Files 清单内的文件；测试跑 scoped（新测试文件 + 四个架构守卫测试）。
- 后端 python 用 `PYTHON_BIN`（缺省 `/opt/homebrew/Caskroom/miniconda/base/bin/python`），从 `backend/` 目录跑 `pytest`；不启动任何服务。
- 前端测试放 `frontend/app/` 顶层 `*.test.mjs`（嵌套目录不会被 `node --test` 收集）；API 路径用现有 `API_BASE` 约定（防双 `/api` 404）；`page.tsx` 中文文案的弯引号“”是有意的，禁止批量替换。
- 面向用户文案友好、不暴露技术细节；UI 对齐精致（同类控件成列、省略号截断）。
- LLM/VLM 零调用（本 PR 无任何模型调用新增，embedding 走既有服务且仅增量）。

## 任务依赖与并行波次（编排者用）

- **Wave A（并行）**：Task 1（迁移）、Task 3（网格解析器）、Task 7（前端模型层）
- **Wave B（Task 1 后，并行）**：Task 2（仓储模块）、Task 4（资产服务）
- **Wave C（Task 2 后，并行）**：Task 5（投影服务）、Task 6（表/导入 API）
- **Wave D（Task 6+7 后，并行）**：Task 8（总览+抽屉 UI）、Task 9（导入向导 UI）
- **Wave E（全部后）**：Task 10（检索接通集成验证）

---

### Task 1: Schema 迁移（五张新表）

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`（`SCHEMA_VERSION = 16`；追加 `_migration_16`）
- Test: `backend/tests/test_knowhow_schema.py`

**Interfaces:**
- Produces: 表 `knowhow_tables(id, notebook_id, title, description, mutation_seq, hidden_source_id, created_by, created_at, updated_at)`；`knowhow_columns(id, table_id, name, role, position)`；`knowhow_rows(id, table_id, position, projection_status, created_at, updated_at)`；`knowhow_cells(id, row_id, column_id, content_md, updated_at, UNIQUE(row_id, column_id))`；`notebook_assets(id, notebook_id, filename, mime, size, created_by, created_at)`。均带 notebook/table/row 外键索引（命名 `idx_knowhow_*`/`idx_notebook_assets_nb`），`role` 默认 `'plain'`，`projection_status` 默认 `'pending'`，`mutation_seq` 默认 0。

- [ ] **Step 1: 写失败测试**：`test_fresh_db_has_knowhow_tables`（全新库五表存在、关键列可插查）+ `test_v15_db_upgraded_gets_knowhow_tables`（先按现有测试套路建到 v15——参照本文件里既有「已部署库补建」测试的构造方式；若无先例，构造：新建库后手动 `DELETE` 五表并把 `schema_version` 写回 15，再走一次迁移入口，断言五表补建、版本=16）。运行确认 FAIL。
- [ ] **Step 2: 实现**：在 `_migration_15` 之后追加 `_migration_16`（`CREATE TABLE IF NOT EXISTS` + 索引，风格照抄 `_migration_15`），bump `SCHEMA_VERSION = 16`，登记进迁移注册处（照抄现有迁移的注册方式）。
- [ ] **Step 3: 跑通**：`pytest tests/test_knowhow_schema.py -x -q` PASS；四个架构守卫测试 PASS（迁移文件行号变动若触发 surface manifest 守卫，按该测试文件头部说明重生成/登记，禁止改语义迁就）。
- [ ] **Step 4: Commit**：`git add backend/app/repositories/sqlite/migrations.py backend/tests/test_knowhow_schema.py`（守卫测试要求同步的清单文件一并显式 add）→ `feat(knowhow): schema migration 16 for knowhow tables + notebook assets`

### Task 2: 仓储模块 knowhow_store + facade 组合

**Files:**
- Create: `backend/app/repositories/sqlite/knowhow_store.py`
- Modify: facade（`backend/app/repositories/sqlite_repository.py`，照现有 store 的组合根模式：构造处组装 + 一跳委托方法）
- Modify: 架构守卫的 allowlist/manifest 登记文件（跑守卫测试按报错提示定位）
- Test: `backend/tests/test_knowhow_store.py`

**Interfaces:**
- Consumes: Task 1 的表；facade 现有 `new_id(prefix)`、连接管理（thread-local 复用，参照相邻 store 模块的连接获取方式，勿自开连接）。
- Produces（facade 一跳委托同名暴露）:
  - `create_knowhow_table(notebook_id, title, description, columns: list[dict]) -> str`（columns=`[{"name","role"}]`，按序生成 position；校验 concept 恰一列、列名非空不重复，违规抛 `ValueError`）
  - `list_knowhow_tables(notebook_id) -> list[dict]`（含 row_count）
  - `get_knowhow_table(table_id) -> dict`（table + columns 按 position + rows 按 position，每行含 `cells: {column_id: content_md}` 与 `projection_status`）
  - `add_knowhow_row(table_id, cells: dict[str, str], position: int | None = None) -> str`
  - `update_knowhow_cell(row_id, column_id, content_md) -> None`（upsert 语义，bump 行 updated_at、表 mutation_seq，行 projection_status→'pending'）
  - `delete_knowhow_table(table_id) -> dict`（级联删列/行/格，返回 `{"hidden_source_id": ...}` 供上层清投影）
  - `set_knowhow_row_projection(row_id, status: str) -> None`；`set_knowhow_hidden_source(table_id, source_id) -> None`；`bump_knowhow_mutation_seq(table_id) -> int`
  - `insert_notebook_asset(notebook_id, filename, mime, size, created_by) -> str`；`get_notebook_asset(asset_id) -> dict | None`

- [ ] **Step 1: 写失败测试**：建表→列序与角色持久化；concept 缺失/重复抛错；add_row+update_cell 幂等 upsert；get 结构完整；delete 级联（列/行/格全空）；mutation_seq 单调。运行 FAIL。
- [ ] **Step 2: 实现** `knowhow_store.py`（类风格、SQL 组织照抄 `notebook_store.py`），facade 组装+一跳委托。
- [ ] **Step 3: 跑通** 新测试 + 四守卫测试（facade 新增成员按守卫报错提示进 allowlist；manifest 行号按其说明重生成）。
- [ ] **Step 4: Commit**：`feat(knowhow): knowhow_store repository module + facade composition`

### Task 3: 网格解析器与角色猜测（纯函数）

**Files:**
- Create: `backend/app/services/knowhow/__init__.py`（空）、`backend/app/services/knowhow/grid_parser.py`
- Test: `backend/tests/test_knowhow_grid_parser.py`

**Interfaces:**
- Produces:
  - `@dataclass ParsedGrid: columns: list[str]; rows: list[list[str]]`
  - `parse_grid(filename: str, data: bytes) -> ParsedGrid`（按后缀分发 .xlsx/.xlsm→openpyxl 首个 sheet；.csv→csv 模块（utf-8-sig 容错）；.md/.markdown→管道表）
  - `guess_roles(columns: list[str]) -> list[str]`
  - 校验失败抛 `GridParseError(ValueError)`：空表头、重名列、无数据行；行长不齐时**补空对齐**到表头长度（不报错），超出表头长度截断。

- [ ] **Step 1: 写失败测试**（含真实构造的 xlsx bytes——用 openpyxl 现场生成）：三格式各一例；md 表含 `\|` 转义与 `<br>`（`<br>`→换行）；分隔行剔除；行长不齐补齐；角色猜测用例：`["违例概念","现象识别方法","根因分析动作","修复方法","依赖工具"] → ["concept","identify","root_cause","fix","tool"]`、无匹配列全 plain 且首列升 concept、双 concept 候选仅保首个。运行 FAIL。
- [ ] **Step 2: 实现**：

```python
ROLE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("concept", ("概念", "违例", "类型", "名称", "violation", "concept", "type")),
    ("identify", ("识别", "现象", "症状", "特征", "identify", "symptom", "detect")),
    ("root_cause", ("根因", "原因", "分析", "定位", "root", "cause")),
    ("fix", ("修复", "解决", "处理", "方法", "对策", "fix", "solution")),
    ("tool", ("工具", "命令", "脚本", "tool", "command", "script")),
]

def guess_roles(columns: list[str]) -> list[str]:
    roles: list[str] = []
    for name in columns:
        low = name.strip().casefold()
        for role, kws in ROLE_KEYWORDS:   # 顺序即优先级：识别先于修复，规避「××识别方法」误中 fix
            if any(k in low for k in kws):
                roles.append(role)
                break
        else:
            roles.append("plain")
    hits = [i for i, r in enumerate(roles) if r == "concept"]
    if not hits:
        roles[0] = "concept"
    else:
        for i in hits[1:]:
            roles[i] = "plain"
    return roles
```

md 管道表：仅取 `|...|` 行，`re.split(r"(?<!\\)\|", ...)` 拆格→去转义 `\|`→`|`；`:?-{3,}:?` 全格行为分隔行剔除；`<br>`/`<br/>` 替换为 `\n`。
- [ ] **Step 3: 跑通** `pytest tests/test_knowhow_grid_parser.py -x -q`。
- [ ] **Step 4: Commit**：`feat(knowhow): grid parser (xlsx/csv/md) + column role guessing`

### Task 4: 资产存储与鉴权路由

**Files:**
- Create: `backend/app/services/knowhow/assets.py`
- Modify: `backend/app/api/routes.py`（两个端点）、openapi golden（按其测试说明重生成）
- Test: `backend/tests/test_notebook_assets.py`

**Interfaces:**
- Consumes: Task 2 的 `insert_notebook_asset/get_notebook_asset`；源文件存储的根目录约定（参照 `source_files` 存储实现定位 `.local` 基目录，资产放同级 `assets/<notebook_id>/<asset_id>.<ext>`）。
- Produces:
  - `POST /notebooks/{notebook_id}/assets`（multipart `file`）→ `{"id","url"}`，`url=f"/api/notebooks/{nb}/assets/{id}"`；守卫：写权限（镜像上传源端点的权限依赖）；校验 mime ∈ image/png|jpeg|gif|webp|svg+xml 且 size ≤ 10MB，违规 400（文案友好中文）。
  - `GET /notebooks/{notebook_id}/assets/{asset_id}` → `FileResponse`（`Cache-Control: private, max-age=86400`）；守卫：读权限（镜像读取源内容端点）；跨 notebook 访问 404。
  - `AssetService.save(notebook_id, filename, mime, data, created_by) -> dict`、`AssetService.path_for(asset) -> Path`（存盘后独立存在性校验，失败即报错不静默）。

- [ ] **Step 1: 写失败测试**（TestClient，镜像现有源上传测试的 app/登录 fixture）：上传→GET 内容一致；超限/坏 mime 400；无权限用户 403/404；跨 notebook 404。运行 FAIL。
- [ ] **Step 2: 实现** service+路由（路由函数体只做参数/权限/编排，逻辑在 service——照本文件既有端点风格）。
- [ ] **Step 3: 跑通** 新测试 + openapi golden 重生成 + 四守卫测试。
- [ ] **Step 4: Commit**：`feat(knowhow): notebook image assets store + authed serving routes`

### Task 5: 确定性投影服务

**Files:**
- Create: `backend/app/services/knowhow/textops.py`、`backend/app/services/knowhow/projection.py`
- Modify: facade 若需补 KO/chunk/element 的行级删写原语（一跳委托，进 allowlist）
- Test: `backend/tests/test_knowhow_textops.py`、`backend/tests/test_knowhow_projection.py`

**Interfaces:**
- Consumes: Task 2 全部；KO/关系写入镜像 `services/kg_ingest.py:build_records` 的写法（`knowledge_objects`/`knowledge_relations`）；chunk 写入直写 chunk 表（结构见 `migrations.py` chunks DDL：`id,notebook_id,source_id,text,section_path,element_ids,created_at`）；embedding 用 `SourceEmbeddingService`（若其仅有全源粒度入口且会重算已有向量，则新增按 chunk_id 列表的增量入口，复用其单条向量写入路径）；失效钩子 `kg_mutations.invalidate_unified_cache(nb)` + `hooks.mark_unified_dirty(nb)`（用法见 `source_ingestion.py` 删除源的收尾）。
- Produces:
  - `textops.strip_images(md) -> str`、`textops.parse_steps(md) -> list[str]`、`textops.split_tools(md) -> list[str]`
  - `KnowhowProjector.ensure_hidden_source(table_id) -> str`（`source_type="knowhow"`、title=`Knowhow 表：{title}`、`parse_status="parsed"`；隐藏方式镜像 memory 合成源被排除出源列表的同一机制——先 grep `source_type` 过滤处确认，同一处加 `"knowhow"`）
  - `KnowhowProjector.project_row(table_id, row_id) -> None`（幂等）；`project_table(table_id) -> None`（全量重建=逃生口，顺带清孤儿 tool KO）；`delete_table_projection(hidden_source_id) -> None`
  - 派生 id（稳定、可重复）：`_h=lambda *p: hashlib.sha1("|".join(p).encode()).hexdigest()`；case=`ko-kh-{_h('case',row_id)[:32]}`；procedure=`ko-kh-{_h('proc',row_id,column_id)[:32]}`；tool=`ko-kh-{_h('tool',table_id,norm_name)[:32]}`；element=`el-kh-{_h(row_id,column_id)[:32]}`；chunk=`chunk-kh-{_h(row_id)[:16]}-{part}`；relation=`kr-kh-{_h(src,rel,dst)[:32]}`。

**投影算法（project_row，规格④的落地）：**
1. 取表/列/行/格；concept=概念格净文本首行（空则 `行{position+1}`）。
2. 元素：删该行旧元素（按 `json_extract(metadata,'$.knowhow.row_id')`），每个非空格写 `source_elements`：`element_type="knowhow_cell"`、`text=strip_images(content_md)`、`metadata={"knowhow":{"table_id","row_id","column_id","role","column_name","concept","content_hash":sha256(净文本)}}`。
3. chunk：按行前缀 `chunk-kh-{rowhash}-%` 取旧 chunk，若（净文本、section_path）集合未变→跳过（**不重算 embedding**）；否则删旧写新：每格一 chunk（`section_path=f"{table.title} › {concept} › {column_name}"`，>4000 字符按段落续切 part+1），`element_ids=[element_id]`；对缺向量的 chunk 增量 embed；embedding 失败→行 status='failed' + 走既有 model_error 事件通道，**不抛穿**。
4. KO/边：删 `ko-kh-{rowhash}` 前缀旧 KO 与其 `kr-kh-` 边→写 case KO（payload：`{"title":concept,"table_id","row_id","fields":{role: 净文本,...}}`）；identify/root_cause/fix 非空格各写 procedure KO（payload 含 `method_kind`、`name=f"{concept}·{column_name}"`、`steps=parse_steps(...)`、正文净文本）；tool 格 `split_tools` 逐项 upsert tool KO（表内去重）；边：`identified_by/diagnosed_by/fixed_by`（case→procedure）、`requires_tool`（case→tool）。
5. 收尾：行 status='synced'、`bump_knowhow_mutation_seq`、invalidate_unified_cache + mark_unified_dirty。

- [ ] **Step 1: textops 失败测试**：剥图（有/无 alt、多图、行内图）；parse_steps（有序/无序/续行/无列表→[]）；split_tools（列表项、换行、去重键 casefold、剔空）。FAIL。
- [ ] **Step 2: 实现 textops**（代码见规格与上文正则约定）。跑通。Commit：`feat(knowhow): textops (image strip, steps, tools)`
- [ ] **Step 3: projection 失败测试**（真 SQLite + fake embedder 记录调用次数）：project_row 两次→KO/chunk/element id 集合完全一致（幂等）；改一格→仅该格 chunk 重建、fake embedder 仅收到该 chunk；未改格 embedding 调用数=0；case/procedure/tool 数量与边类型断言；隐藏源不出现在源列表 API 数据层查询；delete_table_projection 后产物清零；embedding 抛错→行 failed 不抛穿。FAIL。
- [ ] **Step 4: 实现 projection**。跑通新测试+四守卫。
- [ ] **Step 5: Commit**：`feat(knowhow): deterministic row projection (KO/edges/chunks, incremental embed)`

### Task 6: 表/导入 API 端点

**Files:**
- Modify: `backend/app/api/routes.py`、`backend/app/models/schemas.py`（Pydantic 响应模型）、openapi golden
- Test: `backend/tests/test_knowhow_api.py`

**Interfaces:**
- Consumes: Task 2/3/5 全部。
- Produces（权限依赖镜像同 notebook 的源端点；错误文案中文友好）:
  - `POST /notebooks/{nb}/knowhow/import/preview`（multipart `file`）→ `{"columns":[{"name","guessed_role"}],"rows_preview":前 5 行,"total_rows"}`
  - `POST /notebooks/{nb}/knowhow/import`（multipart `file` + form `title` + form `columns_json`=`[{"name","role"}]` 与文件列序对齐）→ 建表+全量行入库+**后台投影**（沿用既有后台 job 线程模式，`copy_context` 传播用户上下文；镜像 KG job 的写法）→ 返回表 detail
  - `GET /notebooks/{nb}/knowhow` → `[KnowhowTableSummary]`；`GET /notebooks/{nb}/knowhow/{table_id}` → `KnowhowTableDetail`（列+行+格+行投影状态）
  - `DELETE /notebooks/{nb}/knowhow/{table_id}`（连投影产物+隐藏源）；`POST /notebooks/{nb}/knowhow/{table_id}/reproject`（全量重投影逃生口，后台执行）

- [ ] **Step 1: 写失败测试**：xlsx 上传 preview 角色猜测正确；import→表 detail 行格齐全；投影完成后行 status='synced'（测试内直接同步调用 projector 或等 job，镜像现有 job 测试手法）；只读成员可 GET 不可 POST/DELETE；无关用户 404；delete 级联；openapi golden。FAIL。
- [ ] **Step 2: 实现**（路由薄、编排进 service；`columns_json` 校验列数与文件一致、concept 恰一）。
- [ ] **Step 3: 跑通** 新测试 + golden 重生成 + 四守卫。
- [ ] **Step 4: Commit**：`feat(knowhow): import + table read/delete/reproject API`

### Task 7: 前端模型层

**Files:**
- Create: `frontend/app/knowhow-model.ts`
- Test: `frontend/app/knowhow-model.test.mjs`（顶层！）
- Setup（本任务顺带）：worktree 首个前端任务先 `cd frontend && npm install`（worktree 无 node_modules）

**Interfaces:**
- Consumes: Task 6 的 API 形状（本计划为契约，前后端并行开发）。
- Produces（Task 8/9 依赖，命名精确）:
  - 类型 `KnowhowColumn{id,name,role,position}`、`KnowhowRow{id,position,projectionStatus,cells:Record<string,string>}`、`KnowhowTableSummary{id,title,description,rowCount}`、`KnowhowTableDetail{...含 columns,rows}`、`Role = "concept"|"identify"|"root_cause"|"fix"|"tool"|"plain"`
  - `ROLE_LABELS: Record<Role,string>`（概念/现象识别/根因分析/修复方法/依赖工具/普通）
  - `rewriteAssetUrls(md: string, notebookId: string, apiBase: string) -> string`（`asset://<id>` → `${apiBase}/notebooks/${nb}/assets/${id}`，仅替换图片链接目标）
  - `cellSummary(md: string, maxLen=80) -> string`（剥图占位、去 md 记号、截断加省略号）
  - fetch 封装：`fetchKnowhowTables/fetchKnowhowTable/importKnowhowPreview/importKnowhow/deleteKnowhowTable/reprojectKnowhowTable`（走现有 `API_BASE` 约定，参照 `notebook-share.ts` 的封装风格）

- [ ] **Step 1: 失败测试**（node --test，纯逻辑不发网络）：rewriteAssetUrls 多图/无图/非 asset 链接不动；cellSummary 剥图+截断+空格子；ROLE_LABELS 全角色覆盖。FAIL（`node --test app/knowhow-model.test.mjs` 从 frontend/ 跑，镜像现有 *.test.mjs 的导入方式）。
- [ ] **Step 2: 实现**；`npx tsc --noEmit`（若项目配置了）通过。
- [ ] **Step 3: Commit**：`feat(knowhow): frontend model layer (types, asset url rewrite, fetchers)`

### Task 8: 总览网格 + 行详情抽屉（只读）

**Files:**
- Create: `frontend/app/knowhow-panel.tsx`（含 KnowhowMarkdown 小包装：复用 `answer-markdown.tsx` 的渲染配置 + rewriteAssetUrls 前处理）
- Modify: `frontend/app/page.tsx`（notebook 页新增「Knowhow 表」区块入口，挂载 KnowhowPanel；只加不改既有逻辑）
- Test: `frontend/app/knowhow-panel.test.mjs`（可测纯逻辑抽到组件文件外部导出：行过滤、列序、状态徽标映射）

**Interfaces:**
- Consumes: Task 7 全部导出。
- Produces: `<KnowhowPanel notebookId apiBase />`；内部状态 `tables list → table grid → row drawer` 三层。

**行为清单（验收标准）：**
- 表列表：标题+描述+行数，空态文案友好（「还没有 knowhow 表，可从 Excel/CSV/Markdown 导入」）。
- 网格：列头=列名+角色徽章（ROLE_LABELS，concept 列钉首列）；单元格=cellSummary 截断（1-2 行、省略号、等宽列对齐）；行首列可点击开抽屉；顶部过滤框（按概念/全文包含过滤行）；行投影状态徽标（同步中/失败可重试→触发 reproject，文案友好）。
- 行详情抽屉：右侧滑出（宽 ~640px），标题=概念，按列 position 分节（节头=列名+角色徽章），KnowhowMarkdown 渲染完整富文本（含图片）；Esc/遮罩关闭。
- 删除表带确认弹层；所有交互控件对齐既有样式体系（globals.css 变量）。

- [ ] **Step 1: 纯逻辑失败测试**（过滤/列序/徽标映射）。FAIL。
- [ ] **Step 2: 实现组件 + page.tsx 挂载**（对 page.tsx 的 diff 保持最小：import + 区块渲染 + 现有 notebook 选中态传参；**勿动弯引号**）。
- [ ] **Step 3: 跑通** node --test + `npx tsc --noEmit`；`git diff frontend/app/page.tsx | grep -c '^-.*[“”]'` 必须=0。
- [ ] **Step 4: Commit**：`feat(knowhow): read-only grid overview + row detail drawer`

### Task 9: 导入向导 UI

**Files:**
- Create: `frontend/app/knowhow-import.tsx`
- Modify: `frontend/app/knowhow-panel.tsx`（列表页「导入」按钮挂载向导）
- Test: `frontend/app/knowhow-import.test.mjs`（纯逻辑：角色下拉选项、提交 payload 组装、校验信息）

**Interfaces:**
- Consumes: Task 7 的 importKnowhowPreview/importKnowhow；Task 8 的面板刷新回调。
- Produces: `<KnowhowImportWizard notebookId apiBase onDone />` 三步：选文件（.xlsx/.csv/.md）→ 预览+角色映射（表格预览前 5 行；每列角色下拉，默认 guessed_role；表标题输入，默认文件名去后缀；concept 非恰一列时禁提交并提示）→ 提交（进度态、错误中文展示、成功回列表并刷新）。

- [ ] **Step 1: 纯逻辑失败测试**（payload 组装含 columns_json 对齐列序；concept 校验）。FAIL。
- [ ] **Step 2: 实现**；typecheck 通过。
- [ ] **Step 3: Commit**：`feat(knowhow): import wizard (preview + role mapping)`

### Task 10: 检索接通集成验证

**Files:**
- Test: `backend/tests/test_knowhow_retrieval.py`

**Interfaces:**
- Consumes: Task 2/5/6 全链路；现有检索服务测试 fixture（grep 既有 ask/检索测试怎么装 fake embedder 与查询，镜像之）。

- [ ] **Step 1: 写测试**（此任务测试即交付）：建 notebook→导入小表（3 行×5 列）→投影→断言:
  1. chunk 表内有格子级 chunk 且 `section_path="表 › 概念 › 列名"`；
  2. FTS/检索路径用修复方法里的独特词查询能命中 knowhow chunk，命中 chunk 可回溯 element metadata 的 `{table_id,row_id}`；
  3. 隐藏源不在 `GET /notebooks/{nb}/sources` 返回中，但 KO 计数/图数据包含投影产物；
  4. 删表后检索不再命中。
- [ ] **Step 2: 跑通**（若暴露真 bug——如隐藏过滤漏、section 拼接错——修在对应模块并在本任务 commit 说明）。全量跑一次 `pytest tests/ -x -q` 的 knowhow 相关文件 + 四守卫收尾。
- [ ] **Step 3: Commit**：`test(knowhow): end-to-end projection→retrieval integration`

---

## PR 收尾（编排者执行）

- [ ] 全部任务完成后：`pytest backend/tests`（全量）、前端 `node --test app/*.test.mjs` + typecheck。
- [ ] 视觉验证（编排者/用户在主 checkout 起服务后进行，worktree 不起服务）。
- [ ] rebase 到 origin/master 保持线性 → push → `gh pr create --base master`（标题 `feat: knowhow tables PR-1 (skeleton)`，正文对照规格勾交付项）。

## 后续（不在本计划）

- PR-2（格子浮窗编辑/建表向导/Excel 模板往返/LLM 表达优化）、PR-3（Agent API+MCP）在检索细节讨论收口后另立计划——判别集/引用跳转的最终形状可能吸收该讨论结论。
