# 部署插件后端底座实现计划（X1 发现装载 + X4 路由宿主）

状态：执行中（2026-08-23）。基线 origin/master `b608805c`。本文是实现子代理的唯一规格来源；
它们看不到产生本文的对话。

## 主 agent 裁决（对 §5 风险的拍板，优先于下文任何相反表述）

1. **`/admin/extensions` 不带 `enabled` 字段。** 拓扑启动冻结，被停用的插件根本不进 registry，
   一个恒为 `true` 的字段只会误导运维。投影字段恰好 6 个：`id / version / trust / display_name /
   contributions / ui_contributions`。T4 的模型、测试（key 集合断言）与 T7 文档按 6 字段写。
2. **路由上下文再给一个读门。** `PluginRouteContext` 增加 `require_notebook_read`（core 的 notebook
   读权 FastAPI 依赖对象；名字以 `backend/app/api/deps.py` 里实际导出的为准，实现时核对），
   `_validate_plugin_router` 的 `gates` 集合同时收录它——只给写门，插件就写不出一条只读的
   notebook 作用域路由。`PluginRouteContext` 因此是 8 个字段；T5 的结构断言按 8 个写。
3. **界面词表守卫给插件一个自检口。** `scripts/check_ui_vocabulary.py` 增加可重复的
   `--extra-root <dir>`（只扩大扫描面，默认行为一字不变，有一条测试钉「不传时输出逐字不变」），
   部署文档写明插件包必须对自己的源码跑一次。归入 T6。
4. 其余按推荐落：`registry` 拒绝 `isolated`；probe 不做 I/O 只是文档合同 + 既有 fail-closed 消毒；
   `{notebook_id}` 门守卫同时保留正向与反向用例并在 `_dependant_calls` 上注明 FastAPI 版本锁；
   `enabled` 缺省为 `true`。

## 全局约定

- 工作树 `R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/plugin-x1`。
- `PY="${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python}"`；pytest 一律
  `-p no:cacheprovider`；每个任务收尾 `bash scripts/check_backend.sh` 与
  `bash scripts/check_contracts.sh` 必须绿。
- 不做热更新、不引入任何代际/热加载机制（用户拍板）。
- 用 Edit/Write 改文件，不用 shell 重定向整体覆写；不回滚既有改动。

---

## 0. 现状核对结论（以此为准）

| 说法 | 实际 |
|---|---|
| `bootstrap.py` 内建元组 | `backend/app/extensions/bootstrap.py:208` 是 `return build_extension_runtime(`，`:209-219` 是 10 元组，`:221` 起 `capability_decisions={`，`:260` 起 `trusted_report_exporter_plugins=frozenset(` |
| `registry.py:114` trust 校验 | 当前值域 `{"builtin", "isolated"}`——`isolated` 今天被接受；全仓无任何代码/测试构造 `trust="isolated"`，改成 `{"builtin","deployment"}` 是安全的净收紧 |
| 刷新 `api_contract` 的脚本 | **不是** `check_architecture_boundaries.py`。是 `scripts/generate_repository_contract_fixtures.py`（默认模式），产物 `backend/tests/fixtures/repository_contract/api_contract.json`，守卫 `backend/tests/test_repository_api_contract.py::test_openapi_contract_is_byte_semantically_frozen` |
| Python 下限 / TOML | AGENTS.md：Python 3.13+。用 stdlib `tomllib`（`backend/app/services/model_registry.py:11` 同做法），零新依赖 |
| 未配置 MinerU 时 URL 导入 | `backend/app/services/source_ingestion.py:511-517`：`add_url_sources` 在探测之前检查 `mineru_client().configured or mineru_cloud_client().configured`，都没有就抛 `MinerUCloudNotConfigured(...)`；`source_routes.py:310-311` 映射成 `HTTPException(400, detail=str(exc))`——不是 `user_error`。X4 的导入端口保持逐字行为 |
| `facade_surface.json` | `test_facade_surface_fixture.py` 只对账名字集合；在 `source_routes.py` 内抽模块级函数**不需要** `--rebaseline-surface` |
| `extension_sdk` 能否 import fastapi | 守卫只拦 `app.*`，但 SDK 8 个文件零第三方 import 且 docstring 写明 no transport。**维持 SDK 零第三方依赖**：路由用 `Any` + Protocol，`APIRouter` 由 core 挂载时校验 |

硬事实：
- `app.api.*` 既不能 import `app.extensions.*` 也不能 import `app.extension_sdk.*`（`check_architecture_boundaries.py:261-289`）；`app.api.deps` 经 `app.bootstrap` + `app.domain.*` 拿 host——X4 复用这个形态。
- 反向：`app.extensions.*` 绝不能 import `app.api.*`（会成 SCC）。
- `create_app()` 已有两处「部署配置不对就拒绝启动」先例：`_env_file_preflight()`（`main.py:154-173`）与 `validate_mcp_deployment(...)`（`main.py:193`）。`main.py:403` 模块级 `app = create_app()`。发现/校验/挂载失败走这一档（抛异常 → 进程不起）。`deps.repository() → create_application_repository → application_extension_runtime()` 撞同一异常 → `run_startup` 保持 503。
- `default_extension_runtime()` 与 `get_settings()` 都是 `lru_cache`；测试改 `EXTENSIONS_CONFIG` 必须两个都 `cache_clear()`，teardown 再清一次。
- `scripts/check.sh:9-12` 已清 `SILICON_NOTEBOOK_ENV_FILE` / `MODEL_SERVICES_CONFIG` / MinerU；`backend/tests/conftest.py:13` 硬清 `LLM_CACHE_ENABLED`。`EXTENSIONS_CONFIG` 两处都要加。
- `_STABLE_METADATA_ID = ^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`（`registry.py:32-34`）全部 URL path-segment 安全。
- `require_notebook_capability` 带 `@lru_cache`（`deps.py:264-265`）。
- `scripts/check_ui_vocabulary.py:382-397` 扫 `backend/app/**/*.py` 的 `user_error(...)` 文案，有 `MIN_USER_ERROR_SITES` 非空洞下限。

