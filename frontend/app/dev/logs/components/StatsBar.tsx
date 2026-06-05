"use client";
import type { Stats } from "../types";
import { formatTokens } from "../format";

export function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return <div className="logview-stats" />;
  const chip = (k: string, v: string | number) => (
    <span className="stat-chip" key={k}>
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </span>
  );
  return (
    <div className="logview-stats">
      {chip("总数", stats.total)}
      {chip("命中", stats.filtered)}
      {chip("ok", stats.by_status.ok ?? 0)}
      {chip("retry", stats.by_status.retry ?? 0)}
      {chip("error", stats.by_status.error ?? 0)}
      {chip("tokens", formatTokens(stats.total_tokens))}
      {chip("延迟avg", `${stats.latency_ms.avg}ms`)}
      {chip("延迟max", `${stats.latency_ms.max}ms`)}
      {stats.malformed_lines > 0 ? chip("坏行", stats.malformed_lines) : null}
    </div>
  );
}
