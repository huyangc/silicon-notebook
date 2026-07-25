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

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const fetchHealth = () => requestJson<Health>("/health", options);

export const fetchDocumentTypes = () =>
  requestJson<Array<{ id: string; label: string }>>("/doc-types", options);

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
