# 前端构建期私有 UI 插件接入实现计划（X5）

状态：执行中（2026-08-23）。工作树 `/Users/huzhifeng/workspace/silicon-notebook/.claude/worktrees/plugin-x5/frontend`
（含 `node_modules`）。本文是实现子代理的唯一规格来源；它们看不到产生本文的对话。
后端配套计划：`docs/superpowers/plans/2026-08-23-deployment-extensions-backend.md`（另一 worktree，独立 PR）。

全局约定：用 Edit/Write 改文件；不回滚既有改动；生产代码只放 `app`/`features`；测试只放 `tests/{unit,component,guards}`；
不做热更新；不引入远程 JS / 运行时扫描注册 / 全局 store / raw fetch / 跨 owner setter。每个任务结束
`npm run test && npm run build` 必须绿，`git status --short` 必须干净（生成物全部被忽略）。

## 0. 实测结论（实现子代理先接受）

**(0.1) Node 无法加载 `.tsx`**（`node --test` 对 `.tsx` 报 `Unknown file extension`）。`tests/guards/extension-ui-parity.test.mjs:5`
与 `tests/unit/extension-ui-registry.test.mjs:4` 在 node 泳道里**真的 import** `features/extension-sdk/registry.ts`。这是内建插件
写成 `workspace-plugin.ts` + `createElement` 的真实原因。**结论：`registry.ts` 不能静态 import `registry.local.ts`。** registry 拆成
两个模块（§1.2）。

**(0.2) 复制进来的插件代码会被现有全仓守卫扫到。** `test-support/semantic-source.mjs:39` 的 `appSourceModules()` 递归扫
`app/` + `features/`；9 个守卫用它。这是特性。但两条会咬人，必须写进插件作者文档：
- `errors-guard.test.mjs` 是精确计数普查：任何模块里的 `.message`/`.error`/`.error_message` 属性读取、`new Error("中文")`
  都必须登记在公网仓库的 `APPROVED_*` 清单——私有插件登记不进去 → 插件不得读 `error.message`、不得 `throw new Error("中文…")`。
  SDK 给 `api.userMessage(error, fallback)`。
- `api-boundary.test.mjs:12-20`：任何模块直接 `fetch(` 都报红。api 端口是插件唯一的 HTTP 出口。

**(0.3) `tsconfig.json:24` 的 `"exclude": ["node_modules"]` 只排除 `frontend/node_modules`**，不排除 `features/ext-foo/node_modules`。
带依赖的插件包会被 `next build` 的类型检查整个吞进去。必须拒绝。同理 `next build` 会类型检查 `tests/`，改签名时组件测试的调用点同步改。

**(0.4) `git status` 洁净性只有一条路**：已跟踪文件不受 `.gitignore` 影响，所以"提交空存根 + 脚本覆写"必然脏。
**唯一稳妥解：`registry.local.ts` 不入库、被忽略，由 `postinstall` + 五个 `pre*` 钩子保证它总在。** `npm ci` 必触发 `postinstall`
（`scripts/prod.sh:164`、`scripts/pack.sh:60` 都是 `npm ci`）。

## 1. 总览

### 1.1 数据流

```
SILICON_NOTEBOOK_UI_PLUGINS="/srv/plugins/ieee-xplore:/srv/plugins/foo"
   │  npm run {dev,build,start,test,lint} 的 pre* 钩子 / npm ci 的 postinstall
   ▼
frontend/scripts/sync-ui-plugins.mjs        （零依赖，只用 node:fs/path/url）
   ├─ 校验每个包：扁平、只含 .ts/.tsx + ui-plugin.json、无 node_modules/package.json
   ├─ 复制 →  frontend/features/ext-<basename>/                 （.gitignore 忽略）
   ├─ 生成 →  frontend/features/extension-sdk/registry.local.ts  （.gitignore 忽略）
   └─ 生成 →  frontend/.local/ui-extension-contract.json         （.local/ 已忽略）
                 = backend/tests/fixtures/ui_extension_contract.json 的内建行 + 各包 ui-plugin.json 的行
   ▼
features/extension-sdk/workspace-registry.ts
   WORKSPACE_UI_CONTRIBUTIONS = defineWorkspaceUiRegistry([...BUILTIN（registry.ts）, ...LOCAL（registry.local.ts）])
   ▼
app/page.tsx → <WorkspaceExtensionOutlet registry={WORKSPACE_UI_CONTRIBUTIONS} …/>
   ▼
features/extension-sdk/host.tsx：四门过滤（visibility.ts 不变）；每个 contribution：actions = withExtensionApi(actions, contribution.pluginId)
   ▼
插件组件 props = { context, actions: { openUnderstanding, refreshSources, api } }
```

