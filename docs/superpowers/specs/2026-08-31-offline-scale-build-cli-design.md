# W-CLI：离线 scale build CLI（设计与实施规格 v2，经 opus 设计评审修订）

热路径修复计划批 2 的 W-CLI 项（批 3 之前安全操作 9M 库检索索引的前提工具）。
三条红线不变：不降检索性能、不降 KG 抽取性能、不改问答结果质量。

## 目标与非目标

目标：一个可与**运行中的服务并存**的独立进程构建通道——同库取数、`.tmp`+
原子 swap、per-notebook 跨进程单飞、服务进程按既有逐请求探测自动换代
（无需重启）；支持异机构建后工件拷回（rename 原子落位）。动机：49 万对象
库曾把 64GB 机器打爆（2026-07-02 事故档），scale index 常驻 ~5GB。

非目标：不改构建算法；不做跨进程通知通道；不动 maintenance CLI（停服 +
全局锁）语义；不引入 flock 类新机制（SQLite 部署由 CLI 拒绝闭合，见 T-W2）。

## 摸底结论（v2 精确化，评审已逐条核实）

1. `batch_ingest index` 经 `open_maintenance_cli_repository`：停服闸 +
   **数据库级全局** advisory lock（固定 key 0x53494C49434F4E）——是「服务
   已停」通道，W-CLI 不能复用。
2. 互斥现状的精确表述：`build()` 全程不碰 `building_lock`/`building`
   （facade `build_scale_index` 也绕过 `_admit_scale_op`）；`fold()` 有
   进程内 `building` 认领但**看不见** build；跨进程三条路径皆无互斥；
   `prepare_fold_directory` 会 rmtree 别人正在写的 `.tmp`。
3. 换代读取逐请求探测（version_signal + manifest 磁盘签名），外部 rename
   产生新 inode 自然感知；盲区=「数据不变、产物变化」时两条路径按
   version 值判等命中旧对象（T-W3 解决）。
4. 工件可移植（manifest 无机器绑定、npy/npz 平台无关、hnswlib .bin 是
   数据）；风险=两机 hnswlib/numpy/scipy 版本（requirements 只有下界
   `>=`，漂移是常态）**与代码/迁移账本版本**。
5. **评审最重发现**：组装仓库的 bundle `_initialize` 会 (a) 跑迁移
   （bundle.py:68——异机新 checkout 对在役库执行未预期 DDL），(b) **无
   条件 UPDATE user-local 的 admin 凭据**（bundle.py:101-106，salt 每次
   随机——异机 .env 缺 ADMIN_PASSWORD 时把生产密码静默改成默认值）。
   停服闸此前兜住了这两条；在线伴随进程必须显式关闭它们。

## 任务拆分

### T-W1 per-notebook 跨进程锁 + 服务侧准入探测（opus 实现）

**锁原语**（落点 repositories 层，postgres/sqlite 两适配器各一份实现，
服务层不嗅 scheme）：
- 模板照 `database.py:400-430` 的 `table_projection_lock`（**不是**
  offline_maintenance_lock）：专用非池化会话 + **双参**
  `pg_try_advisory_lock(namespace, key)`（namespace 取固定 int32 常量，
  key = notebook_id 哈希归一 int32；双参让 pg_locks 探查直读两列，规避
  单参 64 位负 key 重组陷阱——评审实测 200 样本 108 负）+ 信号量限制此类
  连接数。SQLite 适配器返回「不支持」哨兵（不是 nullcontext no-op——
  CLI 据此拒绝，见 T-W2；服务进程内互斥由 T-W1 服务侧改动补齐）。
- **会话韧性（阻塞级）**：锁会话上显式 `SET idle_session_timeout = 0`
  （USERSET；托管 PG 常开该参数，会杀 idle 持锁会话=锁静默释放）+
  conninfo keepalives（idle/interval/count）；任何 SET 必须在
  `_restore_session_defaults` 的 `RESET ALL` 之后执行（照
  database.py:418-424 的既有写法与注释）。
- **swap 前复验持锁（阻塞级，比心跳更关键）**：`swap` 是唯一破坏性步骤，
  执行前在锁会话上复验锁仍在手（一次 SELECT 自查双参锁存在）；复验失败
  =构建作废、`.tmp` 留置、响亮报错（绝不 swap）。
