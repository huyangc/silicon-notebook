# 方案：插件提供的 reflect 动作（`ask.reflect_action` 扩展点 + 外部证据类）

状态：设计定稿待实施（2026-09-13）。
上游背景：插件化 X 路线 **X6**（「有真实消费者才重开扩展点」——`agent.tool_provider` 在
#569 因零消费者删除）与 **PR-B v2**（#596 登记的下一步：把 `ask.gap_consult` 演化成模型
自选的 reflect 动作）。本方案就是那个真实消费者：部署插件向检索 Agent 提供一个**函数**
（典型是互联网/文献检索），其名称、作用与参数描述先进入 reflect 的提示词、schema 与白名单，
由模型在库内通道反复空手之后自行决定是否调用；返回的材料以**外部证据类**进入合成，可用
`[k]` 引用，前端渲染为带「外部」标识、指向 URL 的引用。

与 `ask.gap_consult` 的关系：gap_consult 是确定性触发、草稿之后调一次、产出不进答案；本点
是模型触发、循环之内调用、产出进答案且可引用。两者 v1 并存，退役与否见 §十一。

## 一、目标与非目标

**目标**

1. 插件用一份**描述符**注册一个 reflect 动作；核心从这一份描述符同时生成 prompt 动作行、
   schema hint 分支、`allowed_actions` 白名单条目与参数解析规则——「一处描述、三处同步」
   是 `reflect()` 既有纪律（`kg_actions` 闸的注释写明三处任一不同步的后果）。
2. 没有插件、或插件不可用、或档位/预算把它关掉时，reflect 的 prompt/schema/白名单
   **逐字节不变**（`enumeration`/`outline`/`consult_memory`/`search_chunks`/`kg_actions`
   五把 run 级闸的同一原则）。
3. 插件结果经核心铸键成为**外部证据**：进合成上下文、可被 `[k]` 引用、锚点/引用带 URL、
   前端显示「外部」标识与打开链接，一眼能与库内证据分开。
4. 插件拿不到任何核心端口：只收问题、已校验的参数、取消令牌与 deadline；硬 deadline、
   净化、预算、去重全部由核心宿主拥有（照抄 `extensions/gap_consult.py`）。

**非目标（v1 登记，不是遗漏）**

- **报告与 Knowhow 补全不提供插件动作**。两者各自构造 `ReasoningRetriever` 调 `run()`，
  v1 不传 `plugin_actions`，与 ask.engine「报告不接」同款边界；报告逐节深挖一节一 run，
  外部调用次数会随节数放大，需要单独的预算合同。
- **不给插件任何库内材料**：不给候选文本、不给子查询清单、不给 gap phrases。插件看到的
  外泄面只有问题与模型写的参数（§九 有专门的透明与红线条款）。
- **参数只有字符串与字符串枚举**。`model_json._validate_against_example` 对布尔示例硬类型
  校验（模型答 `"true"` 整轮反思作废），字符串示例享受空串宽容；整数同理不开。
- **不进经验库**：`retrieval_experience_projection.RETRIEVAL_ACTIONS` 闭集不动，投影层对
  未知 `step_type` 本来就跳过；零命中计数只在 run 内生效。
- **公开分享页不带 URL**（只带 `is_external` 标记），见 §七。
- **不做前缀缓存适配**：reflect V2 已退役（`df14096cb`），Legacy 每轮整段重建 prompt，
  按轮开闸不影响任何缓存合同。

## 二、SDK 契约（新文件 `backend/app/extension_sdk/reflect_action.py`）

新扩展点 `ask.reflect_action`，contribution kind = `CONTRIBUTOR`，**一条 contribution =
一个动作**（不是一个提供多个动作的 provider——可用性探针、预算与轨迹都按动作计，一条
一个最简单）。

```python
ASK_REFLECT_ACTION_POINT = "ask.reflect_action"

@dataclass(frozen=True, slots=True)
class ReflectActionParameter:
    name: str                 # ^[a-z][a-z0-9_]{0,23}$
    description: str          # ≤ REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS
    kind: Literal["text", "enum"]
    values: tuple[str, ...] = ()   # kind=="enum" 时 ≥ 2 个值，每个值 ^[a-z][a-z0-9_]{0,31}$
    required: bool = False

@dataclass(frozen=True, slots=True)
class ReflectActionDescriptor:
    name: str                 # 模型看到的动作 id，^[a-z][a-z0-9_]{2,31}$，见 §四 冲突规则
    description: str          # 一段「做什么、返回什么」，≤ REFLECT_ACTION_DESCRIPTION_MAX_CHARS
    source_label: str         # 引用徽章上的来源词，如 "IEEE Xplore"，≤ 40 字符
    parameters: tuple[ReflectActionParameter, ...] = ()   # ≤ REFLECT_ACTION_PARAMETERS_MAX
    max_calls_per_run: int = 1                              # 与策略上限取小

@dataclass(frozen=True, slots=True)
class ReflectActionAvailabilityContext:      # I/O-free，镜像 GapConsultAvailabilityContext
    contribution_id: str
    deadline_monotonic: float

@dataclass(frozen=True, slots=True)
class ReflectActionCallContext:
    question: str                            # 用户实际看到过的措辞（同 gap_consult 的外泄规则）
    arguments: Mapping[str, str]             # 已按描述符校验/夹取过的参数，缺省参数为空串
    cancellation: CancellationToken | None
    deadline_monotonic: float
    max_items: int                           # 本次调用还能被接纳的条目数

@dataclass(frozen=True, slots=True)
class ReflectActionItem:
    title: str                # ≤ EXTERNAL_EVIDENCE_TITLE_MAX_CHARS
    excerpt: str              # 将被作为 quoted_span 展示与喂给合成的文本，≤ EXCERPT_MAX_CHARS
    url: str                  # http/https，≤ EXTERNAL_EVIDENCE_URL_MAX_CHARS
    location_label: str = ""  # 可选位置词（"§3.2" / "p.4"），≤ 60 字符

@dataclass(frozen=True, slots=True)
class ReflectActionResult:            # 住 extension_sdk/reflect_action.py（要 import SDK 的 status/failure）
    items: tuple[ReflectActionItem, ...]
    note: str = ""            # 给模型看的一句话观察（"仅找到综述，无原始数据"），≤ NOTE_MAX_CHARS
    status: ExtensionResultStatus = ExtensionResultStatus.AVAILABLE
    failure: ExtensionFailure | None = None

class ReflectActionContributor(Protocol):
    descriptor: ReflectActionDescriptor
    def invoke(self, context: ReflectActionCallContext) -> ReflectActionResult: ...
```

