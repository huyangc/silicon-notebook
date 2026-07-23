# 开发与仓库契约

[返回 README](../README_zh.md) · [English](./development.md)

本文保留面向贡献者的架构摘要、验证门、工作流、测试架构和文档维护契约。完整 Agent/开发约束仍以 [AGENTS.md](../AGENTS.md) 为准，详细运行时架构以 [architecture.md](../architecture.md) 为准。

## 架构边界

- 后端 endpoint body 位于由 `backend/app/api/routes.py` 组合的领域 FastAPI router；聚合层只负责 composition/order，不承载产品 handler，也不提供兼容导出。边界测试直接检查领域 router 的 endpoint 所有权，并以语义 AST 检查聚合组合声明；不要假设 `include_router()` 一定把子路由平铺，因为新版 FastAPI 会保留惰性的 included-router 节点。领域 Pydantic model 位于 `backend/app/models/`；`backend/app/models/schemas.py` 是旧导入的兼容 facade，re-export 同一批 model object。
- `SQLiteRepository` 是组合式 `RepositoryRuntime` 之上的兼容 facade。application service 不拼装主业务库 SQL。store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。store 共享一个 `SqliteDatabase` 连接工厂、写锁与版本闸 `SqliteMigrator`；service 保留顺序与策略。facade 每个操作要么是显式兼容 adapter，要么是源码守卫验证的单跳委托，真实目标必须与 ownership manifest 一致。消费者依赖 `backend/app/repositories/ports.py` 中可执行、按消费者划分的小型 Protocol；依赖方向单向——facade → runtime → services → stores → SQLite——未来 PostgreSQL adapter 只需在同一 ports 后替换 store 层，调用方不动。`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim，请求 Context、`_COPY_CHUNK`、`_remap_json_ids` 等旧导出继续可 import。
- `RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有；完成组合后替换受支持的兼容属性时，所有已持有它们的消费者都会同步更新。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再把提交异常重新抛出；成功 worker 的次序与既有 Ask 事务 checkpoint 不变。
- 重构前创建的数据库可原样加载。`scripts/verify_repository_snapshot.py` 使用精确的逐版本 migration manifest 与稳定 seed manifest，对 SQLite URI 路径做百分号编码，只在临时 backup 上构造 repository；cleanup 失败时只报告保留的 backup 路径，不输出私有行。它校验原 DB/WAL metadata 以及 SHM 的存在性和大小；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

当前 schema 版本为 28。已提交的 v9 兼容 fixture 会经由 v10–v28 migration 升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖 Memory/Agent 与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，v17 覆盖论文元数据，v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标，v21 覆盖交互式规整 anchor 成员检查的归一化表达式索引，v22 增加持久化的 notebook 级 KG 构建任务，v23 增加每用户最新模型服务状态，v24 为写锁瘦身的簇映射切换段增加 kg_canonical_scratch 表，v25 不可逆地清除已存的用户模型凭据与旧状态并新增按服务 ID 存储的部署级模型服务健康状态，v26 增加 knowhow 表变更流水与命名里程碑，v27 增加 sources.chunked_at 完成标记，使「已就绪但无分块」的来源历史可判定（合法的零分块解析 vs 中途失败的分块），v28 新增 app_settings 全局设置表与可空的 user_profiles.upload_document_limit 列，用于每笔记本文档数量上限。
- `frontend/app/page.tsx` 只承担 notebook workspace 编排，不再持有全部共享模型和面板实现。API/视图类型与常量位于 `workspace-model.ts`，答案/引用/推理轨迹位于 `answer-panel.tsx`，内置 KG 类型文案/样式位于 `kg-type-model.ts`，图谱和答案共用 `kg-type-mark.tsx` 渲染。
- workspace HTTP 职责拆分到 `system-api.ts`、`notebook-api.ts`、`source-api.ts`、`ask-api.ts`、`knowledge-api.ts`、`report-api.ts` 与 `kg-api.ts`。共享 `frontend/app/api-client.ts` transport 负责 HTTP mechanics，领域模块保留 endpoint policy；`page.tsx` 保留 state、过期结果 guard、轮询与 Blob URL 生命周期；`api-boundary.test.mjs` 用语义扫描禁止 transport core 外的生产 `fetch`。
- 结构回归测试只使用 public HTTP contract 或显式 domain seam，不得绑定 private aggregate helper、源码位置、行数或 route/model 总数。workspace-state hook 拆分与 FastAPI lifespan/application lifecycle composition 仍是独立债务。

