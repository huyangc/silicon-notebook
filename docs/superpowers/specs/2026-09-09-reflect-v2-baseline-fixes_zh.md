# reflect v2 · PR-1 公共基线修复实施计划

日期:2026-09-09。上游:[reflect 上下文与前缀复用最终设计](2026-09-09-reflect-prefix-cache-final-design_zh.md)§7(公共基线采纳项)与主 agent review 调整 1/2;生产证据(GLM-5.3,136 run search-only)见会话 scratchpad `prod-glm53-evidence.md`。本计划是 B/P/D/L 四臂共用的执行基线;合入后旧批次数据只作历史参考。

## 0. 结论先行

- **枚举规模守卫可零新增 I/O 判定**:`sources` 的 `total` 每 run 已在集合地图里算过一次,只是对象被丢掉只留字符串;额度是 `enum_rows_per_run - enum_rows_used`(deep 300 / standard 200,页 50),生产那 8 次正是一页把整 run 额度吃光。`complete=False` 时 `enum:sources…` 键本来就不签发,只缺回归用例。
- **`exact_lookup` 的形状判据在执行层**(`exact_probe_terms`,纯函数,可前移到解析层),v2 参数说明只字未提,模型每次要先烧一轮才从回喂里学到。
- **`enumeration_rejected` 的唯一模型可触达产生点**是执行器的 `source is not in scope` / `not enumerable`,入口是 v2 投影里向模型开放却「内部 id 从不上屏」的 `enumerate.source_id` 自由文本槽。
- **追问不是调用**:`_nudge_missing_assessment` 零模型调用零 I/O,其成本已在下一轮的 reflect 步上;长 `answer` 步的真身是收尾配额重排(含缓存未命中逐子查询重检索),它没有自己的 trace 步。
- **assessment 整份拒绝点是 `AspectLedger.apply` 的全有或全无**;六条解耦要求里两条今天已成立(非法键剔除+降级、逐方面披露槽位),四条(未知方面、重复方面、`evidence_keys` 超限、`gap` 超限)仍整轮作废。

## 1. 机制核实(文件:行号以 HEAD 8cfa68c11 为准)

- A:`collection_enumeration.py:1122` `total = scope_source_plan(db, notebook_ids).total`;`collection_catalog.render_collection_map` 的 `sources: N (current notebook: M)`;`reasoning_retrieval.py:5023` 只留 `collection_map_text`;额度 `state.enum_limits.enum_rows_per_run - state.enum_rows_used`,`enum_page_size` 恒 50;`complete_enumeration_keys`(`reasoning_retrieval.py:1377`)对 `complete=False` 不签发;legacy 规模提示 `prompts.py:880-885`;v2 `_ENUMERATE_SCOPE_NOTE`(`reasoning_actions.py:135`)只有前半句。
- B:判据 `reasoning_retrieval.py:7239` `elif not probed`,`probed` 来自 `app/repositories/lexical_query.py::exact_probe_terms`;`EXACT_LOOKUP_ACTION` 的 `term` note 无判据;被拒后经 `_NOT_A_NAME_NOTE` 回喂;`enumeration_rejected` 产生点 `collection_enumeration.py:1564/1569`;kind 白名单在 v2 下被 `_v2_enum` 提前折成 `invalid_argument:kind`。
- C:`_nudge_missing_assessment` 无调用;收尾 `_quota_rerank`(`reasoning_retrieval.py:4415`,含 `_per_query_scored` 未命中时逐子查询 `self.search()`)与单查询分支 `retrieval.retrieve_scored`(~7546)都落在上一步与 `record(TraceStep(step_type="answer"))`(7568)之间;`_TraceRecorder.__call__`(2841)以相邻记账之差计时。新 step_type 需登记 `reasoning_trace_stats.STEP_TYPES` + `NON_ACTION_STEP_TYPES`、`reasoning_observation.NON_ACTION_STEP_TYPES`(否则 `test_observation_contract_covers_every_trace_step_type_in_the_retriever` 红)、前端 `TRACE_STEP_LABELS`;`durations_ms` 键开放,不需要新投影键。
- D:`AspectLedger.apply`(`reasoning_aspects.py:463`)`_plan_row` 任一原因码 ⇒ 整份不落账;`_absorb_assessment`(`reasoning_retrieval.py:3824`)据此 `_reflect_invalid(f"invalid_assessment:{why}")`;#701 的 `normalize_assessment_payload`/`note_missing_assessment`/`nudge_pending`/`restore_pending_nudge` 与本项不重叠;`_legal_keys` + `DEMOTION_KEYS_REJECTED/MISSING` 已是剔除+降级,`render_aspect_block` 已有「服务端降级: …」披露行。

## 2. 统一硬约束

关闭态(`REASONING_REFLECT_V2_ENABLED=false`)prompt/schema/trace 逐字节不变;`scripts/architecture_boundary_baseline.json::function_length_ceiling` 的 `run`(1359)/`_new_run_state`(201)/`_run_enumeration`(基线 267,T-BF5 评审后收成 260)**双向零松弛**(缩了也要同步下调);prompt/schema 改动用例经 `_GatedV2LLM`;零新增 LLM 调用、零新增 I/O;Knowhow 恒 legacy。

