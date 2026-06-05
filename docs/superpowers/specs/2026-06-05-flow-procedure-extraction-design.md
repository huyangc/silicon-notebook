# flow 章节 → 结构化 procedure 对象（二期设计）

日期：2026-06-05
分支：`claude/modest-hodgkin-8cb2c0`
关联：`2026-06-05-followup-retrieval-grounding-design.md`（一期）、`2026-06-04-large-doc-ingestion-retrieval.md`

## 背景

一期诊断 innovus 对话后修了检索侧（追问改写、流程召回排序、grounded 三档）。二期治本抽取侧——"能列不能展开"。

查证 innovus 笔记本（nb-59ce4f4923）数据现实：

- **流程其实已被抽成多个 procedure 节点**：206 个 section 含 ≥2 个 procedure 步骤，机制本就存在。
- 但 `node_context`（`sqlite_repository.py`）**按 `section_path` 精确分组**重建步骤，注释自承 "precedes edges are sparse"。问题：
  1. **过/误分组**：`64 > Overview` 把 95 个不相关 procedure 当成一条"流程"；真正的顺序信号（`precedes` 边）太稀疏没被用。
  2. 章节层级塌成 `64 > xxx`（本期**不修**）。
  3. "RTL到GDSII流程"在源里**不是章节**，只是前言伞形词、散落多章（本期**不合成**）。

确认的抽取链路：`extract_window`（`kg/extract.py`）每窗口让 LLM 出 nodes/edges → `canonicalize`（**仅合并 Concept**，Procedure 原样透传）→ `build_records`（`kg_ingest.py:62`，node→object，procedure payload 现为 `{name, section_path}`）→ `store_kg`。重抽取 `_run_extraction`（`sqlite_repository.py:1089`）按 source 删旧建新。

## 目标（范围 A，已与用户确认）

真实存在的 flow 章节 → 一等、自洽的 `procedure` 对象，payload 带**有序 `steps[]`**（每步 name + 证据）。"展开某 flow"直接取整条有序流程；过/误分组因"flow 对象即单位"而消失。

**不做**（非目标）：不修章节层级（`64 > everything`）；不跨章节合成命名流程（RTL-to-GDSII 继续由一期标「概述/推断」）；效果验收并入一期之后统一看（本期判据=正确性）。

## 产出方式（已确认）

**抽取期：LLM 直接为每个 flow 输出有序 `steps[]`**（而非依赖稀疏 `precedes` 边重建顺序）。

## 改动点

### 1. 抽取 schema + prompt（`backend/app/services/kg/extract.py`）

- `_KG_SCHEMA_HINT`（:18）：Procedure 节点可带 `"steps":[{"name":"","ev":0}]`。
- `_prompt`（:25）：新增指引——passage 描述**有序多步流程**时，出**一个** Procedure 节点（名取该 flow，可借 `section_path` 标题），其 `steps[]` 按顺序列出各步，每步 `ev` = 所在元素的整数标签；单步动作可只给 `name`、`steps` 省略或单元素。`steps[]` 是顺序的**权威来源**；`precedes` 边保留为可选、不再被分组依赖。

### 2. Node 模型 + 解析（`kg/models.py`、`kg/extract.py`）

- `kg/models.py`：新增 `class Step(BaseModel){ name:str; evidence:List[Evidence] }`；`Node` 增 `steps: List[Step] = []`。
- `extract_window`（:96-106）：解析 `it.get("steps")`，对每步用现有 `_resolve(elements, step.ev, step.name)` 绑元素、`_ev(el)` 取证据，构造 `Step`；挂到 `Node.steps`。无法绑定的步骤丢弃（与节点级一致）。

### 3. build_records → payload.steps（`kg_ingest.py:62`）

- 对 procedure 节点：对每个 `Step` 用现有 `_bind_quote(step.evidence.quote, elements, source_id, source_title)` 绑定，写 `payload["steps"] = [{"name":.., "element_id":.., "quote":..}, ...]`（只保留绑定成功的步骤，保持顺序）。非 procedure 或无 steps 的节点 payload 不变（`{name, section_path}`）。

