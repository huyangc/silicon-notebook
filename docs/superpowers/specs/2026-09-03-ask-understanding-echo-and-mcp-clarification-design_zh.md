# 问答纠偏根因整改：合成限定词保真、推断标记、MCP 歧义文案（设计规格 **v2·草案**）

> **状态**：v2 草案，待评审。v1（「理解回显 + MCP 澄清往返」）经两路评审与一次生产离线归因后**整体撤回**，
> 本文是换方向后的重写；v1 评审结论以精简形式保留在文末「v1 评审记录」节，作为「为什么不做」的依据。
> 三项决策（D-1..D-3）列在「待拍板」节。本文只写设计，不改产品代码。

**v2 相对 v1 的实质改动**：
① 撤回全部浏览器「理解回显」（`AskUnderstanding` 字段、回答顶部理解块、自动路由合同下传）——生产数据里没有一条纠偏
是「系统理解错了问题」，且高级模式已有更强的可编辑合同审阅仍未消除纠偏；
② 撤回 MCP 两步澄清往返（`on_ambiguity` / `intent` 入参、`needs_clarification` 终态、D-4 跑模型理解）——评审 C4/C5/C7/C8/C9
证明协议在 job 时序、响应预算、必填字段与兼容性上都走不通，且数据不支持投入；
③ 新增 **T1 合成限定词保真**（生产 4 条纠偏里 3 条是「贴着问题答偏」）；
④ 新增 **T2 推断标记核查与呈现**（第 4 条是证据充分下的合成事实错误）；
⑤ MCP 改动缩成 **T3 歧义错误文案携带歧义问题并前移到建 job 之前**（评审 C9 的零兼容风险版本）；
⑥ 「纠偏」这条产品线的优先级从画像里的 P1 降到 P3，理由见「生产数据」节。

**基线口径**：全部现场引用按本 worktree `HEAD = 0ed6b674c`（与 `origin/master` 同点）逐条实测。
不涉及 schema 变更、不涉及 `AskResponse`/`AskRequest` 形状变更，OpenAPI 冻结夹具不动。

## 目标与非目标

目标：

1. 回答不得把问题里的限定词（范围、条件、方向、周期性、「只/仅/除了」、点名的子部件）静默泛化成更宽的问题；证据只覆盖邻近情形时必须明说。
2. 弄清 s28 那条「证据充分仍合成错误结论」的回答里推断标记是缺失还是不可见，按结果二选一整改。
3. MCP `ask_notebook` 与 HTTP 直连 `/ask` 遇确定性歧义时，错误文案带上具体歧义问题，并在任何持久状态建立之前失败。

非目标（本轮明确不做，理由见「生产数据」与「v1 评审记录」）：

- 不做浏览器回显、不给 `AskResponse` 加理解字段、不改自动路由、不给自动模式加澄清界面。
- 不给 MCP 加 `on_ambiguity`/`intent` 入参，不引入 `needs_clarification` 成功终态，不让 MCP reasoning 跑模型理解。
- 不做订正轮识别、上一轮证据接力、会话级约束注入（v1 评审 R-A 的替代建议）——4 条纠偏在下一轮全部自行修正，纠偏环路今天是通的，这些投资缺乏数据支撑。
- 不改检索范围语义、不改任何召回/重排/预算；不动深度报告的 `report_section_prompt`。

## 生产数据（2026-09-03，PostgreSQL 只读离线归因）

| 指标 | 值 |
| --- | ---: |
| answers 总数 / 会话数 | 321 / 127 |
| 非首轮数 | 194 |
| 纠偏轮数（正则识别） | 4（占非首轮 2.1%） |
| 纠偏轮 T1 引擎 | chunk 4/136，reasoning 0/38，插件 0 |
| 纠偏轮 T1 用户界面模式 | advanced 3/99，auto 1/95 |
| 根因标签 | (a) 检索命中邻近而非点名对象 3；(b) 追问丢对象 1（与一条 (a) 共现）；(c) 意图理解错 0；(d) 合成丢限定 0（限定词表太窄，实际「周期性」那条应归此类）；(e) 引擎降级 0；无标签 1（证据充分、合成事实错误） |
| 纠偏后下一轮仍纠偏 | 0/4 |
| 纠偏轮 T1 有 feedback | 0 |

