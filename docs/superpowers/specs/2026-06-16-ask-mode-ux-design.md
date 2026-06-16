# 问答 mode 体系 + 前端呈现 设计

日期：2026-06-16
状态：已与用户对齐全部关键决策（菜单结构 / 严格推理双引擎 / 后端真源+契约校验 / 端点统一走 stream）
关联：`docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md`（本设计落地其 §4.6「路由 + 前端」与 P5）

## 1. 背景与根因

后端 ask 路由（`backend/app/services/sqlite_repository.py` 的 `ask()`，约 4231 行）已支持多 mode，但前端从未系统设计，刚踩坑（PR #45）。实证根因有三：

1. **前端写死过时 mode**：`frontend/app/page.tsx:1553` 用三元式 `useReasoning ? "reasoning" : "chunk"` 硬编码 mode；PR #45 之前写死的是已过时的 `"fast"`，导致 UI 长期没走过 chunk-native 默认路径。`fast/graph/global` 在 UI 上完全无入口。
2. **后端静默 fall-through 到 fast**：`ask()` 对 `chunk/reasoning/graph/global` 各有分支，**任何未匹配值（含 `fast`、含拼错）都落到最后那段旧 KG claim 检索**——即表现最差的路径。无校验、无报错。
3. **端点×模式分裂且默认值不一致**：`reasoning` 走 `/ask/stream`（流式 + trace），其余走 `/ask`（非流式）；前端按 `useReasoning` 分两条调用路径。`ask_stream` 里两处 `getattr(payload, "mode", "fast")`（`routes.py:422`、`:431`）默认仍是 `"fast"`，与 schema/`ask()` 的 `"chunk"` 不一致——目前因 payload 恒带 mode 没爆，是颗雷。

**结论**：mode 体系缺「单一真源 + 跨端校验」，缺「显式合法性闸门」，端点与模式耦合分裂。需把可用 mode 收成后端 canonical registry，前端只持有展示、id 对齐后端并由 smoke 契约校验，端点统一走 stream。

## 2. 已确认的关键决策

1. **用户可见模式集 = chunk + 严格推理（两档菜单）**。`fast` 从 UI 撤出（保留后端代码供 eval/兼容，仅显式 mode 可达）；`global` 内部化/待评估（spec「定去留」），不进菜单。
2. **严格推理含两引擎**：顶层 `通用问答 / 严格推理` 两档；选严格推理后二级选 `深挖推理(reasoning，默认) | 图谱多跳(graph)`。
3. **后端真源 + 契约校验**：后端 mode registry 单一真源（dispatcher 与校验同读）；未知 mode 显式 422；前端 typed const 持有标签/文案、id 对齐后端，由 smoke 断言两端 id 集一致。
4. **端点统一走 `/ask/stream`**：所有 mode 走流式端点，统一 loading/trace 通道；reasoning/graph 发 trace/子图，chunk 暂包成 `start→final`（不逐 token）。`/ask` 非流式保留供 eval/程序化。
5. **严格推理依赖 KG，不静默降级**：无 KG 时提示需先建 KG（本设计只消费 KG 信号，不设计构建流程——构建属 chunk-native spec P4「KG 抽取开关化」）。

## 3. 模式总览

| mode | 角色 | 用户可见 | 端点 | 需 KG | user_facing |
|---|---|---|---|---|---|
| `chunk` | 通用问答（默认） | ✅ 顶层「通用问答」 | stream | 否 | true |
| `reasoning` | 深挖推理（agentic + trace） | ✅ 严格推理→**深挖推理**（默认引擎） | stream | 是 | true |
| `graph` | 图谱多跳（关联子图） | ✅ 严格推理→**图谱多跳** | stream | 是 | true |
| `fast` | 旧 KG claim 检索 | ❌ 撤出 UI（仅显式 mode 可达，eval/兼容） | —（dispatch 可达） | 是 | false |
| `global` | GraphRAG 全局综述 | ❌ 内部/待评估 | —（dispatch 可达） | 是 | false |

前端菜单两级：`通用问答 | 严格推理`；选严格推理展开 `深挖推理(默认) | 图谱多跳`。

## 4. 详细设计

### 4.1 后端 mode registry（单一真源）

新模块 `backend/app/services/ask_modes.py`，定义 canonical registry：

