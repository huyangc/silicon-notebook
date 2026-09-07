# 逐步推理对标 chunk：无图首轮并入关键词臂，模型在首轮后判定是否直接作答（设计规格 v3，已拍板）

> **状态**：v3，已拍板进入实施（2026-09-07）。
> v1（固定「直答」档位）与 v2（先合成、不足再查）均已撤回：用户裁决「不设固定直答选项，由模型判断；问题理解与子问题检索不能省；
> 检索效果优先于省一次调用」。v2 的先合成只是为了把首轮后的 reflect 并进合成省一次调用，代价是拆 `run()` 的重构与判定质量的不确定，
> 按效果优先撤回。本文是缩小后的范围。
> 这是「问答方法归一」路线的第二步（第一步 KG 可选 reasoning 已合入 PR #690）。

**基线口径**：`origin/master = 4e9ed2ea1`。行号会腐烂，实现按分支内容定位。

## 结论先行

「让模型判断是否直接回答」在今天的 reasoning 里**已经成立**：首轮检索后的第一次 `reflect()` 就是这个判定点，模型返回 `answer`/`sufficient`
时 run 不进循环、不再付任何检索，直接收尾合成。第二步不需要新机制，只需要：

1. **无图首轮对标 chunk 的检索面**：把 `expand_query` 已产出的关键词留下来，在无图原文播种里并入 chunk 模式同款的词法臂（只加无图，D 已拍板）。
2. **一条契约测试**钉住「首轮后模型判定可答即直接作答」的成本形状：恰好一次 reflect、零动作执行、零额外检索。
3. **文档**把成本对照写清楚：reasoning 比 chunk 多一次 reflect（高级界面另有一次可编辑合同预览；自动模式下两者都付路由理解）。

## 现状事实

- `plan()` 调 `expand_query` 得到 `ExpandedQuery`（`sub_queries` + `high_level_keywords` + `low_level_keywords`，关键词按语料语言双语给出），
  只保留 `sub_queries`；关键词被丢弃。
- chunk 模式（`ask_chunk`）把 `high_level_keywords + low_level_keywords` 拼成一个空格分隔串，调 `candidates.keyword_chunk_candidates(notebook_id, kw_str)`
  得到词法（FTS）命中，在 mix/multi/single 三个分支里都与向量命中按段落去重合并（`_merge_direct_chunk_hits` / `_merge_multi_direct_chunk_hits`）。
  这是 chunk 在向量之外的第二条臂，也是「FTS 携带第二语言」到达 chunk 的路径。
- `RetrievalService.keyword_chunk_candidates(notebook_id, keywords)` 已存在（经 `filter_retrieval_items` 过滤），reasoning 未用。
- 无图播种（PR #690）：`_first_round_chunk_seed` 对每条子查询走 `search_chunks`（向量 + MMR），并入 `state.chunks`，命中记回 `attempted[query].new`。
- 首轮后的 reflect：`while steps < max_steps` 第一轮 `reflect()`，`decision.next_action == "answer" or decision.sufficient` 即 break。
  reflect 看的是候选摘要（每条 80 字）；这是既有设计，本规格不改。
- 成本（高级界面）：chunk = expand_query + answer = 2；reasoning 直接作答 = 理解预览 + plan + reflect + answer = 4；自动模式下 chunk = 3、reasoning = 4。

## 设计

### T1 关键词臂并入无图首轮播种（只加无图）

- `plan()`：保留 `ExpandedQuery` 的关键词，拼成与 chunk 模式同形的串（`" ".join(high_level_keywords + low_level_keywords)`），
  作为返回值的一部分交给首轮（`plan()` 的返回类型今天是 `List[SubQuery]`；改为返回 `(subqueries, keyword_string)` 会动报告引擎等调用方，
  因此改成把串挂到 `self`/state 之外的**显式出参**：新增 `plan_with_keywords(...) -> PlanOutcome(subqueries, keywords)`，`plan()` 保持签名与返回值不变、内部调用它并丢弃关键词——报告引擎与既有测试逐字不变；`_first_round_plan` 改调 `plan_with_keywords` 并把 `keywords` 写入 `state.plan_keywords`）。
  已确认意图路径（`reviewed_queries` 非空、`plan()` 不执行）没有关键词：`state.plan_keywords = ""`，词法臂不跑（chunk 模式在该路径也没有 expand 关键词）。
