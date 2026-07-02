# PostgreSQL + pgvector 迁移 spec:向量入库 / 全局写锁退役 / 多 worker(CSR PPR 旁挂保留)

> 日期:2026-07-03 · 状态:设计草案,待需求方评审。纯文档,无代码改动。
> 关联:[docs/kg-perf-audit-16c64g.md](../../kg-perf-audit-16c64g.md)(动因)、[2026-07-01-index-lifecycle-redesign.md](2026-07-01-index-lifecycle-redesign.md)(索引生命周期,本 spec 沿用其状态机词汇)、[2026-06-29-base-kg-scale-retrieval-design.md](2026-06-29-base-kg-scale-retrieval-design.md)(CSR+PPR 基底)。
> 规模标尺:单库 10⁶ 节点 / 百万级 chunk / 百万级关系;16C/64G 单机起步;多用户。

---

## 0. 结论先行(TL;DR)

- **做什么**:把全部关系型数据迁到 **PostgreSQL**,四张 embeddings 表(knowledge / chunk / element / relation)的向量迁到 **pgvector(HNSW)**。检索的**向量最近邻由数据库负责**,应用侧不再在进程内物化 GB 级矩阵。
- **不做什么**:不引 Neo4j(有界多跳 + 静态 PPR,CSR+scipy 已 0.019s);不做分布式 / 多机 / 读写分离;不换 KG 抽取管线。
- **保留什么**:`kg/scale_index.py` 的 **CSR + 个性化 PPR 旁挂件保留**,但**改为从 PG 构建**、瘦身为**纯图件**(`graph.npz` / `node_ids` / viz 数组)。向量部分(kg/chunk/relation ANN 旁挂 bin、向量 BLOB、进程内向量矩阵缓存)**整体退役**,由 pgvector 承接。
- **最大取舍**:数据访问层 **psycopg3 raw SQL 平移**(不引 SQLAlchemy),配 `DATABASE_URL` 驱动的**双后端骨架 + 一次性硬切迁移工具**;双跑期短(仅 P0/P1 内部对照),不长期维护两套生产路径。
- **验收基线 = 本周事故清单**:矩阵加载/OOM、旁挂索引生命周期、版本探针、全局写锁、workers=1、evidence 反查、typeof 全表扫 —— §9 逐条映射「迁移后如何消失/简化」。
- **分期**:P0 抽象层收口 + PG 双跑骨架 → P1 关系型切换 → P2 向量切换 + 向量旁挂退役 → P3 多 worker + 清理。每期独立可交付、可回滚。

---

## 1. 目标与非目标

### 1.1 目标

| # | 目标 | 兑现的事故清单项 |
|---|---|---|
| G1 | 向量最近邻入库(pgvector HNSW),消灭进程内 `_vector_matrix` 全量矩阵与 `top_k_sims` 的 GB 级 dict | 矩阵加载/OOM、P0-7 |
| G2 | 退役进程级全局写锁 `_write()`,应用趋近无状态,支持 `uvicorn --workers N` | 全局写锁 P1-5、workers=1 |
| G3 | evidence 反查、typeof 全表扫、`IN(...)` 分块等 sqlite 惯用法用 PG 原生能力(JSONB/GIN、参数数组)替换 | P0-4 evidence 反查、typeof 全表扫 |
| G4 | 版本探针机制简化(PG 事务可见性 + 触发器/序列替代手工 COUNT/MAX 聚合与 `kg_mutation_seq` 单调计数器) | 版本探针 |
| G5 | CSR+PPR 旁挂件从 PG 构建、生命周期简化(向量部分退役,只余图件) | 旁挂索引生命周期 build/fold/stale/自动触发 |

### 1.2 非目标(明确不做)

- **Neo4j / 图数据库**:PPR 是静态个性化 PageRank,scipy CSR 幂迭代已达 0.019s 且零 JVM/GDS 运维;引 Neo4j 只增运维面不增能力。**排除。**
- **分布式 / 多机 / 分片 / 读写分离**:标尺是单机 16C/64G;PG 单实例 + 连接池足以承载百万级。多机留给未来,不在本 spec。
- **换向量库**(Milvus/Qdrant/Weaviate):pgvector 让向量与关系数据同事务、同备份、同权限,免去两存储一致性问题;百万×1024 维在单机 pgvector HNSW 可行(§8 需 spike 验证建索引成本)。**本 spec 只用 pgvector。**
- **ORM 全量重写业务逻辑**:不引 SQLAlchemy ORM 模型层(见 §4)。

### 1.3 保留边界:CSR PPR 旁挂 —— hnswlib 只在图件内?还是 PPR 种子也改 pgvector?

**结论:PPR 的种子选择改走 pgvector 查询;`scale_index.py` 内不再持有任何 hnswlib 件。图件(CSR 转移阵 + node_ids + viz 数组)保留旁挂。**

理由 —— 拆分「图结构」与「向量最近邻」两个正交职责:

