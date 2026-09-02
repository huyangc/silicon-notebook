# 批 3·W1：删库作业化（设计规格 **v5·定稿**）

> **定稿标记**：v5，经**四轮规格评审闭环**，终审 **零 P0 / 零 P1**。
> 四项决策（D-1..D-4）已于 **2026-09-01** 拍板并烘进本文，见「已拍板」节。
> 本文自此为 W1 的实施依据；后续改动走 PR 评审，不再出规格新版。

热路径修复计划批 3 的 W1 项（批 2 与 W-CLI 已收官）。
三条红线不变：不降检索性能、不降 KG 抽取性能、不改问答结果质量。

本文只写设计，不改产品代码。

**基线口径**：全部现场引用按 **`origin/master` = `16a0f444`** 落点，逐条实测。
PG 迁移共 **46** 个 → 下一可用号 **0047**；SQLite `SCHEMA_VERSION = 67`
（`backend/app/repositories/sqlite/migrations.py:114`）→ 下一为 **`_migration_68` / 68**。
`notebooks` 级联闭包实测 **65 张**（L1 47 / L2 15 / L3 3）。
⚠ 动手时须复核迁移号——trunk 仍在前进。

**v3 相对 v2 的实质改动**：① 归档围栏方案推翻重做（`archive_fence_seconds` 关不上在途
作业写，改为相位 5 单事务）；② 新增复合键删除原语（22 张表不能用 `= ANY` 单列形）；
③ `paths` 相位改回全量物化；④ 新增 `quiesce` 相位闸（等重建真的停）。

**v4 相对 v3 的实质改动**：① `quiesce` 闸补齐 `KgMaintenanceJobs` 这条腿——
`relinkkg-`/`unifiedkg-` 不写任何 `kg_build_jobs` 行，v3 的闸对 2/3 对手形同虚设；
② 相位 5 的时长估算按实测输入改准（**3–10s**），
并修掉 v3「65 次零命中索引探查」那句被证伪的话；
③ `0047` 并入**三条**新索引（v3 只有一条）；④ 守卫扫描范围与豁免机制定稿；
⑤ 新增「定稿后处理」节。

**v5 相对 v4 的实质改动**：**D-1 落地——生产 `POSTGRES_STATEMENT_TIMEOUT_SECONDS`
实测为 180（不是仓库默认的 30）**。全篇按 180 重算：
① W1 的定性从「删除从未成功过」修正为「180s 预算下大概率仍不够，且一个 3 分钟的
写事务本身就是病灶」（摸底 3、§1.1、§1.2）；
② 相位 5 的 3–10s 对 180s 有 18–60 倍裕度，**D-4 的语义因此从「放宽」翻转为
「收紧」**（§T-3.2、D-4）；③ 批大小与 T-0 判读基线按 180 标定；
④ D-2/D-3/D-4 的决定写进正文，「待用户拍板」节改为「已拍板」。

## 目标与非目标

目标：把「删除笔记本」与「清空笔记本 KG」两条今天各自跑在**一个无界事务**里的路径，
改成「用户侧立即返回的代次化标记 + 后台分批清理作业」，使**除相位 5 之外的任何事务**
都是页级短事务、相位 5 的单事务时长从 O(对象数) 降到 O(来源数)，同时
① 不破坏 trunk 的归档快照一致性不变量，② 闭合磁盘产物泄漏，
③ 吸收 R3·PR-A 登记的 `delete_notebook_kg` seq 语义冲突（PR #638 R4 P2）。

非目标：不做表分区；不改 FK 拓扑；不引入新的跨进程通知通道；
不改 `copy_notebook` 深拷贝语义；不给 SQLite 部署做跨进程互斥；不做回收站。

## 摸底结论（v4，全部按 `origin/master` 复核）

1. **删除笔记本 = 一个事务里的「行锁围栏 + 归档投影 + 65 表级联」。**
   `DELETE /api/notebooks/{id}`（`backend/app/api/notebook_routes.py:133-138`，204，
   `require_notebook_capability("notebook:delete")`）→
   `NotebookCatalogService.delete_notebook`（`backend/app/services/notebook_catalog.py:704-717`，
   诊断相位 `notebook_delete.db` / `notebook_delete.files`）→
   `delete_row_and_orphan_embeddings`（PG `backend/app/repositories/postgres/notebook_store.py:365-411`，
   SQLite 孪生 `backend/app/repositories/sqlite/notebook_store.py:413-447`）。
   单事务九步：① 聚合根 `FOR UPDATE`；②–⑤ 对 `ask_jobs` / `sources` /
   `source_paper_meta` / `reports` **逐行 `FOR UPDATE`**（`postgres/notebook_store.py:386-402`）；
   ⑥ 读 `sources.file_path`；⑦ `_retain_user_activity_before_delete`（`:413-491`）；
   ⑧ `DELETE FROM knowledge_embeddings`；⑨ `DELETE FROM notebooks`（65 表级联）。
2. **围栏防的正是并发 UPDATE，不是并发 INSERT。**
   `postgres/notebook_store.py:380-385` 的注释原文：「The parent lock blocks new FK
   children, but **an update that keeps the same notebook_id takes no parent-key lock**.
   Lock every row whose current metadata enters the snapshot」。
   在途写者按**行 id** 定位、**不重读** `notebooks.status`：
   `postgres/ask_state_store.py:237`（`UPDATE ask_jobs SET status,answer_id,error,updated_at`）、
   `:256`（cancelled）、`:893`（done）、`postgres/source_store.py` 的 10 处 `UPDATE sources`、
   `postgres/kg_build_job_store.py` 3 处、`postgres/chunk_store.py` 1 处。
   ⚠ **这直接否掉 v2 的 `archive_fence_seconds`**：tombstone 只挡**新请求**，
   `statement_timeout` 只界**单条语句**不界**作业**（一个 ask worker 可以在 tombstone
   之后继续跑几分钟再落最后一条 UPDATE），分页 `FOR UPDATE` 的行锁**每页提交即释放**。
   三条机制没有一条能覆盖「已在跑的作业稍后写一行」。见 §T-3 相位 5。
3. **在线 `statement_timeout`：仓库默认 30，但生产实测为 180（D-1）。**
   `postgres_statement_timeout_seconds` 的**仓库默认**是 30
   （`backend/app/core/config.py:1274-1277`）、`lock_timeout` 默认 5（`:1286-1289`），
   由池在**每次借出**时 `set_config(..., false)` 重压
   （`backend/app/repositories/postgres/database.py:359-366`）；
   `docs/operations.md:871` 描述的也是 30 这个默认值。
   ⚠ **D-1 已核实：生产环境把该值调到了 `180`。全文按 180 推导，30 只作为仓库默认
   出现。**（`docs/operations.md` 的该段应顺带补一句「生产实际值见部署配置」，
   否则读者会照 30 推导——列入「定稿后处理」。）
   级联触发器算在同一条语句内，所以**整个 65 表级联是一条语句、吃一份预算**。
   ⚠ **定性修正（v5）**：这**不是**「删除从未成功过」。180s 下的实际形态是——
   ① 按 §1.1 的量级，878 万对象的整库级联**大概率仍超 180s**，
   撞墙后整体回滚，且每次失败前烧掉的 WAL 与 xmin 停滞是 30s 时代的 **6 倍**；
   ② 即便某些中等库能在 180s 内跑完，**一个长达 3 分钟的写事务本身就是病灶**
   （xmin horizon 停滞 3 分钟、独占一条池连接 3 分钟、四张表的全部行锁持有 3 分钟）。
   W1 要消灭的是②，而不只是让①不再撞墙。
   ⚠ **事务级 timeout 调整的既有先例**：`postgres/database.py:162`
   `SELECT set_config('statement_timeout', %s, true)`（第三参 `true` = 事务局部），
   相位 5 用它（见 §T-3.2 与 D-4）。
4. **`delete_notebook_kg` 没有 HTTP 路径，且是 11 条无界 DELETE。**
   生产唯一调用点在已后台化的 `rebuildkg-` 作业内部
   （`backend/app/services/knowledge_lifecycle.py:3147-3148`），另有
   `scripts/denoise_reextract_nb.py` 与三处运维转发面
   （`services/repository_facade.py:1550`、`postgres/maintenance.py:502-503`、
   `sqlite/maintenance.py:116-117`）。`delete_notebook_graph_rows`
   （`postgres/knowledge_store.py:262-335`）= 7 条显式 DELETE + 对
   `_GRAPH_RESET_TABLES - {knowledge_embeddings, extraction_runs}`（4 张，`:50-57`）
   的循环 = **11 条**。
5. **`copying` 哨兵是三个词法类，绝不能合并。**
   - **读侧谓词 40 处**（本次逐行枚举：PG 20 + SQLite 20）：
     `postgres/query_store.py` 13、`sqlite/query_store.py` 13、两个 `notebook_store.py`
     各 3、两个 `mount_sql.py`（PG 2 / SQLite 3）、两个 `group_store.py` 各 1、
     两个 `knowledge_counts_cache.py` 各 1、`sqlite/access_sql.py` 剩余。
     语义 = 「这行还不算存在」。
     ⚠ 评审给的是 PG19/SQLite21（合计同为 40）；两边各差 1 的归类分歧不重要——
     **守卫必须由逐行枚举的清单驱动，不由计数驱动**，计数只作 sanity check。
   - **写侧哨兵 6 处**，全在 `sharing_store.py`（PG `:646,656,661` /
     SQLite `:728,740,746`）：`compensate_copy` 的 `DELETE ... AND status='copying'`
     与 `sweep_stale_copies` 的两条 `SELECT ... WHERE status='copying' ... FOR UPDATE`。
     语义 = 「专指半拷贝，去物理删掉它」。
   - **生产者 1 处**：`services/notebook_sharing.py:88` 的 `status="copying"`
     （Python kwarg，不是 SQL）。守卫不得误伤它，但覆盖说明里要点名。
   - **散文/docstring 豁免**：带 `!=` 形的散文引用 4 行——
     `postgres/access_sql.py:57`、`sqlite/access_sql.py:106`、`sqlite/mount_sql.py:101`、
     `api/admin_routes.py:479`；另有若干只提 `copying` 一词的注释不触发守卫。
     **守卫规则明文声明：只扫非注释、非 docstring 的字符串字面量**，并附上述豁免清单。
   **把写侧折进两值谓词 = 灾难**：`sweep_stale_copies` 会把 `deleting` 的库当半拷贝，
   走它自己那条**无界的** `DELETE FROM notebooks WHERE id=ANY(%s)`
   （`postgres/sharing_store.py:665-671`）整删——绕过分批清理器、绕过归档、重演超时。
6. **分批删除的既有形是「keyset 取页 + PK 列表 DELETE」，且该实测只覆盖单列 PK。**
   `GovernanceStore.sweep_orphan_clusters_page`（`postgres/governance_store.py:247-330`）：
   页重新表达成键 RANGE 会退化 Seq Scan + Hash Anti Join，**1M 行 fixture 上 201ms
   且线性于 N**（`:285`）；PK `= ANY(%s)` 形是 Index Scan + Nested Loop Anti Join，
   **5.3ms**（`:287`）。
   ⚠ **这两个数字只覆盖「单列 PK + `= ANY`」形**（`concept_clusters` PK 是 `id`）。
   §1.5 的第二循环形（ctid）**没有任何既有实测**，必须单独测。
   服务侧循环形见 `services/knowledge_lifecycle.py:1781-1836`。房规批大小 500
   （`postgres/knowledge_store.py:59`、`postgres/governance_store.py:40` 明文
   store-wide convention），崩溃恢复用 5000（`postgres/maintenance.py:58`）。
7. **后台作业设施齐备但没有一条接到删除上。**
   `background_jobs.submit`（`services/background_jobs.py:442`）+ 重/轻双池
   （`:41-58` 前缀路由、`:74-103` 池成员、`:104-107` 池名与默认容量 4/4、`:111` 排队告警）；
   崩溃恢复 `recover_interrupted_jobs`（`postgres/maintenance.py:581`，逐语句独立事务，
   由 `services/startup_warmup.py:482` 调用）⚠ 语义是**结算为 failed**，不是**续跑**。
   ⚠ **`rebuildkg-`/`relinkkg-`/`unifiedkg-` 都不取 per-notebook advisory 锁**——
   与删除作业**零互斥**。
7b. **⚠ KG 维护有两套互不相通的作业簿记，`quiesce` 闸必须同时查两套。**
   - **durable 簿记**：`kg_build_jobs` 表（`0001_initial.sql:234-252`），单飞靠部分唯一
     索引 `idx_kg_build_jobs_one_running ON kg_build_jobs(notebook_id)
     WHERE status='running'`（`0002_integrity_indexes.sql:22`）——「该库有没有在跑的
     **抽取**」是一次索引点查。**只有 `buildkg-`/`rebuildkg-`（`execute_notebook_kg_job`）
     写这里。**
   - **进程内簿记**：`KgMaintenanceJobs.jobs` 纯 Python 字典 + 锁
     （`services/kg/maintenance_jobs.py:71-88` 的 `claim`；字典与锁定义在 `:68-69`）。
     `relinkkg-`（`api/kg_routes.py:165-173`，`start_notebook_relink` →
     `run_notebook_relink_job`）与 `unifiedkg-`（`api/kg_routes.py:236-244`，
     `start_unified_kg_rebuild` → `run_unified_kg_rebuild_job`）走这里，
     **一行 `kg_build_jobs` 都不写**。模块注释明写这是刻意的分离：
     「This stays separate from durable kg_build_jobs: relink/rebuild are maintenance
     passes, and publishing them as extraction jobs would claim the wrong single-flight
     domain」（`maintenance_jobs.py:60-67`），同段并明写
     「Production runs one worker, so process-local ownership is the deployment
     contract」——**这正是 §T-3.3 依赖它的授权依据，也是必须显式声明的依赖**。
   ⚠ **KG 构建没有任何跨线程取消入口**：`KgExtractionRunControl`
   （`services/kg/run_control.py:41-155`）只由作业自己的线程持有，全仓无控制器注册表、
   无 cancel 端点（对比 ask/report/catalog/scale 都有）。
   ⚠ 而且 4.2 选项 A 的检查点位置**只覆盖抽取路径**
   （`knowledge_lifecycle.py:3176` 的批循环）：
   `run_notebook_relink_job` / `run_unified_kg_rebuild_job`
   （`maintenance_jobs.py:141-159` / `:174-192`）本身只是 try/settle 包装，
   真正的循环在 `knowledge_lifecycle.relink_notebook_kg`（`:1352`，逐源循环在 `:1409`）
   与 `rebuild_unified_kg`（`:4285`，10 个阶段边界共用 `_stage` 于 `:4389-4395`）。
   **这两处今天各自没有任何 `notebooks.status` 检查点。**
8. **跨进程 per-notebook 互斥原语已落地（W-CLI/PR #643）。**
   三值契约 `ScaleBuildLockAttempt`（`repositories/scale_build_lock.py:15-29,109-126`）、
   `advisory_lock_key`（`:129-140`）、PG 实现 `try_scale_build_lock`
   （`postgres/database.py:678-754`，namespace `0x53434C42` 于 `:60`，
   `application_name` 于 `:712`，会话槽预算 `SCALE_BUILD_CONCURRENCY + 1` 于 `:287-292`）、
   SQLite 哨兵 `:82-106`。