四条脱敏样例的形态：

1. 问 trim 电阻串里的开关 → 答了另一处开关。（贴着问题答偏，子部件点名被丢）
2. 问底噪指标 → 答了动态指标；用户「我刚才指的是…」订正。（贴着问题答偏 + 追问对象漂移）
3. 问「周期性发出」→ 答成「发出」；用户「注意，是周期性发送」。（限定词被泛化）
4. `grounded=true`、相关度高于中位数，模型强加了「s28 需要 DAC 校准」的结论；用户逐条否定。（合成事实错误）

**从数据得出的判断。**

- 没有一条纠偏是「问题被理解错」。样例 1–3 的问题都清晰，系统理解对了，回答松了。v1 的回显对四条全部无效。
- 「reasoning 意图审阅把纠偏压到 0」**不采信**：0/38 对 4/136 的单侧超几何 p≈0.37，与随机无异；且有混杂（选 reasoning 的用户、问题类型、答案长度都不同）。
- 纠偏率 2.1% 且环路自愈，画像里「多轮澄清/主动引导 P1」高估了；「规格 vs 反标反复问 5 次」是更强的信号，另立规格。
- 样本只有 4 条，本文只做数据**指向**的最小改动，不做任何需要统计显著性支撑的投资。

## 摸底结论（按 `0ed6b674c` 复核，只保留 v2 用到的）

1. **三条合成路径共用一份 `answer_prompt`。** `backend/app/services/prompts.py:291`（`answer_prompt`），调用点 `ask_service.py:1760`、`:1818`（chunk 两种合成）与 `:2204`（`_answer_reasoning`，含分节合成）。规则 1–3 是 L0 引用/推断纪律（`prompts.py:328-338`），规则 4/8/9/10 是 L1 可优化片段（`prompt_layers.py:126-175`，每片段带「起始序号与结尾换行符属于契约」边界），规则 11 是集合枚举纪律（`prompts.py:362-380`），之后直接进 history/section/style 段与问题。**没有任何一条规则说「保留问题的限定词」。**
2. **chunk 合成用的是原始问题。** 检索用 `retrieval_query`（`ask_service.py:2477`），合成把原始 `question` 交给 `answer_prompt`（`:2689`）。限定词在合成侧是可见的，丢限定是模型行为，不是输入缺失。
3. **「（推断）」在前端是裸文本。** `rg 推断 frontend/app/` 只命中 `answer-panel.tsx:1526-1527` 的 `evidence_level` 标签（「概述（仅薄证据，余为推断）」/「推断（未命中笔记本依据）」）；`answer-markdown.tsx` / `answer-formatting.ts` / `globals.css` 对正文里的 `（推断）` 前缀无任何识别或样式。引用标记有专门的 remark 插件（`answer-citations.ts:23-84`）把 `[kN]` 变成可点 chip，推断标记没有对应物。
4. **确定性歧义闸有三处，文案都不带歧义问题本身。** HTTP 直连 `/ask`：`ask_routes.py:682-694`，`plan_query_intent(None, question, "", max_topics=1)` → `user_error(422, "问题仍有关键歧义，请先确认问题理解")`。引擎内兼容分支：`ask_service.py:1072-1074` 同文案 `ValueError`，但它在 `begin_job_current`（`:776-778`）之后触发，会留下一条 `status='failed'` 的 ask_job。MCP `ask_notebook`（`memory_context.py:395-447`）没有自己的闸，直接进 `repo.ask` 撞引擎内那条。`plan_query_intent` 返回的 `seed["ambiguities"]` 里每行都有 `question`（`query_intent.py:327-343`，如「你提到的对象具体是什么？请给出名称或简要背景。」），只是没被拼进文案。
5. **零松弛函数长度天花板。** `scripts/architecture_boundary_baseline.json:346` 钉 `register_memory_context_tools.ask_notebook = 157`；`ask_chunk = 399`。T3 会给 `ask_notebook` 加语句，须同 diff 更新基线。

## 设计

### T1 合成限定词保真（`answer_prompt` 新增规则 12）

在规则 11 之后、`\n\n` 之前追加一条 L0 规则（不进 `L1_FRAGMENTS`，理由见 D-1）：

