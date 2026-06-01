# KG 上线 silicon-notebook — 产品集成与瘦身设计

- 日期：2026-06-01
- 状态：brainstorming 已通过四个关键决策，待写实现计划
- 背景：`backend/app/services/kg/` 的知识图谱抽取（4 粗节点 Concept/Claim/Formula/Procedure + 富边 + 字符级证据）已完成 gold 子系统，但**未接入产品**（零产品 import，仅 scripts/tests 使用）。本设计把 KG 接成产品**唯一**的抽取与知识模型，并删除历史 qiefen / legacy / 其它文档类型，使系统只支持 `academic_paper` 与 `textbook`。

## 0. 决策摘要（已与用户确认）
1. **集成方式**：KG **替换** qiefen，复用现有「抽取→knowledge_objects→graph/检索」骨架。
2. **审核模式**：自动校验后**直接入库**（`status=approved`），浏览页可删/改；无逐条审核队列。
3. **规范化范围**：仅**单文档内** Concept 合并；跨文档合并延后（见 §7）。
4. **前后端排期**：**同一分支，后端先、前端跟上**，一次交付完整可用。
5. **attrs 已移除**（前置完成）：节点只有 `id/type/name/section_path/evidence/mentions`，`name` 承载节点文本。每类节点的细属性形态未定，记于 `fangan_todo.md`「KG 重构」，**本次不加任何节点字段**。

## 1. 目标系统形态
- 文档类型：**仅 `academic_paper`、`textbook`**。上传时二选一（或自动判定）。
- 抽取：**唯一路径 = KG**，模型 `deepseek-v4-flash`（`OPENAI_COMPAT_*`）。
- 知识模型：节点（4 类）+ 有向 typed 边，全部字符级证据 grounding。
- 检索 / 图谱：**全部基于 KG**。
- 无 LLM 端点时：抽取产出为空并把 source 标记为 `extracted`（无启发式兜底）。

## 2. 删除清单（"整理代码"）
**后端删除**
- `app/services/qiefen/`（整包）、`app/services/qiefen_ingest.py`。
- `app/services/extraction.py`（legacy LLM + 启发式抽取）。
- `extraction_profiles.py` 中除 `academic_paper`/`textbook` 外的 profile（design_spec/method/postmortem/review/general）及其对象类型（rule/method/risk/case/checklist/glossary）、`OBJECT_TYPE_LABELS`、`TEMPLATE_PROFILE` 中对应项、`_register_qiefen_types`。
- 旧 `/graph` 的 `related_*` 自由文本拼边逻辑。
- `retrieval.py` 中旧类型权威权重（rule>case>…）等只服务旧类型的逻辑。

**前端删除/改造**
- `rules`/`methods`/`risks`/`glossary` 等旧类型的浏览页与卡片组件。
- doc-type picker 收敛到 2 项。
- knowledge 浏览与 graph 视图改为 KG 类型（4 节点类型 + typed 边）。

**保留复用**
- 上传/解析/embedding 流水线、`source_elements` 表、`knowledge_objects` 表（改用 4 个新类型）、approve/embedding 机制、`/graph` 与 `/ask` 端点外壳（内部改 KG）、`process_source` 状态机。

## 3. 数据模型映射
### 3.1 节点 → `knowledge_objects`
- `object_type ∈ {concept, claim, formula, procedure}`（schema registry 注册这 4 类，删除旧类型）。
- `payload = {"name": <节点文本>, "section_path": <章节路径>}`（无 attrs）。
- `status = "approved"`（自动入库）。
- `evidence`：见 §3.3 绑定。

### 3.2 边 → 新表 `knowledge_relations`
| 列 | 说明 |
| --- | --- |
| id | 主键 |
| notebook_id / source_id | 归属 |
| source_object_id / target_object_id | 指向 `knowledge_objects.id` |
| edge_type | 12 类边之一（defines/about/supports/part_of/…） |
| evidence | JSON（字符级 span，可空） |
- `/graph` 端点改为：节点取 4 类 `knowledge_objects`，边取 `knowledge_relations`（精确端点 + 类型）。

### 3.3 证据绑定（KG 字符 span → 产品元素）
- 复用现有「quote 精确子串匹配 → 模糊回退」策略：用节点/边 evidence 的逐字 `quote` 在该 source 的 `source_elements` 中定位，得到 `element_id` + `quoted_span`，写入产品 Evidence 结构。
- 定位不到元素的节点/边**丢弃**（与"ungroundable 丢弃"一致），保证 grounding 不变量。

### 3.4 embedding
- 节点 `name` 进 `knowledge_embeddings`（复用 approve 时的 embedding 逻辑），供 `score_knowledge` 的向量召回。

## 4. 抽取接线
`process_source` 的 `_run_extraction`（替换 `_extract_records`）改为调用新适配器 `app/services/kg_ingest.py`：
```
1. 读该 source 落盘原文 + 解析出的 source_elements
2. 复用 kg.windowing.make_windows 切窗（N=9000, M=450）
3. 并发 kg.extract.extract_window(client=flash) 出 nodes/edges（局部 id）
4. kg.canonicalize 单文档内合并 Concept、重指向边
5. 校验：证据 span 逐字成立、节点类型合法、边端点存在
6. 证据绑定到 source_elements（§3.3），丢弃 ungroundable
7. 写 knowledge_objects(approved) + knowledge_relations + 节点 embedding
8. 标记 source = extracted；发 pipeline 事件（窗口数/节点数/边数/耗时）
```
- doc_type：`academic_paper`→KG `academic`、`textbook`→KG `textbook`。

## 5. KG-native 检索 + 图谱
- `/graph`：直接出 KG 节点 + 边（§3.2）。
- `/ask` / `score_knowledge`：query 向量召回节点 → 沿 `knowledge_relations` 扩 1 跳 → 用命中节点 `name` + 邻居 + evidence 生成带引用答案。类型权重改为 KG 节点类型（例如 Claim/Formula 在问答中权重高于裸 Concept；具体权重在计划阶段定，可配置）。
- `scenario-query` 等仅服务旧结构化字段的端点：若无 KG 语义则删除；保留的改读 KG。

## 6. 测试 + 迁移
- 单测：KG→knowledge_objects/relations 映射、证据绑定（精确 + 模糊回退）、校验丢弃逻辑、`/graph` 由 KG 构建、`score_knowledge` 走 KG。
- 离线 smoke：mock LLM 客户端跑 `kg_ingest`（subagent 无外网，真实 LLM smoke 在主会话跑）。
- 删除/重写旧类型相关测试。
- 迁移：本地 SQLite 单用户，旧抽取数据直接弃用（重新上传文档重建），不写迁移脚本；新表 `knowledge_relations` 建表即可。

## 7. 非目标（YAGNI / 延后，见 fangan_todo.md）
- 跨文档 Concept 合并（notebook 级 canonical）。
- 节点细属性（attrs）形态。
- 窗口阶段 reference/bibliography 过滤。
- 低置信进审核队列（当前全量自动入库）。
- 第 3 种文档类型。

## 8. 风险
- **破坏性删除**面广（后端多文件 + 前端多页），需一次成型并跑通端到端；按"后端先跑通、再改前端"降低中间态风险。
- **证据模糊回退**对 CJK/公式可能漏绑：沿用现有阈值，ungroundable 丢弃，事件日志记录丢弃量以便观测。
- **密度**：单文档上千节点 → 浏览/图谱需分页/按类型/按章节过滤（前端阶段处理）。
