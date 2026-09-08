# reflect v2 开闸前测试摸排 · A/B：真实模型决策对照与判分（设计规格 v1）

> **状态**：v1 待拍板（§13 列出需要用户裁决的点）。只写规格，不含实现。
> **上游**：`2026-09-07-retrieval-reflect-final-design_zh.md` §9.2（题集覆盖形态、评测区分维度、开闸候选标准、外部判分模型只作辅助）与 §11；
> T0 规格 `2026-09-08-reflect-t0-trace-analysis-design_zh.md`（PR #700，**尚未合入**，分支 `origin/claude/reflect-t0-trace-analysis`）。
> **基线**：`origin/master`（含 PR #698 reflect v2 接入、#701 鲁棒性整改）。
> **硬依赖**：本规格的 rig 是 T0 rig 的一个新子命令，题集是 T0 题集的原地扩充——**PR #700 合入之前不能开工**。

## 0. 结论先行

1. A/B 在 T0 的 `scripts/reflect_shadow_rig.py` 上新增 `ab` / `ab-judge` / `ab-report` 三个子命令。跑的是**完整 Ask**（意图契约 → 检索 → 合成 → 引用绑定），不是 T0 的 search-only。
2. **进程内**调 `AskService.ask_reasoning()`，跑在 T0 `seed` 建出来的**一次性 PG 测试库**上。不走 HTTP。决定性理由不是省事，是**配对**：HTTP 侧换策略必须重启后端（策略是后端进程的环境变量），两条臂只能整批分开跑、相隔数小时，provider 的漂移会整块落在臂的差上；进程内可以按 T0 已经验证过的办法为两个策略各构造一份 `Settings`，同题同档的 legacy/v2 **背靠背**跑，漂移对两臂等量。详见 §5。
3. 判分**三层，权重从高到低**：确定性判据（唯一能进开闸判据的一层）→ 人工盲抽（裁决确定性判据的命中）→ 判分模型（只做逐项「答案里有没有这条」的粗筛，**只用于给人工抽样排队**，一个数都不进开闸判据）。
4. 开闸候选标准逐类题成立，不看总平均分；任一类题在关键回归维度上退化即不达标（§9）。
5. search-only 量不出答案质量。T0 已经证明的是「反思轮更少、总时更短、方面自评齐全」；A/B 要补的是「**答案有没有更好**」这一半——两件事在 T0 的数据里是分开的，规格里也不合并。

## 1. 目标与非目标

### 目标

- 在冻结的语料、索引、模型服务与档位下，只切 `REASONING_REFLECT_V2_ENABLED`，产出**逐题配对**的答案质量与成本对照。
- 给出一份能直接回答「开闸 / 不开闸」的证据表：逐题差值、逐类题结论、成本与延迟、人工抽样记录。
- 产出的数据集只含闭集键，可以随报告存档；答案正文与引用文本只落 `.local`。

### 非目标

- 不改任何生产代码路径（§5.3 说明为什么连一个「不落库的合成入口」都不加）。
- 不引入新的判分服务、不新增 provider、不给判分模型任何裁决权。
- rig 不进标准门，不在 CI 跑真实模型（沿用 T0 的边界）。
- 不做统计显著性检验。3 次重复的样本量支撑不了 p 值，声称显著就是造假；证据形态是逐题表 + 逐类符号计数（§8.3）。
- 不评测 Knowhow 与跨库原文（§4.3 说明它们为什么以断言而不是以臂的形式覆盖）。

## 2. 现状事实（T0 首跑，2026-09-08/09 实测）

T0 的 `search` 子命令：主库只读、不建图、**不合成答案**，deepseek-v4-flash，A 无图单篇 + B 有图多篇各 4 题 × `{standard, deep}` × `{legacy, v2}`。

| 事实 | 值 | 读法 |
| --- | --- | --- |
| 反思轮总数 | v2 **33** vs legacy **61** | v2 更早收敛；但 search-only，收敛得早不等于答得好 |
| 总墙钟 | v2 **86 s** vs legacy **126 s** | 同上，且含排队与 provider 往返 |
| v2 方面自评 | **22/22** 个 run 拿到逐方面读数 | #701 的追问机制在真机上生效，`assessment` 不再是空账 |
| A 格 gold 段命中 | v2 **35** vs legacy **30** | search-only 下唯一一个沾到「质量」的信号，且只到「检索到了 gold 段」，没到「答案用了它」 |
| 目录题的支撑 | 靠集合键，终态 `model_sufficient` | §7.1 那条「complete 枚举签发集合键」的取舍在真机上兑现 |
| 开思考（flash）| **20%** 空正文（`finish_reason=stop`），13 次单轮失败被接住，7 个 run 连续两次失败收 `model_degraded`，熔断全程未开 | 空正文是**模型侧**故障，不是策略差异；A/B 里必须按 `finish_reason` 归因后再谈臂差（§9 硬判据 4） |

**A/B 要补的**：答案正文、引用是否落在 gold 上、必答方面在答案里有没有被回答、完整性有没有虚报、成本与延迟。这些在 search-only 里结构上不存在。

另有两条来自代码的现状事实，直接决定 §5 的取舍：

- `AskService._commit_reasoning_draft` 的 docstring 写明它是 **the only reasoning answer persistence boundary**，并对 draft 的 6 个身份字段逐个复核（codex #571 R2/R3 加固）。`_run_reasoning_stage` 的 docstring 同样写明「commit boundary stays core-owned below both seams — no stage implementation reaches the atomic save」。
- `ask_reasoning()` = `_prepare_reasoning_ask` + `_run_reasoning_stage(...).response`，**会**落库（conversation + answer；有 `job_id` 时还有 job 与 trace 步）。所以「跑完整 Ask 又不落库」在今天不存在现成入口。

