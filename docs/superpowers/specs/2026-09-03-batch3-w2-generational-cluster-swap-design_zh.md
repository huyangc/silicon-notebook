# 批 3·W2：簇图切换代次化 + rebuild 并发写不丢（设计规格 v3·两轮内评后定稿候选）

> 对应审计 WR-5（及 WR-4 的残余核销）、修复计划「W2 重抽与簇切换代次化」。
> 三条红线不变：不降检索性能、不降 KG 抽取性能、不改问答结果质量。
> 行号基于 `origin/master`（PR #666 合入后）复核。
> v1→v2：两路 opus 内评（并发/恢复 + 红线/契约）全部裁决吸收；
> v2→v3：第二轮复评（专攻 v2 新机制）2×P0、8×P1、7×P2、3×P3 全部裁决吸收，
> 复评确认 v2 七项决策零推翻。两轮处置表见文末「内评裁决记录」。

## 目标与非目标

**目标**（对齐修复计划对 W2 的原始要求）：

1. 簇图派生表（`concept_clusters`、`communities`/`community_members`）的重算
   切换从「DELETE+重插」改「写新代次 + 翻指针」；
2. `concept_clusters` 上的 advisory lock 收窄到指针翻转微事务——增量融合的
   `append_clusters` 不再撞分钟级持锁切换（链 c 结构性消失）；
3. 修掉「rebuild 期间的并发写被静默丢弃」：链 a（融合行被切换吞掉）与
   链 b（抽取循环漏源）都闭合；
4. 读侧任何时刻只见完整代次，四 object_type 一把翻（修跨类型半态）。

**非目标**：

- 不动 `delete_notebook_kg`（T-5a #663 定形；本设计对其唯一触碰是终局
  UPSERT 重置段的受控扩列，见 §2.3）；
- 不给主表（objects/relations/embeddings）加行级代次（WR-4 已由 T-5a 核销）；
- 不做多 worker durable claim（W1 残余债 #10 原样保留；但见 §2.1——本设计的
  **数据级取号 CAS** 顺带给「离线 recluster CLI 绕过进程内单飞」补了跨进程闸）；
- `communities` 的板块报告（`update_community_report` 的 LLM title/summary）
  随代次退休——**现状即如此**（DELETE+INSERT 同样丢），本设计不承诺保住，
  登记残余债。

## 摸底结论（v2，内评复核后修正）

### 摸底 1：WR-4 已核销

`delete_notebook_kg` 已是「N 个有界排水事务 + 单终局事务」（T-5a #663）。
W2 对 WR-4 只记核销，不改代码。

### 摸底 2：WR-5 现行站点与写者普查（生产口径）

- `swap_cluster_map_from_scratch`（`postgres/unified_kg_store.py:539-565`）：
  per object_type 一事务，xact advisory lock 包住 DELETE+INSERT…SELECT；
  调用方 `_write_cluster_map_streamed`（`knowledge_lifecycle.py:4629-4710`）×4。
- `replace_communities`（`postgres/unified_kg_store.py:864-894`）：按
  `(notebook_id, level)` DELETE+INSERT；**其调用处是一个被 codex 钉过的
  合并发布事务**（`knowledge_lifecycle.py:5786-5850`：replace + 依赖板块
  账本作废 + `set_community_seq` + 三张分析产物表重写，同一 `_write()`），
  不可拆散。
- `concept_clusters` 的生产写者共三类：rebuild 切换、`append_clusters`
  （增量融合）、per-source 清理删除（`clear_source_graph_state`/
  `prune_cluster_rows_for_source`/孤儿清扫分页删）。`write_clusters`/
  `replace_cluster_rows_streamed` 为 test-only（`test_incremental_fuse_perf.py:24`
  登记），无生产调用者。
- `rebuild_canonical_relations`/`rebuild_mention_bridge` **PR-1 摸底结论**：
  确为整 notebook DELETE+INSERT 单事务切换（`replace_canonical_relations`/
  `replace_mention_bridge`，无 advisory lock），但无 `append_clusters` 型
  增量并发写者——链 a 不适用，登记 W2 尾款（残余债 #3），不并入 PR-2。
  `replace_communities` 调用方**不持任何 advisory lock**（仅发布事务隐式
  行锁 + `board_partition_still_holds` 的一次性 FOR SHARE 探测 + 进程内
  维护槽；离线 `recluster_kg --force` 绕过维护槽的缝隙由 PR-2 的数据级
  取号 CAS 闭合）。

### 摸底 3：关键 schema 约束（v1 漏查，两路内评实测补上）

1. **`uq_clusters_notebook_type_member`**（`0007_cluster_membership_unique.sql`，
   SQLite 孪生 `migrations.py:1827`）：`(notebook_id, object_type,
   member_object_id)` 唯一——**双代共存在物理上被禁止**，今天合法全靠
   同事务先 DELETE。代次化必须给这条唯一索引扩列。
2. **`idx_clusters_nb_canonical_member`**（0043 覆盖索引）：
   `cluster_member_rows`（`unified_kg_store.py:298-304`，graph_retrieval 的
   `_ppr_graph`/`_fed_rxgraph` 消费）是教科书 Index Only Scan。内评在
   50 万行复制品上实测：只加 generation 残余谓词（全 0 稳态）就退化成
   Incremental Sort + Index Scan、buffers 8.8×；双代窗口 68× buffers；
   generation 做 INCLUDE 列后恢复 IOS，稳态 ≈1×、双代 ≈2.4×。
