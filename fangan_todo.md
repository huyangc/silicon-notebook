# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-10-23（第二节「隐藏成员名字经 canonical id 暴露」已处理并移入 `fangan_done.md` §65：摸排确认
直接读者面不越界，只修了经挂载读与来源天花板下的取名；同日早些时候，图谱分析「问题清单」特性已全部合入并记入 `fangan_done.md` §64，本文件相应补登：
第二节「知识图谱」新增三条（换描述模型或提示词后没有重写概念描述的路径、隐藏成员名字经 canonical id 暴露、
问题清单阈值待生产演练复核），第四节「其它残余」新增两条（Uvicorn 访问日志里其它路由的查询串、`.snbk`
归档不带种子空间与板块分区列），`communities.title` / `summary` / `findings` 一条补记 v102 / 0083 增列时
未一并删列；此前 2026-10-19：用户当日四条裁决已落地并记入 `fangan_done.md` §61：本机热门禁目标改 100 秒；
`facade_surface.json` 细节字段守卫不上、全局回答 synthesis-only 重合成不做、退役 chunk 流水线不做，三者登记在第四节
「产品与守卫裁决」组；第四节新增「整改计划刻意不做、只登记」：整改计划里标为只登记、此前只留在程序
台账的性能 / 结构 / 工程卫生项逐条按代码复核后补登；移除过时的「BM25 / FTS5 / tsvector 全文索引：暂缓」
（SQLite FTS5 trigram 索引 + `bm25()` 排序与 PostgreSQL `pg_trgm` 词法候选早已在用，见 `fangan_done.md` §9；
PostgreSQL 侧走 trigram 而不是 tsvector），「企业能力」去掉已交付的统一认证与跨库全局问答，
`_federated_graph_is_large` 一条按现行代码重写机制，状态速览改正 closeout 计划已完成；10-15 之后合入的 #913 / #914 / #915 / #916 记入 `fangan_done.md` §60；第二节「reasoning 原文段引用卡重复三次批量读」已完成，移入 `fangan_done.md` §61。此前 2026-10-15：技术债整改程序
移交，新增「四、技术债整改程序：收官后剩余」，`read_document` 延后项 (b) 与「历史全局回答 payload 归一」
已移入 `fangan_done.md` §59；再此前 2026-10-07）
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
- 本文件按「近期可动手」→「产品功能」→「长期方向」→「技术债残余」排列。2026-09-07 收尾计划
  `docs/superpowers/plans/2026-09-07-todo-closeout.md` 的 PR-1～PR-5 已全部交付（见 `fangan_done.md`
  交付表 2026-09-07 各行），不再是待办来源；需拍板的项留在本文件。
- 2026-10-12 起的「技术债整改程序」已合入的各项见 `fangan_done.md` §59；其登记的残余债与整改计划
  裁定「不做、只登记」的项集中在本文件第四节，不必再翻程序台账。
- 2026-10-19～23 的图谱分析「问题清单」特性已全部合入（`fangan_done.md` §64）；它遗留的后续项登记在第二节
  「知识图谱」与第四节「其它残余」，不必再翻实施计划。

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

- [ ] **首轮原文播种的大库耗时待实测**：首轮原文播种现在对**所有** reasoning run 默认生效
      （2026-09-29 起不看知识图谱，有图 run 也跑），逐子查询并发调用 `search_chunks`；外层并发已按参与库数收紧为
      `max(1, min(N, 8, 8 // P))`（外层 × 联邦腿 ≤ 8，PG 池默认 10），但大库上的实际
      耗时仍待实测，改用多查询合并召回（`RetrievalService.retrieve_chunk_candidates_multi`）
      之前先量。
- [ ] **首轮原文播种的人工抽问验收未做**：`docs/superpowers/specs/2026-09-07-reasoning-chunk-parity-keyword-arm-design_zh.md`
      「验收」一节给出的人工抽问（点名子部件 / 周期性 / 方向三句式 + 一句对比题，无图库与有图库各跑一遍）未做。
