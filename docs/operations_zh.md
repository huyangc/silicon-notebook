# 运维、诊断与摄取工具

[返回 README](../README_zh.md) · [English](./operations.md)

本文覆盖日志、线上诊断、MinerU、离线摄取、检索回放、迁移与回填。按脚本查找的速查入口另见 [scripts/README.md](../scripts/README.md)。

## 可观测性 / 日志

后端通过统一的 `EventLogger`（`app/core/event_logging.py`）输出结构化日志：每条事件一行 JSONL 写入 `.local/logs/`，并附控制台简要行。写日志是 best-effort，绝不影响它所观测的请求或管线；未配置模型时 LLM 通道为 no-op。

- `requests.jsonl` — 每个 HTTP 请求（方法、路径、状态码、耗时、`request_id`）。超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 关联前后端。
- `events.jsonl` — 异步来源管线：各阶段（`parse` / `embed` / `extract`）耗时与每次状态机跃迁。卡住时能看到当前阶段及已运行时长；失败记录真实异常（以及来源的 `error_message`）。
- `llm.jsonl` — 每次大模型调用：chat（prompt/响应/token/耗时，按 `LLM_LOG_MAX_CHARS` 截断）、embedding（仅摘要，不存原始向量）、以及 deterministic fallback 容易让人忽略的错误。

浏览器 DevTools console 会镜像请求为 `[api] 方法 /路径 -> 状态 N毫秒 (request_id)`；轮询期间 UI 显示当前阶段/已用时长，失败时点名是哪个来源。来源的 `error_message` 由后端写成 Python 异常字符串，因此进 console 而不上屏。

错误信息按受众分流。用户看到的一律是中文：前端把 HTTP 状态码映射成人话（「没有权限进行这个操作」「没找到，可能已被删除」），裸状态码和后端异常原文都不会出现在界面上。**除非后端明确声明「这句是写给用户的」，否则一概不原样展示**——API 会给这类响应打上 `X-User-Message` 头，只有它们才透传（如「用户名已被占用」，比泛化文案更具体）。其余一律泛化，**包括恰好是中文的后端文本**：像「解析失败：不支持的文件类型」这种串，同样可能是一条原始异常，光看内容分不出来。5xx 无论有没有标记都泛化，避免内部错误外泄。压根没产生 HTTP 响应的失败（断连、后端没起来、藏在流式事件、后台任务记录、失败的报告、来源解析失败里的错误串）走同一条规则，不会把原文直接印出来。

开发者与 MCP agent 看到的东西不变：后端 `detail` 在 API 响应和日志里保持原样，而完整诊断——状态码、状态文本、原始响应正文、以及能和 `requests.jsonl` 对上的 `X-Request-Id`——在每次请求失败时写进 DevTools console；凡是被界面换成泛化文案的错误，其原文也一并进 console。所以「它说我没权限」这类问题靠 console 里的 request id 定位，而不是猜是哪道校验拒的。

### 生产事故即时采集

在 Ubuntu 24.04 上通过 `npm run start` 启动的部署中，SSH 到主机，在**卡顿仍在发生时**
从仓库根执行主命令：

```bash
ssh <production-host>
cd <silicon-notebook-repository>
python3 scripts/diag.py incident
```

正常的单 Uvicorn worker 会自动发现。若报告显示进程发现为 missing、ambiguous 或
incomplete，请从服务管理器或主机监听信息取得**仍在运行**的后端 PID，然后重试；不要重启：

```bash
python3 scripts/diag.py incident --pid <backend-pid>
```

默认结果是一段可整体复制、最多 **32 KiB** 的 UTF-8 文本。所有采集共享一个最长 10 秒
的总截止时间；进程采样、两次线程栈、loopback 健康探测、有界历史日志读取，以及自身最多
一秒的 DB 探测都消耗同一个时间预算。后端把 `SIGUSR1` 注册为**不终止进程**的全 Python
线程 faulthandler dump；它只采线程栈，不采任何局部变量值，成功采集后后端继续存活。

运行态心跳每两秒原子写入 `.local/diagnostics/runtime.json`；超过六秒即判 stale，活跃工作
字段不会参与高置信结论。线程栈采集使用 `.local/diagnostics/incident.lock`，追加到
`.local/diagnostics/thread-dumps.log`；一次成功采集后 dump 文件保持在 8 MiB 内。只读 DB
分析使用 `.local/diagnostics/db-snapshots/` 下的有界临时快照。采集器只允许创建、替换或
截断这些诊断工件。运行时只接受当前用户控制的 `0700` diagnostics 目录，以及同一用户拥有、
单硬链接、普通文件类型的 `0600` heartbeat/dump 文件；已有路径不安全或目录路径被替换时，
诊断降级且不会跟随链接或截断敌对目标。

按以下顺序解释输出：

- `Confidence-ranked diagnoses` 最多列三个确定性规则生成的假设。`high` / `medium` / `low`
  表示证据强度，不等于确定性；单个弱信号不会被宣称为根因。
- `Observations`、`Relevant stacks`、`Database and host signals`、`Log metadata` 给出排序所用
  的元数据证据链；`Safe next commands/actions` 只建议下一步检查，不执行修复。
- `Missing/degraded evidence` 是正式结果而非被隐藏的错误。snapshot stale 通常说明采集太晚；
  PID 缺失或歧义时用上面的 `--pid` 重试。DB busy/locked、权限不足、deadline、损坏或 malformed
  日志、信号路径不可用、进程/文件发生竞态时，对应证据会被排除，其余采集继续。
- 空闲部署可能正确报告没有多信号结论达到有效置信度。应在操作肉眼可见地卡住时重跑，不能用
  空闲采集臆造根因。

可复制输出绝不打印原始不透明 id：允许出现的 notebook/request/job 引用会一致地映射为假名，
其它原始 id 直接省略。它也绝不打印用户控制的原始文件名、request body、来源正文、Ask 问题/回答、prompt 或模型消息、Memory/Knowhow 内容、SQL
文本或参数、authorization header、cookie、token、secret、原始命令行或局部变量。即使输出已
脱敏，分享给可信团队之外的人之前仍必须人工复核。

`incident` 不需要 root 或第三方 Python 包，不 import `app`，也不会重启或终止进程。七个诊断
命令对应用数据都只读：不执行 delete/其它业务写入，不做 SQLite checkpoint/vacuum/analyze/reindex，
不跑 migration，也不自动修复。`incident` 仅可按上述约束维护有界的
`.local/diagnostics/` 工件。

### 七命令诊断速查

`scripts/diag.py` 只提供以下七个命令：

