# 来源范围与引用可信度修复计划（2026-09-29）

来源：`fangan_todo.md` 中四条有正确性/安全含义的待办（KG `definition` 绕过来源勾选、集合枚举引用
不过来源天花板、mix 分支无当前库保底、全局引用复核缺指纹快照）。三份只读摸排（opus）确认实际
范围比待办描述的更宽，本计划按下面这条规则把同族缺口全部收进来。

## 交付规则（用户 2026-09-29 提出，本计划起生效）

**实现一个特性时，不允许把它的一部分遗留到之后再实现。**

- 计划写进去的范围在这一轮做完；范围大就拆成多个 PR **连续交付**，不砍范围。
- 实现或评审中发现的、属于该特性效果闭环的缺口，当场补进同一轮，不登记到 `fangan_todo.md`。
- 需要产品裁决的点在动手前问清，不用「先做一半」绕过。
- 子代理不得自行把范围内缺口登记为延后项；发现即报告并补齐。
- 本规则随 PR-A 写入 `docs/development.md` / `docs/development_zh.md` 的交付规则。

## 已裁决（采纳摸排建议，不需要用户拍板）

| 编号 | 裁决 |
| --- | --- |
| J1 | 全局作废文案按原因分三句：原文变化 / 文档被移除或隐藏 / 暂时无法核对。只有真实快照不一致才说「发生了变化」。 |
| J2 | 检索时刻已经悬空的元素 id 不再生成引用卡，单库与全局一致（单库每个 reasoning 回答多一次有界主键读）。 |
| J3 | 没有元素定位的引用（文档行、只有 `quoted_span` 的 KG 条目、子图节点）只做来源级校验（天花板 + 可见性），不做文本指纹。 |
| J4 | 保底席位只约束**机械切分**（重排 + token 预算、相关度 + 字符预算），不覆盖模型判断（证据精炼、大纲绑定）；与精确席位同一边界，写进文档。 |
| J5 | 按节合成不跨节注入未绑定的当前库段落。 |
| J6 | 原文段下限同时覆盖精确席位与当前库席位；唯一改变字节的是今天精确席位被结构化预览静默挤空的那类运行。 |
| J7 | 对等（全局）模式 mix 最终切分的逐库保底**纳入本轮**：已核实该路径生产可达（全局问答 `mode` 默认 `chunk`，重排已配置且任一参与库有图即走 mix）。 |
| J8 | 单库问答不新增引用复核（那里对任何通道都没有复核、也没有作废界面）；登记在单库是空操作。 |

## 用户裁决（2026-09-29）

| 编号 | 问题 | 裁决 |
| --- | --- | --- |
| Q1 | 簇融合描述在来源天花板下是否合法 | **严格**：簇内任一成员的证据来源在天花板之外就不用该描述（`NOT EXISTS 成员来源 ∉ 天花板`），改用范围内的 `defines` 证据或原文片段。 |
| Q2 | 枚举与 `read_document` 是否认来源勾选 | **认，且处处一致**：花名册、计数、引用卡、`read_document` 全部只看勾选的来源；收窄时工具继续可用。 |
| Q3 | 引用复核失效后的终态 | **不整份作废**。部分失败时告知用户「本次有部分失败」，答案照常交付、用户可以查看当前结果。J1 的三句文案改为随答案一起呈现的部分失败说明，失效引用逐条标注原因；PR-D 的消费方按此重新设计（见 PR-D）。 |
| Q4 | KG 详情面板是否过滤他人私有 Memory 派生文字 | **过滤**：按查看者「可见来源 ∪ 自己的隐藏来源」应用与 Q1 相同的严格谓词，描述与出处都不出现查看者无权看到的来源内容。 |
| Q5 | 保底席位比例是否改为动态 | **固定比例（方案 A）**，且该参数必须开放给部署管理员配置。对比过「按库内名次轮转」与「模型判定主体」两种动态方案后用户选定 A。落地：沿用既有部署设置 `CHUNK_FEDERATION_ACTIVE_RESERVE`（0..1，默认 0.25，0 = 关闭），本轮把它的作用面从 MMR / 配额两条分支扩到 mix、reasoning、按节合成、深度报告与对等模式逐库保底，原文段下限由席位数派生、随同一参数变化；补进 `.env.example`，部署文档双语写明作用面、取值含义与调参建议，启动期校验越界即拒启。 |

## PR 划分与顺序

PR-A 与 PR-C 互不相交，可并行；PR-B 依赖 PR-A（同改 `evidence_context.py`）；PR-D 依赖 PR-B
（同改 `collection_item_citations`）。每个 PR 都含双后端、文档双语、`fangan_todo` → `fangan_done`。

### PR-A　KG 对象上下文认来源天花板

