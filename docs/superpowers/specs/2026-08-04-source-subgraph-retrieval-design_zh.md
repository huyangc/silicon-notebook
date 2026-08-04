# 用户选定来源的子图检索设计

- 日期：2026-08-04
- 状态：**待评审草案 v2；按“所选来源内检索不回退”目标刷新；尚未实施**
- 适用范围：问答（`chunk` / `reasoning` / `graph`）与深度报告共用的来源范围检索
- 前置契约：来源范围只由用户勾选决定；模型不得扩大或缩小范围

## 1. 结论

当用户只选择一篇或部分文章时，系统不应把“图能力”整体关闭，而应在用户所选来源形成的**来源诱导子图**中执行图检索。

来源诱导子图必须在查询进入图、候选 Top-K、PPR 排名和关系遍历之前完成隔离，不能先跑 notebook 全图再过滤输出。无法证明来源归属的节点、边和预计算产物继续 fail closed。

目标行为：

1. 选中全部来源：保持当前整库行为和结果，不增加额外查询。
2. 选中部分来源：先完整保留当前来源限定的直接检索结果，再以来源子图内的 KG 候选、邻居扩展、两跳关系、精确章节和 PPR 做只增不减的增强。
3. 未选来源不得影响种子、路径、排序、反思、大纲、答案或引用。
4. 整库预计算且不可分解的社区摘要等通道继续跳过，但只披露具体未启用的通道，不再笼统声称“图扩展已关闭”。
5. 来源子图构建失败、超时、缺索引或超限时，必须返回与当前来源限定路径相同的基线结果，不能因为尝试图增强而丢失直接检索证据、消耗其步骤或缩小其上下文预算。

这里的“不回退”是一个范围内契约：对同一问题、同一冻结来源集合、同一模式、同一索引代次和同一配置，新增图能力不能降低**所选来源内部**的直接检索召回与最终证据保留。它不承诺只选 A 时复现选择 A+B+C 的答案；后者可能依赖用户明确排除的 B/C，强行保持会破坏来源隔离。

## 2. 当前现状

### 2.1 已经正确的部分

当前 HTTP 入口会：

- 校验 `SourceScope` 只能引用当前 notebook 的可见来源；
- 把 `exclude` 冻结为明确的 `include` 列表；
- 在服务端按可见来源全集计算 `narrowed`；
- 通过 `source_scope_context` 把冻结范围传给候选生成和结果裁剪；
- 在 chunk ANN/FTS、KG FTS、原文元素检索以及最终 evidence/citation 边界执行来源过滤；
- 在真正收窄时排除隐藏的 Memory/Knowhow 投影来源。

因此，来源复选框已经是可靠的请求级授权上限。

### 2.2 当前问题

只要 `narrowed=true`，推理层会把以下能力整体视为不安全：

- PPR；
- community；
- `expand_graph` / neighbors；
- `follow_chain`；
- exact lookup；
- collection enumeration；
- active notebook 的 relation retrieval。

这能防止越界，但会造成“选择一篇文章后，反而不能沿这篇文章自己的关系继续搜索”。EnergAIzer 的最新问题即属于此情况：PDAgent 初检索得到了 20 个 KG 候选，模型随后请求 `expand_graph`，执行层却在 I/O 前跳过，导致“根因分析原理”缺少补充证据。

### 2.3 现有 `graph_rows(source_ids)` 不能直接作为完成方案

`IndexProjectionStore.graph_rows(notebook_id, source_ids)` 已支持来源参数，并会按来源读取对象、关系和 chunk；这是本方案应复用的入口。但当前 scoped 路径仍有四个缺口：

1. 对象按 `knowledge_objects.source_id` 过滤，可能漏掉已经合并、但在 `knowledge_object_sources` 中确实由所选来源支撑的对象。
2. membership 仍通过整库 `_ent_chunk_map(notebook_id)` 构建；在大 notebook 中，即使只选一篇也会读取整库对象 evidence 与 chunk 元素映射。
3. cluster membership 仍读取整本 notebook 后再取交集；隔离结果虽然可保守正确，但成本不是来源有界。
4. scoped 路径会跳过 variant、embedding synonym、mention bridge；这是当前安全选择，但需要在产品轨迹中明确披露，而不能假装与整库 PPR 等价。

