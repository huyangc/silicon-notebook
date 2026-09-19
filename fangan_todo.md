# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-09-08
对照：`silicon_notebook_fangan.md`（产品方案）。已完成项见 `fangan_done.md`；本文件只列**尚未做完**的部分。

> 规则：完成某项后，从本文件移除并补进 `fangan_done.md`（见 `AGENTS.md` 的「Documentation Ownership」）。
>
> 本版是 2026-09-07 按当前代码逐条对账后的重写。上一版（2026-05-29）里以下条目已经落地，
> 已从本文件移除：关系图改为 id 级 `knowledge_relations` + 力导图可视化；参考文献/索引等
> backmatter 小节在切窗阶段跳过（`kg/filters.py`）；合并候选批量单事务写入；rep-pair 配对改
> hnswlib ANN；分窗改贪心打包、并发按模型服务 `max_concurrency`；抽取回传 `ev:<int>` 做
> element-id 证据锚定；`knowledge_embeddings` payload 级向量进 `score_knowledge`；账号系统、
> 会话管理、分享链接、群组共享；Knowhow 版本历史/diff/回退；逐步推理大纲协同
> （PR #407/#411/#418）；在途提问接回（PR #661/#662/#664/#665）。Article Studio 与
> schema profile 抽取已退役，相关条目一并删除。

## 状态速览

- 产品主线已上线：KG-native 抽取 + 混合检索 + Ask（chunk / reasoning，含意图澄清与大纲协同）
  + Deep Report + Memory / Knowhow + MCP + 群组分享 + 部署插件扩展点。
- 2026-08-29 起的「生产热路径修复计划」（批 0 / 1 / 2 / 3·W1–W4）全部实施项已合入（至 PR #676）。
  剩余为用户侧生产动作与各设计稿登记的残余债。
- 本文件按「近期可动手」→「产品功能」→「长期方向」排列。可直接实现的项已排进
  `docs/superpowers/plans/2026-09-07-todo-closeout.md`（PR-1～PR-5），需拍板的项留在本文件。

---

## 一、生产热路径修复：收官后剩余

### 用户侧动作（需要生产环境，仓库内无法代做）

> T-0 生产只读测量、PostgreSQL 调参 runbook、批 1 索引 `--apply` 三项已由用户在生产环境完成
> （2026-09-07 告知）。SR-1（element 搜索腿 OR→UNION）已被差分测量证伪撤销，不再重试。

- [ ] **backfill-images 放量前**：原图缺口盘点只命中 12.3%，要么找回其它 MinerU output 根，
      要么接受部分回填；先挑 1–2 个来源用 `--source-id` 试点，在问答里亲眼看到引用带图再放量。

### 登记的残余债（有真实需求再做；真源为各设计稿的「残余债 / 登记」节）

- [ ] **多 worker 部署下进程内互斥失效**（W1 #10 / W2 #1）：正解是把 relink / unifiedkg /
      conflictresolve 的 claim 提升为 durable 行，而不是给删除加锁。当前沿用「生产单 worker」契约。
- [ ] **W1**：`_delete_indexing_pipeline_stages_in_batches` 级联未分批；
      `agent_access_tokens.default_notebook_id ON DELETE CASCADE` 语义登记不改；
      守卫若在 `scripts/` 扫出新的可见性谓词站点，逐条处置。
- [ ] **W2**：双代窗口时长无硬上界告警；`rebuild_canonical_relations` / `mention_bridge` 若仍是
      DELETE+INSERT 需并入代际协议；「翻转后 finish 前崩溃 ⇒ 下次全量重算 + 状态计数陈旧」
      是已知格。
- [ ] **旧索引退役债**：`idx_chunks_source`、`idx_clusters_nb_canonical`、0043 / 0007 原形索引
      均已被新索引前缀覆盖，登记为可回退的写放大冗余，尚未下线。
- [ ] **W3**：大库切换索引管线目前只是按活跃对象数阈值显式禁用（切回内建豁免）；
      「保存索引管线」发布事务的真正重构排到有真实需求时。
