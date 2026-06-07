# 去过度合并（确定性核心 + LLM 兜底，全程 sub-quadratic）设计

- 日期：2026-06-06
- 状态：设计已与用户确认（含复杂度修订），待用户复核 spec
- 范围：`backend/app/services/kg_merge.py`（`cluster_concepts`）+ `backend/app/services/sqlite_repository.py`（`rebuild_unified_kg` 编排）+ 复用 `concept_merge_review.py`；新增依赖 `hnswlib`
- 关联：由 `docs/kg-denoise-effect-analysis.md` 暴露——去噪成功但 unified KG 仍过度合并。

## 背景与根因（已验证）

去噪后 `rebuild_unified_kg` 仍把不同真概念错并成大簇：`[Channel Length] ⇐ drain, source, gate, bulk, diffusion length…`、`[voltage-voltage feedback] ⇐ current-voltage…`、`[double-balanced mixer] ⇐ single-balanced…`。根因（`kg_merge.py:126-128`）：
1. **单链接传递合并**：`if sim>=hi: uf.union(a,b)`，任一相邻对 ≥hi 即经 Union-Find 链并 → drain~channel~source~gate 滚成一簇。
2. **hi=0.90 偏松**。
3. **LLM 预审只覆盖 [0.82,0.90) pending，不覆盖 ≥0.90 自动合并**。

## 复杂度约束（硬要求）

N = 去重概念名数（nb-012 ≈ 6–8k，可能增长到数万），d = 1024。**整条聚类管线不得含 O(N²) 算法**。现状有两处 O(N²) 必须消除：
- 候选生成 `M @ M.T` = **O(N²·d)**（全相似度矩阵）+ 每行 argpartition O(N²)。
- （原 spec 提的完全链接 = O(N²)~O(N³)，本版已弃用）。

## 目标 / 非目标

**目标**：消除过度合并（链式大簇 + 近孪生误并），保留正确合并；除精确同名外所有合并经 LLM 复核；**候选生成 O(N log N)、聚类 O(N·k)，全程无 O(N²)**。

**非目标**：不改抽取/去噪；不做 claim/formula 去重；不动 `derive_unified_graph`/检索。（注：`hnswlib` 是 ANN 索引库，非聚类库；聚类算法仍自研。）

## 设计：四层（②③ 为复杂度核心）

### ① 阈值收紧
`hi: 0.90 → 0.94`；`lo: 0.82`（pending 下限）。保留 VCO↔voltage controlled oscillator 这类真合并。

### ② 判别 token 护栏（确定性，O(N·k)）
`kg_merge.py` 新增纯函数 `_discriminative_conflict(name_a, name_b) -> bool`：
- `_norm` 后取 token 列表；多重集差 `only_a/only_b`；若各差恰一个 token 且这对差异 token 落在**同一对立组** → True（禁并）。
- `_CONTRAST_GROUPS`（可增量扩充）：`{single,double}`、`{low,high}`、`{n,p}`、`{nmos,pmos}`、`{series,shunt}`、`{voltage,current}`、`{positive,negative}`、`{input,output}`、`{forward,reverse}`、`{drain,source,gate,bulk,body}`、`{first,second,third,fourth}`、`{upper,lower}`、`{even,odd}`、`{internal,external}`、`{inverting,noninverting}`。
- 命中即从候选剔除。直击 `voltage-voltage⇐current-voltage`、`single⇐double`、`drain⇐source`。

### ③ 候选生成(ANN) + 贪心星型聚类（替换矩阵乘 + 单链接/完全链接）

**候选生成 — hnswlib top-k，O(N log N)：**
- 每个 seed 取成员向量均值作 rep；归一化后建 hnswlib 索引（`space='ip'` = 余弦；`M=16, ef_construction=200`，单线程确定性）。
- 每个 rep 查 top-(k+1)（去自身）→ 候选对 `(a,b,sim)`，保留 sim≥lo。**无 N×N 矩阵**。
- 无向量的 seed 不入 ANN，仅参与精确同名聚类。

