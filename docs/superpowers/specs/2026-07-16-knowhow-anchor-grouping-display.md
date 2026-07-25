# Knowhow 表：Anchor 分组显示（转置/合并型表格支持）

- 日期：2026-07-16
- 状态：设计定稿，待实现
- 关联：`2026-07-15-knowhow-tables-design.md`（cell-level node model 是本设计的地基）
- 触发场景：用户真实表 `know-how沉淀.xlsx`（EDA 时序违例知识沉淀）

---

## 1. 背景与动机

用户维护一批 EDA 领域的 know-how 表，典型形态与我们 knowhow 表的"每行一条记录"预设**行列相反**，且带合并单元格。以 `know-how沉淀.xlsx` 为例（转置前原貌）：

| （行=属性） | B | C | D | E |
|---|---|---|---|---|
| **违例概念** | hold和setup打架 | | | |
| **现象识别方法** | 单条 path 跨多 corner s/h 打架（`B2:E2` 合并） | ← | ← | ← |
| **根因分析动作** | inst 变化率过大 | cell delay 占比大 | noise 吃 s/h | clock latency 长 |
| **修复方法** | 换 VT 开窗 | 底层走线 shielding | 提高 victim | CP 重新挂树 |
| **依赖工具** | pt | pt/innovus | pt/innovus | pt/innovus |

两个结构性问题：

1. **行列写反**：属性是行、案例是列（B/C/D/E 四个诊断-对策分支）。我们的表预设是"属性是列、每行一条记录"。
2. **跨列/分组合并**：
   - 现象行 `B2:E2` 是**真合并单元格**（四个分支共享同一现象）。
   - 违例概念行只有 `B1` 写了一次（`C1/D1/E1` 空，**非合并**）——整表只讲一个违例概念，作者按"分组列只写一次"的电子表格惯例填。

**用户已决定的处理方式（本 spec 前一轮确认）：**
- 行列反的问题走 **A 方案**——用户在 Excel 里手动转置（copy → paste special → transpose）后再导入，本 spec **不做自动转置探测**。
- 转置后，原本的横向共享变成纵向共享：现象列变成跨行合并、违例概念列变成"首行有值、后续行空"的分组列。本 spec 负责让这种**转置后的分组型表**导入不丢数据、显示贴合原表、KG/检索归并到一个概念。

### 1.1 现状为什么会崩

- **导入解析**：现有 `grid_parser._extract_xlsx_rows` 用 `read_only=True`，openpyxl 流式读取**不解析合并信息**，合并区只有左上角有值、其余为 `None`。表头行若含合并会崩成"空列名"，数据行会掉大片。
- **显示**：现有 `KnowhowTableGrid` 把所有**物理行**平铺。转置 + 补齐后，同概念的多行会显示成 N 行重复的"违例概念/现象"，观感崩坏，且丢失"它们同属一个概念"的结构。

---

## 2. 现状地基（本设计不改动的部分）

### 2.1 Cell-level node model（`projection.py`，design doc §④）

- 每个**非空格子** → 一个 KO，`object_type` = 该格的**列名**（动态类型）。
- **同列同值跨行归并成一个 KO**：身份 = `(table_id, column_name, value_key(text))`，`value_key` 归一化大小写/空白/标点。每个贡献行累积一条 evidence + 一个 `payload.rows`。
- **行 = 星形**：每个非 anchor 格子的 KO `--about-->` 该行 anchor 格子的 KO；边 id 内容派生，跨行同端点自动 collapse。
- **anchor 空的行不投影**（`projection.py:_accumulate_row_knowledge`：`if not anchor_text: return`）——不产生 row-title KO，其他格子也没有 about 目标。
- 无 anchor 的**记录型表**投影零 KO/边，只做检索。

**关键推论**：把用户表 fill 成"每个分支行 anchor 都等于同一个概念值"后，现有模型**自动**产出用户想要的结果——一个 anchor KO（违例概念）+ 所有分支的根因/修复/工具 KO 星形挂上去。**这正是用户"KG 只有一个 anchor 节点、挂多列值"的诉求，无需改 KG 层。**

### 2.2 检索（design doc §④/PR#273）

