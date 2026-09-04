# 批 3·W4:写路径杂项收尾(WR-3/WR-7/WR-9/QueryCanceled/SR-1-UNION)设计

状态:v1(2026-09-04)。对应审计 WR-3/WR-7/WR-9、结构性事实③,及批 2 遗留登记项
「element 搜索腿 OR 跨表等价 UNION 改写」。三条红线(检索/抽取/质量)照旧;
每项独立可回滚,合并为 2 个实施 PR(PR-A=T1+T2+T4+T5 服务/查询层;PR-B=T3
scale build,单独隔离因为它带构建时长基线门)。

## T-W4-1(WR-3)「全部重新分析」同步半程瘦身

现状:`prepare_notebook_kg_job` 在返回 202 之前跑 `_kg_target_count`
(整库 keyset 枚举,`source_build_state_page` 每行 5 个相关子查询——审计
实测 96 次借连接 + 28.8 万次索引探查),worker 主循环随后再枚举一遍。

改法:同步半程只留「单飞 CAS(kg_building)+ create_job + 事件/待办推送」。
`create_job` 落 `total_sources=0` 占位;worker(`_run_notebook_kg_job`)起跑
后第一步计数并经 `kg_build_jobs.extend_total_sources(job_id, count)`(PR-3
已有原语,从 0 起 extend ≡ set)回填,再进抽取循环。

- probe 分支(`if mode != "rebuild" and total_targets:`)与 `total_targets`
  改读 worker 自己算出的数,语义不变(它本来就是「本轮要抽的目标数」)。
- 可观察面变化(如实声明):202 与 worker 起跑之间的秒级窗口里 status 显示
  0/0——与既有「prepare 快照与主循环之间落地的源」同款的短暂失真,前端按
  未完成余量渲染不受影响;`GET /kg/build-status` 的 total 从「受理即准」变
  「起跑即准」。
- 拒绝路径(LLM 未配置/被闸)不变——它们在计数之前本来就返回。

## T-W4-2(WR-7)删除单个来源:容量锁退出 teardown

现状:`delete_source` 的唯一写事务以 `source_exists_for_update_tx(db, sid,
notebook_id)` 开头——它顺带取 `_lock_notebook_row_for_capacity`(notebooks 行
FOR NO KEY UPDATE),随后整个 per-source teardown(KO/簇/向量/facts 手工清 +
行删 CASCADE)都骑在这把锁上。该锁的文档不变式是「毫秒级事务」;大源上
teardown 分钟级,期间容量上传、notebooks 行 UPDATE、memory FOR SHARE 探针
全部 5s 锁超时。

改法(计划原文「容量锁只覆盖容量判据 + 批间提交」):
1. `delete_source` 不再取 notebook 容量锁——删除**不需要**容量判据(删只会
   腾位;并发容量检查读到删前计数只是保守拒绝,方向安全)。
   `source_exists_for_update_tx` 的 notebook_id 参数由 delete 调用点传 None,
   只保留 source 行 FOR UPDATE(对 extraction 终写 KEY SHARE 的围栏语义
   原样保留——这才是它在 delete 里的真职责)。容量创建者与删除者之间无
   死锁边:创建者持 notebooks NO KEY UPDATE 等待无物;删除者只碰 source 行
   与其子行,FK RI 对 notebooks 只取 KEY SHARE(不冲突)。
2. teardown 拆批间提交:`clear_source_extraction_state` 的各清理步已按
   member/source 索引有界,单源子行量级(10³–10⁴)本就远小于 30s——**本项
   刻意不把单源 teardown 再切页**(切页引入「半删源可见」的新窗口,收益是
   已经不超时的语句),只把「多分钟持锁」的病灶(容量锁)摘掉。若未来单源
   量级增长到语句超时,再按 T-5a drain 形态切页——登记为显式不做。

红线论证:删除路径不在检索/抽取热路径;fence 语义(FOR UPDATE source 行)
不变 ⇒ extraction 发布与删除的互斥不变;容量口径只可能更保守。

## T-W4-3(WR-9)scale build 图侧有界化(实施 PR-B)

三个子项,`manifest.total_build_ms` 基线验证构建时长劣化 ≤10%:

