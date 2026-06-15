# P3:查询改写/扩展(共享查询理解层)设计

**状态:** 设计已与用户敲定核心架构(2026-06-16),待用户复审本 spec → writing-plans。

## 背景与根因
chunk-native(P1+P2,#44)上线后,默认 chunk 模式对**单实体**问题表现良好,但两类系统性偏弱(本次会话实测诊断):
1. **多实体对比**(如 "deepseekv3 相比 deepseekv2 有什么改进"):单条 query 的 embedding 语义偏向被问"改进"的一方(V3),另一方(V2)被低估——纯相关度下 V2 仅排第 1 名;k=40/pro 只是"挖深"硬捞,非根治。
2. **跨语言**:中文问题对英文语料召回弱。
3. **实体/版本号**:`deepseekv2` 与语料 `DeepSeek-V2` 关键词零匹配(`keyword_score("deepseekv2", "…DeepSeek-V2…")=0` vs `"deepseek v2"=1`)。

根因统一为:**用户原始 query 与语料之间存在"语言/写法/单视角"鸿沟,单条 query 无法兼顾**。解法是在检索前加一层**查询理解**(改写 + 翻译 + 分解),并对多子查询结果做**配额融合**保证各方都被代表。

## 已定决策(brainstorm)
- **共享查询理解层**(而非给 chunk 单独造一套):查询理解与配额融合做成 chunk 与 reasoning 共用的原语,两模式各自编排。
- **范围四项**全做,且都收敛进"一次 LLM 扩展调用 + 配额融合":对比分解 / 中→英 / 实体规整 / 泛→具体。
- **总是 expand**(不做脆弱的"是否需要改写"检测;简单题自然只出 1 个子查询,退化成现有行为)。
- DeepSeek-V4 采样已按官方推荐改为 temperature=1.0/top_p=1.0(#48),expand 调用沿用。

## 架构:三个共享原语
新增模块 `backend/app/services/query_rewrite.py`:

1. **`normalize_terms(q: str) -> str`** — 纯函数,轻量版本号/实体规整(letter↔digit 边界补空格等,如 `gpt4→gpt 4`)。**无 LLM**。注意:`deepseekv2→deepseek v2` 这种"连续字母中间无边界"的,靠下面的 LLM 扩展规范化(LLM 会写出 `DeepSeek-V2`),`normalize_terms` 只做边界明确的廉价补充。

2. **`expand_query(llm_client, question, history="") -> ExpandedQuery`** — 一次 LLM 调用(`prompts.expand_query_prompt` + `EXPAND_SCHEMA_HINT='{"query_en":"","sub_queries":[""]}'`)。输出 `{query_en: str, sub_queries: List[str]}`:把问题(任意语言)改写成**英文**,并分解成 1–4 个**具体英文子查询**——对比题每实体一个、宽问题每维度一个、简单题就 1 个;子查询用规范实体名。封顶 `settings.chunk_max_subqueries`(默认 4)。
   - `ExpandedQuery` dataclass:`query_en`、`sub_queries`(已含 `normalize_terms` 处理 + 去空去重 + 封顶;保证 `len(sub_queries) >= 1`)。

3. **`quota_fuse(candidates, score_for, sub_queries, top_n) -> List`** — 从 `reasoning_retrieval._quota_rerank` 抽出的通用配额 round-robin:把候选按"relevance 最高的子查询"分组,各组内降序后**跨组轮流取队首**直至 top_n;全 0 命中的归兜底组最后轮转。`score_for(candidate, sub_query) -> float` 由调用方提供(chunk 用 chunk relevance,KG 用 knowledge relevance)。
   - 落地:把现有 `_quota_rerank` 的分组/轮转核心抽到此函数;`reasoning_retrieval._quota_rerank` 改为薄封装调用它(行为不变,现有推理测试须仍过)。

## 数据流:chunk 模式(`ask_chunk`)
```
question
 → normalize_terms
 → expand_query(1 次 LLM)  → {query_en, sub_queries[1..4]}     # 未配置/失败 → 回退 [normalize_terms(question)]
 → 对每个 sub_query 并发跑 _retrieve_chunks(大召回)            # ThreadPoolExecutor,沿用 reasoning 的并发写法
 → 合并候选 → 选 top-K(chunk_mmr_k):
       len(sub_queries) >= 2 → quota_fuse(按子查询配额,保证各实体都被代表)
       len(sub_queries) == 1 → 现有 _mmr_select_chunks(单查询多样性)
 → _chunk_answer_context + _answer_chunks(合成,不变)
```
比现状多一次 expand LLM 调用(默认模式略慢,换对比/跨语言可靠 + 综述覆盖更全)。`conversation` 历史改写(`_rewrite_followup_query`)保留在 expand 之前或并入 expand(实现期定;二者都接受 history)。

## 与推理模式结合
- `reasoning_retrieval.plan()` 改为**建在 `expand_query` 之上**:先拿到共享的 `sub_queries`(于是翻译/泛→具体/实体规整也让推理受益),再补它特有的 KG `types`(沿用其现有 type 选择逻辑)。`reflect`/迭代 agent 循环**不变**。
- `_quota_rerank` 改为复用 `quota_fuse`。
- **查询理解 + 融合 = 共享原语;chunk 一次性编排、reasoning 迭代编排。**
- **Think Max 384K(顺带项,低优先):** 推理模式上下文窗口建议 ≥384K。实现期加一条检查:推理路径的上下文 budget 不被卡到远小于模型窗口;若有硬编码上限则提为可配。具体阈值由用户定;不影响 chunk 主线。

## 配置(`config.py`)
- `chunk_max_subqueries: int = Field(4, env="CHUNK_MAX_SUBQUERIES")`
- `query_rewrite_enabled: bool = Field(True, env="QUERY_REWRITE_ENABLED")` —— 关掉则 chunk 退回单查询(便于排错/对照)。

## 错误处理(永不阻塞回答)
- `expand_query`:LLM 未配置 / 超时 / 异常 / 非法 JSON / 空 `sub_queries` → 一律回退 `[normalize_terms(question)]`(等同现有单查询检索)。始终 `len(sub_queries) >= 1`。
- 某子查询 0 命中 → 不贡献候选(`quota_fuse` 容空组)。
- 全部 0 命中 → selected 空 → 现有确定性"无内容"结论。
- 成本/延迟界定:子查询 ≤ `chunk_max_subqueries`,每个召回 ≤ `chunk_recall`。
- 不引入缓存(若 `llm_cache_enabled` 则天然受益);不做查询分类分支(总是 expand)。YAGNI。

## 测试
- `normalize_terms`、`quota_fuse`:纯函数单测(后者从现有 `_quota_rerank` 测试扩展/迁移,守住分组/轮转/兜底组与计数语义)。
- `expand_query`:假 LLM → 正常解析 + 各回退路径(非法 JSON / 空 / 异常 / 未配置)+ 封顶 + 去重。
- `ask_chunk` 多查询(hermetic:FakeEmbedder + 假 LLM 出 2 个子查询):种"两实体"chunk,问对比题 → **断言 selected/anchors 两实体都被代表**(守住对比平衡修复)。单子查询时走 MMR、行为不变。
- reasoning:重构 `_quota_rerank`→`quota_fuse`、`plan`→`expand_query` 后,现有推理测试仍全过;补一个 `plan` 调用 `expand_query` 的断言。
- 真机:Jamba 的 "deepseekv3 vs deepseekv2" → 稳定 grounded 且 **V2、V3 都被引用**;一个中文问英文材料的跨语言题召回明显改善。

## 实施阶段(供 writing-plans)
- **P3-1** `normalize_terms` + 纯函数单测。
- **P3-2** `expand_query` + `expand_query_prompt`/`EXPAND_SCHEMA_HINT` + 假 LLM 单测(含回退)。
- **P3-3** 抽 `quota_fuse`(共享)+ `reasoning_retrieval._quota_rerank` 改薄封装 + 测试(现有推理测试不破)。
- **P3-4** `ask_chunk` 接线:多子查询并发召回 + quota_fuse/MMR 选择 + config 旋钮 + 多查询集成测试。
- **P3-5** `reasoning_retrieval.plan` 改建在 `expand_query` 上(+ 384K 上下文检查)+ 测试。
- **P3-6** 全量验证 + 真机 V3vsV2/跨语言对照 + PR。

## YAGNI / 非目标
- 不做查询"是否需要改写"的分类器(总是 expand)。
- 不引入新的融合算法(复用 quota round-robin)。
- 不做 expand 结果缓存(交给 llm_cache)。
- 不动两层 KB / 存储解耦(另线 roadmap)。
