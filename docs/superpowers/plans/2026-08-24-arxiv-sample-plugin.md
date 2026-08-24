# 实现计划：arXiv 样板部署插件（X9 PR-B）

状态：待执行（2026-08-24）。基线 master `fd2479ce`（X9 PR-A `ask.gap_consult` 已合入）。
本文是实现子代理的唯一规格来源。
方案页（用户已批准全部六项裁决）：arXiv 样板插件——两个入口（人工检索导入 / Agent 缺口外扩），
公网仓库 `examples/extensions/arxiv-search/`，**出厂关闭**，同时充当 SOP 的可运行范例与
「干净 checkout + 配置 = 生效」的机器证明。

## 主 agent 裁决（优先于下文任何相反表述）

1. **落点** `examples/extensions/arxiv-search/`：backend 包（`src/` 布局，可 `pip install -e`）
   + `ui/arxiv-search/` UI 包 + `extensions.example.toml` + README 对。
2. **出厂关闭**：不进任何默认部署、不进默认 `npm run test` 的前端树。启用 = 部署方在自己的
   `EXTENSIONS_CONFIG` 里点名 + `SILICON_NOTEBOOK_UI_PLUGINS` 指向 `ui/arxiv-search`。
3. **零第三方依赖**：arXiv 客户端只用 stdlib `urllib` + `xml.etree` 解析 Atom；`pyproject.toml`
   对本仓库**零依赖**（SDK 从后端环境 import）。
4. **礼貌性限速 3 秒**是 arXiv 官方要求：进程内串行 + 最小间隔，**绝不 sleep 越过调用方预算**
   （见 §1.4）。
5. **入口一的导入走插件自己的 `/import` 路由 → core `url_sources` 端口**（方案 D5）；
   **入口二回答卡里的导入按钮是 core UI、走 core URL 端点**，不经插件（PR-A 已落地，本 PR 不碰）。
6. **`consult_enabled` 默认 false**：装了插件 ≠ 同意把问题派生词发给 arXiv。
7. **⚠ 与方案页的一处技术性偏离（必须照本文）**：方案页写「manifest `provides` 一个 capability
   （如 `consult_enabled`）」来门控外扩。**不能这么做**——`provides` 不是开关，它是
   `capability_decisions` 必须逐个配对探针的名字表（`contracts.py:165-168`），一个 capability
   表达不了「router/UI 走一个门、gap-consult 走另一个门」。**也不该改用** `manifest.requires`
   ——那是 `registry.availability(contribution_id, ...)` 这一个访问入口才会读取的字段
   （`registry.py:480`），而 HTTP 路由挂载压根不经过这个入口（启动时按已注册 contribution 集合
   无条件挂载），侧栏入口也只走自己声明的 `capability`（`registry.capability_availability()`），
   两者都读不到 `manifest.requires`。把 `consult_enabled` 放进 `requires` **不会**连累路由或
   侧栏，与本文早前版本的判断相反。真正的理由是精确性（`requires` 是 manifest 级、会被这份
   manifest 将来任何经 `registry.availability()` 消费的 contribution 共享，放单一特性开关进去
   等于让日后新增的 contribution 平白继承这个门）与语义（`requires` 表达插件实例整体的前置
   条件，不是某个功能自己的开关）。改用**逐 contribution 的 `ExtensionContribution.availability`
   探针**（`contracts.py:179` → `registry.py:494-520`）门控外扩，`manifest.provides` 那一个
   capability 只门控 UI 入口。详见 §1.5。
8. **G2 e2e 用 `pytest.mark.slow`**，同批把 `backend/pytest.ini` 的 `slow` 描述从「real HNSW/ANN」
   放宽一句。**不新增 marker**：新 marker 要同时改两个 shell 的 `-m` 表达式**和**
   `test_test_architecture_policy.py:195-205` 里钉死的两条字面量，为一个样板测试动 G1/G2 的
   共享闸不划算。
9. **PR-B 不新增任何 core 数值上限**，`docs/product-and-api*.md` **零改动**。插件私有默认值
   （3 秒间隔、每页条数、超时）登记在 examples 包的 README 对 + SOP 样板节；归属写清楚。
10. **样板的单测放 `backend/tests/`**（后端 lane 只跑 `backend/tests`），刻意偏离 SOP §5.3 第 1 项
    「插件自己的 repo 里跑 `pytest tests`」——那条是给仓库外插件写的。README 里点明这条差异。

## 全局约定

- worktree `R=/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/plugin-x9b`；
  **先 `ls -l frontend/node_modules` 判断是不是软链**，是软链就绝不跑 `npm install`。
- `PY="${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python}"`；pytest 一律带
  `-p no:cacheprovider`。
- 变异验证前先 `git commit --only` 打检查点；先 `grep -c` 确认变异真的改到了再跑。
- 任务串行 T1 → T2 → T3 → T4，每任务收尾至少 backend + contracts 两泳道绿；T3 之后整条
  `bash scripts/check.sh` 可绿。

---

## 0. 现状核实（行号均已对本 worktree 亲自核实）

### 0.1 gap-consult 接缝（PR-A 落地形态）

- SDK：`backend/app/extension_sdk/gap_consult.py:35` `ASK_GAP_CONSULT_POINT = "ask.gap_consult"`；
  `:38-44` `GapConsultAvailabilityContext{contribution_id, deadline_monotonic}`；
  `:46-64` `GapConsultExtensionContext{query, cancellation, max_suggestions, deadline_monotonic}`
  ——**没有任何 core 端口字段**，`max_suggestions` 是**本次剩余**预算（可小于
  `query.max_suggestions`）；`:67-70` `GapConsultContributor.consult(context) ->
  ContributorResult[GapSuggestion]`。SDK 从 `app.extension_sdk` 顶层已导出
  （`__init__.py:86-99, 212-223`）。
- domain：`backend/app/domain/gap_consult.py:38-46` 八个上限常量
  （`MAX_GAP_PHRASES=2`/`MAX_SUGGESTIONS=5`/`QUESTION_MAX_CHARS=300`/`PHRASE_MAX_CHARS=60`/
  title 200/summary 400/source_label 40/url 2048）；`:49-55` `GapConsultQuery{question, gaps,
  max_suggestions}`；`:58-71` `GapSuggestion{title, url, summary="", source_label=""}`（**四字段**，
  刻意无日期）。
- **⚠ 承重事实（决定插件怎么写）**：`backend/app/extensions/gap_consult.py:333-436` `_execute`
  ——**可用性探针与 `consult` 一起跑在同一条 `daemon=True` 私有线程上**（`:372-401`），
  **不 `copy_context()`**（`:356-360`，注释明写「不要修」），主线程按 50ms 分片 `join`
  （`:403-431`），**每一片都重读取消与 deadline，包括观察到线程结束的那一片**——迟到的返回
  值一律丢弃。推论三条：①**探针必须 I/O-free 且极快**，它花的是读者的等待时间；
  ②插件线程里 **ContextVar / 线程局部全部为空**；③超过 deadline 才返回 = 白算。
