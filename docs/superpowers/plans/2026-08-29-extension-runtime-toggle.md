# 部署插件运行时开关（管理员页启停，不重启生效）

日期：2026-08-29。需求六项决定（与用户逐项确认）：

1. **运行时失活闸**：registry 启动冻结契约不破。插件照常随启动装载；「关」是一道全局闸
   （UI 入口隐藏、插件 HTTP 路由拒绝、各贡献点调度跳过）。「开」只对已装载插件有效；
   TOML `enabled = false` / 未点名的插件仍需改配置 + 重启。
2. **DB 为运行态真值**：新表存 per-plugin 开关 + 审计（谁、何时），重启保留。
   生效 = 已装载 ∧ 运行时开；TOML `enabled` 保留「装不装载」原义。
3. **仅 `trust == "deployment"` 插件**可开关；builtin 只读。
4. **秒级收敛**：闸的消费端逐次读进程内快照（零 I/O）；写路径本进程立即发布；
   其它服务进程靠低频轮询线程收敛（默认 3s，Settings 可调）；在途请求跑完不中断。
5. **页面只列已装载插件**：TOML 停用条目不出现（discovery 不为其保留记录）。
6. **依赖复用缺席降级**：关闭总能成功；受影响笔记本走 pluggable-indexing 既有
   「插件缺席」语义（`available=false`、阻新索引写入、可切回内建）。

## 架构决定（研究结论，实施任务不得偷换）

- **闸点在 registry 的两个求值口**，不是只在 `availability()`：
  `retrieval.py:337` 与 `parser_chain.py:326` 直接调 `contribution_availability()`，
  `retrieval.py:748` / `parser_chain.py:573` 调 `capability_availability()`；只闸
  `availability()` 会漏掉检索贡献者与解析链。`availability()` 本身经由这两者，天然继承。
- **被闸返回 `Availability(DISABLED, "admin_disabled")`**。公开 wire 不新增取值：
  `ui_projection` 已把 `DISABLED` 映射为 `"disabled"`。
- **停用集合快照**放 `app/core/extension_admission.py`（新模块，仅标准库）：
  `disabled_plugin_ids() -> frozenset[str]` / `publish_disabled_plugin_ids(...)` /
  `reset_for_tests()`。frozenset 整体替换，消费端零锁零 I/O。默认空集 = 全启用，
  零插件默认部署行为逐字节不变。
- **可见性守恒**：`app.extensions.*` 不 import `app.services`；`app.api` 不 import
  `app.extensions`。holder 放 `app.core` 两边都可达（extensions 已 import
  `app.core.config`；api 亦然）。发布者是 `app.services.extension_toggles` +
  `app.bootstrap`（唯一获准连接 extensions 与 adapters 的组合根）。
- **DB prime 在 `app.bootstrap.create_application_repository`**（构库→迁移完成→读一次
  停用集→发布）。服务端 `startup_warmup.run_startup` 在仓库 ready 后**另起 daemon 轮询
  线程**（间隔 `EXTENSION_ADMISSION_REFRESH_SECONDS`，默认 3s），`close_repository`
  路径停掉。CLI / 批处理凡经 `create_application_repository` 组合即获启动时快照；
  跨进程运行中不再刷新（与 checkup 跨进程背底同精神），在部署文档如实登记。
  轮询失败保留上次快照并告警，绝不让刷新失败打断服务。
- **可用性 probe 零 I/O 教义不破**：闸的消费端只读内存 frozenset；一切 DB I/O 在
  写路径、prime 与轮询线程里。

## 契约（wire / 存储 / 文案）

- 表 `extension_runtime_toggles`（双后端）：`plugin_id TEXT PRIMARY KEY`、
  `enabled`（SQLite INTEGER 0/1，PG boolean）NOT NULL、`updated_by TEXT NOT NULL`
  （管理员 user id）、`updated_at`（SQLite TEXT ISO、PG timestamptz）NOT NULL。
  **无行 = 启用**。行随插件从 TOML 移除而保留（再次装载沿用既往开关，文档登记）。
- 端口（`repositories/ports.py` 新增独立小 Protocol，三个方法；
  `scripts/architecture_boundary_baseline.json` 的 Protocol 计数同 diff 同步）：
  - `extension_runtime_disabled_ids() -> frozenset[str]`
  - `list_extension_runtime_toggles() -> list[dict]`（plugin_id/enabled/updated_by/updated_at）
  - `set_extension_runtime_enabled(plugin_id, enabled, actor_id) -> dict`（upsert，返回行）
- `GET /admin/extensions` 每行新增：`runtime_enabled: bool | None`（builtin 为 None）、
  `runtime_updated_by: str | None`、`runtime_updated_at: str | None`（无行的 deployment
  插件 = `true, None, None`）。路由层用新 deps accessor 读 toggle 行与投影合成；
  admin_projection 本身保持纯注册表函数不变。
- 新端点 `PATCH /api/admin/extensions/{plugin_id}`，body `{"enabled": bool}`：
  仅 admin（非 admin 403，与 GET 同文案风格）；`plugin_id` 不在已装载 deployment
  插件集合 → `user_error(404, "该扩展不存在或不支持运行时开关")`；成功即写行、
  本进程立即 `refresh` 发布，返回 `{plugin_id, runtime_enabled, runtime_updated_by,
  runtime_updated_at}`。
- 被闸插件 HTTP 路由：mount 处按 plugin_id 加 router 级依赖，命中停用 →
  `user_error(403, "该扩展已被管理员停用")`（文案内联字面量，过 ui_vocabulary 与
  中文文案守卫）。
