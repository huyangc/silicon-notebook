# 逐步推理的 KG 可选化：原文检索一等动作、按图存在收缩动作空间、撤销建图闸（设计规格 v1·草案）

> **状态**：v1.1，已拍板进入实施（2026-09-07 用户决议：D-1..D-4 全部按推荐；追加 T5「整理知识图谱」增强提示）。
> 这是「问答方法归一」路线的第一步（四步：KG 可选的 reasoning → 直答档位 → 自动模式灰度 → 退役 chunk 流水线）。
> 本文**只覆盖第一步**；后三步各自另立规格。
> **实施状态**：T1–T5 已落地于本分支，文档同步见「文档」小节。

**基线口径**：全部现场引用按本 worktree `HEAD = 3c9e9ac40`（`claude/agentic-qa-feasibility-f0a84f`，与 `origin/master` 同点）逐条实测。
行号会随后续改动腐烂，实现时按分支内容定位，不按行号。

## 目标与非目标

目标：

1. 一个**没有知识图谱**（本库无图、且勾选范围内没有已建图的参考库）的笔记本，逐步推理能拿到与 chunk 通用问答同等级的原文证据：
   模型有一个一等的原文段落检索动作，首轮也有一条确定性的原文播种。
2. 无图时模型看不到、也选不到只对图有意义的动作；不再把反思轮浪费在必然空手的动作上。
3. 「请先构建知识图谱」从**拒答**变成**披露**：注册表、前端提交闸、早退分支三处不再把图当前提；`kg_required` 保留为如实上报的响应字段。
4. 有图的笔记本行为**逐字节不变**（prompt、动作空间、检索、预算、引用）。这是本 PR 的回归红线。

非目标（本轮明确不做）：

- 不做「直答」档位（零反思轮），不改自动模式选路（`auto_ask_mode_from_intent`），不动 `ask_chunk` 流水线的任何一行。
- 不改 `ask.engine` 插件端口（它已经是 KG 可选的：`search` 必有、`search_kg`/`kg_neighbors`/`kg_overview` 可选）。
- 不给有图笔记本加原文播种或改变其检索预算分配（D-1）。
- 不建问答质量评测台；本轮验收用契约测试 + 三句式人工抽问（见「验收」）。
- 不改 `AskResponse`/`AskRequest` 形状、不动 OpenAPI 冻结夹具、不加 schema 迁移。
- 不动 `answer_prompt` 规则编号（L0 规则 1..13 被 `test_memory_authority` 钉成连续序号）。

## 现状事实（为什么无图的 reasoning 今天是残的）

三条路径对 KG 的真实依赖：

| 路径 | KG 角色 | 现场 |
| --- | --- | --- |
| `chunk` | 可选增强：有图且配了 rerank 才走三路 mix（向量 + KG overlay + PPR），否则 multi/single | `retrieval_candidates._build_chunk_retrieval_plan`；`ask_chunk` docstring 里「KG 不参与」已过时 |
| `reasoning` | 注册表 `requires_kg=True`；自集合枚举 PR-2.5 起无图可放行，但工具集仍是图形状 | `ask_modes.ASK_MODES["reasoning"]`；`ask_service.ask_reasoning` 的 `no_usable_kg` 早退 |
| `ask.engine` 插件 | 端口天然可选；核心 `is_available` 只看插件可用性，不看 `requires_kg` | `extensions/ask_engine.py:118`，`plugin_ask_engine.search_kg` 无图返回空 |

无图时内置 reasoning 的四个结构性缺口（本文 T1–T4 一一对应）：

1. **主检索原语是图形状的。** 首轮 `_first_round_search` → `ReasoningRetriever.search` → `federated_retrieve`，返回知识对象；无图恒空。
   原文只能靠三条路进入 `state.chunks`：PPR seed（无图为空）、精确查找 seed（问题里要有标识符）、`search_elements`。
   `search_elements` 走 `_retrieve_elements`（chunk 命中回灌成元素，limit 8，每 run 上限 `reasoning_max_element_searches=5`），
   而它的产物进入合成时落在「Direct source elements」分区，被 `answer_element_items` 硬顶（标准档 **6 条**）；
   真正的大预算分区「Retrieved chunks」（标准档 `chunk_context_chars=30,000`）在无图时**没有任何一等入口**。
   这是差距的核心：不是「找不到原文」，而是找到的原文进不了大预算分区。
