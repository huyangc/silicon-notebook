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
- [ ] **Agentic Memory 注入的真实 A/B 观测**：按笔记本分区一系列已收官（PR-1 #769 分区、
      PR-2 #771 界面、PR-3 本 PR 开闸；规格
      `docs/superpowers/specs/2026-09-22-retrieval-experience-per-notebook-design_zh.md`）。
      PR-3 合入后 `RETRIEVAL_EXPERIENCE_INJECT_ENABLED` 默认开，本机试跑证明链路通（同一个库
      10 次 reasoning 提问蒸出 2 条，开闸后轨迹 `experience` 步 `notebook_entries=2`）。
      **剩余**：真实 A/B 观测——这次试跑验证的是链路与条目可用，不是检索效果提升；效果按
      条目的 `adopted` 计数看采纳率（`adopted` 只在条目真正送达且模型那一轮点名了该动作时
      递增），需要生产上跑够量才谈得上。觉得每 reflect 轮重复注入的 token 不划算的部署把
      注入闸设回 false 即逐字回到「只蒸馏不注入」。
- [ ] **模型服务状态页不显示未绑定的工作负载**：`/admin` 的模型服务状态是 service-centric 的
      （按物理服务列出它承载的 workload），未绑定的工作负载不产生任何一行，所以「升级后缺了
      两个绑定」这件事在页面上看不出来。PR-4 之后默认档（`MODEL_BINDINGS_STRICT=true`）根本
      不会让这种部署起来——缺/多绑定直接拒启并一次列全；只有显式设 `MODEL_BINDINGS_STRICT=false`
      放行时才回到「服务照跑、功能静默不可用」，此时以启动日志里的 `model-bindings:` 告警为准
      （汇总一行 + 陈旧 id 一行 + 特性已开却未绑定的各占一行），笔记本面板「AI 对这个库的理解」
      会显示 `模型未配置` 的失败原因。**状态页不显示未绑定工作负载这件事本身仍成立**，放行档下
      依然看不出来：后续可在 `/model-services/status` 的 payload 里带一份未绑定清单，并在该面板
      加一行显示；本期只登记，不做前端。
- [ ] **全局回答的 👍/👎 反馈仍未进管理端提问分析 / 笔记本分析口径的聚合**：`POST
      /global-ask/jobs/{job_id}/feedback` 把评分写进该任务 `global_ask_jobs.payload_json` 的
      `feedback` 字段（首次写入为准），并发一个内容无关事件 `global_ask_feedback`；记录口径
      对齐改动（本次改动，SQLite v81 / PostgreSQL 0061 给 `global_ask_jobs` 新增的是
      `submitted_via`/`asked_at`/`updated_at`/`error_detail` 四个物理列，不含反馈）顺带给同一个
      `payload_json` 补上了 `feedback_at`（写入反馈的瞬间，与 `feedback` 同一次首写、随 patch
      一起落进 `payload_json`，不是新的 schema 列），但反馈本身仍只落在 `global_ask_jobs`，不进
      笔记本内问答用的 `feedback` 表，因此管理端「提问分析」报告与笔记本分析面板现有的反馈统计
      仍看不到这批数据、也没有聚合面。要并进同一份统计口径，需要决定是新增一条跨表聚合，还是
      把全局反馈也镜像写一份进 `feedback` 表（后者要解决它没有 `answers` 行外键可挂的问题）；
      这是产品决定，尚未拍板。
- [ ] **全局问答的删库后可读性与事件标记未做**：记录口径对齐（SQLite v81 / PostgreSQL 0061）、
      问答后学习链（覆盖层按参与库各计一次、经验库只计全局分区、检索偏好按人一次，注入只读全局
      分区）与待办铃铛「进行中的提问」（在途全局作业同组呈现、参与库集合整体可读才出现、点击打开
      全局问答浮窗里的那个会话）已补齐；仍未触碰、各需单独产品决定的有：(c) 全局会话的所有者一旦
      其中任一参与笔记本被删除，整条会话对所有者即变 404（没有留存投影、没有到期规则）——笔记本内
      问答有 `retained_user_activity` 留存投影，全局问答没有对应机制；(d) `ask_stage` /
      `retrieval_run_stats` 事件给全局 run 打的仍是某个锚点笔记本 id，没有专门的全局标记。全局反馈
      的聚合面缺口见上一条，不在此重复登记。
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

