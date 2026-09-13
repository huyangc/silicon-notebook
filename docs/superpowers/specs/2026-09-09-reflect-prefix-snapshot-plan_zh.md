> 历史档案（2026-09-13）：V2 实验已退役，本文不再是实施或启用指引。当前仅保留 Legacy；参见 [当前状态](../../../fangan_done.md#32-reflect-实验退役与-legacy-保留2026-09-13) 和 [产品/API 合同](../../product-and-api_zh.md)。

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

**实施记录(2026-09-10)。** 硬接缝两条都兑现(键名一律经 `_registered_measure_key` 在导入期对着登记清单兑过 + 落账点一条 `⊆` 断言;`ctx_bytes_total` 有等式用例)。与上面字面不同的五处取舍,都记在这里:

- **块长度落在 `_reflect_v2_attempt`,不在 `_reflect_v2_context`。** 五块里有三块的最终字节只有那里才有:`off` 的 S 是那一轮**现渲染**的动态 system 段(`_reflect_v2_context` 拿不到 `capabilities`)、C 要等 `prompts` 把问题拼进去才成形、P 的 T 还差本轮动作面那一半。改成「谁产出那个值谁记」:`_reflect_v2_context` 只记它独有的 `cards_shown`/`cards_omitted`(产地是 `selection`,那边看不到)。
- **C 与 T 是差值口径,两条臂同形。** `T = len(材料块) − K − D`(`off` 是服务器状态摘要、P 是动作清单 + 状态半,块间分隔符落在这一格),`C = len(user 正文) − len(材料块)`(问题、契约、引号规则、收尾那句)。于是 `S + C + K + D + T` 恰等于两条消息正文字符数之和——这条恒等式是这五个数唯一的自洽判据,有用例钉住。按块名各自直取会让分隔符与 `prompts` 的框架字节无人认领,五个数看起来都合理却对不上任何一条消息。
- **压缩/回退次数不写。** 计划说本 PR 恒 0,而 T-PS4 的登记清单里没有它们的键;写侧键集必须 ⊆ 那份清单,所以「恒 0」的兑现方式是**这两个键不存在**,而不是写两个 0。
- **落账点在 `_TraceRecorder.__call__`,缓存有三个把手。** 那几个键要进的是 `run()` 里现拼的 reflect detail,而 `run()` 在零松弛长度天花板下一条语句都不能加(同 `defer` 的既有理由)。记账器是那条 reflect 步唯一的入口,所以合并在那里,判据加 `step_type == "reflect"`;`_ReasoningRunState.reflect_measurement` 是 owner(计划要的那个默认 `None` 字段,`_new_run_state` 零改动),`state.record.measurement` 是记账器的把手,`ReflectContext.measurement` 是每轮交给 `_reflect_v2_attempt` 的那一格(它拿不到 `state`,理由同 `static_prompt`)。
- **`previous` / `current` 分两格,晋升在记账时。** 同一轮可能调用两次模型(加预算重试),当场把本轮字节串升为 `previous` 会让第二次尝试拿第一次当基准,量出一个恒等于全长的假前缀(而那个数看起来更漂亮)。晋升因此由 `ReflectMeasurement.take()` 在真的记了 reflect 步的那一刻做,一轮一次。
- **`call_wall_ms` / `call_attempts` / `response_chars` 同一轮内累加**(不是覆盖):读侧 `model_calls_real` 是各 reflect 步之和,一轮只报最后一次的请求数会让整 run 的真实请求数偏低,而那一列正是用来判「日志里的数是真值还是下界」的。
- **验收 (c) 的形态要构造。** 真实 run 里 K(新卡)与 D(新观察行)每轮都在长,「只有末尾 T 变」不会自然出现;所以那一条取一轮真发出去的两条消息、只改 user 正文末尾,再用生产的 `serialize_provider_messages` / `_common_prefix_bytes` 量一次。真 run 上另有两条:P 的每一轮都越过整个 system 帧,`off` 至少有一轮越不过它(动作面一变就断在里面)——后者是「测量真的在量东西」的反向证据。
- `T-PS8` 那个「静态目录渲染缓存留给 T-PS3」的注释保留原样:`static_prompt` **没有**挪到这个缓存对象上。挪了它就得在测量关、只开布局时也构造缓存,而「测量关不构造缓存」是本任务的硬约束;每轮重渲染一次是纯字符串拼接,S 的稳定性本来就不依赖缓存。

**评审修正轮(2026-09-10,规格 + 质量两份评审)。**

- **P1:观测故障曾能改变业务决定(两份评审同一条)。** `_measure_reflect_messages` 排在 `client.chat_json` 之前、而整段住在 `_reflect_v2_attempt` 的 fail-open `try` 里,于是一次序列化故障被洗成一次**假的模型兜底**:那一轮的请求根本没发出去,轨迹上却多出 `__reflect_invalid__` + `fallback_reason=UnicodeEncodeError`,`fail_closed` 调用方(knowhow 补全)整次死掉。入口是模型可控的:`json.loads` 接受 `"\ud800"`,孤立代理经动作参数进观察账,下一轮就在 user 正文里,而严格 UTF-8 对它抛。两层都修:(a) 新增 `_measure_reflect_messages_safely`,把测量关进它自己的 `try/except`(纪律同 `_TraceRecorder.__call__` 对 observer 投影那段),失败只表现为**键缺席**(读作 unknown,同「客户端不报就缺键、绝不写 0」),日志只记异常**类名**——`exc_info` 会把正文带进 traceback;失败时连 `previous` 一起丢掉,免得下一轮拿一个**隔了一轮**的基准量出一个「看着正常、却回答了另一个问题」的前缀。(b) `serialize_provider_messages` 改 `errors="surrogatepass"`,让这把尺子对任意 `str` **全定义**;它只被测量消费(llm.jsonl 与真实发送都不走它),所以对既有行为零影响。
- **质量 P2-1:`⊆` 那道守卫从 `assert` 改 `raise`。** `python -O` 会把断言整条删掉,而这一道是「键名合法但走错写点」唯一的判据——与同文件 `_registered_measure_key` 自己给出的「在导入期 raise 而不是 assert」的理由自相矛盾。用例侧同步:原来的 `_measure_keys(detail) <= 清单` 是 `X & R ⊆ R`(恒真,越界键被交集掩掉),改成拿**未掩码**的整份 detail 键集去比「冻结的既有键 ∪ 登记清单」,并另断一条「测量关时的既有键集恰好是那份冻结值」防基线漂移。
- **质量 P3-1/P3-2 的覆盖缺口。** 补 `attempts=True` 替身(bool 排除)、「有 reflect 步但那一轮没量到 ⇒ `take()` 保住基准」两条;`ReflectMeasurement.previous/current` 加 `repr=False` 并补一条「repr 里没有请求正文」的用例——那句「文本只在内存里过一遍」此前是偶然成立(没人打印它),现在是结构成立。
- **两处文档口径。** ① 内存上界改成「**一轮消息的量级,峰值两轮**」:晋升前 `previous` 与 `current` 并存,而这中间横跨整次模型调用(`reasoning_context.py` 的类 docstring 与部署文档中英三处)。② `off` 臂 `ctx_chars_c/t` 的口径写进读侧(`reasoning_trace_stats.REFLECT_CONTEXT_DETAIL_KEYS` 的注释)与 `docs/product-and-api*.md` 的 P 模式分块那一级:五个数是**稳定性类别**不是块标题,C/T 是差值,`off` 的 T 量的是那块排最前的服务器状态摘要,两条臂同形同尺、可直接比。部署文档里「把测量整个关掉…逐字节相同」那句同步改准:测量**永不**改变发出的消息与决定,成功如此、失败亦然;测量自身失败只表现为键缺席。

### T-PS4 trace 步与投影闭集扩展
落点 `reasoning_trace_stats.py:43-103、594-634、637-751、786-866`。reflect 步 detail 稀疏键:`ctx_chars_s/c/k/d/t`、`ctx_bytes_total`、`message_prefix_bytes`、`cards_shown`、`cards_omitted`、`call_wall_ms`、`call_attempts`、`response_chars`。投影顶层:`run_wall_ms`、`model_calls_real`、`attempts_observed`、`optimization`(新闭集 `OPTIMIZATIONS`,与 `POLICY_VERSIONS` 并列)、`context_chars`(短码→数值)、`prefix_bytes_median`/`prefix_bytes_min`/`prefix_turns`(逐轮细节留 rig per-call 表)。新键缺失一律 `None`。
验收:`assert_closed`/`assert_projection_values`(`923-1031`)全绿;旧行可读;`legacy`/`off` 行键集不变(稀疏键不出现)。用例:隐私守卫拦自由文本;缺 usage/finish_reason/cached 仍出整行;截断 trace 不掩盖 `model_calls_real`;`off` 行逐键比对。

**评审后修正(2026-09-10)。** ① `context_chars` 的短码是 `s/c/k/d/t/bytes_total`,不是本段初稿写的 `total`——短码集合由 `reasoning_trace_stats` 本模块定义(计划钉住的只是 detail 侧的 `ctx_chars_*`/`ctx_bytes_total` 键名),而一个叫 `total` 的短码会被顺手读成「总字符数」,它其实是字节数。② 投影顶层实际是**九**列:本段列出的八列之外,显式新增 `response_chars_total`(各 reflect 步交还给调用方的正文字符数之和,与 `model_calls_real` 同一条「任一步缺 ⇒ unknown」口径)。③ `attempts_observed` 只表达「每一条 reflect 步都带了 `call_attempts` 吗」,与 `trace_truncated` 解耦:后者判的是某一步的 id 列表被 `TRACE_RESULT_IDS_MAX` 截了,不是轨迹缺轮。

### T-PS5 rig:隔离、run 墙钟、optimization 第二维
落点 rig `1614-1663`、`2411-2547`、`2609+`、`ab` 臂与 `_report_rows`(`1399-1470`);`reflect_ab.py:40-86、79、595-641`。
`_rig_process_env` 加 `EVENT_LOG_DIR=out_dir/events`(G5);`elapsed_ms` 写进 `run_wall_ms`(串行 `2489`、并发 `2723/2729`,失败 run 也写);新增 per-call 表 `calls-<arm>.jsonl`(llm.jsonl ⋈ events.jsonl on `support_id`),核心纯分析放 `backend/app/eval/reflect_context_bench.py`,rig 薄适配;`ab` 臂加第二维 `--arms v2:off,v2:prefix_snapshot`,`ARMS` 扩成 `(policy, optimization)`,投影加 `optimization` 并核对实际运行值(镜像 `assert_arm_matches_evidence`,`656-670`);`pair_id`(`788-793`)不变,`mark_paired` 的 `len(ARMS)` 判据二维化。
验收:rig 不再写 `.local/logs/events.jsonl`;失败/取消 run 也有 `run_wall_ms`;并发下按 `support_id` 归因,切不干净仍 unknown;dry-run 打印调用数与请求上界。用例:`_rig_process_env` 键集;日志⋈事件三种残缺;只跑一臂 `paired=False`;声明 `prefix_snapshot` 证据 `off` ⇒ RuntimeError。

**实施记录(2026-09-10)。** 验收里「取消 run 也有 `run_wall_ms`」这半句**没做**,其余全部兑现。与计划字面不同、或计划没说而下一个人必须知道的,都记在这里:

- **取消的 run 一行都不落**,所以它没有墙钟。`ab`/`search` 三处 `except AskCancelled: raise`(rig `2591 / 2843 / 4786`)是**整批收摊**语义:取消是人按下的,那一刻的半份 run 不是一个观测。要给它造一行就得先改 abort 契约,超出 T-PS5,留待需要时单独决定。**已经覆盖的是另一条路**:取消位置起之后抛出的**非** `AskCancelled` 异常照常落 `status=failed` 的失败行,那一行有墙钟(用例钉住)。读这份计划核对进度的人:这一条验收**未完成**,不要当成四条全过。
- **`search` 那条路的 `optimization` 恒 `unknown`。** 计划只给 `ab` 加第二维,`project_search_run` 不收它。分析侧 `scripts/analyze_reasoning_trace.py` 明确「unknown 不当臂」,所以不会串格,但按 optimization 分组时 `search` 的行整体落在 unknown 那一格,是预期的。
- **per-call 表只在 `ab`。** `search` 不出 `calls-*.jsonl`(它不跑完整 Ask,也没有臂的第二维)。
- **`--arms` 与 `--only-policy` 互斥,且 `--arms` 一次只收一对。** 前者:「只跑 v2」在二维写法下有两种读法,产出的数据集与 `paired` 都不一样,预检拦在跑批之前。后者:`ARMS` 是**合法组合的闭集**(三格),不是一批的臂数;三条臂的批次里每个配对单元落三行,`mark_paired` 的门槛 `PAIR_ARM_COUNT` 判不出配对,整批 `paired` 全 `False`、配对差值表凭空空掉,而每一行看起来完全正常。要跑三格就分两批,每批一对。`--arms ""`(空写法)同样响亮拒绝,不退化成默认两臂。
- **`REASONING_ATTEMPT_BUDGET` 与 config 默认值是同 diff 约定。** rig 那个常量硬编码 `1 + REASONING_MAX_RETRIES` 的默认值(dry-run 不 import config,不能现读);改配置默认值要同 diff 改它,用例钉住两者相等。
- **`EVENT_LOG_DIR` 与 `LLM_LOG_PATH` 目录不对齐的启动告警维持现状。** 每次构造 repo 打一行,内容是「日志查看器读不到 per-user 的 llm 日志」——rig 的产物没有查看器要读,两串日志各按自己的 glob 读,所以是噪声不是问题。要消噪最省的做法是两串都落 `<out-dir>/logs`(两个 glob `**/llm-*.jsonl` / `**/events-*.jsonl` 互不相交,合目录不会互相污染),属于对计划字面的小偏离,留待 T-PS9 一并拍板,别单独改。
- **per-call 表里重试行单列一格 `join="retry"`。** `llm.py` 每次瞬时错误重试都再写一行,与终态行**同号**(重试循环整个在 `interaction_support_scope` 内),而调度事件只有一条。按行数判扇出会让**每一次重试过的调用**都落成两行 `ambiguous`、排队/执行时长与 workload 全线 unknown——而那批恰是最慢、最该被看见的调用。所以:重试行不参与扇出判定、单列一格进 `CALL_JOIN_STATES`,终态行照常与那条唯一事件配对(事件侧列不丢);「一次调用一格」按**终态行**计,重试行的 `call_index` 是 `None`,数调用数格子不数行。只剩重试行的号(重试到一半被掐),它那条事件仍以 `event_only` 落表。

### T-PS6 `REASONING_REFLECT_OPTIMIZATION` 配置与单点判定
落点 `config.py:1007-1037` 之后;`reasoning_retrieval.py:3230-3245` 旁加 `reflect_optimization()`。
`reasoning_reflect_optimization: Literal["off","prefix_snapshot","prefix_delta","prefix_delta_lean"]`,默认 `off`;**本 PR 对 `prefix_delta`/`prefix_delta_lean` 在校验器里响亮拒绝**(PR-3/PR-4 各放开一格)。`reflect_optimization()`:v2 总闸关 ⇒ `off`;Knowhow(`allow_reflect_v2=False`)⇒ `off`;duck-typed settings 缺字段 ⇒ `off`;全仓唯一读点。
验收:v2 关 + `prefix_snapshot` ⇒ 与 `off` 逐字节相同;Knowhow 不受影响。用例:四取值 × v2 开/关矩阵;非法取值报错口径;Knowhow 恒 legacy。

### T-PS7 同源静态工具目录
落点 `reasoning_actions.py` 新增纯函数 `static_catalog_facts(facts) -> ReflectCapabilityFacts`(**逐轮项一律归一**:`*_left` 归 1、`last_turn=False`、`terminal_overflow_repair=False`、`has_candidates=True`、`outline_repair_available=False`、`scope_restricted=False`;只保留 run 级的部署与调用方通道位 `kg_in_scope`/各 `*_active`/枚举白名单——`scope_restricted` 与 `outline_repair_available` 两格按 §5 Q4 的**评审后修正**归一,不再「原样带过」,否则范围收窄的 run 里目录不再是超集);目录 = `build_reflect_capabilities(static_catalog_facts(facts))`,同一份 `ACTION_DEFINITIONS`;`_reflect_capabilities`(`3280-3358`)旁在**本 run 第一次 reflect 时**、且**仅当 `reflect_optimization() != "off"`** 时用那轮 facts 生成并缓存(不在 run 起点算,避免 `_unsafe_scope_restricted()` 额外库读;纯测量臂没有消费者,见 §5 Q4)。取舍写进注释:目录是「可能执行」的超集,不授予资格。
验收:目录 run 内逐字节不变;额度耗尽说明仍在;`recognized_actions` 不改;`parse_reflect_v2` 仍读逐轮 `capabilities.actions`。用例:幂等;通道位不改(无图 run 图动作不在目录);额度 3→0 目录不变、actions 变;`follow_chain` 候选池空时在目录、不在 T。

### T-PS8 S/C/K/D/T 布局(P 模式本体)
落点 `prompts.py:1214-1298、1301-1314`、`reasoning_context.py:617-646`、`reasoning_aspects.py:663-719`(方面块拆半)、`reasoning_retrieval.py:3734-3822、3689-3702`。`run()` 不动一行。
- S:`reflect_v2_static_prompt(catalog)` = 协议 + 静态目录 + 「目录是参数参考,T 的本轮清单才是可调用集合」+ `_V2_ASSESSMENT_INSTRUCTION`;`UNTRUSTED_EVIDENCE_SYSTEM_INSTRUCTION` 仍叠最前(run 级)。
- T(user 段末尾,`ReflectContext.turn_state`):本轮 `allowed_actions` + 剩余额度 + 不可用清单与原因(词表不变)+ 方面**状态**半 + 已完整集合键 + **整块 `summary`** + 一句「T 的服务端当前状态优先于上方 K/D 过时观察」。
- C(问题之后):`quoted_phrase_grounding` + `[Question]` + 方面**契约**半(id↔原文 + constraints)+ 范围语义。新增 `render_aspect_contract_block`/`render_aspect_status_block` 纯函数;`nudge_pending` 消费随状态半,仍单一调用点。
- K/D:内容判据不改,只改位置。消息形状 `system(UNTRUSTED? + S)` + `user(C + K + D + T)`。`off` 走原路径原顺序。
验收(§12):同 run 额度耗尽/方面变化/轮数增长 ⇒ S、C 字节不变;最终消息检查覆盖 wrapper,S/C 开头无动态值;P 与 off 冻结输入下同一批证据事实与执行限制;T 的 `allowed_actions` 与 `parse_reflect_v2` 白名单同源;`unavailable_action:*` 计数两模式可读;off 与关闭态字节等价;Knowhow/legacy 不受影响。用例:(a) `_V2ContextLLM` 加 `system_prompt(turn)`/`turn_state_block(turn)` 取值器,额度耗尽多轮 run 断言 S、C 不变;(b) 方面 unresolved→supported ⇒ C 不变 T 变;(c) 还原 provider-facing 消息含 wrapper;(d) 冻结 fixture 上证据键集/可用动作/额度逐项相等;(e) 选目录有、T 无的动作 ⇒ `unavailable_action:<既有 reason>` 零 I/O 观察、循环继续(过 `_GatedV2LLM`);(f) `nudge_pending` 只消费一次且在 T。

**实施记录(2026-09-10)。** 落地形态与上面一致,三处与计划字面不同的取舍,都记在这里:
- 「T 里的服务端当前状态优先于 K/D 过时观察」那一句放在 **S**(§3 的 T 条目把它列在 T 里)。它是**指令**而不是状态,设计 §4.5 的原话也是「S 规定」;放在 S 里它每轮零成本、且读起来是规则而不是数据。
- T **不新铸额度数字**。§4.2 要 T 带「相关剩余额度」,兑现方式是被搬过来的那几块自带:不可用行的原因词(`per-run budget spent` 等)、服务器状态摘要里的集合地图额度行、以及紧排在 T 之上的观察行 `余额=`。另铸一套会让「P 与 off 在冻结输入下执行限制逐项相等」这条验收失效,也会让同一笔预算有两处可以互相分叉的渲染。
- C 的「范围语义」由既有的 `SCOPE_DEIXIS_GROUNDING`(在 S,run 级)与问题原文承担。拍板 Q3 把 `summary` 整块搬到 T,所以没有别的范围文本可以在不动内容判据的前提下挪进 C。
- 新增守卫「块序 C→K→D→T 且 T 在最末」:验收 (a)–(f) 六条对「T 挪到 K/D 之前」全绿(S 与 C 仍在原处、字节仍不变),而那正是这条臂唯一不可协商的东西。变异验证补出来的。
- `render_aspect_block` 一个字节未动(off 专用),两半**不组装**它:off 的行把原文与状态排在同一行,拆开后拼不回同一串字节。

### T-PS9 文档与门
`docs/deployment-and-configuration_zh.md:848`/`.md:1047` 之后新增 `REASONING_REFLECT_OPTIMIZATION`(默认 off、四取值、本期两格、v2 关与 Knowhow 忽略、不是前端档位、预告 `REASONING_REFLECT_RECENT_OBSERVATIONS` 在 delta 模式 = 重建 K 时保留几条,以及独立测量开关);`docs/product-and-api_zh.md:1408/1422`、`.md:1947/1961` 补 P 模式分块与稳定性口径(合同不变);**llm.jsonl 字段契约**——在「`finish_reason` 无条件写进 LLM 调用日志」那一级(`_zh.md:1452`/`.md:1991`)补 `attempts`(这一次逻辑调用真正发出的请求数,只在终态行,`status="retry"` 行不带,按行累加会重复计入同一次调用)与 `response_chars`(交还给调用方的正文长度,不经 `LLM_LOG_MAX_CHARS` 截断),中英成对,只加数值键;`architecture.md:111` 补静态目录产地与 `reflect_optimization()` 唯一读点、登记 `reflect_context_bench.py`;`scripts/README.md:488` 附近补第二维臂与 `EVENT_LOG_DIR` 隔离。不改 AGENTS/CLAUDE。

**T-PS4 评审后追加(2026-09-10)。** 还要过一遍 `docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md` 的**§键集**一节:T-PS4 在闭集上加了九个顶层键、`context_chars` 的短码含一个字节口径的 `bytes_total`,那份设计稿的键集清单因此已经落后。带日期的设计稿本身不改(它是当天的决定),在 T-PS9 里以「后续修正」的形式在**引用它的那一级**记清差异,别让下一个人拿旧清单当闭集真源。

**实施记录(2026-09-10)。** 本任务只改文档与注释,不改任何 `.py`。落点与上面字面不同的地方,都记在这里:

- **`docs/deployment-and-configuration{,_zh}.md`。** 两条配置在 T-PS6/T-PS3 已随代码写进数值表,本任务做的是**核对定稿 + 补两处**:① `REASONING_REFLECT_RECENT_OBSERVATIONS` 那一条补 delta 模式的预告(设计 §5.1 的原话:snapshot 保持旧含义,delta 用于重建 K 时保留多少条近期详细观察、不触发逐轮滑窗),并明写「那两格今天被启动期校验器拒绝,所以这是预告不是当前行为」——不然读的人会以为配了就有。② `REASONING_REFLECT_OPTIMIZATION` 里「**尚未落地的是测量**」那半句已经**过时**(T-PS3/T-PS4 落地之后写的),改成「测量要另开一格,尺子是下一条,两条臂共用同一次装配因此共用同一把尺子,本项自己一个数都不写」。其余定稿项(默认/四取值/本期两格/v2 关与 Knowhow 忽略/不是前端档位/正交/未命中 +1–3KB/峰值两轮/失败只表现为键缺席)逐条核对,与代码一致,未改。
- **`docs/product-and-api{,_zh}.md`。** P 模式分块那一级 T-PS8 修正轮已写全(五个 `context_chars` 是稳定性类别、C/T 是差值、`off` 的 T 是排最前的服务器状态摘要、两臂同形同尺),本任务补两块:① `message_prefix_bytes` 的定义单列一条——量的是**整条最终序列**(含 wrapper 与帧开销)的 UTF-8 字节、同一轮的两次尝试都与上一轮比、首轮如实缺键、与同轮字节总数同口径,并写明它是**客户端结构指标**而非上游复用量(字段名里没有任何「命中」类词,与 §2 硬约束同源)。② 闭集投影新增九键的一句话说明,连同「没量到 = 缺值不是 0」与「`model_calls_real`/`response_chars_total` 任一步缺 ⇒ 整列 unknown」两条口径,并在同一段落末尾按上面那条追加记下**T0 设计稿键集的后续修正**(带日期的设计稿不动,闭集真源指向 `backend/app/domain/reasoning_trace_stats.py`)。**llm.jsonl 字段契约**那一级(`attempts` 只在终态行、`status="retry"` 行不带、按行累加会重复计入;`response_chars` 不经 `LLM_LOG_MAX_CHARS` 截断)T-PS2 已按本节要求写进中英两侧,本任务只核对,与 `llm.py` 935/941/944/853-859 一致,未改。
- **`architecture.md`(reflect 那一段)。** 无 `_zh` 配对(全仓只有一份 `architecture.md`),所以只改这一处。补:两条判据的唯一读点与 AST 守卫的用例名、静态目录产地 `static_catalog_facts` 的归一清单与「恒为超集」的理由、`_prime_static_catalog` 的时机与 `!= "off"` 判据、`ReflectContext` 两个消费点**双向**互斥(各自对非空的对侧字段抛错)、测量落账点 `_TraceRecorder.__call__` 与越界键 `raise` 而不是 `assert` 的理由、以及登记 `backend/app/eval/reflect_context_bench.py`。⚠ 那个模块由 T-PS5 在 `claude/reflect-prefix-rig` 分支落地,本分支没有它;登记按 T-PS5 的交付描述写(纯分析、零 I/O、llm.jsonl ⋈ events.jsonl on `support_id`、闭集行键),**两条分支合并后要核对一次**。
- **`fangan_todo.md`。** 在「reflect v2 开闸前待办」下新增子项 (j),体例同 (i):PR-2 已交付什么、仍未做的是拿它跑对照实验、开闸仍是独立决定,外加七条已知限制——`search` 投影 `optimization` 恒 unknown(第二维只接在 `ab`)、`AskCancelled` 整批收摊不产行、`--only-policy` 与 `--arms` 互斥、`EVENT_LOG_DIR`/`LLM_LOG_PATH` 目录不对齐的启动告警、`prefix_snapshot` 每轮 +1–3KB、PR-3/4/5 待做、A/B 采用与开闸的拍板点(设计 §13)仍在用户手上。前三条与第四条是 T-PS5 的实况,同样待合并后核对。
- **`scripts/README*` 一个字未动**:T-PS5 已在 rig 那一节写了第二维臂、`EVENT_LOG_DIR` 隔离与 per-call 表,本任务不重复写以免合并冲突。

依赖:T-PS1→T-PS2→T-PS3;T-PS6→T-PS7→T-PS8;T-PS4 依赖 T-PS3;T-PS5 依赖 T-PS2+T-PS4+T-PS6;T-PS9 收尾。两条链可并行,T-PS5 汇合。

## 4. 刻意不做
不实现 `prefix_delta`/`lean`;不改 K/D 选择判据;不改首轮查询宽度/范围/最大步数/工具次数;不加原生 tool calling/完整聊天历史/reasoning 回放;llm.jsonl 只加数值;请求文本不进日志/投影;生产策略不加实验标记(E1 接缝留 PR-5);不改 `policy_version` 语义;不动 `recognized_actions` 与 schema 枚举。

## 5. 拍板结果(主 agent,2026-09-09)
- **Q1** 未实现取值:**拒绝**(校验器抛 ValueError,PR-3/PR-4 各放开一格)。
- **Q2** 公共前缀内存口径接受(一轮消息字节);**独立测量开关** `REASONING_REFLECT_MEASURE_CONTEXT`(默认 false),与 `optimization` 正交,`off` 臂也能出 `message_prefix_bytes`。
- **Q3** `summary` **整块搬到 T(末尾)**,按设计 §4.5;P 与 B 的差异包含布局这一点在报告里写明(§9.2 已承认)。
- **Q4** 静态目录在本 run 第一次 reflect 时生成并缓存,接受「范围收窄后目录留着不可用动作」的形态,T 每轮如实说明;验证多覆盖这一形态。
  - **评审后修正(T-PS6/T-PS7 评审,2026-09-10)**:`scope_restricted` 由「按通道位原样带过」改为**归一为 False**,目录因此**恒为超集**。原写法在范围收窄的 run 里会让目录**少掉**五个范围敏感动作:那时它不再是超集,模型压根不知道这几个工具存在;而来源勾选上限是**请求级**的、判据按契约禁止 memo,目录一个 run 只定型一次,上限之后放宽也补不回来。收窄与放宽一律由每轮当前状态说明(`source_scope_unsafe_channel` 每轮照报),与「目录不授予资格」是同一条原则的两半。`outline_repair_available` 同期一并归一为 False(逐轮项一律归一;投影结果一格不变,只为分类上不留活扣)。
  - **评审后修正(同上)**:静态目录只在 `reflect_optimization() != "off"` 时构造。纯测量臂(`off` + 测量开)不需要目录——测量量的是消息字节,目录是布局的输入,给它构造一份是没有消费者的开销。`reflect_measures_context()` 保留给 T-PS3。
- **Q5** 归因口径优先级:`support_id` 关联优先;无 events 文件时退时间窗;并发 > 1 且只能时间窗 ⇒ unknown。每行记 `attribution`(`support_id|window|unknown`)。
- **Q6** 逐轮前缀字节不进闭集投影,退成 `prefix_bytes_median/min/turns` 三格;逐轮细节只在 rig per-call 表。