3. **`version_facts` 的簇分量已在 manifest 版本向量里**
   （`postgres/index_projection_store.py:162-164`：
   `SELECT COUNT(*), MAX(created_at) FROM concept_clusters WHERE notebook_id=?`，
   docstring 明言 on-disk manifest.version 兼容性）。双代窗口 COUNT 翻倍 =
   W1 §3.4 的重建风暴。同族：`concept_clusters_count`（跳过闸第二腿，
   `knowledge_lifecycle.py:4749`）、`distinct_cluster_count`（`:3975`）。
4. **拷贝与合库**：深拷贝快照 `SELECT * FROM concept_clusters`
   （`postgres/sharing_store.py:161`）会原样带走 generation，而副本**刻意
   无 `unified_kg_state` 行**（`notebook_sharing.py:63-79`/`494-505` 的既有
   红线）；`scripts/merge_dbs.py` 三表整表导入 + `KG_STATE_TABLES` 清空
   state 行。两条实产路径都会制造「行有代次、库无指针」。
5. **读者普查量级**（PR-1 实测勘误）：三表 SQL 站点约 150 处/29 文件
   （含声明驱动的动态 SQL 表名清单）。**LEFT JOIN 红线清单修正为 4 函数
   7 站点/侧**：`canonical_relation_seed_rows`(2)、`community_graph_rows`
   (2)、`relation_endpoint_name_rows`(2)、`source_canonical_rows`(1)——
   v2 点名的 `mention_seed_rows` 实为 inner join（B 类 rebuild 流内部读，
   移出红线清单）；`relation_endpoint_name_rows`/`source_canonical_rows`
   为普查新增收录。同语句双引用先例：`query_store` 的 NOT EXISTS 内层用
   `mc.generation = c.generation` 相关对齐（零新参数，同代整簇排除）。
   普查守卫落地为 `tests/test_cluster_generation_census.py`（逐文件表出现
   数+谓词数登记，三分类注记，C 类豁免非空）。
6. **SQLite 事务语义**：`with conn:` 是 deferred；首条 DML 前的 SELECT 不在
   写事务里，仓库已有 `begin_immediate` seam（`sqlite/database.py:676-687`）
   与离线共库写者实例（`recluster_kg.py`、batch_ingest）。
7. **`recluster_kg.py:25` 直连 `rebuild_unified_kg(force=True)`**，不经
   `KgMaintenanceJobs`、不登记 `kg_building`——「同库并发 rebuild」是实存
   受支持路径（scratch 表 run_id 隔离的注释即为其服务）。

### 摸底 4：三条丢失/竞态链（v2 修正链 b 机制）

- **链 a**（结构性）：融合的 `append_clusters` 行落在「种子快照~切换提交」
  窗口内即被 DELETE 吞掉。关键补充事实：`append_clusters` bump 的是
  `cluster_mutation_seq`，**不动 `kg_mutation_seq`**（`kg_mutation.py:104`）；
  丢失行在退休代里**完整存在**（本设计催收机制的基石，见 §1.5）。
- **链 b**（时序）：漏源机制是「rebuild 开始前已上传、`has_elements` 在
  rebuild 期间才变真（解析完成）」的来源——keyset 游标已越过其
  `(created_at,id)` 位置，本轮永不再访。~~新上传落在游标身后~~（v1 描述
  有误：新上传 created_at 在游标前方，会被扫到）。
- **链 c**：融合撞切换持锁 → 5s lock timeout（`postgres_lock_timeout_seconds`
  默认 5）→ 异常在 `source_ingestion.py` fail-open 吞掉。

### 摸底 5：先例与红线（同 v1）

行级世代列（`source_generation`）、desired/published CAS
（`indexing_pipeline_generation`）、工件侧 `.tmp`+rename、
`kg_reset_epoch` 的「epoch>0 才进版本向量」双规则（W1 §3.4）。

## §1 目标形

### 1.1 schema 与索引整改（D-W2-1 v2 重裁）

```sql
-- 迁移 00XX（编号按合入时 master 取号；SQLite 孪生同批）
ALTER TABLE concept_clusters   ADD COLUMN generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE communities        ADD COLUMN generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE community_members  ADD COLUMN generation BIGINT NOT NULL DEFAULT 0;
ALTER TABLE unified_kg_state
  ADD COLUMN cluster_generation          BIGINT NOT NULL DEFAULT 0,  -- published
  ADD COLUMN community_generation        BIGINT NOT NULL DEFAULT 0,  -- published
  ADD COLUMN derived_generation_counter  BIGINT NOT NULL DEFAULT 0,  -- 只增取号器
  ADD COLUMN derived_building_generation BIGINT NOT NULL DEFAULT 0,  -- 在飞代(0=无)
  ADD COLUMN derived_building_claimed_at TEXT,                       -- 在飞认领墙钟
  ADD COLUMN derived_catchup_from        TEXT;                       -- 催收落库标记
```

**索引整改（一次付清，代次化的必要代价）**：

1. `uq_clusters_notebook_type_member` → 重建为
   `(notebook_id, object_type, member_object_id, generation)` UNIQUE
   （摸底 3-1：否则第一条新代 INSERT 即撞约束）；
2. `idx_clusters_nb_canonical_member` → PG 重建为同键 +
   `INCLUDE (generation)`；SQLite 无 INCLUDE，generation 做尾键
   （摸底 3-2：否则稳态就丢 Index Only Scan）；
3. `idx_clusters_nb_created` → 同款 INCLUDE/尾键（复评 P1-3/P2-3：
   `version_facts` 簇分量与 `concept_clusters_count` 两个**活着的**聚合
   读者按「版本身份只数 published 代」红线必须加谓词，不整改就丢 IOS；
   同一条索引也是催收查询有界性的唯一凭据——三用一改）。

