# 跨文档社区层设计（GraphRAG 式，通用长期资产）

- 日期：2026-07-07
- 状态：方向经 PoC 验证，待写实现计划
- 分支：`claude/cool-liskov-f77904`
- 取代：本文件取代同日的 `comparative-sibling-fanout-design.md`（find_siblings/词典方案，经 PoC 否决）

## 1. 问题与方向演变

**症状**：在 DeepSeek-V4 notebook 问「分析 deepseekv4 相比其他 llm 的优势，还有什么提升空间」，ask 与深度报告都带不出基准库（`nb-b37185f4ae`「LLM Structure & Infra」，tier=base，4 万+ 对象、84 篇论文、满库其他 LLM）里的「其他 llm」。

**根因**：管线里没有任何一步把「其他 llm」落地成语料库里真实存在的兄弟实体。检索/规划全程锚在焦点（DeepSeek）上（子查询条条写 DeepSeek）；而底层缺一张能"从一个实体走到它同类"的持久结构。

**方向演变（三条路，PoC 逐一验证）**：
- **embedding-kNN 建边** ❌：向量最近邻是"同型号"（DeepSeek-V4↔V3=0.95），兄弟模型远（↔Qwen=0.62、↔Mixtral=0.48）且与技术词纠缠；无阈值能既连兄弟又不糊成蜘蛛网（sim≥0.6 随机度均值 31.6）。
- **名称频次 + 模型词典** ❌：能出兄弟清单，但**领域专用**（手写模型正则），用户否决——notebook 不只锚定"模型"这一类实体。
- **GraphRAG 式社区** ✅：Louvain 在 canonical 实体图上**零词典**自动聚出主题社区（MoE 族 / SSM 族 / 前沿榜 / agentic 编码 / 多模态），完全领域无关。

**决定性 PoC**：在 canonical 图（3.6万节点/4.9万边）上跑 Louvain，DeepSeek-V4 落进社区#7（707 节点），其**跨家族兄弟 34 类、21 类真跨文档**——GPT-5.2、Gemini 3.1 Pro、Claude Sonnet 4.5、GLM-5、Kimi K2.6、Nemotron、Step-3.5，正是它对标的前沿模型。**社区直接给对了相关兄弟集。**

**更正**：早前"4 篇孤岛、DeepSeek-V4 未聚类、需先修融合"的判断在当前库**不成立**（实测 41,713 对象 100% 聚类，V4 论文 1,292 对象全聚类）。**无孤岛前置**。

## 2. 目标 / 非目标

**目标**
- 建一个**持久、通用、领域无关**的跨文档社区层：把语料里相关实体聚成社区，作为长期资产（可浏览、可复用、可增量）。
- 让"对比 / 广度"类问题能从焦点实体走到其**社区兄弟**（跨文档），在 ask 与深度报告两路生效。
- 兑现原始诉求："让检索/PPR 直接把相关概念带出来，而不是写补丁。"

**非目标**
- 不做 embedding-kNN 边、不做模型词典（PoC 否决）。
- 不引入新依赖（用现有 networkx Louvain，不引 leidenalg/igraph；Leiden 留后续）。
- 不默认对全部社区跑 LLM 报告（成本闸；按需生成）。
- 不改 base 库的抽取/标记流程；不需要"先修孤岛"。

## 3. 设计总览（三层）

```
┌ Layer 1（资产·纯图算法·无 LLM）───────────────────────────┐
│ 社区检测：canonical 实体图 → Louvain → communities 表       │
│ 复活现有 rebuild_communities，改喂 canonical 图，接入 KG 重建 │
└──────────────────────────────────────────────────────────┘
        │ 焦点实体 → 所在社区 → 成员（跨文档相关实体）
┌ Layer 2（检索集成·让对比题生效）──────────────────────────┐
│ 社区感知检索：焦点社区成员按 query 相关度排序 = 兄弟集       │
│ · reasoning：新增 reflect 动作 expand_community（模型驱动）  │
│ · 报告：STORM 规划"横向对比"节，深挖时触发                   │
│ · 跨层：焦点在 base 库社区里查（base=共享语料所在）           │
└──────────────────────────────────────────────────────────┘
┌ Layer 3（可选·增强）─────────────────────────────────────┐
│ · 按需 community report（仅焦点社区，LLM，缓存到行）         │
│ · 社区 hub 边入 PPR 图（transit-only），PPR 天然扩散到兄弟   │
└──────────────────────────────────────────────────────────┘
```

本 spec 聚焦 **Layer 1 + Layer 2**（解决问题的最小闭环）；Layer 3 列为后续。

