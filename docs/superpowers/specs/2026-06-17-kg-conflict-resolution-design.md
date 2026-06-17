# KG 冲突消解（Conflict Resolution）— 设计文档

- **Date:** 2026-06-17
- **Status:** Draft for review
- **Branch:** `claude/condescending-hoover-e0f854`
- **来源:** 借鉴 MemGraphRAG（KDD'26, arXiv:2606.00610）的 Global Adjudication / Conflict Resolution。论文消融中该模块是 HotpotQA 上最大杠杆（full 69.40% → w/o 66.95%，−2.45%）。

## 1. 背景与目标

当前 KG 抽取产出三元组后**没有任何矛盾治理**：`contrasts_with` 边能抽出但无下游消费，`USABLE_STATUSES` 里预留的 `'conflict'` 状态全代码无处赋值（`sqlite_repository.py:123`）。矛盾/冗余三元组会污染严格推理链。

**目标：** 在**现有封闭 schema**（节点 Concept/Claim/Formula/Procedure + 12 类边）上检测矛盾三元组，用证据原文做 LLM 裁决，写回 keep/discard/modify，从而清洗 KG、提升 reasoning/graph 严格推理质量。

**预期边界（重要）：** 边级改动只影响 reasoning/graph 模式（`_rx_graph`/`_federated_rx_graph` 按 `review_status!='rejected'` 过滤，`:3930`/`:4024`）；**默认 chunk-native 问答不读 KG，无感**。即本特性提升的是 KG 现在唯一的用途——严格推理。

**非目标（OUT，本期不做）：** 开放关系名 / schema 自动归纳 / 频率过滤 / hub 抑制 / 信息密度 / 三层记忆结构 / 增量式触发 / 跨 tier 联邦候选召回。

## 2. 范围（标准版 A）

- **per-notebook**，建图后台跑一遍；独立 **opt-in** 端点 + 可选挂 `build_notebook_kg` 末尾。
- 检测**两级**冲突：矛盾 Claim 节点（对象）+ 矛盾关系边。
- 三类：**mutual / temporal / granularity**（一次 LLM call 分类）。
- 写回：**评审队列 + 高置信自动应用**；**base 赢**。
- 默认关（config flag），随你们"新增强先默认关"惯例。

## 3. 架构与数据流

```
build_notebook_kg 末尾(开关开) 或 POST .../kg/conflicts/resolve(后台线程)
        │
        ▼
[1] 候选召回 (kg/conflict_detect.py) ── 结构为主、稀疏
        │  · corroboration_counts 三元组分组(edge_trust.py:108) → 共享 head/tail 的断言簇
        │  · _discriminative_conflict(kg_merge.py:100) → 对立 token 信号(mutual/granularity)
        │  · 可选 _ann_candidates(kg_merge.py:112) over 端点对象 knowledge_embeddings → 语义近邻
        ▼
[2] LLM 裁决 (kg/conflict_review.py，仿 concept_merge_review.py)
        │  · 取双方 evidence → source_elements/node_context(:1473/:4570) 解析回原文
        │  · 一次 call: {conflict_type, resolution(keep/discard/modify), winner, confidence, rationale, resolved_payload?}
        │  · 主 LLM(deepseek-v4-flash), temperature=0, JSON mode(仿 kg/client)；bilingual prompt
        ▼
[3] 入队 + 写回 (kg_conflict_candidates 表 + apply 逻辑)
        │  · confidence ≥ τ_auto → 自动应用；否则 status='pending' 等人审
        │  · keep: 不动(记 winner)
        │  · discard 边: set_edge_review('rejected') (:2412)
        │  · discard Claim 节点: update_knowledge(status='conflict') (:3499)  ← 仍 usable、可逆、可审计
        │  · modify: update_knowledge(payload=...) 塞入时间区间/粒度标注 → 自动 re-embed(:3533)
        │  · base 赢: 候选带 tier(:2265)，base vs personal 矛盾默认 keep base / discard|conflict personal
        ▼
[4] 收尾: _invalidate_unified_cache(:2557) + _mark_unified_kg_dirty
        ▼
[5] 评审 API: GET pending / POST confirm / POST reject (仿 merge 端点 routes.py:687/711/720)
```

## 4. 数据模型

**新表 `kg_conflict_candidates`**（仿 `concept_merge_candidates` DDL `:481-487`）：

