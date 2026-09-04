# 批 3·W4:写路径杂项收尾(WR-3/WR-7/WR-9/QueryCanceled/SR-1-UNION)设计

状态:v2(2026-09-04,设计评审 4×P1 + 15×P2 全部采纳整改;v1 归档于 git 历史)。
对应审计 WR-3/WR-7/WR-9、结构性事实③,及批 2 遗留登记项「element 搜索腿 OR
跨表等价 UNION 改写」。三条红线照旧;实施切 2 个 PR(PR-A=T1+T2+T4+T5;
PR-B=T3,单独隔离因为带构建基线门)。

## T-W4-1(WR-3)「全部重新分析」同步半程瘦身

现状:`_prepare_notebook_kg_job_reserved` 在 `create_job` 之前跑
`_kg_target_count`(整库 keyset 枚举,每行 5 个相关子查询);worker 主循环
随后再枚举一遍。拒绝路径(模型未配置/单飞/维护闸)都在计数之前。

改法:同步半程去掉 `_kg_target_count`,`create_job` 落 `total_sources=0`;
worker 起跑后回填。**四条实施约束(评审 P2-1..P2-5)**:

1. **回填必须在 `set_stage(job_id, "extracting")` 之前**:create_job 落
   `stage='probing'`,前端对 probing 显示「正在连接模型服务…」(不带数字);
   计数放在 set_stage 之后会让大库用户盯几十秒「正在分析 0/0 项内容」。
2. **计数谓词用 `target_mode`**(主循环同款,rebuild 非 preserve 分支降级
   incremental)+ 同一 `target_limit/retry_partial`;计数点放在 rebuild 的
   `delete_notebook_kg` 相位**之后**(与主循环看到同一世界)。回填原语复用
   `extend_total_sources`(从 0 起 extend ≡ set,只在 running 时生效)。
3. **如实声明收益口径**:本项只把整库枚举从 202 请求路径搬进 worker 线程
   ——DB 总负载不变(仍两遍枚举)。按批 extend(边枚举边抬 total)是
   显式不做:total 递增会让前端进度分母跳动,换来的只是同一笔钱换个付法。
4. **可观测面登记**:`kg_build_started` 事件的 `total_sources` 恒为 0
   (计数已后移),product-and-api 双语的事件族段落同步;
   `GET /kg/build-status` 的 total 语义从「受理即准」变「起跑即准」。
   `extend_total_sources` 顺手补进 `KgBuildJobStorePort` 与 ownership
   manifest(登记债,非硬门)。

崩溃格:create_job 已提交、worker 计数前死 → total=0 的 running 行由既有
启动恢复(stale running 结算)兜住,与今天 total>0 的同形残行同一通道。
回滚 = revert(prepare 恢复同步计数),无持久状态迁移。

## T-W4-2(WR-7)删除单个来源:容量锁退出 teardown

现状(核实属实):`delete_source` 的唯一写事务以
`source_exists_for_update_tx(db, sid, notebook_id)` 开头,notebook_id 非
None 时顺带取 notebooks 行 FOR NO KEY UPDATE(容量锁),整个 per-source
teardown 骑在上面;锁的既有 docstring 承诺「毫秒级事务」。

改法:delete 调用点 notebook_id 传 None(签名已支持,sqlite 侧本就丢弃),
只保留 source 行 FOR UPDATE。论证(按评审 P2-6/P2-7 修正措辞):

- **容量方向安全**:READ COMMITTED 下并发容量创建者的 COUNT 只可能**多**数
  到待删行 → 保守拒绝;不存在少数的路径。
- **死锁**:删除者的持锁集合是改前的真子集,去锁不可能新增等待环。
  (事实修正:DELETE 引用行对父行不取锁——取 KEY SHARE 的是 INSERT;
  本事务尾部的 `mark_unified_kg_dirty_in_tx` 写的是 unified_kg_state 行,
  与 notebooks 行无关。)
