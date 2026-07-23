# 离线部署包 — 目标机说明

本包是**自包含**的:内含便携 Node 运行时(跑前端)+ 预编译的 Python 依赖(wheelhouse)。
目标机**无需 npm/node、无需 root**,只需一个 `python3`(≥ 3.10)和一个可用的 pip 源。

## 目录结构

```
backend/       后端源码(FastAPI,纯 python)
frontend/      前端 standalone 产物(node server.js,自带精简 node_modules)
node/          便携 node 运行时(bin/node)
wheelhouse/    预编译的 python 依赖 wheel(离线安装用;若打包时 SKIP_WHEELHOUSE 则无)
scripts/       运行时脚本(autotune.sh)
.env.example   配置模板
install.sh     一键安装(建 venv + 装依赖 + 生成 .env + 自检)
start.sh       启动(venv uvicorn 后端 + 便携 node 前端)
stop.sh        停止
```

## 安装 → 配置 → 启动

```bash
tar xzf silicon_notebook_<version>_linux-<arch>.tar.gz
cd silicon_notebook_<version>_linux-<arch>

./install.sh            # 建 .venv、装 python 依赖、生成 .env、就绪自检(不启动服务)

vi .env                 # 必填:模型服务 URL(OPENAI_COMPAT_* / EMBED_* / RERANK_* …)
                        # 服务靠这些 URL 才能工作,缺失不会静默降级

./start.sh              # 启动;默认后端 127.0.0.1:8000、前端 127.0.0.1:3000
```

浏览器打开前端地址即可。停止用 `./stop.sh`。

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

只改 `DATABASE_URL` **不会**复制、迁移或同步存量数据。本 adapter 阶段
没有 dual-write、shadow replication 或 cutover。`SHADOW_DATABASE_URL` 只会被保留/
校验，它不启用同步。在
`docs/superpowers/plans/2026-07-22-postgresql-forward-shadow-sync.md` 被实现并受保护之前，
只支持全新 target，或由外部工具在完全停机状态下迁移、对账并演练回滚。
本包没有可以虚构使用的存量迁移命令。

### E. 切换决策表 / checklist

| 目标 | `DATABASE_URL` | 可见数据 | 安全回滚条件 |
|---|---|---|---|
| 默认 SQLite | `sqlite:///.local/silicon_notebook.db` | 该 SQLite 文件 | 存在一致 SQLite 备份 |
| 全新 PostgreSQL | `postgresql://user:password@host:5432/new_db` | 新库，初始仅 bootstrap 行 | PG 新写入前回原 SQLite，或已对账 |
| 切回原 SQLite | `sqlite:////absolute/path/original.db` | SQLite 最后存下的状态 | 放弃或已对账 PG-only 写入 |
| 存量 SQLite 迁 PG | **外部停机迁移后**的 PG URL | 只有外部复制且验证的数据 | 两端备份与回滚演练都完成 |

实际操作顺序不可缩减：隐去凭据核对 identity → 停写/停 backend → 两端备份
→ 验证 target UTF8/owner/capacity → 改唯一 URL → 以 `--workers 1` 启动/自动 migration
→ status/`/api/ready`/认证/数量/抽样 smoke → 放流量。

设计边界见
`docs/superpowers/specs/2026-07-22-postgresql-shadow-cutover-design.md`，已交付 adapter 见
`docs/superpowers/plans/2026-07-22-postgresql-repository-adapter.md`；forward-shadow 文档是未实现的下一阶段。

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