- [ ] **W4**：未回填老库（`source_index_backfilled=0`）的 legacy evidence 分支单语句可撞 30s，
      前置是既有离线 backfill；来源页签搜索改 UNION 后 `paper_meta_for_sources` 水合腿未跟进同口径。
- [ ] **PG 打开路径计数缓存的刻意延后项**：checkup H4/H5 缺向量 anti-join COUNT 仍是 30s TTL memo；
      SQLite 侧 pending memo 的全局 epoch 有跨库误伤弱点。

---

## 二、产品功能待办

### 架构与清理

- [ ] **前端 `page.tsx` 继续减负（阶段 5 三片已交付后的后续）**：三片合入后 `frontend/app/page.tsx`
      约 8000 行，剩余大块为问答区、知识库浏览器与工作区壳；按同一纪律（零行为变化、守卫重指向、
      组件测试）再分片，有需要时立项。

### Ask / Deep Report

- [ ] **问答方法归一（路线，共四步，第一步已实施，第二步 ✅ 已合入 PR #693）**：① KG 可选的
      reasoning——`docs/superpowers/specs/2026-09-07-reasoning-kg-optional-design_zh.md`
      （T1–T5），原文段落检索一等动作 `search_chunks` + 无图首轮播种、按图存在收缩动作
      空间、早退收窄为零源、注册表与前端闸放行，已落地；② 无图首轮并入关键词臂 + 首轮后
      模型判定直接作答的成本契约（规格 `docs/superpowers/specs/2026-09-07-reasoning-chunk-parity-keyword-arm-design_zh.md`），
      ✅ 已合入 PR #693；③ ✅ 2026-09-14 用户裁决直接下线请求级 `auto` 选择器，简化界面固定
      `reasoning` 标准档（本 PR），原「自动模式灰度」不再做；④ 前置：chunk 独有能力补进
      reasoning——PR-A `read_document` ✅ #724；PR-B 引用卡 + 精确席位 ✅ #728；PR-C 无 intent
      跟进改写（本 PR；MCP `ask_notebook` 已由 #721 的调用内理解步覆盖，不在此改）；
      ④ 退役 chunk 流水线，待立规格。
- [ ] **无条件跟进改写作为后续开关（PR-C 延后项）**：PR-C 只在确定性澄清闸命中时才读会话
      历史并改写；chunk 模式那种「有历史就改写」的无条件形态若要给 reasoning 直连路径，
      是一个独立开关（多一次改写模型调用换更好的检索词），用户裁决时明确不做。
      ④ 的硬前提：做 chunk/reasoning 对照前必须确认**界面路径的 chunk 向量检索基线已修复**
      （`docs/superpowers/specs/2026-09-07-scoped-chunk-vector-lane-design_zh.md`，已由 PR #697
      合入），否则两臂对比测的是一个坏掉的对照组；原「自动模式下 4 比 3 调用数」前提随
      auto 下线作废——现在两种界面都是意图预检 + reflect + answer，直答比 chunk 只多一次 reflect。
      v1（固定「直答」档位）与 v2（先合成、不足再查）均已撤回：用户裁决不设固定直答选项、
      问题理解与子问题检索不能省、检索效果优先于省一次调用；v2 的先合成本可把首轮后的
      reflect 并进合成再省一次调用，但代价是拆 `run()` 的重构与判定质量的不确定，按效果
      优先撤回。放量前两条待办：
      (a) 无图首轮播种目前逐子查询串行调用 `search_chunks`，改用多查询合并召回
      （`RetrievalService.retrieve_chunk_candidates_multi`）之前，需先在大库上实测
      并发度 N=8 时的耗时；(b) 该规格「验收」一节给出的人工抽问（点名子部件 /
      周期性 / 方向三句式 + 一句对比题，无图库与有图库各跑一遍）未做。
- [ ] **问答纠偏规则 12（限定词保真）人工 A/B**：仓库无问答质量评测台，放量前用「点名子部件 /
      周期性 / 方向」三句式各问一次验证；ledger 喂摘要未做。
