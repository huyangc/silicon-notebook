# PostgreSQL 影子同步、切换与 SQLite 退役设计

> 日期：2026-07-22
> 状态：用户已确认设计，实施计划已编写
> 适用项目：`silicon-notebook`

## 1. 决策摘要

`silicon-notebook` 将以单一权威源、分阶段单向复制的方式从 SQLite 迁移到
PostgreSQL：

1. **正向影子期**：SQLite 是唯一业务读写权威，PostgreSQL 只接收复制并承担影子校验；
2. **切换只读期**：冻结所有业务写入，追平并完成最终校验，再把正式 repository 切到 PostgreSQL；
3. **反向观察期**：PostgreSQL 是唯一业务读写权威，SQLite 只接收临时反向复制，作为可追平的安全副本；
4. **退役期**：稳定门槛全部通过后停止反向复制，冻结最后一份 SQLite 快照，并在独立变更中删除 SQLite 正式运行路径。

任一时刻只允许一个数据库接受业务写入。系统不做双主，不做并行双向复制，也不在
application service 或领域 store 中散落双写逻辑。

初次 PostgreSQL 切换只替换关系数据库。embeddings 先以等价 `BYTEA` 形式迁移，现有
应用侧向量矩阵、hnswlib 和 scale/viz 文件工件暂时保留；pgvector 切换和多 uvicorn
worker 分别作为后续独立项目，不能与数据库切换叠加发布。

## 2. 背景与目标

当前 `SqliteDatabase.write()` 使用进程内 `threading.RLock` 串行化写入，SQLite WAL
允许并发读，但跨进程仍只有一个数据库写者。`BEGIN IMMEDIATE` 路径为了保证跨进程
compare-and-write 原子性，会提前取得 SQLite 写锁。后端、摄取进程和离线维护工具同时
写库时，进程内锁无法协调它们，最终仍可能等待 `busy_timeout` 或报
`database is locked`。

当前 repository 上层已经有 consumer-specific ports，但组合根仍直接构造
`SqliteDatabase`，正式 SQL、迁移、FTS、JSON、隐式 `rowid` 顺序和事务实现仍在
`backend/app/repositories/sqlite/`。因此本迁移不是修改一个连接串，而是新增完整的
PostgreSQL adapter、在线同步与可验证切换能力。

### 2.1 目标

- 在不中断正常 SQLite 业务的前提下建立 PostgreSQL 全量基线并持续追平；
- 在切换前以后台校验证明普通表、领域读取、embedding 和检索结果满足一致性门槛；
- 最终切换只需要一个短暂的全局只读窗口；
- PostgreSQL 开放写入后，观察期内仍能无数据丢失地切回 SQLite；
- PostgreSQL 稳定后可整体删除 shadow 模块和 SQLite 正式运行代码；
- PostgreSQL 消除 SQLite 文件级单写者限制，同时保留现有业务原子性与并发冲突语义。

### 2.2 非目标

- 不实现 SQLite/PostgreSQL 双主或冲突合并；
- 不在首次切换中引入 pgvector、改变融合分或 ANN 召回语义；
- 不在首次切换中把 uvicorn 从单 worker 扩为多 worker；
- 不把原始来源文件或 scale/viz 文件工件搬进 PostgreSQL；
- 不要求影子库在同步中的每一瞬间呈现与源库相同的中间事务状态；
- 不长期维护两个生产数据库实现。SQLite adapter 是迁移期兼容路径，最终必须退役。

## 3. 代码边界

迁移代码必须可以整体启用、整体停用和整体删除。目标目录边界如下：

```text
backend/app/repositories/postgres/       # PostgreSQL 正式持久化实现
backend/app/migration/shadow/            # 临时影子同步与切换模块
backend/app/repositories/factory.py      # 唯一 repository 选择点
scripts/migrate_sqlite_to_postgres.py    # shadowctl 的薄 CLI 入口
```

### 3.1 依赖规则

