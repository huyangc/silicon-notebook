# SQLite → PostgreSQL 迁移 runbook(执行清单)

> **本文件是「怎么做」的执行清单,给按步骤驱动的 Agent 或运维用。**
> 「为什么这么设计」「有哪些限制」的真源是
> [docs/operations.md](operations.md#sqlite--postgresql-cutover-and-rollback)
> (中文:[docs/operations_zh.md](operations_zh.md#sqlite--postgresql-切换与回滚))。
> **两者冲突时以 operations 那一节为准**,并回来订正本文件。
> 迁移是一次性单向导入:只搬数据库里的数据,不搬文件,不做持续同步,不能反向回放。

## Agent 硬规则

执行本 runbook 的 Agent 必须遵守,违反其一就停下来问人:

1. **不代劳启停服务。** 停服务、重启后端一律把命令交给人执行,不自己跑。
2. **不可逆动作前必须停下来拿明确同意**:正式窗口的 `--apply`、`--activate-env`、
   任何删除/覆盖目标库、放开流量。彩排(独立空库)不在此列。
3. **失败即停,不重试到底。** CLI 的失败都是 fail-closed 的设计,不是需要绕过的障碍。
   看懂 `MIGRATION FAILED: <原因>` 再决定,不要换参数硬闯。
4. **不静默降级。** 任何一步的判据没达成,就是没达成;不要用"看起来差不多"继续。
5. **URL 只走环境变量**,不进命令行参数、不进日志、不贴进对话。

## 占位符

| 占位符 | 含义 |
| --- | --- |
| `<SQLITE_DB>` | 源 SQLite 文件绝对路径(部署机上的 `.local/silicon_notebook.db`) |
| `<WORK_DIR>` | 迁移私有工作目录绝对路径(放快照与 receipt,需与源库同量级空闲空间) |
| `<ENV_FILE>` | 部署 `.env` 绝对路径 |
| `<TZ>` | SQLite **部署机**的 IANA 时区,如 `Asia/Shanghai` |
| `<RECEIPT>` | `--apply` 成功后打印的 receipt 路径 |

## P0 前置采集(只读,可随时做)

```bash
sqlite3 <SQLITE_DB> "PRAGMA user_version; PRAGMA quick_check;" \
  && ls -l <SQLITE_DB> && df -h <WORK_DIR>
```

判据:`quick_check` 为 `ok`;`<WORK_DIR>` 与目标库所在盘各自剩余空间 ≥ 源库大小(快照 + 升级
工作副本 + PostgreSQL 数据与索引)。任一不满足就停,先扩容。

留档以便事后核对:各主表行数(至少 `notebooks`/`sources`/`chunks`/`knowledge_objects`)、
当前 `SILICON_NOTEBOOK_STORAGE_DIR` 的值。

## P1 预检(只读,不写任何数据)

```bash
export POSTGRES_MIGRATION_URL='postgresql://USER:PASSWORD@HOST:5432/EMPTY_DB'
python scripts/migrate_sqlite_to_postgres.py --source <SQLITE_DB>
```

不带 `--apply` 就是预检,默认行为。

- **通过**:打印 `PRECHECK OK: ...` 与 `No data was written.`,退出码 0。
- **失败**:打印 `MIGRATION FAILED: <原因>`,退出码 2 → 对照本文末尾「会硬失败的情形」。

目标库必须是**专用的空库**且 UTF-8,`pg_trgm` 可在 `public` 建。指向已有业务库会被拒。

## P2 彩排(必做,不能跳)

**对 500GB 级别的库,彩排不是可选项。** CLI 对脏值(非法 JSON、坏向量、非法时间戳、NUL)
一律 fail-closed:跑到第几百 GB 才撞上一条,窗口就废了。彩排的目的就是把这类失败提前
到不影响生产的时候,并实测真实吞吐。

在**生产数据副本**上、对一个**独立的空目标库**执行:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source <SQLITE_DB> \
  --work-dir <WORK_DIR> \
  --source-timezone <TZ> \
  --maintenance-work-mem 2GB \
  --max-parallel-index-workers 4 \
  --apply
```

- 进度行:`COPY i/N` → `VERIFY i/N` → `INDEX i/N`;续跑时已完成的表报 `SKIP i/N ... (checkpointed)`。
- **成功**:`MIGRATION OK: <行数> rows across <表数> tables; SQLite v<a>-><b>; snapshot=...; receipt=...`。
- **中断**(崩溃、断连、重启):**原样重跑同一条命令**即可续跑。为避免重新快照多 GB 源库,
  加上上一次打印的快照路径:`--snapshot <WORK_DIR>/sqlite-vNN-HASH.snapshot.db`。

记录耗时,用于估算正式窗口。`--source-timezone` 未显式传时默认取**执行机**本地时区——
执行机与 SQLite 部署机时区不同的话,所有 naive 历史时间戳会整体偏移,且不报错。

彩排结束后,彩排用的目标库**不能**用作正式目标(里面是旧快照,正式流程会拒)。

## P3 正式切换(有人工闸门)

> ⚠️ 本阶段每一步都改变生产状态。Agent 到这里必须先拿到明确同意再往下走,
> 且第 1、5 步的服务启停交给人执行。

1. **停写**(人执行):公告窗口 → 停 API、后台 worker、MCP 写入方、批处理/维护脚本、
   调度器 → 待在途写入结束后停后端。
2. **建新的空目标库**,并按部署策略做好 SQLite 与 PostgreSQL 的备份(且验证过可恢复)。
3. **再跑一次 P1 预检**(此时源库已停写)。
4. **正式导入 + 激活**(路径必须全部绝对):

   ```bash
   python scripts/migrate_sqlite_to_postgres.py \
     --source <SQLITE_DB> \
     --work-dir <WORK_DIR> \
     --source-timezone <TZ> \
     --maintenance-work-mem 2GB \
     --apply \
     --activate-env <ENV_FILE> \
     --confirm-service-stopped
   ```

   激活前会重做源库快照与逐表校验和比对,不一致就不动 `<ENV_FILE>`。
   大库可加 `--fast-activation` **只**跳过第二遍全表校验和读取(导入时已逐表校验并
   checkpoint),源库重快照与 schema/manifest 检查照常执行。
   若导入已完成、源库自那以后一直停写,可改用 `--activation-receipt <RECEIPT>` 直接激活。
   执行者应是 `<ENV_FILE>` 的属主(或 root)。

   成功打印 `ACTIVATION OK: ...`。CLI 只改 `DATABASE_URL`,把原 SQLite URL 存为惰性的
   `SHADOW_DATABASE_URL`(不参与选择、不同步),**不启停任何进程**。
5. **重启后端**(人执行),保持 `--workers 1`。

## P4 验收(放流量前必须全过)

```bash
curl -fsS http://127.0.0.1:8000/api/ready
```

1. `/api/ready` 返回 `"ready": true`(大库首启要等索引预载,不要提前放行)。
2. admin 与普通用户各登录一次。
3. 笔记本/来源数量与 receipt 对得上。
4. 抽查检索、Ask、知识、Memory、Knowhow、报告的代表性读取。
5. 确认引用到的文件能打开(见下「迁移不覆盖什么」)。
6. 做**一次**明确批准的金丝雀写入,并确认其后台任务跑完。

全过之后才放流量。

## P5 回滚边界

- **PostgreSQL 尚未接受任何业务写入之前**:停后端 → 把 `DATABASE_URL` 改回 SQLite →
  单 worker 启动 → 重跑 P4 冒烟。安全。
- **一旦发生第一次业务写入**:改回 URL 就会丢掉那些写入。本仓库**没有**反向导入器,
  也没有双写日志。此时 PostgreSQL 就是回滚边界。

## 迁移不覆盖什么

| 不覆盖 | 处理方式 |
| --- | --- |
| 上传件、资产、`kg_index`/`kg_viz` 索引工件 | 都在 `SILICON_NOTEBOOK_STORAGE_DIR` 下。同机切换无碍;换机必须单独拷贝并校验 |
| `llm_cache_v2.db` | 纯缓存,不迁 |
| 事件/LLM 日志 | 不迁 |
| 后续写入 | 一次性导入,没有持续捕获 |

切换后 SQLite 专用工具不可用:`scripts/batch_ingest.py` 的变更阶段有显式门,会打印
`batch_ingest mutation phases are SQLite-only` 并退出 2(PostgreSQL 请走正常应用/API 摄取
与 KG/重建索引流程);其余直连 SQLite 的脚本会以 `DATABASE_URL is not a SQLite URL`
硬失败——**是响亮失败,不会静默写进那份已经作废的 SQLite 文件**。

## 会硬失败的情形(照原样理解,不要绕过)

| 现象 | 原因与处置 |
| --- | --- |
| 预检报目标非空 | 目标不是专用空库,或复用了彩排库。换一个新空库 |
| `retired SQLite table <名> contains N rows` | 退役表(`articles`/`article_claims`/`derived_rule_candidates`/`extraction_candidates`)仍有数据。**这是拒绝丢弃历史数据,不是 bug**;先确认这些数据确实不再需要并自行处置 |
| `invalid JSON in <表>.<列>` / `invalid legacy vector` / `invalid timestamp` | 源库脏值。回 SQLite 侧修数据后重跑;这正是必须先彩排的原因 |
| `upgraded SQLite snapshot has the wrong schema version` / `table manifest mismatch` | 代码版本与源库 schema 不配对。用与源库匹配的代码版本,不要改 manifest 绕过 |
| `--activate-env requires --apply and --confirm-service-stopped` | 少了确认参数;先真正停写再补上 |
| 校验和不一致 | 激活会中止且不动 `.env`。不要重试激活,先查源库在导入期间是否仍在写 |
| 退出码 130 | 被 Ctrl-C/信号取消。原样重跑即可续跑 |