---

## 1. 总览

### 1.1 数据流

```
EXTENSIONS_CONFIG (TOML 路径)
   │  Settings.extensions_config（相对路径锚定仓库根，与 model_services_config 同口径）
   ▼
app/extensions/discovery.py::discover_deployment_extensions(path)
   │  tomllib.load → 顶层键白名单 → 逐条目解析（点名 + enabled）
   │  importlib.import_module + getattr → 结构化 bundle 校验
   │  manifest.id == 配置键 / trust == "deployment" / api_version == "1"
   │  settings_model 校验 TOML [settings] → bundle.configure(instance)
   ▼  tuple[DiscoveredExtension]（按 plugin_id 排序）
app/extensions/bootstrap.py::default_extension_runtime()   ← @lru_cache(1)
   │  bundles = 内建 10 元组 + 发现到的（追加在后）
   │  capability_decisions = capability_decisions_from_bundles(bundles, core_dict)
   ▼
frozen_registry(...)  ← 冻结时 _validate_required_capabilities / _validate_ui_capabilities 看得到插件自带的判定入口
   ▼ ExtensionRuntime(registry=…, plugin_settings={id: 已校验实例})
app/bootstrap.py
   ├─ application_extension_ui_projection(runtime)     （既有）
   ├─ application_extension_admin_projection(runtime)  （新）→ /admin/extensions
   └─ application_plugin_router_specs(runtime)         （新）→ tuple[PluginRouterSpec]
   ▼
app/main.py::create_app()
   ├─ app.state.extension_admin_projection（新）
   └─ mount_extension_routers(app, specs)  ← app/api/extension_routes.py
         factory(PluginRouteContext) → 必须是 APIRouter → 结构校验 →
         include_router(prefix=f"/api/extensions/{plugin_id}", dependencies=[Depends(get_current_user)])
```

### 1.2 新增 / 修改文件

新增：`backend/app/domain/extension_http.py`、`backend/app/extension_sdk/http.py`、
`backend/app/extension_sdk/deployment.py`、`backend/app/extensions/discovery.py`、
`backend/app/extensions/admin_projection.py`、`backend/app/extensions/http_router.py`、
`backend/app/api/extension_routes.py`、`scripts/check_deployment_extension_parity.py`、
`backend/tests/test_extension_discovery.py`、`backend/tests/test_extension_plugin_routes.py`、
`backend/tests/test_admin_extensions_routes.py`。

修改：`backend/app/core/config.py`、`backend/app/extension_sdk/contracts.py`、
`backend/app/extension_sdk/__init__.py`、`backend/app/extensions/registry.py`、
`backend/app/extensions/bootstrap.py`、`backend/app/extensions/__init__.py`、`backend/app/bootstrap.py`、
`backend/app/main.py`、`backend/app/api/source_routes.py`、`backend/app/api/admin_routes.py`、
`backend/app/models/admin.py`、`scripts/check.sh`、`backend/tests/conftest.py`、
`backend/tests/test_phase0_architecture_guard.py`、`backend/tests/fixtures/repository_contract/api_contract.json`、
`scripts/generate_ui_extension_contract.py`、`scripts/check_ui_vocabulary.py`、文档（T7）。

### 1.3 SDK 公开面新增名字（净增 13 + 裁决 2 的 `require_notebook_read` 不是导出名）

| 名字 | 模块 | 必要性 |
|---|---|---|
| `PLUGIN_HTTP_ROUTER_POINT` | `extension_sdk/http.py` | 插件声明路由 contribution 的扩展点名 |
| `PLUGIN_ROUTE_PREFIX` | 同上（转出 domain 常量） | 前缀只有一个真源 |
| `PluginRouteContext` | 同上 | 路由工厂形参类型 |
| `PluginRouterFactory` | 同上 | 工厂 Protocol |
| `PluginActor` | 同上 | 当前用户的唯一只读视图（`id` + `is_admin`） |
| `PluginUrlSourceImportPort` | 同上 | 按 URL 导入端口 Protocol |
| `PluginUrlImportResult` / `PluginImportedSource` / `PluginRejectedUrl` | 同上 | 端口返回值（插件不能 import `app.models.sources`） |
| `DeploymentExtensionBundle` | `extension_sdk/deployment.py` | `settings_model` + `configure(instance)` 合同 |
| `CapabilityProvidingBundle` | 同上 | `capability_decisions` 合同 |
| `AvailabilityProbe` | `contracts.py`（已存在未导出） | probe 类型标注 |

`ExtensionManifest` 末尾新增 `provides: tuple[str, ...] = ()`。

### 1.4 决策点

