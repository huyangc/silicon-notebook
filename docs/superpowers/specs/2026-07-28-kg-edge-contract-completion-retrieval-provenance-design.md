# KG 边契约、受约束关系补全与检索来源配额设计

- 日期：2026-07-28
- 状态：实现完成（P0/P1 代码与回归已落地；生产 shadow/write 灰度、A/B 与指标验收待执行）
- 分支：`codex/kg-quality-p0-p1-plan`
- Worktree：`.worktrees/kg-quality-p0-p1-plan`
- 基线：`origin/master` / `68ba7687025ba299f52d98d655b1e7b605ce3b4f`
- 优先级：P0 + P1
- 参考：`../ref-kg/llm_wiki` 的两类安全模式——“模型只提交受约束增量”和“最终结果给图候选保留可回收配额”。本文只吸收方法，不复制其 Markdown Wiki、无类型 wikilink、固定权重或全图扫描实现。

## 1. 结论与范围

本轮实施三项改进：

1. **P0：统一 KG 边类型契约。** 抽取提示、落库前校验、edge trust、图推理和 `follow_chain` 不再各自维护边集合及端点约束。
2. **P1：增加受约束关系补全。** 针对窗口级抽取遗漏的跨窗口关系，模型只能从后端给定的有限节点/证据候选中返回关系提案；后端完成类型、端点、证据、重复和范围验证后才能进入现有边评审体系。
3. **P1：保留检索来源并增加图候选自适应配额。** 三路 mix 经过统一 rerank 后仍保留向量、词法、KG-source、PPR、relation 等来源；相关的 graph-only 候选可以获得小型、可回收、受 token 预算约束的保留空间。

**明确排除：**本设计不增加文档级/全书级抽取计划，不生成全书摘要 contract，不把整本书交给一次模型调用，也不依赖这一能力作为关系补全的前置条件。

关系补全必须独立适用于短文、论文、一本书和多来源 notebook。面对书籍时，它只处理有界节点与证据候选，不能全书节点两两比较。

### 1.1 当前实现状态

P0 和本文列出的 P1 实现已完成。两个改变候选或最终选择的开关仍保持安全默认：关系补全为 `off`，图候选 reserve 为 `0`。关系补全使用按 `mode + source_generation + object_id` 的持久化 keyset 水位，在多次有界任务中逐页推进；达到 page/batch 上限时重新入队，进程启动时会恢复当前 pending 代次。两个启用模式互切时，generation-CAS 事务先发布新模式可恢复游标，再把旧 pending 游标标为 `stale`；切到 `off` 不排替代任务。每页只执行候选 ID 范围内、符合统一边契约的 indexed relation `EXISTS`、同源 FTS/ANN 有界 overfetch，并仅 hydrate 当前有界对象所引用的受限 evidence IDs（SQLite 每条语句最多 900 个 ID）。它不读取 notebook 全表，也不把一次运行包装成“全书补全”；只有 cursor exhaustion 才会把当前代次标记完成。

核心四类端点使用严格契约；查询侧对 core→core 非法 pair fail-closed，但已知边连接管理员定义扩展类型时继续可查。这一区分避免把未来 Schema 类型误当作历史脏数据；只读审计仍把它们单独列为 extension/unknown，而不会伪装成 core 合法 pair。

## 2. 当前问题

### 2.1 边类型契约已经发生漂移

`backend/app/services/kg/extract.py` 的提示允许：

- `supports`：Claim / Formula / Concept → Claim；
- `derived_from`：Claim / Formula → Claim / Formula；
- `contrasts_with`：Claim / Formula / Concept 之间；
- `used_in`：Formula / Concept → Procedure。

但 `backend/app/services/kg/edge_trust.py` 维护的是更窄的镜像表，例如不接受 Concept→Claim 的 `supports`、Claim 参与的 `derived_from`、Claim/Formula 的 `contrasts_with` 和 Concept→Procedure 的 `used_in`。与此同时，`extract_window()` 只检查边名称、端点存在和非自环，没有统一检查端点类型。

结果是：抽取提示要求模型生成的关系可能在 trust 计算时被判成类型无效，从而错误降低 `trust_score`、提高 review priority。`follow_chain.py` 和 `graph_reason.py` 又分别维护可组合关系、节点类型和 reasoning edge 集合，未来还会继续漂移。

### 2.2 窗口抽取遗漏的关系没有通用恢复路径

当前 `extract_graph()` 并行抽取各窗口；首轮边只能连接同一个窗口内的 `local_id`。`_glean_nodes()` 明确只补节点、不补边。现有 `complete_isolated_edges()` 是安全的确定性兜底，但只在同源内依据共享 element 和名称命中补 `about/used_in`。

因此以下关系容易丢失：

- 一章前面给出前提、后面给出结论的 `supports/derived_from`；
- 不同小节分别描述条件和结论的 `depends_on`；
- 分散在不同窗口中的比较和限制 `contrasts_with`；
- gleaning 新增节点与既有节点之间的高价值推理边。

