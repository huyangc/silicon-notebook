# 「当前notebook的文章说明了什么」怪回答的根因整改：枚举校验、目录问法、首轮空手提示（设计规格 v1，已拍板）

> **状态更新（本 PR，用户裁决）**：F2 的 **reasoning 目录播种半已撤回**。用户裁决「agentic 模式里目录该由 LLM 通过工具自己决定是否
> 调用，不该用正则路由替模型做决定」，因此 `_first_round_catalog_seed`、`ReasoningRunInput.original_question` 与随播种引入的
> `_run_enumeration(phase=...)` 全部删除，来源清单的范围改为**工具参数** `enumerate.scope`（由模型填，**字符串枚举** `all|current_notebook`，
> 默认 `all` = 检索范围内全部文档，与集合地图 `sources: N` 的联邦口径一致；`current_notebook` 才收窄成只列本库）。参数刻意不是布尔：
> `model_json._validate_against_example` 对布尔示例是硬类型校验，模型吐 `"true"` 会整轮被打成兜底（与 F1 修的根因同类），字符串枚举
> 才享受 F1 的空串宽容规则；非空非法字符串仍按 `invalid_enum` 拒、非字符串按 `invalid_type` 拒，解析器则对任何取不到的值落回默认且不废动作。
> 范围进**续跑键**（`(collection, kind, source_id, local_only)`），所以换范围是新开一条链而不是 `already_enumerated`；范围在集合地图
> （`sources: N (current notebook: M)`）、`enumerate` 步摘要与回喂账目（后缀「（仅当前笔记本）」）三处可见。F2 的另一半
> （`document_overview.py` 动词表扩展）保留，但它此后**只服务 chunk 模式**的文档介绍通道，随归一第四步一并退役。
> F1、F3 与 `_run_enumeration` 抽取全部保留。
>
> **状态**：已拍板（2026-09-07，用户：「按这个顺序修，重点考虑修法的泛化性」）。生产复现：本机部署（PR #690 版本）
> 笔记本「递归深度语言模型」，1 篇英文论文、无图、高级模式「深入分析」。整改项按根因权重排序；第 4 项（相关度地板对跨语言
> 查询的行为）刻意留到归一第三步的对照集里量化再动。

**基线口径**：`origin/master = 2589d55e7`。行号会腐烂，实现按分支内容定位。

## 根因链（已用日志与复现实验证实）

1. **主因**：`app/core/model_json.py::_validate_against_example` 把 schema 提示里含 `|` 的字符串当闭集枚举；`reflect_schema_hint` 的
   `"kind":"formula|table|image|code_block"` 因此要求 kind 必为四值之一，而 reflect prompt 明文要求列文档目录时 `enumerate.collection="sources"`、
   kind 留空。模型照 prompt 做 → `invalid_enum` → `MalformedModelResponse` → `reflect()` fail-open 返回 `ReflectDecision(sufficient=True, next_action="answer")`
   → 零证据合成 → 答案只剩集合地图计数。轨迹指纹：reflect 步 summary 为 "answer"、reason 空、sufficient=true。该校验自 2026-09-01 生效，
   `validate_model_json_shape` 在 repair 关闭态也跑，因此文档目录动作与 `enumerate_kg_objects`（kind 空）在生产一直是死的；既有测试用假 chat client
   直接返回 dict，绕过了校验层。
2. 次因：中文元问题（原问题 + 整段问题契约）对英文语料，`score_chunks` 的 `RELEVANCE_FLOOR=0.12` 把全部候选丢掉，首轮三路 0 命中。
3. 首轮空手后 `state.stale=1`，第一次 reflect 就被注入 `NO_NEW_EVIDENCE_NOTE`（「请直接选择 answer」）。
4. chunk 模式的文档介绍分类器 `document_overview.overview_intent` 中文动词表只有 介绍|讲|讲述|讲的|包含，「说明了什么」不命中；reasoning 侧
   没有对应的确定性通道，全靠模型在 reflect 里选目录动作。

## 修法（每条都写成通用规则）

### F1 JSON 形状校验：枚举字段接受空串，语义留给领域解析器

- 规则：schema 提示里含 `|` 的字符串字段表示「若填则必须是这些值之一」；空串表示「本次不用这个字段」，**一律接受**。非空且不在集合内仍
  `invalid_enum`。理由：校验层只管形状/语法；每个领域解析器都已对枚举字段做白名单收窄（`kind if kind in ENUMERABLE_ELEMENT_KINDS else ""`、
  `direction if in (...) else "both"`、未知 `next_action` 按 fail_closed/answer 合同处理），校验层再加一道更严的语义闸只会制造 prompt 与校验器的矛盾。