### 1.2 新增文件

| 路径（`frontend/` 相对） | 入库 | 说明 |
|---|---|---|
| `scripts/sync-ui-plugins.mjs` | ✅ | 复制 + 生成 + 契约导出，零依赖 |
| `features/extension-sdk/workspace-registry.ts` | ✅ | 合并 registry（内建 + local） |
| `features/extension-sdk/api.ts` | ✅ | 按 pluginId 绑定的 HTTP 端口，宿主侧构造 |
| `features/extension-sdk/ui.tsx` | ✅ | `ExtensionModal`（复用 `FloatingModalCard`） |
| `features/extension-sdk/registry.local.ts` | ❌ 生成 | 空存根或本地条目 |
| `features/ext-*/` | ❌ 生成 | 插件包副本 |
| `.local/ui-extension-contract.json` | ❌ 生成 | 部署期对账输入 |
| `tests/unit/sync-ui-plugins.test.mjs` | ✅ | 脚本单测 |
| `tests/unit/extension-api-port.test.mjs` | ✅ | api 端口路径限定 |
| `tests/guards/extension-module-graph-guard.test.mjs` | ✅ | node 泳道模块图守卫 |
| `tests/guards/extension-plugin-package-guard.test.mjs` | ✅ | `ext-*` 包边界 |
| `tests/component/extension-plugin-surface.component.test.tsx` | ✅ | api/refreshSources/modal 真渲染 |

### 1.3 修改文件

`package.json`、根 `.gitignore`、`features/extension-sdk/{contracts.ts,registry.ts,host.tsx,actions.ts}`、
`features/agent-profile/workspace-plugin.ts`（类名换通用类）、`app/{page.tsx,use-workspace-extensions.ts,globals.css}`、
`tests/guards/{extension-ui-boundary,extension-ui-layout-guard,extension-ui-parity,static-source-policy}.test.mjs`、
`tests/component/extension-ui-host.component.test.tsx`、`test-support/static-source-contracts.mjs`、
`docs/development.md`、`docs/development_zh.md`、`docs/product-and-api.md`、`docs/product-and-api_zh.md`、`CLAUDE.md`、`AGENTS.md`。

### 1.4 SDK 公开面

`contracts.ts` 现有 9 个类型不动其名，另加：
```ts
export type ExtensionApiQuery = Readonly<Record<string, string | number | boolean>>;
export type ExtensionRequestInit = Readonly<{
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: string | FormData;
  headers?: Readonly<Record<string, string>>;
  signal?: AbortSignal;
  query?: ExtensionApiQuery;
}>;
export type WorkspaceExtensionApi = Readonly<{
  requestJson<T>(path: string, init?: ExtensionRequestInit): Promise<T>;
  requestVoid(path: string, init?: ExtensionRequestInit): Promise<void>;
  requestBlob(path: string, init?: ExtensionRequestInit): Promise<Blob>;
  userMessage(error: unknown, fallback: string): string;
}>;
export type WorkspaceExtensionPluginActions = WorkspaceExtensionActions & Readonly<{ api: WorkspaceExtensionApi }>;
```
`WorkspaceExtensionActions` 加一个成员（`openUnderstanding` 逐字不动）：`refreshSources(): Promise<void>;`。
`WorkspaceExtensionProps.actions` 的类型改为 `WorkspaceExtensionPluginActions`。内建插件只用 `openUnderstanding()`，不受影响。

- `api.ts`（宿主专用，**不在插件白名单**）：`extensionApiPath`、`createWorkspaceExtensionApi`、`withExtensionApi`、`EXTENSION_API_TRANSPORT`。
  `api.ts` 故意不给插件：`createWorkspaceExtensionApi(pluginId)` 若能被插件调用，插件 A 就能构造插件 B 的端口。api 只经 `actions.api` 由 host 按 `contribution.pluginId` 注入。写进 `api.ts` 头注释与守卫失败信息。