- [ ] **全局引用复核：非联邦通道引用的检索时刻指纹快照**。`GlobalAskService
      ._validate_citations` 的「缺席 + 现读不存在 → 放行」那一支是**刻意保留的现状**
      （codex #755 第 2 轮 P2，裁决为不改并登记）。codex 要求「引用指向真实 source
      element 而现读缺失就拒」，不能只这么做：非空的 `Citation.element_id` 并不保证那一行
      在 run 开始时是活的。元素 id **不是**重新签发的——`source_ingestion` 按
      `(来源, 序号)` 确定性地生成 `el-<source>-<index>`，重新入库后同一个 id 照样回来；
      悬空 id 的来源是另外三条：重新解析后元素**变少**、Knowhow 行级删除，以及
      `knowledge_store._enrich_evidence` 对查无此行的 id 原样交回给
      `evidence_context.knowledge_context`（文本回落 `quoted_span`），这也正是
      `evidence_context.collection_item_citations` 要「挑第一条活的元素」的原因；单库问答
      照样发布这类引用。一律拒绝会把一批**本来就这样**的既有可答问题整份作废，而且用户读到
      的那句「引用原文在回答期间发生了变化」是假的。
      **代价（明写）**：合成窗口内被删的非联邦引用会发布一条打不开的引用卡。
      **真正的修法**：让四个不经联邦 chunk 通道的引用生产者——文档概览
      （`document_source_overview`）、集合枚举（`collection_enumeration` /
      `evidence_context.collection_item_citations`）、KG 对象
      （`evidence_context.knowledge_context`）、`follow_chain`——也在**检索时刻**经同一道
      接缝 `FederatedRunPlan.on_evidence`（三态：快照 / `None` / 缺席）登记一份**指纹**
      快照。只登记「当时还活着」是不够的：id 确定性复用意味着同一个 id 下的**文字**可以
      整个换掉而存活位始终为真，所以登记的必须是检索那一刻读到的文本指纹（联邦通道侧由
      `passage_evidence_snapshot` 把段落原文与元素指纹放进同一个数据库快照来保证这一点）。
      有了指纹快照，这一支就退化成既有的「快照存在 + 现读缺失/不等 → changed」，不需要
      新判据。四个生产者各自改动，单独立项。
      现状由 `tests/test_global_ask_engine_parity.py::
      test_a_non_federated_citation_whose_element_vanished_is_still_delivered` 钉住：改成
      拒绝而不补快照，那条用例会红。

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
- [ ] **全局（对等）模式的精确查找臂联邦化**。双语关键词臂（`_keyword_chunk_candidates`）
      已逐库联邦化：每个参与库各跑一次、各带冻结天花板、按库轮转交错并以
      `GLOBAL_ASK_CANDIDATE_LIMIT` 封顶、失败不进覆盖回执，开关
      `GLOBAL_ASK_KEYWORD_ARM_ENABLED`（合同见 `docs/product-and-api*.md` 全局问答一节）。
      剩下 `_exact_lookup_chunks`：它仍是 **active-only** 的补召回腿（每次 ask 只对传进来的
      那一个 notebook 发 `exact_lookup_chunks`），对等模式下整条关掉（D0-5，`fangan_done.md`
      第 40 条）。代价如实：全局问答没有精确标识符快查——「问一个命令名」在全局模式下只靠
      语义腿（每条语义腿本来就带一路按子查询原文的全文检索，所以不是「只剩语义臂」，但没有
      整节取回）。联邦化要连带决定 `exact_section_reserve` 的席位在跨库池子里该怎么分（今天它
      只认 `exact_ids` 这一个集合，跨库之后需要一个按库的口径，否则某一个库的章节能把保底
      席位全占了）。