- **PPR 幂迭代需要的是图结构(CSR 转移阵)**,这是 pgvector 给不了的(pgvector 只做 KNN,不做图上传播),必须保留旁挂 `graph.npz`。CSR 从 PG 的 `knowledge_relations` 构建(§5.2)。
- **PPR 的 reset 种子**(`ppr_kg_seed_top_n` / `ppr_chunk_seed_top_n`)当前靠 `scale_index.py` 内旁挂的 `ann.bin`/`chunk_ann.bin`(hnswlib)选 top-N。**这一步改为 pgvector HNSW 查询**:`ORDER BY vector <=> :q LIMIT :n`。好处:
  - `save_scale_index` 不再构建两个 hnswlib 索引(审计称「流水线里最贵的计算」),build 时间与内存显著下降;
  - `_open_scale_ann` / `add_items_to_ann` / `chunk_ann_*` / `prebuilt_ann` 复用逻辑整条退役;
  - 种子候选**永远最新**(pgvector 与写同事务),消除「ann.bin 建完到下次 rebuild 之间 delta 种子不进候选」这一新鲜度缺口 —— 索引生命周期的 delta⊕暴力(2026-07-01 spec)在向量侧简化为「pgvector 永远覆盖 delta」。
- **保留在旁挂的仅**:`transition`(CSR)、`node_ids`/`node_index`、`idf`、`chunk_index`、`viz_*` 折叠数组。即 `ScaleIndex` 结构删掉 `ann_*` / `chunk_ann_*` / `*_handle` 六个字段,退化为「纯图件 + viz 件」。

> 一句话:**向量 KNN 全归 pgvector;图传播(PPR/viz)留 scipy CSR 旁挂,从 PG 构建。**

---

## 2. 目标架构

```
┌─────────────────────────────────────────────────────────────┐
│  应用进程(uvicorn --workers N,趋近无状态)                    │
│                                                              │
│   ├─ 检索热路径:向量 KNN → PG (pgvector HNSW, 同事务最新)     │
│   ├─ 关键词/词法:PG FTS(tsvector/GIN)或 pg_trgm             │
│   ├─ PPR 图传播:进程内 CSR(旁挂加载)+ 种子来自 pgvector KNN  │
│   └─ 写:直连 PG(事务隔离,无进程级写锁)                       │
│                                                              │
│   进程内缓存:                                                 │
│     ✗ _vector_matrix / kwtok(退役 → pgvector / PG FTS)      │
│     ✗ 向量 BLOB / 矩阵缓存 VectorCache 的 matrix/kwtok 条目   │
│     ✓ scale idx cache 的【图件部分】(CSR/viz,LRU 保留)       │
│     ⚠ rustworkx 全图缓存(fed_rxgraph 等)→ 见 §5.2 收敛到 CSR │
└───────────────┬──────────────────────────────┬──────────────┘
                │ SQL + 向量 KNN                │ 旁挂读(只读文件)
      ┌─────────▼─────────┐          ┌──────────▼─────────────┐
      │  PostgreSQL 15+   │  构建     │  CSR 图旁挂(每 nb 一份) │
      │  + pgvector HNSW  │ ────────▶ │  graph.npz / node_ids   │
      │  (所有关系数据     │  (从 PG   │  viz.npz / viz_adj.npz  │
      │   + 4 张向量表)    │   查关系   │  (无 ann.bin/chunk_ann) │
      └───────────────────┘   建 CSR) └────────────────────────┘
```

### 2.1 缓存去留清单

| 进程内缓存 | 现状 | 迁移后 |
|---|---|---|
| `_vector_matrix`(kg/chunk/element 矩阵,~4-6GB) | 全量物化 + `VectorCache` 版本键 | **退役**。向量 KNN 走 pgvector,不再进程内矩阵。 |
| `:kwtok` 关键词 token 集(GB 级) | `_keyword_token_sets` 缓存 | **退役**(见 §2.2 kwtok 去向)。 |
| 向量 BLOB(`encode_vector`/`decode_vector`) | SQLite BLOB / 遗留 JSON | **退役**,列改 `vector(1024)` pgvector 原生类型。 |
| `_scale_idx_cache` / `_viz_idx_cache`(LRU) | 图件 + memoized hnsw handle | **保留图件部分**;删 hnsw handle memo(`_open_scale_ann`)。 |
| `_scale_ver_cache` / `_scale_ver_lock`(版本探针 memo + 单飞) | `kg_mutation_seq` 键控 5 聚合 | **简化**(§3.4 版本探针)。 |
| `fed_rxgraph` / `ppr_graph` / `rxgraph`(rustworkx 全图,多 GB) | 版本缓存,体积未实测 | **收敛到 CSR**(§5.2):PPR 统一走 `scale_index.py` 的 scipy CSR,退役 rustworkx 全内存图。 |
| `VectorCache` LRU(32)/`LRUProcessCache`(8) | 通用 LRU 容器 | **保留**,但承载条目只剩 CSR 图件(matrix/kwtok 条目消失)。 |

### 2.2 kwtok 何去何从:PG FTS(tsvector/pg_trgm)vs 保留 chunks_fts 等价物

**结论:关键词/词法召回改用 PG 原生 FTS,分两类:**