## 3. 任务

### T-BF1 目录枚举规模守卫(服务端)
`_first_round_prompt_blocks`(`reasoning_retrieval.py:5023`)改为先取 `collection_catalog.collection_map(nb)` 再 `render_collection_map`,对象存进 `_ReasoningRunState` 新增**带默认值**字段(`_new_run_state` 零改动);新增纯函数 `oversize_source_listing(map_sources, rows_left, factor)`;`_run_enumeration` 里把现有 `EnumerationBudget(...)` 构造收进新方法 `_enum_budget(...)`(净不增长),守卫命中时 `max_rows=min(rows_left, enum_page_size)`;门 `self.reflect_v2_active()`。不自动改范围、不拒绝动作。
验收:48839/300 场景返回 50 条、`enum_rows_used` 只扣 50、链态 `open`(不是 `conflict`)、`enum:sources` 不在 `complete_enumeration_keys`;84 篇小库逐字节同现状;baseline 三数按实际重算。

**评审后修正(规格 F2/F3、质量 P2-1/P3-1/P3-3/P3-4)**:守卫按集合泛化,纯函数拆成 `enumeration_map_count(map, collection, kind, local_only, source_id)`(分母:`sources`+`all` ⇒ `CollectionMap.sources`;`sources`+`current_notebook` ⇒ `active_sources`;`elements`+kind ⇒ 该 kind 的元素计数;`kg_objects`+type ⇒ 该类型计数;地图缺项或请求收窄到单篇 ⇒ `None` 不触发)与 `oversize_listing(map_count, rows_left)`(倍数是模块常量 `OVERSIZE_LISTING_FACTOR`,不再有只有测试会传的 `factor` 形参)。守卫再加一条 `rows_left > enum_page_size`:池只剩不到一页时它连行数都改不动,而 `oversize_sample` 的「额度没用光」在那一档是假话,自然落回 `TRUNCATED_BUDGET`。`CollectionMap` 本来就带元素 kind 与 KG 类型的计数,所以泛化**不新增任何查询**,也没有留下集合缺口。

### T-BF2 规模守卫的诚实披露
`truncated_reason` **新增第四个值 `oversize_sample`**(拍板:开):`collection_enumeration.py` 词表常量、`collection_enumeration_answer._REASON_LABELS`、`frontend/app/answer-panel.tsx::truncatedReasonLabel`(走 `vocabulary.label(map, v, 中性兜底)`,`raw-enum-fallback` 守卫拦 `MAP[x] ?? x`)、`docs/product-and-api*.md` §集合枚举工具「覆盖率合同」段。观察账零改动(`_StepContract("enumerate")` 的 `truncation_keys` 已含 `truncated_reason`)。

### T-BF3 v2 能力投影补齐规模提示
`_ENUMERATE_SCOPE_NOTE` 补 legacy `prompts.py:880-885` 对等半句(计数远大于本轮额度 ⇒ 不翻页、按计数+样本作答、建议收窄到一个来源/一节/一个主题),与地图行 `sources: N (current notebook: M)`、`_allowance_suffix` 的 `listing allowance left: R rows` 口径对齐。验收:`_GatedV2LLM` 用例断言该句每轮在 system 段;legacy prompt 一字不动。

**评审后修正(规格 F2、质量 P3-1)**:那半句与集合无关,却挂在只对 `collection="sources"` 有意义的 `scope` 参数上,等于只对一个集合说这句话。改为 `ActionDefinition` 新增**动作级** `note`(`ReflectCapabilities.note_for` 直接读定义,不进按轮收窄的 `params`;`reflect_v2_system_prompt` 渲染在动作描述之后、`arguments` 之前),`_ENUMERATE_SIZE_NOTE` 由两个枚举动作共用,措辞改成「地图行里**这个集合**的计数」;`_ENUMERATE_SCOPE_NOTE` 只留 sources 专属的范围部分。

### T-BF4 `exact_lookup` 判据前移 + 写进参数说明
`EXACT_LOOKUP_ACTION` 的 `term` note 写出形状判据(带下划线/点,或连字符+数字),与 `_NOT_A_NAME_NOTE` 共用字面,并在其后**追加**那份冻结字面只是蕴含的两条从句(至少 4 个字符、含 ASCII 字母);`_v2_apply_arguments` 的 `exact_lookup` 分支在 `clean_exact_term` 后用 `exact_probe_terms(term, honor_quotes=False)` 预校验,空 ⇒ `_V2ArgumentError("invalid_argument:term")`(零 I/O invalid 观察,`status_for_skip` 已归 `STATUS_INVALID`)。**回喂不走 `feed_exact_lookup_skip`**(评审修正):那份 legacy 散文账目只由 `legacy_action_ledger_note` 渲染,而它的判据是 `capabilities is None` —— v2 下永不拼接,写进去是死代码。被拒的词改由 `parse_reflect_v2` 写进 `invalid_request_identity`,经 `v2_request_identity` 落进动作观察账的「请求」列,与原因码同一行上屏;"该给什么"那半由每轮常驻的参数说明承担。legacy 执行层分支原样保留。