- `consult` 入口的短路顺序：`:232` 零 contribution 严格 no-op → `:233` 类型 → `:238` `_valid_query`
  → `:241` `_valid_deadline` → `:245` 调用方连接租约。`:301-315`：`status is UNAVAILABLE` 的结果
  **一条都不收**（且其 URL 不进跨 contributor 去重集）；`PARTIAL` 照收。
- Settings：`backend/app/core/config.py:172-177` `ask_gap_consult_timeout_seconds`
  默认 **4.0**、`0 < x ≤ 30`。
- 文档落点：`docs/product-and-api.md:2235` `### Gap consultation (ask.gap_consult)`、`:2261`
  超时行；`docs/product-and-api_zh.md:1747` `### 缺口外扩检索`。

### 0.2 registry / capability（裁决 7 的证据）

- `backend/app/extension_sdk/contracts.py:176-179` `ExtensionContribution{declaration,
  implementation, availability: AvailabilityProbe | None = None}` —— **逐 contribution** 的可选探针。
- `backend/app/extensions/registry.py:469-484` `availability(contribution_id, context)`：
  **先**迭代 `manifest.requires`（`:480`，**manifest 级**，全 contribution 共享），**再**调
  `contribution_availability`（`:484`）；`:494-520` 后者只跑该 contribution 自己的探针，
  探针抛错 → `availability_probe_failed`，返回值不是 `Availability` → `invalid_availability_probe`。
- `backend/app/extension_sdk/contracts.py:138-169` `ExtensionManifest`：`requires` / `provides` /
  `ui_contributions` / `depends_on` 各自独立；`:165-168` 明写 `provides` 的每个名字必须在
  `capability_decisions` 里有配对探针、且 `:` 是 core 保留分隔符。
- `backend/app/extension_sdk/ui.py:16-19` `UiContributionDeclaration{id, slot, capability}`
  —— UI 那条**有自己的** `capability` 字段。
- `backend/app/extension_sdk/contracts.py:65-68` `CancellationToken` 协议 =
  `is_set()` + `raise_if_cancelled()`；`:71-78` `Availability` / `Availability.available()`；
  `:114-117` `ContributorResult{items, status, failure}`；`:81-87` `ExtensionFailure{kind, code}`
  （code 必须稳定且 content-free）。

### 0.3 部署插件装载与路由

- `backend/app/extensions/discovery.py:66` `_STABLE_ID = ^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$`
  （`examples.arxiv_search` 合法）；`:68` `_ENTRY_KEYS = {bundle, enabled, settings}`；
  `:209-272` `_load_bundle`：`"<module>:<attr>"`、`manifest.id` 必须等于 config key、
  `trust` 必须 `deployment`、`api_version` 必须等于本 build；`:275-380` `_bind_settings`：
  **core 自己从 `model_fields` 算允许键集**（不信插件的 `extra="forbid"`），
  `configure` 在 `register` **之前**跑，拒绝只带键名与异常类名。
- `backend/app/extension_sdk/http.py:36` `PLUGIN_HTTP_ROUTER_POINT = "http.plugin_router"`。
- `backend/app/extensions/http_router.py:32-101` `collect_plugin_router_specs`：kind 必须
  `CONTRIBUTOR`、trust 必须 `deployment`、**每插件至多一个 router**、factory 必须 callable。
- `backend/app/domain/extension_http.py:24` `PLUGIN_ROUTE_PREFIX = "/api/extensions"`；
  `:132-161` `PluginRouteContext` **八个字段**（`plugin_id` / `settings` /
  `require_notebook_capability` / `require_notebook_read` / `current_actor` / `user_error` /
  `url_sources` / `emit_event`）；`:73-129` `PluginUrlSourceImportPort`：**端口自己授权**
  （对请求自身的已认证用户查 `sources:write`，不通过给 404），
  **`def` handler 用 `import_urls`，`async def` handler 必须 `await import_urls_async`**——
  用反了在运行时 `RuntimeError`；`:27-42` `PluginActor{id, is_admin}`（`is_admin` 是站点级，
  **不是** notebook 授权，注释明写不得据此分支）。
- SOP：`docs/deployment-extensions-sop.md:183-252` §3.4 路由（含 `{notebook_id}` 结构性门、
  401→424 翻译、`emit_event` 只收四个字段）；`:265` §3.5 扩展点表里 `ASK_GAP_CONSULT_POINT` 那行；
  `:277` §3.6 gap-consult 红线（独立线程/硬超时/勿依赖 contextvars/超时即丢弃/**只给 PDF 直链**）。

### 0.4 前端 UI 包与同步

- `frontend/scripts/sync-ui-plugins.mjs:31-56`：包目录名 `^[a-z][a-z0-9-]*$` 且**不许以 `ext-` 开头**
  （落点 `features/ext-<包名>/`）；`STABLE_ID` 与后端同一条正则；`SLOTS` /`PERMISSIONS` /`MODES`
  取值表；`MANIFEST_FILE = "ui-plugin.json"`；`ENTRY_FILES = ["workspace-plugin.ts",
  "workspace-plugin.tsx"]`。导出纯函数供单测直调——`frontend/tests/unit/sync-ui-plugins.test.mjs:9-16`
  import 了 `inspectPackage` / `validateManifest` / `mergeContractRows` / `parsePluginRoots` /
  `renderLocalRegistry` / `syncUiPlugins`。
- `frontend/package.json:7-19`：`sync:ui-plugins` + `postinstall` + 五个 `pre*` 钩子
  （`predev`/`prebuild`/`prestart`/`pretest`/`prelint`）都会带当前环境重跑同步（幂等）。
- `.gitignore` 末三行：`/frontend/features/ext-*/` 与
  `/frontend/features/extension-sdk/registry.local.ts` 都是生成物、不入库。
- **⚠ SOP `:460` 明写：配了插件的树跑不过基座的 `npm run test`**——
  `extension-ui-host.component.test.tsx` 钉「零插件时合并 registry == 内建目录、长度 1」，
  这条不许放宽成 `>= 1`。**推论**：样板 UI 包**不能**在默认前端泳道里被同步进去，
  它的守卫必须另开一条显式 lane（§1.7）。
- 五条扩展守卫（`frontend/tests/guards/extension-*.test.mjs`）扫的是
  `frontend/features/ext-*/`——**公网仓库里这一档恒为空**
  （`extension-ui-layout-guard.test.mjs:195-200` 自己登记了这句），样板是让它非空的机会，
  但只有先同步才行。
- `frontend/features/extension-sdk/contracts.ts:55-95` `WorkspaceExtensionContext`
  （`pluginId`/`slot`/`actor`/`notebook`/`source`/`uiMode`/`permissions`/`dialog`）；
  `:97-136` actions（`refreshSources()` **会 reject，必须 catch**；`openDialog`/`closeDialog` 到插件
  手里是零参重载）；`:148-154` `ExtensionRequestInit`（`query` 是唯一拼查询串的路子）；
  `:162-167` `WorkspaceExtensionApi{requestJson, requestVoid, requestBlob, userMessage}`。
  `frontend/features/extension-sdk/ui.tsx:113-120` `ExtensionModal({context, actions, storageKey,
  title, description, children})`。

### 0.5 门禁与扫描面

- `backend/pytest.ini:6-11` 五个 marker；`slow` 的描述当前写死为
  「long-running scale/perf tests that build real HNSW/ANN indexes」。
- `scripts/check_backend.sh:19-20` `-m "not slow and not architecture_contract_heavy and not
  graph_index_contract"`；`scripts/check_backend_extended.sh` 对偶的
  `-m "slow or architecture_contract_heavy or graph_index_contract"`；
  **两条字面量被 `backend/tests/test_test_architecture_policy.py:195-205` 逐字钉死**
  （裁决 8 的成本依据）。`scripts/check_extended.sh` 是薄 wrapper（`check.sh` +
  `check_backend_extended.sh` + facade 棘轮），新 lane 挂这里。
