# Knowhow 表设计规格（2026-07-15）

## 背景与目标

用户的领域知识（首例：半导体时序修复）以表格形式沉淀：行=违例类型，列=方法维度（违例概念/现象识别方法/根因分析动作/修复方法/依赖工具），格子内容为 markdown+图片的富文本。

**两个痛点：**
1. 表大（格子内容长），平铺渲染不可读；
2. 富文本中的图片只是给人看的示意，机器消费（KG/检索/代码生成）不需要。

**两个目标：**
1. 从表格构建 KG，支撑「遍历现象识别方法做判别 → 选对应修复方法」的流程；识别/修复方法的 python+tcl 代码由**外部 Agent 生成与执行**，notebook 只提供生成代码所需的结构化信息；
2. 外部 Agent 通过 API/MCP 接入，基于 KG 做分析。

## 已确认的需求决策

| 决策点 | 结论 |
| --- | --- |
| 进入方式 | 导入存量文件 + 在线维护（持续沉淀演进） |
| 通用性 | 通用机制：列自定义 + 列角色标签，时序修复只是第一张表 |
| 规模 | 单表百行内；单格几百字到几屏富文本；无需虚拟滚动/分页 |
| 图片 | 只给人看：展示层保留，KG/向量化/机器输出一律剥离，零 VLM 成本 |
| Agent 形态 | 外部 Agent 走 HTTP/MCP；notebook 不存代码、不执行代码 |
| LLM 表达优化 | 显式按钮触发（绝不自动），优化结果对照预览、用户确认后回填 |
| 建表流程 | 先定表头（列名用户现场定+打角色），再填值；填值支持应用内逐格 + Excel 模板往返 |
| 填值交互 | 网格为画布，点格子弹浮窗编辑，「保存并下一格」推进 |

## ① 数据模型

新增四张业务表 + 一张资产表（遵循 `_migration_N` 追加 + `SCHEMA_VERSION` bump 约定；id 一律 `_new_id()` 全 128 位）：

- `knowhow_tables`：`id, notebook_id, title, description, mutation_seq(单调计数器), created_by, created_at, updated_at`
- `knowhow_columns`：`id, table_id, name, role, position`
  - `role ∈ {concept(概念锚点，每表恰一列) | identify(现象识别) | root_cause(根因分析) | fix(修复方法) | tool(依赖工具) | plain(普通)}`
  - 角色驱动投影语义与 Agent API 字段命名；列名本身自由。
- `knowhow_rows`：`id, table_id, position, created_at, updated_at`
- `knowhow_cells`：`id, row_id, column_id, content_md(原始 markdown，含 asset:// 图片引用), updated_at`
- `notebook_assets`：`id, notebook_id, filename, mime, size, created_by, created_at`；文件落根 `.local/assets/<notebook_id>/`

图片引用协议：格子 markdown 中统一写 `![alt](asset://<asset_id>)`；前端渲染时改写为带鉴权的 API URL；机器侧文本替换为 `（图示：alt）` 占位。

表头可变：加列=全行补空格子；删列=确认后连格子删除；改角色=触发全表重投影（百行内代价可忽略）。

## ② 建表与填值流程

### 建表向导
1. **定表头**：用户现场输入列名（可增删改排序），每列打角色标签（concept 必选恰一列）；确认后建表。
2. **填值**，两条路可混用：

### 路A · 应用内逐格填写（网格画布 + 格子浮窗）
- **网格即进度画布**：空格显浅色占位（"+"），已填显 1-2 行截断摘要；保存后背后格子实时更新。
- **点格子弹浮窗**：空格子直接进编辑态；已填格子先进**渲染预览态**（长内容阅读入口），右上「编辑」切换。
- **浮窗结构**（约 880px 宽 / 80vh 高上限，风格对齐项目 UI 标准）：
  - 头部：`行概念 › 列名 + 角色徽章`；可展开「本行其他格子」摘要栏（写修复时回看识别措辞，不丢行上下文）。
  - 主体：markdown 编辑器 + 编辑/预览分栏或切换；轻工具栏（列表/代码/图片）；图片**粘贴/拖拽**直接上传入资产库并插入 `asset://` 引用。
  - 底部：「保存并下一格」（行内按列顺序推进，行末跳下一行首格）/「保存」（关闭回网格）/「取消」。
  - 快捷键：`Cmd+Enter` 保存、`Cmd+Shift+Enter` 保存并下一格、`Esc` 关闭（未保存内容提醒）；格子级自动草稿。
  - 步骤类角色（identify/root_cause/fix）的编辑器带轻提示：「用有序列表写步骤，系统会识别为可执行步骤」。