- API、application services、领域服务和前端只依赖现有 repository ports；
- `repositories/postgres/` 不 import `repositories/sqlite/`；
- `repositories/sqlite/` 不 import `repositories/postgres/`；
- 只有 `migration/shadow/` 可以同时依赖两个数据库 adapter；
- 只有 `repositories/factory.py` 根据 `DATABASE_URL` 选择正式 repository；
- 普通 store 不感知 shadow phase，不执行第二数据库写入；
- shadow 配置和 PostgreSQL 方言判断不得进入 application service；
- 架构测试递归扫描上述非法 import、方言分支和直接双写。

`backend/app/migration/shadow/` 内进一步按单一职责拆分：

```text
manifest.py        # 表分类、主键、复制顺序、字段规范化规则
capture.py         # SQLite/PG change-log DDL 与 trigger 生成
snapshot.py        # SQLite 一致性 backup 与初始水位
bulk_copy.py       # FK 顺序全量 COPY、断点和校验
replicator.py      # 顺序读取、行 hydration、幂等 apply、checkpoint
verifier.py        # 行数、PK、分块哈希、领域读取与检索对照
control.py         # 阶段状态机、不变量和写入闸
cli.py             # status/preflight/start/freeze/cutover/rollback/retire
```

顶层脚本只解析参数、创建依赖并调用 `cli.py`，不承载迁移策略。

### 3.2 PostgreSQL adapter

第一版使用同步 psycopg 3 和显式连接池，保持当前同步 repository 心智模型；FastAPI
继续通过 threadpool 调用同步持久化端口。PostgreSQL adapter 必须完整实现切换所需的
现有 ports，而不是用 facade 内的 dialect `if` 复用 SQLite SQL。

PostgreSQL schema 采用以下兼容优先策略：

- 现有字符串 ID 保持字符串主键，不在迁移时改 UUID 语义；
- 时间字段使用 `timestamptz`，row mapper 统一转回现有领域层接受的 offset-aware ISO 值；
- 明确由应用拥有的结构化 JSON 使用 `jsonb`，校验时使用规范化 JSON 而非原始字节；
- Markdown、来源文本、公式和用户内容保持文本；
- embeddings 第一阶段使用 `bytea`，复用当前 float32 编解码与运行时维度守卫；
- SQLite FTS5 虚拟表不迁移；PostgreSQL 以 `pg_trgm` 为混合中英文候选召回基础，保留现有应用侧融合与排序语义；
- 所有依赖 SQLite `rowid` 的稳定顺序改为显式、可迁移的序号或已验证的稳定排序键；
- `INSERT OR REPLACE` 必须逐处改成语义等价的 `ON CONFLICT DO UPDATE`，不得用删除再插入破坏外键和触发器语义；
- compare-and-write 使用条件 `UPDATE`、`SELECT ... FOR UPDATE`、事务级 advisory lock 或按用例选择的 `SERIALIZABLE`，不保留全局 Python 写锁。

PostgreSQL 使用独立的 schema migration version table。SQLite 与 PostgreSQL migration
在影子期必须由一个 schema compatibility manifest 配对；任一侧版本不受支持时同步器
拒绝运行。

## 4. 数据分类与复制 manifest

`manifest.py` 是复制范围的唯一真相源。每张条目至少声明：

- 表名和分类；
- 单主键或复合主键字段及规范化顺序；
- 初始 COPY 的依赖顺序；
- INSERT/UPDATE/DELETE 是否捕获；
- SQLite 行到 PostgreSQL 行的字段转换器；
- PostgreSQL 行到 SQLite 行的观察期反向转换器；
- 哈希校验时的规范化规则；
- 是否包含 BLOB/embedding；
- 是否为可在目标库重建的派生结构。

分类固定为：

1. **replicated**：所有正式运行会读取的普通 SQLite 表，包括 identity、notebook、sharing、source、element、chunk、embedding、knowledge、governance、Ask 状态、report、Memory、knowhow 和持久化 job/state；
2. **rebuilt**：SQLite FTS5 虚拟表、SQLite index、PostgreSQL index、数据库统计信息等数据库专属派生结构；
3. **shared-filesystem**：原始来源、上传资产及 scale/viz 工件，只校验引用和文件存在性，不进入 change log；
4. **shadow-internal**：change log、checkpoint、控制和差异报告表，永远排除在业务复制之外。

