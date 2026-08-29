// `GET /admin/extensions` 的前端适配:把后端的只读运维视图翻成页面用的形状。
//
// 解析**刻意比 `system-api.ts::parseSystemExtensions` 宽**,两者服务的消费者不同:
//   · 那一份喂的是渲染管线(哪个界面入口可以挂出来),一条读不懂的行会让宿主渲染出
//     一个语义不明的按钮,所以它对整份响应 fail closed(抛 TypeError)。
//   · 这一份喂的是**运维只读清单**。管理员打开它,往往正是因为部署侧刚出了状况;
//     此时因为某一行多了个未知字段就把整页换成一句「格式无效」,恰好在最需要它的
//     时刻把唯一的可见性拿走。所以这里逐行降级:读不懂的字段给安全默认,认不出
//     身份(缺 `id`)的行丢掉,其余照常显示。
//
// 同理 `api_version` 不一致**不是致命错误**:后端比页面新时,已知字段大概率仍然
// 读得懂,页面照常渲染并在顶部挂一条提示,由人判断要不要信。把它当致命错误等于
// 让一次灰度发布把整页变成白屏。
import { requestJson } from "../../api-client.ts";

/** 本页面已知的接口版本。后端声明别的值时只提示,不拒绝渲染。 */
export const KNOWN_API_VERSION = "1";

export type ExtensionTrust = "builtin" | "deployment" | "unknown";

/** 服务端扩展点上的一条接入声明。 */
export type LoadedContribution = Readonly<{
  id: string;
  /** 稳定扩展点 id(如 `retrieval.contributor`)。原样显示,不翻译。 */
  point: string;
  /** 封闭枚举(provider / provider_chain / contributor / observer)。 */
  kind: string;
}>;

/** 界面槽位上的一条接入声明。 */
export type LoadedUiContribution = Readonly<{
  id: string;
  /** 稳定槽位 id(如 `workspace.side_panel`)。 */
  slot: string;
  /** 稳定能力名。 */
  capability: string;
}>;

export type LoadedExtension = Readonly<{
  id: string;
  displayName: string;
  version: string;
  trust: ExtensionTrust;
  contributions: readonly LoadedContribution[];
  uiContributions: readonly LoadedUiContribution[];
  // 运行时开关现状——另一层,来自 `extension_runtime_toggles` 表,不是启动冻结的
  // 装载拓扑。builtin 恒为 null(只读,不可开关)。deployment 行认不出/缺失时也
  // 退回 null,渲染侧按「无法确认→当启用处理」兜底(与后端「无行=启用」同精神),
  // 不把一个解析失败误判成「已停用」。
  runtimeEnabled: boolean | null;
  /** 上次改动这个开关的管理员 user id,原样显示,不翻译。 */
  runtimeUpdatedBy: string | null;
  /** 上次改动时间,ISO 字符串。SQLite/PG 两种形状都交给 `new Date()` 解析,
   *  这里只负责透传,不做字符串切片。 */
  runtimeUpdatedAt: string | null;
}>;

export type LoadedExtensionTopology = Readonly<{
  /** 后端声明的接口版本;读不到时为空串。 */
  apiVersion: string;
  /** 与 `KNOWN_API_VERSION` 一致。不一致只提示,内容照常渲染。 */
  versionRecognized: boolean;
  extensions: readonly LoadedExtension[];
}>;

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asList(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

// 后端 `trust` 是 Literal["builtin", "deployment"];认不出的值归 "unknown",由
// 渲染侧走中性兜底词,而不是把后端的原始串直接上屏。
function asTrust(value: unknown): ExtensionTrust {
  return value === "builtin" || value === "deployment" ? value : "unknown";
}

function asOptionalBool(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function asOptionalText(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function parseContribution(value: unknown): LoadedContribution | null {
  const row = asRecord(value);
  const id = asText(row?.id);
  if (!id) return null; // 无身份的接入行没法解释,丢掉好过显示一行空白
  return { id, point: asText(row?.point), kind: asText(row?.kind) };
}

function parseUiContribution(value: unknown): LoadedUiContribution | null {
  const row = asRecord(value);
  const id = asText(row?.id);
  if (!id) return null;
  return { id, slot: asText(row?.slot), capability: asText(row?.capability) };
}

function parseExtension(value: unknown): LoadedExtension | null {
  const row = asRecord(value);
  const id = asText(row?.id);
  if (!id) return null;
  return {
    id,
    displayName: asText(row?.display_name),
    version: asText(row?.version),
    trust: asTrust(row?.trust),
    contributions: asList(row?.contributions)
      .map(parseContribution)
      .filter((item): item is LoadedContribution => item !== null),
    uiContributions: asList(row?.ui_contributions)
      .map(parseUiContribution)
      .filter((item): item is LoadedUiContribution => item !== null),
    runtimeEnabled: asOptionalBool(row?.runtime_enabled),
    runtimeUpdatedBy: asOptionalText(row?.runtime_updated_by),
    runtimeUpdatedAt: asOptionalText(row?.runtime_updated_at),
  };
}

/** 防御性解析:未知字段忽略,缺字段给安全默认,认不出身份的行丢掉。绝不抛。 */
export function parseLoadedExtensions(value: unknown): LoadedExtensionTopology {
  const body = asRecord(value);
  const apiVersion = asText(body?.api_version);
  return {
    apiVersion,
    versionRecognized: apiVersion === KNOWN_API_VERSION,
    extensions: asList(body?.extensions)
      .map(parseExtension)
      .filter((item): item is LoadedExtension => item !== null),
  };
}

export async function fetchLoadedExtensions(): Promise<LoadedExtensionTopology> {
  return parseLoadedExtensions(
    await requestJson<unknown>("/admin/extensions", { tag: "admin" }),
  );
}

// --- PATCH /admin/extensions/{plugin_id}:运行时开关写入 --------------------

export type ExtensionRuntimeUpdate = Readonly<{
  pluginId: string;
  runtimeEnabled: boolean;
  runtimeUpdatedBy: string;
  runtimeUpdatedAt: string;
}>;

/** 写响应的形状由后端 `AdminExtensionRuntimeResult` 冻结(四个字段全部必填),
 *  但仍按本文件一贯的防御性风格解析:一次写请求成功与否由 HTTP 状态码判断
 *  (失败已经在 `requestJson` 里抛成人话错误),这里只负责把 200 的正文兜住,
 *  不因为响应体形状意外就让调用方拿到 `undefined.something`。 */
function parseExtensionRuntimeUpdate(value: unknown, pluginId: string, enabled: boolean): ExtensionRuntimeUpdate {
  const row = asRecord(value);
  return {
    pluginId: asText(row?.plugin_id) || pluginId,
    runtimeEnabled: typeof row?.runtime_enabled === "boolean" ? row.runtime_enabled : enabled,
    runtimeUpdatedBy: asText(row?.runtime_updated_by),
    runtimeUpdatedAt: asText(row?.runtime_updated_at),
  };
}

/** 开/关一个已装载的部署插件。非 admin/未知/builtin id 由后端 403/404,经
 *  `requestJson` 统一抛成人话错误——调用方(页面)按行捕获,不发全局横幅。 */
export async function setExtensionRuntimeEnabled(
  pluginId: string,
  enabled: boolean,
): Promise<ExtensionRuntimeUpdate> {
  const body = await requestJson<unknown>(`/admin/extensions/${encodeURIComponent(pluginId)}`, {
    tag: "admin",
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
  return parseExtensionRuntimeUpdate(body, pluginId, enabled);
}
