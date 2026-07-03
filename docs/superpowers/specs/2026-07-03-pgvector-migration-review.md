# pgvector 迁移:必要性与可行性评审(结合部署实况)

> 日期:2026-07-03 · 状态:评审结论,供 [2026-07-03-pgvector-migration.md](2026-07-03-pgvector-migration.md) 修订与排期决策。
> 方法:两轮多 agent 审计——第一轮 7 路把 spec 收益断言逐条对照当前代码并扣除同周守卫(盲点清单见附录 A);第二轮 5 路定量必要性(写锁碰撞/增长时间线)与可行性(方言普查/向量路线外部证据/运维),叠加部署方四项确认。
> 部署实况(部署方确认):EMBED_DIM=4096(Qwen3-Embedding-8B)、56 万 KG 对象/12.9 万 chunk/112GB 库、16C/64G Ubuntu(可装 PG)、多人写各自库 + admin 写大 base 库、无真 OOM 史。
> ⚠ 时效注记:本文审计基线为 2026-07-03 早间 master;当日稍晚 master 已合入 indexed-only 原则化守卫(详见文中「核销注记」)——三条无守卫路径大部分已收口,相关"立即动作"已核销,不要重复施工。

## TL;DR(要点一屏)

- **不迁能撑 ≈1.5–2× 增长**(KG 对象 84–112 万之间);最先倒的不是查询而是**索引生命周期**:auto-fold 每次摄取 20–34GB 瞬时、全量 build 23–35GB,2× 时单独穿 64G。稳态查询墙远在 4–5×。
- **当下真实的疼只有两类**:①admin 摄取/rebuild 日的阵发全站冻结(0.3–10s 级,根因是 resolve_session 每认证请求拿全局写锁 + 长事务,**4–5 个 SQLite 补丁可消**);②三条今天即可单发引爆的无守卫路径(13GB/30GB/10GB+ 级),共同触发器恰是「维度切换后 ANN dim-mismatch」。**A2 单独不构成迁移理由**。
- **迁移可行**:P1 方言平移 12–17 人日(代码库连接纪律异常干净);4096 维**确认不可直接 HNSW**(pgvector vector≤2000/halfvec≤4000),必须先过 MRL 截断 gate(官方支持 32–4096 自定义维度,证据链完整);运维绿灯带一个未闭环前提(**磁盘 ≥400GB 空闲待确认**)。
- **推荐**:立即执行「止血补丁包 + MRL 截断 spike」(两条路线共用,~1 周);近期走 **R1 续命**(截断 1024 重建旁挂索引,天花板推到 ~6×);**R3 全迁设为条件触发**——库规模预期 ≥2× / rebuild 变日常 / 确需 workers>1,任一信号出现即启动。R2 仅作降级备选。

---

## 一、必要性(不迁,什么时候撞墙)

### 现在就疼的(与规模无关,触发型/阵发型)