| 命令 | 用途 | 运行边界 |
| --- | --- | --- |
| `python3 scripts/diag.py incident` | 首选的线上即时有界采集；自动发现不能唯一选中 worker 时加 `--pid <backend-pid>`。 | Ubuntu/Linux 活体进程证据；纯 stdlib，不 import app。 |
| `python3 scripts/diag.py slow --since 24 --deep` | 从历史日志、DB 聚合与 scale-index manifest 分析慢路径；`--deep` 会增加可能耗时数分钟的只读 DB 检查。裸跑 `python3 scripts/diag.py` 仍等于 `slow`。 | 离线、纯 stdlib，不 import app。 |
| `python3 scripts/diag.py latency --last 500` | 从 `ask_stage` 事件统计各 Ask 阶段 P50/P95/max。 | 离线、纯 stdlib，不 import app。 |
| `python3 scripts/diag.py locks --top 20` | 从 `db_write_lock_slow` / `db_write_lock_stats` 事件按调用点聚合 SQLite 写锁争用。 | 离线、纯 stdlib，不 import app。 |
| `python3 scripts/diag.py open --local .local` | 分析打开笔记本的查询/端点耗时、缓存冷成本与 mutation-sequence churn。 | 离线、纯 stdlib，不 import app。 |
| `python3 scripts/diag.py db --db .local/silicon_notebook.db` | 有界、源端无副作用地采集 SQLite/WAL/表/FK 索引/query plan 证据。 | 离线、纯 stdlib，不 import app。 |
| `python3 scripts/diag.py base-recall [active_notebook_id] --db .local/silicon_notebook.db` | 仅用元数据诊断挂载 base 的可用性与最近报告的 tier 引用计数。 | 有界、源端无副作用的 SQLite 快照；纯 stdlib、不 import app；不执行检索、不回显查询/正文、不构造 repository、不迁移、不用 SQLite 打开源库。 |

`base-recall` 与 `db` 共用 `O_NOATIME` pin、非阻塞锁、文件身份复核的 DB/WAL 拷贝，只在自己
拥有的快照上执行固定聚合投影。安全边界不可用时只输出 category-only 降级信息，绝不回退为活体
SQLite 连接。单段 UTF-8 报告最多 32 KiB，只含计数、固定状态标签和本次报告内假名；不包含原始
notebook/user/report/object/chunk id、标题、问题、正文、文件名、路径、异常、凭据或 secret。

历史读取器会覆盖、去重并限制 `requests`、`events`、`llm` 三通道的全部支持布局：legacy
`<channel>.jsonl`、daily `<channel>-YYYY-MM-DD.jsonl`、daily gzip
`<channel>-YYYY-MM-DD.jsonl.gz`，以及下一层 per-user 日志目录。malformed 行和字节/时间窗口截断
会作为 degraded metadata 报告。既有独立引擎脚本继续可用于存量运维笔记与 cron；新操作优先走
这个七命令统一入口。

`python3 scripts/diag.py locks [--log PATH] [--top N]` —— 从 `events.jsonl` 按调用点聚合
SQLite 写锁争用。`wait` 是写者排队等锁的时长（用户感知为「页面卡住」），`hold` 是持锁时长
（谁害的）。按 `hold_max` 降序，最该改的排最前。输出两张表：超阈值违规
（`db_write_lock_slow`，按 site 限流，只见「尾巴」）与周期性全量快照
（`db_write_lock_stats`，不做阈值过滤，但只是某一时刻的累计快照）——某个调用点即使从未
超阈值也可能很忙，这种情况只有第二张表能看见。采集阈值由 `DB_WRITE_LOCK_WARN_MS` 控制
（默认 200）。

**日志可视化页面 — `/dev/logs`。** 针对上述 JSONL 通道的只读 debug 页面（v1 聚焦 LLM 通道）。左侧列表可按 kind / status / model 过滤并全文搜索；详情区完整展示发给 LLM 的内容（`system` / `user` 消息与 `schema_hint`）以及模型回复、token 用量、耗时。由门控的后端接口 `/api/debug/logs/...` 提供，需显式设置 `DEBUG_LOGS_ENABLED=true` 才会开启（默认关闭——完整 LLM 记录可能包含私有来源材料）。

## SQLite / PostgreSQL 切换与回滚

运行时只有一个 active database，应用不做 dual-write。仓库内 importer 只提供单向
SQLite→PostgreSQL 快照迁移；只改 `DATABASE_URL` 只会打开另一份数据存储。它不迁 MySQL、
不持续捕获后续写入、不回放 PostgreSQL→SQLite，也不复制 source/upload/asset 文件。

本节是「为什么这么设计、拒绝做什么」的真源。要按步骤执行（分阶段、每步判据、必须由人
拍板的节点、以及那些本就应该失败的失败），走
[docs/postgres-migration-runbook.md](postgres-migration-runbook.md)；两者冲突时以本节为准。

### 1. 准备空目标并预检

创建专用的 UTF-8 PostgreSQL 数据库。不要指向已有应用库：任一业务表有行都会 fail closed。
URL 通过环境变量传入，不放进命令参数：

```bash
export POSTGRES_MIGRATION_URL='postgresql://USER:PASSWORD@HOST:5432/EMPTY_DB'

python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db
```

默认只做只读 preflight：检查 SQLite 身份/schema 与 PostgreSQL UTF-8、current schema、空库和
migration ledger，然后退出；不会创建快照或写目标。目标用户还必须能在 `public` 安装/使用 `pg_trgm`。

工作目录要按**源库大小的两倍**准备，不是一倍；演练与正式切换共用同一目录时要三倍。密封快照在
整个导入期间常驻；源库 schema 落后于代码时还会另拷一份完整升级工作副本；而激活阶段会**无条件**
再生成一份完整快照作为停写锚点，它先完整写成临时文件、**之后**才按 hash 与密封快照判重，所以
即使内容完全相同，峰值也确实是两份。演练是在 SQLite 仍在线时做的，到正式窗口时源库通常已经变了，
两次的密封快照 hash 不同、文件名不同，会同时留在盘上。500GB 的源库因此是：正式窗口用独立目录
需要 1TB，与演练共用需要 1.5TB；建议演练用单独目录，并在窗口开始前确认它已归档或删除。
按 1× 准备会在成功导入数小时之后、激活那一步失败。目标库要分开估：PostgreSQL 的数据加索引通常大于 SQLite 文件，重建索引还需
额外临时空间，应采用演练实测值（`SELECT pg_size_pretty(pg_database_size(current_database()));`），
不要用 SQLite 文件大小推。

### 2. SQLite 在线时先做演练

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db \
  --work-dir /protected/path/postgres-migration \
  --apply
```

工具使用 SQLite backup API，能包含已提交 WAL，不会错误地裸复制 live `.db`。它只升级私有
快照副本，按 FK 顺序和有界 batch 流式 COPY，保留历史 ordinal，把旧 JSON 向量转换为
float32 `bytea`；PostgreSQL 无法表示的 NUL codepoint 会规整为字面文本 `\\u0000`(单向规整——PostgreSQL 文本/JSON 无法存 NUL)。随后
逐表做内容 checksum，并把每张已校验的表连同一条 per-table checkpoint 一起提交。run 头绑定
sealed snapshot hash，使中断的导入能从最后完成的表续跑，而不是整体重来；finalize（ordinal
reseed、重建索引、`ANALYZE`）是幂等的。这里**刻意用整体单事务原子性换取可续跑**——最终激活阶段
会在任何切换前重算每张表的 checksum。无凭据 receipt 记录每表数量/checksum、全部 NUL
normalization 与所用调优。退役 SQLite 表为空时只记录不复制；只要其中一张非空就拒绝迁移，
绝不静默丢历史数据。

若导入中途停止（崩溃、远程连接断开、机器重启），重跑同一条命令即可:copier 会复用绑定到同一停写
源的 checkpoint，跳过已提交的表继续。要避免每次重试都重新快照数 GB 源库，显式传入本工具生成的
sealed snapshot：

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source /absolute/path/to/.local/silicon_notebook.db \
  --work-dir /protected/path/postgres-migration \
  --snapshot /protected/path/postgres-migration/sqlite-vNN-HASH.snapshot.db \
  --apply
```

