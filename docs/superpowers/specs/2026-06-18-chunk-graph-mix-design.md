# chunk×graph mix(忠实 LightRAG)+ qwen3-rerank 检索升级

- 日期:2026-06-18
- 状态:设计已确认,待写实现计划
- 关联:`2026-06-17-kg-relation-retrieval-design.md`(关系检索)、`2026-06-18-kg-quality-pass-design.md`(KG 去噪);借鉴源 `/Users/hzf/workspace/ref-kg/LightRAG`(mix 模式 `operate.py:_build_query_context`)
- 前置:依赖 PR #59 的关系检索 + KG 去噪(relation_embeddings / canonical 折叠 / 干净 _payload_text)

## 1. 背景

LightRAG 对比里最大的未实现项是 **mix**:把 chunk 向量 + KG(实体 + 1-hop 关系)**融在一个答案**里。我们之前 chunk-native 与 KG 完全分家(chunk 默认问答不碰 KG)。现在前置已就绪:关系可检索、KG 去噪后 graph A/B 转正(种子更好→答案更好)。同时**输入用得太少**(`CHUNK_MMR_K=16`),而答案模型(deepseek-v4-pro)有 1M 上下文。

**本设计:把 `ask_chunk` 升级成忠实 LightRAG 的三路 mix,并用 qwen3-rerank 取代双塔+MMR 做 chunk 选择;token 预算照 LightRAG 实际值。** chunk 用量随之从 16 升到上百(token 预算自然吃满)。

## 2. 目标与成功判据

**目标:** 默认 chunk 问答同时用上 chunk 原文 + KG 局部结构;chunk 选择用 cross-encoder rerank;充分利用大上下文。

**成功判据:** `CHUNK_KG_OVERLAY_ENABLED` ON vs OFF 在 nb-b37185f4ae 上跑 chunk 问答 LLM-judge —— 覆盖面/correctness ↑、grounding 不降、伪引用不升。

## 3. 范围

**In:** ① `ask_chunk` 三路 mix(naive/local/global);② qwen3-rerank 模型客户端 + chunk 选择改 rerank→token 预算;③ **删除旧 LLM 打分 rerank**(`RERANK_ENABLED`/`_rerank_hits`/`rerank_prompt`);④ token 预算截断(LightRAG 值);⑤ 统一 `[k]` 引用;⑥ `CHUNK_KG_OVERLAY_ENABLED` 默认 True + 可用性门控;⑦ ON/OFF eval。

**Out:** 不改 `graph`/`reasoning` 模式的答案逻辑(仅删其中的 `_rerank_hits` no-op 调用);不动 KG 抽取;v2 才考虑 rerank 用于 graph 种子、per-subquery rerank。

## 4. 不变量(硬约束)

- **rerank 分只驱动排序/选择,绝不进 grounding。** chunk 的 `.relevance` 仍用 `_fuse`(keyword+semantic)喂 `classify_evidence`/tau;qwen3-rerank 的 0-1 分仅作 rank_key。**与 RRF/tier 同纪律,防 `0ca8f1a` 把 tau 打塌。**
- `CHUNK_KG_OVERLAY_ENABLED` 关 → `ask_chunk` 与现状**字节等价**(走 MMR、不注入 KG)。
- rerank 模型未配 → **回退 MMR**(不退化、不报错)。
- 无 KG(notebook 及 base 均无)→ 退**纯 chunk**。
- KG 文本走干净 `_payload_text`(已去 section_path)+ canonical 折叠。

## 5. 架构与组件

### 5.1 qwen3-rerank 客户端(新)
- `RerankClient`(`app/services/rerank_client.py`):`POST {base}/reranks`,`Authorization: Bearer {key}`,body `{model, query, documents:[text...], top_n}`,resp `results:[{index, relevance_score}]`。
- config:`RERANK_MODEL`(默认 ""=关→回退 MMR)、`RERANK_BASE_URL`(默认 `https://dashscope.aliyuncs.com/compatible-api/v1`)、`RERANK_API_KEY`(缺省复用 embedder/DashScope key)、`RERANK_TOP_N`(可选)。`configured` = MODEL 非空。
- 约束:≤500 文档/请求 ≤120k token(我们召回 150 远在内);query ≤4000 token。失败/超时 → 返回原序(降级)。

### 5.2 三路检索(`_mix_retrieve`,在 ask_chunk 内)
复用现有原语,**不新建检索**:
- **naive(chunk)**:`_retrieve_chunks(_multi)` 召回(`CHUNK_RECALL`,建议升到 ~200)。
- **local(节点+1hop)**:`ex.low_level_keywords`/`query_en` → `federated_retrieve`(节点,canonical 折叠)取 top_k → `multihop_subgraph(depth=1, fan_out)` 取 1-hop 边/邻居 → 实体(`node_context` 取描述/簇描述)+ 关系 + 它们 evidence 关联的 chunk。
- **global(关系)**:`ex.high_level_keywords` → `federated_retrieve_relations` top_k → 两端实体 → 关联 chunk。
- 实体/关系 round-robin 去重(实体按 canonical_id/name、关系按 sorted(src,tgt))。
- 注:local 节点 `top_k` 与 1-hop `fan_out` 用**宽松的硬编码常量**(广召回,如 node top_k≈20 / fan_out≈8),**不新增 env 旋钮**;真正的量由 §5.5 的 entity/relation token 预算按排序截断决定(照 LightRAG"猛召回 + token 预算截"的范式)。