因此，不能只删除 `_unsafe_scope_restricted()` 的判断。必须先建立来源有界、查询前隔离的图投影。

### 2.4 当前实例规模

EnergAIzer 中选定的 PDAgent 来源当前包含：

- 980 个来源对象（主来源和 `knowledge_object_sources` 口径一致）；
- 1,236 条来源关系；
- 79 个 chunk；
- 176 个原文元素。

这一规模适合在线构建并缓存来源子图；设计同时必须覆盖“大 notebook 中的一篇超大来源”，不能假设单篇一定小。

## 3. 设计原则与硬约束

### 3.1 授权先于检索

所有来源子图入口只接受 API 已解析并冻结的 `ActiveSourceScope`，不接受模型生成的来源 ID。底层 `source_ids=[]` 永远表示空集合，不能解释为“全部”。

### 3.2 诱导子图而不是结果过滤

一个 active-notebook 图元素只有在以下规则下才可进入来源子图：

- **对象节点**：`knowledge_object_sources` 至少有一条属于允许来源；未回填反向索引的 notebook 只能走有界、数据库原生 evidence 归属查询，不能扫描整库 JSON 后再过滤。
- **关系边**：`knowledge_relations.source_id` 属于允许来源，且两个端点都属于允许对象集合；`source_id` 为空或无效时 fail closed。
- **对象—chunk membership**：对象属于允许对象集合，chunk 属于允许来源，并且 membership 能由该对象在允许来源中的 evidence element 绑定到该 chunk。
- **canonical cluster hub**：只连接当前允许对象集合中的成员；未选来源的 cluster member 不进入 hub，也不能提供桥接路径。
- **chunk 节点**：`chunks.source_id` 属于允许来源。

任何未选来源都不能占候选位、提供中间节点、改变度数/IDF、改变 PPR 归一化区间或影响路径选择。

### 3.3 事实载荷也要隔离

对象可能由多个来源共同支撑，而 `payload` 中的 definition、steps、formula 等字段目前没有字段级来源标记。仅过滤 `evidence`、保留完整 payload，仍可能把未选来源整理出的事实放进 prompt。

来源子图模式采用严格策略：

- topology 可以使用稳定对象 ID、对象类型和规范化名称；
- 进入反思/答案 prompt 的事实内容只来自裁剪后仍属于允许来源的 evidence；
- 未具备字段级 provenance 的 payload 事实字段不得作为独立证据；
- evidence 裁剪后为空的对象从候选与路径中移除。

如后续增加 payload 字段级 provenance，可再安全恢复相应字段。

### 3.4 全选行为不变

省略 `source_scope` 或服务端确认当前全集全选时，继续使用现有整库图、scale CSR、community、synonym、mention bridge 等路径。来源子图逻辑不得改变该路径的调用序列和结果。

### 3.5 大库不能“整库后过滤”

来源子图的数据库工作量必须由所选来源的对象、关系、chunk 与已配置硬上限决定，不能由 notebook 总规模决定。旧索引或缺少来源 sidecar 时允许降级，但不得偷偷运行全图。

### 3.6 所选来源内检索不回退

定义当前生产的来源限定直接检索为基线 `B`，包括：

- source-scoped chunk ANN/FTS；
- source-scoped KG lexical/semantic candidate（按当前可安全执行的实现）；
- raw element 兜底；
- 当前已经产出的 exact/keyword 候选；
- 上述通道现有的分数、排序、引用绑定和 token 截断结果。

来源子图新增结果记为 `G`。实现必须满足：

1. `B` 先独立完成并冻结；图构建不能改变其 query rewrite、候选窗口、分数或顺序。
2. `G` 只能追加来自允许来源的证据或为 `B` 增加来源内关系解释，不能删除、覆盖或降权 `B`。
3. 当前 `select_with_reserves` 会在同一 token 预算内用图候选驱逐直接候选，不能直接用于此契约。来源子图模式必须先按历史预算选出 `B_final`，再使用独立、可关闭、有硬上限的 enrichment budget 追加 `G`；若模型上下文装不下，则丢弃 `G`，不得截短 `B_final`。
4. reasoning 的图动作使用独立的 enrichment step budget；不能消耗当前直接检索、原文兜底、反思或回答所需的既有步数。
5. Deep Report 每个 retrieval direction 也先形成并冻结直接检索基线，图增强失败不得让该方向从“已有直接证据”变成“无证据”。
6. 图通道超时、异常、数据代次漂移或能力关闭时，交付结果必须退回 `B_final`，并可通过候选 manifest 校验为基线等价。