工具会重新验证目录、文件名、SHA-256 前缀、`quick_check`、schema 版本以及不存在 WAL/SHM；
快照 hash 必须与已记录的 run 一致;快照记录的**源身份**也必须与所选 `--source` 一致——复用一个
从**别的库**封出的快照(或缺少源身份记录的快照)会 fail closed,而不会把错的数据导进去。不要
因为旧演练成功就拿其 `--snapshot` 做正式切换:快照之后的 SQLite commit 不在里面。

### 2a. 大库的调优与前置条件

源库很大时，吞吐与可靠性由几个杠杆主导：

- **把 CLI 跑在 PostgreSQL 本机（或同一高速网络内）。** 每次 COPY 与读回校验都过连接；远程链路
  会把整份数据来回搬两遍。
- **抬高建索引预算。** 在已装满的大表上重建索引是 finalize 最慢的一步。传
  `--maintenance-work-mem 2GB`（或在主机内存允许下更大）和 `--max-parallel-index-workers N`；
  二者都是会话级、只影响本次导入。
- **会话级批量装载设置。** 导入用一条独占连接跑，带 `synchronous_commit=off`、
  `statement_timeout=0`、`idle_in_transaction_session_timeout=0`（可用 `--keep-synchronous-commit`
  关掉第一项）。确认服务端没有会掐断数小时长拷贝的全局 `statement_timeout`/
  `idle_in_transaction_session_timeout`，长连接有 TCP keepalive，`pg_wal` 有空间——大库装载在
  checkpoint 回收 WAL 前会累积大量 WAL。
- **先量后切。** 正式切换前，在目标主机上用生产数据的副本实跑一次 `--apply` 量真实吞吐。逐表进度
  行（`COPY i/N`、`VERIFY i/N`、`INDEX i/N`）能看出时间花在哪，续跑对复用的表会打
  `SKIP i/N ...（checkpointed）`。
- **`--batch-rows`** 界定 SQLite 取数 / COPY 的批大小（默认 1000）；窄表可调大，超宽行压内存则调小。
- **`--source-timezone`** 指定把旧的 *naive* SQLite 时间戳按哪个时区解读再转 UTC。默认取导入
  主机的本地时区——只有当导入运行在与 SQLite 部署同时区的主机上才正确。若把导入跑在不同时区
  的 PostgreSQL 主机上,必须显式给出 SQLite 主机的 IANA 时区(如 `--source-timezone
  Asia/Shanghai`),否则每个 naive 时间都会平移一个偏移量。所用时区会记入 receipt。
- **正式激活要以部署 env 文件的属主(或 root)身份运行。** 激活把凭据 `.env` 写成 `0600`;当它以
  与后端服务账号**不同**的用户运行时,会把文件属主恢复为原属主以保证服务仍能读取,若没有权限则
  fail-closed,而不是切换后把服务锁在门外。

### 3. 正式从 SQLite 切到 PostgreSQL

1. 公告维护窗口。停止所有 API、后台 worker、会写数据的 MCP 客户端、batch/maintenance 和
   scheduler；等进行中写入结束后停止后端。
2. 创建一个**新的空最终 PostgreSQL 数据库**。演练目标保存的是旧时间点，会被工具主动拒绝。
   同时按部署策略完成 SQLite、PostgreSQL 常规备份与恢复演练。
3. 对已停止的 SQLite 源重新执行 preview，然后用一个命令完成最终迁移和本地配置原子激活
   （所有路径必须是绝对路径）：

   ```bash
   python scripts/migrate_sqlite_to_postgres.py \
     --source /absolute/path/to/.local/silicon_notebook.db \
     --work-dir /protected/path/postgres-migration \
     --apply \
     --activate-env /absolute/path/to/.env \
     --confirm-service-stopped
   ```

   激活阶段会再次生成 SQLite 快照并重算 PostgreSQL 每张表的 checksum，全部匹配 receipt 后才
   原子替换 `.env`；不匹配时 `.env` 保持不变。大目标上可加 `--fast-activation` 只跳过这第二遍
   PostgreSQL 全表 checksum（导入时已逐表校验并落 checkpoint），而 SQLite 源重新快照（证明没有
   写入偷偷混入的切换锚点）与 schema/清单校验仍会执行。保留最终 sealed snapshot、receipt 和权限受限的
   `.env.pre-postgres-*.bak`。确认新部署仍能访问同一个 `SILICON_NOTEBOOK_STORAGE_DIR`；若换主机，
   单独复制并校验这个目录，因为 DB importer 不复制文件。已经完成迁移且 SQLite 此后一直停写时，
   可用 `--activation-receipt /absolute/path/migration-*.receipt.json` 代替 `--apply`，仍会执行相同
   的全量校验。CLI 把 PostgreSQL 写成唯一 `DATABASE_URL`，旧 SQLite URL 只保存为惰性的
   `SHADOW_DATABASE_URL`；CLI 不负责停止或启动进程。
4. 重启后端，部署仍固定 `--workers 1`。receipt 校验和启动之间不要再手工改 selector。
5. 放流量前要求 `curl -fsS http://127.0.0.1:8000/api/ready` 返回 `"ready": true`；分别用 admin
   和普通用户登录；按 receipt 核对 notebook/source 数量；检查搜索和 Ask/Knowledge/Memory/
   Knowhow/report 代表性读取与引用文件；最后做一次明确批准的 canary 写入并等其后台任务结束。
   全部通过后才恢复流量。

### 4. 安全切回 SQLite

- PostgreSQL 尚未接受任何业务写入时，可以停后端、把 `DATABASE_URL` 恢复成 SQLite、以单 worker
  启动并重复 readiness/认证/数量/读取 smoke。
- 注意这条边界的实际位置。**启动后端必然写入**，与数据内容无关：`_initialize()`
  （`postgres/bundle.py`）每次启动都用新盐重算 admin 密码哈希，并无条件 `UPDATE` 内置的
  `user-local` 行。另有两类取决于数据：`recover_interrupted_jobs()` 在 readiness 之前把遗留的
  `ask_jobs`/`merge_review_jobs`/`extraction_runs`/`kg_build_jobs` running 行、`knowhow_rows` 的
  syncing/pending、`sources` 的 extracting/queued/parsing 收敛到终态并清空两张 KG scratch 表；
  `_reproject_legacy_knowhow_tables()` 在 `mark_ready()` **之后**运行，对仍带旧版固定 KO 的
  knowhow 表调度后台 cell 级重投影，**会替换 KG 对象**。所以**「可证明零写入」的回滚必须在
  第一次启动 PostgreSQL 之前决定**——不存在「已启动但没动过」的状态。bootstrap 与恢复性写入
  回滚无实质损失（SQLite 端启动做同样的事），但遗留 knowhow 重投影属于业务数据变更。
  再往后，登录成功会经 `create_session()` 写 `auth_sessions`，此后回滚丢的是会话（重新登录即可）。
- 第一次 PG 业务写入之后，直接改 URL 会丢这次及后续写入。仓库没有 reverse importer 或
  dual-write log；必须再次停写，在外部完成 PostgreSQL→SQLite 对账（包括 storage 副作用），
  验证两端后才能恢复 SQLite。若这套流程没有设计并演练，PostgreSQL 就是回滚边界。