9. **`notebooks` 仍无软删列；但 trunk 已有「删除后保留的归档」。**
   `retained_user_activity`（`0044_retained_user_activity.sql:5-51`）带 `deleted_at`/`expires_at`，
   由 `USER_ACTIVITY_RETENTION_DAYS`（默认 180，`config.py:1213-1218`）与
   `core/activity_time.py:86` 打戳。它是删除的**产物投影**，不是软删标记
   （`notebooks` DDL 自 `0036` 后未变）。结论：**软删标记仍需复用 `status`，
   但「删除时刻」已有权威来源，不必再加列**。
10. **8 张带 `notebook_id` 却在级联闭包外的表**（穷举 88 张表定义）：
    `community_members`（`0001_initial.sql:135`）、`conversations`（`:191`）、
    `knowledge_object_sources`（`:353`）、`knowledge_embeddings`（`:345`）、
    `object_schemas`（`:538`）、`kg_cluster_scratch`（`:254`）、
    `kg_canonical_scratch`（`0008_master_v28_features.sql:3`）、
    `retained_user_activity`（`0044:5`）。
    只有 `knowledge_embeddings` 被两个后端显式删除。
    ⚠ **`retained_user_activity` 与 `object_schemas` 绝不能进清理表序**（前者是删除的
    产物，DDL 注释明文「Deliberately no notebook FK」；后者是全局注册表）。
    ⚠ 今天 `delete_notebook` 留下 **5 张表的孤儿行**：`community_members`、
    `conversations`、`knowledge_object_sources`、两张 scratch——前三张在 analog base 上
    是千万行量级。`compensate_copy`/`sweep_stale_copies` 有同一缺口。
11. **磁盘产物今天完全没被删，而且是静默泄漏。**
    `delete_notebook` 只清来源文件（`backend/app/repositories/source_files.py:64-67`）
    与资产目录（`services/notebook_catalog.py:83`）。scale 三根
    `{storage_dir}/kg_index/{nb}`、`{storage_dir}/kg_viz/{nb}`、
    `{storage_dir}/kg_index_partitions/{nb}`
    （`backend/app/repositories/filesystem/scale_artifact_store.py:192-205`）
    及其 `.old` / `.tmp` / `.tmp-<claim_token>` **三种**兄弟目录
    （过滤器 `backend/app/repositories/filesystem/scale_artifact_store.py:211-216,222-227`
    明文排除这三种）一个都不删。
    ⚠ 启动预载只是**跳过**孤儿目录（`services/scale_artifact_runtime.py:625-629`
    的 `notebook_tier(...) is not None` 过滤），**不删**——所以泄漏无任何报错，
    按删除次数线性增长（scale index 常驻 ~5GB/大库）。

---

## §1 问题量化

**本节不编数字。** 逐表精确行数与逐批计时由 **T-0** 实测回填。

仓库内锚点（全部带出处）：
- `sources` ≈ **48 836** 行（`services/kg_analysis.py:45,605`）
- `knowledge_objects` ≈ **878 万**（`kg_analysis.py:45`）
- `knowledge_relations` ≈ **835 万**（`kg_analysis.py:53`）
- `communities` ≈ **88 580** 个板块（`kg_analysis.py:50,508`；原文明确标注「来自生产库
  只读查询，不是本机实测」，本文沿用同一强度声明）
- `concept_clusters` ≈ **9.1M** 行（`services/knowledge_lifecycle.py:1898`）
- 库总量 484GB（审计 artifact「大库热路径审计」）
- **同库同量级的冷 IO 事故：835 万边冷扫 39 分钟**（`kg_analysis.py:53-54`）。
  ⚠ 它界定的是「随机 IO 支配下、这个库这个量级的一次全扫要多久」，
  **不是删除本身的耗时**——引用它是为了否掉「几千万行操作能在 180s 内做完」这个直觉，
  不是把 39 分钟当成删除的预估。

### 1.1 结构性上界

单条 `DELETE FROM notebooks` 的工作量 = Σ(每张级联表命中行数 ×(1 次堆元组删除 +
每个索引 1 条索引项删除)) + 每删一个父行、每个子 FK 一次子表探查。
索引扇出（从 46 个迁移逐条解析，**含 PK/UNIQUE**）：

| 表 | 索引数（含 PK） | 其中 GIN trgm |
| --- | --- | --- |
| `knowledge_objects` | **10** | 2（`0006_search_gin.sql:3`、`0042:302`） |
| `knowledge_relations` | **9** | 0 |
| `concept_clusters` | **8** | 0 |
| `memory_items` | **8** | 3（`0006:5,7,9`） |
| `sources` | **10**（9 非 PK） | 0 |
| `chunks` | **7** | 1（`0006:2`） |
| `retained_user_activity` | **5** | 0 |

**按 180s 预算重算（D-1）**，只用三张最大的表就够说明问题：

| 表 | 行数 | ×索引数 | 索引项删除 |
| --- | --- | --- | --- |
| `knowledge_objects` | 878 万 | ×10（含 2 条 GIN trgm） | ≈ 8 800 万 |
| `knowledge_relations` | 835 万 | ×9 | ≈ 7 500 万 |
| `concept_clusters` | 910 万 | ×8 | ≈ 7 300 万 |
| 其余 62 张（chunks/embeddings/memory/sources/…） | — | — | 同量级追加 |

合计 **2.5–3 × 10^8 次索引项删除 + ~3 × 10^7 次堆元组删除**。
即便按**乐观的 1µs/索引项**（GIN 条目远不止），也已经是 **250–300 秒**；
按 3µs 则 750–900 秒。**结论不依赖精确行数：180s 大概率仍不够**，
配合上面那条同库 IO 锚点（835 万边冷扫 39 分钟）更是如此。
⚠ 与 30s 时代的差别只是「撞墙推后 6 倍」——每次失败前多烧 6 倍的 WAL 与 xmin 停滞。
而**即便它跑得完，3 分钟的写事务也仍是 W1 要消灭的东西**（摸底 3）。

⚠ **FK 探查不一定是索引查找**。PostgreSQL 不会为引用列自动建索引；逐条核对 47 条
L1 FK 后，**只有 `agent_access_tokens.default_notebook_id` 没有前导索引**
（该表唯一的索引是 `idx_agent_tokens_profile ON (agent_profile_id, revoked_at,
expires_at)`，`0003_core_indexes.sql:4`）——所以今天每一次 `DELETE FROM notebooks`
都要对 `agent_access_tokens` 做一次**全表 Seq Scan**。
（`notebook_grants` 曾疑似同类，实为误判：`uq_notebook_grants_principal UNIQUE
(notebook_id, principal_type, principal_id)`，`0027_group_sharing.sql:76-77`，是
notebook_id 前导唯一索引。）由 `0047` 补索引（§1.4）。

### 1.2 三种代价

- **事务时长（按 D-1 = 180s 重述）**：大库上跑满 180 秒后 abort 回滚，回滚再付一份
  同量级代价，下次重试从零开始——「永远删不掉，但每次都很贵」。
  中等库可能在 180s 内跑完，那就是一个**长达 3 分钟的写事务**，下面两项代价照付。
- **锁面**：除级联行锁外，围栏对 `sources`(48 836) / `ask_jobs` / `reports` /
  `source_paper_meta` 逐行取排他锁并持到提交（最长 3 分钟）。主要伤害在 **xmin horizon**：
  分钟级事务让全库 autovacuum 在 484GB 上原地踏步。
- **连接**：一次删除独占一条写连接**整程（最长 3 分钟）**。
  `POSTGRES_POOL_MAX_SIZE` 默认 10（`config.py:1269`；台账建议生产 ≥23），
  重/轻维护池各 4，`SEARCH_CONCURRENCY_LIMIT` 4，`SCALE_BUILD_CONCURRENCY` 2，
  加上 D-2 新增的 `NOTEBOOK_DELETE_CONCURRENCY`（1–2）。
  ⚠ 180s 预算把这项代价也放大了 6 倍：两个并发删除即可让写连接被占满 3 分钟。

### 1.3 表分类（可分页性）

判据是「能否被 `notebook_id` 前导索引（或 PK 前导）直接分页」。65 张闭包表：

- **A 类（52 张，可自取页）**：`knowledge_objects`、`knowledge_relations`、
  `concept_clusters`、`chunks`、`sources`、`knowledge_embeddings`、`relation_embeddings`、
  `chunk_embeddings`、`element_embeddings`、`memory_items`、`extraction_runs`、
  `communities`、`concept_comentions`、`concept_merge_candidates`、`canonical_relations`、
  `mention_edges`、`kg_community_edges`、`kg_source_profiles`、`kg_analysis_artifacts`、
  `chunk_elements`、`chunk_questions`（`0022:18`）、
  `kg_relation_completion_state`（`0012:20`）、`feedback`（`0003:18`）、
  **`notebook_grants`**、`knowledge_source_facts`、`knowledge_source_fact_elements`（`0039:122`）、
  `knowledge_source_fact_backfills`、`promotion_candidates`、`reports`、`answers`、
  `ask_jobs`、`notebook_assets`、`source_authors`、`source_paper_meta`、
  `source_index_backfills`、`chunk_element_backfills`、`agent_observations`、
  `agent_notebook_profile`、`agent_profile_jobs`、`agent_token_notebooks`、`catalog_jobs`、
  `catalog_candidates`、`indexing_pipeline_stages`、`kg_build_jobs`、
  `kg_conflict_candidates`、`kg_rebuild_checkpoint`、`knowhow_tables`、`merge_review_jobs`、
  `notebook_bases`、`notebook_members`、`notebook_object_schemas`、
  `notebook_share_requests`、`unified_kg_state`。
  ⚠ **`notebook_grants` 从 v2 的 B 类改判 A 类**：实测有
  `uq_notebook_grants_principal UNIQUE (notebook_id, principal_type, principal_id)`
  （`0027_group_sharing.sql:76-77`），是 notebook_id 前导唯一索引，v2 的「无 nb 索引」为假。
  ⚠ 但它**不需要**第二循环形：其 PK 是单列 `id`（`0027:75`），走第一循环形即可。
  （此处与评审给的「它也在复合键名单里」不同，以 DDL 为准。）
  记账：A 类 52 = v2 的 51 + `notebook_grants`。
- **B 类（13 张，须经父表）**：`source_elements`（仅 `source_id`）、`ask_trace_steps`、
  `memory_embeddings`、`memory_provenance`、`memory_revisions`、
  `knowhow_changes`/`knowhow_columns`/`knowhow_milestones`/`knowhow_rows`（经 `table_id`）、
  `knowhow_cell_code`/`knowhow_cells`（L3，经 `column_id` **与** `row_id` 双父）、
  `indexing_pipeline_stage_sources`（经 `job_id` **与** `source_id` 双父）、
  `agent_access_tokens`（经 `default_notebook_id`）。
  删除单位是「一页父键」，同一事务内自底向上删完该页的整棵子树。**删除序显式化**：
  - `knowhow_tables` 一页 → `knowhow_cells` + `knowhow_cell_code`（按该页 table_id 下的
    `column_id ∪ row_id` 两组，先删两张叶）→ `knowhow_columns` + `knowhow_rows`
    → `knowhow_changes` + `knowhow_milestones` → `knowhow_tables` 本页；
  - `indexing_pipeline_stages` 一页 → `indexing_pipeline_stage_sources`（按 `job_id ∈ 本页`）
    → 本页；⚠ 该子表另有 `source_id` 父，故它必须在 `sources` 之前清完；
  - `sources` 一页 → `source_elements` → `element_embeddings` → 本页（相位 5，见 §T-3）；
  - `ask_jobs` 一页 → `ask_trace_steps` → 本页（`ask_trace_steps` 相位 3，
    `ask_jobs` 本体相位 5）。
- **闭包外补删（6 张）**：`knowledge_embeddings`（`0004:13`）、
  `community_members`（`0004:8`）、`knowledge_object_sources`（`0004:25`）、
  `kg_cluster_scratch`（`0004:12`）、`kg_canonical_scratch`（`0008:12`）、
  `conversations`（**无 `notebook_id` 索引**，见下）。
- **刻意不删（2 张）**：`object_schemas`、`retained_user_activity`（摸底 10）。

**`agent_access_tokens` 的语义登记**：FK 是 `default_notebook_id ON DELETE CASCADE`
（`0001:709-712`）——删一个库连带删掉**整个 token 行**。既有语义，本批不改，列残余债；
但它**缺索引**这件事本批必须修（§1.4）。

### 1.4 `0047` / `_migration_68` 并入的三条索引

三条都不是「优化」，是让本设计的某一步从「全表扫」变成「索引查」的**前提**：

| 索引 | 为谁而建 | 不建的后果 |
| --- | --- | --- |
| `idx_agent_tokens_default_notebook ON agent_access_tokens(default_notebook_id)` | **相位 5** 的 `DELETE FROM notebooks` FK 级联探查 | 每次删库对 `agent_access_tokens` 全表 Seq Scan（§1.1） |
| `idx_knowhow_cell_code_column ON knowhow_cell_code(column_id)` | **B 类** knowhow 链的 `column_id` 腿（该表今天只有 `idx_knowhow_cell_code_row ON (row_id)`，`0005_memory_knowhow_governance_indexes.sql:2`；姊妹表 `knowhow_cells` 两条腿都有，`0005:3,4`） | 逐父行对 `knowhow_cell_code` 全表扫，B 类整条链退化 |
| `idx_conversations_notebook ON conversations(notebook_id, id)` | **闭包外补删** `conversations`（今天只有 `created_by`（`0003:13`）与 `share_token`（`0030:49`）） | 形二的内层 `LIMIT` 每批从块 0 重扫 → O(N²)（§1.5 前置条件） |

⚠ 三条都要按 `0042_hotpath_batch2_search_indexes.sql` 的守卫 DO 先例写
（`IF NOT EXISTS` + 同名先存索引按语义维度校验），并同步进
`scripts/build_hotpath_indexes.py` 的离线通道（批 1 建立的先例：迁移与离线脚本
两条路都能建，互为 no-op）。`conversations` 的替代方案（经 `created_by` 反查、
或无界单条删除）已否：前者跨库放大，后者正是本设计要消灭的形态。

### 1.5 两种批删循环形（P1-A）

**形一（单列唯一键，43 张表）**：keyset 取一页 PK → `DELETE ... WHERE <pk> = ANY(%s)
AND notebook_id = %s`。这是摸底 6 的实测形（5.3ms/5000 行），已有权威成本模型。

**形二（无单列唯一键，22 张表）**：`= ANY` 拿不到可寻址的单列键。逐表实测确认，
**下列 22 张闭包/补删表没有单列唯一键**（PK 为复合，或**根本没有 PK**）：

| 表 | PK | 量级 |
| --- | --- | --- |
| `knowledge_object_sources` | **无 PK** | 千万级 |
| `community_members` | **无 PK** | 千万级 |
| `kg_cluster_scratch` | **无 PK** | 9M 级 |
| `kg_canonical_scratch` | **无 PK** | 9M 级 |
| `canonical_relations` | `(notebook_id, canonical_src, edge_type, canonical_tgt)` | 大 |
| `mention_edges` | `(notebook_id, claim_object_id, concept_canonical_id)` | 大 |
| `concept_comentions` | `(notebook_id, canonical_a, canonical_b)` | 大 |
| `chunk_elements` | `(notebook_id, element_id, chunk_id)` | 大 |
| `kg_community_edges` | `(notebook_id, src_community_id, dst_community_id)` | 中 |
| `kg_rebuild_checkpoint` | `(notebook_id, input_version, stage, item_key)` | 中 |
| `knowledge_source_fact_elements` | `(fact_id, element_id)` | 大 |
| `kg_relation_completion_state` | `(source_id, source_generation, mode)` | 中 |
| `indexing_pipeline_stage_sources` | `(job_id, source_id)` | 中 |
| `ask_trace_steps` | `(job_id, seq)` | 中 |
| `kg_source_profiles` | `(notebook_id, source_id)` | 小 |
| `kg_analysis_artifacts` | `(notebook_id, kind)` | 小（≤5） |
| `agent_notebook_profile` | `(notebook_id, owner_id, label)` | 小 |
| `agent_profile_jobs` | `(notebook_id, owner_id)` | 小 |
| `agent_token_notebooks` | `(token_id, notebook_id)` | 小 |
| `notebook_bases` | `(notebook_id, base_notebook_id)` | 小 |
| `notebook_members` | `(notebook_id, user_id)` | 小 |
| `notebook_object_schemas` | `(notebook_id, object_type)` | 小 |