不能通过增大窗口或全节点两两送模解决：一本书可能产生大量节点，全量比较会放大上下文、成本、延迟和幻觉风险。

### 2.3 mix 的来源均衡可能被最终 rerank 抹掉

当前 `_mix_retrieve()` 对 vector chunks、KG-source chunks、PPR chunks 做 round-robin 合并；`ask_service.py` 随后把全部候选统一 rerank，再按 token 预算从头截断。

候选没有完整保留其检索来源，去重时也不会合并来源信息。一个只被 PPR 或 relation 路径发现、但表面语义不够强的相关 chunk，可能在最终截断时全部退出。前面的三路均衡因此不构成最终选择保证。

## 3. 全局不变量

三项工作都必须遵守以下约束：

1. **证据优先。** 模型输出的 quote 不是权威；证据文本必须由后端根据合法 `SourceElement` ID 重新 hydrate。
2. **候选有界。** 大 notebook/书籍不得全表扫描节点、关系、文本或向量；所有 ANN/FTS 后的数据库 hydration 都必须受候选窗口约束。
3. **模型只做判别和提案。** 模型不能修改节点文本、创建自由节点、改写来源、发明端点或提交候选集外 ID。
4. **评分域分离。** rerank 分、来源配额、graph support 和 trust 只驱动排序/选择；不能写入现有 relevance、`_fuse`、tau 或 grounded 判定。
5. **引用不变。** 最终保留的 chunk 仍用真实 `element_ids` 生成引用；来源配额不能产生无引用内容。
6. **拒绝边继续排除。** `review_status='rejected'` 的关系不能进入 reasoning、PPR、relation retrieval 或补全候选。
7. **不持久化推导链。** `follow_chain` 的查询期组合仍然只在查询期存在；关系补全只写入原文明确支持的直接边。
8. **SQLite/PostgreSQL 同步。** 新增 repository 查询或写入必须同时实现两个后端，并保持结果和边界一致。
9. **关闭开关时等价。** 新 P1 能力关闭时，现有抽取、检索顺序、引用和输出必须保持特征测试定义的等价性。
10. **不针对单一报告案例。** gold/反例至少覆盖教材、论文、技术手册和一般说明文，不使用某一份深度报告作为唯一验收集。

## 4. P0：统一边类型契约

### 4.1 单一注册表

新增 `backend/app/services/kg/edge_schema.py`，定义唯一 `EDGE_SPECS`。建议的数据结构：

```python
@dataclass(frozen=True)
class EdgeSpec:
    edge_type: str
    allowed_pairs: frozenset[tuple[str, str]]
    category: Literal["structural", "reasoning"]
    symmetric: bool = False
    transitive: bool = False
    default_reasoning_traversal: bool = False
    prompt_description: str = ""
```

注册表统一使用数据库的小写节点类型；边界函数接受抽取模型的 Title Case，先规范为小写。注册表至少提供：

- `VALID_EDGE_TYPES`；
- `is_valid_edge_pair(edge_type, source_type, target_type)`；
- `canonical_edge_key(...)`，对称边用无向端点 key 去重；
- `render_edge_prompt_rules()`；
- `REASONING_EDGE_TYPES`；
- `TRANSITIVE_EDGE_TYPES` 或供 `follow_chain` 构造 composition 白名单的元数据。

首版端点矩阵以当前抽取提示表达的产品语义为起点，而不是以已经漂移的 `edge_trust.TYPE_CONSTRAINTS` 为准。本文直接冻结完整有向 pair；实现者不得再解释提示里的 `...`：

| 边 | 初始端点语义 |
|---|---|
| `defines` | Claim → Concept |
| `about` | Claim / Formula → Concept |
| `supports` | Claim / Formula / Concept → Claim |
| `derived_from` | Claim / Formula → Claim / Formula |
| `depends_on` | Concept / Claim / Formula → Concept / Claim / Formula / Procedure（12 个有向 pair） |
| `contrasts_with` | Concept / Claim / Formula 的完整 3×3 pair，对称语义；持久化 key 对端点排序 |
| `prerequisite_of` | Concept / Claim → Concept / Claim |
| `part_of` / `composed_of` / `kind_of` | Concept → Concept |
| `used_in` | Formula / Concept → Procedure |
| `precedes` | Claim→Claim、Formula→Formula、Procedure→Procedure；禁止 Concept 和所有跨类型组合，保持当前 `follow_chain.EDGE_NODE_TYPES` 的类型集合 |

`depends_on` 的 Procedure 只允许作为 target，沿用当前 prompt 的源端约束。`contrasts_with` 的跨类型 pair 是明确产品语义，不由“之间”临时推断。

注册表的三个维度互相独立：

- `allowed_pairs` 只控制抽取、历史边合法性和 completion；
- `default_reasoning_traversal` 首版严格只对 `derived_from/supports/depends_on` 为 true，保持当前 `DEFAULT_REASONING_EDGES`；
- `TRANSITIVE_COMPOSITIONS` 首版仍严格只有 `derived_from/kind_of/prerequisite_of/precedes/part_of` 的同类型二跳组合。

