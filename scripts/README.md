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

**关键:** DB / storage / `.env` 的相对路径已在代码层锚定到**仓库根**(见 `backend/app/core/config.py` 的 `_ROOT_DIR`),从哪个目录启动 uvicorn 都指向同一套 `仓库根/.local` 与根 `.env`——后端启动日志首行会打印解析后的绝对路径,可一眼核对。脚本仍从 `backend/` 目录启动只是为了模块导入(`app.main`)。注意:多 worktree 时各 worktree 锚各自的根(`.local` 互相独立)。生产启动用仓库根的 `npm run start`（`scripts/prod.sh`：先安装后端 requirements 与 lockfile 锁定的前端依赖，再前端 build + `nohup` 后台 start，后端固定 `--workers 1`）；两个后台进程拉起后它立即退出，不做 readiness/HTTP 校验，服务在关闭 terminal 后仍运行，由运维方自行校验并用 `npm run stop` 停止。只有预装环境用 `SKIP_INSTALL=1` 跳过安装。交接完成前中断会同时 TERM 两个直接子进程，最多等待 `START_CLEANUP_GRACE_SECONDS` 后强制清理并回收；端口占用以监听行为准，不依赖 PID 可见性。模型调度容量位于单个后端进程内，禁止以多 worker 乘大 TOML 声明的容量。

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

### `example_mcp_memory_client.py` —— 外部 Agent MCP/Memory 接入示例

用官方 Python MCP client 连接已经启动的 `/mcp`，完成工具发现、默认/指定 notebook
选择、正式上下文检索与 Agent Memory 检索；加 `--propose` 后提交一条幂等 candidate 并
立即从 Agent Memory 召回。token 只从 `SILICON_NOTEBOOK_AGENT_TOKEN` 读取且不打印。

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<界面签发且只显示一次的 token>'
python scripts/example_mcp_memory_client.py --query '有哪些可复用经验？' --propose
```

完整的界面签发、scope、Codex/Claude 配置、人审与撤销步骤见
[`docs/agent-mcp-memory-sop_zh.md`](../docs/agent-mcp-memory-sop_zh.md)。

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

### `build_postgres_retrieval_indexes.py` —— 在线建立 notebook-aware 词法索引

默认只读检查 `knowledge_objects` / `chunks` 的复合 GIN 是否就绪；`--apply` 才会使用
`CREATE INDEX CONCURRENTLY` 逐条建立。数据库 URL 从 `DATABASE_URL`（或
`--database-url-env` 指定的环境变量）读取且不打印：

```bash
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py
PYTHONPATH=backend python scripts/build_postgres_retrieval_indexes.py --apply
```

工具可续跑，默认保留旧全局 trgm 索引；只有新索引全部验证后，显式
`--apply --drop-legacy` 才会并发删除旧索引。大型活库执行前检查备份、空闲磁盘与副本容量，
低流量窗口运行并监控 `pg_stat_progress_create_index`。完整安全步骤见
`docs/operations_zh.md` 的「PostgreSQL notebook-aware 词法索引」。索引只改变 planner 候选裁剪，
不改变检索谓词、打分或排序。

### `build_hotpath_indexes.py` —— 在线建立热路径修复索引（批 1 + 批 2 + 批 3 + 批 4，共十四条）

默认只读检查热路径修复的全部十四条索引：批 1 六组共八条（`concept_clusters` 两条、三条反向 FK 覆盖、
`knowledge_relations` 一条复合、`chunks(source_id, ordinal)`、`sources` 一条 partial），加批 2（迁移 0042）两条——`idx_knowledge_objects_nb_payload_trgm`（notebook 域复合 partial payload 全文 GIN：btree_gin 令 `notebook_id` 前置、`WHERE status != 'deprecated'`，与 `idx_knowledge_objects_nb_name_trgm` 同形，词集中在别的 notebook 时不再建全局位图；服务集合页搜索 knowledge 腿，稀有词从 5.9s 降到毫秒级（5.9s→3.6ms 的对照基准测于评审前的单表达式全局形，复合形是它的严格收窄、量级结论沿用，精确数字未重测）；生产 9.65M 行上体积按合成语料基准外推约为表段 1.5×（真实语料可能更大）、构建数分钟级，属登记过的写放大债，可 `DROP INDEX CONCURRENTLY` 无损回退；`--apply` 会按需安装 btree_gin 扩展）与 `idx_source_elements_nonblank`（体检 H5 的非空元素 partial），再加批 3（迁移 0043）一条——`idx_clusters_nb_canonical_member`：`concept_clusters(notebook_id, canonical_id, member_object_id)` 普通复合 btree，服务概念详情 hub 簇 keyset 分页的 `ORDER BY member_object_id`，秒级构建，无 GIN 那些顾虑；既有的 `idx_clusters_nb_canonical` 现在是它的严格前缀，同样登记为写放大冗余债、本批不下线，再加批 4（迁移 0048）三条——`idx_sources_nb_title_file_trgm`（`sources(notebook_id, lower(title), lower(file_name))` 的 notebook 域复合 partial GIN trgm，按可见来源类型 partial；两个 trgm 键让 `title`/`file_name` 两条 `LIKE` 腿的 `OR` 能对同一条索引扫两次再 BitmapOr）、`idx_source_authors_nb_name_trgm` 与 `idx_source_paper_meta_nb_ptitle_trgm`，服务来源页签服务端检索的三腿 UNION（生产实测：4.9 万 source 的 notebook 上带 q 的 COUNT 363ms，`source_authors` 21 万行、`source_paper_meta` 3.9 万行被整表扫）；索引的是短文本列而非整份 payload，基准语料上分别约为各自表段的 1.0×/0.3×/1.2×，分钟级构建，三条各自独立可 `DROP INDEX CONCURRENTLY` 回退；短 needle（<3 字符）与 planner 选型这两项实测取舍写在迁移 0048 头注释里——是否
就绪；`--apply` 才会用 `CREATE INDEX CONCURRENTLY`（逐条独立语句，`autocommit=True`，不占
事务）逐条建立。数据库 URL 从 `DATABASE_URL`（或 `--database-url-env` 指定的环境变量）读取
且不打印：

```bash
PYTHONPATH=backend python scripts/build_hotpath_indexes.py
PYTHONPATH=backend python scripts/build_hotpath_indexes.py --apply
```

若 `--apply` 报告某条索引状态是 `INVALID`（此前一次 `CONCURRENTLY` 建索引中途失败留下的
残留），工具打印确切的 `DROP INDEX CONCURRENTLY <name>;` 指引后以退出码 1 结束，重跑前
先手动执行——工具自己绝不会代劳删除，也不会跳过其余仍缺失的索引继续建。若某条索引存在
但列序或谓词与预期不符（同名但形态不同的手建索引），工具同样报错拒绝，绝不把它当成自己
的产物修复或删除。批 1 与批 3 每条都是普通 btree（批 1 其中一条 partial、一条表达式）索引，批 2 的 payload 一条与批 4 的三条是 GIN，单条
建索引通常秒级，但 `CREATE INDEX CONCURRENTLY` 仍要对表做一次全表扫描，繁忙数据库上应
避开高峰期。

与迁移 `0039_hotpath_batch1_indexes.sql`（同理 `0042_hotpath_batch2_search_indexes.sql`、
`0043_concept_cluster_keyset_index.sql`、`0048_source_search_trgm_indexes.sql`）的先后关系：这些迁移在事务里用普通
`CREATE INDEX IF NOT EXISTS` 声明同一批索引，`CONCURRENTLY` 进不了事务，所以已有生产
流量的库应先跑本脚本 `--apply` 在线建好，迁移落地时就是 no-op 的账本记录；全新部署、还
没有生产流量的库，迁移本身已经够用，先跑本脚本是可选项。完整运维步骤见
`docs/deployment-and-configuration_zh.md` 的热路径索引一节。

### `batch_ingest.py` —— SQLite / PostgreSQL 离线批处理

`ingest`、`kg`、`index`、`all`、`embed`、`metadata`、`question-index`、`reparse`、
`backfill-source-index` 会通过统一 factory 使用 `DATABASE_URL` 选中的正式后端。
PostgreSQL 必须先停 API 与全部后台 writer，再给命令追加
`--confirm-service-stopped`；该参数不会替你停服务。所有生产维护 wrapper 使用同一
preflight + database-wide advisory lock，锁竞争会以状态码 2 退出。`--dry-run` 不连接
数据库。`vectors-to-blob` 只用于 SQLite 旧文本向量；PostgreSQL 已存 `bytea`，会在连接前拒绝。

```bash
PYTHONPATH=backend python scripts/batch_ingest.py index \
  --notebook-id nb-xxxx --confirm-service-stopped

# GENERATED_QUESTION_INDEX_MODE=shadow|on，且模型 TOML 已绑定两个 workload 后执行
PYTHONPATH=backend python scripts/batch_ingest.py question-index \
  --notebook-id nb-xxxx --confirm-service-stopped