- `scripts/check_architecture_boundaries.py:721` `app_root = root / "backend/app"`
  —— 架构守卫**只扫 `backend/app`**，`examples/` 不在扫描面内（也没有任何 `app.*` 会 import 它）。
- `scripts/check_ui_vocabulary.py:77-82` 默认三个根（`frontend/app`、`frontend/features`、
  `backend/app`）；`:382` 有 `--extra-root`（可重复），`:440-441` 对每个额外根扫 `**/*.py` 的
  `user_error()` 文案，`:63` 明写它**只扩大扫描面**、不改任何既有检查。
  → 样板的中文文案默认**扫不到**，必须在 contracts 泳道显式加 `--extra-root`（§1.7）。
- `scripts/check_contracts.sh` 里 `check_ui_vocabulary.py` 当前无参调用；
  `test_test_architecture_policy.py` 的 `REQUIRED_LAYERS` 只按**子串**判它在场，加参数不破坏它。
- `scripts/generate_ui_extension_contract.py --check` 对账的是**内建** fixture
  `backend/tests/fixtures/ui_extension_contract.json`；样板 UI 包默认不同步，故**零影响**。

### 0.6 e2e 可以照抄的既有形态

- `backend/tests/test_extension_plugin_routes.py:1-56`：真 `.py` 文件上真 `sys.path`、真 discovery、
  真 `create_app()` + `TestClient`——「monkeypatch 掉挂载就等于跳过被测机制」。
- `backend/tests/test_extension_discovery.py:75-107` `frozen_runtime_reset`（清三个进程级
  `lru_cache`：`get_settings` / `default_extension_runtime` / `deps.repository`）、
  `:109-119` `_plugin_import_isolation`（autouse，还原 `sys.path` 与 `sys.modules`）、
  `:152-199` `_module_name` / `_write_plugin_package` / `_write_config` / `_entry`。
- **URL 导入的零网络先例**：`test_extension_plugin_routes.py:912-920` monkeypatch
  `remote_sources.probe_pdf` 与 `source_routes.kg_scheduler.submit_job`。
- `backend/app/services/remote_sources.py:88-115` `probe_pdf`：接受 `application/pdf`
  或 `%PDF-` 魔数，重定向逐跳复验公网策略（`:60-72`）；`:23-53` `validate_public_http_url`
  拒绝私网/回环地址 → **e2e 不能用 loopback stub 服务导入链路**，只能走上面那条 monkeypatch。

### 0.7 其它

- `silicon_notebook_fangan.md` 全文 **0 处**提到插件/arXiv → 本 PR **不需要**动 `fangan_done.md`。
- 生产固定 `--workers 1`（`packaging/start.sh:82-86`、`scripts/prod.sh:190-197`、
  `AGENTS.md:1151`）→ 进程内节流对生产就是全局节流（§1.4 的前提）。
- 仓库里**尚无** `examples/` 目录，也**尚无**任何 `extensions.example.toml`（现有的
  `EXTENSIONS_CONFIG` 提及只在四份根文档 + SOP 对 + 部署文档对里）。

## 1. 设计

### 1.1 目录与模块图

```
examples/extensions/arxiv-search/
├── pyproject.toml                 # name="silicon-notebook-arxiv-search"；对本仓库零依赖
├── extensions.example.toml        # 抄改即用的部署配置样例
├── README.md / README_zh.md       # 三步启用 + 设置表（插件私有数值的登记处）+ 已登记局限
├── src/silicon_notebook_arxiv_search/
│   ├── __init__.py                # 只导出 BUNDLE 与版本号
│   ├── settings.py                # ArxivSearchSettings(BaseModel)
│   ├── atom.py                    # 纯函数：Atom bytes -> tuple[ArxivPaper, ...]；零 I/O
│   ├── client.py                  # 节流 + 取数 + search()；零 app.* import
│   ├── routes.py                  # build_router(context) -> APIRouter
│   ├── consult.py                 # ArxivGapConsultContributor
│   └── bundle.py                  # manifest / configure / register / capability_decisions / BUNDLE
└── ui/arxiv-search/               # 同步落点 features/ext-arxiv-search/（必须扁平）
    ├── ui-plugin.json
    ├── workspace-plugin.tsx       # ArxivSearchEntry
    └── search-panel-model.ts      # 纯逻辑（勾选/翻页/作者串），便于单测
```

依赖方向（**单向，不许成环**）：
`bundle → {settings, routes, consult}`；`routes → {client, atom, settings}`；
`consult → {client, atom, settings}`；`client → atom`；`atom` 零依赖。
`atom.py` 与 `client.py` **不得** import `app.*` 的任何东西——它们就是给 IEEE 内网变体换掉的那一层
（方案 v2 复用性：外扩 contributor 与检索客户端必须分层，将来把外扩做成 reflect 循环动作时
`consult.py` 换调用点即可，`client.py` 一行不动）。

### 1.2 settings（`settings.py`）

```python
class ArxivSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")     # core 自己也算允许键集，这只是双保险
    base_url: str = "https://export.arxiv.org/api/query"
    max_results: int = Field(10, ge=1, le=20)     # 单页条数
    timeout_seconds: float = Field(10.0, gt=0, le=60)
    politeness_interval_seconds: float = Field(3.0, ge=0, le=30)
    user_agent: str = "silicon-notebook-arxiv-sample/0.1 (+https://arxiv.org/help/api)"
    consult_enabled: bool = False                 # 裁决 6
    consult_max_suggestions: int = Field(3, ge=1, le=5)
```

刻意**没有** `api_key_env`：arXiv API 无鉴权。README 要点明「凭证怎么配」看 SOP §3.2 的
`api_key_env` 惯例，IEEE 变体在那里落地——样板不假造一个用不上的密钥字段。
`configure()` 只存值，不起线程、不开连接（SOP §3.2 红线）。

### 1.3 Atom 解析（`atom.py`，纯函数）

`parse_atom(payload: bytes, *, limit: int) -> tuple[ArxivPaper, ...]`，
`ArxivPaper = dataclass(frozen=True, slots=True){arxiv_id, title, authors, published, summary,
pdf_url, abs_url}`。

- 命名空间 `{http://www.w3.org/2005/Atom}`；逐 `entry` 取 `id` / `title` / `summary` /
  `published` / `author/name`。
- `pdf_url` 取 `<link title="pdf">` 的 `href`，**`http:` 升级成 `https:`**；缺这条 link 时
  由 `arxiv_id` 构造 `https://arxiv.org/pdf/{id}`。SOP §3.6 红线要求**只给 PDF 直链**——
  导入端只探测你给的那个 URL，不会去落地页找。