- [ ] **`read_document`（PR-A）已登记的延后项**：(a) `source_scope` 真正收窄了检索范围时，
      整套枚举工具连同 `read_document` 一并不提供（与枚举同一道闸）——需要把来源清单本身做成
      按选中来源可寻址（source-addressable）才能在收窄范围下继续工作；(b) 清单里**同名**的第二篇
      文档无法按标题精确匹配（动作的参数就是标题），只能被跳过并提示模型改读别的文档，尚无第二个
      消歧维度（如序号）；无标题文档已可按花名册占位串「未命名来源」读取，不再属于这一条。
      (c) **取样通道的 I/O 放大**：`prepare_source_overview` 对每个元素调一次
      `source_elements_page(offset, limit=1)`，而每次调用含「来源存在性检查 + `COUNT(*)` +
      单行窗口」三条语句——standard 档一篇取 5 个元素即 15 条语句、加两次 generation 读约 17 条
      往返，一个 run 读 4 篇约 70 条；exhaustive 档一篇 16 个元素约 50 条，一 run 约 200 条。
      优化方向二选一：给 `SourceStorePort` 加「按一组 offsets 批量取元素」的原语，
      或让首页之后的每次取页跳过存在性检查与计数（总数在首页已经拿到，执行体的
      `stable_count` 只需要最后再核一次）。零锚点路径与目录补摘要通道共享同一份收益。
      规模现状可接受（每次读取的元素数已由字符份额反推压到个位数），所以登记而不在 PR-A 内做。
- [ ] **`structured_block` 挤空 reasoning 的原文段（PR-B 风险 a）**：`_answer_reasoning` 里
      knowhow 整表预览与集合地图先于 chunk 段装配、共用同一份 `chunk_context_chars`，整表足够大
      时 chunk 段可以一条不剩——`REASONING_EXACT_RESERVE` 的前缀席位只在 chunk 段拿到字符时才
      有意义，救不了这一种。需要的是给 chunk 段一条下限夹（或给结构化预览一个上限），量纲与取舍
      都是独立决定，PR-B 不做。
- [ ] **reasoning 原文段引用卡重复三次批量读**：`_draft_reasoning_response` 的原文段腿对同一批
      chunk 再做一次 `tier_map` / `citation_source_info` / `knowhow_refs_for`，而合成前的
      `chunk_context` 已经做过（按节路径本来就是 N 次，这里再 +1）。形状与 chunk 模式一致、
      KG-only run 零开销，所以只登记：把装配期结果沿 `baseline_sink` 带出来复用即可省两次。
- [ ] **Prompt 三层化后的 per-notebook 定制与 self-evo**：接缝只有 `fragment_text()`；L1 片段分
      两类（A 类离线 GEPA + 人审，B 类只改示例槽位），尚未拍板开放。
- [ ] **Agentic Memory 注入开闸与 A/B**：P1–P4 已合入，注入默认关闭，开闸是独立决定。
- [ ] **无图披露步文案「构建知识图谱」→「整理知识图谱」**：`reasoning_retrieval.py` 那条
      `kg_unavailable` 披露步的 `summary` 违反界面词汇表，但它被 `docs/product-and-api*.md`
      逐字冻结（文档明写「含其中的半角逗号」）、并被 `tests/fixtures/repository_contract/
      ask_responses.json` 与 `test_reasoning_retrieval.py` 钉住。眼下登记在
      `scripts/check_ui_vocabulary.py::GRANDFATHERED_TRACE_SUMMARIES`（逐字全串例外，改一个字
      就重新违规）；改它要同改文案、两份文档、既有用例与黄金 fixture，是独立的一次改动。

### 知识图谱

- [ ] **节点属性 attrs 形态未定**：`Node` 仍只有 name / section_path / evidence / mentions /
      steps / validity_scope；决定前不要再往节点加字段（`scripts/kg_strip_attrs.py` 头注释同此）。
      候选：Concept `aliases[]`/`kind`/`definition`、Claim `quantitative_values{}`/`polarity`、
      Formula `variables{}`/`role`。决策牵动抽取 prompt、`models.Node`、canonicalize、评测维度。
