import { requestJson } from "./api-client.ts";


export type ModelServiceStatusItem = {
  service_id: string;
  display_name: string;
  kind: "chat" | "embedding" | "rerank";
  model: string;
  workloads: Array<{ id: string; label: string }>;
  status: "untested" | "ok" | "busy" | "error" | "circuit_open" | "half_open";
  active: number;
  maximum: number;
  queued: number;
  oldest_wait_ms: number;
  latency_ms: number;
  checked_at: string;
  trigger: string;
  code: string;
  support_id: string;
};

export type ModelServicesStatus = { services: ModelServiceStatusItem[] };

export type ModelFailure = {
  service_id: string;
  service_name?: string;
  workload_id?: string;
  workload_label?: string;
  stage?: string;
  model?: string;
  message?: string;
  support_id?: string;
};

export type ModelServicesSummary = {
  text: string;
  tone: "ok" | "warn" | "bad";
  abnormal: ModelServiceStatusItem[];
};


const SERVICE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const SUPPORT_ID = /^mdl-[A-Za-z0-9_-]{1,80}$/;
const KINDS = new Set(["chat", "embedding", "rerank"]);
const STATUSES = new Set(["untested", "ok", "busy", "error", "circuit_open", "half_open"]);
const TRIGGERS = new Set(["", "manual_test", "observed_failure", "recovery_probe"]);


function invalidStatus(): never {
  throw new TypeError("模型服务状态格式无效");
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return invalidStatus();
  return value as Record<string, unknown>;
}

function stringScalar(value: unknown, maximum: number): string {
  if (typeof value !== "string" || value.length > maximum) return invalidStatus();
  return value;
}

function optionalString(value: unknown, maximum: number): string {
  if (value === undefined || value === null) return "";
  return stringScalar(value, maximum);
}

function nonnegativeInteger(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) return invalidStatus();
  return value;
}

function safeDisplayName(value: unknown): string {
  if (typeof value !== "string") return "";
  const normalized = value.trim().replace(/\s+/g, " ");
  if (!normalized || normalized.length > 80) return "";
  const lowered = normalized.toLowerCase();
  if (
    /[a-z][a-z0-9+.-]*:\/\//i.test(normalized)
    || /(?:\d{1,3}\.){3}\d{1,3}/.test(normalized)
    || /(?:authorization|bearer|apikey|secret|token|password|credential)/i.test(lowered)
  ) return "";
  return normalized;
}