2. **五个动作是死的。** `expand_graph`、`ppr_retrieve`、`expand_community`、`follow_chain`、`enumerate_kg_objects` 在无图时必空或必 skip，
   但 `reflect_schema_hint`/`reflect_prompt` 的动作枚举与 `reflect()` 解析处的 `allowed_actions` 都不随图存在与否收缩。
   `plan_prompt` 开头「You plan how to retrieve a knowledge graph (KG)…」也是按有图写的。
3. **早退与文案。** `ask_reasoning` 里 `no_usable_kg = not memory_hits and not (has_kg or any_base_has_kg)`；
   `no_usable_kg and not _collections_reachable()` 时直接返回「本笔记本尚未构建知识图谱……请先点『构建知识图谱』」。
   `_collections_reachable` 依赖 `enumeration_wiring_active`：枚举 kill switch 关着时它恒 False，无图笔记本就整体回到拒答。
   放行后的响应仍带 `kg_required=True`（前端不读该字段，纯上报）。
4. **前端与注册表的闸。** `ask-modes.ts` 内置表把 reasoning 标成 `requiresKg: true`，`/ask-modes` 也下发 `requires_kg: true`；
   `use-ask-session.ts` 两处提交入口在 `requiresKg(mode) && !kgAvailable` 时直接拦截并弹「深入分析需要知识图谱」。
   后端能跑、前端不让跑，这是当前最明显的不一致。自动模式提交的是 `mode="auto"`，不受此闸影响，
   所以今天自动模式选到 reasoning 时已经在无图笔记本上跑那套残工具集。

另有一处**文档漂移**顺手修：`docs/product-and-api_zh.md`「尚未构建知识图谱的笔记本」段写「来源数刻意不算在这道闸里」，
但代码 `_collections_reachable` 自 codex R5 P1 订正起已把 `collection_map.sources > 0` 计入放行；英文档同段同样过时。

## 设计

### T1 `search_chunks`：原文段落检索一等动作 + 无图首轮播种

**动作语义。** 新增 reflect 动作 `search_chunks`：按语义 + 关键词检索来源原文段落（chunk），把命中并入 `state.chunks`，
进入合成时与 PPR/精确查找带来的 chunk 同分区、同预算（`chunk_context_chars`）、同引用绑定（`k1..N`）。零模型调用。

**检索实现。** 复用 chunk 模式的现成原语，不新写检索：

- 召回：`RetrievalService.retrieve_chunk_candidates(notebook_id, query)` → `(scored, ids, matrix)`（走 `_retrieve_chunks`，
  `chunk_recall` 候选池，来源范围天花板由 `_chunk_source_ceiling` 施加，`filter_retrieval_items` 过滤）。
- 选择：`RetrievalService.select_chunk_candidates(scored, ids, matrix, k=chunk_mmr_k, lambda_=chunk_mmr_lambda)`（MMR 多样性，与 chunk 单查询分支同参）。
- 并入：`take_distinct_chunk_hits(found, seen_chunks, chunks)` + `chunks.extend(new)`，与 `ppr_retrieve` 分支逐字同法（内容键去重、同段升级）。
- 策略边界：在 `ReasoningRetriever` 上加包装方法 `search_chunks(notebook_id, query)`，经 `retrieval_fanout_slot()` 与 `_filter_candidates("chunk", …)`，
  与 `ppr_retrieve`/`exact_lookup` 包装同形——knowhow 智能补全用它剔除私有 Memory 与当前表投影，新通道不得绕过。
