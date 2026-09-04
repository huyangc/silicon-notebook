# 批 3·W4:写路径杂项收尾(WR-3/WR-7/WR-9/QueryCanceled/SR-1-UNION)设计

状态:v3(2026-09-04,复评 3×P1[均为文档层]+6×P2 采纳;v1/v2 归档于 git 历史)。
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
4. **`total_targets` 改读 worker 自算值**(复评 P2-4 找回的 v1 约束):
   `_run_notebook_kg_job` 入口的 `int(job["total_sources"])` 读的是快照,
   total 落 0 后不改这处会让 `if mode != "rebuild" and total_targets:` 恒假
   ——增量构建的起始探测被静默跳过(反向护栏在 circuit-breaker 套件,会红
   不会静默,但约束必须写明)。
5. **可观测面登记**:`kg_build_started` 事件的 `total_sources` 恒为 0
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
- **死锁**(T2 质量评 P2 修正为**有条件**表述):尾部
  `mark_unified_kg_dirty_in_tx` 不取 notebooks FK 父行锁,依赖的不变量是
  「每个 notebook 出生即种 `unified_kg_state` 行 → upsert 恒走 DO UPDATE
  分支(FK 不复检)」;该前提一旦失效,本事务变成「持 sources FOR UPDATE
  再等 notebooks KEY SHARE」,与删库 finalize(先 notebooks FOR UPDATE 再
  sources FOR UPDATE)方向相反,是真死锁形。生产路径上前提成立
  (create_notebook 出生种行、0047 后 delete_notebook_kg 不再删该行)。
- **同库 delete×delete 并发**(备查,评审证伪未果):容量锁曾把同库两个
  delete_source 完全串行化,改后在共享 knowledge_objects/embeddings/
  clusters 行上并行相撞;1200 共享对象双线程实测无死锁。残余风险点是
  `_delete_object_id_batch` 的 `= ANY(%s)` 双事务计划不一致时加锁顺序
  反转(同文件历史上咬过一次)——不加锁回去,后续动这块时明写这一对。
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
   JSON)与 `id_element_rows`。分页键逐条点名(复评 P2-1/P2-2):
   - objects/chunks:`ordinal`(全局 UNIQUE,单列 keyset 安全;登记:无
     (notebook_id, ordinal) 复合索引,keyset 在全局唯一索引上带 notebook
     过滤——单租户生产可接受,多库共存残余成本如实登记);
   - relations 腿:`id COLLATE "C"` keyset(既有 ORDER BY 同键);
   - clusters:现存索引是 `idx_clusters_nb_canonical_member_gen`(0051 已
     DROP 旧名并 INCLUDE(generation)),键 (canonical_id, member);**每一页
     都必须带 published 代次谓词**——掉谓词既丢 IOS 又踩「版本身份只数
     published 代」红线;
   - `notebook_object_evidence_rows` 现无 ORDER BY,分页要新引入 id 序:
     消费侧 `membership_object_ids` 经 sorted() 后使用,行序无依赖(这是
     论证,不是巧合——写进实现注释);`id_element_rows` 同理按 id keyset。
   **如实声明**:分页界住的是 fetchall 缓冲与快照时长,Python 侧图结构的
   峰值驻留不变(那是 WR-9 矩阵/图派生的固有形态,本项不动)。
   oracle:小库两实现产出逐位对比(排序后)。

   **实施偏离(PR-B 双内评整改后,以代码为准)**——三处与本节原文不同:
   - `id_element_rows` 按 **`ordinal`** 分页,不是原文写的 id。该 ORDER BY
     是被消费的(`_elem_chunks_scoped` 明写两条路径同为 chunk 插入序),
     换 id 会改动未回填库上 KG 溯源的首见 chunk,属检索结果变化,踩红线。
   - `notebook_object_evidence_rows` **双模**,不是整条改分页。该函数在
     `_ent_chunk_map` 上,是每次 KG 版本变动后首次 graph 模式提问的在线
     读;真机实测新引入的 `ORDER BY id` 让它 +31%(102k objects 多库
     136.6ms→178.3ms),单页库还落盘 external merge sort。因此在线入口
     逐字保留旧无序 fetchall,分页只走独立入口
     `notebook_object_evidence_rows_paged`,由 `graph_rows` 的 gather 经
     `ent_chunk_map(paged=True)` 这条链取用;两个入口产出同一 dict,共用
     同一条 version-cache 记录。
   - clusters 的 published 代次**循环前求值一次**,再作为绑定参数施加在
     每一页,而不是把 `COALESCE((SELECT cluster_generation ...), 0)` 标量
     子查询留在谓词里逐页重求值。后者在 READ COMMITTED 下,若翻代提交
     落在两页之间,同一 member 会跨页挂上两个 hub(单代内被
     `uq_clusters_nb_type_member_generation` 构造性排除的状态),撕裂图还会
     带上新代的版本身份。「每页都带谓词」的要求不变,变的只是谓词的值
     来自一次求值。