**形二 = ctid 形（选定）**：

```sql
DELETE FROM t
 WHERE ctid = ANY(ARRAY(SELECT ctid FROM t WHERE notebook_id=%s LIMIT %s))
```

优点：无需游标（每批重跑内层 SELECT，上一批已删所以自然前进）、天然幂等
（重放时内层查出的是当前存活行）、外层是 Tid Scan（O(页)，不可能退化成范围扫，
与摸底 6 否掉键区间的理由同源）。

⚠ **终止条件必须是 `rowcount == 0`，不是 `rowcount < 批大小`。**
READ COMMITTED 下若某行在 `ARRAY(...)` 求值之后、`DELETE` 命中之前被并发 UPDATE，
该 ctid 上的元组不再是当前版本，EPQ 重检查会跳过它——于是 `rowcount` 可以小于数组
长度而表中仍有大量剩余行，按 `< 批大小` 终止会**静默漏删**。
本设计下并发写本应已被 tombstone + `quiesce` 挡净，所以这是纵深防御；
但正因为它是「本不该发生」的情形，用宽松的终止条件等于把一个静默漏删藏进
最不可能被发现的路径。配套：连续 N 轮（默认 3）`rowcount == 0` 但计数仍不为零时
响亮失败（防御「全部行被外部长事务锁住」这种理论上的活锁），不静默转下一张表。
变异钉进 T-5a 用例（§7 G1）。

**注意面（必须写进实现注释与测试）**：
- **ctid 绝不可跨语句/跨事务缓存**。上式的 `ARRAY(...)` 子查询与 `DELETE` 在**同一条
  语句、同一快照**内，这是它正确的全部依据。把 ctid 取出来存到 Python 再删 = 错。
- `VACUUM FULL` / `CLUSTER` 会重写 ctid，但它们取 `AccessExclusiveLock`，
  与本语句互斥，不可能在语句执行中途发生。普通 `VACUUM` 不移动存活行。
  所以 **ctid 漂移只影响「跨语句缓存」这种被禁止的用法，不影响本形的正确性**。
- **前置条件：该表必须有 `notebook_id`（或所用父键）前导索引**，否则内层
  `SELECT ... LIMIT n` 每批都从第 0 块重新 Seq Scan，整趟清扫退化成 O(N²)。
  上表 22 张里除 `conversations`（已在 §1.4 补索引）外全部满足
  （`knowledge_object_sources` `0004:25`、`community_members` `0004:8`、
  两张 scratch `0004:12`/`0008:12`，其余为 PK 前导）。
- **SQLite 对等形**：`DELETE FROM t WHERE rowid IN (SELECT rowid FROM t
  WHERE notebook_id=? LIMIT ?)`。实测**全仓无 `WITHOUT ROWID` 生产表**
  （`git grep "WITHOUT ROWID"` 只命中 `backend/app/migration/shadow/verifier.py`
  的临时表），故 `rowid` 普遍可用。SQLite `VACUUM` 会为无 INTEGER PRIMARY KEY 的表
  重编 rowid，但同样只在离线维护 CLI 里跑，且同语句快照规则相同。
- **无既有实测**：摸底 6 的 5.3ms/201ms **只覆盖形一**。形二的 EXPLAIN 与计时
  必须列进 T-0 与 G3（见 §7）。

**scratch 两张单独归类**：`kg_cluster_scratch` / `kg_canonical_scratch` 的语义是
「本次聚类运行的临时行」，`recover_interrupted_jobs`（`postgres/maintenance.py:681-689`）
已经在崩溃恢复里 `TRUNCATE` 它们**整表**。删单个库时不能 TRUNCATE 全表，
但它们无 PK 且行数巨大，正是形二的典型用例；同时登记：若将来确认
「删库时这两张表本就该整表清空」（单 worker 部署下聚类不并发），可退化为 TRUNCATE，
本批不做该假设。

**批大小与退避**：形一起始 500（房规），A 类大表可配到 2000；形二起始 500，
按 T-0 的 Tid Scan 实测调。**批间不 sleep**——`bulk_write`
（`postgres/database.py:543`）明文 PG 侧不需要进程侧节流；节流靠「每库作业并发 1 +
全局清理并发上限」。若 T-0 显示复制延迟被推高，再引入
`NOTEBOOK_DELETE_BATCH_PAUSE_MS`（默认 0，新参数）。

---

## §2 目标形：作业化 + 代次化标记

### T-1 可见性谓词单点化（先决，独立可合）

把 **40 处读侧谓词**折成每后端一个常量（例如 `postgres/access_sql.py` /
`sqlite/access_sql.py` 各导出 `NOTEBOOK_LIVE_SQL = "status NOT IN ('copying','deleting')"`），
全部读侧站点引用它。

⚠ **写侧 6 处哨兵与生产者 1 处不进常量**（摸底 5）。守卫规格定稿如下。

**扫描范围**：`backend/app/**.py` + `scripts/**.py`。
**`backend/tests/**` 不扫**——测试里出现该字面量恰恰是它在断言这条口径
（如 `backend/tests/test_admin_user_notebooks.py:77` 的 docstring），扫它只会制造噪音。

**豁免机制（二选一，定稿选前者）**：
- ✅ **规则式：AST 扫描，只看非 docstring 的字符串字面量。**
  用 `ast.walk` 收集 `ast.Constant(str)`，跳过 Module/ClassDef/FunctionDef 的首语句
  （docstring）；注释根本不进 AST，天然免扫。
  **好处：零豁免清单**。本仓库的注释/docstring 是长篇散文体（这正是房规），
  枚举式清单会在每次注释改动时假红，维护成本高于它买到的确定性。
- ❌ 清单式：枚举 40 处站点 + 6 行散文豁免。否决理由同上；
  且一旦选它，40 处清单本身要进规格附录并随每次行号漂移更新——
  规格会变成一份必然过时的行号表。
  （若评审坚持清单式，则本节改为「附录 A：40 处站点清单」，
  并接受它是一份需要随 trunk 维护的活文档。）
  逐行核实：AST 规则能自动排掉的散文共 **6 行**——
  `postgres/access_sql.py:57`、`sqlite/access_sql.py:106`、`sqlite/mount_sql.py:101`、
  `api/admin_routes.py:472`（docstring）、`api/admin_routes.py:479`（注释）、
  `tests/test_admin_user_notebooks.py:77`（docstring，且在扫描范围外）。

**读侧守卫**：常量之外出现 `status != 'copying'` / `status <> 'copying'` 即失败。
覆盖 `backend/app/repositories/` 的 40 处（PG 20 / SQLite 20）。

**写侧守卫**：`status='copying'` 等值形**只允许**出现在两个 `sharing_store.py` 的
`compensate_copy` / `sweep_stale_copies` 共 6 处白名单内；白名单外出现即失败，
白名单内出现 `'deleting'` 也失败。

**`scripts/diag_db.py` 的处置（登记）**：该文件有**两处真谓词**——
`:1530`（`WHERE tier!='base' AND status!='copying'` 的库枚举）与
`:1543`（`WHERE status!='copying' ORDER BY id LIMIT 1` 的样本库选取）。
它是只读诊断，**不豁免，两处都改**：
- `:1543` **必须**排除 `deleting`——否则诊断可能挑中一个正在被拆解的库，
  跑出一份毫无意义的基线；
- `:1530` 一并改，保持两处口径一致（一个文件里两份拼写是本仓库反复批评的形态）。
若 `scripts/` 无法干净导入该常量，则在 `scripts/diag_common.py` 里放一份**由守卫
校验与 `access_sql` 常量逐字相等**的副本，而不是让它漂。

**覆盖说明（非违规，但要点名）**：`services/notebook_sharing.py:88` 的
`status="copying"` 是**生产者**（Python kwarg 非 SQL），两条守卫都不管它；
在守卫的模块 docstring 里点名，防止后来者以为漏了一类。

**`include_copying` 参数的裁决**：`get_row(..., include_copying=True)`
（`postgres/notebook_store.py:246-249`、`sqlite/notebook_store.py:275-283`）加了
`deleting` 之后名实不符。实测**全仓零 `include_copying=True` 调用点**（只有 docstring
提及）。**PR-1 直接删除该参数**；若测试依赖则改名 `include_hidden_lifecycle`。

本步单独做时**行为零变化**（`deleting` 还没有任何行），先合、先审、先进 CI。

#### T-1.1 授权谓词并入（codex #653 R2，PR-1 同批补齐）

**发现**：§4.1 互斥矩阵原表述「`deleting` 后入口 `get_notebook` 已 404 → 谓词即闸」
只对**目录寻址**成立——`GET /api/notebooks/{id}` 一类端点先经 `get_notebook`，
`NOTEBOOK_LIVE_SQL` 已经把它挡住。但**直连资源端点**（`/sources/{id}`、
`/elements` 等，路径里不带 notebook_id、不经过 `get_notebook`）走的是
`deps.require_notebook_capability` → `sharing_store.user_can_read_notebook` /
`user_can_admin_notebook` / `user_can_access_notebook` → 两个后端
`access_sql.py` 的 `NOTEBOOK_READ_SQL` / `NOTEBOOK_ADMIN_SQL` /
`NOTEBOOK_WRITE_SQL` 这三条独立谓词——T-1 折叠 40 处读侧站点时**没有覆盖它们**，
它们在折叠前后都没有任何生命周期过滤。对 `deleting`（尚无任何行）是理论缺口；
对**今天已存在的 `copying`** 是**真实的既有不一致**：半拷贝哨兵库理应「还不算
存在」，但直连资源端点从未把它挡住。

**修法**：两个后端的 `NOTEBOOK_WRITE_SQL` / `NOTEBOOK_ADMIN_SQL` /
`NOTEBOOK_READ_SQL` 各自追加 `AND NOTEBOOK_LIVE_SQL`（`WRITE` 无表别名故裸引用，
`ADMIN`/`READ` 用 `nb.` 前缀）。⚠ **加在这三条最终常量上，不折进
`read_access_clause()` / `admin_access_clause()` 内部**：那两个函数还喂
Memory 读查询（`memory_store._read_access_clause` 及至少两处直接调用）、
`group_store` 的 `_notebook_name` 列投影等更大范围的消费者，折进共享子函数会把
改动面扩大到本轮未逐一审查的地方。写权（owner-only，`created_by=%s`）本就独立
成句，不经过任何 clause 函数，直接追加。三条都不新增字面量拼写——单点引用
既有的 `access_sql.NOTEBOOK_LIVE_SQL`（T-1 已建），符合 T-1 的单点纪律；
`test_access_sql_contract.py` 的双后端 parity 守卫（占位符方向、public 符号集合、
`NOTEBOOK_ADMIN_SQL` 逐字内含受限三臂等结构断言）全部复核仍绿——矩阵夹具用的
`status='draft'`，从未触达 `copying`/`deleting`，故这批既有断言本身不需要改。

**依赖排查（先查后动，未发现依赖）**：`NotebookCopyService.copy_notebook`
（`services/notebook_sharing.py`）全程走 **store 层**写（`insert_copy_rows` 等），
从不对目标（正在 `copying` 的）笔记本调用 `user_can_*`/`NOTEBOOK_*_SQL`；唯一
的 API 入口 `POST /shared/{token}/copy`（`notebook_routes.py:294-311`）是
**同步**处理——`copy_notebook()` 跑完（状态已翻回正常）才把 `NotebookSummary`
返给客户端，调用方在拷贝完成前无法得知新 notebook id，因而不存在「客户端在
`copying` 窗口内直连访问新库」的路径。全仓搜索确认没有测试对 `copying` 状态的
笔记本断言 `user_can_read_notebook`/`user_can_admin_notebook`/
`user_can_access_notebook` 为真（PG 会话层 `test_core_store_conformance.py`
用到的 `sharing.notebook_row` 是无条件裸取行的诊断方法，不经过这三条谓词，
不受影响）。

**行为面守卫扩展**：`backend/tests/test_notebook_lifecycle_visibility.py` 与
`tests/postgres/test_notebook_lifecycle_visibility_pg.py` 新增
`test_direct_resource_authorization`：为 active/copying/deleting 三本库播种
owner 与只读成员两类主体，断言 `user_can_read_notebook` /
`user_can_admin_notebook` / `user_can_access_notebook` 在 active 上按权限矩阵
放行、在 copying/deleting 上三权皆否；变异（去掉追加的 `AND NOTEBOOK_LIVE_SQL`
合取）已实测使对应断言变红。

**Memory 的登记（不改，理由如下）**：Memory 的读路径不经过上述三条谓词，而是
`memory_store.py` 自己的 `m.created_by=%s AND <owner∨成员∨授权边>` 组合
（`_read_access_clause` 私有薄封装 + 至少两处**直接**调用模块级
`read_access_clause()` 且各自换了不同的表别名——`memory_for_user` 之外，
`create_answer_with_initial_revision` 的来源答案访问校验、
`validate_promotion_approval_access_on` 的晋升审批校验都是独立形态）。
`GET /memories/{memory_id}` 同样是直连端点、不
经过 `get_notebook`，理论上与本条同类缺口；但权衡后**本批不动**，登记到批 3
删库重造（T-2/T-3 落地、`deleting` 真正开始产出行的那个 PR）一并处理，理由：
① 语义上限定于**自己**——`m.created_by=%s` 已经把可读范围收紧到「这条 Memory
的作者本人」，即便笔记本进入 deleting，暴露面也只是作者读回自己写过的内容，
不是任何跨用户的读权/写权穿透，风险量级与直连资源端点（任意被授权主体）不同；
② 正确的折叠需要同时改掉 `_read_access_clause` 私有封装与至少两处直接调用
`read_access_clause()` 的独立形态（两个后端各三处、共六处，且各自表别名不同），
是比本条三常量修法更大的改动面，理应有自己的一轮评审与 Memory 专项测试覆盖，
不宜在本 PR 尾部顺带塞入；③ 今天 `deleting` 尚无任何行，`copying` 状态下
Memory 本就不构成新增暴露（半拷贝库的 Memory 行本身也还没拷贝完整），推迟到
真正产出 `deleting` 行的那个 PR 处理不产生窗口期风险。

### T-2 tombstone 状态机

`notebooks.status` 增加取值 `deleting`（**不加列**）。不需要 `deleted`（清理完成即物理删行），
不需要 `deleted_at`（权威来源已是 `retained_user_activity.deleted_at`，摸底 9；
扫尾 cutoff 用 `updated_at`，与 `sweep_stale_copies` 用 `created_at` 同形）。

迁移 `0047` / `_migration_68` + `SCHEMA_VERSION 68`。「立即返回」的那一笔：

```
UPDATE notebooks SET status='deleting', updated_at=now()
 WHERE id=%s AND status<>'copying' AND status<>'deleting'
```