- **不**并入关键词臂（`keyword_chunk_candidates`）与精确标识符臂（`exact_lookup_chunks`）：前者依赖 `expand_query` 的双语关键词，
  reasoning 的 plan 不产这个；后者已是独立动作 `exact_lookup`。
- **不**做 rerank：chunk 模式只在 mix 策略（有图）下 rerank；无图分支本来就是 MMR，对齐即可。

**决策参数。** `ReflectDecision` 加 `chunks_query: str = ""`；`reflect_schema_hint` 尾部字段组加 `"chunks_query":""`；
解析处对 `search_chunks` 不做必填校验（空则回退到 `question`，与 `elements_query`/`ppr_query` 同）。

**每 run 上限。** `Settings.reasoning_max_chunk_searches: int = Field(3, ge=0, validation_alias="REASONING_MAX_CHUNK_SEARCHES")`，
并入 `reasoning_action_policy` 作 `max_chunk_searches`；达上限记 `skip` 步（`reason="chunk_search_cap"`），与 `ppr_retrieve_cap` 同形。
默认 3 与 PPR/精确查找一致；它不是档位字段（同类上限都不在 `AskRetrievalLimits` 里，这里不开先例）。
连续无进展熔断（stale rounds）对它自然生效：`take_distinct_chunk_hits` 返回空即「无进展」。

**轨迹。** 执行步 `TraceStep(step_type="search_chunks", summary=f"检索原文段落:{q},新增 {n} 段", detail={"query", "found", "result_ids"[, "result_ids_truncated"]})`，
`result_ids` 走 `_capped_result_ids`（Agentic Memory P4 的硬规则：真正发起 I/O 的步无条件写 `result_ids`，skip 步不写）。
零命中计数并入 `zero_hit_by_action["search_chunks"]`，并把 `"search_chunks"` 追加进 `_ZERO_HIT_TRACKED_ACTIONS`（T6 步级零命中提示对它生效）。

**经验库词表（D-2）。** `retrieval_experience_projection.RETRIEVAL_ACTIONS` / `RetrievalAction` Literal 追加 `"search_chunks"`。
这是内容寻址的持久词表：追加值不影响既有条目的主键，只让新动作的 invocation/zero-hit 统计与 `consult_memory` 的经验条目能覆盖它。
不追加的后果是：新主检索通道在经验库里是盲区（与今天 `search_elements` 记成 `fallback` 步、被投影整步丢弃一样）。
推荐追加；连带 `retrieval_experience_prompt` 的动作清单会多一个词，相关 prompt 契约测试同步。

**无图首轮播种（确定性，不赌模型）。** 沿用 PPR seed / 精确查找 seed 的哲学：`_run_first_round` 在 `_first_round_exact_seed` 之后、
`_first_round_empty_fallback` 之前插入 `_first_round_chunk_seed(state)`：

- 仅当本 run `kg_in_scope=False`（定义见 T2）时执行；有图 run 一字不动（D-1）。
- 对首轮子查询（`state.subqueries`，已按 `max_initial_subqueries` 切片）逐条调 `search_chunks`，并发与 `_first_round_initial_search` 同形
  （`ThreadPoolExecutor`，每任务 `contextvars.copy_context()`），按子查询顺序收集后依次 `take_distinct_chunk_hits`。
- 每子查询 MMR k 取 `limits.ranked_per_query_take`（档位化：4/8/8/12/16），而不是 `chunk_mmr_k`：首轮是并发多路，
  合成侧还有 `chunk_context_chars` 兜底，用档位字段让「档位买更多首轮证据」的既有语义对原文同样成立。
- 记一条 `retrieve` 类步（`step_type="search_chunks"`，`detail.phase="seed"`），与 PPR seed 的 `phase` 记账口径一致；**不计入** `max_chunk_searches`（seed 非 agent 动作，与 PPR/精确 seed 同口径）。
- 查询 embedding 走请求级 `_ASK_EMBED_CACHE` memo，与 chunk 模式共享同一份缓存语义；seed 不新增模型调用。
- `_first_round_empty_fallback` 的触发条件 `not (collected or elements or chunks)` 不变——播种有命中时它自然不触发；全空时仍补一次 `search_elements`。

