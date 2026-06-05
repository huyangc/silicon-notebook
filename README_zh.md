# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是一个面向半导体研发团队的 knowhow notebook 平台。它把上传的技术文档转化成可查询的知识图谱（Concept / Claim / Formula / Procedure 对象），并提供元素级 evidence 引用与接地多轮问答。

## 当前范围

当前仓库已进入以 KG-native 管线为核心的本机真实 beta 闭环：

- Python FastAPI 后端；SQLite 持久化路径 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端
- OpenAI-compatible LLM 端点，用于抽取、回答和文章研究；embedding 通过 `EMBED_*` 独立配置
- 未配置 LLM/embedder 时全管线可离线运行（deterministic fallback）
- 干净起点：全新数据库只初始化本机用户，不预置 demo 笔记本或合成来源
- 支持 PDF、Markdown、DOCX、PPTX、CSV、XLSX 的 multipart 文件上传（异步 BackgroundTasks）
- **KG-native 摄取**：结构化 Markdown 解析 → 贪心窗口化 KG 抽取（Concept / Claim / Formula / Procedure）并发 embedding → 抽取优先状态（`extracted` = KG 就绪，不等 embedding）
- PDF 走 MinerU（公式/表格/版面）；本机或未配置时回退 pypdf
- 混合检索：CJK 感知 bi-gram 关键词 + float32 矩阵语义检索（每 notebook 独立缓存）
- KG-native 接地问答：逐句 `[k_i]` 引用、多轮会话、1-hop KG 邻居扩展
- 知识治理：通过 `/knowledge-types` + `/knowledge?type=...` 浏览任意对象类型，状态生命周期，重复检测与合并；`deprecated` 对象从检索和 1-hop 扩展中排除
- 统一 KG：跨文档概念聚类（`concept_clusters`），待合并审核
- Object 级 KG 可视化：Concept / Claim / Formula / Procedure 节点，类型形状、边标签、多选过滤、按类型分组侧栏
- Notebook 集合页（网格/紧凑/列表、编辑/删除）；点击「＋ 新建」直接创建 `Untitled notebook` 并进入，无弹窗
- 第一版不使用 Docker

PostgreSQL + pgvector 仍是后续生产/团队 beta 目标，当前本机开发不需要。

## 本机设置

复制环境变量模板：

```bash
cp .env.example .env
```

默认本机数据库配置为：

```text
DATABASE_URL=sqlite:///.local/silicon_notebook.db
```

默认 CORS 会放行 `localhost:3000` 和 `localhost:3001`，因为当 `3000` 被占用时 Next.js 会自动切到 `3001`。

后端统一使用本机已有的 Miniconda Python：

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python --version
```

安装后端依赖到这个共享环境：

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install -r backend/requirements.txt
```

安装前端依赖：

```bash
cd frontend
npm install
```

### 手动启动（推荐给 agent / 真实处理）

后端请**不要带 `--reload`**，否则文件一变动 uvicorn 就重启 worker，会**杀掉进行中的 `BackgroundTask`**，导致上传的 source 卡在 `parse_status=extracting`（解析→嵌入→抽取无法跑完）。

```bash
# 后端（不带 --reload）：前台运行，或用 & / nohup 放后台
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
# 前端（另开一个终端）
cd frontend
npm run dev
```

放后台并记录日志（适合 agent 调用）：

```bash
cd backend
nohup /opt/homebrew/Caskroom/miniconda/base/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 > /tmp/sn-backend.log 2>&1 &
```

健康检查 / 打开 UI：

```bash
curl -s http://127.0.0.1:8000/api/health      # {"status":"ok", "llm_configured":...}
open http://localhost:3000
```

后端会把结构化日志写入 `.local/logs/`（同时输出控制台简要行），便于查看系统在做什么、上传卡在哪一步：

```bash
tail -f .local/logs/requests.jsonl   # 每个 HTTP 请求：方法/路径/状态码/耗时/request_id（慢请求标 SLOW）
tail -f .local/logs/events.jsonl     # 异步管线阶段（parse/embed/extract）+ 状态机跃迁 + 失败原因
tail -f .local/logs/llm.jsonl        # 大模型调用：chat（prompt/响应/token/耗时）+ embedding 摘要 + 错误
```

响应头 `X-Request-Id` 可把浏览器动作与服务端日志行关联；DevTools console 也会打印 `[api] 方法 /路径 -> 状态 N毫秒 (request_id)`。详见下方“可观测性 / 日志”。

### 快速启动（仅用于开发迭代）

```bash
npm run dev    # 仓库根目录：后端(uvicorn --reload) + Next.js 前端
```

后端 `http://localhost:8000`，UI `http://localhost:3000`。此路径用了 `--reload`，**只适合改 UI/代码时用，处理上传时不要用**（见上方警告）。若 `frontend/node_modules` 不存在，请先在 `frontend/` 下 `npm install`。

## 产品流程