- `ui.tsx`（插件可 import）：`ExtensionModal`、`type ExtensionModalProps`。
- `registry.ts`：`defineWorkspaceUiRegistry`、`BUILTIN_WORKSPACE_UI_CONTRIBUTIONS`。`workspace-registry.ts`：`WORKSPACE_UI_CONTRIBUTIONS`。`registry.local.ts`（生成）：`LOCAL_WORKSPACE_UI_CONTRIBUTIONS`。

插件 import 白名单（最终）：`../extension-sdk/contracts.ts`、`../extension-sdk/ui.tsx`、`react`、`lucide-react`，外加同包兄弟 `./x.ts(x)`。

### 1.5 插件包形状（交给内网团队的契约）

```
<package-root>/                     # 目录名 ^[a-z][a-z0-9-]*$ 且不以 ext- 开头；落点 features/ext-<目录名>/
  ui-plugin.json                    # 唯一允许的非 TS 文件
  workspace-plugin.ts | .tsx        # 导出 manifest 里 component 指名的具名导出（恰好一个入口文件）
  <任意兄弟 .ts/.tsx>               # 扁平，禁止子目录
```
`ui-plugin.json`：
```json
{ "api_version": "1",
  "contributions": [{
    "id": "ieee_xplore.search.workspace_panel", "plugin_id": "ieee_xplore", "version": "1.0.0",
    "capability": "ui.ieee_xplore.search", "slot": "workspace.side_panel",
    "permission": "notebook:write", "mode": "all", "component": "IeeeSearchEntry" }]}
```
元数据在 manifest、组件在 TS——与内建（元数据在 `registry.ts:45-54`、组件在 `agent-profile/workspace-plugin.ts`）同形；脚本不解析 TypeScript。

## 2. 任务拆分

### T1 — 构建期装配：同步脚本 / 忽略规则 / npm 钩子（opus）

本任务结束时没有任何生产模块 import 生成物，泳道天然绿。

**新增 `scripts/sync-ui-plugins.mjs`**：只 import `node:fs/promises`、`node:path`、`node:url`；路径由 `fileURLToPath(new URL("../", import.meta.url))` 推导。导出纯函数，`main()` 只在作为入口运行时跑（判据用 `process.argv[1]` 与 `import.meta.url` 比对，或 `import.meta.main` 若本 Node 版本支持——实现时确认）：
```js
export function parsePluginRoots(value, cwd)          // ":" 分隔，去空段，relative→resolve(cwd)
export function validateManifest(manifest, packageName) // 抛 Error，信息含包名与字段名；返回 ContractRow[]
export async function inspectPackage(dir)             // { name, entry, manifest, files }；抛 Error
export function renderLocalRegistry(entries)          // registry.local.ts 文本
export function renderContract(builtinRows, localRows) // 契约 JSON 文本
export async function syncUiPlugins({ frontendDir, roots }) // { packages, rows }
```
包校验（任一不满足即抛错并中止整个同步）：
1. 目录名匹配 `/^[a-z][a-z0-9-]*$/` 且不以 `ext-` 开头；两个包同名 → 抛错。
2. 存在 `ui-plugin.json`；`api_version === "1"`；`contributions` 非空数组。
3. 每行 `id`/`plugin_id`/`capability` 匹配 `registry.ts:5` 同一正则 `/^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/`；`version` 非空字符串；`slot ∈ {workspace.side_panel, source.detail_section}`；`permission` ∈ `registry.ts:7-14` 的 6 个；`mode ∈ {all, advanced}`；`component` 匹配 `/^[A-Z][A-Za-z0-9_]*$/`。
4. 存在 `workspace-plugin.ts` 或 `.tsx`（恰好一个）。
5. 目录内不得有子目录（同时拦 `node_modules/`）。
6. 除 `ui-plugin.json` 外只允许 `.ts`/`.tsx`；出现 `package.json`/`.css`/图片/`.d.ts`/`*.test.*` 一律抛错，错误信息写明：插件包只能依赖基座已有依赖（react / lucide-react / SDK），带 package.json 或 node_modules 的包会被 tsconfig 的 `exclude: ["node_modules"]` 漏掉、整个进 next build 类型检查；CSS 无法参与基座样式表，视觉必须复用系统类与 `:root` token。