- 开发时反复切 URL 只是选择两条彼此独立的历史，任何一边都不会自动同步。选择 PostgreSQL 时
  不得运行 SQLite-only maintenance 或 `scripts/batch_ingest.py` 的变更阶段。

## 用 MinerU 解析 PDF

PDF 解析与 GPU 解耦：后端本身不引入 torch，只有在配置 MinerU 时才调用它，否则回退到 pypdf 纯文本。

- **本机 / 无 GPU**：保持 `MINERU_MODE=off`，PDF 走 pypdf（仅纯文本）。
- **GPU 部署机（推荐 HTTP 服务）**：把 MinerU 作为独立服务运行，让后端指向它：

  ```bash
  pip install -U "mineru[all]"      # 在 GPU 机器上
  mineru-api --host 0.0.0.0 --port 8000
  ```

  然后在后端设置：

  ```text
  MINERU_MODE=http
  MINERU_API_URL=http://<gpu-host>:8000
  MINERU_BACKEND=pipeline
  MINERU_FORMULA_ENABLE=true
  MINERU_TABLE_ENABLE=true
  MINERU_TIMEOUT_SECONDS=600
  ```

- **同机 Python API**：如果 `mineru` Python 包与后端装在同一台机器，可改用 `MINERU_MODE=cli`（无需 `MINERU_API_URL`）。这个模式会在隔离子进程里调用 `mineru.cli.common.do_parse/read_fn`，不会调用 `mineru` shell 命令；因为部分 MinerU 版本的 CLI 会自行拉起本地 API server，长文档场景下更容易卡住。

- **远端 VLM 推理服务器**：若只想把 VLM 模型卸载到一台独立的 vllm/sglang 服务器（而非整套 `mineru-api`），用 client 后端并指向该服务器：

  ```text
  MINERU_BACKEND=vlm-http-client        # 或 vlm-sglang-client
  MINERU_VLM_SERVER_URL=http://<vlm-host>:30000
  ```

  `http` 与 `cli` 两种模式都生效；非 client 后端会忽略该 URL。

- **Apple Silicon 本地（MLX，离线）**：Apple Silicon 的 Mac 没有 NVIDIA GPU，但可用 MLX 加速 MinerU，因此本地也能跑同质的高保真解析：

  ```bash
  python -m pip install -U "mineru[core]"
  mineru-models-download -s huggingface -m vlm     # 一次性(~GB)；HF 慢可用 -s modelscope
  ```

  然后在本机 `.env` 写：

  ```text
  MINERU_MODE=cli
  MINERU_BACKEND=vlm-auto-engine     # Apple Silicon 上走 MLX
  MINERU_PARSE_METHOD=auto           # 如需对齐手工 MinerU 结果，可改 txt/ocr
  MINERU_LANG=en                     # 可选；已知 PDF 语言时建议设置
  MINERU_MODEL_SOURCE=huggingface
  MINERU_TIMEOUT_SECONDS=1800        # 本地 VLM 跑完整论文可能超过 10 分钟
  ```

  `.env.example` 默认仍保持 `MINERU_MODE=off`，让其他环境默认离线安全。

**URL 来源（「添加链接」）优先用本地 MinerU。** 只要配置了本地 MinerU 服务（`MINERU_MODE=http`/`cli`），公开 PDF 链接就由本地解析：后端下载后走与文件上传相同的「本地 MinerU→pypdf」路径。为防 SSRF，下载器会校验初始地址和每次重定向，拒绝 localhost、私网、link-local 与保留地址；内部文档请改用文件上传。`MINERU_API_TOKEN` 云端（mineru.net）仅在未配置本地 MinerU 时作为回退——一旦走本地，绝不会再静默调用云端。添加链接需要本地 MinerU 或云端 token 二者其一。文件上传遵循同一条规则：本地 MinerU 未配置、仅配置了云端 token 时，上传文件同样经该云端 v4 路径解析（含图片、公式、表格），云端调用失败会自动回落 pypdf。

MinerU 输出会映射为结构化 `SourceElement`：公式→`formula` 元素（保留 LaTeX），表格→`table` 元素（HTML 存入 metadata），标题保留层级。前端在 source detail 里渲染它们——公式用 KaTeX、表格用其 HTML——所以公式是排版后的样子而不是原始 LaTeX。若 MinerU 不可达或出错，摄取会降级到 pypdf，保证上传不被阻塞，同时 pipeline log 和 source `error_message` 会保留回退诊断；若某 PDF 解析出 0 文本（如扫描/图片型 PDF），会给出提示而不是看起来"空成功"。桌面端的来源详情窗口使用常规关闭按钮，并可按住标题栏拖动——应用里其它居中浮动弹窗（模型服务状态、添加来源、知识/图谱、报告、各类确认框）同样可拖动，共用同一套拖动 hook；窄屏继续使用固定弹窗布局，详情正文保持独立滚动。

### 单文件解析自检(`scripts/mineru_probe.py`)

一个单文件诊断脚本，把一个文件(`.pdf`/`.docx`/`.pptx`)沿**应用上传时的同一条内联路径**发出去——即配置好的 MinerU 服务(`MINERU_MODE=http` → `/file_parse`，或 `MINERU_MODE=cli`)，再经同样的 `content_list` → `SourceElement` 映射——并报告能否解析。用于在把某个 MinerU 部署接入摄取前，确认它可达、且确实能解析给定文件。

```bash
python scripts/mineru_probe.py /path/to/paper.pdf
python scripts/mineru_probe.py /path/to/paper.pdf --dump /tmp/content_list.json
```

它会先打印从仓库根 `.env` 读到的生效 MinerU 配置——含 `http_proxy`/`no_proxy` 对 MinerU URL 的解析结果（内网调用被正向代理静默接管是 `504` 的常见根因；注意 `no_proxy` 不识别 `10.0.0.0/8` 这类 CIDR 网段，只认精确主机）——再给出原始块数/类型分布，以及映射后的结构化元素数。退出码 `0`=解析成功(≥1 个元素)；`1`=根本没发请求(MinerU 未开/配置缺失，或文件不存在)；`2`=已发送但失败(不可达、超时、HTTP 错、或返回空/映射为 0 元素)，每种都附一句分类排障提示。它会 import backend 并读仓库根 `.env`，请从主 checkout 根目录运行。本探针只覆盖内联 `MINERU_MODE` 路径——不含 mineru.net 云端(URL 来源)与下面的异步 `/tasks` 批量端点。

### 批量 PDF→Markdown 解析(`scripts/mineru_batch_parse.py`)

独立于 backend 之外的部署侧 CLI,用于批量/离线预解析一整个 PDF 目录(如一批书),对接你自己的 MinerU 部署,产出供下面「离线批量摄取」消费:PDF 目录 → `mineru_batch_parse.py` → Markdown 目录 → `batch_ingest.py` → KG。它递归扫描 `--src` 下的 PDF,把每个文件提交到内网 MinerU server 的**异步** `/tasks` API(提交→轮询→取结果),轮流分派到各配置的 server(每台各自有并发上限),产出与源目录同构的 `.md` 文件树到 `--out`。这与上面应用内联的单文件上传解析(`MINERU_MODE=http`,MinerU 同步的 `/file_parse` 接口)以及 mineru.net 云端路径都是独立的两条路——请指向你自己的、支持异步 API 的 MinerU server。

