# silicon-notebook 待办（fangan_todo.md）

更新日期：2026-10-15（技术债整改程序移交：新增「四、技术债整改程序：收官后剩余」，`read_document`
延后项 (b) 与「历史全局回答 payload 归一」已移入 `fangan_done.md` §59；此前 2026-10-07）
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
- 2026-10-12 起的「技术债整改程序」已合入的各项见 `fangan_done.md` §59；其登记的残余债集中在
  本文件第四节。

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
      跟进改写（本 PR；MCP `ask` 已由 #721 的调用内理解步覆盖，不在此改）；
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
      (a) 首轮原文播种现在对**所有** reasoning run 默认生效（2026-09-29 起不看知识图谱，
      有图 run 也跑），逐子查询并发调用 `search_chunks`；外层并发已按参与库数收紧为
      `max(1, min(N, 8, 8 // P))`（外层 × 联邦腿 ≤ 8，PG 池默认 10），但大库上的实际
      耗时仍待实测，改用多查询合并召回（`RetrievalService.retrieve_chunk_candidates_multi`）
      之前先量；(b) 该规格「验收」一节给出的人工抽问（点名子部件 /
      周期性 / 方向三句式 + 一句对比题，无图库与有图库各跑一遍）未做。
- [ ] **首轮原文播种与 KG 初检索并行提交**：`_run_first_round` 里播种排在 PPR / 精确 seed
      之后串行执行（为了 `seen_chunks` 去重与计数顺序），脱钩后有图 run 也要多等这一段。
      可以把播种的 I/O 与 KG 初检索一起提前提交、在原位置按序合并（与 PPR 预取同形），
      去掉串行等待。未做。
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
- [ ] **`read_document`（PR-A）已登记的延后项 (a)**：清单里**同名**的第二篇
      文档无法按标题精确匹配（动作的参数就是标题），只能被跳过并提示模型改读别的文档，尚无第二个
      消歧维度（如序号）；无标题文档已可按花名册占位串「未命名来源」读取，不再属于这一条。
      （原 (b)「取样通道的 I/O 放大」已由 PR #859 解决，见 `fangan_done.md` §59。）
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
        了这条纪律），而首轮原文播种的词法臂用的正是 `plan()` 里 `expand_query` 产出的高/低层
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
- [ ] **`AskService` 的 synthesis-only 入口（全局引用复核重合成的前置件）**。今天全局回答的
      引用核对发现失败时，回答照常整份交付：未通过的引用逐条带标记，答案下方一句说明
      （`fangan_done.md` 第 48 条）；不作废任何内容，也不要求用户重新提问。所以这个入口不再是
      任何已交付行为的前置件。只有产品日后想要「剔除未通过的证据、用剩下的证据再合成一次」
      时才需要它：重新调 `AskService.ask` 会重新检索、重新冻结，得到的是另一份答案顶着这一份的
      身份，所以要一个只做合成、复用已有证据与已解析引用的入口。要不要做仍待产品拍板；若做，
      连带要决定：重新合成后的引用是否再核对一轮、轮次上限、推理轨迹里怎么如实记这一步，以及
      重新合成的回答与第一份回答的引用标记和 `citation_check` 汇总是什么关系（沿用、替换，
      还是两份都保留给读者看）。
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
- [ ] **跨库最终答案的全局关键词臂 / 精确标识符臂（可选后续）**。两条 active-only 补召回腿
      已按**逐库联邦化**形态落地（双语关键词臂与精确标识符臂：每库各跑一次、各带冻结天花板、
      按库轮转合并并以 `GLOBAL_ASK_CANDIDATE_LIMIT` 封顶，精确臂以整节为单位且保底席位按库分配，
      失败不进覆盖回执；开关 `GLOBAL_ASK_KEYWORD_ARM_ENABLED` / `GLOBAL_ASK_EXACT_ARM_ENABLED`，
      合同见 `docs/product-and-api*.md` 全局问答一节）。仍未做的是另一种形态：在跨库合并**之后**、
      对最终候选池再跑一次全局口径的词法/精确匹配（成本与库数无关，但拿不到各库的 FTS 索引）。
      两种形态解决的问题不同（前者补逐库召回，后者补跨库排序与标识符定位）；后者只在逐库形态
      被证明不够时再评估。
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
      下一次 `communities` 结构变更时一并决定。
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
- [ ] **S2-A：协议绑定缺口只剩 `NotebookRepository.rerank`**（来源：PR #857 / #862 / #872 / #906）：
      `scripts/architecture_boundary_baseline.json::facade_protocol_binding_gaps` 起始 44 条
      （AskStateStorePort 20、McpMemoryRepository 4、NotebookRepository 20），其余已清零；剩下的
      `rerank` 经 `FacadePropertyContract` → `ModelClientProvider` 继承进 `NotebookRepository`，facade
      本体不实现它；#906 刻意未动。清零需要另立一项处理这条继承链。