- **per-cell chunk**（默认路径）：每格一个 chunk 带向量，`section_path = 表名 › 行概念 › 列名`（行概念 = anchor 值），默认参与向量/FTS 检索。
- **KG-node 检索**（`Settings.knowhow_kg_node_retrieval_enabled`，**默认开、可设 false 回滚**）：让 cell KO 通过 chunk 反查拿语义分，命中 anchor KO 可顺 about 边带出整组；关闭只影响直接节点路径，不移除逐格 chunk 召回。

---

## 3. 设计目标

1. 转置 + 分组/合并型表导入**不丢数据、不崩**。
2. 显示以 **anchor 概念**为单位，贴合原 Excel（一屏看全），不平铺重复行。
3. 保留**横向对应**：同一分支的根因 ↔ 修复 ↔ 工具 一眼可辨。
4. KG/检索**复用现有归并**，不新增召回开销（遵循 efficiency-first 约束）。
5. 有 anchor 表启用新视图；记录型表**零改动**。

---

## 4. 分层设计

### 4.1 导入层

#### 4.1.1 合并单元格 fill（已实现）

`_extract_xlsx_rows` 改为非 read-only（`load_workbook(data_only=True)`），新增 `_expand_merged_ranges(rows, ranges)`：遍历 `sheet.merged_cells.ranges`，把每个合并区左上角值填满其覆盖的所有单元格（横向/竖向/矩形一视同仁）。

- 覆盖用户表的现象行 `B2:E2`（转置后成竖向合并 → 各分支行现象列填同值 → 归并成一个现象 KO）。
- **为什么 fill 是 KG 正确性的前提**：anchor 列合并延续行不 fill 就是空，会被 `projection` 的 `if not anchor_text: return` 整行丢弃——用户表 4 个分支里 3 个的根因/修复会彻底不进 KG。
- 已交付：`backend/tests/test_knowhow_grid_parser.py` 新增 5 个测试（横向/表头/数据行/竖向/多合并共存/矩形），全绿。

#### 4.1.2 Anchor 列 forward-fill（新增，待实现）★ 复审重点

违例概念列是"只写一次"的**分组列**（非合并），fill 救不了它转置后的空。需要对**用户在导入向导选定的 anchor 列**做 forward-fill：空值继承上一个非空概念值。

- 时机：`grid_parser.parse_grid` 阶段**不知道**哪列是 anchor（anchor 在向导里由 `anchor_index` 选定），故 forward-fill **不能**放在 `parse_grid`。放在**落库前**——导入流程确定 anchor 列后，对该列做 forward-fill 再写行数据（`app/services/knowhow/api.py` 的 import 路径，anchor 归属确定处）。
- 语义：仅对 anchor 列生效；其他列的空是真空，**不** forward-fill。
- 对正常表无害：每行 anchor 都有值时 forward-fill 无操作。
- Tradeoff（复审确认点）：有 anchor 表里某行 anchor 真的该空（"这行没概念"）时，forward-fill 会把它归入上一个概念。判断：分组语义（继承）比"孤立空概念行"更常见，且现状下空 anchor 行本就被 KG 丢弃，forward-fill 反而救活。默认 forward-fill。
- 与 4.2.2 空概念行的衔接：forward-fill 后仍空的（表最开头就空、前面无非空可继承）按 4.2.2 独立显示 + 提示补概念。

### 4.2 主网格：G2 合并矩阵

现有 `KnowhowTableGrid` 的 tbody 从"物理行平铺"改为"**相邻同值自动 rowspan 合并**"的完整矩阵。

#### 4.2.1 相邻同值自动合并（不存 span 元信息）

渲染时，anchor 列及其他列**相邻行值相同就合并成一个 rowspan 格**。合并不落库、纯显示层现算。

- 与 KG "同列同值归并"用**同一判据**——网格里合并成一格的，就是 KG 里归并成一个节点的，所见即所得。
- 编辑免维护：改了值合并自动重算，无 span 元信息要同步。
- 拆开/并回免费：把某分支某格改成与邻居不同 → 该列合并自动散开；改回同值 → 自动并上。**没有"合并/拆分"这种独立操作**。

#### 4.2.2 同概念行相邻排序（补充决策 1）

