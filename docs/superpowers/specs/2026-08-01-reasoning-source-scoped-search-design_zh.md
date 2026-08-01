# 逐步推理来源限定搜索功能设计

- 日期：2026-08-01
- 状态：已实现，并已通过全仓门禁验证
- 分支：`codex/reasoning-source-scope`
- 适用范围：问答的 `reasoning` 模式

## 1. 结论

为逐步推理 Agent 增加一个统一的内部工具 `search_evidence`：

```json
{
  "query": "place_opt_design 在 ICC2 中对应什么命令",
  "source_refs": ["Innovus User Guide", "ICC2 Command Reference"]
}
```

- `source_refs` 省略：在当前 notebook 及其已授权挂载库的全部可见来源中搜索，行为与现状一致。
- `source_refs` 非空：服务端把引用解析成确定的来源 ID 集合，并在该集合内执行严格搜索。
- 空数组不是“全部来源”的别名；首版拒绝空数组，避免调用方在省略与空值之间产生歧义。
- 名称未找到、匹配多个来源或来源已失效时，返回结构化的 `unresolved` 结果，不执行全库兜底。

这是推理 Agent 的内部证据工具，不新增外部 MCP 工具，也不改变 External Agent 当前公开工具集合。

## 2. 用户问题与失败原因

当 notebook 同时包含 A、B、C 三个工具的 manual，用户问“A 的命令在 B 中对应什么”时，当前系统只把 A/B 作为查询词和软提示。KG、SourceElement、PPR、精确章节查找、图扩展和最终引用仍可从整个授权语料取证，因此 C manual 可能因为术语重合进入证据池并被引用。

只在最终引用阶段删除 C 不够：C 仍会影响候选排名、图传播、反思决策和答案合成。来源限定必须是检索执行契约，而不是 prompt 建议。

## 3. 产品语义

### 3.1 全库模式

Agent 省略 `source_refs` 时使用 `mode=all`。所有现有检索、排序和引用行为保持不变；没有来源引用的问题不得因为本功能增加额外数据库查询或改变结果。

### 3.2 限定模式

Agent 提供一个或多个来源引用时使用 `mode=selected`。一旦服务端解析成功：

1. 选中的来源集合成为本轮不可扩大的上限；
2. 后续工具省略范围时继承该集合；
3. 后续工具不能切回 `all`，也不能增加新来源；
4. 首次锁定前产生的全库候选必须被清除或重新按限定范围检索，不能与限定证据混合；
5. 最终合成、anchors 和 citations 再做一次来源集合校验。

限定来源与 notebook/mount 权限取交集。来源 ID 即使真实存在，只要不在当前授权参与者中也视为不可用。

### 3.3 架构决策：run 前固定范围

首次 `selected` 范围只能由 `/ask/intent` 建立：预检解析用户明确指称的来源，返回展示安全快照，并在创建持久 run 或读取任何证据前要求确认。因此每轮开始时只能是 `all` 或 `selected`；`all` run 不允许通过后续 `search_evidence` 首次动态建立限定。`selected` run 内省略 `source_refs` 表示继承，显式列表也只能保持或收窄当前集合。解析歧义或失配一律 fail closed；受限时无法证明可按来源隔离的通道必须带可见原因跳过。

### 3.3 歧义和失效

来源解析只允许确定性结果：规范化后的展示标题、原始文件名或已暴露的稳定来源 ID 精确匹配。大小写、Unicode 和首尾空白可规范化，但首版不把模糊相似度当作授权。

- 0 个匹配：`not_found`；
- 1 个匹配：接受；
- 多个匹配：`ambiguous`，返回最多若干个展示安全的候选名称；
- 删除、撤销挂载或权限变化：`unavailable`。

以上状态都必须 fail closed：停止限定搜索并要求用户补充信息，绝不悄悄退回全库。

## 4. Agent 工具协议

内部请求：

```text
search_evidence(
  query: string,
  source_refs?: non-empty list[string],
  channels?: "auto" | list["kg", "elements", "ppr", "exact"]
)
```

首版默认 `channels="auto"`。服务端根据查询形状和已启用能力执行现有有界通道；模型不能提高候选、token、步骤或探测次数上限。

内部响应：

```json
{
  "scope": {
    "mode": "selected",
    "status": "resolved",
    "sources": [
      {"source_id": "...", "notebook_id": "...", "title": "...", "source_file_name": "..."}
    ]
  },
  "kg": [],
  "elements": [],
  "chunks": []
}
```