- 标题/摘要折叠空白后各自截断（插件私有上限，登记在 README）。
- **逐条降级**：一条 entry 缺 `id` 或 `title` 就丢这一条，绝不掀翻整页。
- **⚠ 已登记局限**：stdlib `xml.etree` 不防 XML 实体炸弹。缓解是双重的——`base_url` 是
  部署方自己配的（不是用户输入），且 `client.py` 读响应时**按字节上限截断**。
  面向不可信上游的插件应当上 `defusedxml`；样板不引入第三方依赖（裁决 3），这条写进 README。

### 1.4 节流（`client.py`）— 承重设计

模块级 `_THROTTLE = threading.Lock()` + `_LAST_REQUEST_AT: float`（`time.monotonic()`）。

```python
def acquire_slot(interval: float, budget_seconds: float) -> bool:
    """在 budget 内拿到发一次请求的许可；拿不到返回 False（绝不 sleep 越过 budget）。"""
```

- `_THROTTLE.acquire(timeout=budget)`；拿不到 → `False`。
- 算 `wait = interval - (monotonic() - _LAST_REQUEST_AT)`；**若 `wait` 超过剩余 budget，
  立刻 `release()` 并返回 `False`**——这是整条设计的关键：宁可这次不检索，也不睡过头。
- `sleep(wait)` → 调用方发请求 → `finally` 里更新 `_LAST_REQUEST_AT` 并 `release()`。
- 锁**跨整个 HTTP 请求持有**，请求因此真正串行、间隔 ≥ `interval`（arXiv 官方要求）。

两个调用方的 budget 截然不同，**这是 §1.5/§1.6 各自最要紧的一行**：

| 调用方 | budget | 拿不到许可时 |
| --- | --- | --- |
| `/search` 路由 | `timeout_seconds + politeness_interval_seconds`（宽裕） | `user_error(503, "arXiv 检索排队中，请稍后再试")` |
| gap consult | `deadline_monotonic - monotonic() - timeout_seconds` | 立刻返回 `UNAVAILABLE("arxiv_throttled")`，**零网络** |

**⚠ 已登记局限**：节流是**进程内**的。生产固定 `--workers 1`（§0.7）所以它等于全进程节流；
多 worker / 多副本部署需要外部协调（Redis 之类），README 写明。

### 1.5 bundle 与两条 capability 门（裁决 7 的落地）

```python
_ROUTER  = ContributionDeclaration(id="examples.arxiv_search.router",
                                   point=PLUGIN_HTTP_ROUTER_POINT, kind=ContributionKind.CONTRIBUTOR)
_CONSULT = ContributionDeclaration(id="examples.arxiv_search.gap_consult",
                                   point=ASK_GAP_CONSULT_POINT,    kind=ContributionKind.CONTRIBUTOR)
_PANEL   = UiContributionDeclaration(id="examples.arxiv_search.panel",
                                     slot="workspace.side_panel",
                                     capability="examples.arxiv_search.available")

manifest = ExtensionManifest(
    id="examples.arxiv_search", version="0.1.0", api_version=EXTENSION_API_VERSION,
    display_name="arXiv 文献检索（样板）", trust="deployment",
    contributions=(_ROUTER, _CONSULT),
    requires=(),                                    # ← 空，见下
    provides=("examples.arxiv_search.available",),
    ui_contributions=(_PANEL,),
)
```

**`requires` 必须留空。** `registry.availability(contribution_id, ...)` 才会迭代 manifest 级
`requires`（`registry.py:480`），而这个插件的 router 挂载不经过这个入口（启动时无条件挂载），
UI 侧栏也只走自己声明的 `capability`（`registry.capability_availability()`）——把
`consult_enabled` 放进 `requires` 不会像早前判断的那样连累它们。留空是为了精确性（`requires`
是 manifest 级，会被这份 manifest 将来任何经 `registry.availability()` 消费的 contribution
共享）与语义（`requires` 表达的是插件整体前置条件，不是单个功能的开关）。外扩要单独可关
（裁决 6），所以：

- `capability_decisions = {"examples.arxiv_search.available": _configured}`，
  `_configured(_ctx)` = `settings is not None and bool(settings.base_url)` →
  只门控**侧栏入口**（`UiContributionDeclaration.capability`）。
- 外扩用**逐 contribution 探针**：
  `registrar.add_contributor(ExtensionContribution(declaration=_CONSULT,
   implementation=self._contributor, availability=self._consult_available))`，
  `_consult_available(_ctx)` → 未配置或 `consult_enabled is False` 时返回
  `Availability(AvailabilityStatus.DISABLED, "consult_disabled")`。
- 两个探针都**必须 I/O-free**（SOP §3.3；且外扩那个跑在 deadline 内的 worker 线程上，§0.1）。
  只读已绑定的 settings 对象，一个字节的网络都不碰。

`register()` 注册的 contribution id 集合必须**逐字等于** `manifest.contributions` 的 id 集合，
否则 core 停机（SOP §3.1）。UI 那条不进 `contributions`（它是 metadata-only）。

### 1.6 三条路由（`routes.py`）

工厂 `build_router(context: PluginRouteContext) -> APIRouter`。三个 handler 全部是
**同步 `def`**（已在 FastAPI 线程池里，可以直接调阻塞的 `url_sources.import_urls`；
写成 `async def` 再调它会在运行时 `RuntimeError`，见 §0.3）。

| 路由 | 门 | 行为 |
| --- | --- | --- |
| `GET /health` | `Depends(context.current_actor)` | `{plugin_id, configured: bool}`，**零远端调用** |
| `GET /notebooks/{notebook_id}/search` | `Depends(context.require_notebook_read)` | `q`（必填）、`start`（≥0）→ `{items:[...], start, has_more}` |
| `POST /notebooks/{notebook_id}/import` | `Depends(context.require_notebook_capability("sources:write"))` | body `{urls: [...]}` → `{created:[...], rejected:[...]}` |

两条 notebook 路由的路径里都带字面量 `{notebook_id}`，好让 core 的结构性门看见
（§0.3；那是纵深防御，真正的授权在端口自己身上）。

失败路径（**逐条都必须走 `context.user_error`，绝不 401，绝不把 `str(exc)` 上屏**）：

- `q` 空白 → `400 请输入检索关键词`；`q` 超长 → `400 检索关键词过长`。
- 节流拿不到许可 → `503`（见 §1.4 表）。
- 上游超时/HTTP 错/解析不出 → `502 arXiv 检索暂时不可用，请稍后再试`。异常只记类名，不记正文。
- 导入 body 不是非空字符串列表、或条数超上限 → `400`。
- **导入 URL 的主机白名单**：只接受 `arxiv.org`（及其子域）的 `http(s)` URL，否则
  `400 只能导入 arXiv 的 PDF 链接`。理由：不加这道闸，插件路由就成了一个通用 URL 导入代理。
  它**不是**提权（core 自己的端点本来就收任意 URL），但样板应当示范最窄的形状。
  **这道闸必须排在调用 `url_sources` 之前**，用例要断言端口一次都没被调到。