- **notebook→source 写序不变量**(`replace_elements` 明写的 phantom-source/
  element-swap 竞态闭合依据):删除退出该序后两条竞态仍闭合——双方互斥的
  载重件是 `sources` 行 FOR UPDATE(删除者与 element 替换者都取),notebook
  行锁在删除侧从来只是搭便车,不是那两条论证的一部分。
- **「单源 teardown 不切页」的前提**(评审 P2-8 修正):各清理步按
  member/source 索引有界**当 `source_index_backfilled=1`**;未回填老库上
  `_stale_object_ids_for_source_batch` 的 legacy 分支走 `evidence @>` 无
  GIN 键集扫,单语句可撞 30s——登记:该分支的修复前置是既有离线
  backfill(operations 文档已有 runbook),本项不给 legacy 分支切页。
- **显式不做**:`ingest_memory_source` 重解析分支同样经该函数带 notebook_id
  ——保持不动(memory 源恒小,teardown 微秒级,改它只添审面)。

回滚 = revert 单提交。

## T-W4-3(WR-9)scale build 图侧有界化(实施 PR-B)

按评审两条 P1 重定向:

1. **图侧取数 keyset 分页**——病灶名单按实际代码(评审 P2 补全):
   `graph_rows` 的 4 条 fetchall + `active_object_graph_rows` +
   `ent_chunk_map` 链上的 `notebook_object_evidence_rows`(全量 evidence
   JSON)与 `id_element_rows`。分页键与索引依据逐条点名:objects/chunks 用
   `ordinal`(全局 UNIQUE,单列 keyset 安全;登记:无 (notebook_id,
   ordinal) 复合索引,keyset 在全局唯一索引上带 notebook 过滤——单租户
   生产可接受,多库共存时的残余成本如实登记);clusters 用
   `idx_clusters_nb_canonical_member` 的 (canonical_id, member) 键。
   **如实声明**:分页界住的是 fetchall 缓冲与快照时长,Python 侧图结构的
   峰值驻留不变(那是 WR-9 矩阵/图派生的固有形态,本项不动)。
   oracle:小库两实现产出逐位对比(排序后)。
2. **在线 standalone viz 生成补大库闸**(取代 v1 的「scale build viz 阶段
   加闸」——那是方向反了:scale build 的 viz 阶段是大库 viz 的**指定
   生产者**(rebuild 尾部明文委托,off-peak),闸它=把 12-20GB 物化逼回
   API 进程的 `_spawn_viz_build`,OOM 从离线搬到线上)。真正的在线洞:
   大库**尚无** scale-embedded viz 时,开图触发 `_spawn_viz_build` 在服务
   进程 daemon 线程里物化整图(audit P0-2 形态,且每次开图重触发)。
   改法:`_spawn_viz_build` 前加 `viz_sync_build_max_objects` 同款判据
   (rebuild 尾部已用),超限不 spawn、发结构化事件,`unified_graph` 返回
   诚实降级(viz_building=false + 既有「产物缺失」态;该库的 viz 由下一次
   scale build 生产——`maybe_auto_index` 本就会排队)。产品可见变化:
   超大库在 scale index 建成前图谱视图无折叠 viz,显示既有的缺失文案,
   不再冒 OOM 风险;product-and-api 双语登记。
3. **ANN 构建分页喂入**(按评审 P1-B 修正机制):`np.asarray` 是别名不是
   拷贝,收益必须来自**加载**分页——chunk/relation 腿复用
   `embedding_store.vector_pages` 的 keyset 页直喂 `add_items`(hnswlib
   内部副本 1× 不可免,免掉的是加载侧整矩阵第二份);KG 腿因
   `emb_synonym_edges` 的 `knn_query` 要整矩阵作查询集,同步改**分页
   query**(逐页 knn_query,合并候选——上限/去重语义与整矩阵一致,
   oracle 钉 top-k 集合相等)。验收改 recall/top-k 断言 + 测试
   `num_threads=1`(hnswlib 多线程插入不保证逐位可复现,评审 P2-9);
   `total_build_ms` 基线劣化 ≤10% 硬门,超门回退分页粒度或整项回退。

