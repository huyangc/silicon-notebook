import type { ChannelsResponse, FullRecord, ListResponse } from "./types";
import { authHeaders } from "../../auth.ts";
import { throwHumanizedHttpError } from "../../errors.ts";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
  if (!res.ok) await throwHumanizedHttpError(res, "dev-logs");
  return res.json();
}

export function fetchChannels(owner?: string): Promise<ChannelsResponse> {
  const suffix = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return get<ChannelsResponse>(`/debug/logs${suffix}`);
}

export type RecordQuery = {
  limit?: number;
  before?: number;
  since?: number;
  kind?: string;
  status?: string;
  model?: string;
  q?: string;
  owner?: string;
  date?: string;
};

export function fetchRecords(channel: string, params: RecordQuery): Promise<ListResponse> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<ListResponse>(`/debug/logs/${channel}${suffix}`);
}

export function fetchDays(channel: string, owner?: string): Promise<{ channel: string; days: string[] }> {
  const suffix = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return get(`/debug/logs/${channel}/days${suffix}`);
}

export function fetchRecord(
  channel: string,
  id: string,
  date?: string,
  seq?: number,
): Promise<FullRecord> {
  const qs = new URLSearchParams();
  if (date) qs.set("date", date);
  if (seq !== undefined && seq !== null) qs.set("seq", String(seq));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return get<FullRecord>(`/debug/logs/${channel}/${encodeURIComponent(id)}${suffix}`);
}