## 4. 详细设计

### 4.1 Layer 1 — 社区检测（复活 + 修正）

现状：`communities` 表、`rebuild_communities`（`sqlite_repository.py:6629`，networkx Louvain）、`summarize_communities`、`list_communities` 都在，但**休眠**（表 0 行），且 `rebuild_communities` **只喂裸 `knowledge_relations`** → 逐篇封闭（PoC 证明喂 canonical 图才跨文档）。

**改造 `rebuild_communities(notebook_id, level=0)`**：
1. 取 `cluster_map = self.cluster_map(notebook_id)`（member_object_id → canonical_id，已有，`:4958`）。
2. 读 `knowledge_relations`，**两端经 cluster_map 映射到 canonical_id**（未映射对象用自身 id 作单例 canonical），构无向带权图（weight=关系出现次数，跳自环）。
3. `louvain_communities(G, weight='weight', seed=42)`（确定性）。
4. `member_ids` 存 **canonical_id 列表**（非原 object_id）；持久化到 `communities`（delete+reinsert，现有逻辑）。
5. 小社区过滤：`size < COMMUNITY_MIN_SIZE`（默认 3）不入库（噪声/单例）。

**接入 KG 重建**：`rebuild_unified_kg`（`:6301`）末尾、聚类完成后调 `rebuild_communities`（社区依赖 canonical 稳定）。纯图算法、无 LLM、秒级；随 `kg_mutation_seq` 版本闸跳过未变输入（复用现有缓存闸）。

**成本**：Louvain O(边)，base 库 4.9 万边实测秒级；无模型调用。

### 4.2 Layer 2 — 社区感知检索

#### 4.2.1 焦点 → 社区 → 兄弟集（核心原语，新 `comparative.py` 或 `retrieval.py`）

```python
def community_peers(repo, base_nb, focal_name, query, *,
                    top_k, rerank=True) -> list[RetrievedKnowledge]:
    """焦点实体在 base 库社区里的同社区成员，按 query 相关度排序，取 top_k 作兄弟集。
    焦点按归一化名解析到 base 库 canonical（跨层：焦点对象来自 active 库，只能按名匹配，
    不能用 active 的 object_id 去查 base 的社区）。"""
```
1. **焦点解析（按名跨层）**：`focal_name` 归一化后，在 `base_nb` 的 `concept_clusters.canonical_name` 里匹配 `canonical_id`（同名取成员最多者）；匹配不到 → 返回 `[]`（fail-open，退化为现状）。
2. 查 `communities`（notebook_id=base_nb）找含该 canonical 的社区行 → `member_ids`（canonical 列表）。多层时取最细层（level 最大）含它的社区。
3. 成员**按 query 相关度重排**（复用 `_retrieve_scored` / rerank 的打分），过滤明显非实体（payload.name 过长的句子碎片降权），排除焦点同 canonical，取 `top_k`（默认 `COMMUNITY_PEERS_TOPK`）。
4. 返回 `RetrievedKnowledge`（tier=base），供上层做子查询 fan-out 或直接进上下文。

**为何要重排**：社区成员几百个且混杂（技术词、基准、句子碎片），社区只给**候选池**，query 相关度**选出真正相关的兄弟**（效率题→efficiency 相关模型靠前）。

#### 4.2.2 跨层：焦点在 base 库社区里查

对比/广度题的兄弟住在 **base 库**。集成点在 `federated_retrieve` 语境：active notebook 提问时，`community_peers` 对 **base notebook** 查（focal 同 canonical 名在 base 库社区里定位）。若 active 自身也有社区，一并考虑，base 优先（共享语料）。复用 `federated_retrieve`（`:10434`）已有的"遍历 base notebooks"骨架取 base_nb。

#### 4.2.3 reasoning：reflect 动作 `expand_community`（模型驱动，通用）

镜像现有 `ppr_retrieve` 动作（`reasoning_retrieval.py:435`）：
- **prompts.py `reflect_prompt` + `REFLECT_SCHEMA_HINT`**：动作集加 `expand_community`——"当问题要把某实体与其同类/相关实体横向比较、而候选里缺这些同类时，用它拉出该实体所在**语义社区**的相关成员"；schema 加 `"community_focal":""`（**焦点实体名**，缺省用当前 top 候选的名）。
- **`reflect()`**：解析动作 + `community_focal` 写入 `ReflectDecision`（新增字段）。
- **`run()`**：新增分支——`focal_name = decision.community_focal or 当前最高分候选.payload["name"]`；`base_nb = federated_retrieve 同款查得的 base notebook id`；`peers = community_peers(repo, base_nb, focal_name, question, top_k=settings.community_peers_topk)`；对每个 peer 起 `"{peer.name} {angle}"` 子查询并发检索、折进 `collected`、记 `attempted`（防重）+ 加入 `used_queries`（进配额）；`record(TraceStep(step_type="expand_community", ...))`；同一 run 内同一 focal_name 只触发一次。
- **一处覆盖两路**：ask-reasoning 直接得到；报告逐节 `_deep_dive`→`ReasoningRetriever.run` 白拿。