- [ ] **首轮播种脱钩的已知残余（2026-09-29 评审登记，不修代码）**：
      ① `source_graph_activation.py` 约 419-424 行的 kg_source 邻居段 relevance 恒为 1.0，
      既不是 PPR 归一分也不是绝对融合分，按量纲判道会落进检索道——只在选中来源图
      激活（selected-source graph）放量时出现；② 同一条选中来源图激活路径的
      `chunk_seeds` 把 PPR 归一分与播种绝对分混在一起当种子权重（同样只在放量时出现）；
      ③ 经验库投影不看 `phase`：确定性播种步被算进 `search_chunks` 的调用数与零命中统计
      （沿用 PPR seed 的口径），有图 run 也播种之后这两个统计会整体漂移；④ 有图谱 run
      的证据构成变了，只引用播种段（绝对融合分较低）的答案可能从 grounded 降到
      overview，待真模型抽问观察；⑤ `_answer_reasoning` 的 chunk 号段 k1..N 与 KG 段
      k1001+ 的撞号界现在由「字符预算能渲染几行」决定而不是证据池大小（最深档池子可
      上千），大量极短段的库理论上可能逼近 1000。
- [ ] **问答纠偏规则 12（限定词保真）人工 A/B**：仓库无问答质量评测台，放量前用「点名子部件 /
      周期性 / 方向」三句式各问一次验证；ledger 喂摘要未做。
- [ ] **`read_document` 多篇无标题文档仍区分不开**：同名序号已交付（见 `fangan_done.md` §61），
      但多篇无标题文档共用的「未命名来源」占位串刻意**不编号**——账目对该占位串「最多占一个
      标题位」是 #724 R2 钉死的规矩（20 篇无名文档不得挤掉真标题），编号会让每篇各占一位。
      要让它们也可读，需要先决定账目预算怎么分（如无名条目共享子预算）。
- [ ] **Prompt 三层化后的 per-notebook 定制与 self-evo**：接缝只有 `fragment_text()`；L1 片段分
      两类（A 类离线 GEPA + 人审，B 类只改示例槽位），尚未拍板开放。
- [ ] **Agentic Memory 注入的真实 A/B 观测**：按笔记本分区一系列已收官（PR-1 #769 分区、
      PR-2 #771 界面、PR-3 本 PR 开闸；规格
      `docs/superpowers/specs/2026-09-22-retrieval-experience-per-notebook-design_zh.md`）。
      PR-3 合入后 `RETRIEVAL_EXPERIENCE_INJECT_ENABLED` 默认开，本机试跑证明链路通（同一个库
      10 次 reasoning 提问蒸出 2 条，开闸后轨迹 `experience` 步 `notebook_entries=2`）。
      **剩余**：真实 A/B 观测——这次试跑验证的是链路与条目可用，不是检索效果提升；效果按
      条目的 `adopted` 计数看采纳率（`adopted` 只在条目真正送达且模型那一轮点名了该动作时
      递增），需要生产上跑够量才谈得上。觉得每 reflect 轮重复注入的 token 不划算的部署把
      注入闸设回 false 即逐字回到「只蒸馏不注入」。

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
- [ ] **换描述模型或提示词后，没有任何路径能重写概念描述**（来源：PR #939）：概念描述按签名复用，签名是
      `_concept_desc_sig(name, quotes)`（`backend/app/services/knowledge_lifecycle.py`）的 sha1，输入只有概念名与
      去重排序后的前 8 条引文，不含描述模型（`kg_concept_description` 工作负载绑定的服务），也不含
      `concept_description_prompt`（`backend/app/services/prompts.py`）的版本。所以换了描述模型、或改了描述提示词
      之后，名字与引文都没变的概念，下一次统一重建照样命中旧描述、不调模型；`batch_ingest --fresh` 只清 rebuild
      checkpoint，描述阶段仍按签名复用，同样不重写（`docs/operations*.md` 已如实写）。目前没有任何入口能让一个
      已发布的库按新模型 / 新提示词把描述整批重写，只能等名字或引文变化逐个触发。要补一条显式路径（签名里带模型
      身份与提示词版本，或一个只重写描述的维护命令）；动手前先定「换模型是否应当整批重写」——整批重写是一大笔
      描述模型调用，按签名复用正是为了避免种子归一后几乎每个多词概念都被重调一遍。
