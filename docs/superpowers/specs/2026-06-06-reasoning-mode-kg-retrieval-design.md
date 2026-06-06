# 推理模式 Agentic KG 检索 — 设计文档

- 日期: 2026-06-06
- 状态: 已通过 brainstorming,待 review → writing-plans
- 范围: 在 silicon_notebook 后端新增一条独立的「推理模式」KG 检索路径

---

## 1. 背景与动机

当前 `/notebooks/{id}/ask` 走的是一条**固定启发式 pipeline**(`SQLiteRepository.ask`):

1. (可选) follow-up 改写 (`_rewrite_followup_query`,仅当问题 < 12 字或含指代标记)
2. 关键词 + 向量混合打分,4 类对象分别算 (`score_knowledge`)
3. 启发式意图识别 (`is_process_query`,匹配 "流程/步骤/flow" 等词) → 调类型权重
4. 过程配额保证 (`ensure_procedure_quota`)
5. 固定 1 跳图展开
6. LLM 合成答案 + 证据三档分类 (`_answer_kg` / `classify_evidence`)

这条 pipeline 里唯一让 LLM 参与"决策"的只有 follow-up 改写,其余全部写死。用户反馈三个痛点:

- **召回不准** — 类型选错 / 关键词漏 / 语义偏,答案缺关键内容
- **复杂问题覆盖不全** — 一个问题涉及多概念或要顺关系链走几跳,单次检索 + 固定 1 跳覆盖不到
- **策略太死板** — 不同问题该用不同检索方式,现在一刀切

核心诉求:**KG 是必经路径(100% 用 KG),但让模型根据问题自己决定怎么检索 KG。**

## 2. 目标 / 非目标

### 目标
- 新增独立的「推理模式」(`mode="reasoning"`),用户自选,**质量优先、允许多次 LLM 往返**
- 让模型决策检索策略:子查询分解、查哪些类型、关键词/语义偏好、是否顺关系链深挖、挖多深
- 三层兜底保证"用了知识库":KG 节点 → 原文段落 → inferred 标注
- 把 agent 的推理过程作为**可折叠轨迹**返回前端
- **最大化复用**现有检索原语,不另造检索器

### 非目标
- 不改动现有快速 pipeline(`mode="fast"` 保持原样,作为默认)
- 不引入原生 function calling(后端模型能力不确定),用手搓 JSON-action 循环替代
- 不做完全自由的 ReAct agent(flash 级模型易跑偏);采用结构化骨架 + 局部自由
- 不引入图数据库 / 向量库等新基础设施;继续用 SQLite + numpy

## 3. 关键决策(含理由)

| # | 决策 | 理由 |
|---|---|---|
| D1 | 独立「推理模式」,而非替换 fast pipeline | 用户要"用户自选、舍得花时间换质量";fast 路径仍服务低延迟场景 |
| D2 | 混合架构:结构化骨架 + Reflect 阶段自由深挖 | 骨架防跑飞、可观测、好调试;自由度留在最该灵活的图遍历 |
| D3 | 手搓 JSON-action 循环(每步 LLM 输出决策 JSON,代码执行) | 现有 `chat_json` 无原生 tool calling;不依赖后端模型支持 tool use |
| D4 | 深挖跳数**由模型每轮 Reflect 自主决定**,无写死跳数上限 | 用户明确要求;自由度本就该留在图遍历 |
| D5 | 护栏只做 circuit breaker(去重防环 + 总步数上限),非策略上限 | 防模型 bug/图环导致死循环;质量优先的正常对话撞不到 |
| D6 | follow-up 改写 + 意图识别 **吸收进 Plan** | 模型规划时天然消解指代、天然知道该查 procedure,比写死启发式准 |
| D7 | 兜底链 KG节点 → 原文段落 → inferred | 复用现有 `score_elements` + 三档分类;"100% 用 KG"= 必先查库,查不到才标推断 |
| D8 | LLM 调用失败/超时 → 降级现有 fast pipeline | 推理模式永不因 LLM 抖动而崩,保证可用性 |
| D9 | 答案合成复用 `answer_prompt`/`_answer_kg`/`classify_evidence` | 引用锚点、证据三档语义与 fast 路径一致,前端无需区分 |