单行 UPDATE，微秒级；rowcount≠1 → 404/409（CAS 形照 `set_indexing_pipeline_desired`，
`postgres/notebook_store.py:330-351`）。同事务写一行删除作业。
API 由 204 改 **202 + `{"status":"deleting"}`**。

### T-3 删除作业：六相位

**作业行**：新表 `notebook_delete_jobs`，形照 `kg_build_jobs`，列
`id, notebook_id, status('queued'|'running'|'waiting'|'failed'), phase, cursor_table,
cursor_key, deleted_rows, error_code, error_message, created_at, updated_at, finished_at`，
外加单飞用的部分唯一索引 `WHERE status IN ('queued','running','waiting')`（照 `0002:22`）。
侧表 `notebook_delete_files(job_id, ordinal, file_path)`（相位 1 用）。

⚠ **两张新表都不 FK 到 `notebooks`**。v2 给的理由（「清理器要在 notebooks 行删掉之后
再删作业行」）在 v3 已不成立——相位 5 是单事务，FK 级联在那里无害。**更新后的理由**：
扫尾的第二条驱动要能识别「**作业行在、`notebooks` 行不在**」这个状态并**补完残渣**
（见下）。若有 FK 级联，该状态在数据面上不可表达，一次带外的 `notebooks` 行删除
（旧路径残留、`sweep_stale_copies` 误吞、DBA 手工删）就会把「清理未完成」这个事实
连同作业行一起抹掉，孤儿行与磁盘产物永久留下且无人知晓。

| # | phase | 做什么 | 事务形态 / 不变量 |
| --- | --- | --- | --- |
| 0 | `mark` | T-2 的 CAS + 建作业行 | 单事务，微秒级 |
| 1 | `paths` | **分页物化全量 `sources.file_path`** 到 `notebook_delete_files` | 每页一事务；必须在任何 `sources` 行被删之前完成 |
| 2 | `quiesce` | 轮询「该库无 running 的 `kg_build_jobs`」 | 只读点查；超时转 `waiting` |
| 3 | `rows` | 分批清理 **61 张闭包表（65 − 相位 5 的 4 张）+ 6 张闭包外补删表**；形一/形二按 §1.5 分派 | 每页一事务；每批前 `verify_held()`；游标同事务写回 |
| 4 | `files` | 按侧表删来源文件、删资产目录、删 scale 三根及三种兄弟目录 | 无事务；失败只记账 |
| 5 | `finalize` | **单事务**：四表围栏 `FOR UPDATE` → 归档投影 → 删四表行 → `DELETE FROM notebooks` → 删侧表 → 删作业行 | 见 §T-3.2 |

#### T-3.1 相位 1：`paths` 必须全量物化（P1-B）

分批之后 `sources` 行会先于文件删除而消失，路径就永久丢了。
v2 曾推荐「删每页 `sources` 前先取该页 `file_path` 同事务删文件」——**该方案崩溃不安全**：
页删行一旦提交、进程随即消失，那一页的文件路径再也查不回来，
G3 的「磁盘零残留」断言不可达。

**裁决：采用全量物化。** 相位 1 分页读 `sources.file_path` 写入
`notebook_delete_files`，每页一事务，读游标即 `sources` 的 keyset 游标。
成本：48 836 条窄行的一次拷贝——对一个本就要删掉数千万行的作业是噪声
（v2 说「不新增表、不物化 48 836 条路径」的成本论据不成立）。
侧表行在**相位 5 单事务内**随作业行一并删除；相位 4 只读它。

#### T-3.2 相位 5：单事务，且只剩 O(来源数)（P0-A）

摸底 2 已证：没有任何机制能让「在途作业稍后写一行 `ask_jobs`/`sources`」在分批世界里
被挡住。唯一有效的手段就是 trunk 已有的那个手段——**把围栏、归档、和这四张表的
行删除放进同一个事务**。

相位 5 单事务内容（顺序即 trunk 的顺序，逐条对应 `postgres/notebook_store.py:365-411`）：
1. `SELECT id FROM notebooks WHERE id=%s FOR UPDATE`（聚合根锁；行已不在则整个相位
   转「补完残渣」，见 T-4）；
2. 对 `ask_jobs` / `sources` / `source_paper_meta` / `reports` 逐行 `FOR UPDATE`；
3. `_retain_user_activity_before_delete` 的三段投影（ask / source / report）
   —— ⚠ **三段回到同一事务即同一快照**，v2 把它们拆成分页多事务时丢掉的
   「三段单快照一致性」在此自动恢复；
4. 删这四张表的行（其 L2 子级 `source_elements` / `element_embeddings` /
   `ask_trace_steps` / `indexing_pipeline_stage_sources` 已在相位 3 清空，
   所以子表探查全部零命中——但**探查本身照付**，见下表 ④）；
5. `DELETE FROM knowledge_embeddings`（相位 3 已清，这里是兜底空删）；
6. `DELETE FROM notebooks`（65 表级联，此时全部为空，付 **47 次 L1 FK 探查**）；
7. 删 `notebook_delete_files` 本作业的行、删作业行。

**时长上界论证（O(来源数) 而非 O(对象数)）**：

| 步骤 | 工作量 | 量级估算 |
| --- | --- | --- |
| ② 围栏 `FOR UPDATE` | ~50k 窄行 × 1 次元组头更新，走 `(notebook_id,…)` 索引 | 0.05–0.5s |
| ③ 归档投影 | 2 条 GC DELETE + 3 段 INSERT；source 段是 **48.8k 行 `execute_many` + `ON CONFLICT DO UPDATE`**，落进 `retained_user_activity` 的 **5 条索引** ≈ **24 万索引项插入** + 堆 | **1–4s（主成本之一）** |
| ④ 删四表 | `sources` 48.8k × **10 索引** ≈ **49 万索引项删除** + 堆；**外加 48.8k × 14 个子表 FK ≈ 68 万次子表探查**（`sources` 的子表实测 14 张：`chunks`/`chunk_questions`/`source_elements`/`extraction_runs`/`knowledge_relations`/`knowledge_source_facts`/`knowledge_source_fact_elements`/`knowledge_source_fact_backfills`/`source_authors`/`source_paper_meta`/`catalog_jobs`/`catalog_candidates`/`kg_relation_completion_state`/`indexing_pipeline_stage_sources`）；另加 `ask_jobs`(4 索引)、`reports`(3)、`source_paper_meta`(2) | **1.5–5s（主成本之二）** |
| ⑤⑥ 兜底 + 级联 | 47 次 L1 FK 探查，其中 `agent_access_tokens` 今天是**全表 Seq Scan**（§1.1）——`0047` 补索引后归零 | 补索引前可达百毫秒级；补后毫秒级 |

⚠ **v3 写的「65 次零命中索引探查，毫秒级」被证伪，本表已改准**：
它既数错了（是 47 条 L1 FK，不是 65 张表），也漏掉了④那 68 万次
per-source 子表探查，还假设了所有探查都有索引可走（`agent_access_tokens` 没有）。

合计 **3–10 秒量级**（保守，实测前不收窄）。
按 **D-1 = 180s** 的生产预算，这是 **18–60 倍裕度**；
相对今天的 O(对象数=878 万) 形态减少约 **180 倍**（48 836 / 8.78M）。

⚠ **v5 的口径修正**：v4 在 30s 预算下把 10s 称作「压在触发线上」，那句话随 D-1 作废。
180s 下相位 5 的**超时风险已不是主要矛盾**；真正要守的是设计目标本身——
**W1 要消灭的是长写事务，不是「让它勉强不超时」**（摸底 3 的定性②）。
因此触发线**不按预算的分数取，按目标取**：

**触发线 = 实测 > 30 秒**。取 30 不是因为预算是 30（生产是 180），
而是因为一个超过 30s 的相位 5 已经把 xmin 停滞与连接占用带回来了（§1.2）——
那时该回来改设计（例如把归档投影的 `execute_many` 再优化，或重新审视围栏范围），
**而不是调大一个数**。

**兜底旋钮（D-4，语义已随 D-1 翻转）**：
在相位 5 的事务开头执行 `SELECT set_config('statement_timeout', %s, true)`
（第三参 `true` = 事务局部），值取 `NOTEBOOK_DELETE_FINALIZE_TIMEOUT_SECONDS`
（默认 `0` = 不设置，沿用池的 180s）。先例与写法照 `postgres/database.py:152-166`。
⚠ **它现在是一个「收紧」旋钮，不是「放宽」旋钮**：生产预算 180s 已经远大于
相位 5 的 3–10s，所以任何有意义的设定值都比池的默认**更短**——
它的用途是给这一个我们真正在意的事务**加一道显式的、比池更严的上界**，
让「相位 5 跑飞了」在几十秒内响亮失败，而不是拖满 3 分钟。
三条硬约束见 D-4。

**归档口径不变**：`deleted_at` 仍是「归档时刻」= 相位 5 的时刻。
分批化把「删除请求时刻」与「归档时刻」拉开了（相位 0 → 相位 5），
保留窗口（`USER_ACTIVITY_RETENTION_DAYS`）从后者起算，与今天一致，
但**必须写进文档面**（§8）：管理员看到的 `deleted_at` 可能晚于用户点删除数十分钟。

**`_retain_user_activity_before_delete` 开头两条 GC**
（`DELETE ... WHERE expires_at<=%s`、`DELETE ... WHERE notebook_id=%s`，
`postgres/notebook_store.py:420-429`）留在相位 5 内：前者行数由**全局过期行数**界住，
可能不小——**登记**：若 T-0 显示它超预算，把「过期 GC」摘出来做成独立的周期作业
（它与本次删除无因果关系，只是搭了顺风车），相位 5 只保留 `WHERE notebook_id=%s` 那条。

#### T-3.3 相位 2：`quiesce`（P1-9）

摸底 7 证实：三类 KG 维护作业**都不取 advisory 锁**，与删除零互斥；4.2 的检查点
只保证「它会停」，粒度是一批/一个阶段，而批内 LLM 抽取可达数分钟。所以必须有显式相位闸。

⚠ **v3 的闸只查 `kg_build_jobs`，对 3 个对手里的 2 个形同虚设**（摸底 7b）：
`relinkkg-` 与 `unifiedkg-` 走 `KgMaintenanceJobs` 的**纯进程内字典**，
一行 `kg_build_jobs` 都不写。v4 的闸查**两套簿记**：

| 腿 | 查什么 | 覆盖谁 | 性质 |
| --- | --- | --- | --- |
| A（durable） | `SELECT 1 FROM kg_build_jobs WHERE notebook_id=%s AND status='running'`，走 `idx_kg_build_jobs_one_running`（`0002_integrity_indexes.sql:22`）——索引点查 | `buildkg-` / `rebuildkg-` | 跨进程正确 |
| B（进程内） | `KgMaintenanceJobs.jobs[notebook_id]["status"] == "running"`（读走它自己的 `lock`，`services/kg/maintenance_jobs.py:68-69`） | `relinkkg-` / `unifiedkg-` / `conflictresolve-` | **只对本进程有效** |

**腿 B 的依赖声明（必须显式写进实现注释与 PR）**：它只在
「**生产钉 `--workers 1`，进程内所有权即部署契约**」这个前提下成立。
该前提不是本设计新引入的——`services/kg/maintenance_jobs.py:60-67` 的模块注释
已经把它登记为这三类作业**单飞机制本身**的前提
（「Production runs one worker, so process-local ownership is the deployment
contract」），checkup 的租约抑制与 H4/H5 缓存也建在同一前提上。
本设计**沿用**该前提，不加强也不削弱；但要在两处点名：删除作业的相位 2 注释、
以及 `docs/operations.md` 的多 worker 警告段（那里已有同类声明，追加一条）。
⚠ 若将来上多 worker，腿 B 失效——届时的正解是把这三类作业的 claim 提升为 durable
行（与 `kg_build_jobs` 合流或另建表），**而不是**给删除加锁；登记进残余债。

时序（四者配合）：
1. 相位 0 的 CAS 把 `status` 翻成 `deleting`；
2. 在跑的 `rebuildkg-` 在**下一个批边界**（`knowledge_lifecycle.py:3176`）读到
   `deleting` → `control.abort(...)` / `KgBuildAborted` → 结算，
   `kg_build_jobs.status` 离开 `running`；
3. 在跑的 `relinkkg-` 在**下一个来源边界**（`knowledge_lifecycle.py:1409` 的
   `for source_id in self._relink_source_partitions(...)`）、
   `unifiedkg-` 在**下一个阶段边界**（`knowledge_lifecycle.py:4389-4395` 的 `_stage`，
   10 个调用点共用）读到 `deleting` → 抛出 → `run_*_job` 既有的
   `except Exception: self.settle(..., "failed")`（`maintenance_jobs.py:144-152` /
   `:178-186`）把进程内字典结算掉；
4. 删除作业在相位 2 轮询**两条腿都为空**才进相位 3。

⚠ 第 3 步的检查点是**新增代码**（今天这两条路径一个检查点都没有），
排进 PR-3，与 4.2 选项 A 同款：读 `notebooks.status`，见 `deleting` 即抛。
落点选在 `relink_notebook_kg` 的逐源循环与 `rebuild_unified_kg` 的 `_stage` 里
——后者一处改动即覆盖 10 个阶段边界，是全篇成本最低的插入点。

节奏与超时：初始 5s，指数退避至 60s 上限，总超时
`NOTEBOOK_DELETE_QUIESCE_TIMEOUT_SECONDS`（默认 1800——覆盖「一批 LLM 抽取」的最坏
时长并留裕度）。超时 → 作业置 `waiting` 并交回，由扫尾按正常节奏重排；
**绝不强行进相位 3**（那正是「一边写一边删」的窗口），
也**绝不**记进 scale 退避（理由同 §4.3）。
⚠ `waiting` 与 `queued` 分开是为了运维可分辨「排队等槽」与「等重建停」；
日志要写明**是哪条腿**在挡（durable 还是进程内），否则运维在 `kg_build_jobs` 里
查不到任何 running 行却看到删除停着，无从下手。

#### T-3b 相位 4：磁盘产物

删除三根及其**三种**兄弟目录（摸底 11）：
`{storage_dir}/kg_index/{nb}`、`{storage_dir}/kg_viz/{nb}`、
`{storage_dir}/kg_index_partitions/{nb}`
（`backend/app/repositories/filesystem/scale_artifact_store.py:192-205`），
兄弟形态为 `*.old`、`*.tmp`（**无后缀形，v2 漏了**）、`*.tmp-<claim_token>`
——三种由 `backend/app/repositories/filesystem/scale_artifact_store.py:222-227` 的
排除式给出，删除侧必须与它**同一份形态清单**。
**时点与归属**：在**持有 per-notebook 锁**的前提下删（§4），否则与在跑的
build/import 抢同一棵树。持锁是排他的，删除期间不可能有别人的 `.tmp*`，
所以整目录 rmtree 安全——**这一条要在规格与测试里明说**。
顺序 `.tmp-*` → `.tmp` → `.old` → live；任何一步失败只记账不中止
（照 `services/notebook_catalog.py:83` 的 `ignore_errors=True` 口径）。
来源文件按 `notebook_delete_files` 逐行删（`repositories/source_files.py:64-67`），
资产目录按 `services/notebook_catalog.py:63-83`。

### T-4 幂等、续跑与扫尾

**幂等**：清理器由 `(status='deleting', phase, cursor)` 驱动。形一「取页→删页」、
形二「同语句取 ctid→删」重放时都自然前进；归档段靠 `ON CONFLICT DO UPDATE` 幂等；
相位 5 是单事务，要么全成要么全无。不需要去重表。