- 成功导入后 `context.emit_event({"event": "arxiv_urls_imported", "count": len(created)})`
  ——只用白名单里的四个字段（§0.3）。

### 1.7 gap-consult contributor（`consult.py`）

```python
def consult(self, context: GapConsultExtensionContext) -> ContributorResult[GapSuggestion]:
```

顺序（每一步都有对应用例）：

1. settings 缺失或 `consult_enabled` 为假 → `UNAVAILABLE("consult_disabled")`（探针已经挡过，
   这是纵深防御）。
2. `context.cancellation.is_set()` 为真 → 返回空。**用 `is_set()`，不要 `raise_if_cancelled()`**
   ——宿主的 `_target` 用 `except BaseException` 把任何异常记成 `gap_consult_failed`
   （`gap_consult.py:396-397`），抛出去只会把「取消」污染成「插件炸了」；宿主自己的 join 循环
   每一片都在查取消（§0.1），结局照样正确。
3. **构造 arXiv 查询词**：从 `context.query.question` 与 `context.query.gaps` 里抽**拉丁字母词**
   （`[A-Za-z][A-Za-z0-9+.#-]{1,}`），去停用词、去重、截到上限。
   **抽不出词就 `UNAVAILABLE("no_latin_query_terms")` 并立即返回**——arXiv 是英文关键词检索，
   把纯中文问题发过去保证零命中，白付一次 3 秒节流 + 一次网络往返。
   （⚠ 这条决定了样板在纯中文笔记本上多数时候不出建议，见 §3 R2，也是给主 agent 的存疑点之一。）
4. 预算：`remaining = context.deadline_monotonic - time.monotonic()`；
   `remaining <= 最小预算` → `UNAVAILABLE("arxiv_budget_too_small")`。
5. `client.search(terms, budget=..., limit=min(context.max_suggestions,
   settings.consult_max_suggestions))`；节流拿不到 → `UNAVAILABLE("arxiv_throttled")`。
6. 映射成 `GapSuggestion(title=…, url=pdf_url, summary=摘要截断, source_label="arXiv")`。
   **`url` 必须是 PDF 直链**（SOP §3.6 红线）；core 只做净化不做探测，是不是 PDF 由导入端点回答。
7. 任何异常 → `UNAVAILABLE` + 稳定 code（`arxiv_upstream_failed`），**异常正文一律不带出去**。

`deadline_monotonic` 按 `time.monotonic()` 口径比较——SDK 字段名即契约。宿主允许注入别的
clock（`gap_consult.py` 构造参数），那种情况下插件只会更保守（早退），方向安全，写进 docstring。

### 1.8 前端 UI 包（`ui/arxiv-search/`）

`ui-plugin.json`：

```json
{ "api_version": "1",
  "contributions": [{
    "id": "examples.arxiv_search.panel", "plugin_id": "examples.arxiv_search",
    "version": "0.1.0", "capability": "examples.arxiv_search.available",
    "slot": "workspace.side_panel", "permission": "source:write",
    "mode": "all", "component": "ArxivSearchEntry" }] }
```

`id`/`plugin_id`/`capability`/`version` 必须与后端 manifest **逐字一致**，否则浏览器的
`(plugin_id, version, contribution_id)` 元组对不上、整条 contribution 不渲染。
跨语言对账放在后端用例里（T4：Python 读这份 JSON 与 manifest 比）。

`workspace-plugin.tsx` 导出 `ArxivSearchEntry({context, actions})`：

- 入口按钮（`className="button secondary workspace-extension-entry"` + `lucide-react` 图标）
  → `actions.openDialog()`；弹窗用 `ExtensionModal`，`storageKey="search"`。
- **不持有 `open` state**：唯一真相是 `context.dialog`（SOP §4.2）。
- 面板：关键词输入 + 「检索」按钮 → 结果列表（标题/作者/日期/摘要摘录 + 复选框）
  → 「导入所选（N）」按钮 → 逐条回执（已创建/已复用/被拒原因）。
- **长任务忙碌位（红线）**：「检索」点下立刻 `disabled` 且文案换「检索中…」；
  「导入所选」同理换「导入中…」。这两个按钮背后各有一次几秒级的服务端往返。
- 成功导入后 `await actions.refreshSources().catch(...)`——**必须 catch**（§0.4）。
- 一切用户可见错误文案走 `actions.api.userMessage(error, fallback)`，
  **绝不读 `error.message`**（`errors-guard` 是精确计数普查，仓库外包登记不进去）。
- **零 CSS 文件、零颜色字面量、零内联 `style`**；只 import
  `../extension-sdk/contracts.ts`、`../extension-sdk/ui.tsx`、`react`、`lucide-react` 与同包兄弟。
  **不许 import `../extension-sdk/api.ts`**。
- `search-panel-model.ts`：纯函数（勾选集合增删、翻页 `start` 推进、作者串格式化、
  回执分类），供 node 单测直调。

### 1.9 门禁接线

三处，各一行：

1. **G2 e2e**：`backend/tests/test_arxiv_sample_plugin_e2e.py` 打 `@pytest.mark.slow`，
   同批把 `backend/pytest.ini:11` 的 `slow` 描述放宽成
   「long-running scale/perf tests that build real HNSW/ANN indexes, plus deliberately-G2
   end-to-end tests (skip with -m "not slow")」。**两个 shell 的 `-m` 表达式一个字都不改**
   （裁决 8）。
2. **样板 UI 守卫 lane**：新增 `scripts/check_sample_plugin.sh`，挂进 `scripts/check_extended.sh`
   （`check_backend_extended.sh` 之后）：

   ```bash
   set -euo pipefail
   PKG="$ROOT_DIR/examples/extensions/arxiv-search/ui/arxiv-search"
   cleanup() { (cd "$ROOT_DIR/frontend" && SILICON_NOTEBOOK_UI_PLUGINS= \
                 node scripts/sync-ui-plugins.mjs) || true; }
   trap cleanup EXIT                       # ← 不可省，见 R5
   cd "$ROOT_DIR/frontend"
   export SILICON_NOTEBOOK_UI_PLUGINS="$PKG"
   node scripts/sync-ui-plugins.mjs
   node --test tests/guards/extension-*.test.mjs   # 五条守卫头一次拿到非空样本
   npm run lint                                    # prelint 用同一环境重同步（幂等）；类型检查覆盖 features/ext-*
   ```

   它**必须**在 G2 而不是 G1：同步进去的树跑不过 `npm run test`（§0.4）。
3. **中文文案扫描面**：`scripts/check_contracts.sh` 里给 `check_ui_vocabulary.py` 加
   `--extra-root "$ROOT_DIR/examples/extensions/arxiv-search/src"`。样板的 `user_error()` 文案
   从此进 G1 硬门；`--extra-root` 只扩大扫描面，既有检查逐字不变（§0.5）。

### 1.10 零补丁验收（X9 的存在理由）

两半，缺一不可：

- **机器可跑的那半**（进 T4 的 e2e 文件）：把整个 `examples/extensions/arxiv-search/` 复制到
  `tmp_path`，只把**副本**的 `src` 放上 `sys.path`，写一份指向副本的 TOML，
  `create_app()` 起服务并走通检索链路。它证明的是**装载与运行不依赖包在仓库里的位置**。
