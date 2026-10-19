# 图谱分析「问题清单」实施计划

2026-10-19。依据：[设计规格](../specs/2026-10-19-kg-analysis-findings-design_zh.md)（已拍板）。两份计划草案由两个 opus 计划代理分别起草（P1+P2、P3+P4），本文合并、去重，并记下实施期的工程取舍。

**用户硬要求**：特性不许留一半，计划范围同一轮做完；不得引起性能或效果回退；双后端 parity，以 PG 为准；测试 hermetic，不许 skip/xfail；热函数天花板只降不升；本机重负载经 heavy-gate 串行。

## 一、PR 切分与顺序

| PR | 内容 | 依赖 | 基线 |
|---|---|---|---|
| PR-0 | 流式读取：PG `community_graph_rows` 改服务端流式游标（既有缓冲问题，规格 §7.1 顺带发现） | 无 | master |
| PR-1 | 存储与契约：迁移、全部登记点、两侧 store 原语、合并代码分量列及两处写入（全量收尾 + 跳过路径），同步捕获集按版本冻结。合入后没有可见行为 | PR-0 | master |
| PR-2 | 规则与读侧：领域契约、纯规则引擎（含 S1 的纯函数 v1/v2 种子与 squash key，只加函数不切调用点）、计算挂点与发布接线、relink 标记、`/findings` 与豁免端点、overview 新字段、后端文档 | PR-1 | PR-1 合入后 rebase |
| PR-3 | 前端：四区、弹窗共存、动作接线、S4、界面词、release note | PR-2（契约先行，可并行开发） | PR-2 合入后 rebase |
| PR-4 | P3+P4 一个 PR：S1 切换种子 + 决定重算 + 描述缓存按签名复用 + 待确认合并显示名 + F4 squash 候选档 + S2 热启动划分与 board_key + 一次 `CLUSTER_ALGO_VERSION` bump + 运维文档 | PR-1（合并代码分量列，否则 S4 会挡住修复）、PR-2（squash key 同源） | PR-2 合入后 rebase |

迁移号：开工时 origin/master 是 SQLite v94 / PG 0074；开工后 master 先合入了 v95/0075 与 v96/0076，所以 PR-1（#937）实际落 **v97/0077**。PR-4 先用占位，rebase 时取合入当时 origin/master 的下一号（PR-1 之后即 v98/0078 起）。PG 同号文件并存不报冲突，所以 MANIFEST 与全仓 `migrate() == N` 断言随改号一起改。

## 二、任务

模型：设计、疑难、评审用 opus；规格已定的实现用 sonnet。每个任务完成后做规格评审 + 代码质量评审（opus），再进下一个。

### PR-1 存储与契约

- **T1 同步捕获集按版本冻结**：`_migration_84` 按**当前**清单渲染触发器，v84 之后新增的 SYNCED 表会让从零迁移梯报 `no such table`。`TableSyncSpec` 加 `since_schema`（默认 84），各渲染函数收 `at_schema`；`_migration_84` 显式传 84，输出逐字不变。验收：从零迁移梯跑通；v84–v94 快照校验逐字不变；变异「去掉 at_schema」报红。
- **T2 迁移**：`kg_analysis_findings`、`kg_analysis_marks`、`unified_kg_state.cluster_code_version`；SQLite `_migration_97` + SCHEMA_VERSION；PG `0077`（原计划 v95/0075，见上「迁移号」）；`POSTGRES_BUSINESS_TABLES`、`POSTGRES_JSON_COLUMNS`、MANIFEST 配对；testkit 回滚；全仓 `migrate()==N`；v9 fixtures 重生；verifier DDL 表。
- **T3 生命周期登记**：shadow manifest 复制序；sync manifest（findings = SYNCED，marks = LOCAL）与 findings 捕获触发器；`user_references` 登记 `kg_analysis_marks.actor_user_id`；`notebook_delete_tables`；两侧删除知识图谱清单（含 state 重置新列）；两侧 `_clear_notebook_derived_kg`；索引管线发布；`merge_dbs`（findings 不导入，marks 随库、清 relink 标记）；`copy_memory_cases` 缺席集。`relink_attempted` 在每一个让 `kg_mutation_seq` 归零的地方清掉；`dismissed` 保留。
- **T4 两侧 store 原语 + ports**：`replace_kg_analysis_artifacts` 加 `findings_rows` / `prune_marks`；`finish_rebuild_state` 加必填 `cluster_code_version`；新方法 `kg_analysis_finding_rows`、`kg_analysis_mark_rows`、`write_kg_analysis_mark`、`clear_kg_analysis_mark`、`kg_finding_source_states`、`kg_finding_display_name_rows`、`stamp_cluster_code_version`、governance `merge_candidate_states_for_canonicals`（ports 计数 +8，先例 AnswerLinkStore +3）。展示名读取排除 memory/knowhow 来源的对象。各守卫清单登记。
- **T5 合并代码分量**：`kg_merge.cluster_code_version(settings)`（算法版本 + embed_dim + runtime_dim），`_cluster_input_version` 哈希逐字不变；全量收尾与跳过路径（带守卫的 UPDATE）各写一次；跳过分支的催收块抽成 helper，下调 `rebuild_unified_kg` 基线。