三条都走 0043 先例：`scripts/build_hotpath_indexes.py` 式离线 CONCURRENTLY
建新（builder 自带幂等 ADD COLUMN 前置——先行顺序需要尚未迁移的列，实现期
补充）→操作员 DROP CONCURRENTLY 旧名→迁移校验落账；生产 9.65M 行在线建，
不停读写。**实现期勘误（PR-1 内评+实测）**：旧索引不是登记退役债而是**随
迁移直接 DROP，且共五条**——三条被接替者严格覆盖（唯一/两条覆盖），外加
两条严格前缀（0004 的 `idx_clusters_nb`、0039 的 `idx_clusters_nb_canonical`，
后者本就是 0043 登记的退役债）：实测更窄前缀会把带谓词读者劫成
Index Scan+回表过滤，恰是 INCLUDE 要防的回归。其余索引不动。列加法（`ADD COLUMN … DEFAULT 0`）PG11+ 元数据操作、不重写堆
（内评实测 relfilenode 不变）；SQLite ADD COLUMN 同为 O(1)。

- 存量行 generation=0、指针=0。**PR-1 行为等值口径**：行为字节级不变指
  **查询结果**；执行计划因新谓词与索引改型而变，PR-1 的 EXPLAIN pin 钉的是
  「整改后形态 = Index Only Scan 恢复 + 无 Seq Scan 回退」，不是「与整改前
  逐字节同计划」（v1 表述作废）。
- 命名：`generation`（与 `source_generation`/`indexing_pipeline_generation`
  词汇一致）；不叫 epoch。

### 1.2 concept_clusters 写者改造

```
[跳过闸] 先判(照旧);命中直接返回——**取号在跳过闸之后**(复评 P0-2:
         否则每次无副作用刷新烧号占认领)
[取号]   UPSERT 微事务（mark_dirty 的 ON CONFLICT 形状,无行则造行）:
           SET derived_generation_counter = counter + 1,
               derived_building_generation = counter + 1,
               derived_building_claimed_at = <DB 服务端时钟>
           WHERE derived_building_generation = 0
              OR derived_building_claimed_at < now() - KG_DERIVED_BUILD_TTL
           RETURNING counter AS G, cluster_generation AS P,
                     <DB now()> AS TS   -- 催收锚点,服务端时钟(§1.5)
         失败(在飞未过期) → KgMaintenanceAlreadyRunning 同款拒绝
         —— 这是数据级跨进程单飞:连 recluster_kg 离线直连也被闸住(摸底 3-7)
         **认领释放三通道**(复评 P0-2,缺一即整库 409 数小时):
           a) 翻转成功清零(主通道);
           b) **finally 释放**: UPDATE … SET building=0
              WHERE building = G ——CAS 只清自己,被抢占后迟到释放天然 no-op;
              覆盖跳过、异常、翻转 CAS 作废、communities 发布失败全部出口;
           c) TTL 仅为**崩溃兜底**(进程 kill/掉电),取大常数(数小时级,
              数值围栏进部署文档),不承担正常失败路径
[催收欠账] derived_catchup_from 非空 → 先跑 §1.5 的搬运段(上轮翻转后崩溃
           留下的欠账),清标记
[预回收] 有界分页删 generation NOT IN (P, G) 的行(T-5a 排水同款分页;
         唯一回收通道,无 finally 回收——给跨翻转在飞读者一整轮宽限,见 §3)
[写新代] per type INSERT…SELECT …, G AS generation ×4
         —— 不持 advisory lock(与 published 代/并发 append 无行冲突)
         每 type 写段前重读 building(PK 单行读,复评 P2-4):≠G 即被抢占,
         当场作废早停——抢占者预回收扫过的键区不会被再填残行
         SQLite: 每写块首走 begin_guarded_write 接缝(复评 P3-2;
         底层即 BEGIN IMMEDIATE)
[翻指针] 微事务:
           lock_cluster_artifact_types(nb, 全 4 类)   -- 既有 helper,按 key 字节
                                                       -- 排序取锁(40P01 纪律)
           读 state 行(锁后同事务读,见 §2.2 顺序红线)
           UPDATE unified_kg_state SET
             cluster_generation = G,
             cluster_mutation_seq = cluster_mutation_seq + 1,
             derived_building_generation = 0,
             derived_catchup_from = <种子快照时刻>
           WHERE notebook_id=? AND cluster_generation = P
                 AND derived_building_generation = G     -- 双 CAS:指针未动+认领仍在
           零行更新 → 响亮失败(被 TTL 抢占/被 delete 重置),本轮作废不发布
[催收]   §1.5 单遍搬运 → 清 derived_catchup_from
[收尾]   finish_rebuild_state 照旧
```

- 四类一把翻：跨类型半态消失。
- 取号 CAS + 翻转双 CAS + TTL 抢占（`KG_DERIVED_BUILD_TTL`，Settings 项，
  默认按「最长 rebuild + 余量」定，实施期定数并走 deployment 文档数值围栏）
  ——崩溃在飞代既不阻塞后续 rebuild（TTL 后可抢），也不会被并发回收误删
  （回收谓词认 building 列，见上）；抢占留结构化事件。
- `cluster_mutation_seq` bump 在翻转事务（版本身份与指针行变化同提交）。
  **kg_mutation.py FULL CENSUS 红线措辞随之两段式重写**：
  「published 代行变化须同事务推进版本身份；未发布代（generation 既非
  published 亦非 0 存量）的写入**不得**推进版本身份——它对读者不可见，
  bump 反而制造假失效」。写新代段不 bump 即为合规而非违例。

### 1.3 communities 族写者改造

