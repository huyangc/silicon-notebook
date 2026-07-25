# scripts/ 使用说明

仓库运维 / 开发 / 评测脚本。**除非特别说明,都在仓库根目录运行**,Python 用装好
`backend/requirements.txt` 依赖的解释器(激活对应环境,或用 `PYTHON_BIN=...` 覆盖脚本默认)。

---

## 一、服务启停(最常用)

### `backend.sh` —— 只启停后端 + 健康自检
```bash
scripts/backend.sh status     # 看 :8000 现在跑的是什么 + notebook 数
scripts/backend.sh start      # 后台启动后端到 :8000(日志落 .local/logs/backend.log)
scripts/backend.sh stop       # 停掉 :8000 上的服务
scripts/backend.sh restart    # 停当前 + 启 silicon-notebook
```
**何时用:**
- **"重启后 notebook 全没了 / 一直 404"** —— 多半是 :8000 被**别的服务**(如 `EDA Agent`,它有 `/v1/chat/completions` 但没有 `/api/notebooks`)占了。先 `scripts/backend.sh status`:若显示不是 silicon-notebook,`scripts/backend.sh restart` 一键换回。**数据不会丢**——notebook 都在 `.local/silicon_notebook.db`,这只是"端口上跑错了服务"。
- 改了 `.env`(模型 / `CHUNK_MMR_K` / DB 等)需要让后端重新加载 → `restart`(后端**没带 `--reload`**,改 config/代码必须重启才生效)。

**关键:** DB / storage / `.env` 的相对路径已在代码层锚定到**仓库根**(见 `backend/app/core/config.py` 的 `_ROOT_DIR`),从哪个目录启动 uvicorn 都指向同一套 `仓库根/.local` 与根 `.env`——后端启动日志首行会打印解析后的绝对路径,可一眼核对。脚本仍从 `backend/` 目录启动只是为了模块导入(`app.main`)。注意:多 worktree 时各 worktree 锚各自的根(`.local` 互相独立)。生产启动用仓库根的 `npm run start`（`scripts/prod.sh`：前端 build+start + 后端固定 `--workers 1`）；模型调度容量位于单个后端进程内，禁止以多 worker 乘大 TOML 声明的容量。

大库默认在 `/api/ready` 前加载全部已发布 scale 索引、启用的 ANN handle 与可安全复用的单索引 PPR core；不会启动时全量复制跨库 mounted 组合图，以免千万节点图成倍常驻导致 OOM。`backend.sh start` 会打印 `warming` / `preloading_indexes` 的进度，默认最多等 1,800 秒，避免原 40 秒窗口误杀正常的大索引加载；极慢磁盘用 `START_TIMEOUT_SECONDS=3600 scripts/backend.sh start` 覆盖。若显示 `error`，脚本会立即清理本次进程并提示看日志。索引数不得超过 `SCALE_IDX_CACHE_MAX`，否则无法保证“全部加载后仍常驻”。

系统模型服务由维护人员统一配置：

```bash
cp model-services.example.toml .local/model-services.toml
vi .local/model-services.toml   # 服务、workload 绑定、每服务 max_concurrency
vi .env                         # MODEL_SERVICES_CONFIG + api_key_env 引用的密钥
scripts/backend.sh restart
```

`max_concurrency` 是每个物理模型服务唯一的并发容量；批处理 `--workers`、KG 来源任务数和本地 CPU/ANN 线程都不会覆盖它。普通用户的「模型服务」面板只读，页面加载不会自动探测；admin 可在面板显式测试单个或全部服务。用户遇到模型错误时应提交界面中的 support id，维护人员据此关联 `.local/logs/` 与只读服务状态，定位具体故障服务。修改 TOML 或密钥后必须重启后端；配置路径留空是明确离线模式，非空但无效会启动失败。

旧部署仍使用逐角色模型变量时，先运行迁移助手：

```bash
python scripts/migrate_legacy_model_env.py --env .env          # 只预览
python scripts/migrate_legacy_model_env.py --env .env --apply  # 备份后写入
```