范围：`definition` 两条产出路径、过程对象的 legacy `steps`、`RetrievalService.node_context` 的形状 bug。

- **A1 store（PG 先行，SQLite 镜像）**：`node_context(..., allowed_source_ids=None)`；新增返回字段
  `definition_basis`（`cluster_description` / `defines_evidence` / `defines_name`）、
  `definition_source_id`、`definition_element_id`。`defines` 查询加 `ORDER BY r.id`（今天是无序
  `LIMIT 1`），有界扫描 `NODE_CONTEXT_DEFINES_SCAN` 条，一次批量 enrich，取第一条在天花板内的证据；
  `defines_name` 无法归因，天花板生效时不返回；簇描述按 Q1 裁决的谓词放行；legacy steps 只保留
  过支持谓词的兄弟过程。谓词复用 `unified_kg_store._object_support_exists` 的单数组参数形态与
  `source_index_backfilled=0` 的权威分支。
- **A2 ports**：三处 `node_context` 协议加关键字参数（不新增方法，棘轮 966 不动）。
- **A3 服务**：`evidence_context._admit` 仅在天花板非 None 时下传，并加服务层兜底（`defines_*` 的
  `definition_source_id` 不在范围内即丢弃）；删掉 960–967 行的 ⚠ 注记。
- **A4 形状 bug**：`RetrievalService.node_context` 过滤的是 `row["evidence"]`，真实行只有
  `occurrences`，导致每次带天花板的运行都返回 `{}`、推理轨迹节点名退回原始 id。改为过滤
  `occurrences`，规则同 `evidence_context.py:938`；`test_source_scope.py:1948-1976` 的假行换成真实形状。
- **A5 KG 详情面板（Q4）**：`/objects/{id}/context` 与概念详情的读路径按查看者「可见来源 ∪ 自己的
  隐藏来源」传入 `allowed_source_ids`，描述、`defines` 证据、出处（occurrences）、legacy steps 同一
  谓词过滤；前端面板在描述被过滤时不留空壳（回落到范围内的定义或不显示该行）。
- **测试**：PG conformance（无天花板值不变 + 新字段；第一条 defines 越界落到第二条；`defines_name`
  在天花板下丢弃；Q1 谓词混合簇/全内簇；未回填权威分支；5k id 单参数；legacy steps 过滤）+
  EXPLAIN pin；服务层变异（store 忽略参数时兜底必须拦住）；真实 store 的轨迹节点名用例。
- **文档**：`product-and-api*.md`「第二次读」段与 `/objects/{id}/context` 响应说明；交付规则写入
  `development*.md`。

### PR-B　集合枚举与 `read_document` 认来源天花板

范围：要素计划与计数、KG 页与计数、`_evidence_refs`、引用卡、指纹、总闸、披露文案、前端结果卡；
`read_document` 延后项 (a) 并入。来源花名册本身已认天花板（`collection_catalog.py:529`）。

- **B1 store**：`knowledge_object_page_rows(..., allowed_source_ids=None)` 把来源谓词下推到 LIMIT 之下
  （keyset 不变）；`count_knowledge(..., supported_by_source_ids=None, excluding_owner_source_ids=())`
  扩展既有方法。双后端 + EXPLAIN pin。
- **B2 catalog**：要素计划在 memo 之后按 `scope.allows` 过滤；计数在天花板排除了 signal 时改用
  逐来源 L1 重算；三个指纹只哈希天花板内的 signal（天花板外的上传不再触发 `concurrent_change`，
  天花板内的重解析仍触发）；KG 计数 memo 键加天花板摘要（有界 LRU）；`knowhow_tables` 只数执行器
  够得着的表。
- **B3 executor**：`_explicit_source_plan` 拒绝天花板外 id；`_evidence_refs` 先按天花板过滤再截断；
  KG 下推沿 `_usable_kg_page` 贯通。
- **B4 引用**：`collection_item_citations` 对文档/要素/KG 行都过 `source_allowed`，取第一条**在天花板内
  且存活**的元素。连带修掉一个线上 bug：全局问答里对等库 Knowhow 投影对象的引用会让整份答案以
  `out_of_ceiling` 作废。
- **B5 总闸**：`enumeration_active()` 不再看 `_unsafe_scope_restricted()`，收窄与漂移下工具继续提供。
- **B6 披露**：`CollectionEnumerationOutcome.source_scoped`（`default_factory`，不动热函数）、
  `SELECTED_SOURCES_SCOPE_SUFFIX="（仅勾选的来源）"` 进轨迹/账目/分区表头/reflect 提示词；
  `TypedCollectionResult.source_scoped` 进前端结果卡标题与 key。