**中断/重启**：库仍是 `deleting`、相位与游标停在最后提交处。
⚠ 删除作业**不进** `recover_interrupted_jobs`（那里语义是「结算为 failed」，
`postgres/maintenance.py:581`），由扫尾承载。

**扫尾（双驱动，缺一不可）**：启动后（`startup_warmup` 里、`recover_interrupted_jobs`
之后）+ 每 `NOTEBOOK_DELETE_SWEEP_SECONDS`（默认 300，与 `services/checkup.py:78`
的 `_H45_CACHE_TTL` 同量级）：
- **驱动 A**：`notebook_delete_jobs` 中 `status IN ('queued','running','waiting')`
  且 `updated_at < cutoff` 的作业行 → 重排（回收孤儿作业行：进程死时作业行停在 `running`）；
- **驱动 B**：`notebooks.status='deleting'` 且没有活作业行的库 → 补建作业行
  （兜住「CAS 提交了但建作业行失败」与作业行被误删）。
- **驱动 A 的特例——「作业行在、`notebooks` 行不在」**：只可能来自带外删除
  （旧路径残留、`sweep_stale_copies` 误吞、DBA 手工删）。处置 = **补完残渣**：
  跳过相位 5 的 1–2 步（无行可锁、无需归档，归档已由带外路径做过或永久缺失，
  **不重做**以免用空快照覆盖），直接重跑相位 3 的残渣清扫 + 相位 4 的磁盘清理，
  然后删侧表与作业行。这条正是「两张新表不 FK 到 `notebooks`」的理由（T-3）。

**执行载体（D-2 已定：新开第三个池）**：
`background_jobs.submit(..., name=f"deletenb-{notebook_id}")`，配套三处改动——
1. `_SAFE_JOB_PREFIXES`（`services/background_jobs.py:41-58`）**显式**加
   `("deletenb-", "deletenb")`。不加 = 不进闸（`_maintenance_pool` 保守放行，
   `:116-133`），等于无并发上限。
2. 新增第三个池常量与集合，与既有两池并列（`:104-107` 的 `_HEAVY_POOL` /
   `_LIGHT_POOL` 旁）：`_DELETE_POOL = "delete"`、
   `_DELETE_OPERATIONS = frozenset({"deletenb"})`、`_DEFAULT_DELETE_CONCURRENCY = 1`；
   `_maintenance_pool` 与 `_pool_capacity`（`:136-151`）各加一条分支。
3. 新配置 `NOTEBOOK_DELETE_CONCURRENCY`（默认 **1**，允许 1–2）。
**判据（与仓库既有分池判据同轴，`background_jobs.py:61-73`「两个池而不是一个，
判据是量级差」）**：删除既不是 LLM 扇出型重活（会被小时级重建饿死，而删除是用户
已点过确认的操作），也不是秒级轻活（它是长时低 CPU 高 I/O，会挤掉单表投影），
所以给它独立预算最诚实。
⚠ 该容量同时是 §4.3 会话槽预算与 §1.2 连接预算的输入，三处必须用同一个配置项。

### T-5 `delete_notebook_kg`

- **T-5a（做）**：`delete_notebook_graph_rows` 的 11 条无界 DELETE 改为 §1.5 的
  形一/形二页删循环，每批一事务。原子性从「一个事务」变成「N 个事务」；
  可接受的判据：清图后紧跟重建，中途失败的中间态与「重建跑到一半」同类，
  后者已被 `kg_build_jobs` 的 failed 结算覆盖。seq 处理与 §3 同批做。
- **T-5b（不做，登记）**：`rebuild` 改走 `preserve_existing_rebuild=True`
  （`knowledge_lifecycle.py:3173-3183`，今天由索引管线重建使用，
  `services/repository_facade.py:1727`）。不能直接切换：整库清图与逐源替换不等价
  （后者留下「源已不在但对象还在」的残渣与全部库级派生表），需先给出等价性证明与差集清扫。

---

## §3 `unified_kg_state` seq 语义统一（吸收 R3·PR-A 登记项）

### 3.1 冲突的确切形状

登记原文见 `services/kg_mutation.py:224-230` 的 FULL CENSUS「Deliberately NOT moved」段
与 `services/review_queue_memo.py:84-93`。核实后判据比登记简写更精确
（`services/kg_analysis.py:679-711`）：

```
row is None  或  (row["kg_mutation_seq"] == 0  且  not row["last_rebuild_at"])
   → present=False（"没有 KG 历史"）
```

判据是 **`seq==0` 且 `last_rebuild_at` 空**，不是裸 `seq==0`；`create_notebook` 的出生行
（`postgres/notebook_store.py:225-238`）正靠这条与「行缺失」等价，
并被 `test_born_state_row_reports_like_a_never_written_notebook` 字节等值钉死。

**关键推论**：出生行与行缺失在该判据下**已经等价**，
所以「保行 + seq **归零** + 清空派生列」与「删行」对 `_state_view` 是**逐字节同一件事**。
冲突只存在于「保行 + **bump**」。

### 3.2 读者逐一裁决

| # | 读者 | 现场 | 读的是 | 行缺失/seq 归零的后果 | 裁决 |
| --- | --- | --- | --- | --- | --- |
| 1 | `kg_analysis._state_view` → 总览 `present` | `services/kg_analysis.py:532,679-711` | 整行 | **语义信号**：无 KG 历史 | **唯一把行缺失当语义用的读者**；方案 C 下逐字节不变 |
| 2 | `unified_kg_status` | `services/knowledge_lifecycle.py:3540` | `cluster_count` + 空值回退 | 回退实时 COUNT | 无害 |
| 3 | `rebuild_unified_kg` 跳过闸 | `knowledge_lifecycle.py:4320-4323` | `cluster_input_version` + `cc>0` | 闸开→重算 | 无害 |
| 4 | `rebuild_canonical_relations` | `knowledge_lifecycle.py:4838-4841` | `canonical_rel_seq==seq` + `cnt>0` | 闸开→重算 | 无害 |
| 5 | 共提桥接 | `knowledge_lifecycle.py:4894-4897` | `mention_seq==seq` + `cnt>0` | 闸开→重算 | 无害 |
| 6 | KG 分析预计算闸 | `knowledge_lifecycle.py:5122-5128` | 整行+账本+板块数 | 闸开→重算 | 无害 |
| 7 | `evidence_context` | `services/evidence_context.py:789` | `canonical_rel_seq` | 判陈旧→重取 | 无害 |
| 8 | 检索 PPR/图版本探针 | `services/retrieval_candidates.py:1442,1505` | `graph_seq_row` 三元组 | **别名** | 今天靠 `invalidate_kg` 全 evict；方案 C 结构性消除 |
| 9 | `knowledge_counts_cache` **4 个** memo（`_MEMO`/`_PENDING`/`_VISIBLE_PENDING`/`_CHUNKS`） | `postgres/knowledge_counts_cache.py:51-54,88-116` | `kg_mutation_seq` | **别名** | 今天靠 `knowledge_lifecycle.py:507` 显式失效（仅进程内） |
| 10 | `ReviewQueueMemo` | `services/review_queue_memo.py:84-93` | `kg_mutation_seq` | **别名** | 今天靠 `knowledge_lifecycle.py:513` 显式失效（仅进程内） |
| 11 | checkup H4/H5 | `services/checkup.py:376,392,417` | `(租约快照, seq)` | **别名** | 只有 300s 背底（`checkup.py:78`） |
| 12 | `version_signal` → scale 产物 / 检索快照 | `postgres/index_projection_store.py:93`；消费于 `services/scale_artifact_runtime.py:486,503,754,860,1188`、`services/scale_index_builder.py:1069`、`services/retrieval_snapshot_cache.py:116` | `(seq,cseq,settings_tail)` | **别名**（`:116` 明文 RESETS to (0,0,-1)） | 方案 C 结构性消除 |
| 13 | 统一图内存缓存 | `knowledge_lifecycle.py:502` | — | 进程内显式失效 | 同 9/10 |
| 14 | `source_subgraph_projection` | `backend/app/repositories/source_subgraph_projection.py:54-56` | `(kg_seq,cluster_seq)` | **别名** | 同 12 |
| 15 | `kg_analysis_artifacts` 账本的 `kg_mutation_seq` | `backend/app/repositories/kg_analysis_payloads.py:49-76` | 账本 seq vs state seq | ⚠ 账本**不在** `_GRAPH_RESET_TABLES`（`postgres/knowledge_store.py:50-57`），清图后账本带旧的大 seq 存活而 state 归零 → `_artifact_freshness` 的 `seq_behind = 0 - built` 是**负数**，而负数按契约明文是「库被手工改过」的异常信号（`kg_analysis.py:912,966`） | **既有潜在假异常**，同源，一并处置 |

**分层结论**：#2–#7 都是「seq 闸 + 计数闸」双条件，行缺失只意味着多算一次，方向保守。
争议只在 #1（语义读者）与 #8–#14（别名读者）之间；#15 是顺带发现的既有缺陷。

### 3.3 方案（**D-3 已定：采用 C**）

- **A（已否）：保行 + 永续 bump + 改 `_state_view` 判据。** 别名根除；但需新增
  「有无历史」列（不能简化成 `not last_rebuild_at`：抽取过但未 rebuild 的库会被误判），
  且要改被字节等值测试钉死的契约。改动面最大。
- **B（已否）：维持现状 + 继续逐个显式失效。** 零迁移；但失效是**进程内**的
  （#9/#10/#13），跨 worker 无效，#11 只有 300s 背底。
  正是 R5 全类扫修反对的「N 个补丁替一个机制」。
- ✅ **C（已定，D-3）：保行 + seq 归零 + 新增持久化代次列。**
  端口签名变化（`version_signal` / `graph_seq_row`）**已获确认承担**，见 §3.4 末段。
  1. `delete_notebook_graph_rows` 不再删 `unified_kg_state` 行，改为同事务
     **重置为出生行形状**（`kg_mutation_seq=0, cluster_mutation_seq=0, community_seq=-1,
     canonical_rel_seq=-1, mention_seq=-1, cluster_input_version='', object_count=0,
     relation_count=0, cluster_count=0, last_rebuild_at=NULL, dirty=0`），
     保留 `source_index_backfilled` 的 certificate 逻辑
     （`knowledge_lifecycle.py:490-501` 原样）。
     → **#1 逐字节不变**，字节等值测试照旧绿，`_state_view` 一行不改。
  2. 同事务 `kg_reset_epoch = kg_reset_epoch + 1`（新列，`BIGINT NOT NULL DEFAULT 0`）。
     **只增不减，清图是唯一推进者。**
     ⚠ **命名裁决**：不叫 `kg_epoch`——会与 `knowledge_counts_cache` 的进程内
     `_EPOCHS`/`_epoch_of`（`postgres/knowledge_counts_cache.py:56-90`）同名而语义不同
     （那个是「本进程失效代次」，best-effort 安全阀；这个是持久化的
     「KG 被清空过几次」，正确性权威）。定名 **`kg_reset_epoch`**，两处 docstring 互相点名。
  3. `graph_seq_row` / `state_row` / `version_signal` 各多带一项 epoch，
     #8–#14 的 memo 键从 `seq` 变成 `(epoch, seq)`。别名结构性消失，**跨进程有效**。
     ⚠ **必须追加到元组末尾**（`version_signal` 变 `(seq, cseq, settings_tail, epoch)`，
     `graph_seq_row` 变 `(kg, cluster, mention, epoch)`）：现存**三处按位置索引消费**
     `version_signal(notebook_id)[1]`——`scale_artifact_runtime.py:754`、`:860`、
     `scale_index_builder.py:1069`——插在中间会静默改变它们读到的值。
  4. `knowledge_lifecycle.py:502/507/513` 三处显式失效**随之删除**（一个机制替三个补丁）。
  5. #15：清图同事务里**一并删掉本库 `kg_analysis_artifacts` 账本行**
     （加进 `_GRAPH_RESET_TABLES`），负数落后量的假异常消失。
     ⚠ 行为变化：总览改显示 `ABSENCE_NEVER_COMPUTED`（`kg_analysis.py:862-863`），
     那正是清图后的真相。单列在 PR 的语义变更段。
  6. **重写 `kg_mutation.py` 的 FULL CENSUS 条目**：`:224-230` 现在写的
     「Cannot bump: it DELETES the row, so the seq restarts from 0 and aliases」
     整段作废。新条目说明：清图**仍不 bump `kg_mutation_seq`**（它归零），
     而是同事务推进 `kg_reset_epoch`；并**重新裁决**该模块红线措辞——
     从「凡提交图行的事务，其 seq bump 必须与这些行同一次提交」改为
     「必须与这些行同一次提交地**推进版本身份**（`kg_mutation_seq` 或 `kg_reset_epoch`）」，
     并把 `kg_reset_epoch` 的唯一写者登记进 census。

### 3.4 epoch 绝不能无条件进 `version()`

`ScaleArtifactRuntime.version()`（`services/scale_artifact_runtime.py:483-522`）返回一个
**扁平 list** `version_facts + list(settings_tail) + ["edge_schema", EDGE_SCHEMA_VERSION]`，
**五个**消费点按整值相等比较：

| # | 站点 | 形态 |
| --- | --- | --- |
| 1 | `services/scale_artifact_catalog.py:292` | `idx.manifest.get("version") == cur`（load 的 exact 闸） |
| 2 | `services/scale_artifact_catalog.py:341-344` | `_still_current`：`cached.manifest.get("version") == version` |
| 3 | `services/scale_artifact_runtime.py:753-754` + `:803` | viz 新鲜度 `_viz_manifest_fresh` |
| 4 | `services/scale_artifact_runtime.py:1354` | `/index-status` 的 `version_stale` |
| 5 | `services/scale_build_cli.py:1587-1590` | CLI `inspect` 的 `version_matches_database` |

⚠ **无条件追加一个 epoch 元素 = 上线那一刻全库每个 manifest 判不等 → 整个 fleet
判 stale → 小时级重建风暴 → 直接撞红线一。**

**修法**：epoch 分两处，规则不同——

| 位置 | 规则 | 理由 |
| --- | --- | --- |
| `version()` 的**进程内 memo 键**（`:487-492,504-511`） | **无条件**加入 epoch | 纯进程内，无磁盘兼容面；不加则「删除把 seq/cseq 归零到一个已 memo 的三元组」直接返回陈旧 list |
| `version()` **返回的 list**（写进 manifest、参与整值比较） | **仅当 `epoch > 0`** 时追加 `["kg_reset_epoch", N]` | epoch=0 的库 list 逐字节不变 → 既有 manifest 恒等 → **零重建**；epoch>0 的库合法转 stale → **只重建该库** |

**行为矩阵**：

| 库的历史 | epoch | version list | 与既有 manifest | 结果 |
| --- | --- | --- | --- | --- |
| 从未 `delete_notebook_kg` | 0 | 与今天逐字节相同 | 相等 | 不重建 ✅ |
| 上线前清过图 | 1 | 多 `["kg_reset_epoch",1]` | 不等 | 重建一次 ✅（KG 确曾被清空，产物本就该重建） |
| 上线后清图 | N→N+1 | 值改变 | 不等 | 重建 ✅ |

单调性：epoch 只增，「无 epoch 元素」与「有 epoch 元素」的 list 永不互相回退。

**站点 5 的新告警形态（登记进运维文档面）**：一个 epoch>0 的库在产物重建之前，
`scale_build_cli inspect` 会报 `version_matches_database: false`。
这是**正确**的（产物确实过期），但运维会新看到这个字段变 false，
`docs/operations.md`/`_zh.md` 必须写明：「首次上线本特性后，历史上清过 KG 的库会短暂
出现 `version_matches_database: false`，一次重建后消失；这不是损坏」。

