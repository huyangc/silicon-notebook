# reflect 前缀复用 · PR-2 实施计划:T0 测量 + T1 `prefix_snapshot`

日期:2026-09-09。上游设计:[reflect 上下文与前缀复用最终设计](2026-09-09-reflect-prefix-cache-final-design_zh.md)§3、§4.1–4.3、§4.5、§5.1、§8、§11(T0/T1)、§12。前置:PR-1 公共基线修复(另一份计划)先合入。

## 0. 结论先行

- wrapper 与 schema hint 今天已经逐轮稳定;S 的逐轮不稳定源只有两处(本轮可执行动作集合、不可用清单),参数行天然稳定——T1 的静态化只需处理这两处。
- 单次调用墙钟已是 monotonic 且三条出口齐全,`call_wall_ms` 只需出参化;排队时长与 workload 标签经 `support_id` 在 llm.jsonl ⋈ events.jsonl 上现成可得。
- C/T 重排完全不碰 `run()`:`summary` 整块搬到末尾即可,`_new_run_state`/`_run_enumeration` 一行不动。
- 六个缺口(G1–G6)不补则 T0 数据不可用:`call_stats` 只在成功路径写且只有 finish_reason;usage 丢 cached/reasoning;响应字符数被 clip;`run_wall_ms` 不进投影;rig 的调度事件落全机共用文件;方面块契约原文与逐轮状态混装且渲染带副作用。

## 1. 机制核实(文件:行号以 HEAD 8cfa68c11 为准)

**W1 wrapper/schema hint 稳定。** `backend/app/core/llm.py:360-370` 在调用方消息前插 system(角色说明 + `Schema hint`);`reflect_v2_schema_hint`(`backend/app/services/prompts.py:1317-1368`)用 `capabilities.recognized_actions`,后者恒为 `ACTION_ORDER`(`backend/app/services/reasoning_actions.py:282`、`341-355`)。

**W2 墙钟已在。** `llm.py:481` `start = time.perf_counter()`;`llm.py:613/621/629` ok/cancelled/error 三条出口写 `latency_ms`。量的是 `chat_json` 内部,不含本地排队。

**W3 排队与 workload 标签现成。** `backend/app/services/model_provider.py:337 queued_at`、`342 started`、`350 finished`,`427-428` 发 `model_scheduler` 事件(`queue_latency_ms`、`execution_latency_ms`、`workload_id`、`service_id`、`model`、`status`、`retry_outcome`、`breaker_transition`、`support_id`);`interaction_support_scope`(`model_provider.py:345`)把 `support_id` 注入 contextvar,`llm_logging.py:79-81` 写进每条 llm.jsonl。

**W4 reflect 经 workload adapter。** `model_provider.py:1277-1289` `chat()` 恒返回 `ScheduledJsonChatClient`;`_reflect_v2_attempt` 用 `self.model_clients.chat("reasoning_agent")`(`reasoning_retrieval.py:4147`);rig `search` 也经 `model_work_scope`(`scripts/reflect_shadow_rig.py:2091-2094`)。

**W5 重试部分可数。** 瞬时错误重试(`llm.py:490-549`)每次落 `status="retry"` 且 `id` 同主记录(`538-544`);但 `response_format` 被拒后的明文重试(`513-526`)与 `stream_options` 被拒后的重建流(`300-308`)无日志 ⇒ 只靠日志是下界。

**W6 非法动作率已可监测。** `reasoning_retrieval.py:461` `_V2_UNAVAILABLE_PREFIX`,`839-841` 生成 `unavailable_action:<reason>`;`backend/app/eval/reflect_ab.py:510-539` 已计数。P 不得改这套词表。

**W7 reflect 不参与响应缓存。** 缓存 opt-in、判据 `response_validator is not None`(`llm.py:432`、`598-604`);`_reflect_v2_attempt`(`3697-3709`)不传 validator。

**W8 C/T 重排不碰 `run()`。** `_reflect_v2_context`(`reasoning_retrieval.py:3734-3822`)拼 `blocks = [summary, render_aspect_block, render_collection_keys_note]` 成 `server_state`;`ReflectContext.as_user_block`(`backend/app/services/reasoning_context.py:638-646`)按 server_state → evidence → observations 排;问题由 `reflect_v2_user_prompt`(`prompts.py:1301-1314`)排最前。

**X1 修正交接信息。** `_v2_param_line`(`prompts.py:1161-1175`)不含「已尝试 N 次」;每轮变化的额度串是观察行的 `余额=`(`reasoning_observation.py:637-638`),在 user 段。S 的不稳定源只有 `capabilities.actions` 成员与 `unavailable_block`(`prompts.py:1244-1259`);`params_for` 的收窄判据(`reasoning_actions.py:556-585`)只读 run 级常量。