“回答文本逐字不变”不作为可证明目标，因为模型生成具有随机性；硬保证落在检索候选、证据内容、顺序、预算和引用可用性上，回答质量再由固定模型版本、固定采样参数的离线评测与线上 shadow 指标把关。

### 3.7 payload 来源归属不能成为质量缺口

`knowledge_object_sources` 只能证明对象由某来源支撑，不能证明合并后 `payload` 的每个 definition、step、formula 来自哪个来源。严格删除 payload 会造成信息损失；保留完整 payload 又可能引入未选来源事实。

完整方案需要来源级事实投影（名称可在实现评审时确定，例如 `knowledge_object_source_facts`）：

```text
object_id
source_id
source_generation
fact_kind / field_path
fact_value
evidence_element_ids
projection_version
```

写入时必须把每条事实绑定到同来源 evidence；旧数据通过离线、可恢复、按 source generation 的任务回填。回填未完成时：

- 基线 `B` 保持当前来源限定直接证据路径，不因图增强而减少；
- 新增图节点的事实载荷只使用允许来源 evidence 和安全名称；
- 不把缺少 provenance 的完整 payload 当成图增强事实；
- 该来源标记为 `payload_provenance_incomplete`，不能宣称“来源内图检索完整等价”。

这项来源级事实投影是“强隔离 + 不损失现有 KG 事实表达”的必要工作，也是本方案最大的新增数据改造之一。

## 4. 核心数据结构

新增服务层只读对象 `SourceSubgraphSnapshot`：

```text
active_notebook_id
allowed_source_ids              # 服务端冻结后的集合
scope_hash                      # 仅用于缓存键/日志，不替代授权集合
kg_generation
cluster_generation
source_generations
node_ids
edge_rows
chunk_ids
membership_rows
cluster_hubs
complete                       # 是否完整形成所选来源子图
degraded_reasons[]
```

它只存在于请求/缓存服务层，不进入公开 `SourceScope`，也不向模型暴露内部来源 ID。

缓存键至少包含：

```text
(notebook_id, scope_hash, kg_mutation_seq, cluster_mutation_seq,
 selected_source_generations, graph-contract version)
```

缓存必须有容量和生命周期上限；来源删除、重解析、KG 写入或 cluster 重建都会自然换键或显式失效。

另外新增内部 `RetrievalBaselineManifest`，用于证明图增强没有改变基线：

```text
scope_hash / query_hash / mode / index generations / settings fingerprint
direct_kg_ids + scores + order
direct_chunk_ids + scores + order
direct_element_ids + scores + order
selected_baseline_ids + token counts + content hashes
baseline_step_usage
```

manifest 不记录正文，只保存稳定 ID、计数、分数和脱敏 hash。它既用于测试/shadow，也用于生产抽样诊断；不能进入公开 API 或模型 prompt。

## 5. 来源子图投影

### 5.1 查询顺序

在一个一致只读快照中：

1. 校验冻结来源仍存在、仍可见、仍属于 active notebook；
2. 通过 `knowledge_object_sources` 获取允许对象 ID；
3. 通过 `knowledge_relations.source_id` 获取允许关系，并要求两个端点都在允许对象 ID 中；
4. 只读取允许来源的 chunk；
5. 只对允许对象 evidence 与允许 chunk 构建 membership；
6. 只读取允许对象涉及的 cluster membership；
7. 生成 cluster hub，但只连接允许成员；
8. 发布不可变 snapshot。

SQLite 与 PostgreSQL 必须有相同语义；来源谓词和 endpoint 约束必须在 `LIMIT` 之前生效。

### 5.2 不纳入第一版来源子图的边

以下边当前没有足够强的来源归属，第一版不加入 narrowed active 子图：

- embedding synonym；
- variant similarity；
- mention bridge；
- notebook 全局 community 路由边。

它们的缺失是可见降级，不是错误。只有在两端都能证明来自允许来源、且构建输入没有使用未选来源时，后续版本才能恢复。