✅ **端口面代价（D-3 已确认承担）**：`version_signal` 的 `tuple[int, int, tuple]`
（`backend/app/repositories/ports.py:3772`）与 `graph_seq_row` 的 `tuple[int,int,int]`
（`ports.py:2058`）签名变化。零松弛机械门（ports 计数、`facade_surface.json`、
`RepositoryFacade.__init__` 行数）任何一道都不许因此松动；**棘轮同 diff 改**，
并在 PR 里引用 W-CLI T-W1 的同款先例（那次为锁 seam 立的规矩：
「若端口确需新方法，棘轮同 diff 且在 PR 里引用 64d5aa10 先例」）。
备选「折进 `settings_tail`」**已否**——那个 tuple 无条件进 version list，会退回本节问题。

**迁移路径**：新列 `DEFAULT 0`，旧行天然 epoch=0；旧 manifest 无 epoch 元素，
按矩阵第一行天然恒等。SQLite 侧 `add_column_if_missing`（`sqlite/migrations.py:130`）同形。

---

## §4 并发与互斥

### 4.1 互斥矩阵

| 对手 | 现状 | W1 要求 | 机制 |
| --- | --- | --- | --- |
| 来源摄取/抽取、上传（**新请求**） | 无互斥 | 不需要：`deleting` 后新请求进不来 | 谓词即闸=**目录寻址 + 授权谓词双层**（T-1）——目录寻址（`get_notebook`）已 404；直连资源端点（`/sources/{id}` 等,不经过目录寻址)靠 `access_sql` 的 `NOTEBOOK_READ_SQL`/`NOTEBOOK_ADMIN_SQL`/`NOTEBOOK_WRITE_SQL` 自身挡住（codex #653 R2 补齐,原表述「谓词即闸」曾只指目录寻址一层,是这一格的表述缺口） |
| **已在跑的 ask / 解析 / 抽取作业（在途写）** | 围栏行锁 | **挡不住**（摸底 2） | **相位 5 单事务**：围栏+归档+四表删同一事务（T-3.2） |
| `rebuildkg-`（durable 簿记：`kg_build_jobs`） | 部分唯一索引单飞，**不取 advisory 锁** | 必须**等它真的停** | **相位 2 `quiesce` 腿 A**（查 `kg_build_jobs` 无 running）+ `knowledge_lifecycle.py:3176` 批边界检查点（4.2 选项 A） |
| `relinkkg-` / `unifiedkg-`（**进程内簿记**：`KgMaintenanceJobs.jobs`，一行 `kg_build_jobs` 都不写——摸底 7b） | 进程内字典 claim（`services/kg/maintenance_jobs.py:71-88`），**不取 advisory 锁** | 同上 | **相位 2 `quiesce` 腿 B**（查进程内字典，依赖「生产单 worker」部署契约）+ **新增**检查点于 `knowledge_lifecycle.py:1409`（逐源）与 `:4389-4395` 的 `_stage`（10 个阶段边界）——今天这两条路径**一个检查点都没有** |
| scale build / fold（在线与 W-CLI） | per-notebook advisory lock（`postgres/database.py:678-754`） | **必须互斥**（产物树 + DB 行） | 复用同一把锁（4.3） |
| W-CLI `build/export/import` | 同上 | 同上 | 同上 |
| `batch_ingest` 维护 CLI | 库级全局 advisory lock + 停服闸（`postgres/maintenance.py:105`） | 天然互斥 | 不改 |
| 另一次删除同库 | 无 | 单飞 | `notebooks` CAS + 作业表部分唯一索引 |
| `copy_notebook`（本库为源） | 无 | 源被删 = 半成品 | 源 `get_notebook` 已 404；在途拷贝由 `compensate_copy`/`sweep_stale_copies` 收 |
| **`sweep_stale_copies` 本身** | 按 `status='copying'` 扫 | **绝不能吞 `deleting`** | T-1 写侧守卫（摸底 5） |

### 4.2 在途重建的处置

摸底 7 证实：全仓无 `KgExtractionRunControl` 注册表、无 KG cancel 端点。

- **选项 A（选定）：给每条长循环加 `notebooks.status` 检查点——三处，不是一处。**
  | 路径 | 落点 | 边界粒度 | 退出通道 |
  | --- | --- | --- | --- |
  | `buildkg-`/`rebuildkg-` | `services/knowledge_lifecycle.py:3176` 的 `_kg_target_batches` 批循环 | 一批（多个来源） | 既有 `control.abort(...)` / `KgBuildAborted`（`services/kg/run_control.py:144-155`、`knowledge_lifecycle.py:3095-3135` 的 `_mark_stopping`） |
  | `relinkkg-` | `knowledge_lifecycle.py:1409` 的 `for source_id in self._relink_source_partitions(...)` | 一个来源 | 抛出 → `run_notebook_relink_job` 既有的 `except Exception: settle(..., "failed")`（`services/kg/maintenance_jobs.py:144-152`） |
  | `unifiedkg-` | `knowledge_lifecycle.py:4389-4395` 的 `_stage` 内（**一处改动覆盖 10 个阶段边界**） | 一个阶段 | 同上，`maintenance_jobs.py:178-186` |
  成本：每边界一次 `notebooks` 主键点查，噪声级。三处全部落在 PR-3。
  ⚠ v3 只写了第一处——那是 P1-C 的另一半：**闸补齐了但检查点没补齐，
  等于把重建的停止时机推给运气**。
- **选项 B（不做）：删除作业主动 abort。** 需新建 `job_id → control` 进程级注册表 +
  生命周期管理，且只对同进程有效（多 worker 下仍要退回 A）。
  登记为「若将来需要 KG 构建取消端点，与之合并实现」。
- ⚠ **A 只保证「会停」，不保证「删除等它停」**——后者由相位 2 的 `quiesce` 闸承担
  （T-3.3）。两者缺一不可：只有 A，删除会在重建的批内动手；只有闸，重建永不自停，
  删除永远等到超时。而且**两者的覆盖面必须一一对应**：
  闸有两条腿（durable / 进程内），检查点就必须有对应的三个落点，
  漏掉哪一条，那类作业就会把删除卡到超时（安全但永远删不掉）。

### 4.3 锁选型

复用 `try_scale_build_lock` 的**同一 namespace**（`0x53434C42`，`postgres/database.py:60`），
不新开。判据：删除与 scale build **必须**互斥，同 namespace 同 key 天然做到；
另开 namespace 反而要写一份获取顺序防死锁。

⚠ **同锁双时长混用的三处代价（必须同 PR 改）**：
1. **会话槽预算**：`_scale_build_lock_slots` 容量 = `SCALE_BUILD_CONCURRENCY + 1`
   （`postgres/database.py:287-292`，注释明说「Sized one above the build ceiling so an
   admission probe can always run」）。删除作业也占这类会话后该假设失效——
   必须改为 `SCALE_BUILD_CONCURRENCY + NOTEBOOK_DELETE_CONCURRENCY + 1`。
   不改则一个长跑删除会让 `_admit_scale_op` 的探测经常拿到
   `SCALE_BUILD_LOCK_UNAVAILABLE`（「判不出」）→ 反复 park，准入退化成永久排队。
2. **消息语义**：删除持锁期间该库的 scale build 探测拿到 `None`（「他人持有」）——
   语义正确，但用户可见文案会说「已在构建中」。
   **必须泛化为「该库有另一项独占任务在进行」**，否则运维按文案找不到那个不存在的构建。
3. **`application_name` 与模块命名**：`'silicon-notebook-scale-build-lock'`
   （`postgres/database.py:712`）与 `repositories/scale_build_lock.py` 的模块 docstring
   与实际语义不符；同 PR 改口径（建议 `'silicon-notebook-notebook-exclusive-lock'`），
   并更新 `docs/operations.md` 的 `pg_locks` 排查段。

**三值逐值处理**（`repositories/scale_build_lock.py:15-29`）：句柄 → 干活；
`None` → 作业留 `queued`，扫尾重试；`SCALE_BUILD_LOCK_UNAVAILABLE` → 同上。
⚠ 两种失败都**绝不调** `_scale_record_failure`（`services/scale_artifact_runtime.py:1486`）。

**每批之前复验持锁**：删除的每一批都是破坏性的——**每批 `write()` 之前调一次
`verify_held()`**。丢锁 → 就地停手、作业置回 `queued`、库仍 `deleting`、报实测状态。
**相位 5 与相位 4 同样各复验一次**（前者是不可回退的终局，后者是不可回退的磁盘删除）。
**锁跨线程移交**：手工 enter/exit，每个出口要么交给 worker 要么自己释放，写测试钉。

### 4.4 SQLite

单进程部署（`UNSUPPORTED_SCALE_BUILD_LOCK`，`repositories/scale_build_lock.py:82-106`），
互斥由进程内 claim 承载。但 SQLite 是**单写者**：分钟级删除事务阻塞全库所有写，
所以分批对 SQLite 收益**更大**。SQLite 侧同样实现全部相位；形二用 rowid（§1.5）；
`sqlite/notebook_store.py:444-445` 的两条 FTS 显式删除进相位 3 各自的批。

---

## §5 用户可见性

- **列表/打开/一切读**：`deleting` 的库经 T-1 统一谓词一律不可见，`get()` 抛 `KeyError`
  → 404（与 `copying` 同口径，`services/notebook_catalog.py:430-433`）。
  **不做「清理中」的可见占位**：判据是 `copying` 的先例——半成品对用户不可用时，
  最诚实的呈现是「不在」。
- **删除接口**：204 → **202 + `{"status":"deleting"}`**。
- **前端零改动即可用**：`deleteNotebook`（`frontend/app/notebook-api.ts:64`）的调用点
  已在成功后写**客户端 tombstone**（`frontend/app/use-notebook-collection.ts:1140-1149`：
  `tombstonesRef` + 递增 `deleteGenerationRef`，随后 `refreshComposite`），
  「立即从列表消失」今天就已做到。
- **无退路弹窗契约（`notebook-delete` slot）**：契约本体（`escape:false, backdrop:false`，
  关闭入口不得随 busy 禁用；守卫 `frontend/tests/guards/root-modal-boundary.test.mjs`）
  继续适用且**更容易满足**——202 让 in-flight 窗口从分钟级缩到一次单行 UPDATE。
  `closeDelete` 的「删除请求仍在进行；结果稍后会反映在列表里」
  （`use-notebook-collection.ts:1112-1126`）语义更准，保留。
  成功文案 `notify("笔记本已删除")`（`:1169`）**刻意不改**。
- **归档的可见性**：管理员的用户活动视图仍能看到最小化活动快照
  （`retained_user_activity`，保留 180 天）。⚠ 分批化后 `deleted_at` = **相位 5 时刻**，
  可能晚于用户点删除数十分钟——保留窗口口径不变，但**必须写进文档面**。
- **管理员视角**：`deleting` 的库与清理进度只经诊断脚本可见
  （`scripts/diag_pg_hotpaths.py` 加只读的 `notebook_delete_jobs` 概览，
  形照 `:306-321` 的 rowcount 段），不做管理端点。

---

## §6 红线论证

**红线一：不降检索性能。**
- 删除路径与检索路径无交集；分批删除本身是**改善**：不再有分钟级事务钉住
  xmin horizon 让 autovacuum 在 484GB 上失效（相位 5 的 1–6s 与今天的分钟级不同量级）。
- 唯一进入检索热路径的改动是 §3 的 epoch：加在
  `graph_seq_row`（`postgres/unified_kg_store.py:335`）与
  `version_signal`（`postgres/index_projection_store.py:93`）**已有的单行点读**的
  SELECT 列表里——**零新增往返、零新增索引查找**，只多解一个 bigint。
- **§3.4 是本红线主要风险面，已用「epoch>0 才进 version list」封住**。
  验收含硬门「上线模拟：全部 epoch=0 的库 manifest 判等率 100%」。
- 实测入 PR：epoch 引入前后 `graph_seq_row` / `version_signal` 的 characterization 计时。

**红线二：不降 KG 抽取性能。**
- 抽取写路径（`store_kg`、`clear_source_graph_state`、`incremental_fuse_source`）一行不动。
- T-5a 只影响 `mode=="rebuild"` 的开头；4.2 选项 A 每批加一次主键点查（批 = 多个来源）。
- §3 方案 C 删掉三处显式失效，是**减少**工作量。
- ⚠ **相位 2 会让一次在跑的 rebuild 被中止**——这是删除语义的必然（要删的库不该继续
  抽取），不是抽取性能回归；但要在 PR 的语义变更段写明「删除会中止该库在跑的重建」。
- 实测入 PR：一次真实 `rebuild` 的分段计时对比（本地 41k 库）。

**红线三：不改问答质量。**
- 问答读 KG 与 chunk 内容；`deleting` 的库对问答不可见，与今天「已删除的库不可见」一致。
- 唯一语义面是 §3 的 #1：方案 C 下 `_state_view` 逐字节不变，字节等值测试是判据。
- #15（账本行随清图删除）会改变 KG 分析总览显示，方向是「从假异常改成如实报告未计算」
  ——不是问答路径，是修正而非回归，单列在 PR 的语义变更段。
- ⚠ **归档不得回归**：相位 5 产出的 `retained_user_activity` 必须与今天逐字段相同
  （同一投影 SQL、同一 `ON CONFLICT` 语义、同一单快照、同一 `deleted_at`/`expires_at` 口径），
  否则改的是产品的审计承诺。进 G1 硬门。

---

## §7 测试与验收门

**G1（`scripts/check.sh`，全绿）**
- T-1 **两类**守卫 + 扫描范围声明 + 4 行散文豁免清单 +
  `services/notebook_sharing.py:88` 生产者的覆盖说明。
- `sweep_stale_copies` 拿 `deleting` 的库当输入**必须不删**（变异钉：把它的谓词
  改成两值即变红）。
- 状态机：`deleting` 的库在 `get`/`list_for_user`/挂载/群组授权/搜索/计数缓存全部不可见。
- CAS：重复 DELETE 第二次不产生第二个作业行。
- **相位序不变量**（变异钉，逐条）：
  ① `sources` 行在 `paths` 相位完成前被删 → 红；
  ② 四张归档输入表的行在相位 5 之外被删 → 红；
  ③ 相位 3 在 `quiesce` 未通过时启动 → 红；
  ④ 相位 5 的归档三段被拆成多事务 → 红（单快照一致性）。
- **形二（ctid/rowid）**：重放安全；**两条变异钉**——
  ① 把 ctid 取到 Python 再跨语句删 → 红；
  ② **把终止条件从 `rowcount == 0` 改回 `rowcount < 批大小` → 红**
  （用例：批中途并发 UPDATE 一行使其 ctid 失配，断言剩余行仍被删净）。
- 幂等重放：同一批跑两次，第二次删 0 行且不报错。
- 中断续跑：任一相位后模拟进程消失 → 扫尾**两条驱动各自单独**都能接上 → 终态零残行；
  **驱动 A 的「作业行在、库不在」特例**单列一条（归档不得重做）。
- **归档等价性**：相位 5 产出的 `retained_user_activity` 行与今天单事务路径**逐字段相等**。
- §3 方案 C：`test_born_state_row_reports_like_a_never_written_notebook` **原样通过**
  （不许改这个测试）；新增「清图后 state 行与出生行逐字节相等」、
  「`kg_reset_epoch` 只增不减」、「delete+重抽回到同一 seq 时四个 memo 全部 miss」、
  **「epoch 追加在元组末尾」**（变异钉：插到中间 → 三处 `[1]` 消费点的用例变红）。
