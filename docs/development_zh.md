# 开发与仓库契约

[返回 README](../README_zh.md) · [English](./development.md)

本文保留面向贡献者的架构摘要、验证门、工作流、测试架构和文档维护契约。完整 Agent/开发约束仍以 [AGENTS.md](../AGENTS.md) 为准，详细运行时架构以 [architecture.md](../architecture.md) 为准。

## 架构边界

- 后端 endpoint body 位于由 `backend/app/api/routes.py` 组合的领域 FastAPI router；聚合层只负责 composition/order，不承载产品 handler，也不提供兼容导出。边界测试直接检查领域 router 的 endpoint 所有权，并以语义 AST 检查聚合组合声明；不要假设 `include_router()` 一定把子路由平铺，因为新版 FastAPI 会保留惰性的 included-router 节点。领域 Pydantic model 位于 `backend/app/models/`；`backend/app/models/schemas.py` 是旧导入的兼容 facade，re-export 同一批 model object。
- 唯一 repository factory 按 `DATABASE_URL` 选择 `SQLiteRepository` 或 `PostgresRepository`；两者组合相同运行时边界。`RepositoryFacade` 是注入 `RepositoryRuntime` bundle 之上的后端中立 facade。application service 不拼装主业务库 SQL、不判断 dialect，也不 import 对侧 adapter。store 独占 product SQL 与 raw row selection；既定 application/query component 可组装 domain/application projection，例如 `NotebookSummaryQuery.from_row`。SQLite 保留 migration/maintenance 兼容 wrapper，PostgreSQL 拥有有界 Psycopg pool 和 checksummed migration。facade 操作仍是显式兼容 adapter 或源码守卫验证的单跳委托，真实目标必须与 ownership manifest 一致；这些单跳委托由 ownership manifest 固定。依赖方向固定为 factory/wrapper → facade → runtime → services → stores。`sqlite_identity.py` 与 `sqlite_notebook_sharing.py` 保留为兼容 re-export shim，请求 Context、`_COPY_CHUNK`、`_remap_json_ids` 等旧导出继续可 import。
- 离线生产维护统一通过 `open_maintenance_cli_repository`：PostgreSQL 停服确认/能力拒绝必须早于 factory 构造，随后由独立非池化 session 持有 fail-fast advisory lock，任何退出路径都关闭 repository。`BatchMaintenancePort` 是可移植编排契约；SQLite 文本向量转换保留为独立物理格式 port。PostgreSQL keyset 的谓词与排序都使用 `COLLATE "C"`，读取一页后释放数据库连接再等待模型；来源清单按 phase 排除隐藏投影源。离线 full gate 不连接 PostgreSQL，真实覆盖放在独立 PostgreSQL 16 lane。
- `RepositoryRuntime` 持有或引用组合后的运行态；`REPORT_CANCELLATIONS` 刻意保持 process-global canonical owner，runtime、report coordinator 与 module compatibility function 共享同一 identity reference。其他可变运行态（storage root、embedder、语言 cache、构建集合、Ask cancellation registry 与工件 cache）由 runtime 持有；完成组合后替换受支持的兼容属性时，所有已持有它们的消费者都会同步更新。Ask/report 同步提交失败会把已经创建的持久化 job/report 标记为 failed、注销 cancellation entry，再把提交异常重新抛出；成功 worker 的次序与既有 Ask 事务 checkpoint 不变。
- 重构前创建的数据库可原样加载。`scripts/verify_repository_snapshot.py` 使用精确的逐版本 migration manifest 与稳定 seed manifest，对 SQLite URI 路径做百分号编码，只在临时 backup 上构造 repository；cleanup 失败时只报告保留的 backup 路径，不输出私有行。它校验原 DB/WAL metadata 以及 SHM 的存在性和大小；连接 live WAL 时只豁免 SHM mtime，因为 SQLite 可能重建它。