脚本从旧值生成 `.local/model-services.toml`，把密钥迁移到新的 `.env` 槽位，并移除已废弃的模型/并发字段；不会在 TOML 或终端中泄露密钥，当前 `.env` 与含密钥备份都会收紧为 `0600`。推算出的 `max_concurrency` 只是初始值，应按真实服务容量复核；可用可重复的 `--max-concurrency ROLE=N` 覆盖。安装流程生成且未改动的示例 TOML 可直接替换；其他已有配置只有显式 `--force` 才会替换，且都会先备份。

环境变量:`PYTHON_BIN` `HOST`(默认 127.0.0.1) `PORT`(默认 8000) `LOG_FILE` `START_TIMEOUT_SECONDS`(默认 1800)。
例:换端口 `PORT=8001 scripts/backend.sh start`。

### `dev.sh` —— 前后端一起跑(前台开发)
```bash
scripts/dev.sh                # 同时起 backend(:8000)+ frontend(:3000),Ctrl+C 一起停
```
全栈本地开发用。前台运行、看实时日志、退出即清理两个进程。需先在 `frontend/` 跑过 `npm install`。
(只需要后端、或要后台常驻 + 明确 stop/status,用 `backend.sh`。)

### `check.sh` —— 本地全量自检(提交/PR 前)
```bash
PYTHON_BIN=/path/to/python bash scripts/check.sh
```
contracts + 后端测试/离线 smoke + 前端测试/tsc/build 三条 lane 并行执行。脚本会强制 `MODEL_SERVICES_CONFIG=""`，不读取开发者真实密钥，也不会访问付费/网络模型服务；EXIT=0 即过。

### `migrate_sqlite_to_postgres.py` —— SQLite 存量迁移到 PostgreSQL

默认只预检；目标必须是空的 PostgreSQL 16 UTF-8 数据库，URL 从环境变量读取而不出现在 CLI 参数：

```bash
export POSTGRES_MIGRATION_URL='postgresql://USER:PASSWORD@HOST:5432/EMPTY_DB'
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/.local/silicon_notebook.db
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/.local/silicon_notebook.db --apply
```

导入按表提交并记录 checkpoint(run 头绑定 sealed snapshot hash):中途失败(崩溃/远程连接断开/重启)后重跑同一条命令即从最后完成的表**续跑**,不必整体重来;显式传 `--snapshot` 复用本工具生成的 sealed snapshot 可省去重新快照数 GB 源库(重新检查目录、文件名/hash、`quick_check`、schema 版本和 WAL/SHM sidecar,且 hash 必须匹配该 run,不接受任意 SQLite 文件或异源 checkpoint)。大库可传会话级批量装载调优:`--maintenance-work-mem 2GB`、`--max-parallel-index-workers N`(加速建索引)、`--batch-rows`(默认 1000);详见 `docs/operations_zh.md`「大库的调优与前置条件」。在线运行只得到某一时刻的一致演练快照,不会同步后续写入;正式切换的停写、URL 修改和回滚步骤见 `docs/operations_zh.md`。脚本只迁 DB 行,不复制 `.local/storage`,也不支持 MySQL。

停掉全部 writer 和后端后，可让同一个 CLI 在重新核对 SQLite 快照和 PostgreSQL 全表 checksum
后原子激活本地 `.env`：

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/.local/silicon_notebook.db \
  --work-dir /absolute/path/postgres-migration \
  --activation-receipt /absolute/path/postgres-migration/migration-TIMESTAMP.receipt.json \
  --activate-env /absolute/path/.env \
  --confirm-service-stopped