这里的“降级”只表示图增强 `G` 尚未达到整库图的全部能力，不能影响直接检索基线 `B`。如果这些边的缺失导致 `B_final` 中任一证据被移除、降权或挤出，则属于实现错误，而不是允许的产品降级。

### 5.3 未回填 `knowledge_object_sources`

若 notebook 的来源反向索引未回填：

- 小范围可使用数据库原生、按所选来源约束的 evidence 查询来获取对象 ID；
- 查询必须 keyset/分页且受硬上限约束；
- 超出上限时不构建不完整图冒充完整图，转入有界邻居/原文降级并披露原因；
- 离线 `backfill-source-index` 仍是大库的显式修复路径。

### 5.4 大库少量来源的 scope 解析

当前 `_validate_source_scope` 为验证一个 `include=[少数来源]` 仍会读取 notebook 的全部可见 source ID；reasoning 的 intent 与 stream 入口还会重复执行。前端若从“全选”逐个取消，也可能发送巨大的 `exclude` 列表。这不会扫描正文，但会让大库少量来源提问保留一个与来源总数线性相关的元数据成本。

改造要求：

1. 前端基于当前可见来源集合自动选择更短的 `include` / `exclude` 表达；选择一篇或少数几篇时必须发送小型 `include`。
2. 后端对 `include` 使用“定向 ownership/visibility 查询 + 可见来源 COUNT”验证，不得读取全部 ID。
3. 已确认 intent 到 stream/generation 的冻结 scope 复用服务端 scope token/manifest，并重验所选来源代次与可见性；不重复物化全部来源 universe。
4. 只有冻结 `exclude` 确实需要求补集时才枚举可见 ID；超大补集应转换成服务端持久/临时 scope manifest，而不是塞进公开请求或任务 payload。
5. scope manifest 与来源子图缓存键分离：前者是授权快照，后者是可丢弃的性能缓存。

## 6. 各检索通道的处理

### 6.1 双通道执行与合并

来源限定请求采用两条相互隔离的内部通道：

```text
Baseline lane: 现有 source-scoped KG/chunk/raw 检索 → 历史预算选择 → B_final（冻结）
Graph lane:    来源子图 seed/neighbor/chain/PPR/exact → 来源校验 → G
Merge:         B_final 原样保留 + 独立 enrichment budget 内追加 G
```

执行要求：

- 两条通道可以并行，但 graph lane 不得成为 baseline lane 的前置条件；
- baseline lane 产出的候选和最终选中证据在合并前不可变；
- 同一 chunk 同时出现在 `B` 与 `G` 时，只追加 provenance/关系解释，不改变 baseline relevance 和位置；
- 图新增 chunk 必须通过与 baseline 相同的来源、evidence、anchor、citation 末端校验；
- enrichment budget 是独立上限，默认值、模型上下文上界和成本必须在产品配置文档中明确；设为 0 时结果与当前 baseline 字节级等价；
- enrichment 上下文过大、过慢或失败时整体丢弃 `G`，而不是重新截断 `B_final`。

这意味着来源子图不会简单接入现有 mix 池后统一 rerank。统一 rerank 会改变 baseline 顺序，现有 graph reserve 还允许图候选驱逐直接候选，两者都不满足不回退契约。

### 6.2 通道能力

| 通道 | narrowed 后目标行为 | 实现边界 |
|---|---|---|
| KG lexical/semantic seeds | 保持启用并纳入 `B` | 现有直接候选集合/排序先冻结；候选生成前按允许对象/来源过滤，新增图事实只用允许 evidence/source facts |
| raw elements | 保持启用并纳入 `B` | 当前来源有界路径继续使用；图失败不能跳过这次兜底 |
| chunk ANN/FTS | 保持启用并纳入 `B` | 继续使用 chunk source sidecar；旧索引走来源内 FTS；图通道不得修改其 query/Top-K |
| `expand_graph` / neighbors | **恢复** | 只查允许关系与允许端点；端点索引 + 来源谓词先于上限 |
| `follow_chain` | **恢复** | 两跳中的每一条边和每个节点均须授权；direct-edge guard 使用同一 scope |
| relation retrieval | **恢复** | relation ANN 无来源 sidecar 时使用来源内 FTS/端点补召回，不跑全库 ANN 后过滤 |
| exact lookup | **恢复为 `G`** | exact probe、分组、section/chunk hydration 全程携带 allowed source IDs；不得挤出 `B_final` |
| enumeration | 条件恢复 | 只枚举所选来源，集合总数也按来源 SQL 聚合；不能复用整库 map |
| PPR | 条件恢复 | 在来源子图上运行；不能切 notebook 全图 CSR 后过滤结果 |
| community summaries | 暂不恢复 | 当前摘要是整库预计算事实，不能从结果中安全减去未选来源 |
| whole-corpus profile | 暂不恢复 | 深度报告需另做来源聚合画像，不能复用整库画像 |