- [ ] **全局（对等）模式下集合枚举的引用卡只给到命名锚点挂载得到的库**（codex #755 第 6 轮
      P2）。`evidence_context.collection_item_citations` 在元素水合前复核成员资格，走的是
      **真实挂载谓词**（`self.notebooks.participant_notebook_ids(active)`）——那是鉴权座位，
      守卫按函数作用域钉着它不得出现任何 `resolve_*`（见 `docs/development*.md` 守卫一节）。
      全局 run 里参与库之间通常互不挂载，于是锚点之外的库枚举出来的行拿不到引用卡，它们的
      来源定位与库归属也进不了合成绑定。方向是**少给**（不是泄漏）：答案里仍会列出这些行，
      只是没有可点开的依据。修法是给这一处新开一条「已由全局准入鉴权过的参与集」通道
      （`can_read_many` 在 `global_ask._execute` 里对同一组 id 刚复核过），而不是放宽共享的
      挂载谓词；它改的是被守卫钉住的鉴权作用域，要单独立 PR、单独做安全评审，连同守卫的
      作用域断言一起改。
- [ ] **全局（对等）模式的元素检索臂联邦化**。`retrieval_candidates.retrieve_elements`
      是第三条 **active-only** 补召回腿（前两条见上一条）：它只对传进来的那一个 notebook
      发一次元素检索，没有联邦通道。对等模式下留着它，等于凭空给**名义 active** 多一条
      别的参与库没有的腿；更糟的是它的命中会经 `evidence_context.element_citations` 出
      引用卡，而那条装配一直是单库口径。D1-4 因此把它整条关掉（生产者一处闸 +
      `ReasoningRetriever._element_search_skip` 让轨迹如实说明 + `_first_round_empty_
      fallback` 不再自动补腿），代价如实：全局问答暂时没有「按原文元素定向检索」这条腿，
      reasoning 的确定性通道全空时也不再自动补它。联邦化要连带决定逐库元素配额、以及
      `element_citations` 的 tier 查表在跨库时按哪一本算。
      **附带**：`search_elements` 这个动作仍然留在 reflect 的动作目录里（与
      `reasoning_max_element_searches=0` 这个既有的「关着但仍提供」态同形），模型偶尔
      选到它会浪费一轮反思。要把它从 prompt/schema/白名单三处一起摘掉，得给 `reflect()`
      加第五把 run 级闸，属于动作契约的独立改动。**概念漫游（PPR）同形**：对等模式下
      `_ppr_retrieve` 恒返回空，但动作仍在目录里——2026-09-20 真引擎端到端实测的一次
      全局 reasoning run 里，轨迹出现过一步「概念漫游:跨文档检索,得到 0 段原文」。两个
      动作应当由同一把「对等模式下不提供」的闸一起摘掉。
- [ ] **全局（对等）模式没有单库那道「检索范围为空」的 409 预检**。单库入口的
      `_require_ask_available` 在勾选后范围为空时直接 409，全局入口没有对应物：八个库
      全空时这次 run 照样跑完，交回一份没有证据的答案（`grounded=False`），用户读到的是
      「没查到」而不是「你这次的范围里什么都没有」。修法要先决定「全局范围为空」的判据
      （逐库冻结天花板全空？还是还要看 chunk/KG 有没有行），以及它该在 `start()` 里拒绝
      还是作为一种终态原因码。
- [ ] **全局（对等）模式的表格分析臂只覆盖名义 active**。`AskService
      ._spreadsheet_reasoning_results` 走的是**真实挂载谓词**
      （`ask_engine_participant_notebooks`，鉴权级座位，绝不许变成覆盖感知——守卫
      `test_participant_override_guard::test_injected_participant_predicate_is_the_real_mount_predicate`
      钉住了这一点），所以对等模式下它答的是名义 active 自己的挂载表，而不是用户选中的
      那几个库。D0-5 已经把**泄漏**那一半关掉：按逐库冻结天花板收窄，挂在命名锚点下却不
      在本次选择里的参考库不再供证据（只减不增）。剩下的是**联邦化**那一半：其余参与库
      的工作簿今天完全不参与分析，于是「比较 A 库和 B 库那两张报价表」在全局模式下只能
      看到 A 库的。修法要新开一条不经鉴权座位的参与集入口（与 `chunk_federation
      ._bounded_participants` 同源），并决定跨库工作簿的成本上界（分析臂是模型规划 +
      逐表读取，× 库数不是免费的）。