- 通用守卫测试：遍历 `prompts.py` 里所有 `*_SCHEMA_HINT` 常量与 `reflect_schema_hint` 的全部门组合，把提示对象中每个枚举字符串置空后必须通过
  `validate_model_json_shape`；非空非法值仍被拒（保留既有用例）。
- 回归测试走**真实校验层**：目录决定（`collection="sources"`, `kind=""`）与知识对象清单决定（`kind=""`, `object_type="concept"`）经
  `parse_model_json_object` + `validate_model_json_shape` 与 `reflect_schema_hint` 校验通过；并通过 `model_provider` 的 `chat_json` 路径（假 provider 返回该 JSON）
  确认 `reflect()` 拿到的是 enumerate 决定而非兜底。
- `reflect()` 的 fail-open 兜底**可观测**：任何走到兜底的路径（非对象、非法动作、`MalformedModelResponse`、其它异常）在返回的决定里带
  **两个专用字段** `fallback: bool` 与 `fallback_reason: str`。轨迹 reflect 步 `detail` 加稀疏键 `fallback_reason`（机器码），summary 换成中文整句
  （「反思结果无法采用（校验拒绝：invalid_enum），按直接作答处理」／「……（模型调用失败），……」），不再是光秃秃的 "answer"。这是通用的：以后任何
  校验/解析失败都能在轨迹里看到，而不是伪装成模型判定「够了」。

  **评审后修正（两处，两处都改变了实现而不只是措辞）：**

  1. **标记走 bool 字段，不走 `reason` 前缀。** 初稿写的是 `reason="reflect_fallback:<原因>"`。`reason` 是**模型可控**的自由文本：一个吐出
     `"reflect_fallback:证据够了"` 的响应就能把自己伪装成兜底，反过来一个真兜底也可能被模型的文本干扰——判据不能落在被判据方能写的那一格里。
     兜底决定的 `reason` 保持空串。
  2. **原因按稳定 `.code` 判定，不按异常类型。** 初稿的 `except MalformedModelResponse` 在**生产里永不触发**：生产 client 是
     `ScheduledJsonChatClient`，它的 `_resolve` 把一切异常重抛成 `ModelInvocationError` —— `MalformedModelResponse` 的**兄弟**类而不是子类
     （`model_provider.py::_invocation_error`），而 `invalid_enum` 躺在**第二层** `__cause__` 上
     （`ModelInvocationError → MalformedModelResponse → ModelJsonRepairError.reason`）。`catalog_job.py` 已登记过同一个坑并给出同一条裁决：
     按 `.code == "malformed_response"` 分支，再沿 `__cause__`/`__context__` 链找第一个带非空 `.reason` 的异常。其它异常取已折叠好的稳定错误码
     （`provider_rate_limited` / `provider_unavailable` …），泛化档退回异常类名。**空串 `next_action` 也算非法动作**（枚举字段放行空串之后新长出的形状，
     按串判会把它当「没有被拒的动作」放过去）；**模型未配置**同样打标（`model_unconfigured`），不再返回裸 `answer_decision`。
     回归测试必须**经真实 `RuntimeModelProvider`** 调 `reflect()`；直接 `raise MalformedModelResponse() from exc` 的替身测的是生产上不存在的形状。

### F2 目录问法：分类器动词表扩展（保留）＋ reasoning 确定性播种（**已撤回**）

- `document_overview.py` 中文动词表扩为 介绍|讲|讲述|讲的|包含|说明|阐述|描述|讨论|探讨|研究|谈|写，并补「讲了些什么/说了什么/主要说什么」类尾巴；
  英文补 `what (?:does|do) {subject} (?:say|explain|present|show|talk about)`、`what (?:is|are) {subject} saying`。用既有 fullmatch + 尾巴约束保住
  「话题类问题保持 ranked」（`文章说明了 CMRR 如何计算`、`这篇文章说明的公式` 不得命中）；测试正反各钉。
> **以下播种小节整段已撤回（见文首状态更新）。** 保留在此仅作决策记录：agentic 模式里目录是模型经 `enumerate.collection="sources"`
> 自选的动作，范围是同一次调用的参数 `enumerate.scope`（字符串枚举，默认 `all` = 全参与集）。「预算先手」「播种算首轮
> 进展」「播种排在 plan 之前」「`phase` 稀疏键」四条随播种一并作废；`no_progress` 计入 `state.enum_rows_used` 的**口径同源**保留
> （首轮此后恒为 0，但两处对「有没有进展」必须永远给同一个答案）。

- ~~**reasoning 确定性目录播种**~~（沿用「不赌模型」的 seed 哲学）：首轮在集合地图之后，若 `overview_intent(原问题).kind == "catalog"` 且枚举接线开、
  作用域未受限（受限 scope 本就禁用整集合枚举），则用与 reflect 目录动作**同一个执行路径**列一次文档目录（同一预算池、同一续跑账目：模型之后再选
  目录动作按「已枚举过」跳过），记一条 `enumerate` 步（`detail.phase="seed"`），目录进入候选摘要与结果卡。原问题从 `ReasoningRunInput` 新字段
  `original_question`（默认空串 → 回退 `question`）传入；`run()` 兼容入口加同名可选 kwarg。chunk 模式的 `_try_document_overview` 与它共用 `overview_intent`。
