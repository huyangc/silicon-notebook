"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export const sourceSidebarHintKey = (userId: string) => `sn.sourcesHintSeen.${userId}`;

export function SourceSidebarHint({ userId, active, onCollapse }: {
  userId: string; active: boolean; onCollapse: () => void;
}) {
  const key = sourceSidebarHintKey(userId);
  const [pending, setPending] = useState(false);
  const seen = useRef(false);
  useEffect(() => {
    seen.current = false;
    try { setPending(window.localStorage.getItem(key) !== "1"); }
    catch { setPending(true); }
  }, [key]);

  const dismiss = useCallback(() => {
    try { window.localStorage.setItem(key, "1"); } catch { /* Private browsing may disallow storage. */ }
    setPending(false);
  }, [key]);

  const show = active && pending;
  useEffect(() => {
    if (show) seen.current = true;
    else if (seen.current && pending) dismiss();
  }, [show, pending, dismiss]);

  if (!show) return null;
  return <aside className="source-sidebar-hint" role="note" aria-label="来源栏提示">
    <p><strong>给阅读多留一点空间</strong>可以<button className="link-button inline-action" onClick={onCollapse}>收起来源栏</button>，再点展开按钮还原。也可以向左拖动右侧边缘缩小，拖到最边上就会收起。</p>
    <button className="source-sidebar-hint-dismiss" onClick={dismiss}>知道了</button>
  </aside>;
}
