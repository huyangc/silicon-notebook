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
| `<WORK_DIR>` | **正式窗口**的私有工作目录绝对路径(放快照与 receipt;容量要求见 P0) |
| `<REHEARSAL_WORK_DIR>` | **彩排专用**的工作目录绝对路径,与 `<WORK_DIR>` 分开(见 P0 容量说明) |
| `<ENV_FILE>` | 部署 `.env` 绝对路径 |
| `<TZ>` | SQLite **部署机**的 IANA 时区,如 `Asia/Shanghai` |
| `<RECEIPT>` | `--apply` 成功后打印的 receipt 路径 |

## P0 前置采集(只读,可随时做)

```bash
mkdir -p -m 700 <WORK_DIR>
sqlite3 <SQLITE_DB> "PRAGMA user_version; PRAGMA quick_check;"
ls -l <SQLITE_DB>
df -h <WORK_DIR>
```

(先建目录再 `df`:首次迁移时 `<WORK_DIR>` 通常还不存在,`df` 会直接失败。`0700` 是因为
里面的快照是完整的库副本。)

判据:

- `quick_check` 为 `ok`。不是就先修源库,不要迁一个已损坏的库。
- **`<WORK_DIR>` 所在盘剩余 ≥ 2 × 源库大小,彩排与正式共用同一目录时要 3 ×。** 不是 1×:
  - 导入阶段密封快照常驻,源库 schema 落后于当前代码时还会再拷一份完整升级工作副本;
  - **激活阶段无条件重新生成一份完整快照**(停写后的一致性锚点)。它先写成完整的临时文件,
    **写完之后**才按 hash 判重,所以哪怕内容与密封快照完全相同,峰值也实打实是 2 份;
  - 彩排是在 SQLite 仍在线时做的,正式窗口前源库通常已经变了 → 两次的快照 hash 不同 →
    **文件名不同,彩排那份不会被复用,会一直留着**。共用目录就变成 3 份。

  500GB 的源库:独立目录 1TB,共用目录 1.5TB。
  **建议彩排用单独的 `<REHEARSAL_WORK_DIR>`**,正式窗口开始前确认它已归档或删除;
  否则按 3× 备。按 1× 备的话前面几小时都正常,会在激活那一步爆盘。
- **目标库所在盘不能用源库大小估。** PostgreSQL 的数据 + 索引通常大于 SQLite 文件,
  重建索引期间还要额外临时空间。**用 P2 彩排实测出来的实际占用来定**,这也是彩排的
  产出之一;没有实测值就不要进正式窗口。

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
  --work-dir <REHEARSAL_WORK_DIR> \
  --source-timezone <TZ> \
  --maintenance-work-mem 2GB \
  --max-parallel-index-workers 4 \
  --apply