**贪心星型聚类 — O(N·k)，杀链式大簇：**
- 候选过 ②护栏后，取 sim≥hi 的边构邻接。
- 按 seed 质量（成员数）降序处理：未分配的 seed 成**锚点**，认领其 ≥hi 邻居中**尚未分配**的 seed 作星型成员。
- **只允许"锚点—直接邻居"，锚点之间不再链** → drain 要进 channel-length 簇必须 ≥hi 于锚点 channel-length 本身（而非经 source 传递）→ 不进。✓
- 复杂度：排序 O(N log N) + 认领 O(N·k)，无平方项。
- 精确同名 + decided-confirmed 仍先 force-union（与原一致）。

### ④ LLM 兜底
③ 产出的星型"高置信合并提案"（锚点↔成员，≥hi、过护栏）**不直接生效**：`rebuild_unified_kg` 调 `concept_merge_review`（LLM merge/keep_separate/置信度），仅 LLM 确认才 union；[lo,hi) pending 同样 LLM 预审 + 人工队列。除精确同名外，所有合并经 LLM 复核。

## 复杂度小结
| 步骤 | 复杂度 |
|---|---|
| rep 构建 | O(N·d) |
| 候选生成 (hnswlib) | O(N log N) |
| 判别护栏 | O(N·k·|token|) |
| 星型聚类 | O(N log N + N·k) |
| LLM 复核 | O(有界候选数)，非全量 |

全程**无 O(N²)**。

## 数据流（`rebuild_unified_kg`）
载入 concepts+向量+decided_pairs → `cluster_concepts`（精确名/confirmed force-union；ANN 候选 → ②护栏 → ③星型 → `auto_candidates`(≥hi)+`pending`([lo,hi))，均不直接 union）→ LLM 复核 `auto_candidates`(+top pending) → 应用 confirmed union → 写 `concept_clusters`/刷新候选/`unified_kg_state`。

## 测试
- **护栏单测**：`voltage voltage`✗`current voltage`、`single`✗`double`、`drain`✗`source`、`nmos`✗`pmos` 必拦；`vco`✓`voltage controlled oscillator`、`current mirror`✓`wilson current mirror`/`cascode current mirror`（差非对立 token）不误拦。
- **星型反链式单测**：构造 A~B、B~C 均 ≥hi 但 A~C <hi → 断言 A、C 不同簇（喂合成候选，不经 ANN，确定可控）。
- **ANN 召回测试**：对一组已知向量，hnswlib top-k 召回率 ≥0.95 vs 暴力（小规模 N）。
- **LLM 兜底单测**：fake LLM keep_separate → 未合并；merge → 合并。
- **复杂度回归**：构造 N=5000 合成 seeds 计时，断言耗时随 N 近线性（对比 N=1000/2000/4000，无平方增长趋势）。
- **集成**：现有 `test_kg_merge.py`/`test_unified_kg_repository.py`/`test_concept_merge_review.py` 绿；真实 nb-012 跑 rebuild，§分析里的垃圾簇消失。

## 风险与权衡
| 项 | 权衡 | 缓解 |
|---|---|---|
| 新依赖 hnswlib | 多一个 C++ 轮子依赖 | 常见、纯 wheel、加进 requirements |
| ANN 近似 | top-k 可能漏个别邻居 | 高召回参数(ef 足够)；漏的本就低相似，影响极小 |
| 星型 vs 完全链接 | 星型较宽松(只认锚点) | 配 ②护栏 + ④LLM 兜底，足够；且杜绝链 |
| 阈值升高 | 可能漏并真同义 | LLM 兜底救回；pending 人工 |
| 对立组不全 | 个别近孪生漏拦 | LLM 兜底；组可扩充 |
| `cluster_concepts` 契约变化 | 仅 `rebuild_unified_kg` 一处调用，可控 | — |
