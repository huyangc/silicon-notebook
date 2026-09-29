# 来源天花板绑定语句风险台账与整改设计（PR-F）

状态：只读清点与实测的结论（2026-09-29，基线 master @ 9bacb8d6 / origin/master @ 5c332b90）。实施任务见文末分组；
与 `2026-09-29-retrieval-permission-remediation.md` 同属一轮整改。测量脚本与原始结果在会话 scratchpad，不入库。

基线与在途分支：
- 基线是 `master` @ `9bacb8d6`。`origin/master` 领先 5 个提交（`5c332b90`），已经 `git diff master origin/master -- backend/app` 核对，不涉及绑定语句，结论同样适用。
- 两个在途分支**都没合入**，不算"已保护"：
- `claude/enumeration-source-ceiling`（`bdf1a4cb`）：KG 枚举页的天花板谓词，已经用了 `prepare=False` 加 SQLite 一元 `+`。
- `claude/scope-ceiling-remediation`（`277a26a0`）：整改总计划，其中 E1 是"所有入口默认冻结"。

## 1. 背景与方法

### 1.1 天花板从哪里来、有多大

| 来源 | 位置 | 形态 | 最大规模 |
|---|---|---|---|
| HTTP 问答 | `api/ask_routes.py:121-207 _validate_source_scope` | 恒冻结为 include；全选（narrowed=False）时并入本人隐藏源 | 49 000 可见 + 本人 Memory/Knowhow；取消 1 个时为 48 999（narrowed=True） |
| 全局问答 | `global_ask.py:787-789` → `global_run.py` | 覆盖全部参与库的逐库天花板（≤8 个库） | 每库 ≤ 可见来源数 |
| 插件引擎 | `ask_service.py:2166-2213`（无 scope 时也合成全选）+ `plugin_ask_engine.py:528-570` | (notebook, source) 键 | 各参与库合计 |
| 报告 | 三个阶段都经 `_validate_source_scope` 重冻结 | 同 HTTP | 同 HTTP |
| MCP `ask_notebook` / `search_notebook_context` | 今天不装 scope | E1 之后与 HTTP 同形 | — |

其它 id 列表都有窗口上界，不会到 49k 量级：
- 元素 id ≤64。
- 对象 id ≤400，按 900 一批。
- chunk 候选按 900 一批。
- 笔记本 id ≤8。

### 1.2 可达性记号

- **R-HTTP-全选**：`POST /api/notebooks/{id}/ask(/stream)`，浏览器默认全选。
- **R-HTTP-收窄**：同上，但至少取消 1 个来源。
- **R-GLOBAL**：`POST /api/global-ask/ask` 或 MCP `ask_global`。
- **R-PLUGIN**：启用 `ask.engine` 插件后的提问。
- **R-GQ**：`GENERATED_QUESTION_INDEX_MODE≠off`（默认 off）且基线命中 <5。
- **R-CONTRIB**：启用了检索贡献扩展。
- **R-REPORT**：报告各节的检索叶子。
- 49k 来源的库一定不可复制（chunks+nodes > 5000），所以 chunk 通道要么走 ANN 加词法并集，要么走 FTS 降级。**两条路都会调用带清单的 `chunk_fts_search`**。

### 1.3 测量环境

- **数据**：由 `gen.py` 确定性生成。`nb-big` 有 49 000 可见源和 20 个本人 Memory 源，每源 2 个 chunk（共 98k），每个 chunk 1 条问题（98k）。每源 1 个 KG 对象（49k），带 evidence 和 kos 反向索引（backfilled=1）。每 5 个对象一簇，500 个 canonical 一个社区，另有一个 5 000 对端的共提 hub。`nb-other` 有 50 源。
- **schema**：PG 用 `PostgresMigrator.migrate()`（65 个迁移），SQLite 用 `SqliteMigrator.migrate()`。**没有**构造仓库工厂，只实例化各 store，避免触发管理员密码重写。
- **天花板规格**：
- 4k：稀疏，每 12 个取 1 个。
- 49k：49 020，全选冻结形态。
- 48999：收窄形态（取消 1 个）。
- none：基线，不传清单。
- **PG**：16.15，psycopg 3.3.4（`prepare_threshold=5`）。每个 case 用单连接池，在同一连接上执行真实 store 方法 15 次，重复 3 轮，最后读 `pg_prepared_statements`。chunk FTS 超时放宽到 10 s。
- **SQLite**：3.53.2，本机编译的变量上限是 250000；用 `setlimit(…,32766)` 模拟部署机（依据 `core/cache/sqlite_backend.py:32-35`）。
- **负载**：1 分钟负载在 9.6–91 之间，17:01–17:04 峰值 68–91。结论以同一时段的比值和中位数为准。

