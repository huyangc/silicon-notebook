"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { COLLAB_CHANGE_NOTICE_MS } from "./collab-refresh";

const documentVisible = () => document.visibilityState !== "hidden";

/** A mounted domain decides whether refreshing would disturb one of its drafts. */
export function useCollabPanelRefresh(revision: number, safe: boolean, refresh: () => Promise<boolean>) {
  const effects = useRef({ safe, refresh });
  const owner = useRef<object | null>(null);
  const lastRevision = useRef(revision);
  const pendingRevision = useRef<number | null>(null);
  const inFlight = useRef(false);
  const automaticPending = useRef(false);
  useEffect(() => {
    effects.current = { safe, refresh };
    if (!safe) automaticPending.current = false;
  });
  const [pending, setPending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  useEffect(() => {
    const identity = {};
    owner.current = identity;
    return () => { if (owner.current === identity) owner.current = null; };
  }, []);
  const run = useCallback(async () => {
    const identity = owner.current;
    if (!identity || inFlight.current || pendingRevision.current === null) return;
    if (!effects.current.safe) {
      automaticPending.current = false;
      setNotice("请结束编辑后再刷新");
      return;
    }
    if (!documentVisible()) return;
    inFlight.current = true;
    setBusy(true);
    setNotice("");
    try {
      while (owner.current === identity && pendingRevision.current !== null
        && effects.current.safe && documentVisible()) {
        const requested: number = pendingRevision.current;
        const success = await effects.current.refresh();
        if (owner.current !== identity) return;
        if (!success) {
          automaticPending.current = false;
          setNotice("刷新没成功，请重试");
          break;
        }
        if (pendingRevision.current === requested) {
          pendingRevision.current = null;
          automaticPending.current = false;
          setPending(false);
          setNotice("已刷新");
          break;
        }
        // A new revision arrived during the successful read. Only drain that
        // distinct work while the same visible owner still has no draft;
        // failure and an editing pause always require a later explicit retry.
        if (!automaticPending.current) break;
      }
    } catch {
      if (owner.current === identity) {
        automaticPending.current = false;
        setNotice("刷新没成功，请重试");
      }
    } finally {
      if (owner.current === identity) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  }, []);
  useEffect(() => {
    if (revision === lastRevision.current) return;
    lastRevision.current = revision;
    pendingRevision.current = revision;
    automaticPending.current = effects.current.safe;
    setPending(true);
    if (effects.current.safe) void run();
  }, [revision, run]);
  useEffect(() => {
    const resume = () => {
      if (documentVisible() && automaticPending.current && effects.current.safe) void run();
    };
    document.addEventListener("visibilitychange", resume);
    return () => document.removeEventListener("visibilitychange", resume);
  }, [run]);
  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(""), COLLAB_CHANGE_NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);
  return { pending, busy, notice, refresh: run };
}
