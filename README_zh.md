# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是一个面向半导体研发团队的 knowhow notebook 平台。它的目标是把历史规则、debug 案例、review checklist、技术文章等资料转化成可查询、可引用、可审核、可推演的工程知识系统。

## 当前范围

当前仓库已经进入本机真实 beta 闭环：

- Python FastAPI 后端
- 默认使用 SQLite 持久化，数据库路径为 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端作为主线
- OpenAI-compatible LLM 配置，用于摘要、抽取、回答和文章研究；embedding 可通过同一兼容 API 端点独立配置
- 未配置 LLM key 时使用 deterministic fallback，保证本机 beta 可用
- 面向真实团队的干净起点：全新数据库只初始化本机用户，不再预置 demo 笔记本或合成来源；新建笔记本的示例提问会根据所选模板/笔记本领域动态生成，而非写死的样例
- 真实 multipart 文件上传（解析经 FastAPI `BackgroundTasks` 异步执行），支持 PDF、Markdown、DOCX、PPTX
- PDF 解析在配置 MinerU（GPU 主机）时走 MinerU（公式转 LaTeX、表格、版面）；本机/未启用时回退到 pypdf 纯文本
- 解析生成 `SourceElement`，包含 `element_type`、`location_label`、`text`、`metadata`
- 自动知识抽取（rule / method / risk / case / checklist / glossary 候选）并做元素级 evidence 绑定，配 curator 审核队列（批准 / 拒绝 / 编辑）
- 混合检索：关键词 + 可选 embedding 余弦，覆盖来源元素和已批准知识
- 真实来源驱动回答 + citation 校验：问答、场景查询、案例检索、Checklist 生成、文章研究，以及带可选评论的 👍/👎 反馈
- 知识治理：规则/方法/风险/术语浏览，状态生命周期（reviewed/approved/deprecated/conflict/project_specific）+ owner/last_reviewed，重复检测与合并，冲突检测（deprecated 知识不参与回答）
- Object 级知识图谱可视化：Concept / Claim / Formula / Procedure 节点同屏展示，主画布显示节点名称、类型形状/颜色和边关系标签，并提供搜索/多选类型过滤、合并审核、按类型分组的节点总览与详情面板，选择节点时画布会自动聚焦
- 外层 notebook 集合页（网格/紧凑/列表视图、编辑/删除）、工作区标题编辑、来源详情预览/删除、文章删除、内部搜索
- 第一版不使用 Docker

PostgreSQL + pgvector 仍然是后续生产/团队 beta 的目标方向，相关 schema 文档保留在 `database/` 下，但当前本机开发不依赖 PostgreSQL。

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

外层页面是 notebook 集合页，类似 notebook library：

1. 点击 `＋ 新建` 或 `新建笔记本` 卡片。
2. 系统立即创建 `Untitled notebook`。
3. 创建后直接进入来源选择界面。
4. 用户导入 PDF、Markdown、DOCX 或 PPTX 文件。
5. 后端保存原始文件、解析来源元素、生成来源摘要，并让内容可被搜索。

进入单个 notebook 后：

- 左栏：用户导入的来源文件（解析/抽取过程中实时显示 parse-status），支持详情预览和删除，并提供 curator 审核队列。网络检索来源暂不开放。
- 中栏：以 tab 形式提供来源驱动的 knowhow 工具——问答（自由提问）、场景查询（结构化场景表单）、案例检索、Checklist 生成、规则库浏览。回答包含相关规则/案例/checklist/风险、缺失信息、引用和 👍/👎 反馈。
- 知识图谱以全屏工作区浮层打开：画布渲染 object 级 KG 节点名称、类型视觉标记和关系边标签；多选类型过滤用于收敛密集视图；侧栏按类型（Concept、Claim、Formula、Procedure）汇总节点，点击总览节点会聚焦画布并展示选中节点的关系、出处和按类型排布的相关节点。
- 右栏：Studio，含思维导图、新建文章、信息图；文章研究驱动思维导图 / 信息图输出，已创建文章可删除。

notebook 工作区会隐藏集合页全局上边栏，并采用更偏工程知识台的视觉风格，避免和 NotebookLM 完全一致。

## API

当前 beta 的关键 API：

