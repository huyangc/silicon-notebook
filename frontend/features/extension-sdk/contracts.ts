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

export type WorkspaceExtensionPermissionSnapshot = Readonly<{
  notebookRead: boolean;
  notebookWrite: boolean;
  notebookConfigure: boolean;
  sourceRead: boolean;
  sourceWrite: boolean;
  systemAdmin: boolean;
}>;

export type WorkspaceExtensionContext = Readonly<{
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
export type WorkspaceExtensionPluginActions = WorkspaceExtensionActions & Readonly<{
  api: WorkspaceExtensionApi;
}>;

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
