import type { ComponentType } from "react";

import type { UiMode } from "../../app/ui-mode.ts";


export type WorkspaceExtensionSlot =
  | "workspace.side_panel"
  | "source.detail_section";

export type WorkspaceExtensionPermission =
  | "notebook:read"
  | "notebook:write"
  | "notebook:configure"
  | "source:read"
  | "source:write"
  | "system:admin";

export type WorkspaceExtensionModePolicy = "all" | "advanced";

/**
 * 本 contribution 的弹窗在 root-dialog 裁决里的当前位置，由 host 注入。
 *
 * 结构上与 `app/use-root-modal-coordinator.ts` 的 `RootModalView` 相同，但**刻意各写
 * 一份**：SDK 不 import 那个协调器（它是 React 客户端模块，而 SDK 合同要留在 node
 * 泳道装得下的闭包里），插件也不该知道核心的 slot 词表长什么样。
 *
 *  · `open`     —— 只对**当前持有那一格的那条 contribution** 为真。别的 contribution
 *                  （含同一个插件注册的其它条目）拿到的恒是一份 closed view，所以不会
 *                  有两个弹窗同时挂出来。
 *  · `topmost`  —— 为假时上面还盖着别的 dialog（例如 `info` 确认框），此时弹窗必须
 *                  退出交互树（`ExtensionModal` 会加 `inert` + `aria-hidden`）。
 *  · `zIndex`   —— 协调器算出的层级，`ExtensionModal` 原样落到 DOM。
 */
export type WorkspaceExtensionDialogView = Readonly<{
  open: boolean;
  topmost: boolean;
  zIndex: number;
}>;

/**
 * 关闭弹窗的理由。刻意只有两个值：核心那张 policy 表给 `extension` 记的是
 * `backdrop: true` / `escape: false`，多写一个 `"escape"` 会让插件以为它能生效。
 */
export type WorkspaceExtensionDialogCloseReason = "button" | "backdrop";

export type WorkspaceExtensionPermissionSnapshot = Readonly<{
  notebookRead: boolean;
  notebookWrite: boolean;
  notebookConfigure: boolean;
  sourceRead: boolean;
  sourceWrite: boolean;
  systemAdmin: boolean;
}>;

export type WorkspaceExtensionContext = Readonly<{
  /**
   * 本 contribution 的插件身份，**由 host 注入**（`contribution.pluginId`），不是壳层
   * 构造 context 时给的——壳层那份是 `Omit<WorkspaceExtensionContext, "pluginId">`
   * （见 `host.tsx` 的 outlet props），一个 outlet 会渲染多条来自不同插件的
   * contribution，身份只可能逐条补。
   *
   * 插件拿它有且只有一个用途：`ExtensionModal` 的 `pluginId`。窗口位置记忆的存储键
   * 里必须有它，否则两个插件都写 `storageKey="search"` 就共用一格记忆、互相顶掉。
   *
   * ⚠ 与 `context` 整体一样，这是**每帧新对象**上的一个字段——`pluginId` 的**取值**
   * 跨渲染稳定，但 `context` 本身不能进依赖数组（`actions.api` 才是那份稳定引用）。
   */
  pluginId: string;
  slot: WorkspaceExtensionSlot;
  actor: Readonly<{
    id: string;
    username: string;
    displayName: string;
  }>;
  notebook: Readonly<{
    id: string;
    name: string;
  }>;
  source: Readonly<{
    id: string;
    notebookId: string;
    title: string;
  }> | null;
  uiMode: UiMode;
  permissions: WorkspaceExtensionPermissionSnapshot;
  /**
   * 本 contribution 的弹窗现在处于什么位置，**由 host 注入**（与 `pluginId` 同一条
   * 路径：壳层给的那一半是 `Omit<…, "pluginId" | "dialog">`）。插件不持有弹窗的开关
   * 状态——它调 `actions.openDialog()` 提出请求，是否真的开由核心的 root-dialog 裁决
   * 决定（拿不到 workspace owner、被更晚的 primary 顶掉、切库/换用户都会把它关掉）。
   *
   * 插件把整个 `context` 交给 `ExtensionModal` 即可，不必自己读这三个字段。
   */
  dialog: WorkspaceExtensionDialogView;
}>;