- `_first_round_chunk_seed`：`state.kg_in_scope` 为假且 `chunk_search_active()` 时（与现有播种同门），若 `state.plan_keywords` 非空，
  调一次 `retrieval.keyword_chunk_candidates(notebook_id, state.plan_keywords)`（经 `_filter_candidates("chunk", …)` 与 `retrieval_fanout_slot()`），
  `take_distinct_chunk_hits` 并入 `state.chunks`。关键词臂不按子查询记 `attempted.new`（它来自整题的关键词，不属于任一方向，与 chunk 模式同义）；
  seed 步 `detail` 加 `keyword_found`（零命中也写）。有图 run 不调（`kg_in_scope` 门）；kill switch 关闭时不调。
- 词法臂**必须有选择步**（评审订正）：`keyword_chunk_candidates` 走 `chunk_recall=200` 的召回窗，且关键词-only 融合分被重归一到 1.0，
  原样并入会把向量臂整体挤出合成 prompt。并入前按 relevance 降序取前 `state.per_query_take`（与向量臂每子查询同宽，档位化 4/8/8/12/16）——
  这是 chunk 模式「关键词命中先进 rerank/quota_fuse/MMR 选择步」的等价物。`keyword_found` 记并入后真正新增的段数。不新增配置。
- 关键词是 `expand_query` 的 zh/en 默认双语（`plan()` 不传 `corpus_langs`）；chunk 模式按语料语言。对齐需给 plan 传 `corpus_langs`，
  会改有图 run 的规划 prompt，登记为后续（`fangan_todo.md`「检索」）。
- 词法臂通道故障（fail-open 吞掉的异常）在 seed 步 `detail` 记稀疏键 `keyword_failed: true`，与「跑了但零命中」可分。

### T2 「首轮后直接作答」的成本契约

`tests/test_reasoning_retrieval.py` 新增：无图库 + fake reflect 返回 `{"next_action":"answer","sufficient":true}` →
恰好一次 `reflect` 调用、零动作分支执行（trace 里无任何动作步）、`search_chunks`/`keyword_chunk_candidates` 只在首轮各调一次、
`res.chunks` 含向量与词法两臂的命中；有图库同样条件下 `keyword_chunk_candidates` 零调用。
这条用例钉的是「模型说够就够」的成本形状，不是新行为。

### 文档

- `docs/product-and-api*.md`「检索模式」段：补「reasoning 首轮后由模型判定是否直接作答；无图首轮的原文证据来自向量臂 + 关键词词法臂，
  与 chunk 模式同法」；成本对照一句（reasoning 直接作答比 chunk 多一次 reflect）。
- `fangan_todo.md` 归一路线：第二步改为本规格并标已实施；第三步前提写「reasoning 直接作答的调用数 = chunk + 1 次 reflect」。

## 验收

1. 无图 run：seed 步 `detail.keyword_found` 存在；词法独有段落进入 `res.chunks`；`plan()` 签名/返回值不变（报告引擎用例全绿）。
2. 有图 run：`keyword_chunk_candidates` 零调用；trace/账目逐字不变。
3. 已确认意图路径（`intent_queries` 非空）：无关键词、不调词法臂。
4. T2 契约用例通过；变异（去掉词法臂并入）必须有用例变红。
5. `bash scripts/check.sh` 全绿。

## 刻意不做

- 先合成/不足再查（v2）：撤回，效果优先。
- 有图 run 的关键词臂：与第一步 D-1 边界一致，不动有图证据构成。
- 生成问题补召回、rerank：前者是部署级 rollout，后者 chunk 模式只在 mix 用。