- [ ] **gold 人工策展**：`fangan/testcases_kg/` 仍在 `.gitignore`，未有策展后的权威 gold 入库。
- [ ] **跨文档概念合并的真模型质量验证**：Embedder 现只有 openai / dashscope 协议 + FakeEmbedder
      （本地 BGE 路线已随 model_registry 退役），真模型下灰区候选量未做正式 smoke。
- [ ] **推理分层**：边类型已有 supports / contrasts_with，无 extends、无 Level 0–4 分层、无
      Hypothesis 对象。
- [ ] schema 归纳只提议新类型，不对既有类型提议新字段。
- [ ] KG refine 自我修正只有总开关 `KG_REFINE_ENABLED`，无抽样 / 比率控制。
- [ ] **KG 对象 `definition` 的来源归因**（`fangan_done.md` 第 38 条的残余口）。
      `knowledge_context` 的 snippet / 引用已按来源天花板过滤，但同一次装配用的
      `node_context(...)["definition"]` 过不了闸，而且过不了是结构性的：store 侧它由两条
      不同的路产出——概念簇的 `canonical_description`（对象级 LLM 融合描述，归因不到单一
      来源）与 `defines` 关系那个源对象证据的首条原文（有来源，但没有随字符串返回）——
      服务层拿到的只是一个字符串，无从判断来自哪一条。后者今天在**单库取消勾选来源**下
      仍可能把范围外的原文当作定义写进提示词。关掉它要给 `node_context` 的返回加上
      definition 的来源归属（或按 basis 分成两个字段），属于返回形状变更，牵动双后端与
      `EvidenceKnowledgeContextPort`；前者要先决定「对象级描述在收窄范围下是否还算合法
      证据」，那是一次产品裁决而不是过滤改动。
- [ ] **集合枚举的引用不过来源天花板**（同一族，另一条通道）。`collection_enumeration`
      的 KG 行按 `knowledge_objects.evidence` 原样取 `evidence_element_ids`，
      `evidence_context.collection_item_citations` 再从中选第一条活的元素建引用卡——两步
      都只认**库维度**（参与集），不认来源天花板。今天这是自洽的：整个枚举面（花名册）
      本来就不按来源收窄，只修引用那一半会变成半套语义。要不要让「我的库里有哪些文档 /
      对象」也认来源勾选，是一次产品裁决（收窄范围时枚举总闸已经会关闭，见
      `docs/product-and-api_zh.md` 的 `read_document` 一段），拍板后再一并改两步。

### 检索

- [ ] **「中文问句检索英文语料首轮空」的三条次因（此前未登记）**。主因——冻结来源范围
      关掉 chunk 向量通道——已修（`docs/superpowers/specs/2026-09-07-scoped-chunk-vector-lane-design_zh.md`）；
      排查过程中另外过了三条，逐条登记如下：
      - ~~相关度地板对跨语言查询过高，把语义命中滤掉~~：**已用离线对照集证伪**。同一库
        同一题，无范围时 134 命中照常返回，地板没有在跨语言查询上多滤掉任何东西；零命中
        完全由「向量臂根本没跑」造成。不要再把这条当成因。
      - **检索查询携带整段问题契约文本**：服务生成的已确认意图查询把完整问题契约保留给
        候选生成与语义 embedding（见 `docs/product-and-api_zh.md` 混合检索一节），只有
        KG 关键词/RRF 打分使用契约分隔符之前的检索方向。整段契约文本进 embedding 会把
        查询向量拉向「契约模板」而不是「这道题」，跨语言时尤其伤。待办：先量化契约前缀
        对查询向量的偏移（同一道题的裸问句 vs 带契约版本，比命中集与 top-k 相似度），
        再决定要不要在 embedding 一侧也只取分隔符之前的方向。
      - **高级界面确认合同后 `plan()` 整个不执行 → 关键词臂不跑**：正式 UI 路径永远带着
        已确认意图（`reviewed_queries` 非空），`ReasoningRetriever` 因此刻意跳过
        `self.plan()`（`backend/app/services/reasoning_retrieval.py` 约 3637 行的注释写明
        了这条纪律），而无图首轮的词法臂用的正是 `plan()` 里 `expand_query` 产出的高/低层
        关键词。结果：走高级界面的 run 只剩语义臂，词法臂在最需要它的跨语言场景缺席。
        与下方「词法臂关键词双语化」是同一处的两个问题（一个是不跑、一个是跑了但语言对
        不上），做的时候一起定。