- 锁所有权跨线程：contextmanager 跨不了线程——手工 enter/exit，纪律同
  并发票据：「每一个出口要么把锁交给 worker、要么自己释放」（写测试钉）。

**服务侧准入**：
- `_admit_scale_op`（scale_artifact_runtime.py:1095-1315）在拿信号量票据
  **之前**做非阻塞探测式获取（拿到即持有并移交 worker；拿不到→QUEUED
  归宿照 `_scale_pending`/idle 既有语义）。**探测失败的回滚纪律（阻塞级）**：
  若实现上探测只能放在 claim/票据之后，失败路径必须照 :1301-1314 的
  `_start_daemon` 失败分支完整回滚（释放票据、`building.discard`、还原
  队列条目），且**不得**调 `_scale_record_failure`（外部 CLI 建 40 分钟
  不能把该库自动重试退避推到上限）。
- `build()` 与 `fold()` 统一收进 `building` 认领 + 新锁（现存缺口一并
  闭合）；facade `build_scale_index` 因此获得互斥。batch_ingest index
  在停服场景拿不到 per-nb 锁时（顺序恒为全局锁→per-nb 锁、非阻塞 try，
  无死锁环）转成可读 busy 错误 + 明确退出码，说明前序阶段成果已落库。
- **锁 seam 从 `wire_scale_runtime`（repository_runtime.py:1698-1756）
  内部自持的 database/maintenance 取，不新增 facade 参数**——三道零松弛
  机械门（ports 计数 898、facade allowed_names 只缩不长、
  `RepositoryFacade.__init__` 行数 474）任何一道都不许因此动；若端口
  确需新方法，棘轮同 diff 且在 PR 里引用 64d5aa10 先例。

测试：双连接互斥；断连自动释锁；swap 前复验失败不 swap；准入探测失败
完整回滚（队列条目不丢、退避不记——变异钉）；SQLite 哨兵；锁移交纪律。

### T-W2 CLI 本体 `scripts/build_scale_index.py`（T-W1 后，impl-task）

argparse 骨架照 `build_hotpath_indexes.py`（URL 永不打印、退出码 0/1/2、
内容无关收据）。**轻组合根（阻塞级）**：`PostgresPersistenceBundleFactory`
开 `migrate=False, seed=False` 缝（默认 True 保持现状，既有全部调用方
零变化），CLI 组装走该缝；组装前用裸连接读 `silicon_schema_migrations`
账本与本 checkout `len(migrations)` 比对，不一致→退出码 2 + 指引（异机
代码版本必须与在役服务一致）；文档硬性要求用生产 `.env` 运行。
`--statement-timeout-seconds`（默认 86400）**在组装仓库之前**改
`settings.postgres_statement_timeout_seconds`（池的 configure/reset 回调
会 RESET ALL 抹掉借出连接上的 SET——评审点名的假达成陷阱）。

子命令：
- `inspect --notebook <id>`（只读）：manifest 摘要（version/计数/built_at/
  build_ms/library_versions）、工件清单与大小、与 DB version_signal 比对、
  `.tmp`/`.old` 残留及大小、锁占用（探测方式=非阻塞 try + 立即 unlock，
  语义正确且最简）。未知 notebook→退出码 2 + 可读消息（不许 KeyError
  裸奔；status() 会抛 KeyError，包住）。
- `build --notebook <id> [--full|--fold]`：SQLite 后端拒绝（退出码 2 +
  说明单进程部署无跨进程场景）；取 per-nb 锁→既有 build/fold→swap 前
  复验；进度经 on_stage 打印分段计时；companion 随
  SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED 与在线一致。**swap 期间
  屏蔽 SIGINT**；Ctrl-C 清理自己的 `.tmp` 并打印路径。
- `export --notebook <id> --to <dir>`：**也取 per-nb 锁**（评审 3c：swap
  两次 rename 之间的 copytree 会拷出跨代混合集合；companion 在主 swap
  之后才重建，天然有主新伴旧窗口）；打包主索引 + companion（存在时校验
  `parent_version == 主 version`，不符拒绝导出）+ viz（存在时）。
