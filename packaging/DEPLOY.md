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