- 同一取号器取 G（同一在飞列——两族共享一次 rebuild 的认领）。
- **翻转事务 = 既有发布事务**（摸底 2）：写新代（communities/
  community_members INSERT，G 代）在事务外先行；发布事务内保持既有内容
  （依赖板块账本作废 + `set_community_seq` + 分析产物重写）并加
  `community_generation = G` 的指针更新。**不追求毫秒级**——该族无并发
  融合写者，锁窗口无收窄需求；原子性优先（kg_analysis 板块缓存签名的
  「replace 与账本作废同事务」不变量原样保住）。
- level 维度（`replace_communities` 按 `(notebook_id, level)` 写）：翻转
  事务内把 published 代中**未被本轮重建的 level** 行复制进 G 代
  （copy-forward）。**复制行重铸板块 id**（复评 P0-1 裁决：`communities.id`
  是单列 PK，同 id 双代必撞；重铸零代价——账本作废与版本记录只在默认层做
  （`knowledge_lifecycle.py:5809-5814`），非默认层板块 id 随代变不影响任何
  签名论证；`community_members.community_id` 随行同步重映射）。今天所有
  调用点 level=0，复制集为空。
- **跳过路径的取号**（复评 P1-1）：`rebuild_communities` 的跳过分支入口
  （`knowledge_lifecycle.py:4776`，无认领）与门面直调各自独立取号
  （P0-2 的 finally 释放使这不再占小时级认领）；被取号 CAS 拒绝时发
  结构化事件区分「被闸」与「真失败」（`:4777-4779` 的吞异常分支同步补
  事件）。
- **`board_partition_still_holds` 替代判据**（复评 P1-6，被打破守卫必须
  与替代同入设计）：代次化后「行还在」不再蕴含「没被换过」（P 代行活过
  翻转）、`FOR SHARE` 无 DELETE 可阻塞——守卫恒真化。替代：
  `_compute_kg_analysis` 读取板块时**连带记下当时的 `community_generation`**
  （穿参一个整数），发布事务内重读 state 行比对，变了即
  `partition_replaced_under_us` 放弃（既有放弃路径复用）。读侧只 SELECT
  state 行不加锁——与 `discard → set_community_seq` 的既有锁序无交叉，
  `:958-962` 的死锁论证不被触碰；零板块档照旧免查（其两条理由中
  「无行可锁」在新判据下改述为「无代次可记,比对恒等」）。
- `communities` 族残代回收（复评 P1-8）：预回收段**三表通用**
  （concept_clusters 在 §1.2 写者内；communities/community_members 在其
  发布路径前同款有界分页 + 启动恢复点名三表）。发布事务放弃/回滚留下的
  新代行由此通道清理。
- `communities_count` 等闸腿按 published 代计数（普查 A 类）。

### 1.4 读者改造与普查（三分类）

**默认读者形态**（方式 2）：谓词内嵌标量子查询

```sql
AND generation = COALESCE(
  (SELECT cluster_generation FROM unified_kg_state u WHERE u.notebook_id = %s), 0)
```

**子查询用绑定参数（`%s`/`?`），不写相关引用**（复评 P1-4：
`u.notebook_id = t.notebook_id` 是相关子查询，PG/SQLite 都逐行求值；
绑定参数形式是 uncorrelated InitPlan，一次求值）。单语句单快照（READ
COMMITTED 下指针与行同快照读取，不存在「先读指针后读行跨翻转」的撕裂）；
COALESCE 兜「无 state 行 ⇒ 代次 0」的显式契约（配合 §1.6 归一，副本/
合并库可读）。方式 1（从已读 state 行传入）仅允许「同一条 SQL 已 JOIN
state 行」的站点。
**LEFT JOIN 站点红线**（复评 P1-4，质量级）：三处倍增站点的代次谓词
必须落在 **ON 子句**——落 WHERE 会把 LEFT JOIN 退化成 INNER JOIN，
静默丢掉端点无簇行的关系。普查守卫按此单列一类检查。

**普查三分类**（PR-1 硬交付，形如 kg_mutation.py FULL CENSUS）：

| 类 | 语义 | 处置 |
| --- | --- | --- |
| A·published 读者 | 检索/计数/版本源/闸腿 | 加指针谓词；**每次表出现各配一个**（同语句双引用如 `query_store.py:106/113` 两处都配） |
| B·目标代写者 | rebuild 写新代、append、催收 | 写显式代次参数 |
| C·跨代维护 | per-source 清理删除三站点、孤儿清扫、`delete_notebook_kg` blanket、诊断脚本只读 | **显式豁免清单 + 逐条理由**（如：删源必须跨代删,否则在飞代留死成员行,破「零 orphan 生产者」不变量） |

守卫测试按**表出现次数**配对判定（非按语句），豁免清单非空、逐条带理由；
新站点未登记即红。必点名站点：`version_facts` 簇分量、
`concept_clusters_count`（跳过闸腿——副本/合库指针缺失时若不加谓词会
永远误判「已建过」）、`distinct_cluster_count`、三处 LEFT JOIN 倍增站点、
孤儿清扫游标（见下）。

**孤儿清扫 keyset 修正**：四列唯一索引后 `(object_type, member_object_id)`
不再唯一，游标加 generation 分量（`(generation, object_type,
member_object_id)`），清扫保持跨代（C 类豁免）。

**新增红线**：「版本身份不得被未发布代污染」——一切进 manifest/版本向量/
memo 键的簇计数与时间戳只数 published 代（这是 W1 §3.4 重建风暴红线在
本设计的化身；两指针本身不进 `version()` 元素）。

### 1.5 催收（链 a 闭合，v2 全面简化）

