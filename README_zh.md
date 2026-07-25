# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是面向半导体工程团队的来源可追溯 knowhow 笔记本。它把 PDF、Markdown、DOCX、PPTX、CSV、XLSX 材料转成可搜索的来源元素、结构化知识、带引用回答、私有 Memory、knowhow 表和深度报告。

当前目标是可供真实团队使用的本地 beta：后端采用 FastAPI，并可选择 SQLite 或 PostgreSQL repository；前端采用 Next.js。发行默认的 SQLite 快速启动不要求 Docker、GPU、数据库服务或本地模型服务；选择 PostgreSQL 时需要可访问的 PostgreSQL 服务。OpenAI 兼容的聊天、嵌入、重排和 MinerU 服务都是可选的 URL 集成；未配置时，确定性降级仍可维持核心流程。

## 核心能力

- 结构化来源摄取；MinerU 配置后可保留元素级证据、公式、表格和文档内图片。
- 带紧凑引用的多轮问答，会话历史按最近活动排序（首轮生成中会话即使立即切走也可重新打开），支持 `chunk`、`reasoning` 和实验性的 `graph` 检索模式。逐步推理会在检索前做不受语料影响的问题理解：意图清晰时自动继续，存在会改变检索方向的歧义时先请用户确认，确认后的合同支配所有检索阶段。
- 逐步推理提供 `overview` / `standard` / `deep` / `thorough` / `exhaustive` 五档有界检索力度。明确的整表 Knowhow 清单和物理行/记录计数改走带覆盖率的游标枚举，例如返回 `100/100`；条件筛选、去重/种类计数（如“多少种”）、分组在没有确定性计划时会披露尚不支持精确完整性，安全上限与有界混合分析也只会明确标为部分结果，绝不冒充“全部”。精确阈值见[产品与 API 参考](./docs/product-and-api_zh.md#逐步推理档位与完整集合请求)。
- Concept / Claim / Formula / Procedure 知识抽取、治理、统一图谱可视化和个人知识向公共库提交。
- 与笔记本绑定、仅创建者可见的 Memory，并通过受限 MCP 向外部 Agent 提供访问。
- 自由列 knowhow 表、Markdown 格子、全库推理检索驱动的显式空列补全建议、确定性图谱映射、历史/里程碑和隔离的代码附件。
- 意图优先的两阶段深度报告：检索前先做不接触语料的问题澄清，并通过原子确认冻结用户已审阅的合同；大库也从有界 chunk 候选恢复精确元素，再提供可编辑的覆盖大纲、优先显示解析论文名的引用、真实 grounded 校验、分节推理、实时进度、取消，以及 Markdown/ZIP 导出。
- 多账号所有权、公共参考库、分享链接、复制/只读成员和管理员控制。
- 结构化 JSONL 日志、有界生产诊断、离线批量摄取、检索回放、迁移和回填工具。

完整产品行为和端点契约见[产品与 API 参考](./docs/product-and-api_zh.md)。

## 快速开始

### 环境要求

- Python 3.13 或更高版本
- Node.js 20 或更高版本及 npm
- git

只有当 pip 无法使用 `numpy`、`rustworkx`、`hnswlib` 等包的预编译 wheel 时，才需要 C/C++ 工具链。

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

### 配置

```bash
cp .env.example .env
mkdir -p .local
cp model-services.example.toml .local/model-services.toml
```

如需模型回答和知识抽取，编辑 `.local/model-services.toml`，把 workload 绑定到物理服务，设置每个服务的 `max_concurrency`，并只把 `api_key_env` 指定的密钥放进 `.env`。

若要明确使用确定性/离线降级，在 `.env` 中留空：

```text
MODEL_SERVICES_CONFIG=
```

`.env.example` 是非服务配置和密钥槽位的权威清单；`model-services.example.toml` 是服务、绑定和容量模板。远程访问、CORS、模型调度、认证、MinerU 配置和升级说明见[部署与配置](./docs/deployment-and-configuration_zh.md)。

### 运行

```bash
npm run dev
```

浏览器打开 <http://127.0.0.1:3000>。全新数据库会创建内置 `admin` 账号，本地默认密码为 `admin`；绑定到非回环地址时必须配置非默认的 `SILICON_NOTEBOOK_ADMIN_PASSWORD`。

启动会迁移当前选中的数据存储。默认值是 `DATABASE_URL=sqlite:///.local/silicon_notebook.db`；已准备好的 PostgreSQL 16 数据库可改用 `DATABASE_URL=postgresql://user:password@host:5432/database`。

生产模式固定一个后端 worker，使进程内模型调度器成为整个部署的容量边界：

```bash
npm run start
npm run stop
```

目标机没有 npm/node 或 root 权限时，先用 `bash scripts/pack.sh` 构建离线包，再按 [packaging/DEPLOY.md](./packaging/DEPLOY.md) 部署。

### 验证

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

`scripts/check.sh` 是完整的离线本地门：后端测试、smoke/契约检查、前端测试与类型检查，以及生产前端构建。

## 产品流程

1. 新建笔记本。系统立即打开 `Untitled notebook`，不会预先要求填写元数据。
2. 导入来源文件。解析过程生成结构化来源元素和可搜索内容块。
3. 通过基于内容块的检索立即问答；知识图谱可按需构建，也可为所有上传开启自动抽取。
4. 浏览和治理抽取知识、查看全屏图谱，并在需要联合检索时挂载公共参考库。
5. 把有价值的回答保存为与笔记本绑定的私有 Memory，维护 knowhow 表，或生成深度报告。
6. 通过链接分享笔记本：小笔记本复制，大笔记本只读加入；beta 不提供实时协同编辑。

笔记本内部保持两列布局：左侧是用户导入的来源，主区域依次为**问答**、**知识库**、**记忆**和**深度报告**。

详细产品行为、检索语义、MCP 工具和端点路径见[产品与 API 参考](./docs/product-and-api_zh.md)。

## 架构概览

```text
浏览器
  → Next.js 前端
  → FastAPI /api 与 Streamable HTTP /mcp
  → 应用服务与 repository ports
  → SQLite 或 PostgreSQL + 本地来源/索引/日志存储

可选外部服务
  → OpenAI 兼容 chat / embedding / rerank
  → MinerU HTTP、隔离 CLI 或云端降级
```

- SQLite 默认位于 `.local/silicon_notebook.db`；PostgreSQL 是可直接选择的替代后端。两者的上传文件和生成工件仍位于 `.local/`。
- 生产后端刻意保持单 worker，因为模型队列、熔断、健康和取消状态都在进程内。
- 默认 `chunk` 检索只读取当前笔记本；图谱增强和推理路径可通过显式挂载的公共库联合检索。
- 词法检索保留整句精确匹配作为排序加分，但会独立召回拉丁字母/数字词项及重叠中文三字片段，不再强制整段查询连续出现；SQLite 安全引用 FTS5 clause，PostgreSQL 应用相同的有界词项并集。带索引的 Chunk 与 KG 检索使用有界的 `ANN ∪ FTS` 候选；带索引的 Relation 检索还会按方向平衡补入与 FTS 命中 KG 端点相邻的有界关系候选。
- 大库的索引检索会把 ANN 后的数据库 hydration 限制在候选窗口内，并让并发推理子查询单飞加载 ANN handle。默认会在 `/api/ready` 放行用户流量前加载全部已发布 scale 索引、已启用的 ANN handle 和可安全复用的单索引 PPR core；跨 notebook 组合图保持按需构造，避免成倍复制千万节点图。
- 候选 Review Queue 已退出当前流程；知识治理直接作用于已存知识对象。
- DATABASE_URL 通过唯一的 repository factory 选择正式 repository 后端。运行时只有一个 active repository 后端，由 `DATABASE_URL` 集中选择。SQLite 和 PostgreSQL 都是可直接启动的后端；发行默认值仍是 SQLite。

### SQLite / PostgreSQL 切换

Shadow SQLite source open 的分类边界刻意收窄：只有 `open_fresh_live_sqlite` 抛出的非瞬态 `sqlite3.OperationalError` 才归为 source-binding identity 失败。locked、busy、interrupted open 仍按瞬态整批重试；后续 SQLite operational error 保持原有 schema/query 分类。

应用的正常 repository 路径不会双写。`SHADOW_DATABASE_URL` 只标识显式正向影子迁移
CLI 使用的 PostgreSQL 目标；单独设置它不会启动任何任务，也不会改变 active backend。只改 `DATABASE_URL` 不会复制、迁移或同步既有数据。

在 `DATABASE_URL` 仍指向 SQLite 时，运维人员可以运行受保护的单向
SQLite→PostgreSQL 影子同步：preflight 绑定并确认两端数据库身份，`start-forward` 安装
run-scoped capture/guard 并复制一致的 60 表 baseline，随后由一个受监督的前台 worker
持续应用 SQLite change log。`status` 提供脱敏的 lag/lease/poison 状态，
`verify --level full` 执行 barrier-aware 一致性校验。worker 使用数据库时钟的排他 lease，
对 PostgreSQL 瞬态失败重试，确定性 poison 会 fail-stop；清理策略至少保留已验证进度之后
的 7 天和 100,000 条事件。

本阶段**不包含** cutover、反向复制或自动修改 `DATABASE_URL`。必须保持 SQLite 为 active，
持续维护两端备份，并在另行评审的切换阶段之前把 PostgreSQL 视为禁止业务读取的影子库。
完整命令顺序与故障规则见[运维文档](./docs/operations_zh.md)。

独立的、默认 dry-run 的 `scripts/migrate_sqlite_to_postgres.py` 继续作为受控的停机快照
importer 与本地激活工具；它不是持续复制。SQLite-active 正向 shadow 只使用
`scripts/shadow_sqlite_to_postgres.py`，且两种流程绝不能指向同一个 target。

Baseline snapshot/COPY 还要求 owner-only 的真实 snapshot 目录；所有业务 SQL 全限定到 run 绑定 schema，在关键绑定处以短写栅栏复核 live SQLite capture 仍启用，采用有界 named server cursor/statement timeout，并在起始和最终验证由正式 migration 派生的完整 v9 表/列/约束/operational+GIN-index/extension catalog。Snapshot/fence 必须用指向当前 SQLite 路径的 fresh 专用连接，不得复用 repository 的线程缓存连接；open 前后以及发布/PG commit 前都要复核 resolved path 与 device/inode。最终 SQLite 栅栏只在 PG 长 proof/ANALYZE 完成后取得，并保持到 PG H0 事务提交成功。

- 在发行默认的 SQLite 后端上，搜索使用 SQLite FTS/向量存储；PostgreSQL 后端改用 `pg_trgm`/`ILIKE`。float32 向量仍存为 `bytea`，不安装也不需要 pgvector。
- PostgreSQL 要求 `pg_trgm` 必须安装在 `public` schema。可用不回显凭据的查询检查：

  ```sql
  SELECT e.extname, n.nspname
  FROM pg_extension e
  JOIN pg_namespace n ON n.oid = e.extnamespace
  WHERE e.extname = 'pg_trgm';
  ```

  `pg_trgm | public` 表示前置条件已就绪。若查询无行，首次 migration 会自动尝试 `CREATE EXTENSION pg_trgm`；既有 `pg_trgm` 位于其他 schema 时会 fail closed。
- importer 要求目标 PostgreSQL 是空的且使用 UTF-8；目标 URL 只从 `POSTGRES_MIGRATION_URL` 读取，不放在 CLI 参数中。它用 SQLite backup API 获取包含已提交 WAL 的在线一致快照，只在工作副本上升级到配对 schema，按有界 batch 流式 `COPY`，保留 ordinal，把旧 JSON 向量转换成 float32 `bytea`，逐表做内容 checksum，并逐表提交 + 记录 checkpoint，中断（崩溃、远程连接断开、重启）后从最后完成的表续跑而非整体重来；finalize（ordinal reseed、重建索引、`ANALYZE`）是幂等的。SQLite-only 的 shadow control/change-log 表会被明确排除并记录在 receipt 中。可为大目标传入会话级批量装载调优（`--maintenance-work-mem`、`--max-parallel-index-workers`）。默认 preview/apply 不会修改 `DATABASE_URL`，也不会复制 `.local/storage`。
- 在线迁移只能算演练快照：快照之后继续写入 SQLite 的数据不会被同步。对已经停服的本地部署，显式 `--activate-env ... --confirm-service-stopped` 会重新生成 SQLite 一致快照，并按无凭据 receipt 重算 PostgreSQL 全表 checksum；全部一致后才原子替换 `.env`，把旧 SQLite URL 保存在惰性的 `SHADOW_DATABASE_URL`，并创建权限受限的回退副本。CLI 不会自行停止或重启服务。随后以 `--workers 1` 启动，并在放流量前检查 `/api/ready`、登录、数量、搜索、代表性读取和一次 canary 写入。
- 切回 SQLite 不会回放 PostgreSQL-only 写入。无损回滚要求切换后尚无新写入，或已经完成并验证双向外部对账迁移。
- `scripts/batch_ingest.py` 的变更阶段仅支持 SQLite；PostgreSQL 请使用正常应用/API 摄取和 KG/索引流程。SQLite 存量库若有部分成功的 KG 抽取，可用 `kg --retry-partial`；每个来源会保留旧图，直到“零失败窗口且非空”的新图成功提交。

preview/apply/retry 的完整命令、SQLite↔PostgreSQL selector 写法、正式切换清单、storage 处理和回滚限制见[运维文档](./docs/operations_zh.md#sqlite--postgresql-切换与回滚)；按步骤执行的清单见[迁移 runbook](./docs/postgres-migration-runbook.md)；部署配置见[部署与配置](./docs/deployment-and-configuration_zh.md)。

运行时边界见 [architecture.md](./architecture.md)，贡献者约束见[开发与仓库契约](./docs/development_zh.md)。

## 文档导航

| 需求 | 文档 |
| --- | --- |
| 产品行为、检索模式、Memory/MCP、knowhow、API、当前限制 | [产品与 API 参考](./docs/product-and-api_zh.md) |
| 安装、源码/生产部署、模型服务、配置项 | [部署与配置](./docs/deployment-and-configuration_zh.md) |
| 日志、事故采集、MinerU、批量摄取、回放、迁移、回填 | [运维、诊断与摄取工具](./docs/operations_zh.md) |
| 验证、CI、开发流程、测试和文档契约 | [开发与仓库契约](./docs/development_zh.md) |
| 详细运行时架构 | [architecture.md](./architecture.md) |
| 按脚本查找命令 | [scripts/README.md](./scripts/README.md) |
| 离线部署包目标机说明 | [packaging/DEPLOY.md](./packaging/DEPLOY.md) |
| KG schema | [schema/README.md](./schema/README.md) |
| 产品规格完成状态 | [fangan_done.md](./fangan_done.md) |

每份拆出的专题文档顶部都提供中英文跳转。

## 当前边界

- SQLite 是发行默认值；PostgreSQL 16 已是可直接选择的后端。仓库提供经过校验的单向 SQLite→PostgreSQL 快照 importer；它不提供实时同步、PostgreSQL→SQLite 回放或 MySQL 迁移。
- Docker 不是一期默认工作流，也不是运行前提。
- 公式、表格、版面和扫描 PDF 的高保真解析需要 MinerU；`MINERU_MODE=off` 使用 pypdf 文本降级。
- 知识抽取和模型回答需要绑定对应 workload；离线模式不会合成知识。
- 图谱问答仍为 opt-in/实验能力，默认模式是 `chunk`。
- Memory 只能由用户主动选择保存，并且仅创建者可见。
- 分享是复制或只读成员，不是实时协同编辑。
- Web/网络来源搜索仍是禁用的未来入口。

## 文档维护

根 README 只保留项目入口信息。详细行为写入上表对应的权威文档，中英文版本保持一致。安装、产品行为、架构或开发约束变化时，仍需同步更新 `README.md`、`README_zh.md`、`AGENTS.md`、`CLAUDE.md`，并更新对应的专题文档。