- **B7 `read_document`**：动作体内加 `source_allowed` 兜底。
- **行为变化（写进文档）**：天花板生效时无证据的 KG 对象不再被列出；挂载库的 `knowhow_tables` 计数
  不再出现在集合地图。
- **测试**：翻转 `test_source_scope.py:1017`、`test_reasoning_document_read.py:738`；枚举三类在
  include 天花板下的分母与 `complete`；指纹变异；越界 refs 超过上限仍引到界内元素；全局 Knowhow
  对象不再作废；PG twin；前端组件测试。

### PR-C　当前库保底席位与原文段下限

范围：mix、reasoning、按节合成、深度报告四条机械切分路径；结构化预览挤空原文段；对等模式逐库保底。

- **C1 判据与席位单点**：`retrieval.is_active_hit(hit, active_id)`（必须先过 `foreign_notebook_id`
  归一化——PPR 腿与生成问题水合腿给当前库段落盖的是当前库自己的 id）；
  `active_reserve_eligible`（非 GQ-only、非 graph-only、`exact_lookup` 或相关度 ≥ 地板）；
  `chunk_federation.active_reserve_seats(settings)`（对等模式为 0）。`enforce_active_floor` 与
  `_qualified_active` 改用同一判据。
- **C2 mix**：`active_reserve_rule` 作为**最后一条**规则（不挤掉图谱/精确席位持有者）；无外库基线行时
  `reserve=0`（单库字节一致的保证）；相同文本只占一席；不扩预算。`ask_chunk` 改为一次
  `mix_reserve_rules(...)` 调用，天花板 332 同 diff 下调。
- **C3 reasoning / 按节 / 报告**：`order_reasoning_passages` 增第 4 步，精确前缀之后稳定提升至多
  `席位 − 精确前缀已含的当前库段落数` 条合格当前库段落；只重排不丢弃。
  `report_engine._section_passage_order` 传入席位与 `notebook_id`。
- **C4 原文段下限**：`floor = min(chunk_context_chars // 2, Σ 前缀段落 (len(text)+120))`；结构化侧
  （knowhow 预览、枚举子预算、文档读取、表格）按 `chunk_context_chars − floor` 渲染，使各块自己的
  覆盖披露保持真实。`_draft_reasoning_response` 抽 `_assemble_structured_evidence`，天花板 698 同
  diff 下调。不新增 Settings，数值登记在 `product-and-api*.md`。
- **C5 生成问题水合腿**：`retrieval_candidates.py:3932` 盖章改走 `foreign_notebook_id`。
- **C6 对等模式逐库保底（J7）**：mix 最终切分增加逐库分席规则，复用精确席位的
  `library_seats` / `libraries_by_best_hit`，判据与 C1 的合格谓词一致；无「当前库」概念，按库公平。
  席位总数同样取 `ceil(k × CHUNK_FEDERATION_ACTIVE_RESERVE)`，按最佳命中顺序分给各参与库。
- **C7 部署配置（Q5）**：`CHUNK_FEDERATION_ACTIVE_RESERVE` 写进 `.env.example`；`core/config.py` 注释与
  `docs/deployment-and-configuration*.md` 更新为完整作用面；不新增第二个参数。
- **测试矩阵**：黄金 fixture 不变；单库 mix/reasoning/报告字节一致；小当前库 + 大参考库保住
  ≥ min(席位, 合格数) 条；无合格候选不浪费席位；精确 + 当前库在紧预算下的优先级；同文本一席；
  结构化块撑满时原文段仍含前缀；按节；报告；对等模式；50 次等分洗牌确定性；八项变异。

### PR-D　全局引用复核的检索时刻指纹

范围：五个非联邦生产者（文档概览、集合枚举、KG 对象、`follow_chain`、表格分析）+ 答案锚点 +
分原因文案 + 静态守卫；是否含重合成入口按 Q3。

- **D1 契约与助手**：`domain/evidence_fingerprint.element_text_sha`（SQLite 两处与
  `chunk_federation._text_sha` 共用，PG 仍在 SQL 里哈希，twin 钉两侧摘要相等）；
  `services/evidence_attestation.py` 的 `attest_read`（生产者自己读过原文，进程内哈希、零额外读）与
  `attest_pointers`（只引用未读原文，一次批量 `evidence_fingerprints`，受 `read_budget` 约束、按 run
  memo，返回 live/dead/unknown）。都只经 `current_federated_run_plan().on_evidence` 发布，无第二套
  机制；无 plan 时立即返回。读取器经 `global_ask_ports.py` 的窄 Protocol 注入，不动 `ports.py`。