关键事实（摸底 4）：翻转后 append 落 G 代（§2.2 锁序保证），此前窗口内的
融合行**完整躺在 P 代**且集合冻结。催收因此是**单遍有界搬运**，无水位账本、
无收敛轮：

```
锚点 TS = 取号事务里的 DB 服务端时钟(复评 P1-7:「种子快照时刻」不良定义
  ——种子流逐批提交无快照瞬间;取号时刻可证早于本轮任何一次种子读,保守正确。
  行的 created_at 来自各写进程的应用时钟,跨进程有偏斜 → 催收谓词用
  TS - KG_CATCHUP_SKEW_SECONDS(默认 300,数值围栏)兜偏斜;搬多了幂等无害,
  漏搬才是洞——余量方向保守正确)
翻转事务已落 derived_catchup_from = TS
催收段(翻转后 / 或下轮取号后发现欠账时):
  SELECT DISTINCT c.member_object_id, c.object_type, o.name, o.payload
    FROM concept_clusters c JOIN knowledge_objects o ON o.id = c.member_object_id
   WHERE c.notebook_id=? AND c.generation = 旧P
     AND c.created_at >= TS - SKEW          -- 有界:走 idx_clusters_nb_created
  (期间被删的对象 join 不到,天然跳过)
  逐 object_type 调既有安置原语(place_new_concepts,四类同一函数换 seed_fn,
   纯 Python 无 LLM)→ append 进 G 代
  完成 → 清 derived_catchup_from
```

- **催收成本照实 = 一次增量融合**（复评 P2-2）：安置需要 G 代该类型的
  簇名切片（`incremental_cluster_rows` 的 DISTINCT 扫描，仓库已登记为
  不可收窄），四类各一次——与一次 `incremental_fuse_source` 同量级，
  只在 rebuild 收尾发生。
- **催收是 published 代写入，必须推进版本身份**（复评 P1-2）：复用
  `append_clusters` 的 `added>0 → bump cluster_mutation_seq` 判据，
  并作为一行登记进 §1.2 的两段式红线表——否则键控缓存吐翻转瞬间的旧图。
- 幂等探针按代收窄（四列唯一索引后自然成立）——v1 的「探针跨代失明成
  静默 no-op」结构性消失，且催收正确性**不再依赖回收先后**。
- 崩溃恢复：`derived_catchup_from` 落库；翻转后崩溃 → 下轮取号先补欠账再
  预回收（回收谓词之外再加「catchup 标记在时不回收其旧 P 代」的保护）。
- Tier2 候选（`concept_merge_candidates`）不代次化，merge/LLM 成本天然幸存。
- 催收后仍可能有极窄窗口新对象未入簇 → `cluster_input_version` 失配 +
  结构化事件；**措辞如实**：等待下一次用户触发的 rebuild 收敛（`dirty` 无
  自动消费者，不虚构自动收敛）。

### 1.6 拷贝与合库（新节，内评 P0）

- `copy_notebook`：**只拷 `concept_clusters` 一张**（复评 P2-6 事实修正：
  `_COPY_TABLES` 不含 communities 两张，副本板块由首次 rebuild 重建）。
  其快照查询加 published 代谓词（只拷发布代，免拷残代/在飞代）；行重映射
  循环里 `generation = 0` 归一；`_COPY_VALIDATED_TABLES` 的 COUNT 校验
  两侧口径同步（源侧带谓词）。副本无 state 行 + 行全 0 + 读者 COALESCE
  兜 0 ⇒ 副本簇图照常可读。
- `scripts/merge_dbs.py`：三表导入时 generation 归一 0（沿用其 table_columns
  按列处理骨架）；顺带修头注释陈旧的 `SCHEMA_VERSION=67`（现 69）。
- 守卫：拷贝/合库后「簇行可读性」pin（无 state 行 + 归一行 → 读者非空）。

### 1.7 链 b 闭合（抽取补漏轮，v2 修正谓词与机制）

抽取循环收尾后追加有界补漏轮：**直接调 `_kg_target_batches(notebook_id,
"incremental")`，不复述谓词**（复评 P1-5：真实 incremental 分支还排除
`is_partial`/`analyzed_empty`——漏掉后者会让零对象来源每轮重付模型钱；
谓词已两次被复述写错，加结构守卫钉「主循环与补漏轮共用同一谓词函数」）。
非空则抽取，至多 3 轮；耗尽仍非空 → 结构化事件 + job 记 partial。测试注入形态（v1 有误）：**created_at 早于
当前游标、元素在循环中途才落齐**的来源（新上传天然会被活键集扫到，
不构成变异杀伤）。补漏轮产生的新对象由其后的融合正常入簇（G 代已发布，
append 直写 G）。

## §2 并发与互斥

### 2.1 互斥矩阵（v2）

| 交叉 | 闸 |
| --- | --- |
| unifiedkg- × unifiedkg-（含离线 CLI） | **数据级取号 CAS**（§1.2，跨进程）+ 既有 KgMaintenanceJobs（进程内先挡） |
| buildkg- × unifiedkg-/relinkkg- | 进程内交叉检查：`KgMaintenanceJobs.claim` 查 `kg_building`，`prepare_notebook_kg_job` 查实例 A 的 `jobs`；409 文案泛化 |
| standalone `delete_notebook_kg` × unifiedkg- | delete 的认领同样查实例 A `jobs`（进程内）；跨进程兜底 = 翻转双 CAS（delete 终局把 building/指针重置 → 在跑 rebuild 的翻转零行更新响亮作废） |
| 多 worker | 进程内检查失效——同 W1 残余债 #10，登记不做；数据级 CAS 项（取号/翻转）天然跨进程有效 |

### 2.2 锁与顺序红线