**kill switch。** `Settings.reasoning_chunk_search_enabled: bool = Field(True, validation_alias="REASONING_CHUNK_SEARCH_ENABLED")`。
关闭时：动作不进 schema/prompt/白名单、seed 不跑、`reasoning_max_chunk_searches` 无消费者——逐字节回到接入前（与 `REASONING_ENUM_TOOLS_ENABLED` 同一「off 就是旧行为」契约）。
T2–T4 **不**受此开关控制（它们是契约修正，不是可选特性）。

**前端。** `reasoning-trace.ts` 的 `NEXT_ACTION` 加 `search_chunks: "在原文段落里检索"`；`getTraceStepDetail` 对 `step_type === "search_chunks"` 渲染 `query` 与 `found`（与 ppr 步同形）。
无新组件、无新端点。

### T2 按「范围内是否有图」收缩动作空间与规划措辞

**判定。** `kg_in_scope = retrieval.has_kg(notebook_id) or retrieval.any_base_has_kg(notebook_id)`，
在 `_new_run_state` 里算一次挂到 `_ReasoningRunState.kg_in_scope`。两处 seam 与 `ask_service.ask_reasoning` 算 `no_usable_kg` 用的是同一对方法：
`any_base_has_kg` 已按勾选的参考库维度收窄（R1 保留：只看 base 维度）。
**不**把 `memory_hits` 算进 `kg_in_scope`——Memory 命中影响的是「有没有可用证据」（早退语义，见 T3），不改变「图动作有没有意义」。

**动作收缩。** `kg_in_scope=False` 时，以下五个动作从 `reflect_schema_hint` 的 `next_action` 枚举、`reflect_prompt` 的动作说明、
`reflect()` 的 `allowed_actions` 三处**同时**移除：`expand_graph`、`ppr_retrieve`、`expand_community`、`follow_chain`、`enumerate_kg_objects`。
schema 里它们的参数分支（`expand`、`follow_chain`、`community_focal`、`ppr_query`、`enumerate.object_type`）一并不出现。
`enumerate_elements` 与 `enumerate.collection="sources"` 保留（它们不需要图）。
实现上给 `reflect_schema_hint`/`reflect_prompt`/`reflect()` 加一个 `kg_actions: bool` 门，与既有 `enumeration`/`outline`/`consult_memory` 三个门并列同形。
`enumerate_kg_objects` 单独受 `kg_actions` 门：现在 `reflect_schema_hint` 只要 `element_kinds or object_types` 非空就同时挂两个 enumerate 动作，需要拆成两个独立分支。

**为什么这不违反「白名单不随 flag 改写」的先例。** 那条先例（`ppr_retrieve` 立下、`exact_lookup` 沿用）针对的是**部署配置**：
prompt 签名不该随运维开关漂移。`kg_in_scope` 是**每次 run 的笔记本事实**，与 enumeration（集合地图非零）、outline（档位）、consult_memory（档位 + 注入闸）一样是 run 级门，
三个先例都已经这么做。反过来，不收缩的代价是确定的：模型在无图 run 里选 `expand_graph` 之类，得到的只能是 skip 或空，白烧一轮反思与一次模型调用。

**规划措辞。** `plan_prompt` 加 `kg_available: bool` 参数。它是**门参数**而非 L2 数据注入块，与 `reflect_prompt` 的 `outline`/`consult_memory` 门同类：
`test_prompt_layers` 的双向对账只覆盖与已登记 block id 同名的形参，布尔门不需要、也不应登记进 `L2_BLOCKS`。
- `True`：现有文本逐字节不变。
- `False`：首句换成中性版本（「You plan how to retrieve evidence from a document library to answer an engineer's question. Sub-queries are run against source passages; the `types` field is ignored when the library has no knowledge graph.」），JSON 合同不变（`types` 仍可为空列表），解析器零改动。
- `expand_query.decomposition_guidance` 这个 L1 片段与 plan_prompt 的 backup 拼写按记忆里的约束同步处理（两处不能说出不同的计划）。

