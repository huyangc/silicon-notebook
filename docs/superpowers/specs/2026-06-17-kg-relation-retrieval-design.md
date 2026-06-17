# KG 检索增强:关系向量化 + 双层关键词 + 检索度量

- 日期:2026-06-17
- 状态:设计已确认,待写实现计划
- 关联:`docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md`、`2026-06-16-p4-kg-shrink-design.md`、`2026-06-12-two-tier-roadmap-design.md`;借鉴源 `/Users/hzf/workspace/ref-kg/LightRAG`(双层关键词 + 关系向量索引)。

## 1. 背景与动机

通用问答已改为 chunk-native 主路径,KG 退到「按需严格推理(`graph`/`reasoning`)+ 两层治理」。但 KG 这条线本身的**检索能力偏弱**,根因之一是:

- **关系不可被直接检索**。`knowledge_relations` 只有 `edge_type` + 两端 object + `evidence`,**没有向量、没有可检索摘要**。`ask_graph` 的种子来自 `federated_retrieve`(只对**节点**打分),多跳 BFS 从节点起,**找不准该从哪条边出发**。
- 由此,**两端节点都没被单独检索到的"桥接边"**(A 在第 1 章、C 在第 50 章,问 A↔C 关系)根本进不了种子——这正是 graph 模式在 eval 里逐层输 chunk 的可疑根因之一。

LightRAG 的做法验证了一条互补路线:**关系作为一等公民被向量化**(其 `rel_content = "{keywords}\t{src}\n{tgt}\n{description}"` 进 `relationships_vdb`,`lightrag/operate.py:1952`),query 侧用 LLM 抽**双层关键词**——低层关键词→实体向量库(local)、高层关键词→关系向量库(global)(`lightrag/operate.py:4347-4348`)。

本 spec 把这两个零件**以"纯检索、不碰抽取"的方式**移植进来,并补上我们历史最大短板——**KG 检索度量**(`retrieval_metrics.py` 早有 recall@k/MRR,但 `gold_object_ids` 标注几乎为零,30 题仅 1 题有)。

> 教训:我们曾在「没有标尺」下调 KG(SA 标定踩过 Goodhart——用 gate 迭代 prompt 过拟合)。本 spec 把度量列为**地基**,任何"提升"都必须可证伪。

## 2. 目标与成功判据

**目标:** 让"关系"可被 query 直接检索;query 产出双层关键词;建立 KG 检索 gold 集与指标。本期**唯一消费方 = `graph`/`reasoning` 的种子选择**。

**成功判据(按优先级):**
1. **主**:双轨 gold 集上 **relation recall@k / MRR 显著↑**,尤其"桥接边"子集。
2. **护栏**:**node recall 不下降**(关系检索是增量,不得伤现有节点召回)。
3. **次**(只观察,不作判据,因答案 LLM 采样噪声大):`graph`/`reasoning` 答案 LLM-judge correctness。

## 3. 范围

**In scope:** 关系向量索引(纯检索)、双层关键词(扩 `expand_query`)、graph 种子融合、双轨 KG 检索度量。

**Out of scope(明确不做):**
- chunk×graph 叠加(用户定为 **P2**)。
- 构建侧质量:gleaning / refine / 概念描述融合 / SA-3 重抽——**不碰抽取 prompt**。
- token 预算装配(deferred)、rerank 换模型、RRF 默认开。

## 4. 架构与组件(每个单元单一职责)

