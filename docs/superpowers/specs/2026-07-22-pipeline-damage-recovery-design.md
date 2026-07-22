# 流水线损坏善后设计

日期：2026-07-22
状态：已获用户批准（2026-07-22）

## 背景

对「解析 → 分块 → embedding → KG 抽取 → 融合 → 索引」整条流水线做了一次
断点续跑审计。结论是**恢复骨架是健全的**，但善后能力分布很不均匀：

已经做对的三层（本设计不改动，只复用）：

- **启动清算**（`backend/app/repositories/sqlite/migrations.py:1540`
  `_recover_interrupted_jobs`）：单进程前提下，「启动时仍是 `running` 的行定义上
  就是上次崩溃的残骸」，无条件翻正 `merge_review_jobs` / `ask_jobs` /
  `knowhow_rows` / `sources.parse_status='extracting'` / `extraction_runs` /
  `kg_build_jobs` 六类。跑在 lifespan 后台线程 + readiness 503 门之后，
  清算未完成前用户点不到任何业务入口。
- **续跑判据看产物而非状态列**：`parse_status` 会「看似前进却没有 elements」
  （`backend/app/services/batch_ingest.py:621` 亲口承认），所以真正的判据是
  `source_elements` / `knowledge_objects` / `chunk_embeddings` 的行存在性，
  KG 侧再叠一道「最近一条 `extraction_runs(run_type='kg')` 为 `completed`」
  （`backend/app/repositories/sqlite/knowledge_store.py:169`）。
- **rebuild 两个 LLM 阶段的行级 checkpoint**（`kg_rebuild_checkpoint`），
  版本键刻意剔除 `emb_count` 以免向量增量提交期间误 GC 掉数小时的裁决
  （`backend/app/services/knowledge_lifecycle.py:1687`）。

审计发现的缺口（本设计要覆盖的全集）：

| # | 缺口 | 位置 | 后果 |
|---|---|---|---|
| G1 | 索引全量重建就地覆盖活目录，无 tmp+rename；加载端无任何完整性校验 | `backend/app/services/kg/scale_index.py:263-334`、`:47-58` | **静默错**：旧 manifest + 半截数组被无条件加载 |
| G2 | 启动清算不覆盖 `queued` / `parsing`；调度队列是纯内存 `ThreadPoolExecutor`、无 job 表 | `migrations.py:1559`、`backend/app/services/kg/scheduler.py:79` | 源永久搁浅，界面只转圈、无提示、无批量入口 |
| G3 | `ingest` 子命令的 hash 跳过认账过早（`file_hash` 在 INSERT 时即写，早于 parse） | `backend/app/services/source_ingestion.py:378`、`batch_ingest.py:386` | parse 中途中断的源永久变空源。**当前 schema 下不可判定，见 A4**；存量走 `reparse` |
| G4 | embedding 无任何进度记账；`extracted` 只 gate 在 KG 抽取上，与向量无关 | `source_ingestion.py:596` | 「已完成」不代表向量齐全 |
| G5 | element 向量没有 missing/backfill 查询（chunk 侧与 KG 节点侧都有） | 全仓无命中 | 写了一半只能整源重跑 |
| G6 | 索引水位存「全部 source id」而非「成功索引的 source id」；`rearm_auto_index` 全仓无调用方 | `backend/app/services/scale_index_builder.py:274`、`scale_artifact_runtime.py:677` | delta 恒为 0、fold 空跑；不重启不自愈 |
| G7 | 增量融合跨 4 个独立写事务、无融合标记 | `knowledge_lifecycle.py:421-546` | 半程崩溃留下「concept 并了、claim 没并」，只能等下次全量 rebuild 纠正 |
| G8 | fold 的 `.old` 目录在崩溃窗口后永久占盘 | `backend/app/repositories/filesystem/scale_artifact_store.py:103` | 磁盘膨胀 |
| G9 | `README.md:1070` 把只写不读的 `.jsonl` 日志描述成「续跑依据」 | — | 文档误导运维 |

## 目标

把善后能力整理成**三层**，每层职责单一：

1. **加固层** —— 让损坏不发生（G1/G2 的代码修复；G3 已确认需 schema 支持，见 A4）。自动，无 UI。
2. **体检层** —— 让已发生的损坏可被发现（G4/G5/G6/G7 的检测面）。
3. **修复层** —— 让发现的损坏能一键善后（复用已有重建动作 + 两个新增动作）。