| 决策点 | 选择 | 理由 |
|---|---|---|
| settings 放哪 | bundle 上 `settings_model`（pydantic 模型类）+ `configure(instance)`，不放 manifest | manifest 是冻结元数据并进投影 |
| 未知键 | core 自己算 accepted 集合（`model_fields` 名 + `validation_alias`/`alias`），差集非空即失败，不依赖插件 `extra="forbid"` | fail-closed 不建立在插件自觉上 |
| settings 何时交给插件 | `register()` 之前、发现阶段内 `configure(instance)` | 配置错永远早于拓扑错 |
| 秘密不外泄 | 所有失败 `raise ExtensionDiscoveryError(...) from None`，永不 `str(exc)`；只带 plugin_id + reason code + 异常类名 +（settings 场景）键名 | pydantic `ValidationError` 回显输入值 |
| capability 并入 | bundle 上 `capability_decisions: Mapping[str, AvailabilityProbe]`，在 bootstrap 合并，registry 不动 | registry 的回滚语义不该再塞状态 |
| 冲突 | 与 core 同名 `plugin_capability_conflicts_core`；与另一插件同名 `plugin_capability_conflicts_plugin`；均启动失败 | |
| 路由声明 | 复用 contribution 机制：`point="http.plugin_router"`, `kind=CONTRIBUTOR`, `implementation=路由工厂` | 零 registry 改动，自动得到 id 唯一性与投影 |
| 谁能声明路由 | 只有 `trust == "deployment"`；builtin 声明 → 启动失败 | core 端点必须留在 `app/api/*_routes.py` 受 `api_contract` 管 |
| 失败落在哪 | `create_app()` 抛异常 → 进程不启动 | 路由拓扑必须建 app 时定死 |
| `api_contract` | `/api/admin/extensions` 必须重生成；`/api/extensions/...` 不登记（默认运行时零插件 router，`mount_extension_routers` 空 specs 直接 return） | |
| UI 契约 fixture | 只钉内建，`generate_ui_extension_contract.py` 不变 | CI 无仓库外插件 |
| 仓库外插件对等 | `scripts/check_deployment_extension_parity.py`，部署 SOP 里跑，不进 CI | |

---

## 2. 任务拆分

### T1 — `EXTENSIONS_CONFIG` 配置位与验证环境隔离（sonnet）

1. `backend/app/core/config.py`：紧跟 `model_services_config`（`:118`）之后：
   ```python
   # Deployment-owned out-of-repo plugin manifest (TOML). Empty = no deployment
   # plugins; the frozen topology is then byte-identical to the built-in tuple. A
   # non-empty path that cannot be read or parsed is a startup failure, never a
   # silent downgrade — same posture as MODEL_SERVICES_CONFIG. Relative paths are
   # anchored to the repo root below.
   extensions_config: str = Field("", validation_alias="EXTENSIONS_CONFIG")
   ```
   在 `_anchor_relative_paths_to_repo_root`（`:1164`）里紧跟 `model_services_config` 两行（`:1178-1179`）之后加同形状三行。不加 `field_validator`。
2. `scripts/check.sh`：`:10` 的 `export MODEL_SERVICES_CONFIG=""` 之后加 `export EXTENSIONS_CONFIG=""   # 验证绝不装入部署插件（否则会漏进冻结的 app.openapi()）`。
3. `backend/tests/conftest.py`：`:13` 之后加硬赋值 `os.environ["EXTENSIONS_CONFIG"] = ""`（注释说明理由）。

测试（新建 `backend/tests/test_extension_discovery.py`）：
- `test_extensions_config_defaults_to_empty_and_anchors_relative_paths`：`Settings(_env_file=None).extensions_config == ""`；相对路径以仓库根开头且 `is_absolute()`；绝对路径原样。
- `test_verification_entrypoints_clear_extensions_config`：读 `scripts/check.sh` 含 `export EXTENSIONS_CONFIG=""`；读 `backend/tests/conftest.py` 含 `os.environ["EXTENSIONS_CONFIG"] = ""`。

变异验证：删 `check.sh` 那行 → 第二条红；删锚定三行 → 第一条相对路径断言红；把三行搬进 `mineru_enabled` property（永不执行）→ 红。

### T2 — SDK / domain 合同面（sonnet）

1. `backend/app/extension_sdk/contracts.py`：`:144` `trust: Literal["builtin", "deployment", "isolated"]`（注释：随版本构建 / 部署装入的仓库外可信包 / 进程隔离，永不进同进程 registry）；`ExtensionManifest` 末尾追加 `provides: tuple[str, ...] = ()`（带注释）。
2. `backend/app/domain/extension_http.py`（新，零 `app.*`、零第三方 import）：
   ```python
   PLUGIN_ROUTE_PREFIX = "/api/extensions"
   @dataclass(frozen=True, slots=True) class PluginActor: id: str; is_admin: bool
   @dataclass(frozen=True, slots=True) class PluginImportedSource: source_id: str; title: str; url: str
   @dataclass(frozen=True, slots=True) class PluginRejectedUrl: url: str; reason: str
   @dataclass(frozen=True, slots=True) class PluginUrlImportResult: created: tuple[...]; rejected: tuple[...]
   class PluginUrlSourceImportPort(Protocol):
       def import_urls(self, notebook_id: str, urls: Sequence[str]) -> PluginUrlImportResult: ...
   @dataclass(frozen=True, slots=True)
   class PluginRouteContext:
       plugin_id: str
       settings: Any                                   # validated model instance, or None
       require_notebook_capability: Callable[[str], Any]   # -> FastAPI dependency (write gates)
       require_notebook_read: Any                      # FastAPI dependency: notebook read gate（裁决 2）
       current_actor: Callable[..., Any]               # FastAPI dependency -> PluginActor
       user_error: Callable[[int, str], Exception]
       url_sources: PluginUrlSourceImportPort
       emit_event: Callable[[Mapping[str, object]], None]
   class PluginRouterFactory(Protocol):
       def __call__(self, context: PluginRouteContext) -> Any: ...
   @dataclass(frozen=True, slots=True)
   class PluginRouterSpec: plugin_id: str; contribution_id: str; factory: PluginRouterFactory; settings: Any
   ```
   docstring 说明为何在 domain（`app.api` 不能 import extensions/SDK，组合根不能 import `app.api`）。