写入（按目录名字典序）：
- 删除 `features/ext-*`：只删同时满足目录名匹配 `^ext-[a-z][a-z0-9-]*$`、含 `.ui-plugin-origin` 标记文件、且路径在 `frontend/features/` 之下（`path.relative` 判据）的目录；匹配前缀但无标记 → 抛错不删。
- 复制到 `features/ext-<name>/`，写 `.ui-plugin-origin`（源绝对路径 + ISO 时间戳）。
- 写 `features/extension-sdk/registry.local.ts`。空态模板：
  ```ts
  // GENERATED by frontend/scripts/sync-ui-plugins.mjs — do not edit, do not commit.
  // source: SILICON_NOTEBOOK_UI_PLUGINS=<unset>
  import type { WorkspaceUiContribution } from "./contracts.ts";

  export const LOCAL_WORKSPACE_UI_CONTRIBUTIONS: readonly WorkspaceUiContribution[] = [];
  ```
  非空态每包一行 `import { IeeeSearchEntry } from "../ext-ieee-xplore/workspace-plugin.tsx";`，数组逐字段展开（`Component: IeeeSearchEntry`）。
- 读 `../backend/tests/fixtures/ui_extension_contract.json` 取内建行（缺失 → 抛错提示跑 `python3 scripts/generate_ui_extension_contract.py`）；与本地行合并、按 `(plugin_id, version, contribution_id, slot, capability)` 排序（与 `scripts/generate_ui_extension_contract.py:132` 的 `_CONTRIBUTION_SORT_FIELDS` 一致），写 `.local/ui-extension-contract.json`（`JSON.stringify(x, null, 2) + "\n"`）。
- stdout 一行摘要：`sync-ui-plugins: 0 package(s), 1 contribution(s) → .local/ui-extension-contract.json`。

**修改 `package.json`**（只加 key；不动被 `tests/guards/test-runner-config.test.mjs:19-27` 与 `test-location-guard.test.mjs:48-51` 钉死的 `test`/`test:node`/`test:component`）：
```json
"sync:ui-plugins": "node scripts/sync-ui-plugins.mjs",
"postinstall": "npm run sync:ui-plugins",
"predev": "npm run sync:ui-plugins", "prebuild": "npm run sync:ui-plugins", "prestart": "npm run sync:ui-plugins",
"pretest": "npm run sync:ui-plugins", "prelint": "npm run sync:ui-plugins"
```
**修改根 `.gitignore`**（注释说明 `ext-*` 与 `extension-sdk` 不冲突：第 4 个字符 `-` vs `e`）：
```
# 构建期装载的私有 UI 插件（SILICON_NOTEBOOK_UI_PLUGINS）——生成物，绝不入库
/frontend/features/ext-*/
/frontend/features/extension-sdk/registry.local.ts
```
`.local/`（第 16 行）已匹配任意层级，无需新增。

**测试 `tests/unit/sync-ui-plugins.test.mjs`**（Node，`mkdtemp`）：零插件时生成空存根与只含内建行的契约；复制只搬 TS 与 manifest、包内子目录被拒（`node_modules/`、`package.json`、`styles.css` 各拒）；manifest 字段按 registry 同一套规则校验（`slot: "side_panel"`、`plugin_id: "Ieee"`、`mode: "expert"`、缺 `component` 各抛）；`registry.local.ts` 的 import 与条目一一对应且顺序确定；二次同步幂等、移除环境变量后清掉旧 ext 目录；没有 `.ui-plugin-origin` 的 `features/ext-x` 不会被删；契约 JSON 排序键与后端生成器一致。
必须同步：`tests/guards/static-source-policy.test.mjs` 的 `DIRECT_READ_ALLOWLIST`（第 21-70 行）加 `"tests/unit/sync-ui-plugins.test.mjs"`。
守卫（新文件 `tests/guards/extension-plugin-package-guard.test.mjs` 的前两条用例，T6 再扩）：npm 生命周期钩子六个 key 都恰好是 `"npm run sync:ui-plugins"` 且 `sync:ui-plugins` 指向 `scripts/sync-ui-plugins.mjs`；根 `.gitignore` 含上述两行。该文件也进 `DIRECT_READ_ALLOWLIST`。

变异验证：删 `postinstall` → 红；`.gitignore` 的 `ext-*` 改 `ext*` → 红；注释掉子目录检查 → node_modules 用例红。
验证：`npm run sync:ui-plugins`；`node --test tests/unit/sync-ui-plugins.test.mjs tests/guards/extension-plugin-package-guard.test.mjs tests/guards/static-source-policy.test.mjs`；`npm run test && npm run build`；`git status --short` 干净。

### T2 — registry 拆分与合并接线（opus）

