"use client";

import { useState } from "react";
import type { useCollabPulse } from "./use-collab-pulse";
import { CollabActivityPanel } from "./collab-activity-panel";

export type CollabBarProps = {
  view: ReturnType<typeof useCollabPulse>["view"];
  onRefresh: () => Promise<void>;
  notebookId: string;
  canReadActivity: boolean;
  sessionSignal: AbortSignal;
};

export function CollabBar({ view, onRefresh, notebookId, canReadActivity, sessionSignal }: CollabBarProps) {
  const [activityOpen, setActivityOpen] = useState(false);
  if (!view.enabled) return null;
  const writers = view.viewers.filter((person) => ["owner", "admin", "editor", "edit"].includes(person.role));
  return <section className="collab-bar" aria-label="协作动态">
    <div className="collab-bar-line">
      {view.unavailable ? <span role="status">协作同步暂不可用</span> : <span>正在查看：{writers.length ? writers.map((person) => person.display_name || "协作者").join("、") : "暂无其他编辑者"}{view.otherReaderCount > 0 ? ` · 另有 ${view.otherReaderCount} 位读者` : ""}</span>}
      {canReadActivity && <button type="button" className="link-button inline-action" aria-expanded={activityOpen} onClick={() => setActivityOpen((open) => !open)}>{activityOpen ? "收起最近改动" : "最近改动"}</button>}
    </div>
    {view.pending.length > 0 && <p>有新的改动 · <button type="button" className="link-button inline-action" disabled={view.busy} onClick={() => { void onRefresh(); }}>{view.busy ? "正在刷新…" : "刷新"}</button></p>}
    {view.notice && <p role="status">{view.notice}</p>}
    {activityOpen && canReadActivity && <CollabActivityPanel notebookId={notebookId} sessionSignal={sessionSignal} />}
  </section>;
}