3. `backend/app/extension_sdk/http.py`（新）：转出上面全部名字 + `PLUGIN_HTTP_ROUTER_POINT = "http.plugin_router"`。
4. `backend/app/extension_sdk/deployment.py`（新）：`DeploymentExtensionBundle`（`manifest`、`settings_model: Any`、`configure(settings)`、`register`）与 `CapabilityProvidingBundle`（`manifest`、`capability_decisions: Mapping[str, AvailabilityProbe]`、`register`）两个文档性 Protocol。
5. `backend/app/extension_sdk/__init__.py`：加 §1.3 名字（含 `AvailabilityProbe`），同步 `__all__`。
6. `backend/app/extensions/registry.py`：`:114` 改 `{"builtin", "deployment"}`（注释 `isolated` 拒绝进同进程 registry）；`register()` 在 `ui_contributions` 校验之后加 `provides` 形状校验（tuple、每项匹配 `_STABLE_METADATA_ID`、无重复；否则 `ExtensionRegistryError(f"extension {manifest.id!r} declares invalid provided capabilities")`）。

测试（追加 `backend/tests/test_extension_registry.py`）：
- `test_registry_rejects_isolated_trust_and_accepts_deployment`
- `test_registry_rejects_malformed_provided_capability_names`（参数化 list / `"Bad Name"` / 重复）
- `test_default_manifest_provides_is_empty`

`test_extension_ui_projection.py::test_default_backend_ui_topology_matches_cross_stack_contract` 必须仍绿。守卫：跑 `scripts/check_architecture_boundaries.py` 默认模式。

变异验证：`registry.py:114` 改回 `{"builtin","isolated"}` → 红；删 `provides` 校验 → 红；把校验搬进 `manifests()` 读方法 → 红。

### T3 — 发现、装载、settings 校验、capability 合并（opus）

新增 `backend/app/extensions/discovery.py`：

```python
_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TOP_LEVEL_KEYS = frozenset({"extensions"})
_ENTRY_KEYS = frozenset({"bundle", "enabled", "settings"})

class ExtensionDiscoveryError(RuntimeError):
    def __init__(self, plugin_id: str, reason: str, *, exception_type: str = "", names: tuple[str, ...] = ()) -> None: ...
    # message: "extension {id!r} rejected: {reason} keys=[...] (ExcType)"；plugin_id 为空时 "extension configuration rejected: ..."

@dataclass(frozen=True, slots=True)
class DiscoveredExtension: plugin_id: str; bundle: object; settings: object | None

def discover_deployment_extensions(config_path: str) -> tuple[DiscoveredExtension, ...]
def capability_decisions_from_bundles(bundles: Sequence[object], core_decisions: Mapping[str, CapabilityDecision]) -> dict[str, CapabilityDecision]
```

`discover_deployment_extensions` 顺序即失败优先级：
1. `config_path.strip()` 为空 → `()`。
2. `open("rb")` + `tomllib.load`；`OSError` → `config_unreadable`（`exception_type`）；`TOMLDecodeError` → `config_invalid_toml`。
3. 顶层键 ⊄ `{"extensions"}` → `config_unknown_top_level_key`（`names`）。
4. `extensions` 非 dict → `config_extensions_not_a_table`。
5. 遍历 `sorted(entries)`：键不匹配 `_STABLE_ID` → `plugin_id_invalid`；value 非 dict → `plugin_entry_not_a_table`；未知键 → `plugin_unknown_key`（names）；`enabled` 缺省 True、非 bool → `plugin_enabled_not_bool`、False → continue（不 import）；`bundle` 缺 → `plugin_bundle_missing`；形状（非空 str、恰一个 `:`、两侧非空、右侧合法标识符）不对 → `plugin_bundle_spec_invalid`；`import_module` 任何异常 → `plugin_module_import_failed`（异常类名，`from None`）；`getattr` 失败 → `plugin_attribute_missing`；`type(manifest) is not ExtensionManifest or not callable(register)` → `plugin_not_a_bundle`；`manifest.id != key` → `plugin_id_mismatch`；`trust != "deployment"` → `plugin_trust_not_deployment`；`api_version != EXTENSION_API_VERSION` → `plugin_api_version_unsupported`；settings 校验（下）。
6. 返回 tuple。

`_bind_settings(plugin_id, bundle, table)`：
- 无 `settings_model` 且无 `configure`：table 非空 → `plugin_settings_not_accepted`；否则 None。
- 只有其一 / `configure` 不可调用 → `plugin_settings_binding_missing`。
- model 不是 `BaseModel` 子类 → `plugin_settings_model_invalid`。
- accepted = 字段名 ∪ `validation_alias`/`alias`（str）；未知键 → `plugin_settings_unknown_key`（names）。
- `model.model_validate(dict(table))` 异常 → `plugin_settings_invalid`（异常类名，`from None`）。
- `configure(instance)` 异常 → `plugin_settings_binding_failed`。