```python
@dataclass(frozen=True)
class AskMode:
    id: str
    handler: str        # repository 上的方法名，如 "ask_chunk"
    group: str          # "general" | "strict" | "legacy" | "global"
    streaming: bool     # handler 是否支持 on_trace（决定 stream worker 是否传回调）
    requires_kg: bool
    user_facing: bool

ASK_MODES: dict[str, AskMode] = {
    "chunk":     AskMode("chunk",     "ask_chunk",     "general", False, False, True),
    "reasoning": AskMode("reasoning", "ask_reasoning", "strict",  True,  True,  True),
    "graph":     AskMode("graph",     "ask_graph",     "strict",  True,  True,  True),
    "fast":      AskMode("fast",      "ask_fast",      "legacy",  False, True,  False),
    "global":    AskMode("global",    "_ask_global",   "global",  False, True,  False),
}
DEFAULT_MODE = "chunk"
```

- **提取 `ask_fast`**：把 `ask()` 当前的 fallthrough 旧 KG 检索段（约 4243+ 行）抽成独立方法 `ask_fast`，使 registry 能按 handler 名引用，并消除 fallthrough 语义。
- **dispatcher 改写**：`ask()` 由 if-链改为查表分发：

```python
def ask(self, notebook_id, payload):
    spec = ASK_MODES.get(payload.mode)
    if spec is None:
        raise UnknownAskMode(payload.mode)   # 路由层转 422
    return getattr(self, spec.handler)(notebook_id, payload)
```

- **handler 签名统一**：所有 `ask_*` 接受可选 `on_trace: Callable | None = None`；仅 reasoning/graph 实际使用，其余忽略。这样 stream worker 可对任意 streaming handler 统一传回调。
- **graph 的 `streaming` 待核**：上表暂置 `True`（假设 `ask_graph` 可经 `on_trace` 发子图）。若现实现尚未支持回调，置 `False`——行为同 chunk 的 `start→final`，子图随 final response 返回，待补 trace 再翻 `True`。planning 时核 `ask_graph` 是否接 `on_trace`。

### 4.2 校验与 `/ask-modes`（合法性闸门 + 契约出口）

- **路由层校验**：`/ask` 与 `/ask/stream` 进入即校验 `payload.mode in ASK_MODES`，否则 `HTTPException(422, detail={"error":"unknown mode","valid":[...]})`。`AskRequest.mode` 默认仍 `chunk`，另加 pydantic validator 引用 registry keys 兜底。
- **清雷**：`routes.py:422`、`:431` 两处 `getattr(payload,"mode","fast")` → 直接用 `payload.mode`（schema 默认已是 chunk）。
- **新增 `GET /ask-modes`**：返回 user_facing 子集，仅含 `id / group / requires_kg / streaming`（**文案不在此**，留前端）。供前端契约校验与任何非前端消费者。

### 4.3 端点统一：全部走 `/ask/stream`

- **stream worker 改写**（`routes.py:429`）：按 registry 取 handler，`spec.streaming` 为真则传 `on_trace`，否则直接调用后 `start→final`：

```python
def worker():
    spec = ASK_MODES[payload.mode]            # 已在入口校验
    handler = getattr(repo, spec.handler)
    response = handler(notebook_id, payload, on_trace=on_trace) if spec.streaming \
               else handler(notebook_id, payload)
    events.put({"event": "final", "response": response.model_dump()})
```

  起始 `progress.start` 的 `detail.mode` 用 `payload.mode`（删掉 `"fast"` 默认）。
- **chunk 的 loading**：最小版只 `start→final`（无 token 流式）。**可选增强**（不阻塞本设计）：把 `ask_chunk` 的 `ask_stage`（检索/MMR/综合）经 progress 通道转发，给非 token 流式的「思考中」实时感——需 `ask_chunk` 接 `on_progress` 形参，留实现期定。
- **`/ask` 保留**：非流式端点继续存在，供 eval（`backend/tests/eval/test_ask_latency.py`）/ 程序化调用，与 stream 共用 §4.1 的 registry 分发。
- **前端统一**：`runAsk` 删掉 `useReasoning ? readAskStream : api` 分支（`page.tsx:1554`），所有 mode 统一 `readAskStream`。trace 面板对无 trace 的 mode 自然不渲染（`reasoning_trace` 空）。

### 4.4 前端 typed const + 两级控件