### 1.4 命令与退出码

脚本都在上述目录，`PYTHONPATH=…/backend:$PWD`，解释器是 miniconda python。

1. `psql -d postgres -c "CREATE DATABASE silicon_notebook_ceilhaz_f170_test;"`，再装 `pg_trgm` 和 `btree_gin` 扩展。退出码 0。
2. `pg_migrate.py <url>`：0（输出 migrated 65）。
3. `load.py sqlite $PWD/sq`、`load.py pg <url>`：第一次因 `memory_items.origin` 的 CHECK 约束失败（exit 1），修正后都是 0。
4. `bench_pg.py <url> <9 cases> 3`：0。前两次因 EXPLAIN 辅助代码的类型映射报错（exit 1），修正后整轮重跑。
5. `N_EXEC=2 bench_sqlite.py … 1`（探路）：0。
6. SQLite 正式测量三组，退出码都是 0：
 - `N_EXEC=3 LIMITS=local CEILS=none,4k,49k bench_sqlite.py … 3`
 - `… contrib_rows,ids_for_sources 3`
 - `LIMITS=32766 CEILS=48999 … kg_fts,kg_fts_auth 1`
7. `N=3 remedy_sqlite.py`：0。
8. `remedy_pg.py <url> baseline|noprep|excluded …`：三次都是 0。
9. `bench_py.py`：0。
10. `explain_check.py`：0。
11. `DROP DATABASE silicon_notebook_ceilhaz_f170_test`：0；`rm -rf sq`：0。

## 2. 台账

### 2.1 PostgreSQL

**P1 chunk FTS 候选**
- 位置：`postgres/search.py:518`（语句在 `:541-557` 组装和执行）。
- 调用链：`_candidate_rows_for_terms`（chunks 分支）← `chunk_candidate_rows_for_terms:692` ← `knowledge_store.py:118 _lexical_candidate_union` ← `chunk_fts_search:3134`。
- 清单：本库冻结清单 / 全局问答逐库可见清单 / 插件 `source_keys` / 报告授权全集。
- 绑定：`"chunks".source_id=ANY(%s)`，在每个词项的 LATERAL 里。最大 49 020。
- 风险：**H1（实测）**，上游还有 H5。
- 可达：R-HTTP-全选、R-HTTP-收窄、R-GLOBAL、R-PLUGIN、R-REPORT，每个子查询一次（≤8）加关键词臂一次。
- 保护：只有 3 s 私有超时和熔断。

**P2 KG FTS 反向索引闸**
- 位置：`search.py:512-516`。
- 清单：`_retrieve_scored` 里的 `source_filter`。
- 绑定：`EXISTS(… kos.source_id=ANY(%s))`，两个 arm 各绑一次。最大 48 999。
- 风险：**H1（实测）**。
- 可达：R-HTTP-收窄；R-PLUGIN 收窄时（`plugin_ask_engine.py:735-737`）；`_federated_retrieve_impl` 带显式键时。
- 保护：无。

**P3 KG FTS 权威闸**
- 位置：`search.py:505-510`。
- 绑定：`EXISTS(jsonb_array_elements(evidence) … ev->>'source_id'=ANY(%s))`。
- 风险：**H1（实测 40×）**。
- 可达：同 P2，且该库 `source_index_backfilled=0`。
- 保护：无。