```

### `build_scale_index.py` —— 与服务并存的离线 / 异机 scale 索引构建

上面 `batch_ingest.py index` 是**停服**通道（数据库级全局 advisory lock）。这条是
**并存**通道：取 per-notebook 跨进程锁、`.tmp` + 原子 rename，服务按既有逐请求探测
自动换代，**不用重启**。只支持 PostgreSQL（SQLite 单进程部署没有跨进程锁，直接拒绝）。
必须用**生产 `.env`** 运行，组装仓库前会用裸连接校验迁移账本与本 checkout 一致；
组合根显式 `migrate=False, seed=False`，绝不对在役库跑迁移、也绝不改写 admin 凭据。

```bash
PYTHONPATH=backend python scripts/build_scale_index.py inspect --notebook nb-xxxx
PYTHONPATH=backend python scripts/build_scale_index.py build   --notebook nb-xxxx [--full|--fold]
PYTHONPATH=backend python scripts/build_scale_index.py export  --notebook nb-xxxx --to DIR
PYTHONPATH=backend python scripts/build_scale_index.py import  --notebook nb-xxxx --from DIR
```

`--statement-timeout-seconds`（默认 86400）是全局参数，写在子命令**之前**。`import`
会硬拒 pipeline 身份、embedding 维度和 hnswlib 版本失配（后者可用
`--allow-library-mismatch` 覆盖），numpy/scipy 只告警。退出码：0 成功 / 1 已开始但失败
（锁被占、构建失败、swap 前复验失败）/ 2 未动手就拒绝 / 130 Ctrl-C。
异机三步、两机 pin 清单、连接预算、PgBouncer 前提、`.old` 恢复与 allow_pickle 来源约束
见 `docs/operations_zh.md` 的「离线 / 异机 scale 构建」。

### `sweep_legacy_delete_leftovers.py` —— 存量删除残渣一次性清扫

删除作业化(批 3·W1)之前的同步删除路径崩溃留下的孤儿行(5 张无外键表)与
孤儿目录(5 棵存储根,含 scale 产物的 scratch 兄弟)。默认只读盘点,`--apply`
才动手;在线模式只清孤儿行(有界事务),scale 三根仅 PostgreSQL 可在线清
(真排它 claim,被占跳过留声;SQLite 无跨进程锁,同样只报告),
`notebooks`/`assets` 两根在两后端都必须 `--confirm-service-stopped` 停服
窗口执行(在线只报告不删;时间不是锁),停服下年龄闸 `--min-age-seconds`
是防「没停干净」的皮带。盘点/复核是全表扫,建议低峰执行。

```bash
PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py            # 盘点
PYTHONPATH=backend python scripts/sweep_legacy_delete_leftovers.py --apply    # 清扫
```

退出码 0/1/2 与细节见 `docs/operations_zh.md` 的「存量删除残渣清扫」。

---

## 二、检索 / chunk 运维

### `build_chunks.py` —— 为现有 notebook 回填 chunk + 向量
```bash
PYTHONPATH=backend python scripts/build_chunks.py <notebook_id> [--confirm-service-stopped]
```
chunk-native 检索的 chunk 是摄取时自动建的;**老 notebook**(chunk-native 上线前导入的)需用本脚本补建 chunk 表 + chunk_embeddings,之后默认 chunk 模式问答才有内容。幂等(重跑覆盖该 notebook 的 chunk)。

### `backfill_kg_embeddings.py` —— 补全 notebook 的 KG 对象向量
```bash
PYTHONPATH=backend python scripts/backfill_kg_embeddings.py <notebook_id> [--confirm-service-stopped]
```
KG 对象向量在 `store_kg` 入库时嵌入;并发过高被限流漏掉的,用本脚本低并发补齐。
上述确认参数仅在 PostgreSQL 必需；SQLite 可省略。其他生产维护 wrapper
（`build_kg`、`recluster_kg`、`reembed_kg`、`backfill_relation_embeddings`、
`reextract_notebook.py`、`denoise_reextract_nb.py`）遵循同一规则。

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

### `diag_pg_hotpaths.py` —— PostgreSQL 生产热路径自查（不进上面七命令矩阵）

```bash
python3 scripts/diag_pg_hotpaths.py                    # 默认档
python3 scripts/diag_pg_hotpaths.py --notebook-id nb-xxxxxxxx
python3 scripts/diag_pg_hotpaths.py --deep              # + 四条重探针
```

只对 PostgreSQL 生效（`database_identity(...)` 必须解析为 `postgresql`，否则拒绝运行）；
与上面 `diag.py` 七命令矩阵不同，本脚本会 `import app`、需要真实 `DATABASE_URL` 连接，且
只服务 PostgreSQL 后端，故单独登记，不进那张纯 stdlib / app-free 的矩阵。

只读声明：`SET default_transaction_read_only = on` 是连接后的第一条语句（先于任何其它查询，
包括未指定 `--notebook-id` 时的自动选库）；autocommit、每条语句各自一个隐式事务；只跑
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`、`SELECT COUNT(*)`、`pg_indexes`/`pg_constraint`
目录查询，不写库、不建索引、不跑 DDL；单条语句失败只记账跳过，不阻断其余语句，但**任一
语句失败会让整体退出码为 1**（0 表示全部语句成功返回一行结果）。

预期时长：默认档也不是纯秒级——热语句族的 EXPLAIN 探针是 notebook 级、走索引，通常秒级，
但随后的全表行数一览对每张热表跑无 notebook 过滤的 `SELECT COUNT(*)`，9M 行级的表这一节
可能是分钟级；未指定 `--notebook-id` 时的自动选库同样是一次 `knowledge_objects` 全表
`GROUP BY`。`--deep` 额外加两条 ILIKE 全文探针 + 两条缺向量反连接 COUNT（这两条正是 Z7
从 backfill-vectors 受理路径拿掉的查询——冷库单条可超 30s），请勿在生产高峰期运行。

### `diag_retrieval_latency.py` —— 检索分段分布（只读、无正文）

```bash
python3 scripts/diag_retrieval_latency.py --since 24
```

该脚本复用 `diag_common` 合并 legacy / 按日 / gzip / per-user 的 `events` 日志，并按本机
scale manifest 的 `n_chunks` 把 `chunk_fts_ms`、ANN/KNN/索引加载、KG 词法等分段聚合成
P50/P95/max；KG 词法会进一步拆分 GiST KNN、复合 GIN 与短页 fallback 耗时，并汇总各路由
词项数；`retrieval_run_stats` 另按 Ask / 报告 planning / 报告 generation 汇总 FTS
timeout 与熔断跳过次数。它只读日志和 manifest，不打开数据库，不输出 notebook id、问题、
来源、正文、SQL 或错误文本。自定义存储目录时显式传
`--index-root /path/to/storage/kg_index`。manifest 规模不含水位后的 delta；缺 manifest 的
事件保留在 `unknown` 桶，不能误当成小库。时间窗口默认先读最新文件并完整解码所有可能落入
窗口的按日文件；需要限制诊断耗时时可传 `--max-input-mb N`，记录超过默认保留量时可调高
`--max-events`。命中任一上限都会在 `log_scan` 中显示 `truncated:true`。

---

## 四、其它(评测 / 迁移 / 一次性,按需)

| 脚本 | 用途 |
|------|------|
| `smoke_backend.py` | 后端 hermetic 冒烟(sqlite 持久化 / KG 抽取边界 / 检索 / 文章 / 反馈);被 `check.sh` 调用 |
| `embed_md_images.py` | 把 markdown 引用的本地图片文件就地内嵌成 base64 data URI,产出可直接上传的自包含单文件(与摄取端 `MINERU_MAX_IMAGE_BYTES` 默认一致的 5MB 单图上限、四种 MIME 白名单) |
| `denoise_reextract_nb.py` | 去噪重抽一个 notebook(**需先停后端**,单写者) |
| `reextract_notebook.py` | 重抽一个 notebook 的所有 source |
| `compare_kg_dbs.py` | 对比去噪前后的 KG,评估成效 |
| `bench_sqlite_writes.py` | 离线 SQLite 写吞吐**基准**(无 LLM/嵌入);非慢因诊断 |
| `bench_scale_build_paging.py` | scale build 图侧 keyset 分页 / 分页 ANN 喂入的**测量台**(批 3·W4 T-W4-3):`seed`(合成多库语料)/ `explain`(六条改动读的 EXPLAIN ANALYZE,判定 range 续扫 vs 每页重扫+Sort)/ `evidence`(在线无参形 vs 构建分页形的双模 A/B,「不降检索性能」红线证据)/ `build`(分阶段耗时,≤10% 门)/ `rss`(每臂独立子进程测 ru_maxrss 与最大存活数组字节)/ `drop`。**绝不进 CI**:需要专用 `BENCH_POSTGRES_URL`(库名必须含 `_test`)、会写库,只用于复现结论 |
| `replay_retrieval.py` | 检索**回归/A-B 对照**：固定问题集跑检索管线出 JSON，`--compare` 逐问题 diff；`--report-run` 才会进入报告 retrieval-run 并真实触发 `CHUNK_FTS_WITH_ANN_ENABLED`，`--summary-only` 输出可回帖的无问题/命中 id 汇总；非慢因诊断。 |
| `kg_goldgen.py` / `kg_goldgen_all.py` | 为测试章节生成 gold KG 草稿 |
| `kg_product_smoke.py` | 用真实产品抽取链路对样例 source 冒烟 |
| `kg_strip_attrs.py` | 一次性迁移:从 gold 草稿去掉 `attrs` |
| `qiefen_cv.py` | LLM 原子选择器的交叉验证评测 |
| `kg_quality_audit.py` | 审计现有库的 KG 抽取质量:类型构成 / 重名率 / 文档频次长尾 / 噪声探针 / 连通性(只读、零 LLM),见下 |
| `validate_concept_filter.py` | 离线试跑 concept 噪声过滤(无 LLM/不写库) |
| `validate_overmerge_fix.py` | 验证 concept 去过度合并 |
| `shadow_sqlite_to_postgres.py` | 显式 SQLite→PostgreSQL 正向 shadow CLI：`preflight` / `start-forward` / `status` / `verify` / 前台 `worker`；单独设置 `SHADOW_DATABASE_URL` 不会启动同步，且不得与停写 importer 共用目标库，完整 runbook 见 `docs/operations_zh.md` |
| `shadow.sh` | 本机 shadow worker supervisor：按 run/work-dir 做 PID identity 校验并提供 `start/status/stop/restart`；生产也必须保持单 worker |
| `git-cleanup.sh` | 清理「PR 已合并」的本地分支 + worktree:默认 dry-run 预演,`--apply` 执行,`--remote` 连带删远程(保护 master / 当前分支 / `eval` / `backup/*`) |
| `export_reasoning_traces.py` / `analyze_reasoning_trace.py` / `reflect_shadow_rig.py` | reflect v2 开闸前 T0 的三件套:只读导出轨迹 → 闭集投影 JSONL、离线聚合出表、以及在一次性测试库上跑影子 run 的 rig,见下 |

