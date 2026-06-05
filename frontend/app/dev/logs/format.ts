// Pure presentation helpers + the list-row Summary shape. Imported by both the
// React components (compiled by Next) and format.test.mjs (run by Node).

export type Summary = {
  seq: number;
  id: string;
  ts: string;
  kind: string;
  model: string;
  status: string;
  latency_ms: number | null;
  total_tokens: number | null;
  attempt: number | null;
  error: string | null;
  preview: string;
};

export function statusClass(status: string): string {
  if (status === "ok") return "ok";
  if (status === "retry") return "retry";
  if (status === "error") return "error";
  return "muted";
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  return `${(n / 1000).toFixed(1)}k`;
}

export function prettyJson(text: string): { pretty: string; ok: boolean } {
  try {
    return { pretty: JSON.stringify(JSON.parse(text), null, 2), ok: true };
  } catch {
    return { pretty: text, ok: false };
  }
}

export function shortId(id: string | null | undefined): string {
  return id ? id.replace(/^llm-/, "") : "—";
}