- **chunk / KG 对象名词法命中** → **`pg_trgm` GIN 索引**(等价当前 `fts5(tokenize='trigram')`)。当前 `chunks_fts`/`kg_objects_fts` 用的就是 trigram 分词,`pg_trgm` 是最直接的语义等价物,且对中英混排的三元组匹配行为一致。用 `text %  :q` / `similarity()` 或 `to_tsvector`(见下)择一。
- **`_keyword_token_sets` 的融合打分 token 集** → 这是**检索融合分**(关键词 + 语义加权,守 `[0,1]`/tau)里的关键词侧,当前在 Python 里对 payload+evidence 分词成 frozenset 再算 Jaccard 式重合。迁移后**两条路可选**:
  1. **保留 Python 侧分词但去掉全量缓存**:候选集已由 pgvector 收敛到 top-N(recall 池,数百条),对这数百条现场分词成本可忽略 —— kwtok 缓存存在的唯一理由是「百万对象每查询现场分词」,pgvector 界定候选后该理由消失。**推荐此路**:改动最小、融合分公式字节不变(§5.1 等价性最易保)。
  2. 纯下推到 PG `tsvector` + `ts_rank`:更彻底但要重写融合分权重、且 `[0,1]`/tau 契约需重新对齐,风险高。**不推荐**首选。

> 即:**词法召回下推 pgvector⊕pg_trgm;融合分的关键词侧保留 Python 但退掉全量缓存(候选已被界定)。** `chunks_fts`/`kg_objects_fts` 两张 FTS 虚拟表退役,由 `pg_trgm` GIN 替代。

---

## 3. Schema 映射

### 3.1 逐表映射

| SQLite 表 | PG 处置 | 关键变化 |
|---|---|---|
| `users` / `auth_sessions` / `user_profiles` | 平移 | `TEXT` id → `TEXT`(保留,不改 UUID 类型,避免全量 id 重映射);`created_at` 等 `TEXT` 时间戳 → **`timestamptz`**(见 §3.3)。 |
| `notebooks` / `sources` / `source_elements` / `chunks` | 平移 | `element_ids TEXT '[]'` → **`jsonb`**;`metadata TEXT '{}'` → `jsonb`;外键 `ON DELETE CASCADE` 原样(PG 一等支持)。 |
| `chunk_embeddings` / `element_embeddings` / `knowledge_embeddings` / `relation_embeddings` | **重构** | `vector TEXT/BLOB` → **`vector(1024)`**(pgvector);删 `created_at` 版本探针依赖(§3.4);每表建 **HNSW 索引**(§5.3)。 |
| `extraction_runs` | 平移 | 已有 `(source_id, created_at)` 索引,直接映射。 |
| `knowledge_objects` | 平移 | `payload TEXT` / `evidence TEXT` → **`jsonb`**;`evidence` 上建 **GIN**(§3.2 反查)。 |
| `knowledge_relations` | 平移 | 是 CSR 图构建的边源(§5.2);现有 `(nb, source_object_id)`/`(nb, target_object_id)` 索引映射。 |
| `answers` / `conversations` / `feedback` | 平移 | 补 P2-2 缺失索引一并做(analytics 路径)。 |
| `object_schemas`(全局,不随 notebook 拷贝) | 平移 | 保持全局语义。 |
| `concept_clusters` | 平移 | `member_object_id` 等索引映射;增量融合逻辑不变(仅 SQL 方言)。 |
| `concept_merge_candidates` / `merge_review_jobs` / `kg_conflict_candidates` / `promotion_candidates` | 平移 | 状态机语义不变;启动 reconcile(`running`→`failed`)改为 PG 端同义 UPDATE。 |
| `unified_kg_state`(含 `kg_mutation_seq`) | **简化保留**(§3.4) | `kg_mutation_seq` 是否保留取决于版本探针新机制;`dirty`/`cluster_input_version`/计数列保留(rebuild O(1) 版本闸仍需要)。 |
| `concept_whitelist` / `kg_cluster_scratch` / `communities` / `notebook_members` | 平移 | `kg_cluster_scratch` 是 rebuild 临时表,PG 下仍为普通表(跨连接 + 中途 LLM 调用,不能用 TEMP,原注释理由在 PG 下同样成立)。 |
| `kg_objects_fts` / `chunks_fts`(FTS5 虚拟表) | **退役** | → `pg_trgm` GIN(§2.2)。 |
| `knowledge_object_sources`(evidence 反查表) | **可退役,给分析** | 见 §3.2。 |

### 3.2 JSON 列 → JSONB + GIN;evidence 反查表(`knowledge_object_sources`)去留

- 所有 `TEXT` 存的 JSON(`payload`/`evidence`/`element_ids`/`metadata`)→ **`jsonb`**。消灭 `json_extract` / 逐行 `json.loads` 的两类代价:检索融合分对 evidence 逐行解析、`_clear_source_extraction_state` 全库 evidence 扫描找引用某 source 的对象(P0-4)。
- **`knowledge_object_sources` 反查表**(现为规避 P0-4 全库 evidence 扫的冗余表):
  - **推荐:退役该冗余表**,改为在 `knowledge_objects.evidence`(jsonb)上建 **GIN 索引**,反查 "引用了 source X 的对象" 用 `WHERE evidence @> :source_ref` 走 GIN。理由:反查表需在每次 KG 写时与 evidence 双写保持一致(一致性负担 + 一处写漏就静默错),而 jsonb GIN 是同一份真相的索引,不会漂移。
  - **保留的条件**:若 evidence 的 JSON 形状不利于 `@>` 容器查询(如 source id 深埋在嵌套数组对象里,`@>` 需精确路径),则保留反查表但改为 **PG 端触发器维护**(而非应用双写),消除一致性负担。**decision:先 spike `evidence @> ` 的选择率与计划,命中则退役表;否则触发器维护。**(§8 风险项)

### 3.3 id / 约束 / 级联 / 时间戳