## 验证

运行：

```bash
bash scripts/check.sh
```

这是完整的本地离线门禁，并行运行三个有界 lane：`check_backend.sh` 执行完整 backend pytest；`check_contracts.sh` 执行语法/依赖预检、hermetic smoke、契约检查与确定性抽取评分 harness；`check_frontend.sh` 执行递归发现的全部 `*.test.mjs`、全部 `*.component.test.tsx`、`tsc --noEmit` 与 production build。每个 lane 都有独立进程组，因此中断或终止 controller 时，也会终止并回收 pytest、npm 和 Next.js 的后代进程。官方 client MCP smoke 精确锁定十一个工具：七个 Memory 工具加四个 knowhow 工具。缺少 `frontend/node_modules` 会直接失败，不再静默跳过前端门禁。

验收时使用项目一直采用的 Homebrew/Miniconda Python：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

完整门禁并发运行三个 lane：backend、contracts、frontend。`check_backend.sh` 默认使用 9 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖。Apple Silicon warm gate 硬目标是不超过 60 秒；CI 各 lane 时长仅作观察，因此这不是对每一台 CI 机器的可移植超时断言。

### GitHub Actions CI

`.github/workflows/ci.yml` 把同一套完整门禁暴露为唯一的
`CI / full-gate` 检查。它在目标为 `master` 的 PR、`master` push 与手动触发时
运行，环境固定为 `ubuntu-24.04`、Python 3.13、Node.js 22。workflow 从
`backend/requirements.txt` 与 `frontend/package-lock.json` 安装依赖，然后把
测试选择完整委托给 `scripts/check.sh`。

已提交的 OpenAPI 契约是字节语义冻结契约，因此
`backend/requirements.txt` 精确固定 FastAPI `0.135.3` 与 Pydantic
`2.12.4`。只能在有意重生 OpenAPI 契约并在干净环境跑完整门禁时，
才同步升级这两个框架。

该 workflow 只有读权限，不接收模型或部署 secrets，并把后端 pytest worker
限制为 4，避免 GitHub 托管 runner 过度抢占。后端安装设置
`HNSWLIB_NO_NATIVE=1` 并禁用 pip wheel cache：`hnswlib` 默认会用
`-march=native` 编译，把这种本机 wheel 缓存后恢复到 CPU 特性不同的托管
runner，可能以 `SIGILL` 崩溃。CI 使用可移植构建，以少量 ANN 性能换取确定性；
生产 wheelhouse 仍可按已声明的部署 CPU 定向构建。20 分钟 timeout 包含依赖安装，
与 Apple Silicon 本地 warm gate 的 60 秒内目标刻意分开。初次接入时
`CI / full-gate` 仅用于观察；只有在 PR 与合并后的 `master` 都稳定绿跑后，
并由用户明确批准分支保护变更，才把它设为 `master` 的 required check。

CI 可移植性属于门禁契约：所有由 CI 执行的测试使用的文件系统、数据和依赖
路径都必须相对仓库，并且独立于进程 cwd。已提交 fixture 必须从其仓库文件位置
定位，禁止依赖开发机 checkout 绝对路径或 `HOME`，测试也不得读取仓库外源文档。
测试启动时直接导入的第三方包必须声明在 `backend/requirements.txt`；干净 hosted
runner 必须从该文件和 `frontend/package-lock.json` 安装，并且只凭这些声明即可
全绿。各 lane 时长继续输出供观察，60 秒内目标只约束已验证的 Apple Silicon
Homebrew warm gate。

依赖仓库外 PDF 解析产物的 gold 生成、构建与校验脚本仍属于 developer-only
工具并保持在 `scripts/check.sh` 之外；该例外绝不适用于已提交测试。

## 开发流程

每开始一个新的特性开发任务，默认先新建 git worktree，并在该 worktree 内基于新 feature 分支开发；完成后从该分支提交 PR。不要为了特性开发直接在本地主 checkout 里切分支。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。

对于已经批准的多步骤实施计划，默认采用 subagent-driven development：每个任务交给一个全新的实现子 Agent，并在进入下一任务前完成该任务范围内的规格符合性与代码质量审查。纯调研、设计、状态汇报和只读审查不要求创建 worktree 或使用子 Agent。

