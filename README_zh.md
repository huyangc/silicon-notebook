# silicon-notebook

[English README](./README.md)

`silicon-notebook` 是面向半导体工程团队的来源可追溯 knowhow 笔记本。它把 PDF、Markdown、DOCX、PPTX、CSV、XLSX 材料转成可搜索的来源元素、结构化知识、带引用回答、私有 Memory、knowhow 表和深度报告。

当前目标是可供真实团队使用的本地 beta：后端采用 FastAPI，并可选择 SQLite 或 PostgreSQL repository；前端采用 Next.js。发行默认的 SQLite 快速启动不要求 Docker、GPU、数据库服务或本地模型服务；选择 PostgreSQL 时需要可访问的 PostgreSQL 服务。OpenAI 兼容的聊天、嵌入、重排和 MinerU 服务都是可选的 URL 集成；未配置时，确定性降级仍可维持核心流程。

## 核心能力

- 结构化来源摄取；MinerU 配置后可保留元素级证据、公式、表格和文档内图片；公式证据会在来源详情与知识图谱出处卡中排版，LaTeX 渲染失败时仍显示原文，宽块级公式只在所属面板内横向滚动。
- 带紧凑引用的多轮问答，会话历史按最近活动排序（首轮生成中会话即使立即切走也可重新打开）；问题和回答气泡都支持悬停显示、点击固定浏览器本地时间，其中问题采用网页端提交瞬间，回答采用权威持久化完成时间；支持 `chunk`、`reasoning` 和实验性的 `graph` 检索模式。逐步推理会在检索前做不受语料影响的问题理解：意图清晰时自动继续，存在会改变检索方向的歧义时先请用户确认，确认后的合同支配所有检索阶段。超出首轮宽度的已确认必答主题种子会在步骤预算内顺延执行而非被丢弃，仍未覆盖的方向会在轨迹中披露；跨工具映射类问题会为每个被点名工具生成独立必答主题，目标侧检索配目标工具名与功能描述词。实时推理轨迹覆盖整轮——从问题理解一直到答案生成——而不只是检索阶段。
- 逐步推理提供 `overview` / `standard` / `deep` / `thorough` / `exhaustive` 五档有界检索力度。明确的整表 Knowhow 清单和物理行/记录计数改走带覆盖率的游标枚举，例如返回 `100/100`；条件筛选、去重/种类计数（如“多少种”）、分组在没有确定性计划时会披露尚不支持精确完整性，安全上限与有界混合分析也只会明确标为部分结果，绝不冒充“全部”。精确阈值见[产品与 API 参考](./docs/product-and-api_zh.md#逐步推理档位与完整集合请求)。
- 逐步推理还能按需列出（而非只做相关性排序）库里的文档目录（标题、类型、已存摘要），以及全库的公式/表格/图片/代码块清单与概念/论断/公式/过程知识对象清单，每份清单都带“已列出/总数”完整性徽章和有界原文引用；进入答案合成的条目各有独立 `[k]` 绑定，可核对最终回答究竟用了清单里的哪一项。截断时明确标注为部分结果，同一轮推理内可继续列出。结果卡保留状态摘要但默认收起，用户需要时再展开条目。细节见[产品与 API 参考](./docs/product-and-api_zh.md#集合枚举工具)。
- Concept / Claim / Formula / Procedure 抽取统一受有类型的边契约约束，并提供历史非法边过滤与只读审计、统一图谱可视化、从问答引用精确定位图谱节点（包括核心视图范围外的节点），以及个人知识向公共库提交。可选跨元素关系补全使用按模式和来源代次绑定的持久 keyset 水位与同源索引候选；未完页通过有界任务及启动恢复续跑，模式切换会在同一事务内先发布新模式的可恢复游标再将旧游标标为 stale。该能力仍受灰度闸控制且默认关闭。
- 与笔记本绑定、仅创建者可见的 Memory，并通过受限 MCP 向外部 Agent 提供访问。
- 自由列 knowhow 表、Markdown 格子、支持属性按列/按行并提供可操作校验提示的表格导入、有界批量规整审阅、可读审计操作者、内容感知稳定列宽、全库推理检索驱动的显式空列补全建议、格子知识对象默认进入图谱/推理检索的确定性图谱映射、历史/里程碑，以及保存后立即显示归因的隔离代码附件。
- 意图优先的两阶段深度报告：检索前先做不接触语料的问题澄清，并通过原子确认冻结用户已审阅的合同；大库也从有界 chunk 候选恢复精确元素，再提供可编辑的覆盖大纲、优先显示解析论文名的引用、真实 grounded 校验、分节推理、实时进度、取消，以及 Markdown/ZIP 导出。
- 多账号所有权、公共参考库、分享链接、复制/只读成员和管理员控制；用户使用总览支持分页和按表头排序，其中问答用量展示已创建持久任务的用户提问次数，而不是会话容器数量。用户总数包含其在加入笔记本中的提问，展开明细仍只列该用户拥有的笔记本。
- 结构化 JSONL 日志、有界生产诊断、离线批量摄取、检索回放、迁移和回填工具。
- 检索候选保留语义、词法、PPR、KG 来源和社区等全部生产者来源；chunk/图混合选择可在不扩大回答预算的前提下，为纯图路径证据预留有界席位。

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

`npm run start` 会先安装后端依赖与锁定版本的前端依赖，再在前台完成构建并用与 terminal 脱离的后台进程启动前后端；待后端 readiness 与前端 HTTP 访问都通过后，命令即退出。日志仍位于 `.local/logs/`，停服使用 `npm run stop`；已预装依赖的部署可设 `SKIP_INSTALL=1`。

目标机没有 npm/node 或 root 权限时，先用 `bash scripts/pack.sh` 构建离线包，再按 [packaging/DEPLOY.md](./packaging/DEPLOY.md) 部署。

### 验证

```bash
curl -s http://127.0.0.1:8000/api/health
bash scripts/check.sh
```

验证采用分级门禁：G0 按改动选跑目标测试；G1 `scripts/check.sh` 是编辑期及每次 PR/push 的离线门（稳定后端、契约、前端测试及负责类型检查的 production build），默认使用 12 个 backend worker、每个前端 runner 4 个测试 worker，Apple Silicon warm 目标不超过 60 秒；G2 `scripts/check_extended.sh` 追加真实索引/性能测试与全仓语义扫描，每天 18:17 UTC（北京时间次日 02:17）执行一次，也可手动触发；G3 `scripts/check_postgres.sh` 保持为独立 PostgreSQL 集成门。CI 各 lane 时长仅作观察。

仅 Codex 的执行说明：`scripts/check.sh` 包含绑定 loopback 端口和管理子进程的生命周期测试，Codex 第一次运行就必须申请沙箱外执行，不得先在沙箱内试错。GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）同样应直接申请沙箱外执行；普通本地只读 Git 检查仍留在沙箱内。

数据库专项覆盖现在只面向直接 PostgreSQL 后端；已退役的 SQLite 后端实现专项测试、SQLite→PostgreSQL 导入/正向 shadow 测试和跨后端 parity 测试不再属于当前测试套件。

## 产品流程

1. 新建笔记本。系统立即打开 `Untitled notebook`，不会预先要求填写元数据。
2. 导入来源文件。弹窗会压缩显示过长的待上传文件名、保留操作区底部留白，在上传前拒绝超过部署单文件大小上限的文件、执行单次请求 20 个文件的护栏，并在批次超过剩余文档名额时提前禁用上传且说明处理办法；解析过程随后生成结构化来源元素和可搜索内容块。
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
- 提问时用**英文半角双引号**括起来的内容整体检索、不做分词：它作为一个不可拆词项进入词法候选，在关键词覆盖率里只算一项（散落着这几个词的文档得不到分），并额外获得一次精确定位探测。引号是强偏好而非硬过滤。只认英文半角双引号、引号内至少 3 个字、一段文本里超过 4 段**不同**的引号内容则整条语法不生效；提问框与深度报告输入框会当场回执识别到的短语，不让没生效的约束静默通过。
- 词法检索保留整句精确匹配作为排序加分，但会独立召回拉丁字母/数字词项、重叠中文三字片段，以及以 `_`/`-`/`.` 连接的完整标识符（如 `set_db`、`config.yaml`）作为整体词项，不再强制整段查询连续出现；SQLite 安全引用 FTS5 clause，PostgreSQL 应用相同的有界词项并集并转义 LIKE 元字符，使 `set_db` 这类词项保持字面量。带索引的 Chunk 与 KG 检索使用有界的 `ANN ∪ FTS` 候选；带索引的 Relation 检索还会按方向平衡补入与 FTS 命中 KG 端点相邻的有界关系候选。
- 大库的索引检索会把 ANN 后的数据库 hydration 限制在候选窗口内，并让并发推理子查询单飞加载 ANN handle。默认会在 `/api/ready` 放行用户流量前加载全部已发布 scale 索引、已启用的 ANN handle 和可安全复用的单索引 PPR core；跨 notebook 组合图保持按需构造，避免成倍复制千万节点图。
- 检索索引区分立即构建与低峰排队；用户发起立即构建时会覆盖同 notebook 先前的排队任务但保留后来产生的后续任务，前台轮询结束后仍由完成事件刷新实时状态，因此索引发布后历史问答中的“尚未建立索引”提示会同步消失。
- 候选 Review Queue 已退出当前流程；知识治理直接作用于已存知识对象。
- DATABASE_URL 通过唯一的 repository factory 选择正式 repository 后端。运行时只有一个 active repository 后端，由 `DATABASE_URL` 集中选择。SQLite 和 PostgreSQL 都是可直接启动的后端；发行默认值仍是 SQLite。

### SQLite / PostgreSQL 切换

Shadow SQLite source open 的分类边界刻意收窄：只有 `open_fresh_live_sqlite` 抛出的非瞬态 `sqlite3.OperationalError` 才归为 source-binding identity 失败。locked、busy、interrupted open 仍按瞬态整批重试；后续 SQLite operational error 保持原有 schema/query 分类。

应用的正常 repository 路径不会双写。`SHADOW_DATABASE_URL` 只标识显式正向影子迁移
CLI 使用的 PostgreSQL 目标；单独设置它不会启动任何任务，也不会改变 active backend。只改 `DATABASE_URL` 不会复制、迁移或同步既有数据。

在 `DATABASE_URL` 仍指向 SQLite 时，运维人员可以运行受保护的单向
SQLite→PostgreSQL 影子同步：preflight 绑定并确认两端数据库身份，`start-forward` 安装
run-scoped capture/guard 并复制一致的 64 表 baseline，随后由一个受监督的前台 worker
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
- `scripts/batch_ingest.py` 的 `ingest`、`kg`、`index`、`all`、`embed`、`metadata`、`reparse`、`backfill-source-index` 同时支持 SQLite 与 PostgreSQL。PostgreSQL 直连维护只允许离线执行：先停止 API/后台 writer，再显式传 `--confirm-service-stopped`；该参数只是运维确认，不会替你停服务。数据库级 advisory lock 会阻止两个 PostgreSQL 维护 CLI 重叠。`vectors-to-blob` 仍仅适用于 SQLite，因为 PostgreSQL 向量已经是 `bytea`。存量部分 KG 可用 `kg --retry-partial`，旧图会保留到“零失败窗口且非空”的新图成功提交。

preview/apply/retry 的完整命令、SQLite↔PostgreSQL selector 写法、正式切换清单、storage 处理和回滚限制见[运维文档](./docs/operations_zh.md#sqlite--postgresql-切换与回滚)；按步骤执行的清单见[迁移 runbook](./docs/postgres-migration-runbook.md)；部署配置见[部署与配置](./docs/deployment-and-configuration_zh.md)。

运行时边界见 [architecture.md](./architecture.md)，贡献者约束见[开发与仓库契约](./docs/development_zh.md)。

贡献者安全约束：凡任务会写入仓库代码、测试、文档或配置，都必须先新建隔离的 linked git worktree 和分支；该任务期间主 checkout 只读。纯调研、状态汇报和只读审查除外。

## 文档导航

| 需求 | 文档 |
| --- | --- |
| 产品行为、检索模式、Memory/MCP、knowhow、API、当前限制 | [产品与 API 参考](./docs/product-and-api_zh.md) |
| 外部 Agent 界面配置、Codex/Claude CLI 与可运行 MCP/Memory 示例 | [Agent MCP 与 Memory 接入 SOP](./docs/agent-mcp-memory-sop_zh.md) |
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