- `registry.ts`：第 45 行 `WORKSPACE_UI_CONTRIBUTIONS` 改名 `BUILTIN_WORKSPACE_UI_CONTRIBUTIONS`（内容不变）；文件头注释：本模块被 `node --test` 直接 import，它与它 import 的一切必须是 `.ts`。
- 新增 `workspace-registry.ts`：
  ```ts
  import type { WorkspaceUiContribution } from "./contracts.ts";
  import { BUILTIN_WORKSPACE_UI_CONTRIBUTIONS, defineWorkspaceUiRegistry } from "./registry.ts";
  import { LOCAL_WORKSPACE_UI_CONTRIBUTIONS } from "./registry.local.ts";
  export const WORKSPACE_UI_CONTRIBUTIONS: readonly WorkspaceUiContribution[] =
    defineWorkspaceUiRegistry([...BUILTIN_WORKSPACE_UI_CONTRIBUTIONS, ...LOCAL_WORKSPACE_UI_CONTRIBUTIONS]);
  ```
- `app/page.tsx:213` 与 `app/use-workspace-extensions.ts:7` 仅改 import 路径为 `workspace-registry`。
- 守卫 `extension-ui-boundary.test.mjs`（第 212-287 行）：registry import 规则拆三段（`registry.ts` 只许 `./contracts.ts` + 插件模块、**不许** `./registry.local.ts`；`workspace-registry.ts` 只许 `./contracts.ts`/`./registry.ts`/`./registry.local.ts`；`registry.local.ts` 只许 `./contracts.ts` 与 `/^\.\.\/ext-[a-z0-9-]+\/workspace-plugin\.tsx?$/`，文件缺失 → `assert.fail("registry.local.ts 缺失——先跑 npm run sync:ui-plugins")`）；`countEntries` 在 `registry.ts`（`defineWorkspaceUiRegistry([...])`）与 `registry.local.ts`（顶层数组字面量）分别统计，`workspace-registry.ts` 显式排除；`pluginImports` 为两者之和；保留 `plugins.length === registeredEntries`；新增断言非内建插件模块必须落在 `features/ext-*/`。
- 新增守卫 `tests/guards/extension-module-graph-guard.test.mjs`：① `registry.ts` 的模块闭包全是 `.ts`（自写 resolve：原样→`.ts`→`.tsx`→`/index.ts`；空转保护：闭包含 `features/agent-profile/workspace-plugin.ts`）；② `tests/unit/**` 与 `tests/guards/**` 的 import 说明符没有解析到 `workspace-registry.ts`/`registry.local.ts`/`host.tsx`/`ui.tsx`/`features/ext-*/`。进 `DIRECT_READ_ALLOWLIST`；不得出现 `frontend/xxx.ts:123` 形态字符串、`.getStart()/.pos/.end`、参数名 `node`。
- `extension-ui-parity.test.mjs:5,29` 改对账 `BUILTIN_WORKSPACE_UI_CONTRIBUTIONS`（注释：fixture 只钉内建，合并集合部署期对账）。
- `tests/component/extension-ui-host.component.test.tsx:9,41,47` 改 import 来源；新增用例「零插件时合并 registry 与内建逐字相同」（`toEqual` + 逐字段 + 长度 1）。

变异验证：`registry.ts` 加 `import ... "./registry.local.ts"` → boundary 红；内建插件改名 `.tsx` → module-graph 红；`tests/unit/extension-ui-registry.test.mjs` 加 import `workspace-registry.ts` → 红；手写 `features/notmyext/workspace-plugin.ts` 并在 `registry.local.ts` 引用 → 红；删 `registry.local.ts` 单跑 boundary → 给出"先跑 npm run sync:ui-plugins"的红。

### T3 — SDK api 端口 `actions.api`（opus）