### PR-2 规则与读侧

- **T6 领域契约**：`ARTIFACT_FINDINGS` 进 REQUIRED 与 CLUSTER_DEPENDENT、不进 BOARD_DEPENDENT；`LEDGER_CONTRACT_KEY` 戳（service 盖、store 写入口查、缺键硬失败）；`domain/kg_findings_contracts.py`（代号、阈值具名常量、`RULES_VERSION=1`、`MAX_PERSISTED_FINDINGS=500`、persisted = 明细行数的检查）；Settings `kg_findings_max_candidate_pairs`。
- **T7 纯规则引擎 `services/kg_findings.py`**：连通分量（csgraph，`n = edge_table.n_nodes`）、度数；与 `SourceBoardCounter` 合成一趟（不扩 `can2idx`，不在图里的 concept canonical 用旁路 dict）；F2 前缀过滤 → F3 → F1（F1 只按共享判，`giant_share` 只作证据；两份重复的错文件合成一行 F1、伙伴挂附注）；F4 窄/宽分档、排除驳回/暂缓；F5；`finalize_findings`（对比摘要、first_seen 继承、规则版本不同或任一侧截断时 `comparable=false`）。S1 纯函数放 `domain/kg_merge_seed.py`：冻结 v1、v2 种子、squash key = v2 去空格；`_norm` 不动。
- **T8 计算挂点与发布接线**：抽出 `_scan_canonical_graph`（逐行按 `kr.source_id` 计关联数，行序不变，下调 `rebuild_communities` 基线）；两侧 `community_graph_rows` 加 `src`、`source_canonical_rows` 加 `is_concept`；`_compute_kg_analysis` 顺序见规格 §7.1；发布事务内唯一一次有界读（账本 + 旧明细）→ `finalize_findings` → replace；relink 成功出口写 `relink_attempted`。
- **T9 读侧与端点**：`_absence` 加 `upgrade_pending`；产物视图六份；overview 加 `findings_summary`、`merge_code`（同一读快照）；`findings_page`（一个 `read_snapshot`，不记忆化）；豁免写入口放在 `KgAnalysisService`（不加 facade 公开名）；路由 `{subject_key:path}`；`KG_FINDINGS_PAGE_MAX`；重生 `api_contract.json`。
- **T10 parity 与真值**：合成夹具两后端逐字相同；本机 nb-b37185f4ae 手动回归（F1=12、F2=8、S1 窄组=14、F5=1,830）。
- **T11 生产演练脚本入库**：把已交付的只读探针收进 `scripts/kg_findings_rehearsal.py`，复用 T7 的纯函数，登记 `scripts/README.md`。
- **T12 后端文档**：product-and-api 双语、deployment-and-configuration 双语（新 Settings）、原设计 §3.3/§二 修订、`kg_analysis.py` docstring 边界段、`architecture.md`。

### PR-3 前端

- **T13 API 与模型层**：类型、取数函数；纯函数：S4 按钮状态表（代码分量不一致优先 → dirty / stale=true / 账本不齐 → 只有 stale=null → 全部一致）、分区、规则文案、证据格式化；`upgrade_pending` 灰色。
- **T14 弹窗共存**：`source-detail↔kg-analysis`、`catalog-review↔kg-analysis`；关掉来源详情后焦点回原按钮；守卫与组件测试。
- **T15 四区界面**：`kg-analysis-findings.tsx`；头部 S4 按钮与原因；对比行；删「来源复核入口」卡与它额外的 sparse 请求；「主题结构信号」改报 S3；诊断数据 `<details>` 默认折叠；交互反馈规则。
- **T16 page.tsx 接线**：删除 → 既有 `confirmDeleteSource`；重新解析 → 新确认框 → `runReparse`；补上关联 → `startRelink` + 结果写按钮旁；待确认合并 → 锚点定位；处置完成后失效令牌重取问题页；只读/镜像库/promotion 不显示按钮。
- **T17 收尾**：`ui-vocabulary.md`、release note（`feature`）、产品文档界面段、`fangan_done.md`。

### PR-4 种子修复与板块稳定（P3+P4）