## 4. 架构总览

```
AskRequest(mode="reasoning")
   │
   ▼  ReasoningRetriever.run(question, history)
   │
 [1] Plan ──── LLM 看「问题+历史」,拆成 1~N 个子查询
   │           每个子查询自带策略: {query, types, prefer, reason}
   │           (follow-up 指代消解 & 意图判断 都被这步吸收)
   ▼
 [2] Retrieve ── 代码按计划执行(复用 score_knowledge + 向量),每子查询取候选节点
   │
   ▼
 [3] Reflect ── LLM 看候选摘要,判断「够不够」,输出 next_action:
   │              ├─ answer          → 够了,去 [5]
   │              ├─ expand_graph    → 顺某条关系链取邻居(深度模型自定),回 [3]
   │              ├─ add_subquery    → 补一个新子查询,回 [2]
   │              └─ search_elements → KG 太弱,降级查原文段落,回 [3]
   ▼  (去重已访问节点防环;总步数撞 circuit breaker 才强制去 [5])
 [5] Answer ── 复用 _answer_kg + classify_evidence 合成
               → grounded/overview/inferred 三档 + 引用锚点 + reasoning_trace
```

### 痛点 → 设计对应

| 痛点 | 现状(写死) | 推理模式 |
|---|---|---|
| 策略太死板 | `is_process_query()` 关键词匹配定权重 | **Plan 让模型判断**查哪些类型、关键词还是语义 |
| 复杂覆盖不全 | 单次检索 + 固定 1 跳 | **多子查询分解 + Reflect 自由深挖**(深度模型自定) |
| 召回不准 | 固定 0.4/0.6 融合权重 | **per子查询 prefer 策略 + Reflect 补检索 + 原文降级** |

## 5. 组件详细设计

### 5.1 入口与开关
- `AskRequest` 新增 `mode: Literal["fast", "reasoning"] = "fast"`
- `AskResponse` 新增 `reasoning_trace: Optional[List[TraceStep]] = None`(fast 路径恒为 None)
- `routes.py::ask` 按 mode 分流:`fast` → 现有 `SQLiteRepository.ask()`;`reasoning` → 新 `SQLiteRepository.ask_reasoning()`(内部委托 `ReasoningRetriever`)

### 5.2 KG 工具箱
对现有纯函数的薄包装,输入输出均为结构化 dict。**由 loop 各阶段按需调用,不暴露给模型自由调**(模型只输出决策 JSON 指挥代码调哪个)。

| 工具 | 包装 | 返回 |
|---|---|---|
| `search(query, types?, prefer?)` | `score_knowledge` + 向量矩阵 | `[{id, type, name, snippet, relevance}]` |
| `neighbors(object_id, edge_type?, direction?)` | `relations_for_notebook` | 邻居节点列表(同 search 形态) |
| `get(object_id)` | `node_context` | 节点全文:定义 / steps / evidence 原文 |
| `search_elements(query)` | `score_elements` | 原文段落 `[{element_id, source_title, location_label, text, score}]` |

`prefer ∈ {keyword, semantic, balanced}`:映射到融合权重——`keyword` 提高 `W_KEYWORD`、`semantic` 提高 `W_SEMANTIC`、`balanced` 用现有默认。具体映射在 `ReasoningRetriever` 内做,不改 `retrieval.py` 的全局常量。

**relevance 口径统一**:`neighbors` 取回的图邻居、`search_elements` 取回的原文段落,统一用**原始用户问题**(非子查询)重新过一遍 `score_knowledge` / `score_elements` 赋 `relevance`。这样所有累积候选的 `relevance` 口径一致,`classify_evidence` 的 grounded/overview/inferred 分档才不被"子查询打分"扭曲。

