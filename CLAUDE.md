# Claude Code 在本仓库的操作规范

本文件对 **Claude Code** 生效，每个会话自动加载。

`AGENTS.md` 是本仓库完整的开发契约，但 **Claude Code 不会自动加载它**（只加载 `CLAUDE.md` 与 `.claude/rules/`），所以本文件承担两件事：把必须随时在线的红线内联在这里，再给出 `AGENTS.md` 的章节索引供按需查阅。**两者冲突时以 `AGENTS.md` 为准**——它是真源，本文件是摘要（穷举的例外见第一节末尾）。改动开发约束时，本文件与 `AGENTS.md`、两份 README 一起改。

---

## 一、红线

不读 `AGENTS.md` 也必须遵守的部分。

### 工作区

- 凡任务会写仓库代码、测试、文档或配置，第一次写入前必须新开 **git worktree** 和分支；该任务期间主 checkout 只读，小修也不例外。当前目录若已是隔离 worktree，就在原地继续。纯调研、设计、状态汇报和只读审查除外。
- **在 worktree 里跑 `npm install` 之前先 `ls -l frontend/node_modules`**：若它是**软链**（指向主 checkout 的共享安装树），装依赖会写穿真树，绝不能跑——改依赖去主 checkout，或用 `cp -Rc` 拿隔离副本。若它是真目录或不存在，照常装即可。软链由开发者本机的 SessionStart hook 建立，不是仓库产物，所以这条**必须先看一眼再决定**，不能当成无条件红线。
- 不回滚用户的改动；不删生成物或用户提供的文件，除非用户明确要求。
- 改文件用 Edit/Write，不要用 shell 重定向整体覆写。

### 交付完整性

- **全栈对等**：面向用户的后端能力必须在同一次改动里带上前端 UI。不接受只做一侧。
- **深度报告时间**：已完成报告的列表与详情以 `updated_at` 同时显示浏览器本地时区的精确生成时间和相对时间，并显示从原子 `outline_ready → generating` 认领时持久化的 `generation_started_at` 到该终态写入的总耗时；意图/大纲确认等待不计入，旧报告缺开始戳时不编造耗时。未完成报告只显示创建时间，不冒充已有最终耗时。不能退回只显示“几小时前”。
- **深度报告可信度与全篇综合**：报告复用共享意图并持久化 `result_scope` / `completeness_required`；完整枚举未接入时不得把相关性检索伪装成“全部”。假设不是证据也不进入检索串。「资料基础」必须由 SQL 聚合和有界代表页生成，完整 source-family 映射不得进意图、大纲、轮询或 prompt；只按实际触达来源有界解析身份。身份未知来源在参考文献中逐份可见且不增加独立来源数，Top-1 集中度按全部未知支持归入最大已确认族的保守上界计算。深度 ≥ 8 时必须先完成全部章节检索，再至多调用一次全篇综合蓝图，最后并行分节写作；低档保持逐节检索→撰写流水线。综合失败/跳过与账本可用数须在有信息量时上屏：只静默已知低档的 `not_requested` + `0/N` 纯否定回执，旧报告深度未知、高档 no-op、任何跳过/失败与可用账本均不得隐藏；模型/校验失败 fail-open 且可观测。高风险引证扫描始终披露、只代表锚点覆盖；自动降级由独立默认关闭开关控制。趋势置信度按身份可确认的可区分引用来源封顶，来源身份缺失时只能标为研究性；终审只接收语法合法且总量有界的跨节上下文，只审计、不改写正文。数值上限只登记在 `docs/product-and-api*.md`。
- **来源选择检索范围**：检索范围有**两个互相独立的维度**，都由问答意图预检/执行和新建深度报告共用、都默认全选——当前 notebook 每个可见导入来源一个复选框（`source_scope`），每个已挂载参考库**整库**一个复选框（`base_scope`，不展开到库内来源）。`source_scope` 的 `include` / `exclude` 只能指向当前 notebook 的可见来源，`base_scope` 的 id 只能指向当前已挂载的参考库（否则 422）；API 入口把两维的排除式选择都冻结为 include 硬上限并下推到有界候选生成。**提交了显式范围对象本身不等于「已收窄」**：冻结对每一次提交都生效——包括前端默认的全选载荷，每次请求都会带上——所以单看线上格式（`mode == "include"`）分不出「选了全部」和「选了子集」；是否收窄要在冻结那一刻比较所选集合与该维度全集的大小，全选提交冻结后等于全集因此不算收窄，当前库的 PPR、私有 Memory、社区报告与语料画像照常保持开启，如同压根没提交过范围。它与模型确认的 `QueryIntentContract.source_scope` 取交集，后者只能继续缩小。收窄**来源**维度时隐藏 Memory/Knowhow 投影证据不参与，无法安全按来源预过滤的当前库全图/PPR/关系/精确章节/报告整库画像通道跳过，base KG 种子仍可不经组合图而直接映射 base 原文。**409 判据是两维同时为空**（本地有效范围为空**且**参考库有效范围为空），即判据是「这次勾了哪些库」而非「挂了哪些库」，Ask 三个入口与报告的创建/确认/生成三处都生效；请求没有收窄的那一维按 notebook 真实全集作答，两维都没收窄的请求交给既有可用性闸。前端同样禁用问答输入和新建报告，「全选」/「清空」必须一并管两维。报告的解析范围在意图确认与生成前重验并贯穿规划、生成；任一维省略范围字段都保持历史整库行为，且绝不替用户伪造出一份选择。
- **参考库维度与 `restricted` 正交**：仅取消参考库勾选、本地来源全选时**不得**进入限定模式——那会关掉**当前库**的全图/PPR/关系扩展/精确章节/整库画像并让私有 Memory 完全不参与，用户不该为「少借一个参考库」付出「当前库检索能力被砍」的代价。当前库通道（PPR、私有 Memory、社区报告、弱支撑关系、精确章节查找、报告整库画像）只看 `restricted`；跨库通道（集合枚举与集合地图、federated 检索、社区扩展、图漫游、`follow_chain`、证据装配）必须各自认库维度。库级收窄**只在参与库解析这一个边界**统一生效（一个列表、过滤一次、被多处读），因为**计数与行必须同一谓词**：只滤行不减分母会让走完的完整清单被永久判成 `concurrent_change`；`enumeration_active()` 不因库维度关闭，工具照常提供、只是作用域收窄。**泄漏面包括查询词本身**：社区扩展拿参考库实体名当查询词，这些词会进可见轨迹、进已用查询记录并回喂反思，所以收窄必须发生在**取实体名的入口**，光过滤结果无效。**权限参与集不受勾选影响**：`resolve_participants` / `mount_sql.py` 是检索与权限共用的唯一定义点，库级收窄只做在检索消费侧，否则用户会打不开历史答案里参考库来源的详情。**已登记接受的二阶代价**：图漫游与 PPR 的过滤在遍历/截断**之后**（前置会污染进程级图缓存或让每种勾选组合重建一次千万节点图），被排除库的节点仍占扩散名额/候选席位、仍可当中转跳，取消勾选后图模式的允许邻居会变少——刻意接受，不是缺陷。回执 `AskResponse.retrieval_scope` 是只读展示字段：库名是授权时刻的持久化快照（不按当前挂载重新映射），检索侧从不回读，两维都没收窄时不产生；全选也会发显式载荷，故前端只在**确有收窄**时渲染「检索范围：本库 N/M · 参考库 K/L」，措辞与模型点名路径的「本次依据：N 个指定来源」区分。数值上限只登记在 `docs/product-and-api*.md`。
- **来源删除/重解析不做请求内整库回填**：已回填 notebook 通过 `knowledge_object_sources` 反查受影响 KG 对象；未回填 notebook 的交互式删除/重解析用 keyset 分页的数据库原生 evidence 筛选当前 source、保持 marker 不变，绝不能在请求内逐对象反序列化并回填整本库。旧库筛选仍可能扫描该 notebook 的 KG 行，大库应显式离线运行 `backfill-source-index` 预建/修复。投影清理前先锁 source 聚合行，防并发抽取在游标通过后复活子行。大量对象的删除 SQL 每条最多 500 个 id（只约束语句/参数，不承诺常数时延或后台删除），引用图片用一次数据库往返取回并删除；前端必须立即显示该来源“删除中”并禁用重复删除，且以 notebook 级删除墓碑拦住导航/列表旧响应复活已删行。
- **公式渲染**：紧邻正文、独占一行的单行 `$$...$$` 仍按块级公式处理；Ask、报告、Memory、Knowhow 的宽公式只在所属内容块内横向滚动。来源详情、知识对象标题及知识图谱「出处」卡中的 `formula` 元素共用 KaTeX 渲染；直接调用 KaTeX 前先剥除包裹整值的 Markdown 数学定界符，解析失败必须显示原始内容，不能留下空白或仅错误态的可视化。
- **文档同步**：影响安装、产品行为、架构或开发约束时，`README.md`、`README_zh.md`、`AGENTS.md`、`CLAUDE.md` **四份**一起改，并同步 `docs/` 下负责该主题的中英文权威文档。根 README 只保留精简入口，详细契约不得重新堆回 README。漏掉本文件，Claude Code 侧的规范就会悄悄过期——那正是本文件存在的原因。外部 Agent 的首次接入步骤由 `docs/agent-mcp-memory-sop.md` / `_zh.md` 成对维护；界面路径、Codex/Claude 配置、scope 表、`scripts/example_mcp_memory_client.py` 的可运行命令、Memory 人审门与撤销说明必须和产品/API 契约同步。
- **用户使用总览**：先对 `/admin/users` 返回的完整用户集合排序，再做前端分页；默认每页 20 条，可切换 20/50/100 条。用户名、角色、注册时间、各类用量、最近活跃和文档上限表头可切换升降序，排序或翻页时清理已隐藏行的临时交互态。问答用量及展开后的笔记本明细统一返回/展示 `questions`，按归属该用户的持久 `ask_jobs` 提交次数计数（失败/取消任务也算一次已提交提问），不能拿 `conversations` 会话容器数冒充提问次数。用户总数包含其在加入的只读共享笔记本中的提交；展开清单刻意沿用 owner-only，只分解自有笔记本内的提问，不能假设其合计等于用户总数。旧 `conversations` 响应字段仅为兼容保留并标记 deprecated，前端不再用它展示问答用量。
- 完成 `silicon_notebook_fangan.md` 里定义的特性时，同批更新 `fangan_done.md`。
- 提交的文档保持通用口径：绝对解释器路径、本机端口占用这类**机器特定细节不进 git**。

### 硬门