## 3. 题集与 gold

### 3.1 沿用 T0 题集，不另起一套

题集文件仍是 `backend/app/eval/reflect_t0/questions.json`（PR #700 引入）。A/B **原地扩充**它，不复制、不改名：该文件的 `note` 里本来就写着「gold 只对 A 语料保留，供后续 §9.2 的真实决策 A/B 使用」——它从第一天起就是为这件事留的槽。目录名带 `t0` 是历史，不值得为它做一次会与未合入的 #700 冲突的重命名；本规格在 §11 记一条低优先级的改名债务。

| 格 | 语料 | 题 | 已有 gold | A/B 要补 |
| --- | --- | --- | --- | --- |
| A（单篇，arXiv 2502.05171） | `A_nokg` | 24（`A-q01..q24`，中英各一份题面） | `gold_section_path` | 每题 `gold_facts`（≤3 条） |
| B（6 篇公开论文） | `B_kg`（另加一次 `B_nokg` 探针） | 10（`B-q01..q10`） | 无 | `gold_sources` + `gold_facts` |
| Report | `B_kg` | 4（`R-q01..q04`） | 无 | **不补 gold**：只做人工抽样与成本 |

### 3.2 gold 的形态与写法

在 `questions.json` 的每道题上**内联**两个新字段，与已有的 `gold_section_path` 并排：

```jsonc
{
  "key": "B-q06",
  "corpus": "B",
  "shape": "param_default",
  "zh": "KIVI 默认把 key 和 value 各量化到多少 bit?…",
  "gold_sources": ["KIVI"],            // 应当被引用的来源标题（可判等的短标识，见下）
  "gold_facts": [                       // ≤3 条,每条是一句可当场核对的短断言
    "key 按 per-channel 量化到 2 bit",
    "value 按 per-token 量化到 2 bit",
    "默认组大小是 32"
  ]
}
```

约定：

- `gold_facts` 每条 ≤40 字，只写**能在原文里指出出处**的断言。写不出出处的不写——gold 是人工核对过的定值，不是期望值。
- `gold_facts` 至多 3 条，且**不覆盖整题**。它不是答案提纲，是「这几点必须在」。这条限制是为了让 `gold_facts_hit` 保持成一个低方差的粗筛量，而不是一个伪装成客观分的主观分。
- `gold_sources` 用**可判等的来源短标识**（论文短名），rig 侧按「标题包含该短名」解析成 `source_id`；解析不到任何一个就是配置错误，跑批之前响亮失败，不能静默变成 0。
- Report 4 题不写 gold：4 个样本上的自动判分只会制造精度幻觉，成本又最高。它们只出成本表 + 人工抽样。

### 3.3 与既有评测资产的同族关系（复用点与刻意的分歧）

`backend/app/eval/selected_source_graph.py` 是仓库里**已有的双臂（baseline / shadow）质量门**，A/B 按它的形状建，不另发明：

| 既有约定 | 出处 | A/B 怎么用 |
| --- | --- | --- |
| 冻结 dataclass 三件套：`GoldenCase` / `Observation` / `LaneMetrics` + `GateResult(approved, hard_failures, quality_failures, cost_failures)` | `selected_source_graph.py:35-130` | **照抄结构**，改名为 `ReflectAbGoldCase` / `ReflectAbObservation` / `ArmMetrics` / `ReflectAbGateResult`，三类 failure 分开的形态原样保留 |
| `ModelSamplingContract(provider, model, prompt_version, temperature, top_p, seed)` + `corpus_signature` | 同上 `:50-72` | **直接复用同名同形**，另加 `thinking_mode`；每行观察都带，跨批次混用时能当场发现 |
| `RetrievalCost(latency_ms, database_rows, peak_memory_bytes, prompt_tokens, model_calls)` | 同上 `:60` | 复用，并**只**补 `completion_tokens`（T0/§7 的成本键与它对齐，不另造一套命名） |
| 「Questions and answer text never enter the attestation」 | 同上模块 docstring | 同一条口径，见 §7.3 |
| `_ratio(numerator, denominator, *, empty)` 这类空分母显式取值 | 同上 `:31` | 复用；A/B 侧空分母一律 `unknown`，不取 `1.0` 也不取 `0.0`（T0 §4.1 的 unknown 一等值规则更严，以 T0 为准） |
| `--only quality,speed,...` 的分块 CLI | `backend/app/eval/run_all.py` | `ab-report --only paired,cost,human` 同形 |
| `recall_at_k` / `mrr` 对空 gold 返回 `None` 而不是 0 | `retrieval_metrics.py:9-25` | 同一条：没 gold 的题在该指标上跳过，不计 0 |

**刻意不对齐的两处**，理由写在这里免得被当成遗漏：

1. **gold 不单独出 `*_gold.yaml`**。`memory_gold.yaml` / `recall_gold.yaml` / `selected_source_graph_gold.yaml` 那条约定成立在「题面与 gold 分属两个文件」的场景；这里 A 题的 `gold_section_path` 已经内联在 `questions.json` 里，再把 B 题的 gold 拆成一个 yaml，就是把同一张表的两半放进两种格式。单一真源优先。
2. **不新增 `app/services/` 侧的 gate policy 模块**。`SelectedSourceGatePolicy` 落在 services 是因为那个门是**运行时**的放量闸；reflect v2 的开闸是一次人拍板的开关翻转，判据不需要在生产进程里可读。全部留在 `backend/app/eval/reflect_ab.py`。