**缺口。** G1 `call_stats` 只在 ok 分支写 finish_reason(`llm.py:566-568`;`619-632` 不写)。G2 `_usage_dict`(`69-86`)丢 `prompt_tokens_details.cached_tokens`/`completion_tokens_details.reasoning_tokens`。G3 日志 content 经 `logger.clip`(`llm.py:616`,默认 4000)。G4 `run_wall_ms` 只在 `search-runs.log`(rig `2459/2473/2489`、并发 `2709/2723/2729`、`2511-2517`),投影 `total_ms` 是 trace 步之和(`reasoning_trace_stats.py:692、744`)。G5 `_rig_process_env`(rig `1655-1663`)只改 `LLM_LOG_PATH`,`model_scheduler` 事件走 `EVENT_LOG_DIR`(`config.py:1353`,`repository_runtime.py:211`)。G6 `render_aspect_block`(`reasoning_aspects.py:663-719`)混装契约原文与逐轮状态,渲染时消费 `nudge_pending`(`710-718`)。

## 2. 统一硬约束

`REASONING_REFLECT_OPTIMIZATION=off` 与 v2 总闸关闭态逐字节等价;`run`/`_new_run_state`/`_run_enumeration` 零松弛;prompt/schema 改动用例经 `_GatedV2LLM`(`backend/tests/test_reasoning_retrieval.py:6453`)/`_V2ContextLLM`(`6940`);业务只经 ModelProvider workload adapter;`reasoning_actions.py`/`reasoning_context.py`/`reasoning_observation.py` 不读 Settings/DB;字段命名不出现 `cache_hit`/命中率。收尾 `bash scripts/check.sh`。

## 3. 任务

### T-PS1 `llm.py` 暴露 provider-facing 消息纯函数
落点 `llm.py:360-370`。抽模块级纯函数 `provider_messages(messages, response_schema_hint) -> list[dict]`,`chat_json` 自己调用它,`llm_key`(`417-422`)照旧;加确定性 `serialize_provider_messages(msgs) -> bytes`(role/content 带正文不可伪造的分隔符,UTF-8)。
验收:抽取前后发出的 messages 逐字节相同;`llm_key` 不变;零 I/O。用例:(a) 输出 = wrapper + 调用方消息;(b) 序列化幂等;(c) 只改末尾 content 时公共前缀 = 前面全部字节;(d) `test_llm_client.py` 全绿。

**评审后修正(2026-09-09):长度头移到帧尾。** 首版帧形是 `<len>:<bytes>`,长度在字段之前。T-PS8 的消息形状是 `system(S)` + **一条** `user(C+K+D+T)`,只有 T 逐轮变;长度头因此先于它所计的字节分叉——T 的字节数一变,user 帧刚开头的十进制头就不同,C+K+D 那几千个完全相同的字节全落在分叉点之后,`message_prefix_bytes` 塌回 system 帧长(对照实测 720 vs 真实 3134)。改为 `<bytes>:<len>:`,role 与 content 同形各一格。单射性由**从右向左**的解析给出:末尾一个 `:`,向左扫十进制得 n,再一个 `:` 闭合字段,其前 n 字节即该字段,循环左移;所有结构字节都由端点计数定位、不在正文里搜索,正文因此仍伪造不出边界。每条消息的固定开销 = `len(role) + len(str(len(role))) + len(str(len(content))) + 4` 字节,其中只有 content 自身的长度与它两侧的冒号落在 content 之后——T-PS3 据此算前缀时,**能进入公共前缀的固定开销是 `len(role) + len(str(len(role))) + 2`**(system 消息 `system:6:` 共 9 字节,user 消息 `user:4:` 共 7 字节),content 那一格的 `:<len>:` 不进。

### T-PS2 `call_stats` 承载单次调用全部必有观测
落点 `llm.py:185-198、481、566-568、612-632`、`_usage_dict`(`69-86`);`model_provider.py:528-529、556` 转发闸不变。
新增 `call_wall_ms`(三条出口都写)、`status`(`ok|cancelled|error`)、`response_chars`(不经 clip)、`usage`(dict,缺字段缺键)、`attempts`(四个 `.create()` 点 `301/308/508/526` 各 +1,计数器由 `chat_json` 持有并传进 `_stream_chat_content`)、`attempts_observed`。`_usage_dict` 读 `cached_tokens`/`reasoning_tokens`,缺失不写键(绝不写 0),同步进 `record["usage"]`;llm.jsonl 加 `response_chars`、`attempts`(数值)。
验收:不传 `call_stats` 的调用方逐字节不受影响;duck-typed 客户端仍空 sink;cancelled/error 也拿到墙钟与 status;cached 缺席 ⇒ 无键。用例:(a) 三出口;(b) cached 在/不在;(c) 重试两次后成功 ⇒ `attempts == 3`;(d) `response_format` fallback ⇒ `attempts == 2` 且 `attempts_observed`;(e) 长回复 `response_chars` > clip 长度;(f) `reflect_ab.py:644-650` `_accumulate` 对新键的 unknown 口径。