- 门禁分为 G0 目标测试、G1 `bash scripts/check.sh` 标准门（编辑期及每次 PR/push，默认 12 个后端 pytest worker + 语法/契约/harness + 前端 test/负责类型检查的 production build 三条并发泳道；Node/Vitest 各限 4 workers，Apple Silicon warm 目标 ≤60 秒）、G2 `bash scripts/check_extended.sh` 扩展门（再补跑 slow 真实索引/性能测试与 architecture_contract 全仓语义扫描，每天 18:17 UTC/北京时间次日 02:17 一次，也可手动触发）和 G3 独立 PostgreSQL 集成门。G1/G2 的 backend marker 必须精确互补。测试加速保持断言和生产默认值不变：全仓语法扫描按进程复用解析结果，纯缓存策略不搭建无关数据库/索引，生命周期脚本仅通过私有 `_SCRIPT_TEST_*` 控制缩短测试轮询，并发顺序用 event/barrier 而不是固定 sleep 证明；与规模无关的边界分支只降低测试局部阈值并另断言生产 floor，共享同一不可变产物的多组断言只做一次真实构建，纯算术/观测分支用最小接缝且邻近集成测试仍真实构建、打开和查询产物；`next build` 未启用 `ignoreBuildErrors`，因此 G1 不得在它之前重复运行同一遍 `tsc --noEmit`。
- **仅 Codex 的沙箱规则（Claude Code 不适用）**：Codex 第一次运行 `scripts/check.sh` 就必须申请沙箱外执行，因为后端生命周期测试会绑定 loopback 端口并管理子进程；不得先在沙箱内试错。GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）也必须直接申请沙箱外执行；普通本地只读 Git 检查仍在沙箱内完成。
- 数据库专项门只覆盖直接 PostgreSQL 后端；已退役的 SQLite 后端实现专项测试、SQLite→PostgreSQL 导入/正向 shadow 测试与跨后端 parity 测试不得重新加入当前套件。
- **schema**：加表或改结构必须**追加** `_migration_N` 并 bump `SCHEMA_VERSION`，不要塞进已封版的旧迁移——版本闸会对已部署库短路，`IF NOT EXISTS` 救不了没被执行到的语句。当前 SQLite schema 为 v39；PostgreSQL checksummed schema 为 v17；除关系端点 keyset 覆盖索引和关系补全的来源代次水位外，两侧还包含生成中 Ask 的浏览器提交时间 `ask_jobs.asked_at`、图谱质量分析的三张预计算产物表、按 `(source_id, element_type, created_at, id)` 的集合枚举索引 `idx_source_elements_source_type`，排除隐藏 Memory/Knowhow 投影的部分可见来源身份索引 `idx_sources_visible_identity`，以及命令目录抽取的 `catalog_jobs`／`catalog_candidates`（前者带按来源的 `queued`/`running` 条件唯一单飞索引与记录创建时刻来源代次的 `source_generation`；后者**刻意不给 `job_id` 加外键**，否则 `catalog_jobs` 不再是叶表，正向 shadow 就没有可用的 UNIQUE 停车方案）与它按标题解析目标表用的 `idx_knowhow_tables_nb_title`（`knowhow_tables(notebook_id, title, created_at, id)`，v39 里唯一建在既有表上的索引）。
- **界面词汇**：面向用户的文案只用「界面词」，不得出现 `projection`/`tier`/`canonical`/`chunk`/`KG`/`schema` 这类内部黑话。真源是 `AGENTS.md`「界面词汇表」，`scripts/check_ui_vocabulary.py` 是硬门。**唯一放行的英文界面词是「图谱 Schema」**（图谱对象类型/字段管理，原「内容类型」，现从知识图谱视图头部进入）——守卫的 `SANCTIONED_UI` 只放行这一个复合短语（带 CJK 前置断言，不吞「知识图谱」尾字），裸 `schema`/`Schema` 仍拦。
- **错误文案**：deny by default，信任按**出处**判定而非文本形状。后端中文用户文案必须走 `backend/app/api/deps.py` 的 `user_error()`（打 `X-User-Message` 头），前端翻译只在 `frontend/app/errors.ts`。
- **Knowhow 导入校验**：属性按行/按列必须在解析前按用户选择统一；横向合并记录分组导致展开后首行重复时，应提示改选「属性按行」。只有 `GridParseError` / `KnowhowImportValidationError` 的固定可操作文案可经 `user_error()` 上屏，其他 `ValueError` 保持诊断专用。
- **`object_type` 标签**：后端 `OBJECT_TYPE_LABELS` 与前端 `KG_TYPE_LABELS` 必须逐字一致，改一侧就要改另一侧。
- **架构守卫**是语义化的（`{path, scope, kind, target}`），**不含行号**——仓库里任何提到「行号钉死」的注释都是过时残留。重生成走 `--rebaseline-surface` / `--rebaseline-callers`；新端点必须跑默认模式刷 `api_contract`。
- **knowhow 变更历史**（`knowhow_changes`/`knowhow_milestones`，schema v26）：knowhow 表的**每条写路径**必须在写事务**最后一步**经模块级 `record_change` 追加一条流水（存 before/after + 变更后整表指纹，复用传输守卫的 `_FINGERPRINT_SQL`）；`backend/tests/test_knowhow_history_coverage_guard.py` 是硬门，对 SQLite 与 PostgreSQL 两份 `KnowhowStore` **各扫一遍**，新增写方法漏挂就报红（判据是 `record_change` 落在写事务 `with` 块**体内**——挪到块外就丢了原子性、照样红；不钉的是它在块内的位置，「挂在最后一步」这半条仍靠评审）。回退是逆序 delta 重放 + 前后置指纹守卫（行/列复用原 id 保引用与代码附件）；里程碑创建与 `create_milestone` 的复检必须在 `BEGIN IMMEDIATE` 之内，清理只删最老连续前缀且永留 head。完整契约见 `AGENTS.md`「Architecture Baseline」的 knowhow 历史条目与 `architecture.md` § 3.7。
- **Knowhow 批量规整/审计/列宽**：候选生成并发固定为 `min(3, knowhow_reformat 实时服务容量)`，状态失败回退 2；相同 `(column_id, trimmed 原文)` 只允许 single-flight，只有成功且未 stale 的结果可复用，取消后停止调度并丢弃迟到 epoch。保存仍按既有完整物理/共享单元串行，必须保留 frozen snapshot、`expected_before`、anchor/精确成员守卫、409 stale 和关闭弹窗后 reload。diff 在同一弹窗做有界 Markdown 行级+token 级比较，超长降级；若批次已观察 stale，打开已保存项必须先关闭弹窗、等待带 epoch 守卫的父表 reload 完成，再按新 detail 重算并打开 `position,id` 最小代表格，禁止闪开旧目标；reload/epoch 失败或行/列消失时不得打开，并走现有可恢复的表操作错误横幅。整体确认不变成逐项 accept/reject。主表只从有界可见行样本 memoize `colgroup` 宽度，且必须在 Markdown 正则/分行/grapheme 之前截断固定 code-unit 前缀，保留 fixed layout、横向滚动与 sticky 首列。owner/权限/`created_by` 继续用稳定 user id；普通 session 审计 label 统一 username→display_name→id，Agent 用 profile_name，复制/导入/转移必须拆 identity id 与 actor label；历史 user-id 只在读时有界批量解析且同步 identity 查询必须在线程池。`origin=agent` 的 change 只解析 `payload.before` 中被替换的旧人类 updater，actor 与 `after/current` Agent label 原样保留。禁止 N+1、破坏性迁移或为显示重写参与 fingerprint 的 `knowhow_cell_code.updated_by`；单格代码 GET/PUT 响应必须返回可读 `updated_by`，保证 session 保存后立即显示来源。
- **knowhow 知识对象检索**：`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED` 默认 true；格子对象进入 reasoning/graph 的节点检索并保留行跳转引用，false 只回滚该直接节点路径，格子 chunk 仍可搜索。动态类型必须按 `knowhow_tables.hidden_source_id` 收窄；chunk-vector→KO 旁挂版本同时覆盖图变更序号和 Knowhow 向量代次（纯向量修复不 bump KG），并随 KG 变更显式失效。
- **图谱质量分析视图预计算**（`kg_community_edges`/`kg_source_profiles`/`kg_analysis_artifacts`，schema v36）：三张产物表在 `rebuild_communities` 里顺带产出，并与**板块划分本身 + `community_seq` 戳**一起在**同一个**写事务里发布——绝不能拆成「先提交板块、再提交产物」，那会在生产上留下分钟级的「新板块 + 旧账本」窗口；但产物的计算必须整个待在那个事务**之外**（写锁是进程级的，全表扫进事务就锁死全库写入）。因此来源画像的 SQL 不 join `community_members`（那一刻板块还没落库），canonical→板块 由内存 membership 完成，结果逐字等价，在线端点（`GET /notebooks/{id}/kg-analysis[/sources]`，只需 notebook 读权限）只读产物、不做全表扫。**预计算有自己的新鲜度闸**，判据是「账本缺失或账本 seq ≠ 当前 `community_seq`」，绝不能跟社区图共用一个闸——已经 rebuild 过的库 `community_seq == kg_mutation_seq` 早已成立，共用闸会让账本永远短路成空、只能靠 `force=True` 恢复。`kg_source_profiles` 是唯一允许合法缺席的一份（零板块库写一张全 0 行的表等于撒谎，应让它缺席）；`kg_community_edges` 有硬行数上限，截断必须显式记账（`edges`/`edges_total`/`truncated`），不静默丢行——但那个上限只管**落库多少**，聚合阶段的有界性另由「边消费边释放」保证（折叠用 `popitem` 就地搬空输入边图，不得遍历后保留：两份同量级结构同时驻留就是生产库上的 OOM）。收敛率必须**按对象类型分列**返回，四类混算会把 concept 真实收敛率稀释约 3 倍。**新鲜度有两条世代线**：`kg_mutation_seq` + 依赖合并的四份另盖 `built_at_cluster_seq`；其中依赖板块的两份盖的是**板块划分**建在哪一代合并结果上，只补账本那一轮显式记 `null`（划分是库里现成的、建在哪一代无处可查），于是 `stale` 是三值的，`null` 绝不能被显示成「与当前一致」。完整契约见 `AGENTS.md` MVP Scope 与 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`。
- **数据库后端选择**：DATABASE_URL selects the formal repository backend through one repository factory. Exactly one active repository backend is selected centrally from `DATABASE_URL`. SQLite and PostgreSQL are both available direct backends; SQLite remains the shipped default. 选择只存在于 factory；service/store 不判断 dialect，也不 import 对侧 adapter。`SHADOW_DATABASE_URL` 单独设置仍不参与选择/同步；SQLite39/PG17/epoch1 临时 shadow 边界已有 preflight/control/guard、run-bound 原子 snapshot、有界可续跑 baseline COPY/H0，以及 fail-stop 单消费者正向 apply 原语。正向引擎从 checkpoint+1 连续读全局 SQLite seq，在短只读 snapshot 中仅为 upsert hydration 当前行，delete 保持 key-only、hydrated bytes 为零，并显式保留七张表的 rowid ordinal；同一 stable key 在 accepted prefix 内保留最后 event 并按全局最后 seq 排序，raw seq/checkpoint 仍连续，每个 identity 的最终 actual apply 覆盖 synthetic dependency contribution，只有 dependency-only identity 才计一次 synthetic 行与 bytes；短读窗口若在 allocated high-water 前结束，会在 hydration/apply 前立即判为 suffix gap；满窗口低于 high-water 时在同一 snapshot 探测相邻 seq，缺失即失败；批次硬上限 4096 events/64 MiB；仅一个 final bundle 可独占超限，同 key replacement 若在已有其他 actual bundle 时使 bytes 超限则回滚并留到下一批。FK 父闭包只读同一已验证 source snapshot，每事件最多 64 行；固定 v16 图按 FK constraint branch 计数的上界为 9 个 row slots，依赖行计入 bytes 且批内去重，不扫描 suffix log。PG 仅延后 FK/UNIQUE ordering SQLSTATE；CHECK/NOT NULL 立即 poison。精确 PG17 catalog 派生的 89 个 unique surface 均有静态停车方案：nullable 列用 NULL；无 FK/CHECK 的 text/bigint 列使用按其他唯一列的非 NULL 等值/NULL `IS NULL` 和固定 predicate 限定范围的确定性候选（`C` collation 文本 max 拼 `chr(1)`，或先走可索引 bigint MIN/MAX 快速路径选择 min−1/max+1，仅在两个 int64 边界都已占用时扫描首个 gap）；仅对无入向 FK 且批内存在 current-final 恢复行的叶表做同事务 delete/reinsert。停车状态以 `(unique surface, row identity)` 为单位，单个 stagnant pass 停车所有可独立停车的冲突，final apply 成功后清除该 identity 的所有停车面。限制为 8 passes、32 actual statements/apply、16384 actual statements 总量；每次候选查询都计入预算，ordering、statement、pass、候选搜索或候选 UPDATE 容量耗尽均 non-poison，而最终窗口仍无法停车的 UNIQUE 冲突按最早实际 seq poison。`run_forever` 从 256 events/8 MiB 自适应倍增到硬上限，仍阻塞则 non-poison。PG apply 事务 claim worker 后、业务 DML 前复查既有 run/direction poison；poison 发布在 binding/checkpoint 校验后锁定检查该方向任意既有记录，完全相同视为 ACK-loss 成功，不同则 stale 且绝不新增第二条；apply、ack-loss 识别和 poison 发布都绑定 snapshot source/target 与 live target identity，snapshot 与业务 apply 前均要求 `progress.applied_seq == checkpoint.last_seq`；事务按 migration→control→run→worker→checkpoint 锁序，锁 ledger+66 表并复核精确 catalog，再同事务提交业务收敛、脱敏 progress 与 checkpoint CAS。`ProgramLimitExceeded`/`DataError` 候选失败按 non-poison capacity 处理，`QueryCanceled` 保持瞬态并整事务有界重试，SQLite path/file binding 失败通过专用 identity 异常分类，不依赖异常文本；已证明的确定性错误只记录实际阻断 seq 的一条脱敏 poison。每个有效 batch 结局恰好记录一条 metric，batch events 使用实际 accepted/observed raw-event 数而非 lag，并尽可能保留 retries；指标不得带表名、key、行值、URL、worker id 或异常文本。禁止持有 SQLite 时等待 PG。显式运维 CLI 负责 preflight/start-forward/status/verify；前台 worker 使用数据库时钟排他 lease、SIGTERM/INT 批次边界，并仅在 FULL 校验、barrier/replay/poison、至少 7 天/100,000 events tail 等边界之后保守清理。这只是 SQLite-active 正向 shadow；cutover、反向复制和自动 `DATABASE_URL` 交换仍未实现。PG 向量存 `bytea`，不需要 pgvector，生产仍固定 `--workers 1`。
- **Shadow 校验**：在 SQLite 只读 snapshot 记录 `Hv` 并流式落入 owner-private 临时 spool，释放 SQLite 后再等 PG checkpoint；以 `REPEATABLE READ, READ ONLY` 固定 `Ht`，再用新 SQLite 事务扫描 `(Hv,Hseen]` 的 retained dirty key，只排除这些 concurrent key，且 verifier barrier 保持到报告提交。Structural 覆盖精确 schema/guard、stable key/hash、FK/unique/cascade、storage-root 文件引用；Full 增加领域投影、float32 bytes/dimension/norm/抽样 cosine 和固定中英检索门禁；Cutover 复核 write-frozen，并要求 `Hv=Ht=MAX(seq)`、零 concurrent、100% coverage 与前一轮完整 full/cutover。报告不得含原文、Memory、token、密码或 URL；仅同级或更强的 clean run 可 supersede drift。
- **SQLite source open 分类边界**：只在 `open_fresh_live_sqlite` 调用处把非瞬态 `sqlite3.OperationalError` 归为 source-binding identity；locked、busy、interrupted open 仍按瞬态整批重试，后续 SQLite operational error 保持原 schema/query 分类。
- **Shadow baseline 安全边界**：snapshot 目录必须 owner-only 且不可为 symlink；snapshot/live fence 必须 fresh 打开 `SqliteDatabase.db_path` 当前文件，禁止复用线程缓存连接，并在 open/transaction 前后及 snapshot 发布/PG commit 前复核 resolved path + `(st_dev, st_ino)`；这是合作式运维边界，检查窗外替换文件不受支持且下一次检查 fail closed。COPY 的所有业务 SQL 全限定到 run 绑定 schema，起始绑定、每个提交批次/完成点和最终 H0 前均在短 `BEGIN IMMEDIATE` 中复核 live capture 仍启用，且不得把该 SQLite 栅栏跨 PG prefix proof/`ANALYZE` 持有。JSONB prefix proof 只在 JSON 子树内统一有限 int/float/Decimal 的精确十进制语义（bool 排除、负零归零），普通 SQL 数值列仍保持类型差异。Resume 使用有界 named server cursor，PG statement 有 timeout，取消在 proof/migration/analysis 间轮询；起始/最终完整验证由 checksummed packaged migrations 派生并覆盖 v9 table/column/PK/FK/unique/check、operational+GIN index 与 `public.pg_trgm`，逐批路径不得反复扫描 66 表 catalog。
- **最终 H0 lease**：最终 SQLite fence 不是瞬时检查；只能在 PG 双锁/run/table lock 与 66 表长 proof/`ANALYZE` 全部完成后取得，必须保持到 PG H0 checkpoint + run progress 事务实际 commit 成功再释放。PG 事务或 commit 失败时 PG 不落 H0，并释放 SQLite；持 fence 时不得再等 PG pool/advisory lock 或执行长 PG 工作。
- **切换与批处理边界**：只改 `DATABASE_URL` 不会复制、迁移或同步既有数据；停机 importer/activation 使用 `scripts/migrate_sqlite_to_postgres.py`，SQLite-active 连续 shadow 使用 `scripts/shadow_sqlite_to_postgres.py`，两者不得共用 target。停机 importer 只排除 SQLite-only 的 `shadow_capture_control` / `shadow_change_log` 运维表并把排除写入 receipt；退役用户数据表仍要求为空。正式切换必须停写、停服务、验证备份、只改一个 URL、启动后校验 readiness/认证/数量/代表性读取再放流量。`batch_ingest` 除 `vectors-to-blob` 外的 mutation phases 同时支持 SQLite/PostgreSQL；PostgreSQL 直连维护必须在构造 repository 前要求 `--confirm-service-stopped`，再由独立非池化 session 持有数据库级 advisory lock，结束时释放锁并关闭 repository。该 flag 只是运维确认，不会停服务；生产 wrapper 共用同一安全 opener。`vectors-to-blob` 因 PostgreSQL 已用 `bytea` 而仅适用于 SQLite。`kg` 的完整、`--limit` 与 `--retry-partial` 抽取都必须复用页面分析的 durable `kg_build_jobs` 单飞/探活/熔断/排空协议，批量末尾聚类与索引在 job 成功前完成；页面继续分析会自动修复 partial，CLI 则用显式 `--retry-partial`。修复时模型窗口未全成功或新结果为空保留旧图，仅“零失败窗口且非空”的结果事务替换。显式 `--workers` 必须在 repository 构造前写入本进程 `kg_job_concurrency`，不得只打印而不生效。`scripts/check.sh` 保持离线，PostgreSQL 16 只走独立本地/CI integration lane。
- **离线批处理大库边界**：完整/限量 KG、metadata、reextract、向量、关系和反向索引的 PostgreSQL inventory 都必须用 `C` collation 的有界 keyset 页；KG inventory 按原始 source 页推进，不能为凑满 eligible 页无界扫描。所有 `kg` 抽取都保留 durable `kg_build_jobs` 单飞语义，只把 inventory/提交窗口分页；`all`/`reparse` 等非 durable 池化阶段收到 Ctrl-C/SystemExit 时必须在 advisory lock/repository 释放前取消排队任务并排空已接受任务，SIGTERM/SIGHUP 保持默认进程级终止。
- **Knowhow 智能补全空列**：记录型表从行详情、带行标题分组的表从概念矩阵的物理分支显式发起；只有缺失格或精确空串可补，已存纯空白文本不算空。一轮请求把同表最多 8 条参考行（同 anchor/行标题分组优先，再比较已知列相似度与覆盖度）与一次有界 `ReasoningRetriever` 全库检索合并；全库只指当前 notebook + 当前有效显式挂载库。它复用 Ask `reasoning` 的规划/联邦检索/反思/扩展/查询期推导，但补全专用策略会在候选进入模型反思前排除私有 Memory 与当前表自身投影，并关闭来源归属不透明的 PPR/社区扩展与精确标识符通道（同一 `allow_*` 策略位机制——补全的查询是 JSON 信封而非自然语言，否则会让 seed pass 恒定探测信封自身的键名）；绝不能调用 `ask_reasoning`/`ask_answer`、创建对话/job 或保存 Ask 答案。响应必须带最终推理轨迹和服务端生成的库内证据 key；模型只能引用合法 evidence key 或同表行 id，无合法引用的建议强制 abstain。`reasoning_agent` 负责检索，`knowhow_complete` 负责结构化合成；两阶段都用 system 级不可信证据指令。推理响应畸形、任一 provider 未配置/执行失败，或合成响应不可解析/顶层结构不可用时显式失败；单条建议畸形则过滤、降级或转成 abstain，任何情况都不能静默退成同表补全或伪造离线结果。审阅弹窗分开显示同表参考与禁用链接/图片的库内 Markdown 证据，保持可拖动和逐项人工确认；确认仍走普通 cell PATCH，固定携带 `expected_before=""` 和 `origin="llm_complete"`，并保留正常历史与同步语义。
- **Ask 会话即时入历史**：历史按带亚秒精度的最近活动时间排序（SQLite 还须按绝对时刻处理 UTC offset 变化），不按创建时间。`/ask/stream` 的首个 `started` 事件必须同时带持久化的 `job_id` 与 `conversation_id`；前端当场将它置顶并设为当前会话。即使用户在 `started` 抵达前就切到同库旧会话，新会话仍须发布并可重新打开；历史发布、请求 generation 与终态摘要刷新都跟随当前 notebook，不依赖旧 run 或会话 epoch，同库旧调用要追随最新请求（包括跳过失败的中间请求），其他 notebook 的延迟请求也不能使当前库的有效响应失效。加载 notebook 最近会话或用户选择的会话详情期间，输入框和模式控制必须保持禁用，避免迟到详情接管新 run。若用户在 `started` 前点击中断，界面立即恢复草稿，但该 run 自己的 controller 仍继续读到 `started`、按返回的 job id 调取消端点后才 abort transport，不能先断流而遗失取消句柄，也不能与重连到其他 job 的取消控制串台。
- **Ask 提问时间**：以网页端提交瞬间为准并随答案持久化，不能拿服务端答案完成时间代替。悬停问题气泡在下方显示，点击固定，点击其他位置恢复悬停；今天只显示时间，本周其他日期显示星期+时间，超出本周显示日期+时间（日期和星期二选一），今年省略年份、其他年份显示年份。固定显示跨过浏览器本地零点时必须刷新。
- **Ask 回答时间**：以 `answers.created_at` 的答案写入瞬间为权威，通过 `AskResponse.answered_at` 同时进入实时 final 与持久化 payload；旧 payload 重开时从答案行回填，不加 migration。回答卡沿用提问时间的浏览器本地格式、悬停显示、点击固定、外部点击取消与跨本地零点刷新规则。
- **问答引用定位图谱节点**：从知识对象引用打开全屏知识图谱时必须选中并居中目标；目标不在有界高连接度核心图时，按引用真实来源 notebook（包括挂载 base）定向拉取并叠加其一跳邻域，纯 graph-BFS anchor 也必须保留所属 base id。浏览器始终用当前 active notebook 过权限；后端只在它的有效 participant 集内校验/解析对象并内部代理读取，挂载公共 base 不等于获得该 base 的直接成员权限。引用携带的原始 Concept id 通过有界 `cluster_fold_rows` 解析为接口返回的 canonical `focus_id`，同时保留用于 context 的 raw object id，不能拿合成 `K-*` id 查询 `knowledge_objects`；大库 viz 产物不可用时显式返回暂不可定位，不得进入会物化全 notebook cluster map 的 DB fallback。前端保持图谱打开并给出可重试提示，不得挂一个永远消费不了的 raw-id focus。
- **参与库资源的代理读取**：挂载参考库的来源详情、元素与图片资产只能经 active notebook 维度的代理端点读取（`GET /notebooks/{active}/sources/{id}`、`.../sources/{id}/elements`、`GET /notebooks/{active}/assets/{asset_id}`），浏览器绝不直连另一个库。与「问答引用定位图谱节点」同一条合同：先按 active notebook 过读权限，再**只**在它的有效 participant 集内解析目标——资源自己声明所属 notebook，不在集内一律 404（不泄露存在性），参与集每次请求实时判定，被挂库降级/易主/深拷贝中或取消挂载即刻失权。挂载公共 base 仍不等于该 base 的直接成员权限：裸 `GET /sources/{id}` 保持 owner∪member 口径不放宽，写入（重新解析/删除）刻意**不**代理，来源详情弹窗对参考库来源按只读渲染（收起写入按钮 + 标注「来自参考库《名》」，防御性 handler 也要复查）。代理读取的披露面也必须比 owner∪member 端点更窄：详情模型**去掉**（不是运行时置空）`file_path` 与原始 `error_message`（两者都可能带服务端绝对路径），改回 `parse_failed` 布尔；跨库的隐藏合成源（memory/knowhow 投影行，集合地图刻意把它们算进作用域）直接拒绝，绝不让「查看来源」把整条合成源摊开（Memory 按创建者私有）；跨库资产响应必须 `no-store`，否则取消挂载会被浏览器缓存静默架空一整天，与「失效即 404」矛盾。同库资源先短路，不为「一张图一次请求」的主流路径多付一次挂载 join；参与集解析仍只有 `mount_sql.py` 一个定义点。
- **逐步推理档位与完整枚举是两套合同**：`retrieval_effort` 的稳定 id 为 `overview` / `standard`（默认）/ `deep` / `thorough` / `exhaustive`。五档的每查询相关性结果数依次为 `4/8/8/12/16`；最终 `floor/aspect/cap` 为 `8/2/12`、`20/3/36`、`24/4/48`、`32/5/64`、`40/6/96`；最大 `步骤/首轮子查询` 为 `4/2`、`8/5`、`16/6`、`32/8`、`50/10`；KG/原文 prompt 字符为 `4000/12000`、`6000/30000`、`8000/50000`、`12000/80000`、`16000/120000`；直接原文段（公式/表格/图片等）进入最终合成 prompt 的条数上限（`answer_element_items`）依次为 `4/6/8/12/16`，按检索相关度降序择优（tie-break element id）而非插入序，仍占用同一份原文 prompt 预算。同五档还携带集合枚举工具的预算：`enum_page_size` 各档恒为 `50`，`enum_pages_per_run` 依次为 `2/4/6/8/12`，`enum_rows_per_run` 依次为 `100/200/300/400/600`。最终相关性预算按 `min(cap, max(floor, aspect × 实际查询数))` 计算，模型只可提前停，不能突破上限。「首轮子查询」只约束**首轮并发**：超出它的已确认主题种子在步骤预算内顺延执行，预算不足时在轨迹中披露并回喂反思（见第四节）。候选召回不随档位变化，但由部署参数独立控制：`CHUNK_RECALL` 默认 200，分别约束带索引 Chunk/KG 的 ANN 与词法窗；`RELATION_RECALL` 默认 200，分别约束 Relation ANN 与词法关系 ID 总窗，后者内部仍为 source/target 方向预留份额。这些默认值不是请求级硬上限。意图合同用 `ranked` / `complete` / `aggregate` / `hybrid` 区分范围；Knowhow 的显式完整请求必须走稳定游标而非放大 Top-N。五档共用每页 25 行、最多 50 页/1,250 物理行、8 表、每表 8 列、模型单元格摘录 1,000 字符、结构化载荷 256,000 字符、正文内联 100 行、结果卡初始 20 行；只有游标耗尽且表目录、行数、列元数据和所选表范围均稳定才是 `complete=true`。触及任一安全线或并发改表必须返回 `complete=false` + `explicit_partial`，不得声称“全部”；100 行 Knowhow 表可返回 `100/100`，低档位也不得降低显式完整枚举上限。物理行完整枚举目前只覆盖 Knowhow；来源元素（公式/表格/图片/代码块）、知识对象（概念/论断/公式/过程）与库内文档目录另有一套模型显式调用的**集合枚举工具**（`enumerate_elements`/`enumerate_kg_objects` 两个 reflect 动作，加这两个动作上的参数值 `enumerate.collection="sources"` 取库内文档目录；这两个动作没有让模型面动作空间越过 10 个——大纲便签的 `update_outline` 才是随后新增的货真价实的第 11 个动作，见下面「大纲便签与按节合成」条），其余集合（Memory 等）仍是相关性结果并须披露边界。
- **集合枚举工具**：三要素合同——① 输出有界（游标翻页，命中 `enum_*` 预算即停）；② coverage 由执行器计算，前端徽章直读 `TypedCollectionCoverage`（`returned_total`/`total`/`complete`/`truncated_reason`/`overflow_semantics`），绝不采信模型自报「已全部」；③ 作用域走既有挂载谓词（active notebook + 有效挂载库），与 Ask 联邦同口径。**清单永不含私有 Memory**：确认 Memory 是 owner 私有的（其他通道都按 owner 隔离），而清单按 participant 作用域取数、没有 owner 过滤，共享库里会被别的成员整份读走。排除是**无条件**的（单人库同口径），且**计数与行同一谓词**——元素侧在 `source_change_signal_rows` 里排 `source_type='memory'`（那条查询同时是计划/信号/计数的唯一来源，三处自动一致；三个信号列不在任何索引上，谓词是纯残余过滤），KG 侧靠每参与库一次有界 `memory_source_ids` 在既有过扫描里过滤行、并用 `knowledge_type_count_rows_for_sources` 按同一批 id 从分母里减掉（只过滤行不减分母 = returned≠total = 把完整清单永久判成 `concurrent_change`）；显式 `source_id` 那条绕开计划的路径自己再挡一次。看板计数（`notebook_catalog`）刻意**不**跟着改——它答的是「库里有多少知识」。Knowhow 投影源仍在清单内。集合地图（`[Collections in scope] ...`，≤600 字符）每 run 建一次，注入 plan/reflect 上下文，让模型先看计数再决定值不值得全量列；同一份地图还必须随 `ReasoningResult` 带出、包成带固定表头的服务端小块（无 `[k]`，整块 ≤800 字符）装进**答案合成**上下文最前，并进入合成触发条件——reflect 教模型「太大就别枚举、直接报数」，那个数必须真的到得了写答案的模型手里，否则「大集合 + 零其它证据」时合成压根不跑。行/页/**载荷**三个池全部是 run 级：每次动作只发剩余额度，执行器按 `payload_chars` 回传真实消耗，一轮里的多次枚举累计不得超过 `structured_payload_chars`。载荷闸收**两次**、量两种东西：执行器按紧凑 dataclass 计费（拦「读得比该请求允许产出的多」），`typed_collection_results` 再按 `model_dump_json()` 的真实 wire 形状计费（拦「传/存得比声明的多」——联合体两臂的默认字段 + result 元数据让 wire 明显更宽），并**先预留**每份 result 的信封（那是「被裁过」的披露载体）；裁到的那份降级为 `complete=False`/`truncated_reason="payload"`/`returned_total=实际送达条数`，合成预览必须按 `delivered_outcomes` 的送达视图渲染，否则 prompt 会列出卡片没有的行、块头还写着 complete。合成预览的 `inline_rows` 是 run 级共享额度，必须**分配**而非先到先得（先按 `max(1, inline_rows // n)` 保底，余量按序贪心补），否则第一份清单吃光额度、多集合问题只按一张卡作答。KG 对象翻页是不带状态谓词的纯 keyset 读（索引不含 `status`，写进 SQL 就是无界残余过滤），执行器读回后用同一份 `USABLE_STATUSES` 过滤，每次动作最多过扫描 `max_rows × 4` 原始行，触顶发 `truncated_reason="budget"` 的诚实 partial 并让游标越过已扫的不可用区段（保证续跑推进）。`replace_elements` 必须在**同一写事务**里推进 `sources.updated_at`（双后端同修），让变更信号与元素换代原子同步——首解析 under-count 窗口由此根治，不再是「已登记的低报」。每次动作的页查询数有**强制**上界（元素 `max_rows + max_pages`；知识对象 `参与库数 + max_pages + 原始行过扫描上限`），越界抛 `EnumerationInvariantError` 并 fail-open 成一次 skip；刻意不给分片首页计费（会重新打破宽而薄语料的 complete 可达性）。收尾稳定性复检必须**重新解析参与库集合**（同一个 `resolve_participants` / `mount_sql.py` 谓词入口），集合不等即不稳定，指纹/seq 也用收尾解析出的集合算——只按开场那份 id 列表重算，挂载/卸载参考库这类变化根本看不见。限定单一来源按**标题**（`enumerate.source_title`）表达并由服务端有界精确解析，零命中/多命中一律 skip，计划超过解析上限时直接拒绝解析（不从前缀断言唯一），绝不扩成全库枚举。工具不依赖图谱：无图早退只在作用域**同时**没有任何可枚举集合（元素/知识对象/**来源**三类全为零）时才触发，放行后 `kg_required` 仍如实为 `True`。来源数**计入**这道闸——纯散文库（有文档、零元素、零知识对象）恰是来源清单的主力场景，挡住它等于把闸拆掉、让「库里有哪几篇」拿回一句非答案；零源库因此仍然早退。**来源清单**（`enumerate.collection="sources"`，**不是**第 11 个动作 id：给出它就忽略 enumerate 的其他参数，字段只识别 `"sources"`、其他值落回按动作 id 分派；代价是反思步「下一步意图」仍显示「列元素清单」，真正发生的那步由 enumerate 步 summary 说清）列的是**用户可见来源**那一批——计划 = `source_change_signal_rows` 里 `user_visible` 为真的那些（可见性由各适配器在 SQL 里对 `list_sources`/`visible_document_count` 共用的可见谓词求值、作为**投影列**随行返回：不新造第三份拼写，也不另开查询——`source_type` 无索引，独立的「哪些被隐藏」查询只能整表扫，且就紧跟在 signal 查询之后），地图行尾 `| sources: N` 与清单分母出自同一个 helper（纯算术、零新扫描）；遍历顺序 = 来源页签的 `(created_at, id)`（两侧 `list_sources`/`list_sources_page` 也必须带 `id` 次键——并列 `created_at` 下缺次键会与目录分叉、让「前 N 篇」在页签与模型手上是不同的 N 篇；PG 本来就带。排序键随 signal 行同一次访问回来，双后端各自归一化，PG 侧先转 UTC 再 isoformat（`timestamptz` 的 offset 跨 DST 不是常量）；**指纹只吃前两个字段**，创建时间不进摘要，否则上线即让 L1/L2 全量失效一次；元素侧顺序仍按 `source_id` 不动——它的游标是 `(source_id, element_id)` keyset）；每份文档计一行，整份清单是**一个**分片（首个 hydration 窗口免费）；条目 = 显示名（论文标题优先）+ 文档类型界面词（`extraction_profiles.PROFILES`，未识别就留空、绝不吐 `academic_paper`）+ 已存摘要摘录，wire 上复用元素臂字段（`location_label` 装类型、`text` 装摘要、`element_type` 留空）。schema 里 `collection` 显示为**空缺省**（写成唯一值会让照抄模板的模型在列公式时也带上它、被静默改道），取值只在动作说明里并写明「会覆盖动作本身」。收尾除作用域指纹外还对**整条链已发出**的文档做一次有界批量复读、比对 (显示名, doc_type) 摘要（账目挂游标、仅内存、不进用户面 coverage），不等即 `concurrent_change`——论文元数据回填不碰 `updated_at`，指纹看不见它；摘要字段刻意不进该键（它的写者同语句就推 `updated_at`）。**账目回喂对它有一条定向豁免**：只有来源清单的链在账目行后附有界标题清单（≤20 条/每条 ≤60 字符/合计 ≤800 字符，超出写 `(+N more)`，分母只数有显示名的条目），因为 prompt 教的「按标题逐篇深挖」离了标题就断链；元素/知识对象清单的账目仍一个字正文都不带，摘要也不回喂。**范围指示语只在 prompt 层接地**：`prompts.SCOPE_DEIXIS_GROUNDING` 一段共用文本进意图契约 + 两份规划拼写 + reflect，教模型把「当前notebook/这个库/知识图谱/KG」解析成范围后**剥掉**、不带进任何子查询/关键词/`exact_term`，但问题本身要原样留；刻意不做确定性词表剥离（那是被否决的词法路由，且会误伤真在讲知识图谱的文档）。总闸 `REASONING_ENUM_TOOLS_ENABLED`（默认 true）由 `reasoning_retrieval.enumeration_wiring_active()` 单点判定（地图注入 / prompt / schema 分支 / allowed_actions / 早退放行五处共用）：关闭即两个动作与来源清单参数一并不提供、地图也不注入、早退恢复，零额外查询，完全回到接入前。
- **清单出处与答案归因**：每个送达的元素/KG 清单条目至多携带一条仍存活的有界 `Citation`（KG 取首个有效 evidence element）；跨库证据必须由服务端在 active notebook 的 participant 集内解析，浏览器只经 active-notebook 代理端点打开原文，绝不直连参考库成员端点。真正进入合成预览的条目使用隔离的 `k5001+` 命名空间和反向映射；只有答案实际绑定的这些锚点才能为清单内容归因，且仅带存活 `source_id`/`element_id` 的绑定键可把确定性枚举行判为 grounded。枚举答案一个锚点都没绑时，不得拿无关 ranked citations 冒充清单来源。
- **问答集合清单默认折叠**：集合枚举结果卡始终显示覆盖率徽章、清单名称与可选的单一来源范围，但条目内容默认收起；用户主动打开后才渲染按来源分组的元素、知识对象或文档清单。打开后继续沿用前 20 条预览与独立的「展开全部已加载内容」控制。部分结果/合成预览披露放在折叠内容内，卡片关闭时不得提前挂载图片条目。
- **命令目录抽取（方案 C）**：面向工具手册的 opt-in 结构化摄取，纯函数层在
`backend/app/services/command_catalog.py`，job/持久化/API 在
`backend/app/services/catalog_job.py`。五条不可省：①**成本闸**——`preview` 零模型调用、
只读有界前缀（元素条数与每条字节都在 SQL 里截），触顶必须回报 `sampled`，绝不为了估算
先把要估的那次扫描做一遍；②**单飞与终态**——`catalog_jobs` 的条件唯一索引覆盖
`queued` **与** `running`（行先写、线程后起，漏 queued 就会在那个窗口排出第二个写同一份
候选的 worker），每条退出路径（含 `BaseException`）都必须落终态，启动兜底同样要收
queued；③**接地校验不可绕，且每一批参数只按它自己那批判**——命令名必须在服务端候选清单里且
逐字出现（整条否决），参数名必须带原始前导短横，`syntax` 必须是原文连续拷贝，`default`
找不到就清空；被拦条目连同原因**一起入表**（`state='rejected'`），那是一次零产出抽取唯一
可解释的证据。同一条命令的参数全写在同一段原文里，所以接地校验分不出「这是不是本批的」：
`validate_entry` 必须同时收下本批的 `param_names`，批外的一律拒（`arg_outside_slice`），
本批问了却没回来的记进账（`arg_not_returned`）。两半都不可省——没有归属，答别批的回复
会连内容寻址缓存一起通过（那个缓存唯一的准入票就是同一个 `validate_entry`），整个 TTL 都
被投毒；没有覆盖账目，`args_keep_ratio` 的分母是「模型愿意答多少」，二十个参数答一个是
100%。分母因此是 `args_seen + args_uncovered`。空指派（无 flag 的命令）表示不设约束而不是
「什么都不许返回」——位置参数正是这类小节记的东西，提示词的无 flag 分支必须**主动问**
（`parameter_names` 是 flag 扫描器，`set_dont_use lib_cells` 因此拿到空指派；那一支命令模型
返回 `args: []` 就等于系统性丢掉整类命令的参数元数据）。这类命令没有可服务的清单，把关
全靠接地：名字仍须逐字在本节原文，覆盖账目恒空（没指派就没有未覆盖），而模型一旦作答，
该节就照常计入 args 轴；④**三轴熔断**——满 10 节后命令名否决率
>20%、args 保留率 <50%（闸是「这一轮问过东西」即 `args_seen + args_uncovered > 0`，不是
`args_seen > 0`，否则真正无 flag 的手册会在第十节被误杀）、或「完全没给出可用回答的分片
占比 >20%」任一成立即判 failed 并给用户可读理由，绝不静默交付近空目录。第三轴不可省：
一个什么都不返回的模型让命令名比率保持无害，而它拖低的 args 比率报的是症状（「参数丢了」）
不是病因（「这个端点在返垃圾」）；多轴同时成立时报最具体的那个（不可用回答解释得了差的
args 比率，反过来不成立）；
⑤**apply 保守合并**——目标表不存在则建（「命令目录：<来源标题>」，命令/语法/参数/说明/
示例/出处，「命令」为行标题列），存在只**新增**表里没有的命令；同名一律不改行、只回报
conflict（分不出「陈旧行」与「人工订正过的行」，覆盖是本特性唯一不可逆的破坏），落库全部
走 knowhow 既有服务层因而自带 record_change。列按**名字**寻址而不是按位置——目标表有实时
增删列端点，位置映射在用户删一列之后仍会「正常工作」，只是把语法写进「说明」；缺「命令」列
直接拒绝写入。一次 `all_pending` 最多确认一页，必须用 `pending_remaining` 如实回报剩余，
不能让 300 条候选的库看到 `rows_added: 100` 就以为做完了。apply/dismiss/陈旧清扫的并发锁
**一律是 `("catalog", notebook_id)`**，判据只有一条:锁键必须**不可变**。按表 id 取锁的那一刻，
「表存不存在」正被这把锁自己保护的 `create_knowhow_table` 改写；按派生标题取锁则会被论文元数据
接地在两次 apply 之间把上传名换成论文标题。按来源取锁同样不行——两个派生标题相同的不同来源
解析到同一张表，而来源写栅栏是 per source、挡不住它们。`applied_table_id` 只用于锁内的目标解析，
绝不当锁键。锁序:来源写栅栏在外、目录锁在内，完整枚举在 `_target_lock_key` 的 docstring 里。
粗粒度的代价（同库不同来源的确认会串行）是刻意接受的:每个写者都有界、不调模型、由人点击触发。⑥**模型自撰字段在合并处
封顶**——说明/示例/每个参数的 desc 都是接地校验刻意不查的，`_merge_entry` 是它们到库之间
唯一的收口；参数 desc 需要两道闸（每条 `MODEL_ARG_DESC_CHARS`、整行 `MODEL_ARG_DESC_TOTAL_CHARS`,
一行的条数等于命令的参数个数），总量截断从尾部截并把条数记进 `reject_info.desc_overflow`
（与统计拦截记录条数的 `overflow` 分开两个键，合在一起两个数都读不出来）。
效率合同=每节调用数=分片数；唯一例外是共用同一套二分机制的两条有界补救（回答不可用、
或覆盖率过低且回答偏短——`SLICE_COVERAGE_RETRY_RATIO` / `MIN_ASSIGNED_FOR_COVERAGE_RETRY`，
只在 depth 0 触发，因而 `MAX_CALLS_PER_SLICE` 不变；回答条数与指派一样多只是答错了的不重问，
问得更少救不了它），单个分片最多 `MAX_CALLS_PER_SLICE` 次，失败分片一律记成 rejected 行。模型通道复用
`kg_extract` workload，不新增配置面。⚠两条 seam 事实决定这条补救怎么触发，都极易搞错：
①`chat_json` 不回传 `finish_reason`（签名被契约测试钉死），而 `_validate_json_object` 对
**空 content 与截断抛同一个** `malformed_response`——所以在这一层「模型没给出可用回答」是
**一个**信号而不是两个，别在文档里写成「空内容重试一次」；②必须按 provider 的**稳定错误码**
分类，绝不能按异常类：scheduled adapter 把一切重抛成 `ModelInvocationError`，它是
`MalformedModelResponse` 的**兄弟类**，`except MalformedModelResponse` 只会匹配测试 double、
在生产上永不命中（整轮会在第一次截断时直接 failed）。瞬态 provider 错误（限流/5xx/鉴权）
显式不走二分，直接冒泡判 failed，绝不能被吞成「这一节本来就没有命令」。
⚠`catalog_candidates.job_id` **刻意不加外键**：加了 `catalog_jobs` 就不是叶表，而
`idx_catalog_jobs_one_active` 是 source_id 单列面（source_id 又是外键列、NOT NULL），
正向 shadow 会整个 PG17 catalog 判为不可停车。
⑦**解析前提与来源代次绑定**——`preview`/`start` 都要求 `parse_status ∈
{parsed, extracting, extracted}`（仓库既有白名单，后两档是解析之后的 KG 抽取阶段），
否则 409（未解析完/解析失败两套文案）；`catalog_jobs.source_generation` 记下创建时刻的
**来源代次**＝`MAX(source_elements.created_at)`（`replace_elements` 把整批新元素写成同一个
`created_at`，所以它当且仅当元素被换过时才变），`apply`/`dismiss` 在**同一把 per-target 锁
内**先比一次，对不上就 409 并用 `expire_pending_candidates` 把该 job 剩余候选**整批**标成
`dismissed`（`source_reparsed`）。三处不可省：代次**不能**用 `sources.updated_at`（那是有意
做粗的变更信号，重新抽取 KG／写摘要都会推进而元素没动，按它判会谎报「已重新解析」并逼用户
重跑一整轮付费识别）；作废**必须**整批（页界的 `mark_candidates_dismissed` 会让 >100 条候选
的 job 半作废且仍被重跑守卫挡着）；`start` 侧遇到**已过期**的旧候选要清掉并放行，否则「待审
候选拦重跑」与本条互相死锁——每次确认因过期被拒、每次重跑因未审被拒。
前端 `CommandCatalogReview` 的每次 apply/dismiss（含失败路径）都必须经 `onReviewed` 回调让
`CommandCatalogSection` 重读一次 `.../job`：弹窗在 page 根层、卡片在来源详情里，两者只剩
page.tsx 这一条接线，不重读则「重新识别」会一直被卡片手里那份旧 `pending_candidates` 挡着。
- **完整枚举免责的抑制规则**：`completeness_unavailable` 的前置警告只在**四条同时成立**时抑制——`result_scope != "aggregate"`、意图合同的 `constraints`/`excluded_topics`/`assumptions` 全为空、至少一张清单卡 `returned_total > 0`、且该卡 `complete=True`。方向是宁可多警告：清单卡的 coverage 只证明「某个物理集合被完整走了一遍」，证明不了它就是用户要的那个子集；这里刻意不做语义匹配（无确定性判据）。
- **完整语义与 prompt 硬预算补充**：确认时按最终编辑措辞与权威澄清答案重算 scope；结构化执行器只处理整表物理行/方法清单、直接物理行/记录计数及其 hybrid。“多少种”等去重/种类计数、条件筛选、group-by 没有确定性计划时回退并披露不支持完整。轻量 catalog 最多返回 8 个表描述，不读取格、代码附件或健康大字段，并在截窗前优先纳入显式点名表。响应分开 per-table、batch、synthesis coverage：枚举 200/200 而模型预览 100/200 时写“枚举完整、分析部分”，8 表截断不污染已耗尽单表。KG 对象/关系、confirmed Memory、查询期链共享 KG 字符硬预算；结构化预览、chunk、direct element 共享原文硬预算；最终证据块不超过两者之和。
- **大纲便签与按节合成**（借鉴 DualGraph，PR-3）：仅 `exhaustive`（穷尽）档且 `REASONING_OUTLINE_ENABLED`（默认 true）开启时，reflect 循环新增 `update_outline` 动作（第 11 个动作 id；章节结构全量替换，同一稳定节 id 的合法证据与旧绑定 union，遗漏不删，`remove_evidence` 才显式撤销；8 键满额时旧键优先、未接纳新键进入下一轮账目）。合法 key = 存活候选池 ∩（run 内候选摘要曾实际展示的 key ∪ 当前大纲已持有 key），既不会因窗口滑动丢旧绑定，也不接受从未展示的中段 id；pending 不冻结普通额度内的结构更新，只有 sufficient/stale 的预算内终态纠错与第 6 次后的单次资格才是同结构纯换键，`max_steps` 绝不因纠错增加，stale 事实先落 trace。终态仍未接纳的 key 写入可见 trace。非法键静默丢弃，空节记入回喂账目供模型定向补检索；大纲修订对 stale 熔断中性（纯整理不算进展，真正的检索动作仍照常清零）；一次畸形提交跳过并保留上一份大纲，绝不清空。循环结束时若按候选池解析后仍有 ≥2 节能装配出证据、且该 run 未产出集合清单/结构化整表枚举（清单 run 保持单次合成——清单预览与覆盖披露只进单次路径的合成上下文，节化会拿 ranked 样本写散文而让完整清单闲置），则按节合成——每节只喂该节绑定证据的上下文切片（集合地图/枚举预览/私有 Memory/查询期推导链不进切片，留在回退路径），号段偏移保证跨节不相交，锚点按各节自己的 id_map 解析后再合并（写出别节号段只可能是幻觉，直接丢弃）；每节用自己的切片与锚点通过 `classify_evidence`，逐节记录落 synthesis detail 的 `section_grounded` 列表（不是整篇 flag），全部有据照旧，否则只把全局结果封顶 `overview`，零节精确 grounded 也不强制改成 `inferred`。任一节合成失败即整体回退单次合成，且回退成功后不留假 model_error 横幅；按节被绕过（不足 2 节或清单 run）时大纲披露不消失——规划跑过就在收尾 synthesis detail 带出 `outline_skipped` 等键。模型写进节标题的引用形 `[k]` 标记在解析入口剥除。trace 新增 `outline` 步类型（前端标签「大纲」）与每节一条的合成进度步（收尾步在引用数后披露略过节/依据不足节）；关闭态/低档位与接入前逐字一致。v1 不进 `AskResponse`。数值上限（节数/层数/每节绑定数/每 run 调用次数）只作为契约写在 `docs/product-and-api.md`/`_zh.md`「大纲便签与按节合成」一节，这里不重复。真源见 `docs/reasoning-enumeration-tools-design.md` §3.1。服务端弱支撑边回喂（PR-4，`REASONING_OUTLINE_KG_GAP_ENABLED` 默认开，叠在上面这把总闸之上）：每次**被接受**的 `update_outline` 之后，按本次新绑定证据的 canonical 邻域有界探测 `canonical_relations`（判据 `source_count` 而非原始关系行数、经主键前缀+`idx_clusters_member` 解析显示名、run 级 `kg_gap_probed_seeds` 防重复探测、异常 fail-open 记 skip 步），把「支撑薄弱」的关系提示追加进大纲便签供模型用既有动作定向补检索；终态纠错轮不渲染也不消费该段。数值上限只在 `docs/product-and-api*.md` 契约段维护，真源见设计文档 §3.3。**大纲采用引导**（设计文档 §3.1.1，真机采用率 0/3 才加的）：闸开着、当前大纲**为空**、本 run 引导额度未用尽，且有一条服务端手上现成的结构性理由（本 run 已把来源清单枚举到 `state == "complete"` 且条数达下限，或已确认检索方向数达同一下限；方向数取 `run()` 现成方向清单长度减一，不重新解析契约）时，在几份账目之后、集合地图之前追加一行确定性引导，措辞含现状+具体条数+「判断不需要就忽略」的出口。两条理由同时成立用清单那条；没列完的清单一律不算（半份目录建出的大纲缺节而模型不自知）。零新增查询/模型调用/动作 id；与便签的互斥判据写在 `_outline_nudge_note` 自己的「sections 非空即返回空串」里，不是调用点的 `else`；只有真发出的那一轮给既有 reflect 步 detail 加 `outline_nudged: true`（无条件写 `False` 会破坏「detail 逐键不变」的冻结基线口径）。数值上限（每 run 引导轮数、触发下限）只在 `docs/product-and-api*.md` 契约段维护。同批：`_answer_with_retry` 的**重试成功不留假报警**——进入时记 `mark = len(sink)`（sink 取 `_ASK_MODEL_ERRORS.get()`，为 `None` 则不摘），重试成功时只丢 `sink[mark:]` 里 `workload_id == "ask_answer"` 的那几条（按身份而非位置过滤——今天 `synth()` 内部不记别的报警，但给证据精炼补一条就会让位置式截断在「首次即成功」时把它一起删掉）；两次都失败一条不摘（含终态 empty-content 的 `RuntimeError`，「检索到却答不出」必须可见），`mark` 之前其它 workload 的报警一律不动，取消路径不受影响，`events.jsonl` 始终记全。按节合成那处的区间删除保持闭上界 `outline_err_mark:fallback_err_mark`，不得改成开区间。深度报告穷尽档的逐节深挖同样启用大纲便签与弱支撑边回喂（PR-5）：报告自己的 depth 五档（1/2/4/8/16）映射到本条同一批检索档位名（阈值判定，中间值落更低档），每节 `max_steps` 仍用报告自己的 depth 值而非档位表的步数上限；深挖整理出的子大纲只折成有界「发现的结构」块，作为 `###` 组织建议附进该节撰写 prompt，绝不回写用户确认过的 `reports.outline_json`。档位同样管到检索之后的两段：按方向补检索的合并结果按该档 `ranked_final_cap`/`answer_element_items` 重新截断（相关度降序，大纲绑定对象豁免），节撰写上下文用 `kg_context_chars` 与**共享**的 `chunk_context_chars`（原文段吃 chunk 剩余额度、条数封顶 `answer_element_items`）而不是 `ANSWER_CONTEXT_BUDGET_CHARS`/`REPORT_SECTION_CHUNK_BUDGET` 定值外加一份 1/3 元素额度；大纲绑定对象的优先额度必须在**一次** `knowledge_context` 调用内完成（`priority_object_ids`/`priority_budget_chars`），拆成两次会丢掉所有跨两半的 `relations:` 边——只在 `run()` 里兑现的档位会被消费它输出的那两段原样送回去。真源见 `docs/reasoning-enumeration-tools-design.md` §3.4。
- **大纲未接纳键**：overflow key 是服务端持久 pending，不是单轮诊断；成功绑定、在 `remove_evidence` 点名放弃或整节删除之前持续回喂。只腾位却漏抄新 key 仍在终态 trace 披露；结构违规只拒绝真正的纠错提交，普通额度轮不会因 pending 被改成 repair-only。完整状态每节恰好以 56 key 为硬顶，prompt 每轮只展示前 8 个与剩余计数，处理后再滚动露出下一批。

### 工程约束

- **效率是一等约束**：新增 LLM / embedding / DB 调用前先问代价——能否合并、缓存、异步、按需 gate。强一致做成 opt-in，默认走低开销路径。
- **大库检索 hydration 必须按候选有界**：ANN 后的孤立节点判断只能对当前候选做带索引的逐候选 `EXISTS`，返回候选 id，不得物化高出度节点的完整邻边；canonical fold 只能经 `cluster_fold_rows` 查询本轮 scored id，不能加载全 notebook cluster map；同一 scale-index 实例同一工件类型的惰性 ANN open 必须单飞。这三项只优化执行成本，不得顺手改 score、阈值、PPR 或召回。
- **引号即用户检索语法，真源是 `backend/app/core/query_syntax.py`**：英文半角双引号内的内容整体匹配、绝不分词。放在 `core` 是因为两端都要用而它们互相 import 不了（`app.repositories` 与 `app.services.retrieval` 经 ports 成环）。三条边界：只认 ASCII `"`（中文排版引号在散文里是普通引用，认它等于把大量既有提问悄悄变成带约束的）、至少 `MIN_LEXICAL_TERM_CHARS` 个字（SQLite trigram 索引更短的根本索引不到，识别了就是一句兑现不了的承诺）、**不同的引号内容超过 `MAX_QUOTED_PHRASES` 段则整段语法不生效**（数不同内容而非出现次数——内部检索问题会把同一段短语在目标/规范化问题/每条必答主题里各留一份，按出现次数数会在推理与报告里把语法整个关掉）——最后这条是形状判据不是预算：knowhow 智能补全的「查询」是 JSON 信封，每个键和格值都是引号跨度，取前 N 个既会凭空造出用户没提的约束，又会把 CJK 格值的三字片段召回路径剥掉。**被拒的跨度必须留在余量里**，遮蔽了又不用等于把那几个词从查询里删掉，精确请求变成召回窟窿。消费方：`lexical_recall_terms` 把短语放在词项表最前（在去掉引号字符的整句项之前），只对余量做分解；`probe_keyword_basis` 让通道按**它自己探测过的名称**打分且每个名称都是原子的（原子性是探测的性质，绝不按「有没有空格」推断——那会把 `config.yaml` 与所有 CJK 短语降级回 token）；`retrieval.KeywordBasis` 把每个短语当**一项**、整段子串命中才算覆盖（比较两段**已存文本**的调用方——如治理合并相似度——传 `honor_quotes=False`，文档引用别人的话不等于声明检索约束）；Memory 的候选是「整串当一个短语」探测,每段被接受的短语还要经 `phrase_queries` 作为额外 OR 词项进同一条有界查询(否则「只含该短语、不含整句」的记忆永远进不了候选池);`exact_probe_terms` 无条件放行短语（那把闸拦的是**顺带出现**的名称，用户的引号从不是顺带的），但模型给的 `exact_term` 走 `honor_quotes=False` 留在窄闸上；四处规划/反思 prompt 追加一句「原样保留引号内容」，且与 `SCOPE_DEIXIS_GROUNDING` 不同是**按需注入**——无引号的问题一个字都不该多付，深度报告是每节每步付一次。调用方已选定的名称经 `exact_probe_query` 逐个加引号交给精确通道：通道会对收到的串**重新**抽名称，裸空格拼接会把多词短语丢掉。无引号查询在以上每一处都必须逐位不变。`frontend/app/query-syntax.ts` 镜像这份解析，让提问框在提交前回执识别结果（没有它，不被识别就是一次静默失败），两侧由同一份用例表钉住（`test_query_syntax.py` 的 PARITY_CASES / `query-syntax.test.mjs`）。
- **词法候选属于索引检索契约**：SQLite FTS5 对每个用户派生 clause 单独按数据转义，可保留整句精确匹配加分，但必须 OR 拉丁字母/数字词项与重叠中文三字片段，禁止把多词查询只包装成一个强制连续短语；`_`/`-`/`.` 连接的完整标识符（`set_db` 这类）额外作为整体词项召回（trigram phrase＝精确子串），受「须含 ASCII 字母、长度 ≥4、至多 16 个、不超整句上限」约束，溢出共享词项预算时给 CJK 词项保有界尾部配额，不含标识符的查询词项集合逐位不变；PostgreSQL 在原生 trigram 候选生成前拆分同一组有界词项，并对 `ILIKE` 分支转义 LIKE 元字符（`%`/`_`/`\`），使 `set_db` 这类词项保持字面量、不退化成通配把 `setXdb`/`set db` 拉进候选；trigram 运算符与 `similarity()` 仍用未转义原词，转义只属于 LIKE 分支。**最终被探测的词项集合还要过一道语料语言闸**（`LEXICAL_LANGUAGE_GATE_ENABLED` 默认 true）：采样探针 `_notebook_langs` 判定该库没有任何 CJK 字符时，丢掉「每个字符都是 CJK」的词项——这类词项加空格补边后每个 trigram 都含 CJK 字符，对该库保证零命中，却在 PG 上各买一次真实 LATERAL 探针（7,026 块英文库实测：64 词项冷 29.7s / 3 词项暖 0.26s，返回同样 26 行；报告 4 节并发即撞 `POSTGRES_STATEMENT_TIMEOUT_SECONDS`，整条词法臂 fail-open 阵亡，代价不是慢而是**本该命中的拉丁词项一起没了**）。闸放在**消费侧**：由知道 `notebook_id` 的调用方把 `corpus_langs` 传进 `fts_search`/`chunk_fts_search`，且**永远传适配器即将查询的那个 notebook 的语言**（挂载参考库不能拿 active 库的语言过滤），绝不改写 `lexical_recall_terms` 自身的输出。三条不对称是硬性的：①只做 CJK 方向（拉丁标识符是中文文档里的常规内容，且空库探针本来就答 `["en"]`，镜像规则等于按猜测过滤）；②只过滤优先头**之后**的词项，用户引号短语与整句词项永不参与（引号契约红线，闸不能变成第二次更安静的截断）；③只丢「全 CJK」词项，不丢「含 CJK」词项（`abc时序` 与拉丁语料共享 `abc` trigram，无零命中证明）。两个后端必须过滤出同一组词项，否则同一个库的召回口径会随适配器分叉。语言判据是**采样**探针：唯一 Chinese 来源落在未采样的中段时会丢该库的 CJK **词法**召回（ANN/语义不受影响，且同一探针本来就决定要不要生成中文关键词），设 false 完全回退。**选定来源（`allowed_source_ids`/`source_filter` 非空）的运行一律不过滤**——那条路刻意跳过全库 ANN，词法是它**唯一**的候选来源，采样误判就不是「少一份召回」而是**整次检索零证据**；而且来源谓词在 LATERAL 内、LIMIT 之前生效，scoped 探针本来只扫选定来源那几行，这道闸要防的全库扫描在那条路上根本不会发生（风险最高、收益最低）。凡是能带来源范围的调用点必须**显式**写 `source_scoped=`，由守卫按形态钉住。带索引的 Chunk/KG 检索合并有界 ANN 与词法窗口；带索引的 Relation 检索通过 source/target 索引补入与词法命中 KG 端点相邻的有界有效关系，并保留 FTS 端点顺序、为两个关系方向预留预算，禁止高出度 source 饿死 target-only 命中。候选 id 与语义分 map 必须分开，纯词法候选按 keyword-only 计分，不得写入伪造的零语义分。
- **KG 边契约、补全与来源追踪**：内置 12 种边及核心四类端点只认 `app/services/kg/edge_schema.py`；prompt、抽取验证、trust、graph/PPR/canonical/relation hydration、`follow_chain` 和工件版本都从它派生，非法 core→core 历史边只留审计、不进查询。已知边触及管理员扩展类型时保持兼容。`scripts/audit_kg_edge_contract.py` 只读且不得输出证据正文。跨元素关系补全默认 `off`；`shadow/write` 还必须命中 notebook allowlist 或稳定灰度，按模式和来源代次的持久 keyset 水位逐页处理。每个任务只 hydrate 有界对象/证据 ID 窗口，仅做索引化 relation `EXISTS` 与有界同源 FTS/ANN overfetch，受 section/pair/batch/字符上限约束；未完水位重新入队且启动恢复当前 pending 代次，模式切换用同一个 generation-CAS 事务先发布新模式可恢复游标再把旧 pending 游标标成 `stale`，无索引时保留水位并 fail closed。模型只能选服务端 id，独立 verifier 后在短事务复核 source/run/object/element 并保存 verifier 看到的同一 excerpt，稳定 relation id 保证重试幂等；非法零值护栏不推进，且绝不全扫整本书或整库。检索候选来源必须按 producer 累积，不能从最终 score 反推；`CHUNK_GRAPH_RESERVE` 只为已过原门槛的 graph-only chunk 预留既有预算席位，不扩 token/item 上限；新增保底席位必须走同一个 `select_with_reserves` rule list，不得另写并列 selector——各 reserve 共享一份预算、reserve=0 必须完全惰性、任何 reserve 不得逐出其他 reserve 刚保住的 chunk。
- **精确标识符通道（exact_lookup）**：问题里出现**可精确查找的名称**（`exact_probe_terms` 非空）才启动（`EXACT_LOOKUP_ENABLED` 默认开）：精确子串定位命中章节 → 取齐该节+子节 → `EXACT_SECTION_RESERVE` 在 mix 选择时给这些 chunk 预算内保底。**闸比词法召回窄**：`exact_probe_terms` 是 `identifier_terms` 的过滤视图，带 `_` 或 `.` 的一律放行，只用连字符连接的必须含数字（`GPT-4`/`v1-2` 留，`state-of-the-art`/`real-time`/`end-to-end`/`high-level` 拦）；**词法召回侧的 `identifier_terms` 保持宽定义不动**（多一个 OR 词项无害，多一次探测不是）。窄闸的理由是实测：这批词几乎出现在每个分析型问题里，报告引擎每节的 sec_question 恒抽出一个＝每节白发一次探测（2 万块库 16ms/50 命中），`real-time` 命中章节标题时整章 12 块以 1.0 分进证据。这把闸就是成本合同——无此类名称的问题零新增查询；每步探测受 `EXACT_LOOKUP_*` 硬界约束，SQLite/PG 两侧 LIKE/ILIKE 必须转义 `%`/`_`/`\`（命令名天然含 `_`）。**命中先按「最近的 identifier-named 面包屑前缀」折叠成组再分 slot**：按面包屑各自排名会让一条命令的子节吃光名额（实测 `set_db > Arguments` 4 命中 / `> Examples` 3 / 主节 2 / `report_timing` 2，三个 slot 下 `report_timing` 整条丢失），而「子节块数多于主节」是参考手册的常态。组键 identifier-named → 对组键做子树取齐；**否则按命中 chunk id 直接 hydrate，绝不按 `(source, section_path)`+LIMIT 重查**——面包屑只有 markdown 解析路径才有，MinerU 解析的 PDF/DOCX 下全库几十个裸「Arguments」节共享同一 section 键，重查只拿回前 12 个、把探测已经找到的命中块丢掉。章节取齐的兄弟 chunk 刻意不套 `RELEVANCE_FLOOR`（准入是结构性的且有 sections×chunks 硬界，分数保持诚实 [0,1]，套地板会把特性要救的低分参数表原样丢掉）；**通道分是「对本次实际探测名称的覆盖率」，不是问题相关度**（按调用方 query 打分会让 mix 报 0.2857、reasoning 报 1.0，同一块证据两个口径）。通道只作用于当前 active notebook，挂载 base 联邦刻意未做。**逐步推理侧接两处，覆盖 `reasoning` 问答与深度报告逐节检索（报告引擎逐字复用 `ReasoningRetriever`），graph 模式不接**：①初检索之后无条件跑一次确定性 seed pass（镜像 PPR seed pass，不赌 agent 选动作），按抽出的名称本身（而非整句问题）打分——与 action 同构、也是 producer-native 打分的既有惯例，整句打分会把命中节的相关度拖到锚点阈值以下——记 `exact_lookup` 轨迹步（前端标签「精查」），排在 PPR seed **之后**以保 PPR 计数逐位不变；②reflect 白名单加 `exact_lookup`（参数 `exact_term`）。动作与 seed 共用 `exact_probe_terms` 这把闸（低选择度短串、纯连字符英文词组都直接 skip——那是按实测定标的成本闸，不能交给模型）、共用按名称的防重账目（seed 查过的名称 agent 不必再查）、只探测本轮新名称，agent 主动调用每 run ≤ `_MAX_EXACT_LOOKUPS = 3`（seed 不占额度）。**四类 skip（未启用/缺名称/非标识符/超上限）都写入同一份账目并回喂教学措辞**（措辞写成「该给什么」而非「你给错了」：「「2.1」不是可精确查找的名称(要像 set_db、config.yaml 这样带下划线或点;只用连字符连接的词还需带数字,如 GPT-4)」——只说非法，模型下一轮往往换一个同样非法的普通词再试），按理由/名称去重，不是只留 TraceStep 沉默；`exact_term` 解析期只去包裹标点不截长，截长挪到使用点，好让 fail_closed 的 2000 字符硬闸对它真正生效。两条路径都必须过 `_filter_candidates`，且受 `allow_exact_lookup` 策略位（镜像 `allow_ppr`）约束——knowhow 智能补全关闭它，否则策略边界会被开后门。动作名与 prompt **不随 flag 改写**（沿用 `ppr_retrieve` 先例）：flag 或策略位关时在执行处 skip、零 I/O，不从 prompt/白名单剔除。
- **大库冷加载前置 readiness**：默认 `STARTUP_PRELOAD_SCALE_INDEXES=true`，服务必须在 `/api/ready` 前加载全部已发布 scale 索引、启用的 ANN handle 与可安全复用的单索引 PPR core；加载失败或索引数超过 `SCALE_IDX_CACHE_MAX` 必须 not-ready，不能把工件冷成本留给首位用户。单一 self-index 组合图复用原 node list/map/idf/PPR core，且 core 随专用 ScaleIndex LRU 常驻；不得在启动时全量物化跨库 mounted 组合图（现有拼接会成倍复制千万节点图并 OOM）。严格保证只覆盖启动时已经发布的工件集合；运行中 build/fold 或新增第 `SCALE_IDX_CACHE_MAX+1` 个索引仍走既有在线路径，稳定部署应提前扩容并重启以重新建立 readiness 保证。关闭 preload 只用于修复坏索引的临时恢复。
- **生产启动生命周期**：`npm run start` 默认先把 `backend/requirements.txt` 安装到 `PYTHON_BIN` 环境，并以 `npm ci` 按 lockfile 重建前端依赖；只有明确预装依赖的部署才用 `SKIP_INSTALL=1`。安装前以监听行判定端口占用，不得因 `ss` 看不到 PID 就当端口空闲。随后在前台 build，再用脱离 terminal 的后台进程拉起前后端；只有在 curl 连续两次确认后端 `ready=true` 和前端可访问、且本次两个 PID 仍存活后才成功退出。成功前的进程退出、超时或中断必须同时 SIGTERM 两个直接子进程，有界等待后 SIGKILL 残留并 `wait` 回收；正常停服只走 `npm run stop`。
- **检索索引立即构建覆盖排队**：手动 `when=now` 只有在原子认领到新的立即构建时才移除同 notebook 的旧 idle 项；已有构建保留后续排队，认领后的新 idle 项也保留，daemon 启动失败则恢复被覆盖项。idle scheduler 必须逐项认领，禁止先清空整批：忙碌项继续排队，单项启动失败不得丢失或阻断其余项。`AskResponse.index_required` 只是回答时快照，前端必须结合实时 `ScaleIndexStatus.exists`；有界前台轮询结束后仍由 `index_done` 刷新当前 notebook，索引发布后立即隐藏历史降级提示。
- **LLM 响应缓存是 opt-in**：`chat_json` 的内容寻址缓存默认开，但**只有传 `response_validator` 的调用方才读写它**——不传就既不读也不写（对调用方透明、正确性保留、只失去性能）。占成本大头的 KG 抽取三处传 validator 保持缓存；Ask、paper_meta、summary 等不传的调用方刻意不缓存，一次关掉「偶发坏值被固化整个 TTL」的投毒类。健康探针走 `bypass_cache`；admin 清缓存 `tag` 与 `clear_all` 二选一（同时传即 400，绝不静默全清）。UI 上传按内容哈希做**同 notebook 内**去重（对齐 `batch_ingest`）；同内容不同后缀重传复用既有源、保留原解析（要换解析器请删除该源再重传）。本特性追加迁移 v30（`sources(notebook_id, file_hash)` 去重索引）。
- **构建任务必须落终态**：KG 构建的每条退出路径都要把 `kg_build_jobs` 那行写成终态，**包括 Ctrl-C/SIGTERM**——它们继承 `BaseException`，`except Exception` 接不到。留在 `running` 会被 notebook 级条件唯一索引把后续每次构建挡成 `KgBuildAlreadyRunning`，界面还一直显示「分析中」；启动期崩溃兜底只属于服务端 lifespan，离线 CLI 刻意无权自清，只能等后端重启。中断要 abort run control 停掉在飞窗口并**先排空再落终态**（守卫是跨进程的，随那行一起释放：不排空，活着的后端可以起新分析，而垂死进程的 worker 还在写图，会把新任务状态冲掉；顺带线程池 atexit join 也会把进程按住不退）。所有 `kg` 抽取形态统一接管 SIGINT/SIGTERM/SIGHUP：第一次信号启动收尾，重复信号在原 handler 还原前被吸收，不能再次打断 extraction drain 或 finalizer executor shutdown。失败事件只在 `finish()` 真的把 `running` 落成 `failed` 时才发（信号可能落在成功提交之后）。但**不得**记 `kg_build_circuit_opened`——那是模型熔断，人工中断不是。真源见 `AGENTS.md`「Architecture Baseline」。
- 不引入 Docker 作为一期默认工作流；装新包前先问。
- **浮动弹窗**：新增的居中浮动弹窗要可拖标题栏移动——复用 `frontend/app/use-floating-window.ts`（`page.tsx` 内联弹窗走 `FloatingModalCard` 包装：只接管卡片、把 `dragHandleProps` 交给标题栏），不要另造一套拖动实现；侧边贴边抽屉、锚定 popover、全屏视图除外，窄屏（<720px）自动停用。真源见 `AGENTS.md` 前端章节。
- **长任务按钮的忙碌态**：点一下就触发长任务的按钮（排后台 job、同步等完的重活、大体积上传），点完必须**立刻**不可点并换成按该动作语义写的进行态文案（「补齐向量」→「补齐中…」，不是笼统的「处理中」）；纯图标按钮改用 `.busy-spin` 转圈并同步 `title`/`aria-label`。理由是硬的：`backfill-vectors`/`reparse` 后端**没有单飞守卫**，POST 立即返回而活在后台跑，按钮不变化用户就反复点、每点一次就再排一份同样的活。两种形态二选一：`disabled={busy}` + 文案切换，或忙碌时整排 CTA 换「取消」/不渲染；不能什么都不做。解除忙碌位要**按证据**，且证据按**修复形状**选、不能一刀切（真源=`frontend/app/checkup-view.ts` 的 `repairRelease`/`isRepairing`，有单测）：逐轮修一批的（`reparse`）存触发时的 count，count 一变就恢复可点；一次修全库的（`backfill_vectors`）⚠**不能**用 count——H4/H5 口径排除活跃租约、job 跑的过程中 count 就递减，按它解锁会排出并发全库补齐（正是后端 `checkup.py` H4/H5 注释点名要防的重复模型调用），只在该组从体检结果消失时解除。忙碌位还要随体检结果收敛（摘掉已消失的分组键），否则旧条目会被重新出现的同名问题继承、锁死新卡片。有界轮询窗口关闭与切库是共用兜底。⚠ `.sort-button`/`.icon-button`/`.ghost-button` 写死了 background/color，浏览器默认 `:disabled` 变灰对它们无效——`globals.css` 那条共用 `:disabled { opacity: .55 }` 是让禁用看得见的必要条件。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/long-task-button-guard.test.mjs`（按 `onClick` 源码文本认入口、不含行号；`disabled={false}` 这类恒假值同样报红）。
- **添加来源弹窗**：待上传文件名超过 48 个 Unicode 字符时中间压缩，保留末尾/扩展名并用 `title` 暴露全名；CSS 仍需在窄窗口省略保护。确认上传前必须把整个暂存批次计入当前笔记本的有效文档上限，超过剩余名额时按钮直接置灰，并在按钮附近写明还能上传几个、需移除几个；防御性 handler 也要复查。单文件大小只认部署变量 `SOURCE_UPLOAD_MAX_MB`（整数 1–1024，默认 50，1 MB = 1024 × 1024 字节）：Settings 派生字节上限，经认证的轻量系统配置接口下发给选择器即时拒绝，上传端仍以携带当前上限的流式 413 作权威兜底。前后端还必须固定限制每次 multipart 最多 20 个文件，后端在解析流时执行，避免单文件额度累积成无界 spool；类型元数据只接受预期的两个字段，最多 40 个标量 part、每项最多 4 KiB，未知或超额字段直接拒绝。最后一排操作按钮与卡片底边保持明确留白。
- **五档强度控件**：深度报告的「研究深度」与逐步推理的「检索档位」档名相同，必须复用 `frontend/app/effort-picker.tsx` 的 `EffortPicker`（chip + 滑块 popover + 该档一句说明），不得再造平铺 chip；档位的**数值阈值不上屏**，精确上限只在 `docs/product-and-api*.md` 的契约表。popover 必须打开时量一次再夹回最近的裁剪祖先（静态锚点做不到：`right:0` 与 `left:0` 各只在一段宽度成立），夹取算术在 `effort-picker-logic.ts`。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/effort-picker.test.mjs`（含「滑块只许存在于共享控件」的移动变异守卫）。报告的「研究深度」现与逐步推理的「检索档位」共享同一张预算表（PR-5，见上面「大纲便签与按节合成」条），控件复用不变，只是背后的数值贯通了。
- **异常提示分级**：来源异常小字（解析失败/抽取缺口/待补全等）分三档（integrity=红/retrieval=黄/info 中性），唯一渲染路径是 `AnomalyBadge` + `sourceAnomalies()` + `--danger`/`--warning`/`--warning-solid` token；新增异常小字必须走它，不得手搓内联样式或裸 `⚠`。真源见 `AGENTS.md` 前端章节，回归门是 `frontend/app/anomaly-guard.test.mjs`。
- **加了守卫 ≠ 有效**：必须做**变异验证**——把代码改回违规形态，确认它真的报红。只做「删除」变异不够，还要做「移动」变异。变异本身极易打空（替了字面量但代码用的是常量、按行号插入而行号已漂），先 `grep -c` 确认改到了再跑。
- 收尾提 PR，不直接合 `master`。分支先 rebase 到基分支保持线性，再 push、`gh pr create --base <基分支>`（通常是 `master`，stacked PR 不是，见第三节）。提 PR 与合入都必须经过 codex 评审，见第三节。

### 刻意不遵从 `AGENTS.md` 的几处

本文件其余部分都服从「冲突时以 `AGENTS.md` 为准」。以下是**穷举**的例外，各有理由：

1. **合入方式**：`AGENTS.md`「Feature Completion」第 1 步写的是把最新 `master` **3-way merge 进特性分支**，与本仓库实际使用的 Rebase and merge 冲突——把 `master` 合进来会破坏分支线性，GitHub 会报 `cannot be rebased`。以 **rebase** 为准，并提醒用户订正 `AGENTS.md`。
2. **worktree 里的 `npm install`**：`AGENTS.md`「Frontend/UI」写「缺 `frontend/node_modules` 就先跑 `npm install`」，在软链共享安装树的 worktree 里照做会写穿主 checkout。以上面「先 `ls -l` 再决定」为准。
3. **编辑方式**：`AGENTS.md`「Git And File Safety」写 `apply_patch`，那是 codex 语境；Claude Code 的等价物是 Edit/Write。这条是载体差异不是规则分歧。

---

## 二、子代理规范

### 1. 起子代理必须显式选模型

**不得默认继承主 agent 的模型。** 每次 `Agent` 调用要么显式传 `model`，要么用 `.claude/agents/` 里已钉好模型的角色。

`.claude/hooks/require-subagent-model.py` 是这条的 PreToolUse 硬门：没显式选模型且角色未钉模型的调用会被拦下。唯一的豁免是 `subagent_type: "fork"`——fork 语义上必须继承父模型，传 `model` 本就无效。省略 `subagent_type` **不算** fork，它等于默认 general-purpose，照样要选模型。

**判据是任务需要多少判断力，不是任务有多大。**

| 模型 | 适用 | 典型任务 |
| --- | --- | --- |
| `opus` | 需要判断力，要能推翻既有结论 | 写实现计划、规格/代码评审、架构取舍、疑难 bug 归因、安全审查、跨文件因果推理 |
| `sonnet` | 规格已定死的转录型工作 | 计划已写明改哪些文件怎么改、机械重构、补测试、文档同步、照既定模式扩展 |
| `haiku` | 纯检索定位清点 | 找文件、列符号、grep 汇总、清点调用点——只需汇报不需推理 |

内置角色（`Explore`、`general-purpose`、`Plan` 等）同样要传 `model`：`Explore` 做的是检索，一般 `haiku` 或 `sonnet` 就够。

**拿不准就上 `opus`**：返工一次的成本远高于模型差价。

**返工时先查计划，别默认怪模型。** 实测归因（PR#288）：`sonnet` 实现者反而抓到了计划里的 bug，问题出在计划不在执行。所以升级路径是——子代理报告「计划有歧义」或返工 ≥1 次时，**回去用 `opus` 重写计划**，而不是把同一份烂计划换 `opus` 再跑一遍。

### 2. 仓库角色（模型已钉在定义里）

| `subagent_type` | 模型 | 用途 |
| --- | --- | --- |
| `impl-task` | sonnet | 执行单个规格已定死的实现任务 |
| `spec-review` | opus | 任务级规格符合性评审 |
| `code-quality-review` | opus | 任务级代码质量与回归风险评审 |

实现任务本身需要设计取舍时，不要用 `impl-task`，改用通用子代理并显式传 `model: opus`。

⚠️ 可用的 `subagent_type` 列表在**会话启动时枚举**：刚新增或改名的角色定义，要重启会话才会出现，否则调用会报 `Agent type not found`。这是**响亮失败**不是静默降级——未知的 `subagent_type` 一律直接报错，不会悄悄落回默认角色。撞上时的兜底是用内置角色并显式传 `model`，判据不变。

同理，`subagent_type` 是**精确匹配**，不做大小写与分隔符的宽松归一：`Impl Task` 不会解析成 `impl-task`，只会报错。

### 3. 何时**不**起子代理

子代理会重建上下文、重新探索、写报告，然后你还要再读一遍报告。收益不明显就自己做：

- 几个文件的读改、简单搜索、小范围验证 —— 自己做更快更准。
- 一个不大的任务不要拆给多个并行子代理。
- 能一个子代理做完的，就不要用多个。

### 4. 本仓库刻意覆盖的通用默认：评审外包

Claude Code 的通用默认是「评审和验证留在主 agent 循环里，不要外包给子代理」。**本仓库刻意相反**：

> 已批准的多步实现计划按「子代理逐任务」执行——每个任务用一个全新的实现子代理，完成后跑任务级的规格评审与代码质量评审，再推进下一个任务。

理由是实测的（PR#288）：opus 的价值主要兑现在评审环节——变异验证、CSS 推演、推翻错误诊断都出自评审子代理。这是用户的明确决定，别按通用默认改回去。

例外：纯研究、设计、状态查询、只读评审类工作**不需要** worktree，也不需要子代理。

### 5. 子代理简报必备字段

一次把上下文喂足，避免「起 → 等 → 补简报」的往返：

1. **目标与验收标准** —— 怎样算完成。
2. **相关文件的绝对路径** —— 别让它自己猜。
3. **适用的红线** —— 从上面第一节挑相关的贴过去；子代理看不到你的对话历史。
4. **输出格式** —— 改了什么 / 没改什么 / 跑了什么验证命令与真实结果 / 阻塞与存疑。

多个互相独立的子代理放在**同一条消息**里并发发起。默认并发 ≤ 4；超过 20 个必须用户明确要求。

### 6. 委托后不返工

派出去了就认结果：不要自己再重做一遍它的工作，也不要在它汇报后重新推导它的结论。有疑问就针对性复核具体某一条，而不是整体重来。

---

## 三、提 PR 与合入：codex 评审闭环

**提 PR 之后、合入之前必须有 codex 评审，每一轮的原始输出逐字贴回 PR。**

执行由本机全局的 PostToolUse hook `~/.claude/hooks/codex-pr-review.sh` 承担。**它不是仓库产物**，`git grep` 在本仓库里找不到它；换机器或新 clone 上没有它时规则依然成立，那就手动跑。

### 自动触发只有两个点

1. `gh pr create` 成功 → 第 1 轮评审。
2. `git push` → **仅当该 PR 状态为 `awaiting_fix`（即上一轮判了 P0/P1）时**才重审。这是刻意设计：无关推送不烧额度。

**推论，很容易踩：上一轮是 🟡 或 🟢 之后再 push 修复，不会自动重审。** 这时必须自己补跑并补贴——hook 只代贴它自己跑的那几轮。手动命令与 hook 内部一致：

```bash
codex exec review --base <base> --ephemeral \
  -c 'model_reasoning_effort=medium' -c 'notify=[]' -o <out>
```

`review --base` 与自定义 PROMPT 位置参数互斥（报 `cannot be used with '[PROMPT]'`），所以不传自定义指令，正文是 codex 原生英文输出。对 codex 评审行为的约定只能经 `AGENTS.md` 传达（codex 每轮自动加载它）：已写明评审场景**勿重跑 `check.sh`/前端构建**（评审沙箱写不了 `frontend/node_modules`，必然 EPERM；完整门由提交方本地跑并在 PR 附结果）——该规则仅限评审场景，不放松实现场景的硬门。

### 判成败要双判据

**退出码 0 且输出非空。** codex 被 SIGTERM 杀掉时退出码也是 0，只看退出码会贴出一条空评论、假装评审通过。

### 每轮都贴原文

包括零意见的轮次，也包括手动补跑的轮次。评论里带上：触发方式（自动 / 手动）、完整命令、head SHA、退出码与输出字节数。**贴原始输出，不贴我的转述**——用户要能自己核对我确实跑了、且结论没被我复述失真。

### 分级与闸门

| 判定 | 含义 | 动作 |
| --- | --- | --- |
| 🔴 P0/P1 | 阻塞 | 状态自动置 `awaiting_fix`；停下来问人。改完 push 会自动触发下一轮 |
| 🟡 P2/P3 | 非阻塞 | 可以如实说明后不改 |
| ⚪️ 解析不出标签但正文很长 | 格式可能变了 | 保守拦人，绝不因解析失败默认放行 |
| 🟢 无输出标签且正文很短 | 通过 | 问人是否合入 |

自动评审上限 5 轮（`CODEX_REVIEW_MAX_ROUNDS`）。人决定放弃修复时，由我显式落状态：

```bash
~/.claude/hooks/codex-pr-review.sh set-state <PR号> <waived|awaiting_fix|passed>
~/.claude/hooks/codex-pr-review.sh show-state <PR号>
```

### 意见不是照单全收

codex 的评审对象是 diff，它未必了解本 harness 的运行时事实。核实后可以驳回，但必须三件事齐全：在 PR 上写明驳回理由与证据、在代码里留下这条取舍的注释、加反向护栏用例钉住它。（PR#322 驳回过「豁免省略 `subagent_type` 的隐式 fork」，照做会把守卫整个掏空。）

### 空 diff 是硬失败

`base..HEAD` 算成空 diff 时 hook 会硬失败，而不是跑出一句「未发现问题」——假绿比不跑更糟。多半是当前目录不是 PR 改动所在的 worktree，`cd` 到正确目录再跑。

### 合入

- **必须先拿到用户明确同意**，绝不自作主张合。
- 本仓库用 **Rebase and merge**：`gh pr merge <PR号> --rebase`（也有 squash 合入的历史，标题带 `(#NNN)` 后缀的即是）。
- base 不一定是 `master`——stacked PR 的 base 是它的基分支，别硬写 `master`。
- 判断分支是否已进 `master`，**只认 `gh pr view --json state` 为 `MERGED`**。别用 SHA 祖先判断（rebase 会改 SHA），也别只信 `git cherry`（squash 合并会把提交全报成未合）。

---

## 四、深度报告与逐步推理准确性契约

深度报告创建后先做完全不读取语料的问题理解，并始终停在 `intent_ready` 等 owner 确认；模糊问题必须回答阻断性歧义，清晰问题也要确认可编辑的最终研究问题，`auto_generate` 不可绕过此门。确认端点必须以数据库原子转换认领 `intent_ready → planning`，确定性保留用户已经看过的全部合同字段，不得在确认后用第二次 LLM 输出静默替换；确认后的问题/答案才是后续检索、规划和生成的权威输入，补充答案可进入内部检索串但不得污染报告可见标题。此后才侦察语料；语料只能影响检索措辞、证据覆盖和章节排序，不能替换或收窄用户明确要求的主题。大纲中每个必答主题都要有可见绑定，最后一个绑定在 UI 与 API 两层都不可删除，确认后的检索方向必须实际执行。`SourceElement` 在 reasoning Ask 与报告中都是可 `[k]` 引用的一等证据，报告点击引用要能看到绑定的来源、位置和原文片段；小库可直接评分 element，大库禁止全表加载元素文本/向量，必须从有界 chunk ANN/FTS 候选的 `element_ids` 做有界 PK hydration。若来源已被接地判定为论文且解析出非空 `paper_title`，Ask 与报告引用优先显示论文标题，否则保留普通来源名/文件名；引用响应另以 `source_file_name` 保留 `sources.file_name`，当它与显示标题不同时，Ask/报告引用卡显示「原始文件」，公共参考库证据同样适用。该值只能来自持久化上传行，绝不能取 MinerU 临时/输出 Markdown 名。标题规则只有一份实现 `app/services/source_display.py::source_display_title`，所有为用户命名来源的路径（引用标题、检索证据卡、清单卡）一律 import 它——它们命名的是同一批来源，写第二份的后果是同一篇论文在同一屏里有两个名字；守卫是 `backend/tests/test_source_display_title.py`。章节的 grounded 状态由真实锚点与证据相关度重算，不能相信模型自报，也不能把同源的不同 chunk/element 折叠成一个引用。最终编辑器只可摘要并报告遗漏/冲突，不可改写正文或新增事实。报告复用共享 intent 解析并持久化 `result_scope`/`completeness_required`；完整枚举未接入前，`complete`/`aggregate`/`hybrid` 必须如实披露仍是相关性检索，假设只限定范围、不得充当证据或进入检索词。每份报告以有界「资料基础」和分组参考文献披露保守可区分资料、身份不确定性、集中度和重复膨胀，精确锚点不折叠；「引证覆盖率」仅是高风险断言的引证覆盖率，不是语义蕴含评分。可选、用户确认的正交比较框架仅在详尽/穷尽档触发「并行检索 → 一次全局证据蓝图 → 并行分节撰写」：正文按主张/条件综合而非逐篇拼贴，终审拿到公平摘录、账本与冲突只做审计。任何新增结构畸形都 fail-open；全局综合是唯一新增模型调用（每报告 ≤1，仅 depth ≥8）。数值上限只在 `docs/product-and-api*.md` 登记。完整约束见 `AGENTS.md` 的 Product Flow。

逐步推理的正式界面也必须先调用 `/ask/intent` 做不读取 notebook / 参考库语料的问题理解；该预检只可使用当前会话最近的用户问题，不可把语料派生的助手回答回灌为意图依据，也不得创建 conversation 或 Ask job。意图清晰时自动确认，但因没有人工审阅，原始用户问题必须保留为第一条权威检索种子，模型规范化问题只能补充；关键歧义暂停并由用户补充后，审阅过的最终问题才成为权威。必答主题/检索方向、实体、比较轴、约束、排除项、前提、期望输出和答案确定性冻结，并统一用于 Memory、PPR、首轮子查询、证据检索与答案合成；原始输入仍作为可见会话问题。首轮先执行完整权威问题，再按主题轮询确认方向，确保每个必答主题先得到一个种子：档位的「首轮子查询」上限只约束首轮并发，装不下的已确认方向进入待覆盖账目，在确定性 seed pass 之后、reflect 循环之前按合同顺序补种，与 reflect 共用同一份 `max_steps`（补种最多用掉一半，reflect 始终留有可用份额，披露才到得了模型）；per-run 注册表把每个已确认方向映射到唯一简称(默认截断撞出同一简称就加宽、仍撞则加确定性序号后缀)并可反查回方向原文;方向仍未覆盖时每轮回喂 reflect,模型用简称提交 `add_subquery` 会经注册表解析——命中未执行方向按方向自己的完整原文执行且账目记在方向身份上，命中已执行方向（补种或更早的 add_subquery）则判重复、不重跑。reflect 循环结束时，才按这份终态把仍未覆盖的方向记成一条 `skip` 轨迹步（不是预算刚耗尽那一刻的旧账，全部被补上则不落这一步），绝不静默丢弃。第二个规划模型不得替换，后续反思只可按证据补充查询。无效确认必须在创建持久 job 前返回 422，预检取消要把取消事件传到意图模型调用。

逐步推理的来源限定是已确认的检索合同，不是 prompt 软提示。用户明确点名 manual/文件时，必须在 `/ask/intent` 的语料盲预检里用有界、已授权的来源身份目录解析，并在读取任何证据前展示来源标题/原始文件名让用户确认；同名、找不到、目录被截断、来源被删、挂载或权限漂移都 fail closed。selected 预检携带绑定 notebook 与已审阅合同的短时、进程内、防篡改能力；`/ask` 与 `/ask/stream` 必须要求精确的 `source_scope_confirmation="确认"`，在创建持久 conversation/job 或 stream 前校验，并在 AskService 内再次校验，禁止直接客户端通过提交 id 建立权限。每个 run 从一开始就是 `all` 或 `selected`：`all` run 不允许在后续 `search_evidence` 动作里首次动态锁定；`selected` run 的动作省略 `source_refs` 表示继承当前上限，显式提交也只能保持或收窄，不能扩大。来源过滤要下推到有界候选生成，并在证据裁剪、合成、anchors 与 citations 再校验；无法证明可安全按来源隔离的 PPR/图扩展/精查等通道在受限 run 中跳过并写可见原因，不得先跑全库再只过滤输出。展示安全的来源快照持久化进 `AskResponse.intent`，回放时不显示内部 id。

**轨迹必须覆盖整轮，不只是检索段。** 问题理解跑在持久 job 之前，前端自行合成理解阶段的前几步（理解中 → 已理解/待澄清 → 已确认）并让后端步骤接在其后，不得再为这一阶段另起轨迹之外的独立提示条；该阶段的客户端墙钟以 `intent.understanding_ms`（可选、有上限、绝不参与检索）回传，成为持久 `intent` 步的 `duration_ms`，重开会话回放时不会凭空少掉这一段。后端必须在 Memory 检索**之前**推送 `intent` 步；命中私有记忆时记 `memory` 步——它记录的是**召回**而非归因（记忆进了 prompt 却没被引用是常态，按锚点过滤会漏报；推迟到合成之后又会离开事情真正发生的位置），归因由答案里的 `[k]` 引用承担，措辞不得声称被采用；零命中改记一条**带耗时的 `skip`**，候选查询与 embedding 调用照样发生，丢掉这段就与「覆盖整轮」矛盾。答案生成之后记 `synthesis` 步——那次生成通常是整轮最长的一段，既不能不可见，也不能被排除在轨迹总耗时之外；它的引用数取绑定锚点，不取检索到的证据卡数。实时轨迹面板按引擎是否真的流式推轨迹（后端 `AskMode.streaming`，前端镜像为 `streamsTrace`，由 `scripts/check_ask_modes_contract.py` 锁同步）判断，**不得按显示分组判断**：深入分析组里的非流式引擎会因此全程只显示「等待后端事件…」。

## 五、`AGENTS.md` 章节索引

按需查阅，用章节标题定位（不要依赖行号）：

| 要查什么 | `AGENTS.md` 章节 |
| --- | --- |
| 文档同步规则（四份） | Documentation Sync |
| 完成 spec 特性怎么记账 | Tracking Completed Spec Features |
| 后端能力必须配前端 | Full-Stack Parity (Backend ⇄ Frontend) |
| 产品形态、四个页签、上传到问答的完整流程 | Product Flow / MVP Scope |
| 分层架构、repository facade、端口与适配器 | Architecture Baseline |
| Python 环境、`PYTHON_BIN`、后端启动命令 | Python Environment / Backend Commands |
| 前端约定、`object_type` 标签契约、错误层三段规则 | Frontend/UI |
| 界面词 ↔ 内部词对照表（词汇守卫的真源） | 界面词汇表 (User-Facing Vocabulary) |
| 依赖政策、Python 版本下限、不加 Docker | Dependency Policy / No Docker In First Version |
| LLM 环境变量、模型服务状态与诊断 | LLM Configuration |
| 事件日志、按用户隔离的日志目录 | Logging / Observability |
| `scripts/check.sh` 三泳道、暖门时间目标、CI | Verification / GitHub Actions CI |
| 测试怎么分层、测试根在哪、并发 worker | Test Architecture |
| worktree、子代理逐任务、文件安全 | Git And File Safety |
| 收尾提 PR 的标准流程 | Feature Completion (Finish With a PR) |