## 核心判断：善后主场放「知识分析看板」

**是，放看板，但不新增独立板块——升级已有的两块。**

理由：

1. **修复动作的家已经在看板了。** 「索引与构建」板块已承载「分析新增 N 篇」
   「全部重新分析」「重新合并知识图谱」「构建/更新索引」四个 CTA
   （`frontend/app/page.tsx` 5030-5100 一带）。善后需要的动作里，
   **只有两个是新增的**（「重新解析 N 篇」「补齐向量」），其余全是已有按钮。
   把「发现问题」搬到别处，等于让用户在两个界面之间来回跳。
2. **看板已有的「来源状态」块本身就是这次要修的东西。** 它现在把
   `parse_status` 分布直接当 tag 列出（`page.tsx:4956`），而 `parse_status`
   恰恰是审计中被判定为不可信的字段。一个卡死在 `queued` 的源在这里只是一个
   中性数字，不标异常、不给出口。所以这不是「要不要加一块」，是「已有的这块
   建立在错误的信号上，需要换判据」。
3. **符合既有的入口收拢偏好**：功能入口收进统一菜单，而不是散落成新页面。

边界（明确不放看板的）：

- **跨库 / 磁盘级残留**（G8：`.tmp` / `.old` 目录）不属于单个 notebook，
  进 admin 面，不进看板。
- **待确认中心（铃铛）只做提醒与跳转**，不承载体检详情。铃铛的语义是
  「待你确认的事」，而体检结果绝大多数是「系统能自己修的事」，塞进去会稀释
  它现有的三类待办语义。有异常时冒一条、点击直达看板对应板块即可。

## 一、加固层（无 UI，可独立先行）

### A1｜索引全量重建改原子写（对应 G1，最高优先）

`save_scale_index` 当前 `os.makedirs(out_dir, exist_ok=True)` 后直写活目录
`kg_index/{nb}`，manifest 最后落盘。改为与 **fold 路径已有的做法对齐**：

- 写入 `{scale_dir}.tmp`（复用 `scale_artifact_store.prepare_fold_directory`
  的 staging 语义，含「先 rmtree 掉上次残留的 `.tmp`」）
- 全部产物落盘后走 `swap_fold_directory` 的 `live → .old`、`tmp → live`、
  `rm .old` 两段 rename

fold 已经做对了，full 没跟上——这是一次**收敛到既有正确实现**，不是发明新机制。

### A2｜索引加载端做完整性交叉校验（对应 G1）

`load_scale_index` 当前只检查 `manifest.json` 是否存在，随后无条件 `np.load`
所有数组，没有 checksum、也没有 `n_nodes` vs `len(node_ids)` 的交叉校验。
补一层**零成本的形状自洽检查**（数组长度 vs manifest 计数），失配即视为损坏、
返回 `None` 走「无索引」路径（会触发全量重建），并 emit 一条可观测事件。

原则：**宁可退化成「无索引」也不能返回错配的索引**——前者是响亮的失败，
后者是静默的错。

### A3｜启动清算覆盖搁浅源（对应 G2）

`_recover_interrupted_jobs` 增加一条：`parse_status IN ('queued','parsing')`
的源回退为一个明确的**可重试终态**，而不是继续假装在进行中。

注意这里与既有六条的区别：`extracting` 回退到 `parsed` 是安全的（KG 抽取
是先删后抽、重跑幂等）；而 `queued`/`parsing` 的源可能**连 elements 都没有**，
不能回退成 `parsed`（那会让它被误判为「已解析」而进入 KG 抽取目标集，
抽出空 KG）。正确终态取「失败可重试」语义，判据仍以 `source_elements`
是否存在为准。具体取值在计划阶段定，需与 `PARSE_STATUS` 标签表同步。

### A4｜`ingest` 子命令的完成判据（对应 G3）—— **已撤回，需 schema 支持**

原计划把 `run_all` 的分流判据搬到 `ingest`。实现并经六轮评审后确认**该问题在当前
schema 下不可判定**，A4 已从 P1 撤出（PR #324），此处保留结论以免后人重蹈：

`ingest` 要回答的是「这个源上次跑完了吗」，但可观测的三个信号
（`source_elements` 有无、`chunks` 有无、`parse_status`）无法区分以下两对：

