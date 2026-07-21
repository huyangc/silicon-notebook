import { requestJson } from "./api-client.ts";

export const MODEL_ROLES = ["llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"] as const;
export const STATUS_MODEL_ROLES = [...MODEL_ROLES, "embedding"] as const;
export type ModelRole = (typeof MODEL_ROLES)[number];
export type StatusModelRole = (typeof STATUS_MODEL_ROLES)[number];
export type ModelFailureCode = "missing_config" | "upstream_error";

export const MODEL_ROLE_LABELS: Record<StatusModelRole, string> = {
  llm: "主模型",
  reasoning_llm: "推理模型",
  rewrite_llm: "改写模型",
  kg_llm: "构图模型",
  rerank: "重排模型",
  embedding: "嵌入模型",
};

export type ModelServiceView = {
  base_url: string; model: string; has_key: boolean; key_hint: string; source: string;
};
export type ModelSettingsView = Record<ModelRole, ModelServiceView>;

export type ModelServiceStatusItem = {
  service: StatusModelRole;
  model: string;
  source: "user" | "system" | "none";
  kind: "llm" | "rerank" | "embedding";
  configured: boolean;
  required: boolean;
  status: "ok" | "error" | "untested" | "unconfigured";
  latency_ms: number;
  checked_at: string;
  trigger: "manual_test" | "observed_failure" | "";
  code: string;
};

export type ModelServicesStatus = { services: ModelServiceStatusItem[] };

// 表单态：每个可编辑字段单独记录是否改动；未改动字段 PUT 时省略。
export type ServiceForm = {
  base_url: string;
  model: string;
  api_key: string;
  baseUrlDirty: boolean;
  modelDirty: boolean;
  keyDirty: boolean;
};

export type ModelServicePatch = Partial<{
  base_url: string;
  model: string;
  api_key: string;
}>;

export type ModelSettingsPatch = Partial<Record<ModelRole, ModelServicePatch>>;

export function modelSettingsForms(
  view: ModelSettingsView,
): Record<ModelRole, ServiceForm> {
  return Object.fromEntries(MODEL_ROLES.map((role) => [role, {
    base_url: view[role].base_url,
    model: view[role].model,
    api_key: "",
    baseUrlDirty: false,
    modelDirty: false,
    keyDirty: false,
  }])) as Record<ModelRole, ServiceForm>;
}

export function buildPutPayload(
  forms: Record<ModelRole, ServiceForm>,
): ModelSettingsPatch {
  const out: ModelSettingsPatch = {};
  for (const role of MODEL_ROLES) {
    const f = forms[role];
    const svc: ModelServicePatch = {};
    if (f.baseUrlDirty) svc.base_url = f.base_url.trim();
    if (f.modelDirty) svc.model = f.model.trim();
    if (f.keyDirty) svc.api_key = f.api_key;   // 改动了才发；"" 表示清除
    if (Object.keys(svc).length > 0) out[role] = svc;
  }
  return out;
}

export async function fetchModelSettings(): Promise<ModelSettingsView> {
  return requestJson("/me/model-settings", { tag: "model-settings" });
}

/** Read the persisted snapshot only; this endpoint never probes a provider. */
export async function fetchModelServiceStatus(): Promise<ModelServicesStatus> {
  return requestJson("/me/model-services/status", { tag: "model-settings" });
}

/** Explicitly probe the effective saved configuration for one service. */
export async function testCurrentModelService(
  service: StatusModelRole,
): Promise<ModelServiceStatusItem> {
  return requestJson(`/me/model-services/${service}/test`, {
    method: "POST",
    tag: "model-settings",
  });
}

/** Explicitly probe every configured effective service. */
export async function testAllCurrentModelServices(): Promise<ModelServicesStatus> {
  return requestJson("/me/model-services/test-all", {
    method: "POST",
    tag: "model-settings",
  });
}

export async function saveModelSettings(payload: ReturnType<typeof buildPutPayload>): Promise<ModelSettingsView> {
  return requestJson("/me/model-settings", {
    method: "PUT",
    body: JSON.stringify(payload),
    tag: "model-settings",
  });
}

export async function testModelService(
  service: ModelRole, base_url: string, model: string, api_key: string | null,
): Promise<{ ok: boolean; latency_ms: number; error: string; code: string }> {
  const result = await requestJson<{
    ok?: unknown; latency_ms?: unknown; error?: unknown; code?: unknown;
  }>("/me/model-settings/test", {
    method: "POST",
    body: JSON.stringify({ service, base_url, model, api_key }),
    tag: "model-settings",
  });
  const code = typeof result.code === "string"
    && ["", "unknown_service", "missing_config", "upstream_error"].includes(result.code)
    ? result.code
    : "";
  return {
    ok: result.ok === true,
    latency_ms: typeof result.latency_ms === "number" && Number.isFinite(result.latency_ms)
      ? result.latency_ms
      : 0,
    error: "",
    code,
  };
}

export type ModelServicesSummary = {
  text: string;
  tone: "ok" | "warn" | "bad";
  abnormal: ModelServiceStatusItem[];
};

/**
 * Collection health is based on persisted state. Optional services that have
 * never been configured are informational, not failures.
 */
export function summarizeModelServices(
  services: ModelServiceStatusItem[],
): ModelServicesSummary {
  const abnormal = services.filter((service) =>
    service.status === "error" || (service.required && !service.configured)
  );
  if (abnormal.length > 0) {
    return {
      text: `API 正常 · ${abnormal.length} 个模型异常`,
      tone: "bad",
      abnormal,
    };
  }
  if (services.some((service) => service.configured && service.status === "untested")) {
    return { text: "API 正常 · 模型待测试", tone: "warn", abnormal: [] };
  }
  return { text: "服务正常", tone: "ok", abnormal: [] };
}

export function modelFailureText(
  error: Pick<ModelServiceStatusItem, "service" | "model"> & {
    message?: ModelFailureCode;
    code?: string;
  },
): string {
  const role = MODEL_ROLE_LABELS[error.service];
  const model = error.model.trim();
  if (model) return `${role} ${model} 调用失败，本次回答可能不完整。`;
  const code = error.message ?? error.code;
  return code === "missing_config"
    ? `${role}尚未配置，本次回答可能不完整。`
    : `${role}调用失败，本次回答可能不完整。`;
}

/** Replace rows by their protocol-stable service role without mutating state. */
export function mergeModelServiceStatus(
  current: ModelServicesStatus | null,
  replacement: ModelServiceStatusItem | ModelServicesStatus,
): ModelServicesStatus {
  const updates = "services" in replacement ? replacement.services : [replacement];
  const byService = new Map(updates.map((item) => [item.service, item]));
  const services = (current?.services ?? []).map((item) => byService.get(item.service) ?? item);
  const existing = new Set(services.map((item) => item.service));
  for (const item of updates) {
    if (!existing.has(item.service)) services.push(item);
  }
  return { services };
}
