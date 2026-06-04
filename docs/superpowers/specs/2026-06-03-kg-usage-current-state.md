# 知识图谱「使用侧」现状（供讨论）

- 日期：2026-06-03
- 目的：抽取/构建侧已收敛（见 `fangan_todo.md` 的「KG 性能」段），本文只梳理**怎么用这张图**的现状，作为讨论底稿，找问题。
- 范围：检索/问答 `/ask`、图谱视图 `/graph` 与 `/unified-kg`、节点详情 `/concepts/{id}/detail`·`/objects/{id}/context`。

## 0. 数据底座（回顾）
- `knowledge_objects`：4 类节点 `concept/claim/formula/procedure`，文本统一在 `payload.name`（concept=实体名、claim=完整断言、formula=表达式、procedure=过程名），`evidence` 是字符级证据（含 `element_id` 指向 `source_elements`）。**逐文档**入库（status=approved）。
- `knowledge_relations`：typed 边（defines/about/supports/part_of/…/precedes），`source_object_id → target_object_id`，逐文档。
- `concept_clusters`/`concept_merge_candidates`：**跨文档**把同义 concept 合并成簇（unified KG），非破坏性，confirm/reject 持久化。
- `knowledge_embeddings`（节点 payload 向量）+ `source_elements` 向量：检索语义信号来源。

## 1. 消费面（端点一览）
| 端点 | 方法 | 用途 | 用到图结构吗 |
|---|---|---|---|
| `/notebooks/{id}/ask` | `ask()` | **问答**（主消费路径） | 仅 1-hop 邻居扩展 |
| `/notebooks/{id}/graph` | `knowledge_graph()` | 原始逐文档图（节点=对象，边=relations） | 直接返回，前端展示 |
| `/notebooks/{id}/unified-kg` | `unified_graph()` | 跨文档 concept 级总览（可视化） | 用 clusters |
| `/concepts/{cid}/detail` | `concept_detail()` | 概念详情（成员/挂载/证据+element_text） | 用 clusters + 边 |
| `/objects/{oid}/context` | `node_context()` | 单节点上下文（所在句/定义/有序步骤） | 用 defines 边 + section 序 |
| `/notebooks/{id}/search` | `search_notebook()` | 关键词/语义搜索 | 否 |

## 2. `/ask` 的实际流程（主消费路径，逐步）
入参：`question`（+ 当前还有个 legacy `scenario` k→v 标签，**已决定移除**，见 §4-11）。当前 `query = question + scenario 值`；移除后 `query = question`。

1. **取候选**：`_knowledge_objects(db, nb, t)` 按 4 类各取**全部** `approved` 对象（注意：是**原始逐文档对象**，**不是** unified 簇）。同时 `_gather_elements` 取来源元素。

2. **向量化（语义信号怎么算）**：所有东西用**同一个 embedder**（text-embedding-v4 / bge）映到**同一向量空间**。涉及三种向量：
   - `query_vector = _embed_query(question)`；
   - `knowledge_vectors[obj_id]`：**节点自身文本**（payload，主要是 name）的向量，存 `knowledge_embeddings`（缺失时惰性回填）；
   - `element_vectors[elem_id]`：**原文句子/段落**的向量，存 `element_embeddings`。节点经其 **evidence 的 `element_id`** 关联到这些元素。
   - 单个对象的语义分 = **两条路取最大**：
     `semantic(obj) = max( cosine(q, knowledge_vectors[obj]), max_e cosine(q, element_vectors[obj.evidence[e].element_id]) )`
   - 为什么两条路：concept 节点常只有裸名（如 "MoE"），向量很薄；它**所在的原文句子**信息量大得多，所以也拿 query 去比证据元素的向量，谁高用谁。无 embedder → `query_vector=None` → `semantic=0`，退化为纯关键词。

3. **逐类型打分**（对**每个类型单独**跑 `score_knowledge`，再各取 top-K）：
   ```
   for t in (claim, formula, procedure, concept):
       scored = score_knowledge(query, 该类型全部对象, t, ...)   # 按 score 降序
       top_hits += scored[: TOP_PER_TYPE[t]]
   ```
   `score_knowledge` 对每个对象：
   - `keyword = keyword_score(query, name+证据文本)` = query token（CJK 分词）命中比例，0..1。
   - `semantic` = §2.2 的两路 max-cosine，0..1。
   - `relevance = _fuse(keyword, semantic)` = 有向量时 `(0.4·kw + 0.6·sem)/(0.4+0.6)`，无向量时就是 `kw`（按激活信号归一化，避免纯关键词被压到 0.4 上限）。
   - `relevance < RELEVANCE_FLOOR(0.12)` → 丢弃。
   - `score = relevance × (1 + 0.5 × structured_boost(scenario))`（scenario 软加权，**随 scenario 一起移除**）。
   - 最终 `scored.sort(key=score, desc)`。**注意：`_TYPE_WEIGHT`(claim1.0/formula1.0/procedure0.7/concept0.5) 没参与这个排序**，且选取是「逐类型 top-K」→ 同类型内权重恒定，**实际不影响任何选择**（见 §4-12）。