配置走 `.env`(`MINERU_BATCH_*`,见 `.env.example`)——`--env-file` 用来指定加载哪个 `.env` 文件(默认 `./.env`)——每个 key 都可用对应的命令行参数按次覆盖(`--servers`、`--src`、`--out`、`--list <文件>` 显式给路径列表而非递归扫描、`--limit N` 限制处理文件数)。重跑会跳过已生成的 `.md`;每个文件的结果(`ok`/`skip`/`fail`,若 Ctrl-C 中断则还没轮到的文件记为 `cancelled`)都会追加进一份 JSONL manifest(默认 `{MINERU_BATCH_OUT_DIR}/_manifest.jsonl`),可续跑、可审计;Ctrl-C 会让进行中的文件跑完,但不再派发新的,重跑会重试所有 `fail`/`cancelled`。`--only-failed` 只重跑上次记为 `fail` 的文件(也会列在 `failed.txt` 里)。

```bash
# .env 里配好(MINERU_BATCH_SERVERS / _SRC_DIR / _OUT_DIR ...)
python scripts/mineru_batch_parse.py --dry-run      # 预览 server 分配
python scripts/mineru_batch_parse.py                # 正式跑
python scripts/mineru_batch_parse.py --only-failed  # 只重跑上次失败的文件
```

脚本不 import 任何 backend 代码——只依赖标准库和 `requests`(backend 已有此依赖)——通过普通 HTTP 与 MinerU server 通信,运行它的机器因此不需要 GPU/torch。

### 离线批量摄取(目录 → KG)

把一个目录里的 Markdown(及偶发 PDF)离线复用现有管线灌进库。分两阶段:
先 `ingest`(无 LLM、快,chunk 问答即可用),再 `kg`(LLM 抽取,单独可恢复)。

```bash
# 1) 解析+分块+向量(无 LLM):新建库须用 --notebook-name 指定名字
PYTHONPATH=backend python scripts/batch_ingest.py ingest --input-dir /path/to/md_dir --notebook-name "我的库"

# 2) 先小范围验证 KG 质量(只抽前 50 个未抽源)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 50

# 3) 整批抽 KG(幂等,跳过已抽;失败可重跑续抽)
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx

# 修复最近一次抽取留下部分 KG 的来源（有失败窗口且已有对象）
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --retry-partial

# 或一条命令跑完(ingest 然后 kg)
PYTHONPATH=backend python scripts/batch_ingest.py all --input-dir /path/to/md_dir --notebook-name "我的库"

# 为基准层 notebook 构建可伸缩检索索引(离线;静态基准重建 KG 后需重跑)
PYTHONPATH=backend python scripts/batch_ingest.py index --notebook-id nb-xxxx

# 补该 notebook 缺失的 chunk + 节点向量（幂等；需绑定 `chunk_embedding`）
PYTHONPATH=backend python scripts/batch_ingest.py embed --notebook-id nb-xxxx

# 一次性存储迁移：把旧的 JSON 文本向量转成 float32 BLOB（幂等，不调用模型）
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py vectors-to-blob --all-notebooks --workers 8

# 主动回填「来源删除反查表」（幂等，不调用模型）
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py backfill-source-index --all-notebooks

# 补已解析论文源缺失的元数据（幂等；需绑定 `paper_metadata`，不调用 embedding）
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx
PYTHONPATH=backend python scripts/batch_ingest.py metadata --notebook-id nb-xxxx --force

# 修复历史空源:对无 source_elements(上次 parse 未落地)的存量源重新 parse 补 elements,再重抽 KG
PYTHONPATH=backend python scripts/batch_ingest.py reparse --notebook-id nb-xxxx
```

`embed` 子命令只补**缺失**的 chunk、element 与 KG 节点向量（例如某次被限流后留下的空洞）。必须给 `--notebook-id` 且系统配置至少已绑定 `chunk_embedding`；`source_element_embedding` 与 `knowledge_object_embedding` 未绑定时对应类型跳过。它本身就是补向量的命令，故**忽略 `--allow-no-embed`**，`chunk_embedding` 未绑定时直接报错退出。

`vectors-to-blob` 子命令是一次性存储迁移:embedding 向量过去以 JSON 文本存 SQLite,导致把几十万行加载成矩阵(建索引、检索冷启动)时大部分时间耗在 `json.loads` 上。现在新写入统一存成 float32 BLOB(`np.frombuffer` 零解析直接重解读字节),且所有读点都已兼容两种格式——所以这个命令是可选但推荐的升级后操作:它把四张 embeddings 表(`chunk_embeddings`、`knowledge_embeddings`、`element_embeddings`、`relation_embeddings`)里仍是 JSON 文本的旧行原地转成 BLOB,分批事务提交(每批 5000 行)并按表打印进度。它**不计算新向量**（故不需要任何模型服务绑定），且幂等/可中断重跑——只选 SQLite 仍判定为 `text` 类型的行,跑第二遍时天然无行可转。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 转换全库所有 notebook。`json.loads`/重编码这一步(百万行规模下的单核瓶颈)按 `--workers` 个进程并行(默认 `min(32, CPU核数)`;`--workers 1` 完全不启动进程池)——主进程始终独占全部数据库读写,SQLite 单写者不变;进程池崩溃时自动回退串行,绝不丢run。

`backfill-source-index` 子命令主动填充 `knowledge_object_sources` 反查表(`object_id, source_id`)——删除或重解析某个来源时,需要找出哪些 KG 对象引用了它;没有这张表,该查找就得逐行 `json.loads` 整本 notebook 的 evidence JSON 才能找到匹配,几十万对象规模下很慢。这张表本来会「首用惰性回填」(未迁移库的第一次来源删除/重解析会付一次全扫描,扫描的同时顺带填表并标记该 notebook,此后每次都是索引直查)——这个命令让你提前批量付这笔成本(有界内存分批 + 打印进度),而不是让某个用户操作(删除来源)撞上它。它不调用模型，且幂等/可中断重跑(每次重跑都清空并按当前 evidence 重建该 notebook 的行,再重新标记)。用 `--notebook-id` 限定单个库,或 `--all-notebooks` 覆盖全库所有 notebook。若怀疑某库的反查表与实际 evidence 不一致(例如异常中断后),重跑本命令即是修复手段——它总是按当前 evidence 全量重建。

`metadata` 子命令给 notebook 里还缺论文元数据（标题、作者、机构、期刊、年份）的来源补抽——适用于「论文元数据抽取」上线前就已入库的旧库，或抽取 prompt/校验升级后想刷新一遍。它只处理已解析、且看起来是论文的来源（doc_type 为空或 `academic_paper`）；文本读的是库里已存的解析产物（source elements），原始 PDF 不在磁盘上也能跑。必须给 `--notebook-id`（本子命令绝不新建 notebook），且系统模型配置必须绑定 `paper_metadata` workload；未绑定时直接报错退出，不会静默跳过，也不需要 embedding workload。幂等、可中断重跑：已有元数据行的源默认跳过，加 `--force` 则对本次范围内所有源强制重抽（例如 prompt/校验升级后）。进度按源逐行打印（`[meta <done>/<total>] <source-id> <status>`），结束打印各状态计数的 JSON 汇总。