| | elements | chunks | parse_status | 正确答案 |
|---|---|---|---|---|
| 分块失败但仍置 extracted | >0 | 0 | extracted | 要重跑 |
| 纯标题 md 解析成功 | >0 | 0 | extracted | 不要重跑 |

（`build_chunks` 对纯标题输入返回 0 chunk——已实测；而
`source_ingestion` 把分块包在 best-effort 的 try 里，失败不阻塞流水线、仍置终态。）

`parsed` 同样二义：既是活跃过渡态（服务端正在处理），也是中断残留态。据此重解析
会与在跑的服务端抢同一个源，而 `process_source` 没有 source 级单飞守卫、还会
clear/replace 抽取态。

**正确解需要两样当前没有的东西**：① 持久化的**完成标记**（证明「本代 elements 已
成功分块」，而非从产物存在与否倒推）；② source 级**活跃租约**（区分「正在跑」与
「中断残留」）。二者都是 schema 变更。

这与体检层 H1–H3 依赖的是**同一个缺口**——它们同样要回答「这个源卡住了吗」。
建议合并设计，不要各做一套。

**用户已定的范围约束（2026-07-22）**：`batch_ingest` 的中断**不需要**在 notebook
的界面按钮里提供 resume 入口。离线批处理的续跑留在 CLI（`reparse` 子命令），
看板只负责呈现与修复**服务端**管线的损坏。

在此之前，`ingest` 维持内容哈希判据（认账偏早、只会漏修不会误伤），存量补救走
显式的 `reparse` 子命令。

### A5｜补 element 向量的缺失查询（对应 G5）

照 `missing_chunk_embedding_rows` / `count_missing_chunk_vectors` 的形状，
补一组 element 侧的 `NOT EXISTS` 查询与计数，让 `batch_ingest embed` 能一并补齐。
这是 A 层里唯一新增能力（其余都是修复/收敛），也是体检层 H5 的数据来源。

### A6｜文档订正（对应 G9）

`README.md:1070` 关于 `.jsonl` 的续跑说法改为事实：续跑靠 DB 查询推导，
该文件是只写日志、全仓无读取方。

## 二、体检层

### 设计原则：体检不是新增扫描，是把已有查询聚合暴露

绝大多数判据**已经存在**（CLI 在用），只是没有暴露给 UI。这条决定了体检的
代价上限：不引入新的全表扫描，不引入模型调用。

### 检测项

| # | 体检项 | 判据 | 数据来源 | 代价 |
|---|---|---|---|---|
| H1 | 搁浅源 | `parse_status IN ('queued','parsing')` 且滞留超阈值 | 新查询 | 走 `idx_sources_nb_parse_status_type` 覆盖扫描 |
| H2 | 空源（解析未落地） | 有 `sources` 行、无 `source_elements` | `sources_with_elements` 已有 | 已在 CLI 热路径使用 |
| H3 | 缺分块 | 有 elements、无 `chunks` | 新查询 | 两个已建索引的存在性判定 |
| H4 | 缺 chunk 向量 | `count_missing_chunk_vectors` | **已有** | 已有 |
| H5 | 缺 element 向量 | A5 新增 | A5 | 与 H4 同形 |
| H6 | KG 未完成 | 最近 `extraction_runs` 非 `completed` | **已有**，且已 memo 在 `kg_mutation_seq` 上 | 已有 |
| H7 | 索引过期 / 维度失配 | `state` / `stale_reason` | **已有** | 已有 |
| H8 | 索引产物损坏 | A2 的交叉校验结果 | A2 | 加载时一次 |

**效率约束**：看板是高频入口，H1–H8 聚合进**一个** endpoint，整体挂到
`knowledge_counts_cache` 已有的 `kg_mutation_seq` memo 机制上（该机制已经在
为 per-type 计数、pending 源计数、chunk 计数服务）。**不新增后台定时扫描**——
只在打开看板时算，命中 memo 即 O(1)。

### 明确不做的检测

- **G7（增量融合半程）不做检测。** 要检测它需要新增「本源已融合」的水位标记，
  而增量融合本身跨 4 个写事务、语义是「Tier-1 名种子 append」，加水位等于
  给一个设计上就允许被全量 rebuild 覆盖的中间态引入新的一致性负担。
  更划算的做法是**在体检层直接给出「重新合并知识图谱」的建议**（该动作本就
  是它的纠正者），而不是精确诊断它有没有偏。这条要在文档里写清楚是**有意
  不做**，不是遗漏。