`ReflectActionParameter`/`ReflectActionDescriptor`/`ReflectActionItem` 三个值类型与常量一起定义
在 `domain/reflect_action.py`，SDK 模块原名再导出（插件的导入路径与名字不变）。

要点：

- 数值上限全部定义在 `backend/app/domain/reflect_action.py`（`REFLECT_ACTION_*` 描述符
  侧、`EXTERNAL_EVIDENCE_*` 结果侧），SDK 只再导出；docs guard 照
  `test_gap_consult_docs_contract.py` 的形状反射这两组常量（§十）。
- **T1 已定死（裁决）**：`invoke()` 直接返回 `ReflectActionResult`，该类型自带
  `items`/`note` 与 `ContributorResult` 的 `status`/`failure` 两个字段；宿主**不再**外包
  一层 `ContributorResult`。只有一种返回形状。`ReflectActionResult` 因此定义在
  `extension_sdk/reflect_action.py` 而非 domain：`ExtensionResultStatus`/`ExtensionFailure`
  住在 `extension_sdk/contracts.py`，而 `app.domain` 不得 import SDK（架构边界守卫）。
- `excerpt` 是**插件自己声明可被引用的文本**：宿主原样展示、原样进合成，不做二次摘要。
  插件应放页面原文片段；放自己写的摘要也允许，但引用卡显示的就是它。

## 三、投影进 reflect（描述符 → prompt / schema / 白名单 / 解析）

这是本方案的第一层，也是用户点名的顺序：先把函数描述交给 reflect，再谈别的。

### 3.1 run 级规格与按轮开闸

- 宿主经构造注入：`ReasoningRetriever(..., reflect_action_host=None)`，与
  `retrieval_experiences`/`identity_store` 同款缺省 None 的可选依赖。只有 Ask 路径
  （`ask_service`）传它；报告与 Knowhow 补全不传，`None` ⇒ 规格恒为空元组（§一 非目标）。
- `run()` 在 `_new_run_state` 里向宿主要一次 `plugin_specs: tuple[ReflectActionSpec, ...]`
  （宿主对每条 contribution 跑 I/O-free 可用性探针 + 管理页插件开关；空元组 = 部署无插件
  或全部不可用）。`ReflectActionSpec` 是描述符加 `contribution_id`/`plugin_id` 的核心内
  部投影，模型看不到 plugin_id。
- **按轮决定是否提供**（run 级事实闸，与 `kg_actions` 同类，不是路由）：本轮提供插件动作
  当且仅当 `plugin_specs` 非空、档位不是 `overview`（仓库里最低档叫 overview，没有 quick；
  报告/Knowhow 无档位 ⇒ `limits is None` 也关）、`action_policy.max_plugin_actions > 0`、
  `plugin_egress_question` 非空（fail-closed，见 §九 1）、**且**本 run 已出现过至少一次
  「库内通道空手」——`zero_hit_by_action` 有任一动作计数 > 0，或上一轮 `no_progress`/`stale`。
  没有单独的轮次守卫：首轮检索本身零收获时，第一轮 reflect 就满足条件、就可以提供——这正是
  「库内已经空手」的字面含义。理由：描述符的用途就是「库内反复找不到再走外部」，把这条前置
  写进事实闸而不只写进提示词，模型就没有机会在库内还没试过时把外部检索当成默认通道；它
  仍然自己决定要不要调、什么时候调、传什么参数。
- **外部证据不计进度，但成功接纳本轮让 `stale` 持平**（T3 评审后裁决）：`run()` 的
  `no_progress` 算术只看库内候选，插件带回条目不会**重置**熔断；但与 `consult_memory` 的
  `consult_delivered_this_turn` 同形，成功接纳 ≥1 条的那一轮 `stale` 不递增——否则一次成功
  调用就把 run 推到熔断，模型永远看不到刚花预算取回的外部块。skip 各态照旧递增。
- 关闭态（上述任一不满足）传 `plugin_actions=()`：`reflect_prompt`/`reflect_schema_hint`
  /白名单三处逐字节等于接入前。

### 3.2 prompt 动作行（`prompts.reflect_prompt` 新增 `plugin_actions=()`）

每个动作渲染一行，位置在 `consult_memory_action` 之后、`SCOPE_DEIXIS_GROUNDING` 之前：