## T-W4-4 QueryCanceled 统一出口(目标按评审 P1-C 降级)

现状修正(评审核实):连接池 reset 路径已 rollback+RESET+IDLE 校验,
「毒化连接」不成立;handler 里拿不到连接对象,「显式 rollback 兜底」删除。
前端 errors.ts 对 5xx 一律泛化文案(刻意防伪造),503 的中文指引到不了
用户——不硬闯这条既有安全裁决。

降级后的目标:**不再裸 500 + 可观测**。
1. FastAPI 全局 handler:`psycopg.errors.QueryCanceled` → 结构化 503
   (机器可读 code=query_timeout + 结构化事件含路由/notebook 维度);
   前端显示既有通用「服务暂时不可用」——如实接受,不发明 4xx 语义
   (408/413 都不是这个错的真语义)。
2. 维护站点超时放宽按**站点分类**(评审 P2-11):事务内站点用
   `SET LOCAL statement_timeout`;非事务站点(CONCURRENTLY 类)沿用既有
   session 级 `set_config` 先例。本项交付=普查现存裸奔维护站点清单 +
   逐个按类补,清单进 PR 描述。

## T-W4-5(SR-1 遗留)element 搜索腿 OR→跨表等价 UNION

现状(核实属实):`notebook_element_rows` 谓词
`se.text ILIKE OR se.location_label ILIKE OR s.title ILIKE`,s.title 腿把
计划钉死成 join 后全 element 扫。

改法(按评审 P1-D 修正两处写法):
- 腿 A:`se.text ILIKE OR se.location_label ILIKE`,**per-leg
  `ORDER BY se.ordinal LIMIT cap`**;
- 腿 B:**`LOWER(s.title) LIKE %s`**(0048 的索引是 lower(title) 表达式
  GIN,`ILIKE 裸列`用不上;needle 上游已 .lower(),零语义代价——先例
  `list_sources_page` 同款)先收 source id,再取其 elements,同样 per-leg
  `ORDER BY se.ordinal LIMIT cap`;
- 外层按 se.id 去重后 `ORDER BY ordinal LIMIT cap`。等价论证:并集的前
  cap 小 ordinal 必含于两腿各自前 cap 之并(ordinal 全局 UNIQUE);
  oracle 四场景(交叉命中/仅 title/仅 text/超 cap 截断)逐字对比 + PG
  EXPLAIN 钉 title 腿走 trgm 位图。
- **收益边界如实登记**(评审 P2-12):source_elements 无 notebook_id 列、
  se.text 无 trgm——腿 A 仍是 join 后扫,真实收益只覆盖「仅 title 命中」
  与「title 腿早停解放整体计划」两类;「给 se.text 加 trgm」按写放大
  登记为显式不做(表 5.77M 行,收益场景窄)。
- **SQLite 侧**(评审 P2-13):现状无 ORDER BY(与 PG 已有分歧)。裁决:
  sqlite 采用同款两腿+ORDER BY ordinal LIMIT 形态——行为变化方向是
  确定性化(修分歧,非引入分歧),登记为刻意对齐;oracle 逐字对比只在
  PG 侧跑,sqlite 侧跑集合等价 + 有序断言。

## 跨项(评审 P2-14/15)

- 文档:T1(build-status total 语义 + kg_build_started 字段)、T3-2
  (超大库 viz 降级)、T4(503 结构化出口)进 product-and-api 双语;
  README/AGENTS 无用户面新入口不动;fangan_done 收官时一并补。
- 崩溃-恢复:T1 见上;T2 批间无(单事务不变,只是少一把锁);T3 分页读
  无写;T4/T5 只读改写。回滚:每项独立 revert,无 schema 迁移。
- 明确不做:T1 按批 extend;T2 legacy evidence 分支切页与 memory 重解析
  分支;T3 Python 图结构峰值;T5 se.text trgm;全局超时分档。