- **id 保持 `TEXT`**:全表 id 是应用生成的字符串(uuid/短码),不改 PG `uuid` 类型 —— 避免 `copy_notebook` 那类**全表 id 重映射含 JSON 内嵌 element_ids**(见分享拷贝 memory)在类型层面再复杂化。`PRIMARY KEY (id)` / `REFERENCES` 平移。
- **时间戳 `TEXT` → `timestamptz`**:当前用 ISO 字符串 `MAX(created_at)` 当版本探针的一部分,PG 下改 `timestamptz` + 事务提交序更可靠(顺便消除字符串比较时区/格式坑)。**注意**:这是行为改动点,迁移工具需按同一格式解析(§6)。
- **级联删除**:`ON DELETE CASCADE` PG 一等支持,`PRAGMA foreign_keys = ON` 的等价物是 PG 默认强制外键 —— 迁移后外键始终生效(SQLite 需每连接开 PRAGMA,PG 无此坑)。
- **确定性序契约**:PG **无 rowid**,插入序不可依赖 —— 见 §5.4。凡当前隐式依赖 SQLite rowid/表扫序的位点,迁移时**显式加 `ORDER BY`**(已在 PR#136 审计标注的 `_stream_seed_reps` 两处 `ORDER BY rowid`,PG 下换成 `ORDER BY created_at, id`)。

### 3.4 版本探针机制:PG 下怎么简化

当前版本探针有两层:(a) `_vector_matrix`/`kwtok`/`fed_rxgraph` 用 `(COUNT, MAX(created_at/updated_at))` 作缓存版本键;(b) `unified_kg_state.kg_mutation_seq` 单调计数器作 rebuild 的 O(1) 版本闸(时间戳 1s 粒度会漏同秒 in-place 编辑,故用计数器)。

迁移后:

- **(a) 向量/kwtok 版本键整体消失**:它们存在的唯一理由是「进程内缓存需知道 DB 变没变以决定是否重建矩阵」。矩阵退役 → pgvector 同事务读永远最新 → **无需版本键**。`_scale_ver_cache`/`_scale_ver_lock`/单飞逻辑随之退役。
- **(b) rebuild 的 O(1) 版本闸保留但可换实现**:CSR 图件 + `cluster_input_version` 仍需知道「输入变没变」以跳过无谓 rebuild/重聚。两条路:
  1. **保留 `kg_mutation_seq`**(应用侧在 `_mark_unified_kg_dirty` +1)—— 改动最小,语义已验证。**推荐**:PG 迁移不趁机改此逻辑,降风险。
  2. 换 PG **触发器**在 `knowledge_objects`/`knowledge_relations`/`concept_clusters` 写时自增 `unified_kg_state.kg_mutation_seq` —— 免「`_mark_unified_kg_dirty` 非唯一汇聚点、被 update_knowledge/re-embed 绕过」这类历史坑(见 memory)。**次选**,作为 P3 清理项 opt-in,不在关键路径引入。

> 版本探针净效果:**5 个 COUNT/MAX 聚合探针 + 向量缓存单飞全退役**;只余 rebuild 的 `kg_mutation_seq` 版本闸(实现不变或转触发器)。

---

## 4. 数据访问层改造策略

### 4.1 现状

- **raw SQL ~200+ 位点**,深度绑定 sqlite3 惯用法:`PRAGMA`、隐式 rowid、`typeof()`、`json_extract`、`executemany`、`IN(...)` 手工分块(`_IN_CHUNK`)、`sqlite3.Row` 行工厂、`_write()` 进程级写锁串行化、`executescript` 建表。
- 已有 `NotebookRepository`(Protocol,`repository.py`)抽象接口,但 `SQLiteRepository` 是**唯一实现**且方法体直接写 SQL —— 抽象在**方法签名层**已收口,**SQL 方言层**未收口。

### 4.2 方案对比:psycopg3 raw SQL 平移 vs SQLAlchemy Core

| 维度 | A. psycopg3 raw SQL 平移(**推荐**) | B. 引入 SQLAlchemy Core |
|---|---|---|
| 改动量 | 200+ 位点逐个换方言(占位符 `?`→`%s`、`PRAGMA` 删、`typeof`/`json_extract` 换) | 200+ 位点重写为 Core 表达式,更大 |
| 学习/维护 | 团队已熟 raw SQL;psycopg3 原生 pgvector 适配(`register_vector`) | 引入 ORM/Core 抽象层认知负担 |
| 双后端 | 方言差异靠一层薄 dialect helper 吸收 | Core 天然多方言,但为吸收一次性迁移引大依赖不划算 |
| 向量 | psycopg3 + pgvector-python 直接 `vector` 类型绑定 | 需 pgvector 的 SQLAlchemy 类型集成,可行但多一层 |
| 风险 | 方言深坑逐个手工(§8),但每处可单测 | 表达式重写引入等价性回归面更大 |

**推荐 A**:psycopg3 raw SQL 平移。仓库已是 raw SQL 心智模型,一次性迁移不值得为「未来可能多方言」引 ORM。把方言差异集中到一个 `_sql` 薄封装(占位符转换、`RETURNING`、`ON CONFLICT` 等)。

### 4.3 双跑期:feature flag 双后端 vs 硬切 + 迁移工具

**推荐:`DATABASE_URL` scheme 驱动的双后端骨架(P0-P1 短期并存)+ 一次性迁移工具做硬切,不长期维护两套生产路径。**

- `DATABASE_URL` 已存在(`sqlite:///` / `postgres://`);`config.py` 的锚定逻辑已对非 sqlite scheme 网开一面(不重锚)。抽象层按 scheme 选实现:`SQLiteRepository` vs 新 `PostgresRepository`(或同类内 dialect 分支 —— 见 §4.4)。
- **双后端只服务于迁移期对照测试**(同一测试套件对两后端跑、等价性 diff),**不作为长期生产形态**:双跑成本高(§8 测试矩阵),SQLite 路径在 P2 完成、真机验证后**删除**。
- **一次性硬切**:迁移工具(§6)把线上 SQLite 搬进 PG → 切 `DATABASE_URL` → 重启多 worker。**不做在线双写渐进切换**(单机单库,停机窗口可接受,双写一致性不值当)。

### 4.4 事务 / 锁语义差异

| 语义 | SQLite 现状 | PG 后 | 应用层是否仍需 |
|---|---|---|---|
| `_write()` 进程级全局写锁(RLock) | 所有 notebook 写串行,长事务(rebuild/copy)全站阻塞;P1-5 | **退役**。PG 行级锁 + MVCC,并发写不互斥(除非改同一行) | **否**。这是 G2/workers=N 的核心解锁项。 |
| `PRAGMA busy_timeout` / WAL | 规避 `database is locked` | 无需 | 否 |
| `_scale_building` in-flight 去重(同一 nb 不并发 rebuild) | 进程内 `set` + Lock | **仍需,但要跨 worker**:多 worker 下进程内 set 失效 → 用 **PG advisory lock**(`pg_try_advisory_lock(hashtext(nb))`)或 `unified_kg_state` 行上 `SELECT ... FOR UPDATE SKIP LOCKED` | 是(改跨进程) |
| `_scale_ver` 单飞 / VectorCache 单飞 | 进程内同 key 只构建一次 | 向量矩阵退役 → 单飞随之退役;CSR 图件旁挂构建仍可进程内单飞(每 worker 各自建自己的旁挂缓存,可接受重复) | 部分保留(仅图件) |
| 事务边界 | `with self._connect() as db`(autocommit-ish + WAL) | 显式 `BEGIN`/`COMMIT`,`ON CONFLICT DO UPDATE` 替 upsert | 是(方言) |
| 启动 reconcile(`running`→`failed`) | 单进程假设「线程不跨重启」 | **多 worker 下失真**:N worker 各自 reconcile → 需**幂等 + 单次**(advisory lock 或只 worker-0 做);或改为「按 job 心跳超时判失效」 | 是(改多 worker 安全) |

> **多 worker 关键清单**:凡当前依赖「单进程」不变量的位点 —— `_scale_building` set、启动 reconcile、调度器(低峰窗口 `_scale_scheduler`)、`_auto_index_checked` once-set —— 迁移到**跨进程协调**(PG advisory lock / 状态表 / 只一个 worker 跑调度)。此为 P3 主体工作。

---

## 5. 检索等价性

### 5.1 融合分与不变量

- **`[0,1]` / tau 契约**:融合分公式(关键词侧 + 语义侧加权)**不变**。语义侧的 cosine 从 `matrix @ q` 变为 pgvector `1 - (vector <=> :q)`(cosine 距离转相似度),值域一致。tau_low/tau_high 阈值(0.18/0.35)不动。
- **dual-index best-of**:当前对多来源/联邦取 best-of 的语义保留;pgvector 查询按 notebook 过滤(`WHERE notebook_id = ANY(:ids)`)后取 max,等价。
- **关键词侧**:见 §2.2,候选界定后 Python 现场分词,融合分字节不变(推荐路)。

### 5.2 CSR PPR 从 PG 构建;rustworkx 全图收敛

- **CSR 转移阵**:`build_transition`/`build_transition_arrays` 输入的边列表**改从 PG `knowledge_relations` 查**(`SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?`),构建逻辑(列随机归一)不变。`splice_active`/`fold_arrays` 的 delta 拼接语义保留。
- **PPR reset 种子**:改走 pgvector KNN(§1.3)。`personalized_ppr` 幂迭代不变。
- **rustworkx 全图缓存收敛**:审计驻留清单里 `fed_rxgraph`/`ppr_graph`/`rxgraph`(多 GB rustworkx 全内存图)与 scipy CSR **功能重叠**。迁移是收敛良机:**PPR 统一走 `scale_index.py` scipy CSR**,退役 rustworkx 全图路径(reasoning/graph 模式的联邦 PPR 走 CSR splice)。此项列 P2,可独立验收(等价性对照 rustworkx PPR 输出 top-k)。

### 5.3 pgvector HNSW vs 现 hnswlib:参数映射与召回对照

| hnswlib(现) | pgvector HNSW | 说明 |
|---|---|---|
| `M=16` | `m = 16` | 图连接度,直接映射。 |
| `ef_construction=200`(`HNSW_EF_CONSTRUCTION`) | `ef_construction = 200` | 建索引质量,直接映射。 |
| `ef`(查询期,现代码用默认) | `hnsw.ef_search`(会话级 `SET`) | 查询期召回旋钮,按 top-N × pad 设。 |
| `space='cosine'` | `vector_cosine_ops` | 距离度量一致。 |

- **召回对照测试方案**:迁移后对同一批 query,pgvector HNSW top-k vs 现 hnswlib top-k(或暴力 exact top-k)算 **recall@k / overlap**,门槛「≥ 现 hnswlib recall 且 ≥ exact 的 X%」(沿用 2026-07-01 spec「fold 等价」的对照范式,允许近似小幅差)。种子选择、chunk ANN、KG ANN 三处各跑一次。
- **近似性差异**:pgvector HNSW 与 hnswlib 都是近似,但**建索引在 DB 内、随写增量维护**(pgvector 支持增量 insert),消除现「build/fold/stale」向量侧生命周期 —— delta 向量写入即进 HNSW,无需攒批 fold(§9 事故映射)。

### 5.4 确定性序契约(PG 无 rowid)

本周多次踩 first-seen/rowid 序。PG 下:

- **显式化所有隐式序**:凡当前靠 SQLite 表扫序/rowid 的位点(`_stream_seed_reps` 两处 `ORDER BY rowid`、canonical 选取顺序),迁移时改 **`ORDER BY created_at, id`**(稳定、可复现、跨后端一致)。
- **canonical 选取来源盲**(kg_merge.py:318,分层锚定 memory 标注的硬缺口):PG 迁移**不趁机改**其选取逻辑,但把「靠 rowid 定基数」显式换成确定列排序,顺带消除「同 rowid 序假设」脆弱性。
- 契约测试:等价性套件断言「同输入 → 同 canonical / 同 top-k 序」,跨 SQLite/PG 双后端跑一致。

---

## 6. 迁移工具与运维

### 6.1 一次性 SQLite → PG 搬运 CLI

- **形态**:`scripts/migrate_sqlite_to_pg.py`(复用现有 CLI 心智,README 补文档 —— 见 memory「CLI 要进 README」)。
- **分表分批**:按表拓扑序(users → notebooks → sources → … → embeddings → 派生表),每表 `CHUNK=1000` 批量 `COPY`/`executemany`(仿 `store_kg` 分块),避免单大事务(百万行)。
- **向量转换**:`vector TEXT/BLOB`(`decode_vector`)→ pgvector `vector(1024)` 字面量;跳过维度不符/空向量(沿用 `build_matrix` 的 skip 语义,**记数而非静默** —— 见 memory「CLI 拒绝静默降级」)。
- **JSON → JSONB**:`payload`/`evidence`/`element_ids`/`metadata` 原样搬入 jsonb(PG 解析)。
- **时间戳**:ISO `TEXT` → `timestamptz` 显式 parse。
- **校验**:每表 **搬运后 `COUNT(*)` 双边比对** + **抽样行内容比对**(随机 N 行 diff)+ **向量抽样 cosine 自比对**(搬运前后同 id 向量点积≈1)。任一不符 → 报错退出,不静默。
- **旁挂图件**:CSR 旁挂**不搬运**,迁移后触发一次 `build_scale_index`(从 PG 重建 CSR + viz),因向量部分已退役,build 更轻。

### 6.2 部署形态

- **docker-compose(PG 单机)**:新增 `docker-compose.yml`:`postgres:15` + `pgvector` 扩展(`CREATE EXTENSION vector`)+ 应用容器。README 部署段从「无 DB 服务、SQLite 文件」改为「需 PostgreSQL(pgvector)」;保留 SQLite 作为**本地开发/轻量单人**默认(README 已注明 "PostgreSQL + pgvector remain the future production/team-beta direction; local development does not require them" —— 与之对齐:**PG 是团队/规模化生产路径,SQLite 留作单人本地**)。
- **README 改动**(通用口径,不写机器特定):部署段增 PG 章节(compose 启动、`CREATE EXTENSION`、`DATABASE_URL=postgres://...`、多 worker `--workers N`);迁移工具用法;明确「SQLite 单人本地 / PG 多用户规模化」两档。

### 6.3 回滚方案

- P0-P2 期间 SQLite 路径保留 → 回滚 = 切回 `DATABASE_URL=sqlite:///...` + 重启(数据仍在原 SQLite 文件,迁移是**单向拷贝不改源**)。
- P3 删 SQLite 路径后 → 回滚需从 PG 反向导出(提供 `--reverse` 或保留切前 SQLite 快照);故 **P3 删除 SQLite 路径要在 PG 真机稳定运行一段观察期后**才做。

### 6.4 多 worker 上线步骤

1. 单 worker 切 PG 跑通(P1 验收)→ 2. 退役全局写锁、跨进程协调就位(P3)→ 3. `--workers 2` 灰度,观察 advisory lock / 调度器不重入 → 4. 逐步升 `--workers N`(N ≈ 核数的合理分数,连接池 `max_connections` 配套)。

---

## 7. 分期计划

每期独立可交付 + 可回滚;验收标准映射 §9 事故清单。

### P0 — 抽象层收口 + PG 双跑骨架(地基)

- 在 `NotebookRepository` Protocol 下补 **SQL 方言收口层**(占位符/`RETURNING`/`ON CONFLICT`/jsonb helper);抽出 `PostgresRepository` 骨架(psycopg3)。
- `DATABASE_URL` scheme 路由双后端;搭等价性测试骨架(同套件双跑)。
- 建 PG schema(建表 DDL 从 SQLite `executescript` 翻译,pgvector 扩展 + `vector(1024)` 列 + HNSW 索引 DDL 就位,先不切流量)。
- **验收**:PG schema 建成、pgvector 扩展可用;双后端骨架下现有测试对 SQLite 全绿;PG 侧空库建表 + 建 HNSW 索引成功;`DATABASE_URL` 切换不崩。
- **风险**:方言收口层遗漏位点(§8 深坑清单驱动 grep 全覆盖)。

### P1 — 关系型切换(不含向量)

- 200+ raw SQL 位点方言平移(除四张向量表);JSON→jsonb + evidence GIN(§3.2);时间戳→timestamptz;确定性序显式化(§5.4)。
- 迁移工具(§6)搬关系数据;`_write()` 全局锁**暂保留**(单 worker,先不动并发模型)。
- evidence 反查(P0-4)走 GIN;typeof 全表扫消除(jsonb 类型化)。
- **验收**:PG 单 worker 下全测试绿;迁移工具 COUNT/抽样比对通过;evidence 反查、typeof 场景 O(N) 消失;确定性序契约测试双后端一致;回滚(切回 SQLite)可用。
- **风险**:方言等价性回归(§8 逐项);canonical 序漂移(§5.4 测试守）。

### P2 — 向量切换 + 向量旁挂退役

- 四张 embeddings 表切 pgvector;检索热路径向量 KNN 下推 PG(`_vector_matrix`/`top_k_sims` 进程内路径退役);kwtok 按 §2.2 处理。
- `scale_index.py` 瘦身:删 `ann_*`/`chunk_ann_*`/`*_handle`;PPR 种子改 pgvector KNN;CSR 从 PG 构建;`save_scale_index`/`_open_scale_ann`/`add_items_to_ann` 向量部分退役。
- rustworkx 全图收敛到 CSR(§5.2)。
- 版本探针简化(§3.4:向量/kwtok 版本键退役)。
- **验收**:召回对照(§5.3)pgvector HNSW ≥ 现 hnswlib;矩阵加载/OOM 场景消失(RSS 实测下降);旁挂 build 时间下降(无双 hnswlib 构建);delta 随写即可查(pgvector 增量,无需 fold);PPR top-k 对照 rustworkx 等价。
- **风险**:pgvector HNSW 在 1024 维百万行的**建索引时间/内存未知**(§8 需 spike);召回近似差异超阈值。

### P3 — 多 worker + 清理

- 退役 `_write()` 全局写锁;跨进程协调:`_scale_building`→advisory lock、启动 reconcile→幂等/单 worker、调度器→单 worker、`_auto_index_checked`→状态表/放弃。
- `--workers N` 上线(§6.4);连接池 + `max_connections` 调优。
- 删 SQLite 路径(观察期后);删 `encode_vector`/`decode_vector`/FTS 虚拟表/向量矩阵缓存等死代码;README 部署段定稿。
- **验收**:`--workers N` 下写不互斥、advisory lock 不重入、调度器不重复跑;并发写吞吐较单锁显著上升;全站写不再被长事务(rebuild/copy)阻塞(P1-5 消失)。
- **风险**:跨进程协调竞态(advisory lock 泄漏/未释放);连接池耗尽。

---

## 8. 风险清单

### 8.1 方言深坑(逐项:sqlite 惯用法 → PG 对应)

| sqlite 惯用法 | PG 对应 | 位点线索 |
|---|---|---|
| `?` 占位符 | `%s`(psycopg3) | 全量位点,方言层统一转 |
| `PRAGMA foreign_keys/journal_mode/busy_timeout/...`(`_connect` 6 条) | 删除;外键默认强制;无 WAL/busy_timeout 概念 | `_connect`:427-433 |
| 隐式 rowid / 表扫序 | **无 rowid**;显式 `ORDER BY created_at, id` | `_stream_seed_reps` 两处 `ORDER BY rowid`;canonical 选取 |
| `typeof(x)` 全表扫 | jsonb 类型化后不需要;或 `pg_typeof`/`jsonb_typeof` | 事故清单「typeof 全表扫」 |
| `json_extract(col,'$.k')` | `col->>'k'` / `col#>>'{a,b}'`(jsonb) | 检索融合分、evidence 解析 |
| `json_each` / `json_group_array` | `jsonb_array_elements` / `jsonb_agg` | 展开/聚合位点 |
| `executemany` 批插 | psycopg3 `executemany` / `COPY`(大批用 COPY 更快) | `store_kg` 分块、迁移工具 |
| `IN(...)` 手工分块(`_IN_CHUNK`) | `= ANY(:array)` 数组参数,**无 999 上限**,分块可删 | `_delta_vector_matrix` 等 |
| upsert `INSERT OR REPLACE` | `INSERT ... ON CONFLICT (pk) DO UPDATE` | 各 upsert 位点 |
| `executescript` 建表 | 逐 DDL / 迁移脚本(Alembic 可选,或纯 SQL) | `_migrate`:460 |
| FTS5 `MATCH` / `tokenize='trigram'` | `pg_trgm` GIN + `%`/`similarity()`(或 tsvector) | `chunks_fts`/`kg_objects_fts` |
| `sqlite3.Row` 行工厂 | psycopg3 `dict_row` / `Row` | 全量读位点 |
| `INTEGER`/`TEXT` 弱类型 | PG 强类型(时间戳、jsonb 显式) | 迁移工具 parse |
| `AUTOINCREMENT`/无(用 TEXT id) | 保持 TEXT id,不引 serial | id 策略 §3.3 |

### 8.2 性能未知点(需 spike 验证)

- **pgvector HNSW 在 1024 维 × 百万行的建索引时间与内存**:单大库四表向量总量可达数百万行;HNSW 建索引在 PG 内的耗时/峰值内存需实测,决定是否分批建、是否需 `maintenance_work_mem` 调大、能否在线建(`CONCURRENTLY`)。**这是最大未知,P2 前必须 spike。**
- **pgvector 查询延迟 vs 现进程内 matmul**:现暴力 matmul 对已缓存矩阵是纯 CPU;pgvector 走 DB round-trip + HNSW。需确认单查询延迟不劣化(尤其 multi-query 扇出 ×5 时的连接/往返成本)。
- **evidence `@>` GIN 选择率**(§3.2):决定反查表能否退役。
- **connection pool 上限**:多 worker × 每查询多次向量查询,`max_connections` 与 pool 大小需按 `--workers N × 扇出` 配。

### 8.3 双跑期测试矩阵成本

- 等价性套件需**双后端各跑一遍**(SQLite + PG),CI 时间翻倍;PG 测试需真 PG 实例(CI 起 service container 或 docker)。
- 融合分 `[0,1]`/tau、确定性序、召回对照三类等价性测试是**新增**(现有测试假设单后端)。
- 成本可控手段:等价性 diff 测试只覆盖**热路径 + 事故清单位点**,不追求 200+ 位点全双跑。

---

## 9. 事故清单 → 迁移后如何消失/简化(验收锚点)

| 本周事故 | 根因(SQLite 时代) | 迁移后 | 兑现分期 |
|---|---|---|---|
| **矩阵加载 / OOM** | `_vector_matrix` 全量物化 4-6GB + `top_k_sims` GB 级 dict(P0-7) | **消失**:向量 KNN 下推 pgvector,进程不再物化矩阵 | P2 |
| **旁挂索引生命周期**(build/fold/stale/自动触发) | 向量部分(ann.bin/chunk_ann.bin)需 build→fold→stale→自动 rebuild 全套 | **大幅简化**:向量随写进 pgvector HNSW,无 fold/stale;旁挂只余 CSR 图件(仍需 rebuild,但轻) | P2 |
| **版本探针** | 5 个 `COUNT/MAX` 聚合 + `kg_mutation_seq` 单调计数器 + 单飞 | **向量/kwtok 探针退役**(pgvector 同事务最新);只余 rebuild 的 `kg_mutation_seq` 闸 | P2/P3 |
| **全局写锁** | `_write()` 进程级 RLock,长事务全站阻塞(P1-5) | **退役**:PG 行级锁 + MVCC,并发写不互斥 | P3 |
| **workers=1** | 进程内缓存/锁/set 假设单进程 | **解锁 `--workers N`**:跨进程协调(advisory lock/状态表)替进程内不变量 | P3 |
| **evidence 反查** | 全库 49 万行 evidence 逐行 `json.loads` 找引用某 source 的对象(P0-4) | **消失**:evidence jsonb + GIN,`@>` 直查(或触发器维护反查表) | P1 |
| **typeof 全表扫** | 弱类型下 `typeof()` 判别列内容全表扫 | **消失**:jsonb 强类型,`jsonb_typeof`/无需判别 | P1 |

---

## 10. 开放问题(每项带推荐)

| # | 问题 | 推荐 |
|---|---|---|
| Q1 | evidence 反查表退役 vs 触发器维护? | **先 spike `@>` GIN 选择率**;命中好则退役表,否则触发器维护(§3.2) |
| Q2 | kwtok 保留 Python 分词(去缓存) vs 全下推 tsvector? | **保留 Python 分词、退掉全量缓存**(候选已界定,融合分字节不变,风险最小)(§2.2) |
| Q3 | `kg_mutation_seq` 保留应用侧 +1 vs 转 PG 触发器? | **P2 保留应用侧**(不趁迁移改逻辑);触发器作 P3 opt-in 清理(§3.4) |
| Q4 | 数据访问层:psycopg3 raw SQL vs SQLAlchemy Core? | **psycopg3 raw SQL 平移**,方言收口到薄封装(§4.2) |
| Q5 | 双后端并存多久? | **仅 P0-P2 内部对照**;P2 真机稳定后 P3 删 SQLite 路径(§4.3) |
| Q6 | id 类型:保持 TEXT vs 转 uuid? | **保持 TEXT**(避 copy_notebook 全表 id+内嵌 element_ids 重映射复杂化)(§3.3) |
| Q7 | 建 HNSW:离线全量 vs `CONCURRENTLY` 在线? | **spike 后定**;迁移期离线全量(停机窗口),日常增量随写(§8.2) |
| Q8 | rustworkx 全图收敛到 CSR 是否随 P2 一起? | **随 P2**(与向量退役同期,共享等价性对照)(§5.2) |

## 11. 不做 / YAGNI

- 不做在线双写渐进切换(单机单库,停机窗口可接受)。
- 不做 PG 读写分离 / 只读副本(标尺单机)。
- 不做 pgvector IVFFlat(HNSW 召回/延迟更优,且与现 hnswlib 参数可映射)。
- 不做业务逻辑 ORM 化(只换持久层方言)。
- 不趁迁移改 canonical 选取语义(kg_merge.py:318 硬缺口另议,仅显式化其序契约)。
- 迁移期不动 KG 抽取/融合管线逻辑(仅其 SQL 方言)。
