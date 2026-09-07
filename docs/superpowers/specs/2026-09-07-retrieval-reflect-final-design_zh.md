# 检索 Agent Reflect 优化：最终设计与实施计划

日期：2026-09-07

状态：设计定稿，交由 Claude 实施；尚未实现、未开启生产新策略。

核对基线：`65b97406cddea6317a7f1b9fbf8dc45b707f54c7`。实施前重新检查 HEAD、工作区和当前 owning documents，按符号定位，不依赖本文的历史行号。

## 1. 最终决策与交付边界

保留「意图理解/确认 → 首轮检索 → 单动作 reflect 循环 → 最终选择 → 合成」架构。第一期提升模型判断下一步所用的信息质量，并消除不可执行动作导致的空转。检索质量优先于减少一次模型调用。

**本期必须交付：**

1. 无正文的离线轨迹统计，以及能评价真实 reflect 决策的独立 A/B 通道。
2. 修复被策略禁用的 `expand_community` 意外终止整个检索循环。
3. 每轮统一的可执行动作视图、按所选动作校验参数、统一动作观察账目。
4. 有界证据卡与指令/数据分层；维护近期动作意图，但不回放完整思考过程。
5. 轻量必答方面记录，区分「查询执行」「模型判断有支撑」「进入合成」「最终引用」。
6. 明确结束原因，贯通 Ask、Report 的合成和已有轨迹展示。
7. 新策略默认关闭，完成隔离测试与真实模型质量对比后再单独决定生产开闸。

**本期不交付：** 多动作并发、新增 `read_evidence`、原文段落联邦化、原生 tool-use/provider 改造、独立 critic 模型、自动改写 prompt 的 self-evo、低档位完整大纲、固定直答档位、先合成再检索。不得在实施过程中顺手实现这些路线。

本文是本次实现的设计输入，不替代当前产品契约。实现改变的行为和精确上限必须同步到对应中英文 owning documents；未实现路线不得登记为完成。

## 2. 两份分析的合并与取舍