- **P4-T2 切换种子并记录发布代的种子空间**：别名表、`_seed_with_alias`、`bridge_canonical_id`、统一重建种子流、催收、增量融合按种子版本参数化；`unified_kg_state.concept_seed_version` 在发布合并结果的同一语句里写；增量融合按已发布代的空间安置。
- **P4-T3 迁移**：候选表 `seed_version`（存量 1）；state 三列 `concept_seed_version`、`community_partition_version`、`community_partition_mode`；`communities.board_key`（回填为 id）；连带登记；删除知识图谱复位。
- **P4-T4 已存决定重算**（算法见规格 §五 S1「实施细化」）：放在每个库第一次 v2 全量重建里；覆盖 `~v1:` 例外；悬置发事件；决定计数折进输入版本。
- **P4-T5 描述缓存按签名复用**。
- **P4-T6 待确认合并「同名不同写法」标记**：两侧 canonical 名（后端 `name_a` / `name_b` + 前端 + 产品文档）已由单独的 PR（`claude/kg-pending-merge-names`）先行交付；「同名不同写法」标记仍在 PR-4 随 S1 的 squash key 一起加。
- **P4-T7 F4 squash 候选档**：星形对、与 ANN 行按 canonical 对去重、过既有决定过滤、上限 Settings、触顶发事件。
- **P4-T8 `CLUSTER_ALGO_VERSION=4`**。
- **P4-T9 纯分区模块 `services/kg_partition.py`**：按分量逐个跑 igraph Leiden（分量内按 canonical id 排名编号、边排序、resolution = m_c/m、可选 `initial_membership`）。每个分量**逐轮**跑 `n_iterations=1`，每轮开跑前重置 seed=42。每个起点先归一：社区按内部边拆成连通段，不足 min_size 的段拆成单点，再按首次出现序编号。一轮的结果若原样回到起点，或者模块度抬高 ≤ `LEIDEN_MIN_ROUND_GAIN`（1e-4）且没有在模块度不降的前提下多进板块，就作废这一轮、输出它的起点。这样以发布结果热启动在构造上就是不动点，发布的板块也都内部连通。轮数上限 64，触顶如实计数。总边权 m_c² < 2m 的分量直接整块成板块（可证的唯一最优）。board_key 续用（互为最佳且 J ≥ 0.5）与新铸（成员内容哈希、代内撞键确定性再散列）。没装 igraph 时走冷启动 networkx Louvain，记 `cold_fallback`，不拆分。模块导出 `PARTITION_ALGO_VERSION`，由 T10 写进 `community_partition_version`。**不用 `n_iterations=-1`**，有两个原因：它在对称结构上可能永不返回（4 个 K4 成环、以两两成对为起点即复现；评审另测 66 种对称变体里 21 种 4 s 不返回）；它停在续下去的随机序列上，输出在重置 seed 后不是不动点。
- **P4-T10 接入 `rebuild_communities`**：冷启动判据（无已发布划分 / 分区版本不同 / networkx 回退 / 显式 `--cold-partition`），**force 不等于冷启动**；热启动在事务外流式读 `_published_from` 那一代的成员；发布语句写分区版本与 mode；payload 记 `partition_mode` 与续用数；补账本路径的放弃复核不动。
- **P4-T11 文档**：operations / product-and-api / ui-vocabulary 双语；release note「主题板块会整体重新划分一次」；原设计 §3.32 推翻指针；K1 注释改公式；`recluster_kg --cold-partition`。
- **P4-T12 实测**：本机真库复现规格数字；分区与种子流的耗时/RSS 对比基线。

## 三、实施期工程取舍（不另行拍板）

| 取舍 | 结论 | 理由 |
|---|---|---|
| `kg_analysis_findings` 的同步类别 | SYNCED，与 `kg_source_profiles` 一致 | 账本行是 SYNCED 的，明细不跟着走会出现「账本说有、明细没有」；代价是 T1 改封存迁移的调用参数（输出逐字不变，有守卫） |
| F3b「每段对象数」 | 本批不纳入，F3b 只用关联/对象比与孤立对象占比；演练脚本照常量 | 需要新增一次 chunks 全表分组；规格 §7.2 写明以生产实测决定 |
| ports 方法计数 | +8 | 有先例；退役死方法另作 |
| 明细表逐行 `built_at_seq` | 删掉，统一取账本行的 seq；补 `first_seen_cluster_seq`、`subject_refs` | 计划代理核对后建议，避免两处记同一件事 |
| F4 状态核验 | 本库候选切片里分批过滤（不是主键点查） | `concept_merge_candidates` 只有 `(notebook_id, status)` 索引 |
| board_key | 新增并在 API 暴露 | 用户批准的方案 C 含 id 续用；`communities.id` 不能跨代沿用 |
| 撞种子例外覆盖面 | 拒绝与暂缓都覆盖，不分人工/自动判重 | 行为零变化 |
| `recluster_kg --cold-partition` | 加 | 运维需要能重置历史依赖 |

## 四、实施期拍板

- **新种子空间的收敛方式（2026-10-19，用户）：懒收敛，只写文档。** 不加启动后自动排队；每个库在下一次「更新分析」或整理时自然切换。面板靠合并代码分量列提示「合并规则已更新，需要更新一次」（S4）。运维文档写明混合期的影响（多词概念的跨库桥接失效，直到活动库与参考库都完成一次重建）与建议：参考库优先、低峰手动跑 `backend/app/scripts/recluster_kg.py`。

## 五、执行台账

在跑的代理、worktree、PR 链接与状态记在会话记忆 `kg-findings-program-2026-10-19.md`，本文只记计划。