4. **每类型截断 top-K**：`_TOP_PER_TYPE = {claim:5, formula:5, procedure:4, concept:4}` → `top_hits`（≤18）。**固定配比**：无论问题是什么，都按这个比例凑（见 §4-11）。
5. **1-hop 扩展**：对 `top_hits` 的 id，扫 `knowledge_relations`，把**任一端命中**的边的另一端加入 `neighbour_ids`（不分边类型、不分方向、不打分）。取这些邻居对象。
6. **组装 `related_knowledge`**：hits 优先 + 邻居，去重，截断 12。
7. **引用**：从 `top_hits` 的 evidence 元素生成 `citations`（element 级）。
8. **合成结论**（`_answer_with_llm_kg`）：把 hits 拼成纯文本块——`- [type] name — k:v; k:v`（payload 里非 name 的字段），再加**最多 8 条来源元素** `text[:300]`，连同 question/scenario 丢给 LLM（`answer_prompt` + `ANSWER_SCHEMA_HINT`）→ 返回**单条 `conclusion` 字符串**。无 LLM 时退化为「找到 N 条」式确定性文案。
9. 存 answer，返回 `{conclusion, related_knowledge(≤12), citations, llm_mode}`。

## 3. 图谱视图
- `/graph`：节点=所有非 deprecated 的 `knowledge_objects`（headline=name[:120]），边=该 notebook 全部 `knowledge_relations`。**原始、逐文档、未合并、未按相关度筛**——整图直出，前端画。
- `/unified-kg` + `/concepts/{id}/detail`：concept 级跨文档总览（用 clusters），是「知识图谱」可视化视图的数据源；右栏详情用 `node_context`/`concept_detail` 呈现完整句子+定义+有序步骤。
- 注意：**可视化用 unified；问答 `/ask` 用原始对象**——两条路径对「同一概念跨文档」的处理不一致。

## 4. 待讨论的问题点（现状的潜在缺陷）
> 下面是我读代码后认为值得一起 review 的点，按「图用得够不够」「检索质量」「合成质量」分组。

### A. 图结构几乎没被用起来
1. **`/ask` 只做 1-hop 加节点，不做图推理**：边只用于「把邻居塞进上下文」，**relation 的类型/方向/多跳/路径全部没用**。`supports / contrasts_with / depends_on / precedes` 在问答里和「随便一条边」无差别。本质上现在是「typed 检索 + 一圈邻居」，不是「图谱遍历/推理」。
2. **LLM 看不到边**：合成时只给节点列表 + 原文片段，**没有把 relations 作为结构喂给模型**，所以模型无法基于「X supports Y，Y contrasts Z」这种结构回答。
3. **无路径/解释能力**：回答不了「X 和 Y 怎么关联」「支撑/反驳链是什么」这类需要沿边走的问题。

### B. 检索质量
4. **问答用原始对象、不用 unified 簇**：跨文档同义 concept 会以多个对象**各自争 top-K**，既稀释又重复；我们做的跨文档合并**在问答里没被使用**。
5. **固定每类型 top-K（5/5/4/4）**：与 query 无关。某问题可能需要关于一个概念的 10 条 claim，却被砍到 5；concept/procedure 顶到 4。
6. **检索入口是「对象文本相似度」，不是「概念锚定再遍历」**：问句点名某 concept 时，并没有先定位该 concept 节点、再沿边取它的定义/断言/公式/流程，而是对所有对象做关键词/向量打分。
7. **concept 节点只有 `name`**：payload 无 definition，向量也只基于 name → 裸概念名的关键词/语义匹配较弱（语义只能靠 name 向量或证据 element 向量兜）。
8. **1-hop 扩展不打分、不限类型/方向、不重排**：邻居无条件追加，可能把弱相关节点塞进上下文挤占预算。

### C. 合成 / 证据
9. **富信息没进答案**：`node_context`（所在句子 / concept 定义 / procedure 有序步骤）**只在可视化用**，`/ask` 的合成只拿到 name+payload+8 条原始元素——procedure 的步骤、concept 的定义**没喂给答题 LLM**。
10. **结论是单串、引用是 element 级**：没有「每个论断绑定到具体证据」的逐句 citation。