- `import --notebook <id> --from <dir>`：取 per-nb 锁→校验→三根各自
  `.tmp`+rename 原子落位→swap 前复验持锁。校验清单（**前两项拒绝而非
  警告**——红线「不改问答质量」）：
  1. `manifest["pipeline_identity"]` 与当前已发布管线身份一致（不符则
     检索侧会整体丢弃 scale 核静默退化——catalog.py:131-140）；
  2. `manifest["dim"]` 与本机 embed 维一致（不符则 open_ann fail-open
     静默零召回）；
  3. hnswlib 版本严格相等，默认**拒绝**（`--allow-library-mismatch`
     覆盖；.bin 无格式版本头，失配可能被 fail-open 吞成静默零召回）；
     numpy/scipy 不符警告；
  4. manifest 可解析、计数与文件齐全；companion 存在时
     `parent_version == 主 version`（**缺失即放行**——开关关闭时构建的
     包本就无 companion，条件化而非「缺一方拒绝」）。
  三根原子性（评审 3a）：prepare/swap 原语现硬编码 kg_index 一根，
  companion 的原子发布内联在 save_source_partitions、viz 根裸写无
  staging——将 prepare/swap **泛化为按任意 live 目录**的原语（或等价的
  每根实现），import 三根都走 tmp+rename；不许出现「主原子、其余裸拷」。

测试：CLI 冒烟（本地一次性 PG 小库全链路 build→export→import→inspect
一致）；import 原子性（swap 失败回滚、半拷贝不落 live）；四项校验各自
拒绝路径；锁被占退出码非 0；SQLite 拒绝；SIGINT 清理。

### T-W3 版本字段与「产物变化」换代盲区（opus 实现，可与 T-W1 并行）

- manifest 新增可选键 `library_versions`（hnswlib/numpy/scipy），构建时
  写入（builder manifest 组装处）；加载侧对 hnswlib 失配发 warning 事件
  （import 的硬拒在 T-W2 层）。
- catalog 判等在 version 值相同处**再比磁盘签名**：两条路径共用的内存
  命中早退（catalog.py:156-159）是必须补的位置——**成本如实登记（评审
  9）**：新增 stat 落在两条路径的最热共用分支（静态大库每次提问 5-10 次
  load），不是「本就每次 stat」；stat 暖 dentry ~µs 级远低于
  version_signal 的单行 SELECT，验收时**实测**（characterization 计时或
  微基准数字入 PR）。缓存的 ScaleIndex 携带加载时签名（照
  `_ann_load_states` 的 setattr 先例）；与 `_manifest_identity` memo 共用
  同一次 `manifest_stat_signature` 调用，一次 load 不 stat 两遍。
- 测试：同 version 换产物→下一次 load 新实例（变异锚点：去掉签名比对
  红）；签名未变不重载（计数器）；stat 次数=每 load 一次。

### T-W4 文档与运维（impl-task，随 T-W2 收尾）

- `docs/operations.md`/`_zh.md` +「离线/异机 scale 构建」段：同机在线
  用法、异机三步（build→scp→import）、**两机 pin 清单（代码版本/迁移
  账本 + hnswlib/numpy/scipy）**、statement_timeout、连接预算（服务侧
  每并发构建 + CLI 各占一条非池化连接，max_connections 紧的部署注意）、
  PgBouncer 前提声明（transaction pooling 下 session 级 advisory lock
  失效，本通道要求 session pooling 或直连）、`.old` 残留人工恢复
  （`mv {dir}.old {dir}`）、**allow_pickle 来源约束（只 import 自己在
  受控机器构建的工件——npy 反序列化=任意代码执行面）**。
- `docs/development.md`/`_zh.md`（离线索引通道的既有落点）与
  `docs/deployment-and-configuration.md`/`_zh.md` 成对更新；
  `scripts/README.md` 增条目；fangan 收官时记。

## 明确不做

- 跨进程通知/缓存 bust 通道；构建算法/内存优化（2026-07-02 计划管辖）；
- maintenance CLI 语义变化（batch_ingest index 保留停服通道，仅顺带获得
  per-nb 锁与 busy 错误形）；
- flock：SQLite 部署由 CLI 拒绝闭合，不引入平台锁。

## 门与流程

T-W1 与 T-W3 并行（无文件重叠）→ T-W2 → T-W4；每任务双内部评审（opus）
→ 汇成一个 PR → check.sh + PG lane + codex 闭环。零松弛机械门（ports
898、facade allowed_names、`RepositoryFacade.__init__` 474、
ownership_manifest 重生成）在「T-W1 锁 seam」一节已约束，评审按此核。