## 7. 来源子图 PPR

### 7.1 小型/中型来源子图

对未超过在线子图硬上限的 snapshot：

1. 从 snapshot 的节点/边直接构建稀疏 transition；
2. KG seed 只来自来源限定 KG 检索；
3. chunk seed 只来自来源限定 chunk 检索；
4. reset 中不存在未授权节点；
5. PPR 只在 snapshot 节点上迭代；
6. 只在允许 chunk 集合内做 min-max 归一化和 Top-K；
7. hydrate 后再次执行来源边界校验。

现有 `graph_rows(source_ids)` 的装配规则可以复用，但必须先完成 §5 的来源有界改造，尤其不能再调用整库 `_ent_chunk_map`。

PPR 排名只决定 `G` 内部的顺序。它不得重排 `B`，也不得参与计算 `B` 的 min-max 区间；否则增加一个远端图节点就可能改变所有直接证据分数，违反不回退契约。

### 7.2 超大单篇来源

在线构建超过硬上限时：

- 不回退 notebook 全图 PPR；
- 继续运行来源内 KG/原文/chunk 检索；
- `expand_graph` / `follow_chain` 继续按端点索引做有界局部遍历；
- 轨迹显示“来源子图过大，本轮概念漫游降级为有界关系扩展”；
- 后续通过 scale artifact 的 KG-node / relation-source sidecar 支持大来源诱导 CSR，而不是提高在线内存上限。

这保证大来源仍有图搜索，只是暂时没有全子图 PPR；不能把降级描述成完整 PPR。无论是否有 PPR，`B_final` 必须完全保留，因此这是“增强能力未完整上线”，而不是来源内现有检索效果回退。

若产品要求“大来源与小来源具有相同的 PPR 能力”，则 §13 的 scale source sidecar 不再是后续优化，而是正式发布来源子图功能的前置条件；在它完成前只能 shadow，不能对外宣称功能完成。

### 7.3 挂载参考库

用户复选框只约束 active notebook，挂载 base 仍是授权参与者。第一版建议：

- active notebook 在所选来源子图内运行；
- 每个 base 按现有全库授权范围独立检索/PPR；
- 各参与者结果在现有统一相关度尺度上合并；
- 第一版不建立 active-source-subgraph 与 base 全图之间的新跨层 synonym 路径，避免重新引入不可审计桥；
- 已有“source-safe KG seed 直接映射 base chunk”路径保留。

独立运行会缺少一部分新增的跨层传播召回，但不得删除 active baseline 或 base baseline 已经直接召回的证据。若后续要恢复跨层路径，只能从已授权 active 节点向 base 节点建立可解释、可审计的桥。

## 8. Reasoning Agent 行为

### 8.1 能力暴露

当来源子图 snapshot 完整时，reflect schema 应重新提供：

- `expand_graph`；
- `follow_chain`；
- `ppr_retrieve`（仅 snapshot 未超上限时）；
- `exact_lookup`；
- 来源约束的 enumeration（集合支持时）。

模型仍不能改变来源集合。

图工具使用单独的 enrichment action/step 额度。基线检索、空证据 raw-element fallback、必要的 reflect 和最终 answer 不因模型调用图工具而少一次机会。若 enrichment 额度耗尽，执行器返回基线继续回答，而不是把整轮变成“不完整检索”。

### 8.2 纵深防御

即使模型提交未开放动作，执行器仍按通道能力表二次校验。区别在于不再使用一个全局 `_unsafe_scope_restricted()` 同时关闭所有能力，而是计算逐通道 capability：

```text
source_subgraph_neighbors
source_subgraph_follow_chain
source_subgraph_ppr
source_scoped_exact_lookup
source_scoped_enumeration
whole_corpus_community
```