2. **在线 standalone viz 生成补大库闸**(取代 v1 的「scale build viz 阶段
   加闸」——那是方向反了:scale build 的 viz 阶段是大库 viz 的**指定
   生产者**,闸它=把物化逼回 API 进程)。`_spawn_viz_build` 有**两个**
   调用点(复评 P1-2),裁决:超过 `viz_sync_build_max_objects` 时**两处
   都闸**——
   - 无产物分支:该分支因上游 `<=` 分流,count 构造性恒超限——加判据
     等于**删掉 API 进程内的大库懒 viz 生产者**,如实这么写;
   - stale 刷新分支:同一条 `build_viz` → 整图物化,不闸等于洞没堵。
     代价如实登记:超限库聚类重建后**继续供 stale 折叠图**直到下一次
     scale build(手动或 maybe_auto_index——后者不是无条件);
     `knowledge_lifecycle.py` rebuild 尾部那段「lazily off the rebuild
     thread」委托注释**同 diff 改写**。

   **实施偏离(PR-B 双内评整改后,以代码为准)**——两处:
   - `maybe_auto_index` 的前置是**四道**,不是原文写的
     「auto_enabled+copyable 两道」,而且 copyable 那道是**反的**:
     `scale_index_auto_enabled` + 未在建/未排队/未 pending + **NOT**
     `copyable`(该函数在库**是** copyable 即「小」时早退)+ 状态 ∈
     {unindexed, suggested, stale},外加**每进程每库一次**的 once-set。
     那道 once-set 让登记的恢复路径在常态下是空的:唯一的 re-arm 点是
     kg_mutation 的 dirty choke point,而 rebuild 刻意不走它
     (`finish_rebuild_state` 必须不动 `kg_mutation_seq`)。两种普通形状因此
     在尾部直接 return:①**常态**——每次入库、每次走全量回退的检索都会调
     `maybe_auto_index`,等到让那次判定作废的 rebuild 跑起来时 set 早已合上;
     ②同进程的第二次**强制**重建(recluster / `rebuild_only` / `--fresh`;
     非强制的那次根本到不了尾部,`cluster_input_version` 的 skip 门先短路)。
     整改:rebuild 尾部**大库跳过分支**在调 `maybe_auto_index`
     之前调 `rearm_auto_index`——它只允许**重新评估**,四道前置一道不减。
     只挂在大库分支上:小库刚在上一行同步建完 viz,没有可恢复的东西,不该
     每次重建都付 `notebook_copy_stats` 的 5 个 COUNT。
   - stale 产物**进热缓存**,键是 `disk_signature`。闸掉刷新之前,stale 是
     几分钟内收敛的过渡态;闸掉之后它是无限期态,而 `_viz_manifest_fresh`
     只认新鲜产物,于是超限库每次请求都重跑一遍 `load_viz`(三个百万级
     list + CSR 邻接)。签名键让两种「换代」都自然失效:scale build 走
     `.tmp`+rename(新 inode),同代替换(离线 CLI `import`)也改
     mtime/size/inode。无签名(适配器没探针/这一次 stat 失败)不写槽位。
   降级路径必须诚实且有前端任务(复评 P1-1,「既有缺失文案」不存在):
   - `unified_graph` 大库分支写死的 `"viz_building": True` 同 diff 改为
     按「是否真的 spawn 了」返回;
   - 图谱画布补第四态:大库无 viz 且未在构建 → 新文案「库规模较大,
     图谱预览将在下一次索引构建后可用」(替代误导的「没有匹配的节点」);
   - `use-kg-graph` 的 locating_unavailable 文案去掉「完成后请重试」的
     无限期承诺,改为指向索引构建;
   - product-and-api 双语登记该产品可见变化。
3. **ANN 构建分页喂入**(机制按复评 P2-3 精确化):`np.asarray` 是别名,
   免掉的是**加载侧**那一份整矩阵(hnswlib 内部副本是第二份、不可免)。
   chunk/relation 腿:`vector_pages` keyset 页直喂 `add_items`,分页要保住
   `build_matrix` 的五条语义(runtime_dim 截断/首个有效行定维/异维行
   丢弃/逐行 L2 归一/ids 与行序对齐),`init_index(max_elements)` 在总数
   未知时先 COUNT 取上界。KG 腿:索引建全后矩阵已不在内存,查询集须
   **第二遍分页读 DB**——页切的是查询集不是索引,每行的 top-k 在其页内
   已完整,「合并」只是按行主序拼接喂给既有 np.unique 首见去重;第二遍的
   行号与第一遍的 hnsw label 是两个空间,**必须按 id 映射回第一遍 label**
   (自环排除与无向对键 a*n+b 都建立在同一空间上),vector_pages 容忍的
   跨页漂移导致两遍不一致时按 id 交集裁决(漂移行丢弃,fail-safe 方向=
   少几条同义边)。验收:top-k 集合断言 + 测试 num_threads=1;
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
2. 维护站点超时放宽按**站点分类**:事务内站点用既有
   `set_config('statement_timeout', …, true)` 先例;非事务站点
   (CONCURRENTLY 类)用 session 级 `set_config(…, false)` 先例。**本项的
   实物交付就是普查清单本身**(复评 P2-6):列出仍按默认 30s 跑的维护
   站点,逐个按类补,清单进 PR 描述;既有 savepoint 先例
   (knowledge_store 的 QueryCanceled 识别站点)不在范围、不被全局
   handler 取代。

