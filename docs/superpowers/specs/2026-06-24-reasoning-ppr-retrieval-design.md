# Spec:PPR 跨文档检索接入深挖推理(reasoning)

- 日期:2026-06-24
- 状态:设计已批(用户「同意」),待实现计划(writing-plans)
- 分支:`claude/reasoning-ppr-retrieval`(off origin/master `bb46f45`)
- 关联记忆:`hipporag-ppr-plan`、`comparative-retrieval-collapse`、`chunk-native-retrieval-state`

## 背景 / 动机

深挖推理(`ask_reasoning` → `ReasoningRetriever` 的 plan→retrieve→reflect 循环)检索的是 **KG 节点**(`federated_retrieve`);reflect 里:

- `expand_graph` = 1-hop 邻居走 `knowledge_relations`,而**跨文档边 = 0**(43719 条边无一跨文档),跳不出单篇;
- `search_elements` = 向量查原文,但结果在 `_answer_reasoning` 里只作**「供参考、无引用编号」**(取 ≤6 条、截 200 字、不进 `[k]` 锚点)。

后果:对比 / 跨文档问题**坍缩到单篇**(与 chunk 模式同病),且答案**基于 KG 节点、缺原文 grounding**。

PPR(HippoRAG 式跨文档传播)已在 **graph 模式**落地并默认开(`_ppr_retrieve`/`_ppr_graph`,`GRAPH_PPR_ENABLED=True`),是**与模式无关的检索原语**。本设计把它接入 reasoning。

## 目标

- 给 reasoning 一个**跨文档检索能力**,产出可被 `[k]` 引用的**原文 chunk 证据**。
- **确定性兜底**对比坍缩:不依赖 agent 是否主动调用。
- **零影响** chunk / graph 模式;**不新增对外开关**。

## 非目标

- 不动 chunk 模式(独立项目,见 `hipporag-ppr-plan` 遗留)。
- 不重写 `expand_graph` / `search_elements`(互补能力,保留)。
- 不做 comparison-intent 检测器(用 always-seed 兜底替代,YAGNI)。
- 不改 `GRAPH_PPR_ENABLED` 命名(后续单独 PR)。

## 设计

### A. 新 reflect 动作 `ppr_retrieve`(agent 自选)

- `ReflectDecision`([reasoning_retrieval.py:49](backend/app/services/reasoning_retrieval.py:49))加 `ppr_query: str = ""`。
- `reflect()` 动作白名单([reasoning_retrieval.py:123](backend/app/services/reasoning_retrieval.py:123))加 `"ppr_retrieve"`;解析 `ppr_query`(同 `elements_query` 在 [:143](backend/app/services/reasoning_retrieval.py:143))。
- `reflect_prompt` 增引导一句:**「需要对比 / 跨模型 / 跨来源 / 求全景 → 用 `ppr_retrieve`,并给 `ppr_query` 检索串」**;`REFLECT_SCHEMA_HINT` 增 `ppr_query` 字段。
- `ReasoningRetriever` 加薄封装(照 [search_elements:89](backend/app/services/reasoning_retrieval.py:89) 模式):

  ```python
  def ppr_retrieve(self, notebook_id, query):
      return self.repo._ppr_retrieve(notebook_id, query)
  ```

### B. seed pass(确定性兜底 —— 用户拍板的 C)

- `run()` 初检索之后、进 reflect 循环之前,若 `settings.graph_ppr_enabled`:**无条件跑一次** `self.ppr_retrieve(notebook_id, question)`(用**原问题**,非子查询,求广覆盖),结果进 `chunks` 累积器。
- 保证对比题**至少有一轮跨文档 chunk**,不赌 agent 是否选动作。纯图传播、无 LLM、graph 已缓存 → 成本可忽略。

### C. chunk 累积 + 去重 + 熔断

