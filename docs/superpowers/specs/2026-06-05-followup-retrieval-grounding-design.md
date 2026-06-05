# 追问检索 + grounding 重定义（一期设计）

日期：2026-06-05
分支：`claude/modest-hodgkin-8cb2c0`
关联：`2026-06-04-kg-multiturn-backend.md`、`2026-06-04-kg-answer-redesign.md`、`2026-06-04-large-doc-ingestion-retrieval.md`

## 背景与问题诊断

innovus 笔记本上的一段 6 轮真实对话（`conv-7806fcc8a7`）暴露了多轮深入追问时的效果问题。逐轮核实：

| 轮 | 问题 | grounded | 检索数 | 锚点[k] |
|---|---|---|---|---|
| 1 | innovus是什么工具 | ✅ | 12 | 7 |
| 2 | innovus中有哪些常见flow | ✅ | 12 | 11 |
| 3 | 展开讲讲RTL到GDSII的流程 | ❌ | 12 | 6 |
| 4 | 把这个流程按阶段画成流程图 | ❌ | 12 | 0（答案以 `（推断）` 开头）|
| 5 | …不是有RTL到GDSII的流程吗？为什么找不到 | ✅ | 12 | 1 |
| 6 | 你来帮我读这一章，然后展开讲讲这个流程 | ✅ | 12 | 11 |

关键事实（已查证）：

- **检索每轮都跑、每轮都返回 12 条**（`ask()` 无任何"是否检索"门控）。"停止检索"是表象。
- 真正发生的是：深入追问时这 12 条与问题**不相关**，LLM 自报 `grounded=false`，退回通用知识推断。
- T3「展开 RTL→GDSII 流程」召回的 12 条**全是 claim、只有 1 条真相关**，其余是 token 撞上的碎片（map 文件图层、3D-IC 导出、flip-chip RDL、micro-bump…）。
- 全库只有 **1 条 claim + 1 条 concept** 提到 "RTL-to-GDSII"，原文里该词组只出现在手册前言一句概述；**没有任何 procedure 对象**描述其步骤（库里有 2272 个 procedure，但没一个是这个流程）。

### 四条根因

1. **无 query 改写 / 指代消解**：`ask()` 里 `query = question` 直接带过（`sqlite_repository.py:2717`），注释明确"retrieval runs fresh per question, history shapes wording only"。T4 的"这个流程"无法被历史补全，于是召回无关对象、0 锚点、纯推断。
2. **procedure 被系统性压低**：跨类型排序乘 `_TYPE_WEIGHT`（claim 1.0 / procedure 0.7 / concept 0.5，`retrieval.py:80` × `sqlite_repository.py:2767`）。于是"问流程"反而召回 12 条 claim、0 条 procedure。
3. **grounded 只看 LLM 自报**：top_hits 恒非空（12），`grounded = LLM自报 AND 有命中`（`sqlite_repository.py:2972`）退化为纯 LLM 布尔，几乎同一个问题在 T3/T6 之间忽 0 锚点忽 11 锚点。
4.（治本，归二期）**KG 对象原子化**：答案上下文只由 top-12 个 KG 对象拼成，原始 `source_elements` 只当相似度信号、不进上下文，连续章节被打碎后无法重组。

## 一期目标（两条诚实的目标）

1. **追问能命中已有内容**：修掉根因 1、2 —— 指代消解 + 让流程类问题召回 procedure。
2. **库里确实只有概述时，老实说**：修掉根因 3 —— grounded 改为相关度感知的三档，前端如实标注「有据 / 概述 / 推断」，而不是把薄证据上的外推伪装成有据。

> 明确认知：T3 这类问题在 innovus 原文里很可能根本没有详细步骤，一期不试图把它"硬变成有据"——正确结果是诚实标注为「概述/推断」。能靠一期救回的是 T4 的指代漏检与 T3↔T6 的忽好忽坏。

## 一期范围（A / B / D / E）

### A. 追问改写（指代消解）—— 轻量 LLM 改写 + 触发门控

落点：`SqliteRepository.ask()`（`backend/app/services/sqlite_repository.py`）。

- **调整执行顺序**：现在 `query = question` 在前、`history` 在后加载。改为先 `_ensure_conversation` + `_conversation_history` 拿到 `history`，再决定检索 query。
- 新增 `_rewrite_followup_query(history: str, question: str) -> str`：
  - **门控（启发式，零成本，CJK 感知）**：满足才改写——
    - `history` 非空（首轮永不改写），且
    - 问题"像追问"：长度 ≤ `FOLLOWUP_MAX_LEN`（默认 20 字），或命中指代标记集合 `{这个,那个,这些,那些,它,它们,他们,上面,上述,前面,刚才,这一,那一,该,此,继续,接着,展开,再讲,这种,这样,如上,同上,这块,这部分,这章,这节,…}`（英文：`it/this/that/these/those/above/former/latter` 词界匹配）。
  - **触发时**：一次 LLM 调用 `llm_client.chat_json`，用新 prompt `followup_rewrite_prompt(history, question)`，要求把"历史 + 当前问"改写成**同语言、独立、简洁的检索 query**，把指代解析成具体实体（"这个流程"→"RTL到GDSII流程"），返回 `{"query":"..."}`，不作答。
  - **回退**：LLM 失败 / 返回空 → 用原 `question`，绝不阻塞。
