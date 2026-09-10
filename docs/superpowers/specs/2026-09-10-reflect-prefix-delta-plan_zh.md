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

### T-PD2 domain 登记:三个新 detail 键 + 两列投影 · sonnet · ~70 行

**落点** `backend/app/domain/reasoning_trace_stats.py:95-135`(`RUN_PROJECTION_KEYS`)、`:317-325`(`REFLECT_MEASUREMENT_DETAIL_KEYS`)、`:940-1035`(投影装配)。

**要点** `REFLECT_MEASUREMENT_DETAIL_KEYS` 加 `context_rebuilds`、`context_fallback`、`delta_blocks`(不进 `REFLECT_CONTEXT_DETAIL_KEYS`)。顶层投影加两列:`context_rebuilds`(各 reflect 步的最大值)、`context_fallback`(任一步为真 ⇒ 真;一步都没带 ⇒ `None`)。`delta_blocks` 不进顶层。`OPTIMIZATIONS` 不动。缺席一律 `None`。

**验收** `assert_closed`/`assert_projection_values` 全绿;旧行可读;`legacy`/`off`/`prefix_snapshot` 行键集不变;冻结基线用例仍绿。

**用例** (a) 三键缺席 ⇒ 两列 `None`;(b) 三步各带 0/1/2 ⇒ 列出 2;(c) 只有中间一步 `context_fallback=True` ⇒ True;(d) 隐私守卫对新键只收整数/布尔;(e) `test_optimizations_match_the_settings_literal` 仍绿。

**依赖** 无。

### T-PD3 `reasoning_observation.py`:历史折算的纯函数 · sonnet · ~60 行

**落点** `backend/app/services/reasoning_observation.py:626-732` 之后。

**要点** 新增 `fold_observation_counts(rows) -> str`:按 `OBSERVATION_STATUSES` 与 `_STATUS_LABELS` 折成一行计数披露,外加"已知截断 N 条"与"总计已尝试 N 次";词表只用现有;零值不渲染;空输入 ⇒ 空串。`render_observations`、`render_observation_row`、`ActionObservationLedger` 一个字节不动。

**用例** (a) 七种状态各一条 ⇒ 计数逐项;(b) 空输入 ⇒ 空串;(c) 全 success ⇒ 只出一格;(d) `truncated ∧ failed` 组合不重复计;(e) 词表与 `_STATUS_LABELS` 同源对账。

**依赖** 无。

### T-PD4 `reasoning_context.py`:冻结卡、增量块、快照重建(delta 本体) · **opus** · ~280 行

**落点** `reasoning_context.py:423-547`、`:617-643`、`:700-812`。

**要点**
1. `build_evidence_block(..., frozen_cards: Mapping[str,str] = EMPTY)`:键在冻结表里时用冻结文本代替现渲染;其余逻辑一格不改;默认空 ⇒ off/P 逐字节不变。
2. `build_delta_evidence_block(...) -> EvidenceSelection`:增量专用选取,共用 `_pool_index`/`excerpt_terms`/`_card_for`/`render_card`/`_diverse_order`;排除 `already_shown`;档序 **本轮新增 → 已绑定但从未展示 → 多样性补位**(与 `build_evidence_block` 相反,这是新写函数的全部理由);硬上限 `max_cards` 与 `budget_chars` 先到先止;省略数只报本块。
3. `render_supplement_card(card, version)`:按 Q3。
4. `ReflectDeltaState`(`@dataclass(slots=True, eq=False)`,大字符串 `repr=False`):`snapshot_evidence` / `snapshot_history` / `blocks` / `frozen_cards` / `card_variants` / `card_versions` / `observation_cursor` / `pending_aspect_notes` / `evidence_chars` / `history_chars` / `rebuilds` / `fallback` / `generation`。docstring 写明 M4 三条硬约束。
5. 纯函数 `build_delta_block(cards_text, observation_lines, aspect_notes, *, generation) -> str`、`compose_snapshot(evidence_text, history_text, folded_counts) -> Tuple[str,str]`。只吃已算好的字符串。
6. 块标题常量 `DELTA_BLOCK_TITLE`(追加不改写;同 key 补充卡是新版本,上一张仍有效)与 `SNAPSHOT_FOLD_TITLE`(折算计数不是"没发生");不与 `TURN_STATE_TITLE`/`TURN_CONTEXT_TITLE` 共用。
7. `ReflectContext.delta: str = ""` + 守卫:`_PREFIX_ONLY_FIELDS` 加 `"delta"`;`as_prefix_user_block` 在 `observations` 之后、T 之前渲染 `delta`。