`reflect_prompt` 首句「for answering a question from a knowledge graph」同样按 `kg_actions` 切换为「from a document library」，其余不动。

**无图披露步。** 首轮开头（`_first_round_prompt_blocks` 之前）当 `kg_in_scope=False` 记一条
`TraceStep(step_type="skip", summary="本笔记本尚未构建知识图谱，本轮只用原文检索与集合清单", detail={"reason": "kg_unavailable"})`。
用 `skip` 类型是刻意的：经验投影按设计整步丢弃 skip（它是给人读的句子），不污染闭集词表。

### T3 早退、`kg_required` 与文案

**早退条件收窄。** `ask_reasoning` 的早退从「无图 **且** 集合不可达」改成「无图 **且** 集合不可达 **且** 范围内没有可检索来源」：

- 「范围内有可检索来源」= 参与集（active + 勾选的 base）内用户可见来源数 > 0，走 `collection_catalog.collection_map(...).sources` 已有的按参与者有界计数；
  但**不能**再经 `_collections_reachable`（它在 `enumeration_wiring_active` 为假时恒 False，会把 T1 的原文检索一起挡掉）。
  拆成两个判定：`_collections_reachable`（枚举工具语义，保留原样）与新的 `_scope_has_searchable_sources`（只读来源计数，不看枚举接线）。
- 结果：只有零源范围、或地图与来源计数都取不到（fail 到早退方向，与现状同）时才早退。

**早退文案。** 「本笔记本尚未构建知识图谱……请先点『构建知识图谱』」改为「当前检索范围内没有可用来源；请先添加来源，或在「设置 → 编辑当前笔记本」里挂载一个参考库。」
（与前端 `askUnavailable` 文案同口径）。结构化 Knowhow 批次的 `coverage_prefix`/`completeness_unavailable` 前缀逻辑不变。

**`kg_required` 字段。** 保留、语义不变（「本 run 范围内无可用图、且无 Memory 命中」），继续在三处如实写入（早退响应、检索器降级路径、正常合成路径）。
它从今天起是**纯披露**：前端本来不读它，MCP 与分享页原样透传。产品文档把它的解释从「需要先建图」改为「本轮未使用知识图谱；建图可增强」。

### T4 注册表、`/ask-modes`、前端提交闸

- `ask_modes.ASK_MODES["reasoning"]` 的 `requires_kg` → `False`。`AskMode.requires_kg` 字段保留：插件引擎描述符仍可声明 `requires_kg=True`，前端闸对它们继续生效。
- `frontend/app/ask-modes.ts` 内置表 reasoning 的 `requiresKg` → `false`。`requiresKg()`/`canUseMode()` 与 `use-ask-session.ts` 两处拦截逻辑**不删**（插件模式还要用），只是对内置 reasoning 不再触发。
- `scripts/check_ask_modes_contract.py` 今天只对账 id 与 `streaming`；本 PR 把 `requires_kg` 也加入跨栈对账，防止两侧再次不一致（这次就是这么漂的）。
- `page.tsx` 里以 `base_kg_available` 驱动的 tooltip/提示（挂参考库以获得图谱）保留：它说的是「有图会更好」，与新语义一致。
- MCP `ask_notebook`：现场没有任何 KG 门（`mcp_tools/*` 与 `mcp_server.py` 无 `has_kg`/`requires_kg` 引用），无需改动；加一条回归测试钉住「无图笔记本 reasoning 经 MCP 可得到非早退回答」。

### T5 无图时的「整理知识图谱」增强提示（用户追加需求）

界面里这件事的既有叫法是 **「整理知识图谱」**（`page.tsx` 来源面板与问答区按钮文案、`startKgBuild` 入口；`kgGraph.buildingKg` 时显示「整理中…」），
本 PR 沿用，不引入「构建」一词。无图时深入分析既然放行，问答区旁的小字从「需先整理」改成「可增强」，按钮保留并可点击：