- **人工转录的那半**（写进 PR 正文）：干净 checkout + `cp -R` 到仓库外 + 只改
  `EXTENSIONS_CONFIG` / `PYTHONPATH` / `SILICON_NOTEBOOK_UI_PLUGINS` 三个变量 →
  `npm run build` 绿、`/api/ready` 通、`/admin/extensions` 列出 `examples.arxiv_search`、
  侧栏入口出现、检索导入走通。这一半自动化不了（需要一个干净 checkout），
  **验收证据就是 PR 正文里的命令与输出转录**。

## 2. 任务拆分（串行 T1→T2→T3→T4）

### T1（opus）— 包骨架 + 纯层（settings / Atom / 节流客户端）

**文件（新建）**：`examples/extensions/arxiv-search/pyproject.toml`、
`src/silicon_notebook_arxiv_search/{__init__,settings,atom,client}.py`、
`backend/tests/fixtures/arxiv_atom_sample.xml`（真实形状的 Atom 响应，含 3 条 entry：
一条完整、一条缺 `<link title="pdf">`、一条缺 `id`）、
`backend/tests/test_arxiv_sample_plugin.py`（本任务只填纯层用例）。

**要点**：§1.2 / §1.3 / §1.4 逐条。`pyproject.toml` 用 setuptools + `src` 布局，
`dependencies` 里**只有** `pydantic`（SDK 与 fastapi 由后端环境提供，SOP §6 第 1 条）。
`client.py` 的取数走模块级 `_fetch(url, timeout) -> bytes` 且**可注入**
（`search(..., fetch=None)`，镜像 `remote_sources.probe_pdf(fetch=...)` 的既有形态）——
它同时是 T4 e2e 的零网络接缝。响应按字节上限截断读取（§1.3 的 XML 缓解之一）。

**用例（`test_arxiv_sample_plugin.py`，本任务这一批）**：

- `test_atom_entries_parse_into_papers`（fixture → 2 条；缺 `id` 的那条被丢弃、不抛）
- `test_pdf_url_is_constructed_when_the_link_is_missing`（回落 `https://arxiv.org/pdf/{id}`）
- `test_pdf_url_http_is_upgraded_to_https`
- `test_title_and_summary_whitespace_is_collapsed_and_truncated`
- `test_entry_count_is_capped_by_limit`
- `test_parser_performs_no_io`（注入的 fetch 一次都没被调）
- `test_throttle_serializes_and_spaces_requests`（interval 0.05，两次调用的间隔 ≥ 0.05）
- `test_throttle_refuses_rather_than_sleeping_past_the_budget`（**承重**：interval 大、budget 小 →
  返回 False，且真实墙钟 < budget + 余量）
- `test_throttle_lock_is_released_on_exception`（调用方抛错后下一次仍能拿到许可）
- `test_settings_defaults`（`consult_enabled is False`、`politeness_interval_seconds == 3.0`）
- `test_settings_reject_out_of_range`

**变异（逐条先 `grep -c` 确认改到了）**：
① 把 §1.4 里「wait 超过 budget 就 release+False」那一支删掉 → budget 用例必须红；
② 把 `acquire(timeout=budget)` 换成无参 `acquire()` → 同一条红；
③ 丢弃畸形 entry 的 `continue` 改成 `raise` → 降级用例红；
④ 把 `limit` 截断删掉 → 条数上限用例红。

**验证**：
`PYTHONPATH=$R/backend $PY -m pytest -p no:cacheprovider -n0 backend/tests/test_arxiv_sample_plugin.py`；
`bash scripts/check_backend.sh`；`bash scripts/check_contracts.sh`。

---

### T2（opus）— bundle + 两条 capability 门 + 路由 + 外扩 contributor

**文件（新建）**：`src/silicon_notebook_arxiv_search/{bundle,routes,consult}.py`、
`examples/extensions/arxiv-search/extensions.example.toml`。
**文件（改）**：`backend/tests/test_arxiv_sample_plugin.py`（追加本批用例）。

**要点**：§1.5 / §1.6 / §1.7 逐条。特别提醒实现者三条最容易做错的：

- `manifest.requires` **必须是空 tuple**，外扩用逐 contribution 的 `availability=` 探针（裁决 7）。
- 路由 handler 全部 `def`（不是 `async def`），因为它们直接调阻塞的 `url_sources.import_urls`。
- 外扩里用 `cancellation.is_set()`，**不要** `raise_if_cancelled()`（§1.7 第 2 步）。

**用例（追加）**：

- `test_manifest_registrations_match_declarations`（注册 id 集合 == `manifest.contributions` id 集合）
- `test_provides_and_capability_decisions_agree`（键集合逐字相等）
- `test_manifest_requires_is_empty_so_the_router_survives_a_disabled_consult`
  （**承重**：`requires == ()`；断言里写清理由，配一条注释指向 `registry.py:480`）
- `test_ui_capability_probe_is_disabled_when_unconfigured`
- `test_consult_probe_is_disabled_by_default` / `..._is_available_when_enabled`
- `test_both_probes_perform_no_io`（注入 fetch 零调用）
- `test_search_route_rejects_blank_and_overlong_query`（`user_error`，不是裸 `HTTPException`）
- `test_search_route_maps_upstream_failure_to_502_without_leaking_the_exception_text`
  （**双断言**：状态码 + 异常正文子串不在文案里）
- `test_import_route_rejects_a_non_arxiv_host_before_touching_the_port`
  （**承重**：`url_sources` 的 spy 调用次数为 0）
- `test_import_route_rejects_an_empty_or_malformed_body`
- `test_import_route_emits_only_whitelisted_event_fields`
- `test_consult_returns_unavailable_without_fetching_when_no_latin_terms`（spy 零调用）
- `test_consult_returns_unavailable_when_the_deadline_has_already_passed`（spy 零调用）
- `test_consult_honours_the_smaller_of_context_and_settings_caps`
- `test_consult_maps_papers_to_pdf_direct_links`
- `test_consult_swallows_upstream_errors_into_a_stable_code`（code 稳定、无异常正文）
- `test_consult_returns_empty_when_cancelled_rather_than_raising`（**承重**，§1.7 第 2 步）

路由用例用手搓的 `PluginRouteContext`（八个字段的 fake，形态照
`backend/tests/test_extension_plugin_routes.py:1-56`），不起 app——真 wire 留给 T4。

**变异**：
① `manifest.requires` 填上 consult capability → `requires` 用例红；
② 把主机白名单挪到 `url_sources` 调用**之后** → 白名单用例红（spy 计数变 1）；
③ `consult` 里把 `is_set()` 换成 `raise_if_cancelled()` → 取消用例红；
④ 502 分支改成 `str(exc)` 上屏 → 泄漏用例红；
⑤ 探针里加一次 fetch → 两条 `no_io` 用例红。

**验证**：同 T1，外加 `bash scripts/check_ui_vocabulary.py --extra-root
examples/extensions/arxiv-search/src`（T3 才进泳道，这里先手跑一次）。

---

### T3（sonnet）— UI 包 + 前端单测 + 三处门禁接线