`transitive=True` 不允许自动推导混合类型可组合。`follow_chain` 对每一跳分别验证 pair，并验证中间节点使两个 pair 连续合法；尤其 `precedes` 的跨类型链必须拒绝。

### 4.2 消费者改造

1. `extract.py`
   - `EDGE_TYPES` 和提示中的 edge block 由注册表生成；
   - 构造边前查询 `by_local` 对应节点类型；
   - 丢弃非法端点组合并记录按 edge type/reason 聚合的观测事件；
   - 不因单条非法边丢弃整个窗口。
2. `edge_trust.py`
   - 删除本地 `TYPE_CONSTRAINTS`、`VALID_EDGE_TYPES`；
   - `type_validity_score()` 委托注册表；
   - 合法的 prompt 边不得再获得 0 类型分。
3. `graph_reason.py`
   - `DEFAULT_REASONING_EDGES` 从注册表的 `default_reasoning_traversal` 派生，保留当前产品允许的精确子集；禁止因为新增一个 reasoning category 边就自动扩大图遍历。
4. `follow_chain.py`
   - composition 表继续是显式白名单；模块加载或测试时验证所有 composition 输入/输出都存在于注册表且端点类型兼容；
   - 删除与注册表重复的 `EDGE_NODE_TYPES`，或改为由注册表派生。
5. 关系补全、治理和后续 UI 标签均只能消费注册表，不得重新硬编码当前关系名。

### 4.3 已有数据处理

本项不重写、删除或自动 reject 已有关系，但**历史非法 pair 默认不得继续参与检索或推理**。上线前提供只读审计：

- 按 edge type 和端点类型统计现有合法/非法数量；
- 区分 LLM 抽取边、deterministic relink 边和人工 verified 边；
- 输出样本 ID，不输出来源全文；
- 对真正不符合新契约的历史边保留原始行并进入现有 review queue，不自动销毁；只有后续产品明确增加的 verified override 才可例外放行，首版没有 override。

运行时所有读取边的入口都执行同一契约过滤，至少覆盖：

- in-memory/federated rustworkx graph；
- scale-index/PPR 构建输入；
- relation retrieval hydration；
- `follow_chain`；
- edge support/corroboration 计算。

P0 修改图语义后必须提升相关 scale-index artifact schema/semantic version，使旧 generation 不可复用；启动 preload/readiness 必须重建或拒绝包含历史非法 pair 的 artifact，不能只清内存缓存后继续加载旧图。

`trust_score` 是读取时计算，因此注册表修正会改变 review queue 排序；必须用固定 fixture 记录修正前后的预期变化。

### 4.4 P0 测试与验收

- 注册表覆盖全部 12 种现有边，边名无增删。
- `render_edge_prompt_rules()` 中的每个允许端点对，`type_validity_score()` 都返回 1。
- 每个禁止端点对在 `extract_window()` 中被丢弃，并产生可观测 rejection reason。
- Title Case / lower-case 规范化结果一致。
- 对称边反向得到同一 canonical key；非对称边保持方向。
- `follow_chain` 的五种当前可传递边行为不变。
- 历史非法 pending/verified 边不进入 rustworkx、relation retrieval、scale PPR 或 `follow_chain`。
- 旧 artifact generation 因 semantic version 不匹配而不能被 readiness 接纳。
- 现有 edge review API 和 UI 数据结构不变。
- 增加契约扫描测试，禁止 `extract.py`、`edge_trust.py`、`graph_reason.py` 再声明第二份完整边集合。

## 5. P1：受约束关系补全

### 5.1 定位

关系补全是现有窗口抽取之后的**有界后处理**，不是第二套抽取器，也不是文档级规划器。它只尝试为已经存在、已经 grounded 的节点恢复直接关系。

```text
窗口级抽取
  → deterministic relink（保持现状）
  → store grounded objects/relations
  → 有界候选生成
  → LLM 关系提案
  → 独立证据校验
  → 后端契约/范围/重复校验
  → shadow 或写入 pending edge
```

先执行 deterministic relink，因为它廉价且高精度；关系补全不得重复提出已有 `about/used_in`。

### 5.2 候选生成

新增纯候选模块 `backend/app/services/kg/relation_completion.py`。输入是已落库对象、关系和 SourceElement 的有界投影；输出 `RelationCandidate`，不写数据库、不调用模型。候选批次必须绑定当前来源最近一次成功 `extraction_runs.id`，该 run ID 即本轮 `source_generation`。

优先 anchor：

- degree 0 或低度节点；
- gleaning 后仍没有推理边的节点；若当前数据没有可靠 origin 标记，首版不为此新增猜测字段，只使用度数；
- 只有弱结构边、没有 reasoning edge 的 Claim / Formula；
- evidence 分布在不同窗口/小节但语义相近的节点。

候选来源按精度从高到低：

