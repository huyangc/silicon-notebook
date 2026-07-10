# Follow-chain：reasoning 模式的查询期类型化两跳推理

- 日期：2026-07-10
- 状态：实现完成，验证中
- 分支：`codex/follow-chain-reasoning`
- 对应产品方案：§5.9（仅 query-time inference 子集）、§6.5、§11

## 1. 背景

当前 `reasoning` 模式可以逐轮 `expand_graph`，`graph` 模式也能在 rustworkx 图上做
有界 BFS；两者都能把 `A→B→C` 的节点和边交给答案模型，但没有一个确定性步骤负责：

1. 判断两条关系是否真的可组合；
2. 检查边的审核状态、证据与节点适用条件；
3. 形成明确的、仅本次查询有效的 `A→C` 推论；
4. 把两条原始边作为独立可引用前提交给答案模型。

因此现状仍是“模型看见路径后自由发挥”，不是受控的部分推理能力。

## 2. 决策

在现有 `reasoning` agent 中新增 `follow_chain` action，而不是新增 Ask mode、API
endpoint 或数据库表。动作通过两轮有界索引查询找到同向两跳路径，执行类型化组合，
产出临时 `InferredChain`。推论不写入 `knowledge_relations`，只进入本轮 reflect
上下文、最终答案上下文与 `reasoning_trace`。

### 2.1 首版组合白名单

仅允许同类型关系的两跳组合：

| 路径 | 临时推论 | 允许节点类型 |
|---|---|---|
| `derived_from ∘ derived_from` | `derived_from` | Claim / Formula |
| `kind_of ∘ kind_of` | `kind_of` | Concept |
| `prerequisite_of ∘ prerequisite_of` | `prerequisite_of` | Concept / Claim |
| `precedes ∘ precedes` | `precedes` | Procedure / Claim / Formula |
| `part_of ∘ part_of` | `part_of` | Concept |

`supports`、`depends_on`、`contrasts_with`、`about`、`defines`、`used_in`、
`composed_of` 不进入首版传递白名单；混合边类型也不组合。宁可漏推，不把关联性误写成
逻辑传递。

### 2.2 Fail-closed 约束

一条路径只有同时满足以下条件才产生推论：

- 两跳都保留数据库原始 `source→target` 方向；反向搜索只返回按存储方向正规化后的链；
- 两跳关系类型相同且在白名单中；
- 三个端点均处于 `approved/reviewed/project_specific/conflict`；
- 任一 `review_status='rejected'` 的边立即淘汰；
- 每一跳必须有非空原始 quote；无证据不沿用 graph verifier 的 fail-open 语义；
- `A/B/C` 三点互异，环路和回到起点的路径不推导；
- 若已经存在同类型直接 `A→C`，不再把它包装成“新推论”；
- 起点必须是本次 reasoning run 已检索到的当前候选，不能使用模型猜测的任意 id；
- `chain_trust < 0.5`、NaN/Infinity 或格式错误的 confidence 均拒绝；
- Claim / Formula 的 `validity_scope` 必须兼容。

### 2.3 适用条件合并

- `region`：所有非空集合取交集；空交集则拒绝；
- `approximation` / `range`：多个非空归一值不一致则拒绝；
- `assumptions`：稳定去重后并集；显式正反条件以及长/短沟道、小/大信号、强/弱反型等窄域互斥条件视为冲突；
- 空 scope 表示“来源未声明限制”，不等于自动证明普遍成立；输出仍保留已知 scope。

首版不引入条件本体或 LLM scope judge，避免在严格路径上增加不可控调用。

### 2.4 可信度

每跳可信度只使用可解释、查询时已有的信号：

```text
hop_trust = evidence_confidence
           × (verified=1.0, pending=0.9)
           × (base=1.0, personal=0.85)

chain_trust = min(hop_trust_1, hop_trust_2) × 0.9
```

缺少 evidence confidence 时按 `1.0`；格式错误/非有限值按 `0`；quote 为空时整条路径
拒绝。可信度使用最终展示的 primary quote 对应 confidence，避免“引用一条、按另一条
高分证据打分”。`chain_trust < 0.5` 不产生推论。该分数与 retrieval relevance 分离；
relation anchor 的相关度只能来自授权该 action 的起点候选，并受 hop trust 上限约束，
不能借用答案中无关高分命中，不污染现有 `[0,1]`/tau 检索不变量。

## 3. Action 协议

Reflect 输出新增：

```json
{
  "next_action": "follow_chain",
  "follow_chain": {
    "start_object_id": "ko-...",
    "target_object_id": "",
    "edge_type": "derived_from",
    "direction": "out"
  }
}
```