- `contracts.ts`：加 §1.4 四个类型；`WorkspaceExtensionProps.actions` 改 `WorkspaceExtensionPluginActions`。
- 新增 `features/extension-sdk/api.ts`：
  ```ts
  import { requestBlob, requestJson, requestVoid } from "../../app/api-client.ts";
  import { toUserMessage } from "../../app/errors.ts";   // 名字以 errors.ts 实际导出为准
  const PLUGIN_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
  const RELATIVE_PATH = /^(?:\/[A-Za-z0-9][A-Za-z0-9._~-]*)+\/?$/;
  export function extensionApiPath(pluginId, path, query?)        // 见规则
  export function createWorkspaceExtensionApi(pluginId, transport = EXTENSION_API_TRANSPORT)
  export function withExtensionApi(actions, pluginId)
  export const EXTENSION_API_TRANSPORT = Object.freeze({ requestJson, requestVoid, requestBlob, toUserMessage });
  ```
  `extensionApiPath` 规则（不满足即 `throw new TypeError("extension API requests must stay under /extensions/<plugin id>/")`）：`PLUGIN_ID.test(pluginId)`；`RELATIVE_PATH.test(path)`（同时拒绝空串、不以 `/` 开头、绝对 URL、`//`、`..`/`.` 段、反斜杠、空格、`?`/`#`、`%` 编码）；结果 `"/extensions/" + pluginId + path` 并冗余断言 `startsWith("/extensions/" + pluginId + "/")`；`query` 用 `URLSearchParams` 拼接（查询串只走 `query`）。
  `createWorkspaceExtensionApi` 每方法固定 `{ tag: "extension", auth: "required", unauthorized: "clear-and-reload" }`（与 `app/source-api.ts:13` 一致），只透传 `method/body/headers/signal`；插件无法设 `tag/auth/unauthorized/credentials/mode`；注释点明 `api-client.ts:50-52` 会覆盖插件传的 `Authorization`。`userMessage` 委托 transport；头注释写明插件不许读 `error.message`（errors-guard 精确计数）。
- `host.tsx` 第 39-45 行：`actions={withExtensionApi(actions, contribution.pluginId)}`；不引入 hook 或模块级缓存。
- 测试 `tests/unit/extension-api-port.test.mjs`（Node）：合法相对路径拼前缀（含 query、末尾 `/`）；拒绝列表 `["https://evil.example/x","//evil/x","http://127.0.0.1:8000/api/notebooks/x","search","","/","/../notebooks/x","/./x","/a//b","/a\\b","/x?y=1","/x#y","/%2e%2e/notebooks","/ext ra"]` 各 `TypeError`；pluginId `["Ieee","-x","ieee..x","../other",""]` 各抛；假 transport 断言路径与窄 init、`tag === "extension"`、`unauthorized === "clear-and-reload"`、不含插件覆盖；默认 transport 与 `api-client`/`errors` 的函数**引用相等**；`withExtensionApi` 保留 owner 闸且返回对象冻结。
- 守卫：`extension-ui-boundary` 补断言 `api.ts` 的 import 集合恰好 `{"../../app/api-client.ts","../../app/errors.ts","./contracts.ts"}`。

变异验证：`RELATIVE_PATH` 段首放宽含 `.` → `/../notebooks/x` 用例红；改成 `{...init, tag}` 顺序让插件 tag 覆盖 → 透传用例红；`api.ts` 多 import `source-api.ts` → boundary 红。

### T4 — `actions.refreshSources()`（sonnet）

具名 command：`app/use-source-library.ts:271` `loadSourcesPage(input)` 与 `:260` `currentPageRequest()`。
- `contracts.ts`：`WorkspaceExtensionActions` 加 `refreshSources(): Promise<void>`。
- `actions.ts`：`createOwnedWorkspaceExtensionActions(owner, owns, openUnderstanding, refreshSources)`，`refreshSources` 在 `!owner || !owns(owner)` 时静默返回（注释：双闸——这里是 extension owner 闸，`use-source-library.ts:271-277` 还有 notebook 自闸）。
- `page.tsx:4624-4630` 加第 4 实参 `() => sourceLibrary.loadSourcesPage(sourceLibrary.currentPageRequest())` + 理由注释：只用 hook 自己的 command；刻意不顺带 `loadNotebookCollection`/`revalidateAskAvailability`/`reloadCheckup`——新来源 `parse_status` 未 `extracted` 时 `use-source-library.ts:582` 的 `hasPending` 轮询会接手并在 `reachedExtracted`（634-650）跑那三个刷新；塞进来会让插件一次动作变四个请求并与 `source-poll-refresh-guard` 重复。
- 守卫 `extension-ui-boundary.test.mjs:345-349`：期望实参列表改 4 项；加断言 `Home` 作用域下 `sourceLibrary.loadSourcesPage` 调用次数恰 +1、`page.tsx` 的 `useEffect` 总数不变（基线实现时现取并写死，注释说明是"不新增 effect"普查）。
- 组件测试 `extension-ui-host.component.test.tsx` 第 62/65 行 actions 加 `refreshSources: async () => {}`；第 111-127 行两处调用加第 4 实参。
- 新增 `tests/component/extension-plugin-surface.component.test.tsx`：当前 owner 下委托 spy 一次；切库后（generation 1→3）迟到调用 spy 0 次且 promise 正常 resolve；换用户（`owns` 带 `actorId`）同样被拒。