1. 同源显式 element/section 邻近与关系触发词；
2. 同源节点名称在对方证据附近的词法命中；
3. 同源 ANN top-K 语义近邻；
4. 同一 canonical cluster 的跨来源对象首版只生成 **shadow 候选**，不得写入跨来源事实边；若未来要人工确认，需另行设计候选存储，不能把未验证提案伪装成事实边塞进现有 edge review queue。

每对候选必须先通过：

- `EDGE_SPECS` 至少存在一种合法端点关系；
- 非自环；
- 不存在相同或对称等价关系；
- 两端均属于当前 notebook，且满足当前阶段的 source 范围；
- 两端状态均为 USABLE；
- 任一现有关系为 rejected 时，不把 rejected 边当作正证据或连接种子。

### 5.3 大书/大库成本护栏

首版不增加未索引的 `section_path` 全表分组。采用**有界 keyset page + 持久化水位**：

1. repository 使用现有 `knowledge_objects.source_id` 索引，并补充 `(source_id, id)` 复合索引；按 `id > cursor ORDER BY id LIMIT page_size` 读取一页 anchor 投影；
2. 每页只对该页 candidate IDs 做 indexed relation `EXISTS`，判断 degree 0/低度/无 reasoning edge；绝不 materialize 完整 adjacency；
3. section spread 只在当前有界页中基于 payload `section_path` 做 round-robin，不为了全书分散读取整源；
4. completion run 持久化 `source_generation + next_object_id` 水位，多轮/重试继续后续页面；到 cursor exhaustion 才声明本 generation 覆盖完成；
5. 同源 ANN/FTS 先走 notebook 级索引，但采用有上限的 overfetch，再通过 indexed `knowledge_objects.source_id` 过滤；过滤后不足不会全量补扫。若实测大书 source 候选长期被挤出，再单独增加 source-aware ANN/FTS artifact，不在 Python 全量回退；
6. 无 ANN/FTS、generation 改变或候选过滤为空时 fail closed，并记录 explicit skip reason。

completion 水位需要一个 SQLite/PostgreSQL 对等的小型状态表，例如：

```text
kg_relation_completion_state(
  notebook_id, source_id, source_generation,
  mode, next_object_id, status, schema_version,
  updated_at,
  PRIMARY KEY(source_id, source_generation, mode)
)
```

旧 generation 的水位由 reparse/delete 清理或标 stale；不能被新 generation 继承。

配置采用明确上限，不允许用“尽量处理”表达：

- 每次来源补全的 anchor 数上限；
- 每个 anchor 的 ANN/FTS top-K；
- 每个小节/章节的候选配额，按 section spread 取样，避免一本书只覆盖开头；
- 每次来源总 candidate pair 上限；
- 每个模型 batch 的 pair 数和字符上限；
- 每次来源最大模型 batch 数；
- repository 只 hydrate 候选 object IDs、relation IDs 和 element IDs；
- 大库缺少可用 ANN/FTS 索引时 fail closed：跳过 relation completion，绝不退回全量暴力。

所有上限进入 `Settings`，使用 `validation_alias`；默认模式为 `off`。建议总开关：

```text
KG_RELATION_COMPLETION_MODE=off|shadow|write
KG_RELATION_COMPLETION_NOTEBOOK_ALLOWLIST=
KG_RELATION_COMPLETION_ROLLOUT_PERCENT=0
```

- `off`：不生成候选、不调用模型，现状等价；
- `shadow`：生成、调用、验证并记录聚合结果，但不写关系；
- `write`：只有通过全部验证的关系以 `review_status='pending'` 写入。

有效模式为全局 mode 与 notebook 门控的交集：allowlist 命中优先；否则使用 notebook ID 的稳定 hash 与 rollout percent。默认 percent=0。隐藏 `memory/knowhow` projection source 首版排除，只有用户可见导入来源参与。

首版配置为每次最多 4 页、160 anchors、120 pairs、24 pairs/batch、4 batches，每 section 最多 24 pairs，每 anchor 取 8 个近邻，候选 overfetch 为 64，单 batch 最多 48,000 字符。这些是可配置的硬上限，不是覆盖或质量承诺；默认 `off` 意味着未经 shadow 评测前不会产生模型调用或新边。

### 5.4 模型输出契约

模型只看到后端分配的短 ID、节点类型/名称和允许的 evidence excerpt。输出必须是：

```json
{
  "relations": [
    {
      "candidate_id": "c17",
      "source_object_id": "ko-1",
      "target_object_id": "ko-2",
      "edge_type": "depends_on",
      "evidence_element_ids": ["el-8"],
      "confidence": 0.86
    }
  ]
}
```

模型不能返回 quote、节点正文修改、候选外 ID、新节点或自由 edge type。后端校验顺序固定：

1. JSON/schema 合法；
2. `candidate_id` 和端点与发给模型的候选完全一致；
3. edge type 在该候选允许集合中；
4. 端点类型通过 `EDGE_SPECS`；
5. element IDs 属于发给该候选的 allowed evidence set；
6. element、object、source、notebook 的所有权一致；
7. 证据中必须存在明确表达两端关系的 excerpt；仅分别证明两个端点存在，不足以证明它们之间的边；
8. 无自环、无现有同型边、无对称重复；
9. 服务端重新 hydrate quote/location，忽略模型可能夹带的文本；
10. 检查候选绑定的 `source_generation` 仍是当前最近成功 extraction run；
11. 在最终写事务内重新读取两个对象和所有 element，确认它们仍存在、USABLE、归属同一 notebook/source/generation；
12. 生成 completion 专属稳定 relation ID，再执行写入。

