# Notebook 统一 KG + 可视化 — 设计文档

- 日期：2026-06-02
- 状态：brainstorming 已逐项确认，待写实现计划
- 背景：当前每个 source 各自抽取一个 KG（仅单文档内 Concept 规范化），notebook 级没有统一图谱；且缺少检查工具来整体审视抽取质量。这两件事是后续「KG 检索」的前置：检索质量取决于图谱结构，需先跨文档合并 + 可视化审视。本设计交付两件事——**notebook 级统一 KG（跨文档 Concept 合并）**与**KG 可视化检查视图**——并先搭一个**可切换的 embedding 接口**（二者共享）。

## 0. 决策摘要（已确认）
1. **embedding 接口先行**：抽象 `Embedder` 接口，开发期默认 **local BGE**，跑通后切 **dashscope `text-embedding-v4`**；与 chat（deepseek）分离，配置切换。
2. **合并数据模型**：**非破坏性规范化层**——保留 per-source 节点不动，新增 `concept_clusters` 把同一 Concept 的各 source 节点映射到一个 canonical id。
3. **仅 Concept 聚类**；Claim/Formula/Procedure 不合并，但其边重指向 canonical Concept；`mentions` 取并集。
4. **分层匹配**：归一化名称/别名精匹自动合并；cosine **≥0.90 自动合并**；**[0.82,0.90) 标 `pending` 灰区待审**；<0.82 分开。阈值后续可调。
5. **可视化 = 检查工具**：**概念级概览 + 逐概念下钻**；从工作区 `关系图` 入口升级为**全屏 KG 视图**；灰区合并在此 confirm/reject。

## 1. Embedder 接口（共享前置）
```
Embedder:
  embed_texts(texts: list[str]) -> list[vector]   # 批量，离线建索引用
  embed_query(text: str) -> vector                # 单条，在线查询用
  dim: int
```
- 后端实现：
  - `LocalBGEEmbedder`（开发默认）：`sentence-transformers` 加载 BGE（如 `BAAI/bge-m3` 或 `bge-base-zh`），本地推理，零网络。
  - `DashscopeEmbedder`（生产）：OpenAI 兼容 `/embeddings`，`text-embedding-v4`，用已有 dashscope key。
- 配置（与 chat 分离）：`EMBED_PROVIDER=local|dashscope`、`EMBED_MODEL`、`EMBED_BASE_URL`、`EMBED_API_KEY`、`EMBED_DIM`。
- 接入：替换/包裹现有 `llm_client.embed` 路径，`store_kg` 用 `Embedder.embed_texts` 给节点（至少 Concept 的 name；Claim/Formula 的 name/statement 供后续检索）建向量，落 `knowledge_embeddings`（已有表：object_id, notebook_id, vector）。
- 不变量：`dim` 一致；provider 切换需重建受影响 notebook 的向量（提供 reindex）。

## 2. Notebook 统一 KG（跨文档合并）
### 2.1 数据模型（非破坏性）
```sql
CREATE TABLE concept_clusters (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  canonical_id TEXT NOT NULL,        -- 同一 canonical_id 的成员属同一概念簇
  member_object_id TEXT NOT NULL,    -- 指向 knowledge_objects.id (type=concept)
  canonical_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE concept_merge_candidates (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  canonical_a TEXT NOT NULL, canonical_b TEXT NOT NULL,   -- 两个簇
  score REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',                 -- pending|confirmed|rejected
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
```
- per-source 的 Concept `knowledge_objects` 行**保持不动**；统一 KG 由映射**派生**。
- **统一 KG 派生规则**：
  - canonical Concept：每个 `canonical_id` 一个节点；`name = canonical_name`；`mentions = ∪ 成员 evidence/sources`。
  - Claim/Formula/Procedure：原样保留。
  - 边：把 `knowledge_relations` 每条边的端点若是成员 Concept → 替换为其 `canonical_id`；按 `(canonical_src, canonical_tgt, edge_type)` 去重；evidence 取并集。

### 2.2 聚类算法（批量、按 notebook，幂等）
1. 取该 notebook 全部 Concept 节点 + 其向量（`store_kg` 已离线建好）。
2. Union-Find：
   a. 按归一化名称/别名相等 **seed 簇**（高准，自动）。
   b. 先把既有 `status=confirmed` 的对作为**强制 union** 应用；`status=rejected` 的对在后续步骤**禁止** union。
   c. 跨簇代表向量 cosine **≥0.90** → 合并（跳过 `rejected` 对）。
   d. cosine **∈[0.82,0.90)** 且非 rejected → 写/保留 `concept_merge_candidates(status=pending)`（不合并）。
3. 每簇定 `canonical_id` + `canonical_name`（取度最高/出现最多成员）。
4. 重写 `concept_clusters`（保留既有 confirmed/rejected 决策；pending 候选按当次相似度刷新，已决策的对不回退为 pending）。
- **确定性**：同输入同结果；不依赖随机。