每个 capability 都有 `enabled` 与具体 `reason`，避免一个布尔值掩盖所有差异。

### 8.3 收尾清理

当前 narrowed run 在 answer 前会无条件清空 `chains`、`enumerations` 和 collection map。改造后只清除未获授权或未完成的通道结果，来源子图内已验证的 chain/enumeration 必须保留到合成。

合成输入按 `B_final`、`G` 两段组织。`B_final` 的内容、顺序和 citation key 先固定；`G` 使用独立 key 区间追加。图通道不得重新编号或使现有 baseline citation key 失效。

## 9. 前端与用户可见行为

不新增来源选择控件，也不新增用户操作步骤。现有复选框仍是唯一范围来源。

建议的轨迹呈现：

- 开始：`已在 1/4 个来源的子图中检索`；
- 邻居扩展：`在所选来源子图中扩展，新增 N 个对象`；
- PPR：`在所选来源子图中概念漫游，新增 N 段`；
- 部分降级：`整库社区摘要不适用于限定来源，已跳过`；
- 超大来源：`来源子图过大，概念漫游已降级为有界关系扩展`。

不再显示笼统的“限定来源下已关闭无法安全隔离的图扩展通道”，除非所有图能力确实都不可用；即使如此也应列出具体原因。

Ask 和 Deep Report 应显示同一来源子图状态。后端新增任何状态字段时，前端必须在同一变更中展示，不能只写 trace 后端字段。

## 10. 并发、一致性与失败处理

- API 入口继续冻结允许来源集合；并发新增来源不会进入本轮。
- 所选来源在执行前被删除、转为隐藏或失去权限：本地参与范围失效；没有 base 时失败关闭，有 base 时 active 子图为空并显式披露。
- 来源重解析或 KG 代次变化：snapshot 缓存换键；构建中发现代次变化则丢弃本次结果并重试一次或降级，不能发布混合代次子图。
- 构建失败：交付冻结的 `B_final`，具体图通道 fail open 并留下可见原因；不得退回全图，也不得重新计算一个更小预算的 baseline。
- evidence 裁剪后为空：对应节点、关系、chain、anchor 和 citation 全部删除。
- graph lane 达到 deadline 时只取消 `G`；不能取消已经完成或仍在合法 deadline 内的 baseline lane。
- 合并或 prompt 装配发现上下文不足时先删除 `G`，直到 `B_final` 完整放入；若连历史 baseline 自身都放不下，沿用历史 oversized-first 行为并记录为 baseline 问题，而不能归因于来源子图。

## 11. 可观测性

新增脱敏事件/指标：

```text
source_subgraph_build_started/done/skipped
selected_source_count
node_count / relation_count / chunk_count / membership_count
cache_hit
build_ms
capability states + degraded reasons
ppr_nodes / ppr_edges / ppr_ms
post_scope_drop_count
baseline_manifest_hash
baseline_candidate_count / baseline_selected_count / baseline_tokens
enrichment_candidate_count / enrichment_selected_count / enrichment_tokens
baseline_preserved_count / baseline_evicted_count
enrichment_timeout / enrichment_failure
```

日志不记录 evidence 正文，不记录完整来源 ID 列表；可记录 scope hash、计数和稳定原因码。

若 `post_scope_drop_count > 0`，说明前置隔离或并发一致性存在异常，应保持结果 fail closed 并发出高优先级诊断，而不是把后过滤当作正常路径。

`baseline_evicted_count` 在任何来源子图请求中必须恒为 0；非零即视为正确性故障并自动丢弃整段 `G`。shadow 阶段还应比较 enrichment 开/关两次运行的 baseline manifest，发现差异即阻止 rollout。

## 12. 测试与验收

### 12.1 核心隔离夹具

构造 A、B、C 三个术语重叠来源：

- A/B 共享 canonical concept；
- B/C 有高相关关系和 chunk；
- 某对象同时含 A/B evidence；
- 某关系 `source_id=B`，但端点也出现在 A；
- 某 cluster 同时含 A/B/C member。

只选择 A 时断言：

1. KG seed、chunk seed、PPR reset 中没有 B/C；
2. A 的邻居和两跳关系可以被找到；
3. B/C relation 不能成为中间边；
4. cluster hub 只连接 A member；
5. PPR 排名和归一化不受 B/C 节点影响；
6. prompt、outline、answer、anchor、citation 都没有 B/C evidence 或 payload 事实；
7. exact lookup 和 enumeration 只返回 A；
8. community 明确降级，不执行整库 community I/O。