`reparse` 子命令修复一类历史存量:某些源已建、`parse_status` 看似前进,却没有 `source_elements`(上次 parse 中断或未落地)。KG 抽取有一道零-LLM 接地校验——每个 LLM 抽出的节点必须把引文匹配回该源的某个 element,否则丢弃;一个源若没有任何 element,抽出的节点会被**整源丢光**,导致 `knowledge_objects` 一行不增(抽了等于白抽),且直接重抽永远补不出。旧版 `all` 的续跑分流曾用「有没有 KG」当「是否已 parse」,把这类无-elements 源当成「已 parse、缺 KG」直接送去抽取,正是踩中此坑(该分流已修正,新导入不再遇到)。本命令对该 notebook 内所有缺 `source_elements` 的源重新跑 `process_source`(parse → 生成 elements),收尾一次 KG rebuild;有 elements 的源自动跳过(幂等、可中断重跑)。`--limit N` 只处理前 N 个;`--no-rebuild` 跳过收尾聚类(分批场景)。必须给 `--notebook-id`。

`kg --retry-partial` 修复的是另一种状态：来源仍有 KG 对象，但最近一次 KG 抽取记录为 `windows_failed>0`。普通增量 `kg` 会把“已完成且已有对象”的来源视为已覆盖，不会自动重跑；显式加此参数后，它们会与正常缺 KG 来源一起进入目标集合。部分来源重试期间，旧对象和关系仍保持可读；若任一窗口再次失败或新结果为空，批处理会把本次尝试计为失败/未完整且旧图不变，只有“零失败窗口且非空”的新结果才会在一个事务内替换该来源的图。`--limit N` 限制“缺失 + 部分”合并后的目标数，`--no-rebuild` 可用于分批，末尾 rebuild/index 行为不变。它不是解析修复；没有 `source_elements` 的来源仍应使用 `reparse`。

**MRL 截断质量 spike(`app.eval.mrl_truncation`)。** 回答「把存量向量截断到前 1024/2048 维(+ re-normalize),检索质量掉多少」——这既是进程内向量内存瘦身(4096→1024 约 ÷4)的前置,也是 pgvector HNSW 建索引(维度上限 2000/4000)的 gate。只读、流式分块(百万行表内存有界),并总是先打印该 notebook 四张 embeddings 表的行数。

```bash
# 邻居保持率模式(默认):零 API 调用,任意 notebook 可跑——
# 从表内采样向量当查询,对比原维 vs 截断维的 top-K 排名重合率
( cd backend && python -m app.eval.mrl_truncation )                          # 自动挑最大的 notebook
( cd backend && python -m app.eval.mrl_truncation --notebook nb-xxxx --tables knowledge,chunk,relation --dims 2048,1024 )
# 超大表(如百万级 relation):语料侧也抽样——排名在同一子集内对比,
# 原维 vs 截断维的相对结论依然成立(稀疏子集读数略偏乐观;边界值请全量复核)
( cd backend && python -m app.eval.mrl_truncation --tables relation --sample-rows 50000 )

# gold 模式（需绑定 `chunk_embedding` workload；每题按原生维 embed 一次）：
# 对提交在仓库里的 gold 集算各截断档的 recall@12 / MRR 相对衰减
( cd backend && python -m app.eval.mrl_truncation --gold app/eval/recall_gold.yaml --notebook nb-b37185f4ae )
```

判据(出自 pgvector 迁移评审 spec):2048 档 recall@12 相对降 ≤1pt 且 top-10 重合 ≥0.9 → `halfvec 2048`;1024 档降 ≤3pt → `vector 1024`;降 >5pt 该档不通过。把整段输出贴回即可出结论。

**大型基础 KG(10^5–10^6 对象)。** 末尾的 unified 聚类是流式的(内存随**唯一归一化概念名数**而非总对象数有界),所以 `kg` 不物化全量向量即可扩展。超大语料可分批抽取、末尾一次聚类:

```bash
# 分批抽取(跳过昂贵的末尾聚类),按需重复
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --limit 1000 --no-rebuild
# 末尾只聚类 +(重)建 scale 索引,不再抽取
PYTHONPATH=backend python scripts/batch_ingest.py kg --notebook-id nb-xxxx --rebuild-only
```

`--limit` 只限本轮**抽取**的来源数;最终聚类始终覆盖整个 notebook。大库(见上文 `SCALE_INDEX_AUTO_ENABLED`)在 `kg` 重建后会**自动重建**可伸缩检索索引(不会陈旧)。`KG_CLUSTER_REP_ANN_MAX`(默认 2,000,000)封顶 rep-ANN 规模——超出则分片建索引并 WARNING(绝不静默截断)。

**批处理并发。** `--workers` 只控制来源/文档任务，省略时回退 `KG_JOB_CONCURRENCY`。它在 `all` 中分派来源 job、在 `ingest` 中控制文件解析；`vectors-to-blob` 中则表示解析/重编码进程池大小（默认 `min(32, CPU核数)`；`1` 关闭该进程池）。

`all`、`kg`、`reparse`、`metadata`、`ingest` 和 `embed` 里的每次模型调用，都与在线请求共用系统模型服务调度器。各 workload 所绑定服务只从部署 TOML 读取一个模型容量参数 `max_concurrency`；批处理 CLI 不再提供模型并发覆盖项，增大 `--workers` 也不会乘大该服务上限。若一次限流留下缺失向量，之后用 `embed` 子命令补修。

例如：模型容量已经在部署 TOML 声明后，可单独提高来源管线并发：

```bash
PYTHONPATH=backend python scripts/batch_ingest.py reparse \
  --notebook-id nb-xxxx \
  --workers 32 \
  --pool-report-interval 5
```

- `--pool-report-interval` —— `all`/`kg`/`reparse` 阶段每 N 秒打印 producer/source 业务线程池占用（默认 15；`0` 关闭）。它不是模型容量权威来源；每个服务的运行数、排队数、健康状态和熔断状态应在只读「模型服务」状态中查看。

选项：`--owner`（notebook 属主用户名，大小写不敏感，默认 = admin 用户）、`--workers`（来源管线并发 = `KG_JOB_CONCURRENCY`；`vectors-to-blob` 中为解析/编码进程池大小，默认 `min(32, CPU核数)`，`1` = 不启进程池）、`--limit`（kg 抽取子集——聚类仍覆盖全量）、`--retry-partial`（仅 `kg`：安全重试最近一次有失败窗口且已有对象的来源）、`--no-rebuild` / `--rebuild-only`（分批大库构建时拆分「抽取」与「末尾聚类」）、`--fresh`（清空 rebuild checkpoint，强制 merge 审查 + 概念描述全量重裁；用于只换了 KG 模型/阈值、数据没变时——隐含强制 rebuild）、`--allow-no-embed`（`chunk_embedding` 未绑定时显式允许无向量降级；默认拒绝、不静默；`embed` 子命令忽略此项）、`--pool-report-interval`（`all`/`kg`/`reparse` 阶段每隔几秒报告 producer/source 业务线程池；默认 15，`0` 关）、`--all-notebooks`（仅 `vectors-to-blob` / `backfill-source-index`）、`--force`（仅 `metadata`）、`--dry-run`（只扫描预估）。模型并发不提供 CLI 覆盖参数，只取所绑定物理服务的 `max_concurrency`。`embed` 子命令补缺失的 chunk + element + 节点向量。