- **D2 消费方**：`record_evidence` 改为首个真实快照获胜；锚点纳入校验（今天 `follow_chain` 的锚点
  连天花板都没核对过）；新增原因 `source_gone`、`unattested`；文案按 J1 分三句；终态读扩到锚点元素。
- **D3–D7 五个生产者**逐个登记（键 = `element_id`，值 = `(source_id, sha(全文))`，枚举要素在截断为
  摘录**之前**取全文）。
- **D8 接线**：`repository_runtime.py` 注入读取器。
- **D9 静态守卫** `test_citation_attestation_guard.py`：每个 `Citation(` / `AnswerAnchor(` 构造点与
  id_map 的 `"element_id"` 写入必须登记为 联邦 / 读登记 / 指针登记 / 无元素 / 对等模式关闭 之一。
- **测试**：改写被钉住的放行用例为「已登记被删 → changed」与「无人登记 → unattested 且文案不是
  changed」；每个生产者的真实仓库竞态用例（终态读之前 UPDATE/DELETE 被引元素）；同文同 id 重插不
  作废；提问前就悬空的 id 照常交付；PG twin（CJK / emoji / CRLF / 组合字符 / 非 UTF 库）；五项变异。
- **可观测**：内容无关事件 `producer_evidence_attested` / `producer_evidence_unavailable`。

## 执行纪律

- 每任务：实现子代理 → `spec-review` → `code-quality-review`（两份评审分工，变异实验独占源文件、
  先 commit 再变异）→ 成立的 finding 全部修完 → 下一项。
- 每 PR：rebase 到最新 master → `bash scripts/check.sh` + PG lane → push → codex 闭环（成立的
  finding 修到没有新问题）→ CI 三个 check 按 head SHA 全 success → `verify` → `gh pr merge --rebase`。
- 红线：不降检索性能（新增 SQL 配 EXPLAIN pin）、单库无挂载运行字节一致、热函数天花板与 ports
  棘轮零松弛。

## PR-D 消费方设计定稿（Q3：部分失败而非整份作废）

事实更正：全局问答没有逐来源勾选，冻结天花板 = 每个参与库的全部可见来源；来源「不再可见」只可能是
行被删除，权限丢失只发生在笔记本级，由既有的 `_check` 处理（作业失败并提示重新选择范围），不变。

- **线上形状（全部 additive、空则不序列化）**：`Citation.verification` / `AnswerAnchor.verification`
  ∈ `changed` / `source_gone` / `unverifiable`；`AskResponse.citation_check`（`outcome`、`checked`、
  `failed` 与三类计数）仅在 `failed > 0` 时出现；公开页 `PublicReference.verification`、
  `PublicTurn.citation_check`；新增投影 `global_answer_check(job)`。单库响应与黄金 fixture 字节不变。
  内部原因码（unattested / unreadable / unattributed / out_of_ceiling）只进事件，不上线。
- **证据等级**：部分失败时 `grounded=False`、`evidence_level` 封顶 `overview`（`inferred` 不抬升）。
- **失效引用卡**：保留在列表里并保留摘录，不提供打开原文/图谱/Knowhow/图片等入口，显示原因行；
  后端 `cited_element` 对带标记的引用返回 404。删除卡片会在正文里留下裸 `[k]` 标记，故不删。
- **文案**：答案下方一条说明「本次回答有部分引用未通过核对：…。回答内容照常保留，带标记的引用可点开
  查看原因。」；卡片标签「原文已改动 / 资料已删除 / 无法核对」。分享页用过去时，状态是回答时刻的快照。
- **校验改为全量**：逐条判定、不再首个失败即退出；新模块 `services/global_citation_check.py`，
  读取口加在 `global_ask_ports.py`（不动 `ports.py`）。
- **呈现面（十处全部纳入）**：`answer-panel`（含全局窗口与管理端活动详情）、`citation-card`、
  `answer-markdown` 标记样式、内联图片跳过、全局窗口、公开页 `/c/{token}`、MCP `get_global_ask`
  （摘要放进 `coverage.citation_check`，顶层键数仍为 20）、MCP/HTTP 引用元素下钻、SSE/作业/会话详情、
  前端类型镜像。
- **持久化**：随 `payload_json` 落库，无迁移；历史上已作废的行保留原句。
- **学习链与事件**：部分失败的回答照常进学习链；新事件 `global_ask_citations_partial`（内容无关计数）。
- **轨迹**：reasoning 模式追加一步「核对」，如实写已核对条数与未通过条数。
- **已裁决的小项**：会话列表不加角标（重开该轮即见说明，与失败/停止轮一致）；MCP 不新增锚点输出。
- **待用户裁决**：唯一的「不展示正文」例外——核对**证明**某条被引来源是提问人无权查看的
  （他人的私有 Memory、非参与库）时是否仍不展示回答。