- [ ] **问题清单阈值是本机标定的初值，生产只读演练数据回来后复核**（来源：PR #941，规格 §14③ 的用户裁决「先按本机
      初值实现」）：F1 / F2 / F3 / F4 的各阈值与 F2 排除「通用概念」的界（出现在超过 30 个来源里）只在一个 84 个来源的
      本机库（`nb-b37185f4ae`）上标定过，F3 在本机没有正样本；规格 §14③ 写明 4.9 万个来源上 30 可能太小。精确值在
      `backend/app/domain/kg_findings_contracts.py` 的具名常量与产品/API 参考「问题清单」一段。待办是拿生产只读
      演练的数据复核规格 §14③ 列的量项：F1 共享占比分布、F2 候选对数与 Jaccard 分布、概念出现在几个来源里的分布
      （定「通用概念」的界）、F3 各子信号分布、F4 窄 / 宽两档各多少组、F5 数量与补上关联的历史、新增计算的耗时与
      RSS。仓库内的演练脚本是 `scripts/kg_findings_rehearsal.py`（与引擎同口径、只读、默认不输出标题与概念名）；
      生产侧用的 `kg_findings_prod_probe.py` 不在仓库内。

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
- [ ] **全局元素臂在 run 内复用整条原文召回（未做，只登记）**。全局问答的原文元素检索臂
      （`chunk_federation.federated_element_candidates`）每条逐库腿都是单库 `_retrieve_elements`，
      而它先跑一次**完整**的 `_retrieve_chunks`（ANN + 全文 + 回表，召回窗
      `max(CHUNK_RECALL, 4×上限)`）再回表取元素；同一 run 里语义腿、`search_chunks` 与首轮
      全空补腿对同一库、同一问法的那次召回并不复用。成本上界：每 run 至多
      `REASONING_MAX_ELEMENT_SEARCHES`（默认 5）次元素检索 × 8 库 = 最多多出 40 次完整召回
      （本机 live 库副本实测一次 8 库元素检索中位 1.9 s、p95 2.9 s，最大单库单独一次 1.4 s）。
      做法候选：按 `(库, query, 天花板摘要, recall)` 在 retrieval run 内记忆 `_retrieve_chunks`
      的结果，补腿与 `search_chunks` 共用；要先确认记忆不跨越天花板漂移判词翻转
      （`verify_unbound_read` 会重跑绑定版本）且不放大内存峰值（结果含整库向量矩阵引用）。
- [ ] **8 库 reasoning 的 `payload_json` 体积观测**。全局作业没有轨迹子表，推理轨迹整份存在
      作业行的单个 JSON 里，`_append_trace` 只靠时间/步数双阈值节流重写。8 个库的 reasoning
      run 轮数与每步 detail 都比单库大，这份 payload 的真实分布没有测过；要先量（p50/p95
      字节数、重写次数），再决定是否需要轨迹子表或 detail 截断。
- [ ] **跨库最终答案的全局关键词臂 / 精确标识符臂（可选后续）**。两条 active-only 补召回腿
      已按**逐库联邦化**形态落地（双语关键词臂与精确标识符臂：每库各跑一次、各带冻结天花板、
      按库轮转合并并以 `GLOBAL_ASK_CANDIDATE_LIMIT` 封顶，精确臂以整节为单位且保底席位按库分配，
      失败不进覆盖回执；开关 `GLOBAL_ASK_KEYWORD_ARM_ENABLED` / `GLOBAL_ASK_EXACT_ARM_ENABLED`，
      合同见 `docs/product-and-api*.md` 全局问答一节）。仍未做的是另一种形态：在跨库合并**之后**、
      对最终候选池再跑一次全局口径的词法/精确匹配（成本与库数无关，但拿不到各库的 FTS 索引）。
      两种形态解决的问题不同（前者补逐库召回，后者补跨库排序与标识符定位）；后者只在逐库形态
      被证明不够时再评估。
- [ ] 结构化硬过滤：软加权已够用，硬过滤有清空结果风险，暂缓。
- [ ] **`_federated_graph_is_large` 把取消勾选的参考库也算进「图是否过大」**：这个
      判据（`backend/app/services/retrieval_candidates.py`）在**无参与集覆盖**时读
      `_viewer_graph_participants` 的快照——当前查看者有效的挂载（经 `participant_rows`，
      PR #828 起只算查看者能读的被挂库），每个检索 run 按查看者 memo 一次——只要其中任一库
      `notebook_copy_stats(...)["copyable"]` 为假就判「大图」；这份快照从不过
      `notebook_in_scope`。于是一个刚被用户取消勾选的大参考库仍然把它撑成「大图」，
      本次 run 范围内每个库都很小时，`_chunk_kg_overlay` 与 PPR 回退照样以
      `reason=large_notebook` 拒绝，事件也因此误描述了这次 run 的语料。
      **不要单独收窄这个守卫**：它守的两张图在无覆盖时按同一份快照里的全部挂载库建
      （`graph_retrieval._federated_rx_graph` 与 `_ppr_graph` 都读 `_viewer_graph_participants`，
      库维度是在走查**结果**上由 `scoped_subgraph_nodes` 过滤的），守卫与建图同源，
      只放松守卫会放行它本来要拒绝的那次构建——小笔记本挂一本 9M 对象的参考库、
      用户取消勾选它，就会逐库跑 `graph_object_rows`/`graph_relation_rows`/
      `cluster_member_rows`（`retrieval_service.ppr_retrieve` 的 docstring 记着这条路
      数十分钟、数 GB，曾在 1.13M 节点库上冻结 reasoning）。所以这是**一次联合改
      动**：必须先让那两张图按库勾选建，而那是缓存键的独立取舍（论证见
      `source_scope.scoped_subgraph_nodes` 的 docstring：把 scope 放进键会按勾选组合
      重建整图）。参与集覆盖在场时守卫已经读座位——因为那时两张图也读座位，守卫与
      建图口径同源；要修的是无覆盖那一半。