**P4 生成问题行**
- 位置：`chunk_store.py:95-125 question_index_rows`。
- 绑定：`q.source_id=ANY(%s)`，`ORDER BY q.id LIMIT 10001`。
- 风险：**H1** 和 H5。
- 可达：仅 R-GQ。49k 时结果总是超过 10 000 行，付完查询后被 `skipped_scan_limit` 跳过。

**P5 贡献水合**
- 位置：`chunk_store.py:423-455 retrieval_contribution_rows`。
- 绑定：`c.source_id=ANY / <>ALL(%s)` 加 ≤900 个 `c.id` 占位符。清单来自 `retrieval_candidates.py:3895-3908`，每次都 `sorted(set())`。
- 风险：**H1（实测 3.7×）** 和 H5。
- 可达：R-GQ、R-CONTRIB。

**P6 `ids_for_sources`**
- 位置：`chunk_store.py:173-176`，`source_id=ANY(%s)`。
- 风险：H1 轻微。
- 可达：只在可复制的小库（`_gather_chunks`）。

**P7 `ids_for_sources(presence_only)`**
- 位置：`chunk_store.py:161-171`，`unnest(%s::text[])`。
- 清单：报告 ANN 旁车缺失的来源，通常很小。无显著风险。

**P8 社区成员对比兄弟**
- 位置：`unified_kg_store.py:1446-1478`，谓词在 `:95-125`。
- 清单：逐库天花板，由 `communities.py:266-294` 以 `sorted` 结果传入。
- 风险：**H1（实测）** 和 H5。
- 可达：**R-GLOBAL**。

**P9 共提对比兄弟**
- 位置：`unified_kg_store.py:1481-1545`，limit-gate 在 `:1515`，取名在 `:1534`。
- 风险：**H1（实测）**。
- 可达：**R-GLOBAL**。

**潜在与其余**
- **P10** `source_store.py:1043-1066 retrieval_element_rows` 带清单的一支：**不可达**。`_retrieve_elements` 只要有清单就改走 chunk 通道（`retrieval_candidates.py:2622-2663`）。
- **P11** `knowledge_store.py:1385-1398 relation_endpoint_rows(source_ids)`：逐个 `%s` 占位符，有 H1 和 65 535 绑定上限的隐患，但**不可达**（唯一调用方 `:2200` 不传清单）。
- **P12** `source_store.py:708-765 element_type_count_rows`：清单是笔记本全集的过期信号，不是天花板；冷缓存时是潜在 H1。
- **P13** `source_store.py:220-251 visible_source_scope_snapshot`：jsonb 单参数，冻结时每请求一次，无风险。
- **P14** `visible_source_ids_by_notebook`：≤8 个 id，无风险。
- **P15** `*_candidate_documents`：`id=ANY`，≤200 个 PK，无风险。
- **P16** `embedding_store.py:340-400 *_delta_rows`：按 900 分批，仅在 `SCALE_SEARCH_INCLUDE_DELTA` 开启时（默认关）。

### 2.2 SQLite

**S1 chunk FTS**
- 位置：`knowledge_store.py:3010-3040`。
- 绑定：`c.source_id IN (SELECT … json_each(?))`，单参数。
- 计划良好：LIST SUBQUERY 加 BLOOM FILTER，由 FTS 驱动。每次都 `json.dumps`（H5）。可达性同 P1。

**S2 KG FTS 反向索引支**
- 位置：`knowledge_store.py:2974, 2990-2998`。
- 绑定：**逐个 `?`**，`kos.source_id IN (?,…)`，在关联 EXISTS 里。
- 风险：**H2 + H3（均实测）**。
- 可达：R-HTTP-收窄、R-PLUGIN 收窄。
- 保护：无，而且异常被 `retrieval_candidates.py:1470-1477` 吞掉。

**S3 KG FTS 权威支**
- 位置：`knowledge_store.py:2974-2987`。
- 绑定：逐个 `?`，`json_extract(…) IN (?,…)`。
- 风险：**H3（实测）**；性能尚可，49k 时 43 ms。

**S4 生成问题行**
- 位置：`chunk_store.py:75-110`。
- 计划以天花板驱动（`idx_chunks_source`），再用 TEMP B-TREE 排序，成本与天花板内 chunk 总数成正比。
- 可达：R-GQ。

