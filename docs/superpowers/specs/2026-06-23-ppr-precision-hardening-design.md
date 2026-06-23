# PPR 精度硬化(graph 模式)设计

**日期:** 2026-06-23
**状态:** 设计已与用户对齐,待写实施计划
**前置:** P1(HippoRAG 式 PPR 跨文档检索,PR #63 已合入 master)

## 目标

提升 graph 模式 PPR 检索的**精度/质量**,使其足够好到可以考虑默认开启。两个组件:种子侧 **specificity 权重**(抑制大众概念霸权)和 **LLM fact-rerank**(recognition memory,过滤无关种子)。

## 范围与隔离不变量(硬约束)

**只动 graph 模式的 PPR 路径(`_ppr_retrieve`)。** 经代码核对,`_ppr_retrieve` 全仓库唯一调用点是 `ask_graph`(在 `graph_ppr_enabled` 开关内)。

- **通用问答(`ask_chunk`)零改动、零回归** —— 不碰。
- **reasoning 模式零改动** —— 它走 `ReasoningRetriever`,不调 PPR。
- **绝不修改共享方法** `federated_retrieve` / `_retrieve_scored` / `_retrieve_chunks`(被 reasoning 和 graph-BFS 共用)。fact-rerank 必须做成 **PPR 路径内的后置过滤**,specificity 只动 `_ppr_retrieve` 的 reset 数学。
- 把跨文档能力推广到 chunk 模式是**另一个独立项目**(本设计不含)。

## 组件 A:Specificity 权重(种子侧)

HippoRAG 同款做法(`weighted_fact_score /= len(ent_node_to_chunk_ids[phrase_key])`,HippoRAG.py:1463-1464):一个实体出现在越多 chunk 里,越「大众」,作为 PPR 种子的权重应越低,避免 `Transformer`(19 篇)/`KV cache`(31 篇)这类通用概念灌满概率。

**改动点:** `_ppr_retrieve` 的 KG 种子循环:
```
# 现状: reset[idx] += relevance
# 改为: reset[idx] += relevance / max(1, len(ent_chunk_map[object_id]))
```
`ent_chunk_map` 已由 P1 的 `_ent_chunk_map` 提供(只读)。chunk 种子侧不变。

**开关:** `ppr_specificity_enabled`,**默认 True**(PPR 本身已 opt-in,这是把它做对;留 flag 仅为 A/B 对照)。

## 组件 B:LLM fact-rerank / recognition memory(种子质量)

HippoRAG 用 LLM 过滤候选 facts(三元组)再抽实体当种子(rerank.py)。我们的种子是 KG **节点**;我们的 `claim` 节点本身就是 NL fact 载体(如「DeepSeek-V3 employs DeepSeekMoE」)。故落地为**节点级过滤**,不重建三元组(我们 `about` 边 24k 噪声大)。

**机制:** 在 `_ppr_retrieve` 内,拿到 `federated_retrieve` 的 top-N 候选后、构造 reset 前:
1. 把候选(id + name + 首条 evidence 片段)交给 LLM 一次,问「哪些与 query 真正相关」,返回保留的 id 子集(JSON)。
2. 只有被保留的候选进入 reset 向量(再叠加 specificity 权重)。chunk 种子不受影响。

**LLM 客户端:** 复用 `reasoning_llm_client`(graph 模式的 verify 步已用它);未配置时跳过(fail-open)。

**失败处理(fail-open):** LLM 报错/超时/返回非法 JSON → 不过滤,沿用全部候选(并 `_note_model_error` 记录,对齐 graph verify 的容错口径)。绝不因 rerank 失败而让 graph 模式答不出。

**开关:** `ppr_fact_rerank_enabled`,**默认 False**(每查一次额外 LLM 调用,opt-in)。

**候选规模:** 输入 `ppr_kg_seed_top_n`(默认 20)个候选;LLM 返回相关子集(不强制固定数量,但实现可对空返回兜底为原候选,避免 reset 全空)。

## Q1 决策:不做「大簇星型边降权」

读了 HippoRAG `add_synonymy_edges`(821-883):它**不按簇/尺寸降权**。hub 控制是 ① 边权=余弦相似度(非 flat),② 每节点同义边硬封顶 100,③ PageRank 本身按度数归一(质量进高度数节点 ∝1/度数 摊薄)。

我们的星型路由 degree=簇大小,PageRank 已自动 ∝1/size 摊薄;且最大簇(KV cache ~32)远小于 100 封顶。故**不加 1/√size**(重复惩罚,且 HippoRAG 不这么做)。

**逃生口(本设计不实现):** 若真机数据显示某巨型簇灌水,HippoRAG 对齐的修法是**尺寸封顶**(簇 > N 成员则不建/抽稀其同义边),便宜,届时再加。

## 数据流(graph 模式,flag 全开时)

```
ask_graph (graph_ppr_enabled)
  → _ppr_retrieve(notebook_id, question)
      G = _ppr_graph(notebook_id)                      # P1,不变
      kg_cands = federated_retrieve(...)[:top_n]        # 共享,只读不改
      if ppr_fact_rerank_enabled:                       # 组件 B
          kg_cands = _ppr_fact_rerank(question, kg_cands)   # 后置过滤,fail-open
      for h in kg_cands:                                # 组件 A
          w = h.relevance / max(1, len(ent_chunk_map[h.object_id]))  if ppr_specificity_enabled else h.relevance
          reset[idx(h)] += w
      reset += chunk 种子(× passage_node_weight)        # 不变
      run_ppr → 归一 chunk 分                            # 不变
```

## 测试

- **specificity 单测:** 构造一个出现在多 chunk 的「大众」实体 + 一个稀有实体,二者 query 相关度相同;断言大众实体在 reset 里的权重被按 chunk 数压低(稀有实体 reset 权重更高);`ppr_specificity_enabled=False` 时回到等权。
- **specificity 端到端:** 在 `_seed_two_doc_moe` 基础上让某实体横跨多 chunk,断言开启后其主导被削弱(排序变化或权重断言)。
- **fact-rerank 单测:** stub LLM 过滤掉一个无关候选 → 断言该候选不进 reset(其专属 chunk 拿到的 PPR 质量下降/为 0);LLM 未配置 → fail-open(等同不过滤);LLM 抛错 → fail-open + 记 model_error。
- **隔离回归:** 断言 `ask_chunk` 与 `ask_reasoning` 行为不变(沿用现有用例);两开关默认值(specificity=True, fact_rerank=False)。
- 全量 suite 绿,无新回归。

## 不在本设计范围(后续)

emb-KNN 补未聚类同义边、`variant_of` 版本边、跑 `review_pending_merges`、`_ppr_graph` 纳入 base-tier 联邦、communities(GraphRAG Leiden)、把跨文档能力推广到 chunk 模式、大簇尺寸封顶。