变异验证：删 `refreshSources` 的 owner 闸 → 两条组件用例红；`page.tsx` 第 4 实参换成页面级 `loadSourcesPage(currentNotebookId ?? "", {})` → boundary 红。

### T5 — SDK `ui.tsx`：可拖动浮动弹窗 + 通用入口类（sonnet）

- 新增 `features/extension-sdk/ui.tsx`：`ExtensionModal({ storageKey, title, description?, onClose, children })`，骨架与 `app/conversation-share-modal.tsx:362-464`/`app/groups-panel.tsx:238-758` 同构（`section.utility-modal[role=dialog][aria-modal][aria-label]` → `FloatingModalCard storageKey={`extension.${storageKey}.window`} className="utility-modal-card"` → `div.source-modal-header` 带 `floating.dragHandleProps` + 标题/描述 + `button.icon-button` 关闭 → `div.source-detail-body`）。头注释两条限制：只保证在 `workspace.side_panel` 下正确定位（`source.detail_section` 的宿主本身走 `FloatingModalCard`，`position:fixed` 后代以卡片为包含块；仓库无 `createPortal`，本轮不引入）；不接入 `use-root-modal-coordinator`，生命周期由 outlet 的 `ownerKey` 门承担。
- `app/globals.css:1092-1120`：`.agent-profile-workspace-plugin` 三段规则改名 `.workspace-extension-entry`（声明一字不改）。`features/agent-profile/workspace-plugin.ts:23` className 改 `"button secondary workspace-extension-entry"`。
- 守卫 `extension-ui-layout-guard.test.mjs`：第 39 行前缀改 `.workspace-extension-entry`；第 147-169 行扫描范围扩到 `features/ext-*/` 全部 `.ts/.tsx` + 内建插件 + `features/extension-sdk/ui.tsx`（不许内联 style/颜色字面量）；新增用例「扩展弹窗只用系统弹窗骨架类」（`ui.tsx` 的 className 字面量集合 ⊆ `{utility-modal, utility-modal-card, source-modal-header, source-detail-body, icon-button}` 且非空；`storageKey` 含 `extension.` 前缀）。
- 组件用例（并入 `extension-plugin-surface.component.test.tsx`）：渲染 `role="dialog"`、`aria-label`、header 有 `onPointerDown` 与 `touchAction: none`、× 调 `onClose` 一次；`innerWidth=700` + resize 后卡片 `style.transform` 为空；outlet 因 `ownerKey` 消失卸载时弹窗一并消失。

变异验证：`ui.tsx` header 加 `style={{ color: "#fff" }}` → layout guard 红；`.workspace-extension-entry` 改成颜色声明 → 红；删整个 CSS 块 → 空转保护红。验证另跑 `python3 ../scripts/check_ui_vocabulary.py`。

### T6 — `ext-*` 插件包边界守卫（opus）

在 `tests/guards/extension-plugin-package-guard.test.mjs` 里加 `export function pluginPackageImportOffenders(packagePath, specifiers)`：说明符按 `path.posix.normalize` 解析，允许集合 = 同一 `features/ext-<name>/` 目录内；恰好 `features/extension-sdk/contracts.ts` 或 `features/extension-sdk/ui.tsx`；裸 `react`/`lucide-react`。其余违规，特别点名 `api.ts`（错误信息：api 端口必须由 host 按 contribution.pluginId 注入）。
用例：真值表（`./ieee-model.ts`、`../extension-sdk/contracts.ts`、`../extension-sdk/ui.tsx`、`react`、`lucide-react` → `[]`；`../extension-sdk/api.ts`、`../extension-sdk/registry.ts`、`../../app/api-client.ts`、`../../app/page.tsx`、`../ext-other/x.ts`、`../agent-profile/profile-api.ts`、`next/link`、`axios` → 恰 1 条）；真实 `ext-*`（若存在）全部合规（空集合合法，注释说明分工）；`ext-*` 扁平且只含 TS 与 manifest（`readdir`）；内建插件 import 集合恰为 `{react, lucide-react, ../extension-sdk/contracts.ts}`。
`extension-ui-boundary.test.mjs:249` 的 `PLUGIN_IMPORT_ALLOWLIST` 扩成四项并改为调用共享函数（guard 之间 import，不放 `test-support/`）。
`test-support/static-source-contracts.mjs` 加登记 `workspaceUiPluginPackaging`（category `supply-chain`，roots `["features/ext-*","features/extension-sdk/registry.local.ts","package.json","../.gitignore"]`）。