### 3.4 §9.2 点名形态的覆盖对账

不吹覆盖率。逐条对账，缺的写成缺的：

| §9.2 形态 | 覆盖 | 说明 |
| --- | --- | --- |
| 单事实 | ✅ A-q08 / A-q14 / A-q24 | 数值型单事实 |
| 参数默认值与条件 | ✅ B-q05 / B-q06 | |
| 两个对象比较 | ✅ B-q03 / B-q04 | |
| 来源目录与类型清单 | ✅ B-q01 / B-q02 | T0 已证实走集合键 |
| 同标题/同内容 | ✅ B-q07 | 两次不同解析的 DeepSeek-V2 |
| 首轮空手 | ✅ B-q08 | |
| 工具耗尽 / 超预算 | ✅ B-q10 + `deep`；R-q03 / R-q04 | |
| 范围缩小 | ✅ B-q09 | |
| 无图库 | ✅ A 全部（`A_nokg`）+ B 的 `B_nokg` 探针 | |
| outline overflow / Report 方向补取 | ✅ Report 臂（depth 8） | |
| 模型坏 JSON | ⚠️ 不可由题面驱动 | 只**报告**两臂的发生率与 `model_degraded` 归因（T0 已实测到 13/7）；行为正确性由既有单测拥有 |
| 新证据关键句在前 80 字之后 | ⚠️ 是证据版式属性，不是题面属性 | 由既有确定性用例拥有，A/B 不设臂 |
| TX/RX/周期性限定 | ❌ **缺** | 该形态出自另一套（模拟/射频）语料。本期以 B-q09 的范围收窄作最近似，并在 §11 记一条债务：往 A 语料补 2 道限定条件题（如「只看 r=32 那一组」），零新语料成本 |
| 冲突材料 | ❌ **缺** | B-q07 的两份解析是同内容不同切分，不构成结论冲突。§11 记债务 |
| 混合/参考库 | ❌ **刻意不做** | 测试库挂基座会让 `seed` 成本翻倍，而 reflect 策略本身没有基座分支。§11 记债务 |
| Knowhow 未接新策略 | ✅ 以断言而非以臂覆盖 | 见 §4.3 |

## 4. 运行矩阵

### 4.1 主矩阵

```
{legacy, v2} × {standard, deep} × 3 次重复
A-q01..q24  跑在 A_nokg
B-q01..q10  跑在 B_kg
```

= 34 题 × 2 策略 × 2 档 × 3 次 = **408 个 Ask run** / 每个模型服务组。满足 §9.2 的「至少 30 个案例、各运行 3 次」。

题面语言默认 `zh`（`--lang zh`）。中英双跑会把 408 变成 816；英文题面作为**可选组**，只在 flash 关思考这一组做，用来看语言是否引入策略无关的方差。

### 4.2 重复轮是最外层循环

跑批顺序是 `repeat → effort → question → {两臂背靠背，臂序随机}`。两条约束：

- **重复轮在最外层**⇒ 任何被预算或故障截断的前缀都是一份**配对完整、平衡**的数据集（第 1 轮跑完就有一份能出结论的 n=1 数据）。
- **两臂在最内层背靠背、且臂序按 run 随机**⇒ provider 的排队与限流漂移对两臂等量，臂序本身不成为系统偏差。这是选进程内路径的直接原因（§5）。

`--only-policy` 只在一侧被整批打废时用；这样重跑出来的行标 `paired=false`，**不进配对差值表**，只进单臂基线表。

### 4.3 模型服务分层与旁支

| 组 | 内容 | 何时跑 |
| --- | --- | --- |
| G1 | deepseek-v4-flash，**关思考** | **先全量**跑完主矩阵（408 run）。这是 T0 已有对照的那一组，也是唯一有把握跑完的预算 |
| G2 | deepseek-v4-flash，**开思考** | 按预算逐组加。T0 实测该配置有 20% 空正文（模型侧），所以它主要是**鲁棒性**探针：看 `model_degraded` / 空正文率两臂是否对称，而不是看质量 |
| G3 | deepseek-v4-pro，默认配置 | 最后加，且可只跑 `standard` + 重复 1 轮（34 × 2 = 68 run），用来确认结论不是 flash 独有 |
| P1 | `B_nokg` 探针：B 的 10 题 × 两臂 × `standard` × 1 轮 = 40 run | 与 G1 同批。作用是抓「无图时 v2 反而更差」，不进主表，单出一节 |

Knowhow **不设臂**：它构造检索器时 `allow_reflect_v2=False`，两臂对它逐字相同。`ab` 只做一次一次性断言（构造出的 retriever `reflect_v2_active()` 为假），断言失败就整批停；行为正确性由既有单测拥有。

### 4.4 预算与耗时（要在开工前跟用户对齐）

T0 的 search-only 是 ~2.7–4 s/run；加上合成、意图契约与引用绑定，每 run 保守按 30–60 s 估。G1 的 408 run 串行 ⇒ **3.5–7 小时**，外加 `seed` 一次（解析 + 建图，以小时计，可复用于全部组与全部重复轮）。`ab` 必须在 `--dry-run` 下打印 run 数与模型调用上界估计（沿用 T0 `_search_call_estimate` 的「是上界不是预测」措辞）。

**并发默认 1**。理由不是保守：token 归因靠 LLM 日志的时间窗切片（§7.2），并发 >1 时切不干净。`--concurrency > 1` 时 rig 强制把 `prompt_tokens` / `completion_tokens` / `model_calls` 三个键写成 `unknown`，只保留 rig 自己掐的 `latency_ms_total`——宁可少一个成本表，不要一个对不上的。

