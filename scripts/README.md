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
才动手;在线模式只清孤儿行(有界事务)与 scale 三根(逐本取跨进程排它
claim,被占跳过留声),`notebooks`/`assets` 两根的删除必须
`--confirm-service-stopped` 停服窗口执行(在线只报告不删;时间不是锁),
停服下年龄闸 `--min-age-seconds` 是防「没停干净」的皮带。两后端都支持;
盘点/复核是全表扫,建议低峰执行。

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