- **检索用改写后的 query**（embedding + keyword 都用它）；**回答 prompt 仍用用户原话 `question`**（答用户字面问的）。
- 新方法只读 `history` 字符串，不依赖 DB 句柄；纯函数 `_looks_like_followup(question)` 便于单测。

### B. 流程类问题的召回排序

落点：`backend/app/services/retrieval.py` + `ask()` 排序处（`sqlite_repository.py:2767`）。

- **意图识别** `is_process_query(text) -> bool`（基于**原始 question**判定，因"展开/画成流程图"在原话里）：关键词 `流程/步骤/怎么/如何/展开/阶段/画成/过程/顺序/先后/flow/step/procedure/process/pipeline/stage/walkthrough`。
- **意图条件化的类型权重**：新增 `_PROCESS_TYPE_WEIGHT = {"procedure":1.0, "claim":0.9, "formula":0.9, "concept":0.6}`（procedure 不再被惩罚、略占优）。封装 `type_weight(object_type, process_intent)`；`ask()` 的排序 key 改为按此取权重。非流程问题保持现有 `_TYPE_WEIGHT` 不变。
- **类型配额（防塌缩）**：流程意图下，若库里有 ≥`PROC_MIN`（默认 2）条 procedure 越过 `RELEVANCE_FLOOR`，保证 top-N 至少含这么多条 procedure —— 实现为排序后的后填：用次优 procedure 顶替选中集合里最弱的非 procedure 项，且不驱逐当前最强命中。

### D. grounded 重定义 + 前端徽章

落点：`_answer_kg`（`sqlite_repository.py:2972`）、`AskResponse`（`backend/app/models/schemas.py`）、前端答案卡片。

- `_answer_kg` 已持有 `top_hits` 与解析后的 `anchors`。每个 `RetrievedKnowledge` 带 `.relevance`（纯融合相关度 ∈[0,1]）；已查证现 `ask()` 路径不传 scenario、boost=0，故 `.score == .relevance`。阈值一律按 `.relevance`：
  - `anchored_rel` = 被答案 [k] 锚点引用到的命中里的最高 `.relevance`（没有锚点则 0）。
  - `top_rel` = `max(h.relevance for h in top_hits)`（无命中则 0）。
- **三档 `evidence_level`**：
  - `grounded`（有据）：`anchored_rel ≥ τ_high` 且 `len(anchors) ≥ 1` 且 LLM 自报 grounded。
  - `overview`（概述）：非 grounded 且 `top_rel ≥ τ_low`（有相关但偏薄）。
  - `inferred`（推断）：其余（无命中 / `top_rel < τ_low` / 0 锚点）。
- **阈值**：`τ_low` 默认 0.18、`τ_high` 默认 0.35，经 env `EVIDENCE_TAU_LOW/EVIDENCE_TAU_HIGH` 可调。**视为暂定值，最终标定推迟到二期完成后**。
- **兼容**：保留 `grounded: bool`（= `evidence_level=="grounded"`）与 `llm_mode` 原字段；`AskResponse` **新增 `evidence_level: str`**。
- **前端**：答案卡片按 `evidence_level` 渲染徽章——有据(绿) / 概述(黄) / 推断(灰)。粒度=**每条回答一个徽章**（不做逐句、不做逐 flow 项标注）。

### E. 可观测字段（只落库，不在一期跑效果回归）

`AskResponse` / answers.payload 增：

- `retrieval_query: str` —— 实际用于检索的 query（原问或改写后），便于排错与日后标定。
- `evidence_level: str` —— 见 D。
- `top_relevance: float` —— `top_rel`，供阈值标定。

不新增表。这些字段支撑二期完成后的统一效果评估。

## 非目标 / 推迟项

- **C 原文段落兜底检索** → 二期。
- **flow 章节重抽取出结构化 procedure 对象（治本）** → 二期。
- **效果回归 / 阈值最终标定** → 一期**不做**；按用户要求，效果在一期+二期全部完成后统一看。
- 逐句 / 逐 flow 项标注、检索是否检索的门控 → 不做。

## 数据 / 接口变更

- `AskResponse`（`schemas.py`）+ `retrieval_query`、`evidence_level`、`top_relevance` 三字段；既有字段不动。
- `prompts.py` + `followup_rewrite_prompt(history, question)` 及其 schema 提示。
- `config.py` + `FOLLOWUP_MAX_LEN`、`EVIDENCE_TAU_LOW`、`EVIDENCE_TAU_HIGH`、`PROC_MIN`（均 env 可调，带默认）。
- 无 DB schema 迁移（payload 是 JSON，旧记录读取时字段缺省即可）。

## 测试（一期"完成"判据 —— 是正确性，不是效果）

- 单测：`_looks_like_followup`（标记/长度/首轮不触发）、`is_process_query`、`type_weight` 意图切换、`evidence_level` 三档分类（构造合成 score/anchor）、改写回退（LLM 异常/空返回 → 用原问）。
- 改写门控：mock `llm_client`，断言标准问题**不**触发改写、追问**触发**改写。
- 不破坏既有行为：`history` 为空时（首轮）路径与现状一致；现有测试全绿。
- 冒烟：`/ask` 返回体含 `evidence_level`、`retrieval_query`；按需对齐现有 smoke 期望（参考既往 `test(smoke): 对齐 L4`）。

## 风险

- 改写 prompt 质量影响召回：用门控限制触发面 + 失败回退原问兜底。
- 阈值 τ 暂定，可能初期分档不准：env 可调，二期统一标定，不阻塞。
- 配额后填逻辑需保证不驱逐最强命中：实现时加保护并单测。