- `GET /api/notebooks`
- `POST /api/notebooks`
- `PATCH /api/notebooks/{notebook_id}`
- `DELETE /api/notebooks/{notebook_id}`
- `POST /api/notebooks/{notebook_id}/sources` multipart 文件上传（异步解析/抽取）
- `GET /api/sources/{source_id}`、`DELETE /api/sources/{source_id}`、`POST /api/sources/{source_id}/parse`（轮询状态 / 重跑管线）
- `GET /api/sources/{source_id}/elements`、`POST /api/sources/{source_id}/extract`
- `GET /api/notebooks/{notebook_id}/candidates[/{type}]`、`PATCH /api/candidates/{id}`、`POST /api/candidates/{id}/approve|reject`
- `GET /api/notebooks/{notebook_id}/rules`
- `GET /api/notebooks/{notebook_id}/search?q=`
- `POST /api/notebooks/{notebook_id}/ask`、`.../scenario-query`、`.../case-search`、`.../checklist`
- `GET|POST /api/notebooks/{notebook_id}/articles`、`DELETE /api/articles/{id}`、`POST /api/articles/{id}/research`
- `POST /api/answers/{answer_id}/feedback`

## 配置

LLM 使用 OpenAI-compatible 配置：

```text
OPENAI_COMPAT_BASE_URL
OPENAI_COMPAT_API_KEY
OPENAI_COMPAT_MODEL
OPENAI_COMPAT_TIMEOUT_SECONDS
SILICON_NOTEBOOK_CORS_ORIGINS
```

向量检索（语义召回）单独配置：

```text
EMBED_PROVIDER          # ""=关闭（仅关键词） | local | dashscope
EMBED_MODEL             # 如 BAAI/bge-m3（local）或 API 模型名
EMBED_BASE_URL          # dashscope / OpenAI 兼容的 embedding 端点
EMBED_API_KEY
EMBED_DIM               # 须与模型输出维度一致（默认 1024）
```

日志相关配置：

```text
LLM_LOG_ENABLED / LLM_LOG_PATH / LLM_LOG_MAX_CHARS   # LLM 交互日志（chat 按 MAX_CHARS 截断）
EVENT_LOG_ENABLED / EVENT_LOG_DIR                     # HTTP + 管线事件日志
SLOW_REQUEST_MS                                       # 超过该毫秒数的请求标记 SLOW
```

没有配置 LLM 时，抽取、摘要和回答都会退化为 deterministic 启发式，保证本机 beta 离线也能跑通完整闭环。

## 可观测性 / 日志

后端通过统一的 `EventLogger`（`app/core/event_logging.py`）输出结构化日志：每条事件一行 JSONL 写入 `.local/logs/`，并附控制台简要行。写日志是 best-effort，绝不影响它所观测的请求或管线；未配置模型时 LLM 通道为 no-op。

- `requests.jsonl` — 每个 HTTP 请求（方法、路径、状态码、耗时、`request_id`）。超过 `SLOW_REQUEST_MS`（默认 3000ms）标 `SLOW`；响应头带 `X-Request-Id` 关联前后端。
- `events.jsonl` — 异步来源管线：各阶段（`parse` / `embed` / `extract`）耗时与每次状态机跃迁。卡住时能看到当前阶段及已运行时长；失败记录真实异常（以及来源的 `error_message`）。
- `llm.jsonl` — 每次大模型调用：chat（prompt/响应/token/耗时，按 `LLM_LOG_MAX_CHARS` 截断）、embedding（仅摘要，不存原始向量）、以及原本会被启发式回退掩盖的错误。

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

- 检索为 SQLite 关键词 + Python 内 embedding 余弦；BM25/FTS5 与 pgvector 后续再做。
- 规则治理较基础（批准/拒绝）；状态生命周期、重复合并、冲突检测是下一步（Tier 2）。
- Article Studio 当前基于标题/摘要文本，并在存在关联 source 时使用其元素；一等文章全文上传与更丰富的关系打分是下一步（Tier 3）。
- PostgreSQL + pgvector 暂不阻塞本机 beta，后续再迁移。
- `off` 模式 PDF 回退用 pypdf layout 抽取 + 标题/段落切分（阅读顺序尚可、零新依赖）；但公式、表格、扫描/图片型 PDF 仍需 MinerU，见"用 MinerU 解析 PDF"。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。

## 验证

运行：

```bash
bash scripts/check.sh
```

该脚本会检查后端语法、SQLite/上传/解析/抽取/批准/删除/问答/反馈/文章 smoke 路径（含检索打分与异步上传路径），以及在依赖已安装时检查 Next.js TypeScript。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
