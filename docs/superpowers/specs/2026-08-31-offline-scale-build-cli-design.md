# W-CLI：离线 scale build CLI（设计与实施规格）

热路径修复计划批 2 的 W-CLI 项（批 3 之前安全操作 9M 库检索索引的前提工具）。
三条红线不变：不降检索性能、不降 KG 抽取性能、不改问答结果质量。

## 目标与非目标

目标：一个可与**运行中的服务并存**的独立进程构建通道——同库取数、复用
`.tmp`+原子 swap、per-notebook 跨进程单飞、服务进程按既有逐请求探测自动
换代（无需重启）；支持在另一台大内存机器构建后把工件拷回（rename 原子
落位）。动机数据：49 万对象库曾把 64GB 机器内存打爆（2026-07-02 事故档），
scale index 常驻 ~5GB、构建峰值曾达 40GB。

非目标：不改构建算法本身；不做跨进程通知通道（换代靠既有逐请求探测）；
不动 maintenance CLI（停服 + 全局锁）的既有语义。

## 摸底结论中承重的四条（评审请核）

1. `batch_ingest.py index` 已是离线构建入口，但它经
   `open_maintenance_cli_repository`（停服确认 + **数据库级全局** advisory
   lock）——语义是「服务已停的维护」。W-CLI 的全部意义在**在线伴随**，
   不能复用该组合根；需要新的轻组合根（仓库组装、无停服闸、无全局锁）。
2. `repository_facade.build_scale_index` 绕过 `_admit_scale_op`——**当前
   没有任何 per-notebook 互斥**（进程内 CLI-vs-调度器、跨进程皆无）；两个
   构建者会争 `.tmp`、后到者顶替先到者。这是 W-CLI 顺带要堵的现存缺口。
3. 换代读取：`ScaleArtifactCatalog.load()` 逐请求探测（version_signal 单行
   SELECT + manifest `(mtime_ns,size,ino)` 签名），外部 rename 产生新 inode
   自然被感知——**新数据触发的重建无需任何服务侧改动即生效**。盲区：
   「数据不变、产物变化」（修复/换 HNSW 参数重建）时 version 值不变，
   exact 与 allow_stale 两路都按值判等命中旧对象，重启前不生效。
4. 工件设计上可移植（manifest 无机器绑定、npy/npz 平台无关、hnswlib .bin
   是数据不是机器码），唯一风险是**两台机器的 hnswlib/numpy/scipy 版本
   一致性**——当前零校验零记录。source-partition companion 与主索引是
   世代绑定（operations.md:829），必须同批产出同批拷回。

## 任务拆分

### T-W1 per-notebook 跨进程锁 + 服务侧准入探测

- 新原语（形态：`offline_maintenance_lock` 的 session 级
  `pg_try_advisory_lock` + 专用非池化连接 + finally unlock 结构，配
  `cluster_lock.py` 的命名空间 key 风格）：
  `scale_build_lock(notebook_id)`，key =
  `f"silicon-notebook:scale-build:{notebook_id}"` → `hashtextextended(k,0)`。
  持有横跨整个构建（分钟~几十分钟），**不得**用事务级锁。SQLite 部署
  no-op 兜底（判 backend scheme，照 maintenance_cli 的判别方式）。落点在
  repositories 层（postgres 侧实现 + 端口/组合按既有分层惯例）。
- 服务侧 `_admit_scale_op`：启动构建线程前对该 notebook 做一次**非阻塞
  探测式获取**；拿不到（外部 CLI 在建）→ 按既有 QUEUED/退避语义处理
  （不新增状态面；复用 `_scale_pending`/idle 队列的既有归宿，探测失败视作
  暂不可建）。服务进程拿到锁后持有至构建结束（同一专用连接）。
- `repository_facade.build_scale_index`（batch_ingest index 也走它）同样
  收进锁——现存的「三条路径无互斥」缺口一并闭合。
- 测试：双进程模拟（两个连接各自 try lock）互斥；服务准入在锁被占时
  QUEUED 不阻塞线程；锁持有者异常退出（连接断）锁自动释放（session 级
  语义,断连即释——测试钉住）；SQLite no-op。

### T-W2 CLI 本体 `scripts/build_scale_index.py`

argparse 骨架照 `build_hotpath_indexes.py`（`--database-url-env` 默认
DATABASE_URL、URL 永不打印、退出码 0/1/2、内容无关 JSON 收据）。子命令：