- [ ] **对等模式关键词腿的重复超时（待大库实测）**：对等模式的关键词腿自身超时
      **不**打开按库 FTS 熔断（`retrieval_candidates._chunk_fts_hits` 的
      `trip_circuit=False`，既有设计：补召回腿超时不该关掉该库语义腿的全文检索）。
      chunk 模式每问只调一次关键词臂，代价有界；但 reasoning 一个 run 最多调
      1 + `REASONING_MAX_CHUNK_SEARCHES` 次（首轮播种一次 + 每个带
      `chunks_keywords` 的 `search_chunks` 动作一次），同一个慢库最坏每次都多等一个
      `POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS`。待大库实测后决定是否加 run 级「本库关键词
      腿已超时」标记（只管关键词腿，不碰语义腿共用的熔断）。

### 解析

- [ ] 扫描件本地 OCR（MinerU 之外无 OCR 路径）；DOCX / PPTX 的 OMML 公式解析。

---

## 三、长期方向（方案 v0.4 / v1.0）

- [ ] **Review Mode**：review session、场景 checklist sign-off、reviewer 评论 / action items、
      导出 review 报告、project-level workspace。均无代码。
- [ ] **企业能力**：source 级 ACL（现只有 notebook 级 capability tier + 群组角色）、结构化审计
      日志（现只有 Knowhow 的 actor 标签投影）、VPC 部署形态、Connectors（Confluence / SharePoint /
      Drive / Jira / Git / Slack）。已交付、不再列为待办：多用户 / 登录 / 管理员角色 / 分享链接 /
      群组；统一认证 SSO（PR #837 工号直登或关联/新建账号，见 `fangan_done.md` §3；#876 补了管理员
      合并统一认证账号；插件代码已交付，真实 IDaaS 联调与生产切换属部署侧验收）；跨多个 notebook 的检索与问答
      （全局问答 #751–#755，见 `fangan_done.md` §41–§44）。
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

## 四、技术债整改程序（2026-10）：收官后剩余

已合入的各项见 `fangan_done.md` §59。以下是该程序执行中登记、刻意未做的残余；每条注明来源 PR。

### 实验开关评估出口

> `backend/app/core/config.py` 里 8 个默认关（或默认 shadow）的实验开关，注释都写「评估后转正或删除，
> 出口登记在 fangan_todo.md「实验开关评估出口」」，指针指向本节（来源：PR #853）。默认值刻意不动；
> 本期只补了注释，没有评估。

- [ ] **8 个实验开关逐个评估后转正或删除**：`REPORT_HIGH_RISK_DOWNGRADE_ENABLED`、
      `KG_RELATION_COMPLETION_MODE`（默认 `off`）、`RETRIEVAL_RRF_ENABLED`、`RELATION_RETRIEVAL_ENABLED`、
      `SELECTED_SOURCE_GRAPH_ROLLOUT_MODE`（默认 `shadow`）、`PPR_FACT_RERANK_ENABLED`、
      `KG_CANONICAL_FOLD_ENABLED`、`KG_ABOUT_DOWNWEIGHT_ENABLED`。出口只有两种：有放量证据则改默认值并
      删开关与旧分支；没有则删开关与旁路代码。评估尚未排期。

### 其它残余

- [ ] **`IGNORABLE_RETIRED_WORKLOADS` 两个版本后移除**（来源：PR #853）：`kg_community_summary` 已退役，
      `backend/app/services/model_registry.py` 的 `IGNORABLE_RETIRED_WORKLOADS` 让部署文件里残留的
      绑定行不致拒启，并在每次加载成功时打一行脱敏提示。自 #853 所在版本起再过两个版本，部署文件
      都已清掉这两行后，删除该名单、对应启动提示与部署文档（`docs/deployment-and-configuration*.md`）里
      「已退役可忽略 id」一段。回滚到 #853 之前的版本前，运维要把那两行加回，否则旧版严格校验拒启。
