# 全局性能/内存审计(16核/64G,49万对象/百万 chunk 标尺)

> 2026-07-02,三路并行审计(查询时检索 / 后台任务+摄取 / 端点+SQLite+驻留)合成。
> 已修不列:KG viz 页 O(N)(PR#152)、VectorCache single-flight+LRU(PR#153)、版本探针 O(1)(PR#147)、hnsw handle 缓存(PR#147)、chunk FTS(PR#149)、kb 列表轮询风暴(PR#136)、find_duplicates 分块(已修)、build_scale_index 内部(PR 进行中:hnsw 复用/内存节食/向量 BLOB 化)。

## P0 — 用户可感知的查询/交互路径(每次提问/点击都在付费)

| # | 位置 | 问题 | 规模代价 | 修法 |
|---|---|---|---|---|
| P0-1 | `_retrieve_relations_scored` sqlite_repository.py:8095 + `_relations_with_names`:8071 | **关系检索完全无候选界定**:全量 relations⋈objects⋈objects JOIN + 逐行 json.loads,每次调用,无缓存 | 百万行/查询,秒~几十秒 | 关系 ANN(镜像 `_kg_object_candidates`)或先按候选 id 集合界定 JOIN |
| P0-2 | `_mix_retrieve`:9028 → `federated_retrieve_relations` | P0-1 挂在**默认 chunk 模式**的 overlay 上(`chunk_kg_overlay_enabled` 默认 True + rerank 配置了就走)→ **每个普通问答都付 P0-1** | 同上,乘以所有问答 | 短期:overlay 里关系检索限定到已算出的候选集;长期同 P0-1 |
| P0-3 | `review_queue`:3699(边审查队列端点) | 全量 relations+objects 建 rustworkx 全图 + **Brandes betweenness centrality O(V·E) 同步跑在请求线程**,零缓存 | 分钟级 CPU/请求 | 按 mutation-seq 版本缓存 + 后台算;或先 top-K by degree 再算 |
| P0-4 | `_clear_source_extraction_state`:1064(删除/重解析来源必经) | 全库 49万行 evidence JSON 逐行解析,只为找引用某一个 source 的对象 | O(全库)/一次来源操作 | 反查表 `knowledge_object_sources(object_id, source_id)` 或冗余列,变 SQL 直查 |
| P0-5 | `_ent_chunk_map`:8963(无缓存)+ `_kg_source_chunks`:8932 | 前者:49万 evidence + 百万 chunk element_ids 全解析,PPR 回退每查询付;后者:百万 chunk 全扫+集合交,mix/graph 模式每查询付 | O(N)/查询 ×2 处 | `_ent_chunk_map` 版本缓存(同 `_vector_matrix` 范式);element_id→chunk 反查表 |
| P0-6 | `trigger_scale_index_rebuild` 只有手动入口(routes.py:901) | **索引从不自动触发**:错过按钮 → P0-5/回退路径永久成为稳态 | 全部 O(N) 回退常驻 | eligible 翻真/delta 超阈值时自动后台 `_run_scale_op`(已有去重+idle 窗口调度,只差自动入队) |
| P0-7 | `_retrieve_scored` 无 ANN 分支:8200 + `query_sims`(vector_index.py:47) | 回退路径全量物化:49万行 SELECT*+Evidence 构造 + **两个全量 Python dict**(49万+百万 float);multi-query 扇出 ×5 | GB 级瞬时+秒级/查询 | 索引常驻后自然消;dict 改对齐 numpy+id→idx 映射 |
| P0-8 | `_retrieve_chunks` 无 ANN 分支:8368 + `score_chunks`(retrieval.py:634) | chunk 无 token 缓存(KG 对象有 `_keyword_token_sets`,chunk 没有):百万 chunk 每查询现场分词 | O(1M) 分词/查询 ×扇出 | `_chunk_token_sets` 版本缓存或纯走 chunks_fts |

## P1 — 后台任务/运维路径(拖慢摄取、锁住写、静默失能)

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| P1-1 | `run_merge_review_job`:4602 + `review_pending_merges`:4516 | 排空循环每轮 **2 次全量物化 pending**(百万行)再 Python 切 100 → O(total²/batch),百万候选=跑不完 | SQL LIMIT 取批 + EXISTS 做续判 |
| P1-2 | `incremental_fuse_source`:3884/:3947 | 每次上传**两遍**全量 `cluster_map`(百万成员行)同步扫 | 版本缓存 cluster_map 或按新对象种子定向查;至少两处共用一次加载 |
| P1-3 | 同上 :3897(Tier2 桥接) | `kg_incremental_tier2_max_entities=50000` → 49万库上 Tier2 **静默失能**(跨文档桥接不再发生,功能性回归非纯性能) | 用 scale index 的 hnsw 做桥接候选(替代全量向量加载),阈值语义显式化 |
| P1-4 | `copy_notebook`:1440 | 全部 9 张表 fetchall+重插在**单个全局写锁事务**里 + 2 个全表 FTS 全删全建(:1559) | 分表分批事务(仿 store_kg CHUNK=1000);FTS 增量插入新 id;大库拷贝后台化 |
| P1-5 | `_write_lock`:302(进程级全局写锁) | 所有 notebook 的写互相排队;长事务(rebuild/copy)期间全站写阻塞,40 线程池可被写等待耗尽 | per-notebook 写锁 |
| P1-6 ◐ | `relink_notebook_kg`:3592(CLI kg 阶段必经 + `/kg/relink` 端点同步跑) | 全库对象+关系载入解析,即使本轮只新增几个来源;端点还占请求线程 | **内存那一半已修**:改按来源分页驱动(峰值 = 单来源,不再是整库 payload+evidence),端点后台化 + 笔记本级单飞 + `GET /kg/relink/status`。**仍未做**:构建尾照旧跑整库(没有「只 relink 本轮新增来源」的范围),单来源内 sibling 双层循环的 O(n²) 也没动——巨型单来源仍是热点 |
| P1-7 | `backfill_kg_embeddings` 脚本:57 + `backfill_node_embeddings`(batch_ingest.py:511) | 每轮全量载 payload 再 Python 差集,×80 轮 | 改 NOT EXISTS SQL 只取缺失行(同文件已有正确范例) |

## P2 — 卫生项(索引缺失/N+1/无界缓存/设计债)

| # | 位置 | 问题 | 修法 |
|---|---|---|---|
| P2-1 | `knowledge_graph`:3442(GET /graph 旧端点) | 无任何规模守卫,49万全图物化;前端已无调用(潜在雷) | 删端点或套 `viz_sync_build_max_objects` 守卫 |
| P2-2 | answers/feedback **零索引**(analytics/会话路径);extraction_runs 无 (source_id, created_at) 索引(来源分页 1+3N) | 长期部署后全表扫 | 补 4+1 个索引;来源页 3 个 per-row 查询批量化 |
| P2-3 | `list_promotion_queue`:5896 N+1;`node_context`:5749 procedure 兜底全扫;`list_notebooks` 每本 7 个 COUNT | 次热路径浪费 | IN(...) 批量/GROUP BY 合并/兜底路径回填 steps |
| P2-4 | `_scale_idx_cache`/`_viz_idx_cache` 无界(:6873) | 多大库进程长期驻留线性涨(每本几十 MB~GB) | 套 VectorCache 同款 LRU |
| P2-5 | debug_logs 全文件 parse/请求(debug_logs.py:64);EventLogger 每事件 open/write/close+mkdir(event_logging.py:127);`_connect` 每调用 6 PRAGMA×202 调用点、40线程×64MB page cache 上限 | 高并发下系统调用/文件 IO 债 | 倒序 seek 分页;带队列的 writer 线程;thread-local 连接池 |
| P2-6 | `_quota_rerank`(reasoning_retrieval.py:163)答案期按子查询**重跑全量检索**无上限;`bm25_scores` 无 token 缓存(RRF 默认关) | 推理模式重复付检索成本 ×5 | 复用首轮已评分结果;开 RRF 前接 token 缓存 |
| P2-7 | `cluster_seeds` 确认合并后**整体重聚**一遍(rebuild:5311);概念描述阶段逐 canonical 串行小查询(:5350) | rebuild 峰值时长 ×2;几十万次 DB 往返 | 增量 union-merge 确认对;evidence 查询按批 |

## 驻留内存清单(64G 预算怎么分)

单大库稳态(全部缓存热):kg/chunk/element 矩阵 ~4-6GB + `:kwtok` GB 级 + `rxgraph`/`fed_rxgraph`/`ppr_graph` 多 GB(rustworkx 全图,版本缓存正确但**体积未实测**)+ scale index(CSR+双 hnsw)~5GB + 折叠 viz 数组。**建议部署后实测一次 RSS 分解**;`uvicorn --workers N` 会 ×N,当前架构保持单进程多线程。

## 推荐执行顺序

1. **正在进行**:build_scale_index 优化 PR(hnsw 复用 ✅ / 内存节食 / 向量 BLOB 化)→ 合并部署 → 建索引。
2. **P0 批**(建完索引后用户体感最大):P0-1/2(关系检索界定)、P0-6(自动触发)、P0-5(两个缓存)、P0-3(centrality 缓存)、P0-4(反查表)。
3. **P1 批**:P1-1(merge-review 排空)、P1-2/3(增量融合,含 Tier2 失能修复)、P1-5(per-notebook 写锁)、P1-4(copy 分批)。
4. **P2 批**:索引补齐一把梭(P2-2)+ 守卫(P2-1)+ LRU(P2-4),其余按碰到再修。