- **顺序**：先取 advisory lock（多把按 key 字节排序——`lock_cluster_artifact_
  types` 既有实现；#663 R20 的 40P01 纪律），再在**同一事务**内读指针，
  再写行。append 侧与翻转侧同规。违序即 P0（源码结构守卫钉住）。
- SQLite：append 写块、翻转、每个回收/催收批次块首 `begin_immediate`
  （deferred 事务的「读判据~首写」窗口，`sqlite/database.py:676-687` 的
  既有 seam；离线共库写者实存）。
- `lock_cluster_artifact_type` 语义收窄仅限 concept_clusters 族；
  communities 族翻转 = 发布事务（§1.3），不承诺毫秒级。

### 2.3 与 delete_notebook_kg

终局 UPSERT 重置扩列（受控变更，T-5a 出生行等值测试同步）：
`cluster_generation=0, community_generation=0, derived_building_generation=0,
derived_building_claimed_at=NULL, derived_catchup_from=NULL`；
**`derived_generation_counter` 不归零**（与 `kg_reset_epoch` 同款只增——
归零会让计数器重爬撞上终局快照后幸存的并发写行代号，D-W2-3 即被打穿）。
等值测试从显式列白名单改为**全列比对 + 显式排除表**（防新列静默漏钉）。

## §3 红线论证（v2）

1. **检索**：索引整改后稳态计划 = 现状同形（IOS 恢复，实测 ≈1×）；双代
   窗口 ≈2.4×（实测，INCLUDE 后）且窗口 = 单次 rebuild 时长、单飞不叠加；
   无 finally 回收 ⇒ 跨翻转在飞请求整轮宽限内读完整 P 代。版本身份只数
   published 代 ⇒ 无 manifest 漂移/重建风暴。EXPLAIN pin 双侧（PG twin +
   SQLite `INDEXED BY` 站点回归）。
2. **抽取**：append 事务新增成本 = 锁内一次 state 行 PK 读；切换不再持锁
   写 9M 行，融合 5s 超时结构性消失；催收单遍有界、补漏轮走增量谓词
   （全量重抽的反转风险已在 §1.7 排除）。
3. **质量**：完整代次可见 + 两条丢失链闭合 + 拷贝/合库代次归一。跳过闸
   判据不变（其簇计数腿按 published 代后，副本/合库场景从「误判已建」
   变为正确的全量首建）。

## §4 崩溃与恢复矩阵（v2 扩格）

| 崩溃点 | 状态 | 恢复 |
| --- | --- | --- |
| 取号微事务内 | 事务原子，无副作用 | 无 |
| 取号后（号已烧）写新代前/中 | G 残行、building=G、指针 P | 读者无感；正常失败路径由 finally 释放（P0-2 通道 b）；进程 kill 由 TTL 兜底抢占；被抢占者每 type 写段前重读 building 早停（复评 P2-4），残代由抢占者预回收 + 启动恢复清 |
| 翻转微事务内 | 原子，要么 P 要么 G+标记 | 无残 |
| 翻转后催收前/中 | 指针=G，`derived_catchup_from` 在 | 下轮取号先补欠账（落库标记，进程重启不失账） |
| 翻转后 finish 前 | 内容已发布，`cluster_input_version`/counts 陈旧 | 下次非 force rebuild 全量重算（正确但贵）+ 状态接口计数陈旧——登记为已知格 |
| 回收批次中 | 残代部分存留 | 幂等重删（预回收/启动恢复） |
| 启动恢复 | 残代清理 | **入口走 state 行**（复评 P2-1：先扫 `unified_kg_state` 取 `derived_generation_counter > 0` 的库——一本一行，比三表 O(表) 扫便宜一个量级），逐库有界分页删；每页尺寸按 `postgres_statement_timeout_seconds`（默认 30s）定（T-5a 排水同一约束）；**硬预算**（库数×页数上限，剩余留给各库下轮预回收）；锚在 `postgres/maintenance.py::recover_interrupted_jobs` 的 scratch TRUNCATE 步骤之后（v2 误写为 startup_warmup）、`mark_ready` 前；谓词认 building 列与 catchup 标记（不删在飞/欠账代）；覆盖**三表** |

## §5 测试与验收门（v2）

1. 读侧永不见半态：并发读者跨翻转断言「完整 P ∨ 完整 G」且四类同代；
   含「同请求先读指针后翻转」形态（方式 2 单语句单快照的正确性）；双侧。
2. 链 a pin：快照后注入 append（落 P 代）→ 翻转 → 催收后 G 代含其安置
   结果；删催收段 → 红。崩溃恢复：翻转后 kill → 下轮补欠账 → 绿。
3. 链 b pin：注入 created_at 早于游标、元素中途落齐的来源 → 补漏轮抽到；
   删补漏轮 → 红（v1 的注入形态杀不死变异，作废）。
4. 锁序/结构守卫：advisory lock 只出现在翻转微事务与 append 事务
   （**预留豁免**：test-only 的 `replace_cluster_rows_streamed` 与治理路径
   `delete_clusters`，复评 P3-3）；写新代段无锁；锁后读指针的顺序；
   SQLite 块首 `begin_guarded_write`。
5. 回收 pin：谓词认 building/catchup（注入在飞代+欠账标记 → 不删）；
   TTL 抢占 + 事件；启动恢复预算；T-5a 等值测试全列比对+排除表。
6. 普查守卫：按表出现次数配对 + 三分类 + 非空豁免清单。
7. EXPLAIN pin：`cluster_member_rows`/`version_facts` 簇分量/
   `concept_clusters_count` 三站点 IOS 恢复、热查询无 Seq Scan 回退、
   **COALESCE 子查询为 InitPlan（一次求值）**（PG twin）；SQLite
   `INDEXED BY` 站点行为回归 + 子查询一次性求值。