#### 4.2.4 chunk 模式

chunk 无 reflect 循环。`expand_query` schema 加可选 `comparison:{focal}`（模型判定对比时填）；`ask_chunk`（`:10988` expand 调用点）拿到后调 `community_peers(base_nb, focal, query)`，把兄弟子查询**追加**进该路检索（独立预算）。无新增 LLM 调用（搭 expand 车）。

#### 4.2.5 报告 STORM 规划

`report_storm_outline_prompt` 加一句："对比题规划一节横向对比，其 sub_queries 面向焦点的同类实体。" 规划器产出「横向对比」节，兄弟在该节深挖时由 `expand_community` 动作落地（无规划期枚举）。

### 4.3 Layer 3 — 可选增强（后续，不在本 PR）

- **按需 community report**：命中的焦点社区调 `summarize_communities` 生成 title/summary/findings（LLM，`kg_community_summary_enabled` gate），缓存到 `communities` 行；报告合成时把该 report 作背景。仅焦点社区，非全量。
- **社区 hub 入 PPR**：`_scale_combined_graph` 加"成员↔社区 hub"transit-only 边（镜像现有 cluster hub），PPR 从焦点天然扩散到社区兄弟——兑现"PPR 直接带出相关概念"。因社区大（数百成员），hub 需限流/rerank，故先做 4.2 的显式检索、hub 作后续。

## 5. 效率预算（硬约束）

| 步骤 | 成本 | 说明 |
|---|---|---|
| 社区检测 | 纯图 O(边)，秒级，**0 LLM** | 接 KG 重建，版本闸跳过未变输入 |
| 焦点→社区查表 | 1 次索引查询 | `communities` 加 `(notebook_id, level)` 已有索引 |
| 兄弟重排 | 复用现有打分/rerank | 无新增模型调用类型 |
| expand_community fan-out | K 次检索 pass | K≤`COMMUNITY_PEERS_TOPK`(默认 8)，模型触发才发 |
| community report | LLM，**仅按需、仅焦点社区** | gate + 缓存；非全 2034 个 |

**门控**：`COMMUNITY_LAYER_ENABLED` 默认开（仅 Layer 1 建表）；`expand_community` 由模型触发→非对比题零 fan-out。社区检测是一次性/增量资产，不进每问热路径。

## 6. 边界与诚实的 caveat

- **社区跟关系图走**：给的是"上下文相关的兄弟"（前沿模型对前沿模型），非穷尽类型表。老架构模型（Llama/Mixtral/Jamba）在相邻架构社区——对"vs 前沿"这类问题反而合适；要更宽用**分层社区**粗粒度层（本 PR 先做单层 level=0，分层留后续）。
- **成员池大且有噪声**（数百，含技术词/基准/抽取出的句子碎片）→ 必须靠 query 相关度重排 + 实体过滤选兄弟。
- **Louvain 非确定性风险**：固定 `seed=42`；社区 id 每次重建可能变→下游只认 `member_ids` 内容、不认社区 id。
- **焦点识别**：优先用初检索 top KG 候选作 focal（query 驱动，不解析问题串）；模型也可显式给 `community_focal`。
- **跨层一致**：兄弟主要在 base 库社区；active 库自身社区可选叠加。
- Leiden 比 Louvain 略优但需新依赖，本 PR 用 Louvain。

## 6.5 规模化（10^6–10^7）与兜底策略

base 库是唯一会长到百万~千万的库（个人库小；部署库现已 ~87万 knowledge、奔 10^6）。§4 的 networkx Louvain 是 base 库当前 4 万节点的 PoC 形态（秒级），到 10^6–10^7 必须改造，且所有失败路径**可观测、fail-open、绝不在大库上暴力**（沿用 [[deploy-reasoning-freeze-diagnosis]] / [[scale-retrieval-review-state]] 的硬纪律）。

### 6.5.1 规模化改造（硬约束）