```

未来的最终迁移也可把 `--apply`、`--activate-env`、`--confirm-service-stopped` 放在同一条命令。
大目标上可加 `--fast-activation`：只跳过激活阶段第二遍 PostgreSQL 全表 checksum(导入已逐表校验并落
checkpoint),源库重新快照锚点与 schema/清单校验仍执行。CLI 原子替换配置并保存权限受限的回退副本,
但不会自行停止或重启服务。

---

## 二、检索 / chunk 运维

### `build_chunks.py` —— 为现有 notebook 回填 chunk + 向量
```bash
PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>
```
chunk-native 检索的 chunk 是摄取时自动建的;**老 notebook**(chunk-native 上线前导入的)需用本脚本补建 chunk 表 + chunk_embeddings,之后默认 chunk 模式问答才有内容。幂等(重跑覆盖该 notebook 的 chunk)。

### `backfill_kg_embeddings.py` —— 补全 notebook 的 KG 对象向量
```bash
PYTHONPATH=backend python scripts/backfill_kg_embeddings.py <notebook_id>
```
KG 对象向量在 `store_kg` 入库时嵌入;并发过高被限流漏掉的,用本脚本低并发补齐。

---

## 三、生产 DFX 诊断

### 卡顿发生时的首选命令

生产目标是 Ubuntu 24.04，在仓库根通过 `npm run start` 启动前端与单 Uvicorn worker。
不要先 restart/stop；请在卡顿**正在发生时** SSH 到主机采集：

```bash
ssh <production-host>
cd <silicon-notebook-repository>
python3 scripts/diag.py incident
```

若输出的 `Missing/degraded evidence` 表明 PID 自动发现 missing/ambiguous/incomplete，
从服务管理器或监听信息取得仍在运行的后端 PID 后重试：

```bash
python3 scripts/diag.py incident --pid <backend-pid>
```

默认 stdout 是一段最多 **32 KiB** 的 UTF-8 文本，可整体复制。所有采集共享最长 10 秒
deadline，DB 部分最多使用其中一秒。后端每两秒原子刷新
`.local/diagnostics/runtime.json`；超过六秒即按 stale 处理，不用其活跃工作字段下高置信
结论。`SIGUSR1` 只触发不终止进程的全线程 Python 栈 dump，不含 locals，后端继续运行。
采集使用 `.local/diagnostics/incident.lock`，线程栈追加到有 8 MiB retention 上限的
`.local/diagnostics/thread-dumps.log`；只读 DB 临时快照位于
`.local/diagnostics/db-snapshots/`。运行时只接受当前用户拥有的 `0700` diagnostics 目录与
同用户拥有、单硬链接、普通文件类型的 `0600` heartbeat/dump 文件；不安全的已有工件或目录路径
替换只会让诊断降级，不会跟随链接、阻塞于特殊文件或截断敌对目标。

报告最多给出三个按证据强度排序的假设。`high` / `medium` / `low` 是置信标签，不是根因
宣判；先看 `Confidence-ranked diagnoses`，再核对 `Observations`、`Relevant stacks`、
`Database and host signals` 与 `Log metadata`。`Missing/degraded evidence` 会明确列出 stale
snapshot、PID/权限/信号问题、DB busy/locked/deadline、日志 malformed/corrupt 或竞态；该来源
会被排除，其余证据仍保留。空闲服务没有有效多信号结论是正常结果，应在卡顿时重跑。

`incident` 纯 stdlib、不 import app、不需要 root 或第三方包，不发送终止信号、不重启、
不执行 maintenance 或自动修复。所有诊断对业务数据只读：不执行 delete/写库、
checkpoint/vacuum/analyze/reindex/migration；只允许维护上述有界 `.local/diagnostics/` 工件。
可复制报告只挑选元数据：notebook/request/job 引用分配本报告内假名，其它原始不透明 id 省略；绝不包含原始 id/用户文件名、request body、
来源/Ask/prompt/模型消息/Memory/Knowhow 正文、SQL 文本或参数、authorization/cookie/token/secret、
原始命令行或局部变量。脱敏输出发给可信团队之外的人之前仍须人工复核。
新增 notebook API 路径时必须同步登记运行时诊断的精确安全路径形状；未登记的深层路径只会降级为
`/api/notebooks/{id}/{redacted}`，不得为了保留可读路径而放宽到回显原始不透明 id。

### `diag.py` 七命令矩阵

| 命令 | 用途 | 边界 / 委托 |
|---|---|---|
| `python3 scripts/diag.py incident` | 卡顿现场的首选有界采集；必要时加 `--pid <backend-pid>`，删除分析也可显式加 `--notebook <id>`。 | Ubuntu/Linux 活体证据；纯 stdlib、app-free → `diag_incident.py`。 |
| `python3 scripts/diag.py slow --since 24 --deep` | 历史慢因：请求/事件/LLM 延迟、规模画像、reasoning/PPR 与 scale-index 审计；`--deep` 增加可能耗时数分钟的只读 DB 检查。裸 `python3 scripts/diag.py` 仍等于 `slow`。 | 离线、纯 stdlib、app-free → `diag_slow.py`。 |
| `python3 scripts/diag.py latency --last 500` | `ask_stage` 的逐阶段 P50/P95/max。 | 离线、纯 stdlib、app-free；口径与 `app/eval/ask_latency.py` 一致。 |
| `python3 scripts/diag.py locks --top 20` | 按调用点汇总 SQLite 写锁的 wait/hold 分布。 | 离线、纯 stdlib、app-free；读取 `db_write_lock_slow` / `db_write_lock_stats` 事件。 |
| `python3 scripts/diag.py open --local .local` | 打开笔记本的查询/端点延迟、计数缓存冷成本、pending 子查询与 mutation-sequence churn。 | 离线、纯 stdlib、app-free → `diag_open_latency.py`。 |
| `python3 scripts/diag.py db --db .local/silicon_notebook.db` | SQLite/WAL/表/FK 索引/query plan 的有界源端无副作用证据。 | 离线、纯 stdlib、app-free → `diag_db.py`。 |
| `python3 scripts/diag.py base-recall [active_notebook_id] --db .local/silicon_notebook.db` | 用元数据诊断挂载 base 的可用性与最近报告的 tier 引用计数。 | 有界、源端无副作用的 SQLite 快照；纯 stdlib、app-free → `diag_base_report.py`；不执行检索或回显查询/正文。 |

`base-recall` 复用 `diag_db.py` 的 `O_NOATIME` pin、非阻塞共享锁、源文件身份复核和有界 DB/WAL
拷贝，只在诊断自己拥有的快照上运行固定聚合投影。它不构造 repository、不加载 application、
不迁移、也不用 SQLite 打开源库；安全边界不可用时仅输出 category-only 降级信息。stdout 是一段
最多 32 KiB 的 UTF-8 固定字段报告，只含计数、状态和本次报告内假名，不含原始 notebook/user/
report/object/chunk id、标题、问题、正文、文件名、路径、异常、凭据或 secret。

历史日志读取覆盖并去重 `requests` / `events` / `llm` 的 legacy `<channel>.jsonl`、daily
`<channel>-YYYY-MM-DD.jsonl`、daily gzip `<channel>-YYYY-MM-DD.jsonl.gz` 与下一层 per-user
目录；读取受时间窗、记录数、输入字节和总 deadline 约束，malformed/截断会进入降级信息。

既有独立引擎脚本仍可直接运行，旧运维笔记与 cron 不受影响；新操作优先使用上表七命令。
`bench_sqlite_writes.py`（合成写吞吐基准）与 `replay_retrieval.py`（检索回归对照）不属于
生产 DFX 命令，见下表。

---

## 四、其它(评测 / 迁移 / 一次性,按需)

| 脚本 | 用途 |
|------|------|
| `smoke_backend.py` | 后端 hermetic 冒烟(sqlite 持久化 / KG 抽取边界 / 检索 / 文章 / 反馈);被 `check.sh` 调用 |
| `denoise_reextract_nb.py` | 去噪重抽一个 notebook(**需先停后端**,单写者) |
| `reextract_notebook.py` | 重抽一个 notebook 的所有 source |
| `compare_kg_dbs.py` | 对比去噪前后的 KG,评估成效 |
| `bench_sqlite_writes.py` | 离线 SQLite 写吞吐**基准**(无 LLM/嵌入);非慢因诊断 |
| `replay_retrieval.py` | 检索**回归对照**:固定问题集跑检索管线出 JSON,`--compare` 逐问题 diff,验收"优化前后检索不变";非慢因诊断 |
| `kg_goldgen.py` / `kg_goldgen_all.py` | 为测试章节生成 gold KG 草稿 |
| `kg_product_smoke.py` | 用真实产品抽取链路对样例 source 冒烟 |
| `kg_strip_attrs.py` | 一次性迁移:从 gold 草稿去掉 `attrs` |
| `qiefen_cv.py` | LLM 原子选择器的交叉验证评测 |
| `kg_quality_audit.py` | 审计现有库的 KG 抽取质量:类型构成 / 重名率 / 文档频次长尾 / 噪声探针 / 连通性(只读、零 LLM),见下 |
| `validate_concept_filter.py` | 离线试跑 concept 噪声过滤(无 LLM/不写库) |
| `validate_overmerge_fix.py` | 验证 concept 去过度合并 |
| `git-cleanup.sh` | 清理「PR 已合并」的本地分支 + worktree:默认 dry-run 预演,`--apply` 执行,`--remote` 连带删远程(保护 master / 当前分支 / `eval` / `backup/*`) |

### `kg_quality_audit.py` —— 「库里的节点都是些什么」

回答「这个库为什么有这么多节点、其中有多少是噪声」。只读、零 LLM、零写库,可以对
生产库直接跑(服务在跑也没关系)。

```bash
# 不指定 notebook 时先列候选,并选来源最多的那个
PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db .local/silicon_notebook.db

# 指定库 + 加大抽样
PYTHONPATH=backend python3 scripts/kg_quality_audit.py \
  --db .local/silicon_notebook.db --notebook nb-xxxxxxxx --sources 200

# 全量(千万级库很慢)
PYTHONPATH=backend python3 scripts/kg_quality_audit.py --db <path> --notebook nb-xxxx --sources 0
```

报告分三节:①对象类型构成(全量,走索引);②内容分析(默认随机抽 `--sources` 个来源,
报告里会写明是抽样还是全量);③连通性与边标注(对节点子样本,含 relink 补边占比)。
`--no-samples` 只出数字,一个概念名/命题/公式原文都不打印。

要点:

- **判据直接 import 产品代码**(`app.services.kg.filters` / `app.eval.probes`),不重实现
  —— 否则「现有过滤器拦下多少」会因口径漂移失真。所以必须带 `PYTHONPATH=backend`
  并用后端解释器;缺了会直接报错退出,不会静默降级。
- **只读的准确边界**:产品数据一个字节不动(`mode=ro` + `PRAGMA query_only`)。但 WAL 库
  上 SQLite 仍可能创建/触碰 `-wal`/`-shm`(读最新快照的必需品);服务在跑时这两个文件
  本就存在。它不是「一个文件都不碰」。
- **口径与产品一致**:只统计 `USABLE_STATUSES` 内的对象、`review_status != 'rejected'`
  且**两端都可用**的边 —— 合并/弃用的历史对象、被评审否掉的边、以及指向已合并对端的
  边都不进产品图,算进来会让治理做得越多的库看起来质量越差。被排除的数量单独报出,
  不凭空消失。
- **内存闸是 `--max-objects`(默认 30 万),不是来源抽样**:来源数少于 `--sources` 时
  一个都不会被抽掉,单个来源也可能自己就有几百万对象。触到上限会当场声明截断到了
  第几个来源,绝不静默。
- **抽样绝不静默**:每一节都标注口径;DF(文档频次)在抽样下被系统性低估,报告里有明说。
  `--sources` / `--degree-sample` 拒绝负数(手滑写成 `-1` 会被当成「全量」而在大库上
  静默全扫),在打开库之前就报错。
- **不挂来源的对象**(晋升 / Memory→KG 写路径刻意写 `source_id=''`)按来源抽不到:抽样
  模式会报出它们的数量并说明未纳入,`--sources 0` 才会读进来。晋升为主的 base 库尤其
  要注意这一段,否则会出现「构成几十万行、内容分析近乎空」而看不出原因。
- **边的出处只认标注**:只有 relink 会写 `basis`;没有 `basis` 的边归「未标注」,因为
  knowhow 投影写的 `about` 边也是 `evidence='[]'`,与 LLM 抽取的边不可区分 —— 不替它
  们认领出处。
- 两条已知的判据局限会被自动提示:`is_noise_concept` 的 `len(raw) <= 2` 有丢中文双字
  术语的风险(报告按**风险**措辞,不按已确认的丢失 —— 长度直方图证明不了这件事);
  `probes.claim_degraded` 的动词表只覆盖英文,中文库上该数字无效。

---

## 常见坑
- **后端无 `--reload`**:改了 `.env` 或后端代码,必须 `backend.sh restart` 才生效。
- **必须用对的 Python**:没装依赖的 `python` 会 `ModuleNotFoundError`。设 `PYTHON_BIN` 或激活对应环境。
- **`:8000` 起错服务** → 前端 `/api/notebooks` 404、notebook 看似消失。`backend.sh status` 一查便知,`restart` 修复;数据始终在 `.local/silicon_notebook.db`。
- **多 worktree / root 共用一个库**:`.local` 在仓库根;从 worktree 手敲 uvicorn 可能连到 worktree 自己的空 `.local`。用 `backend.sh`(它固定指向仓库根)最稳。