前置：用 `MODEL_SERVICES_CONFIG` 指向部署 TOML，按阶段绑定所需 workload（尤其是 `chunk_embedding`、`source_element_embedding`、`knowledge_object_embedding`、`kg_extract` 和 `paper_metadata`），`.env` 只保存 TOML 引用的密钥。`chunk_embedding` 未绑定时 CLI 默认拒绝运行；确需无向量导入须显式加 `--allow-no-embed`。续跑从**数据库状态**推导而非读取进度文件：`ingest` 看内容哈希，`kg` 看最近一次抽取是否完成，`embed` 看向量行是否存在。parse 中断但已写入哈希的来源用 `reparse` 修复；`<storage>/batch_ingest/<notebook>.jsonl` 只是只写运行日志。

### 大库检索热路径

索引 KG 检索在 ANN 生成候选后仍必须保持有界。孤立节点排序降权只对每个候选执行带索引的 `EXISTS`，并且只返回已有连接的候选 id，绝不能拉取 hub 的完整邻边；canonical fold 只能通过 `cluster_fold_rows` 读取 scored id 的映射。并发推理子查询按 scale-index 实例和工件类型共享一次惰性 ANN 加载。这些优化不改变检索 id、score、阈值、PPR 行为或召回。

生产回归时，先用 `python3 scripts/diag.py incident` 和 `python3 scripts/diag.py slow --since 6 --deep` 抓线程栈与慢阶段拆分。在 `_retrieve_scored` 事件里分别比较 `ann_ms`、`hydrate_ms`、`fold_ms`；候选数很小时，hydration 不得随全库关系行或 cluster 行数增长。前后版本验收使用下一节的 exact 回放对照。

### 检索回放对照(`scripts/replay_retrieval.py`)

性能优化改动前后,证明"检索效果不变"的验收工具:拿一份固定问题集跑 reasoning 检索原语(`federated_retrieve` + `ppr_retrieve`),**不调用任何答案 LLM**,把命中的 id/分数序列存成 JSON;两次运行的输出可逐问题 diff。

```bash
# 记录一次（需绑定 `chunk_embedding`，使用真实查询向量；仅读检索原语，不需要 chat 模型）
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out before.json

# --full:额外跑一遍完整 reasoning 编排层(plan/reflect 用固定子查询 + 立即 answer 的 stub 代替 LLM,
# 验证编排层改动的确定性部分等价),子查询从 plan.json 里取
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt \
    --full --plan-file plan.json --out before.json

# 改动后重新记录一次,再对照两份输出
python scripts/replay_retrieval.py --notebook nb-xxxx --questions questions.txt --out after.json
python scripts/replay_retrieval.py --compare before.json after.json                  # --mode exact(默认):id + 分数序列须逐位相同
python scripts/replay_retrieval.py --compare before.json after.json --mode topk --k 30  # 只比较前 k 个 id 的集合重叠率与序(允许分数因 float32 化等改动而漂移)
```

`questions.txt` 每行一个问题;`plan.json` = `{"<问题>": ["子查询1", "子查询2", ...]}`。**必须从主 checkout 根目录运行**(`.env` 按当前工作目录加载,与 `batch_ingest.py` 相同)。`--owner` 复用与 `batch_ingest.py` 相同的属主解析(大小写不敏感,默认 = `"admin"`)。

退出码即验收结果,可直接接入 CI/脚本判定:`0` 成功(记录模式)或 `--compare` 全部一致;`1` `--compare` 发现不一致(两次运行结果有差异);`2` 对照发生前的前置条件失败（`retrieval_query_embedding` 未绑定、notebook 不存在、或属主用户不存在）——CLI **直接报错退出**,绝不用零向量静默跑出误导性的"零召回"对照结果。

### 合并两个共享 base 库的部署(`scripts/merge_dbs.py`)

离线、非破坏性工具,用于把两个各自独立部署、但**共享同一个公共知识库**(同一个 base notebook id)的 silicon-notebook 实例合并成一个。保留哪侧的 base 由 `--keep-base` 指定(通常选更全的那侧)——运行时会先打印两侧 base 的统计(`sources`/`chunks`/`knowledge_objects` 计数)供核对;两侧其余(个人)notebook 原样全部并入,包括各自持有的参考库挂载边(`notebook_bases`)。源库的 `.db`/storage 文件只读,工具始终写出全新的 `--out` / `--out-storage`。两侧输入允许是旧 schema 版本——合并前会先各自迁移到最新(在私有临时副本上进行,不改动源文件)。多领域部署下一侧可能不止一个公共知识库:本工具不支持这种形态、也不会替你猜——若任一侧存在不止一个 `tier='base'` 的 notebook,会立即中止并点名是哪一侧、列出全部候选,而不是自作主张选一个。

```bash
PYTHONPATH=backend python scripts/merge_dbs.py \
  --db-a A/silicon_notebook.db --storage-a A/storage \
  --db-b B/silicon_notebook.db --storage-b B/storage \
  --keep-base a \
  --out merged/silicon_notebook.db --out-storage merged/storage \
  --assume-same-users
```

- `--keep-base a|b` —— 保留哪侧的 base notebook(通常选更全的那侧)。
- `--assume-same-users` —— 两库存在相同 user id 时必须加此项,用于确认两侧确实是同一个人的账号,否则工具会中止以避免内容归属错乱。
- `--dry-run` —— 只迁移+校验+打印将会导入哪些 notebook,不产出任何文件;即使 `--out` 已存在也能预览。
- `--force` —— 覆盖已存在的 `--out` 文件。

前提条件:两侧各自必须恰好有一个 `tier='base'` 的公共知识库(见上);除共享的 base 外,两库的 notebook id 不得重叠——一旦撞车,工具会中止并列出冲突的 id。

**重要提醒:** `--db-a`/`--db-b` 要指向已静置(先停服务)的数据库文件。工具只拷贝 `.db` 文件本身,正在运行的部署若有未落盘的 `-wal` sidecar 不会被带上——直接对着运行中的实例合并可能静默丢失最近的写入。(工具自身做 schema 迁移时的写入会在使用前 checkpoint 回 `.db`,这部分是安全的;这条提醒针对的是你提供的源文件本身。)

**落败一侧 base 自己的参考库挂载边:** 合并后保留的 base notebook(`--keep-base` 那一侧)只保留它自己的参考库挂载边;如果**另一侧**的 base notebook 曾挂载过别的参考库,这些挂载边不会被带过来——这和该 base 名下其它 notebook-scoped 数据(它自己的 sources、chunks、knowledge objects……)的既定规则完全一致。两侧其它(个人)notebook 自己持有的挂载边则原样全部并入,不受影响。

合并完成后,把 `merged/` 产出(db + storage)部署到要保留下来的那台主机,首次启动后在 app 内触发一次索引重建(「重建索引」/「刷新图谱」)以重新生成 `kg_index`/`kg_viz`/ANN 等未被拷贝的产物。

### 补齐存量待批晋升候选的目标公共知识库(`scripts/backfill_promotion_targets.py`)