```
- search_ieee: <descriptor.description> Set search_ieee.query (<param description>);
  search_ieee.venue is one of journal|conference (<param description>, optional).
  Returns EXTERNAL material from outside the library, labelled [external] in the
  candidates; it is citable with [k] but must never be presented as library content.
  Use it only for an aspect that library actions have already come back empty on;
  derive the arguments from the question and the missing aspect, never copy
  candidate text into them.
```

- 插件只贡献 `description` 与各参数的 `description`；「返回外部材料 / 可引用 / 何时用 /
  参数不得抄候选」四句由核心模板固定，插件改不了。
- 描述文本在注册期**超限或含控制字符即注册失败**（`ExtensionRegistryError`，启动失败），
  不夹取、不净化：插件描述符是部署配置，不是用户数据，静默夹取会让模型看到与部署方写的
  不一样的说明。prompt 行里因此不可能出现 `\n`，插件文本也就无法伪装成模板里的新规则。

### 3.3 schema hint（`prompts.reflect_schema_hint` 新增 `plugin_actions=()`）

- `next_action` 枚举串追加 `|search_ieee`（顺序 = 注册序，稳定）。
- 参数以**动作名为顶层键**的嵌套对象加在 `enumerate_branch` 之后：
  `"search_ieee":{"query":"","venue":"journal|conference"},`。text 参数显示空串，enum
  参数显示 `a|b` 串——与 `enumerate.scope` 同一纪律，享受校验层的空串宽容与
  `invalid_enum` 只在填了非法值时触发。
- 没有参数的动作不产生对象分支，只加枚举词（同 `consult_memory`）。

### 3.4 白名单与解析（`reasoning_retrieval.reflect`）

- `allowed_actions += tuple(spec.name for spec in plugin_actions)`。
- 动作命中插件集合时才读 `data[name]`：非 dict 视为空；每个参数 `str(...).strip()`；
  enum 值不在白名单则清成空串并把原值存进 `ReflectDecision.plugin_rejected_arguments[param]`
  （只用于教学式 skip 文案，不参与分派，同 `enumerate_collection_rejected`）；text 值夹到
  `REFLECT_ACTION_ARGUMENT_MAX_CHARS`。
- 产出：`ReflectDecision.plugin_action_arguments: dict[str, str]`（未提供的参数为空串）。
- `fail_closed`：必填参数为空 → `ValueError("reasoning <name> action is missing <param>")`；
  自由文本参数并入既有 2000 字符硬闸的 `bounded_fields`。
- 非 fail_closed：必填缺失不在这里拦，交给 `run()` 的执行分支记 skip（fail-open，与
  `expand_graph` 空 object_id 同形）。

### 3.5 候选摘要里的外部证据

插件返回的条目进入 `state.external_evidence`，下一轮起 `_reflection_summary` 在候选摘要
尾部追加有界段：

```
[External evidence] (outside the library; citable, labelled [external · <source>])
x1 · [external · IEEE Xplore] · <title> · <excerpt 前 160 字符>
x2 · …
Note: <插件那句观察，有才出>
```

条目上的 `[external · <source_label>]` 前缀是**跨三处的同一个词**，不是排版选择：
§3.2 的固定句向模型承诺候选里的外部材料就是这么标的（`labelled [external]`），
§6.2 的合成块按 `[external · <源>]` 渲染，这里是模型第一次见到它的地方。三处任一
改词，模型就会在候选里找不到提示词说会有的那个标记。

- 键 `x{n}` 只是 reflect 阶段的显示编号，不是合成时的 `[k]` 号——合成阶段统一按
  `id_offset` 重编（§六），与 chunk/element 段的做法一致。
- 段落预算 `EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS`，超出截断并标 `…(+N)`。
- 外部内容是不受信文本：一旦 `state.external_evidence` 非空，后续每轮 reflect 都插入
  `UNTRUSTED_EVIDENCE_SYSTEM_INSTRUCTION` 系统消息（复用现有 `untrusted_evidence` 机制）。
  这个常量在 T3 拆成了两段：**通用主体**（本条用的就是它）与 knowhow 补全专用的收束句
  `KNOWHOW_COMPLETION_TASK_SENTENCE`。拆分的理由是原串末句写死「只围绕这次空格补全来
  规划与反思」——对一次带外部证据的 Ask 提问，那是一句错话。knowhow 侧显式拼
  `KNOWHOW_UNTRUSTED_EVIDENCE_SYSTEM_INSTRUCTION`，与拆分前逐字节相同（
  `test_knowhow_completion.py` 钉住）。
  开关落在**实例**上（`untrusted_evidence` / `untrusted_evidence_instruction`）而不是
  run 级：Ask 每次提问都新建 `ReasoningRetriever`（`ask_service._build_reasoning_
  retriever`），报告与 knowhow 也各自新建，没有任何生产路径跨 run 复用同一个实例。

## 四、注册与冲突规则（`extensions/registry.py` freeze 期校验）