- `run()` 维护 `chunks: List[RetrievedChunk]`;seed pass 与 `ppr_retrieve` 动作都 `extend`,按 `chunk_id` 去重(`seen` 集,照 `search_elements` 的 `seen_el` 模式)。
- `ppr_retrieve` **动作**纳入熔断:**写死的模块常量** `_MAX_PPR_RETRIEVES = 3`(照 `reasoning_retrieval.py` 已有的 `_PER_QUERY_LIMIT` 风格,**不进 Settings、不加 env 开关**);超限则跳过并写 trace。**B 的 seed pass 不计入此上限**(它是保证基线、非 agent 动作)。`no_progress` 判定把 `len(chunks)` 计入(与 `collected`/`elements` 并列)。
  - **为何需要这个上限**:循环硬边界是 `reasoning_max_steps=50`,而 `stale` 熔断只在「无新证据」时跳;`ppr_retrieve` 每次换 query 都能拉到新 chunk = 算「有进展」= stale 不跳 → 没有专门上限时,一次推理最多触发 **50 次全图 PageRank**(38592 实体 + 6534 chunk),延迟不可接受。`search_elements` 早有同一上限(`reasoning_max_element_searches=5`,注释「防每次有新增但永不满足」),PPR 更贵,故必须有。
  - **为何写死而非 env knob**:它是内部循环安全阀、非调参旋钮,用户偏好少开关(与 `element_searches` 是 env knob 略不一致,但少开关优先)。
- `_summarize(collected, elements)`([:165](backend/app/services/reasoning_retrieval.py:165))扩为含 `chunks` 摘要,让 reflect 的 agent **看到已有的跨文档证据**(避免重复 `ppr_retrieve` 或在足够时收尾)。

### D. 结果透传

- `ReasoningResult`([:62](backend/app/services/reasoning_retrieval.py:62))加 `chunks: List[RetrievedChunk] = field(default_factory=list)`。
- `ask_reasoning`([sqlite_repository.py:5921](backend/app/services/sqlite_repository.py:5921)):`... = result.top_hits, result.elements, result.trace, result.chunks`;把 `chunks` 传给 `_answer_reasoning`([:5954](backend/app/services/sqlite_repository.py:5954))。

### E. 答案侧:chunk 升为一等引用证据

`_answer_reasoning`([:5839](backend/app/services/sqlite_repository.py:5839))加 `chunks` 参数:

- **chunks 非空**时,上下文组装升级为 mix:
  - chunk 段:`_chunk_answer_context(chunks)` → `k1..kN`;
  - KG 推理链段:`top_hits` 以 `id_offset=_MIX_KG_KEY_BASE`(=1000)构 block → `k1001+`;
  - 合并 `context_block` + `id_map`,**两者都 `[k]` 可引用**;
  - **仍走 `reasoning_llm_client.chat_json` + `reasoning_timeout`/`reasoning_max_retries`**(不串 fast 模型);
  - `search_elements` 的 `elements` 继续作「供参考、无编号」二级段,与 PPR chunk **并存**(两种能力互补)。
- **chunks 为空**时:**维持现状**(`_answer_context` KG-only + 供参考 elements)。
- 实现取向:**在 `_answer_reasoning` 内联 mix 组装**(复用 `_chunk_answer_context` + offset 化 KG block),而非直接调 `_answer_mix`——因为 [`_answer_mix`:5207](backend/app/services/sqlite_repository.py:5207) 写死 `self.llm_client`(fast 模型);`_answer_reasoning` 已自带 reasoning client 调用,只需升级 context/id_map 组装即可。
- 锚点:返回的 `anchors` 含 **chunk 锚(`object_type=passage`)+ KG 锚**;reasoning 答案首次出现 chunk 引用(graph BFS mix 已验证前端 UI 支持 chunk 锚)。

### F. 开关 / 不变量