export type WorkspaceExtensionActions = Readonly<{
  openUnderstanding(): void;
  /**
   * 重取当前笔记本的来源列表首页（复用打开时的分页/搜索状态）。窄命令，不是
   * setter：宿主侧只用 `use-source-library.ts` 自己的具名 command
   * `loadSourcesPage(currentPageRequest())`，插件拿不到列表状态本身。
   *
   * **返回的 promise 会 reject，插件必须 catch。** 两种结局刻意不同：
   *  · 被 owner 闸拒绝（切库/换用户之后拿着旧回调调它，或宿主侧 notebook 闸判否）
   *    ——静默 resolve。那不是错误，是这次刷新已经没有意义了，不该给用户弹东西。
   *  · 核心加载真的失败（列表请求抛错且该请求仍是当前请求）——**reject**，异常原样
   *    冒出来。插件要 `catch` 并用 `api.userMessage(error, fallback)` 出文案；直接读
   *    `error.message` 会被 `errors-guard` 的精确计数普查拦下（见 `api.ts` 头注释）。
   *    不 catch 就是一条无人处理的 promise rejection。
   */
  refreshSources(): Promise<void>;
  /**
   * 认领那唯一一格插件弹窗。**壳层持有的这一份收 `contributionId`**——一个 outlet 会
   * 渲染多条 contribution，身份只可能由 host 逐条绑定（见 `withExtensionApi`，与 `api`
   * 同一机制）。插件手上那一份是零参数的 `openDialog()`，它造不出别人的认领。
   *
   * ⚠ 键是 **contribution id，不是 plugin id**：同一个插件可以注册多条 contribution
   * （不同 slot、甚至同一 slot 里的两个入口），按 plugin 判会让它们**同时**看到
   * `dialog.open === true`、一起挂出弹窗。contribution id 由 `defineWorkspaceUiRegistry`
   * 保证全局唯一，正好是"那一格归谁"的粒度。
   *
   * 它只是**提出请求**：核心的 root-dialog 裁决可能拒绝（当前没有 workspace owner），
   * 也可能随后把它关掉（另一个 primary 弹窗打开、切库、换用户）。插件唯一的真相来源
   * 是 `context.dialog`，不是自己的一个 `useState`。
   */
  openDialog(contributionId: string): void;
  /**
   * 关掉**自己**的弹窗。同样收 `contributionId` 且同样由 host 逐条绑定，理由与
   * `openDialog` 对称但更硬：插件留着一个旧回调（`setTimeout`、迟到的请求回调、一个
   * 没卸载干净的组件）时，那一格可能已经易主，一次迟到的 `closeDialog()` 会把**别人**
   * 刚开的弹窗关掉。壳层因此按**当时**的持有者判，非持有者静默返回。省略 `reason`
   * 等同 `"button"`。
   */
  closeDialog(contributionId: string, reason?: WorkspaceExtensionDialogCloseReason): void;
}>;

/**
 * 查询串只能经这个字段走：`extensionApiPath` 拒绝路径里出现 `?`，所以插件没有
 * 第二条拼查询的路子，而拼接由 `URLSearchParams` 统一编码。
 */
export type ExtensionApiQuery = Readonly<Record<string, string | number | boolean>>;

/**
 * 插件能对一次请求说的**全部**话。刻意没有 `tag`/`auth`/`unauthorized`/`credentials`/
 * `mode`：那几项是核心的鉴权与诊断口径，由 `createWorkspaceExtensionApi` 固定。
 */
export type ExtensionRequestInit = Readonly<{
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  body?: string | FormData;
  headers?: Readonly<Record<string, string>>;
  signal?: AbortSignal;
  query?: ExtensionApiQuery;
}>;

/**
 * 插件唯一的 HTTP 出口，按 `pluginId` 绑定，路径恒在 `/extensions/<plugin id>/` 之下。
 *
 * `userMessage` 不是便利函数而是必需品：`errors-guard` 是精确计数普查，读
 * `error.message` 必须登记在公网仓库的清单里，仓库外插件登记不进去。
 */
export type WorkspaceExtensionApi = Readonly<{
  requestJson<T>(path: string, init?: ExtensionRequestInit): Promise<T>;
  requestVoid(path: string, init?: ExtensionRequestInit): Promise<void>;
  requestBlob(path: string, init?: ExtensionRequestInit): Promise<Blob>;
  userMessage(error: unknown, fallback: string): string;
}>;

/**
 * 插件组件实际收到的 actions：宿主自己的窄 action 加上一份**按本 contribution 的
 * pluginId 绑定**的 api 端口。宿主侧持有的仍是 `WorkspaceExtensionActions`，
 * `api` 由 outlet 逐 contribution 注入——插件 A 因此拿不到插件 B 的端口。
 */
export type WorkspaceExtensionPluginActions = Readonly<
  Omit<WorkspaceExtensionActions, "openDialog" | "closeDialog">
  & {
    /**
     * 零参数：身份由 host 按 `contribution.id` 绑定。**不是**壳层那份
     * `openDialog(contributionId)` 的重载——插件 A 能替插件 B 认领唯一那格弹窗的话，
     * "一次一个插件弹窗"就成了一句谁都能替别人做主的话。
     */
    openDialog(): void;
    /** 同上：身份由 host 绑定，插件只说关不关、说不了关谁的。 */
    closeDialog(reason?: WorkspaceExtensionDialogCloseReason): void;
    api: WorkspaceExtensionApi;
  }
>;

export type WorkspaceExtensionProps = Readonly<{
  context: WorkspaceExtensionContext;
  actions: WorkspaceExtensionPluginActions;
}>;

export type WorkspaceUiContribution = Readonly<{
  id: string;
  pluginId: string;
  pluginVersion: string;
  capability: string;
  slot: WorkspaceExtensionSlot;
  permission: WorkspaceExtensionPermission;
  mode: WorkspaceExtensionModePolicy;
  Component: ComponentType<WorkspaceExtensionProps>;
}>;

export type SystemExtensionProjection = Readonly<{
  apiVersion: "1";
  extensions: readonly Readonly<{
    pluginId: string;
    displayName: string;
    version: string;
    contributionId: string;
    available: boolean;
    unavailableReason: "disabled" | "unavailable" | null;
  }>[];
}>;
