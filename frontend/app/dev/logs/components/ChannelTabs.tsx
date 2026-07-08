"use client";
import type { ChannelInfo } from "../types";

const ORDER = ["llm", "events", "requests"];

function formatBytes(bytes?: number): string {
  if (bytes === undefined || bytes === null) return "";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(0)} KB`;
}

export function ChannelTabs({
  channels,
  active,
  onSelect,
}: {
  channels: ChannelInfo[];
  active: string;
  onSelect: (name: string) => void;
}) {
  const sorted = [...channels].sort((a, b) => ORDER.indexOf(a.name) - ORDER.indexOf(b.name));
  return (
    <div className="logview-tabs">
      {sorted.map((ch) => {
        const disabled = ch.name !== "llm"; // v1: only LLM is interactive
        return (
          <button
            key={ch.name}
            className={`logview-tab${ch.name === active ? " active" : ""}${disabled ? " disabled" : ""}`}
            disabled={disabled}
            title={disabled ? "v1 仅支持 LLM 通道" : ""}
            onClick={() => !disabled && onSelect(ch.name)}
          >
            {ch.name.toUpperCase()} <span className="tab-count">{formatBytes(ch.bytes)}</span>
          </button>
        );
      })}
    </div>
  );
}