**S5 贡献水合**
- 位置：`chunk_store.py:426-460`。
- 风险：**H2（实测）**。计划以天花板驱动，而不是用 ≤64 个候选 PK。
- 可达：R-GQ、R-CONTRIB。

**S6 `ids_for_sources`**
- 位置：`chunk_store.py:130-155`。以天花板驱动是本意，没问题。

**S7 社区成员对比兄弟 / S8 共提对比兄弟**
- 位置：`unified_kg_store.py:88-130`（`_object_support_exists`、`_canonical_support_exists`），S7 在 `:1361-1395`，S8 在 `:1396-1470`。
- 风险：**H2（实测，S8 最重）**。
- 可达：**R-GLOBAL**。
- 保护：`_JSON_ID_LIST` 只防住了 H3。

**其余**
- **S9** `source_store.py:1059-1085 retrieval_element_rows`：逐个 `?`，潜在 H3，不可达。
- **S10** `source_store.py:692-752 element_type_count_rows`：已分批。
- **S11** `visible_source_scope_snapshot`：无风险。
- **S12** delta rows：已分批。

### 2.3 Python 热循环（H5）

**Y1 `source_scope.py:896-923 scoped_allowed_source_ids`**
- 无显式清单时 `sorted(ceiling)`，8.0 ms。
- 有显式清单时 `dict.fromkeys` 加逐个求交，5.2 ms。
- 逐库天花板形态分别是 11.7 / 8.9 ms。

**Y2 `retrieval_candidates.py:2832-2846 _chunk_source_ceiling`**
- 每次 = Y1 + 2.3 ms。
- 每个子查询调用 **3 次**：`:2810`、`:3203`、`:3473`，每次都把上一次的结果当显式清单再求交一遍。

**Y3 `retrieval_candidates.py:3478-3481 / 3549-3558 / 3730-3737`**
- 每次新建 frozenset，作 memo 键时重算哈希，再 `tuple(sorted(lexical_allowed))`。

**Y2+Y3 合计**：重放整条链，每子查询 **42.5 ms（PG）/ 77.9 ms（SQLite）**。

**其它服务层循环**
- **Y4** `:1959-1970, 2150` `_retrieve_scored`：Y1 + `dict.fromkeys` + `set()`，约 10 ms。
- **Y5** `:3895-3908`：`sorted(set())`，8.4 ms。
- **Y6** `source_scope.py:1152-1198`：两个 49k set，4.0 ms，每个检索臂一次（按设计不许 memo）。
- **Y7** `plugin_ask_engine.py:528-570` 加 `retrieval_candidates.py:2440-2468`：每次 `search` 都重建按库分桶的字典，再走整条链。
- **Y8** `graph_retrieval.py:1575-1594`：每个 owner 一次 frozenset。
- **Y9** `communities.py:290-294`：`sorted(ceiling)`。
- **Y10** `source_graph_activation.py:297` 和 `source_subgraph.py:215-233`：先排序、逐 id 哈希，再判 32 个来源的上限。
- **Y11** `retrieval_candidates.py:164-185`：frozenset。
- **Y12** `retrieval_baseline.py:354-366`：sorted，仅评测工具使用。

### 2.4 H4：谓词在 LIMIT 之后

- **A.** 未收窄时 KG 走整库 ANN(200) 加 FTS，再用 Python 按 evidence 丢弃（`retrieval_candidates.py:2016-2098, 2150-2162`）。他人私有 Memory 派生的对象、冻结后新增来源的对象会占名额。这是文档化的已接受残余；E1 之后所有入口都会是这种形态。
- **B.** 关系 ANN/FTS 不按来源分区（`:2376-2420`），文档化残余。
- **C.** 精确标识符的全局腿先取 `FTS_K` 窗口再过滤（`:164-185, 4287-4321`）；单库全选时没有来源谓词（`:4323-4345`）。
- **D.** PPR 和多跳叠加是整图游走后在边界裁剪，设计如此。
- **E.** GQ 在 49k 天花板下结果必然超过扫描上限，整条通道静默跳过（`:3033-3048`）。