关系提案使用现有 `kg_extract` 模型通道；独立校验使用现有 `kg_refine` 通道，避免首版新增模型 workload 和对应管理 UI。`write` 模式要求 verifier 可用且返回肯定结果；verifier 缺失、超时或解析失败时 fail closed，不写边。补全失败不得回滚已经成功的主抽取结果。

### 5.5 持久化和幂等性

通过验证的关系复用 `knowledge_relations`：

- `review_status='pending'`；
- evidence 增加机器可读 `basis='completion:bounded-llm'`、真实 element/source/location 信息和 verifier 版本；
- `source_id` 只在单来源直接边时填写；跨来源 shadow 候选不写入；
- 写入前使用 indexed existence check 排除已存在的等价事实边；对称关系用 canonical endpoint order；
- completion relation ID 使用 `hash(notebook_id, source_generation, canonical endpoints, edge_type, completion_schema_version)` 的稳定值，SQLite/PostgreSQL 都以主键 `INSERT ... ON CONFLICT DO NOTHING` 实现并发幂等；
- 首版不增加全表 triple unique constraint：它可能压掉多证据/corroboration，并可能因历史重复导致迁移失败；若未来统一事实边，必须单独设计 evidence 合并和 rejected 策略；
- 一批 validated relations 在一个短事务内写入，事务外完成模型调用和 embedding；
- 最终短事务内必须重复完成 generation/ownership/existence 校验，防止模型调用期间发生 reparse/delete；任一变化都丢弃该提案，不使用旧 excerpt 重新解释；
- 写入后沿用现有 mutation sequence、unified KG dirty、relation embedding 和缓存失效流程。

completion 挂载在主 KG 对象/边落库和增量融合成功之后、自动索引调度之前，作为可取消的 best-effort 阶段。它失败不能把已经成功的主抽取标为 failed。reparse/delete 必须取消在途 completion，并让 generation CAS 阻止迟到写入。

如果需要新增 repository port，应放在 knowledge/governance store，而不是把 SQL 塞回 `repository_facade.py`。同时更新 ownership manifest 和 dependency contract。

### 5.6 与评审和检索的关系

补全边不会绕过现有 edge review：

- 初始状态 pending；
- edge trust 使用统一端点契约、真实证据和 corroboration 计算；
- rejected 后从图推理和检索排除；
- UI 继续通过现有 edge-review queue 确认/拒绝，不新增第二个队列；
- review queue 如需展示 `basis`，必须在同一变更中补齐 backend 字段和 frontend 展示，遵守 full-stack parity。

首版不自动把补全边标为 verified，也不因为模型 confidence 高就跳过人工治理。

### 5.7 P1 补全测试与验收

测试集必须包含：

- 同一来源两个窗口之间的 `supports/derived_from/depends_on/contrasts_with` 正例；
- 两个节点分别有证据、但原文没有表达关系的负例；
- 同关键词、不同产品/方法主体，禁止串边；
- 同一书中相距较远章节但无显式关联，禁止仅凭相似度写边；
- 跨来源候选只能 shadow，不能首版直接写边；
- 模型发明 ID、edge type、element ID、quote 的攻击性输出；
- 对称边反向重复、重复运行、并发重试的幂等性；
- 模型取得候选后发生 reparse/delete 的 generation race，迟到提案必须写入 0 条；
- verifier 失败、无 ANN、候选超限和取消；
- SQLite/PostgreSQL 查询和写入等价；
- 一本书规模 fixture 证明 candidate/hydration/model batch 均不越上限。
- keyset page 逐页推进且不跳页、不回读整源；generation 改变后旧 cursor 不复用。

启用 `write` 前必须满足：

- 所有落库边端点/类型/证据通过服务端验证；
- cross-window edge recall 相对 baseline 提升；
- edge precision、citation grounding 不低于事先记录的 baseline 容差；
- 不增加全表扫描；
- shadow 统计显示候选量、模型调用量和延迟在配置上限内；
- 关闭模式的抽取 characterization tests 完全一致。

## 6. P1：检索来源保留与图候选自适应配额

### 6.1 来源数据模型

不能只给 `RetrievedChunk` 增加几个集合字段：当前 ANN/FTS 在构造 chunk 前已经 union，KG overlay 和 graph renderer 也会丢失 seed relation/traversed edge 身份。首版先定义结构化支持记录，并从 producer 开始贯穿：

```python
@dataclass(frozen=True)
class RetrievalSupport:
    origin: Literal["semantic", "lexical", "kg_source", "ppr", "relation"]
    support_kind: Literal["chunk", "object", "relation", "ppr"]
    support_id: str = ""
    score: float | None = None
    review_status_snapshot: str = ""

@dataclass
class RetrievedChunk:
    # ... existing fields unchanged ...
    retrieval_supports: tuple[RetrievalSupport, ...] = ()
```