### T-PS3 v2 上下文观测与 `message_prefix_bytes`
落点 `_reflect_v2_attempt`(`3673-3732`)与 `_reflect_v2_context`(`3734-3822`);`_ReasoningRunState` 上一个只读渲染缓存字段(测量开时才构造,关闭态恒 `None`)。
`_reflect_v2_context` 返回各块字符数(S/C/K/D/T)、总字符、总字节、卡数、省略数、压缩/回退次数(本 PR 恒 0);`_reflect_v2_attempt` 用 T-PS1 纯函数算本轮最终消息字节串,与缓存的上一轮比公共前缀 ⇒ `message_prefix_bytes`,首轮 `None`;请求文本只在内存。
验收:`off` 且测量关时不构造缓存不序列化;缓存只存字节串与块长度;内存上界一轮消息。用例:(a) `off` 下返回对象逐字段同前;(b) 冻结输入两次调用相同;(c) 只末尾 T 变 ⇒ prefix = S+C+K+D 字节;(d) 缓存对象结构断言。

**写侧与 T-PS4 的硬接缝(T-PS4 评审后补,2026-09-10)。** 写 detail 的那几个键**必须**用 `reasoning_trace_stats.REFLECT_CONTEXT_DETAIL_KEYS` 的值与 `REFLECT_MEASUREMENT_DETAIL_KEYS` 的成员来构键(两者都已从 domain 导出),不许在写侧另抄一份字面量;并加一条 `written_keys ⊆ REFLECT_MEASUREMENT_DETAIL_KEYS` 的断言——写侧多写一个读侧不认的键,那一列会静默缺席,而不是报错。另外 `ctx_bytes_total` **必须**等于 `len(serialize_provider_messages(provider_messages(...)))`(这一轮最终消息的全部字节,含 wrapper 与帧开销),它才能当 `message_prefix_bytes` 的分母(公共前缀是在同一串字节上算的);做不到就删掉「`bytes_total` 是 `prefix_bytes_*` 分母」这条说明,只留下它自己的绝对值,别让人做一道两边口径不同的除法。

### T-PS4 trace 步与投影闭集扩展
落点 `reasoning_trace_stats.py:43-103、594-634、637-751、786-866`。reflect 步 detail 稀疏键:`ctx_chars_s/c/k/d/t`、`ctx_bytes_total`、`message_prefix_bytes`、`cards_shown`、`cards_omitted`、`call_wall_ms`、`call_attempts`、`response_chars`。投影顶层:`run_wall_ms`、`model_calls_real`、`attempts_observed`、`optimization`(新闭集 `OPTIMIZATIONS`,与 `POLICY_VERSIONS` 并列)、`context_chars`(短码→数值)、`prefix_bytes_median`/`prefix_bytes_min`/`prefix_turns`(逐轮细节留 rig per-call 表)。新键缺失一律 `None`。
验收:`assert_closed`/`assert_projection_values`(`923-1031`)全绿;旧行可读;`legacy`/`off` 行键集不变(稀疏键不出现)。用例:隐私守卫拦自由文本;缺 usage/finish_reason/cached 仍出整行;截断 trace 不掩盖 `model_calls_real`;`off` 行逐键比对。

**评审后修正(2026-09-10)。** ① `context_chars` 的短码是 `s/c/k/d/t/bytes_total`,不是本段初稿写的 `total`——短码集合由 `reasoning_trace_stats` 本模块定义(计划钉住的只是 detail 侧的 `ctx_chars_*`/`ctx_bytes_total` 键名),而一个叫 `total` 的短码会被顺手读成「总字符数」,它其实是字节数。② 投影顶层实际是**九**列:本段列出的八列之外,显式新增 `response_chars_total`(各 reflect 步交还给调用方的正文字符数之和,与 `model_calls_real` 同一条「任一步缺 ⇒ unknown」口径)。③ `attempts_observed` 只表达「每一条 reflect 步都带了 `call_attempts` 吗」,与 `trace_truncated` 解耦:后者判的是某一步的 id 列表被 `TRACE_RESULT_IDS_MAX` 截了,不是轨迹缺轮。