- 动作名正则 `^[a-z][a-z0-9_]{2,31}$`。
- **保留字集** `RESERVED_REFLECT_KEYS`（唯一字面量定义点在 `domain/reflect_action.py`，
  不是 `services/reasoning_actions.py`：校验发生在 `extensions/registry.py`，而
  `extensions/` 不得 import `services/`（架构边界守卫），domain 是两边都能读的那一层）
  = 核心动作 id 全集 ∪ schema 顶层字段名全集：
  `answer add_subquery search_elements exact_lookup expand_graph ppr_retrieve expand_community
  follow_chain search_chunks enumerate_elements enumerate_kg_objects update_outline
  consult_memory` ∪ `sufficient next_action reason expand new_sub_query enumerate outline
  community_focal elements_query ppr_query exact_term chunks_query`。命中即
  `ExtensionRegistryError`。两集合都要：参数对象以动作名为顶层键，撞了字段名模型就会把
  参数写进错误的槽位。
- 跨插件动作名唯一；同一插件重复注册同名动作也拒。
- 参数名不得为 `name`/`reason`（防与动作对象外的字段混淆），数量 ≤
  `REFLECT_ACTION_PARAMETERS_MAX`；enum 值数量 ≥ 2 且 ≤
  `REFLECT_ACTION_ENUM_VALUES_MAX`。下界不是风格要求：单值枚举渲染出来没有 `|`，而
  `_validate_against_example` 正是靠这个字符判定「这是枚举」，于是模型看到的是一个封闭
  选项、校验层放行的却是任意串——正好是 §3.3 承诺不会出现的那种提示词与校验层不一致。
- `max_calls_per_run ≥ 1`；实际生效值 = `min(descriptor, policy.max_plugin_actions)`。
- 描述符所有字符串在 freeze 期只校验、不改写：**空串、超限或含控制字符即注册失败**。
  控制字符类按「不能出现在一行提示词里的字符」定义，比 ASCII 控制符更宽：C0
  （U+0000–U+001F，含换行与回车）、DEL 与 C1（U+007F–U+009F，含 NEL）、
  U+2028/U+2029（多数渲染器与分词器当换行）、以及 bidi 控制符 U+202A–U+202E 与
  U+2066–U+2069（未闭合的覆盖会让整行视觉反转，运维审到的与人看到的可以不是一句话）。
  理由同 §3.2——插件描述符是部署配置，不是用户数据，静默夹取会让模型看到与部署方写的
  不一样的说明。校验失败是启动失败，不是运行期跳过——插件是显式配置、启动冻结的
  （SOP §9.1）。

## 五、执行宿主（新文件 `extensions/reflect_action.py`）

`ReflectActionHost` 复用 gap_consult 宿主的全部骨架，逐条对应：

调用方先算 run 级前置再问拓扑：无宿主 / 无外泄串 / 无 `limits` / `overview` 档 /
`max_plugin_actions <= 0` 时 `_offerable_plugin_specs` **一次都不碰宿主**——关闭态不该
为一条本 run 不会提供的通道付探针、付线程或付一条事件。

| gap_consult 宿主 | 本宿主 |
| --- | --- |
| 一个 `daemon=True` 私有线程跑探针 + `consult` | 同款私有线程；**`specs()` 的每条可用性探针也各起一个**（探针只是合同上 I/O-free，不是结构上），超时/取消按不可用记事件 |
| 50ms 切片 join，硬 wall-clock deadline | 同，deadline = `REASONING_PLUGIN_ACTION_TIMEOUT_SECONDS`（默认 8.0） |
| `_SdkCancellation` 适配 raw Event | 原样复用（抽到共享模块，两处 import） |
| 扫描上限 `_ADMISSION_SCAN_FACTOR`、strip 头部余量 | 原样复用 |
| URL 只许 http/https；title/summary 限长 | 同；另加 `excerpt`/`location_label` 限长 |
| `seen_urls` 去重 | run 内去重（跨多次调用、跨多个动作） |
| 失败码 `gap_consult_failed` 等 | `plugin_action_failed` / `_timeout` / `_cancelled` / `_invalid_result` / `_unavailable` |
| 核心侧 `GapConsultCallContext` → SDK 侧 `GapConsultExtensionContext` | 同款两段式：核心侧 `ReflectActionCall`（`domain/reflect_action.py`）→ SDK 侧 `ReflectActionCallContext` |

**调用上下文是两个类型，不是一个**：reflect 循环住在 `app.services`，而
`scripts/check_architecture_boundaries.py` 只许组合根与插件实现 import
`app.extension_sdk` —— 所以循环拼的是核心侧的 `ReflectActionCall`（domain 层，两边
都能读），宿主在 worker 线程上把它逐字段翻成插件真正收到的
`ReflectActionCallContext`，`cancellation` 在这一步被套上 SDK 的完整令牌面
（`SdkCancellation`）。宿主的返回同样是核心类型 `ReflectActionOutcome`（domain），
所以 SDK 形状一次都不跨这条缝。

取消**不抛异常**，两个入口一致：`invoke` 返回 `plugin_action_cancelled`，`specs()` 把
被取消（以及超时）的那条探针按不可用处理、记一条事件并把该动作排除在本轮之外。宿主
只有一种失败形状。上抛发生在**核心侧**：`_action_plugin` 与 `_offerable_plugin_specs`
在宿主返回后各重读一次**自己**的 `cancel_event` 再 `raise_if_cancelled`。取消仍在第一
时间终止 run，判据却留在核心手里，而不是信一个宿主对它的转述。

**外部证据不重置进度、但成功接纳的那一轮 `stale` 持平**：`run()` 的 `before`/
`no_progress` 算术只数库内证据，所以插件带回的条目不会把熔断计数清零；与
`consult_delivered_this_turn` 同形，真送达 ≥1 条的那一轮 `stale` 也不递增（否则两次成功
调用就吃掉三轮熔断预算里的两轮，模型看不到刚花预算取回的外部块）。零条目与各态 skip
照常递增。