CI 将实际 schema 的所有普通用户表与 manifest 做集合对比。新增正式表而未明确归类时测试
失败，避免静默漏同步。

## 5. 变更捕获协议

### 5.1 SQLite 正向日志

SQLite 新增以下 shadow 内部表；实现时占用当时下一个可用 `SqliteMigrator` 版本
（当前基线为 v23，因此若无并行 schema 变更则为 v24）：

```text
shadow_change_log
  seq INTEGER PRIMARY KEY AUTOINCREMENT
  run_id TEXT NOT NULL
  table_name TEXT NOT NULL
  pk_json TEXT NOT NULL
  operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete'))
  schema_epoch INTEGER NOT NULL
  captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

shadow_capture_control
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1)
  enabled INTEGER NOT NULL
  write_frozen INTEGER NOT NULL
  apply_active INTEGER NOT NULL DEFAULT 0
  run_id TEXT NOT NULL
  schema_epoch INTEGER NOT NULL
```

每个 replicated 表由 manifest 生成 AFTER INSERT、AFTER UPDATE、AFTER DELETE trigger：

- INSERT 记录 `upsert` 和 NEW 主键；
- UPDATE 主键不变时记录 NEW 主键 `upsert`；
- UPDATE 改变主键时依次记录 OLD 主键 `delete`、NEW 主键 `upsert`；
- DELETE 记录 OLD 主键 `delete`；
- trigger 只在 `shadow_capture_control.enabled=1` 时追加日志；
- shadow 内部表没有 capture trigger；
- 写入被冻结且 `apply_active=0` 时，replicated 表的业务写 trigger 使用
  `RAISE(ABORT, ...)` 拒绝未授权写入。

change log 不存整行镜像，尤其不重复保存 embedding BLOB。业务变更和 trigger 日志由同一
SQLite 事务提交；回滚的业务事务不会留下可见日志。SQLite 单写者保证 `seq` 是可消费的
全序。`captured_at` 只用于保留期清理，不用于排序或一致性判断；同步正确性不依赖不可靠的
墙钟提交时间。

### 5.2 PostgreSQL 反向日志

观察期开始前，PostgreSQL 安装 `shadow_reverse_change_log` 和 capture control。每条事件
具有唯一 `seq bigint identity`、`source_txid`、表/主键/操作以及 nullable `applied_at`。
`seq` 只作为事件身份和诊断顺序，**不能**被当作提交顺序：PostgreSQL sequence 可在事务
回滚时留洞，也可能由较早取号、较晚提交的事务产生“迟到的小序号”。反向复制因此不使用
“连续最大 seq”作为正确性 checkpoint。

单个 reverse worker 持续选择所有已提交且 `applied_at IS NULL` 的事务事件组。它在一个
PostgreSQL `REPEATABLE READ` snapshot 中按 `source_txid` 收集事件和当前行状态，按 manifest
的 FK 拓扑整理 upsert/delete，然后在一个 SQLite `BEGIN IMMEDIATE` 事务内：取得
`apply_active` lease、应用当前状态、为每个源事件写入
`shadow_reverse_applied_events(run_id, source_seq)` 回执、释放 lease 并提交。只有 SQLite
事务提交后，worker 才回到 PostgreSQL 把这些 outbox 行标为 `applied_at`。若在两库提交
之间崩溃，事件仍是未确认状态；重放时 SQLite 回执使业务 apply 幂等，随后补写源端确认。
因此 target 业务变更与“已应用回执”仍在同一 target 事务中，不需要不可靠的跨库事务。

SQLite 正向 capture 在切换冻结窗口中关闭，正向同步器永久停止；因此反向 apply 即使写入
SQLite 普通表也不会形成复制回环。

回滚时顺序相反：先冻结 PostgreSQL 写入，清空反向队列并校验 SQLite，再关闭
PostgreSQL capture；只有 SQLite 重新成为权威后才允许重新启用其 capture。

SQLite 在反向观察期仍保持 `write_frozen=1`。SQLite 同一时刻只有一个写者，因此其他
进程不能在 applier 事务中借用 `apply_active` lease；任何异常都会回滚业务行、事件回执和
lease。这样数据库级冻结仍能阻止意外进程，同时不会挡住受控的反向复制。