| 列 | 说明 |
|---|---|
| id | PK |
| notebook_id | |
| kind | `'node'` / `'edge'` |
| left_ref / right_ref | 冲突双方的 object_id 或 relation_id（含其 tier） |
| conflict_type | `mutual` / `temporal` / `granularity` |
| resolution | `keep` / `discard` / `modify` |
| winner_ref | keep/discard 时的保留方 |
| resolved_payload | modify 时的新 payload（JSON） |
| confidence | float |
| rationale | LLM 裁决理由 |
| status | `pending` / `applied` / `rejected` |
| created_at / updated_at | |

- 复用 `'conflict'` 对象状态；边 discard 走 `review_status='rejected'`。
- **不改** `knowledge_objects` / `knowledge_relations` 既有结构。

## 5. 配置开关（仿现有 opt-in，default 全关/保守）

- `KG_CONFLICT_RESOLUTION_ENABLED`（default `False`）— 是否在 `build_notebook_kg` 末尾自动跑。
- `KG_CONFLICT_AUTO_APPLY_THRESHOLD`（default `0.95`）— 自动应用阈值；**设 `1.0` = 纯评审模式**（即保守版 B 的行为，可配置达成）。
- `KG_CONFLICT_SIM_THRESHOLD`（default `0.8`）— 语义候选阈值（对齐 `resolve_conflict.py` 默认）。
- 裁决 LLM 复用主 LLM 端点（URL 接入）。

## 6. 不变量与安全

- **不碰打分**：`_fuse` / `classify_evidence` / `tier_weight` / `score_knowledge`（`retrieval.py:143/291/116/338`）一律不动。本特性只改图内容（status / review_status / payload），by-construction 不影响 [0,1]/tau、dual-index best-of、联邦不跨库归一。
- **可逆**：discard 用 `'rejected'`/`'conflict'` 状态而非物理删除 → 评审里可撤销。
- **缓存**：写回后**必须显式** `_invalidate_unified_cache`（就地编辑同秒版本键不变的坑，`:2562` 注释）。
- **成本**：候选稀疏（共享端点 + 相似度双闸）；LLM call 数 ≪ 三元组数；后台异步不卡建图。
- **两路一致性提醒**：边 discard 只影响 reasoning/graph；chunk-native 默认问答不受影响（预期，非 bug）。

## 7. 测试策略（TDD）

- 单测：候选召回（造矛盾三元组 fixtures）、三类冲突分类、写回三动作、tier base-wins、缓存失效。
- 不变量回归：跑现有 `retrieval` 测试，确认 [0,1]/tau 不动。
- 端到端：小语料建图 → resolve → 校验矛盾边被 `rejected` / 矛盾 Claim 标 `conflict`、modify 改了 payload 且 re-embed。

## 8. 任务拆解（subagent-driven，逐任务 TDD）

- **T1** 新表 `kg_conflict_candidates` DDL + 迁移 + CRUD（仿 `concept_merge_*` `:2461-2490`）。
- **T2** 候选召回模块 `kg/conflict_detect.py`（复用 `corroboration_counts` / `_discriminative_conflict` / `_ann_candidates`）。
- **T3** LLM 裁决器 `kg/conflict_review.py`（仿 `concept_merge_review.py`，bilingual prompt，JSON schema）。
- **T4** 写回/应用 `apply_conflict_resolution`（keep/discard/modify，tier base-wins，缓存失效）。
- **T5** 编排 `resolve_notebook_conflicts`（候选→裁决→自动应用/入队）+ 挂 `build_notebook_kg` 末尾（开关控）。
- **T6** 端点：`POST .../kg/conflicts/resolve`（后台线程，仿 `kg/build` `routes.py:631`）、`GET pending`、`POST confirm/reject`。
- **T7** config flags（`core/config.py`）。
- **T8** 测试 + 不变量回归。

依赖序：T1 → T2/T3（并行）→ T4 → T5 → T6；T7 随时；T8 贯穿。

## 9. 回退 / Rollout

- 默认关、opt-in；评审队列可撤销；不动检索打分。
- 回退 = 关开关 + 评审里 reject 已应用项。

## 10. 风险与开放问题

1. **边 evidence 瘠薄**：relation evidence 只有 `{"quote": ...}`、无 element_id 锚（`kg_ingest.py:109`）→ 裁决取完整原文需经**端点对象**的富 evidence 反查；上下文不足时兜底用 `_source_raw_text`（`:1601`）。
2. **temporal 冲突在 EDA/教材语料可能罕见**：mutual 是主力，temporal/granularity 顺带。
3. **modify 改 Claim 文本会 re-embed**：须经 `update_knowledge`，确保向量/文本同步、不破坏引用。
4. **`'conflict'` 对象状态仍 usable**：前端展示支持留作后续（本期后端为主）。