`run()` 的 elif 链只加分派一行：`elif decision.next_action in state.plugin_action_names:
self._action_plugin(state, decision)`。`run` 是零松弛天花板的热函数，执行体住在
`_action_plugin` 里（与 `_action_search_chunks` 同形）。分支顺序：

1. 纵深防御：本轮未提供该动作（闸未开或 spec 不在集合）→ skip `plugin_action_disabled`；
2. 必填参数为空 → skip `plugin_action_missing_argument`，文案写明缺哪个、该给什么；
3. 该动作已达 `max_calls_per_run` 或 run 已达 `max_plugin_actions`、或 run 级外部证据
   额度已满 → skip `plugin_action_cap`；
4. 已是最后一轮 → skip `plugin_action_last_turn`（同 consult_memory：产出只进下一轮
   reflect，末轮无人消费）。**排在重复判据之前**：末轮对模型有用的信息是「来不及用
   上了」而不是「这个参数问过了」，而且末轮一格预算都不该扣；
5. 相同（动作, 规范化参数）**已经成功取回过材料** → skip `duplicate_plugin_action`。
   判据是「成功过」而不是「发起过」：一次超时之后模型用同样的参数再试一次是合理的
   （它一条材料都没拿到），重试风暴由第 3 条的预算挡住——预算在**发起调用之前**扣，
   所以一个总是超时的插件最多花掉它自己那几格额度，不会把每一轮都变成一次满额的
   墙钟等待；
6. 调用宿主；结果按 §六 铸键并入 `state.external_evidence`；零条目则
   `zero_hit_by_action[name] += 1`，否则清零。真送达 ≥1 条时置
   `external_delivered_this_turn`，链尾记账让 `stale` **持平**（不清零、不递增）——
   与 `consult_delivered_this_turn` 逐字同形。

铸键这一步同时对 `title`/`excerpt`/`source_label`/`location_label`/`note` 过一次
`fold_control_characters`：候选摘要外部段（§3.5）与合成上下文块（§6.2）都是「一行一
条」的格式，而反向绑定照着这个形状读回来，所以一条 excerpt 里的换行会渲染成一整行核心
从没写过的证据。折叠发生在**铸键处、只一次**，两个渲染点共用它的结果（渲染侧各自再折
一次作纵深）。

轨迹：新 `step_type="plugin_action"`（核心拥有的一个值，不按插件增长），`summary` 形如
「调用扩展检索 search_ieee：<query>，新增 2 条外部材料」，`detail` =
`{"plugin_id","action","arguments","found","result_keys","reason"(skip 时),"truncated"}`。
`arguments` 逐字记录——这是外泄透明的落点（§九）。

## 六、外部证据进合成与引用（核心独占）

### 6.1 载体

- `ExternalEvidence(key, plugin_id, action, source_label, title, excerpt, url, location_label)`
  定义在 `domain/reflect_action.py`（`reasoning_retrieval` 与合成侧都要读，domain 是两边共同的
  下层；不从 SDK 导出，插件永远不构造它），`key` 由核心按 run 内序号铸造（`ext:{plugin_id}:{n}`，用作 `object_id`）。
- `ReasoningResult.external_evidence: List[ExternalEvidence]`；
  `ResponseDraftInput.external_evidence: tuple[...]`（新字段，缺省空元组，既有构造方不改）。
  这是与 gap_consult **相反**的选择：gap 建议刻意在草稿阶段之后填、合成看不见；外部证据
  必须在草稿阶段之内，否则就退回方案 2。

### 6.2 上下文块

`EvidenceContextService.external_context(items, *, id_offset, budget_chars)` 与
`chunk_context` 同形：返回 `(block, evidence_by_id)`。每条：

```
k7: [external · IEEE Xplore] <title> (<location_label>) — <excerpt>
```

行首是 `k7:` 而不是 `[k7]`：`_bounded_context_append` 按 `^k\d+:` 认哪些 key 真的留在了
渲染文本里（截断之后只保留还看得见的那几个），与 `chunk_context`/`element_context` 同形。
无 `location_label` 时省略括号那一段。**每条渲染字段（title/excerpt/source_label/
location_label）都过 `domain.reflect_action.fold_control_characters`**：块是「一行一条」，
一条摘录里塞进换行加 `k1: [chunk][personal] …` 就会伪造出一条核心从没写过的库内证据行。
**`url` 为空或不是 `http(s)://` 开头的条目整条跳过**（§九 不变量 3 的运行期闸），且不占号——
键号在**已接纳**条目上连续，不留空洞。

`evidence_by_id[k]` 记录 `object_type="external"`、`object_id=key`、`name=title`、
`snippet=excerpt`、`source_title=title`、`location_label`、`source_id=""`、`element_id=""`、
`tier="external"`、`provenance={"kind":"external","plugin_id","action","url","source_label"}`。
块在 KG/元素/chunk 三段之后拼接，键号段 `_EXTERNAL_KEY_BASE = 6000`（5000 已是集合枚举的
`_COLLECTION_KEY_BASE`，两者可同时出现在一次合成里；6000 仍低于按节合成的
`OUTLINE_SECTION_KEY_STRIDE = 10000`），预算 `EXTERNAL_EVIDENCE_CONTEXT_CHARS`，超预算整条丢、
不切半条（半句带出处的引文就是误引）。`ReasoningEvidenceSnapshot.from_result` 同步拷贝
`external_evidence`，否则编排层拿到的快照里没有这个字段。
`answer_prompt` 仅在块非空时追加一条规则：「`[external · …]` 项来自库外，可像其它条目一样
用 `[k]` 引用，但不得表述为笔记本内容」；块为空时 prompt 逐字节不变。