- [ ] **`communities.title` / `summary` / `findings` 三列已无写入方**（来源：PR #853）：社区摘要生成
      已删除，这三列除 `copy_forward_communities`（翻转事务内把未重建层级的旧行复制进新代）原样带过之外
      没有写入方。本轮不做迁移（SQLite 与 PostgreSQL 的 `communities` 表都还有这三列）；是否删列留到
      下一次 `communities` 结构变更时一并决定。**补记（PR #988）**：SQLite v102 / PostgreSQL 0083 给 `communities`
      增了 `board_key`，那次没有一并删这三列，三列原样保留；是否删列仍留到下一次 `communities` 结构变更时决定。
- [ ] **C6 生产信号：可见性图未设好时计划可能被劫持**（来源：PR #854 / #897 / #908）：测试里的计划钉
      已改成不依赖可见性图，但生产同形问题没有测量：表的可见性图（visibility map）没设好时，
      `version_facts` 与 skip-gate 的 count 可能被 `idx_clusters_nb_canonical_name_lower` 劫持
      （Index Scan + heap Filter，而不是预期的索引）。需要在生产只读库读
      `pg_class.relallvisible` 对 `relpages`，并对这两条语句抽查 `EXPLAIN`；成立与否未知，
      不要在没有生产测量的情况下改索引或查询。
- [ ] **K1（裁决 11/12/13）：删除知识图谱后 5 张派生表行保留到下次重建**（来源：PR #905）：删除作业
      不再逐批清 `canonical_relations`、`mention_edges`、`concept_comentions`、`communities`、
      `community_members`（生产规模下排水使删除从 0.87 s 变 33 s），而是写代际哨兵（社区 `-1`、
      规范关系与提及桥各 `-2`）让读取侧视为没有派生数据，下次重建重写它们。所以删库后这五张表的存储
      不会立刻回收。若将来需要即时回收，另立「离线/低峰批量清理」方案。**K1 上线之前**做过的删除没有哨兵，其保留行按原样可读到下次
      重建；对该图再删一次即可闭合，**不做回填**。
- [ ] **K2：历史深拷贝副本的图片修复先量后做**（来源：PR #847）：#847 只修了此后新做的深拷贝，此前
      的副本里元素的图片资产 id 可能仍指向源库。先在生产只读库跑 #847 描述里的三条核对 SQL（①元素所在库
      与资产所在库不同；②副本资产行的 `source_id` 指向别库来源；③能否按 filename/mime/size/created_at/
      source_id 唯一找回对应副本资产）。①② 为零则无事；不为零且 ③ 基本唯一时，再另开离线修复命令
      （仿 `backfill-images` 的 CAS 与续跑方式），不放进迁移。
- [ ] **K3：`bulk_delete` 在非存活库仍可删本人 Memory**（来源：PR #848）：刻意保留——
      `MemoryService.bulk_delete`（`backend/app/services/memory_service.py`）只看创建者、不看笔记本存活，
      库卡在 importing 时作者仍能清理自己的 Memory（代码注释已写）。其余 Memory 与自助问答/报告读取
      已加存活闸；这一条不是漏网，只在此登记，别当 bug 修。
- [ ] **K5：公开页存量规整的残余形态**（来源：PR #849）：失败章节正文改为固定中性句之后，公开页
      对仍带异常原文的存量报告，读取失败节原文并按生成器当时写下的字面串精确替换为中性句；
      只有中性句的报告不读 `sections`。已知残余：存下的原文本身以「中性句 + 「）」+ 空行」开头时，
      不读 `sections` 就无法与新格式区分。只有异常文本恰好以本产品自己的中文失败文案开头才会出现，
      代码注释与用例已写明，登记为已知形态，不另行处理。
- [ ] **Uvicorn 访问日志里其它路由的查询串仍原样记录，是否改为只记键待拍板**（来源：PR #969 codex 第 8 轮）：
      `backend/app/core/auth_logging.py::AuthenticationAccessFilter` 只处理两类请求：`/api/auth/` 下的丢弃整个查询串
      （OAuth 回调带凭据），以及 `backend/app/core/log_paths.py` 登记的几类内容路径段（问题清单豁免的 subject、
      邀请与公开分享口令、自定义对象类型名）换成占位符。其它路由的查询串——如 `/api/search?q=…`——仍原样写进
      `uvicorn.access`，`backend/tests/test_auth_logging.py::test_unrelated_access_logs_remain_unchanged` 钉住了这一点。
      前端共享传输层的请求日志已改为查询串只记键；后端访问日志是否对所有路由同样只记键，与 `AGENTS.md`
      「日志不带内容」的口径相比是缺口，但会改变运维排障时能在日志里看到的查询内容，待用户拍板；拍板前不要放宽
      或改写那条用例。