**验收** 纯函数、确定性、零 I/O;`frozen_cards` 空 ⇒ `build_evidence_block` 逐字节回到接入前;`delta` 空 ⇒ `as_prefix_user_block` 逐字节回到接入前;冻结命中的键重建前后卡片文本逐字节相同;同一摘录不产生第二张补充卡。

**用例** (a) `frozen_cards` 空/非空,前者与 HEAD 逐字节比对;(b) 同 key 不同 `action_query` ⇒ 冻结生效 + v2 补充卡,`key=` 相同;(c) 同 key 相同摘录 ⇒ 不追加;(d) `max_cards=2` 候选 10 ⇒ 恰两张 + 省略 8;(e) `already_shown` 覆盖全池 ⇒ 空块;(f) `as_user_block` 遇非空 `delta` 抛;(g) `repr` 无请求正文;(h) 档序:fresh 在 bound-unshown 之前。

**依赖** T-PD3。

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

### T-PD6 `prompts.py`:S 的 delta 段 · sonnet · ~45 行 · 过 `_GatedV2LLM`

**落点** `backend/app/services/prompts.py:1401-1457`。

**要点** `reflect_v2_static_prompt(catalog, *, delta: bool = False)`;`delta=True` 在 `_V2_STATIC_CATALOG_INSTRUCTION` 之后追加 `_V2_DELTA_INSTRUCTION` 四句:(1) 标着"新增"的块是只追加的历史,服务端不会回头改写;(2) 同一 `key` 可能多张卡,后来那张标着版本、是同一条证据的新摘录,前一张仍有效、绑定用同一个 key;(3) 折算计数与"未展开 N 条"是披露,不是"没找到";(4) 末尾那块 T 的执行限制优先于上文任何观察与卡片。`delta=False` ⇒ 返回值逐字节不变。

**用例** (a) `delta=False` 与 HEAD 逐字节比对;(b) delta run 多轮 ⇒ `system_prompt(turn)` 全等;(c) 过 `_GatedV2LLM` 一次完整 delta run。

**依赖** 无。

### T-PD7 rig / A-B 第二维放开一格 · sonnet · ~20 行

**落点** `backend/app/eval/reflect_ab.py:96-100`。`ARMS` 加 `("v2","prefix_delta")`;其余不动。

**用例** (a) `parse_arms` 三种新写法;(b) `legacy:prefix_delta` 错误文案列出四格;(c) 逐臂 Settings 回读核对对新格生效;(d) dry-run 输出不变。

**依赖** T-PD1。

### T-PD8 验证清单与聚焦测试 · sonnet(用例)+ opus(变异复核)

见 §4。额外:对 T-PD5 的三处不可协商性质各补一次变异验证——(a) D 改成每轮重渲染 ⇒ 必须红;(b) 重建时去掉 `frozen_cards` ⇒ 必须红;(c) 回退改成可逆 ⇒ 必须红。

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

- `docs/deployment-and-configuration{,_zh}.md`(`_zh:847-850` / `en:1046-1049`):① 新增两条配置;② `RECENT_OBSERVATIONS` 的"预告"改成当前行为;③ `REASONING_REFLECT_OPTIMIZATION` 三格已实现、`prefix_delta_lean` 仍拒,补"`prefix_delta` 做的事"与如实字节账(省的是重选重排那部分,补充卡与折算披露是净增,真实收益等实验);④ `MEASURE_CONTEXT` 按 Q7 改准。
- `docs/product-and-api{,_zh}.md`(`_zh:1426-1436`):`prefix_delta` 的分块与稳定性口径(逐轮不变的是 wrapper、S、C 与全部已发出的 D);`context_chars` 在 delta 下的 `d` 口径;投影新增两列与"缺席 ≠ 0"。
- `architecture.md:111`:`ReflectDeltaState` 的 owner/把手/三条硬约束、`_reflect_delta_context` 六步与 `run()` 零改动、`_PREFIX_LAYOUTS` 单点、回退判据不是第二次读策略位、`ReflectContext.delta` 第三处互斥守卫、`fold_observation_counts` 产地。
- `scripts/README.md`:第二维臂多一格。
- `fangan_todo.md` (j):PR-3 已交付、仍未做的是跑对照实验、开闸仍是独立决定;已知限制追加(回退不可逆、`search` 投影 `optimization` 仍恒 unknown、`prefix_delta_lean` 待 PR-4)。
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