### 5.3 chunk 选择:rerank → token 预算
- 三路 chunk 源(vector / entity-related / relation-related)**round-robin 去重合并**(按 chunk_id;vector 优先)。
- **rerank**:把合并后 chunk 文本喂 `RerankClient`,按 `relevance_score` 重排;未配则 MMR(`_mmr_select_chunks` / 多查询 `quota_fuse`)。
- **token 预算**:`chunk_budget = MAX_TOTAL_TOKENS − entity_used − relation_used − sys/query/buffer`,按排序累加 chunk 至填满(`truncate_by_token`)。

### 5.4 统一上下文 + `[k]` 引用
- 扩 `_chunk_answer_context`:产出三段(实体段 / 关系段 / chunk 段),**所有项进同一 `id_map`** 拿 `k{i}`。
- `_parse_answer_anchors(answer, id_map)`:chunk 项→`Citation`(element_id),KG 项→`AnswerAnchor`(object/relation id + tier)。
- `classify_evidence(selected_chunks ∪ kg_items, anchors, ...)`:grounding 在合并集上、tau 用各项**融合 relevance**(KG 项用其检索 relevance,chunk 用 `_fuse`),rerank 分不参与。

### 5.5 token 预算(照 LightRAG 实际值,env 可调)
- `MAX_ENTITY_TOKENS=6000`、`MAX_RELATION_TOKENS=8000`、`MAX_TOTAL_TOKENS=30000`。实体/关系按检索序累加截断;chunk 吃总预算余量。

### 5.6 删除旧 LLM rerank
- 删:config `rerank_enabled`/`rerank_candidates`/`rerank_timeout_seconds`;`sqlite_repository._rerank_hits` + import;`prompts.rerank_prompt`/`RERANK_SCHEMA_HINT`;`tests/test_rerank.py`;`.env.example`/README 相关行。
- 改调用点(均 no-op,行为不变):`ask_graph:5008`、`reasoning_retrieval.py:320` 去掉 `_rerank_hits` wrap。

## 6. 数据流
```
ask_chunk: rewrite → expand_query(ex: 子查询 + hl/ll keywords)
   ├─ naive: _retrieve_chunks(_multi) 召回
   ├─ [有KG] local: ll→federated_retrieve 节点(折叠)→ multihop depth=1 → 实体+关系+关联chunk
   └─ [有KG] global: hl→federated_retrieve_relations → 两端实体 → 关联chunk
   → 实体/关系 round-robin 去重 + token 预算截断(6000/8000)
   → 三路 chunk round-robin 合并 → rerank(qwen3,未配回退MMR)→ chunk token 预算
   → _chunk_answer_context(实体段+关系段+chunk段, 统一 id_map)
   → 答案 LLM([k] 引三类)→ anchors(Citation+AnswerAnchor)
   → classify_evidence(合并集, 融合relevance) → grounding
```

## 7. 错误处理 / 回退
- rerank 失败/未配 → MMR/quota_fuse。
- KG 缺 / 关系向量或簇缺 → 跳过 local/global,纯 chunk。
- KG LLM(node_context 描述)无 → 用 evidence snippet(现状)。
- `CHUNK_KG_OVERLAY_ENABLED` 关 → 纯 chunk 字节等价。

## 8. 测试
- 单元:`RerankClient` 解析(mock HTTP);rerank→token 预算选择(rerank 分排序、融合分管 grounding 的分离);三路 round-robin 去重;统一 id_map 混合 anchor;无 KG/无 rerank 优雅退化;flag 关等价;token 预算截断边界。
- 删除:`test_rerank.py`(旧),`ask_graph`/reasoning 去 `_rerank_hits` 后既有测试仍绿。
- 真机 eval:nb-b37185f4ae chunk 问答 ON/OFF + rerank 开关对照(correctness/grounding/伪引用/覆盖面)。

## 9. 配置增减
- 增:`CHUNK_KG_OVERLAY_ENABLED=true`、`RERANK_MODEL`/`RERANK_BASE_URL`/`RERANK_API_KEY`/`RERANK_TOP_N`、`MAX_ENTITY_TOKENS=6000`/`MAX_RELATION_TOKENS=8000`/`MAX_TOTAL_TOKENS=30000`、`CHUNK_RECALL` 升至 ~200。
- 删:`RERANK_ENABLED`/`RERANK_CANDIDATES`/`RERANK_TIMEOUT_SECONDS`。

## 10. 关键决策
- v1 忠实照 LightRAG mix(三路 + token 预算实际值),输出层保留我们 `[k]`/Citation/AnswerAnchor。
- chunk 选择 qwen3-rerank(cross-encoder)取代双塔+MMR(块数变大后精排序>多样性);MMR 降级 fallback。
- 删旧 LLM 打分 rerank(被 qwen3 取代)。
- rerank 分只排序、融合分管 grounding(守 [0,1]/tau)。
- `CHUNK_KG_OVERLAY_ENABLED` 默认 True(非 opt-in)+ 留作 A/B/kill-switch。
