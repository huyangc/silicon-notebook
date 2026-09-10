# reflect 前缀复用 · PR-4 实施计划:T3 `prefix_delta_lean` 轻量自评

日期:2026-09-10。上游设计:[reflect 上下文与前缀复用最终设计](2026-09-09-reflect-prefix-cache-final-design_zh.md) **§6**(核心)、§2.3(末两行)、§4.5、§5.2(回退保留 L 设置)、§7、§11(T3 行)、§12、§13。前置:PR-1([v2 基线修复](2026-09-09-reflect-v2-baseline-fixes_zh.md),T-BF7 的逐方面解耦已在基线)、PR-2([T0+T1](2026-09-09-reflect-prefix-snapshot-plan_zh.md),#706)、PR-3([T2 `prefix_delta`](2026-09-10-reflect-prefix-delta-plan_zh.md),#707)。主 agent review 调整第 1 条(assessment 与动作解耦进公共基线,B/P/D/L 共用)已由 PR-1 兑现,本期**不再重做解耦**。机制核实由 Plan 代理(opus)针对 master @ `9fcdcb58f` 完成,行号以该基线为准。

## §0 结论先行

**L 不是一个新的上下文布局,而是同一布局上的一份新自评合同。**三个核心机制,其余全是接线:

- **只报变化(prompt 侧,唯一真正的"新东西")。** 服务端在常规轮**今天就已经**接受省略自评(`_absorb_assessment` 对 `assessment is None` 原样返回,非收尾轮 `_nudge_missing_assessment` 直接放行,`reasoning_retrieval.py:5326-5328`、`:5497-5499`)。真正逼着模型每轮重述全量的是**两句话**:`_V2_ASSESSMENT_INSTRUCTION` 的「fill `assessment` for the aspects you can judge now」加上违规后果的恐吓句(`prompts.py:1187-1212`),以及 T 里那句 `ASPECT_BLOCK_NOTE`「本轮请在同一份 JSON 的 assessment 里重新给出」(`reasoning_aspects.py:102-105`,`off` 与 P/D 两个渲染点共用)。L 的改动就是把这两处换成 lean 双胞胎,别的一个字不动。
- **收尾不为记账追加一轮(服务端侧,收益的主要来源)。** 今天收尾缺自评会被 `_reflect_invalid` 折成伪动作、扣一步、下一轮追问(`reasoning_retrieval.py:5419-5510`),这是实测里最贵的一格(生产约 40s/轮)。L 下这一轮不发。现成的语义已经存在:`AspectLedger.note_missing_assessment(may_prompt=False)`(`reasoning_aspects.py:476-515`)做的正是「接受这份收尾、标记没被判断过的方面、不烧追问额度」。所以 L 的服务端接线收敛成**一格 run 级冻结的账本开关**,让 `note_missing_assessment` 自己短路——`_absorb_assessment` / `_nudge_missing_assessment` / `run()` 一行不改。`run():9157` 那条 `invalid_reason == _V2_MISSING_ASSESSMENT` 的 stale 持平分支从此不可达,**不需要删**(热函数零松弛,删了反而红)。
- **未评估不冒充缺失(合成侧,§6 最后一段)。** `render_termination_block`(`reasoning_aspects.py:1137-1206`)今天把「模型压根没评的方面」和「查过确实没有的方面」一起塞进 `Questions the retrieval did not resolve`。L 下这成为常态,所以必须补一句 L 专属的披露:模型自己的结束判断 + 未逐项核验的计数。判据放在 `RetrievalTermination` 的一格新布尔上(纯函数照旧不读 Settings,三个调用点签名一格不改)。

一句话的边界:**L = D + 自评合同**。它是配置闭集里的第四格,不是与 P/D 正交的第二维(§6 原文「`prefix_delta_lean` 在 `prefix_delta` 上增加本节改动」;`config.py:1095` 的 `Literal` 四格与 `REFLECT_OPTIMIZATIONS` 已钉死)。消息形状与 D 逐块相同,`_reflect_prefix_layout`(`reasoning_retrieval.py:4637-4662`)只多认一个取值;两处 `== "prefix_delta"` 的字面相等(`:4870`、`:5008`)换成新的 `_DELTA_LAYOUTS` 成员判断。

## §1 机制核实

### M1 现行 assessment 合同的四个半区

| 半区 | 落点 | 结论 |
| --- | --- | --- |
| schema/传输闸 | `prompts.py:1582-1636`(`reflect_v2_schema_hint`) | `'"assessment":{}'`——开放对象,闸只管「是对象或 null」,字段/枚举/上界全归 `AspectLedger.apply`。**L 不改 schema 一个字节**,`_GatedV2LLM` 的形状闸不用跟着变(闭集守卫是 `recognized_actions`,与 assessment 无关) |
| prompt 合同 | `prompts.py:1187-1212`(`_V2_ASSESSMENT_INSTRUCTION`),被 `reflect_v2_system_prompt:1352`(off)与 `reflect_v2_static_prompt:1521`(P/D)共用同一份常量 | L 要换的正是这一份;`:1488-1522` 的 `delta` 参数是现成范本 |
| 逐方面校验 | `reasoning_aspects.py:565-668`(`apply`)、`:670-701`(`_commit`)、`:703-771`(`_plan_row`)、`:231-262`(`AssessmentOutcome`) | §6 的"独立校验/去重/冲突/未知方面"全部已在基线(T-BF7)。**本期一行不改** |
| 服务端处理 | `reasoning_retrieval.py:5271-5365`(`_absorb_assessment`)、`:5367-5417`(`_note_assessment_rejections`)、`:5419-5510`(`_nudge_missing_assessment`) | 唯一与 L 冲突的是收尾追问那条路 |

### M2 收尾追问链的完整判据(L 要关的就是它)

`_v2_note_turn:5526-5562` → `_absorb_assessment:5326/5356` → `_nudge_missing_assessment:5497-5510`:唯一的闸就是 `note_missing_assessment` 的返回值。它返回 False 的那条路(`:503-510`)已经完整实现 L 想要的收尾语义——接受收尾、给没判断过的方面打标、不烧追问额度、不折轮。把 L 做成"这本账不追问"是最小改动面;`_absorb_assessment` 不需要抽 helper。追问链的其余四处消费点(`restore_pending_nudge:516-532`、两个渲染点的 `nudge_pending` 消费、`_v2_termination:5638-5639` 的兜底、`run():9157` 的 stale 持平分支)在 L 下全部自然不可达,一格都不用改。

### M3 T 的方面状态半与 D 的「已接受方面」note

- `render_aspect_status_block`(`reasoning_aspects.py:1004-1048`)逐行列出全部方面,尾部 `ASPECT_BLOCK_NOTE`,两处消费副作用;`_prefix_context:5057` 是 P/D 下唯一调用点,与 `off` 的 `render_aspect_block:4962` 互斥。
- **状态半不能收窄成"只列未落定 + 计数"**:(a) §4.5 明写「其余方面不从状态列表消失」;(b) L 下模型停止重述,状态半就成了唯一完整持有方面账的地方(`demotion`/`服务端未采纳` 挂在那几行上);(c) 实验隔离(PR-3 §5 风险 7)。
- 要改的只有一句话:`ASPECT_BLOCK_NOTE`(`:102-105`)里的「本轮请在同一份 JSON 的 assessment 里重新给出」。给 `render_aspect_status_block` 加 `lean: bool = False`,为真时换成 lean 双胞胎常量。
- D 里的「已接受的方面 id」note(`reasoning_retrieval.py:473`,写点 `:5339-5348`)在 L 下不用改。

### M4 三臂字节等价面与新增的中性开关

`off` / `prefix_snapshot` / `prefix_delta` 必须逐字节回到 #707 合入态。本期改到的共用面只有四处,全部"默认值中性":`reflect_v2_static_prompt` 加 `lean`(`prompts.py:1488-1522`);`render_aspect_status_block` 加 `lean`(`reasoning_aspects.py:1004`);`AspectLedger` 加 `lean_assessment` 槽 + `build_aspect_ledger` 加关键字(`:390-394`、`:396-431`、`:856-889`);`RetrievalTermination` 加一格布尔(`retrieval_termination.py:168-186`)。`_PREFIX_LAYOUTS`(`:438`)加一格、导入期对账守卫(`:449-453`)与 `REFLECT_OPTIMIZATION_IMPLEMENTED` 同 diff 同步。

### M5 热函数与状态拥有者

`function_length_ceiling` 精确相等(`scripts/check_architecture_boundaries.py:583-618`):`run` 1334、`_new_run_state` 201、`_run_enumeration` 261 一个 token 都不能动(包括不能删 `:9157`)。L 的接线全部落在 `_reflect_v2_context` / `_prefix_context` / `_open_v2_ledgers` / `reasoning_aspects.py` 的纯函数里;`_ReasoningRunState` 不加字段(run 级事实住在 `state.aspects` 上)。

### M6 记账时序

`_TraceRecorder.__call__:399-412` 在记 `reflect` 步时 `measurement.take()` 合并 detail,逐轮 `⊆` 判(`raise`);写侧 `_registered_measure_key:395-410` 导入期兑账。`_absorb_assessment` 跑在 `reflect()` 之后、记 reflect 步之前(`defer` docstring `:3659-3684`),往 `measurement.detail` 写的键落在同一轮的 reflect 步上。

### M7 「省略自评不追加专门调用」的读侧证据已经现成

`skip_reasons`(`reasoning_trace_stats.py:956`)已按原因码计数,收尾折轮记的 skip 原因码正是 `missing_assessment`(`reasoning_retrieval.py:8577`)。验证不需要新写侧字段——断 L 的 `skip_reasons` 里没有 `missing_assessment`、且 `model_calls_real` 与配对的 D run 少一次即可。

### M8 测量与投影的现状

`REFLECT_MEASUREMENT_DETAIL_KEYS`(`:335-347`)含 11 键;PR-3 Q7 先例:`context_rebuilds`/`context_fallback`/`delta_blocks` 是行为事实,delta 臂测量关也写(`ReflectMeasurement.measures_messages`,门在 `_reflect_measurement:5008`);`cards_shown`/`cards_omitted` 门在 `measures_messages`。终态那一步 detail(`:5652-5670`)已有 `aspects`/`unresolved_aspects`/`aspects_assessment_omitted`。

### M9 与设计字面的两处冲突(登记)

1. **§6 vs §7 的"解耦"归属**:取 §7,已进公共基线(PR-1 T-BF7),本期不重做。
2. **共用 prompt 文本在 T-BF7 之后已经过时,而本期不能修**:`_V2_ASSESSMENT_INSTRUCTION`(`prompts.py:1187-1212`)最后一次改动是 T4-A 的 `331f32d4d`,PR-1 没同步。三句话与现行校验器不符:「neither list may be longer than the aspect list」(现行上限 `_group_row_cap = min(4×方面数, 64)`);「A payload that breaks one of these bounds is rejected WHOLE, runs no retrieval, and costs you a step」(现在逐方面拒绝,整轮照常执行);「same aspect must not appear in both lists」的后果也已逐方面。修它会改三臂字节,与本期硬约束冲突。处理见 Q3。

## §2 拍板(主 agent,2026-09-10;全部采纳 Plan 代理建议)

| # | 问题 | 拍板 | 理由 |
| --- | --- | --- | --- |
| Q1 | L 的服务端接线放在哪 | 一格 run 级冻结的账本开关 `AspectLedger.lean_assessment`,由 `note_missing_assessment` 自己短路;`_absorb_assessment`/`_nudge_missing_assessment`/`run()` 零改动 | 追问链唯一的闸就是那个返回值;账本是自评状态的唯一拥有者 |
| Q2 | 开关何时定型 | run 开始时冻结(`_open_v2_ledgers` 建账时按 `reflect_optimization()` 传入),不每轮重读;prompt 侧按 `_reflect_v2_context` 已有的 `optimization` 局部值 | 中途翻位不能改追问合同,否则臂标签是假的;同 PR-3 `static_delta` 款 |
| Q3 | 过时的共用自评 prompt | L 的 lean 双胞胎写对的那一版;共用那份本期一字节不改,`fangan_todo.md` 登记独立待办(修它要重设三臂字节基线) | 三臂字节等价是硬约束与四臂可比性前提 |
| Q4 | T 的状态半是否收窄 | 不收窄;只换 `ASPECT_BLOCK_NOTE` 为 lean 双胞胎 | M3 三条理由 |
| Q5 | 「未评估」用什么表达 | 不加 per-aspect 新字段:`model_assessed=False` 即"没有模型判断",`assessment_omitted` 保持"问过了它不给"、L 下永不置位;run 级加 `RetrievalTermination.lean_assessment: bool = False` | 现成两格分得开;run 级事实挂 run 级 |
| Q6 | 合成侧披露 | `render_termination_block` 在 `termination.lean_assessment` 且存在 `not model_assessed` 的未解决方面时多一行:模型结束判断 + 未逐项核验计数 + 「这不是"资料里没有"」;`directive` 尾句不改 | §6 原话;判据在 DTO 上,三个调用点签名零改动 |
| Q7 | 终态 reason 闭集 | 不动 | §6 明文;改闭集破坏四臂横向可比 |
| Q8 | 新 detail 键 | 两个,挂既有 `ReflectMeasurement`、门用 `measures_messages`,四臂通写:`assessment_rows`(本轮实际落账方面数 = `len(outcome.accepted)`;整份越界写 0)、`assessment_absent`(载荷没带 `assessment`)。追问轮数不新增键(M7) | 四臂同一把尺子;只在 L 写则 D 侧无可比基线 |
| Q9 | 顶层投影 | 加两列:`assessment_rows_total`(各 reflect 步求和,任一步缺 ⇒ unknown)、`aspects_unassessed`(终态那一步新 detail 键,缺 ⇒ None);`assessment_absent` 不出顶层。**已被评审后修正,见表下** | 前者机制读数,后者质量读数 |
| Q10 | `aspects_unassessed` 写在终态步 detail 是否算改三臂 | 算,登记为已知偏离:v2-only、四臂无条件写,`0` 与缺席分得开;三臂 prompt 字节不变 | 与旁边三个计数同一份快照;只在 L 写则配对表恒缺一半 |
| Q11 | 回退后 L 还是 L | 是;S 的 lean 段与账本开关不受回退影响 | 设计 §5.2「保留轻量自评与否的原设置」 |
| Q12 | `PLANNED` 收窄为空后校验器 | 保留校验器与 `PLANNED = ()`,参数化用例钉「闭集为空时恒放行」;拒绝文案读 `IMPLEMENTED` 四格 | 零成本挂钩点 |

### Q9 评审后修正(2026-09-10,T-PL2 双评审后由主 agent 拍板)

上表 Q9 的「任一步缺 ⇒ unknown」口径**在 T-PL2 修正轮被推翻**,原因是它与 Q8 的写侧门在
fail-open 轮上对不上:provider 故障的兜底轮不进 `_absorb_assessment`(`_v2_note_turn` 的
`if not decision.fallback` 把它挡在外面),那一轮的 reflect 步压根不带这两个键,但它仍是一条
正常记的 reflect 步——沿用全或无口径会让一次偶发 provider 抖动把整条 run 的这一列读成
unknown,而这一列正是 D↔L 唯一的机制读数。

规格评审最初给出的替代方案是「让 T-PL5 在 fallback 轮也写两键(`absent=True, rows=0`)」;
**质量评审否掉了它并被采纳**——那会把一次 provider 故障读成一次「模型没带 assessment」,而
L 的全部收益判据就建在「省略」这个读数上。最终拍板是**读侧改口径**:

- `assessment_rows_total` 改成 **sum-over-present**(照 `_stale` 的 max-over-present 先例,
  实现在 `reasoning_trace_stats._reflect_assessment`),全部步都缺 ⇒ `None`;
- 顶层加**伴生披露列** `assessment_observed: bool|None`(每一条 reflect 步都带
  `assessment_rows` ⇒ `True`;有步缺 ⇒ `False`;一步都没带 ⇒ `None`),照 `attempts_observed`
  (`_reflect_attempts`)的先例登记。与 `attempts_observed` 的一处**不同**要写清:那一列为
  `False` 时和是**下界**,这一列为 `False` 时和是**真实**的部分和(缺的那些轮结构上没有可加的
  行,不是"有行但没量到")。

于是本期投影**共加三列**(不是两列):`assessment_rows_total`、`assessment_observed`、
`aspects_unassessed`。T-PL5 实施后又收窄了一次写侧判据(见该任务的实施记录):不只 fail-open
轮,**任何折叠决定**(`decision.invalid_reason` 非空:首次抖动降级 / 解析失败 / 参数越界)也
不写两键,口径统一成「两键只在**模型自己那份载荷**的那一轮写;服务端折出来的决定与 fail-open
轮都不写,由 `assessment_observed=False` 披露」。

## §3 任务

统一硬约束(与 PR-3 §3 逐字相同,外加):`prefix_snapshot`/`prefix_delta` 字节与 #707 合入态逐字节等价;三个热函数精确相等(长短都红);`reasoning_aspects.py`/`reasoning_context.py`/`reasoning_observation.py` 不读 Settings/DB;零新增模型调用;`AspectLedger.apply`/`_plan_row`/`_commit`/`_absorb_assessment`(校验/折叠逻辑)/`_nudge_missing_assessment` 一行不改。收尾 `bash scripts/check.sh`。

### T-PL1 放开第四格 + 布局对账 · sonnet · ~45 行 · 不碰热函数

**落点** `backend/app/core/config.py:76-88`、`:1786-1809`;`backend/app/services/reasoning_retrieval.py:438`(`_PREFIX_LAYOUTS`)、`:428-437`(新常量 `_DELTA_LAYOUTS`)、`:449-453`(导入期对账守卫)、`:4870`、`:5008`。

**要点** `IMPLEMENTED` 四格、`PLANNED = ()`(Q12);校验器保留、拒绝文案按 `IMPLEMENTED` 构造;`_DELTA_LAYOUTS = ("prefix_delta","prefix_delta_lean")`,`_PREFIX_LAYOUTS = ("prefix_snapshot", *_DELTA_LAYOUTS)`;守卫表达式不变;`:4870` 的 `== "prefix_delta"` → `in _DELTA_LAYOUTS`,`:5008` 的 `!=` → `not in`。

**验收** 四取值 × v2 开/关矩阵自动扩;`prefix_delta_lean` 能起来且拿到 delta 装配;off/P/D 逐字节不变;`PLANNED` 为空校验器恒放行。

**用例** (a) `IMPLEMENTED + PLANNED == REFLECT_OPTIMIZATIONS` 且不交、`PLANNED == ()`;(b) 只放开 config 不加 `_DELTA_LAYOUTS` ⇒ 导入期 `RuntimeError`;(c) 只放开 `_PREFIX_LAYOUTS` 漏 `_DELTA_LAYOUTS` ⇒ L 发出不带 D 通道的消息 ⇒ 红;(d) L 臂测量关时 `ReflectMeasurement` 仍构造;(e) 未知取值仍折回 `off`。

**依赖** 无。

**实施记录(2026-09-10;`432aa3898` + 修正轮 `6977ceb19`)** 按计划落地,`_DELTA_LAYOUTS` 取
**派生**写法(`_PREFIX_LAYOUTS = ("prefix_snapshot", *_DELTA_LAYOUTS)`),这样导入期那条对账
守卫连第四格一起盖住,不用第二条守卫。修正轮三件事:(a) 质量评审 P1-1 指出 Q12 的「闭集为空
恒放行」只覆盖了条件为假那一半,校验器改成 `if False:` 也没人红——补
`test_reflect_optimization_validator_still_rejects_a_planned_value`,用 monkeypatch 把
`PLANNED` 打回非空直接执行那条 `raise`,顺带把「拒绝文案读 `IMPLEMENTED` 四格」这半也从恒不
执行的分支里救出来。(b) 两份部署文档 `RECENT_OBSERVATIONS` 条目里「`prefix_delta_lean` 今天
仍被拒」的预告句与同文件「四格全部已实现」自相矛盾,改成「L 与 D 同一套 K/D 判据,这一句对
四格都是当前行为」。(c) `test_prefix_delta_lean_reaches_delta_assembly_not_the_snapshot_fallback`
原来断「L 与 D 逐字节相同」,那是 T-PL3/T-PL5 落地前的**中间态**;拍板改成**差分**形式(保留
`delta_blocks` 计数 + 逐轮相等 + user 段按 `aspect_block(turn)` 排除后逐字节相同 + 一份显式的
允许差异白名单),不是放宽成不比。**存疑一处、拍板保留**:规格评审建议文档不引 pydantic 的
`Input should be 'off', …` 原句(它由 pydantic 版本决定),质量评审主张保留钉定(精确钉版、
运维可 grep 日志)——采纳后者,只在用例 docstring 注明这串字节的产地在 pydantic、升版时与两份
部署文档同 diff 改。

### T-PL2 domain 登记:两个 detail 键 + 一个终态键 + 两列投影 · sonnet · ~80 行

**落点** `backend/app/domain/reasoning_trace_stats.py:95-140`、`:325-347`、投影装配段;`backend/app/domain/retrieval_termination.py:168-186`。

**要点** `REFLECT_MEASUREMENT_DETAIL_KEYS` 加 `assessment_rows`、`assessment_absent`;顶层加 `assessment_rows_total`(sum,任一步缺 ⇒ unknown,复用 `_reflect_sum`)、`aspects_unassessed`(终态步 detail,缺 ⇒ None);`RetrievalTermination.lean_assessment: bool = False`;`OPTIMIZATIONS` 不动。⚠ **顶层这一段已被评审后修正成三列、口径也改了**,见 §2 的「Q9 评审后修正」与下面的实施记录。

**用例** (a) 两键缺席 ⇒ 两列 None;(b) 三步 2/0/1 ⇒ 3,一步缺 ⇒ unknown;(c) `0` ≠ None;(d) 隐私守卫只收 int/bool;(e) `aspects_unassessed == 0` 与缺席分得开;(f) `test_optimizations_match_the_settings_literal` 仍绿。

**依赖** 无。

**实施记录(2026-09-10;`40724886e` + 修正轮 `af5209baf`)** 两个 detail 键、终态键
`aspects_unassessed`、`RetrievalTermination.lean_assessment` 都按计划落地;**投影列数按 Q9 的
评审后修正变成三列**(见上面那一节),`assessment_rows_total` 的口径从「任一步缺 ⇒ unknown」
改成 sum-over-present,并新增伴生列 `assessment_observed`(同 diff 登记进
`RUN_PROJECTION_KEYS` / 投影装配 / `analyze_reasoning_trace.py` 的 `BOOLEAN_METRICS` 与
`NEW_MEASUREMENT_KEYS`)。修正轮另外收了五件小事:markdown 配对表补一条**按表头索引取单元格**
的用例(钉住 metric/label 两层推导式的顺序,移动变异必红);`retrieval_termination.py` 那格
docstring 的写点拆成两跳(`AspectLedger.lean_assessment` 由 `_v2_build_aspect_ledger` 建,DTO
那一笔在 `classify_termination`);一处变异注记本身不成立(`max(2,0,1)=2≠3`,照它改不会红)
已改；`analyze_reasoning_trace.py` 稀疏指标那段散文里会过期的数字去掉;`_aspects_unassessed`
补「取首条终态 skip 步」与「`step_type=="skip"` 判据」两条移动变异用例。存疑项也一并收了:
`RetrievalTermination` 加一条**字段名集合闭集守卫**,以后加字段必须同 diff 更新那份名集。

### T-PL3 `prompts.py`:S 的 lean 自评段 · sonnet · ~70 行 · 过 `_GatedV2LLM`

**落点** `backend/app/services/prompts.py:1180-1212`(新常量)、`:1488-1522`(`reflect_v2_static_prompt` 加 `lean`)。

**要点** `_V2_LEAN_ASSESSMENT_INSTRUCTION` 共用协议常量插值(`ASPECT_UNRESOLVED_STATUSES`、`REFLECT_ASPECT_MAX_EVIDENCE_KEYS`、`REFLECT_ASPECT_GAP_MAX_CHARS`),五句:(1) 只报本轮的变化,省略的方面保留服务端状态,已支撑项不必重述,省略不花代价也不追问;(2) 收尾轮在同一份 JSON 里给出能判断的最终变化与缺口,没走到的方面留着不评,服务端不会为补齐账目退回一轮;(3) 字段与上界照旧;(4) 写错的后果说实话:动作与自评独立校验,某方面不合规只作废那一个方面(保留旧状态、下一轮状态块告知原因),动作照常执行;键被剔除后剩下的行照收、可能因此掉档,与"整行被拒"分两句说;**此外**(不是"只有")服务端压根无法归属的载荷作废整轮,而那是**四种**情形——整份不是对象、某一组不是列表、组里某一行不是对象、某一组超过行数上限(上限按 `_group_row_cap` 的两个协议常量同源插值:`min(REFLECT_ASPECT_GROUP_ROWS_FACTOR × 方面数, REFLECT_ASPECT_GROUP_ROWS_HARD_MAX)`);(5) 方面清单只能由用户改;省略不等于"证据不存在"。`reflect_v2_static_prompt(catalog, *, delta=False, lean=False)`:`lean` 为真时**替换**(不追加)`_V2_ASSESSMENT_INSTRUCTION`;`reflect_v2_system_prompt` 一字节不改;schema 不改。

**用例** (a) `lean=False` × `delta` 两种对着 HEAD 逐字节比;(b) L run 多轮 `system_prompts` 全等且不含 `rejected WHOLE`;(c) 三个上界数字与协议常量同源;(d) 过 `_GatedV2LLM` 一次完整 L run(需 T-PL1;若先落地,先用直接构造 + 序列化,报告注明)。

**依赖** 无。

**实施记录(2026-09-10;`df17de5dd` + 修正轮 `9240975e1`)** 五句的骨架按计划,但**两份评审都
指向第 (4) 句与 `AspectLedger.apply` 的实际形状不符**,修正轮按主 agent 定稿文本改了三处漏洞:
第 (1) 句补回「listing an aspect REPLACES its whole row, keys included」(lean 段原来只讲省略
保留、没讲报一次就整格替换,模型按 union 理解会丢证据键)并补上「首轮没有上一次判断时,现在
能判的全算变化」;第 (3) 句补回**三条旧段有而 lean 段漏掉的护栏**(非法证据键被剔除且剩不下
键就掉档、同一方面不得跨两表各出现一次、组内行数上限);第 (4) 句把「Only … invalidates the
whole turn」的全称断言改成「Beyond that …」并补上行数超限那一条——整份作废实际是**四种**情形
(`not_object` / `<group>_not_list` / `item_not_object` / `<group>_overflow`),原文只列了三种,
计划 §3 那一句已同步(上面)。为了让 lean 段能**同源插值**行数上限而不必从 `prompts` 反向
import 服务层,`_group_row_cap` 的两个常量从 `reasoning_aspects.py` 挪进
`domain/retrieval_termination.py` 并更名 `REFLECT_ASPECT_GROUP_ROWS_FACTOR`/`_HARD_MAX`(行为
不变)。Q3 的注释块原来误挂在 lean 常量头上,挪回 `_V2_ASSESSMENT_INSTRUCTION` 自己头上;lean
常量头上换成它自己的说明 + 一笔**字节账**(L 比 D 恒多 1,246 字符,那是本次补回的护栏文案,
**不是布局差**)。用例:五句短语断言按新文本重写、两组 golden 重烧,新增
`test_lean_assessment_instruction_sentences_3_and_4_match_apply` 拿 `apply` 的实测行为逐条对账
第 (3)(4) 句的每一条后果。

### T-PL4 `reasoning_aspects.py`:账本开关、状态半那一句、合成披露 · **opus** · ~130 行

**落点** `reasoning_aspects.py:102-121`、`:390-394`、`:396-431`、`:476-515`、`:856-889`、`:1004-1048`、`:1137-1206`、`:1451-1499`。

**要点** `AspectLedger.__slots__`/`__init__` 加 `lean_assessment: bool = False`;`build_aspect_ledger(intent_detail, question, *, lean=False)` 传给三个返回点;`note_missing_assessment` 开头:`lean_assessment` 为真 ⇒ 走与 `may_prompt=False` 完全同一段(接受收尾、给 `not model_assessed` 的行打标、不动 `assessment_prompts`)并返回 False——**不写 `assessment_omitted`**(Q5),docstring 明写区分;`ASPECT_BLOCK_NOTE_LEAN`(「上面的状态是此前某一轮模型自己的判断,不是服务端对语义支撑的证明;本轮只需给出有变化的方面,省略的保留现状」),`render_aspect_status_block(ledger, *, lean=False)` 二选一,其余每格不动;`classify_termination` 带 `lean_assessment=ledger.lean_assessment`(签名不变);`render_termination_block` 按 Q6 加一行(复用 `_TERMINATION_BLOCK_MAX_ASPECTS` 截断口径),`directive` 尾句不改。

**用例** (a) `lean_assessment=True` 账本收到无 `assessment` 的 `answer` ⇒ 返回 False、`assessment_prompts == 0`、`assessment_omitted` 全 False、`model_assessed` 全 False;(b) `False` 下返回 True(对照);(c) `lean=True/False` 状态半只差那一句且全部方面行都在;(d) 第 2 轮只报一个方面 ⇒ 其余方面状态与绑定键第 3 轮逐字不变;(e) 合成新行:计数正确、两种终态各一条、B/P/D 恒不出;(f) 变异:新行无条件渲染 ⇒ 红;(g) 变异:L 下写 `assessment_omitted` ⇒ `aspects_assessment_omitted == 0` 用例红。

**依赖** T-PL2。

**实施记录(2026-09-10;`6d8bd7732` + 修正轮 `1cdc12e5b`)** ⚠ **上面「要点」里那句话字面自相
矛盾,以本段为准**:它写的是「`lean_assessment` 为真 ⇒ 走与 `may_prompt=False` **完全同一段**
(接受收尾、给 `not model_assessed` 的行打标、不动 `assessment_prompts`)并返回 False——**不写
`assessment_omitted`**」,而 `may_prompt=False` 那一段的**全部内容就是写 `assessment_omitted`**
(run 级一格 + 每一条 `not model_assessed` 的 per-row 一格),所以「同一段」与「不写」不可能同
时成立。实际实现是:**L 的闸直接 `return False`,什么都不标**;「这些方面没有模型判断」这件事
由既有的 `model_assessed=False` 表达,`assessment_omitted` 在 L 下永不置位(Q5)。方面账为空的
run 与 `may_prompt=False` 两条既有路一行未改。修正轮五件事:(P2-1)那条闸的**位置**是判据的一
部分——排在 `may_prompt`/`MAX_PROMPTS` 两个分支之**前**,排到后面会把一次照章省略误记成「问过
它不给」,补一组 `may_prompt=False` 的移动变异守卫钉住行序;(P2-2)合成新行的判据补对照用例
——显式自评成 `unknown` 的方面(`status=unknown, model_assessed=True`)**不算**"从未评估",判据
必须看 `model_assessed` 而不是 `status == unknown`;(P3-2)`AspectLedger.lean_assessment` 从
「docstring 里的约定」升成**只读 property + 私有槽**,建账后赋值结构性地抛 `AttributeError`;
(P3-4)`unassessed` 的计算挪进 `if termination.lean_assessment` 之内,三臂不再白付这笔零消费的
计算;(P3-5)新行的措辞改成单复数正确,并把模型自报判读("The planner reported / did not
report …")挪到**计数与列举之后**——那半句更宽松的自报放句首会给合成模型留一句可以援引来跳过
缺口说明的话。另修 `_termination_aspect_list` 的 `more == 1` 边界用例与
`render_aspect_status_block` docstring 的两处事实错误(`off` 走的是 `render_aspect_block`;这个
函数由 P/D/L 共用,不是"四臂共用")。

### T-PL5 `reasoning_retrieval.py`:接线与两个 detail 键 · **opus** · ~75 行 · `run()` 零改动

**落点** `:5511-5524`(`_open_v2_ledgers`)、`:5326`(防御建账)、`_absorb_assessment` 内仅两处写、`:4855-4875`、`:5020-5068`(`_prefix_context`)、`:420-437`(两个新测量键常量)。

**要点** 私有 helper `_v2_build_aspect_ledger(self, state)` 一处调 `build_aspect_ledger(..., lean=self.reflect_optimization() == "prefix_delta_lean")`,`_open_v2_ledgers` 与防御分支共用;`_prefix_context` 加 `static_lean: bool = False` 传给 `reflect_v2_static_prompt(..., lean=)` 与 `render_aspect_status_block(..., lean=)`,两个调用点(delta 支与 P/回退支)都传 `optimization == "prefix_delta_lean"`(Q2、Q11);`_MEASURE_ASSESSMENT_ROWS`/`_MEASURE_ASSESSMENT_ABSENT` 走 `_registered_measure_key`,写点在 `_absorb_assessment` 内、门 `measures_messages`,三条路(`assessment is None` ⇒ absent=True,rows=0;`not outcome.error` ⇒ absent=False,rows=len(accepted);整份越界 ⇒ absent=False,rows=0)合成一个 3 行私有 helper,`_absorb_assessment` 净增 ≤ 4 行;终态步 detail 加 `aspects_unassessed`(v2 无条件,Q10)。不改 `run()`、`_new_run_state`、`_run_enumeration`、校验/折叠逻辑、`_nudge_missing_assessment`、`_note_assessment_rejections`、`_reflect_prefix_layout`。

**验收(§12)** L 完整 run:S/C 逐轮不变、已发出 D 逐块不变;收尾轮没有 `missing_assessment` skip、`model_calls_real` 比配对 D run 少 1;off/P/D 完整 run 消息序列与 schema hint 逐字节回到 #707;热函数精确相等;`retrieval`/`model_clients` 调用计数 L 与 D 只差收尾那一次;取消与 provider 故障原语义;回退后仍是 L。

**用例** (a) L 三轮:全量 → 省略 → 收尾只报一个变化 ⇒ 不折轮、`model_assessed_sufficient=True`、`aspects_unassessed` 如实;(b) 同剧本在 `prefix_delta` ⇒ 收尾被折、多一轮、`skip_reasons["missing_assessment"] == 1`;(c) L 下逐方面全被拒的收尾 ⇒ 不折轮、仍记 `invalid_assessment:*` skip、下一轮「服务端未采纳」;(d) 整份形状越界 ⇒ 照旧折并扣步;(e) 两键三条路 + 测量关缺席;(f) 中途翻位 ⇒ S 两种字节可见而账本合同不变(Q2 守卫);(g) 三臂 golden;(h) 变异:防御分支漏 helper ⇒ 红;(i) 变异:`static_lean` 第二次读策略位 ⇒ (f) 红。

**依赖** T-PL1、T-PL3、T-PL4。

**实施记录(2026-09-10;`fe620891f` + `c95ca8ee7` + 修正轮 `6fb014c63`)** 接线按计划:私有
helper `_v2_build_aspect_ledger` 成为本模块方面账的唯一产地(三处建账全部改走它,含总闸中途
翻开时 `_reflect_v2_context` / `_absorb_assessment` 各自那条防御重建),并加「模块内
`build_aspect_ledger(` 直调点数 == 1」的 AST 守卫;`_prefix_context` 加 `static_lean`,两个调用
点(delta 支与 P/回退支)都传 `optimization == "prefix_delta_lean"`,策略位在一轮内**只读一次**。
**写侧判据在修正轮被收窄一次(P1)**:`decision.assessment is None` 那条路原来对
`_reflect_invalid` 折出来的伪动作(首次 provider 抖动降级、解析失败、参数越界)也无条件写
`assessment_absent=True`,把「服务端替模型编的决定」记成了「模型自己省了自评」;判据改成
`not decision.invalid_reason`(`_reflect_invalid` 是 `invalid_reason` 的唯一产地),折叠决定两键
都不写,读侧因此如实变「未观测」。**这条比双评审前的存疑收得更宽**:此前的存疑 1 只想判
`model_degraded` 前缀,拍板放宽成「`invalid_reason` 非空即不写」,不再逐种原因列举。修正轮另加
三条:一条**运行期计数守卫**(monkeypatch `reflect_optimization` 计次,钉住建账内部只读一次
策略位——既有 AST 守卫只钉自由函数的直调点数,挡不住"移到 helper 里再读一次"的移动变异);
`aspects_unassessed == 0`(三个方面全评过)的写侧用例;以及把
`test_the_lean_wiring_leaves_the_other_three_arms_untouched` 的 docstring 从「四处默认值中性都
由用例证明」改成**如实陈述**——`_prefix_context` 的 `static_lean` 两个调用点都显式传值,那个默认
值在生产代码里没有活路径,**不由这条用例证明**。T-PL7 的差分白名单同理只声明「允许替换」,「两句
从不同时在场」那两行才是变异 (b) 的守卫。

### T-PL6 rig / A-B 第二维放开最后一格 · sonnet · ~20 行

**落点** `backend/app/eval/reflect_ab.py:85-101`、`:195-208`;`scripts/README.md:455-480`。`ARMS` 加 `("v2","prefix_delta_lean")`;两句过期散文;`_settings_by_arm` docstring 改引 `Literal` 之外的拼写。

**用例** (a) `--arms v2:prefix_delta,v2:prefix_delta_lean` 解析 + 逐臂回读;(b) `legacy:prefix_delta_lean` 文案列五格;(c) dry-run 请求上界不变;(d) 新两列进 `analyze`。

**依赖** T-PL1、T-PL2、**T-PL5**(T-PL1 quality 评审 P2-1 拍板:`ARMS` 放开 lean 必须与接线同 PR
落地,否则 rig 能声明一条跑不出 lean 合同的臂;对 T-PL1/T-PL2 的依赖经 T-PL5 传递)。

**实施记录(2026-09-10;`4a6934f62` + 修正轮 `ca0ac856e`)** `ARMS` 加最后一行、两句过期散文改
事实(`reflect_ab.py` 里「由 config 校验器在启动期拒绝」改成「不在这份 `ARMS` 闭集里」,再由
本任务放开)。配对表那两列在 T-PL2 就做完了,本任务只加 `ARMS` 与文案。修正轮四件事:报错文案
用例**参数化到 `legacy:prefix_delta` 与 `legacy:prefix_delta_lean` 两格**,恢复 legacy × 三个非
`off` 取值的全交叉(否则「组合闸按取值绕过 `prefix_delta`」这种变异抓不到);一处 docstring 的
「七种」去掉数字(参数化清单本来是 8 条);补两句**负向断言**防过期文案回潮;
`test_arms_v2_side_covers_every_implemented_optimization` 的等号右边从 `OPTIMIZATIONS` 换成
`REFLECT_OPTIMIZATION_IMPLEMENTED` 并另加一条 ⊆ 方向断言,避免在「下一格先进
`PLANNED`+`Literal`+domain 闭集、`ARMS` 还没跟上」的**合法 staging 态**上误红。另把 dry-run 的
请求上界数字逐字钉死(≤ 106 次逻辑调用、上界 ≤ 212),防上界估算按臂身份打折的回归。
**一处已知限制登记进 `fangan_todo.md`**:`optimization_pairs` 表的基线恒 `off`,一批只有 D 和 L
时两侧配对表都是空,D↔L 的差值要分两批各自对 `off` 做差再相减;`ab-runs.jsonl` 带 ab 专属键,
`analyze_reasoning_trace.py` 的 `load_rows` 会整批拒绝,需先降到 T0 键集——这是 T-PS4 起的既有
结构,PR-5 的实验通道再定读出路径。`scripts/README.md` 的 rig 一节已在本轮改到位(第二维五格、
过期举例、D↔L 归因警告、上面那条读法),T-PL8 只核对不重复。

### T-PL7 验证清单与变异复核 · opus

三处不可协商性质各一次变异:(a) L 下收尾改成仍追问 ⇒ 红;(b) lean 段改成追加而非替换 ⇒ 「两句矛盾合同同时在场」红;(c) 合成新行去掉计数 ⇒ 红。

**实施记录(2026-09-10;纯验证任务,零代码提交)** 三次变异各做一次、逐一确认报红后精确还原,
落在这三处守卫上:(a) `test_a_lean_run_accepts_a_silent_closing_turn_without_a_follow_up` 与配对
的 `test_a_lean_silent_closing_turn_costs_no_extra_model_call`(后者断「同一剧本下 L 比 D 少恰好
一次真实模型调用」,是 §12「省略自评不追加专门调用」那一条的落点);(b)
`test_reflect_v2_static_prompt_lean_replaces_not_appends_the_assessment_paragraph`——**它的守卫
是那两条「旧段与新段不同时在场」的断言**,不是差分白名单:白名单只声明「这一段允许被替换」,
追加式变异下白名单本身照样通过;(c) `test_a_lean_termination_discloses_what_was_never_assessed`。
合入前置断言 N1(L 臂收尾不出 `missing_assessment` skip)由 T-PL5 用例 (a)(b) 覆盖并在本任务复
核。本任务同时把两处**过度声明**改回如实陈述:`_prefix_context` 那两个 `lean` 默认值在生产代码
里没有活路径(两个调用点都显式传值),**不由任何用例证明**,相关 docstring 已在 T-PL5 修正轮改
掉。另记一次不可复现的 314 errors(xdist 临时目录互踩形态),事后查明是**变异回滚脚本自伤**
(空串 `replace`),**不记为 flake**。

### T-PL8 文档 · sonnet · 只改文档与注释

见 §4。

### 依赖与并行

```
T-PL1 ──┐
T-PL2 ──┼─→ T-PL4 ──┐
T-PL3 ──┴───────────┴─→ T-PL5 ─→ T-PL6 ─→ T-PL7 ─→ T-PL8
```

T-PL6 原先与 T-PL5 并行(只依赖 T-PL1、T-PL2),T-PL1 quality 评审 P2-1 拍板改成**依赖 T-PL5**:
`ARMS` 放开 lean 必须与服务端接线同 PR 落地,否则 rig 能声明一条起得来、却跑着 D 追问合同的
「lean 臂」,而那批数据在表上看着完全正常。T-PL6 对 T-PL1/T-PL2 的依赖因此经 T-PL5 传递,不再
单列。于是 T-PL5 之后整条链是线性的,并行只发生在 T-PL1/T-PL2/T-PL3 这三格与 T-PL4 上。

### 刻意不做

不改 `AspectLedger.apply`/`_plan_row`/`_commit`/`AssessmentOutcome`;不改共用 `_V2_ASSESSMENT_INSTRUCTION`(Q3 待办);不改 schema 与 `recognized_actions`;不改终态 reason 闭集;不收窄 T 状态半;不删 `run():9157`;不改 `REFLECT_ASSESSMENT_MAX_PROMPTS`、最大步数、工具次数;不实现「两轮没新键一律停止」;不把自评变动当清零 stale 依据;不加原始响应修复 LLM;不新增读证据工具;`AB_DEFAULT_ARMS` 不变;E1 接缝仍不做。

## §4 验证清单与文档落点

### 映射设计 §12

| §12 条目 | 落点 | 状态 |
| --- | --- | --- |
| S、C 字节不变;已发出 D 不变 | T-PL5 (a) | D 版继承 |
| provider-facing 消息覆盖 wrapper/schema | T-PL3 (d) + T-PL5 (a) | schema 未改 |
| assessment 单项错误不吞合法动作;冲突重复不静默去重 | T-PL5 (c)(d) | 基线 PR-1,L 重跑 |
| **省略自评不追加专门调用** | T-PL5 (a)(b) | 本期核心断言 |
| **未评估不冒充缺失** | T-PL4 (e)(f) + T-PL7 (c) | 本期新增 |
| 取消/真实模型失败原语义保留 | T-PL5 (d) + 既有 | 零改动 |
| PR-3 全部用例在 L 臂重跑(含回退后仍是 L) | T-PL5 (g) | |
| 静态目录已耗尽工具不能执行;Knowhow/legacy 不受影响 | T-PL1 | |
| 无 usage 仍出表、缺失不变 0 | T-PL2 | |
| 既有聚焦测试可达 | `check.sh` | |

### 文档落点

- `docs/deployment-and-configuration{,_zh}.md`:四格全部已实现,删「配上去会被拒绝」与 `RECENT_OBSERVATIONS` 那句预告(**已由 T-PL1 修正轮删掉**,T-PL8 只核对无残留);新增「`prefix_delta_lean` 做的事」(同布局同内容判据,差别只有自评合同三条,每条各指一个用例;字节账**如实且已实测的两个数都是净增**:S 自评段换 lean 版比 D 多 1,246 字符、T 那一句多 3 个字符,每轮少的是模型重述已支撑项的那些行——那在模型的输出侧、**一个数都还没量**;省的主要是一次收尾轮);明写解耦是公共基线(PR-1 T-BF7)、L 只改模型提交什么不改服务端展示什么、开关建账冻结一次、回退后仍是 L;`MEASURE_CONTEXT` 补两个新键(门是本开关、四臂通写、只在模型自己那份载荷那一轮写)与 `assessment_observed` 伴生列,并把已登记偏离从「`prefix_delta`」改成「两条 delta 臂」、另记与本开关无关的 `aspects_unassessed`(Q10)。
- `docs/product-and-api{,_zh}.md`:投影十一列 → **十四列**(Q9 评审后修正把两列变成三列,见上面那一节);`assessment_absent` 登记进稀疏键集但不出顶层;终态步 `aspects_unassessed` 四臂通写(Q10 已知偏离);Q5 三格分工;合成「未逐项核验」的口径与三条边界(判据两个条件都要、是未解决清单的子集、被熔断的 L run 也出这一行)。
- `architecture.md:111`:`AspectLedger.lean_assessment` run 级冻结(只读 property、唯一产地 `_v2_build_aspect_ledger`、三处建账同源 + 直调点数 == 1 守卫)、`note_missing_assessment` 三条路与 per-aspect 标记语义分工、`_DELTA_LAYOUTS` 派生 + 对账守卫覆盖第四格、两个 `lean` 参数默认中性、`REFLECT_ASPECT_GROUP_ROWS_*` 挪进 domain 的理由(校验器与 lean 段插同一个常量)、热函数零改动。
- `scripts/README.md`:第二维五格;删过期举例;「D↔L 是唯一只差自评合同的配对臂,收益不得归因到缓存」。**已由 T-PL6 修正轮改到位**(连带 D↔L 的读出路径),T-PL8 只核对不重复。
- `fangan_todo.md`:新增 (l);已知限制:收益未量(两个实测数都是净增)、共用自评指令过时(Q3 独立待办,修它要重设三臂字节基线)、L 下终态仍可能 `model_partial`、`assessment_absent` 不出顶层、折叠与 fail-open 轮不写两键(用 `assessment_observed` 披露)、`search` 投影 `optimization` 仍 unknown、D↔L 差值的读出路径;(j)/(k) 里 lean 相关条目标已解除。
- `backend/app/services/prompt_layers.py` 的 L0-only 段散文(只改注释):三条前缀臂共用同一份控制流文本,两条 delta 布局各带 D 读法四句,L 独有 lean 自评段(替换共用那份)。
- 不改 `AGENTS.md`/`CLAUDE.md`/`README{,_zh}.md`/带日期的设计稿本体。

## §5 风险

1. 三臂字节等价:四处共用面默认中性需用例证明(T-PL3 (a)、T-PL4 (c)、T-PL5 (g));唯一允许偏离是 Q10 那个终态 detail 键。**实施后如实收窄**(T-PL5 修正轮 P3-3):`_prefix_context` 的 `static_lean` 那个默认值**不由任何用例证明**——它的两个调用点都显式传值,默认值在生产代码里没有活路径;三臂字节等价这件事由 `test_the_lean_wiring_leaves_the_other_three_arms_untouched` 从**行为**一侧证明,不是靠"默认值被测过"。
2. prompt 语义变化过 `_GatedV2LLM`;lean 段与旧段互斥(两句同时在场是最坏形态)。
3. 不新增模型调用:配对断 `model_calls_real` 差恰为 1、`skip_reasons` 差恰是 `missing_assessment`。
4. 「不追问」不能变成「悄悄判 supported」:未评估留 `unknown` + `model_assessed=False`;变异 (g) 挡住写 `assessment_omitted`。
5. `assessment_omitted` 语义漂移:三格分工写进 docstring 与文档,两侧钉住。
6. L 与 D 隔离:除自评合同外任何差异都毁归因;Q4 是第一条应用。
7. 热函数精确相等是双向的:不能"清理"`run():9157`;`_new_run_state` 不加字段。