```text
12. Preserve every qualifier the question states — scope, operating condition,
direction (e.g. TX vs RX), periodicity, 'only'/'except', a named sub-component —
exactly as asked, and answer the qualified question rather than its broader or
neighbouring form. If the knowledge items cover only the unqualified case or an
adjacent object, say so in one explicit sentence, keep any extrapolation to the
asked case marked （推断）, and never silently generalize the question or
substitute a related object.
```

- **位置**：追加在末尾而不是插到规则 3 旁边，因为 L1 片段的起始序号是契约（`prompt_layers.py` 每个片段的 boundary），中间插号会连带改四个片段的编号。
- **覆盖面**：三条合成路径自动继承（摸底 1）。分节合成（`sectioned=True`）同样走 `answer_prompt`，每节自己的问题带自己的限定词。
- **不改**：`report_section_prompt`（深度报告另有一套规则与评审），`followup_rewrite_prompt`、`expand_query_prompt`。限定词丢在合成侧，不在检索侧（摸底 2）。
- **与 L0 规则 2/3 的关系**：规则 12 不授权任何新的 `[k]` 绑定；「证据只覆盖邻近情形」的那句说明本身不带 `[k]`，对邻近情形的引用照常绑；对点名情形的外推按规则 2 标 `（推断）`。`grounded` 的判定不变。

**验证门**：`backend/tests/test_prompts.py` 新增断言（规则 12 在规则 11 之后、在 `history_section` 之前；`style_block` 仍落在规则与问题之间——沿用 `test_style_block_lands_between_the_rules_and_the_question_in_answer_prompt` 的定位方式）。仓库没有问答质量离线评测台（`scripts/` 只有 `kg_quality_audit.py` / `replay_retrieval.py`），因此 PR 须附一次真机 A/B：用样例 1–3 的**句式**各构造一问（点名子部件 / 周期性限定 / 方向限定），同一语料、同一引擎，贴规则 12 前后的回答摘要与是否出现「证据只覆盖…」的显式句。这是人工门，不是自动门，PR 说明里如实标注。

### T2 推断标记核查与呈现

**先诊断，后二选一。** 诊断由用户在生产库完成（本文不再派 agent）：取样例 4 的 T1 `answers.payload.answer`，看「s28 需要 DAC 校准」那句是否带 `（推断）` 前缀。

| 诊断结果 | 结论 | 整改 |
| --- | --- | --- |
| 有前缀 | 模型守了规则 2，用户没看见 | T2-a：前端给 `（推断）` 做可见样式 |
| 无前缀 | 模型违反规则 2，把推断写成事实 | T2-b：不改 prompt 文本（规则 2 已够明确），改为在 `test_prompts` 之外加一条真机回归样例进 PR 门，并把该问题句式收进 T1 的 A/B 集 |

**T2-a 设计**（只在诊断为「有前缀」时实施；D-2）：

- 在 `frontend/app/answer-citations.ts` 旁新增 `answer-inference.ts` remark 插件：文本节点里的 `（推断）` / `(推断)` / 行首 `Likely,` 前缀（与 L0 规则 2 的三种拼写一致）包成 `<span class="answer-inference">`，样式为弱化色 + 细边框小标签，不改文本内容、不改复制结果（`copyAnswer` 走 `renderTextWithReferenceNumbers`，与渲染树无关）。
- 不动 `evidence_level` 顶栏标签；两者语义不同（顶栏说整篇的证据等级，行内标记说这一句是推断）。
- 公开分享视图（`conversation_public_view`）复用同一渲染组件即同样生效；不新增字段。
- `docs/ui-vocabulary.md` 若已有「推断」词条则复用，没有则补一条。

### T3 歧义错误文案携带歧义问题，并前移到建 job 之前

**一份文案构造器**，三处闸共用：

```python
# app/services/query_intent.py
def clarification_gate_message(seed: dict) -> str:
    """'问题仍有关键歧义，请先确认问题理解：① …；② …。' 至多 8 行，每行 ≤ 500 字符（与 QueryIntentAmbiguity 上限一致）。"""
```

只拼 `ambiguities[*].question`（服务端确定性生成或模型生成的提问句），不拼 `reason`，不拼用户原文。