8. 性能门（v2 改口径）：翻转/append 持锁段**语句形态守卫**（无
   INSERT…SELECT/DELETE 大语句入锁）替代墙钟断言（v1 引用的 647-1240ms
   基线属已拆除的旧预备段，作废）；写新代段与现状 DELETE+INSERT 的对照
   计时在 PR-2 落地时实测记录于 PR（非 CI 门）。
8b. **指针翻转驱动缓存失效**（PR-1 内评新增验收项）：`SourceSubgraph
   Snapshot.cluster_generation` 与新指针列**同名不同义**（前者实为
   cluster_mutation_seq，互指注释已加）——翻转微事务 bump cluster_
   mutation_seq 即驱动该签名与 PPR/version_signal 失效，PR-2 须有 pin
   断言翻转后 partition signature 失配。
9. 互斥/认领 pin：并发取号 CAS 拒绝、**三通道释放各一例**（跳过路径/
   异常路径/翻转作废路径后认领立即可再取，缺 finally 释放的变异 → 红）、
   TTL 崩溃兜底抢占 + 被抢占者写段早停、buildkg-×unifiedkg- 409、
   standalone delete 后翻转作废。
9b. board_partition 替代判据 pin：翻转夹在读板块与发布之间 →
   `community_generation` 比对失配 → `partition_replaced_under_us` 放弃；
   恒真化变异（判据回退「行还在」）→ 红。
9c. copy-forward pin：非默认层 published 行翻代后仍可读且板块 id 已重铸、
   member 的 community_id 同步重映射。
10. 拷贝/合库 pin：副本/合并库簇行可读、COUNT 校验口径、generation 归一。
11. 每 PR：双内评 + check.sh + PG 泳道 + codex 闭环 + CI 按 head SHA。

## §6 PR 切法与回滚（v2）

- **PR-1（基建，非零测试改动）**：列迁移 + **三条索引整改**（CONCURRENTLY
  builder 脚本 + 迁移落账 + 旧索引退役债登记）+ 读者普查三分类清单与全站点
  谓词 + 普查守卫（**覆盖面即 PR-1 全站点；留待 PR-2 改写的站点——如
  `board_partition_still_holds` 判据——以「PR-2 待办」身份进豁免清单**，
  复评 P2-7）+ 拷贝/合库归一（§1.6）+ EXPLAIN pin + 迁移计数断言更新
  （`test_hotpath_indexes_batch3_live.py` **五处** `migrate()==50` 硬编码：
  `:159/:182/:244/:293/:344`）。
  指针恒 0、行恒 0 ⇒ 查询结果不变；计划变化按 §1.1 口径钉。回滚 = revert
  （列与索引可留，无写者）。摸底交付：canonical_relations/mention_bridge
  写形态、communities 写锁形态。
- **PR-2（写者切换）**：取号/在飞/TTL/预回收/写新代/双 CAS 翻转/催收/
  communities 发布事务翻转与 copy-forward/delete 终局扩列（counter 不归零）/
  锁窗口收窄/FULL CENSUS 红线两段式重写/启动恢复预算回收。回滚 = revert +
  附「指针归零 + 残代清扫」一次性 SQL。
- **PR-3（并发写不丢收尾）**：抽取补漏轮 + §2.1 进程内交叉检查 + 融合
  except 补结构化事件。独立可回滚。
- 每 PR：ports 棘轮基线同 diff 更新（R21 语义）；迁移编号按合入时 master
  现状取（本 worktree 已有 0050，**0051 起**；警惕批内撞号——#659 已发生过
  一次改号）。

## 明确不做（v2）

1. 主表行级代次化（同 v1）。
2. 两指针进 `version()` 元素（W1 §3.4）；代之以「版本身份只数 published 代」
   红线（§1.4）。
3. 多 worker durable claim（W1 #10 原样）。
4. scratch 机制改造（run_id 隔离 + TRUNCATE 恢复现状良好）。
5. 板块报告（`update_community_report`）随代退休的保全——现状即丢，
   登记不改。
6. 融合 fail-open 重试机制——只补结构化事件。

## 残余债（登记）

1. 多 worker 进程内互斥失效（W1 #10 同源）。
2. 双代窗口时长无硬上界告警（单飞 + TTL 兜底；可选后续加窗口时长事件）。
3. `rebuild_canonical_relations`/`mention_bridge` 若摸底确认 DELETE+INSERT
   而未并入，登记 W2 尾款。
4. 旧索引退役债（0043 原形覆盖索引、0007 原形唯一索引）。
5. 「翻转后 finish 前崩溃 ⇒ 下次全量重算 + 状态计数陈旧」已知格。
6. 板块报告随代退休（明确不做 5）。

## 决策点（v2）

| # | 决策 | 取舍 |
| --- | --- | --- |
| D-W2-1(v2) | 代次化必付一次索引整改：唯一索引扩列 + 覆盖索引 INCLUDE/尾键；其余索引不动 | 稳态计划保形（实测）；双代 ≈2.4× 有界；在线 CONCURRENTLY 免锁 |
| D-W2-2 | 四类一把翻（单指针） | 修跨类半态；任一类失败整轮作废（单飞+重试可接受） |
| D-W2-3(v2) | counter 独立取号且**永不归零**（delete 终局保留） | 崩溃/并发幸存行永不撞号 |
| D-W2-4(v2) | 催收 = 落库标记 + 翻转后单遍搬运（P 代窗口行冻结集合） | 无水位账本、无收敛轮、崩溃不失账；比 v1 的时间窗+有界轮强且简 |
| D-W2-5(v3) | 在飞代用 state 列；释放三通道=翻转清零/finally CAS/TTL 崩溃兜底 | 数据级跨进程单飞兼收「离线 CLI 绕单飞」缺口；TTL 只兜 kill/掉电故取大常数可解（复评 P2-5），正常失败即时释放 |
| D-W2-6 | concept_clusters 毫秒翻转；communities 翻转=发布事务 | 前者有并发融合写者需窄锁；后者原子性优先、无写者竞争 |
| D-W2-7 | 无 finally 回收，P 代活到下轮预回收 | 跨翻转在飞读者整轮宽限；代价是稳态多存一代行（≈1× 额外空间，rebuild 间隔期） |