- [ ] **`.snbk` 归档导出带 `concept_clusters`，但不带种子空间与板块分区列**（来源：PR #988）：归档的
      复制闭包（`backend/app/domain/notebook_archive.py::ARCHIVE_TABLES`，导出在
      `backend/app/services/notebook_transfer/export.py`）含 `concept_clusters`，却不含 `unified_kg_state`——所以没有
      `concept_seed_version`（这个库已发布的合并所用的概念种子空间）——也不含 `communities`，板块的 `board_key`
      与 `unified_kg_state.community_partition_version` / `community_partition_mode` 同样不在其中。导入尚未实现
      （`docs/operations*.md`「笔记本单文件导出」写明不在本版本内，计划见
      `docs/superpowers/plans/2026-10-22-notebook-export-import.md`）；将来实现导入时需一并携带：没有状态行的库读作
      种子空间 2，旧部署导出的空间 1 簇会被读歪（v102 / 0083 迁移注释记着同一风险）；板块分区列随板块一起——归档
      目前整个不含 `communities`，要么连同板块一起带，要么接受导入后的第一次重建冷启动重新划分。

### 整改计划刻意不做、只登记

> 整改计划评审时逐条裁定「不做、只登记」的项，此前只记在程序台账（不入库）里。2026-10-19 逐条按当时
> 代码复核后补登于此：行数用 `ast` 量（含嵌套函数），文件与行号为当日 master。复核时已不成立的不登。
> 删除索引管线阶段级联未分批（计划 5d）已在第一节 W1 一条，不重复。

**产品与守卫裁决**（用户裁决，不是技术上做不到；重开前先问用户）

- [ ] **`facade_surface.json` 细节字段（`owner` / `consumers` / `patch_targets` / `signature`）漂移守卫：
      不上**（用户 2026-10-19 裁决；来源：S2-D，PR #862）。`test_facade_surface_fixture` 维持只断言名字集合。
      理由：全量等值前提是先去掉生成器对 `SURFACE_SOURCE_COMMIT` 版本历史的依赖（CI 浅克隆拿不到），
      并且是全仓 PR 政策变化——每个改消费点或签名的 PR 都要重基线，成本高于它能抓到的漂移。
- [ ] **全局回答的 synthesis-only 重合成入口（`AskService` 只做合成、复用已有证据与已解析引用）：
      不做**（用户 2026-10-19 裁决）。理由：现行逐条标记交付已让读者看到哪条引用没通过（`fangan_done.md`
      第 48 条），回答照常整份交付；再合成一次会让同一问题出现两份引用口径，还多一次模型调用。
- [ ] **无条件跟进改写（chunk 模式那种「有历史就改写」）不给 reasoning 直连路径：不做**（PR-C 时用户裁决）。
      reasoning 只在确定性澄清闸命中时才读会话历史并改写；无条件形态是多一次改写模型调用换更好的检索词，
      用户明确不做。
- [ ] **reasoning 的固定「直答」档位与「先合成、不足再查」流程：不做**。v1（固定「直答」档位）与 v2
      （先合成、不足再查）均已撤回：用户裁决不设固定直答选项、问题理解与子问题检索不能省、检索效果优先于
      省一次调用；v2 的先合成本可把首轮后的 reflect 并进合成再省一次调用，但代价是拆 `run()` 的重构与判定
      质量的不确定，按效果优先撤回。
- [ ] **退役 chunk 流水线：不做**（用户 2026-10-19 裁决）。通用问答（chunk）与深度分析（reasoning）两种模式
      长期并存；高级界面默认模式改为 reasoning（「高级模式」名字不改）。原「问答方法归一」路线的第 ④ 步
      及其放量前置（chunk / reasoning 对照、对照基线核实）随之作废。
- [ ] **SQLite 后端退役：不做**（用户 2026-10-19 裁决）。SQLite 长期保留，支持离线、单机与测试；
      PostgreSQL 仍是规范定义。2026-07-22 的退役计划（`docs/superpowers/plans/2026-07-22-postgresql-sqlite-retirement.md`）
      从未执行，状态头已标「已放弃」。双后端义务与能力对等矩阵见 `architecture.md` 2.2.2 节。