升级到 `SCHEMA_VERSION>=20`(多领域参考库)会给 `promotion_candidates` 加一列
`target_base_id`,但迁移只加列、**不回填**存量行。任何在升级前就已创建、此时仍处
`proposed`/`under_review` 的晋升候选,`target_base_id` 都是空串,批准时会失败
(target_base_id 只在候选首次提交时可设,没有别的接口能事后补写)。如果部署库里可能有
这类存量候选,升级后先跑一次本工具处理;它按 propose 时同一条规则解析每一行的目标——
经该候选所属 notebook 已挂载的公共知识库(挂 0 个则阻塞、恰好 1 个自动解析、多个则需要
显式指定),复用同一个 `GovernanceStore.mounted_public_base_ids` 判定,不另写一份。

```bash
PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db .local/silicon_notebook.db list
PYTHONPATH=backend python scripts/backfill_promotion_targets.py --db .local/silicon_notebook.db apply \
  [--set NOTEBOOK_ID=BASE_ID ...] [--dry-run]
```

- `list` —— 只读报告:列出每条 `target_base_id` 为空的 `proposed`/`under_review` 候选,
  按 notebook 分组,附带该 notebook 已挂载的公共知识库与每条候选的解析结果预览。
- `apply` —— 为每条能无歧义解析的候选(自动解析,或经 `--set` 指定)写入
  `target_base_id`;仍阻塞(该 notebook 未挂载任何公共知识库)或仍有歧义(挂了多个、
  又没给匹配的 `--set`)的候选原样不动并在报告里列出,挂载/补 `--set` 后再跑一次即可
  只处理剩下的部分。默认直接写库(与 `merge_dbs.py` 的约定一致);加 `--dry-run` 只
  预览、不写库。
- `--set NOTEBOOK_ID=BASE_ID` —— 给挂载了不止一个公共知识库的 notebook 显式指定目标,
  必需时才用,可重复用于多个 notebook。目标若不在该 notebook 的挂载集合内,整次运行
  会在任何写入之前直接中止(不会出现部分写入)。

和 `merge_dbs.py` 「总是写全新输出」的约定不同,本工具直接就地修改你给的 `--db` 路径;
如果该库还没迁移到 `SCHEMA_VERSION>=20` 则拒绝运行。

**重要提醒:** 运行 `apply` 前请先停止后端服务——本工具直接打开 `--db` 文件且不设
`busy_timeout`,若后端正持有该库的活跃事务,同时写入可能相互冲突。
### 回填存量 knowhow 格子的 Markdown 格式(`scripts/backfill_knowhow_md.py`)

Knowhow 表格已经对新导入/追加的数据、以及格子级「整理格式」操作自动做 Excel 习惯排版规整(Tab 缩进的 `•` 项目符号、`A.`/`a.` 分节/子项编号、软换行等清理成干净的 CommonMark),但这个规整不会回溯性地应用到规整功能上线之前就已存在的格子。这个一次性 CLI 用于给指定 notebook 的这些存量格子补做同样的规整。

**先 dry-run,再按评审过的 plan 文件写入。** dry-run 绝不写库,而是把完整计划写成一个 JSON plan 文件(并打印其路径),供你逐条评审;随后 `--apply --plan` 按【那个文件】逐条写入——落库的就是你评审过的,不会重新规划。

**默认 dry-run 是只读的,随时可安全执行。** 默认(纯规则)dry-run 以只读方式打开数据库,不会构造可写仓库,因此对着正在运行/繁忙的后端跑也安全。`--use-llm`(需要改写模型)和 `--apply`(写库)则以【可写】方式打开数据库——打开时可能执行尚未完成的 schema 迁移与崩溃恢复,工具会在这么做时打印一行提示,建议在后端空闲时再执行这两种。

```bash
# dry-run(默认):打印每格 before/after/来源 + 汇总,并写出 plan 文件
#(默认 .local/backfill_plans/knowhow_md_<notebook>_<时间戳>.json)——不写库
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx

# 评审 plan 文件无误后,按它逐条写入(确定性规则,不涉及 LLM)——任何 --apply 都【必须】带 --plan
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --apply --plan <plan.json>

# 改走 LLM 重排(每格「重排 -> 内容不变式校验 -> 规则兜底」):先 dry-run 评审,再按
# 评审过的 plan 写入(同样的 --apply --plan 握手)
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm
PYTHONPATH=backend python scripts/backfill_knowhow_md.py --notebook nb-xxxx --use-llm --apply --plan <plan.json>
```

- `--notebook`(必填)—— 要回填的 notebook。
- `--apply` —— 真正写入;它【必须】带 `--plan PATH`,按该评审过的 plan 文件逐条写入。某个格子若在评审后被人改过(当前内容与 plan 记录的 `before` 不一致)会被【跳过并报告】,绝不覆盖已经改动过的目标。每个写入的行标记为 pending,交由投影重算其 KG/步骤(重投影在命令退出前同步完成)。不带 `--plan` 的 `--apply` 是【硬错误】:apply 时从【当前】库重新规划,会把评审后被改过的格子也带进来写入却从未被评审(`--use-llm` 更甚——改写模型随机,重新规划连候选都不同)——所以请先 dry-run、评审其 plan 文件,再按【那份】写入。
- `--use-llm` —— 改走系统为 `knowhow_reformat` 绑定的服务逐格重排（自带零 LLM 的内容不变式校验，校验不过会自动退回确定性规则），而不是默认那套随时可用的规则规整器。若该 workload 未绑定、或其结果未过校验已退回规则，工具会打印明确的 `WARNING`，不会悄悄假装 LLM 生效了。
- `--save-plan PATH` —— 覆盖 dry-run 写出 plan 文件的路径。
- `--plan PATH` —— 要写入的评审过的 plan 文件(见 `--apply`)。

**行标题(anchor)列绝不参与规整**——不论是导入、追加还是本回填等【批量】路径:它是分组键,必须字节稳定,规整它会让刚被改动的行与既有概念组的键失配、组被劈开。(只有编辑器里【显式】的单格「整理格式」——有人逐格评审建议、且同组兄弟行一起改写——才可以动它。)

这项并发契约只适用于编辑器交互式整行/整表规整批次的保存单元；普通共享格编辑和普通
API 不获得此保证。批次打开时会冻结完整表快照，但仅为由完整 anchor-group 保存单元覆盖的
每个非空 anchor 分组冻结精确成员集合（合并共享列扇写或单例完整组）。同一个 SQLite 写
事务会逐一重新校验全部写目标的 expected 内容基线、当前行标题列指定，以及这些被覆盖冻结
分组的精确成员。多行 anchor 分组里的非共享列是合法子集写：只校验其写目标基线，不做整组
成员集 guard。适用的任一内容、anchor 或成员漂移都会使整个保存单元以 HTTP 409 拒绝，且
绝不部分写入。UI 会保留刚生成的规整候选并标记为陈旧，要求用户重新运行规整，并在关闭
批量弹窗后刷新整表。

v21 为 `(column_id, JS-trim(content_md), row_id)` 建立索引；guarded 成员检查以同一归一化表达式做等值查询。因此完整 anchor 分组仍 fail-closed，但在写事务中按分组查找而不再扫描整列。

必须在主 checkout 根目录下运行(需要真实的 `.env`/数据库配置,与上面的 `batch_ingest.py`/`replay_retrieval.py` 一样)。可安全重复执行:再按同一个 plan 应用一次是 no-op(每个已应用的格子当前内容都已不再等于它记录的 `before`)。
