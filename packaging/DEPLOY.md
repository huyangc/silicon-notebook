# 离线部署包 — 目标机说明

本包是**自包含**的:内含便携 Node 运行时(跑前端)+ 预编译的 Python 依赖(wheelhouse)。
目标机**无需 npm/node、无需 root**,只需一个 `python3`(≥ 3.13;SQLite 写锁公平性依赖
CPython 3.13 的 PyMutex 交接语义,见 `backend/app/repositories/sqlite/database.py`)
和一个可用的 pip 源。

## 目录结构

```
backend/       后端源码(FastAPI,纯 python)
frontend/      前端 standalone 产物(node server.js,自带精简 node_modules)
node/          便携 node 运行时(bin/node)
wheelhouse/    预编译的 python 依赖 wheel(离线安装用;若打包时 SKIP_WHEELHOUSE 则无)
scripts/       运行时/升级脚本(autotune.sh、migrate_legacy_model_env.py)
.env.example   配置模板
model-services.example.toml  系统模型服务模板(无密钥)
install.sh     一键安装(建 venv + 装依赖 + 生成模型 TOML/.env + 自检)
start.sh       启动(venv uvicorn 后端 + 便携 node 前端)
stop.sh        停止
```

## 安装 → 配置 → 启动

```bash
tar xzf silicon_notebook_<version>_linux-<arch>.tar.gz
cd silicon_notebook_<version>_linux-<arch>

./install.sh            # 建 .venv、装 python 依赖、生成 .env、就绪自检(不启动服务)

vi .local/model-services.toml
                        # 配置 [services.<id>] 的协议/URL/模型/api_key_env/
                        # max_concurrency，以及 [bindings] 的 workload→服务绑定

vi .env                 # 这里只填写 api_key_env 所引用的密钥；endpoint、模型名、
                        # bindings 和并发容量都不在 .env 配置

./start.sh              # 启动;默认后端 127.0.0.1:8000、前端 127.0.0.1:3000
```

浏览器打开前端地址即可。停止用 `./stop.sh`。

`install.sh` 在 `.local/model-services.toml` 缺失时从无密钥模板生成它；文件已存在时
原样保留。`.env.example` 默认令 `MODEL_SERVICES_CONFIG=.local/model-services.toml`，因此
默认路径在安装后一定存在。若部署明确不使用任何模型服务，可在 `.env` 留空：

```bash
MODEL_SERVICES_CONFIG=
```

此时系统以离线/确定性降级运行；不会尝试旧的逐角色 endpoint 配置。

从旧版 `.env` 升级时，可在启动前使用仓库内迁移助手：

```bash
.venv/bin/python scripts/migrate_legacy_model_env.py --env .env          # 默认只预览
.venv/bin/python scripts/migrate_legacy_model_env.py --env .env --apply  # 备份后应用
```

它会保留非模型环境变量，把旧的聊天、KG、embedding 和 rerank 配置转换为系统服务
TOML 与新的密钥槽位。推算出的并发容量仅是迁移初值，应用前应复核；可用可重复的
`--max-concurrency ROLE=N` 覆盖。目标 TOML 若只是安装器生成且未改动的模板，可直接
替换；其他已存在配置需显式传入 `--force`。两种情况都会先备份旧文件，并把当前
`.env` 与含密钥的备份权限收紧为 `0600`。脚本不会把密钥写入 TOML 或输出到终端。

### 模型服务 TOML 约定

- `[services.<id>]` 定义一个物理 chat、embedding 或 rerank 服务；`max_concurrency`
  是该物理服务唯一的模型并发容量，同服务的所有 workload 共用同一调度器。
- `[bindings]` 把稳定 workload id（如 `ask_answer`、`kg_extract`、
  `retrieval_query_embedding`）绑定到同种类服务。
- `api_key_env` 只写密钥环境变量名；实际密钥只填进 `.env`，不要写进 TOML。
- 一个后端进程统一维护队列、熔断与健康状态；`start.sh` 固定 `--workers 1`，避免容量倍增。

## 常用可配置项

安装(`install.sh`):

| 变量 | 作用 |
|---|---|
| `PYTHON_BIN` | 指定 python 解释器(默认 `python3`);想用 conda/pyenv 的 python 时用它 |
| `PIP_INDEX_URL` | pip 源地址(wheelhouse 缺包时在线补装用) |

启动(`start.sh`):

| 变量 | 默认 | 作用 |
|---|---|---|
| `FRONTEND_HOST` | `127.0.0.1` | 前端监听地址;设 `0.0.0.0` 对外暴露 |
| `FRONTEND_PORT` | `3000` | 前端端口 |
| `BACKEND_HOST` | `127.0.0.1` | 后端监听地址 |
| `MCP_REQUIRE_HTTPS` | `0` | MCP 是否强制 HTTPS。默认关(允许内网明文+放宽 Host 校验);公网设 `1` |
| `PORT` | `8000` | 后端端口 |
| `MODEL_SERVICES_CONFIG` | `.local/model-services.toml` | 系统模型服务 TOML；留空即明确离线运行 |
| `SILICON_NOTEBOOK_ADMIN_PASSWORD` | — | 对外暴露(非 loopback)时**必须**设为非默认值,否则拒绝启动 |
| `ALLOW_NO_ENV_FILE` | `0` | 设 `1` 则允许无 `.env`、仅用系统环境变量启动 |