### reflect v2 开闸前 T0 —— 轨迹统计与影子 run

设计真源:`docs/superpowers/specs/2026-09-08-reflect-t0-trace-analysis-design_zh.md`。
三个脚本读写分离,**导出与分析对任何库都只读**,rig 的写入全部落在一次性测试库。

```bash
# 1) 只读导出:--database-url 必填(脚本不读 .env、不隐式连库);PG 与 SQLite 通用
python scripts/export_reasoning_traces.py \
  --database-url postgresql://127.0.0.1:5432/<库名> --reports \
  --out .local/t0/baseline.jsonl

# 2) 离线聚合:零 DB / 零模型 / 零网络,只吃上一步的 JSONL
python scripts/analyze_reasoning_trace.py .local/t0/baseline.jsonl \
  --group-by consumer,policy_version,effort,kg_in_scope \
  --min-samples 5 --out-md .local/t0/baseline.md --out-json .local/t0/baseline.json

# 3) 影子 run:先看它打算做什么,再真跑
python scripts/reflect_shadow_rig.py --dry-run seed
python scripts/reflect_shadow_rig.py --dry-run --limit 2 ask

# 3b) 不调模型的冒烟:一次性 SQLite 后端 + 一份小 markdown,只验上传与解析半程
python scripts/reflect_shadow_rig.py \
  --database-url sqlite:///$TMP/t0.db --env-file $TMP/empty.env \
  --storage-dir $TMP/storage --out-dir $TMP/out --corpus-dir $TMP/corpusB \
  --cell B_kg --port 8011 --skip-create-db --skip-kg --skip-embed seed

# 3c) 换策略:seed 起的后端默认是 legacy;换 v2 之前用 restart 切过去,
#     不要自己去 kill -TERM 再手动重起 uvicorn(端口/DB/env-file 从 state 里
#     seed 落的那份取,不用重复输入)
python scripts/reflect_shadow_rig.py --dry-run restart --policy v2
python scripts/reflect_shadow_rig.py restart --policy v2
python scripts/reflect_shadow_rig.py --limit 2 --policy v2 ask

# 4) search:**不建测试库、不建图、不合成**,直接对主库(只读)里两个既有笔记本
#    跑检索过程(plan + reflect 循环),轨迹进程内投影成 JSONL。
#    先 dry-run 看计划与模型调用估计:
python scripts/reflect_shadow_rig.py --dry-run --limit 5 search

#    再真跑(一次进程跑完 legacy 与 v2 两侧,--policy 在这条路上不读):
python scripts/reflect_shadow_rig.py \
  --database-url postgresql://127.0.0.1:5432/<主库名> \
  --env-file /path/to/main-checkout/.env \
  --source-notebook-a nb-<无图单篇> --source-notebook-b nb-<有图多篇> \
  --owner <主库用户名> --limit 5 --out-dir .local/t0-search search

#    题目多、要跑得快时加 --concurrency(默认 1,行为与不加这个参数逐字一致):
#    两阶段——先按同一并发度并发把这批题的意图契约算完,再把全部 run(legacy /
#    v2 合并进同一个线程池)提交上去跑。真跑前**先确认**:模型服务那边的并发
#    限流吃得下这个数(否则会被限流打回,不是变快而是变成一堆失败),以及主库是
#    PG 时 POSTGRES_POOL_MAX_SIZE 不小于它(rig 会自动 clamp,但 clamp 到 1 就等于
#    没加速);SQLite 主库(冒烟场景)真跑时一律强制 clamp 到 1。
python scripts/reflect_shadow_rig.py \
  --database-url postgresql://127.0.0.1:5432/<主库名> \
  --env-file /path/to/main-checkout/.env \
  --source-notebook-a nb-<无图单篇> --source-notebook-b nb-<有图多篇> \
  --owner <主库用户名> --limit 5 --concurrency 6 \
  --out-dir .local/t0-search search

#    出的两份 JSONL 与 ask/report 那边同格式,直接进同一个聚合器:
python scripts/analyze_reasoning_trace.py \
  .local/t0-search/search-legacy.jsonl .local/t0-search/search-v2.jsonl \
  --group-by corpus_cell,policy_version,effort \
  --out-md .local/t0-search/search.md --out-json .local/t0-search/search.json

#    只想跑某几题/某个语料格(核对反推口径、复现一个可疑 run)时用
#    --only-question(可多次,题号如 B-q03)/--only-cell(A_nokg 或 B_kg);
#    两者都在 --limit 切片**之前**生效(先筛后限),dry-run 与真跑读的是
#    同一份枚举:
python scripts/reflect_shadow_rig.py --dry-run \
  --only-question B-q03 --only-cell B_kg search

#    --keep-raw-trace 额外把每个 run 的原始 TraceStep 列表(含 summary 人话
#    摘要)与 termination DTO 写到 <out-dir>/raw/<policy>/<question_key>_
#    <corpus_cell>_<effort>.json。**这份文件含标题与模型 reason,不进数据集/
#    不进仓库**,只用来人工核对 §4.5 的反推口径:
python scripts/reflect_shadow_rig.py \
  --database-url postgresql://127.0.0.1:5432/<主库名> \
  --env-file /path/to/main-checkout/.env \
  --source-notebook-a nb-<无图单篇> --source-notebook-b nb-<有图多篇> \
  --owner <主库用户名> --only-question B-q03 --only-cell B_kg \
  --keep-raw-trace --out-dir .local/t0-search search

# 5) ab:在 `seed` 建的一次性测试库上跑**完整 Ask**(意图契约 + 检索 + 合成 +
#    引用绑定),同题同档两条臂背靠背。臂是 `(policy, optimization)` 这一**对**
#    ——`--arms` 不给就是既有的一维两臂 `legacy,v2`(第二维恒 off):
python scripts/reflect_shadow_rig.py --dry-run \
  --database-url postgresql://127.0.0.1:5432/<库名>_test \
  --source-db-url postgresql://127.0.0.1:5432/<主库名> ab

#    前缀复用实验换成**第二维**:同一条 v2 协议,只翻
#    REASONING_REFLECT_OPTIMIZATION。`legacy:prefix_snapshot` 这类合法值的非法
#    组合、重复臂、空段都在跑批之前响亮拒绝。
#    第二维要跑 `prefix_delta` 就把 `--arms` 换成 `v2:off,v2:prefix_delta`
#    (或 `v2:prefix_snapshot,v2:prefix_delta`);跑 `prefix_delta_lean`(D↔L 是
#    本设计里唯一一对只差自评合同的配对臂,收益不得归因到缓存本身)就换成
#    `v2:prefix_delta,v2:prefix_delta_lean`——一次只收一对:
python scripts/reflect_shadow_rig.py \
  --database-url postgresql://127.0.0.1:5432/<库名>_test \
  --source-db-url postgresql://127.0.0.1:5432/<主库名> \
  --env-file /path/to/main-checkout/.env \
  --arms v2:off,v2:prefix_snapshot --repeats 3 \
  --out-dir .local/ab-prefix ab
```

**`ab` 的臂是二维的**(前缀复用最终设计 §11)。`--arms` 收两种写法,产出同一种
结构:一维 `legacy,v2`(省略的第二维一律补 `off`,不跟随进程默认——那会让同一条
命令在两台机器上跑出两批数据)、二维 `v2:off,v2:prefix_snapshot`。合法组合是
**五格**——`legacy:off` / `v2:off` / `v2:prefix_snapshot` / `v2:prefix_delta` /
`v2:prefix_delta_lean`(`reflect_ab.ARMS`):v2 总闸关时 `reflect_optimization()`
恒返回 `off`,所以 legacy 那一维上没有「前缀」这个概念。`prefix_delta_lean`
(简称 L)字节上是 `prefix_delta`(D)的双胞胎,唯一差别是自评合同——**D↔L 是
这份设计里唯一一对只差自评合同的配对臂**,两者之间量出的任何收益都不得归因到
前缀缓存本身,那笔账已经在 D↔P 那一对上算过了。`--arms` 与 `--only-policy`
**互斥**(后者是一维时代按 policy 过滤默认两臂的写法)。

