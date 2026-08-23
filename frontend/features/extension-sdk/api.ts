/**
 * 插件的 HTTP 端口：按 `pluginId` 绑定的窄 api，由 outlet 逐 contribution 注入。
 *
 * **本模块刻意不在插件 import 白名单里。** `createWorkspaceExtensionApi(pluginId)`
 * 只要能被插件自己调用，插件 A 就能给自己造一个绑定插件 B 的端口，"路径限定"当场
 * 失去意义。插件唯一拿得到端口的途径是 `actions.api`——那一份由 `host.tsx` 用
 * `contribution.pluginId` 构造，插件说了不算。白名单由
 * `tests/guards/extension-ui-boundary.test.mjs` 与 `extension-plugin-package-guard`
 * 两侧钉住，后者会点名拒绝本模块。
 *
 * 另外两条写给插件作者的限制，都是被现有全仓守卫决定的、不是风格偏好：
 *  · **插件不得读 `error.message`**（也不得 `new Error("中文…")`）。`errors-guard`
 *    是精确计数普查，每一处属性读取都要登记在公网仓库的 `APPROVED_*` 清单里，仓库外
 *    的插件包登记不进去。要给用户看的文案走 `api.userMessage(error, fallback)`。
 *  · **插件不得自己发请求**：`api-boundary` 守卫对任何模块的直接网络调用报红，这个
 *    端口是唯一出口。
 *
 * 本模块的整条 import 闭包必须是 `.ts`：`tests/unit/extension-api-port.test.mjs`
 * 在 node 泳道里直接 import 它，而 Node 对 `.tsx` 报 `Unknown file extension`。
 */
import { requestBlob, requestJson, requestVoid } from "../../app/api-client.ts";
import { toUserMessage } from "../../app/errors.ts";
import type {
  ExtensionApiQuery,
  ExtensionRequestInit,
  WorkspaceExtensionActions,
  WorkspaceExtensionApi,
  WorkspaceExtensionPluginActions,
} from "./contracts.ts";


/** 与 `registry.ts` 的 `STABLE_ID` 同一条正则：端口的前缀就是登记时那个 id。 */
const PLUGIN_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
/**
 * 允许的相对路径形状：一段或多段 `/<字母数字开头的段>`，可带一个结尾 `/`。
 *
 * 段首必须是 `[A-Za-z0-9]` 是承重的那一半——它同时挡掉 `..` 与 `.` 段（路径穿越）、
 * `//`（协议相对 URL）与空段。其余禁止字符是**不出现在字符类里**带来的：反斜杠、
 * 空白、`?`、`#`、`%`（因此 `%2e%2e` 这类编码穿越也过不去，查询串只能走 `query`）。
 */
const RELATIVE_PATH = /^(?:\/[A-Za-z0-9][A-Za-z0-9._~-]*)+\/?$/;
const CONFINED = "extension API requests must stay under /extensions/<plugin id>/";


export type ExtensionApiTransport = Readonly<{
  requestJson: typeof requestJson;
  requestVoid: typeof requestVoid;
  requestBlob: typeof requestBlob;
  toUserMessage: typeof toUserMessage;
}>;


/**
 * 端口默认使用的核心 transport。单独具名是为了让单测能在不碰网络的前提下断言
 * "路径与 init 长什么样"，同时**引用相等**地证明生产路径用的就是 `api-client`
 * 与 `errors` 里那几个函数，而不是某份影子实现。
 */
export const EXTENSION_API_TRANSPORT: ExtensionApiTransport = Object.freeze({
  requestJson,
  requestVoid,
  requestBlob,
  toUserMessage,
});


/**
 * 把插件给的相对路径解析成核心 api 客户端接受的路径，越界即抛。
 *
 * 两道闸各自有牙，缺一不可：正则决定"这个形状允许吗"，随后的前缀断言决定
 * "拼出来的东西真的落在本插件的前缀下吗"。举例：正则若被放宽到不再要求前导 `/`，
 * `"search"` 会拼成 `/extensions/<id>search`——那是**另一个插件的兄弟路径**，
 * 只有前缀断言拦得住它。
 */
export function extensionApiPath(
  pluginId: string,
  path: string,
  query?: ExtensionApiQuery,
): string {
  if (typeof pluginId !== "string" || !PLUGIN_ID.test(pluginId)) throw new TypeError(CONFINED);
  if (typeof path !== "string" || !RELATIVE_PATH.test(path)) throw new TypeError(CONFINED);
  const prefix = `/extensions/${pluginId}`;
  const resolved = `${prefix}${path}`;
  if (!resolved.startsWith(`${prefix}/`)) throw new TypeError(CONFINED);
  if (query === undefined) return resolved;
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query)) search.set(name, String(value));
  const encoded = search.toString();
  return encoded.length === 0 ? resolved : `${resolved}?${encoded}`;
}


/**
 * 把插件那份窄 init 翻译成核心 api 客户端的 options。
 *
 * **只逐字段挑 4 项，绝不 spread 插件给的对象。** `tag`/`auth`/`unauthorized` 在最后
 * 写死不是排版问题：把 init spread 在它们之后，插件就能覆盖 `unauthorized`（关掉
 * 401 清 token 重载）或伪造 `tag`（把自己的失败记进核心的诊断口径）。TypeScript 挡得住
 * 老实的调用方，挡不住运行时多塞的键，所以这里是运行时判据。
 *
 * `headers` 会被 `api-client` 的鉴权头覆盖（`auth: "required"` 时逐个 `set`），
 * 插件设不了 `Authorization`。
 */
function coreOptions(init: ExtensionRequestInit | undefined) {
  const { method, body, headers, signal } = init ?? {};
  return {
    ...(method === undefined ? {} : { method }),
    ...(body === undefined ? {} : { body }),
    ...(headers === undefined ? {} : { headers }),
    ...(signal === undefined ? {} : { signal }),
    tag: "extension",
    auth: "required" as const,
    unauthorized: "clear-and-reload" as const,
  };
}


export function createWorkspaceExtensionApi(
  pluginId: string,
  transport: ExtensionApiTransport = EXTENSION_API_TRANSPORT,
): WorkspaceExtensionApi {
  return Object.freeze({
    requestJson<T>(path: string, init?: ExtensionRequestInit): Promise<T> {
      return transport.requestJson<T>(
        extensionApiPath(pluginId, path, init?.query),
        coreOptions(init),
      );
    },
    requestVoid(path: string, init?: ExtensionRequestInit): Promise<void> {
      return transport.requestVoid(
        extensionApiPath(pluginId, path, init?.query),
        coreOptions(init),
      );
    },
    requestBlob(path: string, init?: ExtensionRequestInit): Promise<Blob> {
      return transport.requestBlob(
        extensionApiPath(pluginId, path, init?.query),
        coreOptions(init),
      );
    },
    userMessage(error: unknown, fallback: string): string {
      return transport.toUserMessage(error, fallback);
    },
  });
}


/**
 * 宿主侧的注入点：保留宿主 actions 上那道 exact-owner 闸（`openUnderstanding` 的
 * 闭包原样搬过来，不重新包一层），追加一份按 `pluginId` 绑定的 api。
 */
export function withExtensionApi(
  actions: WorkspaceExtensionActions,
  pluginId: string,
): WorkspaceExtensionPluginActions {
  return Object.freeze({
    ...actions,
    api: createWorkspaceExtensionApi(pluginId),
  });
}