- 有图/无图都播种（目录与图无关）；`allow_enumeration=False` 的调用方（knowhow 补全）不播种。
- **预算先手（评审补记，已登记为产品行为）**：播种与模型动作共用一个 run 级预算池，播种排在最前，所以**大库在低档位下播种这一次就可能吃满
  `enum_rows_per_run`**，之后模型自己选的枚举动作会按预算跳过（轨迹 `enumeration_budget`）。这是刻意的取舍（目录问法的答案就是那份目录），但必须
  写进产品文档，否则「模型选了枚举却什么都没列出来」在排查时看不出因果。
- **播种算首轮进展（评审修正）**：首轮 `no_progress` 的口径必须与 reflect 循环里的那一份同源（循环数 `state.enum_rows_used`）。初稿只数
  `collected/elements/chunks`，于是一个只有来源、chunk 零命中的库在列全目录之后仍被判「首轮空手」：`stale` 从 1 起步，第一次 reflect 还会收到
  「一条证据都没查到，请先换通道」——就在那份目录已经躺在同一份上下文里的时候。
- **播种排在 plan 之前**的理由是候选（目录必须先于 plan 进入轨迹与候选摘要，第一次 reflect 才看得见），**不是**「要读地图那一步才建起来的接线判据」
  ——`enumeration_active` 在 `_new_run_state` 里就算好了。
- 播种的 `enumerate` 步与 `_run_enumeration` 的**每一条 `skip`** 都带 `phase`（非空才写）：只给成功步打标的话，「播种被挡掉了」与「模型那次被挡掉了」
  在轨迹上完全同形。

### F3 首轮空手：换通道提示，不是「直接作答」提示

- 新 `FIRST_ROUND_EMPTY_NOTE`：「系统提示：首轮检索未命中任何证据。这通常是查询措辞与语料不匹配（语言、术语）或问题属于目录/清单类。
  请先换通道或改写查询——用语料语言的关键词做 search_chunks、或用 enumerate 列目录/清单——不要在零证据下直接作答；只有多次尝试仍无命中时才
  answer 并如实说明依据不足。」
- 注入规则：第一次 reflect（`steps == 0`）且首轮无进展时用它；之后各轮无进展仍用 `NO_NEW_EVIDENCE_NOTE`。stale 硬熔断账目不变。
- **按 run 级闸拼装（评审修正）**：常量改成函数 `first_round_empty_note(chunk_search, enumeration)`。写死那两个动作名会在 knowhow 补全那一档炸掉——
  它把 `allow_search_chunks` 与 `allow_enumeration` **双双关掉**且 `fail_closed=True`，模型照提示选 `search_chunks` → 不在白名单 → `reflect()` 抛
  `ValueError` → 整轮检索硬失败。建议句按可用动作拼：都可用「用语料语言的关键词做 search_chunks、或用 enumerate 列目录/清单」；只一个就只提那一个；
  都不可用则「换一个可用的检索通道或改写查询」。其余措辞四档逐字相同。这与「prompt / schema / `allowed_actions` 三处必须同源」是同一条纪律。
- 测试：首轮空手的第一次 reflect 上下文含新提示、不含旧提示；第二轮无进展含旧提示；knowhow 补全档位（两闸关 + fail_closed）首轮空手不抛、提示不含
  两个动作名。

## 刻意不做

- 相关度地板与跨语言查询：留第三步对照集。
- 检索查询带整段问题契约（`confirmed_research_question`）：与地板同一评估。
- 高级界面确认合同后 `plan()` 不执行、关键词臂不跑：登记，归一第三步一并看。

## 验收

1. 生产复现题在修后的引擎上：第一次 reflect 的目录决定被接受，轨迹出现 `enumerate`（sources）步，答案基于来源摘要（用真实校验层 + 假模型回放）。
2. 通用守卫：所有 schema 提示置空枚举通过；非法非空仍拒。
3. 分类器正反用例（chunk 侧）；~~reasoning 目录播种在 catalog 问法下发生、在话题问法下不发生、受限 scope 不发生、knowhow 补全不发生~~
   → 撤回后改为：catalog 问法**不**触发任何枚举（模型不选就不列），目录动作的范围按 `scope` 参数生效
   （缺省/留空/解析不出 → `all`，含挂载参考库；`current_notebook` → 只列本库；换范围 = 新开一条链，同范围重复 = `already_enumerated`）。
4. 首轮空手提示切换用例。
5. `bash scripts/check.sh` 全绿。