外层页面为 notebook 集合页（KG-native 管线）：

1. 点击「＋ 新建」——系统立即创建 `Untitled notebook` 并进入，无弹窗。
2. 上传 PDF、Markdown、DOCX、PPTX、CSV 或 XLSX 来源（multipart）。
3. 后端：结构化 Markdown 解析 → KG 抽取（Concept / Claim / Formula / Procedure，16 线程并发窗口化）在前台运行，元素向量化同时在后台 daemon 线程并发执行。
4. 来源在 KG 抽取完成后立即变绿（`extracted`）——无需等待向量化完成。
5. 知识对象写入 `knowledge_objects` + `knowledge_relations`，并绑定元素级 evidence。
6. 混合检索（bi-gram 关键词 + float32 矩阵语义）驱动 KG-native 问答：答案含逐句 `[k_i]` 引用，支持多轮会话，并沿 KG 关系做 1-hop 邻居扩展。
7. 统一 KG 跨文档聚合概念；待合并的跨文档概念对可逐一确认或拒绝。

进入单个 notebook 后：

- 左栏：用户导入来源文件，实时显示 parse-status（绿色仅给 `extracted`，其余处理中为橙色），支持详情预览和删除。网络来源检索暂不开放。
- 中栏：两个 tab——**问答**（KG-native 接地问答，逐句 `[k_i]` 引用，多轮会话列表，👍/👎 反馈）和**知识库**（从 `/knowledge-types` 动态获取类型，支持状态生命周期、重复检测与合并）。
- 知识图谱以全屏浮层打开：object 级 KG 节点（Concept / Claim / Formula / Procedure），类型形状，边关系标签，多选类型过滤，按类型分组侧栏（选中节点聚焦画布）。
- 右栏：Studio，含文章、派生规则候选和知识图谱入口。

notebook 工作区隐藏集合页全局上边栏，采用偏工程风格的视觉治理。

## API

当前 beta 的关键 API：

- `GET /api/notebooks`、`POST /api/notebooks`、`PATCH /api/notebooks/{id}`、`DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `POST /api/notebooks/{id}/sources` — multipart 文件上传（异步解析/抽取）
- `GET /api/sources/{id}`、`DELETE /api/sources/{id}`、`POST /api/sources/{id}/parse`、`GET /api/sources/{id}/elements`
- `GET /api/notebooks/{id}/knowledge-types`、`GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`、`PATCH /api/knowledge/{id}`
- `GET /api/notebooks/{id}/graph`
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask` — KG-native 接地问答（逐句 `[k_i]` 引用）
- `GET /api/notebooks/{id}/conversations`、`GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- `GET|POST /api/notebooks/{id}/articles`、`DELETE /api/articles/{id}`、`POST /api/articles/{id}/research`
- 统一 KG：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`POST .../unified-kg/merges/{id}/confirm|reject`
- `GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`
- `GET /api/object-schemas`、`POST /api/object-schemas`、`PATCH /api/object-schemas/{type}`、`DELETE /api/object-schemas/{type}`
- `GET /api/notebooks/{id}/duplicates`、`POST /api/knowledge/{id}/merge`
- `GET /api/notebooks/{id}/derived-rules`、`POST /api/derived-rules/{id}/approve|reject`

## 配置

所有模型服务均通过 URL 端点接入，不启动本地模型服务。

**LLM（OpenAI-compatible）：**

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS   # 默认 60
OPENAI_COMPAT_MAX_RETRIES       # 默认 2
```

**嵌入（向量检索）：**

```text
EMBED_PROVIDER          # ""=关闭（仅关键词） | dashscope
EMBED_MODEL             # 嵌入模型名，如 text-embedding-v4
EMBED_BASE_URL          # 嵌入端点 URL
EMBED_API_KEY
EMBED_DIM               # 须与模型输出维度一致（默认 1024）
EMBED_TRUNCATE_CHARS    # 每段文本喂给 embedder 的最大字符数（默认 2000）
EMBED_BATCH_SIZE        # 每次嵌入调用的元素数（默认 10）
EMBED_PERSIST_CHUNK     # 每批落库行数（默认 200）
EMBED_CONCURRENCY       # 并发嵌入线程数（默认 50）
```

**KG 抽取窗口化：**

```text
KG_WINDOW_TARGET_CHARS      # 贪心打包目标窗口字符数（默认 9000）
KG_WINDOW_OVERLAP_CHARS     # 相邻窗口重叠字符数（默认 450）
KG_EXTRACT_WORKERS          # 窗口抽取线程池大小（默认 16）
KG_WINDOW_WARN_THRESHOLD    # 窗口数超此值记 WARNING（默认 1200）
```

**数据库：**

```text
DB_BUSY_TIMEOUT_MS      # SQLite busy_timeout（毫秒，默认 30000）
DATABASE_URL            # SQLite 路径（默认 .local/silicon_notebook.db）
STORAGE_DIR             # 上传文件存储目录
```