- [ ] **空笔记本 + 无图参考库：原文通道已能跨库检索，端到端仍不可用**。联邦 chunk 通道
      落地后，`_retrieve_chunks` 确实会对每个参与库各发一条召回腿，`AskService
      ._no_kg_scope_admits_run` 的放行判据也已改成参与集口径（`collection_map.sources`）。
      但 HTTP 上够不着这一支：`NotebookSummary.ask_available` 的参考库判据是
      `base_kg_available`（挂载参考库**有可用知识图谱**，`query_store
      .notebook_has_usable_base_kg`），一个无图参考库撑不起它，于是前端 `isAskBlocked`
      保持输入框禁用、直连 `POST /ask` 被 `_require_ask_available` 拦成 409。修复需要一个
      「任一参与库有可检索的原文（chunk）」的可用性判据，并同时改 `ask_available` /
      `_require_ask_available` / 前端三处同源口径；默认配置下
      `enumeration_wired and collection_map.sources > 0` 已先放行，所以这条缺口只在枚举
      工具被关掉时单独暴露。刻意不在联邦 PR 里扩范围。
- [ ] **mix 分支没有当前笔记本的保底席位，跨库放开后风险变大**。`chunk_federation`
      给另外两条选择分支都做了 active 保底（MMR 走 `apply_active_reserve`、配额融合走
      `_reserve_lanes`，见 `CHUNK_FEDERATION_ACTIVE_RESERVE`），mix 分支刻意没做——当时
      的论据是「mix 的序来自对整池的 rerank 模型、截断是 token 预算，保底要嵌进
      `select_with_reserves_baseline_first` 的既有 reserve 规则里，不能在召回侧硬塞」。
      结构性论据仍然成立，但**前提已经变了**：做那个裁决时 mix 的第 2 路（KG-overlay
      源 chunk）只在当前库内反查，池子里必然有当前库的原文；`_kg_source_chunks` 拿到
      跨库反查之后，三路可以**全部**是参考库的。触发形态：当前笔记本只有两篇短笔记
      （relevance 0.30–0.40），挂着一个强命中的大参考库 → rerank 之后 token 预算内一条
      当前库的原文都不剩，用户问的是自己刚上传的文档，答案却全部引自参考库。修法方向：
      在 `select_with_reserves_baseline_first` 的 reserve rules 里加一条 active 通道
      （判据仍是 `not hit.notebook_id`，与另外两条分支同一个「空 = 当前库」口径），
      席位数复用 `CHUNK_FEDERATION_ACTIVE_RESERVE × CHUNK_MMR_K`，合格候选不足时以实际
      数量为上限。刻意不在联邦 PR 里做：那个函数的 reserve 规则是独立一处改动。