- [ ] **全局（对等）模式下「参考库」这套措辞没有对应物**。`document_overview
      .overview_intent` 从问句里解析「不包括参考库 / 只介绍…」并产出 `local_only=True`，
      `collection_enumeration` 据此把枚举范围收成 `notebook_ids == active_notebook_id`
      一本。对等 run 里没有「当前库 vs 参考库」这组关系，`local_only` 于是把一次跨 8 库
      的「我的库里有哪些文档」压成只看命名锚点。方向是**少给**（不是泄漏），所以 D0-5
      没有动它；拍板时要一起决定全局模式下这组措辞映射到什么（整体忽略？还是换一套
      「只看某一个库」的显式范围表达），以及 `docs/product-and-api*.md` 里 `read_document`
      那段的对应文案。
- [ ] **`AskService` 的 synthesis-only 入口（全局引用复核重合成的前置件）**。归一之前，全局
      问答在引用复核失效时会「剔除失效证据 + 用剩余段落重新合成一次」；由引擎作答之后没有
      这个入口——重新调 `AskService.ask` 会重新检索、重新冻结，得到的是另一份答案顶着这一份
      的身份，所以 PR-D1 改成**整份作废 + 中文重试提示**（`fangan_done.md` 第 43 条）。要把
      「重新合成一次」拿回来，需要一个只做合成、复用已有证据与已解析引用的入口，并连带决定：
      重新合成后的引用是否再复核一轮、轮次上限、以及推理轨迹里怎么如实记这一步。
- [ ] **历史全局回答 payload 归一**。归一之前的作业存的是旧形状 `GlobalAskJob.response`，
      之后是标准 `GlobalAskJob.answer`。今天靠 `models/global_ask.py` 的三个投影函数
      （`global_answer_text` / `global_answer_citations` / `global_answer_trace`）与历史 SQL 的
      `COALESCE(answer.answer, response.answer)` 兼容，零迁移；代价是每个新读者都要记得走投影，
      漏一个就只对一半作业生效。要么写一次性迁移把旧 payload 改写成新形状，要么给这条「只许经
      投影读」加一道守卫，二选一，目前两者都没有。
- [ ] **8 库 reasoning 的 `payload_json` 体积观测**。全局作业没有轨迹子表，推理轨迹整份存在
      作业行的单个 JSON 里，`_append_trace` 只靠时间/步数双阈值节流重写。8 个库的 reasoning
      run 轮数与每步 detail 都比单库大，这份 payload 的真实分布没有测过；要先量（p50/p95
      字节数、重写次数），再决定是否需要轨迹子表或 detail 截断。
- [ ] **超长全局会话的分享披露只能给「拿不全」而不是确数**。弹窗按页取全一条会话的轮次
      （`loadGlobalShareTurns`，上界 `MAX_TURNS / 页大小` 页），翻到上界仍 `has_more` 时
      `complete: false`，界面改说「拿不全」而不给偏小的确数——这是刻意的，但代价是这类会话
      永远看不到「本次公开 N 轮 / M 条引用」。要给出确数，需要后端出一个专门的披露计数端点
      （按同一条水位口径在 SQL 里数），而不是让浏览器翻更多页。
- [ ] **全局问答窗口里大图预览未接**。单库问答与公开页 `/c/{token}` 的引用配图点开是
      `answer-image-preview` 那一格 root modal；全局问答的小窗/大窗里配图只能原位看，点开无效。
      直接复用要把 Esc 拦截下沉（全局窗口自己已经吃掉 Esc，再叠一层 modal 会出现「按 Esc 关掉
      的是窗口而不是图」），属于弹窗协调器一侧的独立改动。
- [ ] **跨库最终答案的全局关键词臂 / 精确标识符臂**。上面那条登记的是把 active-only 腿
      **逐库联邦化**（关键词臂已按这种形态落地，精确查找臂待做）；另有一种形态是在跨库合并**之后**、对最终候选池再跑一次全局口径的词法
      /精确匹配（成本与库数无关，但拿不到各库的 FTS 索引）。两种形态解决的问题不同（前者补
      逐库召回，后者补跨库排序与标识符定位），拍板时要一起比较，不要默认前者就是答案。
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
