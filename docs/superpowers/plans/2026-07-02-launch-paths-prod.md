# 启动路径锚定 + 生产启动命令 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景(真机第三次踩「双 .local」)**:相对路径按进程 CWD 解析,而启动方式各异 → 数据/索引分裂:
- `npm run dev` → `scripts/dev.sh` → **`cd backend/` 再起 uvicorn** → DB/storage 解析到 `backend/.local`;
- 离线 CLI(README 口径从仓库根跑)→ 根 `.local`;
- 部署机实况:CLI 在根 `.local` 建好 113万节点索引,`npm run dev` 的服务在 `backend/.local` 找 → 图谱页永远「构建中」。
- `backend.sh` 注释声称「DB 解析到仓库根/.local」——现状下是错的(cd backend 后是 backend/.local)。
- 此外没有生产启动命令(只有 dev)。

**Goal**:①代码层把相对路径**锚定到仓库根**(先例:event_logging.py 的 `_ROOT_DIR`),启动目录从此无关;②`npm run start`/`scripts/prod.sh` 生产启动(前端 build+start,后端 uvicorn 单进程);③启动日志打印解析后的绝对路径,一眼可查;④README 双语部署段更新 + 老部署一次性迁移说明。

**Tech Stack**:pydantic-settings v2、bash、Next.js。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;worktree `backend/` 跑 pytest,基线 **1552 passed, 1 skipped**(#157 后 master,以实跑为准)。

## Global Constraints
- **绝对路径 env 覆盖永远原样尊重**(`SILICON_NOTEBOOK_STORAGE_DIR=/abs`、`sqlite:////abs`);只有**相对**默认/相对 env 值被锚定到仓库根。
- 锚定实现集中一处(Settings 后处理/validator),锚点 = `Path(__file__).resolve().parents[3]`(与 event_logging `_ROOT_DIR` 同口径,worktree 里各自锚各自根——现状语义,不变)。
- `env_file` 同步锚定:`(_ROOT/".env",)` 绝对化(现为 `("../.env", ".env")` 双 CWD 依赖)。⚠ 测试大量用 monkeypatch.setenv——env 变量优先级高于 env_file,不受影响;确认无测试依赖「CWD 下 .env 被加载」。
- **迁移语义变化要显式**:锚定后,过去把数据落在 `backend/.local` 的部署(cd backend 启动的)会改指根 `.local`。启动日志必须打印解析结果;README 写一次性迁移(把 `backend/.local` 整体 mv 到根 `.local`,或设绝对 env 保持原位)。**代码绝不自动搬数据**。
- 生产后端**单进程**(`--workers 1` 显式 + 注释:进程内缓存/去重集合不支持多 worker,×N 内存);前端 `next build` + `next start`。
- README/README_zh 通用口径,不写机器特定路径(committed-docs-stay-generic)。

---

## Task 1: Settings 路径锚定 + 启动日志(backend)

**Files:** `backend/app/core/config.py`;Test 新建 `backend/tests/test_settings_path_anchor.py`。

- [ ] Step 1 测试先行:
  - `os.chdir` 到 tmp 任意目录后构造 `Settings()`(清掉相关 env)→ `storage_dir`、`database_path`(sqlite 文件路径)均为**仓库根下**绝对路径;chdir 到 backend/ 再构造 → 相同结果(CWD 无关性)。
  - 绝对 env 覆盖:`SILICON_NOTEBOOK_STORAGE_DIR=/tmp/x`、`DATABASE_URL=sqlite:////tmp/y.db` → 原样尊重不重锚。
  - 相对 env 值(如 `DATABASE_URL=sqlite:///.local/foo.db`)→ 也锚定到仓库根(与默认同规则)。
  - 非 sqlite 的 database_url(如 postgres://)不动。
- [ ] Step 2 实现:
  - config.py 顶部 `_ROOT_DIR = Path(__file__).resolve().parents[3]`;`model_config` 的 `env_file` 改 `str(_ROOT_DIR / ".env")`。
  - `@model_validator(mode="after")`(或 field_validator)统一后处理:storage_dir 相对→`str(_ROOT_DIR / v)`;database_url 以 `sqlite:///` 开头且路径部分为相对→重写为根锚定的 `sqlite:///` 绝对形式。注意 `sqlite:///` 三斜杠+相对 vs 四斜杠绝对的拼写正确性(现有 `database_path` 属性 L386 的解析要继续工作,测试覆盖)。
  - 仓内既有测试大量 `DATABASE_URL=sqlite:///{tmp_path}/t.db`(绝对)→ 不受影响;跑全量验证。
  - 启动日志:`create_app()`(backend/app/main.py)起始处 log 一行 `paths: db=<绝对> storage=<绝对> logs=<绝对>`(INFO,uvicorn 控制台可见)。
- [ ] Step 3 回归:新文件 + 全量(基线 1552)。
- [ ] Step 4 提交 `fix(config): 相对路径锚定仓库根(DB/storage/env_file 与启动目录解耦)+ 启动路径日志`。

## Task 2: 生产启动命令 + 脚本修缮 + README(scripts/frontend/docs)

**Files:** 新 `scripts/prod.sh`、`package.json`(根)、`scripts/dev.sh`(注释)、`scripts/backend.sh`(错误注释)、`README.md`/`README_zh.md`。

- [ ] Step 1 `scripts/prod.sh`(仿 dev.sh 结构:ROOT_DIR/PYTHON_BIN 探测/加载根 .env/trap 清理):
  - 前端:`cd frontend && npm run build`(有 node_modules 检查)→ `npm run start -- -p "${FRONTEND_PORT:-3000}"` 后台;
  - 后端:`cd backend && uvicorn app.main:app --host "${BACKEND_HOST:-0.0.0.0}" --port "${PORT:-8000}" --workers 1`(注释:进程内缓存不支持多 worker);日志落 `"$ROOT_DIR/.local/logs/"`(`2>&1`,勿写成 2>1);
  - 前台守护两个子进程(wait 任一退出即清理),Ctrl-C 优雅退出;`--build-only`/`SKIP_BUILD=1` 可选(实现者裁量,写 README)。
- [ ] Step 2 根 package.json 加 `"start": "bash scripts/prod.sh"`;dev.sh 里补一行注释(路径已锚定,cd backend 只为模块导入);backend.sh 把「DB 解析到仓库根」注释改为如实描述(锚定后恰好为真,更新措辞即可)。
- [ ] Step 3 README.md + README_zh.md「Deployment」段重写(通用):`npm run start` 生产启动;路径锚定语义(启动目录无关,日志首行可核对);**一次性迁移注意**:此前用 `npm run dev`/`cd backend` 启动且数据在 `backend/.local` 的部署,升级后需 `mv backend/.local/* <repo>/.local/` 合并(或设绝对路径 env 保持原位);CLI 与服务共享同一套根 `.local`。
- [ ] Step 4 验证:`bash -n scripts/prod.sh` 语法;`cd frontend && npx tsc --noEmit`(未动前端代码,应 clean);人工 dry 检查脚本逻辑(不真起服务——环境无 node 生产依赖也不许启动用户服务)。
- [ ] Step 5 提交 `feat(ops): 生产启动 scripts/prod.sh + npm start(前端 build/start + 后端单进程)+ 部署文档与迁移说明`。

## 收尾
- 单 PR 两任务;终审重点=锚定的迁移面(哪些既有部署形态受影响、README 是否把每种都讲清)→ rebase → push → PR。