模型提交的是引用；服务端签发和持有真实范围。模型不能通过伪造 ID 越过授权目录。

## 5. 来源信息如何从用户描述得到

分两层实现，避免让模型独自承担授权判断：

1. **本轮基础能力：**向规划器提供一个有界、仅含身份元数据的来源目录（展示标题、原始文件名、稳定 ID/别名，不含正文、摘要、KG 或 embedding）。模型只在用户明确提到 manual、论文、文件或“这两份资料”等来源指称时生成 `source_refs`。
2. **后续增强：**增加确定性的来源指称提取器，识别引号/书名号、文件扩展名和精确标题片段；模型负责语义判断，服务端解析器负责唯一性和授权。模糊候选只用于向用户澄清，不能自动形成范围。

不得仅因为问题中的工具名、领域名碰巧接近某个文档标题就自动限定来源。没有明确来源意图时保持全库模式。

大来源目录不能整表塞进 prompt。目录层必须有界：优先给出与用户显式指称精确/词法匹配的身份候选；无法在上限内证明唯一时返回歧义，而不是截断后声称唯一。

## 6. 后端执行边界

`ReasoningSourceScope` 是一次 run 的服务层对象：

```text
mode: all | selected
status: resolved | not_found | ambiguous | unavailable
allowed: set[(notebook_id, source_id)]
display_sources: immutable snapshots
```

以下路径必须消费同一个 scope：

- 初始子查询和 `add_subquery` 的 federated KG；
- `search_elements`；
- PPR seed 与 Agent 主动 PPR；
- exact lookup seed 与 Agent 主动精查；
- neighbors、community 和 follow-chain；
- 类型化集合枚举；
- quota rerank、证据 hydration、最终合成与 citation 构建。

过滤应尽量下推到候选生成，避免不允许的来源占据有界 top-K。暂时无法安全下推的图通道在限定模式下必须关闭并留下可见 skip reason，不能先用全库图传播再只过滤输出。KG 对象跨多个来源有 evidence 时，仅保留选中来源的 evidence；若裁剪后为空则删除该对象。

SQLite 与 PostgreSQL 的新增端口参数和语义必须一致。`source_ids=[]` 在底层始终表示空集合，不得解释成不限制。

## 7. API、持久化与前端

`QueryIntentContract` / `AskResponse.intent` 增加可选的展示安全来源范围快照。省略字段兼容历史数据并表示全库模式。快照至少包含标题、原始文件名、来源数和模式；前端不提交任意来源 ID 来建立权限。

来源范围成功解析后，即使问题本身没有其他歧义，也必须进入一次确认：

- `检索资料：仅限 2 个来源`
- 展开后显示标题；原始文件名不同则显示 `原始文件`。

运行轨迹显示 `已确认检索范围：仅限 2 个来源`。最终答案显示可展开的 `本次依据：2 个指定来源`，重开会话时从持久化快照恢复。任何 UI 文案都不展示内部 token、ID 或“硬范围”等实现术语。

## 8. 兼容性

- 仅 `reasoning` 使用本协议；`chunk` 和实验 `graph` 保持现状。
- 旧请求与旧存储没有范围字段时走 `all`，并保持现有结果。
- 无明确来源指称的请求必须通过特征测试证明调用序列和结果不变。
- 引号搜索、exact probe、检索配额、federation tier、Memory 隔离和引用标题规则保持现有契约。

## 9. 验收场景

构造 A、B、C 三份术语重叠的 manual，每份都有独有事实和可区分引用：

1. 限定 A+B 的初始搜索、补充子查询、元素搜索、精查和 PPR 不返回 C；
2. C 的 KG/evidence/chunk 不进入反思 prompt、答案 prompt、anchor 或 citation；
3. 同名来源产生歧义并停止，不选任意一个，也不全库搜索；
4. 不存在的名称停止，不全库搜索；
5. 已选来源在确认后删除或失去 mount 权限，任务在取证前失败关闭；
6. 省略范围时 A+B+C 的历史行为不变；
7. 回答完成及重开会话后仍显示同一来源快照；
8. SQLite/PostgreSQL 的来源过滤结果一致。

## 10. 实施拆分

1. 来源范围模型、目录解析器与范围守卫；
2. `search_evidence` 协议及初始/反思检索接线；
3. 各检索端口的范围下推与最终防线；
4. 意图确认、轨迹和答案范围 UI；
5. 三 manual 回归、双后端契约、文档与全量门禁。

每一任务完成后分别进行规格符合性审查和代码质量审查；所有审查问题修复并复验后才进入下一任务。