### 5.3 三个 LLM 决策点
全走现有 `OpenAICompatibleClient.chat_json`(JSON 输出 + schema hint)。

**Plan** — 输入:问题 + 对话历史;输出:
```json
{
  "sub_queries": [
    {
      "query": "改写后的独立可检索查询(已消解指代)",
      "types": ["concept" | "claim" | "formula" | "procedure"],
      "prefer": "keyword" | "semantic" | "balanced",
      "reason": "为什么需要这个子查询"
    }
  ]
}
```

**Reflect** — 输入:问题 + 当前候选摘要(节点名/类型/relevance/已走关系);输出:
```json
{
  "sufficient": false,
  "next_action": "answer" | "expand_graph" | "add_subquery" | "search_elements",
  "expand": { "object_id": "...", "edge_type": "关系类型或null", "direction": "out|in|both" },
  "new_sub_query": { "query": "...", "types": ["..."], "prefer": "...", "reason": "..." },
  "elements_query": "降级原文检索用的查询",
  "reason": "本步决策理由(写入 trace)"
}
```
- `expand` 仅当 `next_action=expand_graph` 时必填,其余为 null
- `new_sub_query` 仅当 `next_action=add_subquery` 时必填
- `elements_query` 仅当 `next_action=search_elements` 时必填

**Answer** — 复用现有 `answer_prompt`;context block 由 agent 累积的候选(KG 节点 + 降级原文段落)拼成;输出 `{answer, grounded}`,锚点解析与 fast 路径一致。

### 5.4 Agent Loop 控制流(伪代码)
```python
def run(question, history):
    trace, candidates, visited, elements, steps = [], {}, set(), [], 0

    # [1] Plan —— 吸收 follow-up 改写 + 意图识别
    plan = llm_plan(question, history)
    trace.append(TraceStep("plan", summary, detail=plan))

    # [2] 初始 Retrieve
    for sq in plan.sub_queries[:MAX_SUBQUERIES]:
        merge(candidates, tools.search(sq.query, sq.types, sq.prefer))
    trace.append(TraceStep("retrieve", ...))

    # [3]/[4] Reflect 循环(深度由模型决定;步数撞上限才停)
    while steps < MAX_STEPS:
        steps += 1
        d = llm_reflect(question, summarize(candidates, elements))
        trace.append(TraceStep("reflect", d.reason, detail=d))
        if d.next_action == "answer" or d.sufficient:
            break
        if d.next_action == "expand_graph":
            oid = d.expand.object_id
            if oid in visited:           # 去重防环
                continue
            visited.add(oid)
            merge(candidates, tools.neighbors(oid, d.expand.edge_type, d.expand.direction))
            trace.append(TraceStep("expand", ...))
        elif d.next_action == "add_subquery":
            merge(candidates, tools.search(**d.new_sub_query))
            trace.append(TraceStep("retrieve", ...))
        elif d.next_action == "search_elements":   # 兜底第 2 层
            elements += tools.search_elements(d.elements_query)
            trace.append(TraceStep("fallback", ...))

    # [5] Answer —— 复用合成 + 证据三档
    context = build_context(candidates, elements)
    answer, grounded = llm_answer(question, context, history)
    level, _ = classify_evidence(top_hits, anchors, grounded, tau_low, tau_high)
    trace.append(TraceStep("answer", ...))
    return AskResponse(answer=answer, evidence_level=level, ..., reasoning_trace=trace)

# 任意 llm_* 抛错/超时 → except 块降级到现有 fast pipeline ask(),trace 标注"推理模式降级"
```

### 5.5 护栏(circuit breaker,非策略上限)
- **节点去重**:`visited` 集合,同一 `object_id` 不重复 `expand`,顺带防图环
- **总步数上限**:`reasoning_max_steps`(默认 50),撞到即强制进入 Answer
- **子查询数上限**:`reasoning_max_subqueries`(默认 5),Plan 输出超出则截断
- **失败降级**:任何 LLM 调用异常 → 降级 fast pipeline(D8)
- **超时**:复用现有 `openai_compat_timeout_seconds`,不新增
> 质量优先的正常对话远撞不到这些上限;它们只在模型 bug / 图环 / 网络异常时兜底。