function safeModel(value: unknown): string {
  if (typeof value !== "string") return "";
  const model = value.trim();
  if (!model || model.length > 128 || !/^[\p{L}\p{N}][\p{L}\p{N}._/@+:-]*$/u.test(model)) return "";
  if (/[:/]\//.test(model) || /(?:secret|token|password|credential)/i.test(model)) return "";
  return model;
}

export function sanitizeModelServiceId(value: unknown): string {
  if (typeof value !== "string") return "";
  const candidate = value.trim();
  return SERVICE_ID.test(candidate) ? candidate : "";
}

export function sanitizeModelSupportId(value: unknown): string {
  if (typeof value !== "string") return "";
  const candidate = value.trim();
  return SUPPORT_ID.test(candidate) ? candidate : "";
}

function parseWorkload(value: unknown): { id: string; label: string } {
  const item = record(value);
  const id = stringScalar(item.id, 64).trim();
  if (!SERVICE_ID.test(id)) return invalidStatus();
  return { id, label: safeDisplayName(item.label) };
}

export function parseModelServiceStatusItem(value: unknown): ModelServiceStatusItem {
  const item = record(value);
  const serviceId = sanitizeModelServiceId(stringScalar(item.service_id, 64));
  const kind = stringScalar(item.kind, 16);
  const status = stringScalar(item.status, 24);
  const trigger = optionalString(item.trigger, 32);
  if (!SERVICE_ID.test(serviceId) || !KINDS.has(kind) || !STATUSES.has(status) || !TRIGGERS.has(trigger)) {
    return invalidStatus();
  }
  if (!Array.isArray(item.workloads)) return invalidStatus();
  const rawSupportId = optionalString(item.support_id, 84);
  const supportId = sanitizeModelSupportId(rawSupportId);
  if (rawSupportId.trim() && !supportId) return invalidStatus();
  return {
    service_id: serviceId,
    display_name: safeDisplayName(item.display_name),
    kind: kind as ModelServiceStatusItem["kind"],
    model: safeModel(item.model),
    workloads: item.workloads.map(parseWorkload),
    status: status as ModelServiceStatusItem["status"],
    active: nonnegativeInteger(item.active),
    maximum: nonnegativeInteger(item.maximum),
    queued: nonnegativeInteger(item.queued),
    oldest_wait_ms: nonnegativeInteger(item.oldest_wait_ms),
    latency_ms: nonnegativeInteger(item.latency_ms),
    checked_at: optionalString(item.checked_at, 64),
    trigger,
    code: optionalString(item.code, 64),
    support_id: supportId,
  };
}

function parseModelServicesStatus(value: unknown): ModelServicesStatus {
  const snapshot = record(value);
  if (!Array.isArray(snapshot.services)) return invalidStatus();
  const services = snapshot.services.map(parseModelServiceStatusItem);
  if (new Set(services.map((item) => item.service_id)).size !== services.length) return invalidStatus();
  return { services };
}

export async function fetchModelServiceStatus(): Promise<ModelServicesStatus> {
  return parseModelServicesStatus(await requestJson<unknown>(
    "/model-services/status", { tag: "model-services" },
  ));
}

export async function testSystemModelService(serviceId: string): Promise<ModelServiceStatusItem> {
  const safeServiceId = sanitizeModelServiceId(serviceId);
  if (!safeServiceId) return invalidStatus();
  return parseModelServiceStatusItem(await requestJson<unknown>(
    `/admin/model-services/${encodeURIComponent(safeServiceId)}/test`,
    { method: "POST", tag: "model-services" },
  ));
}

export async function testAllSystemModelServices(): Promise<ModelServicesStatus> {
  return parseModelServicesStatus(await requestJson<unknown>(
    "/admin/model-services/test-all", { method: "POST", tag: "model-services" },
  ));
}

export function modelServiceDisplayName(
  item: Pick<ModelServiceStatusItem, "display_name">,
): string {
  return safeDisplayName(item.display_name) || "模型服务";
}

export function modelFailureText(error: ModelFailure): string {
  const serviceName = safeDisplayName(error.service_name) || "模型服务";
  const model = safeModel(error.model);
  return model
    ? `${serviceName} ${model} 调用失败，本次回答可能不完整。`
    : `${serviceName}调用失败，本次回答可能不完整。`;
}

export function summarizeModelServices(services: ModelServiceStatusItem[]): ModelServicesSummary {
  const abnormal = services.filter((item) =>
    item.status === "error" || item.status === "circuit_open" || item.status === "half_open"
  );
  if (abnormal.length > 0) {
    return { text: `API 正常 · ${abnormal.length} 个模型异常`, tone: "bad", abnormal };
  }
  const busy = services.filter((item) => item.status === "busy");
  if (busy.length > 0) {
    return { text: `API 正常 · ${busy.length} 个模型繁忙`, tone: "warn", abnormal: [] };
  }
  if (services.some((item) => item.status === "untested")) {
    return { text: "API 正常 · 模型待检查", tone: "warn", abnormal: [] };
  }
  return { text: "服务正常", tone: "ok", abnormal: [] };
}

export function mergeModelServiceStatus(
  current: ModelServicesStatus | null,
  replacement: ModelServiceStatusItem | ModelServicesStatus,
): ModelServicesStatus {
  const updates = "services" in replacement ? replacement.services : [replacement];
  const byId = new Map(updates.map((item) => [item.service_id, item]));
  const services = (current?.services ?? []).map((item) => byId.get(item.service_id) ?? item);
  const existing = new Set(services.map((item) => item.service_id));
  for (const item of updates) if (!existing.has(item.service_id)) services.push(item);
  return { services };
}