`CLAUDE.md` 是 Claude Code 在本仓库的操作规范：Claude Code 只自动加载 `CLAUDE.md` 与 `.claude/rules/`，不会加载 `AGENTS.md`，因此该文件内联了必须随时在线的红线，并给出 `AGENTS.md` 的章节索引；两者冲突时以 `AGENTS.md` 为准，刻意的例外由 `CLAUDE.md` 穷举列出。也正因为 Claude Code 读的是它而不是 `AGENTS.md`，`CLAUDE.md` 属于四份文档同步集合的一员。其中最硬的一条是**起子代理必须显式选模型，不得默认继承主 Agent**，按任务需要的判断力分层——需要判断力（写计划、评审、架构取舍、疑难归因）用 `opus`，规格已定死的转录型实现用 `sonnet`，纯检索定位用 `haiku`。这条由 PreToolUse 硬门 `.claude/hooks/require-subagent-model.py` 强制：没显式传 `model`、且 `subagent_type` 未在 `.claude/agents/` 中钉好模型的调用会被拒绝。`.claude/agents/` 已提供三个钉好模型的角色：`impl-task`（sonnet）、`spec-review`（opus）、`code-quality-review`（opus）。`backend/tests/test_claude_subagent_model_hook.py` 是这个 hook 的回归网：以子进程方式跑真实脚本，两个方向都覆盖——既盖「绕过」（让继承模型的调用溜过去），也盖「误拦」（把合法调用堵死，逼人绕开守卫）。

PR 在合入前必须经过 codex 评审，且**每一轮的原始输出都要逐字贴回 PR**——零意见的轮次要贴，手动补跑的轮次也要贴，并附上触发方式、完整命令、head SHA、退出码与输出字节数，便于核对评审确实跑过、结论没被转述失真。判一轮成功要**退出码为 0 且输出非空**两个条件：codex 被 SIGTERM 杀掉时退出码同样是 0，只看退出码会贴出一条空评论、看起来像通过。P0/P1 阻塞并停下来交人决定；P2/P3 不阻塞、可如实说明后不改；优先级标签解析不出来时保守拦人而不是默认放行。评审意见可以在核实后驳回（codex 评的是 diff，未必了解运行时事实），但驳回要同时给出 PR 上的理由与证据、代码里记录取舍的注释，以及钉住既有行为的回归用例。合入一律需要人明确同意。评审的自动化本身是开发者本机的 Claude Code hook、不是仓库产物，新 clone 上没有它——规则依然成立，那就手动跑；机制细节见 `CLAUDE.md`。

### 测试架构

- 后端与前端静态契约使用模块路径、限定 scope、操作种类、目标和审核后的计数等语义身份。源码位置只能作为诊断元数据；行号、offset、CSS 顺序和源码切片都不得用来标识预期站点。
- 前端 `*.test.mjs` 用 `node:test` 覆盖纯逻辑，以及少量有明确理由的架构/安全/词汇/入口契约；`*.component.test.tsx` 用 Vitest、jsdom 与 Testing Library，通过 role、用户动作和状态验证可见行为。
- 组件行为不得由 CSS 几何或源码布局钉死。普通特性重构只有在可观察契约改变时才应修改测试。
- 已提交测试不得使用 skip/xfail/todo/only 禁用；repository policy 会同时检查测试入口及其 helper 模块，并禁止绕过共享 semantic-source 适配器直接读取生产源码。
- 前端源码策略必须保持有界：通过语法规则拒绝 AST 位置/集合顺序 API，以及源码语义命名值上的文本位置操作；共享 `semantic-source.mjs` 只能暴露 AST 语义，不能把文本切片、分行、下标或长度当作契约。不要为此实现整套 JavaScript 数据流解释器，普通数组操作仍然合法。
- backend 测试会在 xdist worker 启动前，由主进程预热一份仓库本地 Matplotlib 字体缓存。必须保留这个 controller 边界，不能让每个图谱 worker 各自重复枚举 macOS 字体。

## 文档维护

后续只要产品行为、启动方式、架构或开发约束发生变化，需要同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`
- `CLAUDE.md`

根 README 保持精简；同时更新 `docs/` 下负责该主题的中英文权威文档：`product-and-api`、`deployment-and-configuration`、`operations` 或 `development`。