`capability_decisions_from_bundles`：对全部 bundle（内建 + 发现）：`provides` 非空时必须有 `capability_decisions` Mapping（否则 `plugin_capability_declaration_invalid`）；名字匹配 `_STABLE_ID`（`plugin_capability_name_invalid`）；mapping 里多出的 key → `plugin_capability_not_declared`；`provides` 里缺 probe → `plugin_capability_missing_decision`；与 core 同名 → `plugin_capability_conflicts_core`；与另一插件同名 → `plugin_capability_conflicts_plugin`；probe 必须 callable。返回 `dict(core_decisions)` + 合并项。

改 `backend/app/extensions/bootstrap.py`：
- `ExtensionRuntime` 末尾加 `plugin_settings: Mapping[str, object] = MappingProxyType({})`。
- `build_extension_runtime(..., plugin_settings=None)`，构造时 `MappingProxyType(dict(plugin_settings or {}))`。
- `default_extension_runtime()`：函数内惰性 `from app.core.config import get_settings`；`builtin_bundles` 元组、`core_decisions` 字典保持原内容；`discovered = discover_deployment_extensions(get_settings().extensions_config)`；`bundles = builtin_bundles + tuple(i.bundle for i in discovered)`；`capability_decisions=capability_decisions_from_bundles(bundles, core_decisions)`；`plugin_settings={i.plugin_id: i.settings for i in discovered}`。失败时 `logger = logging.getLogger("silicon_notebook.extensions")` 记 `extension discovery FAILED — service will not start: plugin=%s reason=%s exc=%s`，然后 re-raise。**日志不得出现 `exc.names`、`str(exc)`、模块路径、文件路径。**
- `backend/app/extensions/__init__.py` 转出 `ExtensionDiscoveryError`、`DiscoveredExtension`、`discover_deployment_extensions`、`capability_decisions_from_bundles`。

测试：`backend/tests/test_extension_discovery.py` 扩到 §3.2 全量。守卫：`check_architecture_boundaries.py` 默认模式 0 SCC；`generate_ui_extension_contract.py --check` 仍绿。

### T4 — `GET /admin/extensions`（sonnet；按裁决 1 无 `enabled`）

1. `backend/app/extensions/admin_projection.py`（新，与 `ui_projection.py` 同构）：三个 frozen dataclass `AdminExtensionContributionProjection{id,point,kind}`、`AdminExtensionUiProjection{id,slot,capability}`、`LoadedExtensionProjection{id,version,trust,display_name,contributions,ui_contributions}`；`project_loaded_extensions(registry)` 按 `manifest.id` 排序，两个列表各按声明 id 排序。docstring：白名单，绝不含模块路径/文件路径/settings 值/reason/异常文本；被停用的插件不进 registry，因此不出现。
2. `backend/app/bootstrap.py`：`application_extension_admin_projection(runtime) -> Callable[[], tuple[LoadedExtensionProjection, ...]]`（惰性 lambda，与 ui projection 同形）。
3. `backend/app/main.py`：`:260-262` 之后 `app.state.extension_admin_projection = application_extension_admin_projection(extension_runtime)`；import 同步。
4. `backend/app/models/admin.py`：`AdminExtensionContribution{id,point,kind}`、`AdminExtensionUiContribution{id,slot,capability}`、`AdminExtension{id,version,trust:Literal["builtin","deployment"],display_name,contributions,ui_contributions}`、`AdminExtensionsResponse{api_version:Literal["1"]="1", extensions:list[AdminExtension]}`，全部 `extra="forbid"`。
5. `backend/app/api/admin_routes.py` 末尾：`GET /admin/extensions`，`user.role != "admin"` → `user_error(403, "仅管理员可查看已加载的扩展")`；读 `request.app.state.extension_admin_projection`。跑 `scripts/check_ui_vocabulary.py` 确认文案合规。

测试：`backend/tests/test_admin_extensions_routes.py`（§3.4，key 集合按 6 字段）。
baseline：`PYTHONPATH=backend "$PY" scripts/generate_repository_contract_fixtures.py`，`git diff --stat` 只应有 `api_contract.json`；若 `facade_surface.json` 或 `repository_v9/baseline.db` 也变了，`git checkout --` 撤回那些。`test_route_domain_boundaries.py` 无需改。

变异验证：删 admin 判定 → 红；投影多加一个 `module_path` 字段 → 白名单测试红；admin 判定搬进 `project_loaded_extensions`（拿不到 user）→ 红。

### T5 — 插件 HTTP 路由宿主（opus；按裁决 2 含读门）

