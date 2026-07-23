# P1.5 设计：源完成标记（体检层的可判定性前置）

日期：2026-07-22
状态：已获用户批准（2026-07-22）

## 背景

「流水线损坏善后」专题的 P2（体检 endpoint）在设计前做了一次可判定性核查
（opus），结论是设计文档「二、体检层」的乐观假设大面积不成立。分类：

- **H6（KG 未完成）** 唯一四条全齐（判据存在、能判准、已 memo 在 `kg_mutation_seq`、
  O(1)），可直接用。
- **H4/H5（缺 chunk/element 向量）** 判据对，但向量 embed 成功路径**不 bump seq**
  （`source_embedding.py` 无 `mark_unified`/`invalidate`），折进 seq-memo 会一直报
  旧值 → P2 改直连 COUNT。
- **H7（索引过期）** 走它自己的 `version_signal` memo（含维度），**不能**折进
  `kg_mutation_seq`。
- **H8（索引损坏）** A2 的校验只活在 `load_scale_index` 内部，`status()` 路径从不
  load 数组 → P2 需新增廉价探测/持久化判定。
- **H1/H2/H3（搁浅源、空源、缺分块）** 落进与 A4 完全相同的**不可判定**坑。

用户已定：**先补 schema 再做完整体检**。本文档（P1.5）就是补这个缺口——它是
P2 的前置依赖，不含任何体检/UI 逻辑。

## A4 与 H1/H2/H3 的共同根因

`ingest`（A4）和 H1–H3 要回答的都是「这个源上次到底跑完没有 / 现在是否正被处理」。
当前 `sources` 表只有 `status`/`parse_status`/`error_message` 三个状态列
（`backend/app/repositories/sqlite/migrations.py` sources 建表处），而：

1. **无完成标记**：`process_source` 在**分块之前**就置 `parsed`
   （`backend/app/services/source_ingestion.py:576`），分块被包在 best-effort 的
   try 里、失败只 log + invalidate 计数缓存、源仍留终态（`:584-593`）。于是
   「纯标题 md 合法产出 0 chunk」（`build_chunks` 对纯标题返回 0，已实测）与
   「分块中途失败」**落到完全相同的可观测状态**（`extracted` + elements>0 +
   chunks=0），答案却相反。
2. **无源级活跃租约**：`parse_status='parsing'` 既是「服务端正在处理」的活跃态，
   也是「崩溃搁浅」的残留态，不可分。

## 头条设计决策：非对称，只加一列

两样东西的生命周期要求**根本不同**，所以不对称处理：

| | 形态 | 理由 |
|---|---|---|
| **完成标记** | **持久列 `sources.chunked_at`**（TEXT，可空，默认 NULL） | 全部意义就是跨崩溃/重启后仍能判定 `extracted`+0chunk 当初是合法 0 产物还是中途失败——必须是 schema 列 |
| **活跃租约** | **内存 dict**（不加列），镜像已有的 `_kg_building` 单飞惯例 | 只需「本进程存活期内」有效。崩溃后正确答案是「没人在处理」，而内存结构重启后**天然为空**，恰好就是这个答案。持久化它反而要在启动清算里再擦一遍，并引入「租约悬挂在 `parsed`/`extracting` 行上」的新失败模式 |

**结果：P1.5 的 schema 变更收敛到单列 `chunked_at`。** 这比原设计文档 A4 一节假设的
「完成标记 + 活跃租约都是 schema 变更」更小、更安全，且启动清算近乎零改动。
（据此订正设计文档 `2026-07-22-pipeline-damage-recovery-design.md` 的 A4 一节。）

### H1 的处置：作为独立体检项删除

原设计的 H1 是「`parse_status IN ('queued','parsing')` 且**滞留超阈值**」。这条：

1. **仍不可判定**：内存租约的 `started_at` 跨重启看不到，而搁浅恰恰是崩溃/重启
   造成的；「滞留超阈值」是墙钟时间的函数，跨过阈值时没有任何写入、seq 不动，
   根本无法 memo。
2. **且已无必要**：P0 的启动清算已把崩溃遗留的 `queued`/`parsing` 无条件翻成
   `failed`（`migrations.py` `_recover_interrupted_jobs` 里
   `WHERE parse_status IN ('queued','parsing')`）。所以「崩溃搁浅」已被 P0 转化为
   「失败源」，由 H6 家族（失败源计数）覆盖；单进程 + 就绪门下，启动后仍停在
   `queued`/`parsing` 的源要么正被本进程处理（内存租约能证），不存在第三种。

故 **H1 出局**，不再硬凑墙钟阈值。P2 的体检项从 H1–H8 变为 **H2–H8**（H2/H3 由本
P1.5 变可判定，H4–H8 见上文分类）。

## Schema 变更

