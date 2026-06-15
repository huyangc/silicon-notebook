# chunk-native 检索架构重做 设计

日期：2026-06-15
状态：已与用户对齐全部关键决策（KG 边界 / 路由 / chunking / embedding 配合 / KG 开关化）

## 1. 背景与根因(三轮实验的实证)

对照 Google NotebookLM,silicon-notebook 在三类问题上都明显更差,实测根因如下:

| 问题 | 我们的表现 | 实测根因 |
|---|---|---|
| 综述全库「LLM 架构演进」 | 只覆盖 ~8 篇(配额前只 DeepSeek) | top-N 通吃 + 覆盖不足 |
| 「V3 vs V2 + R1」复合 | R1 通吃(已修配额 #43) | 整串重排通吃 |
| 「V3 vs V2 差别」具体对比 | 答不出 FP8/MTP/DualPipe(库里有 35/30/19 条) | **检索召回空洞 claim,漏实质** |

关键验证数据(`ans-77a58b1998` + 离线对比):
- **抽取不是瓶颈**:V3 论文的 FP8/MTP/DualPipe claim 库里都有(35/30/19 条)。
- **KG 原子 claim 检索召回空洞**:plan 完美(拆 3 子查询)、配额生效(`[4,4,4]`),但 top-12 全是 "DeepSeek-V3"、"advancements are effective" 这类高层空洞 claim,实质创新一条没进。
- **chunk > claim**:同一英文具体 query 下,原文 chunk 召回 **8/8 实质** vs KG claim **0**。
- **element 检索是 `limit=8` 的 fallback 降级层**(`_retrieve_elements`)——信息无损的原文被当降级,碎片空洞的 claim 当主力,本末倒置。
- **element 粒度太碎**:18624 个 element 中 **47% < 150 字**,含 2549 个纯标题(均 29 字)、24 个图片占位——直接做检索单元会被碎片稀释。
- **跨语言弱**:中文 query 对英文库召回 0 实质(关键词 bi-gram 对英文无效 + query 泛);英文具体 query 召回好。
- **query 质量决定召回**:点名创新词的 query 8/8,泛 query("key features")1/8。

**结论**:瓶颈是检索层——检索单元用 KG 原子 claim(碎、空洞、丢语境)+ 小召回(8/12)+ 原文降级 + query 不扩展 + 跨语言弱。NotebookLM 用原文 chunk(语境全、信息无损)+ 大召回 + query 理解 + 跨语言 + 长上下文综合,每一环都更强。

## 2. 已确认的关键决策

1. **KG 边界**:KG **退出通用问答检索**,保留两个角色——① 严格推理(graph 多跳推导链)② 两层知识库治理资产(晋升/审核/概念统一)。
2. **图谱可视化**:保留(/graph),KG 既为推理/治理保留,可视化顺带复用。
3. **路由**:用户**显式 mode**。默认 chunk-RAG;"严格推理"做成显式开关。**不做自动问题分类**(脆弱)。
4. **KG 抽取开关化**:KG 不再每次摄取同步跑。做成开关由用户选;用户切严格推理但该 notebook 无 KG → 提示"需先建 KG"。
5. **chunking 层**:检索单元不用原始碎 element,而是合并相邻 element 成 ~600 字 chunk(复用 `kg/windowing.py` 贪心打包,调小窗口)。
6. **embedding 配合**:**单一 chunk embedding 源**;KG 对象向量(含 concept)**派生自其 evidence chunk**,不独立 embed。

## 3. 架构总览

```
摄取(默认轻):  解析 → element(最小单元) → chunking(~600字) → chunk embedding
                                                                    │
用户开 KG(按需): element → KG 抽取 → knowledge_objects/relations    │
                              (evidence 指向 element)                │
                                                                    ▼
检索默认 = chunk-native RAG:  query改写 → 大召回 → MMR → 长上下文综合 → 答案(引用绑 chunk)
检索严格推理(显式) = KG:  chunk检索找种子chunk → (chunk→element→KG对象映射) → graph多跳遍历
治理/可视化 = KG:  晋升/审核/概念聚类(concept向量派生自chunk)/图谱
```

## 4. 详细设计

### 4.1 chunking 层(新,检索单元重组)

`backend/app/services/chunking.py`(新),复用 `kg/windowing.py` 的贪心打包:

- 输入:一个 source 的 `source_elements`(按文档顺序)。
- 合并:相邻 prose element 贪心打包到目标 ~600 字符(`CHUNK_TARGET_CHARS`,默认 600;对比 KG 窗口 4000-8000),相邻 chunk overlap ~100 字(`CHUNK_OVERLAP_CHARS`)。
- **heading**:不单独成 chunk,作为所属 chunk 的 `section_path` 前缀拼进文本(帮助语义)。
- **跳过**:image 占位、空 element。
- **超大 element**(>目标):按 step=target-overlap 内切(同 windowing)。
- 产出 `chunks` 表:`id, notebook_id, source_id, text, section_path, element_ids(JSON,该chunk含哪些element), char_start/end, created_at`。
- `element_ids` 是 chunk↔KG 衔接的关键(见 4.4)。

### 4.2 chunk embedding(单一源,替代 element embedding)

- 新表 `chunk_embeddings`:`chunk_id, notebook_id, vector, created_at`。
- 摄取时对 chunk 文本 embed(复用 `_embed_objects_batch` 的并发 + 429 退避,改 embed chunk)。
- **element_embeddings 废弃**(保留表兼容历史,新检索不用)。
- 复用 `vector_index`/`vector_cache`(版本键改 chunk 表)。

### 4.3 chunk-native 检索(默认路径,替换 fast 的 KG 检索)

五个组件,各治一个验证瓶颈:

1. **query 改写**(治"泛 query/跨语言"):答题前一次 LLM,把用户问题 → 1-3 个"检索优化查询"——具体化(泛→维度)+ 翻译到库主语言(中文 query → 英文/双语)。复用现有 `reasoning` 的 plan 思路但**不进 agentic 循环**,只为生成检索查询。无 LLM 时退化为原 query。
2. **大召回**(治"top-N 小"):对每个改写查询,chunk embedding 检索召回大池(`CHUNK_RECALL_POOL`,默认 150),关键词+语义融合(复用 `score_elements` 改 chunk)。多查询结果合并去重。
3. **MMR 多样性**(治"密集主题通吃"):大池上 MMR 选 M 条(`CHUNK_ANSWER_K`,默认 40),`score = λ·相关 − (1−λ)·与已选最大相似`,λ 默认 0.5(`MMR_LAMBDA`)。基于 chunk 向量余弦,纯向量计算无额外 LLM。
4. **长上下文综合**(治"top-12 截断"):M 条 chunk 原文一次喂 deepseek-v4(~128k 窗口),综合答案。`answer_context_budget_chars` 提高(默认 30000)。
5. **引用绑 chunk**:答案 `[k_i]` 绑 chunk(原文,直接可溯),`AnswerAnchor` 增 `chunk_id`,snippet 取 chunk 文本。

### 4.4 KG 收缩(保留推理/治理/可视化,退出检索)

- **KG 抽取开关化**:`notebooks` 增 `kg_enabled`(或 per-notebook 标记);摄取默认不抽 KG。用户显式触发"为此 notebook 建 KG"(后台 job,复用现 `_run_extraction`)。
- **严格推理 seed 走 chunk 映射**(关键衔接):`ask_graph` 不再用 KG 向量找种子,而是 chunk 检索找相关 chunk → `chunk.element_ids` → 反查绑这些 element 的 KG 对象(evidence) → 作为 graph 多跳种子。chunk 检索是统一入口。若 notebook 无 KG → 返回提示"需先建 KG"。
- **concept 向量派生**:`rebuild_unified_kg` 的概念聚类,concept 向量 = 其 mention element 所属 chunk 的向量(均值/首个),不独立 embed。
- **治理不变**:晋升/审核/duplicates/merge 仍操作 knowledge_objects(建了 KG 才有)。
- **可视化不变**:/graph 读 knowledge_objects/relations。

### 4.5 摄取分段(回应"上传即用")

- **默认轻摄取**:解析 → element → chunking → chunk embedding。上传完 chunk-RAG 立即可用(对标 NotebookLM)。
- **KG 按需**:用户开 KG → 后台 `_run_extraction`(element→KG)+ concept 向量派生。不阻塞上传。

### 4.6 路由

- `AskRequest.mode`:默认 `chunk`(新默认,chunk-native RAG);`reasoning`/`graph` = 严格推理(KG);保留 `global` 评估后定去留。
- UI:普通问答走默认;"严格推理"开关 → mode=graph。无自动分类。
- mode=graph 但 notebook 无 KG → 返回明确提示(不静默降级)。

## 5. 数据流

```
上传 → process_source: 解析 → element 存库 → chunking → chunk + chunk_embedding (轻,即用)
[可选] 用户开 KG → 后台: element → KG 抽取 → knowledge_objects/relations + concept向量(派生)

问答(默认 chunk):
  query → LLM改写(具体化+翻译, 1-3查询) → chunk 大召回(150) → MMR(选40) → 长上下文综合 → 答案+chunk引用

问答(严格推理, 显式):
  query → chunk检索 → chunk.element_ids → KG对象(evidence反查) → graph多跳 → 推导链答案
  (notebook 无 KG → 提示需先建)
```

## 6. 错误处理 / 降级

- query 改写 LLM 失败 → 用原 query 检索(不阻塞)。
- chunk embedding 缺失(embedder 未配) → 退化关键词-only(融合分自动归一,现有逻辑)。
- 严格推理 notebook 无 KG → 明确提示,不静默走 chunk(避免答非所问)。
- MMR 池为空 → 返回空候选,答题如实说无依据。

## 7. 测试策略(离线优先,沿用 rrepo fixture 清 LLM key)

- **chunking**:碎 element 合并到 ~600 字、heading 作 section、跳 image、overlap、超大切分、element_ids 正确。
- **chunk 检索**:大召回 limit 可配、关键词+语义融合、MMR 多样性(密集主题不通吃)、跨语言(中文 query 经改写召回英文 chunk)。
- **query 改写**:泛→具体、中→英、LLM 失败退化原 query。
- **chunk→KG 映射**:chunk.element_ids → KG 对象反查正确(严格推理种子)。
- **路由**:默认走 chunk;mode=graph 无 KG 提示;有 KG 走推理。
- **真机对照**:三个基准问题(综述/V3vsV2/具体)chunk-native vs 旧 KG,人工对照 NotebookLM。

## 8. 迁移

- 新增表:`chunks`、`chunk_embeddings`。`source_elements` 保留(chunk 的来源 + KG evidence 中介)。`element_embeddings` 废弃(保留表,不再写/读)。
- 现有 notebook:需重跑 chunking + chunk embedding(一次性,轻——只 embedding 无 LLM 抽取)。提供 `scripts/build_chunks.py`(类似 backfill)。
- `knowledge_embeddings`:KG 仍存在的 notebook 改为派生向量(或保留旧值,rebuild 时切派生)。

## 9. 明确不做(YAGNI)

- 不做自动问题分类路由(用户显式)。
- 不做 query 的 agentic 多轮(改写是一次性,reasoning 的 agentic 循环留给 graph 模式)。
- 不删 KG 抽取代码(开关化,推理/治理仍用)。
- 不引入新向量库(SQLite + numpy 矩阵,超大规模再上 sqlite-vec)。
- global 社区模式:本次不新建社区构建;chunk-native 的大召回+MMR 已覆盖综述,global 评估后定去留。

## 10. 实施 phase 划分(供 writing-plans)

可独立交付、逐步上线:
- **P1 chunking + chunk embedding**:chunking.py、chunks/chunk_embeddings 表、摄取接线、build_chunks 脚本。(基础设施)
- **P2 chunk-native 检索**:大召回 + MMR + 长上下文综合 + 引用绑 chunk,接入 mode=chunk 作默认。(核心,可对照验证)
- **P3 query 改写**:具体化 + 跨语言改写。(质量提升)
- **P4 KG 收缩**:抽取开关化、严格推理 seed 走 chunk 映射、concept 向量派生、摄取分段。
- **P5 路由 + 前端**:mode 默认 chunk、严格推理开关、无 KG 提示。

每 phase TDD + 离线测;P2 后即可真机对照 NotebookLM 看效果。

## 11. 验证基线

- `scripts/check.sh` 全绿(py_compile + hermetic smoke + tsc)。
- 三基准问题(综述/V3vsV2差别/具体)chunk-native 召回实质内容、覆盖多文档,对照 NotebookLM 接近。
- 生效需重启后端(逻辑改动)——交用户重启。
