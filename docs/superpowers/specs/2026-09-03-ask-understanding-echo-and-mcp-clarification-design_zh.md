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

### T2 推断标记：传递规则 + 行内呈现（诊断结果：第三种，2026-09-03 用户在生产核实）

**诊断结果。** 原设计只列了「有前缀 / 无前缀」两种；生产回答的实际形态是第三种：支线（分层机理）里的句子带 `（推断）`，**结论段不带**。推断状态没有从前提传递到结论，结论读起来像已证事实。

**根因（按 `0836efdb` 复核）。** 所有推断规则都是逐句规则，没有一条要求「依赖推断的结论继承推断」：
- Ask `answer_prompt` L0 规则 2（`prompts.py:332-336`）只约束「当一句话是你自己的推断时」；规则 8 鼓励分层写机理、层间用 `（推断）` 桥接，模型最后写的「结论」段是对各层的归纳，按字面不是「一句推断」，于是既不挂 `[k]` 也不标 `（推断）`。
- 分节合成没这个问题：每节被明令禁止「为整篇答案写结论」（`prompts.py:283`），整篇没有结论段。所以出问题的是单次合成（chunk / mix / 未分节的 reasoning）与深度报告。
- 深度报告是 prompt 明文造成的：`report_summary_prompt`（`prompts.py:1163-1171`）要求执行摘要「direct answer first」「只用章节里已有的事实」「no citation markers」——章节里标了 `（推断）` 的发现被提炼进摘要时，标记随「no citation markers」一起丢，摘要把推断当直接答案写在最前面。章节侧 claim ledger 有 `type: inference`（`prompts.py:1107`），但摘要 prompt 只吃章节正文 `sections_block`，不吃 ledger。
- `conclusion` 字段不是原因：`MARKER_RE`（`citation_markers.py:13`）只剥 `[k]` 组，保留 `（推断）`。前端把 `（推断）` 当裸文本渲染（`rg 推断 frontend/app/` 只命中 `evidence_level` 顶栏标签），不放大也不缩小问题，只是让它更难被看见。

**T2-c 传递规则（三处 prompt 文本，全部 L0，不做 L1 片段）。**

1. `answer_prompt` 追加规则 13（规则 12 之后、`history_section` 之前，追加在末尾的理由同规则 12）：

```text
13. Inference status propagates. A conclusion, summary, 'therefore'/'so' sentence, or final recommendation that rests on any （推断） or 'Likely,' sentence is itself an inference: prefix it with （推断） (or 'Likely,' in an English answer) and attach NO [k]. Only a conclusion whose every premise is a [k]-cited sentence may be stated without the marker. Never let a closing section state as established fact what the body only inferred.
```

2. `report_section_prompt` 规则 2（`prompts.py:1063-1064`）扩成：

```text
2. When a sentence is your own inference bridging the items, prefix it with （推断） and attach NO [k]. A conclusion or in-section summary that rests on any （推断） or 【通识】 sentence is itself an inference: prefix it with （推断） and attach NO [k]; only a conclusion whose every premise is [k]-cited may omit it.
```

   规则 3/4 的编号与 `report_section.domain_conventions` 片段的「起始序号 3.」契约不动。`allow_parametric=False`（通识关闭）时前提列表只写 `（推断）`——既有契约 `test_report_prompts_contract` 要求关闭态 prompt 里不得出现 `【通识】`。

3. `report_summary_prompt`（`prompts.py:1163-1171`）在「no citation markers」之后补一段：

```text
（推断） and 【通识】 are NOT citation markers: keep them. A summary sentence distilled from a section finding that carries （推断） or 【通识】 keeps that marker at its start, and if the direct answer itself rests on such findings it opens with （推断）. Never promote an inferred or general-knowledge finding into an unmarked fact.
```

- 三处都不授权任何新的 `[k]` 绑定；`grounded` 判定（Ask 规则 3、报告规则 7）不变。
- **第二步（本轮不做）**：让 `report_summary_prompt` 同时吃 claim ledger 里 `type=inference` 的条目清单，让「哪些是推断」变成结构化输入而不是靠模型从正文里认。改动比 prompt 文本大，待本轮真机观察后决定。

**T2-a 行内呈现（既然前缀确实存在，样式就有意义了；D-2 已由诊断结果落定为「做」）。**