### 4. 跨窗口合并（`kg/canonicalize.py`）

- 现 `canonicalize` 仅合并 Concept。新增：**按 `(_norm(name), section_path)` 合并 Procedure**——一个 flow 跨多个 9000 字窗口时各出局部同名节点，合并并**按文档顺序（步骤证据 char_start）拼接 `steps`、去重**。
- 合并键含 `section_path`：跨窗口的同一 flow 共享标题→合并；不同章节的同名流程（不同 section_path）**不**误并。多数 flow ≤9000 字落单窗口、此路不触发（稳健兜底）。

### 5. node_context 读 steps（`backend/app/services/sqlite_repository.py`，procedure 分支 ~2104-2131）

- procedure 的 payload 含 `steps[]` → **直接读**（有序；每步 name + 证据元素文本经现有 `_element_texts`）。
- 无 `steps[]`（未重抽取的旧对象）→ **回退**现有 `section_path` 分组逻辑。新旧混存都工作。
- 答案上下文里步骤拼接（`_answer_context` 的 `"steps: a -> b"`，:2939）不变，数据源切到 payload.steps。

### 6. 重抽取（离线作业，`scripts/`）

- 新增脚本：对指定 notebook 的所有 source 循环调用 `extract_source`（→`_run_extraction` 按源删旧建新）+ 重建知识对象向量（payload 文本含 steps，需可检索）。
- **先在 innovus 笔记本验证**；更大范围铺开是后续决定。成本=该 notebook 全 source 全窗口 LLM 调用 + 重嵌入（已接受的离线大改动）。脚本走共享 Python 解释器，幂等（重跑=再次删旧建新）。

### 检索

不新增——一期 B 已让流程类问题优先召回 procedure；flow 对象的 name + 步骤文本进入 payload 文本，天然改善关键词/语义匹配。

## 数据形状

抽取期 LLM（节选）：
```json
{"nodes":[{"local_id":"p1","type":"Procedure","name":"Foundation Flow","ev":0,
           "steps":[{"name":"import design","ev":1},{"name":"floorplan","ev":2}]}]}
```
落库 procedure 对象 payload：
```json
{"name":"Foundation Flow","section_path":"64 > Foundation Flow",
 "steps":[{"name":"import design","element_id":"SE-..","quote":".."},
          {"name":"floorplan","element_id":"SE-..","quote":".."}]}
```

## 默认值（已确认）

1. 步骤 = `{name, 证据}`，不让 LLM 另写 description（详情用证据元素原文）。
2. 铺开 = 脚本 + 先验证 innovus，不自动重抽所有 notebook。
3. node_context 对旧形态 procedure 回退。

## 测试（正确性，非效果）

- 单测：
  - `extract_window` 解析 `steps[]`、每步绑元素、丢弃不可绑步骤（mock LLM 返回带 steps 的 Procedure）。
  - `build_records` 把 steps 绑成 `payload.steps`（element_id/quote、保序）。
  - `canonicalize` 跨窗口同 `(name, section_path)` 合并、按 char_start 拼接去重；不同 section 不误并。
  - `node_context` 读 `payload.steps`；旧形态无 steps 时回退 section_path 分组。
- 集成：小型合成 flow 文档过 `extract_graph`/`build_records` → 一个带有序 steps 的 procedure 对象；存库后 `node_context` 返回有序步骤。
- 既有 `kg/` 抽取测试随 schema 变更同步更新并保持绿（steps 为可选、旧无-steps 路径不破）。
- 重抽取脚本：dry-run/小样本验证（不在一期/二期跑全量效果回归）。

## 风险

- **schema 变更影响既有抽取测试**：steps 设为可选、旧路径保持，逐个更新断言。
- **Procedure 误合并**：合并键含 `section_path`，且仅对 Procedure；单测覆盖"同名不同章不合并"。
- **LLM 不总按 steps[] 输出**：prompt 明确；缺 steps 时退化为现状（单 procedure 节点 + 旧分组回退），不劣化。
- **重抽取成本/耗时**：先 innovus 单库验证，铺开另议；脚本可断点续跑（按 source 幂等）。