### 2.5 已保护与已核对

**已保护**：
- chunk ANN 在 HNSW 回调里按来源过滤（`:3526-3627`）。
- SQLite 的计数、显示、元素水合都分批；`_in_batches` 覆盖候选水合。
- selected-source graph 有 32 个来源的硬上限。
- `_JSON_ID_LIST` 防住了 H3。
- master 上读语句**零** `prepare=False`（只在 `migrator.py:194` 和 `sqlite_to_postgres.py:1294` 出现）。

**已核对、不绑定天花板**：
- Memory 检索（有界候选）。
- 报告语料画像。
- 文档概览、表格分析（`citation_active_id`）。
- 集合枚举（`scoped_participants` 加 `scope.allows`，都是 O(1)）。
- PPR 构图（按参与集缓存）。

E1/E2 会新增绑定语句（`in_network_relation_rows`、PPR 水合、KG 枚举页），应直接用 §5 的机制。

## 3. 测量结果

### 3.1 PG：真实方法，同一连接（ms，3 轮中位）

每个 case 结束后 `pg_prepared_statements` 都显示 custom 5 / generic 5，所有序列都在**第 11 次**跳变。

| 用例 | 天花板 | 第1次 | 第5次 | 第15次 | 第11–15次 | 无天花板 | noprep 第11–15次 |
|---|---|---|---|---|---|---|---|
| P1 高频词 | 4k | 168 | 149 | 267 | 268 | 1669 | 206 |
| P1 高频词 | 49k | 1116 | 1145 | **3072** | **3136**（高负载轮次连续超时） | 1669 | 1746 |
| P1 稀有词 | 4k | 191 | 20 | **968** | 996 | 4.3 | 8.4 |
| P1 稀有词 | 49k | 325 | 245 | 第11次超时，缓存清空后回到 custom；EXPLAIN custom 7 / **generic 7709** | — | 4.3 | 47 |
| P2 | 4k | 46 | 29 | 37 | 37 | 17 | 39 |
| P2 | 49k | 122 | 100 | **550** | 373 | 17 | 124 |
| P3 | 4k | 59 | 20 | **693** | 737 | 22 | 29 |
| P3 | 49k | 169 | 118 | **6693** | **5993** | 22 | 78 |
| P4 | 49k | 358 | 277 | 498 | 470 | 151 | 182 |
| P5 | 49k | 78 | 32 | **293** | 304 | — | 51 |
| P8 | 49k | 122 | 75 | 228 | 237 | 0.9 | 88 |
| P9 | 49k | 187 | 200 | **501** | 511 | 1.8 | 143 |
| P6 | 49k | 144 | 100 | 165 | 156 | — | — |

反集形态：`NOT(col=ANY(21 个 id))`，正常预备，15 次，48 999 收窄场景。

| 用例 | 第1次 | 第15次 | 第11–15次 |
|---|---|---|---|
| P1 高频词 | 1666 | 1571 | 1569（≈ 基线） |
| P1 稀有词 | 12.8 | 5.7 | **6.2** |
| P2 | 36 | 34 | **35** |

**计划形态**：generic 计划一律把 `source_id=ANY($n)` 变成 Index Cond（按 10 个元素估计）。
- P1 稀有词：`Rows Removed by Filter: 97951`，7.7 s；custom 走三元组 bitmap，7 ms。
- P3：generic 下子计划里的 `=ANY($5)` 不能哈希，每次 0.695 ms，custom 是 0.002 ms。
- P5：generic 下 `Rows Removed by Filter: 97936`，custom 走 64 个 PK。
- 经 psycopg 自身绑定、`prepare=False` 的 EXPLAIN 确认：P1 49k 的 custom 为 1 315 ms，与第 1–10 次一致，也不比无天花板基线（1.7–2.1 s）慢。所以 **custom 计划下 49k 数组几乎不增加成本，退化完全来自 generic 计划**。