| 组件 | 职责 | 落点 | 镜像/参照 |
|---|---|---|---|
| `relation_embeddings` 表 | `(relation_id PK, notebook_id, vector TEXT, created_at)` + `idx_*_nb` | `sqlite_repository.py` DDL(`knowledge_embeddings` ~:369 旁) | 镜像 `knowledge_embeddings` |
| `relation_embed_text()` | 纯函数:关系 → embedding 文本 | `retrieval.py` | 无 |
| `_embed_relations_batch()` | 建图后并发 COMPUTE 关系向量 + 一次写事务持久化 | `sqlite_repository.py` | 镜像 `_embed_objects_batch`:1693 |
| 回填 | 旧库补关系向量(幂等,只补缺失) | `_backfill_relation_embeddings` + `scripts/backfill_relation_embeddings.py` | 镜像 `_backfill_knowledge_embeddings`:1875 |
| `score_relations()` | 关键词+语义融合打分关系,**守 [0,1]/tau** | `retrieval.py`(`score_knowledge`:304 旁) | 镜像 `score_knowledge` |
| `federated_retrieve` 扩展 | 额外返回 `relation_hits`(各带 `notebook_id`/`tier`) | `sqlite_repository.py:4120` | 复用 base∪active |
| 双层关键词 | `ExpandedQuery` 加 `high_level_keywords`/`low_level_keywords`;prompt+schema 同步 | `query_rewrite.py` + `prompts.py:328/331` | 扩**现有一次** LLM 调用,无新增往返 |
| graph 种子融合 | 高层关键词命中的关系 → 两端 object 并入 `use_seeds`(∪ node seeds ∪ 现有 1-hop) | `ask_graph`:4854 | 既有 `use_seeds` 拼接点 |
| 度量 | 双轨 gold 集 + `run_recall` 扩关系 + 报告 | `eval/` | 扩 `retrieval_metrics.py` + `run_all.py:64` |

### 关系 embedding 文本格式

```
relation_embed_text(src_name, edge_type, tgt_name, evidence_quotes)
  = f"{src_name} —{edge_type}→ {tgt_name}. {evidence_concat}"
```
- `evidence_concat` = 关系 evidence 引文拼接,截断到上限(如 ~400 字),保证语义而不爆 token。
- **不新增 LightRAG 那种 LLM 生成的 `keywords` 字段**(那是抽取改动,越界);用 `edge_type` + 两端名 + evidence 已足够语义。

## 5. 数据流

**建图期:** KG build → embed objects(现有)+ **embed relations(新)**;旧 notebook 用回填 CLI 补。

**查询期(graph/reasoning):**
```
question → expand_query  (现有一次调用，多吐 hl/ll keywords)
   ├─ low-level kw  → 节点索引  _retrieve_scored        → node seeds
   └─ high-level kw → 关系索引  score_relations          → relation hits
                                         │
              relation 两端 object ──────┘  ∪ node seeds ∪ 现有 1-hop 邻居
                                         ↓
                       use_seeds → multihop_subgraph → render_subgraph_context → answer
```
**节点检索保留现有 `query`/`sub_queries` 驱动不变**;low-level 关键词作为**额外并集信号**注入(只增不减命中)——护栏「node recall 不下降」由并集语义在构造上保证。本期真正的新能力是 **high-level → 关系索引**。

**chunk 路径完全不变**(关系检索不接 chunk;叠加层留 P2)。双层关键词的**输出**始终产出(chunk 路径忽略多余字段 = 字节等价),**消费关系索引**受开关门控。

**度量期:** gold 集 → 每题跑 node + relation 检索 → recall@k / MRR(分对象/关系)→ 报告。

## 6. 度量设计(双轨 gold 集)

### Track 1 · KG 反向出题(铺量,自动 gold)

- 从真实 notebook KG 采样**对象与关系** → LLM 为每个生成一道自然问题。**gold = 源 `object_id` / `relation_id`(+ 两端 object_id)**。
- **防泄漏**(关键,否则召回虚高、循环偏置):
  - prompt 强制改写、**禁逐字引用 evidence**、实体名尽量抽离或换近义表达;
  - 生成后做**"裸关键词命中率"体检**:若问题与源对象/关系文本的字面重合度过高,判为泄漏并剔除。
- 每题打标签:`node-recall` / `relation-recall(bridge)`、hop 数 → 支持分桶看哪类提升。

### Track 2 · 人工锚点(诚实,小量)

- ~10–20 道**真实措辞**难题,人工标 gold 对象/关系。作为 Track 1 的诚实校准,防自动出题的循环偏置。

### 存储与指标

