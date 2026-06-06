# 去过度合并（确定性核心 + LLM 兜底）设计

- 日期：2026-06-06
- 状态：设计已与用户确认，待用户复核 spec
- 范围：`backend/app/services/kg_merge.py`（`cluster_concepts`）+ `backend/app/services/sqlite_repository.py`（`rebuild_unified_kg` 编排）+ 复用 `backend/app/services/concept_merge_review.py`
- 关联：由 `kg-db-compare` 分支的 `docs/kg-denoise-effect-analysis.md` 暴露——去噪成功但 unified KG 仍过度合并。

## 背景与根因（已验证）

去噪重抽后，`rebuild_unified_kg` 的聚类仍把**不同的真概念**错并成大簇：
- `[Channel Length] ⇐ drain, source, gate, bulk, diffusion length, minority carrier concentration…`
- `[voltage-voltage feedback] ⇐ current-voltage / voltage-current / current-current feedback`
- `[double-balanced mixer] ⇐ single-balanced mixer`

根因（见 `kg_merge.py:126-128`）：
1. **单链接传递合并**：`for sa,sb,sim in candidates: if sim>=hi: uf.union(sa,sb)`。任一相邻对 ≥hi 即经 Union-Find 传递并簇 → drain~channel、channel~source、source~gate 各自 ≥0.90 就把 drain/source/gate/channel 全滚进一个簇（即便 drain 与 gate 互不相似）。
2. **hi=0.90 偏松**，对"语义相邻但不同"的 EE 概念误并。
3. **LLM 预审只覆盖 [0.82,0.90) 的 pending，不覆盖 ≥0.90 的自动合并**——错并直接生效、无人复核。

## 目标 / 非目标

**目标**：消除过度合并（链式大簇 + 近孪生误并），同时保留正确合并（缩写↔全称、确切同名）。除精确同名外，所有合并经 LLM 复核。

**非目标**：不改抽取/去噪管线；不做 claim/formula 去重（另一条线）；不引入新聚类库；不改 `derive_unified_graph`/检索。

## 设计：四层

### ① 阈值收紧
`cluster_concepts` 默认 `hi: 0.90 → 0.94`；`lo` 维持 `0.82`（pending 下限）。保留 VCO↔voltage controlled oscillator（别名归一+高相似）这类真合并。

### ② 判别 token 护栏（确定性，免费）
`kg_merge.py` 新增纯函数 `_discriminative_conflict(name_a, name_b) -> bool`：
- 取 `_norm` 后的 token 列表 `ta/tb`；算多重集差 `only_a=ta-tb`、`only_b=tb-ta`。
- 若 `len(only_a)==1 and len(only_b)==1`（两名仅各差一个 token，其余相同），且这两个差异 token 落在**同一对立组** → 返回 True（禁止合并）。
- 对立组 `_CONTRAST_GROUPS`（集合列表，可后续扩充）：
  `{single,double}`、`{low,high}`、`{n,p}`、`{nmos,pmos}`、`{series,shunt}`、`{voltage,current}`、`{positive,negative}`、`{input,output}`、`{forward,reverse}`、`{drain,source,gate,bulk,body}`、`{first,second,third,fourth}`、`{upper,lower}`、`{even,odd}`、`{internal,external}`、`{inverting,noninverting}`。
- 命中即从候选中剔除（既不自动合并也不入 pending；记为 deterministic-reject）。
- 直击 `voltage-voltage⇐current-voltage`（voltage↔current）、`single⇐double balanced`（single↔double）、`drain⇐source`（terminal 组）。

### ③ 反链式：完全链接（替换单链接 Union-Find）
向量候选不再两两 `uf.union`。改为**完全链接凝聚**：一个 seed 只有在 **≥hi 于目标簇内全部成员**（用 rep 向量两两 cosine 校验）时才加入；否则另起簇。
- 实现可用：按 sim 降序处理候选；合并两簇前校验所有跨簇 rep 对 ≥hi，否则不合。
- 效果：`channel length` 簇不会因 drain~source 链而吸入 drain（drain 对 channel length 的 cosine <hi）。

### ④ LLM 兜底（用户选定）
经 ①②③ 后得到的"高置信合并候选"（≥0.94、过护栏、过完全链接）**不再直接生效**：
- `cluster_concepts` 只对**精确同名 + 已决定确认对(decided confirmed)**做 auto-union；其余向量候选（高置信 + [lo,hi) pending）一律作为**候选返回**，不在纯函数内 union。
- `rebuild_unified_kg` 对候选调 `concept_merge_review`（LLM：merge / keep_separate / unsure + 置信度）；LLM 判 merge 且置信≥阈值的 → 写入 `concept_merge_candidates` 为 confirmed → 应用 union（本轮再跑一遍 union 或直接 union 确认对）；keep_separate → rejected；unsure / 低置信 → 留 pending 供人工。
- 结果：除精确同名外，所有合并都经 LLM 复核才生效。

## 数据流（`rebuild_unified_kg`）
1. 载入 concepts + 向量 + `decided_pairs`（confirmed/rejected）。
2. `cluster_concepts`：精确同名 + confirmed auto-union；向量候选过 ②护栏 → ③完全链接 → 产出 `auto_candidates`(≥hi) + `pending`([lo,hi))，**均不直接 union**。
3. LLM 复核 `auto_candidates`(+top pending)：confirmed → 应用 union；rejected/unsure → 落库。
4. 写 `concept_clusters` + 刷新 `concept_merge_candidates` + `unified_kg_state`。

## 测试
- **护栏单测**（`tests/test_kg_merge.py`）：`voltage voltage feedback`✗`current voltage feedback`、`single balanced mixer`✗`double balanced mixer`、`drain`✗`source`、`nmos`✗`pmos` 必拦；`vco`✓`voltage controlled oscillator`、`current mirror`✓`wilson current mirror`（差非对立 token）、`current mirror`✓`cascode current mirror` 不被误拦。
- **反链式单测**：构造 A~B、B~C 均 ≥hi 但 A~C <hi → 断言 A、C 不在同簇。
- **LLM 兜底单测**：fake LLM 对一个 auto_candidate 返回 keep_separate → 断言未合并；返回 merge → 合并。
- **集成/回归**：现有 `test_kg_merge.py` / `test_unified_kg_repository.py` / `test_concept_merge_review.py` 保持绿；在真实 nb-012 概念上跑 rebuild，断言 §分析里的垃圾簇（Channel Length⇐drain…、feedback 四拓扑、single⇐double）消失。

## 风险与权衡
| 项 | 权衡 | 缓解 |
|---|---|---|
| 阈值升高 + 护栏 | 可能漏并真同义（偏保守） | LLM 兜底可救回；pending 人工队列 |
| 完全链接 O(簇²) 校验 | 大簇校验成本 | 候选已 top-k 有界；簇通常小 |
| 对立组列表不全 | 个别近孪生漏拦 | LLM 兜底兜住；组可增量扩充 |
| LLM 兜底成本 | 每次 rebuild 增 LLM 调用 | 仅审有界候选(高置信+top pending)，非全量 |
| `cluster_concepts` 契约变化(不再自动 union 向量对) | 调用方需适配 | 仅 `rebuild_unified_kg` 一处调用 |
