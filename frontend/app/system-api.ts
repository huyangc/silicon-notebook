import { performApiRequest, requestJson } from "./api-client.ts";
import { logDiagnostic } from "./errors.ts";
import type { Health } from "./workspace-model.ts";
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
  "pdf", "md", "markdown", "docx", "pptx", "csv", "xlsx", "xlsm",
];

const PARSER_IDS = new Set(["mineru_self_hosted", "mineru_cloud", "builtin"]);
const PARSER_EXECUTIONS = new Set(["local", "private_service", "public_cloud"]);
const PARSER_CAPABILITIES = new Set([
  "structured_text", "headings", "layout", "tables", "formulas", "images", "ocr",
]);
const PARSER_UNAVAILABLE_REASONS = new Set([
  "disabled", "missing_endpoint", "missing_credentials",
]);

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
  const supportedExtensions = normalizedExtensions(record.supported_source_extensions)
    ?? DEFAULT_SUPPORTED_SOURCE_EXTENSIONS;
  return {
    source_upload_max_bytes: limit,
    source_upload_max_files_per_batch: batchFiles,
    supported_source_extensions: supportedExtensions,
    parser_engines: parseParserEngines(record.parser_engines),
    report_max_sections: reportMaxSections,
    report_max_subqueries_per_section: reportMaxSubqueries,
    user_activity_view_enabled: activityViewEnabled === true,
  };
}

/** Authenticated, deliberately small mirror of browser-relevant backend Settings. */
export const fetchSystemConfiguration = async (): Promise<SystemConfiguration> =>
  parseSystemConfiguration(await requestJson<unknown>("/system/config", options));

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