## 5. rig `ab` 子命令的形状

### 5.1 取舍：进程内，不走 HTTP

两条路都要先有 T0 的 `seed` 测试库——§9.2 要求「冻结语料/索引」，而主库是用户在用的活库，既不冻结也不能被写。所以真正的取舍只在「测试库之上，进程内调 `AskService` 还是走 HTTP」。

| | 进程内 `AskService.ask_reasoning()` | HTTP（T0 的 `cmd_ask`） |
| --- | --- | --- |
| 换策略 | 同进程各构造一份 `Settings`，**逐 run 切**（T0 `cmd_search` 已验证，且每 run 开跑前核对 `retriever.reflect_v2_active()`） | 策略是后端进程的环境变量，**换策略必须重启后端**（T0 `cmd_ask` 原文） |
| 能否两臂背靠背配对 | ✅ | ❌ 只能整批分臂，相隔数小时 |
| 覆盖面 | 意图契约 + 检索 + 合成 + 引用绑定 + 落库 | 同上，另加路由校验、durable job、SSE、幂等键 |
| 额外成本 | 无 | 每次重启的等待与预热；`_assert_tag_landed` 那类端点选择陷阱 |
| 生产改动 | 无 | 无 |

**结论：进程内。** 决定性的一条是配对——A/B 的全部说服力来自「同题同档同时刻，只差一个开关」，而 HTTP 路径在这个矩阵上结构性地做不到；用它就等于把 provider 数小时的漂移整块记到策略头上。HTTP 那份增量覆盖（路由、job、SSE、幂等）与反思策略无关，且已由既有集成测试与 T0 的 `ask` 子命令拥有——需要时单独跑一小批 `ask` 做接线抽查，不必让整个 A/B 迁就它。

### 5.2 合成会落库，落进一次性测试库——这是接受的，不是绕过

进程内的 `ask_reasoning()` 会写 conversation + answer 行。这些行落在 `silicon_notebook_ab_test`（`--teardown` 删库），语料是公开论文，题面公开，答案是模型对公开论文的作答：**没有需要防的敏感面**。`.local` 的隔离是仓库卫生（正文不进数据集、不进 git），不是保密（§7.3）。

`job_id` 传空串 ⇒ 走 `_prepare_turn` 的 legacy create-or-continue 分支，不建 durable job、不写 `ask_trace_steps`。轨迹**不从库里导**，而是经 `on_trace` sink 在进程内拿（与 T0 `report` / `search` 同形），所以也不需要 `export` 那一步：`ab` 一次跑完就同时写出闭集数据行与 `.local` 的正文档案。

### 5.3 为什么不加「不落库的合成」入口

现成入口不存在。技术上最近的落点是 `_draft_reasoning_response`（可注入的 `ResponseDraftStage`，产出 `ReasoningResponseDraft` 而不提交），但它吃的 `ResponseDraftInput` 是在 `_run_reasoning_stage` 内部装配的——gap 补取、Memory、结构化预览、集合地图、结束事实块都在那几百行里。在 rig 侧重造这个装配，就是把 A/B 变成「评测 rig 自己的合成」而不是产品的合成，而且会随生产代码静默漂移。

给它开一个 `persist=False` 的口子同样不做：`_commit_reasoning_draft` 是被 codex #571 R2/R3 加固过的、docstring 里声明为**唯一**的落库边界，为一个评测 rig 在它旁边开旁路，是拿一条安全边界换一点便利。

**若用户仍要「对主库只读跑完整 Ask」（省掉 `seed` 的数小时）**，最小生产改动是：在 `app/application/ask_reasoning.py` 增一个 `dry_run_reasoning_ask()`，走同一条 `_prepare_reasoning_ask` → `_run_reasoning_stage`，但在 stage 编排层以**编译期分支**（不是运行时 flag）返回 `ReasoningResponseDraft` 而不调 `commit_response`，并加守卫用例断言任何路由都到不了它。代价：`_prepare_turn` 仍会建 conversation 行（要一并旁路），落库边界从「一个」变成「一个 + 一个显式不落库分支」，且必须说服评审这不是在削弱 #571 的加固。**规格建议不做**，列在这里是因为它是唯一的替代路径，需要用户拍板（§13-1）。

### 5.4 子命令与开关

```
reflect_shadow_rig.py ab \
  --database-url <测试库 URL>        # 必填,不读 .env
  --cell A_nokg,B_kg                  # 主矩阵;B_nokg 探针单独一次
  --efforts standard,deep
  --repeats 3  --round 1              # --round 只跑第 N 轮(重复轮是最外层)
  --lang zh
  --concurrency 1                     # >1 时成本三键强制 unknown
  --limit N  --only-question K,...    # 缩批验证
  --out-dir .local/ab
  --dry-run
```

产物（全部在 `--out-dir` 下）：

| 文件 | 内容 | 进仓库？ |
| --- | --- | --- |
| `ab-runs.jsonl` | 每 run 一行的**闭集**投影（§7） | 可以（随报告存档） |
| `ab-runs.log` | run 序号/题号/格/臂/档/轮次/耗时/反思轮数/终态 | 可以（无原文） |
| `raw/<arm>/<key>_<cell>_<effort>_r<n>.json` | 答案正文、引用列表、锚点、意图契约、原始 TraceStep（含 summary） | ❌ 只在 `.local` |
| `intents.jsonl` | 每题一份冻结契约（含问题原文） | ❌ 只在 `.local`（沿用 T0） |
| `judged.jsonl` | `ab-judge` 的逐题逐项判分（只有计数与布尔） | 可以 |
| `report.md` | `ab-report` 的出表 | 可以 |