- `start_object_id` 必须来自当前候选；
- `target_object_id` 可空，非空时只返回连接到该另一端点的推论；
- `edge_type` 可空，空时探索全部白名单类型；
- `direction` 为 `out|in|both`，默认 `out`；无论搜索方向，结果始终按数据库
  `source→target` 渲染；
- 同一 `(start,target,type,direction)` 每次 run 只执行一次，动作总数有内部硬上限。

## 4. 查询与规模边界

不复用 `_retrieve_neighbors`（会丢边证据/方向/review/tier，也只查 active），不复用
`_federated_rx_graph`（大库会全图加载）。新 retrieval primitive：

1. 校验起点属于 active notebook 或任一 base notebook；
2. 根据起点实际所属 notebook 查询第一跳；
3. 以每个中间节点为 frontier 查询第二跳；
4. 每节点、每关系最多 `max_fan_out=8`，最终最多返回 4 条推论；每个 endpoint 原始
   relation 读取预算为 `clamp(max_fan_out×8, 32, 256)`，在 SQL `LIMIT` 内截断；
5. 强制使用已有 `(notebook_id, source_object_id)` / `(notebook_id, target_object_id)`
   索引；类型/review 优先级只在有界样本内处理；
6. 若 endpoint 被截断，且无法在样本内证明不存在直接 `A→C`，该候选推论直接拒绝；
7. 两跳必须位于同一 notebook，首版不跨 notebook 虚构关系。

该路径成本随本次 frontier 有界，不随整库节点数增长。生产 schema 保持 v9，明确不做
schema migration、新索引、启动时建索引或历史回填；千万级既有 Concept / relation 数据
继续由原检索路径使用。follow_chain 对旧边缺 quote 时只会漏推，不会修改或废弃旧数据。

## 5. Grounding 与答案表达

每条原始 hop 生成一个 relation evidence key，绑定 relation id、存储方向、原始 quote、
source metadata 和 tier。最终上下文形如：

```text
k2001: [relation][base] A --derived_from--> B — evidence: "..."
k2002: [relation][base] B --derived_from--> C — evidence: "..."

[Query-time typed inference; NOT directly stated]
path: [k2001] + [k2002]
inference: A --derived_from--> C via B; chain_trust=0.90
```

答案纪律保持不变：两条直接前提可以分别引用 `[k2001]` / `[k2002]`；`A→C` 推论句必须
以 `（推断）` / `Likely,` 标明，且推论句自身不挂 `[k]`。不得给临时边伪造来源。

关系 evidence anchor 在引用浮层显示 quote、来源、位置和 tier；由于 relation id 不是
KG node id，“在知识图谱中定位”按钮对这类 anchor 禁用。

新抽取的 relation quote 同时尽力绑定到 `SourceElement`，使后续路径具备元素级来源；
旧数据若只有 quote，则保留 source-level 降级展示，不回填、不阻塞推理。

## 6. 产品接线

- `ReasoningResult` 独立携带 chains；不能只把 A/B/C 塞进 `top_hits`，否则最终 quota
  可能淘汰中间节点并丢失推论；
- chains 进入下一轮 reflect 摘要、Ask 最终合成和 Deep Report 单节合成；
- 每次成功或空结果均产生 `follow_chain` trace step，流式 `/ask/stream` 原样发送；
- trace detail 最多携带 4 条无 quote 的结构化 path（source/via/target/type/trust/scope），
  供会话审计；
- 前端新增短标签“推导”和 `2 跳 · N 条 · 可信度 X%` 详情，无新按钮；
- trace detail 只放节点名/关系/trust/scope，不放完整私密 evidence quote；
- 取消信号在动作前后继续检查，取消的回答不保存。

## 7. 非目标

- 不微调模型；
- 不做任意 Horn rule、混合关系代数或超过两跳的 closure；
- 不把推论写回 KG，不新增候选审核队列；
- 不改变 `graph` 模式现有 renderer 的历史方向语义；
- 不新增环境变量；
- 不宣称 `supports` / `depends_on` 具有传递性。

## 8. 验收

- 五种白名单关系正例；禁止类型/混合类型反例；
- 方向、target 过滤、环路、fan-out/result cap；
- 缺 quote、rejected edge、deprecated node、scope 冲突均拒绝；
- base 起点可从 personal Ask 使用，无关 personal notebook 起点不可访问；
- trust 使用最弱跳、tier 与 hop penalty；
- schema golden / `SCHEMA_VERSION=9` 不变，查询计划命中既有 endpoint 索引；
- 高度节点原始样本截断时 direct-edge guard fail-closed，不出现无界扫描；
- 查询前后 `knowledge_relations` 行数不变；
- synthesis prompt 含两条 relation anchors 与明确 query-time inference；
- reasoning live/final trace、Deep Report、前端短标签生效；
- `scripts/check.sh` 与 `frontend npm run build` 通过。