## SQLite / PostgreSQL 直接选择与生产切换

`DATABASE_URL` 是唯一 active repository 后端选择器；一次只会构造 SQLite
或 PostgreSQL 中的一个。发行默认仍是 SQLite，但 PostgreSQL adapter 已可直接
启动。两者都会在启动时自动运行自己的 migration。PostgreSQL 向量使用
float32 `bytea`，不安装也不需要 pgvector。打包启动固定 `--workers 1`，不要
手工改成多 worker。

`.env` 中二选一：

```dotenv
# 默认；包目录下的相对路径
DATABASE_URL=sqlite:///.local/silicon_notebook.db

# 已由 DBA 创建好的 UTF8 数据库；特殊字符需 URL 编码
DATABASE_URL=postgresql://silicon_app:change-me@127.0.0.1:5432/silicon_notebook
```

SQLite 绝对路径例子是
`sqlite:////srv/silicon-notebook/silicon_notebook.db`。源码部署可用
`PYTHON_BIN="$PWD/.venv/bin/python" scripts/backend.sh status`看安全 identity：
`database=sqlite path=...` 或 `database=postgresql host=... db=...`。密码、userinfo
和 query option 不会进入 status/readiness 日志；离线包通过 `.local/logs/backend.log`
与 `/api/ready` 核对同一 identity/readiness。

### A. 全新 PostgreSQL 直接启动

`pg_trgm` 必须安装在 `public` schema。应用数据库 owner 必须有权执行
`CREATE EXTENSION pg_trgm`，否则 DBA 必须在首次应用启动前预装到 `public`。用不含
凭据的 SQL 验证：

```sql
SELECT e.extname, n.nspname
FROM pg_extension e
JOIN pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = 'pg_trgm';
```

`pg_trgm | public` 表示前置条件已就绪。若查询无行，首次 migration 会自动尝试 `CREATE EXTENSION pg_trgm`；
应用数据库 owner 必须有该权限，否则由 DBA 预装到 `public`。
既有 `pg_trgm` 位于其他 schema 时会 fail closed。

1. DBA 创建专用 UTF8 空库和 login owner；应是
   `NOSUPERUSER NOCREATEDB NOCREATEROLE`，有足够连接/磁盘容量，并且备份策略已演练。
2. `./stop.sh`，确认旧 backend 已停止；绝不边写边改 URL。
3. 修改 `.env` 的唯一 `DATABASE_URL`；保持正确的
   `SILICON_NOTEBOOK_STORAGE_DIR`。按需设置 `POSTGRES_POOL_MIN_SIZE`/
   `POSTGRES_POOL_MAX_SIZE` 及 pool acquire/statement/lock timeout，不要取消有界 timeout。
4. `./start.sh`。首次启动在 advisory lock 下自动 migration；在完成前 readiness
   不会发布为 true。
5. `curl -fsS http://127.0.0.1:8000/api/ready`，再核对登录、notebook/source/
   Memory 数量和代表性读取；全部通过才放入流量/写入。

### B. 从 PostgreSQL 切回原 SQLite

1. 停止新写入并执行 `./stop.sh`。
2. 核对原 SQLite 主文件及 `-wal`/`-shm` sidecar/备份，再把 `.env` 改回该文件 URL。
3. `./start.sh`，要求 `/api/ready`、登录、数量和抽样读取都通过后再放流量。

切回只会显示 SQLite 最后一次存下的状态，不会把 PostgreSQL 写入回放到
SQLite。只有 PG 切换后无新写入，或所有 PG-only 写入已经外部对账并验证，
回滚才能无损。

### C. 备份与回滚闸门

- 每次改 URL 前先停写和后端。SQLite 用 backup API，例如
  `sqlite3 /path/live.db ".backup '/secure/sqlite-before-switch.db'"`。后端已停时可先跑
  `PRAGMA wal_checkpoint(TRUNCATE)`。不得只拷贝活跃 WAL 下的主文件。
- PostgreSQL 用已演练的 `pg_dump --format=custom`/`pg_restore` 或组织标准物理备份；
  凭据经 `.pgpass`/service definition 传递，不进 shell history/日志。
- `/api/health` 只是 liveness；切换放流量闸门必须包含 `/api/ready`、认证与业务
  smoke。失败时保持停流量，恢复备份或原 URL，然后重跑整个验证。

### D. 存量 SQLite → PostgreSQL 的明确边界

