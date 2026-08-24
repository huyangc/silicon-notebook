import { performApiRequest, requestJson } from "./api-client.ts";
import { logDiagnostic } from "./errors.ts";
import type { Health } from "./workspace-model.ts";
import type { SystemExtensionProjection } from "../features/extension-sdk/contracts.ts";
import {
  DEFAULT_REPORT_MAX_SECTIONS,
  DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION,
} from "./report-outline-model.ts";

export type ReadySnapshot = {
  ready: boolean;
  phase:
    | "starting"
    | "migrating"
    | "warming"
    | "preloading_indexes"
    | "ready"
    | "error";
  detail?: string;
  warmed_notebooks?: number;
  total_notebooks?: number;
  preloaded_indexes?: number;
  total_indexes?: number;
  error?: string | null;
};

export type SystemConfiguration = {
  /** Parsed backend Settings value; use this exact byte limit for file picking. */
  source_upload_max_bytes: number;
  /** Fixed multipart resource guard published by the backend. */
  source_upload_max_files_per_batch: number;
  /** Formats accepted by the upload API, ordered for display. */
  supported_source_extensions: string[];
  /** Sanitized, ordered automatic parser strategy; never contains endpoints or credentials. */
  parser_engines: ParserEngineCapability[];
  /** Backend/API rail for editable retrieval directions in one report section. */
  report_max_sections: number;
  report_max_subqueries_per_section: number;
  /** /dev/logs 的能力位:后端 USER_ACTIVITY_VIEW_ENABLED 是否开启「活动」tab。
   *  旧后端可能不下发这个字段——缺失或类型不对时按 `true` 处理(后端默认就是开
   *  的,不该在新前端 + 旧后端组合下把一个其实可用的视图藏掉)。 */
  user_activity_view_enabled: boolean;
  /** 部署的单图字节上限(镜像 MINERU_MAX_IMAGE_BYTES),供压缩包/文件夹上传的图片
   *  配对预检。旧后端不下发时为 `null`——与 `source_upload_max_bytes` 缺失时一样,
   *  含义是「拿不到这个上限,不做本地预检,由服务端护栏兜底」,不能猜一个假上限。
   *  `0` 是**合法值**,语义与 `null` 相反:一张都不持久化,等效于图片存储关闭。 */
  source_image_max_bytes: number | null;
  /** 部署的每来源图片张数上限(镜像 MINERU_MAX_IMAGES_PER_SOURCE)。同上,缺失即
   *  `null` = 不做本地预检,`0` = 一张都不持久化。 */
  source_image_max_per_source: number | null;
  /** 部署级图片存储总开关(镜像 MINERU_RETURN_IMAGES)。缺失(旧后端)按 `true`
   *  处理——这是能力位不是校验值,旧后端从未关闭过这个开关,方向必须是「不凭空弹
   *  警告」而不是「不确定就当关闭」。 */
  source_images_enabled: boolean;
  /** 「AI 对这个库的理解」入口的能力位,直接反映后端 AGENT_PROFILE_ENABLED。
   *  与四个理解端点同一批上线,缺失(旧后端)按 `false` 处理——不同于上面那个
   *  字段:这里不存在「端点已经在、字段还没下发」的组合,缺字段就是这个后端
   *  压根没有这个特性,渲染入口只会打开一个整片 404 的面板。 */
  agent_profile_enabled: boolean;
  /** 「我的回答偏好」入口的能力位（账户菜单），直接反映后端
   *  Settings.user_search_profile_enabled。缺失(旧后端)按 **false** 处理——与
   *  `agent_profile_enabled` 同一条论证(codex #535 R1 P2 订正了最初的反向
   *  设定):这个字段与 `PATCH /me/search-profile` 端点是同一批新增的,缺字段
   *  可靠地说明这个后端**没有那个端点**,按 true 渲染入口只会让保存打出裸 404
   *  而不是承诺过的 409 文案;「部署默认开启」描述的是新后端的 Settings 默认值,
   *  救不了一个路由都不存在的旧后端。 */
  user_search_profile_enabled: boolean;
};