**文件（新建）**：`examples/extensions/arxiv-search/ui/arxiv-search/{ui-plugin.json,
workspace-plugin.tsx,search-panel-model.ts}`、
`frontend/tests/unit/arxiv-sample-ui-package.test.mjs`、`scripts/check_sample_plugin.sh`。
**文件（改）**：`backend/pytest.ini`（`slow` 描述放宽一句）、
`scripts/check_extended.sh`（挂 lane）、`scripts/check_contracts.sh`（加 `--extra-root`）。

**要点**：§1.8 / §1.9 逐条。UI 包目录**必须扁平**（子目录一律被同步脚本拒绝）；
不许出现 `.css` / `package.json` / `node_modules` / `.d.ts` / `*.test.*`。

**用例（`arxiv-sample-ui-package.test.mjs`，直调同步脚本的纯函数，**不**改文件树）**：

- `inspectPackage(<样板 ui 包路径>)` 通过，且返回的 manifest 行的
  `plugin_id`/`id`/`capability`/`slot`/`permission`/`mode`/`component` 逐字符合 §1.8。
- 包目录名 `arxiv-search` 过 `PACKAGE_NAME` 且不以 `ext-` 开头。
- 包内文件集合 == 三个预期文件（**反向断言**：没有多余文件混进去）。
- `search-panel-model.ts` 的纯逻辑：勾选增删幂等、`start` 翻页推进、作者串格式化、
  回执分类（created/reused/rejected）。

**接线的三处改动各配一条断言**（放在同一个 mjs 里或就近的既有守卫里）：
`check_extended.sh` 里出现 `check_sample_plugin.sh`；`check_contracts.sh` 里
`check_ui_vocabulary.py` 那行带 `--extra-root`。

**变异**：
① 给 UI 包塞一个 `styles.css` → `inspectPackage` 用例红；
② 把 `ui-plugin.json` 的 `plugin_id` 改一个字 → 逐字断言红（T4 的跨语言对账另有一条）；
③ 从 `check_extended.sh` 删掉 lane 那行 → 接线断言红；
④ **移动变异**：把 `search-panel-model.ts` 的逻辑内联进 `.tsx` → 纯逻辑用例 import 失败即红。

**⚠ 收尾必须确认工作树干净**：`scripts/check_sample_plugin.sh` 跑完后
`git status --short` 必须为空，且 `frontend/features/ext-arxiv-search/` 已被 trap 清掉。
若手工中断过，恢复命令是
`cd frontend && SILICON_NOTEBOOK_UI_PLUGINS= node scripts/sync-ui-plugins.mjs`。

**验证**：`cd frontend && npm run test`（**必须仍然全绿**——样板包没被同步进去，
`extension-ui-host.component.test.tsx` 的「长度 1」不受影响）、`npm run build`、`npm run lint`；
`bash scripts/check_sample_plugin.sh`；`bash scripts/check.sh`。

---

### T4（opus）— G2 零网络 e2e + 零补丁验收 + 文档

**文件（新建）**：`backend/tests/test_arxiv_sample_plugin_e2e.py`、
`examples/extensions/arxiv-search/README.md` / `README_zh.md`。
**文件（改）**：`docs/deployment-extensions-sop.md` / `_zh.md`（新增「样板插件」一节）、
`README.md` / `README_zh.md` / `AGENTS.md` / `CLAUDE.md`（各一句）。

**e2e（`@pytest.mark.slow`）**：脚手架 import
`tests.test_extension_discovery` 的 `frozen_runtime_reset` / `_plugin_import_isolation` /
`_write_config`（§0.6），**不复制**。真 TOML → 真 discovery → 真 `create_app()` + `TestClient`。
TOML 里 `politeness_interval_seconds = 0.0`、`consult_enabled = true`、
`base_url = "https://export.arxiv.example/api/query"`（永不解析——fetch 被 monkeypatch）。

- `test_search_chain_returns_fixture_papers_over_the_real_wire`
  （`GET {mount}/notebooks/{nb}/search` 200；fetch 恰好被调 1 次）
- `test_import_chain_creates_a_source_through_the_core_port`
  （monkeypatch `remote_sources.probe_pdf` 与 `source_routes.kg_scheduler.submit_job`，
  照 §0.6 的既有先例；`created` 非空）
- `test_import_chain_refuses_a_foreign_host_end_to_end`（400，且 core 端口零调用）
- `test_gap_consult_chain_yields_suggestions_from_the_frozen_host`
  （从冻结 runtime 取 `GapConsultHost`，用真 `GapConsultCallContext` 调 `consult`，
  拿到 `source_label == "arXiv"`、`url` 是 PDF 直链的建议）
- `test_gap_consult_is_silent_when_consult_is_disabled`（第二份 TOML，`consult_enabled = false`
  → 零建议、fetch 零调用、事件 reason 为 `consult_disabled`）
- `test_ui_manifest_matches_the_backend_manifest`（**跨语言对账**：Python 读
  `ui/arxiv-search/ui-plugin.json`，比 `plugin_id`/`id`/`capability`/`version`）
- `test_the_package_runs_from_outside_the_repository`（§1.10 上半：整包 `cp` 到 `tmp_path`，
  只把副本的 `src` 上 `sys.path`，指向副本的 TOML，`create_app()` + 检索链路走通）
- `test_no_network_is_dialled`（monkeypatch `socket.getaddrinfo` 抛错，整批仍绿）
  ——**⚠ 实现时先确认没有别的东西顺带调它**（SQLite 是文件、`TestClient` 是进程内 ASGI，
  预期安全；万一有，退化成只 patch `client._fetch` 并在注释里登记原因）。

**gap-consult 链路刻意停在宿主层**、不真跑一次 reasoning Ask：core 侧的触发条件与接线已由
`backend/tests/test_gap_consult_ask_wiring.py`（PR-A）钉住，PR-B 要证的是**插件这一侧**
在真装载下能被宿主调到并如实作答。这条选择写进文件头 docstring。

**文档**：

- **SOP 对**新增一节「样板插件：`examples/extensions/arxiv-search`」——它是什么、三步启用、
  演示了哪两个入口、以及「它同时是本 SOP 的可运行范例」。**插件私有数值的登记处是
  examples 的 README 对**（3 秒间隔、每页条数、超时、建议条数），SOP 那节只指过去。
- **examples README 对**：三步启用、设置表（默认值 + 含义）、arXiv 礼貌性限速的出处、
  §3 里所有「已登记局限」的用户可读版本、以及「样板的测试放在 `backend/tests/`、
  真正的仓库外插件应放自己 repo 的 `tests/`」这条差异（裁决 10）。
- **四份根文档**各一句：仓库带一份**默认关闭**的样板部署插件，启用 = 部署方点名
  `EXTENSIONS_CONFIG` + `SILICON_NOTEBOOK_UI_PLUGINS`。
- **`docs/product-and-api*.md` 零改动**（裁决 9）；`fangan_done.md` 零改动（§0.7）。