### D. 简化 / 误导项（已确认）
11. **scenario 入参移除**（用户确认）：`scenario`(k→v) 是旧规则治理产品遗留（字段 domain/block_type/package_type/…）。当前被拼进 query + 驱动 `structured_boost`。KG 问答**直接 `query = question`**，删 scenario 参数与 structured_boost。
12. **`_TYPE_WEIGHT` 当前无效**：排序只按 `score`，且选取是逐类型 top-K → 同类型内权重恒定，**不影响任何选择**。要么真正用起来（改全局统一排序，把 type 当软先验），要么删掉以免误导。

## 5. 一句话总结（待你确认）
现在的「使用」= **混合检索（关键词+向量，逐类型 top-K）+ 一圈无类型邻居 + 把名字丢给 LLM 写一段话**。图谱的**边语义、跨文档合并、节点富信息**这三块在问答路径上基本闲置；可视化路径用了合并和富信息，但和问答各走各的。讨论方向：要不要把 `/ask` 改成「概念锚定→按边类型做有意义的遍历→带富上下文+结构合成→逐句引用」，以及问答是否切到 unified 簇。

## 6. 复杂问题为何回「无相关知识」（实测根因）
问 "engram是什么，它有什么缺点，有什么改进建议" → 返回 canned「The notebook does not yet contain approved knowledge…」。**不是知识缺失（engram 就在库里），是检索把全部命中都拒了 + 无命中时是死路**：
- **A1 embedder 未配置**：`.env` 无 `EMBED_*` → `embedder_configured=False` → `query_vector=None` → 所有 `semantic=0`。**线上检索目前是纯关键词**（向量机制全程休眠）。
- **A2 关键词指标惩罚长/多部分/跨语种问题**：`keyword = |query∩text| / |query|`，分母是**整条问题**。该问句 CJK 分词 ~10–14 token，`Engram` 节点文本只含 "engram" 一个 → `1/12≈0.08 < RELEVANCE_FLOOR(0.12)` → **连 Engram 节点本身都被丢**。问题越长分越低（与意图相关性无关），中文 token 永不匹配英文 KG → 全部 < floor → `top_hits` 与 `scored_elements` 皆空。
- **B 无命中即死路（设计如此）**：LLM 被 `if configured and (has_knowledge or scored_elements)` 门控，两者皆空 → **模型根本没被调用** → 返回 canned 串。且 `answer_prompt` 明确「ONLY retrieved knowledge / if insufficient state that」→ 天生只会拒答，不会用模型自有知识兜底。

## 7. 期望的使用形态（产品需求，讨论中）
- **推演能力（核心）**：场景 = 引入文档后问「接下来能做什么 / 怎么改进」。答案**不能只复述原文**，要在 grounded 基础上**向前推理**（next steps / 改进 / 蕴含）。即 **RAG + reasoning**，不是 RAG-only。前轮例子里「改进建议」正是推演。
- **逐句 provenance（让推演安全的机制，= 前端需求1）**：同一条答案里 grounded 与 inferred 混排——
  - grounded 句尾带短 `[anchor]`（KG 名 / 原文短 token，可点→弹完整证据）；
  - inferred / 推演句**不带 anchor**（一眼可见是模型推理）。
  - 这样把上一版的「grounded / partial / ungrounded 三态」收敛为**同一答案内逐句标注的连续谱**：有出处就标、没出处就坦白是模型理解。
- **无命中兜底**：检索空/低置信时，仍用模型自有知识回答并**明确标注「非笔记本来源」**，不再 canned 拒答。
- 实现范式：复用「回传 id 标记」——给答题 LLM 每个检索项一个稳定短 id，指示它 grounding 处产出 `[id]`、推演处不带 id；后端把 `[id]` 解析成弹窗 payload。

## 8. 前端 / 配置需求（用户提出，待排期）
1. **内联引用**：答案正文用短 `[anchor]`（少量字便于显示），鼠标点击弹出完整信息（证据原文 / 节点详情）。机制见 §7。
2. **多轮对话**：中间问答区改成多轮对话框，持久化每轮「用户问 + 模型/系统答」，发送后以聊天线程呈现。**现状是无状态单轮**（`_save_answer` 存单条、无 thread；`AskRequest` 无 history）→ 需会话/线程模型 + 历史喂进 prompt + 前端聊天 UI；追问需带历史 + 上轮检索锚点。
3. **embedding 独立配置**：**配置项已存在**——`EMBED_PROVIDER / EMBED_BASE_URL / EMBED_API_KEY / EMBED_MODEL / EMBED_DIM`，与 chat 的 `OPENAI_COMPAT_*` 完全独立。当前 `.env` 未设值 → embedder 关闭（§6-A1）。设上（如 `EMBED_PROVIDER=dashscope` + v4 端点）即启用语义+跨语种检索，是上面一切的前置。