- **复用 `GRAPH_PPR_ENABLED`**(默认 `True`)作为 reasoning 的 PPR 总开关。关 → seed pass 不跑、`ppr_retrieve` **动作被 skip**(`ppr_disabled`,零 PageRank)、`_answer_reasoning` 收到空 `chunks` → **回到今天的行为**。**不新增开关**。`reflect_prompt` 始终列出该动作(不把 flag 串进 prompt 签名);off 时 agent 偶尔选到只是一次 no-op skip。
- 命名小瑕:`GRAPH_PPR_ENABLED` 现在也管 reasoning、名字带 `graph`;先复用,后续想改 `PPR_ENABLED` 另起小 PR。
- 守 `[0,1]`/tau:`_ppr_retrieve` 已 min-max 归一 `relevance ∈ [0,1]`;chunk 与 KG hit 同量纲。需核 `ask_reasoning` 里 `evidence_level`/`top_relevance` 计算把 chunks 纳入或至少不破坏(chunks 的 relevance 已在 [0,1])。

## 数据流

```
plan(question) → 初检索(KG 种子,per sub-query)
  → [seed pass] ppr_retrieve(question) → chunks    # 兜底,flag 开时无条件
  → reflect 循环:
       expand_graph / add_subquery / search_elements   # 原样
       ppr_retrieve(ppr_query)                          # 新:agent 选 → 再拉跨文档 chunk
       answer → 收尾
  → ReasoningResult(top_hits, elements, trace, chunks)
ask_reasoning → _answer_reasoning(..., chunks)
  → chunks 非空:mix(chunk k1..N + KG k1001+) → answer + (chunk锚 + KG锚)
  → chunks 为空:KG-only(今天的行为)
```

## 错误处理 / 边界

- `_ppr_retrieve` 无 KG / 无 chunk / 无 reset 向量 → 返回 `[]`(已实现);seed pass 与动作都安全降级到 KG-only。
- flag off → 全链路 no-op。
- chunk 去重防 seed pass 与动作重复拉同段。
- 熔断上限防 agent 刷 `ppr_retrieve`。
- reflect LLM 异常 → 现有 `except` 返回 `answer_decision`(不受影响)。
- 聚焦单篇问题:PPR reset 向量由 question 锚定 → chunk 虽跨文档但**题相关**;specificity 权重(always-on)压通用概念桥;答案 LLM 自行取舍。**真机观察是否稀释**(见未决)。

## 测试

1. **动作路由**:stub reflect 决策 `next_action=ppr_retrieve` → 断言 `_ppr_retrieve` 被调、chunks 进 `ReasoningResult`。
2. **答案引用**:chunks 非空 → `_answer_reasoning` 上下文含 chunk 段、`anchors` 含 chunk 锚、答案 `[k]` 可引用 chunk。
3. **seed-pass 兜底**:stub 决策只 `answer`(永不选 ppr_retrieve)+ 2 文档概念桥 → 答案仍含跨文档 chunk。
4. **跨文档正确性**:2 文档 + 共享概念簇 → `ppr_retrieve` 拉到**兄弟文档** chunk。
5. **熔断**:`_MAX_PPR_RETRIEVES` 常量上限生效(超限跳过 + trace);seed pass 不计入。
6. **flag off**:`GRAPH_PPR_ENABLED=false` → 无 ppr_retrieve 动作 / 无 seed、`_answer_reasoning` chunks 空、行为同今天。
7. **隔离**:`ask_chunk` / `ask_graph` 路径快照不变(护栏)。
8. **不变量**:chunk `relevance ∈ [0,1]`;`evidence_level` 计算不被 chunks 破坏。

## 未决 / 后续

- `GRAPH_PPR_ENABLED` → `PPR_ENABLED` 改名(单独小 PR,改 `.env`/config/tests)。
- 若真机见 agent 忽略 / 过度依赖 `ppr_retrieve`,或 seed-pass 稀释聚焦单篇答案,再评估 comparison-intent 检测器 / 单篇短路(当前用 always-seed 兜底 + specificity 替代)。
- chunk 模式接 PPR(独立项目,已记 `hipporag-ppr-plan`)。