**D↔L 这一对的差值现在可以由 `optimization_pair_table`(`scripts/
analyze_reasoning_trace.py`)直接给出——这条已知限制已解除(PR-5 T-EX9)**:
`--baseline-arm`(默认仍是 `off`)把基线从硬编码换成参数,
`--arms v2:prefix_delta,v2:prefix_delta_lean --baseline-arm prefix_delta`
就能在只有 D/L 两条臂的一批上直接出配对表,不必再把 `off` 也跑进各自的批次
自己相减。默认参数下这份报告 dict 的**键集**与接入前逐字节相同(golden 用例
全绿即证);唯一的默认路径偏离(预存缺陷修复,评审 P3-6):布尔维度的 `False`
不再折成 `unknown`——只在输入真带 `has_intent_contract=False` 这类行时才可见,
`pairs`/`optimization_pairs` 会跟着变,golden fixture 未重签。
另外 `ab-runs.jsonl` 的每一行带 ab 专属键(`paired`、`pair_id` 等),
`analyze_reasoning_trace.py` 的 `load_rows` **默认**仍整批拒绝它们——这条既有
纪律(T-PS4 起)不变,要吃 A/B 键就显式传 `--key-set ab`(见下)。

**整批墙钟预算**:`--max-wall-minutes`(默认不给 = 今天的行为逐字相同,不设
上限、不早停)。到点后 `ab` **停止派发**新单元、`cancel_event.set()` 唤醒
在途、`shutdown(cancel_futures=True)` 撤掉队列里未派发的;在途未完成单元落
`status=cancelled` 行(删失观察),**不偷偷补跑到矩阵齐全**——整批以「预算
到点」为由非零退出,已完成的前缀仍是一份配对完整的数据集(重复轮在最外层
这条既有性质保住它)。

**manifest**:收尾写 `<out-dir>/manifest.json`(最新,覆盖)+
`<out-dir>/manifests.jsonl`(历史,追加)——三条实验通道(E1/E2/E3)共用同一
处写点与同一套 **18 键**闭集(`backend/app/eval/reflect_manifest.py` 的
`build_manifest`/`MANIFEST_KEYS`)。`ab`(E3)这条通道的**必填**键 = 三条通道
共同必填(`REQUIRED_KEYS_ALL_CHANNELS`:`code_sha`——`git rev-parse HEAD`,
工作树不干净时带 `-dirty` 后缀,`git status` 本身失败时带 `-dirty_unknown`
——、`started_at`、`finished_at`、`stopped_by_budget`,如实写,预算没触发就是
`false`)并上 `REQUIRED_KEYS_BY_CHANNEL["e3"]`(`intent_contract_digest_by_question`、
`arm_order_seed`——本期 `ab` 的臂序按 run 无种子,恒 `null`,但键本身必须
在场——、`matrix`、`corpus_signature_by_cell`);`channel` 单独校验,不在这两张
表里但同样必须在场。`matrix` 必填子键是**五个**维度基数
(`REQUIRED_MATRIX_KEYS_BY_CHANNEL["e3"]`):`questions`/`cells`/`efforts`/
`arms`/`repeats`——**`cells` 不是乘数**,各语料格的题集不相交,总 run 数 =
`questions × efforts × arms × repeats`,`cells` 只记「这批横跨几个格」;另有
额外子键 `planned_runs` 直接给出这批**计划**的 run 数上界,不必自己相乘对账。
`ab` 本 PR 也写但**非必填**的键:`arms`、`optimization_by_arm`、
`common_baseline`、`model_contract`、`budgets`(`reasoning_timeout_seconds`/
`reasoning_attempt_budget`/`max_wall_minutes`/`batch_deadline_seconds` 四个
子键)——E3 本期不写 `order`/`seed`/`case_set_digest`/`sample_digest`。值只许
是短码字符串、数值、`bool`、`null`,或以短码为键、值同样合法
的字典——**不含**题面原文、答案正文、URL、`postgresql://` 连接串、生产凭据,
一处污染(哪怕落在字典**键**上)都会被隐私断言当场拒绝。

**`analyze` 的三处新参数(PR-5 T-EX9)**:`--baseline-arm`(默认 `off`)如上;
`--pair-rows`(默认关)额外出一张**逐题配对差值表**——同一格内先对 `repeat`
取中位数、再出 `Δ` 与 `ratio`(**不是**两组独立 P50 的比值),分位数仍受
`--min-samples` 约束并逐格标 `n_pairs`,超时/取消进删失一列(`n_censored`/
`n_failed`/`n_unfinished` 分基线/变体两侧报);`--key-set {t0,ab}`(默认
`t0`)让 `load_rows` 认 A/B 专属键——**默认不改**,多出来的键必须让人显式看见
是 `load_rows` docstring 的原话,`--key-set ab` 直接吃 `ab-runs.jsonl` 而不必
先把行降到 T0 键集。

**`--arms` 一次只收一对臂**,超过两条在跑批之前拒绝。配对差值表按**对**出:三条
臂的批次里每个配对单元落三行,`mark_paired` 判不出配对,整批 `paired` 全 `False`
——数据集、日志、投影一切正常,只是一个配对结论都出不来,而这批已经烧掉了几百次
模型调用。要跑三格就分两批,每批一对。`--arms ""`(空写法,常见于脚本里
`--arms "$ARMS"` 而变量恰好为空)同样响亮拒绝,**不**退化成默认两臂。

两条臂的 `REASONING_REFLECT_MEASURE_CONTEXT` **都是 `true`**:测量开关与
`optimization` 正交,对照要的正是两条臂用同一把尺子;只给一侧开会让差值表只剩一
侧有数,而每一行看起来都很正常。每条臂各自的 `REASONING_REFLECT_OPTIMIZATION` 在
构造那一份 `Settings` 时逐臂设(进程环境只能有一个值)。声明与实际跑起来的那一个
在**第一次调用模型之前**当场对号(`reflect_optimization()` 的直接读数),不符就
整批停——两条 v2 臂的轨迹形状逐字相同,事后从产物里反推不出第二维。

`arm` 与 `optimization` 在投影行里是**两列**,都是 rig 的声明;`policy_version`
是从轨迹反推的证据。产物路径按 `arm_label` 分:`off` 那一格落回光秃秃的 policy
(`raw/v2/`、`calls-v2.jsonl`,与二维化之前逐字相同),非 `off` 带上第二维
(`raw/v2-prefix_snapshot/`)——否则两条 v2 臂的存档会撞进同一个路径。

**日志隔离**:rig 的两条进程内路径(`search` / `ab`)与它起的临时后端都把
`LLM_LOG_PATH` 与 `EVENT_LOG_DIR` 圈进本次 `--out-dir`(`<out-dir>/llm/llm.jsonl`
与 `<out-dir>/events/`),**不往机器共用的 `.local/logs/` 写一个字节**。两个默认
落点都是整台机器共用的:同一天里别的后端、别的冒烟、别的 rig 会话都往同一份
`llm-*.jsonl` / `events-*.jsonl` 追加,在共用日志上按时间窗切片会把**别的进程**的
模型调用记到本 run 头上。副作用是启动时会打一行「LLM_LOG_PATH 的目录与
EVENT_LOG_DIR 不一致」的告警——那条告警说的是日志查看器读不到 per-user 的 llm
日志,rig 的产物没有查看器要读,两串日志各按自己的 glob 读。

**per-call 表 `calls-<arm>.jsonl`**:一次模型调用一行,由 `llm.jsonl` ⋈
`events.jsonl` 按 `support_id` 对号得来(核心纯分析在
`backend/app/eval/reflect_context_bench.py`,rig 只做薄适配)。列只有数值与短码:
`call_wall_ms` / `attempts` / `response_chars` / 各 token 计数(传输侧,来自
`app/core/llm.py` 写的那一行)与 `queue_latency_ms` / `execution_latency_ms` /
`workload_id`(调度侧,来自 `model_scheduler` 事件);请求正文、响应片段、模型名、
时间戳与 id 一个都不进。`join` 那一列如实说出归因质量:`joined` / `log_only`
(缺事件)/ `event_only`(缺**终态**日志行)/ `ambiguous`(同号多行,跨侧列一律
unknown,**不按顺序猜配对**)/ `unattributed`(这一行没有相关号)/ `retry`(这一
行是一次没跑完的尝试,不是一次调用的结果)。

**重试行不占格子**。`app/core/llm.py` 每次瞬时错误重试都再写一条 `status="retry"`
的日志行,它与终态行**同号**,而调度事件只有一条。这几行单列成 `retry`:不参与
扇出判定(否则每一次重试过的调用都会落成 `ambiguous`,排队/执行时长与 workload
全线 unknown,而那批恰是最慢、最该被看见的调用),`call_index` 是 `null`。**数这
一批跑了多少次调用要数 `call_index` 有值的行,不要数行数**,否则重试过的调用会被
数成两次。这几列的闭集(以及每一行写出去之前那两道自检)在
`backend/app/eval/reflect_context_bench.py`。