稳定来源 ID 首版固定为：

- `semantic`：chunk ANN/向量；
- `lexical`：chunk FTS；
- `kg_source`：KG object evidence 反查 chunk；
- `ppr`：PPR/概念漫游；
- `relation`：relation seed/evidence 路径。

这些 ID 是内部协议，不使用中文显示名，不写入用户会话。support score 只保存各路径自己的排序/支持分，不替换 `.score`/`.relevance`。PPR 可能来自 relation、membership、synonym 或 co-mention；没有具体事实 relation 时使用 `support_kind='ppr'` 和空 `support_id`，绝不伪造 relation ID。

producer 必须在 union 前保留信息：

- chunk ANN labels 形成 semantic membership set；FTS hits 形成独立 lexical membership set，二者 union 后再构造 support；
- `_chunk_kg_overlay()` 返回 seed node、seed relation 和遍历 edge 的 support map，不只返回渲染后的 `id_map`；
- graph renderer/overlay 需要把真实 relation ID 和当次 `review_status` 快照传到 chunk 映射；
- `_ppr_retrieve()` 至少附带 PPR score；只有能从本轮已加载图确定真实 relation 时才附 relation support；
- reserve 只消费本轮 support snapshot，不得为了 provenance 追加 DB 查询。

### 6.2 去重和来源合并

`_mix_retrieve()` 与 `_union_chunk_candidates()` 按 `chunk_id` 去重时：

- 按 `(origin, support_kind, support_id)` union `retrieval_supports`；
- 相同 support key 的 score 取最佳值，并保留最严格的 review snapshot（`rejected` 优先，防止旧 pending 快照覆盖拒绝）；
- union 后有 direct 和 graph 来源的 chunk 不是 graph-only；
- 保持当前主候选对象的文本、element IDs、notebook ownership 和 relevance 选择语义；
- 所有新建/复制 `RetrievedChunk` 的调用点必须显式保留 provenance，防止中途丢失。

定义：

```text
direct origin = semantic | lexical
graph origin  = kg_source | ppr | relation
graph-only    = 至少一个 graph origin，且没有 direct origin
```

### 6.3 最终选择算法

新增纯函数，例如 `select_with_graph_reserve(ranked, token_budget, policy)`，放在 retrieval 领域模块而不是 `ask_service.py` 内联。流程：

1. rerank 全部候选并保留稳定 `rerank_position`；
2. 筛选 eligible graph-only：
   - 至少一个 graph origin；
   - provenance 含 supporting relation 时，本轮 support snapshot 不为 rejected；
   - 原始 graph support 达到既有相关性下限；
   - rerank 后仍位于有限候选窗口内；
   - 有真实 element IDs；
3. 根据 direct 高质量候选对 token budget 的覆盖程度计算小型 graph reserve；
4. 按 rerank 顺序从 eligible graph-only 中选取，不能超过 reserve；
5. 按全局 rerank 顺序填充剩余预算；没有合格 graph 候选时，reserve 100% 返还普通结果；
6. 最终 context 按 rerank position 排序，引用编号保持确定；
7. 任意候选都不能让总 token 超过原 `chunk_budget`。

第 7 条只适用于 reserve 开启的新 selector。现有 `truncate_by_tokens()` 在首个 chunk 自身超预算时仍保留一条，这是 flag-off characterization contract，不能被新实现悄悄改变。reserve 开启时采用严格预算：超长 graph candidate 直接跳过 reserve；普通候选若全部超长，沿用“至少保留全局 rerank 第一条”的现状，并记录 `oversize_first_chunk=true`，因此唯一允许的超预算例外仍是旧行为的首条，而不是 reserve 强插入。

策略参数必须有上限和 kill switch，不照搬参考项目的 15%–30%。首版配置为：

```text
CHUNK_GRAPH_RESERVE=0
```

值表示最多保护的 graph-only chunk 数；`0` 为默认 kill switch。首版不增加比例或伪校准分开关，而只使用既有 rerank 顺序、原 graph support、真实 element IDs 和原 relevance floor 做门控。

### 6.4 严禁的做法

- 不为 graph-only 候选伪造 semantic score；
- 不修改 `_fuse` 权重或 relevance floor；
- 不把 reserve 身份视为 grounded；
- 不保证无关 PPR 候选必进上下文；
- 不在 relation 被 rejected 后继续依靠其 provenance 获得配额；
- 不按来源固定切死最终窗口；空余预算必须返还；
- 不为计算配额重新执行 ANN、FTS、PPR、rerank 或 DB 查询；只消费本轮已有 metadata。

### 6.5 检索测试与验收

