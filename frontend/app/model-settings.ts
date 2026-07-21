import { requestJson } from "./api-client.ts";

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
  return requestJson("/me/model-settings", { tag: "model-settings" });
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
  // 两个通道在类型上就分开:error = 诊断(只进 console),code = 稳定枚举,
  // 文案由前端 vocabulary.ts 的 MODEL_TEST_ERROR 提供。调用点想拿 error 上屏会显得刺眼。
): Promise<{ ok: boolean; latency_ms: number; error: string; code: string }> {
  return requestJson("/me/model-settings/test", {
    method: "POST",
    body: JSON.stringify({ service, base_url, model, api_key }),
    tag: "model-settings",
  });
}