当前 schema 版本为 33。这里指 SQLite schema。已提交的 v9 兼容 fixture 会经由 v10–v33 migration 升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖 Memory/Agent 与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，v17 覆盖论文元数据，v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标，v21 覆盖交互式规整 anchor 成员检查的归一化表达式索引，v22 增加持久化的 notebook 级 KG 构建任务，v23 增加每用户最新模型服务状态，v24 增加 kg_canonical_scratch，v25 清除旧用户模型凭据并新增部署级模型服务状态，v26 增加 knowhow 变更流水/里程碑，v27 增加 sources.chunked_at，v28 增加文档数量上限 schema，v29 确定性清理重复 cluster membership 并安装唯一索引，v30 增加 sources(notebook_id, file_hash) 内容哈希去重索引（上传去重 / batch_ingest 续跑），v31 只增加 inert、无 payload 的 shadow_change_log 与 shadow_capture_control 内部表，v32 增加 reports.understanding_json，持久化深度报告的问题理解确认契约，v33 增加 `(notebook_id, source_object_id/target_object_id, id)` 覆盖索引，供关系词法补召回稳定地做有界 keyset 查询。PostgreSQL v11 是配对业务 schema。临时 shadow 边界已有 preflight/control/guard、run-bound 原子 snapshot、有界可续跑 baseline COPY/H0，以及 fail-stop 单消费者正向 replicator 原语。replicator 连续校验全局 seq、在短只读 snapshot 仅为 upsert hydration 当前行，delete 保持 key-only 且 hydrated bytes 为零；同一 stable key 在 accepted prefix 内保留最后 event 并按全局最后 seq 排序，raw seq/checkpoint 仍连续，每个 identity 的最终 actual apply 覆盖 synthetic dependency contribution，只有 dependency-only identity 才引用计数一次 synthetic 行及其 bytes；短读窗口若在 allocated high-water 前结束，会在 hydration/apply 前立即判为 suffix gap；满窗口低于 high-water 时在同一 snapshot 探测相邻 seq，缺失即失败；PG apply 事务 claim worker 后、业务 DML 前复查既有 run/direction poison；poison 发布在 binding/checkpoint 校验后锁定检查该方向任意既有记录，完全相同视为 ACK-loss 成功，不同则 stale 且绝不新增第二条，再重新锁定 ledger+60 表并复核 snapshot source/target、live target identity 与精确 catalog后，把业务收敛、脱敏 progress 与 checkpoint CAS 同事务提交。批次硬上限为 4096 events/64 MiB；仅一个 final bundle 可独占超限，同 key replacement 若在已有其他 actual bundle 时使 bytes 超限则回滚并延后。FK 父闭包只读同一验证 source snapshot，每事件最多 64 行；固定 v11 图按 FK constraint branch 计数的上界为 9 个 row slots，依赖行计入 bytes且批内去重，不扫描 suffix log。PG 只延后 FK/UNIQUE ordering SQLSTATE，CHECK/NOT NULL 立即 poison；精确 PG11 catalog 的 82 个 unique surface 通过 NULL、按其他唯一列的非 NULL 等值/NULL `IS NULL` 与固定 predicate 定域的确定性 text/bigint 候选（`C` collation 文本 max 拼 `chr(1)`，或先走可索引 bigint MIN/MAX 快速路径选择 min−1/max+1，仅在两个 int64 边界都占用时扫描首个 gap），或仅限无入向 FK 且有 accepted current-final 恢复行的叶表同事务 delete/reinsert 来解 cycle。停车状态按 `(unique surface, row identity)` 跟踪；每个 stagnant pass 会停车所有可独立停车的冲突，final apply 成功会清除该 identity 的所有停车面。限制为 8 passes、32 actual statements/apply、16384 actual statements 总量；每次候选查询都计入预算，ordering、statement、pass、`ProgramLimitExceeded`/`DataError` 候选搜索与候选 UPDATE 容量耗尽保持 non-poison，`QueryCanceled` 保持瞬态并整事务重试，最终窗口不可停车的 UNIQUE 冲突则按最早实际 seq poison。worker 从 256 events/8 MiB 自适应倍增至硬上限，仍 ordering-blocked 时 non-poison；ack-loss 与 poison publication 使用相同 identity 绑定，snapshot 与业务 apply 前均要求 `progress.applied_seq == checkpoint.last_seq`。每个有效 batch 结局恰好记录一条脱敏 metric，batch events 使用实际 accepted/observed raw-event 数并尽可能保留 retries。瞬态错误整事务有界重试，SQLite path/file binding 失败使用专用 identity 异常而不依赖文本分类；已证明的确定性错误在实际阻断 seq 写一条脱敏 poison 后停止。显式运维 CLI 已提供 preflight/start-forward/status/verify；前台 worker 使用数据库时钟排他 lease、SIGTERM/INT 批次边界，并只在 FULL 校验、barrier/replay/poison、至少 7 天/100,000 events tail 等边界之后保守清理。`SHADOW_DATABASE_URL` 单独设置仍不启动同步，且只有该 CLI 可以读取；本阶段不含 cutover、反向复制或自动 active URL 交换。
Verifier 在 SQLite 只读 snapshot 记录 `Hv`，把规范化事实流式写入 owner-private 临时 spool，释放 SQLite 后才等待 PG checkpoint；随后固定 PostgreSQL `REPEATABLE READ, READ ONLY` snapshot 的 `Ht`，并用第二个 SQLite 事务扫描 `(Hv, Hseen]` 的全部 retained dirty key，只排除这些可证明的 concurrent key。PG retention barrier 一直保留到报告事务提交。Structural 校验覆盖精确 catalog、稳定 key 集与规范化哈希、源/目标外键、unique/cascade 和 storage root 内文件引用；Full 再覆盖选定领域投影、float32 bytes/dimension/norm/抽样 cosine，以及固定中英检索集（recall@12 下降不超过 1 个百分点、top-10 overlap 不低于 0.90、citation/source id 集合完全一致）。Cutover 还会在报告前复核 SQLite 仍 write-frozen，并要求 `Hv=Ht=MAX(seq)`、零 concurrent key、100% coverage 和前一轮完整 full/cutover 报告。持久报告只含安全表名、stable key hash、类别、计数和固定摘要；干净报告只能 supersede 同级或更强等级已覆盖的 drift。