只改 `DATABASE_URL` **不会**复制、迁移或同步存量数据。当前包提供显式、单向的
SQLite→PostgreSQL forward-shadow CLI，但没有 cutover、反向复制或应用 dual-write。
`SHADOW_DATABASE_URL` 只标识 shadow target，单独设置不启动同步，也不改变 active backend。

从包根目录运行，且让 `DATABASE_URL` 始终指向 active SQLite：

1. 恢复演练 SQLite DB + storage 和 PostgreSQL 目标备份，记录 evidence ID 与 target capacity。
2. 设置 `SHADOW_DATABASE_URL` 为专用 PostgreSQL 16 UTF-8 target；`public.pg_trgm` 必须可用。
3. 执行 `PYTHONPATH=backend python scripts/migrate_sqlite_to_postgres.py preflight ... --json`，
   私密保存 confirmation token。
4. 用 token 执行 `... start-forward ...`；命令可续跑，并生成 owner-only worker token。
5. 用 `scripts/shadow.sh start RUN_ID WORK_DIR` 启动恰好一个受监督 worker。
6. 用 `... status --json` 监视 live worker、checkpoint/high-water、lag 与 poison；用 fresh
   preflight token 运行两次 `... verify --level full --json`。

要求零 poison、lag 追平并稳定至少 60 秒、两次连续 FULL complete/100% coverage。该结果只说明
shadow 健康，**不授权**修改 `DATABASE_URL`。完整参数、token 续签、SIGTERM、retention 与 poison
处理见 `docs/operations_zh.md`。

### E. 切换决策表 / checklist

| 目标 | `DATABASE_URL` | 可见数据 | 安全回滚条件 |
|---|---|---|---|
| 默认 SQLite | `sqlite:///.local/silicon_notebook.db` | 该 SQLite 文件 | 存在一致 SQLite 备份 |
| 全新 PostgreSQL | `postgresql://user:password@host:5432/new_db` | 新库，初始仅 bootstrap 行 | PG 新写入前回原 SQLite，或已对账 |
| 切回原 SQLite | `sqlite:////absolute/path/original.db` | SQLite 最后存下的状态 | 放弃或已对账 PG-only 写入 |
| 正向 shadow | SQLite URL（不变） | 应用仍读写 SQLite；PG 是禁止业务访问的 shadow | 停 worker 即可，不影响 SQLite |
| 存量 SQLite 迁 PG cutover | **本阶段不支持** | 不得把流量导向 shadow | 等待另行实现并评审 cutover |

若使用全新 PostgreSQL 或已由其他流程完成的 direct-backend 切换，操作顺序不可缩减：
隐去凭据核对 identity → 停写/停 backend → 两端备份
→ 验证 target UTF8/owner/capacity → 改唯一 URL → 以 `--workers 1` 启动/自动 migration
→ status/`/api/ready`/认证/数量/抽样 smoke → 放流量。
该顺序不适用于上面的 forward shadow；shadow 全程不改 URL、不停 active SQLite。

设计边界见
`docs/superpowers/specs/2026-07-22-postgresql-shadow-cutover-design.md`；已交付 adapter 与
forward-shadow 实现分别对应 `docs/superpowers/plans/2026-07-22-postgresql-repository-adapter.md`
和 `docs/superpowers/plans/2026-07-22-postgresql-forward-shadow-sync.md`。Cutover 仍是后续阶段。

### F. 离线 batch ingest 边界

`batch_ingest.py` 的变更阶段仅支持 SQLite。若 active `DATABASE_URL` 是 PostgreSQL，
CLI 会在构造 SQLite repository 前返回状态码 `2`，且错误不打印 URL/密码。PostgreSQL
请使用正常应用/API 上传与 KG/reindex 流程；不要把 SQLite maintenance 命令指向 PG。
`--dry-run` 只扫描文件系统，不构造 repository，因此两种配置都可使用。

## 排障

- **`python -m venv` 失败(缺 ensurepip / python3-venv)**:装它需 root。`install.sh` 会自动尝试
  `--without-pip` + 用 wheelhouse 里的 pip 轮子兜底;仍失败时按提示让管理员 `apt-get install python3-venv`,
  或设 `PYTHON_BIN` 指向 conda/pyenv 的 python。
- **wheelhouse 装不上(版本标签不匹配)**:wheel 是按打包机 python 小版本(如 cp313)编译的。若目标机
  python 小版本不同,C 扩展 wheel 会装不上——`install.sh` 会自动回退到在线 pip 补装。最稳做法是让打包机
  python 小版本与目标机一致。
- **前端起来但接口 404 / CORS**:前端通过同源反代把 `/api/*` 转发到后端 `127.0.0.1:8000`。确认后端已在
  `.local/logs/backend.log` 正常起来,且未改动后端端口而没同步改反代目标(反代目标在打包时烘焙)。
- 日志在 `.local/logs/`(backend.log / frontend.log),PID 在 `.local/run/`。