- **`frontend/app/ask-modes.ts`（新）= 唯一出现 mode 字面量处**：

```ts
export type AskModeId = "chunk" | "reasoning" | "graph";
export type AskModeGroup = "general" | "strict";
export interface AskModeDef {
  id: AskModeId; group: AskModeGroup;
  label: string; desc: string;
  requiresKg: boolean; groupDefault?: boolean;  // 组内默认引擎
}
export const ASK_MODES: AskModeDef[] = [
  { id:"chunk",     group:"general", label:"通用问答", desc:"…", requiresKg:false, groupDefault:true },
  { id:"reasoning", group:"strict",  label:"深挖推理", desc:"…", requiresKg:true,  groupDefault:true },
  { id:"graph",     group:"strict",  label:"图谱多跳", desc:"…", requiresKg:true },
];
```

  （上方 `label`/`desc` 的 `"…"` 仅占位，实际文案见 §4.7，集中维护避免重复。）其余代码（payload 构造、控件渲染、恢复）只引用 const，禁止内联 mode 字面量。
- **状态扁平**：`const [askMode, setAskMode] = useState<AskModeId>("chunk")`。两级是「按 `group` 渲染」的呈现，不是两个 state。`payload.mode = askMode`。
- **控件**：替换现「✦推理」单 toggle（`page.tsx:2474`）为输入条内 mode 控件——
  - 主分段：`通用问答 | 严格推理`（严格推理 = group `strict`）。
  - 选严格推理时内联出现子分段 `深挖推理 | 图谱多跳`（默认 `groupDefault` = reasoning）。切回通用问答收起。
  - 空间紧张可退化为下拉/popover，留实现期定（不改语义）。
  - `pendingReasoning` 等推理态 UI 泛化为「当前 askMode 是否 strict」。

### 4.5 严格推理依赖 KG 的门控

- reasoning + graph 都基于 KG（`requires_kg=true`），无 KG **不静默降级**。
- **KG 信号**：优先复用 `NotebookSummary.counts`（KG 对象类型计数之和 > 0 视为有 KG）；若 counts 不含 KG 计数，则 `NotebookSummary` 新增 `kg_ready: bool`。
- **无 KG 行为**：严格推理选项仍可见（教育用户该能力存在），但 `askMode∈strict && !kgReady` 时，发送前内联提示「该 notebook 尚无知识图谱，严格推理需先构建」+「构建 KG」CTA，**不发请求**。构建动作挂到 chunk-native spec P4 的 KG 开关化入口（本设计只消费信号、给 CTA，不设计构建流程）。
- **确认点**（planning 时核）：`ask_reasoning` 对 KG 的硬依赖程度与无 KG 时的实际行为，确保前端门控与后端一致。

### 4.6 会话持久化 / 恢复

- **`AskResponse` 增 `mode: str = ""`**（`schemas.py:174`）；各 `ask_*` 回填 `response.mode = <自身 id>`。
- 经 `ConversationTurn.response`（`schemas.py:209`）自然落库；`_save_answer` 若按序列化 response JSON 存则透明，若按列存则加 `mode` 列（确认点，planning 核 `_save_answer`）。
- **`openSession`** 读最后一轮 `turn.response.mode` 精确恢复 `askMode`（含引擎），替换现 `lastTurnUsedReasoning`（`page.tsx:1587`）的脆弱猜测。
- **退役** `frontend/app/session-reasoning.ts` 及其测试（启发式不再需要）。

### 4.7 文案（前端 const 内）

- **通用问答**：默认。大范围检索原文，适合综述、对比、找事实。
- **严格推理**：基于知识图谱的严格推导，带推导链/关联，需先建知识图谱。
  - **深挖推理**：agent 多轮深挖，展示思考轨迹。
  - **图谱多跳**：沿知识图谱多跳遍历，展示关联子图。

## 5. 数据流

```
前端问答（统一）：
  askMode(const) → payload.mode → POST /ask/stream
    → 路由校验 mode ∈ ASK_MODES（否则 422）
    → worker 查 registry 取 handler
        ├ streaming(reasoning/graph): handler(on_trace) → progress(trace/子图)… → final
        └ 非streaming(chunk): handler() → start → final
    → 前端 readAskStream 统一渲染（trace 面板按 reasoning_trace 有无自适应）
  response.mode 落库 → openSession 精确恢复 askMode

严格推理无 KG：
  askMode∈strict && !kgReady → 前端拦截 → 提示 + 建 KG CTA（不发请求）
```