### 12.2 成本与查询形状

- PostgreSQL/SQLite 都断言来源谓词发生在 `LIMIT` 前；
- scoped graph build 不调用整库 `_ent_chunk_map`；
- scoped cluster 查询只读取允许对象涉及的 member；
- 高度节点的 neighbors/follow-chain 仍受 endpoint 与行数上限约束；
- 旧 scale index 缺来源 sidecar 时不会执行全库 ANN/PPR 后过滤；
- 超大来源稳定降级且没有 notebook 全图构建。

### 12.3 检索不回退夹具

在固定 query rewrite、embedding、reranker 与索引代次下，对同一冻结 scope 分别运行 enrichment off/on/failure/timeout，断言：

1. `B` 的 KG/chunk/element candidate ID、分数和顺序一致；
2. `B_final` 的 ID、正文 hash、顺序、token 数和 citation key 一致；
3. 开启 `G` 后 `baseline_evicted_count=0`；同一 chunk 命中 `G` 只增加 provenance，不改变 baseline relevance；
4. `G` 超预算时只丢弃图新增项；oversized-first baseline 行为不变；
5. reasoning 图动作不减少 baseline step/fallback 次数；
6. Deep Report 每个 retrieval direction 的 baseline manifest 不因图失败而变化；
7. 多来源对象只能呈现所选来源事实投影/evidence，且 source-facts 回填前后 baseline 证据召回不下降；
8. 一篇、小批量来源、超大单篇、mounted base 四类场景都执行上述比较。

### 12.4 质量门与 rollout

单元测试只能证明集合不被删，不能证明新增图上下文不会让模型更容易答错。正式启用还需使用真实问题集执行固定模型版本/采样参数的离线 A/B：

- selected-source evidence Recall@K 不低于 baseline；
- citation coverage、citation validity 和 grounded sentence coverage 不低于 baseline；
- 未选来源证据/事实/引用为零；
- 无答案率、错误拒答率和 outline dropped section 不高于 baseline；
- 延迟、数据库行数、峰值内存和新增 prompt token 在批准的预算内。

上线顺序固定为 `off → shadow → 小范围 allowlist → 稳定 hash rollout → default-on`。shadow 同时跑 baseline manifest 与图检索，但用户仍收到 baseline；任何硬指标回退自动阻止放量。回答质量属于统计门，不能以单次示例宣称无回退。

### 12.5 兼容性

- 全选与省略 scope 的调用序列和结果保持既有测试逐位不变；
- 单篇 notebook 全选仍走整库路径；
- narrowed 后 conversation history 的现有隔离语义不变；
- mounted base、Memory/Knowhow 排除、引用标题规则、quoted spans、exact identifier gate 均保持现有契约；
- Ask 与 Deep Report 使用同一来源子图服务和同一失败原因码。

## 13. 实施拆分建议

可执行的逐 PR 依赖、合并门和回滚点见：`docs/superpowers/plans/2026-08-04-source-subgraph-retrieval-pr-series.md`。

### 阶段 A：不回退地基与 shadow

1. 固化当前 source-scoped baseline，增加 `RetrievalBaselineManifest`；
2. 把 baseline 选择与 graph enrichment 选择拆开，增加独立 enrichment token/step budget；
3. 保证 enrichment=0/failure/timeout 时 baseline 字节级等价；
4. 优化大库少量来源的 scope 验证和前端 include/exclude 规范化；
5. 建立 shadow 指标、回退熔断与真实问题评测集。

阶段 A 不恢复任何新图能力，但它是后续所有阶段不回退的发布前提。

### 阶段 B：来源事实与授权投影

1. 增加 `SourceSubgraphSnapshot` 与逐通道 capability；
2. 改造双后端 graph projection，使用 `knowledge_object_sources`、来源内 membership 与 scoped clusters；
3. 增加来源级 fact/payload provenance 写入、离线回填和代次；
4. 增加图通道 payload/evidence 严格裁剪；
5. 建立 A/B/C 隔离夹具、SQL 形状测试和回填/重解析/删除竞态测试。

### 阶段 C：局部图能力