- 每条检索路径正确标记 origin。
- 同一 chunk 多路命中后 origin 和 origin score 正确合并。
- producer 在 ANN∪FTS 前保留双 membership，端到端经过 union/rerank/select 后仍可区分 semantic-only、lexical-only 和双命中。
- KG overlay 的真实 relation support/review snapshot 能到达 selector；纯 PPR 不伪造 relation ID。
- graph-only 合格候选在直接候选充足时仍能按策略获得有限空间。
- graph-only 候选不合格、无 element IDs 或 supporting relation rejected 时不受保护。
- 无 graph 候选时输出与旧 token truncate 顺序完全一致。
- reserve 未用完时全部返还 direct 候选。
- reserve 不引入新的超预算；除旧 contract 的 oversize-first-chunk 例外外，总 token 不超过现有 `chunk_budget`。
- flag 关闭时，selected chunk ID 顺序、答案上下文和引用编号与 characterization baseline 一致。
- quota/rerank/origin score 不进入 grounding relevance。
- active/base notebook chunk 的 `notebook_id` 不因 provenance 合并丢失。
- graph-only Recall@K 提升，同时 citation precision、answer grounding 和 P95 检索延迟不超出上线前约定容差。

## 7. 观测与评估

### 7.1 事件

增加不含来源全文的聚合事件：

- `kg_edge_contract_rejected`：edge type、source/target type、reason、count；
- `kg_relation_completion_done`：mode、anchors、candidates、proposed、verified、written、各 rejection reason、耗时、模型 batch 数；
- `mix_provenance_selected`：各 origin recall/selected 数、graph-only eligible/selected、reserve token used/returned、总 token。

事件中的 object/element ID 如非诊断必需应省略；严禁记录完整 evidence excerpt。

### 7.2 通用评测切片

至少维护以下切片：

1. 教材：定义、前置、推导、公式应用跨小节；
2. 技术专著：论点、证据、适用条件跨窗口；
3. 参考手册：结构关系多、推理关系少，验证不会强行补因果边；
4. 多实体比较：防止 Claim 串主体；
5. 中英文别名：规范化但不因别名自动制造关系；
6. 图路径召回：向量/词法 miss、PPR/relation 命中的答案证据；
7. 图噪声：高中心度但低相关或 rejected 的邻居不得靠配额进入。

### 7.3 评测顺序

```text
固定 master baseline
  → P0 contract 修复
  → completion shadow
  → completion write（仅通过门槛后）
  → provenance only（不启用 reserve）
  → graph reserve A/B
```

每一步单独留结果，不能把三项一起开启后只看最终答案分，否则无法判断收益来源。

## 8. 实施任务拆分

### Task 1：冻结 P0 baseline 和边语义

- [x] 为当前 12 种边建立端点正/负 fixture。
- [x] 用测试复现 prompt 允许但 edge trust 判无效的四类漂移。
- [x] 按本文矩阵固定全部有向 pair，并特别覆盖 `depends_on` 12 pair、`contrasts_with` 3×3 和 `precedes` 禁止跨类型。
- [x] 记录现有 `follow_chain` composition 和 graph reasoning edge 行为。

### Task 2：实现统一边注册表

- [x] 新建 `kg/edge_schema.py`。
- [x] 改造 extraction prompt 和落库前验证。
- [x] 改造 edge trust。
- [x] 改造 graph reasoning / follow_chain 的重复常量。
- [x] 所有边读取路径过滤历史非法 pair，并提升 scale-index artifact semantic version。
- [x] 增加禁止第二份边全集的契约扫描测试。
- [x] 增加现有数据只读审计脚本或测试工具。

### Task 3：实现受约束候选生成器

- [x] 新建纯 `relation_completion` 候选模块和数据类。
- [x] 增加 section spread 与低度/弱结构优先级。
- [x] 增加 `(source_id,id)` keyset page、水位状态表和候选 ID indexed EXISTS。
- [x] 增加同源 ANN/FTS 有界 overfetch/filter repository port。
- [x] 排除已有、对称重复、rejected 和类型非法关系。
- [x] SQLite/PostgreSQL 与 ownership manifest 同步。
- [x] 加入 401 对象的书籍规模 keyset、真实任务续跑/启动恢复入口和无索引 fail-closed 测试。

### Task 4：实现模型提案、验证和 shadow

- [x] 定义严格 JSON schema 和 response validator。
- [x] 后端维护 candidate/allowed element authority map。
- [x] candidate 绑定 source_generation；最终写事务执行 generation/ownership CAS。
- [x] 使用 `kg_extract` 提案、`kg_refine` 独立验证。
- [x] 服务端 hydrate evidence，模型 quote 永不落库。
- [x] 增加 off/shadow/write 模式，默认 off。
- [x] shadow 只记聚合事件，不改变 KG。
- [x] 处理取消、超时、解析失败和主抽取成功后的软失败。

### Task 5：完成幂等写入和现有治理接入

- [x] validated edge 以 pending 状态写入现有关系表。
- [x] 用 completion 专属确定性 relation ID + ON CONFLICT 实现并发幂等。
- [x] 复用 mutation/index/cache/embedding 生命周期。
- [x] completion basis 以机器可读 evidence 进入现有 edge review queue；首版不增加用户可见字段，因此无新前端面。
- [x] reparse/delete 能清理对应来源的 completion edges。