- OpenAPI 契约 fixture（`backend/tests/fixtures/repository_contract/api_contract.json`）
  经 `scripts/generate_repository_contract_fixtures.py` 重生成。

## 任务分解（每项：impl 子代理 → spec-review → code-quality-review）

### T1 存储层（sonnet）
SQLite `_migration_61` + `SCHEMA_VERSION=61`；PG `0039_extension_runtime_toggles.sql`
+ `schema_manifest` 登记；store 对（sqlite/postgres，镜像既有小 store 如
`model_status_store` 的风格）；ports.py 新 Protocol + baseline 计数同步；
`RepositoryRuntime` 挂载（`test_repository_runtime_composition.py` 冻结的属性集合同步）；
`app/api/deps.py` 新 accessor `extension_toggle_repository()`。测试：双后端行为
（缺行=启用、upsert、审计字段、frozenset 只含 enabled=false 行）。

### T2 注册表闸 + 投影（opus）
`app/core/extension_admission.py`；`ExtensionRegistry` 增
`disabled_ids_provider: Callable[[], frozenset[str]] | None`（默认 None=不闸），
freeze 时算出 deployment 插件 id 集与「capability → deployment 插件」归属映射，
`contribution_availability` / `capability_availability` 开头判闸返回
`DISABLED, "admin_disabled"`（builtin 永不受闸；provider 缺省行为逐字节不变）；
新只读方法 `plugin_runtime_disabled(plugin_id) -> bool`；
`frozen_registry` / `build_extension_registry` / `build_extension_runtime` 透传；
`default_extension_runtime` 传 `app.core.extension_admission.disabled_plugin_ids`；
`project_ui_contributions` 行级先判 `plugin_runtime_disabled`（deployment 行命中 →
`available=False, reason="disabled"`，不再评估 capability）。
测试：registry 三口闸行为、builtin 不受影响、UI 投影行级闸、既有零插件快照不变。

### T3 刷新服务 + 生命周期接线（opus）
`app/services/extension_toggles.py`：`refresh_extension_admission(store)`（读→发布→
返回集合）与 `start_extension_admission_refresher(...)->stop`（daemon 线程 + stop
Event，异常保留上次快照并告警）；Settings 新字段
`extension_admission_refresh_seconds`（默认 3.0，ge=1 le=300，
validation_alias=`EXTENSION_ADMISSION_REFRESH_SECONDS`）；
`app.bootstrap.create_application_repository` 组合后 prime；
`startup_warmup.run_startup` 起轮询、close 路径停（严格配对，测试驱动 refresh 函数
而非真实计时）；核实 `open_maintenance_cli_repository` 是否经 bootstrap 组合，
不经则补 prime。

### T4 API 面（sonnet）
`extension_routes` 停用依赖（读 holder，零 I/O）；`mount_extension_routers` 接线；
`admin_routes` GET 合成新字段 + PATCH 端点；`models/admin.py` 字段；
api_contract fixture 重生成。测试：非 admin 403、未知/builtin id 404、happy path、
写后本进程立即生效（同测试进程内 registry 闸随之翻转）、插件路由 403、
`test_extension_plugin_routes.py` / `test_admin_extensions_routes.py` 扩展。

### T5 前端（sonnet）
`admin/extensions/api.ts`：新字段解析（容错）+ `setExtensionRuntimeEnabled` PATCH；
`page.tsx`：新「运行状态」列——builtin 显示「始终启用」，deployment 行给启/停控件，
忙碌态落在控件自身、行内错误文案（不发顶部横幅）、成功后行内展示停用态
（badge + 更新时间/操作者微文案）；页面描述文案改为如实描述两层语义
（装载启动固定；运行开关即时生效、数秒内全线收敛；未点名条目不出现）；
`extensions.css` 配套。测试：`admin-extensions-page.component.test.tsx` 与
`admin-extensions-api.test.mjs` 扩展（含忙碌态、错误落点、builtin 无控件）。

### T6 文档同步（sonnet）
- `docs/product-and-api*.md` 部署插件节：运行时开关契约（端点、字段、403/404 文案、
  无行=启用、收敛语义、依赖降级复用）。
- `docs/deployment-and-configuration*.md`：新环境变量 + DB 真值与 TOML 关系。
- `docs/deployment-extensions-sop*.md`：改「刻意不做热更新/启停一律重启」表述为两层
  语义；「临时停用」「回滚」行补运行时开关路径；第 7 节可见性排查补 admin 闸一因。
- `architecture.md`：extension 运行时 admission 一段。
- `docs/development*.md`：架构边界段补一句（registry 闸 + bootstrap prime + baseline 同步）。
- `docs/ui-vocabulary.md`：核对「停用/启用/始终启用」词条。
- `admin_projection.py` 模块注释「没有 enabled 字段」段落改写（该投影仍无 enabled，
  但运行态在路由层合成——注释如实反映）。

## 验证与合入

各任务：相关聚焦测试。全部完成后：`scripts/check.sh` 全门 + 前端构建/测试
（注意 worktree `frontend/node_modules` 软链规则；本机已知预存失败：packaging dotenv、
Node26 vitest localStorage 5 例——非回归，CI 钉 Node22）。PG 独占用例按本机一次性
测试库流程。提交 → push → PR（codex 评审闭环，逐字贴轮次）→ CI 全绿 + verify →
`gh pr merge --rebase`（用户有长期授权）。