意图契约**每题算一次并缓存**（沿用 T0 `_IntentCache`）：两臂共用同一份冻结契约是 A/B 成立的前提——契约不同，必答方面就不同，两臂根本不在比同一件事。契约缓存的 key 是 `question_key + lang`，与档位、臂、轮次无关。

### 5.5 跑批前后的硬断言（照 T0 的「响亮失败」口径）

1. **策略对号**：每 run 开跑前核对 `retriever.reflect_v2_active()` 与该 run 声明的臂一致；每条臂的第一个 run 之后再核对投影出来的 `policy_version`（v2 的判据是这次 run 产出了 `termination`）。对不上当场停。
2. **主库零接触**：`--database-url` 必须指向 `*_test` 库；跑前跑后各点一次主库的 `ask_jobs` / `answers` / `conversations` 行数，任何一张变了就标红（`seed` 只在重建 A 语料元素时只读主库）。
3. **契约一致**：同一 `question_key` 的两臂必须引用同一条契约缓存行（比 hash），否则该题整题作废。
4. **注入面全关**：`agent_profile` / `retrieval_experiences` / `identity_store` 一律不接（沿用 T0 `run_search_once` 的口径），注入三闸强制关。
5. **Knowhow 断言**：一次性断言 Knowhow 路径的 `reflect_v2_active()` 为假。
6. **gold 可解析**：`gold_sources` 的每个短名都能在测试库里解析到恰好一个 `source_id`；解析不到或解析到多个，跑批之前失败。

## 6. 判分：三层，权重与不可越权

### 第一层 · 确定性判据（**唯一进开闸判据的一层**）

零模型、零人工，纯计算。数据来自 `AskResponse`（answer / citations / anchors / evidence_level）、`on_trace` 的步、`RetrievalTermination`、gold 表、LLM 日志的 usage。逐键定义见 §7.1。

不可越权：只有这一层的键能进 §9 的开闸判据。

### 第二层 · 人工盲抽（裁决第一层的命中，并给第一层没覆盖的东西定性）

- 抽样量：**每类题 2 题 × 两臂 × 第 1 轮 = 12 题 × 2 = 24 份答案**；类的划分见 §8.2。
- **盲**：抽样工作表里不出现 `legacy`/`v2`，两臂答案的左右位置按题随机，`reflect_turns` 等会泄露臂的字段一律不展示。
- 人工只回答三个问题（每份答案）：(a) 有没有事实错误；(b) 有没有 §7.1 的 `completeness_claim` 这条自动信号所指的虚报（**这一格是裁决**，自动信号只是候选）；(c) 引用是否指向能支持该句的材料。
- **每一条被自动判成 `completeness_claim` 的 run 都要进人工复核**，不管抽没抽中——虚报是硬判据，硬判据不能建在正则上。
- Report 4 题：两臂各 1 轮，**全部**人工读，只记 (a)(b)(c) 与成本，不出自动分。

### 第三层 · 判分模型（只做粗筛，一个数都不进开闸判据）

- 只做两件事，都是**逐项、绝对、非比较**的判断：
  1. `gold_facts_hit`：对每条 `gold_facts`，答案里有没有陈述这一条（是/否）。
  2. `aspects_missed`：对该 run 冻结契约里的每个 `mandatory_topics`，答案里有没有对应的作答段落（是/否）。
- **不做**成对偏好打分、不做整体质量评分、不排名。理由：偏好打分有位置偏差与长度偏差，而 §9.2 明确「外部判分模型若使用，只作辅助」——只要它给出的是一个可排序的分，它事实上就在裁决。
- 输出的每个键都带 `model_judged` 标；`ab-report` 在这些键所在的列头上印同一个标；§9 的判据表里它们出现在「参考」栏、不出现在「判据」栏。
- 它的**唯一用途**是给第二层排队：`gold_facts_hit` 的两臂差值最大的题优先进人工抽样。
- 工程约束：与被测模型**不同**的服务（拿不到不同服务时，同服务但在报告里写明这一点）；两臂共用同一份 prompt 与同一个模型版本；温度取服务允许的最低；**在全部臂跑完之后一次性批跑**，防止判分侧在实验中途漂移；报告里记录判分模型 id + prompt 的 hash。

## 7. 每题一行的闭集投影

### 7.1 新增键（在 T0 `RUN_PROJECTION_KEYS` 之上）

`AB_PROJECTION_KEYS = RUN_PROJECTION_KEYS ∪ 下表`。所有键都允许 `null` = unknown；**unknown 不当 0、不当 false**（T0 §4.1）。

