# BFS graph 答案叠加源 chunk 原文 设计

**日期:** 2026-06-24
**状态:** 设计已与用户对齐,待写实施计划
**分支:** `claude/graph-bfs-source-chunks`(off origin/master 7ef54b3,已含 P2 + codex cancel 控制)

## 目标

PPR 关时(默认),graph(严格推理)模式走 BFS 子图路径,当前只把 KG 节点名 + 关系链 + 每条边一句证据引文喂给模型,**缺原文血肉**。本特性让 BFS 路径**把子图 KG 节点背后的源文章 chunk 整段也带给模型**,让答案有原文支撑、并能出 chunk 引用——与 PPR 路径同款体验。

## 范围与不变量

- **只动 `ask_graph` 的 BFS 分支**(`graph_ppr_enabled=False` 那条)。不碰 PPR 分支、不碰 `ask_chunk`/`ask_reasoning`/`federated_retrieve`。
- **永久开,无新 flag**(纯增强;无源 chunk 时自动回退现状)。
- 复用现成机器:`_kg_source_chunks`、`_answer_mix`、`render_subgraph_context`、`verify_chain_edges`,**不新增答案/上下文拼装逻辑**。
- 守 [0,1]/tau:`classify_evidence` 口径复用,chunk 与 KG 命中同一相关度尺度。

## 数据流(BFS 分支,改造后)

```
ask_graph (graph_ppr_enabled=False)
  种子 federated_retrieve → _federated_rx_graph → multihop_subgraph 得 subgraph
  verify_chain_edges(subgraph)            # 边对抗校验/降权,保留
  ── 新增 ──
  oids = [子图里 KG 节点 object_id]
  chunks = self._kg_source_chunks(notebook_id, oids)     # 节点 evidence.element_id ∈ chunks.element_ids
  chunks = 去重(按子图顺序,seed 优先) + 截断(_GRAPH_SRC_CHUNK_CAP=12)
  if chunks:
      kg_block, kg_id_map = render_subgraph_context(subgraph, id_offset=_MIX_KG_KEY_BASE)  # KG 用 k1001+
      answer, grounded, anchors = self._answer_mix(question, chunks, kg_block, kg_id_map, history)
      # _answer_mix:chunk 段 k1..kN(原文)+ KG 段 k1001+,统一 id_map,出 [k] 锚点两边都能引
      citations = [Citation(source_id/section…) for a in anchors if a.object_type=='chunk' and a.object_id∈chunks]
  else:
      # 回退:现状 KG-only(render id_offset=0 → _refine_context → answer_prompt),不变
```

## 关键设计点

1. **键位对齐**:`_answer_mix` 约定 chunk 段用 `k1..kN`、KG 段用 `k(_MIX_KG_KEY_BASE)+`(=1001+,`_MIX_KG_KEY_BASE=1000`)。故 KG 块必须 `render_subgraph_context(subgraph, id_offset=_MIX_KG_KEY_BASE)`(现状 BFS 用 id_offset=0,回退分支保持 0)。chunk 段由 `_answer_mix` 内部 `_chunk_answer_context` 编号,`_answer_mix` 已硬截 `chunks[:_MIX_KG_KEY_BASE-1]` 防撞键。
2. **取哪些节点的 chunk**:子图全部 KG 节点(seed + 多跳邻居)的 object_id 一起喂 `_kg_source_chunks`;它按 evidence.element_id ∩ chunks.element_ids 回拉、自带去重。再加节点对应 chunk 数上限 `_GRAPH_SRC_CHUNK_CAP=12` 避免上下文爆(`_answer_mix` 本身也有 token 预算兜底)。
3. **引用**:`_answer_mix` 的 anchor 含 chunk 与 KG 两类。chunk 锚点 → 建 `Citation(source_id, element_id, section, quoted_span)`(BFS 答案从此带 chunk 引用);KG 锚点 → related_knowledge(沿用现状)。
4. **verify_chain_edges 保留**:仍在取 chunk / render 之前跑,降权后的边体现在 kg_block 里。
5. **回退零副作用**:`_kg_source_chunks` 空(节点无 chunk 证据,或 notebook 无 chunk)→ 完全走现状 KG-only 答案。

## 常量

- `_GRAPH_SRC_CHUNK_CAP = 12`(新增类常量):子图节点对应 chunk 的数量上限。非 env flag(用户不想要更多开关),代码常量。

## 测试

- **带原文**:seed KG 节点(claim/concept)+ 其 evidence 指向某 chunk + relations 构成子图;`graph_ppr_enabled=False` 下 `ask_graph` graph 提问 → 断言 `resp.citations` 出现 chunk 引用(有 source_id),且走到了 `_answer_mix`。
- **回退**:KG 节点无 chunk 证据 → 断言走 KG-only(无 chunk 引用、不报错、`reasoning_trace` 仍是 graph_verify)。
- **隔离**:`ask_chunk`/`ask_reasoning` 源码不引用本改动;PPR 分支不受影响。
- 既有 graph 用例若 seed 了 chunk(如 `test_ask_redesign`)→ 适配到「KG+原文」新行为(新行为即预期);未 seed chunk 的 graph 用例自动回退、不变。

## 不在范围

PPR 路径(已带 chunk);把能力下放 chunk 模式;社区/联邦等 P2 项;给 reasoning 模式同样增强(它已有 definition+snippet 富化)。