1. `backend/app/api/source_routes.py`：在 `:292` 装饰器之前抽模块级 `import_url_sources(notebook_id: str, urls: list[str]) -> AddUrlSourcesResult`（docstring 写明容量逐条扣减、admin 豁免、未配置解析服务映射 400 且 detail 是服务层原文而非 `user_error`）；端点体改为一跳委托。**端点函数名 `add_url_sources`、路径、`response_model`、`dependencies` 不动**。
2. `backend/app/extensions/http_router.py`（新）：`collect_plugin_router_specs(registry, plugin_settings) -> tuple[PluginRouterSpec, ...]`；遍历 `registry.contributions(PLUGIN_HTTP_ROUTER_POINT)`；kind 非 CONTRIBUTOR → `plugin_router_kind_invalid`；manifest.trust != deployment → `plugin_router_trust_denied`；同 plugin 第二次 → `plugin_router_multiple`；implementation 不可调用 → `plugin_router_factory_invalid`（均 `ExtensionDiscoveryError`）。
3. `backend/app/bootstrap.py`：`application_plugin_router_specs(runtime)`。
4. `backend/app/api/extension_routes.py`（新；只 import `app.domain.extension_http`、`app.api.deps`、`app.api.source_routes`、`app.core.config`、`app.core.event_logging`、fastapi）：
   - `class PluginRouteMountError(RuntimeError)`（message = `"{plugin_id}: {reason_code}"`）。
   - `plugin_actor(user=Depends(get_current_user)) -> PluginActor(id=user.id, is_admin=user.role == "admin")`。
   - `_UrlSourceImportAdapter.import_urls` → `source_routes.import_url_sources` → 映射成 `PluginUrlImportResult`（字段名按 `AddUrlSourcesResult` 实际结构核对）。
   - `_event_emitter(plugin_id, event_log)`：白名单 `{"event","outcome","count","elapsed_ms"}`；`event`/`outcome` 匹配 `^[a-z][a-z0-9_]{0,63}$`；`count`/`elapsed_ms` 为 0..1e9 的 int；越界整条丢弃、绝不抛回插件；写 `{"kind":"extension_plugin","plugin_id":...}`。`EventLogger` 的构造签名按 `app/core/event_logging.py` 实际核对。
   - `_dependant_calls(dependant)` 递归收集 `dependencies[].call`（注释点名 FastAPI 版本锁 `requirements.txt:1`）。
   - `_validate_plugin_router(plugin_id, router)`：`on_startup/on_shutdown` 非空 → `plugin_route_lifecycle_denied`；非 `APIRoute` → `plugin_route_unsupported_kind`；路径含 `{notebook_id}` 且依赖闭包与 `gates` 无交集 → `plugin_route_missing_notebook_gate`。`gates = {require_notebook_write, require_notebook_admin, <deps 里的 notebook 读门>}`。
   - `mount_extension_routers(app, specs)`：空 specs 直接 return；否则为每个 spec 构造 `PluginRouteContext`（8 字段：`plugin_id, settings, require_notebook_capability, require_notebook_read, current_actor=plugin_actor, user_error, url_sources, emit_event`），`router = spec.factory(context)`，非 `APIRouter` → `plugin_router_not_a_router`，校验后 `app.include_router(router, prefix=f"{PLUGIN_ROUTE_PREFIX}/{spec.plugin_id}", dependencies=[Depends(get_current_user)])`。
5. `backend/app/main.py`：在 `app.include_router(knowhow_agent_router, prefix="/api")`（`:392`）之后、`app.mount("/mcp", ...)` 之前：`mount_extension_routers(app, application_plugin_router_specs(extension_runtime))`（注释：唯一挂载点、零插件零路由、router 级 `Depends(get_current_user)`）。
6. `backend/tests/test_phase0_architecture_guard.py::test_registry_composition_does_not_change_route_topology`（`:701`）：dummy runtime 升级为带 `contributions(point)->()`、`manifests()->()` 的 `_EmptyRegistry` + `plugin_settings={}`；docstring 补「本条断言空插件拓扑不改路由，非空由 `test_extension_plugin_routes.py` 覆盖」。

测试：`backend/tests/test_extension_plugin_routes.py`（§3.3，结构断言按 8 字段）。
守卫/baseline：`api_contract.json` 不需重生成（跑 `test_repository_api_contract.py` 证明）；`facade_surface.json` 不需 rebaseline；`check_architecture_boundaries.py` 绿（重点：`app.api.extension_routes` 无 `app.extensions*`/`app.extension_sdk*` 边）；`scripts/audit_facade_callers.py` 的 `add_url_sources` 仍有 caller。

变异验证（三组，各做违规/删除/移动）：(a) 门守卫：去掉假插件路由的 `dependencies=` 应抛；删守卫分支 → 红；把分支搬到 `if not specs: return` 之前 → 红。(b) 匿名面：`include_router` 去掉 `dependencies=` → 无 token 200 → 红。(c) 事件白名单：直接 `emit(dict(payload))` → 红；删差集检查 → 红；搬进 `except` 之后 → 红。

### T6 — 部署对等脚本 + 词表守卫 `--extra-root`（sonnet）

1. `scripts/generate_ui_extension_contract.py`：把私有比较器提成 `contract_rows_match(committed, live) -> bool` 与 `contract_diff(committed, live) -> list[str]`（不改行为，docstring 说明比较器只有一份）。
2. `scripts/check_deployment_extension_parity.py`（新，只读）：
   ```
   EXTENSIONS_CONFIG=/etc/silicon/extensions.toml PYTHONPATH=<repo>/backend \
     python3 scripts/check_deployment_extension_parity.py --frontend-contract <path.json>
   输入：{"api_version":"1","contributions":[{"plugin_id","version","contribution_id","slot","capability"}...]}
   行为：用当前进程的 EXTENSIONS_CONFIG 构建 default_extension_runtime()，取 ui_contribution_contract(registry)，按行集合比较。永不写文件、不构造 repository、不发网络。
   退出码：0 对等；1 漂移（逐行 diff 到 stderr，只含五个字段）；2 用法/环境错误（参数缺失、文件不存在/非法 JSON、api_version 不是 "1"、后端发现/冻结失败——只打印 plugin id + reason code）。
   ```