**性能**（理由来自整改程序的性能设计稿与分片评审；重开前一律先差分测量）

- [ ] **`kg_cluster_scratch` 的 `(notebook_id, run_id, seed)` 索引：已被测量证伪，不做**。现状：SQLite 只有
      `idx_kg_cluster_scratch_nb_run (notebook_id, run_id)`（隐含 rowid 作末列）；PostgreSQL 自 0079 起是
      `idx_kg_cluster_scratch_nb_run_ordinal (notebook_id, run_id, ordinal)`（写代段 keyset 分页用，替换了
      `0004_knowledge_indexes.sql` 的 `(notebook_id, run_id)`），两侧都没有 `seed` 键。
      计划曾拟用带 `seed` 的复合索引替换它，1M 行夹具实测：claim 型 seed（平均 186 字节）百万行插入
      2.7 s → 7.0 s、WAL 2.1×、索引 7 MB → 564 MB；只有 concept 描述阶段受益，claim / formula /
      procedure 只付成本，生产端到端多半净负；而且迁移在启动时跑、受 30 s 语句超时约束，残留 9M 行
      可让启动永久失败。**不要凭「按 seed 查当然该有索引」的推理重开**。若将来确有需要，只评估两条
      替代：`USING hash (seed)`，或把 B 次批读改成按 seed 有序的流式读。
- [ ] **scale build 的 `(notebook_id, ordinal)` 复合索引：不做**。对象与 chunk 两条 keyset 分页
      （`backend/app/repositories/postgres/index_projection_store.py`）靠全局唯一的
      `uq_knowledge_objects_ordinal` / `uq_chunks_ordinal`，`notebook_id` 只是残余过滤；生产上跑
      scale build 的库占表的主导份额（`postgres/embedding_store.py` 记的是 99.1%），一次遍历全局
      ordinal 索引几乎零额外代价，只有「中等份额且正在跑 scale build」的库吃亏。代价却落在最热的写表
      `knowledge_objects`（已有约 10 个索引外加同步触发器）和 `chunks` 上。重开前提：生产测量显示多个
      大库共存且有中等份额的库在跑 scale build。测量：
      `scripts/bench_scale_build_paging.py seed --notebooks N` 造 99% / 50% / 10% / 1% 四档份额，
      比两条腿每页 `EXPLAIN BUFFERS` 与 `build_scale_index` 取图耗时，写侧比批量入库吞吐、WAL 与索引体积；判据为实际份额档显著改善且
      入库劣化 ≤ 2%。形态只走 builder 的 `CONCURRENTLY` + `IF NOT EXISTS` 账本迁移，届时改
      `backend/tests/postgres/test_index_projection_explain_pins.py` 钉的索引名。
- [ ] **审阅队列不下推到 SQL**：`backend/app/services/knowledge_governance.py` 的优先级是
      「中心度 × (1 − 可信度)」，佐证计数与中心度都依赖全图，必须全量算完再取 top-k；已用
      `heapq.nlargest` 并按 seq 键缓存。平局按输入顺序决定，任何下推到 SQL 的改写都会改变平局顺序。
      唯一可选的微优化是只对前 M 个构造字典，收益不确定，未做。
- [ ] **晋升按类型整表 `FOR UPDATE`：代价真实，暂不改**。
      `backend/app/repositories/postgres/governance_store.py::_base_dedup_rows_for_update`
      对 base 库同类型的全部可用行 `ORDER BY id FOR UPDATE`：每个元组写 xmax、产生 WAL，并阻塞同库其它写者；Memory 晋升的循环里
      每个候选都重扫一遍，复杂度为候选数 × 语料规模。但去重的 seed 匹配是 Python 函数，SQL 里无法精确
      预筛；改用 advisory 锁会破坏与其它行锁写者之间的互斥，拿不出不变性证明。登记一个生产测量项：
      base 库规模分布与晋升耗时，量出来真疼再设计。
- [ ] **relink 不合并正则**：原清单「relink 逐概念编译正则」的描述已过时——
      `backend/app/services/kg/relink.py` 已是每概念惰性编译一次（`concept_patterns` 缓存）。剩下的成本是
      「孤立 claim × 概念」次 `search`。合并成交替大正则会漏掉重叠匹配；先按小写子串预筛与
      `re.IGNORECASE` 的 Unicode 折叠不完全一致（例如 `ſ` 与 `s`），会出假阴性。语义不能保证不变，不做。