### 6.3 锚点与引用

- `parse_anchors` 不改：`evidence_by_id` 已带全部字段。`AnswerAnchor` 与 `Citation` 各加
  `url: str = Field(default="", exclude_if=lambda v: not v)`，`parse_anchors` 从
  `provenance["url"]` 抄到 `url`；无外部证据的答案 payload 一个字节不多。
- 回退列表：`citations_from` 只吃 `top_hits`；外部条目另建 `external_citations(items)` 追加
  到 `citations` 尾部，`label = f"{source_label} · {title}"`，`source_id`/`element_id` 空，
  `tier="external"`，`url` 非空。
- **引用只发给真的进过至少一个 prompt 的条目。** `_answer_reasoning` 用 `external_sink`
  回报本次装进上下文的 `ExternalEvidence.key` 集合，`_draft_reasoning_response` 按它过滤后
  才交给 `external_citations`。引用是「答案可能引自这条材料」的声明，被外部段预算挤掉、
  或因没有可打开 URL 被拒的条目模型从没见过，列进引用就是给一份没看过它的答案挂来源。
  装入/丢弃两个数进合成轨迹步的 detail（`external_included` / `external_dropped`），
  静默丢弃是零披露。
- **按节合成每一节都装外部块。** 与只留在回退路径上的 Memory / 推导链 / 集合地图刻意相反：
  外部材料不由大纲绑定，却是本轮唯一「库里查不到」的东西；只喂给某一节，别的节会在同一篇
  答案里对着一个自己没见过的 `[k]` 号写作，而合并后的引用表偏偏认得它。各节的外部键落在
  自己 `key_offset` 的号段内（`6000 + ≤EXTERNAL_EVIDENCE_MAX_PER_RUN ≪ 10000`），互不相交；
  `external_sink` 收各节装入键的**并集**，同一条材料进了三节仍然只发一条引用。
- 预算记账：外部段在 `context_block[:total_context_budget]` **之后**追加，所以
  `baseline_sink["budget_chars"]` 记的是「总预算 + 本次外部段**实际**占用」，
  `len(context_block) <= budget_chars` 这条不变量在有外部证据的一轮上照样成立。
- `grounded`/`evidence_level` 语义不变（有合法锚点即 grounded）：外部锚点同样算。前端徽章
  另行区分（§七）。
- `tier` 取值集从 `personal|base` 扩为 `personal|base|external`。所有按 tier 分桶的读取点
  （`computeSourceTierCounts`、报告徽章、经验投影的引用计数）默认把未知 tier 当
  `personal`，所以不改的地方最多是少一个桶，不会错桶。

## 七、API、前端与公开分享

- `AskResponse` 不加新顶层字段：外部证据只经 `anchors`/`citations` 的 `url`/`tier` 与
  `reasoning_trace` 的 `plugin_action` 步下发。`docs/product-and-api*.md` 引用条目补
  `url`/`tier=external` 两项。
- 前端 `workspace-model.ts`：`Citation`/`AnswerAnchor` 加 `url?: string`；
  `NON_KG_REFERENCE_LABELS["external"] = "外部"`。
- 引用卡（`answer-panel.tsx` 参考卡组件）：`object_type === "external"` 时不出「知识图谱」
  与来源查看按钮，出「打开链接」（`target=_blank rel=noopener noreferrer`，只渲染 http/https）
  与「导入为来源」；后者复用 `onImportGapSuggestion` 通道与 `answer-gap-suggestions.tsx` 的
  逐行状态机（按下即禁用该行、进行中文案、成功冻结为「已导入」、失败原地持久显示，不发
  toast）。只读工作区不传导入回调 ⇒ 按钮不出现。
- 来源分布徽章：`computeSourceTierCounts` 增加 `external` 桶，文案「来源 · 个人 N ·
  公共 M · 外部 K」（前两段是既有实现的逐字文案）；总数恒等于可见引用数的不变量保持。
- 轨迹标签：`TRACE_STEP_LABELS.plugin_action = "扩展检索"`；`getTraceStepDetail` 对该
  步显示「<action> · <arguments 摘要> · 新增 N 条」，skip 步沿用既有 reason 文案渲染。
- 公开分享 `conversation_public_view.public_reference`：新增 `is_external: bool`（由
  `tier == "external"` 得出），**不**输出 `url`——匿名读者看到「外部」标记、标题与摘录，
  与「nothing addressable」原则一致；是否放开链接留 §十一。
- MCP `ask_notebook`：`anchors`/`citations` 原样透传，`url` 随之可见（已认证的 Agent 面）。

## 八、配置与预算

| 设置 | 默认 | 说明 |
| --- | ---: | --- |
| `REASONING_MAX_PLUGIN_ACTIONS` | 2 | run 级总次数；`0` 即 kill switch，三处投影同时消失 |
| `REASONING_PLUGIN_ACTION_TIMEOUT_SECONDS` | 8.0 | 单次调用硬 deadline，探针 + invoke 共用 |
| `EXTERNAL_EVIDENCE_MAX_PER_RUN` | 10 | run 内接纳条目上限，跨动作累计 |