### 5.6 推理轨迹
```python
@dataclass
class TraceStep:
    step_type: Literal["plan","retrieve","reflect","expand","fallback","answer"]
    summary: str          # 人话,如 "顺『依赖』关系找到 5 个邻居"
    detail: dict          # 结构化:子查询 / 命中 id / 决策 reason
```
逐步追加,随 `AskResponse.reasoning_trace` 返回,前端折叠展示。

## 6. 数据流
`question + history` → Plan(LLM) → sub_queries → search(KG) → candidates → Reflect(LLM) →〔expand_graph→neighbors(KG) | add_subquery→search(KG) | search_elements→原文〕循环累积 candidates/elements → build_context → Answer(LLM) → classify_evidence → `AskResponse{answer, evidence_level, citations, related_knowledge, reasoning_trace}`。

## 7. 错误处理与兜底
- **LLM 失败/超时**:降级现有 fast pipeline,trace 末尾标注降级原因
- **KG 检索空**:Reflect 走 `search_elements` 降级原文;仍空 → Answer 得 `inferred` 档,明确告知"知识库未覆盖"
- **坏 JSON**:Plan/Reflect 解析失败 → 容错(Plan 失败→单子查询=原问题;Reflect 失败→当作 `answer` 收尾),不抛到用户
- **撞步数上限**:用已累积候选直接合成,证据档自然落到 overview/inferred

## 8. 复用 vs 新增

**复用(不改动)**:`score_knowledge` / `score_elements` / `relations_for_notebook` / `node_context` / `classify_evidence` / `answer_prompt` / 向量矩阵(`_vector_matrix`,`query_sims`) / `OpenAICompatibleClient.chat_json`

**新增**:
- `backend/app/services/reasoning_retrieval.py` — `ReasoningRetriever` 类 + KG 工具箱包装 + loop 控制流
- `prompts.py` — `plan_prompt()`、`reflect_prompt()`(Answer 复用现有)
- `schemas.py` — `TraceStep`;`AskRequest.mode`;`AskResponse.reasoning_trace`
- `sqlite_repository.py` — `ask_reasoning()`(委托 ReasoningRetriever)
- `routes.py` — mode 分流
- `config.py` — `reasoning_max_steps=50`、`reasoning_max_subqueries=5`

## 9. 配置项(新增到 Settings)
| 配置 | 默认 | 含义 |
|---|---|---|
| `reasoning_max_steps` | 50 | Reflect 循环总步数 circuit breaker |
| `reasoning_max_subqueries` | 5 | Plan 输出子查询数上限 |

(深挖跳数不设配置 — 由模型自主决定,见 D4)

## 10. 测试策略
- **单测**:4 个工具包装函数(mock repo,验证对原语的正确委托与返回形态)
- **单测**:Plan/Reflect 的 JSON 解析与坏 JSON 容错(降级路径)
- **单测**:`prefer` → 融合权重映射
- **集成测**:reasoning loop 端到端(mock `chat_json` 喂固定 plan/reflect 序列),覆盖:
  - 正常 plan → 直接 answer
  - reflect → expand_graph → answer(验 trace 含 expand 步)
  - reflect → search_elements 降级 → inferred 档
  - 撞 `reasoning_max_steps` 强制收尾
  - LLM 抛错 → 降级 fast pipeline
- **复用**现有 `test_retrieval` / `test_followup_retrieval_grounding` 的 mock 模式

## 11. 未来扩展(本期不做)
- Plan 阶段先 `overview` 勘探 KG 地形(列高频概念)再规划
- 候选并行检索加速(当前串行)
- 把推理模式收益反哺 fast pipeline(如用轻量分类器替代 `is_process_query`)
- 完全自由 ReAct 模式(方向②)作为"专家档"扩展位