**变异**：
① 从 TOML 删掉 `consult_enabled = true` → 建议链路用例红；
② 把 e2e 的 marker 删掉 → `check_backend.sh` 会开始跑它（用
`pytest --collect-only -m "not slow"` 对账，确认它被选中即变异生效）；
③ 改坏 `ui-plugin.json` 的 `capability` 一个字 → 跨语言对账红；
④ 把副本测试改成仍指向仓库内的 `src` → 该用例应当仍绿（**说明它没在测该测的东西**），
所以断言里要显式比较被 import 模块的 `__file__` 前缀是 `tmp_path`。

**验证**：
`PYTHONPATH=$R/backend $PY -m pytest -p no:cacheprovider -n0
backend/tests/test_arxiv_sample_plugin_e2e.py`；
`bash scripts/check_backend_extended.sh`；`bash scripts/check.sh`；`bash scripts/check_extended.sh`；
`$PY -m pytest backend/tests/test_architecture_documentation.py`；
`$PY scripts/check_ui_vocabulary.py`（此时已带 `--extra-root`）。

## 3. 风险登记

| # | 风险 | 处置 |
| --- | --- | --- |
| **R1** | **3 秒礼貌性间隔 vs 4 秒外扩硬预算**：上一次检索若在 3 秒内，外扩可睡掉几乎整个预算 | 接受并设计掉：`acquire_slot` 的 budget 由 `deadline_monotonic` 推出，睡不下就**立刻**返回 `UNAVAILABLE("arxiv_throttled")`、零网络（§1.4/§1.7）。后果是热点部署上外扩经常不出建议——这是正确的行为，宿主 fail-open，答案逐字不变 |
| **R2** | **纯中文问题抽不出拉丁词** → 样板在中文笔记本上多数时候不出外扩建议 | 接受：抽不出就 `no_latin_query_terms` 早退（§1.7 第 3 步）。备选是照发全文——保证零命中还白付一次节流 + 往返。**这条列为存疑点，见下** |
| **R3** | **stdlib `xml.etree` 不防 XML 实体炸弹** | 缓解而非消除：`base_url` 是部署方自己配的（非用户输入）+ 响应按字节上限截断读。零第三方依赖是裁决 3；面向不可信上游应上 `defusedxml`——写进 README |
| **R4** | **节流是进程内的** | 生产固定 `--workers 1`（§0.7）故等于全进程节流；多 worker/多副本需外部协调。README 登记 |
| **R5** | **G2 lane 中断会把 `frontend/features/ext-arxiv-search/` 留在树里**，之后 `npm run test` 必红 | 三重兜底：脚本 `trap ... EXIT` 清理；`pretest` 钩子在变量未设时自愈（同步脚本会删带出处标记的副本）；恢复命令写进 T3 与 README |
| **R6** | **复用 `slow` marker 语义变宽** | 接受（裁决 8）：同批放宽 `pytest.ini` 的描述。新 marker 要改两个 shell 的 `-m` **和** `test_test_architecture_policy.py:195-205` 的两条钉死字面量，为一个样板测试动 G1/G2 共享闸不划算 |
| **R7** | **样板的测试放在 `backend/tests/`，偏离 SOP §5.3 第 1 项** | 刻意（裁决 10）：后端泳道只跑 `backend/tests`。README 点明真正的仓库外插件应放自己 repo 的 `tests/`。代价是「整包 `cp` 出去就能跑测试」这半不成立——但零补丁验收管的是**运行时**，由 §1.10 两半覆盖 |
| **R8** | **样板 UI 包永远不在默认 `npm run test` 的树里** | 结构性事实（§0.4，SOP `:460`）。它的验证 = T3 的纯函数单测（G1）+ T3 的 G2 lane（真同步 + 五条守卫 + 类型检查）。副产品：`extension-ui-layout-guard` 那一档头一次拿到非空样本 |
| **R9** | **导入路由的 `arxiv.org` 主机白名单比 core 端点更窄** | 刻意：不加这道闸插件路由就是通用 URL 导入代理。不是提权（core 端点本就收任意 URL），但样板该示范最窄形状。用例断言它排在端口调用**之前** |
| **R10** | **方案页的「manifest `provides` 一个 capability 门控外扩」在现实里做不到** | 已在裁决 7 改掉：`provides` 是能力名表不是开关，且 `manifest.requires` 只门控经 `registry.availability(contribution_id, ...)` 消费的 contribution（`registry.py:480`）——路由挂载与侧栏探针都不经过那个入口，放进 `requires` 不会像早前判断的那样连累它们。改用逐 contribution 探针（`contracts.py:179`），理由是精确性（`requires` 是 manifest 级、会被这份 manifest 将来任何经 `registry.availability()` 消费的 contribution 共享）与 `requires` 的整体前置条件语义，不是「否则会连累路由/侧栏」。**这是方案与现实的实质冲突，已上报主 agent** |
| **R11** | **e2e 的 `socket.getaddrinfo` 零网络断言可能被无关调用打中** | T4 实现时先验证；打中就退化成只 patch `client._fetch`，并在注释里登记为什么这条断言更弱 |
| **R12** | **`examples/` 是仓库里第一个该目录** | 已核实无副作用：架构守卫只扫 `backend/app`（§0.5），`check_ui_vocabulary` 默认三根不含它（故 T3 显式加 `--extra-root`），packaging 是纯 shell 不做仓库 glob，后端泳道只跑 `backend/tests` |

## 4. 存疑点——主 agent 已全部拍板（2026-08-24）

1. **外扩的中文问题：维持早退**。判据面是**问题措辞 + 全部缺口短语**一起抽拉丁词
   （缺口短语常是「shaping loss bounds」这类术语标签，正是 arXiv 能吃的输入；只抽问题
   会把这半白扔掉），一个拉丁词都抽不出才早退——零网络、零节流占用，返回
   `UNAVAILABLE("arxiv_no_latin_terms")` 之类的稳定 code。备选 (a)(b) 均否决
   （白付成本 / 越权开端口）。样板 README 对写明这条行为与理由（中文库的观感解释权
   放文档，不放 trace）。
2. **e2e 停在宿主层：批准**。PR-A 的 `test_gap_consult_ask_wiring.py` 已钉 core 侧触发，
   PR-B 证「真装载下插件被调到并如实作答」即可，不重复装配 Ask fixture。
3. **`slow` 复用：批准**（即裁决 8，关掉此项）。**T4 评审修订**：实测 e2e 文件串行耗时
   ~2s（9 个用例），远低于 G1 单文件预算；打 `slow` marker 换来的是「每次编辑都跳过它」，
   而不是省下有意义的墙钟。最终没有给它打任何 marker——文件直接留在 G1 每次 PR 跑，
   `pytest.ini` 的 `slow` 描述收回「plus deliberately-G2 end-to-end tests」半句，回到接入前
   逐字。`check_sample_plugin.sh`（G2）继续做它本来该做的事：把插件真同步进
   `frontend/features/ui-plugins` 再整链验证，这是 G1 测试文件本身够不到的另一件事，跟这份
   文件挂不挂 `slow` 无关。理由与取舍详见
   `backend/tests/test_arxiv_sample_plugin_e2e.py` 文件头的「Not marked slow.」段。
4. **layout-guard 注释：顺带改**，按建议措辞（「G1 恒为空；G2 的 `check_sample_plugin.sh`
   会给它非空样本」），落进 T3 的文件清单。
