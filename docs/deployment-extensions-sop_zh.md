# 部署插件 SOP：开发、接入与运维一个仓库外插件

[English](./deployment-extensions-sop.md) · [返回 README](../README_zh.md)

本文把一个私有特性从空目录带到线上：插件住在**自己的仓库**里，安装在公网 checkout 旁边，以进程内方式装载，公网仓库**一个补丁都不打**。内容覆盖后端 bundle、构建期 UI 包、本地联调、打包、安装、启动校验、升级/回滚，以及完整的拒绝码表。

权威契约仍在原处——[部署与配置 → 部署插件](./deployment-and-configuration_zh.md)、[产品与 API 参考 → 部署插件](./product-and-api_zh.md#部署插件)，以及[开发与仓库契约](./development_zh.md)里的前端 registry 规则。本文不重复它们，只回答「按什么顺序做什么」和「每种失败长什么样」。

## 1. 适用范围与信任模型

```text
公网 checkout（不改一行）                     私有插件仓库
  backend/app/extension_sdk   ◀── import ─── silicon_notebook_corp_search/
  backend/app/extensions      ── 装载 ─────▶   bundle.py   (ExtensionManifest + register)
    经 EXTENSIONS_CONFIG                       routes.py   (APIRouter 工厂)
  frontend/features/extension-sdk             ui/
    ◀── 构建期复制 ───────────────────────     ui-plugin.json
    经 SILICON_NOTEBOOK_UI_PLUGINS             workspace-plugin.tsx
```

manifest 的 `trust` 是三值。`builtin` 随本次构建发布；`deployment` 就是本文的主角——**可信、同进程、仓库外、由部署点名装载**；`isolated` 是留给未来进程隔离档的保留值，registry 当前一律拒绝。

`deployment` 插件**可以**：在任意 core 扩展点注册四类 contribution（Provider / ProviderChain / Contributor / Observer）、把自己的 HTTP 路由挂在 `/api/extensions/{plugin_id}` 之下、声明并供给自己的 capability、贡献工作区 UI、从部署的 TOML 拿一份已校验的 settings、import 自己安装的第三方 Python 包。

`deployment` 插件**不可以**：拿到 repository、全局 `Settings`、model client、FastMCP host 或原始 bearer token；扩展应用生命周期；新增 MCP 工具；自建数据库表；提供匿名路由；发布运行时才拉取的浏览器代码。授权永远不归插件：它触达的每个 core 端口都自己对**请求当前的那个用户**做判定。

信任的意思是「我们认这份代码」，不是「这份代码说了算谁能读哪本笔记本」。

## 2. 前置条件

| 项 | 要求 |
| --- | --- |
| 公网 checkout | 就是部署将要跑的那个 commit。`EXTENSION_API_VERSION` 在 `backend/app/extension_sdk/contracts.py` 里读取——当前是 `"1"`。 |
| Python | ≥ 3.13，且插件装进**后端自己那个 `PYTHON_BIN` 环境**，不是另一个解释器或 venv。 |
| Node.js | ≥ 20 与 npm，与部署构建前端时用的是同一版本。 |
| 前端 manifest api 版本 | `ui-plugin.json` 的 `api_version` 同样必须是 `"1"`（`frontend/scripts/sync-ui-plugins.mjs` 的 `CONTRACT_API_VERSION`）。 |

一份可用的插件仓库布局：

```text
silicon-notebook-corp-search/
├─ src/silicon_notebook_corp_search/
│  ├─ __init__.py
│  ├─ bundle.py            # EXTENSIONS_CONFIG 点名的那个模块级对象
│  └─ routes.py            # APIRouter 工厂
├─ ui/corp-search/         # 一个扁平目录 = 一个 UI 包
│  ├─ ui-plugin.json
│  └─ workspace-plugin.tsx
├─ tests/
├─ pyproject.toml
├─ extensions.local.toml   # 本地联调用，绝不随发布交付
└─ CHANGELOG.md            # 每条都写明支持的 api_version
```

从第一个版本起就把支持的 `api_version` 钉在 `CHANGELOG.md` 里。它是运维判断「插件构建与 core 构建配不配套」的唯一依据，而执行这条的启动检查（`plugin_api_version_unsupported`）只点插件名、不告诉你期望值是多少。

## 3. 第一步：写后端包

### 3.1 最小 bundle

发现流程 import `"<模块路径>:<属性名>"` 并对拿到的对象做结构校验。不需要继承任何东西，也不注册到任何全局表：一个碰巧形状对的模块级对象就是完全合规的。`app.extension_sdk.deployment` 里那两个 Protocol 只是给人看的文档。

```python
# silicon_notebook_corp_search/bundle.py
from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT

from .routes import build_router

_ROUTER = ContributionDeclaration(
    id="corp.search.router",
    point=PLUGIN_HTTP_ROUTER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)


@dataclass
class CorpSearchBundle:
    manifest: ExtensionManifest

    def register(self, registrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(declaration=_ROUTER, implementation=build_router)
        )


BUNDLE = CorpSearchBundle(
    ExtensionManifest(
        id="corp.search",
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Corp literature search",
        trust="deployment",
        contributions=(_ROUTER,),
    )
)
```

*core 替你做的：* import 模块、校验 manifest 确实是 `ExtensionManifest`、`manifest.id` 等于配置里的键、`trust == "deployment"`、`api_version` 与本次构建一致，并在 `register()` 返回后核对**你实际注册的 contribution id 集合等于 manifest 声明的那一份**。任一不符即停机。

*你不能：* 注册 manifest 没声明的 contribution，或声明了却不注册。两者都落成「registrations do not match its manifest」。

### 3.2 settings（可选）

`settings_model` 与 `configure` 是**一对**：要么都声明，要么都不声明。不吃配置的插件两者都不声明，此时部署 TOML 里给它写任何 `[settings]` 表都是启动失败。

```python
from pydantic import BaseModel


class CorpSearchSettings(BaseModel):
    base_url: str
    api_key_env: str = "CORP_SEARCH_API_KEY"   # 环境变量的**名字**，不是密钥本身
    timeout_seconds: int = 20


@dataclass
class CorpSearchBundle:
    manifest: ExtensionManifest
    settings_model: type[BaseModel] = CorpSearchSettings
    settings: CorpSearchSettings | None = None

    def configure(self, settings: CorpSearchSettings) -> None:
        self.settings = settings          # 存下来就返回——这个方法只该有这一行

    def register(self, registrar) -> None:
        ...
```

*core 替你做的：* 自己从 `model_fields`（外加纯字符串 alias）算出可接受的键集合，而不是指望你写 `extra="forbid"`；把 TOML 表校验成一个实例；在 `register` **之前**用它调 `configure`。拒绝信息只带出错的键**名**与异常**类名**——绝不带取值，因为 pydantic 的 `ValidationError` 会把被拒的输入原样回显。

*你不能：* 在 `configure` 里起线程或后台任务、开网络/数据库连接、做阻塞 I/O。它跑在启动组合期，registry 还没冻结、服务还没 ready。这些工作挪到第一次真正需要它的请求里惰性做。

密钥按环境变量名引用（镜像 `model-services.toml` 的 `api_key_env`），不要内嵌。core 从不打印任何 settings 值，但配置文件里的明文密钥离一次 `cat` 进聊天记录只差一步。

*已登记的限制：* pydantic 的 `AliasChoices`/`AliasPath` 形式的 alias **不**会被收进可接受键集合。只匹配这类 alias 的 settings 键会被报成 `plugin_settings_unknown_key` 而不是被接受——方向是 fail-closed，刻意如此。

### 3.3 capability（可选）

manifest 一旦声明 `provides`，bundle 就必须暴露一个 `capability_decisions` 映射，其键**恰好等于** `provides`。这也是你自己的 `requires` / `ui_contributions` 能冻结的前提。

```python
from app.extension_sdk import Availability, AvailabilityStatus
from app.extension_sdk.ui import UiContributionDeclaration


def _corp_search_available(_context: object | None) -> Availability:
    if not BUNDLE.settings:
        return Availability(AvailabilityStatus.DISABLED, "not_configured")
    return Availability.available()


BUNDLE = CorpSearchBundle(
    ExtensionManifest(
        ...,
        provides=("corp.search.available",),
        ui_contributions=(
            UiContributionDeclaration(
                id="corp.search.panel",
                slot="workspace.side_panel",
                capability="corp.search.available",
            ),
        ),
    )
)
BUNDLE.capability_decisions = {"corp.search.available": _corp_search_available}
```

*core 替你做的：* 校验每个名字的形状；拒绝「有 probe 却没声明」和「声明了却没 probe」；拒绝与 core capability 或另一个插件的重名——静默让一个 probe 遮蔽另一个，会让可用性取决于注册顺序。

*你不能：* 在 capability 名里用 `:`。core 自己的 capability 写成 `point:name`，那个拼写是保留的，插件因此永远造不出一个长得像 core 的名字。合法名是小写、以 `.`/`_`/`-` 分隔：`corp.search.available`。可用性每次请求实时求值，所以 probe 必须**零 I/O**——不能靠调一次上游来判断自己可不可用。

### 3.4 HTTP 路由（可选，每个插件至多一个）

工厂收一个 `PluginRouteContext`，返回一个 `APIRouter`。core 把它挂在 `/api/extensions/{plugin_id}` 之下、router 级会话依赖之后。

```python
# silicon_notebook_corp_search/routes.py
from fastapi import APIRouter, Depends

from app.extension_sdk.http import PluginRouteContext


def build_router(context: PluginRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health(actor=Depends(context.current_actor)):
        return {"plugin_id": context.plugin_id, "actor_id": actor.id}

    @router.get(
        "/notebooks/{notebook_id}/candidates",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def candidates(notebook_id: str, q: str = ""):
        return {"items": _upstream_search(context.settings, q)}

    @router.post(
        "/notebooks/{notebook_id}/import",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    def import_selected(notebook_id: str, payload: dict):
        urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)]
        if not urls:
            raise context.user_error(400, "请先选择要导入的文献")
        result = context.url_sources.import_urls(notebook_id, urls)
        context.emit_event({"event": "corp_urls_imported", "count": len(result.created)})
        return {
            "created": [
                {"source_id": row.source_id, "title": row.title, "url": row.url}
                for row in result.created
            ],
            "rejected": [
                {"url": row.url, "reason": row.reason} for row in result.rejected
            ],
        }

    return router
```

八个接缝，仅此而已：`plugin_id`、`settings`、`require_notebook_capability`、`require_notebook_read`、`current_actor`、`user_error`、`url_sources`、`emit_event`。

*core 替你做的：*

- **按端口授权。** `url_sources.import_urls` 对**请求自己的那个用户**核对 `sources:write`——用户从 core 的请求上下文解析，绝不从你传进去的任何东西解析——不通过就用与 core 端点相同的 404 拒绝（不泄露存在性）。过了这一关，它就是 core 自己那个 URL 导入函数本人：同样的容量记账、管理员豁免、未配置解析器映射与后台解析调度。
- **对 `{notebook_id}` 路由的结构性守卫。** 路径里含这个字面子串的路由必须跑 core 自己的一道门（任一能力守卫，或读权门）。这是**纵深防御，不是边界**：把参数改名 `{nb}`、或从请求体里取 id，这道检查就看不见了——而端口照样拒绝。拿掉端口那道检查会开一个洞，拿掉这道不会。
- **401 翻译。** 你的 handler **内部**抛出的 401 会变成 `424`，带 core 自己的文案，并记一条 `plugin_upstream_unauthorized` 事件。在 core 里 401 对浏览器只有一个含义——清 token 并重载——所以某个插件路由里一张过期的上游凭据，否则会把用户从整个产品里登出。core 自己 router 级会话门产生的真 401 仍然原样是 401。
- **事件脱敏。** `emit_event` 只收四个字段——`event`、`outcome`、`count`、`elapsed_ms`——出现别的键就**整条**丢弃。`kind` 与 `plugin_id` 由 core 补。它永远不会反向抛回你的 handler。

*你不能：* 在 router 上挂 startup/shutdown 钩子；加非 `APIRoute` 的路由（挂载子应用、裸 websocket、裸 Starlette route）；声明第二个 router；返回不是 `APIRouter` 的东西。每一条都是启动失败，各有自己的码（见第 9 节）。

### 3.5 其它 contribution 类型

其余四个生产扩展点在 SDK 里是 Protocol；实现该 Protocol、声明匹配的 `ContributionKind`、经对应的 `add_*` 注册即可。

| 扩展点常量 | kind | Protocol | 模块 |
| --- | --- | --- | --- |
| `RETRIEVAL_CONTRIBUTOR_POINT` | `CONTRIBUTOR` | `RetrievalContributor` | `app/extension_sdk/retrieval.py` |
| `PARSER_PROVIDER_CHAIN_POINT` | `PROVIDER_CHAIN` | `ParserChainLink` | `app/extension_sdk/parser.py` |
| `ASK_COMPLETED_OBSERVER_POINT` | `OBSERVER` | `AskCompletedObserver` | `app/extension_sdk/ask.py` |
| `REPORT_COMPLETED_OBSERVER_POINT` | `OBSERVER` | `ReportCompletedObserver` | `app/extension_sdk/report.py` |
| `REPORT_EXPORTER_POINT` | `PROVIDER` | `ReportExporterProvider` | `app/extension_sdk/report_export.py` |

每个扩展点给的是窄的、point-specific 的 context——绝不是万能 service locator——并各自声明了 contribution 必须 `require` 哪些 capability 才拿得到访问端口。动手前先读那份 Protocol 与它的模块 docstring：该扩展点的 fail-open 与取消规则写在那里。

### 3.6 后端红线

- 只 import `app.extension_sdk` 与 `app.domain`。绝不 import 具体 repository、facade、runtime、service 或任何 `app.api` 模块。
- settings 值绝不能进日志、事件、异常消息或响应体。
- `configure()` 不起线程、不开连接。
- capability 名以 `.`/`_`/`-` 分隔；`:` 是 core 的。
- 插件路由除真正的会话失效外不得抛 401——想要自己的措辞，就自己把上游 401 翻成 `502`/`424`。
- 绝不在 `register()` 里 `raise ExtensionRegistryError`。它不在 SDK 公开面上，但 import 得到，而 core **刻意不脱敏**它——你写在那里的消息会逐字进运维日志。`register()` 抛出的其它任何异常都会被转成 `plugin_registration_failed`，只留类名。

## 4. 第二步：写前端包

一个 UI 包是一个**扁平目录**，构建期被复制进 `frontend/features/ext-<包名>/`。它不带任何自己的依赖，也不带 CSS。

### 4.1 `ui-plugin.json`

```json
{
  "api_version": "1",
  "contributions": [
    {
      "id": "corp.search.panel",
      "plugin_id": "corp.search",
      "version": "0.1.0",
      "capability": "corp.search.available",
      "slot": "workspace.side_panel",
      "permission": "source:write",
      "mode": "all",
      "component": "CorpSearchEntry"
    }
  ]
}
```

`id`、`plugin_id`、`capability` 必须与后端 manifest 逐字一致——浏览器只在本地三元组 `(plugin_id, version, contribution_id)` 精确命中一条实时服务端行时才渲染。`slot` 取 `workspace.side_panel` 或 `source.detail_section`；`permission` 取 `notebook:read` / `notebook:write` / `notebook:configure` / `source:read` / `source:write` / `system:admin` 之一；`mode` 取 `all` 或 `advanced`；`component` 是导出的 React 组件名。

### 4.2 `workspace-plugin.tsx`

```tsx
import { useState } from "react";
import { Search } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";
import { ExtensionModal } from "../extension-sdk/ui.tsx";

type Candidate = { id: string; title: string; url: string };

export function CorpSearchEntry({ context, actions }: WorkspaceExtensionProps) {
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function importSelected(urls: readonly string[]) {
    setBusy(true);
    setNotice("");
    try {
      const result = await actions.api.requestJson<{ created: Candidate[] }>(
        `/notebooks/${context.notebook.id}/import`,
        { method: "POST", body: JSON.stringify({ urls }) },
      );
      setNotice(`已导入 ${result.created.length} 篇资料`);
      await actions.refreshSources().catch((error: unknown) => {
        setNotice(actions.api.userMessage(error, "资料已导入，但列表未能刷新，请手动刷新页面"));
      });
    } catch (error) {
      setNotice(actions.api.userMessage(error, "导入失败，请稍后再试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="button secondary workspace-extension-entry"
        onClick={() => setOpen(true)}
      >
        <Search size={16} aria-hidden="true" />
        <span>文献检索</span>
      </button>
      {open ? (
        <ExtensionModal
          pluginId={context.pluginId}
          storageKey="search"
          title="文献检索"
          description="搜索并导入到当前笔记本"
          onClose={() => setOpen(false)}
        >
          {/* 你的面板；`busy` 让提交控件不可点，`notice` 就地上屏 */}
        </ExtensionModal>
      ) : null}
    </>
  );
}
```

*core 替你做的：* 注入一份按**本条 contribution 的** `pluginId` 绑定的 `actions.api`，因此每次请求都限定在 `/api/extensions/<plugin id>/` 之下、你设的 `authorization`/`cookie` 头会被剥掉、`tag`/`auth`/`unauthorized` 由 core 写死。`ExtensionModal` 白送系统弹窗外壳、标题栏拖动，以及一格按插件分段的窗口位置记忆。

*你不能：*

| 规则 | 为什么 |
| --- | --- |
| import `../extension-sdk/contracts.ts`、`../extension-sdk/ui.tsx`、裸 `react`、裸 `lucide-react` 与同包兄弟模块**之外**的任何东西 | 更宽的面在仓库外无法评审。 |
| import `../extension-sdk/api.ts` | `createWorkspaceExtensionApi(pluginId)` 一旦到插件手里，插件 A 就能造出插件 B 的端口，路径限定当场失去意义。 |
| `setTimeout` / `setInterval`（裸写、`window.`、`globalThis.` 三种写法）、动态 `import(`、`new WebSocket` / `EventSource` / `XMLHttpRequest`、`navigator.sendBeacon` / 裸 `sendBeacon` | 构建期评审看不见的后台通道。AST 守卫扫包内**每一个** `.ts`/`.tsx`，不只是入口。 |
| `fetch(` | 全仓 `api-boundary` 守卫本就普查每个模块；`actions.api` 是唯一被认可的 I/O 出口。 |
| 读 `error.message` / `.error` / `.error_message`，或 `throw new Error("中文…")` | `errors-guard` 是精确计数普查，它的 `APPROVED_*` 清单住在公网仓库里，仓库外的包登记不进去。用 `api.userMessage(error, fallback)`。 |
| 放 `.css`、`package.json`、`node_modules` 或任何子目录 | CSS 无法参与基座样式表；依赖树会被整个卷进 `next build` 的类型检查（`exclude: ["node_modules"]` 只覆盖 `frontend/node_modules`），还会引入第二个 React 实例。新依赖走基座 PR。 |
| 写颜色字面量 | 复用既有类与 `:root` token。`extension-ui-layout-guard` 两侧都钉。 |
| 把 `actions` 或 `context` 放进 `useEffect`/`useMemo` 的依赖数组 | 两者每帧都是新对象（owner 闸每次渲染现冻结）。`actions.api` 按 `pluginId` 记忆，放进依赖数组是安全的。 |
| 让 `refreshSources()` 的 rejection 没人接 | owner 闸已关闭时它静默 resolve（那不是错误，只是这次刷新已经没有意义），真正的加载失败才 reject。只在自己的动作完成后调**一次**，并 `catch` 住。 |
| 从 `source.detail_section` 开弹窗 | 那个 slot 的宿主自己就是一个浮动卡片，`ExtensionModal` 的 `position: fixed` 会相对宿主卡片而不是视口定位，弹窗会跟着来源详情窗跑。已登记的限制；本轮只在 `workspace.side_panel` 开弹窗。 |

包内文件：恰好一个 `ui-plugin.json`、恰好一个 `workspace-plugin.ts` **或** `.tsx`、任意多个扁平的兄弟 `.ts`/`.tsx` 模块。`.d.ts` 与 `*.test.*` 一律拒绝。包根下以 `.` 开头的**普通文件**（`.DS_Store`、`.gitignore`、编辑器残留）跳过并在 stderr 记一行；**子目录一律拒绝，`.git` 也不例外**——所以 UI 包要放在自己的目录里，不要直接放在仓库根。

## 5. 第三步：本地联调

拿公网 checkout 当运行环境，它一个字都不改。

### 5.1 后端

```bash
# 在公网 checkout 里；PYTHON_BIN 就是后端跑的那个解释器
"$PYTHON_BIN" -m pip install -e /path/to/silicon-notebook-corp-search
```

或者不安装，把插件的 `src/` 放进你启动那个进程的 `PYTHONPATH`。

写一份插件仓库自己留着的本地配置：

```toml
# extensions.local.toml
[extensions."corp.search"]
bundle = "silicon_notebook_corp_search.bundle:BUNDLE"
enabled = true

[extensions."corp.search".settings]
base_url = "https://search.corp.internal/api"
api_key_env = "CORP_SEARCH_API_KEY"
timeout_seconds = 20
```

用 `EXTENSIONS_CONFIG` 指向它（相对路径按仓库根解析）：

```bash
EXTENSIONS_CONFIG=/path/to/extensions.local.toml npm run dev
```

### 5.2 前端

```bash
cd frontend
SILICON_NOTEBOOK_UI_PLUGINS=/path/to/silicon-notebook-corp-search/ui/corp-search \
  npm run sync:ui-plugins
```

这个变量是 `:` 分隔的包目录清单，相对路径按当前目录解析。你很少需要手动跑同步——`postinstall` 与五个 `pre*` 钩子（`predev`、`prebuild`、`prestart`、`pretest`、`prelint`）会替你跑，所以在执行 `npm run dev` / `build` / `test` / `lint` 的那个 shell 里导出变量就够了。

它产出三份产物，全部被 `.gitignore` 忽略：

| 产物 | 是什么 |
| --- | --- |
| `frontend/features/ext-<包名>/` | 校验通过的插件包副本，外加一个 `.ui-plugin-origin` 标记，授权后续同步安全删除它。 |
| `frontend/features/extension-sdk/registry.local.ts` | 本地 contribution 清单（零插件时是一个空数组）。 |
| `frontend/.local/ui-extension-contract.json` | 部署期对账输入：后端内建 fixture 的行，拼上各个包 `ui-plugin.json` 的行。 |

同步刻意分两相位：所有可能失败的事（读内建契约、逐个校验输入包、勘察既有 `ext-*` 目录、渲染两份产物文本）全部在动文件树**之前**完成。

### 5.3 在插件仓库里跑的检查

```bash
# 1. 插件自己的测试
"$PYTHON_BIN" -m pytest tests -q

# 2. core 对自己执行的那条中文文案保证，套在你的树上
python3 /path/to/public-checkout/scripts/check_ui_vocabulary.py \
  --extra-root /path/to/silicon-notebook-corp-search/src

# 3. 同步之后，用基座那五个扩展守卫扫被复制进来的包
#    （模块图、插件包边界、UI 边界、版式、跨栈 parity）
cd /path/to/public-checkout/frontend
node --test tests/guards/extension-*.test.mjs

# 4. 完整类型检查，覆盖 features/ext-*/
npm run build
```

**装了插件的树跑不过基座的 `npm run test`。** `extension-ui-host.component.test.tsx` 钉住的是「零插件时合并出来的 registry 逐字等于内建目录、长度为 1」——那正是 registry 拆成两个模块要证明的唯一性质，绝不能为了迁就本地插件被放宽成 `>= 1`。你的验收判据是上面第 3、4 项加第 7 节的对账，不是 `npm run test`。

`--extra-root` 只加宽词汇守卫的扫描面：它按同一份黑名单、同一条 `SANCTIONED_UI` 放行规则扫你这棵树的 `**/*.py` 里的 `user_error(...)` 文案，其它每一项检查与每一行输出都逐字不变。

### 5.4 端到端手测

1. 起后端与前端，登录。
2. 入口行出现在来源栏固定区（滚动的来源列表之上）。没出现就按第 7 节那四道可见性闸逐条查。
3. 打开它、跑一次你的动作、导入一篇；来源列表随之刷新。
4. 用系统管理员打开 `/admin/extensions`——你的插件在列表里，带版本、信任档「部署装入」、服务端接入与界面接入。
5. 看事件日志：你的记录只有 `kind`、`plugin_id` 与白名单里的计数。

## 6. 第四步：打包与交付

交付四样东西，且只有四样：

1. **一个 Python wheel**（内网 index，或目标机能 `pip install` 的路径）。它不得对本仓库声明依赖——SDK 从后端环境里 import，不是 vendored 进来的。
2. **UI 包目录**：放进 wheel 的 data files、单独一个 tarball，或一个目标机自己解开的内网 npm 包。到达时必须是一个扁平目录，名字就是部署将要指向的那个。
3. **一段 TOML**，供粘进部署的 `extensions.toml`，外加 settings 引用到的环境变量**名**（上例是 `CORP_SEARCH_API_KEY`）——绝不给取值。
4. **一条 `CHANGELOG.md`**，写明支持的 `api_version`、插件 `version`（必须与 `ui-plugin.json` 的 `version` 一致，否则浏览器那道三元组检查会把 contribution 藏掉），以及任何 settings 键的变化。

## 7. 第五步：在部署上安装

```bash
# 1. 公网 checkout，不改一行
cd /srv/silicon-notebook && git pull

# 2. 插件，装进后端自己的解释器
"$PYTHON_BIN" -m pip install /tmp/silicon_notebook_corp_search-0.1.0-py3-none-any.whl

# 3. 部署配置
sudo install -m 0640 -o silicon -g silicon \
  /tmp/extensions.toml /etc/silicon-notebook/extensions.toml
# 写进根 .env（或服务的环境）：
#   EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml
#   SILICON_NOTEBOOK_UI_PLUGINS=/srv/silicon-notebook-plugins/corp-search

# 4. 构建并启动；两个变量都必须在这个进程的环境里
export EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml
export SILICON_NOTEBOOK_UI_PLUGINS=/srv/silicon-notebook-plugins/corp-search
npm run start

# 5. readiness
curl -s http://127.0.0.1:8000/api/ready
```

`npm run start` 在把两个服务转后台之前，会在前台先跑 `npm ci`（它的 `postinstall` 同步 UI 包）再跑 `npm run build`（它的 `prebuild` 再同步一次）。两者都继承这个 shell 的环境，所以第 4 步之前导出 `SILICON_NOTEBOOK_UI_PLUGINS` 正是让插件进入构建的那一步。用了 `SKIP_INSTALL=1` 或 `SKIP_BUILD=1` 就得自己负责先跑过同步。

### 启动校验，逐阶段

每一条拒绝都是**启动失败**：进程宁可起不来，也不半接线地跑起来。全部经 logger `silicon_notebook.extensions` 记一行，带插件 id、稳定 reason code 与异常**类名**——绝不带 settings 值、模块路径、文件路径或上游异常文本。

| 阶段 | 跑什么 | 失败长什么样 |
| --- | --- | --- |
| 发现 | 读 TOML、校验整体形状，再按 plugin id 顺序逐条校验 | `extension configuration rejected: config_*`（此时还没有 plugin id）或 `extension 'corp.search' rejected: plugin_*` |
| 导入 | 对 bundle spec 做 `importlib.import_module`，再 `getattr` | `plugin_module_import_failed (ModuleNotFoundError)`——类名就是全部诊断 |
| 身份 | manifest 类型、id 相符、`trust`、`api_version` | `plugin_api_version_unsupported`——core 升级把 `EXTENSION_API_VERSION` 推进了，装配套的插件构建 |
| settings | 键集合、pydantic 校验、`configure()` | `plugin_settings_unknown_key keys=['timout_seconds']` |
| 注册 / 冻结 | `register()`、声明对账、capability 合并、依赖排序 | `plugin_registration_failed (ConnectionError)`——多半是本该放在 `configure` 之后惰性做的工作漏进了 `register` |
| 路由 | 收集路由 contribution、构建并挂载 | `PluginRouteMountError: corp.search: plugin_route_missing_notebook_gate` |
| ready | 迁移、warm-up、索引预载 | 与插件无关 |

`extension discovery FAILED — service will not start` 那一行点名插件、reason 与异常类。日志面刻意就这么窄；第 9 节把每个码翻成一个动作。

### 把前端构建与本部署的实时拓扑对账

已入库的 `backend/tests/fixtures/ui_extension_contract.json` 是在 `EXTENSIONS_CONFIG` 清空的前提下生成的，所以 CI 从看不见任何站点的插件。要回答「我刚构建的这份前端，还配得上这里真正会装载的插件吗」，就在站点自己的环境里跑：

```bash
EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml PYTHONPATH=backend \
  python3 scripts/check_deployment_extension_parity.py \
  --frontend-contract frontend/.local/ui-extension-contract.json
```

| 退出码 | 含义 |
| --- | --- |
| `0` | 一致——实时拓扑与前端契约相符。 |
| `1` | 漂移——stderr 上逐行 diff（只含五个 wire 字段）。通常是只在一侧 bump 了版本，或构建时 `SILICON_NOTEBOOK_UI_PLUGINS` 里漏了某个 UI 包。 |
| `2` | 用法/环境错误——`--frontend-contract` 缺失或畸形、`api_version` 不是 `"1"`，或本部署自己的发现/registry 组合失败（只打印插件 id 与稳定 reason）。 |

该脚本端到端只读：不写文件、不构造 `Repository`、不发网络请求。`check_contracts.sh` 刻意不跑它——CI 没有可对账的部署。

### 验收清单

- `/api/ready` 报 ready。
- `GET /api/admin/extensions`（浏览器里是 `/admin/extensions`，仅系统管理员）列出插件，版本与 contribution 符合预期。
- 工作区入口渲染出来。它需要**四道闸**同时为真：本地三元组 `(plugin_id, version, contribution_id)` 命中一条服务端行；该行 `available` 为 `true`；核心权限快照授予 manifest 声明的 `permission`；`mode` 是 `all` 或该用户的界面模式是高级模式。
- 一次真实动作端到端跑通（比如导入一篇，来源列表随之刷新）。
- 事件日志里你的记录只有 `plugin_id` 与计数——没有标题、id、问题或异常文本。
- 对账脚本退出码为 `0`。

## 8. 升级、回滚、停用

| 情形 | 做法 |
| --- | --- |
| core 升级，`EXTENSION_API_VERSION` 没变 | `git pull`，带着 `SILICON_NOTEBOOK_UI_PLUGINS` 重新构建前端，重启。插件侧什么都不用动。 |
| core 升级，`EXTENSION_API_VERSION` 变了 | 启动直接拒绝、报 `plugin_api_version_unsupported` 并点名插件。**先**装上支持新版本的插件构建，再升 core。没有兼容窗口。 |
| 插件升级 | 装新 wheel、替换 UI 包目录、重新构建前端、重启。manifest 与 `ui-plugin.json` 的 `version` 必须**一起**bump——不一致会让浏览器那道三元组检查把 contribution 藏掉，而后端仍然列着它。 |
| 回滚 | 装回上一版 wheel 与 UI 包，重启。或者把该条目设 `enabled = false` 后重启——标了 `enabled = false` 的条目连 import 都不会发生。 |
| 临时停用 | TOML 里 `enabled = false`，重启。**不要**为了停用一个插件而清空 `EXTENSIONS_CONFIG`：那是把所有插件的发现/registry 组合一次性换成另一份。 |

**以上每一种都是重启进程。** 刻意不做热更新：registry 在启动时冻结，装载出来的拓扑才是一个在进程生命周期内成立的事实，而不是每次请求都要重新推导的移动目标。

离线 CLI（`scripts/batch_ingest.py` 等）构建同一个 runtime，因而装载同一份插件拓扑。批处理任务卡在某个插件上时，修的是配置文件——绝不是「这一次跑就把变量清掉」，那会悄悄给这个任务一份与服务不同的组合。

## 9. 拒绝码表

下面每个码都是稳定的，会逐字出现在启动日志里，至多携带一个插件 id、出错的键**名**与一个异常类名。

### 发现——文件与条目形状（`app/extensions/discovery.py`）

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `config_unreadable` | `EXTENSIONS_CONFIG` 指向的文件打不开 | 检查路径、属主与权限位。路径本身绝不回显——它可能是部署细节。 |
| `config_invalid_toml` | 文件不是合法 TOML | 本地解析一遍；解析器的原始消息刻意不转发。 |
| `config_unknown_top_level_key` | 出现了 `extensions` 之外的顶层键 | 消息会列出这些键名。唯一合法的顶层表是 `[extensions]`。 |
| `config_extensions_not_a_table` | `extensions` 不是表 | 用 `[extensions."<id>"]`，不是数组。 |
| `plugin_id_invalid` | 表键不是稳定 id | 小写、以 `.`/`_`/`-` 分隔：`corp.search`。 |
| `plugin_entry_not_a_table` | `extensions.<id>` 不是表 | 多半写成了 `corp.search = "..."`。 |
| `plugin_unknown_key` | 出现了 `bundle` / `enabled` / `settings` 之外的键 | 消息会列出键名；拼错落在这里。 |
| `plugin_enabled_not_bool` | `enabled` 不是 TOML 布尔 | 写 `true` / `false`，不加引号。 |

### 发现——装载 bundle

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_bundle_missing` | 没有 `bundle` 键 | 补 `bundle = "module.path:ATTRIBUTE"`。 |
| `plugin_bundle_spec_invalid` | 不是恰好一个 `:`、模块名为空，或属性名不是合法标识符 | 形状是 `module.path:ATTRIBUTE`。 |
| `plugin_module_import_failed` | import 抛异常 | 异常类名就是诊断：`ModuleNotFoundError` = 没装进 `PYTHON_BIN` 的环境；其它 = 插件 import 期代码自己出错。 |
| `plugin_attribute_missing` | 模块没有该属性（或它的 `__getattr__` 抛了） | 核对属性名，并确认它是模块级的。 |
| `plugin_not_a_bundle` | `manifest` 不是 `ExtensionManifest`，或 `register` 不可调用 | 通常是插件按另一版 SDK 构建的，或 manifest 用了 dataclass 的副本。 |
| `plugin_id_mismatch` | `manifest.id` ≠ 配置里的键 | 改成一致；配置键是权威。 |
| `plugin_trust_not_deployment` | `trust` 不是 `"deployment"` | 只有 `deployment` 能这样装载。`isolated` 是保留值，当前处处拒绝。 |
| `plugin_api_version_unsupported` | `manifest.api_version` ≠ 本次构建的 `EXTENSION_API_VERSION` | 装与这个 core 构建配套的插件构建。 |
| `plugin_module_import_interrupted` | 插件 import 期收到 `KeyboardInterrupt`/`SystemExit` | 不是拒绝——信号照常传播。这行只是为了留下「解释器当时在哪个插件的 import 里」。 |

### 发现——settings

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_settings_not_a_table` | `settings` 不是表 | 用 `[extensions."<id>".settings]`。 |
| `plugin_settings_not_accepted` | 给一个既没有 `settings_model` 也没有 `configure` 的插件写了 `[settings]` 表 | 删掉这张表，或给 bundle 补上两半。 |
| `plugin_settings_binding_missing` | `settings_model` / `configure` 只声明了其中一个 | 它们是一对。补上另一个，或两个都删。 |
| `plugin_settings_model_invalid` | `settings_model` 不是 pydantic `BaseModel` 子类 | 用 `pydantic.BaseModel`。 |
| `plugin_settings_unknown_key` | 模型不接受的键 | 消息列出键名。只匹配 `AliasChoices`/`AliasPath` 的键也落在这里（已登记的限制，方向是 fail-closed）。 |
| `plugin_settings_invalid` | pydantic 拒绝了这张表 | 只显示异常类名，因为 `ValidationError` 会回显被拒的取值。本地拿同一个模型校验一遍。 |
| `plugin_settings_binding_failed` | `configure()` 抛异常 | 几乎必然是不该放在那里的工作——一次连接、一个线程、一次上游探测。挪到首次使用。 |

### 发现——capability

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_capability_declaration_invalid` | `provides` 非空却没有 `capability_decisions`、它不是 Mapping、迭代时抛异常，或某个 probe 不可调用 | 给一个普通的 `dict[str, AvailabilityProbe]`。 |
| `plugin_capability_name_invalid` | 名字不是稳定 id | 小写、`.`/`_`/`-` 分隔。`:` 是 core 的。 |
| `plugin_capability_not_declared` | 有 probe 的名字不在 `provides` 里 | 消息列出名字。要么加进 `provides`，要么删掉 probe。 |
| `plugin_capability_missing_decision` | `provides` 里的名字没有 probe | 消息列出名字。 |
| `plugin_capability_conflicts_core` | 与 core 的 capability 重名 | 改名。core 自己的名字都是 `point:name`，所以这通常意味着字面重复。 |
| `plugin_capability_conflicts_plugin` | 与另一个插件的重名 | 用自己的前缀做命名空间。 |

### 注册

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_registration_failed` | `register()` 抛出了 `ExtensionRegistryError` 之外的任何东西 | 只显示异常类名——一个手里握着 API key 的 bundle 不能靠 traceback 把它漏出去。本地复现。 |
| *（不脱敏）* `extension '<id>' registrations do not match its manifest` | 你实际注册的 contribution id 集合 ≠ manifest 声明的 | core 自己的诊断，逐字保留（它只由已校验的 id 拼成）。把两个集合改成相同。 |

### 路由收集（`app/extensions/http_router.py`）

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_router_kind_invalid` | 路由声明的 kind 不是 `CONTRIBUTOR` | 声明 `ContributionKind.CONTRIBUTOR`，并用 `add_contributor` 注册。 |
| `plugin_router_trust_denied` | 一个 `builtin` bundle 贡献了路由 | core 端点必须留在 `app/api/*_routes.py`，受冻结的 `api_contract` fixture 管辖。 |
| `plugin_router_multiple` | 一个插件声明了第二个 router | 两者会挂在同一个前缀下、按注册顺序互相遮蔽。一个前缀，一个 router。 |
| `plugin_router_factory_invalid` | 注册的实现不可调用 | 注册工厂函数本身，不是它的返回值。 |

### 路由挂载（`app/api/extension_routes.py`）

| 码 | 含义 | 怎么修 |
| --- | --- | --- |
| `plugin_router_not_a_router` | 工厂返回的不是 `APIRouter` | 返回 `APIRouter()`；别返回 app 或一个路由列表。 |
| `plugin_route_lifecycle_denied` | router 上挂了 `on_startup`/`on_shutdown` | 那会跑在应用 lifespan 里，紧挨着迁移与 warm-up，既没预算也没有失败隔离。惰性做。 |
| `plugin_route_unsupported_kind` | 出现了非 `APIRoute` 的路由 | 挂载的子应用、裸 websocket、裸 Starlette route 都逃过依赖检查，它们的笔记本门无法被证明。 |
| `plugin_route_missing_notebook_gate` | 路径里含 `{notebook_id}` 却没跑 core 的任何一道门 | 加 `Depends(context.require_notebook_read)` 或 `Depends(context.require_notebook_capability("<能力>"))`。把 core 的门包在自己的依赖里同样算数——扫描是传递的。 |

### 运行期

| 信号 | 含义 |
| --- | --- |
| `plugin_upstream_unauthorized` 事件，客户端看到 `424` | 你的 handler 抛了 401。core 翻译了它，免得浏览器把用户登出。想要自己的措辞，就自己翻译上游 401。 |
| `url_sources.import_urls` 返回 404 | 调用用户对该笔记本没有 `sources:write`——或者该笔记本不存在。两者刻意不可区分。 |
| 你的事件在日志里静默消失 | 载荷带了 `event`/`outcome`/`count`/`elapsed_ms` 之外的字段、稳定码超过 64 字符或不匹配 `^[a-z][a-z0-9_]{0,63}$`、或 `count`/`elapsed_ms` 不是 `0..1e9` 区间的整数（`True` 不算 `1`）。整条记录被丢弃，而不是写一半。 |
| 入口不渲染，但 `/admin/extensions` 列着这个插件 | 第 7 节那四道可见性闸有一道为假。先看 `GET /api/system/extensions`：该行的 `available` 与 `unavailable_reason`（`disabled` = 你的 probe 返回了 `DISABLED`，`unavailable` = 返回了 `UNAVAILABLE`）。如果该行整个不存在，说明浏览器侧的本地三元组没命中——两边 manifest 的 `version` 漂了。 |

## 10. 刻意不支持

| 不支持 | 为什么，以及改用什么 |
| --- | --- |
| 热更新 | registry 在启动时冻结，拓扑因此是一个事实而不是每次请求都要问一遍的问题。启停与升级一律重启。 |
| 进程隔离插件 | `trust="isolated"` 是 registry 当前一律拒绝的保留值。部署插件就是可信的同进程代码。 |
| 插件自建数据库表 | schema 是一套带版本、带校验和、参与正向复制的封闭集合。用给你的接缝做持久化，或把状态留在自己的上游。见模块化插件架构设计稿 §10。 |
| 匿名插件路由 | 挂载点恒带 router 级会话依赖。免登录公开页是核心的产品决定，不是插件能开的。 |
| 扩展 MCP 工具目录 | `PUBLIC_TOOLS` 是一份 core 拥有的冻结清单，静态文档与 smoke guard 都从它派生。不向插件开放。 |
| 插件 CSS、插件依赖、远程浏览器代码 | 视觉复用既有类与 `:root` token；新依赖走基座 PR；浏览器绝不在运行时拉取插件 JavaScript。 |

## 11. 检查清单

### 插件开发者

- [ ] `manifest.id` 等于 TOML 的键，`trust="deployment"`，`api_version` 等于目标构建的。
- [ ] `settings_model` 与 `configure` 要么都在、要么都不在；`configure` 只存不做别的。
- [ ] 密钥按环境变量名引用，绝不内嵌。
- [ ] `provides` 与 `capability_decisions` 的键集合完全相同；probe 零 I/O。
- [ ] 每条 `{notebook_id}` 路由都带 core 的门；没有任何地方抛裸 401。
- [ ] 后端 import 只有 `app.extension_sdk` 与 `app.domain`。
- [ ] UI 包是扁平的：一个 `ui-plugin.json`、一个入口文件、只有 `.ts`/`.tsx` 兄弟，无 CSS、无 `package.json`。
- [ ] `plugin_id` / `id` / `capability` / `version` 与后端 manifest 逐字一致。
- [ ] UI 侧不 import 白名单之外的任何东西——尤其绝不 import `api.ts`。
- [ ] 无计时器、无动态 `import(`、无 socket、无 `fetch(`、不读 `error.message`、无颜色字面量。
- [ ] `ExtensionModal` 收到 `pluginId={context.pluginId}` 与一个 `storageKey`。
- [ ] `refreshSources()` 只在动作完成后调一次，且 rejection 被 catch 住。
- [ ] `check_ui_vocabulary.py --extra-root <src>` 干净。
- [ ] 同步后，基座的 `extension-*` 守卫与 `npm run build` 干净。
- [ ] `CHANGELOG.md` 写明支持的 `api_version`；两处 manifest 的 `version` 一起 bump 过。

### 运维

- [ ] 公网 checkout 未改动，且停在预期 commit。
- [ ] 插件装进了后端自己那个 `PYTHON_BIN` 环境。
- [ ] `extensions.toml` 只有属主可读，且不含明文密钥。
- [ ] `EXTENSIONS_CONFIG` 与 `SILICON_NOTEBOOK_UI_PLUGINS` 都在「构建并启动服务的那个进程」的环境里。
- [ ] 启动没有产生任何 `silicon_notebook.extensions` 的 error 行。
- [ ] `/api/ready` 报 ready。
- [ ] `check_deployment_extension_parity.py` 退出码为 `0`。
- [ ] `/admin/extensions` 列出预期的插件、版本与接入项。
- [ ] 一次真实用户动作端到端跑通。
- [ ] 回滚路径已经写下来：上一版 wheel + 上一版 UI 包，或 `enabled = false`，再加一次重启。