## 6. 初始全量基线

初始同步按以下顺序执行：

1. `preflight` 校验源 SQLite 身份、目标 PostgreSQL 身份、schema epoch、扩展、磁盘、连接和空目标约束；
2. 为 SQLite replicated 表安装并验证 capture trigger，开启正向 capture；
3. 使用 SQLite backup API 生成一致性临时快照，正常业务写入继续；
4. 从快照自身读取 `MAX(shadow_change_log.seq)` 作为基线水位 `H0`；
5. 按 manifest FK 拓扑使用流式批次和 PostgreSQL `COPY` 导入普通表；
6. embeddings 流式解码并写入 `bytea`，不在内存中全量展开；
7. 数据装载完成后创建 PostgreSQL secondary/FTS index；
8. 校验全量表计数、PK 集合、外键、抽样内容及 embedding；
9. 把 PostgreSQL forward checkpoint 原子设为 `H0`；
10. 启动增量同步器消费 `H0 + 1` 之后的源日志。

快照携带业务行和同一时点可见的 change log；trigger 的事务原子性保证快照不会出现
“业务行已进入基线但对应已提交日志不可见”或相反的半状态。

全量导入按表保存断点和累计校验值。失败重跑必须先验证目标 run identity；不得把另一个
迁移 run 的半成品表当作断点继续。

## 7. 增量复制与 checkpoint

正向和反向同步共用行转换、hydration、幂等 apply 与 worker lease 原语，但 checkpoint
协议不同；一个 run 同时只能启动一个方向。第一版每个方向都是单消费者，不做分区并发。

处理规则：

- `upsert`：根据 manifest 主键从当前源库读取行；行存在则转换并在目标 UPSERT；若行已被后续事务删除，则按 delete 收敛；
- `delete`：在目标按主键幂等删除；
- SQLite→PostgreSQL 按源 `seq` 保持语句顺序，目标业务变更和
  `shadow_apply_checkpoint.last_seq` 在同一 PostgreSQL 事务提交；其 checkpoint 只能
  连续推进，禁止跳号、跳 poison event 或人工改到未来；
- PostgreSQL→SQLite 按未确认 `source_txid` 事件组工作；当前状态 apply 和逐事件
  `shadow_reverse_applied_events` 回执在同一 SQLite 事务提交，随后才更新 PostgreSQL
  `applied_at`；不能用 `MAX(seq)` 或“最大已见序号”等价替代未确认集合；
- target 提交前崩溃则业务变更与 checkpoint/回执都回滚；target 提交后、source 确认前
  崩溃则重放，连续 checkpoint 或事件回执保证最终幂等。

由于 upsert hydration 读取源库当前行，它可能提前把某个主键的更晚状态应用到影子库。
这是有意的最终状态优化；影子库在追赶过程中不承诺逐事务中间态一致。同步到稳定水位后，
完整 verifier 才能宣布 caught-up。

### 7.1 错误分类

- 网络中断、连接获取失败、deadlock、serialization failure、锁/语句超时等瞬态错误：整个目标事务回滚，指数退避并进行有上限的自动重试；
- 类型转换、约束、未知表/字段、schema epoch、run identity 和规范化失败：记录 poison
  event，停止该方向；正向 checkpoint 保持原值，反向未确认事件保持未确认且不伪造回执；
- poison event 不得被移入死信队列后继续消费。修复必须通过代码/schema 变更、定向回填或从安全 checkpoint 重放完成。

## 8. 一致性校验

`ShadowVerifier` 不依赖单一 `COUNT(*)`。活跃写入期间使用 barrier-aware 校验：

1. 在一个 SQLite 只读 snapshot 中记录校验水位 `Hv` 并流式读取源数据；
2. 等待 PostgreSQL checkpoint 至少达到 `Hv`，再在 PostgreSQL 只读事务中比较；
3. 对校验期间发生过 `seq > Hv` 事件的主键标记为 concurrent，不把它们的临时差异计为 drift；
4. 未发生后续事件的稳定主键必须严格一致；concurrent 主键进入下一轮重试；
5. change log 清理水位不得越过任何仍活跃 verifier 的 `Hv`。

