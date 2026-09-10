<!-- 主 agent 拍板(2026-09-11):§2「由 Plan 建议」的 Q1–Q10 全部采纳;U1/U2/U4 按建议作为 CLI 默认(E3 质量证据走人工盲审、E2 默认三臂 + `--arms` 显式加 L、E1 默认 96 / `--smoke` 48),用户可改;A/B §13 六个拍板点仍待用户。 -->

# reflect 前缀复用 · PR-5 实施计划:T4 实验通道(E1/E2/E3)+ T5 文档收官

> 设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`(§8 测量合同、§9 三层实验、§10 分析与采用标准、§11 T4/T5、§12 验证、§13 交付物与回退、§14 入口)。
> review 调整真源:主 agent 2026-09-09 六处调整,本期直接相关的是第 3 条(E2 = 脚本化驱动器 + 真实单次决策探针)与第 4 条(E1 标记走 `chat_json` 默认关闭的内部接缝、位于 wrapper 之前;命名不得出现 cache_hit/命中率;E1 只报「稳定前缀的时间收益」)。
> 代码基线:`claude/reflect-prefix-lean` @ `2bcaadd82`(= PR #708 待合入内容,含 PR-1..4 全部)。
> 本文只规划,不含实现;真实 E1/E2/E3 由环境操作者跑,不进 `check.sh`。

## §0 结论先行

三层实验回答三个**不同**的问题,各自的最小实现面差一个数量级:

| 层 | 回答的问题 | 不回答的问题 | 最小实现面 |
| --- | --- | --- | --- |
| **E1** 前缀敏感性探针 | 在这个端点/这个负载/这三个长度档上,**稳定前缀**有没有可重复的墙钟收益 | 命中率、省了多少 token、provider 有没有关缓存;reflect 质量 | `llm.py` 一处**默认关闭的 ContextVar 接缝** + `app/eval/` 一个纯计划/分析模块 + rig 一个薄子命令。**零数据库、零检索、零 reflect** |
| **E2** 固定状态真实决策 | 把轮数与工具反馈**冻住**之后,四臂在同一状态点上的**单轮**成本与决策行为差多少 | 自洽真实轨迹、端到端收益(动作不执行) | `app/eval/` 一个脚本化驱动器 + 一个 `model_clients` 代理 + 12 例自包含 fixture + rig 一个薄子命令。**服务端零改动** |
| **E3** 真实自主循环 + 完整 Ask | **实际采用哪一种模式**(配对墙钟 + 质量 + 失败/尾部) | 机制归因(那是 E1 的事) | 既有 `ab` 子命令**已经就是它**;缺的只有三件:整批墙钟预算与停止派发、运行 manifest、配对读出路径(`analyze` 三处) |

一句话结论:**E3 的代码几乎已经写完了**(PR-2 的 `ab` 二维化 + PR-4 的第五格),PR-5 真正要新写的是 E1 与 E2 两条通道、一份 manifest、和 `analyze` 里那三处让 D↔L / 逐题配对读得出来的口子。

## §1 机制核实(编号 · 文件:行 · 结论 · 与设计字面的冲突)

### M1 E1 的接缝位置:`chat_json` 里只有**一处**装配点,`provider_messages` 是纯函数

`/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/backend/app/core/llm.py:217-256`:`_PROVIDER_WRAPPER_PREFIX` 与 `provider_messages(messages, response_schema_hint)` —— 后者是**模块级纯函数**,返回 `[{role:system, content:wrapper+hint}, *messages]`,docstring 明写「exactly ONE assembly」。`:640` 的 `full_messages = provider_messages(messages, response_schema_hint)` 是 `chat_json` 里唯一的调用点,而 `full_messages` 同时喂三处:`llm_key(...)`(`:679`)、`record["request"]["messages"]`(`:765`)、真正的 `.create()`。

结论:「开头标记位于 wrapper 之前」只有一个可落的位置 —— 给 `provider_messages` 加一个**默认 `None` 的第三参数**,由 `chat_json` 从 `llm.py` 模块级 ContextVar 读一次后传入。`markers is None` ⇒ 返回的列表与今天**同一个表达式、同一个对象形状**,生产路径零字节变化(判据:`provider_messages(m, h) == provider_messages(m, h, markers=None)` 且 `chat_json` 只多一次 `ContextVar.get()`)。

不选 `chat_json` 显式 kwarg 的理由:那要求 `ScheduledJsonChatClient.chat_json`(`backend/app/services/model_provider.py:486-501`,签名逐项枚举)同步加一格并转发,公共传输签名多一个「实验注入位」,与设计 §9.1「不接受公共请求注入」相悖。ContextVar 是**模块内部**的,且有现成先例:`interaction_support_scope`(同文件 `:345` 的 `submission_context.run` 会把 ContextVar 带进调度线程,所以接缝在异步调度下照样成立)。

### M2 E1 的两臂构造与计时读数

设计 §9.1 要求两臂「正文与输出任务一致,只有预定标记稳定性变化」:

* **stable**:开头标记在一个序列(4 次连续调用)内固定,末尾标记逐次变化;
* **disturbed**:开头标记逐次变化,末尾标记在序列内固定。

两臂标记**个数与长度相同**、同一格式、同一位置。可落形状:开头 = 在 wrapper **之前**插一条 `{"role":"system","content":<定宽短码>}`;末尾 = 把定宽短码**追加到最后一条消息 content 的尾部**(拷贝,不改调用方的映射 —— `provider_messages` 的 docstring 明确它 never mutates)。末尾不另起一条消息,是为了让消息条数与生产 reflect 一致(2 条 + 头标记),不让「多一条尾巴消息」本身成为一个变量。

计时:`call_stats` 出参(`llm.py:333-361` 的 `CALL_STATS_KWARG`,`:387-440` 的 `_record_call_stats`)在四个出口都写 `status` / `call_wall_ms` / `attempts` / `attempts_observed`,有就写 `finish_reason` / `response_chars` / `usage`(含 `cached_tokens`,provider 不回就**缺席**,绝不折 0)。`ScheduledJsonChatClient` 自称 `supports_call_stats = True` 并转发(`model_provider.py:519-530`),所以 E1 走 workload 适配器时拿得到全部读数。`cached_tokens` 若在,只作**附录一个计数**呈现,不造分母。

rig 入口:**新子命令**,不是 `search`/`ab` 上的 `--probe` 开关。理由是 E1 不跑检索、不连库、不建 repo,挂进 `cmd_search` 会让那条已经很长的路多一条与语料/范围/意图全部无关的死分支。

**E1 不需要数据库**:`backend/app/eval/mrl_truncation.py:214-227` 已有先例 —— `RuntimeModelProvider(settings, EventLogger(settings, channel="events", per_user=True))` 独立构造、用完 `.close()`。E1 照抄这条,取 `.chat("reasoning_agent")`(与 reflect 同一个 workload、同一个端点、同一个 thinking 配置),因此「业务只能经 ModelProvider workload adapter 使用模型,不直建客户端」(§11)自动满足。

### M3 E2 的接缝是 `model_clients`,而且**已经是一个参数**

`reflect()`(`backend/app/services/reasoning_retrieval.py:5825`,`:5854` 处 `client = self.model_clients.chat("reasoning_agent")`)—— 客户端不是实例字段,是每轮从 `self.model_clients` 现取的。而 `_construct_reasoning_retriever`(`:9475-9503`)里 `model_clients=repository`,`ReasoningRetriever.__init__`(`:4005-4020`)把它原样存下。

结论:E2 的「假反思驱动器」只需要一个 **`__getattr__` 委托到真 repo、只覆写 `chat()` 的薄代理**,再走 `ReasoningRetriever.from_repository(proxy, settings_for_arm, cancel_event)`。服务端**一个字节不改**,不 monkeypatch provider,不 import `backend/tests/model_testkit.py`(那里的 `bind_chat_client`,`:156`,是给测试用的 provider 覆写,把它拉进 rig 会让实验路径依赖测试目录)。

现成的驱动器形状在测试里已经有两个可抄的:`_SeqLLM`(`backend/tests/test_reasoning_retrieval.py:811-823`,plan 固定 + reflect 按序列返回 + 按 `"sub_queries" in schema_hint` 区分两类调用)与 `_GatedV2LLM`(`:7301-7340`,载荷过真闸并留存每轮 system 段/schema hint/messages)。E2 的驱动器 = 前者的剧本机制 + 后者的留存机制 + **状态点处转发给真客户端**这一件新事。

「不序列化 `_ReasoningRunState`」怎么成立:**每个状态点自己跑一条 run**。同一案例的第 k 个状态点 = 用同一份剧本从第 1 轮重放到第 k 轮(全部由剧本推进,零真实调用),第 k+1 轮把这一轮**已经定型的那两条消息**转发给真客户端调一次,记录返回的决定 JSON,然后向 `run()` 返回一条脚本化的停止决定 —— 于是模型选的动作永不执行、不进 trace、不落库。四臂各自重放同一份剧本,同一状态点因此由四种布局各渲染一次(布局由 `settings.reasoning_reflect_optimization` 承载,`reflect_optimization()` 是全仓唯一读点,`:4190-4220`)。

**零模型调用地推进到状态点**的两个前提都成立:

* `run()` 拿到非空 `intent_queries` 就不调 plan 的 LLM(`scripts/reflect_shadow_rig.py:2091-2095` 的 docstring 明说),所以 E2 的 fixture 里冻一份 `QueryIntentContract` 走 `_prepare_search_intent`(`:2086-2150`)即可 —— 既省掉意图调用,又拿到**真实多方面账本**(`intent_detail.mandatory_topics` 是 v2 方面账的来源)。这比 `--no-intent` 好得多:后者只剩整题一个方面,而方面数直接决定 T 的方面块与 L 的 lean 自评段字节。
* 剩下唯一的真实模型消耗是**embedding**(`retrieval_query_embedding` workload,检索必需)。它不是 chat,不计进 §9.2 的 216 次逻辑调用,但 dry-run 必须单列一行说清「另有 N 次 embedding 调用」。

E2 的执行入口沿用 `run_search_once`(`:2151-2213`)的三层 scope 形状(`model_work_scope(INTERACTIVE)` / `retrieval_run(event_log=None)` / `source_scope_context`),只把 repo 换成代理。

### M4 E3 与既有 `ab` 的关系:矩阵已经能表达,缺三件事

`ab` 已经是「逐 run 切策略、真实闭环、完整 Ask(意图契约 + 检索 + 合成 + 引用绑定)」:`cmd_ab`(`:3268-3360`)、`run_ab_once`(`:3906-3997`,调 `repo.ask_reasoning`)、`_run_ab_unit`(`:4572-4618`,两臂在单元内背靠背 + 臂序随机)、`_settings_by_arm`(`:1741-1800`,逐臂构造 Settings 并三重回读)、`ARMS` 五格(`backend/app/eval/reflect_ab.py:101-107`)。

设计 §9.3 的两步矩阵**不用改一行枚举**就能表达:

* 筛选批 12 题 × 2 档 × 2 臂 × 1 = 48 个完整 Ask ⇒ `--limit 6`(A/B 各 6 题)`--repeats 1`;
* 主确认 34 题 × 2 档 × 2 臂 × 3 = 408 ⇒ 默认参数 + `--arms <候选对>`。

**缺的三件**:

1. **整批墙钟预算**。`_ab_run_batch`(`:4412-4500`)只有「首个逃逸异常 ⇒ 收摊」这一条,没有 §13 要的「运行达到整批预算时**停止派发**、按既有取消边界处理在途请求,并保留未完成/不成对标记」。既有的取消机制现成(`cancel_event.set()` + `shutdown(cancel_futures=True)`),只差一个 deadline 判据与一句 dry-run 打印。
2. **运行 manifest**。全仓没有 manifest 写点(grep 无 `manifest`)。§13 要的每一项在 rig 里都已经有产地:`corpus_signature`(`:3581-3635`,含来源 id/标题/updated_at、chunks/elements 行数、KG 对象数)、`model_contract`(`:3538-3580`)、`ab_contract_digest`(`:4501-4526`)、臂与 optimization、`--repeats`/`--round`/`--limit`/`--cell`/`--efforts`;缺的只有 code SHA(一次 `git rev-parse HEAD`)、预算/截止时长、以及**把它们写成一个文件**。
3. **配对读出路径**(见 M5)。

**登记的字面冲突**:§13 要求 manifest 写「随机种子」,而 `_run_ab_unit:4586-4587` 的 docstring 明确「随机不带种子 —— 它要的就是不可预测,而这批数据的可复现性由题号/档位/轮次那三维负责」。本期**不改这条**(改它会让臂序成为可预测量,而两臂背靠背的全部意义就是让 provider 漂移对两臂等量);manifest 如实写 `arm_order_seed: null` + 一句「臂序按 run 无种子随机,可复现性由题号/档位/轮次承载」。E1 的区组随机则**必须**有种子(§9.1 要求「区组内随机 stable→disturbed 或相反,整体平衡」——平衡与可复现都是要求),所以 `--seed` 是 E1 的必填项、进 manifest。

### M5 §10 分析方法:`analyze` 现在有什么、缺什么

`scripts/analyze_reasoning_trace.py` 现有:四组指标(数值/布尔/枚举/计数字典)、`group_rows` + `summarize_group`(n_observed/均值/P50/P95)、`--min-samples`(`:677`,默认 5,分位数样本门)、两张配对表 `pair_table`(沿 `policy_version`,`:311-359`)与 `optimization_pair_table`(沿 `optimization`,`:360-411`)、`PAIR_SIDE_METRICS` 九项(`:418-424`)与 `SPARSE_PAIR_SIDE_METRICS` 的逐项 `n_measured`(`:446-450`)。

**缺四件,每一件都对 §10 有直接后果**:

1. **基线恒 `off`**(`:170` `OPTIMIZATION_BASELINE = "off"`)⇒ 一批只有 D 和 L 时两侧配对表全空,§10.2-4「P 与 D/L 的配对中位收益差不足 5% 就选 P」这条门**读不出来**。已在 `fangan_todo.md` (l) 与 `scripts/README.md` 登记为已知限制,PR-5 是它约定的解除处。
2. **只有均值,没有配对差值/比值**。`_pair_side`(`:452-472`)每一项走 `_mean`。§10.1 的主指标是**配对**比 `candidate_ms / baseline_ms` 与配对差毫秒,且要求「先对同题同档的重复汇总,再按题型/有图无图/档位比较」、「不用两组独立 P50 的比值冒充配对比值」。今天的表结构做不到这件事 —— `PAIR_DIMENSIONS`(`:139-142`)里有 `question_key`,所以格已经是逐题的,但格内两侧各自取均值之后差值就丢了配对身份。
3. **`ab-runs.jsonl` 喂不进去**。`load_rows`(`:173-195`)断言 `set(row) ⊆ RUN_PROJECTION_KEYS`,而 A/B 行多带 `AB_ONLY_KEYS`(`backend/app/eval/reflect_ab.py:43-73`:`paired`/`arm`/`repeat`/`answer_chars`/`anchors_on_gold`/…),整批被拒。
4. **删失观察没有口径**。`JOB_STATUSES = ("running","done","failed","cancelled")`(`backend/app/domain/reasoning_trace_stats.py:203`)—— 被整批预算掐掉的 run 走既有取消路即落 `cancelled`,与普通 `failed` 天然分得开,**不需要新闭集值**;缺的是分析侧把 `cancelled` 单列成「删失」而不是混进失败率,以及「共同截止时长」这个数(它只存在于 manifest 里)。

§10.2 五条门 ↔ 投影列的对账(用来决定要不要加列):

| §10.2 | 读哪些列 | 现状 |
| --- | --- | --- |
| 1 质量先行 | `citations_out_of_scope` / `anchors_unresolved` / `anchors_on_gold` / `completeness_claim` / `gold_facts_hit`ᴹ / `aspects_missed`ᴹ / `human_*` | 确定性那几列 T-AB2 已实现;**gold 与判分/人工那几列恒 `None`**(T-AB1/T-AB3 未做,见 M6) |
| 2 稳定性 | `status` 分布 / `failed` / `termination_reason` / `answer_empty` / `finish_reason_codes` | 全部已在 |
| 3 时间收益 | `run_wall_ms`(配对中位比)+ 按 `effort`/`corpus_cell`/`shape` 分格 | 列在;**配对比值算不出**(缺件 2) |
| 4 优先较简单候选 | P/D/L 三者两两的配对中位差 | **算不出**(缺件 1) |
| 5 收益不确定 ⇒ 保持默认 | 不需要新列;是一句报告纪律 | —— |

辅助指标(§10.1 第二条)全部现成:`model_calls_real` / `prefix_bytes_median` / `prefix_turns` / `context_rebuilds` / `context_fallback` / `assessment_rows_total` / `assessment_observed` / `aspects_unassessed` / `actions_by_type` / `skip_reasons` / `fallback_reasons` / `context_chars`。**不新增任何投影列** —— 这是本期的一条硬纪律(投影键集是数据集形状合同,PR-5 是读侧任务,不是写侧任务)。

### M6 E3 的质量前置:gold 与 judge **都还没做**

`backend/app/eval/reflect_t0/questions.json`:34 道 ask 题 + 4 道 report 题,**`gold_facts` 与 `gold_sources` 的覆盖数都是 0**(逐题实测)。`load_ab_gold` / `resolve_gold_sources` / `count_anchors_on_gold` 的加载与校验代码在(`reflect_ab.py:237-536`),`_ab_assert_gold_resolves`(`scripts/reflect_shadow_rig.py:4272-4313`)在跑批前会跑,但**没有 gold 就整批跳过**(`if gold is None or not gold.gold_sources: continue`)。rig 的子命令闭集(`:5395`)里也没有 `ab-judge` / `ab-report`。

结论:A/B 设计 §12 的 **T-AB1(gold 补齐)与 T-AB3(判分与出表)一格未动**。这直接决定 E3 的边界:筛选批(48 个 Ask)可以在没有 gold 的情况下跑并读出**时间与稳定性**两条门;**主确认批不得据它宣布采用** —— §10.2-1「质量先行」在没有 gold/盲审的情况下只能记「未验证」,而 §10.2 明说未验证 ≠ 通过。这是 §2 里必须由用户拍板的第一件事。

### M7 命名与隐私红线的现状(已经守住的,和一处必须说清的)

已守住:`reflect_context_bench.py` 的模块 docstring 明写「这张表里没有、也不许有任何 `cache_hit` / 命中率 / hit rate 形状的列」;`AB_PROJECTION_KEYS` / `CALL_ROW_KEYS` 两道闭集守卫 + `assert_projection_values` 逐行自检;`_log_side`(`:150-170`)只取数值与短码,请求正文/响应片段/模型名/`ts`/`id` 一个不取。

**必须在计划里说清的一处**:`llm.py:761-779` 的 `record["request"]["messages"]` **确实**把每条消息的 role/content 按 `llm_log_max_chars`(默认 4000,`config.py:1418`)截断后写进 `llm.jsonl`;`record["response"]["content"]`(`:944`)同理。这是**接入前就有的既有结构**,不是本期引入的。它落在哪里由 `_rig_process_env`(`:1671-1709`)决定 —— rig 把 `LLM_LOG_PATH` / `EVENT_LOG_DIR` 圈进本次 `--out-dir`,不往机器共用的 `.local/logs` 写一个字节。所以口径要写成:**测量产物(per-call 表、投影行、聚合报告、manifest)不含任何请求/响应正文;`.local/<out-dir>/llm/` 下那份交互日志含截断正文,它是 §8.2 说的「操作者控制的 `.local` 私有实验资产」,不进数据集、不进仓库、不进共享报告**。review 调整第 4 条的「请求文本只在内存、不进日志」讲的是 `message_prefix_bytes` 那次序列化(`reasoning_retrieval.py:508-520`,`serialize_provider_messages` 的结果只用来算公共前缀,立刻丢),不是要求关掉既有的交互日志 —— 关掉它会同时废掉 per-call 表的传输侧半边(`join_calls` 的 `event_only` 分支就是为 `LLM_LOG_ENABLED=false` 留的)。E1 的标记值会出现在这份日志里,无所谓:它们是本地分配的无语义定宽短码。

### M8 热函数与「不进标准门」

`run()` / `_new_run_state` / `_run_enumeration` 三个热函数的 `function_length_ceiling` 是**精确相等**(长短都红,PR-4 记录在案)。本期落点里**没有一处**在 `reasoning_retrieval.py` 里 —— E2 走 `model_clients` 代理,E1 走 `llm.py`,E3 走 rig 与 `analyze`。这是把 E2 设计成「代理 + 剧本」而不是「给 `reflect()` 加注入参数」的直接收益。

标准门:E1/E2/E3 的**真跑**不进 `check.sh`;它们的 **dry-run、纯函数与闭集守卫必须进**(与 `ab --dry-run` 用例同一条口径,现有 `backend/tests/test_reflect_t0_scripts.py` 104 个用例 + `test_reflect_ab.py` 149 个是范本)。`check.sh` 本身已把 `SILICON_NOTEBOOK_ENV_FILE` / `MODEL_SERVICES_CONFIG` 清空,结构上调不到真实模型。

## §2 拍板点

### 必须由**用户**拍板

**U1 · E3 的质量证据:付不付 T-AB1(gold)+ T-AB3(judge/report)?**
现状见 M6:34 题 gold 一条没写,判分与出表两个子命令不存在。三个选项:

* (a) **只跑筛选批**(48 个 Ask):读时间 + 稳定性两条门,质量那条记「未验证」,不做采用决定。成本最低,但按 §10.2 与 §13 只能交付「可重跑命令 + 质量验证未完成」。
* (b) **人工盲审替代 judge**:跑主确认批,质量靠 §6 第二层的人工盲抽(24 份 Ask)。需要 gold 之外的人力,但不写代码。A/B §13-4 已经把「人工抽样谁做」列为待拍板。
* (c) **先补 T-AB1 + T-AB3**:34 题 × ≤3 条 gold 逐条核出处(A/B §13-5 已列为待拍板,且明说这是整份 A/B 的精度上限所在)+ 判分与出表两个子命令(~600 行)。
建议:**(b)**。理由:§10.2-1 原话是「人工盲审确认必答项、事实及引用质量没有可复现回归」,gold 在原文里只是辅助(「gold 只覆盖部分事实,不能仅凭 gold 命中率宣称全题等价」),模型判分更是「只做粗筛,一个数都不进开闸判据」。花在 gold 上的精力换不来判据强度,而人工盲抽是那条门的**唯一**合格证据。把 T-AB1/T-AB3 留在 A/B 的债务台账里。

**U2 · E2 跑三臂还是四臂?**
设计 §9.2 写的是 B/P/D 三臂 216 次逻辑调用;本期任务书要求四臂(off/P/D/L)同状态点各调一次 ⇒ 288 次。
建议:**默认三臂(B/P/D),L 由 `--arms` 显式加入(+72 次)**。理由:L 在**固定**状态点上相对 D 的差是**净增**且已经实测过(S 的 lean 段 +1,246 字符、T 那一句 +3 字符,见 `fangan_todo.md` (l)),它省的那一侧(模型少重述已支撑项 + 少一次收尾轮)在 E2 的「不执行、单轮」设定下**结构上看不见**。跑 L 的 E2 只能得到一个已知为正的字节差和一个单轮墙钟差,判不出 L 该不该上 —— 那是 E3 第 2 步的题目(§9.3-2)。但把 L 摆进来有一个真实收益:它能验证「lean 合同下模型的决定形状没退化」(方面绑定、是否正确停止),所以留成显式开关而不是砍掉。**这一条要用户拍预算。**

**U3 · A/B 设计 §13 的六个拍板点仍然待用户,本计划不代替。**
逐条列在 §4 的「用户拍板项」表里(主库只读跑完整 Ask 要不要付最小生产改动 / G1 全量预算与 G2-G3 / 判分模型选谁 / 人工抽样谁做 / gold 由谁写 / 硬判据 6 的阈值)。PR-5 的实现不依赖其中任何一条**除了 U1**,所以它们可以在跑批之前任何时刻拍。

**U4 · E1 的初始运行规模:96 次全量,还是先 48 次冒烟?**
设计 §9.1 两个数都给了,并明说「可先跑 48 次冒烟,但不凭该前缀直接定论」。
建议:**dry-run 两个规模都能打印,默认 96;`--smoke` 走 48**,报告里如实标是哪一个。

### 由 Plan 建议、实施时按此执行(不劳用户)

**Q1 · E1 接缝形状**:`llm.py` 模块级 `ContextVar[tuple[str,str] | None]`(默认 `None`)+ `provider_messages(messages, hint, markers=None)` 第三参数 + 一个 `experiment_message_markers(head, tail)` 上下文管理器。头标记 = wrapper **之前**一条独立 `system` 消息;尾标记 = 追加到最后一条消息 content 尾部(拷贝)。
理由见 M1。守卫三条:(a) `markers is None` 时 `provider_messages` 与 `chat_json` 的字节等价用例;(b) AST 判据「`_EXPERIMENT_MARKERS.set(` 在 `backend/app/` 下只允许出现在那个上下文管理器体内」(照抄 PR-4 `test_the_aspect_ledger_has_exactly_one_construction_point` 的形状);(c) 一条负向断言「`app/services/` 与 `app/application/` 下一处都不 import 那个上下文管理器」—— 设计 §9.1「不在生产策略里加入随机标记」。

**Q2 · E1 走 `bypass_cache=True`**。`full_messages` 含标记 ⇒ `llm_key` 逐次不同,disturbed 臂每次都是新键。E1 不传 `response_validator`,所以本地响应缓存的命中门与写门本来都不放行(`llm.py:670-700` 的 opt-in 双门),但显式 `bypass_cache=True` 让「这批数一定不含本地缓存出口」成为一条**结构事实**而不是一条推理,并且避免 `status="cache_hit"` 这个第四出口污染 E1 的 `status` 分布。

**Q3 · E1 的上下文样本**:仓库内一份**合成的、非敏感**的五块 reflect 上下文样板(`backend/app/eval/reflect_t0/prefix_probe_sample.json`,几百字符的种子 + 确定性组装规则),三个长度档由 `REASONING_REFLECT_EVIDENCE_CHARS_BY_EFFORT` 的已登记默认值派生(短 = overview/standard 档量级、中 = deep、长 = exhaustive),并支持 `--sample-file` 指向操作者 `.local` 的真实样本。manifest 记录用了哪一份(短码 + 长度,不记正文)。
理由:§9.1 要求「固定、非敏感」且「不为刷命中而追加大量无关填充」;从预算默认值派生长度档,是唯一能让「短/中/长」对得上真实样本量级、又不需要把真实笔记内容签进仓库的做法。

**Q4 · E1 预热**:整批开头做一次小型连接预热,**用不同正文与不同前缀**,预热成本**单列**不进主统计(§9.1 原文)。

**Q5 · E2 的四件 fixture 约束**(写进 case schema 的校验器,畸形当场响亮失败):

* **一个 case 不含任何数据库 id**。剧本里的动作只用查询串型参数(`search_elements` / `search_chunks` / `add_subquery` / `enumerate_*` / `exact_lookup`);`expand_graph` / `follow_chain` / `ppr_retrieve` 这三个需要候选池内 `object_id` 的动作**本期不进剧本**,登记为债务(要它们得先给剧本加一层「按类型取当前候选第 N 个」的间接层,而驱动器只看得见渲染后的文本)。「有图/无图」这一维改由**语料格**承载(`B_kg` vs `A_nokg`),不由图动作承载 —— 这是与设计 §9.2「有图/无图」的一处**实现口径收窄**,要在计划与报告里都说明。
* **每个 case 冻一份 `QueryIntentContract`**(见 M3),于是零意图调用 + 真实多方面账本。
* **每个 case 可带一小组 settings 覆盖**(仅用于制造形态:`reasoning_max_element_searches=1` 造「工具耗尽」、同一查询重复两轮造「重复」、一个必然零命中的查询造「失败」)。同一 case 的四臂用**同一份**覆盖。
* **三个状态点的轮号写在 case 里**,第三个点要求落在压缩边界之后。做不到就**如实报告**:rig 在 D 臂那条 run 上事后核 `context_rebuilds ≥ 1`,不成立就在产物里标 `compaction_boundary_reached=false`,**不调剧本去凑**(§9.2「缺数据不补造」)。

**Q6 · E2 的记录面**:闭集投影行只装数值与短码 —— `case_key` / `state_point` / `repeat` / `arm` / `optimization` / `call_wall_ms` / `call_attempts` / `response_chars` / `status` / `finish_reason` / `message_bytes_total` / `message_prefix_bytes`(同 case 同臂内相邻状态点之间不可比,恒 `None`,见下)/ 五块字符数 / `decision_action`(短码)/ `decision_sufficient`(bool)/ `assessment_rows` / `aspects_total` / `context_rebuilds` / `context_fallback` / `delta_blocks`。决定 JSON 全文与四臂各自的消息正文**只进 `.local/raw/`**,人工核对用。
`message_prefix_bytes` 在 E2 里恒 `None` 是刻意的:它量的是「上一轮 → 这一轮」的公共前缀,而 E2 每条 run 只在一轮上调真实模型,没有可比的上一轮 —— 与 `_measure_reflect_messages` 首轮的口径逐字相同(`reasoning_retrieval.py:546`)。**不能**拿同 case 相邻状态点之间的差充当它。

**Q7 · E2 不复用 `ab` 的测试库写路径**。E2 只跑检索、不合成、不落库,所以:沿用 `seed` 建好的 `<name>_test` 库、`--database-url` 强制 `_test` 后缀(挡住把主库 URL 抄过来)、`retrieval_run(event_log=None)`,并在跑前跑后各点一次**该库自己**的只读证据(照 `_readonly_counts` 的形状,但量的是测试库 —— E2 结构上就不该写它,所以这条断言是有意义的)。

**Q8 · `analyze` 的三处口子,都做成参数,默认值逐字保持今天的行为**:

* `--baseline-arm`(默认 `off`)取代 `OPTIMIZATION_BASELINE` 的硬编码 ⇒ D↔L 可直接配对;
* `--pair-rows`(默认关)额外出一张**逐题配对差值表**:同 `PAIR_DIMENSIONS` 格内先对 `repeat` 取中位数,再出 `Δ` 与 `ratio`,分位数照旧受 `--min-samples` 约束,并逐格标 `n_pairs`;
* `--key-set {t0,ab}`(默认 `t0`)让 `load_rows` 认 `AB_PROJECTION_KEYS`。**不改默认** —— 「多出来的键必须让人看见」这条纪律是 `load_rows` docstring 的原话,把它改成自动放行等于取消它;显式选键集则是操作者的知情声明。

**Q9 · manifest 落点与形状**:`<out-dir>/manifest.json`,三条通道共用;纯构造函数在 `backend/app/eval/reflect_manifest.py`(闭集键 + 隐私断言 + 零 I/O),rig 只做 `git rev-parse` 与写文件。键集:`code_sha` / `channel`(e1|e2|e3)/ `arms` / `optimization_by_arm` / `common_baseline`(= PR-1 公共基线,固定短码)/ `corpus_signature_by_cell` / `intent_contract_digest_by_question` / `model_contract` / `seed` / `arm_order_seed`(E3 恒 `null` + 一句说明,见 M4)/ `order`(枚举顺序的声明)/ `matrix`(题/档/臂/轮/状态点各自的计数)/ `budgets`(单次超时、重试预算、整批墙钟预算、整批截止时长)/ `started_at` / `finished_at` / `stopped_by_budget`(bool)。隐私断言:值只许是闭集短码、数值、bool、`null`、以及它们的列表/以短码为键的字典 —— 与投影同一把尺子;**不含**生产凭据/URL(§8.2)、不含题面原文、不含标记值以外的任何文本。

**Q9 评审后修正(T-EX11 回填,2026-09-11)**:实施与两轮评审把上面这份 16 键闭集扩到
**18 键**——`+case_set_digest`(E2 必填:12 例 case JSON 的摘要短码)、
`+sample_digest`(E1 必填:上下文样本的摘要短码,长度进 `matrix.tiers`)。`matrix`
子键契约收进 `reflect_manifest.py` 模块常量
`REQUIRED_MATRIX_KEYS_BY_CHANNEL`(评审后再收紧为**维度基数 int** 的形状):
`e1={tiers,blocks,arms,calls_per_series}`、`e2={cases,state_points,arms,repeats}`、
`e3={questions,cells,efforts,arms,repeats}`(`questions` 是本批真实题数、
`repeats` 写 rounds;`--round` 时为 1,`round_index` 另记);`matrix` 必须是非空
`Mapping`;
三条通道各自的 `matrix` 之外还可以带额外 int 子键(`planned_runs` 三通道通用、
E1 的 `marker_variant`),闭集不因此改动。新增**全通道共同必填表**
`REQUIRED_KEYS_ALL_CHANNELS = {code_sha, started_at, finished_at,
stopped_by_budget}`——本期 E1/E2 不实施掐停逻辑,`stopped_by_budget` 恒写
`False`,键本身仍必须在场。落点见 `backend/app/eval/reflect_manifest.py` 的
`MANIFEST_KEYS`/`REQUIRED_KEYS_ALL_CHANNELS`/`REQUIRED_MATRIX_KEYS_BY_CHANNEL`
三个模块常量。三条通道 rig 侧的写点统一为 `manifest.json`(最新,走
`Runner.write` 覆盖)+ `manifests.jsonl`(历史,追加)——这条规则本期由
T-EX4/T-EX8 落地(E1/E3),`state-probe`(E2)由 T-EX7 落地——三条通道
共用同一个 `_write_manifest` 函数(`scripts/reflect_shadow_rig.py`),不留
第二份实现。

**Q10 · 不改 `docs/operations*.md`**。§12 写的是「必要时更新 operations 配对文档」,而那两份文档里今天一句 reflect / rig 都没有(实测 grep 只命中无关的「前缀」字样),E1/E2/E3 是评测工具而不是生产运维动作。落点全部收敛到 `scripts/README.md`。这是一条**如实收窄**,写进 §4。

## §3 任务拆分

命名 `T-EX*`。每格标注:落点 / 要点 / 验收 / 用例 / 依赖 / 大概行数 / 模型选择。

### T-EX1 `reflect_manifest.py`:manifest 纯构造 + 闭集 + 隐私断言 · sonnet · ~130 行

**落点** 新建 `backend/app/eval/reflect_manifest.py`;用例进新建 `backend/tests/test_reflect_manifest.py`。

**要点** 一个 `build_manifest(**facts) -> dict` + `MANIFEST_KEYS` 闭集 + `assert_manifest_closed(row)` + `assert_manifest_values(row)`(复用 `app.domain.reasoning_trace_stats.assert_projection_values` 的短码形状判据,不重写一份)。零 I/O、零 `git`、零 `Settings` —— code SHA / 时间 / 语料签名全部由调用方传进来。三条通道各自的必填键用一个 `channel → required keys` 表声明:E1 必须有 `seed` 与三个长度档,E2 必须有 `case_set_digest` 与状态点表,E3 必须有 `intent_contract_digest_by_question` 与 `arm_order_seed`(允许 `null`,但键必须在)。

**验收** 缺必填键 ⇒ 抛;多一个闭集外的键 ⇒ 抛;值里塞一段题面原文/一个 URL/一个 `postgresql://` ⇒ 抛。

**用例** (a) 三条通道各一份最小合法 manifest;(b) 每条通道各缺一个必填键 ⇒ 逐格参数化报错;(c) 隐私变异:往 `budgets` 里塞一个 `"postgresql://user:pw@host/db"` ⇒ 红;(d) `arm_order_seed=None` 合法但键不可缺(挡住「哪天顺手把它删了」)。

**依赖** 无。**可与 T-EX2 / T-EX6 / T-EX9 完全并行。**

**实施记录(2026-09-11)**:两轮评审把闭集从要点里写的 16 键扩到 **18 键**
(`+case_set_digest`、`+sample_digest`),`matrix` 子键契约收进本模块常量
`REQUIRED_MATRIX_KEYS_BY_CHANNEL`(见 Q9 评审后修正)并改成**维度基数 int**
的形状;新增全通道共同必填表 `REQUIRED_KEYS_ALL_CHANNELS`
(`code_sha`/`started_at`/`finished_at`/`stopped_by_budget`);隐私变异 (c) 组
加「污染点在字典键」一格(`{"budgets": {"题面原文": 1}}` 同样被拒);
命名红线正则同时扫子键表;导出 `assert_manifest(row)` 作为全量入口(内部
`deepcopy` 后分别核闭集/必填/隐私三件事)。docstring 显式登记两处已知限制、
不当成缺口:(1) 值形状闸只挡自由文本/URL/连接串,**不挡**短码字符集内的
`sk-…` 形态凭据或裸 `host:port`——真正挡住凭据落地的是 18 键闭集里没有一个
键是给凭据用的;(2) 标量分支复用 `bool`/`int`/`float` 的 `isinstance` 判据,
`float("nan")`/`float("inf")` 会被放行,与兄弟模块 `_is_numeric_leaf` 共担
同一把尺子。私有导入 `_is_short_code`(而非公开的
`assert_projection_values`)维持——同一个短码判据,仓库内已有同形先例
(`app/services/source_embedding.py`)。

### T-EX2 `llm.py` 的 E1 接缝 · **opus** · ~55 行生产码 + ~120 行用例

**落点** `backend/app/core/llm.py:217-256`(`provider_messages` 加第三参数)、`:640` 附近(`chat_json` 读一次 ContextVar)、模块顶部(ContextVar + 上下文管理器);用例进 `backend/tests/test_llm_client.py`(`:169-330` 已有 `provider_messages` / `serialize_provider_messages` 的用例族,接在后面)。

**要点** 见 Q1。三件事必须同时成立:(1) `markers=None` 时逐字节回到今天;(2) 头标记在 provider-facing 序列的**第 0 条**,尾标记在最后一条 content 的**尾部**;(3) 调用方的消息映射不被 mutate(现有 docstring 的承诺)。上下文管理器负责 set/reset,异常路径也 reset。

**验收** (a) 生产路径零字节变化;(b) 开着接缝时 `.create()` 收到的 messages == `provider_messages(msgs, hint, markers=...)`(照抄 `:205-218` 那条「what goes on the wire IS provider_messages' output」的形状);(c) 两个标记都进 `llm_key` 的输入(所以 disturbed 臂不会被本地缓存悄悄服务);(d) 生产代码里没有第二个 set 点、`app/services/` 与 `app/application/` 不 import 它。

**用例** (a) `markers=None` 等价;(b) 头标记位置 = index 0 且在 wrapper 之前;(c) 尾标记追加而不替换、原映射未变;(d) 上下文管理器抛异常后 ContextVar 已复位;(e) AST 判据:唯一 set 点;(f) 负向断言:业务层不 import;(g) 两臂标记等长等数的构造用例(纯字符串层,不发请求)。

**为什么 opus** 这是本期唯一动生产传输路径的一格,「零字节变化」的判据要自己想清楚(等价性、cache key、mutate、异步调度下的 ContextVar 传播)。

**依赖** 无。

**实施记录(2026-09-11)**:评审后修正三处。F1(负向断言的覆盖面)——AST 判据
除了 `_EXPERIMENT_MARKERS.set(` 这个 Name 形写法,还认 Attribute 形
(`x._EXPERIMENT_MARKERS.set(`)与导入别名;负向扫描从最初的
`app/services/`/`app/application/` 扩到 `backend/app/**`,只豁免
`app/core/llm.py` 自身与 `app/eval/`(只放纯函数,arming 只在 `scripts/`)。
F2——生产路径上的 `_measure_reflect_messages` 用两参数 `provider_messages`
重建消息,接缝开着时那次重建**保持无标记**(纯函数不动):`ctx_bytes_total`/
`message_prefix_bytes` 两个 reflect 测量列因此对标记接缝**盲**,与实际发出去
的字节不同;这是刻意拍板(见 T-EX11 已知限制),不是漏改。F3——两臂标记
「等长等数」的判据半边(纯字符串层,不发请求)划给 T-EX3 的
`build_marker_pair` 承担,本任务只钉「同一次调用两个标记都进 `llm_key`
输入」。另加两条防御:空字符串标记、非 `str` 标记在
`experiment_message_markers` 里响亮拒绝(用例各一格);`_EXPERIMENT_MARKERS`
的接缝注释限定在「经 `copy_context`/`background_jobs.submit` 派发的路径」,
`gap_consult` 私有线程是已登记的例外(不经这条派发路径,接缝在那条线程上打
不开,登记进 E1 已知限制而非漏洞)。`_PRE_SEAM_BASELINE_SHA` 并列比对用例在
合入集成分支后不可达,长期 skip,由两条不依赖 git 历史的等价用例承担 CI
判据。

### T-EX3 E1 纯计划与纯分析 · sonnet · ~200 行

**落点** 新建 `backend/app/eval/reflect_prefix_probe.py`;fixture `backend/app/eval/reflect_t0/prefix_probe_sample.json`;用例进新建 `backend/tests/test_reflect_prefix_probe.py`。

**要点** 五个纯函数,零 I/O、零模型:

1. `build_marker_pair(seed, block_index, arm, call_index) -> (head, tail)` —— 定宽无语义短码;两臂标记值**不相同**(§9.1「减少彼此污染」);每个序列换新标识;字符/字节长度两臂逐字相等(有本地 tokenizer 才附 token 长度,没有就只声明字符/字节匹配,并留一个「换标记值重复验证」的开关)。
2. `probe_plan(tiers, blocks, arms, calls_per_series, seed) -> list[dict]` —— 3 档 × 4 区组 × 2 臂 × 4 次 = 96 格;**区组内**臂序按种子平衡随机(stable→disturbed 与相反各半);序列内部**连续**、不交错、不并发。dry-run 与真跑读同一份计划(照 `ab_arms` / `ab_plan` 的既有纪律)。
3. `render_sample(tier, sample) -> list[dict]` —— 三个长度档各一份固定的五块上下文 + 固定小 JSON 输出任务(`{"ok":true}`)与固定 schema hint;两臂**同一份**正文。
4. `probe_row_keys` / `assert_probe_row_closed(row)` —— 闭集行;**命名红线守卫**:一条用例断言 `probe_row_keys` 里没有任何键名匹配 `cache_hit|hit_rate|命中`(照抄 `reflect_context_bench` 的纪律,但这次要有一条**主动**的用例,不只是 docstring)。
5. `summarize_probe(rows) -> dict` —— 主统计:配对区组内两臂**重复观测**(每序列第 2-4 次)的中位墙钟差与比值,外加「后续相对首次的变化」;每序列第 1 次单独标 `first_observation`(§9.1:「不是已验证冷缓存」);失败/格式不符**单列**;`cached_tokens` 若在只作附录计数;结论词面限定在「有 / 无可辨认 / 不确定的时间收益」三格,**不产出**任何比率型结论。

**验收** 96/48 两个规模的计划都平衡且可复现(同 seed 逐格相同);两臂标记等长等数;摘要在「一半调用失败」的输入下仍出表且失败单列;`cached_tokens` 全缺时不出 0。

**用例** (a) 计划平衡性(每档每区组两臂各恰 4 次、臂序两种各半);(b) 同 seed 可复现、不同 seed 不同;(c) 标记等长等数 + 两臂标记值不相交;(d) 闭集 + 命名红线;(e) 摘要:首次 vs 重复分开、失败单列、`cached_tokens` 缺失不折 0;(f) 结论词面只能取三格之一(变异:让它输出一个百分比 ⇒ 红)。

**依赖** T-EX1(manifest 的 E1 必填键)、T-EX2(标记构造的等价面)。

**实施记录(2026-09-11)**:评审拍板序列身份 = `(tier, block_index, arm)`,
`build_marker_pair` 签名回填为
`build_marker_pair(seed, tier, block_index, arm, call_index, *,
marker_variant=0)`(要点 1 的原始签名缺 `tier`,评审后补上)。行闭集加
`gap_ms`(与上一次调用的间隔,首格 `None`,由 T-EX4 写值)。摘要新增
`repeat_observation` 逐臂中位数,与 `first_observation` 对称报告(design
§9.1「首次观测不是已验证冷缓存」)。命名红线正则扩到
`cache_hit|hit_rate|命中|percent|_pct`,同时递归扫真实 `summarize_probe`
输出的顶层键(不只扫 `probe_row_keys` 这张静态闭集)。`tier_chars` 只写计划
里实跑到的那几个档位,不把样本声明的全部档位都塞进 manifest。阈值来源如实
标注:`_verdict` 的判据是**实现自定阈值**,design 对具体数字沉默,不冒充原文
——阈值本身写进 `scripts/README.md`(见 T-EX11)。质量评审二轮另修:
`render_sample` 地板不足时 `ValueError`;`probe_manifest_facts` 的
`arms`/`calls_per_series` 从 `plan` 推算,参数保留为交叉校验(不一致时
`ValueError`,签名不变,T-EX4 已按现签名调用)。

### T-EX4 E1 rig 子命令 `prefix-probe` · sonnet · ~140 行

**落点** `scripts/reflect_shadow_rig.py`(新增 `cmd_prefix_probe` + `build_parser` 的 command 闭集加一格 + 三四个 `--probe-*` 参数);用例进 `backend/tests/test_reflect_t0_scripts.py`。

**要点** **薄适配**:构造 `Settings` → `RuntimeModelProvider(settings, EventLogger(...))`(照 `backend/app/eval/mrl_truncation.py:214-227`)→ `.chat("reasoning_agent")` → 按 `probe_plan` 逐格调用(`bypass_cache=True` + `call_stats` sink + 固定 `max_tokens`/`thinking` 不动)→ 每格一行 `probe-<arm>.jsonl` → 收尾写 `manifest.json` + `probe-summary.{md,json}` → `.close()` provider。复用 `_rig_process_env` 的日志隔离(`LLM_LOG_PATH` / `EVENT_LOG_DIR` 圈进 out-dir)与 `_rig_call_rows` 出一份 per-call 表。**不连数据库**:`--database-url` 在这条路上不读,显式给了要响亮拒绝(挡住「抄了 ab 的命令行」)。

dry-run 必须打印:逻辑调用数(96/48)、按重试预算算的**请求上界**、单次超时、整批墙钟预算、序列顺序与区组、预热单列、产物落点、以及一句「这批只能支持『稳定前缀的时间收益』,不得报告命中率」。

**验收** dry-run 在没有模型配置、没有 PG 的机器上跑得通(`Runner` 的既有契约);真跑路径上标记接缝**只在这条命令里**打开(退出时必然复位)。

**用例** (a) dry-run 打印两个数与产物落点(逐字钉住 96 / 上界 / 48 三个数,照 T-PL6 修正轮把 `≤106`/`≤212` 钉死的做法);(b) 给了 `--database-url` ⇒ 退 2;(c) 行闭集守卫(往行里加一个 `prompt` 键 ⇒ 红);(d) 用一个进程内假客户端跑通 6 格的缩批,断言标记的头尾位置与两臂剧本;(e) 接缝在命令退出后已复位。

**依赖** T-EX2、T-EX3。

**实施记录(2026-09-11)**:`finish_reason` 空串折 `None`(非短码形状同样折
`None`,行 docstring 写明「provider 没说」)。整批收尾(含预算到点/异常)统一
走 `try/finally`:`provider.close()` 与已打的行落盘/摘要/manifest 写作
salvage,`except BaseException` 也要落盘再重新抛出。删掉临时的
`PREFIX_PROBE_TIMEOUT_SECONDS` 常量,改用 T-EX8 已经镜像好的
`REASONING_TIMEOUT_SECONDS_DEFAULT=90`(与 `ab` 的单次超时读同一个常量);
`_rig_git_sha` 与 `_ab_git_sha` 二选一,取 T-EX8 带 `-dirty` 后缀的那份并在
本任务复用。`gap_ms` 口径钉死为「本格**发起** − 同序列上一格**返回**」
(真实空闲),首格与跨序列一律 `None`。全局 `--max-wall-minutes` 的 help
文案合成一条,同时描述 `ab` 与 `prefix-probe`(不是两条重复参数)。
`--marker-variant`(`type=int, default=0`)落地,`matrix.marker_variant` 作为
额外 int 子键随 manifest 写出。dry-run 八项(target/seed/scale/model
calls/timeout/batch wall-clock budget/sample/marker
variant/warmup/sequence/out/isolated logs/conclusion scope)逐项钉死,两个
规模的数字精确为 `97 次逻辑调用(96 格计划 + 1 预热);请求上界 ≤ 194` 与
`49 次逻辑调用(48 格计划 + 1 预热);请求上界 ≤ 98`。per-call 表
(`calls-e1.jsonl`)的对齐规则写进 `cmd_prefix_probe` 的 docstring(见
T-EX11 的 `scripts/README.md` 转述)。预热格若命中本地缓存单独计数、也让
整批非零退出(接受,更严格);真跑显式传
`timeout=settings.reasoning_timeout_seconds,
max_retries=settings.reasoning_max_retries`;预算过期时预热本身也不打
(比“预热不受预算约束”更严格,接受)。`_write_manifest`/`_wall_budget_problem`
收敛为三通道共用的 rig 私有函数,`ab` 的收尾几行改调它们。

### T-EX5 E2 驱动器与代理 · **opus** · ~230 行

**落点** 新建 `backend/app/eval/reflect_state_probe.py`;用例进新建 `backend/tests/test_reflect_state_probe.py`。

**要点** 四个部件:

1. `_ScriptedReflectDriver` —— duck-typed chat 客户端(`configured = True` + `chat_json(messages, schema_hint, **kwargs)`)。按剧本返回第 1..k 轮的决定;第 k+1 轮把**收到的这两条消息与这个 schema_hint 原样转发**给真客户端调一次(带 `call_stats` sink),记录返回的原始 JSON 与读数,然后返回一条脚本化停止决定;第 k+2 轮起不应到达(到达就是剧本没写对 ⇒ 响亮失败,不静默兜底)。按 `"sub_queries" in schema_hint` 区分 plan 与 reflect(与 `_SeqLLM` 同一判据),而 E2 的 plan 分支**结构上不该被触达**(有冻结意图 ⇒ 不调 plan),触达即失败。
2. `_ProbeModelClients` —— `__getattr__` 委托到真 repo,只覆写 `chat(workload_id)`:`reasoning_agent` 给驱动器,其余(embedding / rerank / 合成)原样透传。这是 E2 与服务端之间**唯一**的接触面。
3. `load_state_probe_cases(raw)` —— case schema 的加载与校验器,兑现 Q5 的四条约束(无 id、必须带冻结契约、settings 覆盖限白名单、状态点轮号单调且落在剧本长度内),畸形当场抛。
4. `probe_row_keys` / `assert_probe_row_closed` / `summarize_state_probe(rows)` —— 闭集行(Q6)+ 分组摘要,**按初始状态 / 后续状态 / 压缩边界三档分别报告**(§9.2 原文),并把 `compaction_boundary_reached=false` 的格子单列。

**验收** 模型选出的动作**一次都不执行**:整条 run 的 trace 里第 k+1 轮之后没有任何动作步,库里没有新行,`.local` 之外没有决定正文。四臂重放同一剧本时前 k 轮的**动作序列逐格相同**(否则「同一状态点」这句话不成立)。

**用例**(全部用进程内 fake,零真实模型)
(a) 剧本推进到第 k 轮后转发恰一次、`call_stats` 被读到;
(b) **不执行**:模型返回一个 `search_elements` 决定,断言 trace 里没有对应动作步、`attempted` 未增;
(c) 四臂同剧本 ⇒ 前 k 轮动作序列相同、而第 k+1 轮的 system/user 分块**不同**(P/D/L 各自的形状);
(d) 代理只截 `reasoning_agent`:embedding 仍走真 provider(用一个计数替身证明);
(e) 第 k+2 轮被触达 ⇒ 响亮失败,不兜底;
(f) case 校验器逐条拒绝:带 id 的动作参数、缺契约、settings 覆盖越白名单、状态点越界;
(g) 闭集 + 隐私(往行里加 `decision_reason` 原文 ⇒ 红);
(h) `message_prefix_bytes` 在 E2 行上恒 `None`(变异:改成拿相邻状态点做差 ⇒ 红)。

**为什么 opus** 「不执行」与「四臂同一状态点」这两条是 E2 的全部价值所在,而它们的判据都在剧本与代理的交界处;写歪了会跑出一批看起来完全正常、其实四臂状态点不同的数据。

**依赖** T-EX1。

**实施记录(2026-09-11)**:`StateProbeError` 继承 `BaseException`(不是
`Exception`)保留——否则 `_reflect_v2_attempt` 的 fail-open 会把「站错状态
点」洗成假兜底;rig 的批处理 `except Exception` 因此**抓不到**它,要按格
容错必须显式 `except StateProbeError`,默认语义是停批。`settings_overrides`
在 `run_state_probe_point` 里套到 `settings_for_arm` 的副本上(建 retriever
之前),覆盖值判据要求 int 且非 bool、`≥1`。`state_point` 行键是**序号**
0/1/2,不是轮号——三档报告按序号分组(轮号在 12 例之间不可比),轮号用
`case.state_point_turn(i)` 单独取。三层 scope
(`model_work_scope`/`retrieval_run(event_log=None)`/`source_scope_context`)
与臂对号 `assert_optimization_matches_evidence` 都已在 `run_state_probe_point`
内部,rig(T-EX7)不必再包一层。`matrix` 四个基数(`cases`/`state_points`/
`arms`/`repeats`)由 `state_probe_manifest_facts` 算好,rig 只补
`code_sha`/时间/语料签名/`model_contract`/`budgets`。`assessment_rows` 读的
是**模型那份决定**(`ForwardedTurn.raw`),不是驱动器自己发的停止决定那一轮
trace(后者恒 1 行)。`n_decision_unreadable` 拆成
`n_decision_absent`/`n_decision_unreadable_action` 两格(修正轮二)。
`FORBIDDEN_VALUE_FRAGMENTS` 的 id 前缀判据加词首锚,`case_key` 过短码闸,
override 值非 int/`<1` 一律拒绝。**登记的一处重复,T-EX7 已收敛**:
`prepare_frozen_intent` 与 rig 的 `_prepare_search_intent` 曾是同一份逻辑
两份实现;T-EX7 落地时让 rig 的 `_prepare_search_intent` 改调前者
(`contract=None` 那半留在 rig 侧),不再各自维护一份。

### T-EX6 E2 的 12 例 case 集 · **opus** · ~300 行 JSON + ~60 行对账用例

**落点** 新建 `backend/app/eval/reflect_t0/state_probes.json`;对账用例进 `backend/tests/test_reflect_state_probe.py`。

**要点** 12 例覆盖设计 §9.2 点名的八种形态,按 Q5 收窄后的口径落表:

| # | 形态 | 承载方式 | 语料格 |
| --- | --- | --- | --- |
| 1-2 | 单事实 | 单跳查询 + 2 轮剧本 | A_nokg / B_kg |
| 3-4 | 复杂条件 | 冻结契约带 3 个 `mandatory_topics` + `constraints` | A_nokg / B_kg |
| 5 | 有图 | 图在范围内(格承载,非图动作) | B_kg |
| 6 | 无图 | 单篇无图 | A_nokg |
| 7 | 目录 | `enumerate_elements`(小集合) | B_kg |
| 8 | 大集合 | `enumerate_kg_objects` + `scope=all`(触 PR-1 的规模守卫) | B_kg |
| 9 | 失败 | 必然零命中的查询 | A_nokg |
| 10 | 重复 | 同一查询连着两轮 | B_kg |
| 11-12 | 工具耗尽 | `reasoning_max_element_searches=1` + 剧本硬选它 | A_nokg / B_kg |

每例:`case_key` / `corpus_cell` / `question`(公开语料上的公开题面)/ 冻结 `intent_contract` / `script`(逐轮决定)/ `state_points`(三个轮号)/ 可选 `settings_overrides`。题面与语料**沿用 `reflect_t0/questions.json` 的 34 题**里合适的那些,不另起一套(§11「复用现有 A/B 闭集投影与题集」)。

**验收** 12 例全部过 `load_state_probe_cases`;八种形态**逐格有对账用例**(一条参数化用例断言每种形态至少一例、且那一例的剧本里真的有对应动作/覆盖);无一条含数据库 id。

**用例** (a) 形态覆盖对账(照 A/B §3.4「§9.2 点名形态的覆盖对账」的形状);(b) 12 例逐例过校验器;(c) 剧本长度 ≥ 最大状态点轮号 + 1;(d) 与 `questions.json` 的题号交叉引用成立。

**依赖** T-EX5 的 case schema(可先按 schema 草案并行写,汇合时对齐一次)。**可与 T-EX1/T-EX2/T-EX9 并行。**

**实施记录(2026-09-11)**:「八种形态」回填为**九种**
(`probe_shape` 闭集:`single_fact`/`complex_condition`/`graph_in_scope`/
`no_graph`/`roster`/`large_collection`/`zero_hit`/`repeat_request`/
`tool_exhausted`,原表格把「单事实」「复杂条件」「工具耗尽」各算一种、实际
按承载方式数出九种独立形态)。原「单事实 2 轮剧本」回填为**≥4 轮**
——三个状态点要求严格递增且第三点尽量落在压缩边界之后,2 轮剧本放不下三个
状态点。case schema 在 Q5 基础上多两个键:`question_key`(与
`reflect_t0/questions.json` 交叉引用)、`probe_shape`(形态对账用),闭集
`REQUIRED_CASE_KEYS`/`OPTIONAL_CASE_KEYS` 落在 T-EX5 的加载器里(顶层键闭集
因此归 T-EX5,不在本模块重复声明)。零命中形态用统一哨兵前缀
`absent_probe.`(`ZERO_HIT_SENTINEL` 常量)过 `exact_lookup` 的 `term` 参数
——过短码闸又保证真实为零命中。质量评审二轮再修两处:「满额度耗尽」形态
改为**生产默认额度 + 逐例 `settings_overrides` 跨轮消耗**(逐例断言期望的
不可用集合非空,如 `te-a-benchmarks` → `{2,4}`、`te-b-kivi-bits` → `{2,5}`);
两条复杂条件例的 `mandatory_topics[].question` 改词避开
`query_intent._UNRESOLVED_REFERENCE`(原文含「这个」会被折回 objective)。
题面与语料沿用 `questions.json` 的 34 题,不另起一套。

### T-EX7 E2 rig 子命令 `state-probe` · sonnet · ~150 行

**落点** `scripts/reflect_shadow_rig.py`(`cmd_state_probe` + command 闭集 + 参数);用例进 `backend/tests/test_reflect_t0_scripts.py`。

**要点** 薄适配:`_rig_process_env`(指测试库)→ `_settings_by_arm(arms)`(**直接复用**,含它那三条回读断言;E2 与 `ab` 一样要两臂/多臂都开 `REASONING_REFLECT_MEASURE_CONTEXT`)→ 每臂一个 repo(`_search_repository`)→ 每臂一个 `_ProbeModelClients` 代理 → 按 `(case, state_point, repeat, arm)` 枚举串行跑(**并发恒 1**:E2 的每条 run 只有一次真实调用,并发只会污染排队时长)→ 每格一行 `state-probe-<arm>.jsonl` + `.local/raw/` 一份决定存档 → manifest + 摘要。

前置硬断言:`--database-url` 必须 `_test` 后缀且显式给出;跑前跑后各点一次该库的只读证据;`assert_optimization_matches_evidence(声明, probe.reflect_optimization())` 逐臂当场对号(复用 `reflect_ab` 的既有函数,与 `run_ab_once:3963` 同一处纪律)。

dry-run 打印:逻辑 chat 调用数(216 / 三臂;`--arms` 加 L 则 288;`--limit 6` 缩批 108)、**另有 N 次 embedding 调用**单列一行、请求上界、状态点表、产物落点、以及一句「模型选出的动作只记录不执行,这批不是自洽真实轨迹」。

**验收** dry-run 无库无模型可跑;真跑收尾时测试库只读证据成立;缩批 `--limit`/`--only-case` 先筛后限(沿用 `search` 的既有口径)。

**用例** (a) dry-run 三个规模的数字逐字钉死;(b) 非 `_test` 库 ⇒ 退 2;(c) 臂对号断言在 settings 被盖住时报错;(d) 用进程内 fake 跑通 2 例 × 1 状态点 × 3 臂的缩批并断言产物形状;(e) `--concurrency 2` ⇒ 响亮拒绝(不是降级)。

**依赖** T-EX5、T-EX6、T-EX1。

**实施记录(2026-09-11,T-EX11b 回填)**:已完成实现与两轮评审修正并合入
集成分支。与计划字面的取舍点:

* **失败行 `status` 取自 E2 自己的词表**(`PROBE_FAILED_STATUSES=
  {"cancelled","error"}`,来源是 `app/core/llm.py` 的调用出口),不是 `ab`
  的 `JOB_STATUSES`——写 `ab` 的 `"failed"` 会让 `summarize_state_probe`
  一格都数不到它,退出码与摘要各说一套(spec F1)。词表漂移在**导入期**
  就红(`assert STATE_PROBE_FAILED_ROW_STATUS in PROBE_FAILED_STATUSES`)。
* **裸 `ValueError`/`TypeError` 与 `StateProbeError` 同等对待,一律停批**
  (spec F2):这两类同样是「剧本没写对」(键改名、参数形状不对),不是
  「这次模型调用失败了」——按格隔离继续跑只会让余下两百多格照跑几个小时,
  产出一批某臂某状态点缺格的不平衡数据。
* **embedding 上界含首轮种子检索**(spec F3):`run()` 拿到非空
  `intent_queries` 先跑一轮零模型调用的种子检索,种子数从冻结契约
  (`prepare_frozen_intent`)确定性算出,这一轮与状态点无关、每格都要重跑,
  漏计它会在批跑到一半时 embedding 配额耗尽。
* **补齐 per-call 表 `calls-<arm>.jsonl`**(spec F4):串行,每格窗口按
  `_rig_call_rows` + 臂标签写;`.local/raw` 补 `forwarded.stats` 与
  `scripted_turns`。
* **`prepare_frozen_intent` 重复已收敛**(spec F5):rig 的
  `_prepare_search_intent` 在 `contract=None` 早退后改调
  `app.eval.reflect_state_probe.prepare_frozen_intent`,不再各自维护一份
  逻辑。
* **`--arms`/`--only-case`/`--limit` 与只读收尾复用既有函数**(spec F6–F8):
  `_assert_readonly_on_exit` 按 `command="state-probe"` 说库;删掉 rig 侧
  `matrix.planned_runs` 的死写(由 `state_probe_manifest_facts` 自己算)。
* **`--concurrency`/`--repeats` 改成 `default=None` 哨兵**(spec/quality
  合并 F8-5 / P2-2):`main()` 里 `None` 时才填旧默认(1/`ab` 的 3,E2 另有
  自己的默认 2),显式性靠这个哨兵而不是扫 `sys.argv`——后者会被
  `--repeat 5` 这种无歧义前缀缩写骗过,用户要 5 会静默拿到默认值。
* **臂序按重复轮号奇偶交替**(quality P3-5,**取代**计划原文「臂序固定不
  随机」):固定臂序会把块内的单调漂移(预热、限流退避、连接池升温)整份
  压在最后一条臂上,交替是确定性的(没有随机、没有种子,`arm_order_seed`
  仍如实写 `None`),manifest 的 `order` 从真实计划里读出来、带
  `alt_by_repeat_parity`/`alt_unobserved` 后缀,是对设计 §9.2「随机化」的
  实现口径收窄,不是它的等价物。
* **`model_contract` 统一为裸短码**(与 E3/`ab` 同一口径,quality 拍板):
  `state_probe_manifest_facts` 签名不变(仍接受一个 Mapping 用于计算),
  rig 写 manifest 前把返回值里的 `model_contract` 覆盖成裸短码。
* **`_test` 判据按 URL path 段(去扩展名)判库名**(quality P3-6),与 `ab`
  的 `AB_TEST_DB_SUFFIX.endswith` 分叉——`ab` 刻意不动,分叉登记为已知
  限制,不是漏洞。
* **`--env-file` 必须早于 `load_state_probe_case_set()` 生效**
  (ex7 自己发现并修复):后者要 `from app.core.config import Settings`,
  而该模块在 import 时就把 `SILICON_NOTEBOOK_ENV_FILE` 读成模块级常量,
  晚一步会让 `--env-file` 静默失效。

`state-probe` 子命令的完整行为细节(前置断言、默认臂/重复次数、dry-run
数字、产物、行闭集、失败语义、已知限制)见 `scripts/README.md` 的
`state-probe` 小节;`fangan_todo.md` (m) 对应条目已回填为已落地状态。

### T-EX8 E3:整批墙钟预算 + manifest 接进 `ab` · sonnet · ~110 行

**落点** `scripts/reflect_shadow_rig.py:4412-4500`(`_ab_run_batch` 加 deadline)、`:3268-3360`(`cmd_ab` 的 dry-run 多打三行)、`:4079-4200`(`_run_ab` 收尾写 manifest);用例进 `backend/tests/test_reflect_t0_scripts.py`。

**要点** 三件,一件都不许多做:

1. `--max-wall-minutes`(默认不设 = 今天的行为)。到点后:**停止派发**(串行路在循环顶部判、并发路在 `worker` 入口判)、`cancel_event.set()` 唤醒在途、`shutdown(cancel_futures=True)`、把已经开跑但没跑完的单元按既有失败/取消路落行(于是 `status="cancelled"` = 删失观察),**不偷偷补跑到矩阵齐全**(§13 原文)。整批以「预算到点」为由非零退出。
2. `manifest.json`(T-EX1 的构造 + 一次 `git rev-parse HEAD`),`stopped_by_budget` 如实写。
3. dry-run 多打三行:整批墙钟预算、单次超时(`reasoning_timeout_seconds`)、以及「到点会停止派发并保留未完成/不成对标记」。

**验收** 不给 `--max-wall-minutes` 时行为与今天**逐字相同**;给了且到点时,已完成的前缀仍是一份配对完整的数据集(重复轮在最外层这条既有性质保住它);被掐掉的单元在数据集里**有行**(不凭空消失,与 `scope_unresolved` 两臂各落一行同一条口径)。

**用例** (a) 不给 ⇒ 路径等价;(b) 给一个已经过期的 deadline ⇒ 零派发、退出码非零、manifest 里 `stopped_by_budget=true`;(c) 中途到点 ⇒ 在途单元落 `cancelled` 行、队列里的单元不落行也不发调用;(d) manifest 键集与隐私守卫;(e) dry-run 三行文案。

**依赖** T-EX1。**与 T-EX3/T-EX5 并行。**

**实施记录(2026-09-11)**:用例落点是 `backend/tests/test_reflect_ab.py`
(计划要点原写 `test_reflect_t0_scripts.py`,以代码现状为准回填)。manifest
`budgets` 子键为实现者自定的四个:`reasoning_timeout_seconds`/
`reasoning_attempt_budget`/`max_wall_minutes`/`batch_deadline_seconds`;
`reasoning_attempt_budget` 复用既有 `REASONING_ATTEMPT_BUDGET` 常量,
`reasoning_timeout_seconds` 钉 `Settings().reasoning_timeout_seconds` 的默认值
而不是硬编码 90(用例不硬写 90s)。`_ab_loop` 改为返回三元组、
`_ab_failed_row` 收 `status=` 关键字参数,便于到点停批时按格标注
`status="cancelled"`。到点判据照搬既有
`_search_loop_concurrent` 的同锁复核模式,worker 入口另补一次 deadline 判据;
`abort()` 内的置位在锁内完成。`_ab_git_sha` 加 `-dirty` 后缀
(工作树不干净时),T-EX4 复用同一份函数(不再各写一份)。
`manifest.json`(最新)+ `manifests.jsonl`(历史)三通道同规则由本任务定稿,
T-EX4/T-EX7 照做。`planned_runs` 作为三条通道 manifest 的额外 int 子键
(E3 = `len(units) * len(arms)`)。质量评审修正 `matrix` 对账:E3 各格题集
**不相交**(`ask_plan` 按 corpus 过滤),因此 E3 总 run 数 =
`questions × efforts × arms × repeats`,`cells` 不是乘数——这一点已回填进
`reflect_manifest.py` 的 E3 注释。`code_sha` 记全 SHA(docstring 已改准);
`intent_contract_digest_by_question` 只取本批 units 实跑的题(与磁盘缓存
`_rows` 求交),不写续跑目录里没跑到的题(存疑项已拍板)。

### T-EX9 `analyze` 的三处读出口子 · **opus** · ~170 行

**落点** `scripts/analyze_reasoning_trace.py:170`(`OPTIMIZATION_BASELINE` → `--baseline-arm`)、`:360-411`(`optimization_pair_table` 收基线参数)、`:452-472`(`_pair_side` 旁边新增逐题配对)、`:173-195`(`load_rows` 收键集参数)、`:495-609`(`render_markdown` 加一节);用例进 `backend/tests/test_reflect_t0_scripts.py`(`:2605-2960` 已有 `optimization_pair_table` 的用例族与四条变异,接在后面)。

**要点** 见 Q8。第二件(逐题配对)是这一格的核心,三条口径不能松:

* **先对同题同档的重复取中位数,再比**(§10.1);
* **配对比值不是两组独立 P50 的比值** —— 用例要有一条变异钉住这件事(把实现改成 `median(cand)/median(base)` ⇒ 红);
* **超时/取消进删失一列,失败率单列,成功配对表可单出但不能独自决定**(§10.1)—— 表头逐格标 `n_pairs` / `n_censored` / `n_failed`,分位数受 `--min-samples` 约束并标样本数。

**验收** 默认参数下 `analyze` 的输出与今天**逐字节相同**(既有 104 个用例全绿即证);`--baseline-arm prefix_delta` 能在只有 D/L 两臂的一批上出配对表;`--key-set ab` 能直接吃 `ab-runs.jsonl`。

**用例** (a) 默认行为等价;(b) D↔L 配对表出得来、且基线侧如实报自己的 `optimization` 分布;(c) 逐题配对:重复先中位、再出 Δ 与 ratio;(d) 变异:换成两组独立 P50 的比值 ⇒ 红;(e) `cancelled` 落删失列而不是失败列;(f) `--key-set ab` 放行 A/B 键、`t0` 仍整批拒绝(既有纪律不变);(g) `n_pairs < --min-samples` 时不出分位数但仍出逐题差值。

**为什么 opus** §10.1 的每一句都是一条容易写成「看起来对」的陷阱(配对 vs 独立分位、删失 vs 失败、重复先汇总 vs 直接摊平),而这张表是最终采用决定的读处。

**依赖** 无(只碰 `analyze` 与它的用例)。**可与 T-EX1/T-EX2/T-EX6 并行。**

**实施记录(2026-09-11)**:`--key-set ab` 放行 A/B 专属列之后,那些列**不进**
四组标量指标的读出(登记已知缺口;要读需要一次「键集感知的指标元组」独立
改动,本期未做)。基线 SHA 并列比对用例在 rebase 合入集成分支后 skip,长期
由冻结常量与 golden fixture 守形状。`paired` 的读侧与写侧改成同一口径:
`paired is False` 的行**不入**配对格(t0 行没有这个键,行为不变),两侧各报
`n_unpaired` 进 `ROLLUP_SIDE_COUNTS`(`=("n_censored","n_failed",
"n_unfinished","n_unpaired")`)与逐格表,不只写进散文。rollup 旧键
`n_censored`/`n_failed` 改为按**基线/变体两侧**分别报(仅 `--pair-rows` 分支
生效),新增 `n_unfinished`(同样分基线/变体);成功判据收拢成常量
`SUCCESS_STATUS = "done"` 白名单。逐题配对表头带臂名 + `P50(n)`;
`ROLLUP_FACETS = ("effort", "corpus_cell")`;golden fixture 落在
`backend/tests/fixtures/reflect_t0_analysis_golden.*` 与
`reflect_t0_pair_rows_golden.*`(重签命令写在用例 docstring 里)。质量评审
二轮**动了一处默认路径**(有意,登记为预存缺陷修复):布尔维度 `False` 不再
折成 `unknown`(`pair_table` 与 `_optimization_cells` 两处),只在输入真带
`has_intent_contract=False` 的行时才改变 `pairs`/`optimization_pairs`,原有
golden 未受影响不需重签。`backend/app/eval/reflect_ab.py:98-100` 那段
「D↔L 差值不由 `optimization_pair_table` 直接给出」的过期 docstring 已随
本任务的 `--baseline-arm` 落地一并改写(见 T-EX11 对该 docstring 的修正)。

### T-EX10 验证清单与变异复核 · **opus** · 纯验证,零代码

**要点** 五处不可协商性质各做一次变异、确认报红、精确还原:

* (a) `provider_messages` 的 `markers=None` 分支改成「无条件插一条空标记消息」⇒ 生产字节等价用例红;
* (b) E1 摘要里加一列 `cache_hit_rate` ⇒ 命名红线用例红;
* (c) E2 驱动器把模型的决定**返回给 `run()`** ⇒「不执行」用例红;
* (d) E3 的预算到点改成「补跑到矩阵齐全」⇒ 预算用例红;
* (e) `analyze` 的配对比值改成两组独立 P50 的比值 ⇒ 变异 (d) 红。

另复核 §12 逐条(见 §4 的映射表),以及**三条通道的 dry-run 在无库无模型机器上全部可跑**这一条(E1/E2 新增,E3 既有)。

**依赖** T-EX2..T-EX9 全部。

**实施记录(2026-09-11,已合入 `135acf2de`)**:五处变异全部实测报红并精确还原
(a 5 例红/b-1 3 例红/c 26 例红/d 8 例红/e 2 例红),§12 十一条逐条落点核实,
§13 交付物表逐行核实(E1/E3 真源确认;E2 三格标「待 ex7」)。发现并**已修补**
一处守卫缺口:变异 (b) 只覆盖了 `summarize_probe` 返回**键**的命名红线
(`_BANNED_NAME_PATTERN` 递归扫描),没有覆盖 `probe-summary.md` **正文渲染层**
——`_render_probe_summary_markdown` 若单独在 md 里多写一行
`- cache_hit_rate: …`,原有判据全绿而不报红(风险点在报告散文,计划 §5
风险 4 已点名)。补测试(生产代码零改动)钉住 md 正文的命名红线扫描,变异复核
后确认真红。三条通道的 dry-run 在无库无模型机器上全部可跑(E1/E2 新增,
E3 既有)。

### T-EX11 T5 文档收官 · sonnet · 只改文档与注释

见 §4「文档落点」。

### 依赖与并行

```
T-EX1 ──┬─→ T-EX3 ─→ T-EX4 ──┐
T-EX2 ──┘                     │
T-EX1 ──┬─→ T-EX5 ──┬→ T-EX7 ─┼─→ T-EX10 ─→ T-EX11
T-EX6 ──┘           │         │
T-EX1 ────→ T-EX8 ──┴─────────┤
T-EX9 ────────────────────────┘
```

**可复用叶子并行 + 汇合**:第一波四格完全独立 —— T-EX1(manifest 纯构造)、T-EX2(`llm.py` 接缝)、T-EX6(12 例 case 数据)、T-EX9(`analyze` 三处)。第二波 T-EX3 / T-EX5 / T-EX8 各自只依赖第一波里的一两格。第三波 T-EX4 / T-EX7 是两条 rig 薄适配。T-EX10 汇合,T-EX11 收尾。

T-EX6 与 T-EX5 之间有一条**schema 契约**:先由 T-EX5 把 case schema 写成一份 docstring 草案(或由本计划的 Q5 直接充当),T-EX6 照它写数据,汇合时跑一次校验器对齐。这比串行省一整格。

### 刻意不做

不新增任何投影列(§10 的门全部用现有列读得出来,见 M5);不动 `RUN_PROJECTION_KEYS` / `AB_PROJECTION_KEYS` / `CALL_ROW_KEYS` / `JOB_STATUSES` 四个闭集;不改 `AB_DEFAULT_ARMS`(既有命令与产物路径一个字节不变);不给 `ab` 加第三条臂或放开「一次只收一对」;不补 T-AB1 的 gold、不实现 `ab-judge` / `ab-report`(见 U1);不实现「主库只读跑完整 Ask」的不落库分支(A/B §13-1 待用户);不给 E2 的剧本加 `expand_graph` / `follow_chain` / `ppr_retrieve`(需要候选池 id 的间接层,登记为债务);不给 `_run_ab_unit` 的臂序加种子(M4);不改 `docs/operations*.md`(Q10);不动 `AGENTS.md` / `CLAUDE.md` / `README{,_zh}.md` / 带日期的设计稿本体;不改 `_V2_ASSESSMENT_INSTRUCTION`(PR-4 的 Q3 独立待办,改它要重设三臂字节基线);热函数三格零改动。

## §4 验证清单与文档落点

### 映射设计 §12(全部十一条)

| §12 条目 | 落点 | 状态 |
| --- | --- | --- |
| 额度耗尽/方面状态变/轮数增长时 S、C 字节不变;已发出 D 不变 | PR-2/3/4 既有用例 | 继承,`check.sh` 复核 |
| provider-facing 消息覆盖 wrapper/schema;动态值没偷插稳定块开头 | T-EX2 (b)(c) + 既有 `_GatedV2LLM` 族 | E1 接缝新增覆盖 |
| P 与基线传达同一批证据事实/执行限制;D 的省略有披露 | PR-2/3 既有 | 继承 |
| 静态目录已耗尽工具不能执行;Knowhow/legacy 不受影响 | PR-2 既有 + T-EX6 的「工具耗尽」两例 | E2 新增真实决策侧证据 |
| 长历史触发预算前重建;硬预算守住;回退不增调用 | PR-3 既有 + T-EX6 第三状态点 | E2 新增「压缩边界」分档报告 |
| 已支撑方面撤销/冲突/摘录升级/非法键 | PR-1/3 既有 | 继承 |
| assessment 单项错误不吞合法动作;省略自评不追加调用 | PR-1/4 既有 | 继承 |
| **无 usage / 无 cached / 无 finish_reason 仍出表;缺失不变 0;重试与取消在计时中可见** | T-EX3 (e) + T-EX5 (g) + T-EX9 (e) | **本期核心** |
| **E1 两臂正文与输出任务一致,只有预定标记稳定性变化** | T-EX3 (c) + T-EX4 (d) | **本期核心** |
| **E2 不执行模型选出的工具** | T-EX5 (b) + T-EX10 (c) | **本期核心** |
| **E3 执行真实闭环** | 既有 `ab`(`run_ab_once` 调 `repo.ask_reasoning`)+ T-EX8 | 继承 + 预算 |
| 停机、枚举、引用、Report 后续方向的既有聚焦测试继续可达 | `bash scripts/check.sh` | |

### 映射设计 §13 交付物

| §13 交付物 | 落点 |
| --- | --- |
| 冻结 manifest | T-EX1 + 三条通道各自的 rig 收尾 |
| 无正文的逐调用表 | `calls-*.jsonl`(既有 `_rig_call_rows` + `join_calls`),E1/E2 复用 |
| 逐 run 表 | `probe-*.jsonl`(E1)/ `state-probe-*.jsonl`(E2)/ `ab-runs.jsonl`(E3,既有) |
| 逐题配对表 | T-EX9 的 `--pair-rows` |
| 质量盲审记录 | **不由代码交付**(U1 建议走人工盲抽);A/B §8.5 的盲表格式沿用 |
| 聚合报告 | `probe-summary.md`(E1)/ `state-probe-summary.md`(E2)/ `analyze` 的 `--out-md`(E3) |
| 异常清单 | 各通道摘要里的失败/删失/不成对单列 |
| 全阶段 dry-run(逻辑调用数、请求上界、单次超时、整批墙钟预算) | T-EX4 / T-EX7 / T-EX8 各一条用例逐字钉死 |
| 达预算停派发 + 保留未完成/不成对标记 | T-EX8 |
| 报告第一页只答四件事 | T-EX11 写进 `scripts/README.md` 的报告口径一节 |
| 默认 `off`、任一回归立即回 `off`、不做数据迁移 | 既有;T-EX11 复核文档无残留过期话术 |

### 用户拍板项(计划不代替,原样转述)

| # | 出处 | 待拍内容 |
| --- | --- | --- |
| U1 | 本计划 §2 | E3 的质量证据走 (a) 只筛选批 / (b) 人工盲审 / (c) 补 gold+judge。**建议 (b)** |
| U2 | 本计划 §2 | E2 三臂(216)还是四臂(288)。**建议默认三臂 + L 显式开关** |
| U4 | 本计划 §2 | E1 跑 96 还是先 48。**建议默认 96,`--smoke` 走 48** |
| A/B §13-1 | A/B 设计 | 要不要为「主库只读跑完整 Ask」付一次最小生产改动。规格建议不付 |
| A/B §13-2 | A/B 设计 | G1 全量 408 run 的预算;G2/G3 跑不跑 |
| A/B §13-3 | A/B 设计 | 判分模型选谁(若走 U1-(c)) |
| A/B §13-4 | A/B 设计 | 人工抽样谁做(U1-(b) 下这条变成前置) |
| A/B §13-5 | A/B 设计 | `gold_facts` 由谁写(若走 U1-(c)) |
| A/B §13-6 | A/B 设计 | 硬判据 6 的「中位数不得高出 1 轮以上」阈值 |

### 文档落点

* **`scripts/README.md`** —— 主落点。在 `### reflect v2 开闸前 T0` 一节(`:367-560`)的 `ab` 小节之后新增两个小节:
  * `prefix-probe`(E1):它回答什么、**不**回答什么(逐字写「不可下结论:命中率 X%、省掉 Y token、provider 已关闭缓存」);96/48 两个规模;`--seed` 必填的理由;标记接缝默认关闭且只在这条命令里打开;不连库;预热单列;两个 dry-run 数字的口径(逻辑调用 vs 请求上界,沿用既有措辞)。
  * `state-probe`(E2):脚本化驱动器 + 真实单次决策的形状;**动作只记录不执行 ⇒ 这批不是自洽真实轨迹**;三个状态点分档报告;`compaction_boundary_reached=false` 的读法;`--concurrency` 恒 1 的理由;有图/无图由**语料格**承载而不由图动作承载(Q5 的实现口径收窄);embedding 调用单列。
  * `ab` 小节补三段:整批墙钟预算与「到点停止派发、不补跑」;manifest 的键与它**不含**什么;**`analyze` 的三处新参数** —— 并把今天那段「D↔L 的差值不由 `optimization_pair_table` 直接给出……先把行降到 T0 键集才能喂进 `analyze`」的**已知限制改写成已解除**(`--baseline-arm` + `--key-set ab`),不留一句过期的读法。
  * 隐私口径那一段补一句 M7 的精确说法:测量产物零正文,`.local/<out-dir>/llm/` 下的交互日志含截断正文、是操作者私有实验资产。
* **`docs/deployment-and-configuration{,_zh}.md`**(`:848-852` 那一族)—— 只加**一条**:`REASONING_REFLECT_MEASURE_CONTEXT` 那一格补一句「E1/E2/E3 三条实验通道都要求参与对照的**每一条臂**都开着它(rig 逐臂回读,不符整批停)」;`REASONING_REFLECT_OPTIMIZATION` 那一格补一句「四格的**实际收益仍未量**,PR-5 只交付实验通道与 dry-run/manifest,真实批次由操作者跑」。**不新增配置项** —— 本期一个 `Settings` 字段都不加(E1 的接缝是 ContextVar 不是配置,预算与种子是 CLI 参数不是配置)。这一条要在文档里明说,免得读者去找一个不存在的 `E1_ENABLED`。
* **`docs/product-and-api{,_zh}.md`**(`:1445` 的投影列一节)—— 只加一句「投影列本期**一列未加**:§10 的五条采用门全部从已有十四列 + 既有 T0 列读出;新增的读法在 `analyze` 的参数上,不在数据形状上」。附带把 `optimization_pairs` 基线可配这件事记一句。
* **`architecture.md:111`**(那段 v2/前缀通道的长散文)—— 追加三句,每句一个新事实:(1) `app/core/llm.py` 上那道**默认关闭的实验标记接缝**(ContextVar + `provider_messages` 第三参数、唯一 set 点的 AST 判据、业务层不 import 的负向断言、`markers=None` 时逐字节回到接入前);(2) 离线一侧的模块分工扩成四格 —— `reflect_context_bench.py`(per-call 纯分析,既有)、`reflect_prefix_probe.py`(E1 计划与统计)、`reflect_state_probe.py`(E2 驱动器与代理)、`reflect_manifest.py`(manifest 纯构造),`scripts/reflect_shadow_rig.py` 三条通道**都只做薄 CLI 适配**;(3) E2 的接缝是 `model_clients`(`_construct_reasoning_retriever` 里那个已经存在的参数)+ 一个 `__getattr__` 委托代理,**服务端零改动、热函数零改动**,这是把它设计成代理而不是给 `reflect()` 加注入参数的直接收益。
* **`fangan_todo.md`** —— 新增 (m)「PR-5 实验通道已落地」,列清:三条通道的入口与产物、manifest、`analyze` 三处;**并把 (l) 里那条 D↔L 读出路径的已知限制标为已解除**。(m) 自己的已知限制照抄本计划的登记项:E2 的图动作缺口(有图/无图改由语料格承载)、E2 的 `message_prefix_bytes` 恒 `None`、E3 的臂序无种子(manifest 如实写 `null`)、`search` 子命令的投影 `optimization` 仍恒 unknown(同 (j)/(k),本期一格未动)、gold 与 judge 仍未做 ⇒ §10.2-1 在没有人工盲审时只能记「未验证」、以及**三条通道的实际收益一个数都还没量**。
* **`fangan_done.md`** —— 特性收官条目:PR-1..PR-5 五期的落地清单 + 「开闸仍是此之后的独立决定,默认 `off` 不变」。
* **不改**:`docs/operations{,_zh}.md`(Q10)、`AGENTS.md`、`CLAUDE.md`、`README{,_zh}.md`、`docs/development{,_zh}.md`、以及任何带日期的设计稿本体。

## §5 风险

1. **生产路径零字节变化**(唯一动生产码的是 T-EX2)。三重判据:`markers=None` 的字节等价用例、唯一 set 点的 AST 判据、业务层不 import 的负向断言。最坏形态是「接缝在某条生产路径上被谁打开了」—— 它不会崩,只会让那条路上的每一次调用多两段无语义标记、并让 `llm_key` 逐次不同(本地缓存整条失效),而这在日志上看着完全正常。所以负向断言不是装饰。
2. **实验不进标准门,但 dry-run 与纯函数必须进**。三条通道的 dry-run 都要能在**没有模型配置、没有 PG** 的机器上跑通(`Runner` 的既有契约);`check.sh` 已把 `MODEL_SERVICES_CONFIG` 清空,结构上调不到真实模型。反向风险是「为了让用例好写而把真跑逻辑掺进 dry-run 分支」—— 判据沿用 `ab_arms` 的既有纪律:**dry-run 与真跑读同一份枚举/同一份解析**。
3. **不补造数据**(§9.2 原话)。三处具体形态:E2 的第三状态点没触到压缩边界 ⇒ 标 `false` 并单列,不调剧本去凑;E3 的预算到点 ⇒ 保留未完成/不成对标记,不补跑到矩阵齐全;`cached_tokens` / `usage` / `finish_reason` 缺失 ⇒ 缺席 = unknown,**绝不折 0**(既有 `_count` / `_measure_reflect_call` 的口径,新模块照抄)。
4. **命名红线**。字段与表头一律不出现 `cache_hit` / 命中率 / hit rate 形状;E1 的结论词面限定在「有 / 无可辨认 / 不确定的时间收益」三格。T-EX3 (d)(f) 两条用例主动钉住,T-EX10 (b) 做一次变异。风险点在**报告散文**而不在代码:一篇写着「稳定前缀省了 30% token」的报告,代码层面一条用例都拦不住 —— 所以 `scripts/README.md` 那一节要把「不可下结论」的三句逐字写进去,让写报告的人抄得到。
5. **E2 的归因边界最容易被越过**。它测的是「固定观察下的策略」,**不是**自洽真实轨迹;P↔B 的差里混着指令/工具说明的布局改动(§9.2 原文:「不能宣称纯缓存因果」);D↔L 在固定状态点上的差是**已知净增**的那一侧(省的那一侧结构上看不见,见 U2)。三句都要同时出现在 `state-probe` 的摘要头部与 README,而不只在设计稿里。
6. **E3 的质量门在 gold 缺位下会被读成「通过」**。§10.2 明说未验证 ≠ 通过,而一份只有时间列漂亮的报告很容易被这样读。缓解:`analyze` 的报告在 `gold_facts_total` 全 `None` 时,那一节要**印一行显式的**质量提示,而不是留空。这是 T-EX9 的一条用例。**实况回填(2026-09-11)**:实现落地的措辞不是「质量:未验证(无 gold / 无盲审记录)」这一句,而是「无 gold / 无盲审记录 ⇒ **判分与人工那半边未验证**」(`scripts/analyze_reasoning_trace.py:1126`)——「未验证」只对判分与人工那半边成立(`QUALITY_JUDGED_KEYS`),不是整份质量结论的全称否定;对一份压根没有质量列的数据集印一句质量结论本身就该更谨慎地限定范围。
7. **热函数与既有闭集零松弛**。本期落点里没有一处在 `reasoning_retrieval.py` / `prompts.py` / `reasoning_aspects.py` / `reasoning_context.py`;四个闭集(`RUN_PROJECTION_KEYS` / `AB_PROJECTION_KEYS` / `CALL_ROW_KEYS` / `JOB_STATUSES`)一格不动。任何一格被动了,就说明这一格的设计跑偏了 —— 停下来重新看 M5(§10 的门确实全部读得出来)。

---

### 实施的关键文件

* `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/backend/app/core/llm.py`(E1 接缝:`:217-256` `provider_messages`、`:615-644` `chat_json` 装配点、`:761-779` 交互日志的请求正文)
* `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/backend/app/services/reasoning_retrieval.py`(E2 接缝:`:9475-9503` `model_clients=repository`、`:5825-5875` `reflect()` 取客户端、`:4190-4238` 两个唯一读点、`:4708-4845` 布局分派与测量 —— **只读,不改**)
* `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/scripts/reflect_shadow_rig.py`(三条通道的薄适配:`:5243-5410` parser、`:3268-3360` `cmd_ab`、`:4412-4500` 预算落点、`:2086-2213` E2 复用的两个函数、`:1671-1800` 环境与逐臂 Settings)
* `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/scripts/analyze_reasoning_trace.py`(§10 的三处口子:`:170` 基线、`:173-195` `load_rows`、`:360-472` 两张配对表)
* `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/reflect-prefix-measure/backend/app/eval/reflect_ab.py`(`:43-107` 键集与 `ARMS` 五格、`:812-861` 两条臂身份断言 —— 复用,不改)

新建:`backend/app/eval/reflect_prefix_probe.py`、`backend/app/eval/reflect_state_probe.py`、`backend/app/eval/reflect_manifest.py`、`backend/app/eval/reflect_t0/state_probes.json`、`backend/app/eval/reflect_t0/prefix_probe_sample.json`,以及三份对应用例。