3. `scripts/check_ui_vocabulary.py`：新增可重复 `--extra-root <dir>`，只把该目录下 `**/*.py` 的 `user_error(...)` 文案加进扫描面，默认行为一字不变；`MIN_USER_ERROR_SITES` 只对默认根计数。

测试（追加 `backend/tests/test_extension_discovery.py`）：
- `test_deployment_parity_script_exits_zero_on_match`（`importlib.util` 加载脚本，照 `test_phase0_architecture_guard.py:13-17`）
- `test_deployment_parity_script_reports_drift_and_never_writes`
- `test_deployment_parity_script_usage_errors_exit_two`（不存在路径 / 非法 JSON / `api_version="2"`）
- `test_ui_vocabulary_extra_root_scans_plugin_sources_and_default_is_unchanged`：tmp 目录放一个含黑名单词的 `user_error(...)` 文件，`--extra-root` 时报红、不传时输出与之前逐字相同。

`check_contracts.sh` **不加**对等脚本。

变异验证：退出码 1 改 0 → 红；删 `api_version` 检查 → 红；`--frontend-contract` 读取搬到 `contract_diff` 之后 → 红；`--extra-root` 分支删掉 → 词表测试红。

### T7 — 文档（sonnet）

1. `docs/product-and-api.md` / `_zh.md`：`## APIs`（`product-and-api.md:2141` 附近）清单追加两条；`## Admin observability`（`:2205` 附近）之前插 `### Deployment extensions`（≤12 行）：`GET /api/admin/extensions`（仅管理员；6 个白名单字段；绝不返回模块路径/文件路径/settings 值/reason/异常文本；被停用的插件不出现）；`/api/extensions/{plugin_id}/…`（唯一挂载面、router 级会话认证无匿名面、notebook 作用域路由必须挂 core 的读门或能力门；上下文 8 个字段；拿不到 repository/全局 settings/model client/FastMCP/原始 token）；数值上限：事件字段白名单 4 键、`count`/`elapsed_ms` ≤ 1e9、稳定码 ≤64 字符、每插件至多 1 个路由贡献。指针句：「新增插件 SOP 见后续部署文档」。
2. `docs/deployment-and-configuration.md` / `_zh.md`：`### System model services…` 之后新起 `### Deployment extensions (EXTENSIONS_CONFIG)`（≤15 行）：未设=无插件且拓扑逐字一致；设置但不可读/不可解析=启动失败；完整 TOML 示例 + 三条铁律（只加载点名且未 `enabled=false` 的；绝不扫描/entry points/其他环境变量；插件包装进同一 `PYTHON_BIN` 环境）；启停/升级一律重启；自查命令与退出码；`generate_ui_extension_contract.py` 重生成时 `EXTENSIONS_CONFIG` 必须为空；插件包须对自己源码跑 `check_ui_vocabulary.py --extra-root`。
3. `README.md` / `README_zh.md`：**不往第 9 行那段堆**，其后另起一段（3–4 句）：第三档信任级别；`EXTENSIONS_CONFIG` 显式点名、不扫描不自动启用；任何发现/能力/settings/挂载拒绝即停止进程；启动冻结无热更新；插件路由只挂 `/api/extensions/{plugin_id}` 且经会话认证与 notebook 门。
4. `AGENTS.md`：`:259` 那条之后新起一条 bullet（≤4 句，红线口吻）：只从 `EXTENSIONS_CONFIG` 点名列表装载、绝不扫描/entry points/第二个环境开关；`trust` 恰三值、只有 `builtin`/`deployment` 进同进程 registry；bundle 只能为 `provides` 的 capability 供 probe，与 core 同名即启动失败；插件路由只挂 `/api/extensions/{plugin_id}`、router 级会话认证；插件拿不到 repository/全局 settings/model client/FastMCP/原始 token；拒绝日志只含 plugin id + reason code。
5. `CLAUDE.md`：`:80` 那条之后新起同内容中文 bullet（≤4 句）；顺手把 `:79`「新端点必须跑默认模式刷 `api_contract`」补上脚本名 `scripts/generate_repository_contract_fixtures.py`。

测试：`backend/tests/test_architecture_documentation.py` 加 `test_deployment_extension_boundary_is_in_all_agent_entry_documents`（四份入口文档 casefold+归一后含 `extensions config`、`deployment`、`trust`、`restart`/`重启`、`api extensions`；照 `:731` 那条形状）。

变异验证：删 AGENTS.md 新 bullet → 红；删 README_zh 新段 → 红；把 bullet 搬到 `docs/development.md` → 红。

---

## 3. 测试设计

### 3.1 共用 fixture（`test_extension_discovery.py` 顶部，路由测试复用）

```python
@pytest.fixture
def frozen_runtime_reset():
    """EXTENSIONS_CONFIG 一动，get_settings / default_extension_runtime / deps.repository 三个 lru_cache 都清——进入与退出各一次。"""

def _write_plugin_package(tmp_path, *, plugin_id="corp.sample", body=...) -> Path:
    """写真实 .py 文件 + sys.path.insert(0, ...)；退出时移除 sys.path 项、清 sys.modules、importlib.invalidate_caches()。"""
```
所有假插件都走真 import。

### 3.2 X1 用例（`test_extension_discovery.py`）