- **不改 `parse_status` 的语义。** 它是流水线内部状态，继续保留；体检一律用
  产物判据。两者并存，不做高风险的语义迁移。

## 三、修复层

### 动作映射

| 体检项 | 修复动作 | 是否新增 | 幂等 | 代价 |
|---|---|---|---|---|
| H1 / H2 / H3 | 重新解析 N 篇 | **新增（批量）** | 是（整源重做） | 解析 + embedding |
| H4 / H5 | 补齐向量 | **新增（UI 入口）** | 是（只补缺失） | 仅 embedding |
| H6 | 分析新增 N 篇 | 已有 | 是 | LLM |
| H7 | 更新索引（fold） | 已有 | 是 | 无模型 |
| H8 | 重建索引（full） | 已有 | 是 | 无模型 |
| 融合偏差 | 重新合并知识图谱 | 已有 | 是 | 少量 LLM |

只有两个动作是真新增，其余是把已有 CTA 接到体检结论上。

### 自动 vs 手动的界线

- **加固层全自动**（A1–A3、A5–A6 是纯粹的正确性修复，不涉及成本；A4 已撤回）。
- **修复层一律不自动触发**。凡是会调用 LLM 或 embedding 的动作（重新解析、
  补齐向量、分析新增）都必须由用户点击。这是运行效率约束的直接要求——
  自动补抽会在用户不知情时烧模型额度。
- 唯一的例外是**无模型成本的索引重建**，它已经有既有的 auto/idle 调度机制，
  维持现状。

## 四、看板改造（前后端同一 PR 交付）

### 「来源状态」→ 升级为体检块

- **无异常时**：保持现在的中性 tag 行，不打扰。这是常态，不能让健康的库看起来
  像有问题。
- **有异常时**：异常项用既有语义色板（黄 `#b97a00` / 红 `#b42318`），配一句
  人话说明 + 修复按钮。
- **文案不暴露技术名词**：不出现 `parse_status`、`queued`、`chunk`、`embedding`
  这类词。例：「3 篇上传后没能开始解析」+「重新解析」按钮，而不是
  「parse_status=queued: 3」。

### 「索引与构建」→ 增加可信度维度

现有的 `state`（indexed / stale / building / queued）之外，接入 A2 的损坏判定。
损坏与过期是两回事：过期是「新内容没进去」，损坏是「现有的不可信」，
后者必须走 full 重建、且措辞要更强。

### 铃铛（待确认中心）

只在体检发现异常时冒一条聚合提醒，点击直达看板。不复制体检详情，不新增待办类型
的语义负担。

### admin 面

G8 的跨库残留（`.tmp` / `.old` 目录）作为 admin
的运维动作，与看板解耦。

## 五、分期

| 期 | 内容 | 依赖 | 可独立合入 |
|---|---|---|---|
| P0 | A1 + A2（索引原子写 + 完整性校验） | 无 | 是 |
| P0 | A3（启动清算覆盖搁浅源） | 无 | 是 |
| P1 | A5 + A6（element 向量查询、文档订正）| 无 | 是（PR #324；A4 撤回）|
| P2 | 体检 endpoint（H1–H8 聚合 + memo） | A2、A5 | 是（后端先行不可见） |
| P3 | 看板改造 + 两个新增修复动作 | P2 | 前后端同一 PR |
| P4 | admin 残留清理（G8） | 无 | 是 |

P0 三项互不依赖，是纯粹的止血，建议先行。

## 六、验证要求

- **A1/A2 必须做故障注入验证**：在写产物的中途 kill，确认活目录仍是上一版
  完好索引；再构造一个「manifest 计数与数组长度失配」的目录，确认加载端
  判定为损坏而非静默加载。只加校验不验证等于没加。
- **A3 需变异验证**：造一个 `queued` 滞留行，确认重启后进入可重试终态，
  且**不会**被误纳入 KG 抽取目标集（抽出空 KG 是这条修复最可能引入的回归）。
- **A4 已撤回**，其验证要求随之作废。重启这条轨道时，先补完成标记与活跃租约，
  再按「六情形表」逐格构造用例——判据没有信息维度支撑时，任何用例都只是在
  两类错误之间挑一个。
- **体检 endpoint 需在大库上量代价**，确认命中 memo 时为 O(1)、未命中时不
  劣于现有看板打开耗时。
