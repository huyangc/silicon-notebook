"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getCollabActivity, type CollabActivity } from "./collab-api";
import { COLLAB_CHANGE_NOTICE_MS, COLLAB_KIND_LABELS } from "./collab-refresh";
import { toUserMessage } from "./errors";

export function CollabActivityPanel({ notebookId, sessionSignal }: { notebookId: string; sessionSignal: AbortSignal }) {
  const [items, setItems] = useState<CollabActivity[]>([]);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [message, setMessage] = useState("");
  const [messageIssue, setMessageIssue] = useState(0);
  const ownerRef = useRef<object | null>(null);
  const inFlight = useRef<AbortController | null>(null);
  const load = useCallback(async (owner: object, explicit: boolean) => {
    if (ownerRef.current !== owner || sessionSignal.aborted || inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setBusy(true);
    setMessage("");
    const abort = () => controller.abort();
    sessionSignal.addEventListener("abort", abort);
    try {
      const result = await getCollabActivity(notebookId, controller.signal);
      if (ownerRef.current === owner && !sessionSignal.aborted) {
        setItems(result.items);
        setLoaded(true);
        setUnavailable(false);
        if (explicit) { setMessage("已刷新"); setMessageIssue((issue) => issue + 1); }
      }
    } catch (error) {
      if (ownerRef.current === owner && !controller.signal.aborted && !sessionSignal.aborted) {
        setUnavailable(true);
        setMessage(toUserMessage(error, "最近改动未能读取，请重试"));
        setMessageIssue((issue) => issue + 1);
      }
    } finally {
      sessionSignal.removeEventListener("abort", abort);
      if (inFlight.current === controller) inFlight.current = null;
      if (ownerRef.current === owner && !sessionSignal.aborted) setBusy(false);
    }
  }, [notebookId, sessionSignal]);
  useEffect(() => {
    const owner = {};
    ownerRef.current = owner;
    setItems([]);
    setLoaded(false);
    setUnavailable(false);
    void load(owner, false);
    return () => {
      if (ownerRef.current === owner) ownerRef.current = null;
      inFlight.current?.abort();
      inFlight.current = null;
    };
  }, [load]);
  useEffect(() => {
    if (!message) return;
    const timer = setTimeout(() => setMessage(""), COLLAB_CHANGE_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [message, messageIssue]);
  return <section className="collab-activity" aria-label="最近改动">
    <div className="collab-bar-line"><strong>最近改动</strong><button type="button" className="link-button inline-action" disabled={busy || sessionSignal.aborted} onClick={() => { if (ownerRef.current) void load(ownerRef.current, true); }}>{busy ? "正在读取…" : "刷新改动"}</button>{message && <span role="status">{message}</span>}</div>
    {unavailable && <p>最近改动暂不可用</p>}
    {!busy && loaded && !unavailable && items.length === 0 && <p>暂无最近改动</p>}
    <ul>{items.map((item) => <li key={item.id}><span>{item.actor_name || "协作者"}更新了{COLLAB_KIND_LABELS[item.kind] ?? "内容"}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString("zh-CN")}</time></li>)}</ul>
  </section>;
}
