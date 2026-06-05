export type { Summary } from "./format";

export type Facets = { kinds: string[]; statuses: string[]; models: string[] };

export type Stats = {
  total: number;
  filtered: number;
  by_kind: Record<string, number>;
  by_status: Record<string, number>;
  by_model: Record<string, number>;
  total_tokens: number;
  latency_ms: { avg: number; max: number };
  malformed_lines: number;
  facets: Facets;
};

export type ChannelInfo = { name: string; file: string; exists: boolean; count: number };
export type ChannelsResponse = { channels: ChannelInfo[] };

import type { Summary } from "./format";
export type ListResponse = {
  channel: string;
  file_exists: boolean;
  records: Summary[];
  stats: Stats;
  has_more: boolean;
  newest_seq: number | null;
};

export type Message = { role: string; content: string };
export type FullRecord = {
  seq: number;
  id: string;
  ts: string;
  kind: string;
  model: string;
  status: string;
  latency_ms?: number;
  usage?: { prompt_tokens?: number; completion_tokens?: number; total_tokens?: number };
  request?: { messages?: Message[]; schema_hint?: string };
  response?: { content?: string };
  input_chars?: number;
  dims?: number;
  error?: string;
  attempt?: number;
  [k: string]: unknown;
};