- 在 `frontend/app/answer-citations.ts` 旁新增 `answer-inference.ts` remark 插件：文本节点里**句首或段首**的 `（推断）` / `(推断)` / `Likely,`（与 L0 规则 2 的三种拼写一致）包成 `<span class="answer-inference">`，`【通识】`（报告规则 4）包成 `<span class="answer-general-knowledge">`；用 mdast `data.hName/hProperties` 产出 span，不改文本内容、不改复制结果（`copyAnswer` 走 `renderTextWithReferenceNumbers`，与渲染树无关）。只认句首/段首：标记前面（可隔空格/制表符）是文本起点、换行，或 `。；！？.;!?` 之一；本 text 节点起点不等于段首——`[k1]（推断）`、`**重点**（推断）` 这类被前一个兄弟节点切开的句中位置要回看兄弟节点判定。句中出现的普通词「推断」不动。
- **四个渲染面都接**：`AnswerMarkdown`（`answer-markdown.tsx`）、`ReportMarkdown`（`report-view.tsx`）、以及公开分享页 `c/[token]/page.tsx`、`r/[token]/page.tsx` 各自的 `ReactMarkdown` 实例（分享页**不**复用前两个组件，是独立实例）。新增守卫测试钉住「渲染模型产出文本的每个面都带 `remarkAnswerInference`」，与既有 `markdown-single-tilde-guard` 同一口径。不新增任何后端字段。
- 样式：弱化色 + 细边框小标签，与 `.cite-chip` 同一视觉家族但不可点；两个 class 各自一套，`【通识】` 用另一种色相区分「推断」与「通识」。不动 `evidence_level` 顶栏标签（顶栏说整篇的证据等级，行内标记说这一句是推断）。
- `docs/ui-vocabulary.md` **不**把这两个标记登进「界面词汇表」（那张表是 JSX 文案黑名单，登进去等于禁止标记出现在界面上），而是新增独立小节「答案正文内的标记」：说明它们是模型输出内容、界面呈现名为「推断」「通识」、渲染层只加样式；守卫 `test_ui_vocabulary_guard` 的扫描面不覆盖模型正文，无需豁免。

**T2-d 标记与列表语法的位置（2026-09-03 本地真机发现，PR #669 之后）。**

真机回答把推断标记写在列表序号**前面**：「（推断）1. 世界模型…」「（推断）2. …」，行间只有单换行。Markdown 不认「（推断）1.」是列表项，`.answer-markdown p` 也没有 `pre-wrap`，七条内容渲染成一整段连在一起的文字；标签本身能切出来（换行算句界），可读性没了。两层修：

1. **prompt（治源头）**：Ask 规则 2 的「(prefix with '（推断）' / 'Likely,')」之后、报告章节规则 2 的「prefix it with （推断） and attach NO [k]」之后、报告规则 4 的「must start with the marker 【通识】」之后，各补同一句位置要求：the marker opens the sentence but goes AFTER any list number, bullet, or heading syntax (write `1. （推断）…`, never `（推断）1. …`, so Markdown lists stay intact)。措辞三处一致，**例句里的标记随本规则替换**：规则 4 的例句写 `1. 【通识】…` / `【通识】1. …`（同一句例子在通识规则里写（推断）会让模型把通识条目错标成推断）。不动规则 12/13。
2. **前端归一（兜底，含历史回答）**：新增 `frontend/app/inference-list-markers.ts` 导出纯函数 `normalizeInferenceListMarkers(markdown)`：逐行处理，行首（允许至多 3 个空格缩进）若是四种标记之一（`（推断）`/`(推断)`/`Likely,`/`【通识】`），紧跟列表语法（`\d{1,9}[.)]` 或 `-`/`*`/`+`）或 ATX 标题语法（`#`×1–6）与至少一个空格或制表符，则只交换标记与列表语法两个 token 的位置，两段原有空白各自跟着自己后面的内容走（「（推断）1. 内容」→「1. （推断）内容」，与模型写对时的形态逐字相同；制表符原样保留），不合成任何字符；其它行逐字不动。围栏代码块（``` / ~~~）内的行不处理（模型在代码块里逐字引用反例时不能被改成正例），围栏状态函数内自维护，围栏可开在列表项那一行、闭合缩进上限随开启行的列表前缀宽度走、反引号围栏 info 含反引号不算围栏；4 空格及以上缩进（顶层是缩进代码块）与 `> ` 引用块前缀不处理，代价是列表项内部 4 空格缩进的子列表不归一，登记为已知覆盖缺口，不为它维护容器栈。守卫除 import 外还要求每个 `<ReactMarkdown>` 子表达式里真的存在该函数的调用（`tsconfig` 未开 `noUnusedLocals`，只查 import 会放过「留 import、改回单层调用」）。**只**接进四个模型文本渲染面（`AnswerMarkdown`、`ReportMarkdown`、`c/[token]`、`r/[token]`），与 `normalizeMathMarkdown` 串联；不接 knowhow 格子编辑器（那是用户内容）。守卫 `answer-inference-surface-guard` 增加一条：四个面都必须 import 该函数。CommonMark 允许以 `1.` 开头的有序列表打断段落，所以「（推断）以下为…：」下一行紧跟「1. （推断）…」也能成列表。
3. **不做**：让插件把「（推断）1.」自己解析成列表（插件跑在解析之后，只能切文本节点）；后端落库前归一（会碰合成收尾与报告章节两条热路径且修不了历史数据，等 MCP/导出侧有需求再加，登记为遗留）。

测试：单元（node test）覆盖「标记在序号前 / 已在序号后 / 段首无序号 / 无序列表 `-` / 缩进 3 空格 / 句中标记不动 / 四种标记」；组件用例：`AnswerMarkdown` 渲染「（推断）以下为：\n（推断）1. a\n（推断）2. b」得到 `ol` 两个 `li`、每个 `li` 里一个 `span.answer-inference`，段首那行仍是一个 span；`ReportMarkdown` 同样。后端 `test_prompts` / `test_report_engine` 钉三处位置句。文档：`product-and-api` 中英在规则 12/13 段补一句标记位置与渲染前归一。

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