export type ParserEngineCapability = {
  id: "mineru_self_hosted" | "mineru_cloud" | "builtin";
  priority: number;
  execution: "local" | "private_service" | "public_cloud";
  file_extensions: string[];
  capabilities: Array<
    "structured_text" | "headings" | "layout" | "tables" | "formulas" | "images" | "ocr"
  >;
  supports_url: boolean;
  fallback: boolean;
  available: boolean;
  unavailable_reason: "disabled" | "missing_endpoint" | "missing_credentials" | null;
};

export const DEFAULT_SUPPORTED_SOURCE_EXTENSIONS = [
  "pdf", "md", "markdown", "zip", "docx", "pptx", "csv", "xlsx", "xlsm", "xls",
];

const EXTENSION_UNAVAILABLE_REASONS = new Set(["disabled", "unavailable"]);
const STABLE_EXTENSION_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;

export function parseSystemExtensions(value: unknown): SystemExtensionProjection {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("系统扩展能力格式无效");
  }
  const response = value as Record<string, unknown>;
  if (
    Object.keys(response).some((key) => !["api_version", "extensions"].includes(key))
    || response.api_version !== "1"
    || !Array.isArray(response.extensions)
  ) {
    throw new TypeError("系统扩展能力格式无效");
  }
  const rows: SystemExtensionProjection["extensions"][number][] = [];
  const exactKeys = new Set<string>();
  for (const item of response.extensions) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new TypeError("系统扩展能力格式无效");
    }
    const row = item as Record<string, unknown>;
    const reason = row.unavailable_reason;
    const knownKeys = [
      "plugin_id", "display_name", "version", "contribution_id",
      "available", "unavailable_reason",
    ];
    if (
      Object.keys(row).some((key) => !knownKeys.includes(key))
      || typeof row.plugin_id !== "string" || !STABLE_EXTENSION_ID.test(row.plugin_id)
      || typeof row.display_name !== "string" || row.display_name.length === 0
      || typeof row.version !== "string" || row.version.length === 0
      || typeof row.contribution_id !== "string" || !STABLE_EXTENSION_ID.test(row.contribution_id)
      || typeof row.available !== "boolean"
      || !(reason === null || (
        typeof reason === "string" && EXTENSION_UNAVAILABLE_REASONS.has(reason)
      ))
      || (row.available ? reason !== null : reason === null)
    ) throw new TypeError("系统扩展能力格式无效");
    const exactKey = `${row.plugin_id}\0${row.version}\0${row.contribution_id}`;
    if (exactKeys.has(exactKey)) throw new TypeError("系统扩展能力格式无效");
    exactKeys.add(exactKey);
    rows.push({
      pluginId: row.plugin_id,
      displayName: row.display_name,
      version: row.version,
      contributionId: row.contribution_id,
      available: row.available,
      unavailableReason: reason as "disabled" | "unavailable" | null,
    });
  }
  return { apiVersion: "1", extensions: rows };
}

const PARSER_IDS = new Set(["mineru_self_hosted", "mineru_cloud", "builtin"]);
const PARSER_EXECUTIONS = new Set(["local", "private_service", "public_cloud"]);
const PARSER_CAPABILITIES = new Set([
  "structured_text", "headings", "layout", "tables", "formulas", "images", "ocr",
]);
const PARSER_UNAVAILABLE_REASONS = new Set([
  "disabled", "missing_endpoint", "missing_credentials",
]);

/** 非负整数**保真**,否则 `null`("拿不到这个值,不做本地预检")。
 *
 *  `0` 必须原样保留,不能折成 `null`。`MINERU_MAX_IMAGE_BYTES=0` /
 *  `MINERU_MAX_IMAGES_PER_SOURCE=0` 是合法部署值(后端转发这两个字段时刻意没有正数
 *  约束,有后端用例钉住),语义是「一张图都不持久化」——与「拿不到上限」恰恰相反。
 *  折成 `null` 会让打包上传管线按「无上限」照常 base64 内联并报「N 张已内联」,而
 *  服务端把这些资产全部丢弃(codex #518 R1 P2)。零值的有效关闭态由
 *  `bundle-intake.ts` 的 `bundleImagesEffectivelyEnabled` 推导。
 *
 *  负数/非整数/缺失仍归 `null`:那些是坏值,不是可执行的配置。 */
function nonNegativeIntOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function normalizedExtensions(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length === 0) return null;
  const extensions = value.filter((item): item is string => (
    typeof item === "string" && /^[a-z0-9]+$/.test(item)
  ));
  return extensions.length === value.length ? extensions : null;
}

function parseParserEngines(value: unknown): ParserEngineCapability[] {
  if (!Array.isArray(value)) return [];
  const rows: ParserEngineCapability[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const row = item as Record<string, unknown>;
    const extensions = normalizedExtensions(row.file_extensions);
    const capabilities = Array.isArray(row.capabilities)
      ? row.capabilities.filter((capability): capability is ParserEngineCapability["capabilities"][number] => (
          typeof capability === "string" && PARSER_CAPABILITIES.has(capability)
        ))
      : [];
    const unavailableReason = row.unavailable_reason;
    if (
      typeof row.id !== "string" || !PARSER_IDS.has(row.id)
      || typeof row.priority !== "number" || !Number.isSafeInteger(row.priority) || row.priority <= 0
      || typeof row.execution !== "string" || !PARSER_EXECUTIONS.has(row.execution)
      || !extensions
      || capabilities.length !== (Array.isArray(row.capabilities) ? row.capabilities.length : -1)
      || typeof row.supports_url !== "boolean"
      || typeof row.fallback !== "boolean"
      || typeof row.available !== "boolean"
      || !(unavailableReason === null || (
        typeof unavailableReason === "string" && PARSER_UNAVAILABLE_REASONS.has(unavailableReason)
      ))
    ) return [];
    rows.push({
      id: row.id as ParserEngineCapability["id"],
      priority: row.priority,
      execution: row.execution as ParserEngineCapability["execution"],
      file_extensions: extensions,
      capabilities,
      supports_url: row.supports_url,
      fallback: row.fallback,
      available: row.available,
      unavailable_reason: unavailableReason as ParserEngineCapability["unavailable_reason"],
    });
  }
  return rows.sort((a, b) => a.priority - b.priority);
}

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const fetchHealth = () => requestJson<Health>("/health", options);

export const fetchDocumentTypes = () =>
  requestJson<Array<{ id: string; label: string }>>("/doc-types", options);