**psycopg 清缓存的连锁反应**：
- psycopg 在执行结果为 ROLLBACK（包括 `ROLLBACK TO SAVEPOINT`）时清空整条连接的预备缓存（`_preparing._should_discard`）。
- `chunk_fts_search` 超时后恰好会执行 `ROLLBACK TO SAVEPOINT`（`knowledge_store.py:3181-3183`），于是形成"generic → 超时 → 清零 → 再 10 次 custom → 再 generic"的循环。
- 每次超时还会打开该库本轮的熔断（`retrieval_candidates.py:1066-1068`）。
- `maintenance.py:115-127` 登记的"是否会被推向 generic plan 尚未实测确认"，现在已确认会。

### 3.2 SQLite：真实方法（ms，3 轮 × 3 次中位）

| 用例 | none | 4k | 49k | 部署上限 32766 下 48 999 |
|---|---|---|---|---|
| S1 高频词 | 40 | 47 | 70 | 正常 |
| S1 稀有词 | 0.3 | 1.0 | 9.8 | 正常 |
| **S2** | 4.8 | **530** | **6586–6972** | **OperationalError** |
| S3 | 4.5 | 19 | 43 | **OperationalError** |
| S4 | 12 | 17 | 77 | 正常 |
| S5 | — | 6.8 | 48 | 正常 |
| **S7** | 0.09 | **215** | 85 | 正常 |
| **S8** | 0.4 | **2033–2087** | **7759–8672** | 正常 |
| S6 | — | 5.3 | 48 | 正常 |

**计划形态**：
- S2、S7、S8：`SEARCH kos … uq_knowledge_object_sources_sync_key (object_id=? AND source_id=?)` 加 LIST SUBQUERY，即每行对每个天花板 id 探针一次。
- S7 稀疏天花板比 49k 更慢，因为不被支撑的成员要把整张清单探完。
- S5：`SCAN json_each` → `SEARCH c USING idx_chunks_source`。

**一元 `+` 与反集**（负载 82 时运行，绝对值偏大，比值有效）：

| 语句 | master 形态 | JSON 单参数 | JSON + `+` | 反集 + `+` |
|---|---|---|---|---|
| S2，4k | 1517（逐个 `?`） | 1217 | **11** | — |
| S2，48 999 | 14936 | 5646 | **14** | **10** |
| S7，4k | — | 544 | **1.4** | — |
| S7，48 999 | — | 86 | **6.1** | **1.1** |
| S8 limit-gate，4k | — | 5643 | **14** | — |
| S8 limit-gate，48 999 | — | 7839 | **20** | **14** |
| S5，4k | — | 12.8 | **0.5** | — |
| S5，48 999 | — | 50 | **5.1** | — |

加 `+` 后，S2/S7/S8 的计划变为 `(object_id=?)` 加 LIST SUBQUERY 成员判定；S5 变为走 chunks 主键。

### 3.3 Python（21 次中位，负载 17）

| 操作 | ms |
|---|---|
| `scoped_allowed_source_ids`：无显式 / 显式 | 7.96 / 5.23 |
| 逐库天花板形态：无显式 / 显式 | 11.74 / 8.86 |
| `dict.fromkeys` 元组 | 2.30 |
| frozenset + hash | 1.65 |
| `sorted(set)` | 8.43 |
| `json.dumps` | 1.69 |
| **整条链：PG / SQLite** | **42.5 / 77.9** |
| `universe_matches` | 3.96 |

## 4. 风险排序

1. S2：部署机上收窄运行 KG 静默全空；4k–32k 区间每子查询 0.5–7 s。
2. P1：覆盖全部大库检索；周期性 3 s 超时加熔断；稀有词 5×–1000×。
3. S8/S7：全局问答对比题 2–9 s。
4. P3/P2：收窄运行 0.5–6.7 s。
5. H5：每问 0.35–0.6 s CPU。
6. P9/P8：0.2–0.5 s。
7. P5/S5、P4/S4：默认关闭的通道，开启即踩。
8. 潜在：P10/S9、P11、P12。

## 5. 设计建议

### 5.1 领域值 `IdList`