### 2.3 触发
- `rebuild`：每个 source 抽取完成后自动跑（保持统一 KG 当前），且**可手动重跑**；幂等、非破坏（仅重建 cluster 表，源节点不动）。
- 先做**全量重算**（数百~数千概念，向量已预建，开销小）；增量分配（只并新 source）作为后续性能优化。

### 2.4 灰区审核
- `pending` 候选在可视化里高亮 + 列表；用户 **confirm**（合并两簇，成员归一到一个 canonical_id）/ **reject**（保持分开，记住该对不再建议）。决策持久化在 `concept_merge_candidates.status`。

### 2.5 API
- `POST /notebooks/{id}/unified-kg/rebuild` → 重算簇。
- `GET /notebooks/{id}/unified-kg?level=concept` → 概念级图：canonical Concept 节点（`{id, name, mentions_count, sources[], degree, claim_count, formula_count}`）+ 概念间 typed 边。
- `GET /notebooks/{id}/concepts/{canonical_id}/detail` → 下钻：成员节点、挂载的 Claim/Formula/Procedure（经 about/defines）、evidence span、来源。
- `GET /notebooks/{id}/unified-kg/pending-merges` → 待审候选。
- `POST /notebooks/{id}/unified-kg/merges/{candidate_id}/confirm|reject`。

## 3. KG 可视化（检查视图）
### 3.1 入口与形态
- 复用工作区 `关系图` 按钮，改名 **「知识图谱」**；点开**全屏 KG 视图**（沿用 app 现有 overlay/状态 `kgViewOpen`，×/Esc 返回；单页、无路由）。替换原来薄的 `/graph` modal。
### 3.2 三区布局
- **左栏 — 过滤与搜索**：来源开关、节点类型开关、章节过滤、名称搜索、**「待确认合并」**列表。
- **中区 — 图画布**：`react-force-graph-2d`（canvas，可承载数千节点）；概念级，节点按 mentions/degree 调大小；概念间 typed 边；pending 合并高亮。
- **右栏 — 详情/下钻**：点概念 → 成员、挂载 Claim/Formula/Procedure、**evidence span + 来源链接**（复用知识浏览器的证据卡样式）、灰区合并 confirm/reject。
### 3.3 与现有区域的关系
- 替换原 `/graph` modal，读新的 `/unified-kg?level=concept`。
- **知识库浏览器**（chat-panel）保留为列表视图；KG 视图是同一知识的图视图，二者互链（概念证据面板与浏览器证据卡一致；可互相 deep-link）。
- 合并审核就在 KG 视图内（合并本质是图的视觉操作）。

## 4. 测试
- **Embedder**：接口契约测试（fake/local 后端）；维度一致；real-backend smoke 在主会话跑。
- **合并**：单测——精匹自动合并、mock 向量阈值合并、灰区 pending、rejected 对不再建议、边重指向 + 去重、canonical 派生、rebuild 幂等。
- **可视化后端**：端点形状测试（concept 级图、concept detail、pending-merges、confirm/reject 改状态并影响下次 rebuild）。
- **前端**：`tsc --noEmit` 通过；检查工具以人工 eyeball 为主。
- **纪律**：合并质量用 14 章 gold KG substrate 抽查（跨章同概念是否正确合并/误合并），阈值在 validation 上调、held-out 上看。

## 5. 排期/分阶段（各自可独立交付）
1. **Embedder 接口**（local BGE + dashscope，配置切换）+ `store_kg` 建节点向量。
2. **统一 KG 合并**（concept_clusters + 聚类 + rebuild 触发 + pending/confirm/reject + `/unified-kg` 端点）。
3. **可视化视图**（后端 concept 级/detail 端点 + 前端全屏三区视图 + 合并审核 UI）。

## 6. 非目标（YAGNI）
- 增量合并（先全量重算）。
- Concept 之外类型的跨文档去重。
- 合并的复杂 ML（先名称+向量阈值+人工灰区）。
- KG 检索（下一个 spec，见 §7）。

## 7. 下一步：KG 检索（已讨论、暂缓，记录以免丢失）
统一 KG + 可视化跑通后做检索，方案要点（详见对话）：
- **入口召回**：混合 = SQLite FTS5/BM25（词法）+ 向量 cosine（稠密，复用 Embedder）→ **RRF 融合**；离线建索引，在线查询快。
- **图遍历**（三场景）：问答=沿 about/supports/defines 扩 1–2 跳；概念探索=typed 1 跳邻域；推导/前置=沿 derived_from/depends_on/prerequisite_of 有序路径走（深度上限+环检测）。内存邻接 BFS，带 per-node fan-out cap + context budget，遍历近乎零开销，延迟由 LLM/embedding 网络调用主导。
- **评测**：三层——入口召回 IR（Recall@K/MRR/nDCG，hybrid vs 单路 ablation）、子图/路径质量、端到端答案（引用 faithfulness/正确率/幻觉率，LLM-judge + 人校）；validation 调参、held-out 评测、私有集防作弊；回归 harness。