| # | 问题 | 10^6–10^7 后果 | 修法 |
|---|---|---|---|
| 1 | networkx 建图 + Louvain | 纯 Python 图，10^7 边**几十 GB 内存 + 分钟~小时** | 复用**已持久化 scale CSR 图**（`graph.npz`/`node_ids.npy`，scale_ppr 已建）+ **编译版 Leiden/Louvain（igraph/leidenalg）**；算法近线性 O(m)，坍的是 networkx 实现非算法 |
| 2 | 焦点→社区（扫 JSON member_ids） | O(总成员)，每查询扫全表 | 新增反向索引表 `community_members(canonical_id PK, community_id, notebook_id)` 建索引，O(1) 定位 |
| 3 | 巨型社区（10^5–10^6 成员） | 查询时全量 rerank 太慢 | 成员**按 intra-community 中心度预排**存好 → 查询先 O(1) 取 top-`COMMUNITY_RERANK_CANDIDATES`（默认 200）再按 query rerank，成本 O(N) 非 O(C) |
| 4 | 全量重建成本 | 即便 igraph 也分钟级 | **离线/后台**（同 scale 索引重建路径，不进查询热路径）+ `kg_mutation_seq` 版本闸 + 周期重建 |
| 5 | `cluster_map` 全量入内存 | 10^7 条 dict = GB 级 | 重建时流式/分批，或复用 scale 索引 `node_ids` 映射，不整表 load |
| 6 | 焦点按名解析 | 10^7 canonical 里匹配名 | 走已有 `kg_objects_fts` / 归一名索引，1 次索引查询 |

**架构原则**：社区层长在 base 库既有离线 scale 基建上（复用持久化 CSR + 编译版社区算法 + 反向索引 + 中心度预排）；**查询时只做 O(1) 焦点定位 + 有界成员 rerank**。

**依赖取舍**：小 notebook 走 networkx（无依赖）；scale-tier 走 **igraph/leidenalg on CSR**（新依赖，scale 必需），按 `_scale_index_eligible` 分派。

### 6.5.2 兜底：大库 / 未建索引 / 未建社区

三种缺失态，**全部 fail-open 到"不做兄弟扩展"（= 现状）+ 发可观测事件，绝不静默零召回、绝不暴力、绝不 crash/hang**：

1. **社区未建/过期（表空或 stale）**：`community_peers` 查不到 → 返回 `[]` → `expand_community` no-op；emit `community_unavailable{reason:not_built}`。前端可提示"社区未建，点刷新图谱"。
2. **大库 + 无 CSR 索引**（社区无法建）：**build 侧硬守卫**——scale-tier notebook 无持久化 CSR 时，`rebuild_communities` **拒绝 networkx 路径**（10^7 networkx 必 OOM/挂），emit `community_build_refused{reason:no_scale_index}`，要求先建 scale 索引（或把建索引接进同一后台流程）。**绝不在大库上尝试暴力建图**（镜像 `_retrieve_scored` 大库拒暴力）。小库无 CSR → networkx 兜底可行。
3. **delta 新内容（最终一致）**：上次重建后新增实体不在任何社区 → 该焦点 `community_peers=[]` → 无兄弟，直到下次重建；取向同 [[scale-retrieval-review-state]]「查询恒定成本·最终一致」，**不为新实体触发全量重建**。

**统一原则**：社区层缺失 = 优雅退回现状 + 事件可观测；查询侧永不因社区层报错或变慢。

### 6.5.3 对本 spec 其它节的影响

- **§7 P1 范围**扩：含 scale-tier 的 **CSR + igraph 路径**、`community_members` 反向索引表（+ 迁移/`_add_column_if_missing` 同款建表）、中心度预排、以及 6.5.2 的三个守卫/事件；小库 networkx 路径与 scale 路径按 `_scale_index_eligible` 分派。
- **§8 涉及文件**增：`community_members` 表 + 建表迁移；`rebuild_communities` 加大库守卫 + CSR/igraph 分支 + 三个 `event_log.emit`；`requirements` 视取舍加 `igraph`/`leidenalg`。
- **§9 配置**增：`COMMUNITY_RERANK_CANDIDATES`（默认 200，中心度预筛再 rerank 的上限）。
- **测试**增：大库无 CSR → 拒 networkx 且发事件（不 OOM）；社区表空 → `community_peers=[]` fail-open；反向索引 O(1) 定位正确；delta 新实体无社区返回空。

## 7. 分阶段（每阶段可独立测试/交付）