`ReasoningActionPolicy` 加 `max_plugin_actions`（`reports/policy.py` 的
`reasoning_action_policy` 读同名设置）。档位：本仓库的档位表是
`overview|standard|deep|thorough|exhaustive`（`core/ask_retrieval_policy.py`），没有
`quick` 这一档——**最省的 `overview` 即关闭档**（`_PLUGIN_ACTION_CLOSED_EFFORTS`），
其余档位按策略值。没有 `limits` 的调用方（报告逐节深挖、knowhow 补全、窄测试替身）
同样不提供：它们连档位这个概念都没有，而这条通道的成本必须由某一档明确买单。
不加独立的 `*_ENABLED` 布尔：插件本身是显式配置 + 管理页运行时开关（#635 三闸口），
`max=0` 已经是部署级关闭，再加一把只是多一处同步点。

领域常量（`domain/reflect_action.py`，docs guard 反射）：

| 常量 | 值 |
| --- | ---: |
| `REFLECT_ACTION_NAME_MAX_CHARS` | 32 |
| `REFLECT_ACTION_DESCRIPTION_MAX_CHARS` | 600 |
| `REFLECT_ACTION_PARAMETERS_MAX` | 4 |
| `REFLECT_ACTION_PARAM_DESCRIPTION_MAX_CHARS` | 200 |
| `REFLECT_ACTION_ENUM_VALUES_MAX` | 8 |
| `REFLECT_ACTION_ARGUMENT_MAX_CHARS` | 300 |
| `REFLECT_ACTION_NOTE_MAX_CHARS` | 300 |
| `EXTERNAL_EVIDENCE_MAX_ITEMS_PER_CALL` | 5 |
| `EXTERNAL_EVIDENCE_TITLE_MAX_CHARS` | 200 |
| `EXTERNAL_EVIDENCE_EXCERPT_MAX_CHARS` | 800 |
| `EXTERNAL_EVIDENCE_URL_MAX_CHARS` | 2048 |
| `EXTERNAL_EVIDENCE_SOURCE_LABEL_MAX_CHARS` | 40 |
| `EXTERNAL_EVIDENCE_LOCATION_LABEL_MAX_CHARS` | 60 |
| `EXTERNAL_EVIDENCE_REFLECT_BLOCK_CHARS` | 1600 |
| `EXTERNAL_EVIDENCE_CONTEXT_CHARS` | 4000 |

## 九、安全不变量（逐条是验收断言）

1. **外泄面 = 问题 + 模型写的参数，且参数逐字进轨迹。** `ReflectActionCallContext` 没有
   候选、没有子查询、没有 gap phrases、没有任何 id；`question` 沿用 gap_consult 的规则
   （只送用户实际看过的措辞，`research_question` 合成串永不外送）。参数是模型文本，核心
   无法证明它不含库内片段——v1 的对策是提示词红线（§3.2 第四句）+ 轨迹逐字披露
   （用户能看到发出去了什么）；n-gram 重叠拦截登记为后续项（§十一）。
2. **插件拿不到核心端口。** 上下文类型没有端口字段；宿主线程不 `copy_context()`，插件
   继承不到本请求的冻结范围、检索 run 或叶 I/O 槽位。
3. **外部证据永远带 `tier="external"` 与非空 `url`，永远没有 `source_id`/`element_id`。**
   `test_citation_notebook_id_guard` 同款登记清单加一条：所有构造 `AnswerAnchor`/`Citation`
   的点，`object_type=="external"` ⇔ `tier=="external"` ⇔ `url` 非空。
4. **公开分享不输出 URL。** `public_reference` 白名单测试断言 `url` 不在输出键里。
5. **fail-open 且零影响。** 插件抛错/挂死/畸形返回 → 一条 skip 步、零外部证据、答案与
   接入前同形；取消照常上抛（`AskCancelled` 不被吞）。
6. **关闭态逐字节等价。** `plugin_actions=()` 时 `reflect_prompt`/`reflect_schema_hint`
   输出与接入前基线逐字节相等（冻结基线测试）；`answer_prompt` 在外部块为空时同样。
7. **模型看不到 plugin_id。** prompt、schema、候选摘要、合成块里只有动作名与
   `source_label`；plugin_id 只进轨迹 detail 与 provenance。
8. **URL 只许 http/https**，前端只对这两种 scheme 渲染链接；`javascript:` 等在宿主净化期
   丢弃整条。
9. **不可信内容标记贯通。** 外部证据出现后 reflect 加系统消息、合成块带 `[external]`
   前缀与规则句——两处缺一即测试红。

## 十、分期与守卫

按 CLAUDE.md 的逐任务委托：每个 T 一个实现子代理，完成后 spec-review + code-quality-review。

- **T1 契约与注册**：`domain/reflect_action.py`（含 `RESERVED_REFLECT_KEYS`）、
  `extension_sdk/reflect_action.py`、registry freeze 校验与错误码、
  `registry.reflect_action_specs()`、`ReasoningResult.external_evidence`。测试：正则、
  保留字冲突（含反射式重导保留字集）、跨插件重名、上限、控制字符与超长响亮失败。
- **T2 投影**：`reflect_prompt`/`reflect_schema_hint` 的 `plugin_actions` 参数与渲染纯函数；
  `reflect()` 白名单与解析；`ReflectDecision` 新字段。测试全部经
  `test_reasoning_enumeration_tools._ValidatingLLM` 真实形状闸：合法参数落地、枚举非法值
  清空且记 rejected、必填缺失在 fail_closed 抛、关闭态逐字节基线。
