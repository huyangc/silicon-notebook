import type { useCollabPanelRefresh } from "./use-collab-panel-refresh";

export function CollabRefreshNotice({ refresh }: { refresh: ReturnType<typeof useCollabPanelRefresh> }) {
  if (!refresh.pending && !refresh.notice) return null;
  return <div className="collab-refresh-notice">
    {refresh.pending && <span>有新的改动 · <button type="button" className="link-button inline-action" disabled={refresh.busy} onClick={() => { void refresh.refresh(); }}>{refresh.busy ? "正在刷新…" : "刷新"}</button></span>}
    {refresh.notice && <span role="status">{refresh.notice}</span>}
  </div>;
}