- 问答区 `mode-hint`（`groupOf(askMode)==="strict" && !kgAvailable` 分支）：
  - 非范围阻断：文案「该笔记本尚无知识图谱，${strictLabel}将只用原文检索；整理知识图谱可增强问答效果」+ 既有「整理知识图谱」按钮（同 `startKgBuild`、同 disabled 条件、同「整理中…」态）。
  - 范围阻断（`kgBlockedByScope`）：文案「已整理知识图谱的参考库这次都没勾选，${strictLabel}将只用原文检索；在来源面板重新勾选可增强」，仍不给按钮（出路是勾回来，不是花钱整理）。
- 来源面板无图分支的 `tool-hint` 与按钮 `title`：把「需先整理知识图谱或挂一个参考库」改为「整理知识图谱可增强${strictLabel}效果；也可挂一个已整理的参考库」；按钮不变。
- 扩展引擎（`requiresKg` 为真的插件模式）的「当前扩展引擎需要知识图谱」提示**不改**：那是插件自己声明的硬前提。
- 借用参考库提示（`shouldShowBorrowedBaseHint`）不改。
- 按钮反馈契约不变（按下有可见变化、结果落在按钮自身：「整理中…」）。

### 文档

- `docs/product-and-api_zh.md` / `.md`「检索模式（问答）」表：reasoning 的「需 KG」→「否（有图时增强）」；一句话改为「agentic 迭代 plan → retrieve → reflect → answer；原文段落检索与集合清单不需要图，图动作只在范围内有图时提供」。
- 同文档「集合枚举工具 → 尚未构建知识图谱的笔记本」段：改写为本 PR 后的真实行为（原文检索一等动作、动作收缩、早退只剩零源），并修正上述「来源数刻意不算」的漂移。
- 「逐步推理档位」表下方补一句：`search_chunks` 每 run 上限由 `REASONING_MAX_CHUNK_SEARCHES` 控制（默认 3），首轮播种每子查询取 `ranked_per_query_take`。
- `docs/deployment-and-configuration*.md`：登记 `REASONING_CHUNK_SEARCH_ENABLED`、`REASONING_MAX_CHUNK_SEARCHES`。
- `fangan_todo.md`「Ask / Deep Report」：加「问答方法归一」条目，标注第一步为本规格，后三步待立。
- `ask_chunk` docstring 里「KG 不参与」订正为「KG overlay 可选」（注释级，顺手）。
- `architecture.md`「§3.3.2 集合枚举工具」：「接线判据与上面的总闸是同一个函数，关掉 kill switch 会同时恢复早退」已被 T3 废除，改写为单点判据 `_no_kg_scope_admits_run`（枚举接线 ∧ 集合非零，或 chunk 检索接线 ∧ 来源非零；两把闸都关才回到早退）。
- `docs/product-and-api_zh.md` / `.md` 第 107 行段（浏览器严格推理门控）：T4 后 `kgAvailable` 对内置 `reasoning` 不再决定可用性，只决定提示文案与插件（`requires_kg=true` 的扩展引擎）闸，改写为现状。

## 有图笔记本零变化的论证（回归红线）

| 接缝 | 有图 run 的取值 | 结果 |
| --- | --- | --- |
| `kg_in_scope` | True | T2 三个门全开：schema/prompt/白名单与今天逐字节相同；plan/reflect 首句不变 |
| 首轮播种 | 仅 `kg_in_scope=False` 触发 | 不跑，`state.chunks` 来源不变 |
| `search_chunks` 动作 | 有图时**也提供**（D-3） | 模型多一个可选动作；这是有图 run 唯一的可见变化 |
| 早退 | `no_usable_kg=False` | 分支不进 |
| `requires_kg` | 仅影响无图闸 | 无影响 |