- `inspect --notebook <id>`（只读，默认）：manifest 摘要（version/计数/
  built_at/total_build_ms/库版本字段）、磁盘工件清单与大小、与 DB 当前
  version_signal 的比对（是否已陈旧）、锁占用状态。
- `build --notebook <id> [--full|--fold] [--statement-timeout-seconds N]`：
  新轻组合根组装仓库（不停服闸、不占全局锁），取 per-notebook 锁，构建
  连接放宽 statement_timeout（默认给个大值,operations 惯例 86400,只影响
  本 CLI 连接），走既有 `build/fold` → `.tmp`+swap。进度经既有 on_stage
  回调打印分段计时。companion（source-partition）随
  `SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED` 与在线行为一致同批产出。
- `export --notebook <id> --to <dir>` / `import --notebook <id> --from <dir>`：
  异机通道。export 把 live 工件目录（主索引 + companion + viz,若在）打包
  复制到目标目录（只读源,不动 live）；import 在目标机取 per-notebook 锁 →
  校验（manifest 可解析、计数与文件齐全、庫版本字段与本机装载库比对,
  不符**警告**不拒绝——见 T-W3）→ `.tmp` 复制 → 原子 swap（复用
  prepare/swap 原语）。companion 与主索引强制同批（缺一方拒绝,
  operations.md:829 的世代绑定）。
- 运行位置：脚本自带 `sys.path.insert(ROOT/backend)`（照 hotpath 脚本），
  `_ROOT_DIR` 锚定已与 CWD 无关——文档写明「storage_dir 归属于脚本所在
  checkout 的根」，异机部署提醒各机各锚。
- 测试：CLI 冒烟（本地一次性 PG + 小库构建全链路）；import 的原子性
  （swap 失败回滚、半拷贝不落 live）；export/import 往返后 inspect 一致；
  锁被占时 build/import 退出码非 0 且给指引。

### T-W3 版本字段与「产物变化」换代盲区

- manifest 新增可选键 `library_versions`（hnswlib/numpy/scipy 的
  `__version__`,构建时写入）。`load_scale_index` 的「缺键放行」校验模型
  下安全新增;加载/import 时与本机版本比对,不符发 warning 事件（不拒绝——
  hnswlib 未承诺跨版本兼容但同版本必然兼容,警告给运维决策）。
- 换代盲区收窄：catalog 的判等在 version 值相同处**再比一层磁盘签名**
  （`(mtime_ns,size,ino)`,签名机制已有）——值同而签名变 → 视为新代重载。
  两条路径（exact 内存缓存命中、allow_stale 判定）都补。语义论证写进
  docstring：签名变化只可能来自 swap（写路径唯一）,重载是保守方向,
  不影响任何现有等价面;正常运行中签名不变,零额外 stat 开销增量
  （allow_stale 路径本就每次 stat）——exact 路径新增一次 stat,量级与
  version_signal 的单行 SELECT 同级,如实登记。
- 测试：同 version 换产物（模拟修复性重建拷回）→ 下一次 load 拿到新
  实例（改前会拿旧——变异锚点）;签名未变不重载（计数器）。

### T-W4 文档与运维

- `docs/operations.md`/`_zh.md` 新段「离线/异机 scale 构建」：何时用
  （大库首建/重建、内存受限的生产机）、同机在线用法、异机三步
  （build→export/scp→import）、**两机库版本 pin 要求**、statement_timeout
  说明、与 batch_ingest index（停服维护通道）的分工表。
- `scripts/README.md` 增条目；`deployment` 文档若有 scale 相关段核对。
- fangan 收官时再记。

## 明确不做

- 跨进程通知/缓存 bust 通道（逐请求探测已够,盲区由 T-W3 签名层解决）。
- 构建算法/内存优化（另有 2026-07-02 计划管辖）。
- maintenance CLI 语义变化;batch_ingest index 保留原样（停服通道）,仅
  经 facade 顺带获得 per-notebook 锁。

## 门与流程

T-W1 → T-W2 → T-W3 可部分并行（W3 的 catalog 改动独立文件）；每任务
实现（T-W1/T-W3 判断力要求高用 opus 实现,T-W2/T-W4 impl-task/sonnet）→
spec-review + code-quality-review（opus）→ 汇成一个 PR → check.sh + PG
lane + codex 闭环 → 合入。