### 版本号（两处字面量副本，必须一起 bump）

- `backend/app/repositories/sqlite/migrations.py:15` — `SCHEMA_VERSION = 24 → 25`
  （CLAUDE.md 认定的真源，`migrate()` 循环用它）
- `backend/app/services/sqlite_repository.py:252` — **独立字面量** `SCHEMA_VERSION = 24
  → 25`（不是 re-import；测试用 `sr.SCHEMA_VERSION`，漏改会静默漂移）
- `backend/app/core/diagnostics_runtime.py:32` 的 `SCHEMA_VERSION = 1` 是**诊断事件
  schema，与 DB 无关，不要动**。

### `_migration_25`

追加在 `_migration_24`（`migrations.py:1540`）之后：

```python
def _migration_25(self) -> None:
    """给 sources 补 chunked_at（本代 elements 已成功走完分块的时刻；NULL=未成功
    分块）。存量回填见 spec E 节：凡老代码里跑到过分块步的源一律置 updated_at,
    否则合法的纯标题/短文 md 会被 H3 集体误报缺分块。"""
    with self._connect() as db:
        self.add_column_if_missing(db, "sources", "chunked_at", "TEXT")
        db.execute(
            "UPDATE sources SET chunked_at = updated_at "
            "WHERE chunked_at IS NULL AND parse_status IN "
            "('parsed','extracting','extracted')"
        )
```

- `add_column_if_missing`（`migrations.py:31`）PRAGMA 守卫、可重入。
- SQLite `ALTER ADD COLUMN` 只允许常量默认，NULL 天然满足；ALTER 追加到列末尾，
  全新库（migrate 扫 1..25）与已部署库落在**同一列序**——正是 `migrations.py:119-131`
  注释锁定的 golden 列序纪律。

## 完成标记语义（`chunked_at`）

- **含义**：本代 elements 已**成功走完分块步骤**的时刻。NULL = 尚未成功分块
  （未跑到 / 分块抛异常 / 崩溃在分块前）。载体是它的 NULL 性，时间戳值只是顺带的
  可观测信息（对齐 `created_at`/`updated_at`/`checked_at` 惯例）。
- **为什么是「分块级」不是「解析级」**：A4 二义性两支解析都成功（elements 都落库），
  分歧纯在分块步。`parse_completed_at` 这种解析级标记区分不了。
- **与 `parse_status` 共存不打架**：`chunked_at` 正交，`parse_status` 一字不改。
  H3 检测判据以 **`chunked_at IS NULL` 为准、不带 `chunks=0` 子句**：
  `elements>0 AND chunked_at IS NULL AND 不在内存租约` 才是真损坏；纯标题 md 的合法
  0-chunk 因 `chunked_at` 有值被排除。
  - ⚠ **为什么丢掉 `chunks=0`**（codex 第 2 轮 P2 finding 1）：`chunked_at` 是
    **世代感知**的（换 elements 即归零、本代成功分块才置位），比 chunks 计数可靠。
    reparse 换了新代 elements 后、旧代 chunks 尚未被 `replace_source_chunks` 清掉时若
    分块失败,会留下 `elements>0 AND chunks>0(旧代) AND chunked_at IS NULL`——判据若带
    `chunks=0` 会**漏检**这种「陈旧 chunks」损坏。只认 `chunked_at IS NULL` 同时覆盖
    「无 chunks」与「陈旧 chunks」两种。
  - ⚠ **marker 只信得过的前提**（finding 2）：既然 H3 只认 marker,marker 就不能被
    错置。同源并发 reparse 由 `source_ingestion.py` 的 **per-source 分块串行锁**保证
    「换 elements→建 chunks+置 marker」整段原子,不会出现「B 代 elements + A 代 chunks
    + marker 已置」的假完成(见「写入点」末尾并发一节)。

## 写入点（`source_ingestion.py` / `source_chunking.py`）

原则：标记的写**与它描述的数据同事务**——随「使它失效的 elements」归零、随「满足
它的 chunks」置位。

| 阶段边界 | file:line | 新增 |
|---|---|---|
| 进入 process_source | `source_ingestion.py:469` 之前（函数体最顶） | **取内存租约**：锁下 `active[source_id] = now()` |
| 写 elements（代边界） | `source_ingestion.py:551-572` 的 `with write() as db:` | **同一事务内** `UPDATE sources SET chunked_at=NULL WHERE id=?`（新代 elements 落库即令旧分块完成失效） |
| **分块成功** | `build_chunks_for_source` 里 `replace_source_chunks(..., mark_chunked_at=now)`（**含 0 chunk**） | chunked_at 与它认证的 chunk 数据在**同一写事务**里提交（codex 第 1 轮 P2 finding 1:分处两事务会在崩溃窗口留假损坏）。knowhow 投影器按格子复用 `replace_source_chunks` 时**不传**该参数,其隐藏源不打标 |
| **分块失败** | `build_chunks_for_source` 抛出（except 在 process_source） | **什么都不写**（留 NULL）——这就是 H3 的损坏信号。现有 invalidate 保留 |
| 管线出口（成功/失败） | process_source 的 try **加 finally**，早于 `maybe_enqueue_scale_fold` | **租约计数减一**（归零才真正撤租，见并发一节） |