function parseSystemConfiguration(value: unknown): SystemConfiguration {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError("系统上传配置格式无效");
  }
  const record = value as Record<string, unknown>;
  const limit = record.source_upload_max_bytes;
  const batchFiles = record.source_upload_max_files_per_batch;
  const reportSections = record.report_max_sections;
  const reportSubqueries = record.report_max_subqueries_per_section;
  if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit <= 0) {
    throw new TypeError("系统上传配置格式无效");
  }
  if (typeof batchFiles !== "number" || !Number.isSafeInteger(batchFiles) || batchFiles <= 0) {
    throw new TypeError("系统上传配置格式无效");
  }
  const reportMaxSections = (
    typeof reportSections === "number"
    && Number.isSafeInteger(reportSections)
    && reportSections > 0
  ) ? reportSections : DEFAULT_REPORT_MAX_SECTIONS;
  const reportMaxSubqueries = (
    typeof reportSubqueries === "number"
    && Number.isSafeInteger(reportSubqueries)
    && reportSubqueries > 0
  ) ? reportSubqueries : DEFAULT_REPORT_MAX_SUBQUERIES_PER_SECTION;
  // 缺失(旧后端)或类型不符一律按 **false** 处理:这是能力位不是配置项,所以是安全
  // 默认而非校验失败,不走前两个字段那样的抛错路径。
  //
  // 方向不能反过来。这个字段与三个活动端点是**同一次改动一起上线的**,因此
  // 「字段缺失」恰恰是「这个后端没有活动视图」的可靠信号——不存在「字段缺失但端点
  // 存在」的组合。映射成 true 只会让新前端配旧后端时默认打开一个请求全 404 的
  // tab,与开关显式关闭时的失败形态一字不差。
  const activityViewEnabled = record.user_activity_view_enabled;
  // 同样缺失按 false:这个字段与四个理解端点是同一批新增的,不存在「后端已经有
  // 端点、字段却没下发」的组合——缺字段可靠地说明这个后端根本没有这个特性。
  const agentProfileEnabled = record.agent_profile_enabled;
  // 同样缺失按 false(见上面 SystemConfiguration.user_search_profile_enabled
  // 的字段注释):字段与端点同批新增,缺字段=旧后端没有那个路由。
  const searchProfileEnabled = record.user_search_profile_enabled;
  const supportedExtensions = normalizedExtensions(record.supported_source_extensions)
    ?? DEFAULT_SUPPORTED_SOURCE_EXTENSIONS;
  // 图片护栏值缺失(旧后端)一律按 `null` = 「拿不到这个上限,不做本地预检」,与
  // `source_upload_max_bytes` 缺失时的既有口径同一方向(md-bundle.ts 的
  // `resolveLimit` 就是这份契约在纯函数层的镜像)。显式下发的 `0` 不属于这一类:
  // 它是「一张都不存」的合法配置,必须保真(见 `nonNegativeIntOrNull`)。
  const imageMaxBytes = nonNegativeIntOrNull(record.source_image_max_bytes);
  const imageMaxPerSource = nonNegativeIntOrNull(record.source_image_max_per_source);
  // 缺字段(旧后端)按 `true` 处理:这个开关此前从不存在,不能让新前端凭空对旧
  // 后端的正常部署弹出一条「图片不会被保存」的警告。只有服务端显式给出 `false`
  // 才关闭本地内联与提示。
  const imagesEnabled = record.source_images_enabled;
  return {
    source_upload_max_bytes: limit,
    source_upload_max_files_per_batch: batchFiles,
    supported_source_extensions: supportedExtensions,
    parser_engines: parseParserEngines(record.parser_engines),
    report_max_sections: reportMaxSections,
    report_max_subqueries_per_section: reportMaxSubqueries,
    user_activity_view_enabled: activityViewEnabled === true,
    source_image_max_bytes: imageMaxBytes,
    source_image_max_per_source: imageMaxPerSource,
    source_images_enabled: imagesEnabled !== false,
    agent_profile_enabled: agentProfileEnabled === true,
    user_search_profile_enabled: searchProfileEnabled === true,
  };
}

/** Authenticated, deliberately small mirror of browser-relevant backend Settings. */
export const fetchSystemConfiguration = async (): Promise<SystemConfiguration> =>
  parseSystemConfiguration(await requestJson<unknown>("/system/config", options));

export const fetchSystemExtensions = async (): Promise<SystemExtensionProjection> =>
  parseSystemExtensions(await requestJson<unknown>("/system/extensions", options));

export async function probeReady(): Promise<ReadySnapshot | null> {
  try {
    const response = await performApiRequest("/ready", {
      auth: "none",
      tag: "ready",
      cache: "no-store",
    });
    let body: Partial<ReadySnapshot> | null = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const snapshot: ReadySnapshot = !response.ok || !body
      ? {
          ready: false,
          phase: (body?.phase as ReadySnapshot["phase"]) ?? "starting",
          detail: body?.detail,
          warmed_notebooks: body?.warmed_notebooks,
          total_notebooks: body?.total_notebooks,
          preloaded_indexes: body?.preloaded_indexes,
          total_indexes: body?.total_indexes,
          error: body?.error ?? null,
        }
      : body as ReadySnapshot;
    if (snapshot.error) logDiagnostic("ready", snapshot.error);
    return snapshot;
  } catch {
    return null;
  }
}