1. 恢复 scoped neighbors / `expand_graph`；
2. 恢复 scoped `follow_chain` 与 relation retrieval；
3. 恢复 scoped exact lookup；
4. 按集合能力恢复 scoped enumeration；
5. 接入独立 enrichment lane，更新 reasoning capability、answer 收尾与前端轨迹；
6. 先 shadow，再按来源规模 allowlist 放量。

### 阶段 D：来源子图 PPR

1. 在线小/中型 snapshot transition 与缓存；
2. 来源限定 KG/chunk seed；
3. PPR 排名、归一化、hydrate 双重校验；
4. Ask、graph mode、Deep Report 共用；
5. PPR 只排序 `G`，不得重排 `B`；
6. 超限行为、可观测性和 shadow 质量门。

### 阶段 E：大来源规模化

1. 评估并设计 KG-node / relation source sidecar；
2. 离线构建可按来源诱导的 scale artifact；
3. 保持旧 artifact 的确定性降级；
4. 用真实大库验证内存、延迟、召回和 baseline preservation；
5. 达到“大来源与小来源相同图能力”的要求后再移除相应未完成标记。

每个阶段都必须后端、前端、测试、英文/中文产品文档同一变更交付；实现完成并通过 `scripts/check.sh` 与前端 build 后，才更新 `fangan_done.md` 为已完成。

## 14. 建议的评审决策

本草案建议确认以下六点后再实施：

1. **community 第一版继续关闭**：因为它是整库预计算摘要，不属于可安全裁剪的来源子图。
2. **跨 base 第一版独立检索后合并**：先保证授权清晰，不恢复不可解释的跨层 synonym 传播。
3. **baseline 与 enrichment 分账**：接受一个独立、严格有界的新增 prompt/step 成本，换取图证据永不驱逐直接证据；不允许拿现有同预算 reserve 冒充“不回退”。
4. **多来源对象建立来源级事实投影**：不接受“删除 payload 导致质量损失”或“保留完整 payload 导致越界”二选一。
5. **大来源能力口径**：若要求与小来源相同 PPR，scale source sidecar 是上线前置；若允许先交付局部图，则必须保留 baseline 且标记 PPR 尚未完成。
6. **shadow 质量门是发布条件**：代码完成不等于效果无回退，真实问题集未过门不得 default-on。

若这六点获批，实施可按 §13 顺序展开。

## 15. 工作量判断

按“所选来源内检索硬性不回退 + 强来源隔离 + 大来源最终具有完整图能力”的目标，这是一个**大活（XL）**，不是删除 `_unsafe_scope_restricted()` 或补几条 SQL 能完成的改动。

| 工作包 | 规模判断 | 主要影响面 |
|---|---|---|
| baseline manifest、双预算/双步骤、shadow 熔断 | 中到大 | Ask chunk、reasoning、Deep Report、selector、观测 |
| scope 大库优化 | 中 | 前端选择状态、Ask/Report API、任务冻结与重验 |
| 双数据库来源子图投影 | 大 | SQLite/PostgreSQL repository、membership、cluster、缓存与代次 |
| 来源级 fact/payload provenance | 大到超大 | schema/migration、抽取/合并、回填、删除/重解析、prompt |
| neighbors/chain/relation/exact/enumeration 接入 | 大 | reasoning actions、图服务、引用与前端轨迹 |
| 来源子图 PPR 与大来源 sidecar | 大到超大 | scale builder/artifact、在线加载、失效、资源上限 |
| 质量评测与渐进 rollout | 中到大 | golden set、shadow A/B、指标与发布开关 |

不建议合成一个巨型 PR。阶段 A 可以独立交付，因为它只建立“不回退护栏”；阶段 B/C 可先让小中型来源在 shadow 中恢复局部图；阶段 D/E 再完成 PPR 和超大来源等价。任何阶段未通过自己的 baseline preservation 与隔离测试，就继续返回当前 baseline，不把半成品暴露给用户。

如果只要求“修复 EnergAIzer 这类中小单篇来源的 neighbors/follow-chain，同时保证当前直接检索不受影响”，可以把第一交付切到阶段 A+B+C 的受限子集，属于**中大型工作**；如果要求所有 Ask 模式、Deep Report、mounted base、超大单篇、PPR、payload provenance 一次性达到完整不回退，则属于**完整的跨层检索工程**。