| 键 | 层 | 定义 | 说明与陷阱 |
| --- | --- | --- | --- |
| `answer_chars` | 确定 | `len(response.answer)` | **协变量，不是质量指标**。人和判分模型都会奖励长答案，所以出表时它与 `gold_facts_hit` 并列，用来识别「靠更长赢」 |
| `citations` | 确定 | `len(response.citations)` | |
| `anchors_on_gold` | 确定 | 落在 gold 上的**不同**锚点数 | A 格：锚点 → `element_id` → `source_elements.metadata.section_path`（T0 已核实该表**没有** `section_path` 列，只在 `metadata` 与 `chunks` 里），按前缀匹配 `gold_section_path`。B 格：锚点 → `source_id` → 标题命中 `gold_sources`。任一跳解析不到 ⇒ 该 run 该键 **unknown**，绝不写 0 |
| `anchors_total` | 确定 | 锚点总数 | `anchors_on_gold` 的分母；缺它就只能比绝对数，长答案自动占便宜 |
| `anchors_unresolved` | 确定 | 解析不到服务端签发证据的锚点数 | 引用有效性的硬判据（§9-2） |
| `citations_out_of_scope` | 确定 | 引用解析到本 run 声明范围之外的条数 | 权限硬判据（§9-1）。范围收窄题（B-q09）是它唯一可能非零的地方，而正确值恒为 0 |
| `completeness_claim` | 确定信号 **+ 人工裁决** | 答案里出现完整性断言（「全部/所有/共 N 篇/一共/逐一列出」类）**且**本 run 的枚举链终态不是 `complete` | 正则只产候选，命中项 100% 进人工（§6 第二层）。进判据的是**人工确认过的**那一份 |
| `invalid_tool_calls` | 确定 | 轨迹里 `invalid` / `unavailable` / `duplicate` 类观察的次数 | 复用 T0 的 `skip_reasons` 投影，不另数一遍 |
| `early_stop` | 确定 + 参考 | run 以 `model_end`/`model_sufficient` 收尾、**且** 至少一条 `gold_facts` 未命中、**且** 步数预算还剩 ≥2 | **不能**用 v2 的 `unresolved_aspect_ids` 定义：legacy 根本没有方面账，那样定义两臂结构上不可比。因为依赖 `gold_facts_hit`，本键带 `model_judged` 传染标，只作参考 |
| `model_calls` | 确定 | 本 run 内的 LLM 调用次数 | 见 §7.2 |
| `prompt_tokens` / `completion_tokens` | 确定 | 本 run 的 usage 累计 | 见 §7.2；`--concurrency > 1` 时恒 unknown |
| `latency_ms_total` | 确定 | rig 在 `ask_reasoning()` 外侧掐的墙钟 | **含排队与 provider 往返**，只在同一次交错跑的配对内可比，跨批次不可比（沿用 T0 §4.3 的措辞） |
| `finish_reason_codes` | 确定 | 本 run 各次调用的 `finish_reason` 计数 | T0 的 20% 空正文靠它归因；§9 硬判据 4 要用它把 provider 故障与策略差异分开 |
| `answer_empty` | 确定 | `answer_chars == 0` | |
| `gold_facts_total` / `gold_facts_hit` | 判分模型 | 该题 gold 条数 / 命中条数 | `model_judged` |
| `aspects_declared` / `aspects_missed` | 判分模型 | 冻结契约的 `mandatory_topics` 数 / 答案未作答的数 | `model_judged`。**注意**：口径是「答案有没有回答」，不是 `unresolved_aspect_ids`（后者是模型对检索的自评，v2 独有） |
| `human_factual_error` / `human_completeness_false` / `human_citation_bad` | 人工 | 三个布尔 | 只有被抽中的 run 有值，其余 unknown |
| `paired` | 确定 | 本行是否有同题同档同轮的对臂行 | `--only-policy` 重跑出来的行为 false |
| `arm` | 确定 | `legacy` / `v2` | 与 T0 的 `policy_version` 冗余但独立：一个是 rig 声明的，一个是从轨迹反推的，两者不符是响亮失败 |
| `repeat` | 确定 | 第几轮（1..3） | |
| `model_contract` | 确定 | `ModelSamplingContract` + `thinking_mode` | 复用既有 dataclass（§3.3） |
| `corpus_signature` | 确定 | 语料/索引指纹 | 复用既有键名；跨 `seed` 混用当场可见 |

### 7.2 成本三键的来源与它的限制

`app/core/llm.py` 把每次调用的 `usage`（`prompt_tokens` / `completion_tokens` / `total_tokens`）、`finish_reason`、`latency_ms`、`status` 写进 LLM 交互日志；日志记录里**没有** run 或 actor 标识（隔离是靠写进哪个文件）。所以归因只能靠**时间窗切片**：rig 记下每个 run 的起止时刻，从日志里取该窗口内的记录求和。

由此产生一条硬约束，写进 `ab` 的参数校验：**`--concurrency 1` 才有成本三键**。并发下窗口重叠，切片会把别的 run 的 token 记到本 run 上；那种数比没有更坏。日志正文（prompt / response 片段）**不读进数据集**，只取数值字段。

### 7.3 隐私与暴露面

**数据集**（`ab-runs.jsonl` / `judged.jsonl` / `report.md`）：只含 `AB_PROJECTION_KEYS`，守卫用例断言 `set(row) ⊆ AB_PROJECTION_KEYS` 且不含 `question` / `answer` / `summary` / `reason` / `title` / `*_id`（哈希桶除外）。往投影里加一个 `answer` 键，守卫必红。

**只落 `.local`**：答案正文、引用文本与 `quoted_span`、来源标题、冻结契约（含问题原文）、原始 TraceStep 的 `summary`、LLM 日志。沿用 T0 `--keep-raw-trace` 已经建立的落点形态。

**判分模型的暴露面——最小集，理由**：

一次判分调用只看四样东西：

1. 题面（本来就是公开的、随仓库的）；
2. `gold_facts` 那 ≤3 条短断言（人工写的，不含语料原文的成段引用）；
3. 该 run 冻结契约里的 `mandatory_topics` 文本（由题面派生，不含语料内容）；
4. **一份**答案正文。

**不给**：来源标题、`gold_sources`、证据正文与 `quoted_span`、引用列表、轨迹、`arm` 标签、耗时与调用数、另一臂的答案。