### T-PS5 rig:隔离、run 墙钟、optimization 第二维
落点 rig `1614-1663`、`2411-2547`、`2609+`、`ab` 臂与 `_report_rows`(`1399-1470`);`reflect_ab.py:40-86、79、595-641`。
`_rig_process_env` 加 `EVENT_LOG_DIR=out_dir/events`(G5);`elapsed_ms` 写进 `run_wall_ms`(串行 `2489`、并发 `2723/2729`,失败 run 也写);新增 per-call 表 `calls-<arm>.jsonl`(llm.jsonl ⋈ events.jsonl on `support_id`),核心纯分析放 `backend/app/eval/reflect_context_bench.py`,rig 薄适配;`ab` 臂加第二维 `--arms v2:off,v2:prefix_snapshot`,`ARMS` 扩成 `(policy, optimization)`,投影加 `optimization` 并核对实际运行值(镜像 `assert_arm_matches_evidence`,`656-670`);`pair_id`(`788-793`)不变,`mark_paired` 的 `len(ARMS)` 判据二维化。
验收:rig 不再写 `.local/logs/events.jsonl`;失败/取消 run 也有 `run_wall_ms`;并发下按 `support_id` 归因,切不干净仍 unknown;dry-run 打印调用数与请求上界。用例:`_rig_process_env` 键集;日志⋈事件三种残缺;只跑一臂 `paired=False`;声明 `prefix_snapshot` 证据 `off` ⇒ RuntimeError。

### T-PS6 `REASONING_REFLECT_OPTIMIZATION` 配置与单点判定
落点 `config.py:1007-1037` 之后;`reasoning_retrieval.py:3230-3245` 旁加 `reflect_optimization()`。
`reasoning_reflect_optimization: Literal["off","prefix_snapshot","prefix_delta","prefix_delta_lean"]`,默认 `off`;**本 PR 对 `prefix_delta`/`prefix_delta_lean` 在校验器里响亮拒绝**(PR-3/PR-4 各放开一格)。`reflect_optimization()`:v2 总闸关 ⇒ `off`;Knowhow(`allow_reflect_v2=False`)⇒ `off`;duck-typed settings 缺字段 ⇒ `off`;全仓唯一读点。
验收:v2 关 + `prefix_snapshot` ⇒ 与 `off` 逐字节相同;Knowhow 不受影响。用例:四取值 × v2 开/关矩阵;非法取值报错口径;Knowhow 恒 legacy。

### T-PS7 同源静态工具目录
落点 `reasoning_actions.py` 新增纯函数 `static_catalog_facts(facts) -> ReflectCapabilityFacts`(`*_left` 归 1、`last_turn=False`、`terminal_overflow_repair=False`、`has_candidates=True`;保留 `kg_in_scope`/`scope_restricted`/各 `*_active`);目录 = `build_reflect_capabilities(static_catalog_facts(facts))`,同一份 `ACTION_DEFINITIONS`;`_reflect_capabilities`(`3280-3358`)旁在**本 run 第一次 reflect 时**用那轮 facts 生成并缓存(不在 run 起点算,避免 `_unsafe_scope_restricted()` 额外库读)。取舍写进注释:目录是「可能执行」的超集,不授予资格。
验收:目录 run 内逐字节不变;额度耗尽说明仍在;`recognized_actions` 不改;`parse_reflect_v2` 仍读逐轮 `capabilities.actions`。用例:幂等;通道位不改(无图 run 图动作不在目录);额度 3→0 目录不变、actions 变;`follow_chain` 候选池空时在目录、不在 T。