- **§3.4 上线模拟（硬门）**：一组 epoch=0 的库，`version()` 输出与改动前**逐字节相同**，
  五个比较站点判等率 100%；一个 epoch=1 的库在五个站点各自判 stale。
  变异钉：把「epoch>0 才追加」改成无条件 → 上一条变红。

**G3（`scripts/check_postgres.sh`，`postgres_integration` marker）**
- 真 PG 全链路：造一个覆盖 A 类、B 类、形二 22 张、闭包外 6 张的中等库 → 删除 →
  **每条语句实测 `< statement_timeout`**（生产口径 180s，D-1），
  且**相位 5 单事务时长实测入报告，并对照 §T-3.2 的 30s 触发线判读**
  （这是 P0-A 的核心验收）→ 终态 **61 张相位 3 表 + 4 张相位 5 表 + 6 张闭包外表全为 0 行**，
  且 **`retained_user_activity` 本库归档行仍在、`object_schemas` 行数不变**（反向断言）。
- **形二专项**：`knowledge_object_sources` / `community_members` / 两张 scratch 上的
  ctid 形 EXPLAIN 必须是 Tid Scan + 内层 Index Scan，逐批计时入 PR
  （摸底 6 的 5.3ms/201ms **不覆盖本形**）。
- **磁盘残留断言**：`kg_index/{nb}`、`kg_viz/{nb}`、`kg_index_partitions/{nb}`
  及其 **`.old` / `.tmp` / `.tmp-<token>` 三种**兄弟、来源文件目录、资产目录全部不存在。
- **quiesce 闸（两条腿各一组）**：
  ① 腿 A：起一个 running 的 `kg_build_jobs` 行 → 删除停在相位 2 → 行改为非 running
  → 删除进相位 3；
  ② **腿 B：`KgMaintenanceJobs.claim` 一个 relink/unified 作业（不写任何
  `kg_build_jobs` 行）→ 删除必须同样停在相位 2**；`settle` 之后才放行。
  **变异钉：去掉腿 B → 该用例变红**（这正是 v3 的漏洞）。
  ③ 超时路径置 `waiting`、不记 scale 退避、日志写明是哪条腿在挡。
- **三个检查点各一条**：`deleting` 写入后，`rebuildkg-` 在批边界、`relinkkg-` 在
  来源边界、`unifiedkg-` 在阶段边界各自结算退出；任一处去掉检查点 → 对应用例
  超时变红（而不是静默通过）。
- 双连接互斥：删除持锁时 scale build 探测得 `None`（不是 `SCALE_BUILD_LOCK_UNAVAILABLE`）；
  会话槽预算改动后 `_admit_scale_op` 探测仍能拿到会话。
- 丢锁停手：中途杀掉锁会话 → 下一批 `verify_held()` 为假 → 就地停手、零额外删除；
  相位 4/5 前的复验同样覆盖。
- 迁移 `0047` 幂等（`IF NOT EXISTS` / 已存在列的形态校验），照 `0042` 的守卫 DO 先例；
  **三条新索引各自的 EXPLAIN 验收**：`agent_access_tokens` 的 FK 级联从 Seq Scan
  变 Index Scan、`knowhow_cell_code(column_id)` 腿从 Seq Scan 变 Index Scan、
  `conversations` 的形二内层从 Seq Scan 变 Index Scan。

**SQLite 侧：对等，不豁免。** SQLite 单写者，长事务伤害更大。
`_migration_68` + `SCHEMA_VERSION 68`；全部相位实现；形二用 rowid。
**唯一豁免**是跨进程锁——按 `UNSUPPORTED_SCALE_BUILD_LOCK` 走进程内 claim，
理由与 W-CLI 相同（单进程部署），PR 里引用该先例。

**性能验收（实测入 PR，不接受估算）**：T-0 的逐表行数与逐批计时（形一/形二分开）；
**相位 5 单事务时长**；epoch 引入前后两个点读的计时；一次真实 rebuild 的分段计时。

---

## §8 PR 切法与回滚

- **T-0（前置动作，不是 PR）**：只读测量脚本跑生产 analog base，回填 §1。
  **必须包含**：① 相位 5 四张表的行数（定 §T-3.2 的时长，按 **30s 触发线**判读）；
  ② 形二（ctid）的 EXPLAIN 与逐批计时；③ 复核生产
  `POSTGRES_STATEMENT_TIMEOUT_SECONDS` 仍为 **180**（D-1 的前提，若已再变则回来重算
  §1.1/§1.2 与 D-4 的交叉校验上界）。
- **PR-1｜可见性谓词单点化（含 AST 守卫）+ `scripts/diag_db.py` 两处 +
  `include_copying` 清理 + `fangan_done.md` 补记 W-CLI 条目**（T-1）。
  行为零变化（`deleting` 尚无任何行）。先合先审。回滚：直接 revert，无数据面。
- **PR-2｜seq 语义统一**（§3 方案 C：`0047` + `_migration_68` 的 `kg_reset_epoch`、
  memo 键扩展、元组末尾追加、§3.4 的条件式 version、三处显式失效删除、
  账本行进 `_GRAPH_RESET_TABLES`、`kg_mutation.py` FULL CENSUS 重写）。
  **与删除作业解耦，可独立上线**。回滚：新列留着（DEFAULT 0，无害），
  代码 revert 后 memo 键退回单 seq；已写进 manifest 的 `["kg_reset_epoch",N]`
  会让那几个库判一次 stale 重建，可接受。
- **PR-3｜tombstone + 六相位删除作业 + 形二原语 + 磁盘产物 + 双腿 quiesce 闸 +
  三处重建检查点 + `0047` 的三条索引**
  （T-2/T-3/T-3b/T-4 + 4.2 选项 A 的三个落点 + 4.3 的三处锁改动 +
  §1.4 的三条索引 + API 202 + 文档。**T-5a 未随本 PR 出货**——与 PR-2
  评审钉回的单事务不变量正面冲突，剥离为独立 PR，冲突分析与验收标尺
  见「勘误 2」）。
  ⚠ **回滚分两段**：代码 revert 后残留的 `status='deleting'` 行会被旧代码的
  40 处 `!= 'copying'` 谓词**放行**，半删的库重新可见。revert **必须**配运维处置：
  已清完的直接物理删、未清完的置回原 status 并接受部分数据丢失。
  写进 PR 描述与 `docs/operations.md`。
  ⚠ 若 PR-3 过大，可再切：**PR-3a**（T-1 之后的 tombstone + 相位 0/1/5 + quiesce，
  即「立即返回 + 单事务终局」，此时相位 3 仍是今天的无界形——**不可单独上线**，
  只作为评审切分）与 **PR-3b**（相位 3 的分批 + 形二 + 相位 4 磁盘）。
  两者必须同批上线，因为 3a 单独并不解决超时。
- **PR-4｜存量孤儿清扫 + 存量磁盘产物清扫 + `compensate_copy`/`sweep_stale_copies` 收编**。
  可延后。回滚：脚本类改动。
- **PR-5（可选）｜T-5b**。需独立等价性证明，默认**不做**。

**文档契约面（随对应 PR 成对更新）**：
`docs/product-and-api.md` / `_zh.md`（DELETE 端点 204→202，条目在
`product-and-api.md:2292` / `_zh.md:1768`；`deleted_at` = 相位 5 时刻的口径）、
`docs/operations.md` / `_zh.md`（删除作业运维、`pg_locks` 排查段口径、
`version_matches_database:false` 的新告警形态、回滚处置、新配置项）、
`docs/deployment-and-configuration.md` / `_zh.md`（新环境变量：
`NOTEBOOK_DELETE_CONCURRENCY`、`NOTEBOOK_DELETE_SWEEP_SECONDS`、
`NOTEBOOK_DELETE_QUIESCE_TIMEOUT_SECONDS`、`NOTEBOOK_DELETE_FINALIZE_TIMEOUT_SECONDS`、
可选 `NOTEBOOK_DELETE_BATCH_PAUSE_MS`）、
`docs/development.md` / `_zh.md`（PG lane 新用例）、
`AGENTS.md` routing table 指向的架构文档（若其中描述了删除事务边界）、
`fangan_done.md`。
⚠ **`fangan_done.md` 无 W-CLI 条目**——由 **PR-1 顺手补记**。

流程照房规：每任务双内部评审（spec-review + code-quality-review，opus）→ 汇成 PR →
`check.sh` + PG lane + codex 闭环 → `gh pr checks` 全 pass + `verify` 成功 → `--rebase` 合入。
PR-2 与 PR-3 有文件重叠（`knowledge_lifecycle.py`、两个 `knowledge_store.py`），**必须串行**；
PR-1 与 PR-2 无重叠，可并行。

---

## 明确不做

- **表分区**：仓库零分区表；给 484GB 的两张大表加 `PARTITION BY` 是全量重写 +
  全索引重建 + 全 SQL 分区键改造，且按 notebook 分区会让分区数无界。
  `TRUNCATE` 因此也不可用于单库删除（全表操作）。
- **删除的撤销/回收站**：`deleting` 不是回收站——清理器一旦开始就在真删。
- **强杀在途重建**：4.2 选检查点而非线程中断（仓库也没有那条通道）。
- **删除进度的用户可见呈现**：见 §5。
- **T-5b**：需等价性证明，登记为 PR-5。
- **批间 sleep 节流**：除非 T-0 显示复制延迟被推高。
- **`notebooks.deleted_at` 列**：`updated_at` + `retained_user_activity.deleted_at` 已够。
- **删除的跨进程通知**：靠扫尾轮询。
- **两张 scratch 表按「删库即整表 TRUNCATE」处理**：需要「聚类不并发」这个假设，本批不做。
- **修 `agent_access_tokens` 的连带删除语义**：既有行为，登记不改。

## 残余债（登记，不在本批修）

1. **存量孤儿行**（`community_members`/`conversations`/`knowledge_object_sources` +
   两张 scratch）：PR-4，量级待 T-0。
2. **存量磁盘产物泄漏**（历史删除留下的三根目录）：PR-4，按「目录名对应的 notebook
   已不存在」清扫，须持锁。
3. **归档的全局过期 GC 搭顺风车**（§T-3.2）：若超预算则摘成独立周期作业。
4. **`object_schemas` 的库级行**：D 类刻意不删。
5. **`agent_access_tokens` 的 `default_notebook_id ON DELETE CASCADE`**（`0001:709-712`）。
6. **`_delete_indexing_pipeline_stages_in_batches` 的级联未分批**
   （`postgres/maintenance.py:530-579` 已自登记）。
7. **`sweep_stale_copies` 自身是无界删除**（`postgres/sharing_store.py:665-671`）：PR-4 收编。
8. **`kg_analysis` 账本负数落后量**（§3 表 #15）：由 PR-2 顺带修。
9. **锁 namespace 的语义扩容**（4.3）：模块名/`application_name`/运维文档口径。
10. **`quiesce` 腿 B 在多 worker 下失效**（T-3.3）：届时的正解是把
    `relinkkg-`/`unifiedkg-`/`conflictresolve-` 的 claim 提升为 durable 行
    （与 `kg_build_jobs` 合流或另建表），**而不是**给删除加锁。
    本批沿用「生产单 worker」的既有部署契约，不加强也不削弱。
11. **`scripts/diag_db.py` 之外的 `scripts/` 谓词**：本批只处置已知的两处
    （`:1530`/`:1543`）；守卫上线后若在 `scripts/` 扫出新站点，逐条处置。

## 存疑与裁决（评审提出，已给出结论）

| 存疑 | 结论 |
| --- | --- |
| 同一把锁被「秒级 build 准入探测」与「分钟级删除」混用，是否伤 W-CLI 准入 | **会**。三处同 PR 改：会话槽预算加删除并发；「已在构建中」文案泛化；`application_name`/模块名/运维文档口径。见 4.3 |
| `get_row(include_copying=True)` 语义变宽 | 全仓**零调用点**。PR-1 直接删除该参数；若测试依赖则改名 `include_hidden_lifecycle`。见 T-1 |
| `notebook_grants` 是否需要形二 | **不需要**。它 PK 是单列 `id`（`0027:75`），走形一；但它确有 nb 前导唯一索引（`0027:76-77`），故从 B 类改判 **A 类**。见 §1.3 |
| 谓词计数 40 的两边拆分（本文 PG20/SQLite20 vs 评审 PG19/SQLite21） | 合计一致（40）。归类分歧不影响交付：**守卫由逐行枚举清单驱动，不由计数驱动**。见摸底 5 |

## 已拍板（2026-09-01）

四项决策已全部拍板并烘进上文正文；本节只记录**决定本身与依据**，
实施细节以正文为准。

### D-1｜生产 `POSTGRES_STATEMENT_TIMEOUT_SECONDS` = **180**

**决定**：生产环境该值已调为 `180`（仓库默认仍是 30，`config.py:1274-1277`）。
全文按 180 推导，30 只作为「仓库默认」出现。

**对设计的三处影响（均已落进正文）**：
1. **定性修正**（摸底 3、§1.1、§1.2）：W1 的问题**不是**「删除从未成功过」，
   而是「180s 预算下 878 万对象的整库级联大概率仍不够（§1.1 算出 2.5–3×10^8 次
   索引项删除，乐观 1µs/项也要 250–300s），且即便偶尔跑完，
   **一个 3 分钟的写事务本身就是病灶**——xmin 停滞 3 分钟、独占写连接 3 分钟、
   四表行锁持有 3 分钟」。与 30s 时代相比，撞墙只是推后 6 倍，
   每次失败前烧掉的 WAL 与 xmin 停滞也是 6 倍。
2. **相位 5 裕度充足**（§T-3.2）：3–10s 对 180s 是 **18–60 倍裕度**，
   超时风险不再是主要矛盾。触发线因此**改为按目标取而非按预算取**：
   **实测 > 30s 即回头改设计**（30s 是「长写事务」的判据，不是预算的分数）。
3. **D-4 语义翻转**：见下。

**基线标定**：批大小与 T-0 的判读基线全部按 180 标定；正文中原先由 30s 推出的
结论已逐条改写为引用实际值。

### D-2｜新开 `delete` 池（容量 1–2，`NOTEBOOK_DELETE_CONCURRENCY`）

**决定**：不复用重活/轻活池，新增第三个池 `_DELETE_POOL = "delete"`，
默认容量 **1**（允许配到 2）。落点与三处配套改动见 §T-4「执行载体」。

**依据**：与仓库既有分池判据同轴（`background_jobs.py:61-73`「两个池而不是一个，
判据是量级差」）。重活池会被小时级整库重建饿死，而删除是用户**已点过确认**的操作；
轻活池里删除的长时 I/O 会挤掉秒级单表投影。删除是「长时、低 CPU、高 I/O」的
第三种量级，独立预算最诚实。
**该容量同时是 §4.3 会话槽预算与 §1.2 连接预算的输入，三处共用同一配置项。**

### D-3｜§3 采用方案 **C**（保行 + seq 归零 + `kg_reset_epoch`）

**决定**：采用 C；配 §3.4 的「epoch>0 才进 version list」与「追加到元组末尾」。
**端口签名变化已确认承担**：`version_signal`（`ports.py:3772`）与
`graph_seq_row`（`ports.py:2058`）各加一项，零松弛棘轮**同 diff 改**，
PR 里引用 W-CLI T-W1 的先例。