**分块 try 成功/失败写不同的东西**（这是全设计的枢纽）：
- 成功 → `replace_source_chunks(mark_chunked_at=now)`（纯标题 0-chunk 也算成功、也置值）→ `extracted`+0chunk+**有值** → H3 排除
- 失败 → 什么都不写 → `extracted`+0chunk+**NULL** → H3 命中

两者 `parse_status`/elements/chunks 完全相同、`chunked_at` 相反——正是 A4 缺的那一维。

### 并发:租约计数 + per-source 分块串行锁（codex 第 2 轮 P2）

同一源可被并发处理(上传后台 job 未完时 owner 又点 `POST /sources/{id}/parse`,二者
都进 `process_source`,无重入守卫)。两处保护:

1. **内存租约用引用计数**(`_active_sources: dict[str,int]`)而非时间戳:每个 invocation
   进入加一、finally 减一、归零才撤租。否则先完成者会撤掉仍在跑者的租约,令其在途缺
   elements/chunks 被体检误报(codex 第 1 轮 finding 2)。
2. **per-source 分块串行锁**(`_source_chunk_locks`)把「换 elements → 建 chunks + 置
   marker」整段串起来:否则一次 invocation 读到 A 代 elements、另一次换成 B 代、第一次
   再把 A 代 chunks 连同 marker 写回,留下「B 代 elements + A 代 chunks + marker 已置」
   的假完成(codex 第 2 轮 finding 2)。**只锁 build_chunks 不够**——replace_elements 若
   不持同一把锁,仍能插进「读→写」之间;故锁必须连续覆盖 replace_elements 到
   build_chunks。锁懒创建、与租约同生命周期(refcount 归零即清,有界不泄漏);单写(常见)
   路径无竞争、零额外开销,只串行罕见的并发同源 reparse。

**memo 说明**：分块成功已 bump `kg_mutation_seq`（`source_chunking.py:58-65`），失败已
`invalidate`（`source_ingestion.py:592-593`），故 `chunked_at` 的写**无需**自己再 bump。

**新增 store 方法**（`source_store.py`）：`mark_chunked(source_id, ts)`；`chunked_at`
归零可折进 `clear_source_extraction_state` 所在事务或就地一条 UPDATE。`set_status`
（`:371`）与 `insert_source`（`:308`）**无需改**——新行 `chunked_at` 走列默认 NULL 即
「未分块」，语义正确。

## 活跃租约（内存）

- 在仓库 runtime 上放 `dict[source_id → started_at]` + `threading.Lock`，镜像
  `knowledge_lifecycle.kg_building` / `kg_building_lock`（`sqlite_repository.py:585-586`
  别名同一对象的先例）。
- process_source 进入 stamp、finally 释放（见写入点表）。
- **职责是在途误报抑制，不是崩溃检测**：体检 endpoint 与 process_source 同进程、
  可并发。上传中途打开看板时在途源瞬时「没 elements/没 chunks」，纯产物判据会误报
  损坏；租约说「有活线程在弄，别报」。崩溃检测归 `parse_status` + 启动清算。
- **P2 消费方式**：租约作为体检 memo **之外的 Python 后置过滤**（active 集通常个位
  数，一次集合减法）。memo 只缓存 SQL 候选集，二者解耦——避免「租约变化要不要
  失效 memo」的耦合。

## 启动清算（`_recover_interrupted_jobs`）：近乎零改动

内存租约方案的直接红利——本函数对 sources **不新增任何改动**：

- 现有两条保留：queued/parsing→failed、extracting→parsed。
- **不碰 `chunked_at`**：崩溃在分块中的源停在 `parsed`（不在 queued/parsing/extracting
  里），现有清算正确地不动它——我们**就是要**它保持 `parsed`+`chunked_at IS NULL`
  让 H3 去发现，而非自动补分块（design doc「三·自动 vs 手动」：H3 映射到用户点击
  的「重新解析」，清算不自动触发）。崩溃在 KG 中的源 `extracting`→`parsed`，其
  `chunked_at` 早已置位（分块先于 KG），H3 不误报。
- **不需清租约**：内存集重启后天然为空。

