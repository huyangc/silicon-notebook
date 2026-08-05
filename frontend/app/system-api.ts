import { performApiRequest, requestJson } from "./api-client.ts";
import { logDiagnostic } from "./errors.ts";
import type { Health } from "./workspace-model.ts";

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
  /** /dev/logs 的能力位:后端 USER_ACTIVITY_VIEW_ENABLED 是否开启「活动」tab。
   *  旧后端可能不下发这个字段——缺失或类型不对时按 `true` 处理(后端默认就是开
   *  的,不该在新前端 + 旧后端组合下把一个其实可用的视图藏掉)。 */
  user_activity_view_enabled: boolean;
};

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
  if (typeof limit !== "number" || !Number.isSafeInteger(limit) || limit <= 0) {
    throw new TypeError("系统上传配置格式无效");
  }
  if (typeof batchFiles !== "number" || !Number.isSafeInteger(batchFiles) || batchFiles <= 0) {
    throw new TypeError("系统上传配置格式无效");
  }
  // 缺失(旧后端)或类型不符一律按 **false** 处理:这是能力位不是配置项,所以是安全
  // 默认而非校验失败,不走前两个字段那样的抛错路径。
  //
  // 方向不能反过来。这个字段与三个活动端点是**同一次改动一起上线的**,因此
  // 「字段缺失」恰恰是「这个后端没有活动视图」的可靠信号——不存在「字段缺失但端点
  // 存在」的组合。映射成 true 只会让新前端配旧后端时默认打开一个请求全 404 的
  // tab,与开关显式关闭时的失败形态一字不差。
  const activityViewEnabled = record.user_activity_view_enabled;
  return {
    source_upload_max_bytes: limit,
    source_upload_max_files_per_batch: batchFiles,
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