hydration 可能提前把更晚状态写入影子库，但该主键必然有 `seq > Hv` 的后续事件，因此会被
上述屏障识别而不是形成假阳性。周期 verifier 持续累计稳定主键覆盖率；最终切换冻结后，
不存在 concurrent 主键，必须得到一次 100% 全量校验。

caught-up 必须同时满足：

1. source `MAX(seq)` 等于 target applied checkpoint，并持续稳定至少 60 秒；
2. 无 poison event、未完成批次、schema epoch/run identity 差异；
3. replicated 表行数一致；
4. 每张表主键集合一致；
5. 按稳定主键范围流式计算规范化内容哈希；
6. JSON 按排序键、禁止 NaN/Infinity 的规范化形式比较，合法 JSON null 保持 null；
7. BLOB/embedding 比较长度、维度、字节或容许的数值误差、范数与抽样 cosine；
8. PostgreSQL 外键、唯一约束和删除级联检查通过；
9. repository contract 抽样在两端返回相同领域对象；
10. 共享文件引用存在且路径不越出配置 storage root；
11. 固定检索评测集相对 SQLite 基线的 recall@12 下降不超过 1 个百分点，top-10
    overlap 不低于 0.90；使用确定性模型替身的 golden answer，其 citation/source id
    集合必须 100% 一致。

FTS5、`pg_trgm` 和未来 pgvector 的底层分数不要求逐位相等。校验目标是候选覆盖、稳定
领域排序、答案引用身份和召回质量，不把数据库私有 rank 当作跨后端协议。

差异报告必须包含 run、表、主键/分块、差异类别和脱敏摘要，不输出来源全文、Memory
内容、token 或密码。修复通过可审计的定向回填或 checkpoint 重放执行，禁止直接手改
影子库后把差异标绿。

## 9. 切换流程

### 9.1 切换前门槛

- PostgreSQL repository contract 与集成测试全绿；
- 至少两次独立完整 verifier 运行无差异；
- 正向复制持续追平且无 poison event；
- PostgreSQL 备份与恢复演练成功；
- 连接池、权限、TLS/网络边界、磁盘和监控已配置；
- 切换前 SQLite 快照和 PostgreSQL 备份路径已验证；
- rollback 演练在非生产数据上通过。

### 9.2 全局只读切换

`shadowctl freeze` 是一个受保护的运维动作，必须同时覆盖：

- HTTP 写路由；
- MCP 写工具；
- 后台 ingestion、embedding、KG、projection、Ask/report 落库任务；
- 离线 batch/maintenance CLI；
- 绕开应用直接写 SQLite 的意外进程（由 SQLite write-frozen trigger 拒绝）。

具体顺序：

1. 发布维护/只读状态，停止接收新写任务；
2. 等待已登记的在途数据库事务、上传和文件删除完成；
3. 设置 SQLite durable write gate，记录最终水位 `Hfinal`；
4. 清空正向队列到 `Hfinal`；
5. 运行最终全表、领域和检索校验；
6. 保存最终 SQLite 权威快照与 PostgreSQL 切换前备份；
7. 停止正向 replicator，关闭 SQLite capture；
8. 停止后端进程，通过下述原子配置交换把正式 `DATABASE_URL` 切到 PostgreSQL，再启动
   后端；PostgreSQL durable write gate 仍保持关闭；
9. 运行登录、notebook、sharing、source、Ask、Knowledge、Memory、Knowhow、report、删除和文件引用 smoke；
10. smoke 失败则在未产生 PostgreSQL 新业务写入的情况下直接切回 SQLite；
11. smoke 成功后开启 PostgreSQL reverse capture，启动并确认 reverse worker 心跳，再通过
    `resume-writes` 解除只读、允许 PostgreSQL 业务写入。

维护期间前端显示明确的只读/维护状态，不把用户写操作表现为普通 500 或无限重试。

### 9.3 正式 backend 的原子切换契约

为了让“当前正式库是谁”只有一个清晰开关，同时不把数据库密码写入 shadow 控制表，迁移期
配置固定为：