### T-PS8 S/C/K/D/T 布局(P 模式本体)
落点 `prompts.py:1214-1298、1301-1314`、`reasoning_context.py:617-646`、`reasoning_aspects.py:663-719`(方面块拆半)、`reasoning_retrieval.py:3734-3822、3689-3702`。`run()` 不动一行。
- S:`reflect_v2_static_prompt(catalog)` = 协议 + 静态目录 + 「目录是参数参考,T 的本轮清单才是可调用集合」+ `_V2_ASSESSMENT_INSTRUCTION`;`UNTRUSTED_EVIDENCE_SYSTEM_INSTRUCTION` 仍叠最前(run 级)。
- T(user 段末尾,`ReflectContext.turn_state`):本轮 `allowed_actions` + 剩余额度 + 不可用清单与原因(词表不变)+ 方面**状态**半 + 已完整集合键 + **整块 `summary`** + 一句「T 的服务端当前状态优先于上方 K/D 过时观察」。
- C(问题之后):`quoted_phrase_grounding` + `[Question]` + 方面**契约**半(id↔原文 + constraints)+ 范围语义。新增 `render_aspect_contract_block`/`render_aspect_status_block` 纯函数;`nudge_pending` 消费随状态半,仍单一调用点。
- K/D:内容判据不改,只改位置。消息形状 `system(UNTRUSTED? + S)` + `user(C + K + D + T)`。`off` 走原路径原顺序。
验收(§12):同 run 额度耗尽/方面变化/轮数增长 ⇒ S、C 字节不变;最终消息检查覆盖 wrapper,S/C 开头无动态值;P 与 off 冻结输入下同一批证据事实与执行限制;T 的 `allowed_actions` 与 `parse_reflect_v2` 白名单同源;`unavailable_action:*` 计数两模式可读;off 与关闭态字节等价;Knowhow/legacy 不受影响。用例:(a) `_V2ContextLLM` 加 `system_prompt(turn)`/`turn_state_block(turn)` 取值器,额度耗尽多轮 run 断言 S、C 不变;(b) 方面 unresolved→supported ⇒ C 不变 T 变;(c) 还原 provider-facing 消息含 wrapper;(d) 冻结 fixture 上证据键集/可用动作/额度逐项相等;(e) 选目录有、T 无的动作 ⇒ `unavailable_action:<既有 reason>` 零 I/O 观察、循环继续(过 `_GatedV2LLM`);(f) `nudge_pending` 只消费一次且在 T。

### T-PS9 文档与门
`docs/deployment-and-configuration_zh.md:848`/`.md:1047` 之后新增 `REASONING_REFLECT_OPTIMIZATION`(默认 off、四取值、本期两格、v2 关与 Knowhow 忽略、不是前端档位、预告 `REASONING_REFLECT_RECENT_OBSERVATIONS` 在 delta 模式 = 重建 K 时保留几条,以及独立测量开关);`docs/product-and-api_zh.md:1408/1422`、`.md:1947/1961` 补 P 模式分块与稳定性口径(合同不变);**llm.jsonl 字段契约**——在「`finish_reason` 无条件写进 LLM 调用日志」那一级(`_zh.md:1452`/`.md:1991`)补 `attempts`(这一次逻辑调用真正发出的请求数,只在终态行,`status="retry"` 行不带,按行累加会重复计入同一次调用)与 `response_chars`(交还给调用方的正文长度,不经 `LLM_LOG_MAX_CHARS` 截断),中英成对,只加数值键;`architecture.md:111` 补静态目录产地与 `reflect_optimization()` 唯一读点、登记 `reflect_context_bench.py`;`scripts/README.md:488` 附近补第二维臂与 `EVENT_LOG_DIR` 隔离。不改 AGENTS/CLAUDE。

依赖:T-PS1→T-PS2→T-PS3;T-PS6→T-PS7→T-PS8;T-PS4 依赖 T-PS3;T-PS5 依赖 T-PS2+T-PS4+T-PS6;T-PS9 收尾。两条链可并行,T-PS5 汇合。

## 4. 刻意不做
不实现 `prefix_delta`/`lean`;不改 K/D 选择判据;不改首轮查询宽度/范围/最大步数/工具次数;不加原生 tool calling/完整聊天历史/reasoning 回放;llm.jsonl 只加数值;请求文本不进日志/投影;生产策略不加实验标记(E1 接缝留 PR-5);不改 `policy_version` 语义;不动 `recognized_actions` 与 schema 枚举。

## 5. 拍板结果(主 agent,2026-09-09)
- **Q1** 未实现取值:**拒绝**(校验器抛 ValueError,PR-3/PR-4 各放开一格)。
- **Q2** 公共前缀内存口径接受(一轮消息字节);**独立测量开关** `REASONING_REFLECT_MEASURE_CONTEXT`(默认 false),与 `optimization` 正交,`off` 臂也能出 `message_prefix_bytes`。
- **Q3** `summary` **整块搬到 T(末尾)**,按设计 §4.5;P 与 B 的差异包含布局这一点在报告里写明(§9.2 已承认)。
- **Q4** 静态目录在本 run 第一次 reflect 时生成并缓存,接受「范围收窄后目录留着不可用动作」的形态,T 每轮如实说明;验证多覆盖这一形态。
- **Q5** 归因口径优先级:`support_id` 关联优先;无 events 文件时退时间窗;并发 > 1 且只能时间窗 ⇒ unknown。每行记 `attribution`(`support_id|window|unknown`)。
- **Q6** 逐轮前缀字节不进闭集投影,退成 `prefix_bytes_median/min/turns` 三格;逐轮细节只在 rig per-call 表。