| 疼点 | 定量 | 性质 |
|---|---|---|
| **全站冻结放大器**:get_current_user 是 async 依赖却同步调 resolve_session,后者每认证请求在 `_write()` 里做续期 UPDATE(74 个路由挂认证)。任何长写事务持锁期间**整个事件循环卡死**,读/写/SSE 全冻结 | 冻结时长 = 最长单次持锁;长事务实测/估值:FTS 回填 7.1s(部署机估 10–20s)、rebuild Pass A2 估 2–10s、簇表重写 1–3s、delete_notebook_kg 10–60s | 阵发,仅 admin 摄取/rebuild 日;跨进程 CLI 摄取单事务 >30s(busy_timeout)则后端认证端点直接 500 |
| **三条无守卫单发路径**(4096 维):①`_retrieve_scored` else 分支 ≈11.4–13.2GB(kn+elem 双矩阵,进 VectorCache 常驻不退);②reasoning `search_elements` 全库元素向量 Python-list 化 ≈**30GB+**(list 形态 8× 膨胀);③联邦 `_ppr_retrieve` 守卫只查 active 库 copyable,base 索引失效时全量加载 base 56 万对象+9.2GB 矩阵+全图 | 共同触发器:**EMBED_DIM 切换后旧索引 dim-mismatch 静默跳 ANN**、embed 端点故障、索引缺失。federated 下每个用户对自己小库提问都会对 base 跑一遍——多人形态里人人可踩 | 触发型,今天就存在;②只要 agent 选一次 search_elements 即打穿 64G |
| **维护税**:向量生命周期专属代码 ≈1500–1800 行;近一周 288 commit 中 102 个(35%)是该子系统补丁(#147/#158/#171/#174/#175/#178 等 6+ PR);每个新检索面要复刻 ANN+delta+fold+守卫四件套 | 近两周 465 commit 中 ~21–35% 提交流量 | 持续性,人力税 |

> **核销注记(2026-07-03 当日 master)**:上表「三条无守卫单发路径」在本审计出数后、文档落库前已被 master 大部收口——①`_retrieve_scored` else 分支已加 `kg_bruteforce_refused` 守卫(FTS 词法有界兜底,与 relation 冷矩阵守卫同 fail-open 出口);②element 侧已加 `element_scoring_skipped` 守卫(commit 568e5ae,reasoning `search_elements` 与 chunk 兜底层均容错空结果);③graph 路径已有 `graph_walk_refused` 大库守卫——**唯 federated `_ppr_retrieve` 的 base 侧缺口需按最新代码复核**。「共同触发器 = 维度切换后 dim-mismatch 静默跳 ANN」的结论不受影响,仍是 MRL 截断窗口期的头号风险(修订清单第 14 条)。resolve_session 放大器与止血补丁包**未落地,仍有效**。

### 增长 N 倍后疼的

- **≈1.5–2×(最先倒)**:build 峰值 24→47GB(开关系 ANN 则 35→70GB)、fold 每次摄取 20–34→40–68GB,叠加稳态 12–21GB 必穿 64G。**墙以「日常添加文献失败」形态先出现**(auto_fold_on_add 默认开,fold 成本 O(全库) 非 O(delta))。
- **≈2×**:漏网路径单发 13→26GB,任何一条触发即死。
- **≈4–5×**:稳态 RSS 本身穿墙。
- 次级:按条数定、按 1024 维校准的阈值(Tier2 cap 50000、synonym cap 50000)在 4096 下系统性偏松 4 倍,中型无索引库一次上传即 6.5GB。

### 基本不疼的

- **多人写各自 notebook 的日常碰撞**:单次持锁几乎全是亚毫秒短写(单行插入 0.02ms、store_kg 每批 1000 行、向量批 7ms),写吞吐实测 ~48 万行/s,远高于负载。碰撞 = 毫秒级排队。
- **稳态查询内存**(守卫全开+ANN 健康):12–15GB(不开关系 ANN)/18–21GB(开),64G 余量 3–4×。
- **hnsw 构建时长**:今天 3–8min,5× 也才 20–45min,时间不是墙。

### A2 写锁的真实碰撞频率结论

在「多人写不同库 + admin 写大库」形态下:**平日接近零疼**;疼的日子 = admin 摄取/rebuild 日,阵发有界(每源尾部 0.3–2s + rebuild 期约 8 次 2–10s 全站冻结)。且最大痛点(resolve_session)与三大长持锁点全部可用 SQLite 栈内 4–5 个瘦身补丁消掉(续期节流+移出事件循环 / ask 读优先 / Pass A2 锁外计算 / 长事务分块),做完后最长持锁降到几十 ms。**PG 独有的剩余收益只有真并行写吞吐+行级锁公平性,当前写负载用不上**;且 workers=N 的真障碍是进程内缓存不共享(prod.sh 自证),PG 解不了这条。per-notebook 分库替代路线:查询层可行但重构面 ≥ 迁 PG 且零 A1/A3 收益,**性价比死刑,不是替代方案**。

**结论:A2 =「量级或形态再涨才疼」;A1/A3 = 中高必要性,触发线在 1.5–2× 增长或任一次 ANN 失效事件。**

---

## 二、可行性(迁,逐期风险与工作量)

### 4096 维 gate 最终定调(MRL 证据核实)

**证据链完整,gate 判断成立**:①Qwen3-Embedding-8B 官方 model card 明确 MRL 支持、自定义输出 32–4096 维(实证);②「取前 N 维 + re-normalize」是 vLLM/sentence-transformers 官方 MRL 标准实现(强推断,spike 本身即最终验证);③pgvector HNSW 上限 vector≤2000/halfvec≤4000(实证)——**4096 原生不可索引,必须先降维,不存在「不动维度直接迁」的选项**。存量 200 万+ 条向量可本地截断+renorm,零重嵌 API 成本;查询侧改 3 行。

### 逐期

| 期 | 工作量 | 最大风险 | 可回滚性 |
|---|---|---|---|
| **P0 spike**(MRL 截断 recall 对照) | ~1–2 pd(~80 行脚本,45 题 gold × 4 维度档暴力余弦,分钟级/次) | gold 仅 1 nb/45 题,semantic-only 绝对值偏低——只看相对差。判据:2048 档 recall@12 降 ≤1pt 且 top-10 重合 ≥0.9 → halfvec 2048;1024 档降 ≤3pt → vector 1024;降 >5pt → 固定 2048 | 零风险,纯读 |
| **P1 方言平移** | **12–17 pd**(桶① shim 三件套 1.5–2 + 桶② 方言 ~100 处 4.5–6 + 桶③ 语义审改 2–3 + 测试启用 2–3 + 迁移 ETL+彩排 2–3)。481 位点中 78% 纯机械被 shim 吸收;连接纪律异常干净(240 处全 context-manager、零手动 commit/close、零 sqlite 异常捕获、零触发器/日期函数) | ①FTS5 bm25→pg_trgm 分数语义反向+量纲变化(recall_gold 可兜);②ORDER BY rowid 插入序(canonical 确定性,PR#136 已证敏感)→迁移严格保序+新增序数列;③vector TEXT 列混存 str/bytes → BYTEA 化+legacy JSON 重编码;④连接池压测 | **近乎免费**:源 SQLite 原地即快照,改 DATABASE_URL 重启分钟级切回;代价 = 丢切换后增量写入(切换后 1–2 周每晚 pg_dump) |
| **P2 pgvector 向量** | 估 10–15 pd 档(本轮未做专项普查,诚实口径):四表 halfvec 化 + 查询侧 ANN SQL + 删旁挂生命周期 ~1200–1500 行(CSR/PPR 图 ~300 行保留,那是图不是向量) | 建索成本有公开基准背书(1M×1536d 并行 9.5min;本库最大表 55.7 万行,分钟~十几分钟,maintenance_work_mem 8–16GB);**必配 pgvector 0.8+ iterative_scan**(所有查询带 notebook_id 过滤,小 nb 会 overfilter);高删改 nb 留 REINDEX 手册项 | 旁挂索引代码在 P3 前保留,可开关切回 |
| **P3 删 SQLite** | 小 | 回滚不现实(反向导出小时级+需维护 --reverse 工具) | **触发条件量化:PG 稳定运行 ≥4 周 + 备份恢复演练通过**,才准删 |

### 运维面(Ubuntu 单机 16C/64G)

- **装机**:必须 PGDG 源(`apt install postgresql-17 postgresql-17-pgvector` → 0.8.x);**Ubuntu 自带源是坑**(0.5.1 无 halfvec/subvector,静默阻断 P2 两条路线)。直接 PG17,别装 15。
- **内存**:PG shared_buffers 12–16GB + 建索期 maintenance_work_mem 16GB(临时)+ 应用 4 worker×1–3GB(矩阵退役后)≈ 峰值 ~45GB/64GB,成立。
- **磁盘**:PG 落库估 130–170GB,迁移期双份 + WAL 峰值 → **需 ≥300GB 空闲,含备份留存建议 ≥400GB——唯一未闭环硬前提,部署方一句话确认**。
- **停机窗口**:搬运 1–3h + 建索 1–2h + 校验,乐观 2–4h,按半天排,一次周末夜间窗完成,无需在线双写。迁移脚本需:按表水位断点续传 + **先灌数据后建索引** + rowid 保序 COPY。
- **备份/监控净增**:从「拷一个文件」升到「每晚 pg_dump -Fd -j4(1–3h,压缩 <50GB×7 份)+ 连接数/慢查询/磁盘三件套」;单机无副本无 PITR 诉求下研发自运维可承受(建议明确不要求 RPO<24h)。注意备份从此永远是「DB+文件(storage/logs/CSR 旁挂)」两套。

---

## 三、三条路线对比(决策矩阵)

| | **R1 不迁**:SQLite + 截断 1024 + 守卫加固 | **R2 半迁**:PG 关系型 + 向量旁挂保留(跳 P2) | **R3 全迁**:PR#173 全量 |
|---|---|---|---|
| **A1 矩阵/OOM** | ◐ 围栏式:补 3 条守卫 + 截断后单发 13GB→3.3GB、30GB→7.5GB(可活但仍要守) | ✗ 矩阵+守卫体系永续,且 ×N workers | ✓ 根治(ANN 成存储原生属性,守卫类消失) |
| **A2 写锁/workers=N** | ✗(但止血补丁消掉全站冻结,当下痛点归零) | ✓(唯一兑现项) | ✓ |
| **A3 向量生命周期** | ✗ fold/build/stale/版本探针照旧,维护税持续(~1/3 提交流量) | ✗ 全保留,**还要新增跨进程 flock**(现 build/fold 去重锁是进程内 Lock,两 worker 并发互删 tmp 半成品);hnswlib 无 mmap,ANN 内存 ×N(4096 维 @workers=4 ≥45GB 直接不可行,截断后 12–28GB 勉强) | ✓ 删 ~1200–1500 行,插入即最新 |
| **工作量** | ~7–12 pd(截断迁移工具+旁挂索引 1024 重建+3 守卫+止血补丁包+阈值重校准) | P1 12–17 pd + 守卫 + flock + 截断(前置与 P2 大半重叠,**不是省事捷径**) | P0+P1+P2 ≈ 25–35 pd(单人 5–7 周)+ 半天停机窗 |
| **天花板/遗留债** | 截断 1024 后 build/fold 峰值 ÷4,内存天花板推到 ~6×;债 = 四件套维护税、每个新检索面复刻、阈值随维度重校准 | 天花板同 R1(截断后),债 = R1 全部 + 双存储运维 | 债 = FTS 语义差异观察期、PG 运维三件套 |
| **可回滚** | 天然 | P2 前免费 | P2 前免费;P3 后不现实 |

### 推荐路线与触发条件

**立即执行(本周,无论最终迁否,两路线共用)**:
1. **止血补丁包**(~3–5 pd):resolve_session 续期节流+移出事件循环;ask `_ensure_conversation` 读优先;Pass A2 读+seed 计算挪锁外;长事务分块(仿 copy P1-4 模式)。→ 全站冻结消失,A2 当下痛点归零。
2. **MRL 截断 recall spike**(~1–2 pd):定维度档,是 R1/R2/R3 共同的第一步。
3. **向部署方要三个数**(收紧全部估算):`SELECT COUNT(*) FROM relation_embeddings/element_embeddings WHERE notebook_id=<大库>`;磁盘空闲量;库月增速。

**近期(spike 通过后,~1–2 周)**:走 **R1** ——存量向量截断 1024 + 旁挂索引重建(三守卫已由当日 master 落地,见核销注记;仅剩 federated base 侧缺口复核)。天花板从 1.5–2× 推到 ~6×,买下从容决策窗。

**R3 启动信号(任一出现即排期,目标在 2× 到来前完成)**:
- 库规模实际或预期 ≥2×(KG 对象逼近 85 万,按月增速换算提前 2 个月动手);
- rebuild/批量重摄取变每日常态,或多 admin 并行摄取;
- 确需 workers>1(注意需同时付进程内缓存外部化成本);
- 守卫/fold/delta 类补丁流量持续占 1/3 且团队判定不可接受。

**R2 仅作降级备选**,触发条件:MRL 截断 spike 失败(2048 也显著掉点)或 P2 窗口排不开而写并发告急。

---

## 四、对 PR#173 的处置建议

**总处置:spec 保留、按下述清单修订后降级为「条件触发的正式路线」;不立即开工 P1,先执行止血包+spike。** 修订清单(合并上轮 11 条盲点,只列条目):

1. §0 收益叙事:A2 重定性(痛 = 长事务冻结非写并发不足,SQLite 补丁可消)、A1/A3 补维护税定量(35% 提交流量)与 1.5–2× 天花板依据,作为主理由。
2. §0 新增「迁移前置止血包」章节:4–5 个 SQLite 补丁与迁移解耦、先行落地。
3. §1 新增 R1/R2 路线对比与 R2 降级备选定位(含触发条件、R2 的 flock/×N 隐藏成本)。
4. §5.5 索引维定调:2048 halfvec 首选 / 1024 vector 备选,spike 判据写死(recall@12 降 ≤1pt/≤3pt + top-10 重合率)。
5. P1 工作量口径:12–17 pd + shim 三件套策略(qmark 转换/Row 兼容行工厂/连接池)写入实施方案;上线前对 455 个 SQL 串跑引号内 `?` 静态扫描。
6. P1 风险清单:FTS5 bm25→pg_trgm 分数语义反向、ORDER BY rowid 保序(canonical 确定性)+新增序数列、vector TEXT→BYTEA 重编码 legacy JSON 行、连接池压测。
7. §6.1 迁移脚本:补断点水位表 + 先灌数据后建索引 + rowid 保序 COPY。
8. §6.2:postgres:15 → **PG17 via PGDG**,docker-compose 改 apt 原生;写死「禁 Ubuntu 自带源(0.5.1 无 halfvec/subvector)」。
9. §8:pgvector 0.8+ iterative_scan 列为必配项(notebook_id 过滤 overfilter);建索基准 spike 降优先级(公开证据已够)。
10. §6.3/P3:触发条件量化为「PG 稳定 ≥4 周 + 备份恢复演练通过」;明确「回滚=丢切换后增量」预案与切换后 1–2 周每晚 pg_dump。
11. 新增硬前提章节:磁盘 ≥400GB 空闲待部署方确认;relation/element_embeddings 行数、库增速三个待确认数。
12. 测试策略:默认 SQLite 快跑 + PG 全量 nightly/手动闸;真正要手改的测试方言位点 <30 处。
13. 落点清单:README「no database server」宣称、prod.sh --workers 硬编码、backend.sh、requirements.txt 加 psycopg[binary]+pgvector;迁移 CLI 用法同 PR 进双语 README。
14. 维度切换风险专节:EMBED_DIM 变更 → dim-mismatch 静默退全量物化,是迁移窗口期最大触发器;窗口期临时禁 `_retrieve_scored` else 分支或强制 FTS 降级。
15. 上轮 11 条盲点逐条并入对应章节(不重复展开)。

**待确认(阻塞项,部署方各一句话)**:磁盘空闲 ≥400GB?relation_embeddings/element_embeddings 大库行数?库月增速(换算 2× 到达时间)?
---

## 附录 A:第一轮审计的 11 条 spec 盲点(修订清单第 15 条所指;含核销状态)

1. **【前提级】「生产 EMBED_DIM=4096」当时全仓零佐证**(config 默认 1024、.env.example 明写 1024、07-02 审计矩阵 4–6GB 合计)——**已核销:部署方 2026-07-03 确认 4096 属实**,此条从「待核实」降为「已核实,补录佐证到 spec」。
2. **未扣除同周守卫**:#171/#174/#175/#147/#132/#158/#152 + evidence 反查表(knowledge_object_sources,07-02 已存在)——§9 事故清单把已修痛点重复计入迁移收益。
3. **伪收益**:`typeof 全表扫`仅存在于一次性离线 BLOB 迁移工具(batch_ingest),热路径 typeof=0——从事故清单删除。
4. **方向反了**:`json_extract` 是 PR#152 为避免全 payload json.loads 引入的**廉价路径**,不是待消灭的成本项。
5. **归错 API**:真正产 GB 级 dict 的是 `query_sims`;`top_k_sims` 是避免全量 dict 的 argpartition 优化版(docstring 明写)。
6. **「0.019s」无 repo 实据**:排除 Neo4j 的定量核心论据在 backend/docs/tests 全仓零命中(仅个人 memory)——需 CSR-PPR benchmark 落库(百万节点+真实 splice+max_iter=100)。
7. **残留 OOM 暴露面漏定位**(`_retrieve_scored` else 分支)——**已核销:当日 master 已加 kg_bruteforce_refused 守卫**(见正文核销注记)。
8. **「长事务全站阻塞」描述部分过时**:copy 已分批(P1-4)、rebuild 重计算在 scratch 表锁外——但本轮新确认的 resolve_session 放大器是真痛点,动机章应以它重写。
9. **workers=N 清单漏项**:kg/scheduler.py 模块级 `_job_pool`/`_window_pool`;`_scale_ver_lock` 兼守 CSR 图件探针(图件保留则单飞不能全退)。
10. **重复计账**:G4(版本探针)能退役的部分本就依附 G1 要退役的向量矩阵;§5.2 rustworkx→CSR 是独立于 pgvector 的应用层重构,挂在迁移下会夸大迁移必要性。
11. **工作量偏乐观**:未区分机械替换与语义替换(rowid 序契约/executescript/IN 数组化);漏计 16 处 `PRAGMA table_info`(PG 下须改 information_schema)——本轮已以 12–17 pd 分桶普查口径取代。
