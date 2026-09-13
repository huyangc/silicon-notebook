> 历史档案（2026-09-13）：V2 实验已退役，本文不再是实施或启用指引。当前仅保留 Legacy；参见 [当前状态](../../../fangan_done.md#32-reflect-实验退役与-legacy-保留2026-09-13) 和 [产品/API 合同](../../product-and-api_zh.md)。

# reflect 前缀复用 · PR-3 实施计划:T2 `prefix_delta` 增量上下文

日期:2026-09-10。上游设计:[reflect 上下文与前缀复用最终设计](2026-09-09-reflect-prefix-cache-final-design_zh.md) §4.1、§4.4、§4.5、§5、§8.1(上下文层)、§11(T2)、§12。前置:PR-2([T0 测量 + T1 `prefix_snapshot`](2026-09-09-reflect-prefix-snapshot-plan_zh.md),#706 已合入),其九个任务的实施记录即本计划的现实基线。主 agent review 调整第 5 条(`REASONING_REFLECT_RECENT_OBSERVATIONS` 在 delta 模式改义)在本期兑现。机制核实由 Plan 代理(opus)对 master @ `f49461be6`(= `f4e509fa2` 合入后)完成;行号以该基线为准。

## §0 结论先行

三个核心机制,其余全部是它们的接线:

- **卡片冻结与补充卡。** 今天 `build_evidence_block`(`reasoning_context.py:454`)每轮从整个候选池重选、按 `_rank_score` 重排、按**本轮** `observer.last_query` 重算摘录——同一条证据在相邻两轮可以拿到不同的摘录文本、不同的位置。delta 要的"展示后保持不变"因此**不是**给现有函数加个开关能拿到的:必须有一份 run 级的**已渲染卡片文本表**(key → 冻结的那一行),重建与增量都从它取字节,只有从没展示过的键才现渲染。摘录升级不就地改写,而是追加一张 `key` 逐字相同、标注版本的补充卡——`key=` 那一格必须原样,否则 `ever_shown_outline_keys` 与 `outline_binding_keys` 的绑定校验会当场对不上。
- **有界 D 与预算合计。** D 只在一次动作完成后追加,一轮至多一块,内容 = 本轮新展开的卡(上限 `REASONING_REFLECT_DELTA_CARDS_BY_EFFORT[档位]`)+ 本轮新观察行(`observer.rows[cursor:]`,照单全收不裁)+ 上一轮已接受的方面更新。证据池预算(五档 4k–16k)与历史池预算(`STATE_CHARS=6000`)在 delta 下是 **K + 所有 D 的总和**,不是每轮各拿一份满额——两个累计计数住在 run 级投影上,判断在**追加之前**做。
- **确定性重建与回退。** 顺序严格是「动作完成 → 形成待追加事件 → 检查预算 → 必要时重建 K → 生成 T → 调模型」,而这条顺序**天然就是** `_reflect_v2_context` 在 `run()` 循环里的位置(`reasoning_retrieval.py:7741`,每轮一次、排在 `self.reflect(...)` 之前、排在上一轮动作执行与记账之后)。所以 `run()` 一行不动,整条逻辑住在 `_reflect_v2_context` 的第三个分派分支里。重建零 LLM、零 I/O,复用现有 `build_evidence_block` + `render_observations` + 一个新的计数折算纯函数。装不下最小有效新证据 ⇒ 本 run 剩余轮**退回 P 的有界选择**(消息形状不变,只换内容装配),记 `context_fallback`,不重置预算、不增调用、不增轨迹步。

一句话的边界:**`prefix_delta` 与 `prefix_snapshot` 的消息形状完全相同**(`system(S)` + 一条 `user(C+K+D+T)`),差别只在 K/D 两块的内容判据。因此 `_reflect_prefix_layout` 的分派逻辑一格不变(只多认一个取值),回退也不是"换布局",这正好绕开了 PR-2 评审 P3-9 那族"一轮里判两次策略、分歧就静默降级"的故障。

## §1 机制核实

### M1 P 模式今天的 K 是怎么选出来的

**证据卡选取。** `build_evidence_block`(`reasoning_context.py:454-547`)。三档确定性顺序:`bound_keys` → `fresh_keys` → `_diverse_order(index)`(`:490-495`),一个键只属于一档(`seen` 跨档生效)。第一档留底:本轮真有 `fresh` 时先切出 `budget // _FRESH_RESERVE_RATIO`(=1/3,`:56-58`、`:496-500`)。渲染循环 `:511-530`,`shown.append(key)` 发生在**这一行真的进了 `lines` 之后**(`:529-530`)。`seen_min` 是"再装一张卡至少要多少字"的收敛下界(`:510`、`:523`),`_MIN_CARD_CHARS=12` 只是起点。推导链卡另在末尾各取最后两条(`:531-540`)。省略披露 `:543-545`。

**每轮重排是真的。** `_diverse_order`(`:582-606`)按 `_rank_score` 降序 + 来源轮转;`_rank_score`(`:566-579`)读 `relevance`/`score`。而 `bound_keys` 每轮由 `evidence_bound_keys(state.aspects, 大纲键)` 现算(`reasoning_retrieval.py:4432-4440`),`fresh_keys` 是 `observer.fresh_result_ids()`。**结论:同一张卡的位置每轮都可能变**,这是 delta 必须自己持有冻结表的直接原因。

**摘录每轮可能变。** `select_excerpt(body, terms, limit)`(`:215-274`)的 `terms = excerpt_terms(question, observer.last_query, excerpt_chars)`(`:485`),而 `last_query` 是上一轮决定里那段原始查询文本(`reasoning_observation.py:406-408`)。同一条 chunk 在两轮里能给出两段不同的摘录——设计 §4.4 说的"以后遇到更好的摘录"在代码里就是这件事。

**预算五档在哪读。** `_reflect_v2_context`(`reasoning_retrieval.py:4416-4423`):`getattr(settings, "reasoning_reflect_evidence_chars_by_effort")` → 缺省回 `DEFAULT_REFLECT_EVIDENCE_CHARS_BY_EFFORT`(`config.py:51-57`)→ 缺档回 `DEFAULT_RETRIEVAL_EFFORT`。摘录上限 240 在 `:4442-4443`。这四项预算的**全仓唯一读点**就是 `_reflect_v2_context`(architecture.md:111 已登记),两个新配置必须落在同一处。

**`_reflect_v2_context` 的装配与六格。** `:4368-4508`。`ReflectContext` 六个字段 `reasoning_context.py:739-745`;`_PREFIX_ONLY_FIELDS`(`:753`)+ 两个渲染方法双向互斥(`:763-766`、`:798-801`)。`as_prefix_user_block(turn_actions)`(`:776-812`)按 evidence → observations → T 排,T 恒在最末。

### M2 五种状态今天各在哪登记

| 状态 | 登记点 | delta 下要做什么 |
| --- | --- | --- |
| 候选池持有 | `state.collected` / `elements` / `chunks`(`_pool_index`,`reasoning_context.py:550-563`) | 不动。重建不清空池 |
| **曾真实展示** | `state.ever_shown_outline_keys.update(selection.shown_keys)`(`reasoning_retrieval.py:4446`),写点在最终渲染之后 | 增量块与重建后的 K 各自把真渲染出来的键并进去。它是 run 级单调 `set`,天然不会被压缩清空 |
| 当前可见 | 没有单独登记(每轮的 `selection.text`) | delta 下 = K 的卡 + 所有 D 的卡,由投影持有 |
| 合成接纳 / 答案引用 | 收尾的证据预算与引用校验 | 不碰 |

**绑定资格的判据链。** `_absorb_assessment`(`:4539+`)在 `:4596-4599` 算 `allowed = outline_binding_keys(collected, elements, chunks, state.ever_shown_outline_keys)`,再 `|= complete_enumeration_keys(state.enum_chains)`。"仅池中未展示不获资格"已经落实,delta 只需保证 (a) 补充卡的 `key=` 与原卡逐字相同,(b) 重建时对已展示键不改写 `key` 那一格。

**assessment 的落账与 D 的关系。** `outcome.accepted` 是真正落账的方面 id(`reasoning_aspects.py:244-251`、`:698`)。`_absorb_assessment` 在 `run()` 调 `reflect()` 之后执行,所以第 N 轮接受的方面更新自然落进第 N+1 轮装配时追加的那块 D。

**⚠ 与设计字面的冲突 C1。** §4.4 把"已接受的方面状态变化"列进 D,但方面**当前状态**今天整块住在 T(`render_aspect_status_block`,`reasoning_aspects.py:1004-1047`),且那个函数有两处副作用(`consume_rejection_notes()`、`nudge_pending` 置回)。把状态块搬进冻结的 D 会让这两处消费落在一个再也不重渲染的块里。**落法:D 里只放"本轮接受了哪些 id"这条历史事实(一行),当前状态仍整块在 T。**

### M3 观察账在 delta 下的复用

`ActionObservationLedger`(`reasoning_observation.py:369-488`)。`_rows` 全程累加;`note_decision` 每轮把 `_turn_start = len(self._rows)` 翻页(`:445`)。

**D 的观察半不需要新函数。** 在 run 级投影上放一个 `observation_cursor`,取 `rows[cursor:]`,逐行过公开的 `render_observation_row`(`:633-671`)。游标与 `_turn_start` 独立且单调。

**K 重建的历史半需要一个新纯函数。** `render_observations(rows, recent, state_chars)`(`:674-732`)只披露一个总数;设计 §5.2 第 3 条要"其余折成已尝试/已失败/重复/完整性计数"。新增 `fold_observation_counts(rows) -> str`,按 `OBSERVATION_STATUSES`(`:66-69`)与 `_STATUS_LABELS`(`:86-95`)出计数行。`render_observations` 一个字节不动。

**`REASONING_REFLECT_RECENT_OBSERVATIONS` 改义的落点。** 今天在 `:4447-4452` 被当逐轮滑窗读。delta 下只在重建那一次把它传给 `render_observations`,增量块不看它。部署文档两侧已预告,本期改成"当前行为"。

**⚠ 与设计字面的冲突 C2。** `render_observations` 的硬界会在装不下时丢最早的行。对**重建**成立,对**增量**不成立:增量块里的观察行是"这一轮发生了什么"的唯一记录。**落法:增量的观察行照单追加,预算判断在追加之前做——超了就先重建 K。**

### M4 热函数与 run 内投影状态

零松弛判据是精确相等(`scripts/check_architecture_boundaries.py:583-619`),固定名单 `run` 1334 / `_new_run_state` 201 / `_run_enumeration` 261;`_reflect_v2_context`、`_reflect_v2_attempt` 不在名单里。

**照抄 `reflect_measurement` 的办法。** `_ReasoningRunState` 是 `@dataclass(slots=True)`,`reflect_measurement: Optional[ReflectMeasurement] = None` 加在末尾带默认值(`:3537`),`_new_run_state` 全部 kwargs 构造 ⇒ 零改动。delta 照此加 `reflect_delta: Optional[ReflectDeltaState] = None`。

**它必须是"渲染缓存/阶段信息",不是第二个检索控制状态拥有者。** 三条硬约束:(a) 只存已渲染的字符串、卡片文本表、几个整数游标/计数;(b) 不持有候选池、额度账、方面账的任何引用;(c) 把它整个删掉,除 K/D 的字节组织外一切逐字节不变。`ReflectMeasurement` 的类 docstring(`reasoning_context.py:647-678`)是范本。

**顺序接在哪。** `run()` 每轮:上一轮动作执行 → 记账 → `reflect_kwargs["context"] = self._reflect_v2_context(...)`(`:7741-7742`)→ `self.reflect(...)`。整条链落在 `_reflect_v2_context` 一次调用之内。全仓只有 `:7741` 一个调用点;`_reflect_v2` 的加预算重试复用同一个 `context`,所以 D 一轮只追加一次。

### M5 配置

`REFLECT_OPTIMIZATION_IMPLEMENTED = ("off","prefix_snapshot")` / `PLANNED = ("prefix_delta","prefix_delta_lean")`(`config.py:72-74`)。放开一格 = 把 `prefix_delta` 挪进 `IMPLEMENTED`,校验器(`:1669-1688`)与运行期折回(`reasoning_retrieval.py:3726-3756`)两处判据一行不改。参数化用例按常量遍历自动覆盖。两个新配置是预算数值不是策略位,按四项既有预算的先例落在 `_reflect_v2_context` 的 `getattr` 里。形状校验范本 `validate_reflect_evidence_chars`(`config.py:1592-1678`)。

### M6 测量

`ctx_chars_{s,c,k,d,t}` 现口径(`reasoning_retrieval.py:477-486`),恒等式 `S+C+K+D+T` = 两条消息正文字符数之和有用例钉住。delta 下 `d` 吃掉"证据卡块之后、T 之前的一切"(观察账 + 全部增量块),`k` 仍是 K 的卡片块。新键必须走 `_registered_measure_key`(导入期 `raise`)与 `_TraceRecorder.__call__` 的 ⊆ 守卫(`raise`),所以 `context_rebuilds` / `context_fallback` / `delta_blocks` 先进 `REFLECT_MEASUREMENT_DETAIL_KEYS`(`reasoning_trace_stats.py:317-325`)。PR-2 刻意未登记前两键(实施记录原话),PR-3 同 diff 登记,读侧加两列。

### M7 回退记在哪,与布局判据如何共存

`_reflect_prefix_layout`(`:4244-4261`)改动只有一处:第二个判据换成 `in ("prefix_snapshot", "prefix_delta")`。两条臂消息形状相同,回退不经过这里;回退只发生在 `_reflect_v2_context` 内部选"增量装配还是 P 的有界选择",判据是投影上那格 `fallback` 布尔,不是第二次读策略位。`context_fallback` 不新开 trace 步(§12),只能是 reflect 步的稀疏 detail;它是行为事实,测量关时也必须可见(载体见 Q7)。

### M8 rig / A-B

`reflect_ab.ARMS`(`:96-100`)加一行 `("v2","prefix_delta")`;`PAIR_ARM_COUNT`、`parse_arms`、`assert_optimization_matches_evidence`、`AB_DEFAULT_ARMS`、`_ab_preflight`、逐臂 Settings 构造、`analyze`(`ARM_DIMENSIONS`、`optimization_pair_table`)全部自动覆盖,不动。

## §2 拍板(主 agent,2026-09-10;全部采纳 Plan 代理的建议)

| # | 问题 | 拍板 | 理由 |
| --- | --- | --- | --- |
| Q1 | D 的块粒度 | **一轮一块**(块内三节:新增卡 → 本轮观察 → 已接受的方面更新;缺哪节不出哪节) | 块数 = 已完成动作数,前缀分叉点唯一;三块各带标题每轮多付上百字节;一块内三节顺序天然稳定 |
| Q2 | `ctx_chars_d` 口径 | **位置口径**:`k` = K 的卡片块,`d` = 观察账块 + 全部增量块;恒等式不变;**新增 `delta_blocks`** 键(本轮消息里 D 的块数) | 恒等式是五个数唯一的自洽判据;池账是投影内部判据,不是给读表人的量 |
| Q3 | 补充卡文本形式 | `key=` **之后**插一格服务端字面量的版本标记(如 `补充摘录 v2（上文同 key 的卡未被改写）`),`key` 那一格逐字不变 | 绑定校验读原始键;字面量不受 `_collapse` 归一影响;与新证据在同一列可区分 |
| Q4 | 回退是否可逆 | **不可逆**,本 run 剩余轮全走 P 的有界选择 | 设计 §5.2 原话;可逆会让 D 块来回出现/消失;§13 要一个 run 一格布尔 |
| Q5 | 首轮 K 目标比例是否也适用 P | **否** | 设计 §5.2 明写;P 的证据选择与 off 逐项等价是硬约束 |
| Q6 | delta 下 `RECENT_OBSERVATIONS` 取值 | **保持 6**,只改语义 | 设计 §5.1:先保持 6,选出布局后再比 2 与 6 |
| Q7 | `context_rebuilds`/`context_fallback` 的载体 | **`prefix_delta` 下无条件构造 `ReflectMeasurement`**,加 `measures_messages: bool`;字节序列化与公共前缀那半仍由 `REASONING_REFLECT_MEASURE_CONTEXT` 单独开关 | 回退与压缩是行为事实,测量关时也要可见;复用唯一落账点与唯一键集守卫;T-PS3 那条"测量关 ⇒ 不构造缓存"在 delta 臂改写为"测量关 ⇒ 不序列化任何消息",是已登记偏离,类 docstring 与部署文档如实改 |
| Q8 | 补充卡检测范围 | 只对**本轮 `fresh_result_ids()` 里、且已在冻结表中**的键重算摘录并比对(至多 `DELTA_CARDS` 张) | 全量比对是每轮 O(池) 次 `select_excerpt`,正是 delta 要省的开销 |
| Q9 | `prefix_delta_lean` | 本期仍拒,`PLANNED` 收窄成一格 | 照 PR-2 Q1 |

### Q8 的评审后修正(2026-09-10,T-PD5 实施后)

上表 Q8 的候选集口径**被评审证伪并已改口径**,落地的是这一版:

- 候选集 = **本轮已绑定证据的键(`evidence_bound_keys`)∩ 冻结表**,不是「本轮
  `fresh_result_ids()` ∩ 冻结表」。原口径在生产上**恒空**:`fresh_result_ids()` 的三个来源
  (trace detail 的 `result_ids`、`note_fresh_ids` 侧信道、首轮播种)都只报**真的新进池子**的
  标识,而一个键第一次进池子的那一轮要么还没被冻结、要么刚被同一轮的 K/D 用同一批检索词
  冻结——两种都不产生「同 key 的更好摘录」,整条补充卡路径因此不可达。绑定键正好是这一轮
  模型要重新判断的那些方面所引的证据(设计 §4.4),而摘录窗口跟着本轮 `action_query` 走。
- **轮转**(`ReflectDeltaState.supplement_last_key`,T-PD8 P3-4 后改按键续接):从上一轮服务的
  最后一个键在**本轮**候选序里的位置往后取至多 `max_cards` 个,末尾绕回队首,那个键不在候选序
  里了就从头。绑定键序是「大纲在前、方面轮转」的序,恒从队首取的话队首那几个键每轮被重算一遍
  摘录、队尾的键永远等不到自己那一轮;而**位置**游标在「候选序每轮移位」这种真实形态下同样会
  稳定取到同一批键(三候选、`max_cards=2`、每轮左旋一格 ⇒ 第三个键一轮都轮不到)。
- 重算的字节与冻结那一行**相同 ⇒ 一张都不发**(判重在 `supplement_for` 里,冻结字节在
  `note_shown` 时就进了 `card_variants`)。所以「这一轮摘录没变」的常态代价只有一次重算。
- **补充卡排在本轮新卡之后**,用新卡之后剩下的那点预算:新证据优先于同一条证据的更好摘录,
  两处口径必须一致,否则「优先」只活在预算里、在模型眼里反而是补充卡先出现。
- `max_cards` 上界不变(仍是 `REASONING_REFLECT_DELTA_CARDS_BY_EFFORT[档位]`),`_delta_supplements`
  的形参因此叫 `candidate_keys` 而不是 `fresh_keys`。用例:`test_delta_supplement_pass_appends_a_versioned_card_for_a_frozen_key`、
  `test_delta_run_puts_the_supplement_card_after_this_turn_new_cards`、
  `test_delta_supplement_skips_a_candidate_that_left_the_pool`、
  `test_delta_run_appends_no_supplement_card_it_should_not`。

## §3 任务

统一硬约束(与 PR-2 §2 逐字相同):`REASONING_REFLECT_OPTIMIZATION=off` 与 v2 总闸关闭态**逐字节等价**;`prefix_snapshot` 的字节与 PR-2 合入时**逐字节等价**;`run`/`_new_run_state`/`_run_enumeration` 零松弛;prompt/schema 改动用例经 `_GatedV2LLM`(`test_reasoning_retrieval.py:7117`)/`_V2ContextLLM`(`:7994`);`reasoning_actions.py`/`reasoning_context.py`/`reasoning_observation.py` 不读 Settings/DB;新逻辑零 I/O、零 LLM;字段命名不出现 `cache_hit`/命中率。收尾 `bash scripts/check.sh`。

### T-PD1 两个新配置 + 放开 `prefix_delta` 一格 · sonnet · ~110 行 · 不碰热函数

**落点** `backend/app/core/config.py:51-74`(常量段)、`:1032-1070`(字段段)、`:1592-1688`(校验器段)。

**要点**
- 新常量 `DEFAULT_REFLECT_DELTA_CARDS_BY_EFFORT = {overview:2, standard:4, deep:6, thorough:8, exhaustive:10}` 与区间常量 `REFLECT_DELTA_CARDS_MIN/MAX = 1/16`,紧挨 `DEFAULT_REFLECT_EVIDENCE_CHARS_BY_EFFORT`。
- 字段 `reasoning_reflect_delta_cards_by_effort: Annotated[Dict[str,int], NoDecode]`,别名 `REASONING_REFLECT_DELTA_CARDS_BY_EFFORT`;校验器 `validate_reflect_delta_cards` 逐条镜像 `validate_reflect_evidence_chars`(空串/非法 JSON 报错口径、恰含五档、`bool` 显式排除、区间、单调不递减)。
- 字段 `reasoning_reflect_compaction_target_ratio: float = Field(0.5, ge=0.25, le=0.75, validation_alias="REASONING_REFLECT_COMPACTION_TARGET_RATIO")`,`mode="before"` 校验器显式拒 `bool`。
- `REFLECT_OPTIMIZATION_IMPLEMENTED = ("off","prefix_snapshot","prefix_delta")`;`PLANNED = ("prefix_delta_lean",)`。两处消费点一行不改。
- 两个新配置在 `config.py` 里一次都不读。

**验收** `off` / `prefix_snapshot` 行为逐字节不变;`prefix_delta` 能起来、`prefix_delta_lean` 仍被启动期响亮拒绝;新配置默认值与 duck-typed settings 缺省口径一致(缺字段回默认,绝不当 0)。

**用例** (a) `delta_cards` 七条校验各一;(b) `ratio` 边界 0.25/0.75 过、0.24/0.76 拒、`True` 拒;(c) `IMPLEMENTED + PLANNED == REFLECT_OPTIMIZATIONS` 且不交;(d) 四取值 × v2 开/关矩阵自动扩到三格;(e) `prefix_delta_lean` 的拒绝文案列出三格。

**依赖** 无。

**实施记录(2026-09-10)** 按计划落地,无偏离。两个新常量紧挨 `DEFAULT_REFLECT_EVIDENCE_CHARS_BY_EFFORT`,
`validate_reflect_delta_cards` 逐条镜像 `validate_reflect_evidence_chars`(空串/非法 JSON/非
JSON 对象/恰含五档/`bool` 显式排除/区间/单调不递减七格);比例那一格用 `mode="before"` 校验器
显式拒 `bool`(它是 `int` 的子类,`True` 会被折算成 1.0,首版 K 就把整份预算吃干净——正是这一格
要防的事)。`IMPLEMENTED` 放开第三格、`PLANNED` 收窄成一格,两处消费点(启动期校验器、运行期
折回)一行未改,参数化用例按常量自动收窄。**评审修正轮**:启动期拒绝文案从两格变三格,两份
部署文档逐字引用的那一串同 diff 同步——`test_reflect_optimization_rejects_a_planned_but_unimplemented_value`
既断运行期整句、也断两份文档**在 `REASONING_REFLECT_OPTIMIZATION` 那一行本身**引了同一串字节
(挪到文末不算);另修了同一行里「后两格未实现」的自相矛盾表述。`config.py` 自己一次都不读这
两个新字段(唯一读点在 T-PD5,AST 判据)。

### T-PD2 domain 登记:三个新 detail 键 + 两列投影 · sonnet · ~70 行

**落点** `backend/app/domain/reasoning_trace_stats.py:95-135`(`RUN_PROJECTION_KEYS`)、`:317-325`(`REFLECT_MEASUREMENT_DETAIL_KEYS`)、`:940-1035`(投影装配)。

**要点** `REFLECT_MEASUREMENT_DETAIL_KEYS` 加 `context_rebuilds`、`context_fallback`、`delta_blocks`(不进 `REFLECT_CONTEXT_DETAIL_KEYS`)。顶层投影加两列:`context_rebuilds`(各 reflect 步的最大值)、`context_fallback`(任一步为真 ⇒ 真;一步都没带 ⇒ `None`)。`delta_blocks` 不进顶层。`OPTIMIZATIONS` 不动。缺席一律 `None`。

**验收** `assert_closed`/`assert_projection_values` 全绿;旧行可读;`legacy`/`off`/`prefix_snapshot` 行键集不变;冻结基线用例仍绿。

**用例** (a) 三键缺席 ⇒ 两列 `None`;(b) 三步各带 0/1/2 ⇒ 列出 2;(c) 只有中间一步 `context_fallback=True` ⇒ True;(d) 隐私守卫对新键只收整数/布尔;(e) `test_optimizations_match_the_settings_literal` 仍绿。

**依赖** 无。

**实施记录(2026-09-10)** 按计划落地。三键进 `REFLECT_MEASUREMENT_DETAIL_KEYS`、不进
`REFLECT_CONTEXT_DETAIL_KEYS`;顶层两列 `context_rebuilds`(max-over-present)与
`context_fallback`(any),`delta_blocks` **刻意不出列**——它不是累计量(重建会清空已发出的块),
一个 run 一格装不下它。两列的读法**刻意不同于** `_reflect_sum` 的「任一步缺 ⇒ 整列 unknown」:
写侧 `context_rebuilds` 是 run 级单调累计,取最大值才是「总共重建了几次」。`0`/`False` 与
`None` 必须分得开(`test_context_rebuilds_zero_is_distinct_from_unknown`)。**评审修正轮**:
补了「键搬到另一列/整数与布尔类型混淆」两族变异守卫,并把 `context_rebuilds` 接进 rig
`analyze` 的数值指标与配对差值表(`test_prefix_delta_columns_land_in_their_own_tables`、
`test_context_rebuilds_appears_in_the_optimization_pair_table`);隐私守卫对新键只收整数与布尔。
`OPTIMIZATIONS` 与冻结基线用例一格未动。

### T-PD3 `reasoning_observation.py`:历史折算的纯函数 · sonnet · ~60 行

**落点** `backend/app/services/reasoning_observation.py:626-732` 之后。

**要点** 新增 `fold_observation_counts(rows) -> str`:按 `OBSERVATION_STATUSES` 与 `_STATUS_LABELS` 折成一行计数披露,外加"已知截断 N 条"与"总计已尝试 N 次";词表只用现有;零值不渲染;空输入 ⇒ 空串。`render_observations`、`render_observation_row`、`ActionObservationLedger` 一个字节不动。

**用例** (a) 七种状态各一条 ⇒ 计数逐项;(b) 空输入 ⇒ 空串;(c) 全 success ⇒ 只出一格;(d) `truncated ∧ failed` 组合不重复计;(e) 词表与 `_STATUS_LABELS` 同源对账。

**依赖** 无。

**实施记录(2026-09-10)** 按计划落地,无偏离。`fold_observation_counts(rows) -> str` 只用现有
词表(`OBSERVATION_STATUSES` + `_STATUS_LABELS`,用例对账同源)、零值不渲染、空输入回空串;
`render_observations` / `render_observation_row` / `ActionObservationLedger` 一个字节未改。
它与 `render_observations` 的两个切片**刻意不相交**(口径定在 T-PD4 的 `compose_snapshot`
docstring 与 T-PD5 的 `_build_delta_snapshot` 里):喂全量行给 `render_observations` 会让「更早的
N 条未列出」与「总计已尝试 M 次」在同一条消息里互相矛盾。

### T-PD4 `reasoning_context.py`:冻结卡、增量块、快照重建(delta 本体) · **opus** · ~280 行

**落点** `reasoning_context.py:423-547`、`:617-643`、`:700-812`。

**要点**
1. `build_evidence_block(..., frozen_cards: Mapping[str,str] = EMPTY)`:键在冻结表里时用冻结文本代替现渲染;其余逻辑一格不改;默认空 ⇒ off/P 逐字节不变。
2. `build_delta_evidence_block(...) -> EvidenceSelection`:增量专用选取,共用 `_pool_index`/`excerpt_terms`/`_card_for`/`render_card`/`_diverse_order`;排除 `already_shown`;档序 **本轮新增 → 已绑定但从未展示 → 多样性补位**(与 `build_evidence_block` 相反,这是新写函数的全部理由);硬上限 `max_cards` 与 `budget_chars` 先到先止;省略数只报本块。
3. ~~`render_supplement_card(card, version)`:按 Q3。~~ **已按拍板删除**(T-PD3/4 spec 评审
   P3-1,pd3 修正轮):它零消费者,而且与 `ReflectDeltaState.supplement_for` 构成同一件事的
   两处标记——版本标记的**唯一接缝**是模块级 `_with_version_marker(line, version)`,由
   `supplement_for` 在「登记与渲染同一处」的那一步调用。
4. `ReflectDeltaState`(`@dataclass(slots=True, eq=False)`,大字符串 `repr=False`):`snapshot_evidence` / `snapshot_history` / `blocks` / `frozen_cards` / `card_variants` / `card_versions` / `observation_cursor` / `pending_aspect_notes` / `evidence_chars` / `history_chars` / `rebuilds` / `fallback` / `generation`。docstring 写明 M4 三条硬约束。
5. 纯函数 `build_delta_block(cards_text, observation_lines, aspect_notes, *, generation) -> str`、`compose_snapshot(evidence_text, history_text, folded_counts) -> Tuple[str,str]`。只吃已算好的字符串。
6. 块标题常量 `DELTA_BLOCK_TITLE`(追加不改写;同 key 补充卡是新版本,上一张仍有效)与 `SNAPSHOT_FOLD_TITLE`(折算计数不是"没发生");不与 `TURN_STATE_TITLE`/`TURN_CONTEXT_TITLE` 共用。
7. `ReflectContext.delta: str = ""` + 守卫:`_PREFIX_ONLY_FIELDS` 加 `"delta"`;`as_prefix_user_block` 在 `observations` 之后、T 之前渲染 `delta`。

**验收** 纯函数、确定性、零 I/O;`frozen_cards` 空 ⇒ `build_evidence_block` 逐字节回到接入前;`delta` 空 ⇒ `as_prefix_user_block` 逐字节回到接入前;冻结命中的键重建前后卡片文本逐字节相同;同一摘录不产生第二张补充卡。

**用例** (a) `frozen_cards` 空/非空,前者与 HEAD 逐字节比对;(b) 同 key 不同 `action_query` ⇒ 冻结生效 + v2 补充卡,`key=` 相同;(c) 同 key 相同摘录 ⇒ 不追加;(d) `max_cards=2` 候选 10 ⇒ 恰两张 + 省略 8;(e) `already_shown` 覆盖全池 ⇒ 空块;(f) `as_user_block` 遇非空 `delta` 抛;(g) `repr` 无请求正文;(h) 档序:fresh 在 bound-unshown 之前。

**依赖** T-PD3。

**实施记录(2026-09-10)** 落地时相对上面的要点有五处**刻意偏离**,均由评审裁定:

- **`build_delta_evidence_block` 只有两档**(本轮新增 → 已绑定但从未展示),**没有第三档
  「多样性补位」**(T-PD5 spec 评审存疑 2)。理由:D 只装「本轮新增或补充」(设计 §4.4),给
  它补位档会在头两轮就把首版 K 按目标比例刻意留出的空档吃光——没有新证据的那些轮里池里
  任意几张卡照样被填进 D,证据池一路涨满、第二三轮就触发重建,正是设计 §5.2 首句要挡的
  形态。`_diverse_order` 因此**只服务 K**(顺带省掉每轮一次全池排序);池里既不新增也没绑定的
  材料由 K 的第三档在**下一次重建**时按相关度带进来。
- **`already_shown` 传的是「当前可见集」**(`ReflectDeltaState.snapshot_keys ∪ block_keys` =
  当前 K 的键 ∪ 当前全部 D 块的键;T-PD8 后拆成两格,理由见 T-PD5 实施记录),不是
  `frozen_cards` 那份「曾展示」的字节表(spec 评审存疑 1)。两者在第
  一次重建之前恰好相等,之后不再相等:重建清空 `blocks` 并按新预算收缩 K,拿冻结表当可见集
  用会让一批卡既不在消息里、又被「曾展示」口径永久挡在增量之外,而且不进任何一个 `omitted`。
  `frozen_cards` 仍是只增不减的字节表,离开 K 的卡以后经档序回到某一块 D 时**复用冻结字节**
  (设计 §4.4「可从已有内存池恢复」,不违反「展示后保持不变」)。三件事——在池子里 / 曾经展示过 /
  此刻还在消息里——在投影上各占一格。
- **`render_supplement_card` 删掉**(见上面第 3 条),版本标记唯一接缝是 `_with_version_marker`;
  省略披露改成共享的 `_OMISSION_NOTE`(输出字节不变);增量块三节之间用 `\n\n`(单个 `\n` 会让
  证据卡的 `- [chunk] | key=…` 与观察行的 `- #3 [action] …` 连成同一份 bullet 列表)。
- **D 的观察节不带 `HISTORY_NOTE`**:「目的是模型当时写下的判断」这句免责由
  `DELTA_BLOCK_TITLE` 里「观察行的含义同上方观察账」一次性接过去,不再每块各付一份(二十轮
  就是上千字节纯重复;设计 §5.2 要的是两处**措辞同源**)。因此 `reasoning_context` **不 import**
  `reasoning_observation`(反之亦然),两个纯模块互不依赖,K 的历史半与折算计数在
  `reasoning_retrieval._build_delta_snapshot` 里合成。
- **预算口径**:`EvidenceSelection` 加 `cards`(键 → 真的进了那一块的**那一行字节**),
  `note_shown` 从它取字节而不是重渲染——重渲染会在「冻结的字节」与「发出去的字节」之间开一道
  缝,而这条臂的全部意义就是两者恒等。块头按每块一次计进证据池:**本块实际占用 =
  `len(DELTA_BLOCK_TITLE) + 1 + len(text)`**,`budget_chars` 因此是**含块头**的额度,块头装不下
  就返回空块;省略披露也算进硬预算(先装行、最后拼披露的话那句话不在任何一次判断里,而 delta
  的预算是 K + 所有 D 的累计)。另新增 `render_pool_cards`(补充卡检测唯一的卡面产地:按给定
  的几个键现渲染,不选卡、不看预算、池外键静默跳过)与 `ReflectContext.delta` +
  `_PREFIX_ONLY_FIELDS` 的第四格。`generation` 只在 >1 时渲染;`as_user_block` 的异常文案统一
  成 `prefix-layout payload`。

### T-PD5 `reasoning_retrieval.py`:接线、预算、重建与回退 · **opus** · ~220 行 · `run()` 零改动

**落点** `:3310-3537`、`:4244-4261`、`:4262-4367`、`:4368-4508`、`:4510-4537`、`:4539+`、`reasoning_context.ReflectMeasurement`。

**要点**
- `_ReasoningRunState.reflect_delta: Optional[ReflectDeltaState] = None`(末尾带默认;`_new_run_state` 零改动)。
- 模块级 `_PREFIX_LAYOUTS = ("prefix_snapshot","prefix_delta")`;`_reflect_prefix_layout` 第二条件改 `in _PREFIX_LAYOUTS`。这是布局分派的全部改动。
- `_reflect_v2_context` 加第三分支(判据 `reflect_optimization() == "prefix_delta" and catalog is not None`),分支体下沉到 `_reflect_delta_context(...)`。
- `_reflect_delta_context` 的顺序 = 设计 §5.2:① 取/建投影,首次用 `budget*ratio` 与 `RECENT_OBSERVATIONS` 建 K,`generation=1`;② 形成待追加事件(`rows[cursor:]`、`pending_aspect_notes`、新卡 + 按 Q8 的补充卡);③ 检查预算(`evidence_chars + 新卡 > 证据池` 或 `history_chars + 新观察 > STATE_CHARS` ⇒ 重建);④ 重建:`build_evidence_block(..., budget_chars=int(budget*ratio), frozen_cards=...)` + `render_observations(rows, recent=RECENT, state_chars=int(STATE_CHARS*ratio))` + `fold_observation_counts(更早的行)`;`blocks` 清空、累计计数重置、`generation += 1`、`rebuilds += 1`、游标推到 `len(rows)`;⑤ 重建后仍装不下最小有效新证据(至少一张新卡或一行新观察)⇒ `fallback=True`、记账、本轮起走 P 那一支;⑥ 追加 D、更新冻结表与累计计数、`ever_shown_outline_keys.update(真渲染的键)`;⑦ 返回 `ReflectContext(server_state="", evidence=K证据, observations=K历史, delta=D, contract, turn_state, static_prompt=reflect_v2_static_prompt(catalog, delta=True), measurement)`。
- 回退后那一支复用 P 分支装配,`static_prompt` 仍 `delta=True`;回退**不清空已发出的 D**(注释点名)。
- `_absorb_assessment` 在 `if not outcome.error:` 内加一行:`outcome.accepted` 非空且 `state.reflect_delta is not None` ⇒ append pending note(「本轮服务端已接受的方面更新:a2、a3(模型判断,非原文)」)。
- 测量载体(Q7):`ReflectMeasurement.measures_messages: bool = True`;`_reflect_measurement` 在 `prefix_delta` 下也构造(`measures_messages=self.reflect_measures_context()`);`_reflect_v2_attempt` 两处判据改 `measurement is not None and measurement.measures_messages`;`_measure_reflect_messages` 的 `d` 口径按 Q2;新增 `_MEASURE_CONTEXT_REBUILDS`/`_MEASURE_CONTEXT_FALLBACK`/`_MEASURE_DELTA_BLOCKS`。

**验收(§12)** 同 run 额度耗尽/方面变化/轮数增长 ⇒ S、C 字节不变,且新增 D 之前已存在的每一块 D 字节与顺序不变;热函数长度不变;`off` 与 `prefix_snapshot` 逐字节回到 PR-2 合入态;无新 I/O/LLM(断 `retrieval` 与 `model_clients` 调用计数);长历史触发预算前重建;目标比例不满足仍守硬预算;回退不增调用/步骤、不清空 `ever_shown_outline_keys`、不清空候选池、不重置配额。

**用例** (a) `_V2ContextLLM` 加 `delta_blocks(turn)`,四轮 run 断"第 k 轮的前 k-1 块 D 与第 k-1 轮逐字节相同";(b) 额度耗尽多轮 ⇒ S、C 不变;(c) 超预算历史 ⇒ 重建,`context_rebuilds` 0→1,冻结卡在新 K 里字节相同;(d) `ratio=0.25` + 超长卡 ⇒ 回退,`context_fallback=True`,此后走 P,`ever_shown_outline_keys` 只增不减,模型调用次数与不回退时相同;(e) 未展示的卡被引用 ⇒ 仍被剔;(f) 同 key 摘录升级 ⇒ 补充卡进 D、绑定不变;(g) 已支撑方面撤销/冲突、旧枚举 complete→conflict 在 delta 重跑;(h) 加预算重试 ⇒ D 只追加一次;(i) 测量关 + `prefix_delta` ⇒ 只有那三个键、`serialize_provider_messages` 零调用;(j) 整份 assessment 越界被折 ⇒ D 里没有方面更新那一节;(k) 一个 run 内 `context_rebuilds` ≤ 轮数。

**依赖** T-PD1、T-PD2、T-PD4、T-PD6。

**实施记录(2026-09-10)** `run()` 零改动、七步顺序与要点一致;下面这些是评审两轮之后的**最终
落法**,与上面要点不同的地方都在这里:

- **重建触发点维持「一张新卡都装不下」**(spec 评审 P3-2 的取舍,刻意):判据是
  `cards.omitted and not cards.shown_keys`,不是「本轮某张卡塞不进剩余额度」。后者每有一张
  超额大卡就压一次 K;前者只在「这一块什么新证据都发不出去」时动手,重建抖动少一个数量级。
  代价如实登记:紧预算下一张新卡可能**晚几轮**才进上文——它有省略披露,下一次重建按同一档序
  回得来。历史池那一侧的触发点仍是严格越界(`history_chars + history_add > state_chars`,
  `>` 边界有自标定用例)。
- **重建带迟滞**(quality 评审 P2-3,`ReflectDeltaState.rebuilt_last_turn`):上一轮刚压紧过
  一版、这一轮又连一张新卡都装不下 ⇒ 这个预算下压缩已经无效,第二次重建是纯空转(每轮一次
  全池 `build_evidence_block` + K 重写 ⇒ 公共前缀塌回只剩 C,这条臂在它自己要改进的两个轴上
  反而比 `prefix_snapshot` 更贵),那时**直接走回退**。历史池溢出触发的重建**不受**迟滞约束
  (账本真的又长了,压缩对它仍然有效)——但这句只在**本轮不拥挤**时成立:判据是
  `crowded and rebuilt_last_turn`,同一轮里既历史溢出又一张新卡都装不下时迟滞照样赢。迟滞只看
  **上一轮**:中间隔一轮没重建就清零(`test_delta_hysteresis_clears_after_a_turn_without_a_rebuild`
  ——粘滞的那一版会让一次早期重建之后任何一次「装不下」都直接不可逆回退)。用例:现实预算下
  `rebuilds < turns`,连续两轮装不下 ⇒ `context_fallback=True`。
- **回退保留已发出的 D**(spec P2-1(a) / quality P2-2):触发回退的那一步**本身就是**一次重建,
  而重建第一件事就是清空 `blocks` ——所以重建前先存一份 `kept_blocks`,走到回退就原样还原。
  回退轮的 K 也传 `frozen_cards`(spec P2-2):同一个键在上文的 D 里是冻结字节,在同一条消息的
  K 里按本轮检索词重算的话,同一条证据就有了**两种都不带版本标记**的写法。`static_delta`
  改成与 `delta=` **同源**的 `delta is not None`(P3-6),所以「发不发那四句读法规则」与「有没有
  D 这条通道」不可能分歧。`pending_aspect_notes` 回退时清空、此后不再 append(那一格从此没有
  消费者,P3-9)。
- ⚠ **回退之后两笔账收缩成「保留 D 那一半」,并成为 P 那一支的额度来源**(T-PD8 质量二审 P1-1
  的拍板;此前那一版写的是「两笔账停用」,而那样一来回退轮及其**此后每一轮**的证据池实测到
  1.87×档位,K 与保留 D 里还出现逐字节相同、两份都不带版本标记的同一张卡)。落法:两条回退支
  共用 `_delta_fallback(delta, carried)` ——保留 D 原样还原、`block_keys` 还原、K 的两块字节清空、
  两笔账各减掉那一版 K 的对应半、`pending_aspect_notes` 清空;`_carried_delta` 是那条减法的**一处
  口径**,重建前存一份、回退之后 P 那一支每轮读一次。P 那一支据此把 K 的额度定成「档位证据池 −
  保留 D 的证据半」、近期观察窗定成「`state_chars` − 保留 D 的历史半」,并把保留 D 里已可见的键
  (`block_keys`)排除在 `bound_keys`/`fresh_keys` 之外。于是**一条消息的证据合计 ≤ 档位证据池**、
  同一条证据不会以两份都不带版本标记的写法出现两遍。代价如实登记:保留 D 把池子吃得多时回退
  之后的 K 可以是空的(`_keep_blocks_run` 实测就是这种形态)——那时模型手里那几块 D 正是它已经
  读过的那批证据。`delta is None`(`off` / `prefix_snapshot`)⇒ 四格全中性,两条臂逐字节不变
  (完整 run 消息序列 + 全 detail 对着 `663e05cf9` 四组比对通过)。真正要守住的两件事仍各自成立:
  上文已发出的 D 一个字节没变,`ever_shown_outline_keys` 只登记真发出去过的键。
- **可见集拆成 `snapshot_keys` + `block_keys` 两格**(同一条拍板的前提):`already_shown` 要的是
  两格的并集,而回退之后 P 那一支要问的是「**保留 D 里**已经可见的是哪些键」——存一份并集答不出
  这个问题(里面混着那一版已经不在消息里的 K 的键,排除它们等于让那些键此后永不可见)。
- **`context_rebuilds` 计的是重建动作次数**(含触发回退那一轮被丢弃的那次:全池重选的开销真的
  付了),比模型看过的 K 版本数多一格;读侧文档在 `product-and-api{,_zh}.md` 那一列点名这件事。
- **被丢弃那一版 K 一格都不登记**(spec 存疑 1):`ever_shown_outline_keys.update` 挪到「确认不
  回退」之后。否则模型从没见过卡面的键会凭「它在某一版被选中过」取得绑定资格。
- **回退判据 = 「本轮有候选新卡且重建后一张都装不下」**(spec P3-1 / quality P2-1),删掉结构性
  恒真的 `not pending_rows` 合取项(重建已把游标推到账本末尾);**无候选新卡 ⇒ 不回退**(那是
  「这一轮没有新证据」,不是「装不下新证据」,而 D 本来就允许为空)。
- **历史记账在重建分支之后重算**(quality P1-1):重建把游标推到账本末尾,本轮 D 里一行观察都
  没有,记账因此是 0。用重建之前那个 `history_add` 会让同一批行被计两遍(一遍在新 K 的
  `snapshot_history` 里、一遍在这一笔追加里),而它最大可以接近整份 `state_chars` ——重建刚做完
  就可能已经越过阈值,下一轮无条件再压一次。恒等式用例:`history_chars == len(snapshot_history)
  + Σ(真进块的观察行 + notes)`。
- **补充卡按 Q8 修正后的口径**(见 §2 后面那一节):候选集 `bound_keys ∩ frozen_cards`、
  按 `supplement_last_key` 轮转(记上一轮服务的最后一个键,按它在**本轮**候选序里的位置往后
  接;存位置的游标在「候选序每轮移位」这种形态下会把队尾的键饿死,T-PD8 P3-4)、重算摘录与冻结
  行不同才发、排在本轮新卡**之后**、装不下就 `continue` 而不是 `break`。`∩ frozen_cards` 这道
  收窄只留在 callee 一处(调用点只传绑定键序,P3-6)。额度按 `_SUPPLEMENT_CARD_OVERHEAD` 这个**上界**预留(版本标记的
  长度只有调用之后才知道,而 `supplement_for` 的调用契约不允许「调了再丢」——那一份更好的摘录
  会从此每轮命中判重、恒返回空串,永远到不了模型,且没有任何计数披露)。上界从产地那一份
  字面量算(版本号取 10**6),不手写数字。
- **H 取舍(块头不双重预留)**:`_delta_cards` 传下去的 `budget_chars` 是 `budget -
  evidence_chars`,而 `build_delta_evidence_block` 自己的硬界**已经含块头**
  (`used` 从 `len(DELTA_BLOCK_TITLE) + 1` 起算);记账侧再加一次 `_DELTA_HEAD_CHARS` 是同一笔
  的**记账**,不是第二次预留。两处同一个常量、同一口径,断言真把块头计进硬预算。
- **每字段一个读点**(quality P3-5):六项预算的 AST 唯一读点守卫参数化扩到六项——四项既有的在
  `_reflect_v2_context` 方法体内,两个新配置各在自己那个专用 helper(`_delta_max_cards` /
  `_delta_target_ratio`,各只有一个调用点)。`optimization` 往 `_reflect_measurement` **下传**,
  不再第三次读策略位(P3-8);待追加的观察行只渲染一次、记账与 `build_delta_block` 复用同一份
  列表(P3-7)。
- **测量载体**按拍板 Q7:`ReflectMeasurement.measures_messages`,`prefix_delta` 下无条件构造,
  为假时一个字节都不序列化、只剩那三个行为事实键。`d` 按拍板 Q2 是**位置口径**(观察账块 +
  本轮全部增量块)。另加**导入期对账守卫**:`set(REFLECT_OPTIMIZATION_IMPLEMENTED) != {"off"} |
  set(_PREFIX_LAYOUTS)` 时 `raise`(quality 评审 P1),挡住「放开一格却忘接线 ⇒ 那条臂能起来、
  却发 off 布局」这种 rig 也拦不住的形态。

**codex #707 第 1 轮的两条 P2(2026-09-10,PR #707 收尾)**

- **保留 D 的键排除要覆盖 `build_evidence_block` 的全部三档。** 原来的落法是在 P 那一支自己
  滤 `bound_keys`/`fresh_keys`,而第三档(历史代表 + 来源多样性)的键序是
  `build_evidence_block` **自己**从池子里算的(`_diverse_order(index)`)——调用方手上没有那份
  序,于是只挡住了三分之二:一张保留 D 里已经发过的卡照样能经第三档回到 K,一条消息里出现两
  份都不带版本标记的同一张卡,还占掉本该给别的证据的额度(§5 风险 4)。改法:
  `build_evidence_block(..., exclude_keys: Collection[str] = ())`,实现是**预置 `seen`** ——被
  排除的键连候选序都进不去,因此也不计进 `omitted`(那个数说的是「候选里本轮没展开的」,而这些
  键此刻在别的块里**可见**);调用点只留 `exclude_keys=carried.keys` 一处排除,不再逐档滤(同一
  条判据的第二份实现改一处时另一处会静默变陈)。默认空 ⇒ off/P 逐字节不变。原来那条端到端用例
  (`test_delta_fallback_turn_keeps_its_own_cards_out_of_the_kept_blocks`)在「只滤两档」这个变异
  下**是绿的**(那条脚本的池子小、那张卡本轮同时在绑定档里),所以补了产地那条
  `test_evidence_block_excludes_the_given_keys_from_every_tier`:fixture 里被排除的键**只可能
  经第三档**被选中,两个变异各自实测红。
- **准入要判「完整的待追加块」对两个池各要多少字节。** 原来的判据只有「有候选卡却一张都装不
  下」(`crowded`)与「历史池被观察行顶破」,于是**只带观察行/方面 note、没有候选新卡**的那些轮
  `crowded` 恒假 ⇒ 块头无条件追加,而历史那一笔也漏了 `pending_aspect_notes`;delta 的两笔账是
  **逐块累计**的,一次放过就一路带下去(codex 复现:995/5990 用量、1000/6000 上限 ⇒ 追加后
  1071/6017 且不重建;本地变异实测 6071 → 6147 → 6223 → 6299)。改法:新增
  `_delta_pending_charges(cards_text, lines, notes)` 在准入之前算两笔费用——块头(`_DELTA_HEAD_CHARS`)
  + 卡片节 → 证据池,观察行 + notes(各 `_joined_chars`)→ 历史池,**与落账同一处口径**(历史那
  一笔的加数直接就是它,所以准入与记账不可能分歧);「这一块会不会是空的」由
  `build_delta_block` 自己回答(空 ⇒ 两笔各 0,什么都不追加),不在第二处重写那条空判据。任一池
  顶破 ⇒ 走重建路径,重建之后重算一次。⚠ **拍板:唯一允许越界的形态** = 本轮没有候选新卡、而
  块头(或那句 note)在重建之后**仍然**装不下 ⇒ 那一块**照发**,记账如实记成超出。观察行是本轮
  那次动作在上下文里的唯一记录,把它推到下一轮等于让模型看不见自己刚做过什么;而「有候选新卡
  却装不下」仍走既有回退判据。既然记账如实,下一轮的准入必然仍在阈值之上 ⇒ **下一轮必重建**,
  越界是一轮的事、不会一路带下去。两条新用例把边界 `pinch` 到 codex 那个形态上
  (`test_delta_counts_the_block_head_of_an_observation_only_pending_block` /
  `..._the_pending_aspect_notes_against_the_history_pool`);「准入与落账**一起**少算 notes」这个
  变异在它们上是绿的(账面自洽),那一格由既有的「记账 == 真发出的字节」
  (`test_delta_history_charge_counts_the_accepted_aspect_note`)接住,已实测红。
- 两条改动之后 `off` / `prefix_snapshot` 仍**逐字节**回到 `663e05cf9`(完整 run 消息序列 +
  schema hints + 全 detail,测量开/关四组比对通过),热函数零松弛。

### T-PD6 `prompts.py`:S 的 delta 段 · sonnet · ~45 行 · 过 `_GatedV2LLM`

**落点** `backend/app/services/prompts.py:1401-1457`。

**要点** `reflect_v2_static_prompt(catalog, *, delta: bool = False)`;`delta=True` 在 `_V2_STATIC_CATALOG_INSTRUCTION` 之后追加 `_V2_DELTA_INSTRUCTION` 四句:(1) 标着"新增"的块是只追加的历史,服务端不会回头改写;(2) 同一 `key` 可能多张卡,后来那张标着版本、是同一条证据的新摘录,前一张仍有效、绑定用同一个 key;(3) 折算计数与"未展开 N 条"是披露,不是"没找到";(4) 末尾那块 T 的执行限制优先于上文任何观察与卡片。`delta=False` ⇒ 返回值逐字节不变。

**用例** (a) `delta=False` 与 HEAD 逐字节比对;(b) delta run 多轮 ⇒ `system_prompt(turn)` 全等;(c) 过 `_GatedV2LLM` 一次完整 delta run。

**依赖** 无。

**实施记录(2026-09-10)** 按计划落地。`reflect_v2_static_prompt(catalog, *, delta=False)`;
`delta=True` 在 `_V2_STATIC_CATALOG_INSTRUCTION` 之后追加 `_V2_DELTA_INSTRUCTION`(1,507 字符,
四句),`delta=False` 与接入前逐字节相同(对着 HEAD 比对的用例 + `off` golden 仍绿)。
**评审修正轮**:四句里的「指对象」被收窄——那几句指的是**块**与**卡**的读法,不越界去声明排序权
(排序权只由 T 的标题为服务端执行限制那四类声明),同轮把过宽的断言也收窄。过了 `_GatedV2LLM`
一次完整 delta run。

### T-PD7 rig / A-B 第二维放开一格 · sonnet · ~20 行

**落点** `backend/app/eval/reflect_ab.py:96-100`。`ARMS` 加 `("v2","prefix_delta")`;其余不动。

**用例** (a) `parse_arms` 三种新写法;(b) `legacy:prefix_delta` 错误文案列出四格;(c) 逐臂 Settings 回读核对对新格生效;(d) dry-run 输出不变。

**依赖** T-PD1。

**实施记录(2026-09-10)** 按计划只动 `ARMS` 一行(加 `("v2","prefix_delta")`);`PAIR_ARM_COUNT`、
`parse_arms`、`assert_optimization_matches_evidence`、`AB_DEFAULT_ARMS`、`_ab_preflight`、逐臂
Settings 构造与 `analyze` 全部按常量自动覆盖,一格未改。**评审修正轮**:`_settings_by_arm` 的
docstring 举例「未实现取值构造期抛」改举 `prefix_delta_lean`(原来举的 `prefix_delta` 已经能起来),
并补齐用例缺的一格。`scripts/README.md` 的第二维散文同期收窄(T-PD9)。

### T-PD8 验证清单与聚焦测试 · sonnet(用例)+ opus(变异复核)

见 §4。额外:对 T-PD5 的三处不可协商性质各补一次变异验证——(a) D 改成每轮重渲染 ⇒ 必须红;(b) 重建时去掉 `frozen_cards` ⇒ 必须红;(c) 回退改成可逆 ⇒ 必须红。

**实施记录(2026-09-10)** 三处不可协商性质的变异各自验证为红,并入 T-PD5 的用例批。修正轮之后
自己又跑了 33 组变异,**三组逃逸并各补一条用例**(补后逐条复跑为红):`static_delta` 回到「第二次
读策略位」(补一条中途把策略位翻回 `prefix_snapshot` 的 run:消息里 D 还在、S 掉了那四句读法规则,
`system_prompts` 于是在一个 run 内出现两种字节)、回退分支不清 `pending_aspect_notes`、回退之后
继续 append(补一条按投影断的用例,收尾轮改交一份**会被接受**的自评,让「回退之后还攒不攒」有得看)。
**仍然逃逸而刻意不补的两组**,均可证等价并在此登记理由:① 回退判据加回 `not pending_lines`
——重建已把游标推到账本末尾,该合取项恒真;② 增量块选卡循环里 `used` 的起点——它只影响「何时
停止构造」的启发式,返回值的硬保证由收敛循环的终判(`块头 + 正文 <= 预算`)给出,那一格去掉即红。
另按 T-PD1/7 评审补了 `validate_reflect_delta_cards`「必须是 JSON 对象」分支的一格用例
(`'[1,2,3]'` 与标量 `5`,镜像 `test_reflect_evidence_chars_rejects_bad_mappings` 第八格)。

### T-PD9 文档 · sonnet · 只改文档与注释

见 §4 文档落点。

### 依赖与并行

```
T-PD1 ──┬─→ T-PD7
T-PD2 ──┤
T-PD3 ─→ T-PD4 ──┼─→ T-PD5 ─→ T-PD8 ─→ T-PD9
T-PD6 ──┘
```

### 刻意不做

不实现 `prefix_delta_lean`;不改 `render_observations`/`render_observation_row`/`ActionObservationLedger`;不改 P/off 的证据选择判据;不改首轮查询宽度、检索范围、最大步数、工具次数;不加原生 tool calling/完整聊天历史/reasoning 回放;不新增读证据工具、不为压缩加 LLM 调用;请求文本不进日志/轨迹/投影;生产策略不加实验标记(E1 接缝留 PR-5);不改 `policy_version` 语义;不动 `recognized_actions` 与 schema 枚举;`AB_DEFAULT_ARMS` 不变。

## §4 验证清单与文档落点

### 映射设计 §12

| §12 条目 | 落点 | 状态 |
| --- | --- | --- |
| 同 run 额度耗尽/方面变化/轮数增长 ⇒ S、C 字节不变 | T-PD5 (b) | PR-2 已有 P 版,delta 重跑 |
| **新增 D 前已存在的 D 字节与顺序不变** | T-PD5 (a) + 变异 (a) | 本期核心断言 |
| provider-facing 消息覆盖 wrapper/schema、动态值没插进稳定块开头 | T-PD5 (b) | PR-2 机制复用 |
| P 与基线传达同一批证据事实/执行限制 | PR-2 既有 | 本期不打破 |
| **D 的省略有披露、用户约束不丢** | T-PD4 (d) + T-PD5 (c) | 本期新增 |
| 静态目录已耗尽工具不能执行 | PR-2 T-PS7 | delta 下重跑 |
| **长历史触发预算前重建** | T-PD5 (c) | 本期新增 |
| **目标比例无法满足仍守硬预算** | T-PD5 (d) | 本期新增 |
| **回退不增调用/步骤、不清空结果或绑定** | T-PD5 (d) + 变异 (c) | 本期新增 |
| 已支撑方面撤销/冲突、旧枚举 complete→conflict | T-PD5 (g) | delta 重跑 |
| **同 key 摘录升级** | T-PD4 (b)(c) + T-PD5 (f) | 本期新增 |
| **非法键与未展示候选** | T-PD5 (e) | delta 重跑 |
| 无 usage/cached/finish_reason 仍出表 | T-PD2 | 新两列同口径 |
| Knowhow/legacy 不受影响 | T-PD1 (d) | 单点自动覆盖 |
| 既有聚焦测试可达 | `scripts/check.sh` | 收尾门 |

### 文档落点

- `docs/deployment-and-configuration{,_zh}.md`(`_zh:847-850` / `en:1046-1049`):① 新增两条配置;② `RECENT_OBSERVATIONS` 的"预告"改成当前行为;③ `REASONING_REFLECT_OPTIMIZATION` 三格已实现、`prefix_delta_lean` 仍拒,补"`prefix_delta` 做的事"与如实字节账(S 相对 `prefix_snapshot` 多 1,507 字符、落在逐轮不变那一段;每块 D 一个 75 字符块头 + 换行,重建后的块再多约 14 字符版本标记;补充卡相对原卡约 +26 字符;省的是每轮一次全池重选重排与摘录重算,**哪一侧更大取决于题型与轮数,一个数都还没量**),并**删掉** T-PD1 修正轮留下的那句"T-PD5 落地前能起来但走 off 布局(T-PD9 收)";④ `MEASURE_CONTEXT` 按 Q7 改准(delta 下载体无条件构造、只为那三个行为事实键;序列化那半仍由本开关管,为假时 `serialize_provider_messages` 零调用),同时把 `OPTIMIZATION` 那句"本项自己一个数都不写"补上这条例外。
- `docs/product-and-api{,_zh}.md`(`_zh:1426-1436`):`prefix_delta` 的分块与稳定性口径(逐轮不变的是 wrapper、S、C 与全部已发出的 D);`context_chars` 在 delta 下的 `d` 口径;`message_prefix_bytes` 在这条臂上的期望形状(随轮数增长、重建轮回落,回落不是缺陷);投影「九列」改**十一列**并写清新两列口径(`context_rebuilds` max-over-present、`context_fallback` any、`0`/`false` ≠ 缺值、两列与测量开关无关只与臂有关)、`delta_blocks` 已登记但不出顶层列。
- `architecture.md:111`(实况,T-PD9 落地时按代码修正了两处计划字面):`ReflectDeltaState` 的
  owner/把手/三条硬约束与「在池子里 / 曾经展示过 / 此刻还在消息里」三格、`rebuilt_last_turn`
  与 `supplement_last_key` 的语义、`_reflect_delta_context` **七步**(计划原写六步)与 `run()` 零改动、
  `_PREFIX_LAYOUTS` 单点**加导入期对账守卫**、回退判据不是第二次读策略位且回退**保留已发出的
  D**、两笔累计账回退后收缩成保留 D 那一半并成为 P 支的额度来源、`ReflectContext.delta` 是
  `_PREFIX_ONLY_FIELDS` 的**第四**格(计划
  原写第三处)、**六项预算每字段一个读点**、`fold_observation_counts` 与 `render_pool_cards` 的
  产地、两个纯模块互不 import。
- `scripts/README.md`:第二维臂**四格**(`legacy:off` / `v2:off` / `v2:prefix_snapshot` /
  `v2:prefix_delta`)、一次只收一对;过期举例(`prefix_delta` 未实现 / 两格被启动期拒绝)按实况改。
- `fangan_todo.md` 新增 (k):PR-3 已交付、仍未做的是跑对照实验、开闸仍是独立决定;已知限制
  (回退不可逆且回退轮字节可超档位池、`search` 投影 `optimization` 仍恒 unknown、
  `prefix_delta_lean` 待 PR-4、D 观察节不带 `HISTORY_NOTE`、`delta_blocks` 不出顶层列);同时把
  (j) 里「两格都被启动期拒绝」那条标成已被本期部分解除。
- 不改 `AGENTS.md`/`CLAUDE.md`;不改带日期的设计稿本体,C1/C2 与九个拍板写在本计划。

## §5 风险

1. **`off` / `prefix_snapshot` 两臂字节等价必须保持。** 本期改到的共用面四处:`build_evidence_block` 加 `frozen_cards`、`ReflectContext` 加 `delta`、`reflect_v2_static_prompt` 加 `delta`、`ReflectMeasurement` 加 `measures_messages`。默认值全部中性,但"默认中性"要用例证明:T-PD4/T-PD6 各一条对着 HEAD 逐字节比对;T-PD5 一条完整 `off` run 与一条完整 `prefix_snapshot` run 的消息序列比对。
2. **不新增 I/O/LLM。** 重建路径不读 `node_context`、不用模型压缩;T-PD5 断 `retrieval` 与 `model_clients` 调用计数在开/关 delta 下相同。
3. **prompt 语义改动必须过 `_GatedV2LLM`。**
4. **冻结与绑定资格的耦合。** 补充卡的 `key=` 被 `_collapse` 归一改写 ⇒ 绑定静默失配;T-PD4 (b) 断两张卡 `key=` 逐字相同且等于池键。
5. **重建时机的震荡。** "最小有效新证据"判据写成"重建之后本轮至少能装下一张新卡或一行新观察",装不下一次性回退;T-PD5 (k) 断重建次数有上界。
6. **`_absorb_assessment` 的新写点**只能落在 `if not outcome.error:` 内;用例 (j)。
7. **两臂实验可比性。** delta 与 P 共用 S(除四句)、C、T;任何"顺手精简 T"都毁掉隔离,评审按此挡回。
8. `delta_blocks` 已采纳,不存在 Q2 否决的退化路径。