## 内评裁决记录（v1→v2，两路 opus，2026-09-03）

| Finding | 裁决 |
| --- | --- |
| 双 P0：唯一索引禁双代 | 采纳，索引整改进 PR-1，D-W2-1 重裁 |
| P0：催收幂等探针跨代失明 | 采纳，探针按代收窄 + 催收改单遍搬运（D-W2-4） |
| P0：回收删并发在飞代 | 采纳，在飞列 + TTL + 双 CAS（D-W2-5） |
| 双 P0：拷贝/合库无 state 行簇图归零 | 采纳，§1.6 新节 + COALESCE 契约 |
| P1：稳态丢 IOS（实测） | 采纳，INCLUDE/尾键进整改 |
| P1：version_facts 簇分量重建风暴 | 采纳，「版本身份只数 published 代」红线 |
| P1：补漏轮 rebuild 谓词全量重抽 | 采纳，走 incremental 谓词；链 b 机制/注入形态改正 |
| P1：催收水位不可实现（kg_seq 失明/无 seq→source 账本/created_at 是启动非完成） | 采纳，机制整体替换（D-W2-4 后水位概念消失） |
| P1：翻转后即回收 vs 在飞读者 | 采纳，取消 finally 回收（D-W2-7） |
| P1：communities 发布事务不可拆/level 维度 | 采纳，翻转=发布事务 + copy-forward（D-W2-6） |
| P1：锁序未定义/SQLite deferred | 采纳，§2.2 顺序红线 + begin_immediate |
| P1：write_clusters 第三写者 | 部分采纳：复核为 test-only（第二路证据），普查登记不改生产语义 |
| P2：取号无行返回空 | 采纳，UPSERT 取号 |
| P2：counter 归零自相矛盾 | 采纳，永不归零（D-W2-3 v2） |
| P2：四锁取序 40P01 | 采纳，按 key 字节排序（既有 helper） |
| P2：等值测试白名单静默漏列 | 采纳，全列比对+排除表 |
| P2：启动恢复 O(表) 无预算 | 采纳，硬预算 + 次序 + 在飞/欠账保护 |
| P2：普查守卫按语句太粗 | 采纳，按表出现次数配对 |
| P2：跳过闸簇计数腿 | 采纳，A 类普查点名 |
| P3：FULL CENSUS 措辞/ports 棘轮/计时基线作废/迁移撞号/INDEXED BY 双侧/板块报告/dirty 措辞 | 全部采纳，落入对应节 |

**第二轮复评（v2→v3）**：

| Finding | 裁决 |
| --- | --- |
| P0：copy-forward 撞 communities 单列 PK | 采纳评审方案 2：复制行重铸板块 id（非默认层账本从不发言，零代价）；members 同步重映射 |
| P0：认领只有成功路径释放，失败锁库数小时 | 采纳：取号移到跳过闸后 + finally CAS 释放 + TTL 降为崩溃兜底 |
| P1：跳过路径 communities 取号无源 | 采纳：独立取号 + 被闸事件与真失败区分 |
| P1：催收是 published 写须 bump | 采纳：复用 added>0 判据,登记进两段式红线表 |
| P1：第三条索引（nb_created）两个活聚合读者丢 IOS + 催收有界凭据 | 采纳：整改扩为三条,EXPLAIN pin 扩站点 |
| P1：COALESCE 相关子查询逐行求值 / LEFT JOIN 谓词入 ON | 采纳：绑定参数模板 + ON 子句红线 + InitPlan pin |
| P1：补漏轮谓词漏 is_partial/analyzed_empty | 采纳：直接调 _kg_target_batches,结构守卫钉共用谓词 |
| P1：board_partition_still_holds 恒真化无替代 | 采纳：community_generation 比对判据 + 穿参 + 死锁论证不触碰 |
| P1：TS 时钟锚点不良定义/跨进程偏斜 | 采纳：锚=取号事务 DB 时钟 + KG_CATCHUP_SKEW_SECONDS 保守余量 |
| P1：communities 残代无回收通道 | 采纳：预回收三表通用 + 启动恢复点名 |
| P2：启动恢复走 state 行入口/页尺寸对 30s 天花板/次序锚纠正 | 采纳 |
| P2：催收投影缺 payload/簇名切片成本 | 采纳：join objects + 成本照实「一次融合」 |
| P2：抢占后被抢占者继续填残行 | 采纳：每 type 写段前重读 building 早停 |
| P2：TTL 定值不可解/无心跳 | 采纳：以 finally 释放为前提的崩溃兜底大常数（二选一取「显式释放」） |
| P2：copy_notebook 不拷 communities 的事实修正 | 采纳，§1.6 改写 |
| P2：PR-1 守卫覆盖面 vs PR-2 待办 | 采纳：豁免清单带 PR-2 标记 |
| P3：迁移号 0051 起/五处硬编码/begin_guarded_write 接缝名/结构守卫豁免 | 全部采纳 |
