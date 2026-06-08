# 历史会话「推理」标记 + 恢复时默认开启推理按钮 — 设计

日期：2026-06-08
状态：已批准（待落地）

## 背景与问题

问答区有一个「✦ 推理」切换按钮（`reasoning-toggle`，[frontend/app/page.tsx:2342](../../../frontend/app/page.tsx#L2342)），开启后走流式推理端点 `/ask/stream`，关闭则走快速端点 `/ask`。提交时通过 `payload.mode = "reasoning" | "fast"` 传给后端。

当前存在两个体验缺口：

1. **历史会话列表不显示某会话是否用过推理。** 「会话」弹窗里的卡片只显示 `标题 / 时间 · N 轮 / 重命名·删除`（[page.tsx:2262-2267](../../../frontend/app/page.tsx#L2262)），无法一眼看出哪些是推理会话。
2. **恢复会话时不会还原推理按钮状态。** `openSession`（[page.tsx:1529](../../../frontend/app/page.tsx#L1529)）只重置了 `pendingReasoning`，没有动 `reasoningMode`（按钮状态）。用户恢复一个推理会话后，按钮仍停在上一次的状态，需要手动再点开。

目标：在历史卡片上标记推理会话，并在恢复推理会话时默认把推理按钮打开。

## 信号源与判定规则

- **某一轮是否走了推理**：以 `AskResponse.reasoning_trace != null` 为准。只有推理模式 `ask_reasoning()` 会写入它（[sqlite_repository.py:3889](../../../backend/app/services/sqlite_repository.py#L3889) `reasoning_trace=trace or None`），快速模式 `ask()` 永远为 null。整条 `AskResponse` 已被序列化进 `answers.payload` 持久化，故该信号在历史数据里始终可得。
  - 备注：`llm_mode` 字段记录的是 grounding 性质（`deterministic`/`grounded`/`ungrounded`/`global` 等），**不**表示推理与否，不可用作信号。
- **会话级判定规则**：**看最后一轮**。会话最近一轮用了推理 → 该会话算「推理会话」；最后一轮是快速 → 不算。卡片标记与恢复时按钮默认值都用这同一规则，保证一致；契合「接着上次的模式继续问」的直觉。单轮会话（当前绝大多数）下与「任意一轮」等价。

## 方案选型

采用**方案 A：从现有 `reasoning_trace` 派生，不改表结构。**

- 后端 `list_conversations` 用子查询取该会话「最后一轮」answer 的 `reasoning_trace` 是否非空，在 `ConversationSummary` 上多回一个 `used_reasoning` 布尔。
- 前端列表读 `used_reasoning` 画标记；恢复会话时前端直接看已加载的 `detail.turns` 最后一轮自行决定按钮开关（无需后端额外字段）。

已否决的方案 B：给 `answers` 加显式 `mode` 列（迁移 + 回填）。更显式、能扛「推理但最终 trace 为空」的极端情况，但为一个展示性小功能引入表结构迁移偏重，YAGNI。

## 详细设计

### 1. 后端：`ConversationSummary.used_reasoning` + `list_conversations`

- `backend/app/models/schemas.py`：`ConversationSummary` 增加字段 `used_reasoning: bool = False`。
  - 注意 `ConversationDetail(ConversationSummary)` 继承该字段；`get_conversation` 不显式设置时取默认 `False` 即可（前端不依赖 detail 上的该字段，恢复逻辑直接读 `turns` 的 `reasoning_trace`）。
- `backend/app/services/sqlite_repository.py` 的 `list_conversations`（[:4030](../../../backend/app/services/sqlite_repository.py#L4030)）：在现有 SQL 中追加一个相关子查询，取该会话 `created_at` 最大那条 answer 的推理标志：

  ```sql
  (SELECT json_extract(a.payload, '$.reasoning_trace') IS NOT NULL
     FROM answers a
    WHERE a.conversation_id = c.id
    ORDER BY a.created_at DESC
    LIMIT 1) AS used_reasoning
  ```

  - `json_extract` 在键缺失或值为 JSON `null`（含空 trace，因 `trace or None`）时返回 NULL → `IS NOT NULL` 得 0；非空数组时得 1。
  - 无 answer 的空会话：子查询返回 NULL → 映射为 `used_reasoning=False`。
  - 构造 `ConversationSummary` 时 `used_reasoning=bool(row["used_reasoning"])`（NULL→False）。

### 2. 前端：历史卡片「✦ 推理」标记

- `frontend/app/page.tsx` 类型 `ConversationSummary`（[:123](../../../frontend/app/page.tsx#L123)）增加 `used_reasoning?: boolean`。
- 卡片主体（[:2264-2267](../../../frontend/app/page.tsx#L2264)）在 `{formatRelativeTime(updated_at)} · {turn_count} 轮` 一行内，当 `session.used_reasoning` 为真时追加一枚紧凑静态 pill「✦ 推理」。
- 样式：新增 `.chat-session-reasoning-badge`，色调与 `.reasoning-toggle` 一致（沿用 ✦ 字面字符，非图标），尺寸压缩成 badge。定位现有样式表中 `.reasoning-toggle` 所在文件并就近添加。

### 3. 前端：恢复会话默认开启推理按钮

- 新增纯函数 `lastTurnUsedReasoning(turns)`，放进一个可被 `node --test` 覆盖的小模块（如 `frontend/app/session-reasoning.ts`）：返回 `turns` 最后一项的 `response.reasoning_trace?.length ? true : false`（空数组/缺失→false）。
- `openSession`（[:1529](../../../frontend/app/page.tsx#L1529)）在 `setTurns(...)` 后调用 `setReasoningMode(lastTurnUsedReasoning(detail.turns))`：推理会话→按钮开，快速会话→按钮显式关。

## 边界与取舍

- **推理但最终 trace 为空**的容错极端情况会被算作快速（方案 A 已知代价，罕见，可接受）。
- `startNewSession`（[:1541](../../../frontend/app/page.tsx#L1541)）**不改**：新会话保持用户当前的推理按钮选择，用户未要求重置，YAGNI。
- 空会话（0 轮）：`used_reasoning=false`，恢复时 `lastTurnUsedReasoning([])=false`，按钮关。
- 卡片标记（后端 `used_reasoning`，看最后一轮）与恢复按钮（前端 `lastTurnUsedReasoning`，看最后一轮）规则一致，不会出现「标了推理但恢复成关」的不一致。

## 测试

- **后端** `backend/tests/test_conversations.py`：在同一会话内依次落 answer，断言 `list_conversations()[*].used_reasoning` 反映**最后一轮**：
  - 单条快速 → `False`；单条推理 → `True`。
  - 快速 → 推理（末轮推理）→ `True`；其后再追加快速 → `False`。
- **前端** 新增 `frontend/app/session-reasoning.test.mjs`（`node --test`）覆盖 `lastTurnUsedReasoning`：空列表 / 末轮快速（无 trace）/ 末轮推理（非空 trace）/ 末轮 trace 为空数组 四种。

## 不在本次范围

- 不加 `answers.mode` 列、不做数据迁移。
- 不改 `startNewSession` 的按钮行为。
- 不做按会话级以外（如逐轮在卡片上展开）的更细粒度标记——逐轮推理轨迹在回答区已有 `ReasoningTracePanel` 呈现。
