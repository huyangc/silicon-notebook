import type { ChannelsResponse, FullRecord, ListResponse } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.clone().json();
      detail = (body && (body.detail || body.message)) || "";
    } catch {
      detail = (await res.text().catch(() => "")) || "";
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` - ${detail}` : ""}`);
  }
  return res.json();
}

export function fetchChannels(): Promise<ChannelsResponse> {
  return get<ChannelsResponse>(`/debug/logs`);
}

export type RecordQuery = {
  limit?: number;
  before?: number;
  since?: number;
  kind?: string;
  status?: string;
  model?: string;
  q?: string;
};

export function fetchRecords(channel: string, params: RecordQuery): Promise<ListResponse> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<ListResponse>(`/debug/logs/${channel}${suffix}`);
}

export function fetchRecord(channel: string, id: string): Promise<FullRecord> {
  return get<FullRecord>(`/debug/logs/${channel}/${encodeURIComponent(id)}`);
}