- **HTTP 直连闸**（`ask_routes.py:686-694`）：`user_error(422, clarification_gate_message(seed))`。这是 `docs/product-and-api_zh.md:1259` 已登记的「fail closed」路径，只改文案不改行为。
- **引擎内兼容分支**（`ask_service.py:1072-1074`）：`ValueError(clarification_gate_message(seed))`。保留作后备。**不删** `seed["mandatory_topics"] = []`（D-3；评审 C5：该分支同时被 HTTP 直连命中，删了会改直连答案且撞已登记契约）。
- **MCP `ask_notebook`**：在 `repo.ask` 之前、`_validate_ask_mode` 之后，对 `mode == "reasoning"` 执行与 HTTP 直连闸**同形**的检查：`plan_query_intent(None, question, "", max_topics=1)`，命中即 `raise ValueError(clarification_gate_message(seed) + " 把对象名写进问题后重试。")`。history 传空串与 HTTP 直连闸一致（确定性检测只看问题本身的指代词与泛化句式，`query_intent.py:17-27`），不需要评审 C10 担心的结构化历史读取口。前移后常见路径不再留下 failed job；引擎内那条只在两处判定不一致时兜底（理论上不会，`max_topics` 不影响 `needs_clarification`）。
- 工具描述追加一句：reasoning 模式对含未解析指代或纯泛化的问题会以错误返回，错误文案里列出需要补充的信息；把对象名写进问题后重试。
- 对 chunk 与插件模式**不加闸**（今天也没有）。

## 不做清单（v1 撤回项）

| v1 项 | 不做的理由 |
| --- | --- |
| `AskUnderstanding` + 回答顶部理解块 | 4 条纠偏无一是理解错；高级 reasoning 的可编辑合同审阅（`ask-intent-review.tsx:46-126`）是回显的严格超集，未消除纠偏；C1/C2 证明字段在两种模式上都对不上代码（chunk 检索用多条子查询，reasoning 自动确认把 `resolved_question` 降为「仅作补充」） |
| 自动路由合同下传 chunk（复用 `AskRequest.intent`） | 随回显撤回；且是公开契约扩面，`assumptions/ambiguities` 未校验即持久化渲染（C16） |
| MCP `on_ambiguity` / `intent` 入参、`needs_clarification` 终态 | C4 job 先于判定建立；C7 合同回传撞 12,000 字符总预算裁剪；C8 `resolved_question` 必填；C9 旧 Agent 取 `answer` 键会 KeyError |
| D-4 MCP reasoning 跑模型理解 | C5：同一分支被 HTTP 直连命中，改了会静默改直连答案且撞 `product-and-api_zh.md:1259` 已登记行为 |
| 自动模式对指代词的降级修正 | 结构上真实（「刚才」命中 `_UNRESOLVED_REFERENCE` 即落 chunk+standard），但数据里 0 次触发，且 4 条纠偏都在 chunk 上自愈；登记为已知行为，不修 |
| 订正轮识别 / 证据接力 / 会话级约束 | 纠偏率 2.1% 且 4/4 自愈，缺乏投入依据；若后续样本 ≥30 且 (b) 类占比上升再议 |

## 测试

后端：

1. `test_prompts.py`：规则 12 存在且位置正确（规则 11 之后、history 段之前）；`style_block` 定位测试不变；`test_answer_prompt_states_marker_and_inference_rules` 不受影响。
2. `test_query_intent.py`：`clarification_gate_message` 对 0 / 1 / 8 行歧义的输出形态；不含 `reason`、不含问题原文；行数与长度上限。
3. `test_memory_mcp.py`：reasoning + 指代不清 → `ValueError` 文案含具体歧义问题，且 `ask_jobs` 行数不变（回归 C4 的 failed job）；chunk + 同一问题 → 正常作答（不加闸）；reasoning + 清晰问题 → 行为与今天逐键相同。
4. `test_ask_routes`（既有直连测试处）：422 文案含歧义问题。
5. `scripts/architecture_boundary_baseline.json`：`ask_notebook` 长度同 diff 更新；`ask_chunk` 不动（T1 只改 prompts.py）。
6. `test_repository_api_contract.py`：不应变红（无形状变更）；若变红即本规格越界，停下来。

前端（仅 T2-a 触发时）：

7. `answer-inference` 插件的三种拼写识别；不误伤「推断」作为普通词出现在句中（只认句首前缀）；复制结果与渲染前逐字相同。

## 文档所有权（与实现同 PR）