### Task 6：贯穿检索 provenance

- [x] 扩展 `RetrievedChunk`，区分内容 notebook 和检索来源。
- [x] 新增结构化 `RetrievalSupport`，在 ANN/FTS union 之前分别标记 membership。
- [x] 让 KG overlay 传递 node/relation/traversed-edge support，让 PPR 保留真实可得支持而不伪造 relation。
- [x] 去重时按 support key union provenance，不改变 relevance。
- [x] 审计所有 `RetrievedChunk(...)` 构造/复制点。
- [x] 增加 provenance-only characterization test，证明未启用 reserve 时输出不变。

### Task 7：实现 token-aware graph reserve

- [x] 新建纯选择函数（首版以有界 seat 数代替独立 policy 类）。
- [x] 实现 eligible graph-only 双门控。
- [x] 实现 reserve 使用、返还和最终稳定排序。
- [x] 接入 mix rerank 后、`truncate_by_tokens` 位置。
- [x] 增加 kill switch，默认关闭。
- [x] 验证 token、grounding relevance 和引用不变量。

### Task 8：评测、上线和文档同步

- [ ] 依次跑 P0、completion shadow/write、provenance/reserve A/B。
- [ ] 记录召回、精度、grounding、引用和延迟结果。
- [ ] 未达到门槛时保持 write/reserve 关闭。
- [ ] 验证 notebook allowlist/stable-hash 灰度和 memory/knowhow 排除策略。
- [x] 等价完整门禁通过：rebase 到最新 `origin/master`（已移除过时 SQLite / SQL2PostgreSQL 测试）后，后端 5,173 通过 + 192 条件跳过；contracts/harness 通过；前端 1,608 项 node tests、92 项 component tests、typecheck 和 production build 通过。
- [x] `cd frontend && npm run build` 通过（由完整门禁执行）。
- [x] 按仓库约定同步 `README.md`、`README_zh.md`、`AGENTS.md`、`CLAUDE.md` 以及所属的中英文 detail docs。
- [ ] 若完成产品 spec 已定义能力，同步 `fangan_done.md`。

## 9. 主要代码落点

预计新增：

- `backend/app/services/kg/edge_schema.py`
- `backend/app/services/kg/relation_completion.py`
- `backend/tests/kg/test_edge_schema.py`
- `backend/tests/kg/test_relation_completion.py`
- `backend/tests/test_mix_provenance_reserve.py`

预计修改：

- `backend/app/services/kg/extract.py`
- `backend/app/services/kg/edge_trust.py`
- `backend/app/services/kg/graph_reason.py`
- `backend/app/services/kg/follow_chain.py`
- `backend/app/services/source_ingestion.py`
- `backend/app/services/knowledge_lifecycle.py`
- `backend/app/services/retrieval.py`
- `backend/app/services/retrieval_candidates.py`
- `backend/app/services/graph_retrieval.py`
- `backend/app/services/retrieval_service.py`
- `backend/app/services/ask_service.py`
- `backend/app/core/config.py`
- SQLite/PostgreSQL knowledge/governance stores、迁移和 ownership manifest（仅在实现需要时）
- 现有 extraction、edge trust、relink、mix characterization 和 edge review 测试

不要因为文件列表是“预计”就绕过仓库的 service/repository ownership 约束；实现前应重新 `rg` 确认真正 owner。

## 10. 发布与回滚

1. P0 注册表先发布。它修复真实契约漂移，不依赖两个 P1。
2. relation completion 默认 `off` 发布；通用 fixture 通过后进入 `shadow`。
3. shadow 达到质量/成本门槛后，仅对受控 notebook 进入 `write`；失败立刻退回 `shadow/off`，既有主抽取不受影响。
4. provenance 字段可先发布但不启用 reserve，验证输出等价。
5. graph reserve 默认关闭，A/B 通过后渐进开启。
6. 回滚开关只停止新补全/新配额；已经写入的 completion edges 通过 `basis` 可审计，并继续服从 pending/verified/rejected 治理，禁止回滚时批量删除未经确认的数据。

## 11. 关键决策记录

- 文档级/全书级抽取计划不在本轮范围。
- 边注册表以产品抽取语义为权威，不能继续以漂移的 trust 镜像表为权威。
- deterministic relink 保留并先执行；LLM completion 只补它覆盖不了的高价值关系。
- 首版自动写入只允许原文明确支持的同来源跨窗口直接边；跨来源关系只做 shadow 候选。
- 模型只能返回 ID 增量，后端掌握端点、类型、证据和持久化权威。
- relation completion 默认 off，write 必须有独立 verifier，失败 fail closed。
- retrieval provenance 不改变 relevance/tau/grounding。
- graph reserve 是相关性门控后的有限保留，不是“图结果无条件占位”；未用预算必须返还。
- 不复制参考项目的固定权重、固定 15%–30% 比例、共享来源强 boost 或全图 Adamic-Adar 扫描。