- **新增行** = 建空行 + 自动打开首格浮窗，一路「保存并下一格」即天然填写向导。

### 路B · Excel 模板往返
- 按当前表头生成 xlsx 模板下载（复用现有 openpyxl 依赖）：首行=列名（锁定），第二行=角色/填写说明。
- 用户线下批量填写后上传；按表头名匹配列，给出**导入预览**（将新增 N 行；概念列与已有行重名标黄提醒；未识别/缺失列报告），确认后**追加**导入。
- **边界**：模板路线只收纯文本（保留换行），不解析 xlsx 内嵌图片（锚定提取不可靠）；图片与排版精修回路A补。

### 一次性整表导入（存量迁移）
- 上传 xlsx/csv/markdown 表 → 解析成网格（不走现有拍平路径）→ 导入向导：预览 + 列→角色映射（按表头名自动猜测：违例/概念→concept、识别→identify、根因→root_cause、修复→fix、工具→tool，用户可改）→ 建表。

## ③ LLM 表达优化（显式触发）

- 入口：格子浮窗内「优化表达」按钮 + 行详情抽屉「优化整行」批量入口。**绝不自动触发。**
- 流程：LLM 重写（提示词约束：保留原意、规整结构与措辞、markdown 列表化步骤、`asset://` 图片引用原样保留）→ **原文/优化后对照预览** → 用户逐格确认 → 回填（回填才触发该行重投影）。
- 约束：走 notebook 既有 LLM 配置（含 per-user 模型配置）；`max_tokens` 上限沿用全局生成档；失败走 `model_error` 事件+前端横幅既有链路。

## ④ 确定性投影（零 LLM 构建 KG）

每张 knowhow 表挂一个**隐藏合成源**（复用 memory 确认入 KG 的既有模式），行级变更后异步重投影（防抖），产物全部挂在该源下：

- **行 → `case` KO**：标题=概念格文本；payload 按角色收纳各格净文本 + `table_id/row_id` 回链。
- **identify/root_cause/fix 格 → `procedure` KO**：payload 带 `method_kind ∈ {identify|root_cause|fix}`；格内 markdown 有序/无序列表**确定性解析**为现成 `steps[]`（无列表结构则 steps 为空、整段作正文）。不动 LLM。
- **tool 格 → `tool` KO**：按列表项/换行拆分多工具，表内按归一化名去重。
- **结构化边直写 `knowledge_relations`**：`case --identified_by--> procedure(identify)`、`case --diagnosed_by--> procedure(root_cause)`、`case --fixed_by--> procedure(fix)`、`case --requires_tool--> tool`。
- **每个非空格子 → chunk**（section 标签=`表名 › 行概念 › 列名`；超长格子按现有 chunker 续切），进向量/FTS 索引——现有 ask/reasoning/外部检索免费吃到；引用标签显示 `表名 › 行概念` 而非隐藏源文件名。
- **机器侧剥图**：`![alt](asset://…)` → `（图示：alt）`。
- **幂等与增量**：派生对象 id = 稳定函数 `f(row_id, column_id, kind)`，编辑=原地更新无 id 抖动；仅变更格子重算 embedding；每次投影 bump 表 `mutation_seq` 与 notebook `kg_mutation_seq`（计数缓存正确失效）。
- **投影状态可见**：行级状态（已同步/同步中/失败可重试）；表级「重建全表投影」逃生口按钮。embedding/模型失败走 `model_error` 可观测链路。
- **与既有 KG 打通**：概念名进 canonical 名种子机制，随「刷新图谱」参与跨源合并，不强制即时重建 unified KG。
- 新 object 类型（case/procedure/tool 扩展字段）经 `object_schemas` 注册表登记，使既有通用记录渲染器/字段标签正常显示；不接入 LLM 抽取器（那是另一条演进线，见「不做的事」）。

## ⑤ 阅读视图

Notebook 页内新增「Knowhow 表」区块：

- **总览网格**：行=概念锚点+各角色列截断摘要（1-2 行、省略号、列对齐、列头角色徽章）；支持按概念/全文过滤；百行内无需虚拟化。
- **行详情抽屉**：点行首/概念列打开；按角色分节渲染整行富文本（含图片；复用答案侧 GFM+KaTeX 渲染器）；每节「编辑」按钮打开同一个格子浮窗；抽屉内含「优化整行」入口。
- **格子浮窗预览态**：单格长内容的快速阅读入口（见②路A）。

阅读、录入、修改三条路径汇于同一格子浮窗组件。

## ⑥ Agent API（HTTP + MCP 同源）