- 新增 `backend/app/eval/recall_gold.yaml`:沿用 `gold_object_ids` 约定 + **新增 `gold_relation_ids`**;与 `questions.yaml`(答案质量集)**分开**,互不污染。每条带 `track: reverse|anchor`、`bucket: node|bridge`、`hops`。
- 指标:`recall@k`、`MRR`,**分对象索引 / 关系索引各算一份** + 桥接边子集单列。k 默认沿用现值(12)。
- **baseline 对照 treatment**:关系检索 + 双层关键词消费 **关 vs 开** 各跑一遍。
- **升默认闸门**:relation recall 实质↑ **且** node recall 不降。
- **运行**:反向出题需真 LLM(一次性生成),度量本身确定性廉价;真机在 prod 副本 KG 上由用户跑(沿用既有 eval 纪律,不动 prod)。

## 7. 错误处理与回退

- **关系未 embed(旧库)** → 关系检索返空 → 种子自动退回 node-only(= 今日行为);回填 CLI 补齐。
- **`expand_query` 失败/未配置** → 既有 fallback(单子查询、无关键词)→ 关系检索跳过,不报错。
- **特性开关** `RELATION_RETRIEVAL_ENABLED`(默认 **关**,eval 验证后再开)。双层关键词**输出**始终产出且零额外成本;仅**消费关系索引**受此开关门控。

## 8. 测试与不变量(硬约束)

**不变量(防 `0ca8f1a` RRF 把 relevance 打塌的回归重演):**
- 关系 cosine 用原始 `max(0.0, cosine)` ∈ [0,1];tau 校准与节点同尺。
- tier 权重只进 `rank_key`,**不进 `_fuse`**。
- **dual-index best-of 分离**:节点矩阵与关系矩阵各自独立,**不合并**(沿用 `score_knowledge` 的 dense id→cosine 双索引范式)。

**测试:**
- 单元:`relation_embed_text` 纯函数;`score_relations` 的 [0,1]/tau;双层关键词解析 + 回退;种子融合去重。
- 等价:开关**关**时,`graph` 路径与当前**字节一致**;`chunk` 路径任何情况下不变。
- harness 自测:微型合成 KG 上跑 recall,锁住 recall@k/MRR 计算正确。

## 9. 代码落点(blast radius)

`sqlite_repository.py`(表 DDL / `_embed_relations_batch` / 接 `score_relations` / `ask_graph` 种子融合)、`retrieval.py`(`score_relations` + `relation_embed_text`)、`query_rewrite.py` + `prompts.py`(双层关键词 schema/prompt/dataclass)、`config.py`(`RELATION_RETRIEVAL_ENABLED`)、`eval/`(反向出题生成器、`recall_gold.yaml`、`retrieval_metrics.py` 扩关系、`run_all.py` 接线)、`scripts/`(关系向量回填 CLI + gold 生成 CLI)。

## 10. 显式推迟 / 未来

- **chunk×graph 叠加(P2)**:本期把 KG 检索做强后,再评估把有界 KG 关系并入 chunk 答案上下文(availability-gated overlay)。
- 构建侧:gleaning / refine 开+调、merge LLM 融合(借 LightRAG `force_llm_summary≥8 片段`)、描述富化、SA-3 重抽。
- token 预算装配、rerank 换模型。

## 11. 关键决策记录

- **范围**:检索为主 + 必要度量;构建侧与 chunk×graph 叠加均移出本期(用户拍板,叠加=P2)。
- **关系向量化走纯检索路线**:从已有字段合成 embedding,**不动抽取 prompt**(守"不碰构建侧"边界)。
- **双层关键词扩 `expand_query`**:复用现有 REWRITE_LLM 一次调用,不新增往返。
- **关系检索狠度 = 方案 A∪B**:建独立关系索引(捞桥接边)+ 保留现有节点 1-hop(recall 最好)。
- **gold 集 = 双轨**:KG 反向出题铺量 + 人工锚点校准(与两层 roadmap 既定"双轨评测"一致)。