- [ ] **S2-D：`facade_surface.json` 细节字段漂移无守卫，待拍板**（来源：PR #862）：
      `test_facade_surface_fixture` 只断言名字集合，`owner` / `consumers` / `patch_targets` /
      `signature` 的漂移没人看。全量等值前提是先去掉生成器对 `SURFACE_SOURCE_COMMIT` git 历史的依赖
      （CI 浅克隆拿不到），并且属于全仓 PR 政策变化——每个改消费点或签名的 PR 都要重基线。
      需要用户裁决要不要上这条政策。
- [ ] **S2-D：组件层「仅测试调用」成员未清点**（来源：PR #862）：审计脚本只统计 facade，看不到组件层
      （`AskService.ask_chunk_current` / `ask_reasoning_current`、两端 store 与 `AskStateStorePort` 的
      `ask_job_status` 等）只被测试调用的成员；登记为一类待清点项。
- [ ] **S2 批 B 待合入**：退役 10 个 KG 治理 facade 方法（PR #907，开放中）。合入后把它并入
      `fangan_done.md` §59 的 S2 条目并删除本条；其后仍刻意保留的只测试调用成员是
      `rebuild_notebook_kg` / `relink_notebook_kg`。
- [ ] **S7 待合入：Knowhow 面板焦点陷阱**（PR #910，开放中）：面板 `aria-modal` 却没有 `inert`/焦点陷阱，
      背后的引用按钮可 Tab+Enter（既有无障碍问题，S6 合入后才开工）。#910 合入后并入 `fangan_done.md`
      §59 的 Knowhow 条目并删除本条。
- [ ] **G1 本机热门禁 ≈77 s，目标 80 s，余量在被吃掉**（来源：ci60 收官 `ci60-closeout.md`，PR #898）：
      后端测试每周约增 1000 条，本机门禁每 1000 条约 +4 s，余量只有约 3 s。下一个杠杆是
      T3-b1——把「全仓 AST 正向扫描」类守卫的真实树扫描挪到每日 G2（约 −3.5 s，约 76 s）；代价是 PR 上
      违反这些守卫的改动要到次日 G2 才报红，**需要用户裁决**，未获裁决前不动。
- [ ] **`test_extension_service_launchers.py::test_launcher_forwards_cancellation_while_companions_start`
      高负载下超时**（来源：K1 第 6 轮验证）：重型本机负载下 4/4 次撞 5 s 子进程超时，干净 master 上
      26/26 通过，判为锁外压测负载所致而非回归。每天看一眼每日 G2，若在干净 runner 上复现就立项，
      改成握手/受控时钟判据，别靠放宽超时搪塞。

---

## 验证基线（每完成一项都要保持）

- `bash scripts/check.sh` 全绿（后端 pytest 全量 + 前端测试 + tsc + production build）；
  PostgreSQL 相关改动另跑 PG lane。
- 离线（无 LLM / embedding / MinerU）闭环不回退。