```dotenv
# 正式 API/repository 当前唯一使用的连接串
DATABASE_URL=sqlite:///./.local/silicon_notebook.db
# shadow worker 使用的另一端；正式业务 factory 不读取它
SHADOW_DATABASE_URL=postgresql://user:password@host:5432/silicon_notebook
```

`repositories/factory.py` 始终只根据 `DATABASE_URL` 构造正式 repository；只有
`migration/shadow/` 读取 `SHADOW_DATABASE_URL`。本地 `.env`/单机部署的 `cutover` 和
`rollback` 不要求人工复制连接串，而是把这两个键的完整值进行原子交换：

1. 解析并验证两个 URL 的 scheme、数据库 identity 和期望的当前权威端；
2. 在同目录写入保持原文件权限的新临时文件并 `fsync`；
3. 用 `os.replace` 原子替换 `.env`，并对父目录 `fsync`；
4. 保存权限受限、带 run id 的一次性回退副本；
5. 日志、终端、审计事件和差异报告只显示 scheme/host/database 的脱敏身份，永不显示密码。

交换配置只能在 durable write gate 已关闭且后端进程已停止时执行。CLI 校验配置文件中的
`DATABASE_URL` 仍与本次 run 的预期当前端一致；若管理员已手改、文件 hash 不一致或后端
仍持有写租约，命令 fail closed。后端重启时 factory 根据新的 `DATABASE_URL` 构造唯一正式
adapter，并再次校验 shadow phase 与正式 backend 匹配；不匹配则 readiness 保持维护状态，
不开放写入。

Kubernetes、systemd `EnvironmentFile` 外的 secret manager 等不能由 CLI 原子改写的部署，
使用同一状态机但由部署编排原子更新 `DATABASE_URL`/`SHADOW_DATABASE_URL` 两个 secret
引用。`cutover --activation-manifest PATH` 生成不含凭据的、带 run id/期望 scheme/配置 hash
的 activation manifest；`confirm-activation` 在新进程启动后验证实际连接身份。未确认前写闸
保持关闭。CLI 不在终端输出可供 `eval` 的含密连接串。

因此正常切换和回滚都具备同一明确的停点：

```text
冻结写入 -> 追平并终检 -> 停后端/复制 worker -> 原子交换 active/shadow 配置
-> 启动新正式后端（仍只读）-> 只读 smoke -> 启动反向复制并确认健康 -> 开放写入
```

在 `resume-writes` 之前失败都可交换配置直接退回，且 PostgreSQL 尚未接收业务写；在开放
PostgreSQL 写入之后则必须执行第 10 节的追平式 rollback，不能只改连接串。

## 10. PostgreSQL 权威观察期与回滚

观察期内：

- PostgreSQL 是唯一正式业务读写库；
- PostgreSQL reverse outbox 以同事务 trigger 捕获变更；
- 反向 replicator 幂等同步到不对外服务的 SQLite；
- SQLite 只承担差异校验和紧急回退，不允许独立写入；
- 原始文件继续使用同一共享 storage root，新上传和删除由正式业务服务执行一次，SQLite 只复制引用行；
- uvicorn 保持一个 worker，避免把进程内缓存/取消/单飞状态问题混入数据库迁移。

观察期发生阻断级故障时：

1. 冻结 PostgreSQL 新写入和后台任务；
2. 冻结后等待所有 PostgreSQL 权威事务结束，记录最终 reverse 事件集合，并处理到
   `applied_at IS NULL` 为零且稳定至少 60 秒；
3. 对 SQLite 安全副本运行完整 verifier；
4. 停止 reverse replicator 和 PostgreSQL capture；
5. 停止后端，通过 `rollback` 原子交换 active/shadow 配置，把正式 repository 切回
   SQLite，再启动仍为只读的 SQLite 后端；
6. 运行只读 smoke，成功后通过 `resume-writes` 恢复 SQLite 写入；
7. 为下一次迁移创建新 run，不复用已终止 run 的 checkpoint。

## 11. 最终退役门槛

观察期至少连续 28 天，且不能只依赖运行天数。以下门槛必须全部通过；任一门槛失败会重新
开始连续观察计时：