**依据**：C 是唯一同时满足三条的方案——① 不动被字节等值测试钉死的
`_state_view` 语义契约（清图后的 state 行与出生行逐字节相等）；
② 跨进程正确（持久化列，不是进程内字典）；③ 一个机制替掉三个补丁
（`knowledge_lifecycle.py:502/507/513` 随之删除）。
A 需要改那份被钉死的契约，B 是 R5 全类扫修明确反对的形态，均已否。
顺带修掉 §3.2 表 #15 的既有假异常（账本负数落后量）。

### D-4｜接受事务级 `statement_timeout` 旋钮，上限 **120s**，默认关闭

**决定**：接受。`NOTEBOOK_DELETE_FINALIZE_TIMEOUT_SECONDS`，默认 `0`（不设置，
沿用池的 180s），只作用于相位 5 那**一个**事务。

⚠ **语义随 D-1 翻转：它现在是「收紧」旋钮，不是「放宽」旋钮。**
v4 是在 30s 预算下把它当兜底豁免；D-1 之后生产预算 180s 已远大于相位 5 的 3–10s，
所以任何有意义的设定值都比池的默认**更短**——它的用途是给这一个我们真正在意的
事务加一道**比池更严**的显式上界，让「相位 5 跑飞了」在几十秒内响亮失败，
而不是拖满 3 分钟。

**三条硬约束**：
1. **默认 `0`**（不设置）——绝不默认改动任何事务的超时。
2. **只作用于相位 5**，用 `set_config(..., true)` 的事务局部形
   （`postgres/database.py:152-166` 先例），绝不碰池的会话级设置。
3. **交叉校验，超限拒绝启动**：设定值必须满足
   `0 < 值 ≤ min(120, postgres_statement_timeout_seconds)`。
   - `≤ 120`：120 是上限帽。超过 120 说明 §T-3.2 的估算模型本身错了，
     那时该回来改设计而不是继续调大这个数。
   - **`≤ postgres_statement_timeout_seconds`**：旋钮的语义是「比池更严」；
     设成比池还大是自相矛盾的配置（事务局部值大于会话值时，真正生效的是谁
     取决于两者的先后顺序，属于容易误读的陷阱），直接拒绝启动。
     生产值 180 > 帽 120，所以现网下**帽 120 是实际生效的约束**；
     若将来有人把池值调到 100，则约束自动收紧到 100。
   - 实现照 `config.py:1353-1363` 的 `validate_chunk_fts_timeout_ceiling`
     ——那是完全同形的既有先例（`POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS` 不得大于
     `POSTGRES_STATEMENT_TIMEOUT_SECONDS`），**新校验并进同一族 model_validator**，
     不另起一套写法。

**已否的备选**：把归档投影再拆（丢掉单快照一致性，退回 v2 的病灶）；
缩短保留窗口内容（改产品的审计承诺）。

---

## 定稿后处理（实现期清单，不影响本设计成立）

下列条目已有明确结论，写在这里是为了实现期不必回头翻评审记录；
它们都不改变上文任何设计取舍。

1. **形二终止条件**：`rowcount == 0` + 连续 3 轮无进展则响亮失败。理由与变异钉
   见 §1.5 与 §7 G1。
2. **§1.1 扇出表已按实测改数**（含 PK）：`knowledge_objects` 10 /
   `knowledge_relations` 9 / `concept_clusters` 8 / `memory_items` 8 /
   `sources` 10（9 非 PK）/ `chunks` 7 / `retained_user_activity` 5。
   v3 用的是非 PK 计数，偏低一档。
3. **`scale_build_cli inspect` 的运维文案**（§3.4 站点 5）落
   `docs/operations.md` / `_zh.md`：「首次上线后，历史上清过 KG 的库会短暂出现
   `version_matches_database: false`，一次重建后消失；这不是损坏」。
3b. **`docs/operations.md:871` 的 `statement_timeout` 段补一句**：那里写的 30 是
   仓库默认，**生产实际值见部署配置（当前 180）**——否则读者会照 30 推导
   （D-1 暴露的文档缺口）。
4. **`quiesce` 腿 B 的部署依赖**在两处点名：相位 2 的实现注释、
   `docs/operations.md` 的多 worker 警告段。
5. **三条新索引**同步进 `scripts/build_hotpath_indexes.py` 的离线通道（§1.4）。
6. **迁移号复核**：`0047` / `_migration_68` / `SCHEMA_VERSION 68` 按动手当天的
   trunk 复核，不照抄本文。
7. **授权谓词并入**（codex #653 R2，PR-1 内已落地，见 §T-1.1）：§4.1 互斥矩阵
   「谓词即闸」原表述只对目录寻址成立，直连资源端点走 `NOTEBOOK_READ_SQL` /
   `NOTEBOOK_ADMIN_SQL` / `NOTEBOOK_WRITE_SQL`，T-1 折叠 40 处读侧站点时未覆盖
   这三条——已在同一 PR 补齐（两条各追加 `AND NOTEBOOK_LIVE_SQL`），对
   `copying` 是闭合既有不一致，对 `deleting` 是行为零变化。Memory 的对应缺口
   （`memory_store.py` 自己的 owner∨成员∨授权边组合，未经这三条谓词）**登记
   不改**，理由与取舍见 §T-1.1 末段，留给 T-2/T-3 落地那个 PR 一并处理。

## 实现期勘误（PR-3 阶段 B，2026-09）

以下条目是实现期发现的**正文错误**（不是"定稿后处理"那类补充说明），发现
途径统一是：Phase B 把 §1.3 的 A 类表逐条接成相位 3 的直删单元后，**一条
与本设计无关的既有测试**（`tests/test_admin_questions.py::test_admin_
questions_combines_ask_and_report_with_filters`）意外变红——不是 Phase B
自己新写的用例先发现的。

### 勘误 1｜`answers` 是第五张归档输入表，正文遗漏

**错误**：§1.3 把 `answers` 列进 A 类 52 张表的直删名单，未加任何特殊说明；
§T-3.2 步骤④「删这四张表的行」与其成本表都只数 `ask_jobs`/`sources`/
`source_paper_meta`/`reports` 四张。

**实际情况**：`NotebookStore._retain_user_activity_before_delete`（两侧
后端）的 ask 投影经 `LEFT JOIN answers a ON a.id=j.answer_id` 读
`answers.question`，用 `COALESCE(NULLIF(a.question,''), j.question)` 优先
取回答自己的（通常更完整的）问题文本。若相位 3 把 `answers` 当成普通 A 类
表提前删掉，这个 JOIN 会读到空值，归档出的 `retained_user_activity` 行
静默退化成更短的 `ask_jobs.question`——直接违反 G1/G3 的「归档等价性」
逐字段相等要求。

**修正**：

- §1.3 A 类 52 张表名单**删除** `answers`；A 类改记为 51 张，`answers`
  单独归为**第五张归档输入表**，与 `ask_jobs`/`sources`/`source_paper_
  meta`/`reports` 同组，行留给相位 5，靠 `DELETE FROM notebooks` 的最终
  级联清空（`answers` 到 `notebooks` 的 FK 是 `ON DELETE CASCADE`，同
  `source_paper_meta` 今天的处理方式——两者都不在
  `delete_row_and_orphan_embeddings` 里被显式 DELETE 过）。
- §T-3.2 步骤④改为「删**五张**表的行」，成本表追加 `answers` 一行：量级
  与 `ask_jobs` 同阶（一次 ask 至多一个 answer），索引数 3
  （`pk_answers`/`idx_answers_conversation`/`idx_answers_nb`，
  `0001_initial.sql`），对总成本估算的影响可忽略（`ask_jobs` 本身的量级
  已经涵盖同阶开销）。
- `feedback`（FK `answer_id` CASCADE）**不受影响**：归档投影从不读它，
  仍留在相位 3 的直删名单里（Group A，排在其余表之前）——`feedback` 先清
  空后，相位 5 级联到 `answers` 时，`feedback` 那一路探查是零命中，不会
  因为 `answers` 改判而退化。
- §7 G3「终态 61 张相位 3 表 + 4 张相位 5 表 + 6 张闭包外表全为 0 行」改为
  「60 张相位 3 表（65 闭包 − 5 张归档输入）+ 5 张相位 5 表 + 6 张闭包外
  表全为 0 行」，闭包总数 65 不变（52 A 改 51 A + 1 张挪去相位 5，B 类 13
  不变）。

实现侧的完整记录（含发现经过、`notebook_delete_tables.py` 模块 docstring
的对应条目、回归钉测试）见 PR-3 阶段 B 的实现报告与
`backend/app/services/notebook_delete_tables.py`/
`backend/tests/test_notebook_delete_review_fixes.py`。

### 勘误 2｜相位 5 时长的生产尺度实测正式挪 T-0；T-5a 与既有测试冲突，未落地

**背景**：`docs/development.md`/`docs/development_zh.md` 与本文档此前均未
给出相位 5（finalize）在生产尺度库上的实测耗时——§1 开头即声明「本节不编
数字，逐表精确行数与逐批计时由 T-0 实测回填」，相位 5 也不例外。PR-3 阶段
B 复查（双内部评审合并修复单 P2-h）要求「PG lane 中型夹具打点入测试输出」，
已落地：`backend/tests/postgres/test_notebook_delete_rows_and_files_pg.py::
test_medium_library_covers_a_class_b_class_and_closure_external_on_postgres`
用 monkeypatch 包住 `_phase_finalize` 打印相位 5 耗时（毫秒级，仅供 CI 输出
可见性追踪，不作为断言阈值）。**正式登记**：这个中型夹具的对象/行数远小于
生产库（单条来源、单条 memory item、单张 3 行 knowhow 表），其耗时数字
*不能*外推到生产尺度；相位 5 在生产尺度下的真实耗时测量，与 §1 其余「本节
不编数字」的量级一样，正式归入 **T-0**（前置只读测量脚本任务），不在
PR-3 阶段 B 范围内。

**T-5a 未落地，登记为阻塞项**：§T-5 的「T-5a（做）」要求把
`delete_notebook_graph_rows` 的无界 DELETE（当前 13 条显式语句 + 1 条
`unified_kg_state` upsert——正文「11 条」的计数在 R1 给
`_GRAPH_RESET_TABLES` 补齐 `kg_community_edges`/`kg_source_profiles`
两张明细表后已经过期，这里一并勘误）改写成 §1.5 形一/形二的分页删除
循环，每批一事务。PR-3 阶段 B 复查中评估此项时发现：

1. `delete_notebook_graph_rows` 目前是 `@staticmethod`，接收调用方已经开
   好的单个 `db` 连接（`knowledge_lifecycle.py:540` 在
   `with self._write() as db:` 内调用）；改成分页循环意味着这个函数必须
   自己拥有多个独立事务（`database.write()` 每批一次），不能再依赖调用方
   传入的单一连接——PostgreSQL 的 `PostgresDatabase.write()`
   **禁止重入**（`_WRITE_ACTIVE` 哨兵，见 `postgres/database.py:560-563`
   的 `NestedPostgresWriteError`），所以调用方那层 `with self._write()`
   必须整体拆掉，不能只在内部“悄悄”多开几个事务。
2. `backend/tests/test_kg_mutation_phase_matrix.py::
   test_delete_delegates_in_write_then_commits_before_cache_invalidation`
   —— 这条测试自己的 docstring 标注「batch-3-W1 PR-2 R1 (P0-1, restored
   after review)」，即上一轮评审曾经就"要不要在这里保留单一事务语义"
   来回过一次，最终**明确保留**了「`delete_notebook_kg` 在一个
   `write()` 事务里调用 `delete_notebook_graph_rows`，提交后才做缓存
   失效」这条不变量，并用连接身份断言（`events[0][1] ==
   events[1][1] == events[2][1]`）把它钉死。T-5a 要求的「原子性从一个
   事务变成 N 个事务」与这条已经被上一轮评审明确重申过的不变量**直接
   冲突**——不是简单的测试更新，而是要在这一轮里推翻上一轮评审刻意做出
   的取舍。

鉴于 `delete_notebook_graph_rows` 是 `rebuildkg-` 后台作业的生产调用路径
（与 PR-3 阶段 B 本身的笔记本删除作业流水线无关），且这条不变量此前已被
评审明确讨论并保留过一次，本轮实现方判断这不是可以按「计划已定」直接执行
的机械改动，而是需要架构决策的取舍——**T-5a 本轮未实现**，`delete_notebook_
graph_rows` 仍是修改前的 13 条显式 DELETE + 1 条 upsert（无分页）。这一
条与 P2-f 的裁决冲突留给下一轮评审/规格所有者拍板：要么明确要推翻
P0-1 的单事务不变量（连带更新
`test_delete_delegates_in_write_then_commits_before_cache_invalidation`
与 `test_kg_mutation_phase_matrix.py` 里的相关连接身份断言），要么把
T-5a 的范围重新界定为不影响这条不变量的形态（例如只分页那些不参与
"commit 后立即失效缓存"时序契约的表）。

### 勘误 2 裁决（T-5a 独立 PR，2026-09-02）

**取第二条路：范围重界定为「预排水 + 原子终局」，P0-1 不变量原样保留。**

`delete_notebook_kg` 在既有单事务终局**之前**加一段分页预排水
（`knowledge_lifecycle._drain_graph_rows_before_reset`）：按与终局 DELETE
逐字节相同的 (表, 谓词) 登记表（两后端各自的 `_GRAPH_DRAIN_STEPS`，
反漂移镜像测试钉住一一对应），每批一个独立 `write()` 事务删一页
（SQLite rowid 形一 / PG ctid 形二，页 2000 行），把每张表的匹配行压到
≤阈值（=页大小）后停手；终局事务里的 13 条 DELETE 因此只处理有界残余，
其原子提交语义、连接身份、「提交后才失效缓存」时序**逐字节不变**——
P0-1 钉测试无需任何修改。要点：

- **小图快路径**：首个探针只读（`graph_drain_backlog`，LIMIT 1 OFFSET
  threshold 点探），全表 ≤阈值时零排水写事务，与 T-5a 之前逐字节同形。
- **census 纪律**：每个非空排水批同事务 `mark_dirty` bump
  `kg_mutation_seq`（否则两次 mid-drain 读会把不同的半清图缓存进同一个
  (epoch, seq) 键）；终局 upsert 照旧 seq→0、epoch+1，排水期键永不别名
  重置后内容。kg_mutation.py FULL CENSUS 已登记新站点。
- **§4.2 检查点**：每批之前查 `_notebook_deleting`，墓碑落地就地
  `NotebookDeletingAbortsMaintenanceError`（相位 3 拥有这些行）。
- **响亮失败**：探针命名某表而分页删 0 行、连续 3 次 → RuntimeError。
- **可见性代价（接受）**：排水期读者看到渐缩的图——与终局提交后
  「空图直到 rebuild 填回」的既有退化窗口同质，只是把窗口向前延长了
  排水时长；rebuild 流程本来就处于 dirty 态。
- **中断残余（接受，双评审 #5）**：排水中途中断（墓碑中止/停摆响亮失败/
  超时/进程死亡）留下一个**形状一致的半清图**——knowledge_objects 页按
  `_delete_object_id_batch` 同形连带同事务清 embeddings/簇成员/kos，
  所以不产生孤儿簇行或悬空 embedding；SQLite 的 `kg_objects_fts` 影子刻意
  不逐页清（与 `_delete_object_id_batch` 同款排除），中断后其悬空行留到
  下一次完整 delete/rebuild 收口；`unified_kg_state` 此时 dirty=1、epoch
  未推进、计数为删前旧值。重试 rebuild 会从头幂等重排。
- **counts 契约**：排水行并回 `delete_notebook_kg` 返回的 counts（含
  knowledge_objects 页连带删除的从属表行数），调用方看到的仍是全量。