**关键不变量（写进实现注释与测试）**：后端单进程 + 清算跑在 `mark_ready()` 之前
（`startup_warmup.run_startup`，业务路由在就绪门后才放行）⇒ **清算这一刻不可能有
任何源正被本进程处理** ⇒ 内存租约此刻本就空、无需清算。
`test_startup_recovery_ownership.py` 已用「mark_ready 那一刻抓快照」直接钉这条不变
量，新覆盖照此扩。

## 迁移与回填（最容易翻车）

**存量默认 = NULL，但必须回填**，否则就是 memory「多领域基准库不回填致上线断层」的
翻版：

- **危险**：几十万存量 `extracted` 源全 `chunked_at=NULL`。虽多数 chunks>0（不被 H3
  命中），但所有**合法纯标题/短文** md（elements>0、chunks=0）会**集体**被误报
  「缺分块，重新解析」——上线一墙假警报。
- **回填规则（保守，选定，即 `_migration_25` 里那条 UPDATE）**：凡「老代码里跑到过
  分块步」的源（`parse_status IN ('parsed','extracting','extracted')`）一律置
  `chunked_at = updated_at`。含义：这些源在本特性前就已走完（best-effort）分块，
  无论产出几个 chunk，老管线都视为已完成并前进。于是 H3 对**任何**存量 parsed+ 源
  不再命中，断层消除；上线后标记只对**新**失败置 NULL，新损坏照抓。
  - **排除** uploaded/queued/parsing（从未解析）、failed（未完成）、metadata-only
    （合成源、无分块步）——留 NULL 正确。
- **明说的代价**：此回填**遮蔽存量的历史分块失败**（不被 H3 追溯发现）。可接受——
  A4 已证存量里「纯标题」与「分块失败」产物不可区分，唯一安全动作是取不报警那支；
  且新代码起真实打标。与「宁可漏修不误伤」的既有取向一致。
- **激进变体**（不做默认，可留作未来 admin 一次性扫描）：只回填「实际有 chunks 的」
  （`AND EXISTS(SELECT 1 FROM chunks WHERE source_id=sources.id)`），让 elements>0/
  chunks=0 存量留 NULL 被 H3 捞出；副作用是纯标题 md 假阳性，但重解析一次即自愈。

## 配套改动（golden + 版本硬编码测试）

- **golden**：`backend/tests/fixtures/schema_contract.txt` 的 sources 段追加
  `sources.chunked_at` 行，用 `UPDATE_SCHEMA_GOLDEN=1 pytest tests/test_legacy_db_compat.py`
  重生成（不手写）。
- **版本硬编码测试（bump 后会红，需一并改）**：`test_sqlite_migrator_component.py`、
  `test_multi_domain_bases.py`、`test_memory_kg_schema.py`、`test_repository_v9_fixture.py`、
  `test_source_asset_migration.py`、`test_legacy_db_compat.py`（含 `test_v24_...` 函数名）。
  以实际报红为准，逐个通读改，不靠「跑一遍看哪个红」代替。
- **新增** `test_deployed_v24_db_upgrades_adds_chunked_at`：照 `test_legacy_db_compat.py`
  的 `_migration_24` 同款，证明 v24 部署库上 `_migration_25` 补出 `chunked_at`。
- **文档四份**：`architecture.md` 只提 1 次 `sources`、不枚举列，故加内部体检支撑列
  **无需**改四份文档；golden 是这里事实上的 schema 文档。

## 不改 / 明确边界

- 不改 `parse_status` 语义。
- 不建 `chunked_at` 上的索引——是否建（部分）索引取决于 H3 最终查询形状，留到 P2；
  CLAUDE.md「新增索引必须独立迁移」，P2 需要时再追加 `_migration_N`。
- 不做心跳/PID liveness——多 worker 下体检/租约不保证正确，与现有清算层**同一**限制
  （`_recover_interrupted_jobs` 早已假设单进程），不新增破坏面。

## 验证要求

- **迁移变异验证**：在 v24 部署库上跑 `_migration_25`，确认 `chunked_at` 被补出且
  存量 parsed+ 源被回填成 `updated_at`；确认 uploaded/failed/metadata-only 留 NULL。
- **H3 可判定性**（这是全设计要证的核心）：构造两个状态相同的源——「分块成功产
  0 chunk」（`chunked_at` 有值）与「分块失败」（`chunked_at` NULL），断言前者不被
  「真损坏」判据命中、后者命中。**这条不做就等于没证明可判定。**
- **崩溃续跑**：模拟崩溃在分块中（elements 已写、`chunked_at` NULL、无 chunks），
  重启后断言该源被判为真损坏（未被回填遮蔽——它是新代码期的源，`chunked_at` 本就
  该是 NULL）。
- **租约不参与清算**：`test_startup_recovery_ownership.py` 扩一条，断言 mark_ready
  那一刻内存租约为空。