- [ ] **rebuild 逐源替换（T-5b）：不做**。`KnowledgeLifecycleService` 的 KG 重建默认先
      `delete_notebook_kg` 整库清图，`preserve_existing_rebuild` 只给索引管线用。逐源替换会留下「来源已删
      但对象还在」的残渣与库级派生表，并改变重建期间的可见性（新旧图混在一起），需要单独的等价性证明
      与差集清扫；W1 设计稿已裁定不做（见
      `docs/superpowers/specs/2026-09-01-batch3-w1-delete-jobization-design_zh.md` 的 T-5b 条）。
      这是效果风险，没有新的理由就不重开。

**结构**（零行为变化的拆分；有需要时立项，每项先补表征测试）

- [ ] **≥ 150 行的函数共 182 个**（`backend/app` 全量，含嵌套函数），`ask_service.py` 7468 行、
      `knowledge_lifecycle.py` 7442 行两个大文件。函数天花板守卫只盯基线里登记的 24 个函数，新出现的长
      函数不会被自动纳入。没有统一拆分计划，按触碰到的模块逐个处理。
- [ ] **`frontend/app/globals.css` 约 1.4 万行（14178 行）未拆分**。前端测试、守卫与脚本里有 21 个文件
      按文件名读 `globals.css`，拆分要同步改这些读取点，属于独立的一次搬家。
- [ ] **仓储层反向 import services 仍有 4 条**（基线 `repository_service_import_ceiling`：other 4、
      postgres 0、sqlite 0；守卫只拦增长）。原 7 条里的 ①–③（backend factory 与 PostgreSQL wrapper）
      已上移到 services，见 `fangan_done.md` §61。剩下的全在
      `repositories/filesystem/scale_artifact_store.py:40`、`:41`、`:475`、`:536`，导入
      `services.kg.{scale_index, viz_index, source_partition_index}`。它们没有沿用 ①–③ 的办法（整文件
      上移）：`ScaleArtifactStore` 是 scale/viz 索引的磁盘持久化 store（目录布局、manifest 探针、
      tmp → live 换位），这些 import 只为调用 `load_*` / `save_*` 编解码函数，而编解码函数和 KG 算法写在
      同一批模块里。把 store 搬进 services 等于把持久化层放进服务层，不符合「store 独占持久化」的分层。
      正解是把文件格式编解码层从合计约 2.2K 行的三个 KG 算法模块里拆出、下沉到 domain 或 filesystem
      （`source_partition_index` 还依赖 `services.source_subgraph`），或者改成构造注入。两种做法都会动
      大库 scale index 的加载热路径；store 按模块属性在调用时取 `load_scale_index` 等函数，这样磁盘缓存
      用例对模块属性打的 monkeypatch 才会生效，搬走编解码函数就得一起迁移这些打桩点；测试里还有 6 处直接
      构造 `ScaleArtifactStore(settings)`。所以需另行设计，并先测量加载耗时。
- [ ] **owner-view 守卫的两张豁免表**：`frontend/tests/guards/owner-view-no-reflatten-guard.test.mjs` 的
      `GRANDFATHERED_DESTRUCTURE_HOOKS`（`askSession`、`sourceLibrary` 两段先于守卫存在的整体解构，
      `frontend/app/page.tsx` 约 2157 / 856 行）与 `PRE_EXISTING_SINGLE_USE_ALLOWLIST`（约 801 / 857 行
      两条单次属性提取）。清掉它们要改 `page.tsx`，并入第二节「前端 `page.tsx` 继续减负」一起做。
- [ ] **宽 `except Exception` 吞掉 `RetrievalControlError` 待排查**：
      `backend/app/services/reasoning_retrieval.py` 的 `_collect_kg_gap`（约 8446 行，探
      `weak_support_relations`）与 `_action_consult_memory`（约 8182 行）只放行 `AskCancelled`，参与集
      身份复核失败等 `RetrievalControlError` 会被记成一条 skip 步后继续，而同文件多数同类站点都先
      `except RetrievalControlError` 照抛。`weak_support_relations` 会不会触达参与集座位尚未核实；
      先排查，成立再改为照抛（这是可见行为变化，要配用例）。

---

## 验证基线（每完成一项都要保持）

- `bash scripts/check.sh` 全绿（后端 pytest 全量 + 前端测试 + tsc + production build）；
  PostgreSQL 相关改动另跑 PG lane。
- 离线（无 LLM / embedding / MinerU）闭环不回退。