唯一的有图侧变化是 D-3：`search_chunks` 是否在有图 run 里也提供。推荐**提供**——它对有图 run 也是合理的通道（补充 KG 节点证据之外的原文），
且「同一个动作词表随图存在与否只做减法」比「两套词表」更好推理。代价是有图 run 的 reflect prompt 多一段动作说明（几十字节）。
若要求有图 run 逐字节零变化，则把它也挂到 `kg_actions=False` 分支——实现上一行之差，等拍板。

## 成本与时延

- `search_chunks` 与首轮播种均**零模型调用**；每次调用 = 一次查询 embedding（请求级 memo 命中则免）+ 一次向量/词法召回 + MMR。与 chunk 模式单查询分支同量级。
- 无图 run 的反思轮数不增：动作空间缩小后模型更快收敛到 `answer`，实际预期是**减少**空转轮。
- 合成 prompt：无图 run 的「Retrieved chunks」分区从几乎为空变为按 `chunk_context_chars` 填满，token 成本上升到与 chunk 模式同级——这正是目标。

## 验收

契约测试（全部在 `backend/tests`，按现有文件归属）：

1. `test_reasoning_retrieval.py`：无图 run 的 `reflect_schema_hint`/`reflect_prompt`/`allowed_actions` 不含五个图动作、含 `search_chunks`；有图 run 与基线逐字节相等（用基线源码重渲染比对，不用 golden snapshot——AGENTS.md 禁 refactor-only 快照）。
2. `test_reasoning_retrieval.py`：`search_chunks` 动作把命中并入 `chunks`，同内容去重/升级，达 `max_chunk_searches` 后记 `skip`；`chunks_query` 为空回退 `question`。
3. 首轮播种：无图 run 在 exact seed 之后、empty fallback 之前记 `search_chunks`/`phase=seed` 步；有图 run 不记；播种命中后 fallback 不触发；每子查询取 `ranked_per_query_take`。
4. `test_trace_result_ids.py`：`search_chunks` 执行步无条件写 `result_ids`，skip 步不写。
5. `test_retrieval_experience_*`：`RETRIEVAL_ACTIONS` 含 `search_chunks`，investigation/zero-hit 统计对它生效（D-2 通过时）。
6. `test_ask_service_boundary.py` / reasoning 早退测试：无图有源 → 不早退、`kg_required=True`、响应含答案；零源 → 早退且文案为「没有可用来源」；枚举 kill switch 关 + 无图有源 → 仍不早退（T3 的关键用例）。
7. `test_prompt_layers.py` 现有对账继续通过（`kg_available` 不是 block id，不触发双向对账）；另加用例：`plan_prompt(kg_available=True)` 与 `reflect_prompt(kg_actions=True)` 的渲染与基线逐字节相等，`False` 时只有首句与动作段不同。
8. MCP：无图笔记本 `ask_notebook(mode="reasoning")` 得到非早退答案。
9. `scripts/check_ask_modes_contract.py`：`requires_kg` 跨栈对账；前端 `tests/unit/ask-modes.test.mjs` 同步 reasoning 的 `requiresKg=false`，`tests/component/use-ask-session.component.test.tsx` 确认无图 + reasoning 不再弹「需要知识图谱」；`tests/unit/reasoning-trace.test.mjs` 覆盖 `search_chunks` 的标签与详情渲染。
10. kill switch：`REASONING_CHUNK_SEARCH_ENABLED=false` 时 schema/prompt/seed/动作全部回到基线（同样用基线重渲染比对）。

人工抽问（放量前，因仓库无问答质量评测台）：在一个只做了纯文本解析、未建图的库上，用高级界面选「深入分析」各问一次
「点名子部件 / 周期性 / 方向」三句式与一句对比题，核对：轨迹首步是无图披露、首轮有 `search_chunks` 播种、
答案引用为 chunk `[k]`、`grounded` 与 chunk 模式同题答案不劣。同一组题在**有图**库上跑一遍，核对轨迹动作与改动前一致。