⚠ **并发跑批时 run 级标签是 unknown**。`support_id` 只把日志行与它自己的调度事件
对上号,它不知道这次调用属于哪个 run;`--concurrency > 1` 时那一批的 per-call 行
落进 `calls-unknown.jsonl`,`arm` / `optimization` / `question_key` / `corpus_cell`
/ `effort` / `repeat` 六列全是 `null`(与并发下成本三键强制 unknown 同一条口径:
硬塞一条臂进去比不出表更坏)。要按臂读 per-call 表就跑 `--concurrency 1`。

**`run_wall_ms`**:一次 run 的墙钟,产地只有 rig(轨迹里的 `total_ms` 是各步耗时
之和,漏掉排队与步与步之间的空隙)。**失败的 run 也写**——它崩在半路,但确实占了
这么久,而「哪一侧更容易崩、崩之前烧了多少时间」正是要量的东西之一;只有**压根
没开跑**的行(范围解析失败)是 `null`,写 0 会被读成「零耗时跑完了」并真的进
P50/P95 的分位数。

**dry-run 打两个数**:`次逻辑调用`(llm.jsonl 的行数、`model_calls` 的口径)与
`请求上界`(真正发出去的请求数、行上 `attempts` 的口径)。两者的乘数是配置里的
重试预算;静默 fallback(`response_format` / `stream_options` 被拒后的重发)另计
——它多发一次请求却不留任何日志行,配置里查不出来。混用这两个数会把一次静默
fallback 记成一次额外的推理,反过来则会低报端点负载。

**隐私口径(三个脚本同一份)**:每个 run 输出一行,键取自
`app.domain.reasoning_trace_stats.RUN_PROJECTION_KEYS` 这个闭集(**闭集与短码形状
的真源是 `backend/app/domain/reasoning_trace_stats.py` 那一个模块**,这份 README 与
任何设计稿都只是它的转述;两处对不上时以模块为准),值只能是闭集字符串、
bool、数值、`None`,短码的列表(`action_seq`),或以短码为键、数值为叶的字典
(`skip_reasons` 是一层,`citation_contribution` 是两层)。其中 `skip_reasons` /
`fallback_reasons` 的**键是服务端原因码的透传**,不另过闭集校验。**问题原文、答案正文、来源标题、证据
文本、模型 reason、trace summary 和任何 id 都不会出现在输出里**;笔记本只以来源数分桶
(`notebook_bucket`)出现,题目只以题号(`question_key`)出现。写每一行之前都过一次
`assert_closed`,加错一个键会当场炸,而不是安静地把一列自由文本落进 JSONL。

**M7 的精确说法(PR-5)**:这份隐私口径只覆盖**测量产物**——写进
`probe-*.jsonl`/`state-probe-*.jsonl`/`ab-runs.jsonl`/`manifest.json` 的每一行
零正文。`.local/<out-dir>/llm/` 下的交互日志(`LLM_LOG_PATH`)是另一回事:它
含**截断正文**,是操作者私有的实验资产,不受这份闭集约束、不进数据集、不进
仓库(`--out-dir` 默认在 `.local/` 下,已被 `.gitignore` 排除)。

**`search` 的 `--keep-raw-trace` 不受这份闭集约束**:它写的
`<out-dir>/raw/<policy>/<question_key>_<corpus_cell>_<effort>.json` 是原始
TraceStep(含 `summary` 人话摘要)与 termination DTO 的逐字落盘,**含标题与
模型 reason**,只为人工核对反推口径,不进 `search-<policy>.jsonl`、不进数据集、
不进仓库(`--out-dir` 默认在 `.local/` 下,已被 `.gitignore` 排除)。

**`fallback_count`/`fallback_reasons` 只数模型兜底**(2026-09-08 订正):reflect 步 detail
里带 `fallback_reason` 键的那些,即反思调用失败或响应畸形后的 fail-open 收尾(原因码如
`provider_unavailable`/`invalid_enum`/`malformed_response`)。`search_elements` 那个同名
的 `fallback` step_type 是初检索空手后补查原文的路由决策,与模型兜底无关,只在
`actions_by_type` 里以 `fallback` 计数,不进这两个键。末尾 reflect 带 `fallback_reason` 时,
`termination_reason` 的 legacy 反推给出 `model_degraded`(与 v2 闭集同一个码),不是
`model_end`。

**`unknown` 是一等值**:旧轨迹缺字段就是 `null`,不折成 0/false;聚合侧为每个指标分别
报 `n_observed` / `n_missing`,并且只在 `n_observed >= --min-samples` 时输出 P50/P95。
两处已知的恒 `unknown`,是**写侧没有这个字段**、不是数据缺失:`candidates_chunks`
(answer 步的 detail 里没有 chunk 候选计数),以及线上导出的 `corpus_cell` /
`question_key`(浏览器铸的幂等键不带 rig 编号,那些 run 只进基线表、不进对照表)。

报告侧**每节只写一行**:轨迹投影与结果级投影按 `merge_key` 当场合成。写成两行会让一份
3 节的报告在聚合表里报成 6 个 run、每个指标恰好一半「缺失」——`n_observed` 那套分母就
此报废,而表面上完全看不出来。

**rig 的硬约束**:
- 主库只在 `seed` 重建 A 语料时被读一次(`--source-db-url` 必须显式给,PG 侧连接开
  `read_only`);所有写入都在 `--database-url` 指向的测试库。`teardown` 只在
  `--database-url` 确实是 PG 的 `--db-name` 时才删库——否则它会去 PG 上删一个同名的、
  可能是别人的库。