沿用 Agent Bearer token（scope `knowledge:read`）+ notebook 白名单：

1. `list_knowhow_tables(notebook)` → 表清单 + 列/角色定义 + 行数。
2. `get_discrimination_set(table_id)` → **判别集一次取全**：所有行的 `{row_id, concept, identify_text}`；「净文本」= 剥图后的 markdown（结构保留，图片替换为占位）；一行有多个 identify 角色列时按列序合并、以列名作小标题。供 Agent 离线批量生成判别代码或运行时遍历判别。
3. `get_knowhow_row(row_id)` → 单行机器视图：逐列的角色+列名+净文本 + `steps[]` + 工具列表；判别命中后取修复方案生成代码。

- MCP 侧同名三工具，与 HTTP 端点共用同一 service 层；响应受既有 `_budget_response` 预算约束（判别集只含概念+识别列，行详情按行取，天然可控）。
- notebook 不存储、不执行代码；Agent 生成的代码若值得沉淀，走既有 `propose_memory` 候选通道回流，零新机制。
- 既有 `ask_notebook`/`search_notebook_context` 因 chunk/KO 投影自动覆盖 knowhow 内容，无需改动。

## ⑦ 权限、边界与错误处理

- **权限**：表/行/格 CRUD 随 notebook 写权限（read/write 成员拆分已有）；只读成员可看；Agent 走 token 白名单。
- **资产路由**：`GET` 带 notebook 读权限守卫；单图 ≤10MB；mime 白名单（图片类）。
- **删除**：删表级联行/格/资产引用/隐藏源/全部投影产物（KO/边/chunk/向量）；删行/删列同理级联其派生物。
- **导入校验**：空表头、重名列、行长不齐、超大格子给出明确错误；模板上传列不匹配给差异报告而非静默丢弃。
- **失败韧性**：投影异步执行、行级状态可见、可手动重试；不引入轮询（保存响应即时返回，状态变化走现有事件/刷新机制）。

## ⑧ 效率约束核对（一等约束）

- 构建 KG **零 LLM**：结构直映，边与 steps 确定性解析。
- LLM 仅在「表达优化」显式按钮处出现，用户触发、逐格确认、max_tokens 封顶。
- embedding 仅对变更格子增量重算；投影幂等、id 稳定，无重复写放大。
- 图片零模型开销（不 VLM、不向量化）。
- 无新增轮询；百行内规模不需要分页/虚拟化/新索引。

## ⑨ 测试策略

- **单元**：三格式网格解析与角色猜测；投影确定性/幂等（编辑后派生 id 稳定）；剥图与 steps 列表解析；工具拆分与去重；模板生成/回读往返。
- **API**：CRUD + 三个 Agent 端点 + 权限（owner/成员/token scope）+ 资产路由守卫；openapi golden 同步更新。
- **架构守卫**：facade 新方法走 allowlist + 一跳委托；surface manifest 行号守卫同步（新文件优先，避免移动既有行）；schema 迁移测试覆盖「已部署库补建」。
- **前端**：测试放顶层 `.test.mjs`（嵌套目录不会被 node --test 收集）。

## 交付切分（每 PR 前后端同 PR 交付）

1. **PR-1 骨架**：数据模型+迁移、一次性整表导入（xlsx/csv/md+角色映射向导）、总览网格+行详情抽屉（只读）、资产存储与鉴权路由、确定性投影+检索接通。
2. **PR-2 维护**：格子浮窗（预览/编辑双态、保存并下一格、图片粘贴上传、自动草稿）、建表向导（定表头→填值）、Excel 模板往返、LLM 表达优化（按钮/对照/确认回填）。
3. **PR-3 Agent 面**：三个 HTTP 端点 + MCP 工具、README 与 README_zh 用法文档（通用口径）。

## 默认值清单（随规格一并审阅）

- 工具格多工具拆分：按列表项/换行。
- 机器文本图片占位：`（图示：alt）`（alt 常含线索，留给代码生成当提示）。
- 模板路线纯文本边界：不解析 xlsx 内嵌图片。
- 「保存并下一格」行末行为：跳下一行首格。

## 不做的事（明确出界）

- notebook 侧代码生成/存储/执行与代码审核生命周期（代码归外部 Agent）。
- 图片 VLM 理解（全量与打标按需均不做，除非未来显式立项）。
- LLM 抽取器接入 `object_schemas`（从非结构化文档抽 knowhow 是独立演进项，不绑本特性）。
- 表格协同编辑/冲突合并（单编辑者假设，百行内规模冲突概率可忽略；写权限已由成员机制约束）。