- **P1｜社区资产**：`rebuild_communities` 改喂 canonical 图 + 接入 `rebuild_unified_kg` + 小社区过滤 + 前端可选"社区浏览"。交付：`communities` 表被正确填充、可查。
- **P2｜社区感知检索**：`community_peers` 原语 + `expand_community` reflect 动作 + chunk `comparison` 字段 + STORM 规划提示 + 来源分布徽章。交付：对比题带出 base 库兄弟并引用。
- **P3（后续）**：按需 community report + PPR 社区 hub + 分层社区 + 前端社区图。

本 spec 覆盖 **P1 + P2**。

## 8. 涉及文件

- 改：`backend/app/services/sqlite_repository.py`（`rebuild_communities` 喂 canonical 图 + 小社区过滤；`rebuild_unified_kg` 末尾接入；`community_peers` 原语或置于 retrieval；`ask_chunk` 消费 `comparison`）。
- 改：`backend/app/services/reasoning_retrieval.py`（`reflect()` 解析、`ReflectDecision` 加字段、`run()` 加 `expand_community` 分支）。
- 改：`backend/app/services/prompts.py`（reflect 动作 + `REFLECT_SCHEMA_HINT`；expand `comparison` 字段 + `EXPAND_SCHEMA_HINT`；STORM 节提示）。
- 改：`backend/app/services/query_rewrite.py`（`ExpandedQuery.comparison` + 解析）。
- 改：`backend/app/core/config.py`（新 flag，`validation_alias`）。
- 可能新增：`backend/app/services/communities.py`（`community_peers` + 焦点解析，若不放 retrieval；单一职责、可测；不堆 God 对象）。
- 测试：`backend/tests/test_communities.py`。
- 前端：报告/ask 的来源分布徽章（读现有 `tier`）；P1 可选社区浏览。
- 文档：README / README_zh 若涉及新 env 或"刷新图谱"语义变化则补。

## 9. 配置项（pydantic `validation_alias`）

| flag | 默认 | 作用 |
|---|---|---|
| `COMMUNITY_LAYER_ENABLED` | `true` | 建/用社区层总开关 |
| `COMMUNITY_MIN_SIZE` | `3` | 入库最小社区规模 |
| `COMMUNITY_PEERS_TOPK` | `8` | expand_community/comparison 取兄弟数 |
| `COMMUNITY_PEERS_RERANK` | `true` | 兄弟按 query 相关度重排 |
| `KG_COMMUNITY_SUMMARY_ENABLED` | `false` | 按需 community report（Layer 3，现有 flag） |

## 10. 测试策略（TDD）

- **rebuild_communities（canonical 图）**：给定 active/base 关系 + cluster_map 的 fixture，断言不同文档的共 canonical 实体进同一社区（跨文档）；裸关系版会分裂而 canonical 版不会（对照）；小社区被 `COMMUNITY_MIN_SIZE` 过滤；`member_ids` 是 canonical。
- **community_peers**：焦点→社区→成员按 query 重排、排除焦点同 canonical、top_k 截断；焦点无社区/无 base → 返回 `[]` fail-open；噪声碎片降权。
- **reflect() 解析**：`expand_community` + `community_focal` 正确解析；缺字段回退 `answer`。
- **run() expand_community 分支**：stub `community_peers`，断言按兄弟发子查询、折进 collected、记 TraceStep、同一 focal 二次触发跳过、加入 used_queries。
- **expand_query comparison 字段**：含/不含/坏 JSON 三态。
- **集成**：active(焦点)+base(多模型+已建社区) fixture，reasoning 触发 `expand_community`，断言检索出 `tier=base` 的跨家族兄弟；非对比题不触发。

## 11. 已确认决策

- 方向：**跨文档社区层**（GraphRAG 式），取代 find_siblings/词典。
- 算法：networkx **Louvain**（现有，无新依赖），`seed=42` 确定性，单层 level=0。
- 图：**canonical 实体图**（关系两端经 cluster_map 映射）。
- 检测：**模型驱动**（reflect `expand_community` / expand `comparison`），非旁路词法/词典。
- 兄弟集：焦点**所在社区成员**按 query 相关度重排取 top-K；池来自 **base 库**社区。
- 无孤岛前置（更正早前误判）；LLM 报告仅 Layer 3 按需。
- 范围：P1（社区资产）+ P2（社区感知检索），ask + 报告一起。

## 12. 后续（本 PR 外）

- 分层社区（粗粒度层给"全类别"兄弟）。
- 社区 hub 入 PPR 图（transit-only），PPR 原生扩散。
- 按需 community report 融入报告合成。
- 评估 Leiden（需新依赖）替换 Louvain 的社区质量增益。
