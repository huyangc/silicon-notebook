# KG 节点内容丰富化（读取期为主 + 抽取 prompt 微调）— 设计文档

- 日期：2026-06-02
- 状态：brainstorming 已与用户确认方向（读取期为主），待 review 后写计划
- 背景：用户反馈 KG 节点信息太薄——Concept 节点只有名字（"MoE"），Procedure 节点没有完整流程。要求：Concept 至少含原文中该概念所在的**句子**；Procedure 含**完整流程**。

## 0. 关键发现（基于线上真实数据）
1. **富信息已被抽取，只是没被呈现**：每个节点的 `evidence` 带 `element_id`，指向 `source_elements.text` —— 那是**完整句子/段落**。例如 Concept `Engram` 的 `quoted_span="Engram"`（裸词），但其 element 文本是「As shown in Figure 1, Engram is a conditional memory module … separating static pattern storage from dynamic computation.」当前 `concept_detail` **没有** join `source_elements.text`，所以只显示了裸 `quoted_span`。
2. **Procedure 是被拆成多个 step 节点的**（retrieval phase / fusion phase / Tokenizer Compression / Multi-Head Hashing …），本应由 `precedes` 边串成有序链。但线上数据 **12 个 procedure 节点只有 1 条 `precedes` 边** —— 链基本没抽出来。因此**只靠 precedes 读取期遍历无法还原完整流程**。

## 1. 结论：以**读取期丰富化**为主，配一小步**抽取 prompt 微调**
回答「可视化时从文档截取 vs 生成 KG 时带上」：**绝大部分是读取期呈现已捕获的数据**（更省、逐字、无需重抽）；唯一需要生成期的是 **procedure 的有序链 + evidence 句子粒度**，因为 `precedes` 当前抽得太稀。

## 2. Part A — 读取期丰富化（不重抽，立即生效）
### 2.1 核心：把 evidence 扩展到「所在句子/段落」
后端新增只读 helper：给一组 evidence（含 `element_id`），join `source_elements.text` 得到 element 全文，挂到每条 evidence 上（字段 `element_text`）。逐字、无 LLM。

### 2.2 节点上下文服务 `node_context(notebook_id, object_id)`
返回（按类型给不同字段）：
```
{ id, object_type, name, section_path,
  occurrences: [{ quoted_span, element_text, source_title, section_path }],   # 该节点/各 mention 出现处的句子
  definition?: string,        # Concept: defines 它的 Claim 的文本(+element_text)
  steps?: [{ name, element_text, section_path }] }   # Procedure: 有序步骤
```
- **occurrences**：节点自身 evidence + （canonical Concept 时）成员/`mentions` 的 evidence，逐条扩成 element_text → 即「该概念出现的句子们」（含跨文档出处）。
- **Concept.definition**：沿 `defines`(Claim→Concept) 边取定义性 Claim 的文本 + 其 element_text（若有）。
- **Procedure.steps（完整流程）有序化策略**：
  1. 若该 procedure 节点经 `precedes` 能连成链 → 用 precedes 顺序；
  2. **否则回退到文档顺序**：取**同一 `section_path`** 下的所有 procedure 节点，按其 evidence 的 `char_start` 升序排列 → 近似还原「这一节里有序的流程步骤」。每个 step = {name, element_text}。

### 2.3 暴露方式
- 丰富现有 `concept_detail`（可视化右栏已用）：加 `definition` 和每条 evidence 的 `element_text`；其 `attached` 里若有 procedure，附带其 `steps`（或前端点开再取）。
- 新增 `GET /notebooks/{id}/objects/{object_id}/context` → `node_context`，供「知识库浏览器」(列表里点开任意类型，尤其 procedure) 与（将来）图上点击非 concept 节点用。

### 2.4 前端呈现
- **KG 可视化右栏**（concept 详情）：显示 definition + 出现句子；挂载的 procedure 显示其有序 steps。
- **知识库浏览器记录**：每条 evidence 显示 `element_text`（不再只裸 quoted_span）；procedure 记录显示拼出的有序 steps。

## 3. Part B — 抽取 prompt 微调（治本，惠及新抽/重抽文档）
只改 `kg/extract.py` 的 `_prompt`，**不改数据模型、不重加 attrs**：
1. **evidence 句子粒度**：要求 evidence 为**包含该节点的完整句子**（而非裸词 "Engram"）。
2. **procedure 串链**：要求把一个有序过程的连续步骤用 `precedes` 边连起来（窗口内）。
这样新文档的 procedure 链是真的，2.2 的 procedure 排序就走 `precedes` 而非文档顺序回退；concept 的 evidence 本身也更可读。
- **不做**：重加 `Procedure.steps`/`Concept.definition` 等结构化 attrs（保持模型精简；element_text join + 边已承载丰富度）。
- **存量数据**：Part A 立即生效；Part B 只惠及新抽/重抽；存量文档要更好的 procedure 链需重抽（可选，非本 spec 强制）。

## 4. 数据模型 / 迁移
- **无 schema 变更**。复用 `source_elements`、`knowledge_objects.evidence`、`knowledge_relations`。
- 读取期 join 性能：`node_context` 一次按 notebook/object 读 + 一次按 element_id 批量取 element 文本；对单节点详情足够，必要时缓存 element 文本。

## 5. 测试
- `node_context` 单测：evidence→element_text join；Concept definition 走 defines 边；Procedure steps 排序——(a) 有 precedes 链时按链序、(b) 无 precedes 时按 section_path + char_start 文档序回退。
- 边界：节点无 evidence、element 缺失、procedure 不在任何 section。
- prompt 改动：保证仍是合法 JSON 模板、占位不破坏；离线 mock 解析通过；真机 smoke（主会话）抽一篇看 evidence 是否变句子粒度、procedure precedes 是否变多。
- 前端 `tsc --noEmit` + 人工 eyeball。

## 6. 非目标（YAGNI）
- 不重加结构化 attrs。
- 不为存量文档自动重抽（用户按需触发）。
- 不在本 spec 改检索（检索可复用 node_context 的句子/步骤，另议）。