- **T3 宿主与执行**：`extensions/reflect_action.py`、`_action_plugin`、按轮闸、
  `state.external_evidence`、候选摘要外部段、系统消息置位、`plugin_action` 轨迹步、
  `ReasoningActionPolicy.max_plugin_actions` 与三个设置。测试：deadline 截断、挂死插件
  fail-open、取消、去重、cap、last_turn、零条目计零命中。
- **T4 合成与引用**：`ExternalEvidence`、`ReasoningResult`/`ResponseDraftInput` 字段、
  `external_context`、`answer_prompt` 条件规则句、`AnswerAnchor`/`Citation.url`、
  `external_citations`、`public_reference.is_external`。测试：`[k]` 绑定到外部条目、payload
  在无外部证据时逐键不变、公开视图无 url。
- **T5 前端**：类型、引用卡三个按钮分支、导入通道复用、tier 徽章第三桶、轨迹标签与
  detail。node 测试覆盖 `buildAnswerReferences`/`computeSourceTierCounts`/引用卡渲染。
- **T6 文档与守卫**：`docs/product-and-api*.md` 新节「Reflect plugin actions
  (`ask.reflect_action`)」+ 引用字段条目；`architecture.md` 一句；
  `test_reflect_action_docs_contract.py` 照 gap_consult guard 反射两组常量与表行。
  （`docs/deployment-extensions-sop*.md` §3.5 的表行与数词 eight/八 → nine/九 已在 T1 完成：
  导出新 `*_POINT` 的同一提交必须过既有 docs guard，拆不到 T6。）
- **PR-B（独立 PR）**：`examples/extensions/arxiv-search/` 新增 `examples.arxiv_search.search`
  动作（参数 `query` text 必填；复用 `client`/`atom`/节流与 egress 策略层），G1 零网络 e2e
  + G2 `scripts/check_sample_plugin.sh` 通过，零补丁验收照 #596 的机器测试。这是 X6 要求
  的真实消费者，与 T1–T6 同一里程碑内合入。

守卫与账目：`reasoning_retrieval.run`/`reflect` 的函数天花板零松弛——新增语句放尾注释
之前，分支体在独立方法；架构守卫计数与 `RUNTIME_ATTRIBUTES` 在 PR 说明里给出前后值；
docs guard 的表行计数与数词必须同 PR 改。

## 十一、已裁决的开放问题（实施时不再讨论）

1. **进合成还是只进 reflect** → 进合成，作为外部证据类（方案 3）。无引用备注（方案 2）
   被否：会让外部内容与模型自有知识在答案里不可分。
2. **动作名裸名还是带插件前缀** → 裸名 + freeze 期冲突校验。模型对短名更稳；命名空间
   由参数嵌套对象承担。
3. **首轮是否提供** → 不提供；且要求本 run 出现过库内空手。这是事实闸，不是正则路由：
   模型仍自主选择，只是不会在库内一次都没试过时看到这个选项。
4. **独立总开关** → 不加；`REASONING_MAX_PLUGIN_ACTIONS=0` 即关闭。
5. **报告/Knowhow** → v1 不接，登记为 v2（需要按节的外部调用预算）。
6. **gap_consult 去留** → 并存。待 arXiv 样例的 reflect 动作在真实部署跑过一轮后，再决定
   是否让 gap_consult 退役为「模型没调时的兜底」或直接删除。
7. **公开分享放不放 URL** → v1 不放，只标 `is_external`。放开需要单独裁决匿名读者能否
   跟随部署外链。
8. **参数抄候选的 n-gram 拦截** → v1 不做，靠提示词红线 + 轨迹透明；若轨迹统计显示参数
   里出现候选原文，再加 `REFLECT_ACTION_EGRESS_OVERLAP_CHARS` 窗口拦截并 skip。
9. **excerpt 是否二次摘要** → 不做。插件声明的 `excerpt` 就是引用卡上的 `quoted_span`。

## 十二、参考与核对入口

- reflect 三处同步与 run 级闸：`backend/app/services/reasoning_retrieval.py::reflect`、
  `backend/app/services/prompts.py::reflect_prompt/reflect_schema_hint`
- 校验层枚举/布尔规则：`backend/app/core/model_json.py::_validate_against_example`
- 动作执行体先例：`reasoning_retrieval.py::_action_search_chunks`、consult_memory 分支
- 宿主先例：`backend/app/extensions/gap_consult.py`、`backend/app/extension_sdk/gap_consult.py`
- 引用装配：`backend/app/services/evidence_context.py::chunk_context/parse_anchors/citations_from`、
  `backend/app/services/ask_service.py::_draft_reasoning_response`
- 公开分享白名单：`backend/app/services/conversation_public_view.py::public_reference`
- 前端：`frontend/app/answer-formatting.ts`、`frontend/app/answer-panel.tsx`（引用卡）、
  `frontend/app/answer-gap-suggestions.tsx`（导入状态机）、`frontend/app/reasoning-trace.ts`
- 样例插件：`examples/extensions/arxiv-search/`
- 上游裁决：`docs/superpowers/specs/2026-08-25-pluggable-ask-engine-design.md`（X3/X6）、
  `docs/superpowers/plans/2026-08-24-ask-gap-consult.md`（外泄与宿主规则）