Baseline snapshot 发布要求 owner-only 的真实目录并以 0600 独占创建临时文件。Snapshot/live fence 必须 fresh 打开当前 SQLite 路径，不复用 repository 线程缓存连接，并跨 open/transaction 及 snapshot 发布/PG commit 前复核 resolved path 与 device/inode。COPY 的所有业务 SQL 全限定到 run 绑定 schema，在每个关键绑定处短暂 `BEGIN IMMEDIATE` 复核 live capture 仍启用；JSONB prefix proof 只在 JSON 子树内把有限 int/float/Decimal 统一成精确十进制语义，普通 SQL 数值列仍保持类型差异。Resume 使用有界 named server cursor，长阶段受 statement timeout 与取消轮询约束；起始/最终按 checksummed migration 派生契约完整验证 v9 表、列、约束、operational/GIN index 与 `public.pg_trgm`，逐批仅做轻量控制验证，且最终 60 表 proof/`ANALYZE` 不持有 SQLite 栅栏。

最终 live SQLite fence 是跨 commit 的 lease：只在 PG 双锁/run/table lock 与 60 表长 proof/`ANALYZE` 完成后取得，保持到 PG H0 checkpoint + run progress 事务实际提交成功再释放；PG 失败不落 H0 并释放 SQLite，持 fence 时不得再等待 PG pool/advisory lock 或执行长 proof。

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

PostgreSQL 覆盖与离线 full gate 明确分离。`postgres-integration` job 启动 PostgreSQL 16，
创建最小权限与辅助 encoding/locale 目标，并通过 `bash scripts/check_postgres.sh` 只运行
`postgres_integration` marker。本地使用已安装的 PostgreSQL 16 和显式 `TEST_POSTGRES_URL`；
`scripts/check.sh` 不得启动或连接 PostgreSQL。

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

凡任务会写入仓库代码、测试、文档或配置，都必须在第一次写入前新建 linked git worktree 和分支，并在其中完成开发、验证及后续 PR；该任务期间本地主 checkout 保持只读，小修也不例外。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。纯调研、设计、状态汇报和只读审查不要求 worktree。

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

SQLite source open 的分类只在 `open_fresh_live_sqlite` 调用边界生效：非瞬态 `sqlite3.OperationalError` 归为 source-binding identity；locked、busy、interrupted open 仍按瞬态整批重试，后续 SQLite operational error 保持原 schema/query 分类。