- 位置：`backend/app/domain/id_list.py`。按规则，跨层稳定值放 domain，端口不能导入 services。
- 形态：tuple 子类，内容去重、全部 str、稳定排序，只在构造时归一化一次。
- 缓存三种形态：`members`（frozenset）、`json`、`array`。
- `intersect`：同一对象或同源时 O(1) 直接返回。
- 端口签名 `Sequence[str]` 不变，方法数 966 不变。

接入方式：
- `ActiveSourceScope` 加一个 `field(compare=False, hash=False, repr=False)` 的缓存槽，每个库只构造一次。
- `scoped_allowed_source_ids` 返回同一个对象；显式清单就是这个对象时直接返回（chunk 链的 3 次调用正属于这种情况），否则只求交一次。
- 全局问答冻结时、插件端口构造时、`_peer_visible_sources` 的 memo 都直接存 `IdList`。
- 反集作为伴随量挂在同一个值对象上，只在已经读过全集的冻结点计算，不额外加读。

### 5.2 PG：`repositories/postgres/id_binding.py`

- `execute_ids(conn, sql, params)`：恒 `prepare=False`。这样永远是 custom 计划，数组是常量、可哈希，规划器按真实元素数估计。
- `source_predicate(column, ceiling)`：默认 `=ANY`；满足反集条件时返回 `NOT(=ANY(excluded))`。
- `_BudgetedReadConnection.execute` 会透传 kwargs（已读代码确认），预算读路径同样生效。
- 代价是每次重新规划，每条约 1–3 ms。
- 可选的池级兜底：`plan_cache_mode=force_custom_plan`。它会让所有热小语句都失去 generic 计划的规划节省，只建议在测量后作为纵深防御，不作主机制。

### 5.3 SQLite：`repositories/sqlite/id_binding.py`

- `JSON_IDS`：`(SELECT CAST(value AS TEXT) FROM json_each(?))`。
- `member_of(col)`：生成 `+col IN JSON_IDS`，用于过滤型语句。
- `drive_by(col)`：生成 `col IN JSON_IDS`，只在确实要按清单逐个 seek 时用，名字本身就是审查信号。
- `ids_param(values)`：`IdList` 直接用缓存的 `.json`。
- 规则：天花板永远只占一个 `?`；默认用 `member_of`。

### 5.4 反集（需要你拍板）

- 收益：PG 上 47 → 6 ms、124 → 35 ms，且不受 generic 计划影响；SQLite 加 `+` 之后边际收益只有 14 → 10 ms。
- 问题：反集会接纳冻结之后新增的来源，违反"concurrent uploads cannot widen an in-flight run"。必须有冻结围栏，两种做法：
- (a) 每个检索臂的漂移探针（本来就不许 memo）判定全集未变才用反集，毫秒级窗口由 `filter_retrieval_items` 兜底，但窗口内的新来源仍可能占 Top-K。
- (b) 给来源加单调的可见序号，语句里加 `≤ 冻结序号` 条件，才是精确围栏，但需要 schema 变更。
- 建议：第一波只做允许集加计划安全绑定。这是零语义变化，已经消除全部秒级退化。`IdList` 预留 `excluded` 字段；反集等你拍板后作为独立任务。

### 5.5 守卫与钉子

新增 `backend/tests/test_id_list_binding_guard.py`（G1 轻量 AST）：
1. PG：SQL 字面量里出现 `ANY(%s)` / `ALL(%s)` / `unnest(%s` 的函数，其执行调用必须经 `execute_ids` 或显式 `prepare=False`。存量例外进 `architecture_boundary_baseline.json` 的新棘轮，只许减少。
2. SQLite：禁止对名称匹配 `source_ids|allowed|ceiling|visible` 的变量做 `",".join("?" for …)`；禁止在 `id_binding.py` 之外出现 `IN (SELECT … json_each(?))`。
3. 行为测试：装一个 49k 的 scope，跑 `_retrieve_chunks`、`_retrieve_scored`、关键词臂，断言每次运行每库只调用一次 `IdList.of`，且 store 收到的是同一个对象。
4. 运行时钉子：
 - G3：以 5k 个 id 执行 15 次后，`pg_prepared_statements` 里没有该语句。
 - SQLite EXPLAIN：不得同时出现 `(object_id=? AND source_id=?)` 与 LIST SUBQUERY；S5 必须走 chunks 主键。
 - 部署上限：`setlimit(32766)` 加 49k 清单，`fts_search` 必须成功且与参考模型一致。