rowspan 合并要求同概念物理行相邻。主网格按 **anchor 分组稳定排序**：同概念聚在一起，组内保持原 `position` 顺序。即使底层存储顺序被打乱，显示也正确合并。

- 空概念行（4.1.2 forward-fill 后仍空的）排在其自然位置，**独立显示不并入任何组**，附"补概念"提示（`canEdit` 时可点直接补）。

#### 4.2.3 适用范围（补充决策，已确认）

- **有 anchor 表** → 启用 G2 合并矩阵。
- **记录型表（无 anchor）** → 保持现状平铺网格，**零改动**（无 anchor 无分组依据，且它本就不建 KG）。
- 运行期按 `detail.anchorColumnId` 是否存在切换两种渲染。

### 4.3 详情：C 矩阵抽屉

点主网格某个概念（合并的概念格）→ 打开**概念矩阵抽屉**（替代现有"行详情抽屉"在该场景的角色）：

- 布局：**属性为行、分支为列**，共享属性（如现象）跨列合并；最贴合原 Excel。
- 顶部：概念名 + 分支数。
- 每个"分支 × 属性"格子独立可点（编辑入口，见 4.4）。
- 长文本在矩阵格内可滚动/截断 + 点开看全（复用现有格子浮层做全文查看/编辑）。

> 记录型表不涉及此抽屉；其现有"行详情抽屉"保持不变。

### 4.4 编辑交互

| 触点 | 语义 | 复用/新增 |
|---|---|---|
| **普通格**（分支 × 属性，如"分支3根因"） | 编辑该格 | ✅ 复用现有格子浮层（预览/编辑双态、图片、代码附件、优化） |
| **主网格合并格**（概念名 / 共享列） | 编辑**整组**共享值：同步写回该组所有 N 行该列；浮层显示"这会同时改该概念下全部 N 个分支"影响范围提示 | 浮层新增影响范围提示 + 批量写 |
| **拆单个分支** | 在 C 抽屉里点该分支那格单独改成不同值 → 主网格该列合并自动散开（4.2.1） | 复用格子浮层 |
| **加分支** | 概念组内"+ 分支"→ 新物理行（anchor = 该概念，其余列空待填） | 复用 `addKnowhowRow`，预填 anchor |
| **加概念** | 网格底"+ 违例概念"→ 新行 + 新 anchor 值 | 复用 `addKnowhowRow` |
| **删分支** | 删该物理行 | 复用行删除 |
| **删整个概念** | 删该概念下全部 N 行，二次确认 | 批量行删除 |

**"去掉按行显示"落地**：现有"点行标题开行详情抽屉"在有 anchor 表被 G2 + C 抽屉取代；"添加行"按钮语义化为"加分支/加概念"。物理"行"仅底层保留（承载 KG/检索/横向对应），UI 不再暴露"第几行"。

### 4.5 检索与引用

- **召回逻辑**：per-cell chunk 召回始终保留（同概念格子语义相近，命中一个大概率带出整组）；KG-node 检索默认开启（`knowhow_kg_node_retrieval_enabled`），可显式关闭以只保留 chunk 路径。默认开启的动态类型必须按 Knowhow 隐藏来源收窄，旁挂矩阵跨子查询缓存，版本同时覆盖 KG 变更和纯向量修复（efficiency-first）。
- **引用跳转改目标**（必做）：ask 引用命中 knowhow chunk 时，现在跳"行详情抽屉"——改为跳**概念矩阵抽屉（C）并高亮命中的那个分支格**。定位信息现成（chunk `section_path = 表名 › 概念 › 列名` + element 的 row/column 归属）。

### 4.6 文案

违例概念下的多个"根因-对策"，UI 统一叫**「分支」**（诊断树的分支义）。复审可改（情况/成因/case）。

---

## 5. 数据流（以 hold&setup 表走一遍）