变异验证：白名单删 `ui.tsx` → 红；把 `api.ts` 加进白名单 → 红；去掉 normalize 改前缀比较 → `["./sub/../../app/page.tsx"]` 红；内建插件加 import `ui.tsx` → 本守卫与 module-graph 守卫都红。验证另跑 `bash ../scripts/check_frontend.sh`。

### T7 — 文档（sonnet）

1. `docs/development.md:517` 与 `docs/development_zh.md:110` 后各追加一条：registry 三模块；`SILICON_NOTEBOOK_UI_PLUGINS` 与 `npm run sync:ui-plugins`；插件包形状；import 白名单四项；`registry.ts` 闭包必须全 `.ts`；复制代码会被 `check_ui_vocabulary.py` 与 9 个 `appSourceModules` 守卫扫到，`errors-guard` 精确计数意味着插件不得读 `error.message`、不得 `new Error("中文")`。
2. `docs/product-and-api.md:2153` 与 `_zh.md:1672`：`GET /api/system/extensions` 那条后加一条：`SILICON_NOTEBOOK_UI_PLUGINS`、`frontend/.local/ui-extension-contract.json` 形状（与 fixture 同形同排序键）、它是部署期对账输入而非运行时依赖；插件路由固定 `/api/extensions/{plugin_id}/…`，浏览器侧由 SDK 端口限定。
3. `CLAUDE.md:339`「Workspace UI registry」bullet 追加一段（构建期注入链、`registry.ts` 保持 `.ts`、插件 import 白名单与 `api.ts` 禁令、不写 CSS/不内联颜色/不读 `error.message`、`refreshSources` 同为 exact-owner 窄 action、仍禁远程 JS 与运行时注册）。
4. `AGENTS.md:81` 同上英文。
验证：`python3 ../scripts/check_ui_vocabulary.py`；中英逐条对应。

## 3. `page.tsx` 接触面

只碰 2 处：`:213` import 路径；`:4629` 第 4 实参 + 注释。不动 `:905-908`、`:4616-4623`、`:5255-5277`、`:6637-6664`、`:2498-2503`。
不新增 effect（boundary 守卫钉 `useEffect` 总数）、不新增请求（`refreshSources` 点击驱动；零插件时四门逐字相同；host 组件测试的 `toHaveBeenCalledTimes` 原样保留）、不碰 owner setter（只传两个具名 command）。

## 4. 风险处置

- R1 `typedRoutes`/TS：无需配置改动（`tsconfig.json:23` include 覆盖 `features/ext-*/**`；`allowImportingTsExtensions: true` 已有先例）。插件不得带 `tsconfig.json`。
- R2 插件 `node_modules`/`package.json`：脚本硬拒绝（原因见 0.3 与双 React 实例）。插件只能用 `react`/`react-dom`/`lucide-react`/SDK；要新依赖走基座 PR。
- R3 `ExtensionModal` 在 `source.detail_section` 下会相对卡片定位：登记限制；本轮只用于 `workspace.side_panel`。
- R4 插件弹窗不接入 root-modal coordinator：登记接受；不扩 `RootModalSlot`。
- R5 内网跑 `npm run test` 时 parity 只对内建；合并结果仍过 `defineWorkspaceUiRegistry` 校验。
- R6 `frontend/scripts/` 新顶层目录：无守卫枚举；"生产代码只放 app/features"约束的是生产代码。
- R7 脚本删目录：三重闸（名字正则、`.ui-plugin-origin` 标记、`path.relative` 在 `features/` 之下）。
- R8 契约 JSON 路径与后端对账脚本耦合：以后端计划为准，改 `renderContract` 落点即可。
- 未决（不阻塞）：`ui-plugin.json.version` 是否强制与后端 `version` 一致——保持各自声明，运行时精确 tuple 比对不一致即静默不显示，部署期对账变响亮。