### T-BF5 `enumeration_rejected` 修法(不删枚举能力)
从 v2 `ENUMERATE_ELEMENTS` 参数表**摘掉 `source_id`**(`source_title` 保留;内部 id 从不上屏,模型填的只可能是猜测);`ReflectDecision.enumerate_source_id` 与 `v2_request_identity` 的 `identity_fields` 不动(legacy 仍解析,服务端解析出的 id 走同一格)。执行层 skip 拆细:范围不符报 `enumeration_source_not_in_scope`,措辞「按名称给出(source_title)」;那条 skip 由 `_enumeration_rejection` 整条产出(原因码与 detail 形状是同一个决定的两半,热函数不背那份字典字面)。**两个都给时以 `source_title` 为准**(评审补充):分派处是 id 压过 title,一个猜来的 id 会把模型如实给出的书名吃掉,所以 v2 解析层在 title 非空时不写 `enumerate_source_id`(类型校验仍做,legacy 与 `_run_enumeration` 逐字节不变)。**假设**:生产 12 条来自模型猜 `source_id`(本地无复现样本);待生产 raw `detail.error` 字符串确认,若实为 memory 合成源 `not enumerable`,改成解析器侧过滤。

### T-BF6 收尾计时归位
`run()` 收尾重排整块抽成 `_closing_rerank(...)`(`run()` 净缩,baseline 下调),记新 step_type `rerank`(v2 门 `capabilities is not None`),detail `{"queries", "reused", "researched", "researched_ms", "top_n"}`;单查询分支 `retrieve_scored` 同样计入。登记三处闭集 + 前端标签(候选「收尾重排」,过 `check_ui_vocabulary.py`)。验收:answer 步只剩合成候选本身;`durations_ms` 出现 `rerank`;关闭态轨迹步序列逐字不变。`_nudge_missing_assessment` 不补步(它不是调用)——写进「刻意不做」。

### T-BF7 assessment 与动作解耦(apply 逐方面)
`AspectLedger.apply` 返回结构(接受的 updates + 逐方面拒绝清单 `[(aspect_id, why)]`);`_plan_row` 四条整份错误改为跳过该行并记原因;完全相同重复项确定性去重(同 aspect_id 且规范化后全等),冲突重复项拒绝该方面并保留旧状态;`not_object`/`<group>_not_list` 等整份形状错误仍整份拒绝。`_absorb_assessment` 不再 `_reflect_invalid`:合法动作照常执行;拒绝清单 (a) 经 `_AspectRecord` 新增 consume-on-render 字段渲染成「服务端未采纳: <why>」行(与 `nudge_pending` 同款一次性语义),(b) 记 `skip` 步 `reason=invalid_assessment:<why>`,并让 `status_for_skip`/`observation_from_step` 对该前缀不产生动作观察(`NON_ACTION_SKIP_REASONS` 加前缀判据),避免同轮两行观察。缺省 assessment 语义、`note_missing_assessment`/追问、`REFLECT_ASSESSMENT_MAX_PROMPTS` 不动。**投影新增键 `assessment_rejections`**(拍板:加,有意扩面),把「拒了几个方面」与「作废了几轮」分开数。验收:`test_reasoning_retrieval.py:6640/8731/9469-9478` 按新语义重写;新增「一个越界方面 + 一个合法 `search_chunks` ⇒ 检索真的发生、方面账只改合法那一格」端到端用例。

## 4. 合同文档触点
`docs/product-and-api*.md` §集合枚举工具:「覆盖率合同」(T-BF2)、「预算与续跑」(守卫改变了「自动翻页直到上限」)、「限定单一来源」(T-BF5 后与「内部 id 不给模型看」一致)、「集合地图」段那句「reflect 提示语要求模型别翻页」(T-BF3 之后才对 v2 成立)。`architecture.md`/部署配置只在引入新配置时改(本 PR 不引入:守卫倍数为常量)。

## 5. 与 `fangan_todo.md` 的重叠
(a) 条 T0 基线报告与 A/B 首份报告:PR-1 改公共基线,四臂须在同一基线上重生成;已登记的「`invalid_tool_calls` 臂不对称、`invalid_assessment:*` 为 v2 独有」在 T-BF7 之后含义从「整轮作废」变「一个方面未采纳」,首份报告说明须重写。

## 6. 刻意不做
不给 legacy 加规模守卫(关闭态字节等价优先;legacy 生产行为不变);不删任何枚举能力;不改 stale 判据、最大步数、工具次数;不补追问 trace 步;不改缺省 assessment 语义。

## 7. 拍板结果(主 agent,2026-09-09)
1. `truncated_reason` 开第四个值 `oversize_sample`。
2. 守卫倍数常量 `4`(`total > rows_left × 4` 触发),样本 `min(rows_left, enum_page_size)`;不设配置项。
3. 守卫 v2-gated,legacy 不改。
4. T-BF5 按「模型猜 `source_id`」假设落地,标注待生产 raw 确认。
5. 投影加 `assessment_rejections` 键。
6. `rerank` 界面词「收尾重排」,以 `check_ui_vocabulary.py` 结果为准。