- reverse replication 无 poison event、无未解释差异；
- 在目标并发下，repository DB-only p95 不高于 `max(SQLite 基线×1.20, 50ms)`，p99
  不高于 `max(SQLite 基线×1.30, 100ms)`；连接池等待 p99 小于 50ms，连续 24 小时无
  pool acquisition timeout、未处理 deadlock 或 serialization failure；按观察期实测
  增长外推 90 天后 PostgreSQL 数据盘使用率仍低于 70%；
- 所有关键 API、MCP、后台任务、复制/分享、删除级联和并发冲突路径已在 PostgreSQL 实际运行；
- PostgreSQL backup/restore 演练成功；
- 至少一次 PostgreSQL schema migration 演练成功；
- 至少一次完整 PostgreSQL→SQLite 回滚演练成功；
- 运维方明确批准 PostgreSQL 成为不可逆的唯一真相源。

通过后，`shadowctl retire` 停止 reverse capture，冻结最终 SQLite 快照和审计报告。后续
独立变更整体删除：

- `backend/app/migration/shadow/`；
- `backend/app/repositories/sqlite/`；
- `SqliteDatabase`、`SqliteMigrator` 和 SQLite runtime wiring；
- SQLite 方言和正式运行依赖；主测试矩阵不再把 SQLite 当作受支持 backend；
- repository factory 的双后端选择逻辑，最终只构造 PostgreSQL repository。

可保留一个不被正式服务 import 的离线 SQLite→PostgreSQL 历史导入工具，用于旧备份恢复。
退役 shadow 模块前，必须把该工具所需的最小 reader、历史 fixture 和测试移入独立
`backend/app/tools/sqlite_import/`；正式应用和 PostgreSQL repository 不得 import 它，
它也不得让 SQLite 重新成为受支持的生产 backend。

hnswlib/scale 向量代码只有在后续 pgvector 迁移通过召回门槛后才能删除，不能因 SQLite
repository 退役而误删仍在使用的检索实现。

## 12. 运维接口与状态机

shadow phase 是单一枚举，不使用可组合布尔开关：

```text
off
sqlite_to_postgres
cutover_readonly
postgres_to_sqlite
retired
```

迁移 run 的控制状态存放在 PostgreSQL shadow control 表；SQLite 只保存本地 capture
和 write gate 所需的最小控制行。业务运行不依赖 PostgreSQL shadow control 可用性：
正向影子期即使 PostgreSQL 故障，SQLite 正式业务仍可继续，change log 累积等待恢复。

唯一运维 CLI 提供：

```text
status
preflight
start-forward
verify
worker
backup
freeze
cutover
confirm-activation
smoke
start-reverse
resume-writes
rollback
retire
```

每次状态转换都验证允许的前态、数据库身份、run id、schema epoch、正向 checkpoint/反向
未确认事件与回执、poison event、备份与写入闸。CLI 不提供 `--force-skip-event`、任意修改
checkpoint 或伪造反向回执的快捷参数。

需要监控的最小指标：

- 正向 source max seq/applied seq；反向未确认事件/事务数、最老未确认事件年龄和估算 lag；
- 每批行数、字节数、耗时和重试次数；
- poison event 数及最早阻断 seq；
- verifier 最近状态、差异表/分块数；
- PostgreSQL pool used/wait、事务时长、deadlock、lock wait、statement timeout；
- SQLite change-log 行数和文件增长；
- reverse safety copy 的未确认事件数、最老未确认年龄和回执覆盖。

change log 不在 apply 成功后立即删除。默认至少保留 7 天且至少保留最近 100,000 条事件。
SQLite 正向日志只允许清理同时满足以下条件的行：

```text
seq <= 已完整验证的 checkpoint
captured_at < 当前时间 - 7 天
seq < 当前 max(seq) - 100,000
seq < 最老的活跃 verifier barrier
seq < 最老的回滚/重放保留 checkpoint
```

清理任务不触发 capture，且不得删除 poison event 所在水位及其后的审计材料。
PostgreSQL 反向 outbox 另加硬条件 `applied_at IS NOT NULL`，且对应 SQLite 事件回执已包含在
最近一次完整 verifier/回滚安全点中；`applied_at IS NULL` 的事件无论序号、年龄或尾部数量
都不得清理。反向保留逻辑不把 `MAX(seq)` 当作已完成证明。