- [ ] BM25 / FTS5 / tsvector 全文索引：已评估为低 ROI、基础设施级，暂缓。
- [ ] 结构化硬过滤：软加权已够用，硬过滤有清空结果风险，暂缓。
- [ ] **`_federated_graph_is_large` 把取消勾选的参考库也算进「图是否过大」**：这个
      判据（`backend/app/services/retrieval_candidates.py`）在**无参与集覆盖**时读
      挂载表的裸 id 清单，从不过 `notebook_in_scope`。于是一个刚被用户取消勾选的大
      参考库仍然把它撑成「大图」，本次 run 范围内每个库都很小时，`_chunk_kg_overlay`
      与 PPR 回退照样以 `reason=large_notebook` 拒绝，事件也因此误描述了这次 run 的
      语料。**不要单独收窄这个守卫**：它守的两张图在无覆盖时仍然按全部挂载库建
      （`graph_retrieval._federated_rx_graph` 读 `participant_rows`、`_ppr_graph` 读
      `participant_ids`，库维度是在走查**结果**上由 `scoped_subgraph_nodes` 过滤的），
      只放松守卫会放行它本来要拒绝的那次构建——小笔记本挂一本 9M 对象的参考库、
      用户取消勾选它，就会逐库跑 `graph_object_rows`/`graph_relation_rows`/
      `cluster_member_rows`（`retrieval_service.ppr_retrieve` 的 docstring 记着这条路
      数十分钟、数 GB，曾在 1.13M 节点库上冻结 reasoning）。所以这是**一次联合改
      动**：必须先让那两张图按库勾选建，而那是缓存键的独立取舍（论证见
      `source_scope.scoped_subgraph_nodes` 的 docstring：把 scope 放进键会按勾选组合
      重建整图）。参与集覆盖在场时守卫已经读座位——因为那时两张图也读座位，守卫与
      建图口径同源；要修的是无覆盖那一半。
- [ ] **逐步推理词法臂的关键词按语料语言双语化（给 `plan()` 传 `corpus_langs`）**：
      无图首轮的词法臂用的是 `plan()` 里 `expand_query` 产出的高/低层关键词，而
      `plan()` 调 `expand_query` 时**不传** `corpus_langs`，拿到的是 prompt 的
      zh/en 默认语言对；chunk 通用问答那条同源的臂是按语料语言给出的。两侧对齐
      需要把 `corpus_langs` 传进 `plan()`，但 `plan()` 是有图/无图两条 run 共用的
      同一个规划入口，传参会一并改到**有图 run 的规划 prompt**，越过本次「有图
      run 一字不动」的边界，故本次只把两侧文案与 docstring 改成说真话，传参登记
      在此。做的时候要连带决定：语料语言探测（`_lexical_corpus_langs`）在
      reasoning 侧的取数时机与失败语义，以及有图 run 规划输出漂移的回归证据。

### 解析

- [ ] 扫描件本地 OCR（MinerU 之外无 OCR 路径）；DOCX / PPTX 的 OMML 公式解析。

---

## 三、长期方向（方案 v0.4 / v1.0）

- [ ] **Review Mode**：review session、场景 checklist sign-off、reviewer 评论 / action items、
      导出 review 报告、project-level workspace。均无代码。
- [ ] **企业能力**：source 级 ACL（现只有 notebook 级 capability tier + 群组角色）、结构化审计
      日志（现只有 Knowhow 的 actor 标签投影）、SSO / VPC、Connectors（Confluence / SharePoint /
      Drive / Jira / Git / Slack）、多 notebook 全局搜索。多用户 / 登录 / 管理员角色 / 分享链接 /
      群组已交付。
- [ ] **分享的 edit 权限层与近实时协作**：写权限仍 owner-only；无 presence / revision 轮询
      （管理员用户列表的「在线状态」是另一特性）。原 spec
      `docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` 的 D2 / D3 决策已被
      账号系统取代，动手前须重写。
- [ ] **自动用户记忆**：`memory_mode` 固定 manual；Agentic Memory 已提供候选 / 巡固机制，是否
      开放自动写入待决。
- [ ] **插件化 X 系列剩余**：X6（「有真实消费者才重开扩展点」）已收官——`ask.reflect_action`
      扩展点（#714）与 arXiv 样例插件的 `search_arxiv` 动作分两个 PR 合入，样例插件现在演示
      三个点。剩余：X7 等仍无真实消费者的点；X10 后端热更用户明确要求「完全热更或不做」；
      niuma 插件 e2e 后的问题回流。

---

## 验证基线（每完成一项都要保持）

- `bash scripts/check.sh` 全绿（后端 pytest 全量 + 前端测试 + tsc + production build）；
  PostgreSQL 相关改动另跑 PG lane。
- 离线（无 LLM / embedding / MinerU）闭环不回退。