- `docs/product-and-api.md` / `_zh.md`：「检索模式（问答）」补规则 12 一句话；`:1259` 段的 fail-closed 文案更新为「列出需补充的信息」；MCP 工具目录 `ask_notebook` 补歧义错误说明。
- `docs/agent-mcp-memory-sop.md` / `_zh.md`：第 9 节常见问题补「reasoning 报『问题仍有关键歧义』怎么办」。
- `docs/ui-vocabulary.md`：仅 T2-a 触发时补「推断」行内标记词条。
- `architecture.md`：不动（无架构变化）。
- 本文：定稿后把「生产数据」节的口径注记补上正则原文，供下次复跑对照。

## 待拍板

| # | 决策 | 建议 | 理由 |
| --- | --- | --- | --- |
| D-1 | 规则 12 放 L0 还是做成 L1 片段 | **L0** | 它是正确性纪律（不得偷换问题），与规则 2/3 同类；做成可覆盖片段等于允许部署把它关掉 |
| D-2 | T2-a 行内推断样式是否随本轮做 | **等诊断** | 诊断为「有前缀」才有意义；无前缀时做样式是空转 |
| D-3 | 是否保留 `seed["mandatory_topics"] = []` | **保留** | 评审 C5；改它是另一个已登记契约的变化，与本轮无关 |

## v1 评审记录（2026-09-03，已吸收）

两路只读评审（R-A 产品效果 / R-B 工程合同），全部断言按 `0ed6b674c` 查证，C2 / C4 / `conclusion` 语义三条经主评审二次抽查坐实。结论：R-A「换方向」，R-B「改后可实施但 C1/C2/C4–C9 是设计级重做」。生产归因随后确认 R-A 的判断。以下只保留仍对后续有约束力的条目：

- **A-P0-1** 高级 reasoning 已有可编辑合同审阅（`ask-intent-review.tsx:46-126`），是任何「回显」的严格超集；用户过闸仍纠偏。
- **A-P1-3** 自动模式对含「刚才/这个/那个」的订正句会落 chunk+standard（`query_intent.py:17-21, 100-119`；`ask_routes.py:532`）。登记为已知行为。
- **A-P1-6** 理解层 corpus-blind（`query_intent.py:1-6`），检测不到语料相关歧义；`agent_profile_block.py:16-20` 的硬边界挡住 `key_entities` 进理解层。若将来要做语料感知歧义，需独立拍板。
- **A 事实纠正** `conclusion` 是去掉 `[k]` 标记的完整回答（`ask_service.py:2802`），不是摘要。
- **C1/C2** chunk 检索用多条子查询与关键词（`ask_service.py:2509, 2533, 2626`）；reasoning 自动确认路径 `objective_is_authoritative=True`（`:3002-3009`，`query_intent.py:480-483`）。任何未来的「回显」都不能拿 `retrieval_query` 或 `contract.resolved_question` 当真值。
- **C4** MCP 走 `repo.ask` → `begin_job_current`（`ask_service.py:776-778`）先于引擎；引擎内抛错会留 failed job。T3 的前移就是回应这条。
- **C5** `_confirmed_reasoning_intent` 的 None 分支同时服务 HTTP 直连与 MCP；`product-and-api_zh.md:1259` 已登记其 fail-closed 行为。
- **C7** `_budget_response` 有 12,000 字符总预算收敛（`_shared.py:36, 481-490`），会削最长字符串；任何要「原样回传」的 MCP 协议都得绕开它。
- **C11** `AskResponse` 新字段须带 `exclude_if`，否则改每条历史 payload 字节；**C12** 零松弛函数长度基线；**C15** `understanding` 一词已被笔记本画像与深度报告占用。

## 已知遗留（不在本规格内）

- 「规格表 vs 反标指标」反复提问 5 次是当前最强的产品信号，需要 spreadsheet lane 的跨表对齐算子与 PDF 表格进快照，另立规格。
- 插件引擎（`ask.engine`）v1 端口不给对话历史；自动路由永远不落插件引擎（`niuma.analog` 只在高级模式可见）；两者各需独立决策。
- 纠偏归因的检测正则可能漏检（画像口径 ~5% vs 本次 2.1%）；样本 ≥30 条时应改为逐对人工判读并加 (f) 合成事实错误标签复跑。
