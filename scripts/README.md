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

系统模型服务由维护人员统一配置：

```bash
cp model-services.example.toml .local/model-services.toml
vi .local/model-services.toml   # 服务、workload 绑定、每服务 max_concurrency
vi .env                         # MODEL_SERVICES_CONFIG + api_key_env 引用的密钥
scripts/backend.sh restart
```

`max_concurrency` 是每个物理模型服务唯一的并发容量；批处理 `--workers`、KG 来源任务数和本地 CPU/ANN 线程都不会覆盖它。普通用户的「模型服务」面板只读，页面加载不会自动探测；admin 可在面板显式测试单个或全部服务。用户遇到模型错误时应提交界面中的 support id，维护人员据此关联 `.local/logs/` 与只读服务状态，定位具体故障服务。修改 TOML 或密钥后必须重启后端；配置路径留空是明确离线模式，非空但无效会启动失败。

环境变量:`PYTHON_BIN` `HOST`(默认 127.0.0.1) `PORT`(默认 8000) `LOG_FILE`。
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

## 三、慢因诊断(分析"慢的现象")

### `diag.py` —— 统一入口(一个命令 + 子命令)
```bash
python3 scripts/diag.py                 # 裸跑 = slow(部署机慢因全量报告)
python3 scripts/diag.py slow --since 24 --deep
python3 scripts/diag.py latency --log .local/logs/events.jsonl --last 500
python3 scripts/diag.py base-recall [active_notebook_id] [查询词]
```
把散在三处、三种跑法的慢因诊断收敛到一个入口。三个子命令各委托一个既有引擎(引擎本身保持原样、仍可单独运行,老路径/cron 不受影响):

| 子命令 | 干什么 | 运行要求 | 委托 |
|--------|--------|----------|------|
| `slow` | 离线从 `.local` 日志/工件捞慢因证据(请求延迟分位 / 事故观测事件 / LLM 延迟 / 规模画像 / reasoning-PPR 审计 / env),出**可直接粘贴**的文本报告 | **纯 stdlib、只读、脱敏**,可在持有 `.local/` 的**部署机**上跑,不 import app | `diag_slow.py` |
| `latency` | 从 `events.jsonl` 的 `ask_stage` 事件出**每阶段 P50/P95/max**(自动并入 per-user 子目录) | 纯 stdlib,不 import app | 聚合口径与 `app/eval/ask_latency.py` 一致(有漂移守卫测试) |
| `base-recall` | 活体连真实库/env,诊断「深度报告 / reasoning 为何不引用参考库」(最常见根因=这个笔记本压根没挂任何参考库,和索引无关——见下方脚本自身的分级说明) | **需 import app**(懒加载:仅此子命令拉起 app),在能加载真实 `.env`+库的机器上跑 | `diag_base_report.py` |

**离线纯净性是硬约束**:`slow` / `latency` 绝不 import app(`diag.py` 自身零 DB 调用、零 app 依赖),这样在裸机上随手就能跑;只有 `base-recall` 才懒加载 app。

### `diag_open_latency.py` —— 打开大 notebook 仍卡几秒的残余定位
```bash
python3 scripts/diag_open_latency.py                 # 自动取最大 notebook
python3 scripts/diag_open_latency.py --notebook nb-xxx
```
主机侧只读诊断(纯 stdlib、mode=ro、不 import app,与 `diag_slow.py` 同款)。计数缓存等读侧修复落地后,若打开最大库仍卡几秒,它把残余定位到:计数缓存**冷成本**(首开/每次 KG 变更后重付)、`from_row` 里未缓存的子查询(`pending_kg_source_count` 相关子查询)、以及**生产请求日志里各端点的真实 P50/P95/max**(哪个 HTTP 请求真的是秒级)+ `dirty`/seq churn(缓存是否被后台变更反复冲掉)。

> 兼容:三个引擎脚本(`diag_slow.py` / `diag_base_report.py` / `app/eval/ask_latency.py`)**未改动、仍可单独运行**——`python3 scripts/diag_slow.py --since 24` 等老命令继续有效。`diag.py` 只是新增的统一入口。
>
> 区分:`bench_sqlite_writes.py`(合成写吞吐**基准**)与 `replay_retrieval.py`(检索**回归对照**)不属于"慢因诊断",见下表。

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
| `validate_concept_filter.py` | 离线试跑 concept 噪声过滤(无 LLM/不写库) |
| `validate_overmerge_fix.py` | 验证 concept 去过度合并 |
| `git-cleanup.sh` | 清理「PR 已合并」的本地分支 + worktree:默认 dry-run 预演,`--apply` 执行,`--remote` 连带删远程(保护 master / 当前分支 / `eval` / `backup/*`) |

---

## 常见坑
- **后端无 `--reload`**:改了 `.env` 或后端代码,必须 `backend.sh restart` 才生效。
- **必须用对的 Python**:没装依赖的 `python` 会 `ModuleNotFoundError`。设 `PYTHON_BIN` 或激活对应环境。
- **`:8000` 起错服务** → 前端 `/api/notebooks` 404、notebook 看似消失。`backend.sh status` 一查便知,`restart` 修复;数据始终在 `.local/silicon_notebook.db`。
- **多 worktree / root 共用一个库**:`.local` 在仓库根;从 worktree 手敲 uvicorn 可能连到 worktree 自己的空 `.local`。用 `backend.sh`(它固定指向仓库根)最稳。