**检索：**

```text
RETRIEVAL_TOP_N         # 1-hop 扩展前的 top-N 命中数（默认 12）
```

**MinerU（PDF 解析）：**

```text
MINERU_MODE             # off（默认） | http | cli
MINERU_API_URL          # 远端 mineru-api 端点（http 模式）
MINERU_BACKEND          # pipeline | vlm-auto-engine | vlm-http-client | vlm-sglang-client
MINERU_VLM_SERVER_URL   # 独立 VLM 推理服务器 URL
MINERU_PARSE_METHOD     # auto | txt | ocr
MINERU_LANG             # 如 en、ch
MINERU_MODEL_SOURCE     # huggingface | modelscope
MINERU_TIMEOUT_SECONDS  # MinerU 调用超时
MINERU_FORMULA_ENABLE   # true/false
MINERU_TABLE_ENABLE     # true/false
```

**日志：**

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS
EVENT_LOG_ENABLED / EVENT_LOG_DIR
SLOW_REQUEST_MS         # 超过该毫秒数的请求标记 SLOW（默认 3000）
SILICON_NOTEBOOK_CORS_ORIGINS
```

没有配置 LLM 时，摘要和回答退化为 deterministic 行为；source 解析仍会完整执行，KG 抽取阶段记录完成的 `no-llm` run，不生成合成知识。

## 可观测性 / 日志

后端通过统一的 `EventLogger`（`app/core/event_logging.py`）输出结构化日志：每条事件一行 JSONL 写入 `.local/logs/`，并附控制台简要行。写日志是 best-effort，绝不影响它所观测的请求或管线；未配置模型时 LLM 通道为 no-op。

- `requests.jsonl` — 每个 HTTP 请求（方法、路径、状态码、耗时、`request_id`）。超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 关联前后端。
- `events.jsonl` — 异步来源管线：各阶段（`parse` / `embed` / `extract`）耗时与每次状态机跃迁。卡住时能看到当前阶段及已运行时长；失败记录真实异常（以及来源的 `error_message`）。
- `llm.jsonl` — 每次大模型调用：chat（prompt/响应/token/耗时，按 `LLM_LOG_MAX_CHARS` 截断）、embedding（仅摘要，不存原始向量）、以及 deterministic fallback 容易让人忽略的错误。

浏览器 DevTools console 会镜像请求为 `[api] 方法 /路径 -> 状态 N毫秒 (request_id)`；轮询期间 UI 显示当前阶段/已用时长，失败时展示来源的 `error_message`。

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
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install -U "mineru[core]"
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

MinerU 输出会映射为结构化 `SourceElement`：公式→`formula` 元素（保留 LaTeX），表格→`table` 元素（HTML 存入 metadata），标题保留层级。前端在 source detail 里渲染它们——公式用 KaTeX、表格用其 HTML——所以公式是排版后的样子而不是原始 LaTeX。若 MinerU 不可达或出错，摄取会降级到 pypdf，保证上传不被阻塞，同时 pipeline log 和 source `error_message` 会保留回退诊断；若某 PDF 解析出 0 文本（如扫描/图片型 PDF），会给出提示而不是看起来"空成功"。

## 当前限制

- 检索使用 SQLite 关键词（CJK bi-gram）+ float32 矩阵语义检索（每 notebook 独立缓存）。内存占用有界（约百 MB，旧版 Python list 约 1.3 GB）。BM25/FTS5 和 pgvector 放量方向后续再做。
- 大文档摄取已加固：贪心窗口化 KG 抽取（成本线性），并发 embedding 逐批落库。极大规模下可再接入 `sqlite-vec`。
- KG 抽取需要配置 `OPENAI_COMPAT_*`；离线 smoke 在需要验证检索/治理时会显式写入 KG 对象。
- Article Studio 当前基于标题/摘要文本，并在存在关联 source 时使用其元素；一等文章全文上传与更丰富的关系打分是下一步（Tier 3）。
- PostgreSQL + pgvector 暂不阻塞本机 beta，后续再迁移。
- `off` 模式 PDF 回退用 pypdf layout 抽取（阅读顺序尚可、零新依赖）；但公式、表格、扫描/图片型 PDF 仍需 MinerU，见"用 MinerU 解析 PDF"。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。

## 验证

运行：

```bash
bash scripts/check.sh
```

该脚本进行后端语法检查（`py_compile`）和离线 hermetic smoke（`smoke_backend.py`——钉死 `mineru_mode=off`，不读真实 LLM/embedding 密钥），覆盖：上传/解析、结构化 Markdown 解析、KG 窗口化、并发 embedding 逐批落库、float32 向量矩阵构建与缓存、混合检索（关键词/向量/None 三态）、多轮 ask、状态机（`extracted` = 绿）、文章研究、反馈、JSON fence 清理、重启持久化。依赖已安装时同步运行前端 `tsc --noEmit`。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
