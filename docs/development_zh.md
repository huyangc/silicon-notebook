# 开发与仓库契约

[返回 README](../README_zh.md) · [English](./development.md)

本文拥有开发护栏、schema/迁移编写、验证、工作流与文档维护规则。
[AGENTS.md](../AGENTS.md) 是精简的 Agent 入口；运行时架构由
[architecture.md](../architecture.md) 拥有。

## 数值上限与截断

生产代码不得在调用点隐藏会改变结果的数字切片或上限。不可调的 wire/storage
边界复用具名协议常量；可在质量/成本之间调整的预算放入带校验的 `Settings`。
用户编辑的列表超过前后端共用护栏时必须明确校验/拒绝，不得静默切片。embedding
等模型输入截断必须在线、批处理与回填路径共用同一配置真源。测试 fixture
中的显式数字不在本规则范围内。

## 架构边界

运行时所有权与数据流以 [architecture.md](../architecture.md) 为准；修改对应功能前，
阅读其中的 [Repository 组合](../architecture.md#22-repository-组合与兼容-facade)、
[前端边界](../architecture.md#24-前端边界) 与 [核心数据流](../architecture.md#3-核心数据流)。
公开行为和精确数值上限由 [产品/API](./product-and-api_zh.md) 拥有，操作步骤由
[运维文档](./operations_zh.md) 拥有。以下只保留贡献者必须遵守的约束，不再重复实现历史。

### 依赖、授权与状态所有权

- 保持 `factory/wrapper → facade → runtime → services → stores`。store 独占业务 SQL
  与原始行选择；application/query 组件可以组装领域投影。service 不分支判断 dialect，
  不导入另一 adapter。稳定跨层值放在 `backend/app/domain`，repository ports 不依赖
  services，静态依赖图无环。领域 router 拥有端点实现，`app/api/routes.py` 只做组合；
  兼容 facade 重导出原对象，不新增第二套实现。
- extension SDK 保持依赖轻量。只有 `app.bootstrap` 连接 adapter 与
  `backend/app/extensions`；workflow 消费领域 host port，plugin 不导入具体 repository、
  facade 或 runtime。availability probe 不做 I/O，capability port 保持窄投影，
  某 contribution 不可用不能关闭其他独立贡献。保留启动冻结的路由/准入、无贡献时原样
  返回 baseline、有界批量 hydration、取消传播与不得持连接执行的边界。
  扩展编写和 UI 包约束见 [部署扩展 SOP](./deployment-extensions-sop_zh.md)。
  模块化扩展 PR 仍需两次独立 subagent review，并遵守 [评审与 CI 流程](#开发流程)。
- `backend/app/application` 的 application stage envelope 保持不可变、依赖轻量。
  有意扩展显式 import allowlist，不放开会绑定 `app` 的裸根 import。Ask/Report stage
  接缝保持精确 source scope、retrieval run、actor、取消 token 和 connection probe
  的身份；在既有边界重验授权，违反契约直接失败。stage wrapper 不持数据库连接或外层
  leaf-I/O 槽位；core 拥有最终审计与落库。终态后 observer 不得改写已提交产物或
  逆转 `done`；`report.completed_observer` 另不得启动检索/模型工作。修改 Ask 完成
  路径时保留其既有 observer 顺序、失败隔离与成本。
- G1 的 `scripts/check_architecture_boundaries.py` 守住
  `scripts/architecture_boundary_baseline.json`：repositories→services 债务与 facade
  表面只减不增，core/models→services import、列出的热点函数长度、repository
  Protocol 方法数均无预留空间；缩小时同批下调 baseline。退役 facade 成员按
  [调用方台账](./superpowers/plans/2026-08-23-facade-retirement-ledger.md) 逐批进行，
  用 `scripts/audit_facade_callers.py` 复现，并同步 ownership/surface fixture：
  `scripts/generate_repository_contract_fixtures.py --rebaseline-surface`。
- 两种 adapter 的 `access_sql.py` 与 `mount_sql.py` 判据同步维护。新的 notebook 写
  端点走 `require_notebook_capability(...)`；在 body 中才解析身份的路径也使用同一
  capability 表及独立 mirror-write fence。先授权再返回镜像写错误，保留已登记的
  报告创建者私有权限例外。不得根据空/null principal id 猜 grant 种类或绕过实时读权；
  读判据扩展时同步更新 Memory 授权锁位 allowlist。access-SQL 与
  notebook-capability 守卫负责验证。
- participant 集合替换只由 `retrieval_participants.py` 在检索消费边界提供，
  `global_run.py` 是唯一安装者。授权仍走真实 mount/read 判据，`source_scope.py`
  只能收窄已证明的集合。保留精确 reader/writer allowlist、actor/run 绑定、无内容
  cache fingerprint 和 `ParticipantOverrideError` 传播。新增可读该上下文的
  fail-soft handler 必须登记到 `_SEAT_FAILSOFT_SITES`；`_bounded_participants`
  必须在这些 handler 外先验证。模式问题 `subjectless_run_active` 与过滤问题
  `peer_scope_ceiling_active` 分开判断。安全边界由
  `test_participant_override_guard.py` 守住。
- 可变运行态归 `RepositoryRuntime`，`REPORT_CANCELLATIONS` 是刻意保留的进程全局
  例外，与 coordinator 和兼容函数共享同一身份。领域 builder 只接较早的 frozen
  bundle，不接 runtime 本身；保留窄迟绑定 accessor 与启动副作用顺序。组合后受支持
  的替换必须同步到所有既有消费者。
- KG edge 定义只放在 `domain/kg/edge_schema.py`，service shim 不定义新 `EdgeSpec`。
  production 不导入 `app.eval`，不从分数重建 provenance，不新增第二套 selected-source
  rollout parser/activation 路径。新增检索贡献保持冻结 baseline，先授权再 hydration；
  工件与来源范围身份由服务端拥有。
- 停服维护使用 `open_maintenance_cli_repository`，先做 factory 前安全检查，再取得
  独立 session 锁；模型调用前释放页读连接，最终始终关闭 repository。在线 scale CLI
  使用独立组合根，强制 `migrate=False, seed=False`，组合前验证 schema，并持
  per-notebook claim 到发布完成。全部工件根及回滚/退役复用 staging/swap 与 claim
  检查，不得原地覆盖活目录。操作步骤见 [运维文档](./operations_zh.md)。

### 前端实现护栏

- workspace hook 独占领域状态，壳层只消费 readonly view 与具名 command，不传递其他
  领域 setter。保留精确 actor/notebook/generation 所有权、迟到响应拒绝、删除 tombstone、
  single-flight 与既有请求预算。导航只脱离持久任务，显式 Stop 才取消。新增 notebook
  owner 登记在 `notebookTransitionSteps`，统一走 `notebook-transition.ts` 的
  begin/commit/settle 路径，root-modal 清理仍在首位。
- `api-client.ts` 拥有 HTTP mechanics，领域 API 模块拥有 endpoint policy；生产
  `fetch` 只能在共享 transport。根弹窗使用 `use-root-modal-coordinator.ts` lease：
  异步前 issue，仅向当前 owner publish；被覆盖的弹窗 inert/ARIA-hidden，提交后确认
  底层 lease 仍有效才归还焦点。关闭弹窗不能释放动作仍在飞的互斥。
- 复用来源/图谱 renderer 和 readonly props，不把领域状态搬进呈现组件。UI extension
  declaration 只投影元数据，在启动/构建期冻结，并受精确 tuple、capability、UI mode
  与 owner 门控。新增贡献后运行 `scripts/generate_ui_extension_contract.py` 更新
  `backend/tests/fixtures/ui_extension_contract.json`，通过 `--check` 对账。
  内建 registry 的 import 闭包保持 `.ts` 以兼容 Node 测试。本地 UI 包遵守
  [扩展 SOP](./deployment-extensions-sop_zh.md) 及独立部署验收门，不放宽基础仓库
  的零插件 registry 断言。
- 修改群组/Schema 面板时保留既有样式与行为守卫，不复制 CSS 或使用未定义类名。
  Schema 选择/草稿身份是 `(object_type, proposal-status)`，写入回调只向发起时的
  pane 提交可见结果，`SchemaWriteOutcome` 的 `confirmed`、`unconfirmed` 与
  `failed` 三值不可合并；已提交但未确认的写入不得提示为失败并要求重试。
- `globals.css` 只保留一条元素级按下基线：
  `button:not(:disabled):not([aria-disabled="true"]):active`，使用 `opacity: .7` 与
  `filter: brightness(.88)`，不加 `transform`、`translate`、`scale`、`rotate`
  或基线 `transition`，避免几何变化吞掉边缘点击或覆盖定位。动作结果落在所按控件
  本身或紧邻处，页面横幅不能单独充当反馈；长任务在飞时另需禁用或替换控件。
- 复制结果复用 `copy-result.ts` 的 `useCopyResult`，按所复制 token/item 的身份分格，
  身份变化时 reset，由共享 timer 恢复 idle。JSX 保留字面量结果类，
  `button.copy-result-copied` / `button.copy-result-failed` 同时声明 background
  与 hover 规则。失败时仅在 `input.value === link` 仍成立后选中紧邻只读输入框。
  `button-press-feedback-guard`、`long-task-button-guard`、
  `command-catalog-button-guard` 与复制结果组件测试共同守护。
- Secure Context 限定 API 必须有共享退路：前端 id 走 `client-request-id.ts` 的
  `newClientRequestId()`，复制走 `copy-text.ts` 的 `copyTextSafely()`。
  发请求前的提交准备也要放进错误边界，使同步异常能还原草稿并释放忙碌状态。
  `secure-context-api-guard.test.mjs` 守住 HTTP 内网部署。
- 问答输入区停止键复用 `stop-control.tsx` 的 `STOP_CONTROL_CLASS` 与 `StopGlyph`，
  只显示图标，语义写入 `aria-label`/`title`，停止中禁用；报告带字动作行只复用图标。
  停止后轮次的判据与文案只来自 `stopped-turn.tsx`，被停止的 notebook 轮次不得作为
  可重发的 storage 草稿复活。全局问答复用既有会话对账与 missing-job 路径，调用点
  不另算替换/历史状态。
- 共享流使用 `task_stream.py::deliver_ask_events` 与浏览器 `ndjson-stream.ts` 行读取，
  route 模块不互相导入。`yieldToPaint` 保留后台标签页需要的 timer 兜底。权限变动走
  `use-notebook-collection.ts::refreshAfterAccessChange` 与壳层窄
  `reconcileOpenNotebook` effect，使已打开工作区与列表一起对账。

### Schema 与迁移编写

- SQLite schema 变更新增 `_migration_N`，同步提升
  [migrator](../backend/app/repositories/sqlite/migrations.py) 的 `SCHEMA_VERSION`；
  不修改已封存迁移。启动恢复、稳定 seed 与管理员原地升级不进入版本门，每次启动照跑。
- PostgreSQL 在 [migrations](../backend/app/repositories/postgres/migrations/) 追加
  连续编号 SQL，并同步 [POSTGRES_SCHEMA_MANIFEST](../backend/app/repositories/postgres/schema_manifest.py)。
  [migrator](../backend/app/repositories/postgres/migrator.py) 在迁移锁下验证 checksum
  与 ledger，不能改写已应用 SQL。当前版本号与逐版本 DDL 以这些可执行源文件为准。
- 同时保留新库与升级行为、null/default 语义、归属、keyset 排序/collation、
  FK/cascade/unique surface 和复制/清理分类。表特有的设计理由写在引入它的迁移旁；
  同批更新受影响的 schema/seed/snapshot fixture 与 migration manifest，不能
  重设旧兼容 fixture 的基线来掩盖升级破坏。
- SQLite 迁移单向：数据库 `PRAGMA user_version` 超前时以
  `schema contains a future version` 拒绝启动；PostgreSQL 同样拒绝超前 ledger。
  readiness 只暴露脱敏的初始化失败。恢复使用升级前备份或兼容/更新的程序，
  不执行反向迁移。执行与切换步骤见 [运维文档](./operations_zh.md)。
- 冻结的 [v9 fixture](../backend/tests/fixtures/repository_v9/) 与
  `scripts/verify_repository_snapshot.py` 保留升级兼容验证。snapshot 校验只在临时
  backup 上构造 repository，验证精确逐版本迁移与稳定 seed，并保持原始 DB/WAL
  metadata 及 SHM 存在性/大小不变（只有 live-WAL 的 SHM mtime 可不同），不记录私有行。

以前的逐版本叙述和交付说明可查阅
[精简前历史](https://github.com/huyangc/silicon-notebook/blob/403b796f/docs/development_zh.md#架构边界)。
这些只供追溯；现状以上面链接的运行时契约、迁移源文件和运维步骤为准。

## 验证

运行：

```bash
bash scripts/check.sh
```

验证门禁分为四级：

| 级别 | 范围 | 执行频率 |
| --- | --- | --- |
| G0 目标测试 | 按当前改动文件与行为选跑 | 编辑循环中随时执行 |
| G1 标准门 | `scripts/check.sh`：稳定后端、契约/harness、前端测试、负责类型检查的 production build 与补测试文件诊断的包内类型检查 | 本地交付前以及每次 PR/push/手动 CI |
| G2 扩展门 | `scripts/check_extended.sh`：G1 加真实索引/性能测试、冷图/索引契约与全仓语义扫描（重活子集） | 每天 `17 18 * * *` UTC（北京时间次日 02:17）一次，也可手动触发 |
| G3 PostgreSQL | `scripts/check_postgres.sh`：直接 PostgreSQL adapter 集成 | 独立的 PR/push/手动 CI job |

G1 并行运行三个有界 lane：`check_backend.sh` 以默认 12 个 worker 执行稳定 backend pytest；`check_contracts.sh` 执行语法/依赖预检、hermetic smoke、契约检查与确定性抽取评分 harness；`check_frontend.sh` 执行递归发现的全部 `*.test.mjs`、全部 `*.component.test.tsx`、production build 与包内类型检查。Node 原生 test runner 和 Vitest 各限制为 4 workers，为 backend 临界路径保留 CPU；Next build 不得启用 `ignoreBuildErrors`、仍是生产代码类型检查的权威，但 Next 的构建期类型检查会静默丢弃 `*.test.*`/`*.spec.*` 文件与 `__tests__`/`__mocks__` 目录里的全部诊断（`next/dist/lib/typescript/runTypeCheck.js` 的 `ignoreRegex`，Next 15.5 实测），只存在于 `frontend/tests/**` 的类型错误对 build 完全不可见；因此前端 lane 在 build **之后**补跑 `npm run lint`（`tsc --noEmit`）——排在 build 后是让它检查刚重新生成的 `.next/types` 而不是脏树上的陈旧生成物——这是唯一看得见测试文件的检查；`incremental` 使 warm 重查不到 1 秒（冷 ~5 秒），当初「同一程序重复解析两遍」要省的成本已不再成立。G1 backend 排除 `slow` 真实索引/性能用例、`graph_index_contract` 冷图/索引契约、`architecture_contract_heavy`（`_ARCHITECTURE_CONTRACT_HEAVY_TESTS` 中的八条全仓语义扫描）和 PostgreSQL 树，其余轻量 `architecture_contract` 测试随 G1 每次 PR/push 跑；G2 先执行 G1，再执行精确互补的 backend marker 集——`backend/tests/test_test_architecture_policy.py::test_verification_lane_markers_partition_every_architecture_contract_test` 用 `--collect-only` 实测验证这个划分，不只是钉两条 `-m` 字符串。每个 lane 都有独立进程组，因此中断或终止 controller 时，也会终止并回收 pytest、npm 和 Next.js 的后代进程。官方 client MCP smoke 精确锁定已公开的二十八个工具：七个 Memory/context、四个 knowhow、一个引用点查、七个来源、三个构建、两个库理解与四个全局问答工具。缺少 `frontend/node_modules` 会直接失败，不再静默跳过前端门禁。

验收时使用项目一直采用的 Homebrew/Miniconda Python：

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

G1 标准门并发运行 backend、contracts、frontend 三个 lane。`check_backend.sh` 默认使用 12 个 backend pytest worker，可用 `BACKEND_PYTEST_WORKERS` 覆盖。Apple Silicon warm gate 硬目标是不超过 60 秒；G2 每日扩展门不受该本机时限约束，各 CI lane 时长仅作观察，因此这不是对每一台 CI 机器的可移植超时断言。

测试加速必须保持结果语义：G1 标准门与 G2 扩展门的 marker 表达式精确互补，PostgreSQL 独立负责，任何已提交用例都不能变成不可达；全仓 AST/协议扫描在同一测试进程（pytest worker 或隔离的 Node guard 进程）内只解析每个生产文件一次，冻结 fixture 用例若只关心成员集合，就应使用只投影名称的入口，不生成详细站点、签名与 ownership；可执行的全仓守卫只在其归属的 contracts lane 对真实源码树运行一次，参数解析、失败模式与 extra-root 的单元测试把默认根重定向到最小 fixture，不再重复扫描全仓；同一份不可变行为矩阵上的断言只遍历一次，并给每一格保留明确失败标签，不能为每一格或不同断言族重复搭建完全相同的数据库世界；frontend lane 只同步一次不可变的本地插件投影，随后仅抑制 npm 重复的 `pretest`/`prebuild`/`prelint` 钩子，开发者单独执行每条命令时这些钩子仍是必跑项；缓存容器策略直接验证容器，不搭建无关数据库与 ANN 索引；autouse 隔离路径从 worker 已有的 pytest base temp 派生，不为每条纯测试额外创建 `tmp_path`；普通 SQLite 仓储测试按 worker 只构建一次当前空 schema，再复制成每条测试各自独立的可变数据库文件，迁移/升级/仓储快照模块必须登记 `_REAL_SQLITE_MIGRATION_MODULES` 并执行真实迁移梯；仓储密集测试只可在 pytest autouse fixture 中降低默认密码派生成本，认证 helper 保留生产默认，比较凭据字段的快照模块必须登记 `_REAL_PASSWORD_HASH_MODULES`；普通 UT 与 G1 测试必须环境自足，不绑定宿主端口、不依赖环境服务；只有合同本身属于进程级行为时才保留自包含的子进程/信号覆盖。并发顺序与公平性使用 event/barrier，而非固定 sleep 或线程唤醒顺序；分波次排队时由控制线程运行被测同步编排，在观测到目标容量后用 event 放行，不能让后一波单独落进 cyclic barrier；进程级延迟任务须在共享 teardown 中取消待执行项并等待活跃项收敛，不能只清理由某个局部 repository 对象可见的任务。

测试 fixture 的成本应跟随被测行为。Scale-build 的锁准入与接力测试使用尚未建索引
的笔记本；真实 build/fold/发布测试继续保留所需的种子数据或索引产物。Scale CLI 的
未知笔记本拒绝用例使用真实迁移后的数据库，无需导入、知识图谱或向量；非 PostgreSQL
URL 拒绝用例使用可连接的 SQLite 文件。完整 CLI 制品流程仍执行导入、抽取与嵌入，
其中只读查找笔记本不再重复迁移和初始化。正常阶段事件
与进度回调断言共用一次 facade build，回调失败与直接 builder 的产物合同仍独立覆盖。
三条笔记本生命周期字面量扫描共用一个 `xdist_group`，确保进程内 AST 缓存确实复用；
G1/G2 选择表达式不变。前端源码策略检查在隔离的 guard 进程内复用不可变模块输入
与解析树。

计时器测试通过受控时钟推进原有截止时间，并保留到期前后的状态断言，不再等待真实
时间。后台删除 HTTP 测试用事件暂停和释放真实 runner；启动 sweeper 测试等待观察到
周期 sweep，并检查关闭后线程确实退出。真实仓库的 UI 词汇扫描非空性归 contracts
lane 的 `check_ui_vocabulary.py` 负责；单元测试保留最小空扫描失败 fixture 和变异
覆盖，不再重复执行一次全仓计数。

扩展服务生命周期测试共用一个 `xdist_group`，限制相互争抢资源的 supervisor 启动数；
每条测试内部仍保留真实进程和线程并发。状态文件回归测试在打开后强制替换路径：
JSON 读取方可读取已打开、当前用户所有的普通文件快照，即使其最后一个链接已移除；
锁和可写句柄仍要求单链接，不安全文件仍被拒绝。

### GitHub Actions CI

`.github/workflows/ci.yml` 把 G1 暴露为 `CI / level-1-standard`，在目标为
`master` 的 PR、`master` push 与手动触发时运行；
`.github/workflows/daily-extended.yml` 把 G2 暴露为
`Daily Extended Gate / level-2-extended`，只保留每日一个 cron 和手动触发。
两者固定使用 `ubuntu-24.04`、Python 3.13、Node.js 22，从声明的依赖文件安装，
并把测试选择委托给对应 wrapper。G3 保持为
`CI / level-3-postgres-integration`。

`CI / level-1-frontend-node26` 在 Node.js **当前**大版本上重跑前端泳道与生产构建，
触发条件与 G1 相同。文档承诺的是「Node.js ≥ 20」而 G1 钉 22，没有这条泳道，承诺的
上半段就无人验证：Node ≥ 24 自带 Web Storage 全局，不给 `--localstorage-file` 时它们
的 getter 返回 `undefined`，而 vitest 的 jsdom 环境会让它们盖住 jsdom 自己那份——凡是
读 `localStorage` 的组件测试都在开发者本机整片红、CI 却全绿。
`frontend/test-support/setup.ts` 只在内建 storage 取值为 `undefined` 时补回真正的 jsdom
storage，并连 `Storage` 类一起补（否则 `vi.spyOn(Storage.prototype, …)` 会静默打空），
Node 22 上行为逐字不变。该泳道同时跑生产构建——那次修复的第一版误引了没有类型声明的
`jsdom`，正是被构建的类型检查当场抓到的。

PR/push G1 在独立 runner 上运行 backend、contracts 和 Node 22 frontend 三条泳道。
后端把同一个 G1 收集集合分成两片，每片四个 pytest worker；显式执行
`check_backend.sh --shard-index 0 --shard-count 2`（或 index 1）才启用分片，
分片环境不会泄漏给嵌套 pytest。插件在正常 marker 筛选后分配，保持每个
`xdist_group` 完整，普通模块也不拆散；使用
`backend/tests/fixtures/g1_module_timings.json` 的耗时提示、未知模块回退到收集条数，
确定性地平衡负载。提示只影响放在哪片，新增模块自动纳入，不影响覆盖集合。
`check.sh` 仍是本地完整门禁，G2 选择不变。原 `level-1-standard` 汇总三条泳道，
只有全部成功（含后端矩阵的两片）才成功；失败、取消、跳过或缺失都不能放行。
G1 墙钟时间从首条泳道开始计到汇总完成，包含安装与 runner 调度，不能只报汇总
job 自己的几秒钟。这种隔离与分片用更多 runner 分钟换取更短的等待时间。
backend 与 contracts wrapper 单独执行时仍隔离部署环境，打印慢 pytest 时长，
并将 JUnit 写入 `backend/.local`；CI 包括失败时也上传，保留七天。

六条生产源码扫描共用一个 worker 和按文件内容校验的 AST 缓存；每次仍重读文件，
修改不会复用旧树。Agent Profile job base/overlay 测试使用当前 schema 的独立副本，
专门的 store/升级用例仍执行真实迁移并保留旧 schema 断言。

已提交的 OpenAPI 契约是字节语义冻结契约，因此
`backend/requirements.txt` 精确固定 FastAPI `0.135.3` 与 Pydantic
`2.12.4`。只能在有意重生 OpenAPI 契约并在干净环境跑 G2 扩展门时，
才同步升级这两个框架。

该 workflow 只有读权限，不接收模型或部署 secrets，并把后端 pytest worker
限制为每台 runner 4 个，避免 GitHub 托管 runner 过度抢占。后端安装设置
`HNSWLIB_NO_NATIVE=1`，G1 Python 泳道共享 G3 专用的可移植 wheel cache，使用精确的
OS/架构/Python/requirements/策略 key，不回退到其他缓存；G2 继续禁用 pip wheel cache。
`hnswlib` 默认会用
`-march=native` 编译，把这种本机 wheel 缓存后恢复到 CPU 特性不同的托管
runner，可能以 `SIGILL` 崩溃。CI 使用可移植构建，以少量 ANN 性能换取确定性；
生产 wheelhouse 仍可按已声明的部署 CPU 定向构建。每条 G1 执行泳道的 20 分钟 timeout 包含依赖安装，
与 Apple Silicon 本地 warm gate 的 60 秒内目标刻意分开。初次接入时
`CI / level-1-standard` 仅用于观察；只有在 PR 与合并后的 `master` 都稳定绿跑后，
并由用户明确批准分支保护变更，才把它设为 `master` 的 required check。

PostgreSQL 覆盖与离线门禁明确分离。`level-3-postgres-integration` job 启动 PostgreSQL 16，
通过 `bash scripts/check_postgres.sh` 选择 `postgres_integration or postgres_lane_contract`；
后者将环境自足的适配器/迁移契约与 launcher/目标安全检查纳入它所归属的泳道。
仅 CI service 将 PGDATA 放在上限 4 GiB 的 tmpfs，减少短命 schema 的宿主磁盘开销。
WAL、`fsync`、`synchronous_commit`、`full_page_writes` 保持默认值，建库时检查后三项；
job 记录存储用量与容器内存峰值。此泳道验证运行中的数据库行为，不验证容器丢失或宿主
断电后的介质持久性；生产与本地 PostgreSQL 存储不变。CI 先创建四组显式的主库、
非 C UTF8 库与非 UTF 库，再交给权限不变的最小权限应用账号。`TEST_POSTGRES_TARGETS_JSON`
是四个对象组成的数组，每个对象包含 `primary`、`non_c`、`non_utf` URL 字段；同一显式
服务端地址上的十二个数据库必须互不相同。launcher 在启动四个 pytest worker 前校验并
预检全部目标，每个 worker 仅使用自己的三个库，密码只存放在临时 pgpass 文件。
目标缺失或重复、串行与并行配置混用、未知 worker 都立即失败，禁用 worker 自动重启。
每条测试仍创建独立 schema 并执行真实迁移；数据库级隔离避免固定迁移 advisory lock
互相竞争。锁观察查询必须限定当前数据库，服务端全局活动视图不会因 schema 隔离而隔离。
batch4 的只读计划矩阵共用一份规模不变的大语料；在线安装与迁移变异测试仍保留独立 schema。
batch2 的 payload 计划矩阵只创建一次 10 万行笔记本：先检查稀有词的自然计划，再加入
其他笔记本的 2 万行命中数据并重新 ANALYZE，检查位图访问被限定在当前笔记本且本地
结果为空。两组原有计划断言仍可达，语料规模和计划检查均不缩减。

本地仍可使用已安装的 PostgreSQL 16 和显式 `TEST_POSTGRES_URL` 运行串行泳道，此时
不得同时设置并行 JSON 变量；权威 CI 仍要求辅助目标齐全。`scripts/check.sh` 不得启动
或连接 PostgreSQL。PG launcher 固定报告慢 setup/call/teardown，并输出
`backend/.local/postgres-junit.xml`，CI 即使失败也上传；不会把任意 `PYTEST_ADDOPTS`
转发给隔离的子进程。PG 依赖缓存使用专属 portable wheel 目录及精确的
OS/架构/Python/requirements/构建策略 key，不回退到通用缓存，仍必须设置
`HNSWLIB_NO_NATIVE=1`。整条 PG job 以三分钟为优化目标，测试全绿本身不代表达到目标，
实测需计入冷缓存时的依赖安装。
该泳道只覆盖直接 PostgreSQL 行为；已退役的 SQLite 后端实现专项测试、
SQLite→PostgreSQL 导入/正向 shadow 测试与跨后端 parity 测试不属于当前覆盖。

CI 可移植性属于门禁契约：所有由 CI 执行的测试使用的文件系统、数据和依赖
路径都必须相对仓库，并且独立于进程 cwd。已提交 fixture 必须从其仓库文件位置
定位，禁止依赖开发机 checkout 绝对路径或 `HOME`，测试也不得读取仓库外源文档。
测试启动时直接导入的第三方包必须声明在 `backend/requirements.txt`；干净 hosted
runner 必须从该文件和 `frontend/package-lock.json` 安装，并且只凭这些声明即可
全绿。各 lane 时长继续输出供观察，60 秒内目标只约束已验证的 Apple Silicon
Homebrew warm gate。

依赖仓库外 PDF 解析产物的 gold 生成、构建与校验脚本仍属于 developer-only
工具并保持在 `scripts/check.sh` 之外；该例外绝不适用于已提交测试。

效率是一等工程约束。新增 LLM、embedding 或数据库调用前，必须评估能否合并、
缓存、延后、异步执行，或按用户真正进入的界面再门控触发。强一致与提前计算必须
显式选择；普通路径默认保持低开销。

跨界面的前端交互护栏保持精简但必须明确：

- 启动长任务的控件在动作发出后必须立即禁用，并显示忙碌文案或 spinner，直到该
  动作结束；服务端单飞不能替代本地防重复提交。新入口若超出有界的长任务守卫，
  必须扩展守卫或在评审中登记覆盖缺口。
- 新增居中或可拖动弹窗必须复用 `FloatingModalCard`，并经 root modal coordinator
  发布，不得另造遮罩、拖动、焦点、色板、边框或圆角行为。
- 来源活动异常只能经 `sourceAnomalies()` 支撑的 `AnomalyBadge` 渲染，不得手搓
  内联警告样式或符号。
- 五档强度选择必须复用 `frontend/app/effort-picker.tsx::EffortPicker`，不得复制 range
  控件或重造其 popover；`frontend/tests/unit/effort-picker.test.mjs` 钉住共享实现及调用方。

## 开发流程

凡任务会写入仓库代码、测试、文档或配置，都必须在第一次写入前新建 linked git worktree 和分支，并在其中完成开发、验证及后续 PR；该任务期间本地主 checkout 保持只读，小修也不例外。如果当前目录已经是隔离的 linked worktree，则继续在当前 worktree 内工作。纯调研、设计、状态汇报和只读审查不要求 worktree。

行动前明确预期结果与授权范围。评审和设计任务交付发现或方案，实施任务交付已验证的改动。评审请求不授权代码修改或远端交付，本地实施请求也不授权部署、对外发消息、创建 PR、合入或其他远端变更。在授权范围内自行决定常规实现选择，持续推进直到完成预期结果，并说明重要假设。只有缺失信息会实质影响正确性、范围、兼容性或授权，且无法从已有证据解决时才询问；等待回答期间继续不依赖该回答的工作。既有授权在其明确范围内持续有效，不重复索取同一批准。

对于已经批准的多步骤实施计划，默认采用 subagent-driven development：每个任务交给一个全新的实现子 Agent，并在进入下一任务前完成该任务范围内的规格符合性与代码质量审查。小任务、纯调研、设计、状态汇报和只读审查不强制使用子 Agent。只委托范围明确、文件与任务归属清晰、验收标准及验证责任明确的工作。归属不冲突的独立任务可以并发；不要为了多用代理而拆分小任务。按验收标准和具体疑点复核委托结果，不把整项任务重新做一遍。

编辑期间运行聚焦检查，声明实施完成前运行规定的标准门。两者通过后即停止验证，除非后续改动、失败或具体未解决风险需要重跑；此停止条件不豁免规定的验证通道或下述 PR 评审与 CI 检查。不要新增仅复述实现细节的测试。只读评审检查 diff 和提交方的验证证据；除非用户明确要求，否则不修改工作树，也不重跑完整门。

交付简洁并先说结果：说明改动、已运行及未能运行的检查和剩余局限，不重复执行过程。评审应注明被审版本与范围；每项发现都关联具体触发条件、影响和代码位置，区分已验证行为、静态推断与架构债务。

共享开发、验证与交付规则由本文及英文配对文档拥有。`AGENTS.md` 提供通用 Agent 入口和权威文档路由；`CLAUDE.md` 补充 Claude Code 专属常驻规则。共享规则有冲突时以对应权威文档为准并修正入口；载体专属规则只适用于其点名的载体。两个入口都不是产品或架构的副本。

Claude Code 自动加载 `CLAUDE.md`。其中最硬的 Claude 专属规则是**起子代理必须显式选模型，不得默认继承主 Agent**，按任务需要的判断力分层——需要判断力（写计划、评审、架构取舍、疑难归因）用 `opus`，规格已定死的转录型实现用 `sonnet`，纯检索定位用 `haiku`。这条由 PreToolUse 硬门 `.claude/hooks/require-subagent-model.py` 强制：没显式传 `model`、且 `subagent_type` 未在 `.claude/agents/` 中钉好模型的调用会被拒绝。`.claude/agents/` 已提供三个钉好模型的角色：`impl-task`（sonnet）、`spec-review`（opus）、`code-quality-review`（opus）。`backend/tests/test_claude_subagent_model_hook.py` 是这个 hook 的回归网：以子进程方式跑真实脚本，两个方向都覆盖——既盖「绕过」（让继承模型的调用溜过去），也盖「误拦」（把合法调用堵死，逼人绕开守卫）。

PR 在合入前必须经过 codex 评审，且**每一轮的原始输出都要逐字贴回 PR**——零意见的轮次要贴，手动补跑的轮次也要贴，并附上触发方式、完整命令、head SHA、退出码与输出字节数，便于核对评审确实跑过、结论没被转述失真。判一轮成功要**退出码为 0 且输出非空**两个条件：codex 被 SIGTERM 杀掉时退出码同样是 0，只看退出码会贴出一条空评论、看起来像通过。P0/P1 阻塞：核实后把站得住的意见修掉并重审，直到判定转为非阻塞——只有意见站不住（走下面的驳回规则）或修复方向需要人拍板时才停下来交人决定；P2/P3 不阻塞、可如实说明后不改；优先级标签解析不出来时保守拦人而不是默认放行。评审意见可以在核实后驳回（codex 评的是 diff，未必了解运行时事实），但驳回要同时给出 PR 上的理由与证据、代码里记录取舍的注释，以及钉住既有行为的回归用例。合入不再逐次征求同意：评审非阻塞**且** CI 全绿时直接 `--rebase` 合。评审仍阻塞或输出解析不出等级时一律不合——先修掉并重审；CI 未全绿、或用户说过等他自己合，同样不合。CI 判绿只认 `gh pr checks` 全部 `pass`——`mergeStateStatus: CLEAN` 只说没有东西拦着合并，不等于检查跑绿了。合入前还必须在 PR 上确认**针对 PR 远端 head（`headRefOid`）的评审已经贴出**（不能用本地 `git rev-parse HEAD`：本地落后时会命中一条旧评审而放行，而合入的是远端那个未经评审的 head）：评审自动化静默没触发，和它跑完判了通过，在外部看起来一模一样；agent 的汇报和 hook 的本地状态都不是证据，PR 上的那条评论才是。评审的自动化本身是开发者本机的 Claude Code hook、不是仓库产物，新 clone 上没有它——规则依然成立，那就手动跑；机制细节见 `CLAUDE.md`。

### 测试架构

- 与规模无关的边界分支只允许降低测试局部阈值，并另行钉住生产 floor。检查同一不可变索引/产物多个视图的断言共享一次真实构建；只验证算术或观测分支的用例走最小归属接缝，同时邻近集成覆盖仍须真实构建、打开并查询该产物。
- 后端与前端静态契约使用模块路径、限定 scope、操作种类、目标和审核后的计数等语义身份。源码位置只能作为诊断元数据；行号、offset、CSS 顺序和源码切片都不得用来标识预期站点。
- 前端测试不得再与生产代码混放：`frontend/tests/unit` 放 `node:test` 纯逻辑用例，`frontend/tests/guards` 放架构/安全/词汇/入口契约，`frontend/tests/component` 放 Vitest/jsdom/Testing Library 行为用例。共享 setup 和语义源码适配器位于 `frontend/test-support`；runner 递归收集这些目录，位置守卫会拒绝 `frontend/app` 或 `frontend/features` 中的测试。
- 组件行为不得由 CSS 几何或源码布局钉死。普通特性重构只有在可观察契约改变时才应修改测试。
- 每条新增的静态或语义守卫在合入前都必须做变异验证：删除受保护行为和把违规形态移动到另一个相关站点，两种变异都必须让守卫报红；并且要先确认变异确实命中了预期站点。记录结果后移除变异；对一条实际未生效的变异跑出绿色不构成证据。
- 已提交测试不得使用 skip/xfail/todo/only 禁用；repository policy 会同时检查测试入口及其 helper 模块，并禁止绕过共享 semantic-source 适配器直接读取生产源码。
- 前端源码策略必须保持有界：通过语法规则拒绝 AST 位置/集合顺序 API，以及源码语义命名值上的文本位置操作；共享 `semantic-source.mjs` 只能暴露 AST 语义，不能把文本切片、分行、下标或长度当作契约。不要为此实现整套 JavaScript 数据流解释器，普通数组操作仍然合法。
- backend 测试会在 xdist worker 启动前，由主进程预热一份仓库本地 Matplotlib 字体缓存。必须保留这个 controller 边界，不能让每个图谱 worker 各自重复枚举 macOS 字体。

## 文档维护

配置改动必须明确使用者和归属文档。`.env.example` 只提供常用部署选择（连接、凭据、
模型兼容、资源容量、使用及保留策略），不镜像全部 `Settings` 字段。可选默认覆盖值
使用注释赋值，让复制后的环境继续继承改进的代码默认值。高级调优、故障恢复、实验
灰度和扩展预算归入配对的部署/运维文档；独立工具参数归入 `scripts/README.md`。
默认值与校验由 `backend/app/core/config.py` 拥有，固定协议边界使用具名常量。
新增、修改、删除配置时同步对应文档，只有常用部署表面变化才同步主示例。仍接受但
已无消费者的兼容字段必须明确标记，并移出有效配置指引。测试校验解析、行为及归属
文档，不要求主示例覆盖所有字段。空值带行内注释时必须写成 `KEY="" # 说明`，保证
shell 与 dotenv 读取一致。维护模板时不得重写已有部署的 `.env`。

改动应更新所有其负责表面确实发生变化的权威文档；同一改动可能同时影响产品、部署、运维与开发表面。`product-and-api`、`deployment-and-configuration`、`operations`、`development` 的中英文版本必须同批保持一致。只有快速开始、高层当前边界或导航变化时才更新根 README 对；只有全仓 Agent 工作流/路由变化时才更新 `AGENTS.md`，只有 Claude Code 常驻规则变化时才更新 `CLAUDE.md`。测试应逐一校验相应权威文档，不得反向要求把详细事实复制进入口文件。

Claude Code 会自动加载 `CLAUDE.md`，因此 `scripts/check_claude_md_budget.py` 在 G1 contracts 泳道把它的总字符数与最长行钉成精确 baseline。文件体量发生任何变化，都必须在同一个 PR 里同步更新 baseline，避免积累无人记账的余量。特性级契约应留在其权威文档；只有 Claude Code 专属常驻规则或路由变化时才修改 `CLAUDE.md`。