- 换检索策略靠**重启后端**(`REASONING_REFLECT_V2_ENABLED`),rig 不热改 Settings;
  `ask` / `report` 每次只跑一个 `--policy`。重启用 `restart` 子命令——它读 state
  里 `seed` 落的 `backend_pid`,优雅停止(SIGTERM,超时 SIGKILL)后按 `--policy`
  重起同一份端口/DB/env-file 配置,等 `/api/ready` 翻牌再更新 state 里的 pid 与
  policy;不要手动 `kill` 再另起一个 uvicorn。`ask` / `report` 开跑前会核对 state
  记的策略与这次的 `--policy` 是否一致,不一致直接报错("先 `restart --policy
  <目标策略>`"),不会悄悄跑出一批策略对不上号的轨迹。
- 题号 / 语料格 / 策略 / 档位只编进 `client_request_id`(`t0:<题号>:<语料格>:<策略>:
  <档位>:<mode>`),导出时据它打标——**不进问题文本,模型看不到**。
- `ask` 提交必须走 `/ask/stream`,**不能走同步的 `/ask`**:同步端点经
  `begin_durable_job` 建行,那条路的 `client_request_id=None` 是写死的
  (`repositories/postgres/ask_state_store.py`,SQLite 侧同形),只有
  `begin_or_attach_durable_job`(stream 端点)才把幂等键落库。走错端点的话整批 run 的
  `question_key`/`corpus_cell` 全是 unknown、对照表恒空,而且要跑完几百个 run 才看得
  出来。rig 在**第一个 run 之后**就回读测试库确认标签落了库,不落就当场停。
- rig 本体要网络与真实模型,**不进 `check.sh`**;进门的只有 `--dry-run` 的枚举、编码、
  上传体/teardown 闸的纯函数用例,`search` 那条在 SQLite 测试替身上跑真检索、假模型的
  接线用例,以及 `--concurrency` 的并发编排用例——一条在同一个 SQLite repo 上真的并发
  跑 legacy / v2、一条用假 `run_search_once`(`threading.Barrier`)钉住并发峰值真的到
  了 `N`、一条钉住「声明策略与证据不一致 ⇒ 取消剩余任务」这条守卫
  (`backend/tests/test_reflect_t0_scripts.py`)。

**`search` 的硬约束(它是唯一直接对主库跑检索的子命令)**:
- **主库只读,而且是可核对的只读**。`--database-url` 必须**显式给出**(默认值指向的是
  `ask`/`seed` 用的一次性测试库,拿它去跑 `search` 是最坏的一种"能跑起来");所有库读
  都走 `_Reader`(PG 侧连接开 `read_only`)。跑前跑后各点一次 `ask_jobs` / `answers` /
  `conversations` / `knowledge_objects` / `retrieval_experiences` 的行数,任何一张对不上
  就标红报错。`ReasoningRetriever.run` 全程唯一的写路径是收尾那次 `note_adopted`
  UPDATE,只在注入开着时可达——rig 另外把 `RETRIEVAL_EXPERIENCE_INJECT_ENABLED` /
  `REASONING_CONSULT_MEMORY_ENABLED` / `AGENT_PROFILE_ENABLED` 全部强制关。
- **不建库、不建图、不合成答案**,所以它跑的是两个**主库里既有的**语料格:
  `A_nokg`(`--source-notebook-a`,那个笔记本本来就没有 knowledge_objects)与 `B_kg`
  (`--source-notebook-b`)。另外两格在主库上不存在,不在枚举里。notebook id 刻意不落
  仓库,只从命令行传。
- **换策略不重启**:`ReasoningRetriever.from_repository(repo, settings)` 读的是传进去的
  那份 Settings,所以 `search` 为 legacy / v2 各构造一份、在同一个进程同一个 repo 上
  交替跑(`--policy` 在这条路上不读)。每个策略的第一个 run 之后当场核对「声明的策略」
  与「轨迹里真的发生了什么」(v2 的判据是 run 产出了 `termination` 事实),对不上直接停。
- **没跑合成的列如实留 unknown**:`anchors` / `included_kg` / `included_chunks` /
  `included_elements` / `citation_contribution` 全部是 `null`。它们由合成阶段写,这条路
  压根没走到那儿——写 0 会被读成「一条证据都没进 prompt」,那是一句关于合成的假话。
- **`intents.jsonl` 不进数据集、不进仓库**。意图契约每题只算一次并缓存在 `--out-dir`
  (默认在 `.local/` 下),纯粹为了一次中断的 rig 重跑时不再付一遍 intent 的模型调用;
  它带着问题原文与模型改写过的 `resolved_question`,是自由文本。进数据集的只有
  `search-<policy>.jsonl`(闭集投影)与 `search-runs.log`(题号/格/策略/档/耗时/reflect
  轮数/终态,无原文)。`--no-intent` 可以跳过这一步,代价是 v2 只剩整题一个方面。
- **`--concurrency N` 默认 1,行为逐字不变**(不经线程池,单线程顺序跑,与加这个
  参数之前完全一样)。`N > 1` 走两阶段:先按同一并发度并发把这批题的意图契约算完
  (`intents.jsonl` 的写入与缓存查询都加了锁,同一题只算一次),再把全部 run(legacy /
  v2 合并进**同一个**线程池,总吞吐最高)提交给 `ThreadPoolExecutor`。每个 run 自建
  一份 `threading.Event()` 取消事件与请求身份的 ContextVar(线程池的 worker 线程不
  继承调用方线程已经 `.set()` 过的 context,必须每次调用内部自己设),不共享
  `ReasoningRetriever`。输出用一把锁保护、每行写完立刻 `flush`,完成顺序可以乱但每行
  仍是一份完整投影;终端进度打印 `done k/N ...`,每个策略**第一个完成**(不一定是第一
  个提交)的 run 之后照旧核对「声明的策略与证据是否一致」,不一致就
  `executor.shutdown(cancel_futures=True)` 撤掉还没开始跑的任务、并给所有在跑的 run
  的取消事件逐个 `.set()`。单个 run 抛异常只记异常类名与题号(不记原文)、隔离后继续
  跑其它 run;`KeyboardInterrupt` / `AskCancelled` 触发全体取消。**真跑前必须确认**
  模型服务的并发限流与(PG 主库时)`POSTGRES_POOL_MAX_SIZE` 都吃得下这个 `N`——PG 连接
  池不够会被 rig 自动 clamp(打印一行 `concurrency clamp` 诊断),SQLite 主库(只在冒烟
  场景出现)真跑时一律强制 clamp 到 1;这两条 clamp 都不影响 `--dry-run`,后者只打印
  请求的并发度,零副作用。

`seed` 走的是真实的浏览器上传面(`multipart/form-data`,`files` + 每文件一对
`doc_types`/`doc_type_explicit`,一批 ≤20 个),上传后轮询 `parse_status` 到终态、再按
需 `kg/build` 并轮询 `index-status`。`--skip-kg` / `--skip-embed` 把后两步关掉,给
「没有模型配置」的冒烟用。fixture 用户名必须是「单个小写字母 + 八位数字」(注册端点的
正则),默认 `t00000001`。worktree 里没有 `.env`,模型配置靠 `--env-file` 指向主 checkout。

题集在 `backend/app/eval/reflect_t0/questions.json`(A 单篇 24 题带 gold、B 多篇 10 题、
报告 4 题),语料是公开论文;A 语料的主库 notebook id 刻意不落仓库,由 `--source-notebook`
传(B 语料的磁盘路径与文件名在题集里,那是本机 MinerU 产物的位置)。

**`prefix-probe`(E1)—— 前缀复用敏感性探针**(设计
`2026-09-09-reflect-prefix-cache-final-design_zh.md` §9.1;实施
`2026-09-11-reflect-prefix-experiments-plan_zh.md` T-EX2–T-EX4)。它回答的是:
同一个 `reasoning_agent` workload、同一段固定正文,两臂只在头/尾标记的
**稳定性**上不同时,墙钟是否有可辨认的差异。**它不回答什么**——逐字:
「不可下结论:命中率 X%、省掉 Y token、provider 已关闭缓存」(design §0/§9.1)。
`prefix-probe` 剥掉了检索、合成、reflect 循环的全部意义,只留一段固定输出
任务上的两臂标记差异,结论词面因此永远限定在「有 / 无可辨认 / 不确定的时间
收益」三格,不产出任何比率型结论。

**`verdict` 判据(实现自定,spec 沉默——design §9.1 与计划 T-EX3 都只给了
「有/无/不确定」三格词面,没给具体阈值)**:① 可配对的区组数(同一
`(tier, block_index)` 里两臂都有观测)< 2 ⇒ `undetermined`——连一次跨区组的
一致性都算不出来;② 否则若区组间中位差的**符号**一致率 ≥ 0.75 且
`disturbed` 中位墙钟比 `stable` 慢 ⇒ `time_benefit`,方向不一致或
`disturbed` 不慢 ⇒ `no_discernible_benefit`;③ 过半非预热行不是 `"ok"`
(失败与本地缓存出口都不算数)⇒ 不看上面两条,直接 `undetermined`——样本已
经被污染到不足以支撑任何结论。

```bash
python scripts/reflect_shadow_rig.py --dry-run --seed 1 prefix-probe
python scripts/reflect_shadow_rig.py --dry-run --seed 1 --smoke prefix-probe
```

**零数据库**:显式给 `--database-url` 会被响亮拒绝
(`ERROR: prefix-probe 不连数据库,不接受 --database-url`)——这条路挂在
`search`/`ab` 任何一条上都会多出一条与语料/范围/意图全无关的死分支。

**标记接缝默认关闭,只在这条命令里打开且退出时必然复位**:`app/core/llm.py`
的实验标记接缝(`ContextVar` + `experiment_message_markers`)在生产路径与
`search`/`ab` 上永远是 `markers=None`,只有 `prefix-probe` 的真跑路径会
`with experiment_message_markers(head, tail):` 包住那一次调用;命令退出后
——不论正常收尾还是异常——接缝都已复位。

**规模**:默认 96 次全量(3 档 × 4 区组 × 2 臂 × 4 次),`--smoke` 走 48 次
冒烟(区组数砍半)——**可先冒烟,但不凭它直接定论**(design §9.1 / U4)。
`--seed` **必填**(缺它退 2:`ERROR: prefix-probe 需要 --seed`)——区组内的
臂序平衡随机与批次可复现都靠它(计划 §1 M4)。

**预热单列**:整批开头一次连接预热,正文与标记均**与主批不同**,单列
`is_warmup=True` 行,不进任何统计(design §9.1「预热成本单列」)。

**两个 dry-run 数字的口径**(与 `ab` 那两行同一套措辞:逻辑调用 vs 请求
上界,乘数是重试预算,静默 fallback 另计、不留日志行):

```
[dry-run] 004 model calls (estimate)  97 次逻辑调用(96 格计划 + 1 预热);请求上界 ≤ 194(……)
```

`--smoke` 时是 `49 次逻辑调用(48 格计划 + 1 预热);请求上界 ≤ 98`。单次
超时读 `REASONING_TIMEOUT_SECONDS_DEFAULT=90s`,**与 `ab` 那一行同一个
常量**(镜像 `Settings().reasoning_timeout_seconds` 的默认值,真跑读的是
构造出来的那一份,可能被 `--env-file` 覆盖)。

**产物文件名**(逐字):`<out-dir>/probe-stable.jsonl` + `probe-disturbed.jsonl`
+ `probe-warmup.jsonl` + `probe-summary.{md,json}` + `manifest.json`(+
`manifests.jsonl` 历史)+ `calls-e1.jsonl`(per-call 表)。

**`gap_ms` 口径**:本格**发起** − 同序列上一格**返回**(真实空闲),首格与
跨序列一律 `None`。

**per-call 表(`calls-e1.jsonl`)对齐规则**:这张表是**无标签**的
(`question_key`/`corpus_cell`/`effort` 这三个 A/B 维度 E1 一个都没有),只能
**按顺序**对齐:E1 全程串行,第 0 行是预热格,其后第 i 行对应
`probe_plan(...)` 的第 `i - 1` 格,也就是 `probe-stable.jsonl`/
`probe-disturbed.jsonl` 按 `series_index`/`call_index` 归并回计划序之后的第
i 格。预算到点提前停批时 per-call 表的行数按实发调用数收窄,前缀仍然对齐
(停的是尾部)。**一旦 E1 改成并发派发,这条规则立刻失效**。

**`calls-e1.jsonl` 每次运行覆盖**,与 `probe-*.jsonl`/`probe-summary.*` 同
寿命(codex #709 R2 P2-2)——同一个 out-dir 换 `--marker-variant` 重跑一次,
这份表只留这一次的调用行,不会把上一次的行留在开头。`ab` 的
`calls-<arm>.jsonl` 不受影响,仍然是追加(并发多单元写同一份表,追加是
唯一安全的写法)。

**`--marker-variant`**(`type=int, default=0`,design §9.1「更换标记值重复
验证」):同一组 `(seed, tier, block, arm, call_index)` 换一个 variant 就拿到
一组全新的标记值,计划形状不变;manifest 的 `matrix.marker_variant` 子键如实
记录这批用的是哪个 variant,拿两批不同 variant 的产物对起来看能分辨一次差异
是不是巧合标记造成的。

**`--sample-file`** 默认指向仓库内合成、非敏感的固定样本
(`backend/app/eval/reflect_t0/prefix_probe_sample.json`);指向操作者
`.local` 下真实样本时用这个,manifest 只记样本的短码摘要(`sample_digest`)与
长度,不记正文。

**`bypass_cache=True`**:disturbed 臂每次标记都不同,`llm_key` 因此逐次不同,
本地响应缓存的命中门本来就不放行(E1 不传 `response_validator`),显式
`bypass_cache=True` 让「这批数一定不含本地缓存出口」成为一条**结构事实**,
并避免 `status="cache_hit"` 这个第四出口污染 E1 的 `status` 分布。命中它
仍是一个「不应出现的计数」,不是一件好事——摘要与退出码走的字段叫
`local_cache_exit_rows`,**不叫** `cache_hit`(命名红线:字段名不许含
`cache_hit`/命中率),真出现时整批以非零退出收尾。

**`invalid_output_rows`**(codex #709 R2 P2-1):`chat_json` 报传输状态
`status="ok"` 只说「provider 回了点什么、没有抛异常」,不说「回的是不是
固定输出任务承诺的那份 `{"ok": true}`」——它自己从不解析、也不校验返回
内容的形状。rig 在每格 `chat_json` 返回后按这份固定形状当场校验一遍(必须
是能 `json.loads` 成的 `dict`,含 `"ok"` 键且值恰好是 `True`;多余的键
放行),不通过就把这一格的 `status` 改记成 `invalid_output`——单列
`invalid_output_rows`,既不算 `failed_row_count`(它不是传输失败),也不
进 `first_observation`/`repeat_observation`/主统计(它没有完成固定输出
任务,墙钟不能当一次已验证的计时观测)。这个数不会单独让退出码非零(它已经
被 `verdict` 的「过半非-ok 行判 undetermined」判据兜住),但非零时 stderr
会单独提示一句。

**`state-probe`(E2)—— 固定状态的真实 reflect 对照**(设计
`2026-09-09-reflect-prefix-cache-final-design_zh.md` §9.2;实施
`2026-09-11-reflect-prefix-experiments-plan_zh.md` T-EX5–T-EX7)。它回答的是:
同一份剧本重放到同一个状态点,四条臂在**这一轮真实转发**上分块方式不同,
是否有可辨认的差异。驱动器把前 k 轮全部按剧本重放(零真实调用,四臂逐格相同),
只在第 k+1 轮把两条消息原样转发给真客户端调一次、把回来的决定**留存**下来,
再向 `run()` 返回一条脚本化的停止决定——**模型选出的动作只记录不执行,这批
数据测的是固定观察下的策略,不是它自洽的真实轨迹**(design §9.2)。

**归因边界(design §5 风险 5,与摘要头部同一措辞,只读 README 也要能抄
到)**:P↔B 的差里混着指令/工具说明的布局改动,不能宣称纯缓存因果;D↔L
在固定状态点上只看得见净增的那一侧(省的那一侧结构上看不见)。

```bash
python scripts/reflect_shadow_rig.py --dry-run --database-url postgresql://h/db_test state-probe
```

**两条前置硬断言**(真跑与 `--dry-run` 下都拦,不放过任何一条):

1. `--database-url` **必须显式给出**——不读 `.env` 猜、不落回 `search`/`ab`
   的默认库(Q7:E2 结构上就不该跑在测试库之外的任何库上)。
2. 库名必须以 `_test` 结尾。库名按 **URL 结构**取(`urlsplit` 的 `path` 最后
   一段,再去掉文件扩展名),不对整串 URL 做 `endswith`:`sqlite:///x_test.db`
   收(库名 `x_test`),`postgresql://u:p@h/prod?application_name=rig_test`
   拒(库名 `prod`,`endswith` 会被这串 query 骗过去连生产库)。这与 `ab` 的
   `AB_TEST_DB_SUFFIX.endswith` 判据在这四行上**分叉**(`ab` 刻意不动,是
   既有资产),分叉本身是已知限制,不是漏洞。

**外加一条逐格运行期断言,不在前置、也不在 `--dry-run` 路径上**:
`assert_optimization_matches_evidence(声明, probe.reflect_optimization())`
在 `run_state_probe_point`(`backend/app/eval/reflect_state_probe.py:1589`)
内部、真实调用模型之前逐臂当场对号(复用 `reflect_ab` 的既有函数)。
`cmd_state_probe` 在 `--dry-run` 下于 `run_state_probe_point` 之前就已返回
(`dry_run: return 0`),这条断言因此**碰不到 `--dry-run` 自检**——只有真实
跑批、真的走到第一格才会触发。上机前想靠 `--dry-run` 排掉「臂声明与
`.env` 实际生效值不符」这一类错配,拦不住,只能等真实调用炸出来。

跑前跑后各点一次测试库自己的只读证据(与 `search`/`ab` 同一把
`_assert_readonly_on_exit`,文案按 `command="state-probe"` 说库)。

**默认三臂**(U2 拍板):`off, prefix_snapshot, prefix_delta`(policy 恒
`v2`;`off` = design §9.2 的 B,当前 v2 snapshot)。`--arms` 给逗号分隔的
`OPTIMIZATIONS` 子集可以再加 `prefix_delta_lean`(L)。**这是一维**——与
`ab`/`reflect_ab.parse_arms` 那套 `policy:optimization` 两维解析器是同一个
`--arms` 参数、两套读法,互不复用。

**`--repeats` 默认 2**(design §9.2 的公式:12 例 × 3 状态点 × 3 臂 × 2 重复 =
216),**与 `ab` 的默认 3 不同,是 E2 专属**。「有没有显式给」靠 argparse 的
`None` 哨兵而不是扫 `sys.argv`(`--repeat 5` 这种无歧义前缀缩写也要生效)。

**`--concurrency` 显式给且 ≠ 1 一律响亮拒绝、退出 2,不降级**:E2 每条 run
只有一次真实调用,并发只会污染排队时长。

**`--only-case`/`--limit` 先筛后限**(与 `search` 的既有口径同款):先按
`case_key` 集合筛,再按数量截断。

**枚举顺序 `case → state_point → repeat → arm`,臂在最内层**(design §9.2
「四臂背靠背站在同一状态点上」,块内的 provider 状态差异最小)。**块内臂序
按重复轮号奇偶交替正/反序**——固定臂序会把块内的单调漂移(预热、限流退避、
连接池升温)整份压在最后一条臂上,交替是确定性的(没有随机、没有种子)。
manifest 的 `order` 字段从真实计划里读出来,如实带一段
`alt_by_repeat_parity`(观察到交替)或 `alt_unobserved`(只有一条臂或一轮
重复,交替在这一批里观察不到)的后缀;`arm_order_seed` 恒 `None`。

**dry-run 打印的数字**(clean env 实测,逐字):

```
model calls (real, total)  216(= 12 例 × 3 状态点 × 3 臂 × 2 重复;每格恰好一次真实转发,不是估计)
embedding calls (separate, upper bound)  ≤ 1254(上界,含首轮种子检索:种子侧 612 + 剧本侧 642;……)
request ceiling  ≤ 432(重试预算 ×2)
timeout (per call)  90s
```

`--arms` 加 L(四臂)⇒ 288 次逻辑调用;`--limit 6`(默认三臂)⇒ 108 次逻辑
调用、embedding 上界 ≤ 642(种子侧 306 + 剧本侧 336)。**embedding 调用单列
一行且含首轮种子检索**——`run()` 拿到非空 `intent_queries` 先跑一轮种子检索
(零模型调用,种子数从冻结契约确定性算出),这一轮与状态点无关、每格都要
重跑一次,漏计它会在批跑到一半时 embedding 配额耗尽(E2 没有按格续跑的
平衡机制)。三个数字都是**上界**而不是精确值:枚举/精确查找类动作未必真的
触发 embedding,请求内相同查询串也只算一次。单次超时读
`REASONING_TIMEOUT_SECONDS_DEFAULT=90s`,与 `ab`/`prefix-probe` 同一个常量。

**产物**(逐字):`<out-dir>/state-probe-<arm>.jsonl`(逐格投影行)+
`state-probe-summary.{md,json}`(分档摘要)+ `calls-<arm>.jsonl`(per-call
表,标签是 `CALL_TAG_KEYS` 六列——`arm`/`optimization`/`question_key`/
`corpus_cell`/`effort`/`repeat`,E2 全部填了值——E2 恒串行,臂这一维可信;
`question_key` 与 case 一一对应,case 那一维不丢,但**状态点这一维在这张表
里表达不出来**,格内三个状态点只能靠文件里的**行序**区分)+
`manifest.json`(最新,覆盖)+ `manifests.jsonl`(历史,追加——与 E1/E3
共用同一个写点函数)+ `raw/<case>-<point>-<arm>-<repeat>.json`(**只进
`.local/raw`,不进数据集/不进仓库**:转发轮与前 k 轮剧本轮的消息/
`schema_hint`/模型决定原文全文、加 `forwarded.stats`)。

**行闭集要点**:`message_prefix_bytes` **恒为 `None`**(`assert_probe_row_closed`
硬断言这一格必须缺席或为 `None`)——它量的是「上一轮 → 这一轮」的公共前缀,
而 E2 每条 run 只在一轮上调真实模型,拿它当基准算出来的字节数回答的是另一
个问题,不是这条 run 的事实;更不能拿同 case 同臂内相邻状态点的差充当它,
那是两条各自独立的 run。`compaction_boundary_reached` 是**三值**:`None`
= 这条臂压根没有这个观测(`off`/`prefix_snapshot` 下 `context_rebuilds`
缺席),`False` = 量到了、重建次数是 0(第三个状态点没有落在压缩边界之后,
如实标、不调剧本去凑),`True` = 量到了、至少重建过一次;unknown ≠ False。
`state_point` 存的是**序号 0/1/2**,不是轮号——三个状态点在报告里按序号对
`initial`/`follow_up`/`compaction_boundary` 三档,轮号在 12 例间不可比,
要轮号用 `case.state_point_turn(i)` 另取。

**摘要顶层除 `by_state_point`(按档分格的主统计)外还有六个键**:
`rows_total`(总行数)、`failed_rows`(取自
`PROBE_FAILED_STATUSES={cancelled,error}`)、`local_cache_exit_rows`
(本地响应缓存出口——转发时显式 `bypass_cache=True`,结构上该是 0,字段名
**不叫** `cache_hit`,命名红线)、`status_unknown_rows`(`status` 本身缺席
的行数)、`compaction_boundary_not_reached_rows`(第三个状态点确实没越过
压缩边界的格数,如实标、不调剧本去凑)、`compaction_boundary_unknown_rows`
(`compaction_boundary_reached` 三值里 `None` 那一格的计数,unknown ≠
`False`)——这五个计数键都**不混进 `by_state_point` 的主统计**。
`by_state_point` **按 `initial`/`follow_up`/`compaction_boundary` 三档分别
报告**(按 `STATE_POINT_LABELS` 顺序而非字母序),每档再逐臂给一份 cell 级
计数与中位数(零比率):`n_rows`/`n_ok`/`n_failed`/`n_local_cache_exit`/
`n_status_unknown`/`n_boundary_reached`/`n_boundary_not_reached`/
`n_boundary_unknown`,以及 `n_decision_absent`(转发失败或返回的不是 JSON
对象,压根没载荷可读)与 `n_decision_unreadable_action`(载荷读出来了但
`next_action` 读不成短码)——是两件不同的事,各自单列;数值列另附
`{key}_p50`(中位数)与 `{key}_n`(参与中位数的样本数)。

**失败语义**:格内一次真实调用失败(provider 报错/取消)落一行
`status="error"` 并继续跑批,与成功行放进同一张按臂分组的表——这与「剧本
没写对」是两件事。遇到 `StateProbeError`(继承 `BaseException`,「这一格的
剧本/编排没对上『同一状态点』这句话」)或裸 `ValueError`/`TypeError`
(键改名、参数形状不对,同样是「剧本没写对」而不是「这次调用失败」)一律
**当场停批、非零退出、不写 manifest**——已经 flush 过的行仍留在
`state-probe-<arm>.jsonl` 里,不因为进程终止而丢。其余 `Exception` 才按格
隔离继续跑。

**`settings_overrides` 由探针自己套**:在 `run_state_probe_point` 里对
`settings_for_arm` 的一份**副本**生效(建检索器之前),白名单只收两个检索
上限键(`reasoning_max_element_searches`/`reasoning_max_chunk_searches`),
渲染预算键(`reasoning_reflect_state_chars` 等)任何 case 都不许覆盖——调小
它们去逼出压缩边界与调剧本去凑是同一件事的两种写法。

**`--env-file` 必须早于案例集加载生效**:`load_state_probe_case_set()` 自己
要 `from app.core.config import Settings`,而 `app.core.config` 在
**import 时**就把 `SILICON_NOTEBOOK_ENV_FILE` 读成模块级常量——真跑时 rig
在加载案例集之前先把 `--env-file` 等环境变量装进本进程,否则整批会静默跑在
当前 checkout 的 `.env` 上。

**`STATE_PROBE_EFFORT="standard"` 固定**,并在跑批前核一次:每例最后一个
状态点的「第 k+1 轮」不能越过这个档位 `ask_retrieval_limits` 的
`max_reasoning_steps` 上限,越界的例子在第一个模型调用之前就说清是哪一例、
差在哪。

**12 例覆盖九种形态**(`probe_shape` 闭集:`single_fact`/`complex_condition`/
`graph_in_scope`/`no_graph`/`roster`/`large_collection`/`zero_hit`/
`repeat_request`/`tool_exhausted`,其中单事实/复杂条件/工具耗尽各有
`A_nokg`/`B_kg` 两例)。

**已知限制**:

* **图动作缺口**:剧本里不允许出现 `expand_graph`/`follow_chain`/
  `ppr_retrieve`——它们的必填参数是候选池里的 `object_id`,驱动器只看得见
  渲染后的文本,给不出一个真实候选 id(§9.2 里点名的「图动作」形态因此只能
  靠语料格承载「图在范围内」,不是真的执行一次图动作)。
* **臂序按重复轮号奇偶交替,不是随机**:是对设计 §9.2「随机化」的实现口径
  收窄,manifest `order` 如实带 `alt_by_repeat_parity`/`alt_unobserved`
  后缀;归因边界写在 `state-probe-summary.md` 的「归因边界」段——单数轮的
  批(`--repeats 1`)没有那一半抵消,`call_wall_ms_p50` 的臂间差里仍混着
  块内漂移。是否要给 E2 加臂序种子交用户拍板。
* **E2 没有整批墙钟预算**:manifest 的 `stopped_by_budget` 恒写 `False`
  (E1/E2 本期都不实施掐停逻辑),这与 E3(`ab`)的 `--max-wall-minutes`
  不是同一件事。
* **per-call 表(`calls-<arm>.jsonl`)没有 `state_point` 标签**:格内三个
  状态点只能靠行序对齐,一旦读表脚本改动写入顺序这条对齐规则就会失效。
* **`_test` 判据与 `ab` 分叉**:`state-probe` 按 URL path 段(去扩展名)
  判库名,`ab` 的 `AB_TEST_DB_SUFFIX.endswith` 判据不动——两条通道对同一个
  `--database-url` 字符串可能给出不同的「是不是测试库」结论。

**报告第一页只答四件事**(设计 §13,PR-5 T-EX11)。不论哪一条通道,聚合报告的
**第一页**只回答这四件事:(1)最终候选是哪种模式;(2)完整 Ask 比基线快多少;
(3)质量/失败/尾部是否退化;(4)结论在哪些模型配置和题型上成立。E1 机制结果
(`prefix-probe` 的摘要)**只作解释,不代替端到端结论**——它能说的最多是
「稳定前缀有 / 无可辨认的时间收益」,不能替 E3(`ab`)的完整 Ask 配对下结论。
默认 `REASONING_REFLECT_OPTIMIZATION=off` 与「任一质量或稳定性回归可立即回到
`off`、不做数据迁移」这两条既有承诺不变;三条通道的实际收益**一个数都还没
量**,本节交付的是可重跑命令与产物形状,不是采用结论。

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
`--no-samples` 只出数字:概念名/命题/公式原文、笔记本名称、以及自定义 `object_type`
(knowhow 投影用的是**用户列名**)一律不打印。

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
  一个都不会被抽掉,单个来源也可能自己就有几百万对象。触到上限会当场声明,绝不静默。
- **触顶就会有偏,两种模式都一样**:截断留下的是库内顺序前缀而非随机样本;`--sources K`
  只随机化了**来源**,截断仍必然落在某个来源中途(一个来源自己超过上限时尤其明显)。
  两条路径触顶时都会打同一句偏差警告。让结论无偏的唯一办法是**别触顶**:调大
  `--max-objects`,或用更小的 `--sources K` 让抽中的来源都能读完。刻意不做蓄水池 /
  `ORDER BY RANDOM` —— 那要把全表 payload 扫一遍,在千万级目标库上是几十分钟到几小时,
  会让「先抽样快速看一眼」这唯一的使用姿势不成立。取舍是**不消除偏差、但绝不隐瞒
  偏差**,有反向护栏钉住那句声明必须出现。
- **无名/坏 payload 的行照样进分母**:它们恰恰是要暴露的低质量行,单独报数而不是从
  统计里悄悄剔掉。
- **连通性只认本 notebook 的可用对端**:关系的端点列既无外键也无 notebook 归属约束,
  跨库的野边不进产品图,也不计入这里的度数。
- **抽样绝不静默**:每一节都标注口径;DF(文档频次)在抽样下被系统性低估,报告里有明说。
  `--sources` / `--degree-sample` 拒绝负数(手滑写成 `-1` 会被当成「全量」而在大库上
  静默全扫),在打开库之前就报错。
- **全量是真全量**:`--sources 0` 按 notebook 直查、不经 `sources` 表,所以挂来源的、
  不挂来源的(晋升 / Memory→KG 刻意写 `source_id=''`)、以及 `source_id` 指向已删来源的
  孤儿对象(该列无外键约束)一并覆盖。抽样模式走 `sources` 表,后两类抽不到 —— 报告会
  报出它们的数量并说明未纳入。晋升为主的 base 库尤其要看这一段,否则会出现「构成几十
  万行、内容分析近乎空」而看不出原因。
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