理由逐条：

- **不给来源正文/证据**：判分模型的任务是「答案里有没有陈述这条已经被人核对过的事实」，不是「这条事实对不对」。后者在写 gold 的时候就由人做完了。给原文只会让它开始自行裁决事实，而那正是 §9.2 不允许它做的。
- **不给来源标题**：`gold_sources` 的核对是**确定性**的（锚点 → `source_id` → 标题匹配），判分模型在这件事上没有位置。给了标题反而给了它一条按「答案提到了正确的论文名」来奖励的捷径。
- **不给 `arm` 与任何能推出臂的字段**：判分模型一旦知道哪份是新策略，A/B 就不再是 A/B。同理不给成对比较——见 §6 第三层。
- **不给引用列表**：引用有效性是确定性判据（`anchors_unresolved` / `citations_out_of_scope`），交给模型只会用一个软信号污染一个硬信号。
- **一次一份答案**：绝对判断而非相对判断，规避位置偏差；也让同一份答案在不同批次里判分可复现。

副作用要认：判分模型看不到证据，就无法发现「答案陈述了 gold 事实但其实引错了地方」。这类错误由第二层人工的 (c) 项拥有，规格里明确它**不**由判分模型覆盖，不是漏了。

## 8. 出表

### 8.1 逐题配对差值表（主表）

同 `question_key + corpus_cell + effort + repeat` 成对，逐 run 出 `v2 − legacy` 的差值。分位数只在 `n_observed ≥ 5` 时出（T0 §4.1）。每题另出一行「跨 3 轮的中位数配对差」，作为该题的代表值。

| question_key | class | effort | Δanchors_on_gold | Δanchors_unresolved | Δgold_facts_hit ᴹ | Δaspects_missed ᴹ | completeness_claim(l/v) | Δinvalid_tool_calls | Δreflect_turns | Δmodel_calls | Δlatency_ms | Δanswer_chars |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

ᴹ = `model_judged`，不进判据。`Δanswer_chars` 紧跟在质量列后面，是为了让「靠更长赢」当场可见。

### 8.2 逐类题小结（开闸判据在这一张表上读）

题按 `shape` 归 6 类：`single_paper`(A 24) / `roster+type_inventory`(B-q01,q02) / `compare_two`(B-q03,q04) / `param_default`(B-q05,q06) / `duplicate+scope`(B-q07,q09) / `empty_first_round+over_budget`(B-q08,q10)。

每类出：题数、改善/持平/退化的**符号计数**（按该类每题的中位数配对差）、该类的 `anchors_on_gold` 中位数（两臂各一）、该类的硬判据是否有新增失败。

**不出该类的综合分，也不出全局综合分。** §9.2 原文：不得只凭整体平均分掩盖某一类题退化。

### 8.3 关于统计

3 次重复只够看方差量级，不够做检验。报告里：给每题 3 轮的极差、给每类的符号计数、给两臂的分布图（`n ≥ 5` 时给 P50/P95）。**不出 p 值，不出置信区间，不写「显著」。**

### 8.4 成本表

按 `arm × effort × 模型服务组`：`model_calls` / `prompt_tokens` / `completion_tokens` 的均值与总和，`latency_ms_total` 的 P50/P95（`n ≥ 5`），`answer_empty` 率，`finish_reason` 分布，`model_degraded` 率。所有成本数字标注是否在 `--concurrency 1` 下采集。

### 8.5 人工抽样工作表

盲表（§6 第二层）+ 揭盲后的记录表。揭盲后的表要能逐条追回 `question_key` 与 `arm`。

## 9. 关键回归与开闸候选标准

**硬判据（任一条不达标 ⇒ 不是开闸候选）**：

1. **权限**：`citations_out_of_scope` 在任何 run 上为 0，两臂皆是。v2 上出现任何一条即失败。
2. **引用**：`anchors_unresolved` 在**任一类题**上，v2 的中位数不得高于 legacy。
3. **完整性**：**人工确认过的** `completeness_claim` 上，v2 不得有 legacy 没有的新增（逐题看，不看比率）。
4. **空答案**：v2 的 `answer_empty` 率不得高于 legacy；若高，必须逐 run 用 `finish_reason_codes` / `model_degraded` 归因，只有当**全部**超出的 run 都能归到 provider 侧故障时才不算失败（T0 已知 flash 开思考有 20% 模型侧空正文，这条就是为它写的）。
5. **逐类题不退化**：6 类题中的**每一类**，v2 的 `anchors_on_gold` 中位数不得低于 legacy。任一类退化即失败——即使总体均值改善。
6. **简单题不膨胀**：`single_paper` 类在 `standard` 档上，v2 的 `reflect_turns` 中位数不得比 legacy 高出 1 轮以上（§9.2「简单题没有系统性增加反思调用」）。样本门：该格 `n_observed ≥ 5`，否则判据不成立、按「未验证」处理而不是按通过。

**必须如实列出、但不阻断**：

7. 成本与延迟的全部变化（§8.4），包括变差的。
8. `gold_facts_hit` / `aspects_missed` 的两臂差（带 `model_judged` 标）。
9. Report 4 题的人工结论与成本。
10. `B_nokg` 探针的结论。

**判据不可解析时保守处理**：任一硬判据因样本不足或 unknown 过多而算不出来，记「未验证」，**不记通过**。

**没有跑成时的口径**（§9.2 原文）：没有真实模型环境或预算不够跑完 G1 时，保留默认关闭，交付可重跑命令，明确写「质量验证未完成」，不得伪称收益已证明。