`bash scripts/check.sh` 全绿；PG lane 不涉及（无 schema 变化，`has_kg`/`any_base_has_kg` 两侧后端已存在）。

## 风险与回滚

- **有图 run 的 prompt 变化**只来自 D-3；若拍板「有图零变化」则完全没有。
- **函数长度基线**：`ask_reasoning`、`ReasoningRetriever.run`/`reflect` 都是热函数，仓库的函数天花板计到最后一条语句。新分支按 `_first_round_*` 的先例抽成独立方法（`_first_round_chunk_seed`、`_action_search_chunks`），不在 `run` 里再长；反思循环本体本就是「登记在案的下一件结构工作」，本 PR 不顺手重构它。
- **回滚**：T1 有 kill switch；T2–T4 是纯契约修正，回滚即 revert。无迁移、无持久格式变化（`RETRIEVAL_ACTIONS` 追加值对既有经验条目是无损的）。
- **codex 评审预期**：会盯「有图 run 逐字节不变」的证据（用例 1/7/10 就是为此）与热函数长度；`user_error` 若有动态实参需登记进 `test_user_error.ALLOWED_DYNAMIC_USER_ERROR`（本 PR 预计不新增）。

## 决议（2026-09-07，已拍板）

D-1 不做有图播种；D-2 `search_chunks` 进 `RETRIEVAL_ACTIONS`；D-3 有图 run 也提供 `search_chunks` 动作；D-4 上限默认 3。下文保留原推荐与理由。

- **D-1 有图 run 是否也做首轮原文播种。** 推荐**不做**：有图 run 的 chunk 分区由 PPR seed 填充，再叠一路会改变预算分配与引用构成，属于独立实验，应在有评测台之后做。
- **D-2 `search_chunks` 是否进 `RETRIEVAL_ACTIONS` 经验词表。** 推荐**进**：否则无图 run 的主通道在 Agentic Memory 里是盲区。代价是 `retrieval_experience_prompt` 动作清单多一个词及相应契约测试同步。
- **D-3 有图 run 是否提供 `search_chunks` 动作。** 推荐**提供**（理由见「零变化论证」）；若要求有图侧逐字节零变化，改为只在无图时提供。
- **D-4 每 run 上限默认值。** 推荐 3（与 PPR、精确查找一致）。备选 5（与 `search_elements` 一致）。上限是 Settings 项，随时可调，不阻塞。

## 实施切分（单 PR，逐任务委托）

| 任务 | 内容 | 主要文件 |
| --- | --- | --- |
| T1-a | `search_chunks` 包装方法、`ReflectDecision.chunks_query`、schema/prompt/白名单接线、动作分支、上限与 kill switch | `reasoning_retrieval.py`、`prompts.py`、`core/config.py`、action policy |
| T1-b | 无图首轮播种 `_first_round_chunk_seed` | `reasoning_retrieval.py` |
| T1-c | 经验词表与零命中追踪（D-2） | `retrieval_experience_projection.py`、`reasoning_retrieval.py` |
| T1-d | 前端轨迹标签与详情 | `frontend/app/reasoning-trace.ts` |
| T2 | `kg_in_scope`、三处门、plan/reflect 首句切换、无图披露步 | `reasoning_retrieval.py`、`prompts.py`、`prompt_layers.py` |
| T3 | 早退条件与文案、`_scope_has_searchable_sources` | `ask_service.py` |
| T4 | 注册表、前端内置表、对账脚本、MCP 回归测试 | `ask_modes.py`、`ask-modes.ts`、`check_ask_modes_contract.py` |
| Docs | 产品/部署文档、todo 登记、docstring 订正 | `docs/product-and-api*.md`、`docs/deployment-and-configuration*.md`、`fangan_todo.md` |

每个任务完成后分别做规格符合性评审与代码质量评审，再推进下一项（`CLAUDE.md` 子代理规范）。T1-a 与 T2 有共同接缝（`reflect_schema_hint` 的门参数），T1-a 先行、T2 在其上叠加。