## T-W4-5(SR-1 遗留)element 搜索腿 OR→跨表等价 UNION——已实施后被测量证伪,撤销

**终局(2026-09-04):实现已按本节落地(commit 539826aea,oracle 四场景
逐字等价 + EXPLAIN 钉计划全绿),随后质量评审用真机差分测量证伪了本节的
设计前提,实现整体 revert。SR-1 遗留项按「测量后不做」结案。**

证伪证据(PG 16.15 本机,30k sources/600k elements,索引按 0003/0016/
0039/0042/0048 复刻,VACUUM ANALYZE 后 7 次取中位;SQLite 用仓库自己的
SQLiteRepository 迁移建库,20k/400k):

| 场景 | 旧 OR | 新 UNION | 倍数 |
| --- | --- | --- | --- |
| PG:命中 5% 源标题、正文无 | 0.3 ms | 48.6 ms | **179x 退化** |
| PG:命中 95% 源标题、正文无 | 0.1 ms | 45.2 ms | **870x 退化** |
| PG:仅正文命中 20 行 | 56.5 ms | 71.9 ms | 1.27x 退化 |
| PG:零命中/稀疏 title/尾部 title | ~50-61 ms | ~44-53 ms | 0.86-0.90x(边际) |
| SQLite:命中源标题、正文无 | 0.06-0.08 ms | 114-117 ms | **1409-1997x 退化** |
| SQLite:仅正文命中 | 0.10 ms | 120 ms | **1173x 退化** |

机制(修正本节原前提):旧形态在 `ORDER BY se.ordinal LIMIT cap` 下走
`uq_source_elements_ordinal` 的 Index Scan、三臂 OR 压成 Join Filter,
**凑满 cap 即早停**——title 臂不是「钉死计划的元凶」,恰是喂早停最密的
那一臂。拆腿后,腿 A 在「title 命中、正文不命中」的常见词上必须走完该
notebook 的全部 element 自证为空;`source_elements` 无 notebook_id 列、
`se.text` 无 trgm,腿 A **没有任何索引退路**——任何「两腿各取前 cap」的
形状都要付这笔自证为空的钱,微调救不了。0048 迁移头的 REGISTERED
TRADE-OFF 早写过同一机制(IN 半连接必须物化整个命中集,在那里代价是
23.62→33.13ms;这里因无索引可退放大成三个数量级)。SQLite 侧连仅正文
命中都退化(丢 LIMIT 早停 + `USE TEMP B-TREE FOR ORDER BY` 两遍扫)。

留给未来的口径:若要重试,前置是 T-0 生产只读测量(关键变量是每源
element 数与命中在 ordinal 序中的位置,不是源总数),且方案必须保住
「单次有序走查 + 多臂 filter 早停」的形态,而不是拆腿。评审过程中的
有效副产物(与撤销无关、仍然成立):`LOWER(title) LIKE` ≡ `title ILIKE`
在 COLLATE "C" 下零语义分歧(160×120 随机 Unicode 差分为 0);needle
上游 `.lower()` 属实。

以下为撤销前的原设计,仅存档:

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
- 腿 B 要吃到 0048 的 partial 索引,除 `LOWER(s.title) LIKE %s` 外还必须
  在本腿内联 `s.notebook_id=%s` 与可见性谓词字面量(partial 蕴含在通用
  计划下成立靠内联字面量,0048 迁移头有整段论证;复评 P2-5)。
- **SQLite 侧**(复评 P1-3 修正):`source_elements` 在 SQLite **没有
  ordinal 列**——PG 的 ordinal 对应隐式 rowid(migrations.py 3394-3406 的
  既有成文裁决,含「rowid 尾列索引消不掉 ORDER BY」的实测)。裁决:
  sqlite 两腿 + `ORDER BY se.rowid` LIMIT;代价如实登记——今天 sqlite 侧
  无序、撞 LIMIT 即停,改后每次搜索对命中集排一次序(确定性化不是零
  成本)。oracle 逐字对比只在 PG 侧跑,sqlite 侧跑集合等价 + rowid 有序
  断言。

## 跨项(评审 P2-14/15)

- 文档:T1(build-status total 语义 + kg_build_started 字段)、T3-2
  (超大库 viz 降级)、T4(503 结构化出口)进 product-and-api 双语;
  README/AGENTS 无用户面新入口不动;fangan_done 收官时一并补。
- 崩溃-恢复:T1 见上;T2 批间无(单事务不变,只是少一把锁);T3 分页读
  无写;T4/T5 只读改写。回滚:每项独立 revert,无 schema 迁移。
- 明确不做:T1 按批 extend;T2 legacy evidence 分支切页与 memory 重解析
  分支;T3 Python 图结构峰值;T5 se.text trgm;全局超时分档。