## 6. 错误处理 / 降级

- 未知/拼错 mode → 422，列出合法值（不再静默落 fast）。
- 严格推理无 KG → 前端门控拦截 + CTA；万一绕过到后端，handler 按 chunk-native spec §4.6 返回明确提示（不静默降级）。
- stream handler 抛错 → 现有 `events.put({"event":"error",...})` 通道不变。
- `/ask-modes` 不可达（极端） → 前端用 const 兜底渲染（const 是本地真源），契约校验只在 CI/smoke 红，不影响运行时。

## 7. 测试策略（离线优先）

- **registry 分发**：每个 mode 命中正确 handler；未知 mode → `UnknownAskMode` → 路由 422。
- **校验闸门**：`/ask` 与 `/ask/stream` 对非法 mode 均 422；合法 mode 放行。
- **契约校验**：`GET /ask-modes` 的 user_facing id 集 == 前端 `ASK_MODES` id 集（见 §10 接入 check.sh）。
- **端点统一**：chunk 经 `/ask/stream` 返回 `start→final`；reasoning/graph 发 trace/子图；`/ask` 非流式仍可用。
- **KG 门控**：`requires_kg` mode 无 KG → 前端拦截提示，不发请求；后端兜底提示不降级。
- **持久化/恢复**：存 `mode` → `openSession` 恢复精确到引擎（reasoning vs graph 可区分）。
- **前端无字面量**：grep 断言 `page.tsx` 等不再出现内联 mode 字符串（仅 `ask-modes.ts`）。

## 8. 迁移 / 兼容

- `AskRequest.mode` 默认仍 `chunk`，旧客户端不传 mode 行为不变。
- 历史会话无 `response.mode` → `openSession` 缺失时回退 `chunk`（不报错）。
- `fast`/`global` 显式调用仍工作（user_facing=false，仅退出菜单与 fallthrough）。
- `/ask` 端点不删，eval/test 不受影响。

## 9. 明确不做（YAGNI）

- chunk 逐 token 流式（打字机）——留后续，本次只统一通道。
- 自动问题分类路由——沿用 chunk-native spec：用户显式 mode。
- global 入 UI / 新建社区摘要——内部/待评估。
- 删除 fast/global 代码——仅降级为 `user_facing=false`。
- 后端动态下发菜单文案——文案留前端，后端只出 id/元数据。
- KG 构建流程 UI——本设计只消费 KG 信号 + 给 CTA，构建属 chunk-native spec P4。

## 10. 实施 phase 划分（供 writing-plans）

可独立交付、逐步上线，每 phase TDD + 离线测：

- **P1 后端 registry + 分发 + 校验**：`ask_modes.py`、提取 `ask_fast`、`ask()` 查表分发、路由 422 校验、清 `getattr(...,"fast")` 雷、`GET /ask-modes`。（纯后端，可独立测）
- **P2 端点统一**：stream worker 按 registry 分发、`ask_*` 统一 `on_trace` 形参、`/ask` 保留。
- **P3 持久化/恢复**：`AskResponse.mode` 回填 + 落库 + `openSession` 读 mode 恢复。
- **P4 前端 const + 两级控件**：`ask-modes.ts`、两级 mode 控件替换旧 toggle、`runAsk` 统一 `readAskStream`、删旧分支、退役 `session-reasoning.ts`。（依赖 P1 的 id、P2 的 stream）
- **P5 KG 门控**：KG 信号（counts 或 `kg_ready`）、无 KG 提示 + 建 KG CTA 挂钩。
- **P6 契约校验接入**：`scripts/check.sh` smoke 加一步——用 node/tsx 读 `ASK_MODES` id 集、curl `/ask-modes` 取 server id 集，断言相等，任一端加/改 mode 不同步即红。

## 11. 验证基线

- `scripts/check.sh` 全绿（py_compile + hermetic smoke + tsc）。
- 手动四态走查：通用问答 / 严格推理-深挖 / 严格推理-图谱 / 无 KG 选严格推理（提示+CTA，不发请求）行为正确。
- 会话恢复精确到引擎；未知 mode 422；前端无内联 mode 字面量。
- 生效需重启后端（逻辑改动）——交用户重启（见 [服务启停边界]）。