`test_unset_config_yields_the_exact_builtin_topology`（硬编码 10 个 id + 5 个扩展点的声明 id 列表逐字相等）、
`test_named_and_enabled_plugin_is_discovered_and_frozen`、`test_disabled_plugin_is_not_loaded_and_not_imported`（`sys.modules` 里无该模块）、
`test_discovered_plugins_are_appended_after_builtins_and_sorted_by_id`、`test_missing_config_file_fails_startup`（`config_unreadable`）、
`test_invalid_toml_fails_startup`、`test_unknown_top_level_table_fails_startup`、`test_bundle_attribute_missing_fails_startup`、
`test_object_that_is_not_a_bundle_fails_startup`、`test_manifest_id_must_equal_the_config_key`、`test_trust_must_be_deployment`、
`test_api_version_mismatch_fails_startup`、
`test_settings_unknown_key_fails_and_error_never_contains_the_value`（键名可见、`"TOPSECRET" not in str(exc)`、`exc.__cause__ is None`）、
`test_settings_type_error_fails_and_error_never_contains_the_value`、
`test_secret_settings_never_reach_logs_or_projections`（`SecretStr`；caplog / admin 投影 repr / ui contract JSON 均无明文）、
`test_settings_table_without_a_settings_model_fails_closed`、`test_validated_settings_reach_the_bundle_before_register`（`calls == ["configure","register"]`）、
`test_provided_capability_enters_the_catalog_and_unblocks_requires_and_ui`（核心解锁证明）、
`test_provided_capability_is_live_on_system_extensions`（TestClient；probe 翻转响应跟着变；无内部 reason 串）、
`test_probe_not_listed_in_provides_fails_startup`、`test_provides_without_a_probe_fails_startup`、
`test_capability_name_colliding_with_core_fails_startup`（`ui.agent_profile.available`）、`test_capability_name_colliding_with_another_plugin_fails_startup`、
`test_probe_exception_is_sanitized_not_propagated`、`test_discovery_failure_log_carries_only_id_reason_and_exception_type`、
`test_create_app_refuses_to_start_on_discovery_failure`（抛 `ExtensionDiscoveryError`，readiness 未翻 ready）。

### 3.3 X4 用例（`test_extension_plugin_routes.py`）

假插件工厂返回：`GET /ping`（`Depends(context.current_actor)`）、`POST /notebooks/{notebook_id}/import`（`dependencies=[Depends(context.require_notebook_capability("sources:write"))]`，调 `context.url_sources.import_urls`）、`GET /notebooks/{notebook_id}/peek`（只挂 `context.require_notebook_read`，裁决 2 的正向用例）、`GET /boom`（`raise context.user_error(409, "这项操作暂时无法完成")`）。

用例：`test_no_plugins_registers_zero_routes_and_keeps_openapi_frozen`、`test_plugin_router_is_mounted_under_its_plugin_id`、
`test_plugin_routes_have_no_anonymous_surface`、`test_plugin_route_notebook_gate_is_the_core_guard`（非 owner 404、owner 200）、
`test_plugin_route_with_only_the_read_gate_mounts`（裁决 2）、`test_router_missing_the_notebook_gate_fails_to_mount`、
`test_user_error_header_is_visible_on_plugin_routes`（409 + `X-User-Message: 1`）、`test_plugin_actor_is_narrow`（字段集合 `{"id","is_admin"}`）、
`test_url_import_reuses_core_capacity_and_scheduler_semantics`、`test_url_import_maps_unconfigured_parser_to_400`（无 `X-User-Message`）、
`test_plugin_cannot_reach_repository_or_settings_through_the_context`（8 字段名集合；值不是 repository/Settings）、
`test_plugin_router_lifecycle_hooks_are_rejected`、`test_non_apiroute_in_plugin_router_is_rejected`、`test_factory_returning_a_non_router_is_rejected`、
`test_two_router_contributions_from_one_plugin_are_rejected`、`test_builtin_trust_may_not_contribute_an_http_router`、
`test_plugin_routes_are_503_before_readiness`、`test_plugin_event_sink_drops_out_of_whitelist_payloads`。

### 3.4 `/admin/extensions` 用例（`test_admin_extensions_routes.py`）

`test_admin_extensions_requires_authentication`（401）、`test_admin_extensions_rejects_non_admin`（403 + `X-User-Message`）、
`test_admin_extensions_lists_builtin_topology_for_admin`（10 条、`trust` 全 `builtin`）、
`test_admin_extensions_response_is_a_closed_field_whitelist`（顶层 `{"api_version","extensions"}`；每条 6 个 key；contribution `{"id","point","kind"}`；ui `{"id","slot","capability"}`）、
`test_admin_extensions_never_leaks_module_paths_or_settings`、`test_system_extensions_response_is_unchanged`。

---

## 4. 顺序

T1、T2 可合并为一个实现任务（文件互不相交）→ T3 → T4 → T5 → T6 → T7。每任务后两路评审（规格 + 质量，含变异验证）。

## 5. 已接受的风险

1. 插件的 `user_error` 文案绕过公网词表守卫：以 `--extra-root` 自检 + 文档要求兜底（裁决 3）。
2. probe 不做 I/O 只能是文档合同 + 既有消毒。
3. `{notebook_id}` 门守卫依赖 FastAPI 半公开的 `route.dependant`（版本锁 `requirements.txt:1`），正反两条用例同时保留。
4. `enabled` 缺省 `true`（点名即启用）。
