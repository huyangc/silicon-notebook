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
 * 允许的相对路径形状：一段或多段 `/<字母数字或下划线起头的段>`，可带一个结尾 `/`。
 *
 * 段首字符类含下划线（`[A-Za-z0-9_]`）——RESTful 资源名常见 `import_selected` 这类
 * 蛇形命名，段内字符类本就含 `_`（见下），段首若不放行会把整个蛇形命名段落
 * 挡在外面（当它是路径唯一一段、且以下划线开头时）。字符类本身是承重的那一半——
 * 它同时挡掉 `..` 与 `.` 段（路径穿越）、`//`（协议相对 URL）与空段。其余禁止字符
 * 是**不出现在字符类里**带来的：反斜杠、空白、`?`、`#`、`%`（因此 `%2e%2e` 这类
 * 编码穿越也过不去，查询串只能走 `query`）。
 */
const RELATIVE_PATH = /^(?:\/[A-Za-z0-9_][A-Za-z0-9._~-]*)+\/?$/;
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
 * 插件绝不许自己设的请求头，按 WHATWG 归一（小写）后比对。
 *
 * `authorization`：核心只在**有 token 时**才 `set` 它（`authHeaders()` 无 token 返回
 * 空对象），所以「核心的鉴权头会盖掉插件的」只在登录态成立——未登录、token 刚被清掉
 * 或 `auth` 口径将来放宽时，插件伪造的 `Authorization` 会**原样发出去**。
 * `cookie`：核心一次都不碰它，浏览器把它列为 forbidden header name（fetch 静默丢弃），
 * 但那道保护是宿主环境给的、不是我们这边的判据。
 * 两条都在这里剥掉，让「插件设不了鉴权凭据」变成无条件成立的性质。
 */
const FORBIDDEN_HEADERS = ["authorization", "cookie"];


/**
 * 把插件那份窄 init 翻译成核心 api 客户端的 options。
 *
 * **只逐字段挑 4 项，绝不 spread 插件给的对象。** `tag`/`auth`/`unauthorized` 在最后
 * 写死不是排版问题：把 init spread 在它们之后，插件就能覆盖 `unauthorized`（关掉
 * 401 清 token 重载）或伪造 `tag`（把自己的失败记进核心的诊断口径）。TypeScript 挡得住
 * 老实的调用方，挡不住运行时多塞的键，所以这里是运行时判据。
 *
 * `headers` 先经 `new Headers(...)` 归一再删掉 `FORBIDDEN_HEADERS`——归一必须用
 * `Headers` 而不是手写 `toLowerCase()` 过滤：它就是 `api-client` 随后要做的同一次
 * 归一（`new Headers(inputHeaders)`），所以"这里删掉的"与"核心会看到的"逐字是同一组
 * 名字，大小写变体与重复键都不会漏。归一后转回普通对象是为了让跨端口的边界保持
 * 纯数据（核心自己会再包一次 `Headers`）；副作用是名字变成小写形态，HTTP 头名本就
 * 大小写不敏感。
 */
function coreOptions(init: ExtensionRequestInit | undefined) {
  const { method, body, headers, signal } = init ?? {};
  return {
    ...(method === undefined ? {} : { method }),
    ...(body === undefined ? {} : { body }),
    ...(headers === undefined ? {} : { headers: safeHeaders(headers) }),
    ...(signal === undefined ? {} : { signal }),
    tag: "extension",
    auth: "required" as const,
    unauthorized: "clear-and-reload" as const,
  };
}


function safeHeaders(headers: Readonly<Record<string, string>>): Record<string, string> {
  const normalized = new Headers(headers);
  for (const name of FORBIDDEN_HEADERS) normalized.delete(name);
  return Object.fromEntries(normalized);
}


/**
 * 默认 transport 的端口按 `pluginId` 记忆，让 `actions.api` 跨渲染保持**引用相等**。
 *
 * 为什么这是安全的：端口的四个方法只闭包 `pluginId` 与 `transport` 两个值——不读
 * owner、notebook、actor 或任何请求期状态，所以同一个 `pluginId` 的端口在任何时刻
 * 行为逐字相同，缓存**不改变任何权限语义**（授权由后端加这里的路径限定承担，owner
 * 闸挂在 `actions` 的两个窄 command 上、那两个仍是每帧新造）。
 *
 * 为什么有界：唯一的生产调用点是 `withExtensionApi(actions, contribution.pluginId)`，
 * 而 contribution 来自构建期冻结的 registry（`defineWorkspaceUiRegistry`），是一个
 * 有限集合。显式传 `transport` 的调用（只有测试）**不进缓存**：那些替身 transport
 * 各不相同，按 `pluginId` 记忆会把上一个用例的替身发给下一个。
 *
 * 为什么需要：插件把 `actions.api` 放进 `useEffect`/`useMemo` 依赖数组是最自然的写法，
 * 每帧新对象会让那种 effect 每帧重跑（本仓库 PR #557 踩过同形态的无限循环）。
 */
const DEFAULT_TRANSPORT_PORTS = new Map<string, WorkspaceExtensionApi>();


export function createWorkspaceExtensionApi(
  pluginId: string,
  transport: ExtensionApiTransport = EXTENSION_API_TRANSPORT,
): WorkspaceExtensionApi {
  if (transport !== EXTENSION_API_TRANSPORT) return buildWorkspaceExtensionApi(pluginId, transport);
  const cached = DEFAULT_TRANSPORT_PORTS.get(pluginId);
  if (cached !== undefined) return cached;
  const port = buildWorkspaceExtensionApi(pluginId, transport);
  DEFAULT_TRANSPORT_PORTS.set(pluginId, port);
  return port;
}


function buildWorkspaceExtensionApi(
  pluginId: string,
  transport: ExtensionApiTransport,
): WorkspaceExtensionApi {
  // 三个请求方法一律 `async`。**这不是排版偏好**：`extensionApiPath` 与 `safeHeaders`
  // 都在碰 transport 之前**同步**抛 TypeError，非 async 的方法会把那次拒绝原样同步
  // 抛出调用点——而插件最自然的写法 `api.requestVoid(p).catch(show)` 或
  // `void api.requestJson(p).catch(...)` 接不住同步异常：`.catch` 还没被求值，异常就
  // 已经从 `api.requestVoid(p)` 那一步炸出去了，落成一次未处理的运行时错误（在
  // React 事件处理器里就是整棵子树的错误边界）。`async` 把它翻成 rejection，`await`
  // 与 `.catch()` 两种写法就都接得住。
  //
  // 路径闸的语义**一个字没变**：拒绝仍发生在 transport 之前，被拒的请求一个字节都
  // 到不了网络（单测按 `calls.length` 钉住这一点）。变的只是它以哪种形式抵达调用方。
  //
  // `userMessage` 刻意保持同步：它返回一句给用户看的文案，插件要在渲染/setState 里
  // 原地用它（`setError(api.userMessage(e, "…"))`），翻成 Promise 会把每个消费点都
  // 逼成异步，而它根本不做 I/O、也没有会抛的闸。
  return Object.freeze({
    async requestJson<T>(path: string, init?: ExtensionRequestInit): Promise<T> {
      return transport.requestJson<T>(
        extensionApiPath(pluginId, path, init?.query),
        coreOptions(init),
      );
    },
    async requestVoid(path: string, init?: ExtensionRequestInit): Promise<void> {
      return transport.requestVoid(
        extensionApiPath(pluginId, path, init?.query),
        coreOptions(init),
      );
    },
    async requestBlob(path: string, init?: ExtensionRequestInit): Promise<Blob> {
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
