# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是面向半导体工程团队、强调来源可追溯的知识笔记本。它把 PDF、Markdown、DOCX、PPTX、CSV、XLSX 和 XLS 材料转成可搜索的来源元素、带引用回答、结构化知识、私有 Memory、knowhow 表和深度报告。

项目目前定位为本地团队 beta，采用 FastAPI 与 Next.js。默认 SQLite 启动不需要 Docker、GPU 或外部模型服务；PostgreSQL、OpenAI 兼容模型服务和 MinerU 均为可选集成。

## 核心能力

- 结构化导入文本、表格、公式、代码和文档图片，并提供元素级引用。
- 多轮问答支持来源选择、可点击证据、会话历史，以及 `chunk`、`reasoning` 和实验性 `graph` 检索。
- 抽取和治理概念、论断、公式、过程与关系，并提供统一知识图谱。
- 支持私有 Memory、结构化 knowhow、深度报告、参考库和受控分享。
- 通过认证 MCP 工具向外部 Agent 提供有范围约束的问答、来源、Memory 与 knowhow 能力。
- 提供启动期冻结的 Extension SDK，可接入部署方后端、UI、问答引擎、解析器、索引管线、导出器和观察器。

后端部署插件只从 `EXTENSIONS_CONFIG` 指向的 TOML 装载，其 `trust` 为 `deployment`；配置变更需要重启，插件自己的 API extensions 只挂载在 `/api/extensions/{plugin_id}` 下。私有 UI 包另由 `SILICON_NOTEBOOK_UI_PLUGINS` 在前端构建期注入，变更后需要重新构建。

界面默认使用自动模式，适合直接上传和提问；高级模式会开放检索力度、报告深度及来源/参考库范围控制。

## 快速开始

### 环境要求

- Python 3.13+
- Node.js 20+ 与 npm
- git

### 安装

```bash
git clone <repo-url> silicon-notebook
cd silicon-notebook

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt

( cd frontend && npm install )
```

### 配置与运行

```bash
cp .env.example .env
mkdir -p .local
```

启动前必须二选一配置模型：

- 使用确定性/离线降级时，在 `.env` 中设置 `MODEL_SERVICES_CONFIG=`。
- 使用模型能力时，把 `model-services.example.toml` 复制到 `.local/model-services.toml`，配置服务，并在 `.env` 中填写其引用的密钥。

然后运行：

```bash
npm run dev
```

打开 <http://127.0.0.1:3000>，API 位于 <http://127.0.0.1:8000>。

全新本地数据库会创建 `admin` / `admin`。对外提供服务前必须修改密码；绑定非 loopback 地址时，必须设置非默认的 `SILICON_NOTEBOOK_ADMIN_PASSWORD`。

默认数据库是 SQLite；需要时可把 `DATABASE_URL` 指向准备好的 PostgreSQL 16 数据库。完整设置与安全说明见[部署与配置](./docs/deployment-and-configuration_zh.md)。

### 生产运行

```bash
npm run start
npm run stop
```

生产模式使用单个后端 worker，因为模型调度与取消状态位于进程内。日志写入 `.local/logs/`，启动后应检查 `/api/ready`。离线部署可先运行 `bash scripts/pack.sh`，再按 [packaging/DEPLOY.md](./packaging/DEPLOY.md) 操作。

### 验证

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

扩展门禁和 PostgreSQL 专项门禁见[开发与仓库契约](./docs/development_zh.md)。
CI 各 lane 时长仅作观察；门禁本身仍是通过/失败契约。

## 典型流程

1. 新建笔记本并导入支持的来源文件。
2. 选择当前来源和参考库范围，然后进行带引用问答。
3. 构建、审核结构化知识，或查看知识图谱。
4. 将有价值的结果保存到私有 Memory、knowhow 表，或生成深度报告。
5. 受控分享笔记本，或发布可撤销的只读报告。

## 架构概览

```text
浏览器
  → Next.js 前端
  → FastAPI /api 与 Streamable HTTP /mcp
  → 应用服务与 repository ports
  → SQLite 或 PostgreSQL + 本地来源/索引/日志存储

可选服务
  → OpenAI 兼容 chat / embedding / rerank
  → MinerU HTTP、隔离 CLI 或云端解析
```

无论使用哪种数据库，上传文件和生成工件都保存在 `.local/`。问答和深度报告使用显式冻结的来源范围；可选检索通道不可用时会回退到已接纳的基线，不会隐藏其他结果。

Extension SDK 的 baseline-preserving retrieval host 会执行实时 capability 判定；模块化架构变更需要两路独立 subagent review 和绿色 CI。Ask 与 Report 通过不可变 application stage 边界传递，每阶段使用新的 retrieval run 且不持有数据库连接；`report.completed_observer` 仅在持久 `done` 提交后运行。

## 文档导航

| 需求 | 文档 |
| --- | --- |
| 产品行为、检索、Memory/MCP、knowhow、API、限制 | [产品与 API 参考](./docs/product-and-api_zh.md) |
| 面向用户的中文界面用词 | [界面词汇约定](./docs/ui-vocabulary.md) |
| 安装、生产部署、模型服务、配置项 | [部署与配置](./docs/deployment-and-configuration_zh.md) |
| 日志、导入、索引、迁移、回填、故障处理 | [运维文档](./docs/operations_zh.md) |
| 测试、CI、贡献流程与仓库契约 | [开发文档](./docs/development_zh.md) |
| 外部 Agent 配置与可运行 MCP/Memory 示例 | [Agent MCP 与 Memory 接入](./docs/agent-mcp-memory-sop_zh.md) |
| 部署插件开发与运维 | [部署扩展](./docs/deployment-extensions-sop_zh.md) |
| 运行时边界 | [architecture.md](./architecture.md) |
| 脚本命令索引 | [scripts/README.md](./scripts/README.md) |
| 产品规格实现状态 | [fangan_done.md](./fangan_done.md) |

每份拆分文档顶部都提供中英文跳转。

## 当前边界

- SQLite 是发行默认数据库，PostgreSQL 16 是受支持的替代后端；切换数据库不会自动复制或同步既有数据。
- 扫描 PDF、公式和图片的最高保真解析需要 MinerU；本地解析器提供确定性降级。
- 模型回答和知识抽取需要对应工作负载绑定；离线模式仍可完成导入与确定性流程。
- Graph Ask 是 opt-in 的实验能力，默认 Ask 模式为 `chunk`；generated-question recall 由部署方显式开启且默认 `off`。
- Memory 仅创建者可见；分享支持复制、只读加入和群组，不提供实时协同编辑。
- Web/网络来源搜索仍是尚未开放的未来能力。

## 文档维护

根 README 只保留项目入口信息。详细行为写入上表对应的权威文档，中英文版本保持一致。只有入口信息变化时才更新 README 对；只有全仓 Agent 工作流/文档路由变化时才更新 `AGENTS.md`，只有 Claude Code 常驻规则变化时才更新 `CLAUDE.md`。普通产品、架构、部署、运维与开发约束变化只更新其权威专题文档，不再复制到所有入口文件。