## 13. 测试策略

### 13.1 Repository 与方言

- 同一套 repository conformance tests 分别运行 SQLite 与 PostgreSQL；
- PostgreSQL adapter 覆盖事务、条件写、冲突、级联、分页、稳定排序和 JSON 语义；
- 冻结的旧 SQLite fixture 先升级到当前版本，再迁入 PostgreSQL，验证历史数据兼容；
- SQLite FTS5 与 PostgreSQL `pg_trgm` 使用固定中英文评测集做候选/排序对照；
- embeddings `BYTEA` 路径验证存储维度、运行时截断、缓存版本和召回不变。

### 13.2 Capture 与复制

- manifest 中每张 replicated 表执行 INSERT、UPDATE、PK UPDATE、DELETE 和适用的级联删除；
- 业务事务回滚后 change log 不得留下可见事件；
- 从目标 apply 到 checkpoint/逐事件回执提交、再到反向源端 `applied_at` 确认之间逐点
  注入崩溃，验证回滚或幂等重放；
- 反向 capture 覆盖 sequence 留洞、事务回滚及“小序号较晚提交”，证明不会因最大水位
  前进而漏事件；
- 连续 checkpoint、run identity、schema epoch 和 poison fail-stop 均有负向测试；
- 大 BLOB/embedding 批量写保持内存有界，change log 不复制整行 payload；
- 正向和反向同时启动必须被状态机拒绝；
- 反向 apply 不得重新产生正向复制回环。

### 13.3 切换 E2E

- 完整运行 snapshot→bulk copy→forward catch-up→verify；
- freeze 能阻止 HTTP、MCP、后台任务、CLI 和直接 SQLite 写；
- PostgreSQL 只读 smoke 失败时可在零新增业务写下切回；
- PostgreSQL 开放写后 reverse safety copy 追平；
- 完整 rollback 保留所有 PostgreSQL 观察期写入；
- retire 前置门槛缺一项时必须拒绝退役。

### 13.4 CI 分层

`scripts/check.sh` 继续保持离线、无需 PostgreSQL 服务，覆盖 SQLite 当前回归和纯单元测试。
新增独立 PostgreSQL integration check，在 CI 临时 PostgreSQL 服务上运行 adapter、shadow
和 E2E 测试。正式 cutover 之前，该 integration check 必须成为受保护的必过检查。

## 14. 实施顺序

本设计应拆成可独立验收的实施阶段：

1. PostgreSQL database/adapter/schema 与双后端 repository conformance；
2. manifest、SQLite capture、snapshot 和全量 COPY；
3. 正向 replicator、checkpoint、verifier 与监控；
4. durable write gate、维护 UI、cutover smoke 和 factory 切换；
5. PostgreSQL reverse capture、反向复制和 rollback 演练；
6. 生产影子期、切换和观察期；
7. SQLite/shadow 退役；
8. 独立的 pgvector 与多 worker 项目。

每一阶段都必须更新 `README.md`、`README_zh.md` 和 `AGENTS.md` 中受影响的设置、架构、
运维和约束。若实施完成了 `silicon_notebook_fangan.md` 中已定义的功能，还必须同步
`fangan_done.md`；纯迁移基础设施不得被错误描述为新的用户功能。

## 15. 验收标准

- 双跑期间正式业务始终只有一个写入权威；
- 所有双库代码只存在于 factory、PostgreSQL adapter 和可删除的 shadow 模块；
- SQLite 业务写与正向日志同事务，PostgreSQL 业务写与反向日志同事务；
- target apply 与正向 checkpoint 或反向逐事件回执同事务，崩溃重试不丢失、不重复最终状态；
- schema/table manifest 无遗漏，未知 schema fail closed；
- 切换前完整 verifier 通过，切换只需短暂只读窗口；
- PostgreSQL 开放写后 SQLite 安全副本能够持续追平并完成无损回滚；
- PostgreSQL 稳定门槛通过后，可在一个独立变更中整体删除 shadow 和 SQLite 正式运行路径；
- 首次数据库切换不改变 pgvector、检索融合或多 worker 行为。
