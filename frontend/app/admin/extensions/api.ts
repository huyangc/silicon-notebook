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