## 10. 验收

1. `bash scripts/check.sh` 全绿。`backend/tests/test_reflect_ab.py` 覆盖：gold 加载与缺 gold 的跳过（不计 0）、`anchors_on_gold` 三跳解析任一跳失败 ⇒ unknown、配对逻辑（`paired=false` 不进差值表）、样本门（`n < 5` 不出分位数）、`completeness_claim` 正则的真/假阳性 fixture、判据表在 unknown 过多时输出「未验证」而不是「通过」、`arm` 与反推 `policy_version` 不符时响亮失败。
2. **隐私守卫变异必红**：往投影里加一个 `answer` 键、或让判分请求带上证据正文，用例都要红。
3. `ab --dry-run` 在缩批（`--limit 2`）下打印出正确的 run 数、模型调用上界与产物落点；`--concurrency 2` 时打印「成本三键将为 unknown」的告警。
4. G1 全量跑通，产出 §8 的四张表 + 抽样工作表；`ab-judge` 与 `ab-report` 可在不重跑臂的情况下重跑。
5. 首份报告能逐条回答 §9 的 6 条硬判据，并对每条给出「达标 / 不达标 / 未验证」与支撑它的逐题证据。

## 11. 刻意不做 / 登记的债务

**刻意不做**：新判分服务；生产代码改动；rig 进标准门；CI 跑真实模型；成对偏好打分；p 值；Report 侧自动判分；Knowhow 与跨库原文设臂；`A_kg` 格（A 是单篇，其 KG 增益小，且 T0 的 A 侧对照就建在 `A_nokg` 上）。

**债务**（记在这里，不在本期做）：

| # | 债务 | 触发条件 |
| --- | --- | --- |
| D1 | §9.2 的「TX/RX/周期性限定」形态未覆盖 | 往 A 语料补 2 道限定条件题（零新语料成本），下一轮跑批时带上 |
| D2 | 「冲突材料」形态未覆盖 | 需要一份互相矛盾的语料；本期语料里没有 |
| D3 | 「混合/参考库」形态未覆盖 | 测试库挂基座会让 `seed` 成本翻倍，而策略无基座分支 |
| D4 | `backend/app/eval/reflect_t0/` 目录名已名不副实 | 等 PR #700 合入且不再有活跃分支引用时再改名，避免与未合入分支冲突 |
| D5 | 英文题面只在 G1 跑 | 若 G1 显示语言引入了与策略同量级的方差，再扩到全部组 |

## 12. 给实现的任务拆分

前置：**PR #700 必须先合入**。

| 任务 | 范围 | 验收 |
| --- | --- | --- |
| **T-AB1** gold 补齐 | `backend/app/eval/reflect_t0/questions.json`：B 10 题补 `gold_sources` + `gold_facts`，A 24 题补 `gold_facts`；`backend/app/eval/reflect_ab.py` 的 gold 加载与校验（≤3 条、≤40 字、短名唯一解析）；对应用例 | 全部 gold 条目人工核对过出处；缺 gold 的题在相关指标上跳过而不是计 0；加载器对畸形 gold 响亮失败 |
| **T-AB2** rig `ab` 子命令与投影 | `scripts/reflect_shadow_rig.py` 新增 `ab`；`AB_PROJECTION_KEYS` 与 §7.1 全部确定性键；§5.5 的 6 条硬断言；`--dry-run`；`.local` 落点 | `--dry-run` 用例进标准门；隐私守卫用例；`anchors_on_gold` 三跳解析的 unknown 路径有 fixture 覆盖；成本三键在 `--concurrency>1` 下强制 unknown |
| **T-AB3** 判分与出表 | `ab-judge`（§6 第三层，暴露面按 §7.3）；`ab-report`（§8 四张表 + 抽样工作表）；`reflect_ab.py` 的配对、样本门、§9 判据求值 | 判据在 unknown 过多时输出「未验证」；`model_judged` 列在判据表里出现在参考栏；判分 prompt 的 hash 与模型 id 进报告；判分请求不含证据/标题/臂标签（用例断言） |
| **T-AB4** 首份报告 | 跑 G1 全量（+ P1 探针），做人工抽样，出报告；`fangan_todo.md` / `fangan_done.md` 台账更新 | §10 全部验收；报告逐条回答 §9 的 6 条硬判据；开闸与否的建议连同「未验证」项一并给出 |

T-AB1 与 T-AB2 可并行；T-AB3 依赖 T-AB2 的产物形态；T-AB4 依赖全部。

## 13. 需要拍板的点

1. **要不要为「主库只读跑完整 Ask」付一次最小生产改动**（§5.3）？规格建议**不付**，走 `seed` 测试库。若用户认为 `seed` 的数小时不可接受，需要接受在 `_commit_reasoning_draft` 旁边多一条显式不落库分支。
2. **预算**：G1 全量 408 run（3.5–7 小时串行）+ 一次 `seed`。G2/G3 是否跑、跑多少（§4.3）。
3. **判分模型选谁**（§6 第三层）。理想是与被测模型不同的服务；若只有同一家，报告里要写明这一点。
4. **人工抽样谁做**：24 份 Ask 答案 + 8 份 Report 答案（4 题 × 2 臂）。
5. **`gold_facts` 由谁写**：34 题 × ≤3 条，需要读语料逐条核出处。这是 T-AB1 的主要工作量，也是整份 A/B 的精度上限所在。
6. **硬判据 6 的阈值**「中位数不得高出 1 轮以上」是本规格拟的，不是上游原文；上游只写了「没有系统性增加」。要不要换成别的阈值，请拍。