1. 用户在 Excel 转置原表 → 保存 → 导入。
2. `parse_grid` → `_extract_xlsx_rows` fill 合并单元格（现象列各分支行填同值）。
3. 向导选 anchor = 违例概念列 → 落库前对该列 forward-fill（分支2/3/4 继承"hold和setup打架"）。
4. 底层落库：4 个物理行，anchor 列同值、现象列同值、根因/修复/工具各异。
5. `project_table`：违例概念 4 行同值 → 1 个 anchor KO；现象同值 → 1 个 KO；根因/修复各 4 个 KO；工具 2 个 KO（pt / pt-innovus）；全部 `--about-->` 违例概念 KO。per-cell chunk 各就位。
6. 主网格 G2：违例概念/现象列 rowspan 合并成一格，根因/修复/工具各分支独立 → 视觉 = 原 Excel。
7. 点违例概念 → C 矩阵抽屉，属性为行、4 分支为列。
8. ask "hold&setup 怎么修" → 多个分支的修复 chunk 命中 → LLM 拼出完整答案 → 引用跳 C 抽屉高亮命中分支。

---

## 6. 边界与错误处理

- **单分支概念**：某概念只有 1 行 → 不发生合并，正常单行显示（无特判）。
- **只有 1 个概念的表**（用户当前表）：整表一个组，G2 = 一个合并块。
- **空概念行**：见 4.2.2，独立显示 + 提示，不并组、不进 KG。
- **长文本矩阵挤压**：C 抽屉格内滚动/截断 + 点开全文（复用浮层）；G2 主网格同理。
- **合并格编辑一致性**：整组批量写在单事务内完成；失败原地报错不半改。
- **概念改名**：编辑合并的概念格 = 改整组 anchor 值 → 触发重投影（KO 身份变，旧 KO 拆、新 KO 建，现有 reproject 幂等重写覆盖）。
- **相邻同值误合并**：两个语义无关的分支恰好某列相邻同值会显示成一格——接受（语义上它们该列确实相同；用户改任一值即散开）。

---

## 7. 测试策略

- **后端**：
  - 合并 fill：已交付 5 测试（`test_knowhow_grid_parser.py`）。
  - anchor forward-fill：新增单测（首行有值后续空 → 填满；多概念分段填；开头空不误填；正常表无操作）。
- **前端**（`node --test`，纯逻辑抽到 `*-logic.ts`）：
  - G2 合并计算：相邻同值分组、rowspan 计算、空概念独立、稳定排序。
  - C 矩阵抽屉：属性×分支矩阵构造、共享属性合并、命中分支高亮。
  - 编辑：合并格改整组的批量写目标计算、拆单个后合并散开。
  - 记录型表：无 anchor 时回退平铺（不启用 G2）。

---

## 8. 改动清单（供实现计划）

- **后端**
  - `app/services/knowhow/grid_parser.py`：合并 fill（已改）。
  - `app/services/knowhow/api.py`（或导入落库处）：anchor 列 forward-fill（新增）。
  - `backend/tests/test_knowhow_grid_parser.py`：fill 测试（已加）+ forward-fill 测试（新增）。
- **前端**
  - `knowhow-panel.tsx`：有 anchor 表切 G2 合并矩阵渲染；同概念稳定排序；加分支/加概念/删概念入口；记录型表回退现状。
  - 新 `knowhow-matrix-drawer.tsx`（或扩展现有抽屉）：C 概念矩阵抽屉。
  - `knowhow-panel-logic.ts` / 新 logic 文件：合并分组计算、rowspan、排序、矩阵构造（纯函数 + `*.test.mjs`）。
  - 格子浮层（`knowhow-cell-editor.tsx`）：合并格编辑的"影响范围"提示 + 批量写回调。
  - 引用跳转（answer/citation 侧）：目标改为概念矩阵抽屉 + 高亮命中分支。

---

## 9. 不做的事（scope 边界）

- **不做自动转置探测**：用户手动在 Excel 转置（A 方案）。
- **不改 KG 投影逻辑**：现有 cell-level model 已满足归并诉求。
- **不改检索召回逻辑**：默认 per-cell chunk + KG-node 检索既有 opt-in；不新增"命中带出整组"。
- **不做记录型表分组**：无 anchor 表保持现状平铺。
- **不存 Excel 原始合并 span 元信息**：合并靠"相邻同值"现算。
- **不改动上一 PR 的空胶囊修复 / 浮层全屏 / 表头 inline 改名**（已在 PR #277）。