### 5.6 边界契合

- 依赖方向：服务层只处理 `IdList`，方言形态全在两个 `id_binding.py` 里。
- 端口方法数：966 不变。死分支 P10/S9、P11 建议直接删除。
- 函数长度天花板：清单里的热函数（`ask_chunk`、`ReasoningRetriever.run`、`_draft_section` 等）都不用动。
- store 只导入 `app.domain.id_list`，不导入服务。

## 6. 任务分组（文件集互不相交，按风险排序）

**W0 前置（串行，opus）**
- 文件：`domain/id_list.py`、`repositories/postgres/id_binding.py`、`repositories/sqlite/id_binding.py`、`services/source_scope.py`，以及对应单测。
- 验收：同一个 scope 同一个库两次调用返回同一对象；现有测试全绿。

**W0 之后并行（同时 ≤4 个）**

- **T1（P0 正确性）**
- 文件：`sqlite/knowledge_store.py`、`sqlite/unified_kg_store.py`。
- 改动：S2/S3 改为 JSON 单参数加 `member_of`；S1 用 `ids_param`；S7/S8 用 `+kos.source_id`。
- 验收：部署上限 32766 加 49k 成功；EXPLAIN 钉子；与 PG 孪生一致；S2/S8 在 49k 下 <50 ms。
- **T2（P0 性能）**
- 文件：`postgres/search.py`、`postgres/knowledge_store.py`。
- 改动：`_candidate_rows_for_terms`、`_knn_candidate_rows_for_terms`、`*_candidate_documents` 改用 `execute_ids`；`_lexical_candidate_union` 对 `IdList` 不再重复去重；删除或改造 P11。
- 验收：G3 钉子；稀有词 49k 第 15 次 <100 ms。
- **T3（P1）**
- 文件：`postgres/chunk_store.py`、`sqlite/chunk_store.py`。
- 改动：P4–P7 改用 `execute_ids`；S5 加 `+c.source_id`；S4 改成 `member_of`；S6 显式 `drive_by`。
- **T4（P1）**
- 文件：`postgres/unified_kg_store.py`、`postgres/source_store.py`、`sqlite/source_store.py`。
- 改动：P8/P9/P12 改用 `execute_ids`；P10/S9 删除或改单参数。
- **T5（P1，H5）**
- 文件：`services/retrieval_candidates.py`、`services/chunk_federation.py`。
- 改动：`_chunk_source_ceiling`、`_retrieve_chunks_ann`、`_retrieve_scored`、`_hydrate_generated_question_chunks`、`_ceiling_bound_exact_deps`、关键词/精确腿都改用 `IdList`；`_peer_visible_sources` 存 `IdList`。
- 验收：行为测试；重放整条链 <5 ms。
- **T6（P2，H5）**
- 文件：`services/plugin_ask_engine.py`、`graph_retrieval.py`、`communities.py`、`source_graph_activation.py`、`source_subgraph.py`、`retrieval_baseline.py`。
- 改动：插件端口按库缓存 `IdList`；Y8/Y9；Y10 先判上限再排序。

**T7 收尾（T1–T6 之后）**
- 文件：`tests/test_id_list_binding_guard.py`、`scripts/architecture_boundary_baseline.json`（棘轮）、`docs/development.md` 和 `_zh.md` 各加一段 id 清单绑定规则。
- E1/E2 新增的语句在各自 PR 里直接用本机制。

**反集**：等你就冻结围栏拍板后，作为独立任务。

## 7. 清理

- 一次性 PG 库已 DROP（退出码 0，查询确认残留 0 个）。
- `sq/` 临时 SQLite 目录已删（退出码 0）。
- 受跟踪的工作树没有改动；`git status` 只剩原本就在的两个未跟踪文件。
- 脚本和原始结果保留在上述 scratchpad 目录，可按 §1.4 在新的一次性库上复现。