```

(彩排用**独立的**工作目录。与正式窗口共用会让两次的密封快照同时留在盘上——见 P0 的容量说明。)

- 进度行:`COPY i/N` → `VERIFY i/N` → `INDEX i/N`;续跑时已完成的表报 `SKIP i/N ... (checkpointed)`。
- **成功**:`MIGRATION OK: <行数> rows across <表数> tables; SQLite v<a>-><b>; snapshot=...; receipt=...`。
- **中断**(崩溃、断连、重启):**原样重跑同一条命令**即可续跑。为避免重新快照多 GB 源库,
  显式复用已密封的快照。中断时不会打印 `MIGRATION OK` 那行,所以路径要自己从工作目录找:

  ```bash
  ls -t <REHEARSAL_WORK_DIR>/*.snapshot.db
  ```

  (在这里找的是彩排目录——就是本步 `--work-dir` 传的那个;正式窗口同理换成 `<WORK_DIR>`。)

  **传给 `--snapshot` 的必须是这一趟自己密封的那个**,不是"目录里最新的那个"。校验只覆盖
  快照**自身**——文件名里的 hash、`quick_check`、来源路径 sidecar——**不会**拿它和当前源库
  比对。所以同一源库留下多个快照时(源库变过就会有),选错一个不会污染数据(激活阶段重做
  源库快照比 hash 时会拦下),但那时已经白跑完整整一趟导入。

  为避免这件事,**开跑时就把密封路径记下来**:第一次 `--apply` 的输出里有
  `正在通过 SQLite backup API 创建一致性快照…`,跑完(或中断后)对照 `ls -t` 的时间戳确认
  只有一个候选;有多个就以你这趟开始之后创建的那个为准,拿不准就不要传 `--snapshot`
  ——重新快照只是慢,不会错。

- **源库被改过之后不能续跑同一个目标。** 修脏值(或处置退休表数据)会改变源库内容,
  密封快照的 hash 随之改变,而目标库里的续跑进度是绑在旧 hash 上的,重跑会被拒。
  改过源库就要**换一个新的空目标库**(或清空当前这个)从头来。

彩排要产出两个数,正式窗口都要用:**耗时**,以及**目标库实际占用**
(`SELECT pg_size_pretty(pg_database_size(current_database()));` 在导入结束后执行)。

`--source-timezone` 未显式传时默认取**执行机**本地时区——执行机与 SQLite 部署机时区
不同的话,所有 naive 历史时间戳会整体偏移,且不报错。

彩排结束后,彩排用的目标库**不能**用作正式目标(里面是旧快照,正式流程会拒)。

## P3 正式切换(有人工闸门)

> ⚠️ 本阶段每一步都改变生产状态。Agent 到这里必须先拿到明确同意再往下走,
> 且第 1、5 步的服务启停交给人执行。

1. **停写**(人执行):公告窗口 → 停 API、后台 worker、MCP 写入方、批处理/维护脚本、
   调度器 → 待在途写入结束后停后端。
2. **建新的空目标库**,并按部署策略做好 SQLite 与 PostgreSQL 的备份(且验证过可恢复)。
   然后**必须把 `POSTGRES_MIGRATION_URL` 指到这个新库**:

   ```bash
   export POSTGRES_MIGRATION_URL='postgresql://USER:PASSWORD@HOST:5432/FINAL_EMPTY_DB'
   ```

   在同一个 shell 里接着 P2 往下做的话,这个变量还指着彩排库。忘了改会在停机窗口里
   才被"目标非空"拒掉——白白烧掉窗口时间。
3. **再跑一次 P1 预检**(此时源库已停写)。核对输出里的目标库确实是新建的那个。
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
   > ⚠️ **「PostgreSQL 上一个字都没写过」的窗口到这一步为止,而且不取决于你的数据。**
   > 启动 PostgreSQL 后端**必然**产生写入,至少三类:
   >
   > 1. **bootstrap 无条件改写内置 admin 行**——`_initialize()`
   >    (`postgres/bundle.py`)每次启动都用**新盐**重算密码哈希并
   >    `UPDATE users SET ... WHERE id='user-local'`。与数据内容无关,必然发生。
   > 2. **中断恢复**(`PostgresMaintenanceAdapter.recover_interrupted_jobs()`,
   >    readiness 之前):把遗留的 `ask_jobs`/`merge_review_jobs`/`extraction_runs`/
   >    `kg_build_jobs` running 行、`knowhow_rows` 的 syncing/pending、`sources` 的
   >    extracting/queued/parsing 收敛到终态,并清空两张 KG scratch 表。库里有这类行时发生。
   > 3. **遗留 knowhow 表自动重投影**(`_reproject_legacy_knowhow_tables`,readiness
   >    **之后**):对仍带旧版固定 KO 的 knowhow 表调度后台 cell 级重投影,**会替换 KG 对象**
   >    ——这已经是业务数据变更,且无人触发。库里有这类表时发生。
   >
   > 所以不存在「启动了但可证明没写过」的状态。要停在真正零写入,只能在**启动之前**决定。

5. **重启后端**(人执行),保持 `--workers 1`。

## P4 验收(放流量前必须全过)

```bash
curl -fsS http://127.0.0.1:8000/api/ready
```

1. `/api/ready` 返回 `"ready": true`(大库首启要等索引预载,不要提前放行)。
2. admin 与普通用户各登录一次。**登录也是 PostgreSQL 写入**
   (`/auth/login` → `create_session()` → 插入 `auth_sessions`);此后回滚会丢掉这些会话
   (代价只是重新登录,不涉及业务数据)。
3. 笔记本/来源数量与 receipt 对得上。
4. 抽查检索、Ask、知识、Memory、Knowhow、报告的代表性读取。若后端日志出现
   `scheduled cell-model reprojection`,说明库里有遗留 knowhow 表、启动已自动改写它们的
   KG 对象;等这些后台作业结束再继续,别在重投影进行中评估检索结果。
5. 确认引用到的文件能打开(见下「迁移不覆盖什么」)。
6. 做**一次**明确批准的金丝雀写入,并确认其后台任务跑完。**这是第一笔真正的业务数据
   写入**,过了它回滚就会丢业务数据。

全过之后才放流量。

## P5 回滚边界

退回 SQLite 的动作都一样(停后端 → 把 `DATABASE_URL` 改回 SQLite → 单 worker 启动 →
重跑 P4 冒烟),区别只在丢什么:

| 时点 | 回滚代价 |
| --- | --- |
| 激活之后、**启动 PostgreSQL 后端之前** | 真正的零写入。要「可证明没动过 PostgreSQL」只有这一段 |
| 启动之后、登录之前 | 必然已有 bootstrap 写入(admin 行重新加盐);库里有中间态行则还有恢复性收敛。这些回滚到 SQLite 无实质损失(SQLite 端启动做同样的事),但「零写入」不再成立。**若库里有遗留 knowhow 表,readiness 后的自动重投影会替换 KG 对象——那已经是业务数据变更**,回滚就会丢掉它 |
| 登录之后、金丝雀写入之前 | 再丢掉 `auth_sessions` 里的会话,用户重新登录即可 |
| **金丝雀写入或放流量之后** | 丢掉那些业务写入。本仓库**没有**反向导入器,也没有双写日志。此时 PostgreSQL 就是回滚边界 |

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
| `retired SQLite table <名> contains N rows` | 退役表(`articles`/`article_claims`/`derived_rule_candidates`/`extraction_candidates`)仍有数据。**这是拒绝丢弃历史数据,不是 bug**;先确认这些数据确实不再需要并自行处置。处置动作改了源库 → 按下一行换新目标 |
| `invalid JSON in <表>.<列>` / `invalid legacy vector` / `invalid timestamp` | 源库脏值。回 SQLite 侧修数据;这正是必须先彩排的原因。**修完要换新的空目标库重跑**——源库变了,密封快照 hash 就变了,旧目标里的续跑进度绑在旧 hash 上会拒绝 |
| `upgraded SQLite snapshot has the wrong schema version` / `table manifest mismatch` | 代码版本与源库 schema 不配对。用与源库匹配的代码版本,不要改 manifest 绕过 |
| `--activate-env requires --apply and --confirm-service-stopped` | 少了确认参数;先真正停写再补上 |
| 校验和不一致 | 激活会中止且不动 `.env`。不要重试激活,先查源库在导入期间是否仍在写 |
| 退出码 130 | 被 Ctrl-C/信号取消。原样重跑即可续跑 |
| 磁盘写满(尤其发生在激活步) | `<WORK_DIR>` 按 1× 备的典型症状——激活会在密封快照仍在的前提下再生成一份完整快照。扩到 ≥ 2× 后重跑激活 |
