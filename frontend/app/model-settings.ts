import { API_BASE, authHeaders } from "./auth.ts";
import { throwHumanizedHttpError } from "./errors.ts";

export const MODEL_ROLES = ["llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank"] as const;
export type ModelRole = (typeof MODEL_ROLES)[number];

export type ModelServiceView = {
  base_url: string; model: string; has_key: boolean; key_hint: string; source: string;
};
export type ModelSettingsView = Record<ModelRole, ModelServiceView>;

// 表单态：api_key 用单独的「已改动」标记，未改动则 PUT 时省略以保留原 key。
export type ServiceForm = { base_url: string; model: string; api_key: string; keyDirty: boolean };

export function buildPutPayload(forms: Record<ModelRole, ServiceForm>) {
  const out: Record<string, { base_url: string; model: string; api_key?: string }> = {};
  for (const role of MODEL_ROLES) {
    const f = forms[role];
    const svc: { base_url: string; model: string; api_key?: string } = {
      base_url: f.base_url.trim(), model: f.model.trim(),
    };
    if (f.keyDirty) svc.api_key = f.api_key;   // 改动了才发；"" 表示清除
    out[role] = svc;
  }
  return out;
}

export async function fetchModelSettings(): Promise<ModelSettingsView> {
  const res = await fetch(`${API_BASE}/me/model-settings`, { headers: authHeaders() });
  if (!res.ok) await throwHumanizedHttpError(res, "model-settings");
  return res.json();
}

export async function saveModelSettings(payload: ReturnType<typeof buildPutPayload>): Promise<ModelSettingsView> {
  const res = await fetch(`${API_BASE}/me/model-settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(payload),
  });
  if (!res.ok) await throwHumanizedHttpError(res, "model-settings");
  return res.json();
}

export async function testModelService(
  service: ModelRole, base_url: string, model: string, api_key: string | null,
  // 两个通道在类型上就分开:error = 诊断(只进 console),user_message = 后端
  // 盖章的用户文案。调用点想拿 error 上屏会显得刺眼。
): Promise<{ ok: boolean; latency_ms: number; error: string; user_message: string }> {
  const res = await fetch(`${API_BASE}/me/model-settings/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ service, base_url, model, api_key }),
  });
  if (!res.ok) await throwHumanizedHttpError(res, "model-settings");
  return res.json();
}