输入包括本次 Codex 的仓库分析，以及用户指定的 [Fable 5.1 会话](https://claude.ai/code/session_01QciEUucxxVsv98utv1q9dP)。网页访问遇到登录门，实际阅读采用本地同步记录：会话标题「检索Agent反思流程优化方案」，问题原文与用户本次原始任务一致，模型字段为 `claude-fable-5-1`，分支为 `claude/retrieval-agent-reflect-optimization-3a5be5`。未直接验证网页内容与本地记录的逐字一致性；不把浏览器访问成功作为分析依据。

| 建议 | 最终取舍 | 理由 |
| --- | --- | --- |
| 先测量轮数、skip、fallback、引用贡献 | 采纳，作为第一项交付；不等待无限量线上样本才修明确缺陷 | 已有轨迹能给出部分基线，缺字段必须报告 unknown |
| 新增证据提供较完整摘录，旧证据压缩 | 采纳；加入必答方面、当前绑定和来源多样性保底 | 仅扩大所有条目的前缀会增加成本，仍可能漏掉限定条件 |
| 最近若干轮 reason 与统一动作账回喂 | 部分采纳；保留短的动作目的和观察，标为历史模型判断 | 原始 reason 不是事实，盲目重放会固化错误判断和材料注入 |
| 配额耗尽即移除工具 | 采纳，并合并部署、调用方、范围和剩余步骤限制 | 三处独立维护容易出现 prompt 允许但执行必然 skip |
| 禁用社区扩展时不应 break 整个循环 | 采纳，独立缺陷修复 | 跳过一个通道不应放弃其它仍可执行的通道 |
| system/user 拆分 | 采纳，但按语义变更测试 | role 变化会改变指令优先级，不能声称仅搬字符串、模型行为等价；缓存收益待测 |
| 一轮多个动作/多个子查询 | 后续独立立项，本期继续单动作 | 先改善决策；并发会改变预算、去重归因、失败和取消语义 |
| 轻量方面与证据记录 | 本期采纳最小版本，不演化为低档位大纲 | 「检索过」不等于「已经回答」，又无需新增模型调用 |
| `read_evidence` 与跨库原文 | 后续独立立项 | 都有价值，但前者改变读取面，后者牵动参与库配额与跨库引用 |
| 新行为只影响无图 run、有图保持不变 | 不采纳为开闸后的约束 | 名称级摘要等问题同样存在于有图 run；兼容由总闸关闭实现，开闸后两类均测试 |

不采用历史测试数量、注释中「第十一个动作」等易过期计数作为合同。上限与能力以执行代码和实时产品参考为准。

## 3. 已核实的现状

### 3.1 调用链与状态

- 正式 Ask UI 携带意图契约时，`_first_round_plan` 采用确认方向，跳过第二次 plan 模型调用。没有契约的兼容路径才调用 `plan_with_keywords`。
- `_run_first_round` 保持现有次序：无图披露、背景/地图、目录播种、规划、初检索、PPR、精查、无图原文、全空兜底、方向补种。PPR 预取与首轮子查询已经有并发；无图 chunk 播种也已使用线程池，不能把旧待办里的「当前串行」当作实施前提。
- reflect 使用 `reasoning_agent.chat_json` 返回一个动作，代码负责执行；不是外部 MCP 工具目录，也没有在这里使用 provider 原生 tool calls。
- 每轮重新投影服务端状态，不携带完整对话历史。`reason` 当前进入轨迹，不作为下一轮独立工作记忆。
- `_summarize` 的 KG 行主要是类型、名称和 ID；元素/chunk 主要是位置与前 80 字符。头尾窗口保留新证据，但不能保证问题相关片段被展示。元素侧已有与合成选择对齐的多样性处理，应保留。
- 方向账证明是否执行和新增量；stale 主要由候选数量是否增长决定。大纲变化不算检索进展，纯大纲动作仍可推进空转计数；成功送达新建议的 consult 轮已有专门的计数持平规则。不要把「中性」误实现为所有大纲轮均不计空转。
- 普通 Ask/Report reflect 错误默认 fail-open，并有 `fallback_reason`；Knowhow 补全使用 `fail_closed=True`。取消不能被降级吞掉。

### 3.2 动作及实际能力

| 动作 | 作用 | 首期保持的边界 |
| --- | --- | --- |
| `answer` | 结束检索，交后续合成 | 本动作不生成答案正文 |
| `add_subquery` | 补充方向，含 types/prefer | 联邦 KG；无图时沿现有路径补本库原文；确认方向身份不能被简称绕过 |
| `search_chunks` | 原文段落检索 | 本库、来源上限、MMR 与现有候选策略；默认 3 次动作，seed 另计 |
| `search_elements` | 检索 SourceElement | 当前调用为本库；可含 paragraph 等，不只公式/表格；大库可通过 chunk 候选回灌元素 |
| `exact_lookup` | 精确名称定位及有界整节取值 | 本库、名称准入、解析结构限制、seed/动作分账 |
| `expand_graph` | 一跳邻居 | 本库；visited 与邻居截断披露 |
| `ppr_retrieve` | 图传播召回原文 | 依赖开关、调用方及安全范围，动作与 seed 分账 |
| `expand_community` | 解析 peers 并补查 | 挂载参考库、焦点防重、共享参与范围 |
| `follow_chain` | 有据的类型化两跳推导 | 合法起点、关系类型/适用范围；不写回 KG |
| `enumerate_elements` | 有界元素枚举 | kind 白名单；`collection=sources` 为目录兼容入口 |
| `enumerate_kg_objects` | 有界知识对象枚举 | 需图、类型白名单、来源上限 |
| `consult_memory` | 检索打法建议 | 不是私有 Memory 证据；deep 及以上、经验注入打开且有消费后续轮次 |
| `update_outline` | 大纲和证据绑定 | 仅 exhaustive；全量结构、绑定并集/显式删除、overflow 纠错规则 |

无图时去掉五个图动作。枚举还有接线与范围门。部分其它动作的禁用和耗尽目前只在执行处分支处理，正是本期统一的对象。

### 3.3 三个消费者

- **Ask**：接完整能力；外层单独召回已确认私有 Memory，后续还可能激活所选来源图；单次/按节合成继续各守现有引用合同。
- **Report**：逐节复用 retriever；已确认节方向不可被重规划替代；当前不接集合枚举；run 后仍有方向元素补取与部分未执行方向兜底，不能误删或重复计入 run 预算。
- **Knowhow**：过滤私有 Memory 与当前表投影；关闭多种通道，严格失败。新策略第一期不接入此调用方，但社区禁用的独立缺陷修复适用它。

站外缺口建议仍在 Ask 草稿之后运行，不进入 reflect/证据/正文。外部 Agent MCP 与本期工具注册结构分别拥有自己的目录，不得合并。

## 4. 配置、预算与兼容决策

新增验证过的 Settings：

| 字段/环境变量 | 初始默认值 | 校验与用途 |
| --- | --- | --- |
| `reasoning_reflect_v2_enabled` / `REASONING_REFLECT_V2_ENABLED` | `false` | Ask 与 Report 新策略总闸；不是前端档位 |
| `reasoning_reflect_evidence_chars_by_effort` / `REASONING_REFLECT_EVIDENCE_CHARS_BY_EFFORT` | overview 4,000；standard 6,000；deep 8,000；thorough 12,000；exhaustive 16,000 | 必须恰含五个 effort；各值为整数 1,000–64,000，随档位不递减 |
| `reasoning_reflect_excerpt_chars` / `REASONING_REFLECT_EXCERPT_CHARS` | `240` | 整数 80–1,000；单证据原文摘录上限 |
| `reasoning_reflect_state_chars` / `REASONING_REFLECT_STATE_CHARS` | `6000` | 整数 1,000–32,000；可压缩观察/历史建议的总投影预算 |
| `reasoning_reflect_recent_observations` / `REASONING_REFLECT_RECENT_OBSERVATIONS` | `6` | 整数 1–20；近期动作观察最多行数 |

这些是初始质量/成本预算，必须先登记到 paired product/API 数值表，再在配置参考写环境变量用法与验证。实现不允许在调用点复制字面切片。映射配置提供可直接使用的 JSON `.env` 示例，并测试非法 key、缺 key、bool 冒充整数和非单调值。

证据预算独立于最终 `kg_context_chars/chunk_context_chars`，不能简单取其比例：反思和写答案需要的信息粒度不同。预算包含证据卡的元信息与省略标记；本轮没有用满的证据预算不用于增加工具次数。

`state_chars` 只界定可压缩的观察和历史建议。用户完整问题、已冻结约束/必答主题、当前合法动作与额度、完整大纲/overflow/枚举覆盖等必要状态分别按现有输入/协议边界保留，不得整体字符串裁尾截掉。可压缩区装不下时明确省略条数。这样总输入由各个具名界之和限制，不宣称整个 prompt 只有 6,000 字符。

关闭总闸：沿用旧 prompt/schema、旧调用次数、旧证据选择、旧 trace 键集；不构造新状态，不增加 I/O。**唯一例外**是 §8 的社区禁用缺陷修复，它有单独回归、明确改变旧错误行为，不用兼容要求保存缺陷。

开启总闸：Ask 与 Report 的有图/无图两类 run 均使用新策略。Knowhow 显式保持旧策略，不能仅依赖「当前没传 limits」的偶然性。历史响应缺新字段照常可读。

不自动开启经验库注入、PPR 或其它既有开关。原有步数、工具次数、seed 额度、首轮宽度、枚举行/页预算都保持；本期不增加每轮批量动作，也不提高模型重试次数。

## 5. 每轮动作能力与参数合同

### 5.1 单一能力投影

在既有 `_ReasoningRunState` 上追加必要的结构化状态，生成不可变的 `ReflectCapabilities`。职责分工：

- 领域层保存纯数据结构和动作 ID/参数形状；服务层持有执行器、文案与运行时策略。`core/models` 不得反向 import services。
- 各动作定义声明所需参数、是否产生证据、预算类别、重复身份和依赖条件。
- 一次纯内存投影同时供 prompt、schema 枚举和解析白名单消费；复用已知图/范围/接线事实，不为每个动作新做数据库探测。
- 执行前继续使用当前 scope/actor/cancellation 的真实校验，快照不成为绕过实时权限的凭据。范围漂移与权限失效沿用现有 fail-closed/StageBoundaryError 边界。
- 描述/参数分支与动作一同移除；已耗尽动作不留下可填写的孤立参数。可显示有界的不可用原因以便模型换通道。

需要覆盖的条件包括图存在、`allow_*`、部署开关、来源限制、剩余动作次数、枚举可续跑预算、outline 正常与纠错额度、consult 是否还有下一轮。图仅存在于参考库但 expand 只支持本库时，候选起点也要按已持有的来源身份验证，不能把不可展开的跨库起点作为有效选项。

只有 `answer` 可用时，可由服务端直接以预算/能力原因收尾，不再请求一个只能返回 answer 的模型；不额外伪造一次 reflect 或“模型判定充分”。若还有正常额度，既有终态 overflow 修复优先级保持。

### 5.2 新策略的 JSON 形状

保留 `chat_json` 传输与现有 13 个动作 ID。新策略使用单一 `arguments` 对象，避免在同一模板中展开所有互斥分支：

```json
{
  "next_action": "search_chunks",
  "sufficient": false,
  "arguments": {"query": "set_db -value 的默认值与适用条件"},
  "assessment": {
    "supported": [{"aspect_id": "a1", "evidence_keys": ["实际展示过的证据键"]}],
    "unresolved": [{"aspect_id": "a2", "status": "partial", "gap": "尚缺适用条件"}]
  },
  "reason": "补查第二个必答方面的适用条件"
}
```

这个例子只是结构说明，不得把其中的示例键或问题文本写入运行时模板。

- 通用 scheduler 的 example 校验继续负责 JSON 基本形状；所选动作的精确参数由 reasoning 层的类型化校验负责，不能误把 example 当完整 JSON Schema。
- `arguments` 映射：三种自由检索动作使用 `query`（add_subquery 另含 types/prefer）；精查使用 `term`；图展开使用 object_id/edge_type/direction；community 使用 focal；链使用 start_object_id/target_object_id/edge_type/direction；枚举使用现有 kind/object_type/collection/source_title/source_id；outline 使用 sections；answer/consult 必须为空对象。
- 校验后的对象适配到现有 `ReflectDecision` 和执行器，不能复制第二套工具执行代码。枚举 kind/object_type 白名单继续引用 collection_catalog 的唯一真源；`collection=sources` 的现有优先语义不改。
- 真 boolean 校验；检索动作携带 `sufficient=true` 为矛盾决定，绝不静默跳过其检索后宣称充分。answer 可带 false 表示部分收尾；outline + sufficient=true 保留先应用绑定、再按既有规则收尾/纠错的能力。
- 模型返回本轮已禁用但可识别的动作、缺参数或矛盾字段：记 invalid/unavailable observation，不做工具 I/O，走统一步数/stale 记账。仍有预算时由下一轮决定其它动作；不插入额外的隐藏修复调用。
- provider/JSON 协议在既有重试后仍失败，沿用 fail-open/fail-closed合同；新终态标为 degraded，不能解释为模型认为充分。AskCancelled 与阶段边界异常始终抛出。

参数 schema 不对最终 Ask/MCP 请求新增字段；属于内部模型决策版本。测试和文档同时注明 legacy/v2 两种模型载荷，不让 mock 偶然决定生产协议。

## 6. 统一观察与证据视图

### 6.1 动作观察账

新增 run 内 `ActionObservation`：序号、阶段 seed/action、动作、规范化请求身份、短目的、执行状态、returned/new/upgraded 数量、已知截断、稳定原因码、剩余预算。详细证据 ID 留在 request memory；写 trace 的投影继续有界。

执行状态至少区分 `success / empty / duplicate / unavailable / invalid / failed / partial`。空结果不是执行失败，新增为零不是返回零，未执行不能记为已检索。

- 首轮与后续动作共用观察转换；首轮 seed 不假装是模型主动采用的工具。
- 新观察由实际执行结果产生；不能从模型 reason 或自然语言 trace 反解析。
- 新 prompt 用统一观察替代重复的散文回喂，但 visited、方向身份、精查名称、枚举 cursor/coverage、outline/overflow 各自仍由原有状态拥有。不要新建可与旧状态分叉的第二份权威账。
- 防重复只对「同动作、同规范化参数、同有效范围、先前成功完成」的请求生效；types/prefer/方向也参与身份。不做语义近似判重。失败请求允许按原剩余配额再尝试。
- 枚举重复请求可能是 cursor 续跑，不能按普通重复搜索拦截；partial/hydration upgrade 不能伪装为全空。精查与确认方向继续复用既有身份归一化。
- 最近若干观察中的 reason 作为“当时的动作目的”回喂，受 state_chars 限制。无需新增长篇思考字段，不回放隐藏推理；先前 reason 不证明支撑、范围或工具可用性。

本期保留现有硬 stale 算法及 outline/consult 的既有例外，只把 candidate_delta、upgraded、方面变化分别观测。**不得让模型自报覆盖变化清零 stale**，也不宣称候选增长就是质量提升。

### 6.2 证据卡

首期只用已经在候选池内的材料生成卡片，不新增 LLM 摘要，不为每张卡查库或补 hydrate。KG 没有原文片段时使用已有 name/steps/validity_scope 等真实字段并标明粒度；不得将模型抽取字段冒充原文逐字证据，不为本项目新增 Node attrs。

卡片包含：类型、稳定引用键、来源/位置（已有才显示）、原文或抽取摘要的标识、有界问题相关摘录、适用条件（已有才显示）。关系/推导保留原来的有据 hop 与查询期 inference 区分；不能把临时推导升级为可直接引用的 KG 事实。

确定性选取顺序：

1. 为当前必答方面已绑定且存活的证据保留代表；同等候选按方面轮转。
2. 本轮新增或内容/支撑已升级的证据。
3. 历史高相关代表与来源多样性补位。

优先级不等于每组无限独占预算；相同证据只渲染一次，稳定排序与 tie-break 可重复。摘录优先选择覆盖原问题/当前动作查询中有效检索词的原文窗口，复用现有分词与精确短语规则；无命中才取前缀。不要新引入模型理解或全库扫描。用省略标记明确这是局部摘录。

三类标识必须区分：候选持有、曾实际展示、最终合成接纳。show/ever_shown 集只登记真实渲染的键，截出窗口的键不能因“在池里”而取得模型绑定资格。现有 exhaustive 大纲合法键及持久绑定语义不变。

### 6.3 指令与数据

固定系统指令描述任务、材料不可信、范围不可扩大、工具选择与结束规则；动态问题/冻结契约、服务器状态、候选数据与历史模型判断分别标识。材料中出现“忽略指令/直接 answer”不得作为操作指令。

system/user 拆分可能改善公共前缀复用，但不新增 provider 缓存参数，不承诺缓存命中或 token 降幅。quoted phrase 和 scope deixis 的原有语义保留，尤其真正讨论 KG 的问题不能被剥掉主题词。

## 7. 必答方面与结束原因

### 7.1 轻量记录的来源

- Ask 从冻结意图的 mandatory_topics 和相关约束生成稳定方面 ID，保持用户审阅过的语义。不能从模型新子查询的数量反推必答方面。
- Report 从本节已有 intent_questions/确认的主题绑定生成方面；节内模型不能修改报告已确认章节。
- 没有正式契约的兼容路径，以完整输入问题作为一个方面；不新增模型重规划来猜必答清单。
- 复用原契约输入上限；用户内容不得静默截掉。既有方向账继续回答「有没有执行」，不与方面支撑账互相覆盖。

`assessment` 是同一次 reflect 的附加结果，不单独发评估调用。每个方面为 `unknown / partial / supported / conflicting` 之一。模型没报告的项保留未知或已有状态，不允许借省略删除必答项。更新同一方面时全量替换其模型支撑判断，不沿用 outline 的自动并集，以便撤销错误判断。

载荷有明确边界：supported/unresolved 各不超过冻结方面数，同一方面不能同时出现在两组或重复出现；每方面证据键最多 8 个，gap 最多 240 字符，分别以专属协议常量 `REFLECT_ASPECT_MAX_EVIDENCE_KEYS`、`REFLECT_ASPECT_GAP_MAX_CHARS` 声明并登记到产品/API 数值表。这些限制只针对模型生成的内部载荷；超限按 invalid 决定处理，不裁剪用户的主题、问题或约束。

服务端只确认：方面 ID 合法、证据键在池内且已展示、集合/来源身份不能冒充细粒度证据。非法键剔除后没有支撑的项不得保持 supported。模型对语义支撑的判断始终标为 `model_assessed`；不新增 grounded 分数，不改变 evidence_level 阈值。

取舍记录（T4-A 实施时定，写下来免得下一次又被当成遗漏）：**约束（constraints）不各自成为一个方面**，只随方面块一起渲染。第一行写的是「从 mandatory_topics 和相关约束生成稳定方面 ID」，实现时按前者建方面、把后者作为整块的约束行带出去。理由是约束是**答案的谓词**（「只看 7nm」「只用 2024 年以后的材料」），不是可以被证据独立支撑的检索对象：给它一个能被标成 supported 的 id，模型无从为它引证据键，于是它永远停在 unknown，`unresolved_aspect_ids` 恒非空，`model_sufficient` 结构上不可达，而这两件事下游都当真。约束仍然进 prompt（每轮渲染在方面块末尾），只是不参与「有没有支撑」这本账。

来源/元素完整枚举的 complete 继续只由确定性执行器证明。方面全 supported 不能据此声称枚举了全部物理集合；反之完整目录也不能证明每篇文章已被分析。

私有 Memory 的外层召回、所选来源图和 Report run 后补取当前发生在这一循环之外。首期不为它们增加检索器读取座位；终态模型评估明确只覆盖当时可见候选，不能据其否定后续新材料，也不能自动把后续候选记为已经反思验证。

### 7.2 结束状态

由服务端生成 `RetrievalTermination`，至少有 `reason / unresolved_aspect_ids / model_assessed_sufficient`。reason 使用闭集：

| reason | 触发 |
| --- | --- |
| `model_sufficient` | 模型明确结束且自报充分；方面记录没有已知未解决/非法支撑矛盾 |
| `model_partial` | 模型选择结束但仍有未知、部分或冲突；或自报充分与方面记录矛盾 |
| `step_budget` | 正常步骤预算耗尽 |
| `stale` | 现有无进展熔断触发 |
| `no_executable_action` | 除 answer 外无可执行动作 |
| `model_degraded` | 模型/JSON 在已有重试后仍失败而降级 |
| `retrieval_degraded` | 检索器/工具异常触发调用方现有 fail-open 收尾，证据收集未正常完成 |

取消和不可恢复阶段错误仍是异常/取消，不落入“成功生成 termination”以掩盖失败。收尾前的 outline overflow 修复沿用原规则；修复不能把既有 stale/预算原因改成充分。

单个可恢复工具失败若随后继续完成检索，只作为 observation 留存，不强制把整个 run 标为 retrieval_degraded。异常终止优先记对应 degraded 原因；否则保留真实首先触发的终止条件，不在最终步骤号等于 max_steps 时覆盖同轮模型已经作出的正常结束决定。

`retrieval_degraded` 的精确判据（T4-A 复审裁决）：**这次 run 没有模型正常结束标记（trace 里第一个终止标记不是 model_end），且时间线上最后一次真实 I/O 执行的观察是 `failed`**。两个条件都要。按「任一通道最后一次执行 failed」判会把跨通道恢复（KG 播种炸掉 → 模型改走 search_chunks 查全 → 自报充分）误标成整次降级，而上一段说的「继续完成检索」并不要求是同一条通道；反过来，最后一次去查时查不动、随后走 stale/预算收尾的，`retrieval_degraded` 仍然盖过 stale/step_budget。没有被恢复的通道另记 `RetrievalTermination.unrecovered_channels`（元组，口径是「该通道最后一次执行仍是 failed」，按首次出现排序），只用于披露，不参与 `reason`：「哪条路没走通」与「这次检索有没有正常收尾」是两个口径，不互相冒充。

把类型化终态经 `ReasoningResult → ReasoningEvidenceSnapshot → ResponseDraftInput` 的实际使用链传递，保留 evidence 对象身份和不可变 envelope 规则。跨层 DTO 放到允许的依赖层；需要扩 application import allowlist 时只增加具体模块并同步架构守卫，不放宽整包。

Ask 单次/按节合成与 Report 节撰写获得这份事实说明。最终装配之后，使用实际进入 prompt 的证据身份复核方面支撑：如果所引用支撑全被预算/过滤移除，应降为“未送达”，不能继续显示“已支持”。不要求每轮提前执行最终重排，也不为复核再读库。

记录 `model_supported / synthesis_admitted / answer_cited` 为不同口径；进入 prompt 不能证明答案引用正确，真实引用仍由现有绑定与 grounding 路径验证。后续补取解决了缺口，可以在最终合成中使用，但不能改写早先 reflect 已停止的历史事实。

UI 复用已有实时 trace 与完成后的合成说明，显示简洁中文结束原因、已处理/待补方面计数，必要时说明未送达或部分完成；不要给普通回答增加固定错误横幅。历史 trace 无新字段照常渲染。通过既有私有 Ask/Report 持久路径存储，不扩大公开分享白名单，不把完整 assessment 自动加入公开会话。

## 8. 社区扩展缺陷的单独修复

`run` 中模型选择 `expand_community` 后，`allow_community_expansion=False` 或安全范围不允许时，当前分支记录 skip 随后 break 整个循环。

修复为本动作 `unavailable`：零社区/检索 I/O，经过与其它 skip 相同的 no_progress/stale/步骤记账；若尚未熔断，下一轮仍能选择允许的工具。**不能简单改成裸 continue** 绕过末尾记账，否则反复非法请求可以规避 stale。

异常分类：通道本来禁用是可恢复 skip；实时权限/阶段绑定错误仍按原异常合同终止。此修复不允许对已禁用范围先执行再过滤。

测试至少覆盖 Ask 的受限来源、Knowhow 的调用方策略、连续重复被禁动作触发熔断，以及“社区被拒 → 下一轮元素检索成功 → answer”。legacy/v2 都要覆盖，明确这是关闭态等价要求的唯一主动修复例外。

## 9. 测量与验证方案

### 9.1 离线轨迹统计

新增 `scripts/analyze_reasoning_trace.py`，默认只读用户指定的本地 trace 导出，不自动读取生产数据库，不调用模型/embedding/网络，不启动服务。业务投影优先复用 `retrieval_experience_projection` 和领域 closed projection；不直接把完整 trace/异常写到普通输出。

输出按模式、档位、可获得的图状态与策略版本聚合：样本数、reflect 次数、动作分布、skip 原因、fallback、stale、结束原因、可获得的耗时，以及能可靠归因的引用贡献。

必须声明：

- 旧记录没有某字段即 unknown，不把不存在当 0/false。提供每个指标的可观察样本数/缺失数。
- 未记录/截断 result_ids、缺最终锚点或 trace 被截断时，动作引用贡献为 unknown；不要拿整轮 citations 代替该动作贡献。
- trace duration 常是相邻记录的墙钟差，不能无条件当成单次 provider 或工具执行时长。
- 不输出问题、来源标题、证据、actor/notebook/source ID、模型 reason 或异常原文。JSON 明细仅含闭集维度和数值。

至少用合成 fixture 验证 legacy 缺字段、空数组、截断、seed/action 分离和共享证据归因。现有 `replay_retrieval.py` 保持其确定性回放定位，不把固定 answer 的结果宣传为 reflect 质量 A/B。

### 9.2 真实决策 A/B

另建显式运行的评测入口与固定、无敏感数据的语料/题集。两组使用同一模型服务版本、冻结语料/索引、范围、effort 与既有 action budget，仅切 legacy/v2。保留至少 30 个案例，各运行 3 次以观察方差；这属于评测夹具规模，不是生产硬限制。

覆盖：单事实；参数默认值与条件；TX/RX/周期性限定；两个对象比较；冲突材料；来源目录与类型清单；同标题/同内容；新证据关键句在前 80 字之后；首轮空手；工具耗尽；范围缩小；无图库；混合/参考库；超预算；模型坏 JSON；outline overflow；Report 方向补取；Knowhow 未接新策略。

评测区分：正确回答、必答方面遗漏、引用支撑、完整性虚报、无效/重复工具调用、过早停止、总调用/token 和端到端 P50/P95。外部判分模型若使用，只作辅助；限定词、完整性与引用由固定 gold 及人工抽样核对。

开闸候选标准：权限/引用/完整性关键回归零新增失败；受影响目标题的改善有逐题证据；简单题没有系统性增加 reflect 调用；报告成本与延迟变化如实列出。不得只凭整体平均分掩盖某一类题退化。没有真实模型环境时，保留默认关闭并交付可重跑命令，明确未完成质量验证，不伪称收益已证明。

### 9.3 标准验证

实施阶段先跑聚焦用例，再执行仓库要求的 `bash scripts/check.sh`。按环境使用：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

普通门保持 hermetic，不依赖宿主端口或外部服务。真实 A/B 不进入标准门。若触及双后端存储，必须增加对应 parity；本期默认不新增 schema/迁移，也不为轨迹统计造数据库新端口。PostgreSQL 扩展通道只按开发文档的显式测试环境运行。

## 10. 给 Claude 的实施任务顺序

按以下范围实施；每个任务交付可审阅的小改动和聚焦证据，不把全部工作塞进 run。已批准的多步骤实施遵循 `docs/development*.md` 的代理分工与验证规则；Claude 专属模型选择按其本地规则执行。

| 任务 | 文件归属/建议落点 | 必须验收 |
| --- | --- | --- |
| T0 基线与评测夹具 | `scripts/analyze_reasoning_trace.py`、`backend/app/eval/`、对应 tests、`scripts/README.md` | 无服务/无正文输出；unknown 与截断正确；固定 answer replay 不冒充决策评测 |
| T1 社区禁用修复 | `reasoning_retrieval.py` 的对应单分支、`test_reasoning_retrieval.py`、相关范围/Knowhow 用例 | 被拒动作零 I/O；后续合法检索可达；重复非法动作正常熔断 |
| T2 能力与协议 | `core/config.py`、纯 DTO、建议 `services/reasoning_actions.py`、prompts、reasoning 适配 | flag off 等价；所有能力组合 prompt/schema/执行一致；参数别名与布尔冲突；不扩 MCP/Provider |
| T3 观察与证据卡 | 建议 `services/reasoning_observation.py`、`services/reasoning_context.py`、`_ReasoningRunState`、prompt_layers | 状态单 owner；不增加 DB/LLM；近期/绑定/方面保底；真实展示键；材料注入用例；预算省略披露 |
| T4 方面与终态 | 类型化 DTO、`application/ask_reasoning.py`、retriever、Ask、Report、相关前端 trace renderer | 一次 reflect 内评价；不可删必答项；非幻觉证明；实际 prompt 接纳复核；结束原因持久化与重开；公开分享无扩大 |
| T5 全链路验证与文档 | paired product/API、deployment、development，architecture，相关 tests/评测报告 | 全标准门；legacy/无图/有图/受限/Report/Knowhow 矩阵；默认关闭；真实 A/B 证据或明确未验证 |

T1 可在 T0 统计样本尚不充分时完成；T2→T3→T4 有依赖。T0 先交框架/合成 fixture，真实 baseline 与 T5 A/B 使用同一题集。不要把“先测量”变成无限等待，也不要跳过最后的质量验证。

新文件名为建议落点，可以按现有模块边界合理调整；动作/预算/终态等上述行为合同不可擅自扩大。`run`、`_new_run_state` 等热函数已在架构脚本中受长度棘轮约束，应抽取有清晰输入输出的纯 helper/执行器；不得提高上限来容纳新增代码。缩短后同步降低 baseline。

实现时更新全部真实改变的 owning documents，尤其：

- `docs/product-and-api.md` 与 `_zh.md`：新模型动作协议、结束/覆盖口径、预算表、显示行为。
- `docs/deployment-and-configuration.md` 与 `_zh.md`：开关、映射环境变量、校验和回退。
- `architecture.md`：新增职责与状态拥有者；保持已有 application/extension/repository 边界。
- `docs/development.md` 与 `_zh.md`：只更新真的改变的开发/验证合同，不复制产品叙述。
- `docs/ui-vocabulary.md`：只有新用户术语需要归档时更新；界面显示中文短文案。
- `fangan_done.md/fangan_todo.md`：只在标准门通过后更新实际完成条目；本期不宣称 read_evidence、跨库原文或多动作完成。

## 11. 延后路线的重新进入条件

1. **`read_evidence`**：当逐题分析证实“找到证据但摘要不足、反复搜索”仍是主要失败类型时独立立规格。只接受服务端已签发且在冻结范围内的引用，优先读内存，后续有界详情读取；不是重新引入模型 source_refs 确认门。
2. **多子查询批量动作**：当有效补查的串行模型往返是主要延迟来源时先评估 add_subquery 批量，而非任意多工具组合。规格必须按实际动作数量扣预算、确定性顺序合并、保留逐动作归因并处理局部失败/取消。
3. **跨库原文**：按既有待办单独解决参与库候选配额、scope、引用与资产解析，不能用移除 notebook-local 过滤冒充联邦化。
4. **终态独立 critic/更强语义停机**：必须先用本期数据证明收益；不能因为循环中有 assessment 就再加一次固定模型评审。

## 12. 参考与核对入口

实施以当前代码和 owning documents 为准。以下为此次核对入口：

- [检索器](../../../backend/app/services/reasoning_retrieval.py)：`reflect`、`_summarize`、`_run_first_round`、`run`、各动作执行器。
- [模型提示](../../../backend/app/services/prompts.py)：`reflect_prompt`、`reflect_schema_hint`；[prompt 分层](../../../backend/app/services/prompt_layers.py)。
- [档位策略](../../../backend/app/core/ask_retrieval_policy.py)、[配置](../../../backend/app/core/config.py)。
- [Ask](../../../backend/app/services/ask_service.py)、[类型化阶段](../../../backend/app/application/ask_reasoning.py)、[Report](../../../backend/app/services/report_engine.py)、[Knowhow](../../../backend/app/services/knowhow/api.py)。
- [经验投影](../../../backend/app/services/retrieval_experience_projection.py)、[领域投影](../../../backend/app/domain/retrieval_experience.py)、[确定性 replay](../../../scripts/replay_retrieval.py)。
- [产品/API](../../product-and-api_zh.md)、[开发合同](../../development_zh.md)、[架构](../../../architecture.md)、[未完成路线](../../../fangan_todo.md)。
- [ReAct 原论文](https://arxiv.org/abs/2210.03629)：动作与环境观察交替；本方案借鉴循环，不要求特定框架。
- [Anthropic 工具设计](https://www.anthropic.com/engineering/writing-tools-for-agents)：清楚的功能边界、有用且有界的观察、可指导修正的错误。
- [Anthropic Agent 模式](https://www.anthropic.com/engineering/building-effective-agents)：简单可组合工作流与按测量结果引入 evaluator/并发；不把框架迁移作为优化前提。

交接要求：先读本文、核对基线和当前工作区，在隔离 linked worktree 实施 T0–T5；遵循已有权限与开发规则。此文只授权设计所列本地实施范围的说明，不作为部署、对外消息、创建 PR 或合入的独立授权。