1. **图侧 keyset 分页**:`graph_rows`/`active_object_graph_rows` 等四条无界
   fetchall 改向量侧既有形态(`{id} > last LIMIT _MATRIX_FETCH_BATCH`,
   PK keyset,页间释放快照)。产出集合与顺序逐位不变(oracle:小库两实现
   排序后逐字对比;行序如对下游敏感,按现查询 ORDER BY 保持)。
2. **viz 复用大库闸**:`build_viz` 的 `_derive_object_graph_lite` 物化整图
   (生产 ~12-20GB),但 scale build 流水内的 viz 阶段绕过了
   `viz_sync_build_max_objects` 判据(rebuild 尾部已有同判据)。补同一
   判据:超限时 viz 阶段跳过并发结构化事件(索引主产物照常发布,viz 走
   既有 lazy/cseq 失效路径),不是砍功能而是把已有闸补到漏网的入口。
3. **hnswlib 矩阵分块 add_items**:`np.asarray(整矩阵)` + 一次 add_items 的
   峰值双持改为 init_index(max_elements=N) 后按页 add——构建结果与单次
   add 逐位同构(hnswlib 增量插入语义),内存峰值从 2× 矩阵降到 1×+页。

## T-W4-4 QueryCanceled 统一出口(审计结构性事实③)

现状:全仓仅 knowledge_store.py 一处识别 `errors.QueryCanceled`;其余站点
超时=裸 500 + 可能毒化事务后继续复用连接。

改法(入口层统一,不逐站点撒 except):
1. FastAPI 全局 exception handler:`psycopg.errors.QueryCanceled` → 结构化
   503 + 中文文案「这次查询在当前库规模下超时了;请缩小范围重试」——连接
   由池的归还路径 rollback(核实 psycopg_pool 归还脏连接的 reset 行为并在
   handler 里显式 rollback 兜底)。
2. 维护会话局部放宽:启动恢复/离线脚本等维护站点已有 `SET LOCAL
   statement_timeout` 先例——本项只做**登记核对**(列出仍裸奔的维护站点,
   逐个补 SET LOCAL),不引入新的全局超时档。

## T-W4-5(SR-1 遗留)element 搜索腿 OR→跨表等价 UNION

现状:`notebook_element_rows` 的谓词是
`se.text ILIKE ? OR se.location_label ILIKE ? OR s.title ILIKE ?` ——
`s.title` 腿把 OR 钉死成「join 后全 element 扫」,`ORDER BY se.ordinal`
再废掉 LIMIT 早停。

改法:拆两腿 id 半连接 UNION(批 4 `list_sources_page` q 过滤的同款先例):
- 腿 A:`se.text ILIKE OR se.location_label ILIKE`(element 自身列);
- 腿 B:`s.title ILIKE` 先收 source id(走 0048 批 4 的 sources 复合 trgm
  GIN),再取其 elements;
- 外层对 UNION(按 se.id 去重)统一 `ORDER BY ordinal LIMIT`。
等价论证:OR 的命中集 = 两腿命中集之并,去重后逐位相同;排序键同一列。
oracle 测试:同数据两实现输出逐字对比(含三腿交叉命中/仅 title 命中/
仅 text 命中/超 cap 截断)。SQLite 侧同构改写(无 trgm 收益但语义一致,
镜像方向红线)。

## 验收与 PR 切分

- PR-A(T1+T2+T4+T5):prepare 零枚举 pin(spy `_kg_target_batches` 在同步
  半程零调用)+ total 回填时序 pin;delete_source 不取容量锁的 pin(spy
  `_lock_notebook_row_for_capacity` 零调用)+ fence 仍在(FOR UPDATE);
  QueryCanceled handler 的 503 形状测试(fake 连接抛 QueryCanceled);
  UNION oracle 四场景 + PG EXPLAIN 形状(title 腿走 trgm 位图)。
- PR-B(T3):图侧分页 oracle + viz 闸 pin(超限跳过+事件)+ 分块 add 的
  ANN 查询等价 pin(同向量集 top-k 一致)+ `total_build_ms` 基线对比
  (本机小库,劣化 >10% 即回退分块粒度)。
- 明确不做:单源 teardown 切页(T2 内登记);全局超时分档;FTS 替换
  (独立轨)。
